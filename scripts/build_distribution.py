#!/usr/bin/env python3
"""Build immutable Setup Core packs for the current platform.

This is a release-time operation. User machines only download, verify, extract,
probe, and atomically activate the resulting zip files.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .pack_release import (
    PACK_IDS,
    asset_name,
    load_metadata,
    pack_tag,
)

ROOT = Path(__file__).resolve().parent.parent
POSIX_LOCK = ROOT / "registry" / "pack-build-posix.lock.json"
WINDOWS_LOCK = ROOT / "registry" / "pack-build-windows.lock.json"
REQUIREMENTS = ROOT / "registry" / "requirements"
OPENCLI_PACKAGE = ROOT / "registry" / "opencli" / "package.json"
OPENCLI_LOCK = ROOT / "registry" / "opencli" / "package-lock.json"
MAX_RELEASE_ASSET_SIZE = 2_147_483_648


class BuildError(RuntimeError):
    pass


def target() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    system = "darwin" if system == "darwin" else system
    architecture = (
        "arm64" if machine in {"arm64", "aarch64"}
        else "x64" if machine in {"x86_64", "amd64"}
        else ""
    )
    if system not in {"darwin", "linux", "windows"} or not architecture:
        raise BuildError(f"unsupported release target: {system}/{machine}")
    if system in {"linux", "windows"} and architecture != "x64":
        raise BuildError(f"unsupported release target: {system}/{architecture}")
    return system, architecture


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def installed_size(path: Path) -> int:
    with zipfile.ZipFile(path) as archive:
        return sum(item.file_size for item in archive.infolist())


def write_zip(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            materialized = path.resolve() if path.is_symlink() else path
            info = zipfile.ZipInfo(path.relative_to(source).as_posix())
            mode = 0o100755 if os.access(materialized, os.X_OK) else 0o100644
            info.external_attr = mode << 16
            archive.writestr(
                info,
                materialized.read_bytes(),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            )
    return destination


def extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        root = destination.resolve()
        for item in bundle.infolist():
            path = (destination / item.filename).resolve()
            if path != root and root not in path.parents:
                raise BuildError(f"unsafe archive member: {item.filename}")
        bundle.extractall(destination)
        if os.name != "nt":
            for item in bundle.infolist():
                if item.is_dir():
                    continue
                mode = (item.external_attr >> 16) & 0o777
                if mode:
                    (destination / item.filename).chmod(mode)


def extract_tar(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as bundle:
        bundle.extractall(destination, filter="data")


def copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=True)


def checked(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: int = 300,
) -> str:
    environment = (env or os.environ).copy()
    environment.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        raise BuildError(completed.stderr.strip() or f"postcheck failed: {argv[0]}")
    return completed.stdout.strip()


def download_verified(url: str, destination: Path, expected: str) -> Path:
    if not url.startswith("https://"):
        raise BuildError(f"refusing non-HTTPS build input: {url}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and sha256(destination) == expected:
        return destination
    temporary = destination.with_suffix(destination.suffix + ".part")
    digest = hashlib.sha256()
    request = urllib.request.Request(
        url, headers={"User-Agent": "my-llm-wiki-pack-builder/2"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as sink:
            for block in iter(lambda: response.read(1024 * 1024), b""):
                digest.update(block)
                sink.write(block)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    actual = digest.hexdigest()
    if actual != expected:
        temporary.unlink(missing_ok=True)
        raise BuildError(f"SHA-256 mismatch for {url}: expected {expected}, got {actual}")
    temporary.replace(destination)
    return destination


def pip_target(
    python: Path,
    destination: Path,
    requirements: Path,
    *,
    extra_index_url: str | None = None,
) -> None:
    if not requirements.is_file():
        raise BuildError(f"hashed requirements lock is missing: {requirements}")
    destination.mkdir(parents=True, exist_ok=True)
    command = [
        str(python), "-m", "pip", "install", "--disable-pip-version-check",
        "--no-input", "--no-compile", "--target", str(destination),
        "--require-hashes", "--requirement", str(requirements),
    ]
    if extra_index_url:
        command.extend(["--extra-index-url", extra_index_url])
    checked(command, timeout=3600)
    normalize_python_install(destination)
    shutil.copy2(requirements, destination / ".my-llm-wiki-requirements.lock")


def normalize_python_install(destination: Path) -> None:
    for cache in destination.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    for scripts in (destination / "bin", destination / "Scripts"):
        shutil.rmtree(scripts, ignore_errors=True)
    for record in destination.rglob("*.dist-info/RECORD"):
        rows = list(csv.reader(io.StringIO(record.read_text(encoding="utf-8"))))
        kept = []
        for row in rows:
            member = row[0].replace("\\", "/")
            if (
                "/__pycache__/" in member
                or member.endswith((".pyc", ".pyo"))
                or member.startswith(("../../bin/", "../../Scripts/"))
            ):
                continue
            kept.append(row)
        output = io.StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerows(kept)
        record.write_text(output.getvalue(), encoding="utf-8", newline="\n")


def requirements_lock(component: str, system: str, architecture: str) -> Path:
    return REQUIREMENTS / f"{component}-{system}-{architecture}.txt"


def python_env(site: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(site)
    return environment


def npm_lock_entry(package_lock: dict, package_name: str) -> dict:
    suffix = f"node_modules/{package_name}"
    matches = [
        value
        for key, value in (package_lock.get("packages") or {}).items()
        if key.replace("\\", "/").rstrip("/").endswith(suffix)
    ]
    if len(matches) != 1:
        raise BuildError(f"npm lock has {len(matches)} entries for {package_name}")
    return matches[0]


def install_posix_python(
    lock: dict, work: Path, system: str, architecture: str
) -> tuple[Path, Path]:
    install_dir = work / "python-runtime"
    version = lock["runtime"]["python"]
    target_spec = lock["runtime"]["targets"].get(f"{system}-{architecture}")
    if not target_spec:
        raise BuildError(f"Python runtime is not locked for {system}/{architecture}")
    archive = download_verified(
        target_spec["url"], work / "python-runtime.tar.gz", target_spec["sha256"]
    )
    extract_tar(archive, install_dir)
    candidates = sorted(
        install_dir.rglob(f"bin/python{'.'.join(version.split('.')[:2])}")
    )
    if len(candidates) != 1:
        raise BuildError("locked Python runtime layout is ambiguous")
    python = candidates[0]
    runtime = python.parent.parent
    if checked([str(python), "-c", "import platform; print(platform.python_version())"]) != version:
        raise BuildError("managed Python version differs from lock")
    try:
        checked([str(python), "-c", "import pip"])
    except BuildError:
        checked([str(python), "-m", "ensurepip", "--upgrade"])
    return runtime, python


def build_posix_documents(
    spec: dict, python: Path, work: Path, system: str, architecture: str
) -> Path:
    stage = work / "documents"
    site = stage / "site"
    pip_target(
        python, site, requirements_lock("documents", system, architecture)
    )
    runner = stage / "markitdown_runner.py"
    runner.write_text(
        "from pathlib import Path\nimport sys\n"
        "sys.path.insert(0, str(Path(__file__).with_name('site')))\n"
        "from markitdown.__main__ import main\nraise SystemExit(main())\n",
        encoding="utf-8",
    )
    checked([str(python), str(runner), "--help"])
    checked(
        [
            str(python),
            "-c",
            f"import sys; sys.path.insert(0, {str(site)!r}); "
            "import mammoth, openpyxl, pandas, pdfplumber, pptx",
        ]
    )
    return stage


def build_posix_video(
    spec: dict, python: Path, work: Path, system: str, architecture: str
) -> Path:
    stage = work / "video"
    pip_target(
        python,
        stage / "site",
        requirements_lock("video", system, architecture),
    )
    runner = stage / "yt_dlp_runner.py"
    runner.write_text(
        "from pathlib import Path\nimport os\nimport sys\n"
        "root = Path(__file__).parent\n"
        "os.environ['PATH'] = str(root) + os.pathsep + os.environ.get('PATH', '')\n"
        "sys.path.insert(0, str(Path(__file__).with_name('site')))\n"
        "from yt_dlp import main\n"
        "if '--ffmpeg-location' not in sys.argv:\n"
        "    sys.argv[1:1] = ['--ffmpeg-location', str(Path(__file__).with_name('ffmpeg'))]\n"
        "raise SystemExit(main())\n",
        encoding="utf-8",
    )
    environment = python_env(stage / "site")
    ffmpeg = Path(
        checked(
            [str(python), "-c", "from imageio_ffmpeg import get_ffmpeg_exe; print(get_ffmpeg_exe())"],
            env=environment,
        )
    )
    if not ffmpeg.is_file():
        raise BuildError("imageio-ffmpeg did not provide an executable")
    shutil.copy2(ffmpeg, stage / "ffmpeg")
    (stage / "ffmpeg").chmod(0o755)
    aria2 = build_posix_aria2(spec["aria2"], work, system)
    shutil.copy2(aria2, stage / "aria2c")
    (stage / "aria2c").chmod(0o755)
    checked([str(python), str(runner), "--version"])
    checked([str(stage / "ffmpeg"), "-version"])
    checked([str(stage / "aria2c"), "--version"])
    return stage


def build_posix_aria2(spec: dict, work: Path, system: str) -> Path:
    archive = download_verified(
        spec["url"], work / "aria2-source.tar.xz", spec["sha256"]
    )
    unpacked = work / "aria2-source"
    extract_tar(archive, unpacked)
    roots = [path for path in unpacked.iterdir() if path.is_dir()]
    if len(roots) != 1 or not (roots[0] / "configure").is_file():
        raise BuildError("unexpected aria2 source archive layout")
    install = work / "aria2-install"
    configure = [
        str(roots[0] / "configure"),
        f"--prefix={install}",
        "--disable-dependency-tracking",
        "--disable-nls",
        "--disable-bittorrent",
        "--disable-metalink",
        "--disable-websocket",
        "--without-libcares",
        "--without-sqlite3",
        "--without-libxml2",
        "--without-libexpat",
        "--without-libssh2",
        "--disable-shared",
    ]
    # Pin one TLS backend per platform so the build never silently falls back to
    # whatever headers the runner happens to carry: AppleTLS on macOS, OpenSSL
    # elsewhere.
    configure.append("--without-gnutls")
    if system == "darwin":
        configure.append("--without-openssl")
    else:
        configure.append("--without-appletls")
    checked(configure, cwd=roots[0], timeout=900)
    jobs = str(max(1, min(4, os.cpu_count() or 1)))
    checked(["make", f"-j{jobs}"], cwd=roots[0], timeout=3600)
    checked(["make", "install"], cwd=roots[0], timeout=900)
    executable = install / "bin" / "aria2c"
    banner = checked([str(executable), "--version"]).splitlines()
    if banner[0] != f"aria2 version {spec['version']}":
        raise BuildError("aria2 version differs from lock")
    features = next(
        (
            line.split(":", 1)[1]
            for line in banner
            if line.startswith("Enabled Features:")
        ),
        "",
    )
    # Without a TLS backend configure still succeeds and `--version` still
    # works; the binary just cannot fetch https media.
    if "HTTPS" not in {item.strip() for item in features.split(",")}:
        raise BuildError("aria2 was built without HTTPS support")
    return executable


def posix_asr_install(spec: dict, system: str) -> tuple[list[str], str | None]:
    platform_spec = spec.get(system) or {}
    packages = platform_spec.get("packages", spec["packages"])
    extra_index_url = platform_spec.get("extra_index_url")
    if any(package.count("==") != 1 for package in packages):
        raise BuildError("ASR packages must use exact pins")
    if extra_index_url and extra_index_url != "https://download.pytorch.org/whl/cpu":
        raise BuildError("unsupported ASR package index")
    return packages, extra_index_url


def build_posix_asr(
    name: str,
    spec: dict,
    python: Path,
    work: Path,
    system: str,
    architecture: str,
) -> Path:
    stage = work / name
    _, extra_index_url = posix_asr_install(spec, system)
    pip_target(
        python,
        stage / "site",
        requirements_lock(name, system, architecture),
        extra_index_url=extra_index_url,
    )
    checked(
        [str(python), *spec["postcheck"]],
        env=python_env(stage / "site"),
        timeout=900,
    )
    return stage


def build_posix_web(
    spec: dict, work: Path, system: str, architecture: str
) -> Path:
    stage = work / "web"
    stage.mkdir(parents=True)
    node_spec = spec["node"]
    target_spec = node_spec["targets"].get(f"{system}-{architecture}")
    if not target_spec:
        raise BuildError(f"Node runtime is not locked for {system}/{architecture}")
    archive = download_verified(
        target_spec["url"], work / "node-runtime.tar.gz", target_spec["sha256"]
    )
    unpacked = work / "node-runtime"
    extract_tar(archive, unpacked)
    nodes = sorted(unpacked.glob("*/bin/node"))
    if len(nodes) != 1:
        raise BuildError("unexpected Node archive layout")
    node = nodes[0]
    if checked([str(node), "--version"]).lstrip("v") != node_spec["version"]:
        raise BuildError("Node version differs from lock")
    shutil.copy2(node, stage / "node")
    (stage / "node").chmod(0o755)
    npm_cli = node.parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    if not npm_cli.is_file():
        raise BuildError("Node runtime has no npm CLI")
    opencli = stage / "opencli"
    opencli.mkdir(parents=True)
    shutil.copy2(OPENCLI_PACKAGE, opencli / "package.json")
    shutil.copy2(OPENCLI_LOCK, opencli / "package-lock.json")
    checked(
        [
            str(node), str(npm_cli), "ci", "--prefix", str(opencli), "--omit=dev",
            "--no-audit", "--no-fund", "--ignore-scripts",
        ],
        timeout=1800,
    )
    package_root = opencli / "node_modules" / "@jackwener" / "opencli"
    package = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    package_lock = json.loads((opencli / "package-lock.json").read_text(encoding="utf-8"))
    locked = npm_lock_entry(package_lock, "@jackwener/opencli")
    if package.get("version") != spec["opencli"]["version"] or locked.get("integrity") != spec["opencli"]["integrity"]:
        raise BuildError("OpenCLI package differs from lock")
    main = package_root / "dist" / "src" / "main.js"
    if spec["opencli"]["version"] not in checked([str(stage / "node"), str(main), "--version"]):
        raise BuildError("OpenCLI postcheck returned another version")
    archive = download_verified(
        spec["extension"]["url"], work / "extension.zip", spec["extension"]["sha256"]
    )
    unpacked = work / "extension-unpacked"
    extract(archive, unpacked)
    manifests = sorted(unpacked.rglob("manifest.json"))
    if not manifests:
        raise BuildError("Browser Bridge archive has no manifest")
    extension_root = min((path.parent for path in manifests), key=lambda row: len(row.parts))
    extension = json.loads((extension_root / "manifest.json").read_text(encoding="utf-8"))
    if extension.get("version") != spec["extension"]["version"]:
        raise BuildError("Browser Bridge version differs from lock")
    shutil.copytree(extension_root, stage / "extension")
    return stage


def validate_windows_lock(lock: dict) -> None:
    if lock.get("schema") != 1 or lock.get("architecture") != "x86_64":
        raise BuildError("unsupported Windows pack lock")
    python = lock.get("python") or {}
    direct = [
        python,
        lock["components"]["web"]["node"],
        lock["components"]["web"]["extension"],
        lock["components"]["video"]["yt_dlp"],
        lock["components"]["video"]["ffmpeg"],
        lock["components"]["video"]["aria2"],
    ]
    for source in direct:
        if not str(source.get("url", "")).startswith("https://") or not re.fullmatch(
            r"[0-9a-f]{64}", str(source.get("sha256", ""))
        ):
            raise BuildError("Windows pack input is not URL+SHA-256 locked")
    for name, spec in lock["components"].items():
        packages = spec.get("packages", [])
        if any(package.count("==") != 1 for package in packages):
            raise BuildError(f"{name} packages must use exact pins")


def enable_embedded_python(runtime: Path) -> None:
    pth_files = list(runtime.glob("python*._pth"))
    if len(pth_files) != 1:
        raise BuildError(f"expected one embedded Python _pth file in {runtime}")
    pth_files[0].write_text(
        "python312.zip\n.\nLib\nLib/site-packages\nimport site\n",
        encoding="utf-8",
    )
    (runtime / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)


def write_notice(stage: Path, component: str, lines: list[str]) -> None:
    (stage / "THIRD-PARTY-NOTICES.txt").write_text(
        f"My LLM Wiki {component} pack\n\n" + "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def build_windows_documents(
    base: Path, spec: dict, system: str, architecture: str
) -> None:
    stage = base / "documents"
    site = stage / "site"
    pip_target(
        Path(sys.executable),
        site,
        requirements_lock("documents", system, architecture),
    )
    runner = stage / "markitdown_runner.py"
    runner.write_text(
        "from pathlib import Path\nimport sys\n"
        "sys.path.insert(0, str(Path(__file__).with_name('site')))\n"
        "from markitdown.__main__ import main\nraise SystemExit(main())\n",
        encoding="utf-8",
    )
    checked([str(base / "runtime" / "python.exe"), str(runner), "--help"])
    checked(
        [
            str(base / "runtime" / "python.exe"),
            "-c",
            f"import sys; sys.path.insert(0, {str(site)!r}); "
            "import mammoth, openpyxl, pandas, pdfplumber, pptx",
        ]
    )
    write_notice(stage, "documents", [
        "MarkItDown: https://github.com/microsoft/markitdown",
        "Resolved Python dependency inventory: site/.my-llm-wiki-requirements.lock",
    ])


def build_windows_web(base: Path, spec: dict, downloads: Path, work: Path) -> None:
    stage = base / "web"
    node_archive = download_verified(
        spec["node"]["url"], downloads / "node.zip", spec["node"]["sha256"]
    )
    node_unpack = work / "node-unpack"
    extract(node_archive, node_unpack)
    roots = [path for path in node_unpack.iterdir() if path.is_dir()]
    if len(roots) != 1 or not (roots[0] / "node.exe").is_file():
        raise BuildError("unexpected Node archive layout")
    shutil.copytree(roots[0], stage / "node")
    if checked([str(stage / "node" / "node.exe"), "--version"]).lstrip("v") != spec["node"]["version"]:
        raise BuildError("Node version differs from lock")
    opencli = stage / "opencli"
    opencli.mkdir(parents=True)
    shutil.copy2(OPENCLI_PACKAGE, opencli / "package.json")
    shutil.copy2(OPENCLI_LOCK, opencli / "package-lock.json")
    checked(
        [
            str(stage / "node" / "npm.cmd"), "ci", "--prefix", str(opencli),
            "--omit=dev", "--no-audit", "--no-fund", "--ignore-scripts",
        ],
        timeout=1800,
    )
    package_root = opencli / "node_modules" / "@jackwener" / "opencli"
    package = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    package_lock = json.loads((opencli / "package-lock.json").read_text(encoding="utf-8"))
    if package.get("version") != spec["opencli"]["version"] or npm_lock_entry(
        package_lock, "@jackwener/opencli"
    ).get("integrity") != spec["opencli"]["integrity"]:
        raise BuildError("OpenCLI package differs from lock")
    main = package_root / "dist" / "src" / "main.js"
    if spec["opencli"]["version"] not in checked(
        [str(stage / "node" / "node.exe"), str(main), "--version"]
    ):
        raise BuildError("OpenCLI postcheck returned another version")
    extension_archive = download_verified(
        spec["extension"]["url"],
        downloads / "extension.zip",
        spec["extension"]["sha256"],
    )
    extension_unpack = work / "extension-unpack"
    extract(extension_archive, extension_unpack)
    manifests = sorted(extension_unpack.rglob("manifest.json"))
    if not manifests:
        raise BuildError("Browser Bridge archive has no manifest")
    root = min((path.parent for path in manifests), key=lambda path: len(path.parts))
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != spec["extension"]["version"]:
        raise BuildError("Browser Bridge version differs from lock")
    shutil.copytree(root, stage / "extension")
    write_notice(stage, "web", [
        "Node.js: https://nodejs.org/",
        "OpenCLI and Browser Bridge: https://github.com/jackwener/OpenCLI",
        "Resolved npm dependency inventory: opencli/package-lock.json",
    ])


def build_windows_video(base: Path, spec: dict, downloads: Path, work: Path) -> None:
    stage = base / "video"
    stage.mkdir(parents=True)
    download_verified(spec["yt_dlp"]["url"], stage / "yt-dlp.exe", spec["yt_dlp"]["sha256"])
    ffmpeg_archive = download_verified(
        spec["ffmpeg"]["url"], downloads / "ffmpeg.zip", spec["ffmpeg"]["sha256"]
    )
    unpacked = work / "ffmpeg-unpack"
    extract(ffmpeg_archive, unpacked)
    executables = sorted(unpacked.rglob("ffmpeg.exe"))
    if not executables:
        raise BuildError("FFmpeg archive has no ffmpeg.exe")
    shutil.copytree(executables[0].parent.parent, stage / "ffmpeg")
    if checked([str(stage / "yt-dlp.exe"), "--version"]) != spec["yt_dlp"]["version"]:
        raise BuildError("yt-dlp version differs from lock")
    first_line = checked([str(stage / "ffmpeg" / "bin" / "ffmpeg.exe"), "-version"]).splitlines()[0]
    if spec["ffmpeg"]["version"] not in first_line:
        raise BuildError("FFmpeg version differs from lock")
    aria2_archive = download_verified(
        spec["aria2"]["url"], downloads / "aria2.zip", spec["aria2"]["sha256"]
    )
    aria2_unpacked = work / "aria2-unpack"
    extract(aria2_archive, aria2_unpacked)
    aria2_executables = sorted(aria2_unpacked.rglob("aria2c.exe"))
    if len(aria2_executables) != 1:
        raise BuildError("aria2 archive does not contain exactly one aria2c.exe")
    shutil.copy2(aria2_executables[0], stage / "aria2c.exe")
    aria2_line = checked([str(stage / "aria2c.exe"), "--version"]).splitlines()[0]
    if aria2_line != f"aria2 version {spec['aria2']['version']}":
        raise BuildError("aria2 version differs from lock")
    (stage / "yt_dlp_runner.py").write_text(
        "from pathlib import Path\nimport os\nimport subprocess\nimport sys\n"
        "root = Path(__file__).parent\n"
        "os.environ['PATH'] = str(root) + os.pathsep + os.environ.get('PATH', '')\n"
        "args = list(sys.argv[1:])\n"
        "if '--ffmpeg-location' not in args:\n"
        "    args[0:0] = ['--ffmpeg-location', str(root / 'ffmpeg' / 'bin')]\n"
        "raise SystemExit(subprocess.run([str(root / 'yt-dlp.exe'), *args], check=False).returncode)\n",
        encoding="utf-8",
    )
    checked(
        [
            str(base / "runtime" / "python.exe"),
            str(stage / "yt_dlp_runner.py"),
            "--version",
        ]
    )
    write_notice(stage, "video", [
        "yt-dlp: https://github.com/yt-dlp/yt-dlp",
        "aria2: https://github.com/aria2/aria2",
        "FFmpeg source: https://ffmpeg.org/",
        "Gyan Windows build: https://www.gyan.dev/ffmpeg/builds/",
    ])


def build_windows_asr(
    name: str,
    spec: dict,
    python_archive: Path,
    work: Path,
    system: str,
    architecture: str,
) -> Path:
    stage = work / f"pack-{name}"
    extract(python_archive, stage / "runtime")
    enable_embedded_python(stage / "runtime")
    site = stage / "runtime" / "Lib" / "site-packages"
    pip_target(
        Path(sys.executable),
        site,
        requirements_lock(name, system, architecture),
    )
    checked(
        [str(stage / "runtime" / "python.exe"), *spec["postcheck"]],
        timeout=900,
    )
    write_notice(stage, name, [
        "Python runtime: https://www.python.org/",
        "Resolved Python dependency inventory: runtime/Lib/site-packages/.my-llm-wiki-requirements.lock",
    ])
    return stage


def build_posix(
    selected: list[str],
    work: Path,
    dist: Path,
    system: str,
    architecture: str,
    version: str,
) -> dict[str, dict]:
    lock = json.loads(POSIX_LOCK.read_text(encoding="utf-8"))
    runtime_root, python = install_posix_python(lock, work, system, architecture)
    artifacts: dict[str, dict] = {}
    if "toolchain-base" in selected:
        stages = {
            "documents": build_posix_documents(
                lock["components"]["documents"], python, work, system, architecture
            ),
            "web": build_posix_web(
                lock["components"]["web"], work, system, architecture
            ),
            "video": build_posix_video(
                lock["components"]["video"], python, work, system, architecture
            ),
        }
        base = work / "pack-toolchain-base"
        copy_tree(runtime_root, base / "runtime")
        for name, source in stages.items():
            copy_tree(source, base / name)
        commands = {
            "python-runtime": ["{pack}/runtime/bin/python3"],
            "node-runtime": ["{pack}/web/node"],
            "markitdown": ["{pack}/runtime/bin/python3", "{pack}/documents/markitdown_runner.py"],
            "opencli": [
                "{pack}/web/node",
                "{pack}/web/opencli/node_modules/@jackwener/opencli/dist/src/main.js",
            ],
            "yt-dlp": ["{pack}/runtime/bin/python3", "{pack}/video/yt_dlp_runner.py"],
            "ffmpeg": ["{pack}/video/ffmpeg"],
            "aria2c": ["{pack}/video/aria2c"],
        }
        release_checks = [
            {"command": "markitdown", "args": ["--help"]},
            {"command": "opencli", "args": ["--version"]},
            {"command": "yt-dlp", "args": ["--version"]},
            {"command": "ffmpeg", "args": ["-version"]},
            {"command": "aria2c", "args": ["--version"]},
        ]
        artifacts["toolchain-base"] = pack_spec(
            base,
            dist,
            "toolchain-base",
            system,
            architecture,
            version,
            commands=commands,
            probes=toolchain_client_probes(),
            release_checks=release_checks,
            capabilities=[
                "capture.web.authenticated",
                "capture.video.captions",
                "media.extract-audio",
                "media.parallel-download",
                "document.to-markdown",
            ],
            manual_actions=[{
                "id": "opencli-browser-bridge",
                "title": "加载 OpenCLI Browser Bridge",
                "detail": "在 Chrome 扩展管理页开启开发者模式，并加载 {pack}/web/extension。",
            }],
        )
    for pack_id in ("asr-zh", "asr-other"):
        if pack_id not in selected:
            continue
        component = build_posix_asr(
            pack_id,
            lock["components"][pack_id],
            python,
            work,
            system,
            architecture,
        )
        stage = work / f"pack-{pack_id}"
        copy_tree(runtime_root, stage / "runtime")
        copy_tree(component, stage / pack_id)
        environment = {pack_id: {"PYTHONPATH": f"{{pack}}/{pack_id}/site"}}
        commands = {
            "python-runtime": ["{pack}/runtime/bin/python3"],
            f"{pack_id}-postcheck": [
                "{pack}/runtime/bin/python3",
                *lock["components"][pack_id]["postcheck"],
            ]
        }
        artifacts[pack_id] = pack_spec(
            stage,
            dist,
            pack_id,
            system,
            architecture,
            version,
            commands=commands,
            python_profiles={pack_id: ["{pack}/runtime/bin/python3"]},
            environment=environment,
            probes=python_client_probe(),
            release_checks=[{"command": f"{pack_id}-postcheck", "args": []}],
            capabilities=["transcribe.audio.timestamped"],
        )
    return artifacts


def build_windows(
    selected: list[str],
    work: Path,
    dist: Path,
    system: str,
    architecture: str,
    version: str,
) -> dict[str, dict]:
    lock = json.loads(WINDOWS_LOCK.read_text(encoding="utf-8"))
    validate_windows_lock(lock)
    downloads = work / "downloads"
    downloads.mkdir()
    python_zip = download_verified(
        lock["python"]["url"], downloads / "python.zip", lock["python"]["sha256"]
    )
    artifacts: dict[str, dict] = {}
    if "toolchain-base" in selected:
        base = work / "pack-toolchain-base"
        extract(python_zip, base / "runtime")
        enable_embedded_python(base / "runtime")
        build_windows_documents(
            base, lock["components"]["documents"], system, architecture
        )
        build_windows_web(base, lock["components"]["web"], downloads, work)
        build_windows_video(base, lock["components"]["video"], downloads, work)
        commands = {
            "python-runtime": ["{pack}/runtime/python.exe"],
            "node-runtime": ["{pack}/web/node/node.exe"],
            "markitdown": ["{pack}/runtime/python.exe", "{pack}/documents/markitdown_runner.py"],
            "opencli": [
                "{pack}/web/node/node.exe",
                "{pack}/web/opencli/node_modules/@jackwener/opencli/dist/src/main.js",
            ],
            "yt-dlp": [
                "{pack}/runtime/python.exe",
                "{pack}/video/yt_dlp_runner.py",
            ],
            "ffmpeg": ["{pack}/video/ffmpeg/bin/ffmpeg.exe"],
            "aria2c": ["{pack}/video/aria2c.exe"],
        }
        artifacts["toolchain-base"] = pack_spec(
            base,
            dist,
            "toolchain-base",
            system,
            architecture,
            version,
            commands=commands,
            probes=toolchain_client_probes(),
            release_checks=[
                {"command": "markitdown", "args": ["--help"]},
                {"command": "opencli", "args": ["--version"]},
                {"command": "yt-dlp", "args": ["--version"]},
                {"command": "ffmpeg", "args": ["-version"]},
                {"command": "aria2c", "args": ["--version"]},
            ],
            capabilities=[
                "capture.web.authenticated",
                "capture.video.captions",
                "media.extract-audio",
                "media.parallel-download",
                "document.to-markdown",
            ],
            manual_actions=[{
                "id": "opencli-browser-bridge",
                "title": "加载 OpenCLI Browser Bridge",
                "detail": "在 Chrome 扩展管理页开启开发者模式，并加载 {pack}\\web\\extension。",
            }],
        )
    for pack_id in ("asr-zh", "asr-other"):
        if pack_id not in selected:
            continue
        stage = build_windows_asr(
            pack_id,
            lock["components"][pack_id],
            python_zip,
            work,
            system,
            architecture,
        )
        commands = {
            "python-runtime": ["{pack}/runtime/python.exe"],
            f"{pack_id}-postcheck": [
                "{pack}/runtime/python.exe",
                *lock["components"][pack_id]["postcheck"],
            ]
        }
        artifacts[pack_id] = pack_spec(
            stage,
            dist,
            pack_id,
            system,
            architecture,
            version,
            commands=commands,
            python_profiles={pack_id: ["{pack}/runtime/python.exe"]},
            environment={},
            probes=python_client_probe(),
            release_checks=[{"command": f"{pack_id}-postcheck", "args": []}],
            capabilities=["transcribe.audio.timestamped"],
        )
    return artifacts


def pack_spec(
    stage: Path,
    dist: Path,
    pack_id: str,
    system: str,
    architecture: str,
    version: str,
    *,
    commands: dict[str, list[str]],
    python_profiles: dict[str, list[str]] | None = None,
    environment: dict[str, dict[str, str]] | None = None,
    probes: list[dict] | None = None,
    release_checks: list[dict] | None = None,
    capabilities: list[str] | None = None,
    manual_actions: list[dict] | None = None,
) -> dict:
    asset = write_zip(
        stage, dist / asset_name(pack_id, version, system, architecture)
    )
    validate_release_asset_size(pack_id, asset.stat().st_size)
    verify_release_archive(
        asset,
        commands,
        environment or {},
        release_checks or [],
    )
    return {
        "asset": asset.name,
        "sha256": sha256(asset),
        "size": asset.stat().st_size,
        "installed_size": installed_size(asset),
        "commands": commands,
        "python_profiles": python_profiles or {},
        "environment": environment or {},
        "capabilities": capabilities or [],
        "probes": probes or [],
        "manual_actions": manual_actions or [],
    }


def python_client_probe() -> list[dict]:
    return [{"command": "python-runtime", "args": ["--version"]}]


def toolchain_client_probes() -> list[dict]:
    return [
        *python_client_probe(),
        {"command": "node-runtime", "args": ["--version"]},
        {"command": "ffmpeg", "args": ["-version"]},
        {"command": "aria2c", "args": ["--version"]},
    ]


def verify_release_archive(
    asset: Path,
    commands: dict[str, list[str]],
    environment: dict[str, dict[str, str]],
    checks: list[dict],
) -> None:
    if not checks:
        return
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "pack"
        extract(asset, root)
        check_environment = os.environ.copy()
        for values in environment.values():
            for key, value in values.items():
                check_environment[key] = value.replace("{pack}", str(root))
        for check in checks:
            name = check["command"]
            if name not in commands:
                raise BuildError(f"release check references undeclared command: {name}")
            argv = [
                value.replace("{pack}", str(root))
                for value in [*commands[name], *check.get("args", [])]
            ]
            checked(argv, env=check_environment, timeout=900)


def validate_release_asset_size(pack_id: str, size: int) -> None:
    if size >= MAX_RELEASE_ASSET_SIZE:
        raise BuildError(
            f"{pack_id} archive exceeds the GitHub release asset limit: "
            f"{size} >= {MAX_RELEASE_ASSET_SIZE}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--packs", default="toolchain-base,asr-zh,asr-other")
    args = parser.parse_args(argv)
    metadata = load_metadata()
    version = metadata["version"]
    selected = [value for value in args.packs.split(",") if value]
    unknown = sorted(set(selected) - set(PACK_IDS))
    if unknown:
        raise BuildError("unknown pack(s): " + ", ".join(unknown))
    system, architecture = target()
    args.dist.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        if system == "windows":
            artifacts = build_windows(
                selected, work, args.dist, system, architecture, version
            )
        else:
            artifacts = build_posix(
                selected, work, args.dist, system, architecture, version
            )
    release_tag = pack_tag(version)
    release_sources = [
        "https://wiki.htmlgo.to/_update/dl/{tag}/{asset}",
        "https://github.com/dake6767/llm-wiki-suite/releases/download/{tag}/{asset}",
    ]
    rows = []
    for pack_id, spec in artifacts.items():
        rows.append({
            "id": pack_id,
            "version": version,
            "platform": system,
            "architecture": architecture,
            "sha256": spec["sha256"],
            "size": spec["size"],
            "installed_size": spec["installed_size"],
            "urls": [
                template.format(tag=release_tag, asset=spec["asset"])
                for template in release_sources
            ],
            **{key: value for key, value in spec.items() if key not in {"asset", "sha256", "size", "installed_size"}},
        })
    index = {
        "schema": 1,
        "pack_version": version,
        "input_sha256": metadata["input_sha256"],
        "artifacts": rows,
    }
    index_path = args.dist / f"pack-index-{system}-{architecture}.json"
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"status": "built", "index": str(index_path), "packs": sorted(artifacts)}
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as exc:
        print(f"distribution-build: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1)
