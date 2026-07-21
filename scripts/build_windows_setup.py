#!/usr/bin/env python3
"""Build pinned Windows component packs and My-LLM-Wiki-Setup.exe.

This script is intended for ``windows-latest`` release CI.  It consumes the
committed upstream lock, verifies every directly downloaded byte, builds
isolated component archives, emits a release manifest containing the final
archive hashes, and finally embeds that manifest plus the suite and CPython
runtime into one PyInstaller executable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "registry" / "windows-toolchain.lock.json"
BROWSER_ICONS = ROOT / "apps" / "my-llm-wiki-browser" / "desktop" / "src-tauri" / "icons"
SETUP_ICON_ICO = BROWSER_ICONS / "icon.ico"
SETUP_ICON_PNG = BROWSER_ICONS / "64x64.png"
MAX_DOWNLOAD = 2 * 1024 * 1024 * 1024
# ``windows_setup.py`` loads suite installer modules from the extracted payload
# at runtime, so PyInstaller cannot discover standard-library imports that are
# unique to those modules. Keep that frozen boundary explicit and tested.
DYNAMIC_SUITE_STDLIB = ("shlex",)


class BuildError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_verified(url: str, dest: Path, expected: str) -> Path:
    if not url.startswith("https://"):
        raise BuildError(f"refusing non-HTTPS build input: {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and sha256_file(dest) == expected:
        return dest
    temp = dest.with_suffix(dest.suffix + ".part")
    digest = hashlib.sha256()
    total = 0
    request = urllib.request.Request(url, headers={"User-Agent": "llm-wiki-setup-builder"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temp.open("wb") as sink:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD:
                    raise BuildError(f"download exceeds {MAX_DOWNLOAD} bytes: {url}")
                digest.update(chunk)
                sink.write(chunk)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    actual = digest.hexdigest()
    if actual != expected:
        temp.unlink(missing_ok=True)
        raise BuildError(f"sha256 mismatch for {url}: expected {expected}, got {actual}")
    temp.replace(dest)
    return dest


def safe_extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    try:
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                member = Path(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    raise BuildError(f"unsafe zip member: {info.filename}")
                resolved = (root / member).resolve()
                if resolved != root and root not in resolved.parents:
                    raise BuildError(f"unsafe zip member: {info.filename}")
            bundle.extractall(dest)
    except zipfile.BadZipFile as exc:
        raise BuildError(f"invalid zip: {archive}") from exc


def write_zip(source: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(dest.suffix + ".part")
    with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(source).as_posix())
    temp.replace(dest)
    return dest


def load_lock() -> dict:
    data = json.loads(LOCK.read_text(encoding="utf-8"))
    validate_lock(data)
    return data


def validate_lock(data: dict) -> None:
    if data.get("schema") != 1 or data.get("architecture") != "x86_64":
        raise BuildError("unsupported Windows toolchain lock")
    if not isinstance(data.get("setup_version"), str) or not data["setup_version"]:
        raise BuildError("Windows toolchain lock has no setup_version")
    python = data.get("python") or {}
    if not str(python.get("url", "")).startswith("https://") or not re.fullmatch(
        r"[0-9a-f]{64}", str(python.get("sha256", ""))
    ):
        raise BuildError("Windows Python input is not URL+SHA256 locked")
    sources = data.get("release_sources")
    if not isinstance(sources, list) or not sources or any(
        not isinstance(source, str) or not source.startswith("https://")
        for source in sources
    ):
        raise BuildError("release_sources must be non-empty HTTPS templates")
    components = data.get("components")
    required = {"documents", "web", "video", "asr-zh", "asr-other"}
    if not isinstance(components, dict) or set(components) != required:
        raise BuildError("Windows component set is incomplete")
    for component, spec in components.items():
        packages = spec.get("packages", [])
        if not isinstance(packages, list) or any(
            not isinstance(package, str)
            or package.count("==") != 1
            or not all(package.split("==", 1))
            for package in packages
        ):
            raise BuildError(f"{component} packages must use exact == pins")
    asr_zh = {
        package.split("==", 1)[0]: package.split("==", 1)[1]
        for package in components["asr-zh"]["packages"]
    }
    if asr_zh.get("torch") != asr_zh.get("torchaudio"):
        raise BuildError("torch and torchaudio must use the same reviewed version")
    direct = [
        components["web"]["node"],
        components["web"]["extension"],
        components["video"]["yt_dlp"],
        components["video"]["ffmpeg"],
    ]
    for spec in direct:
        if not str(spec.get("url", "")).startswith("https://") or not re.fullmatch(
            r"[0-9a-f]{64}", str(spec.get("sha256", ""))
        ):
            raise BuildError("direct Windows input is not URL+SHA256 locked")
    integrity = str(components["web"]["opencli"].get("integrity", ""))
    if not integrity.startswith("sha512-"):
        raise BuildError("OpenCLI npm integrity is not locked")


def enable_embedded_site(runtime: Path) -> None:
    pth_files = list(runtime.glob("python*._pth"))
    if len(pth_files) != 1:
        raise BuildError(f"expected one embedded Python _pth file in {runtime}")
    pth_files[0].write_text(
        "python312.zip\n.\nLib\nLib/site-packages\nimport site\n",
        encoding="utf-8",
    )
    (runtime / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)


def pip_target(target: Path, packages: list[str], report: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-m", "pip", "install",
        "--disable-pip-version-check", "--no-input", "--no-compile",
        "--target", str(target), "--report", str(report), *packages,
    ]
    subprocess.run(command, stdin=subprocess.DEVNULL, timeout=3600, check=True)


def checked_output(argv: list[str], *, timeout: int = 120) -> str:
    result = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise BuildError(
            f"postcheck failed ({result.returncode}): {argv!r}\n{result.stdout}{result.stderr}"
        )
    return (result.stdout + result.stderr).strip()


def copy_tree_contents(source: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def write_notice(stage: Path, component: str, lines: list[str]) -> None:
    body = [
        f"# My LLM Wiki Windows component: {component}",
        "",
        "This archive is assembled from the exact versions recorded in",
        "registry/windows-toolchain.lock.json. Package license files and dist-info",
        "metadata are retained in the archive where supplied upstream.",
        "",
        *[f"- {line}" for line in lines],
        "",
    ]
    (stage / "THIRD-PARTY-NOTICES.md").write_text("\n".join(body), encoding="utf-8")


def build_suite_payload(dest: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "suite"
        stage.mkdir()
        for name in ("AGENTS.md", "README.md"):
            shutil.copy2(ROOT / name, stage / name)
        for name in ("registry", "scripts", "skills"):
            shutil.copytree(
                ROOT / name,
                stage / name,
                ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", "test_*.py", "reports", "data", ".DS_Store"
                ),
            )
        # install-browser.py reads the Tauri updater pubkey from the repo's
        # tauri.conf.json; the installed suite has no apps/ tree, so stage
        # that one file at its expected relative path.
        tauri_config = (
            ROOT / "apps" / "my-llm-wiki-browser" / "desktop" / "src-tauri"
            / "tauri.conf.json"
        )
        staged_config = stage / tauri_config.relative_to(ROOT)
        staged_config.parent.mkdir(parents=True)
        shutil.copy2(tauri_config, staged_config)
        write_zip(stage, dest)


def build_documents(lock: dict, work: Path, dist: Path) -> Path:
    spec = lock["components"]["documents"]
    stage = work / "documents"
    site = stage / "site-packages"
    pip_target(site, spec["packages"], stage / "pip-report.json")
    (stage / "markitdown_runner.py").write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "sys.path.insert(0, str(Path(__file__).with_name('site-packages')))\n"
        "from markitdown.__main__ import main\n"
        "raise SystemExit(main())\n",
        encoding="utf-8",
    )
    checked_output([sys.executable, str(stage / "markitdown_runner.py"), "--help"])
    write_notice(stage, "documents", [
        "MarkItDown: https://github.com/microsoft/markitdown",
        "Resolved Python dependency inventory: pip-report.json",
    ])
    archive = write_zip(stage, dist / "My-LLM-Wiki-Documents_x64.zip")
    shutil.rmtree(stage)
    return archive


def build_web(lock: dict, downloads: Path, work: Path, dist: Path) -> Path:
    spec = lock["components"]["web"]
    stage = work / "web"
    node_archive = download_verified(
        spec["node"]["url"], downloads / "node.zip", spec["node"]["sha256"]
    )
    node_unpack = work / "node-unpack"
    safe_extract(node_archive, node_unpack)
    roots = [path for path in node_unpack.iterdir() if path.is_dir()]
    if len(roots) != 1 or not (roots[0] / "node.exe").is_file():
        raise BuildError("unexpected Node archive layout")
    shutil.copytree(roots[0], stage / "node")
    node_version = checked_output([str(stage / "node" / "node.exe"), "--version"])
    if node_version.lstrip("v") != spec["node"]["version"]:
        raise BuildError(f"Node version mismatch: {node_version}")

    opencli = stage / "opencli"
    subprocess.run(
        [
            str(stage / "node" / "npm.cmd"), "install", "--prefix", str(opencli),
            spec["opencli"]["package"],
            "--omit=dev", "--no-audit", "--no-fund", "--ignore-scripts",
        ],
        stdin=subprocess.DEVNULL,
        timeout=1800,
        check=True,
    )
    package_json = json.loads(
        (opencli / "node_modules" / "@jackwener" / "opencli" / "package.json").read_text(
            encoding="utf-8"
        )
    )
    if package_json.get("version") != spec["opencli"]["version"]:
        raise BuildError("npm installed a different OpenCLI version")
    package_lock = json.loads((opencli / "package-lock.json").read_text(encoding="utf-8"))
    locked_opencli = (package_lock.get("packages") or {}).get(
        "node_modules/@jackwener/opencli", {}
    )
    if locked_opencli.get("integrity") != spec["opencli"]["integrity"]:
        raise BuildError("npm OpenCLI integrity differs from the committed lock")
    opencli_version = checked_output([
        str(stage / "node" / "node.exe"),
        str(opencli / "node_modules" / "@jackwener" / "opencli" / "dist" / "src" / "main.js"),
        "--version",
    ])
    if spec["opencli"]["version"] not in opencli_version:
        raise BuildError(f"OpenCLI version mismatch: {opencli_version}")

    extension_archive = download_verified(
        spec["extension"]["url"],
        downloads / "opencli-extension.zip",
        spec["extension"]["sha256"],
    )
    extension_unpack = work / "extension-unpack"
    safe_extract(extension_archive, extension_unpack)
    manifests = sorted(extension_unpack.rglob("manifest.json"))
    if not manifests:
        raise BuildError("OpenCLI extension archive has no manifest.json")
    root = min((path.parent for path in manifests), key=lambda p: len(p.parts))
    extension_manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if extension_manifest.get("version") != spec["extension"]["version"]:
        raise BuildError("OpenCLI extension version differs from the committed lock")
    copy_tree_contents(root, stage / "extension")
    write_notice(stage, "web", [
        "Node.js: https://nodejs.org/",
        "OpenCLI and Browser Bridge: https://github.com/jackwener/OpenCLI",
        "Resolved npm dependency inventory: opencli/package-lock.json",
    ])
    archive = write_zip(stage, dist / "My-LLM-Wiki-Web_x64.zip")
    shutil.rmtree(stage)
    shutil.rmtree(node_unpack)
    shutil.rmtree(extension_unpack)
    return archive


def build_video(lock: dict, downloads: Path, work: Path, dist: Path) -> Path:
    spec = lock["components"]["video"]
    stage = work / "video"
    stage.mkdir(parents=True)
    download_verified(
        spec["yt_dlp"]["url"], stage / "yt-dlp.exe", spec["yt_dlp"]["sha256"]
    )
    ffmpeg_archive = download_verified(
        spec["ffmpeg"]["url"], downloads / "ffmpeg.zip", spec["ffmpeg"]["sha256"]
    )
    unpacked = work / "ffmpeg-unpack"
    safe_extract(ffmpeg_archive, unpacked)
    executables = sorted(unpacked.rglob("ffmpeg.exe"))
    if not executables:
        raise BuildError("FFmpeg archive has no ffmpeg.exe")
    # Keep the complete Gyan release root, including its license/readme/source
    # provenance, instead of redistributing only detached binaries.
    ffmpeg_root = executables[0].parent.parent
    shutil.copytree(ffmpeg_root, stage / "ffmpeg")
    yt_version = checked_output([str(stage / "yt-dlp.exe"), "--version"])
    if yt_version != spec["yt_dlp"]["version"]:
        raise BuildError(f"yt-dlp version mismatch: {yt_version}")
    ffmpeg_version = checked_output([str(stage / "ffmpeg" / "bin" / "ffmpeg.exe"), "-version"])
    if spec["ffmpeg"]["version"] not in ffmpeg_version.splitlines()[0]:
        raise BuildError("FFmpeg version differs from the committed lock")
    write_notice(stage, "video", [
        "yt-dlp: https://github.com/yt-dlp/yt-dlp",
        "FFmpeg source: https://ffmpeg.org/",
        "Gyan Windows build and bundled license/readme: https://www.gyan.dev/ffmpeg/builds/",
    ])
    archive = write_zip(stage, dist / "My-LLM-Wiki-Video_x64.zip")
    shutil.rmtree(stage)
    shutil.rmtree(unpacked)
    return archive


def build_asr(
    component: str,
    lock: dict,
    python_zip: Path,
    work: Path,
    dist: Path,
) -> Path:
    spec = lock["components"][component]
    stage = work / component
    safe_extract(python_zip, stage)
    enable_embedded_site(stage)
    pip_target(stage / "Lib" / "site-packages", spec["packages"], stage / "pip-report.json")
    checked_output([str(stage / "python.exe"), *spec["postcheck"]], timeout=300)
    write_notice(stage, component, [
        "Python runtime: https://www.python.org/",
        "Resolved Python dependency inventory: pip-report.json",
    ])
    suffix = "ASR-ZH" if component == "asr-zh" else "ASR-Other"
    archive = write_zip(stage, dist / f"My-LLM-Wiki-{suffix}_x64.zip")
    shutil.rmtree(stage)
    return archive


def component_version(component: str, spec: dict) -> str:
    if component == "documents":
        return spec["packages"][0].split("==", 1)[1]
    if component == "web":
        return f"{spec['opencli']['version']}+ext.{spec['extension']['version']}"
    if component == "video":
        return f"{spec['yt_dlp']['version']}+ffmpeg.{spec['ffmpeg']['version']}"
    return "+".join(package.replace("==", ".") for package in spec["packages"])


def build_manifest(lock: dict, tag: str, assets: dict[str, Path], dest: Path) -> dict:
    components = {}
    for component, path in assets.items():
        spec = lock["components"][component]
        components[component] = {
            "label": spec["label"],
            "description": spec["description"],
            "label_zh": spec.get("label_zh", spec["label"]),
            "description_zh": spec.get("description_zh", spec["description"]),
            "default": bool(spec.get("default")),
            "version": component_version(component, spec),
            "asset": path.name,
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
            "tools": spec.get("tools", {}),
            "python_profile": spec.get("python_profile"),
            "runtime_env": spec.get("runtime_env", {}),
            "postcheck": spec.get("postcheck", []),
        }
    manifest = {
        "schema": 1,
        "setup_version": lock["setup_version"],
        "release_tag": tag,
        "architecture": lock["architecture"],
        "sources": lock["release_sources"],
        "components": components,
    }
    dest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def build_executable(payload: Path, dist: Path, work: Path, version: str) -> Path:
    env = os.environ.copy()
    env["LLM_WIKI_SETUP_VERSION"] = version
    dynamic_imports = [
        value
        for module in DYNAMIC_SUITE_STDLIB
        for value in ("--hidden-import", module)
    ]
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller",
            # Console subsystem so shells wait and exit codes propagate (a
            # windowed exe makes PowerShell's `& exe` return immediately with
            # no $LASTEXITCODE).  The bootloader hides the console only when
            # the process owns it, i.e. a double-clicked GUI launch.
            "--noconfirm", "--clean", "--onefile", "--console",
            "--hide-console", "hide-early",
            "--icon", str(SETUP_ICON_ICO),
            "--name", "My-LLM-Wiki-Setup",
            "--hidden-import", "tkinter",
            "--hidden-import", "tkinter.messagebox",
            *dynamic_imports,
            "--add-data", f"{payload}{os.pathsep}payload",
            "--distpath", str(dist),
            "--workpath", str(work / "pyinstaller"),
            "--specpath", str(work / "pyinstaller-spec"),
            str(ROOT / "scripts" / "windows_setup.py"),
        ],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=1800,
        check=True,
    )
    executable = dist / "My-LLM-Wiki-Setup.exe"
    if not executable.is_file():
        raise BuildError(f"PyInstaller did not create {executable}")
    checked_output([str(executable), "inspect", "--json"], timeout=120)
    return executable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument(
        "--components",
        default="documents,web,video,asr-zh,asr-other",
        help="comma-separated component ids",
    )
    parser.add_argument("--skip-exe", action="store_true")
    args = parser.parse_args(argv)
    if os.name != "nt":
        raise SystemExit("build_windows_setup.py must run on Windows")

    lock = load_lock()
    if not args.skip_exe:
        try:
            from importlib.metadata import version as installed_version

            actual_pyinstaller = installed_version("pyinstaller")
        except Exception as exc:
            raise SystemExit(f"PyInstaller is unavailable: {exc}") from exc
        expected_pyinstaller = lock["build"]["pyinstaller"]
        if actual_pyinstaller != expected_pyinstaller:
            raise SystemExit(
                f"PyInstaller version mismatch: expected {expected_pyinstaller}, "
                f"got {actual_pyinstaller}"
            )
    selected = [item for item in args.components.split(",") if item]
    unknown = sorted(set(selected) - set(lock["components"]))
    if unknown:
        raise SystemExit("unknown component(s): " + ", ".join(unknown))
    dist = args.dist.resolve()
    dist.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        downloads = work / "downloads"
        python_zip = download_verified(
            lock["python"]["url"], downloads / "python.zip", lock["python"]["sha256"]
        )
        assets: dict[str, Path] = {}
        for component in selected:
            print(f"building component: {component}", flush=True)
            if component == "documents":
                path = build_documents(lock, work, dist)
            elif component == "web":
                path = build_web(lock, downloads, work, dist)
            elif component == "video":
                path = build_video(lock, downloads, work, dist)
            else:
                path = build_asr(component, lock, python_zip, work, dist)
            assets[component] = path

        payload = work / "payload"
        payload.mkdir()
        build_suite_payload(payload / "suite.zip")
        shutil.copy2(python_zip, payload / "python.zip")
        shutil.copy2(LOCK, payload / "windows-toolchain.lock.json")
        build_manifest(lock, args.release_tag, assets, payload / "component-manifest.json")
        shutil.copy2(SETUP_ICON_PNG, payload / "setup-icon.png")
        payload_files = [
            "suite.zip",
            "python.zip",
            "windows-toolchain.lock.json",
            "component-manifest.json",
        ]
        (payload / "setup-payload.json").write_text(
            json.dumps({
                "schema": 1,
                "setup_version": lock["setup_version"],
                "release_tag": args.release_tag,
                "files": {
                    name: sha256_file(payload / name) for name in payload_files
                },
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        shutil.copy2(payload / "component-manifest.json", dist / "component-manifest.json")
        shutil.copy2(LOCK, dist / "windows-toolchain.lock.json")
        if not args.skip_exe:
            build_executable(payload, dist, work, lock["setup_version"])

    print(json.dumps({
        "status": "built",
        "dist": str(dist),
        "assets": sorted(path.name for path in dist.iterdir() if path.is_file()),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
