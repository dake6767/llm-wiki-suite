#!/usr/bin/env python3
"""Build verified Protocol 5 component archives on their target runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "registry" / "agent-components.lock.json"


class BuildError(RuntimeError):
    pass


def checked(argv: list[str], *, env: dict[str, str] | None = None, timeout: int = 1800) -> str:
    try:
        result = subprocess.run(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BuildError(f"command could not run: {argv[0]}: {exc}") from exc
    if result.returncode:
        raise BuildError(result.stderr.strip() or result.stdout.strip() or "command failed")
    return result.stdout.strip()


def sha256_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def expanded_zip_size(path: Path) -> int:
    with zipfile.ZipFile(path) as bundle:
        return sum(info.file_size for info in bundle.infolist() if not info.is_dir())


def target() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x64" if machine in {"x86_64", "amd64"} else ""
    if system not in {"darwin", "linux"} or not arch or (system == "linux" and arch != "x64"):
        raise BuildError(f"unsupported component build target: {system}/{machine}")
    return system, arch


def write_zip(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                # Materialize runtime symlinks so extraction never needs link privileges.
                if path.is_file():
                    data = path.resolve().read_bytes()
                    info = zipfile.ZipInfo(path.relative_to(source).as_posix())
                    info.external_attr = (0o100755 if os.access(path.resolve(), os.X_OK) else 0o100644) << 16
                    bundle.writestr(info, data)
                continue
            if not path.is_file():
                continue
            info = zipfile.ZipInfo(path.relative_to(source).as_posix())
            info.external_attr = (0o100755 if os.access(path, os.X_OK) else 0o100644) << 16
            with path.open("rb") as handle:
                bundle.writestr(info, handle.read(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
    return destination


def download(url: str, destination: Path, expected: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "my-llm-wiki-component-builder/5"})
    with urllib.request.urlopen(request, timeout=30) as response, destination.open("wb") as out:
        shutil.copyfileobj(response, out)
    if sha256_file(destination) != expected:
        raise BuildError(f"download hash differs from lock: {url}")
    return destination


def safe_extract(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        root = destination.resolve()
        for info in bundle.infolist():
            path = (destination / info.filename).resolve()
            if path != root and root not in path.parents:
                raise BuildError(f"unsafe archive member: {info.filename}")
        bundle.extractall(destination)


def install_managed_python(lock: dict, work: Path) -> tuple[Path, Path]:
    install_dir = work / "uv-python"
    uv = shutil.which("uv")
    if not uv:
        raise BuildError("uv is required to build the private Python runtime")
    version = lock["runtime"]["python"]
    found_version = checked([uv, "--version"]).split()[1]
    if found_version != lock["runtime"]["uv"]:
        raise BuildError(f"uv version {found_version} differs from lock")
    checked([uv, "python", "install", version, "--install-dir", str(install_dir), "--no-bin"], timeout=900)
    candidates = sorted(install_dir.rglob(f"bin/python{'.'.join(version.split('.')[:2])}"))
    if len(candidates) != 1:
        raise BuildError("uv managed Python layout is ambiguous")
    executable = candidates[0]
    root = executable.parent.parent
    actual = checked([str(executable), "-c", "import platform; print(platform.python_version())"])
    if actual != version:
        raise BuildError(f"managed Python {actual} differs from lock {version}")
    try:
        checked([str(executable), "-c", "import pip"])
    except BuildError:
        checked([str(executable), "-m", "ensurepip", "--upgrade"])
    return root, executable


def build_runtime(lock: dict, source: Path, work: Path, dist: Path, system: str, arch: str) -> tuple[Path, str]:
    stage = work / "runtime-stage"
    shutil.copytree(source, stage, symlinks=True)
    for cache in list(stage.rglob("__pycache__")):
        shutil.rmtree(cache, ignore_errors=True)
    executable = stage / "bin" / "python3"
    if not executable.exists():
        version = ".".join(lock["runtime"]["python"].split(".")[:2])
        candidate = stage / "bin" / f"python{version}"
        if not candidate.exists():
            raise BuildError("staged runtime has no Python executable")
        shutil.copy2(candidate.resolve(), executable)
        executable.chmod(0o755)
    if checked([str(executable), "-c", "import ssl, sqlite3, venv; print('ok')"]) != "ok":
        raise BuildError("staged runtime postcheck failed")
    asset = dist / f"My-LLM-Wiki-Agent-Runtime_{system}_{arch}.zip"
    write_zip(stage, asset)
    return asset, lock["runtime"]["python"]


def pip_target(python: Path, stage: Path, packages: list[str]) -> None:
    site = stage / "site"
    report = stage / "pip-report.json"
    checked(
        [
            str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
            "--target", str(site), "--report", str(report), *packages,
        ],
        timeout=3600,
    )
    for cache in list(site.rglob("__pycache__")):
        shutil.rmtree(cache, ignore_errors=True)


def python_env(stage: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(stage / "site")
    return env


def component_version(component: str, spec: dict) -> str:
    if component == "web":
        return f"{spec['opencli']['version']}+ext.{spec['extension']['version']}"
    return "+".join(package.replace("==", ".") for package in spec.get("packages", []))


def build_documents(spec: dict, python: Path, work: Path) -> Path:
    stage = work / "documents"
    pip_target(python, stage, spec["packages"])
    (stage / "markitdown_runner.py").write_text(
        "from pathlib import Path\nimport sys\n"
        "sys.path.insert(0, str(Path(__file__).with_name('site')))\n"
        "from markitdown.__main__ import main\nraise SystemExit(main())\n",
        encoding="utf-8",
    )
    checked([str(python), str(stage / "markitdown_runner.py"), "--help"])
    return stage


def build_video(spec: dict, python: Path, work: Path) -> Path:
    stage = work / "video"
    pip_target(python, stage, spec["packages"])
    (stage / "yt_dlp_runner.py").write_text(
        "from pathlib import Path\nimport sys\n"
        "sys.path.insert(0, str(Path(__file__).with_name('site')))\n"
        "from yt_dlp import main\n"
        "if '--ffmpeg-location' not in sys.argv:\n"
        "    sys.argv[1:1] = ['--ffmpeg-location', str(Path(__file__).with_name('ffmpeg'))]\n"
        "raise SystemExit(main())\n",
        encoding="utf-8",
    )
    env = python_env(stage)
    ffmpeg_source = Path(checked(
        [str(python), "-c", "from imageio_ffmpeg import get_ffmpeg_exe; print(get_ffmpeg_exe())"],
        env=env,
    ))
    if not ffmpeg_source.is_file():
        raise BuildError("imageio-ffmpeg did not provide an executable")
    shutil.copy2(ffmpeg_source, stage / "ffmpeg")
    (stage / "ffmpeg").chmod(0o755)
    checked([str(python), str(stage / "yt_dlp_runner.py"), "--version"])
    checked([str(stage / "ffmpeg"), "-version"])
    return stage


def build_asr(component: str, spec: dict, python: Path, work: Path) -> Path:
    stage = work / component
    pip_target(python, stage, spec["packages"])
    checked([str(python), *spec["postcheck"]], env=python_env(stage), timeout=900)
    return stage


def build_web(spec: dict, work: Path) -> Path:
    stage = work / "web"
    stage.mkdir(parents=True)
    node = Path(checked(["node", "-p", "process.execPath"]))
    actual_node = checked([str(node), "--version"]).lstrip("v")
    if actual_node != spec["node"]:
        raise BuildError(f"Node {actual_node} differs from lock {spec['node']}")
    shutil.copy2(node, stage / "node")
    (stage / "node").chmod(0o755)
    opencli = stage / "opencli"
    checked(
        [
            "npm", "install", "--prefix", str(opencli), spec["opencli"]["package"],
            "--omit=dev", "--no-audit", "--no-fund", "--ignore-scripts",
        ],
        timeout=1800,
    )
    package = json.loads(
        (opencli / "node_modules" / "@jackwener" / "opencli" / "package.json").read_text(encoding="utf-8")
    )
    package_lock = json.loads((opencli / "package-lock.json").read_text(encoding="utf-8"))
    locked = (package_lock.get("packages") or {}).get("node_modules/@jackwener/opencli", {})
    if package.get("version") != spec["opencli"]["version"] or locked.get("integrity") != spec["opencli"]["integrity"]:
        raise BuildError("OpenCLI package differs from lock")
    main = opencli / "node_modules" / "@jackwener" / "opencli" / "dist" / "src" / "main.js"
    if spec["opencli"]["version"] not in checked([str(stage / "node"), str(main), "--version"]):
        raise BuildError("OpenCLI postcheck returned another version")

    archive = download(spec["extension"]["url"], work / "extension.zip", spec["extension"]["sha256"])
    unpacked = work / "extension-unpacked"
    safe_extract(archive, unpacked)
    manifests = sorted(unpacked.rglob("manifest.json"))
    if not manifests:
        raise BuildError("Browser Bridge archive has no manifest")
    extension_root = min((path.parent for path in manifests), key=lambda row: len(row.parts))
    extension = json.loads((extension_root / "manifest.json").read_text(encoding="utf-8"))
    if extension.get("version") != spec["extension"]["version"]:
        raise BuildError("Browser Bridge version differs from lock")
    shutil.copytree(extension_root, stage / "extension")
    return stage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--components", default="documents,web,video,asr-zh,asr-other")
    args = parser.parse_args(argv)
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    if lock.get("schema") != 1 or lock.get("protocol") != 5:
        raise BuildError("unsupported component lock")
    if args.release_tag != lock.get("release_tag") and args.release_tag != "v-ci":
        raise BuildError("release tag differs from the reviewed component lock")
    system, arch = target()
    selected = [row for row in args.components.split(",") if row]
    unknown = sorted(set(selected) - set(lock["components"]))
    if unknown:
        raise BuildError("unknown component(s): " + ", ".join(unknown))
    args.dist.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        runtime_root, python = install_managed_python(lock, work)
        runtime_asset, runtime_version = build_runtime(lock, runtime_root, work, args.dist, system, arch)
        assets = {}
        builders = {
            "documents": lambda spec: build_documents(spec, python, work),
            "web": lambda spec: build_web(spec, work),
            "video": lambda spec: build_video(spec, python, work),
            "asr-zh": lambda spec: build_asr("asr-zh", spec, python, work),
            "asr-other": lambda spec: build_asr("asr-other", spec, python, work),
        }
        for component in selected:
            stage = builders[component](lock["components"][component])
            asset = args.dist / f"My-LLM-Wiki-Agent-{component}_{system}_{arch}.zip"
            write_zip(stage, asset)
            assets[component] = asset
        manifest = {
            "schema": 1,
            "protocol": 5,
            "release_tag": args.release_tag,
            "platform": system,
            "architecture": arch,
            "sources": lock["release_sources"],
            "runtime": {
                "version": runtime_version,
                "asset": runtime_asset.name,
                "sha256": sha256_file(runtime_asset),
                "size": runtime_asset.stat().st_size,
                "installed_size": expanded_zip_size(runtime_asset),
            },
            "components": {},
        }
        for component, asset in assets.items():
            spec = lock["components"][component]
            manifest["components"][component] = {
                "label": spec["label"],
                "description": spec["description"],
                "default": bool(spec.get("default")),
                "version": component_version(component, spec),
                "asset": asset.name,
                "sha256": sha256_file(asset),
                "size": asset.stat().st_size,
                "installed_size": expanded_zip_size(asset),
                "tools": spec.get("tools", {}),
                "python_profile": spec.get("python_profile"),
                "runtime_env": spec.get("runtime_env", {}),
                "postcheck": spec.get("postcheck", []),
            }
        manifest_path = args.dist / f"My-LLM-Wiki-Agent-Components_{system}_{arch}.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "built", "manifest": str(manifest_path), "assets": sorted(path.name for path in args.dist.iterdir())}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as exc:
        print(f"component-build: {exc}", file=sys.stderr)
        raise SystemExit(1)
