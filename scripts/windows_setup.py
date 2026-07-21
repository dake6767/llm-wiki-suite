#!/usr/bin/env python3
"""Native Windows entry point for My LLM Wiki skills and tool components.

The release build freezes this module into ``My-LLM-Wiki-Setup.exe`` and
embeds four payload files: the suite, a private CPython runtime, the committed
upstream lock, and a release component manifest.  Windows has no legacy
fallback: this executable owns install, update, repair, component state, and
uninstall.  macOS and Linux never execute this module.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOME = "~/.my-llm-wiki"
DEFAULT_WIKIS = "~/wikis"
SETUP_DIR = "setup"
RECEIPT_NAME = "install-state.json"
SETUP_EXE = "My-LLM-Wiki-Setup.exe"
MAX_DOWNLOAD = 2 * 1024 * 1024 * 1024
OFFICIAL_REMOTES = {
    "https://github.com/dake6767/llm-wiki-suite",
    "https://github.com/dake6767/llm-wiki-suite.git",
    "https://gitee.com/dake6767/llm-wiki-suite",
    "https://gitee.com/dake6767/llm-wiki-suite.git",
    "git@github.com:dake6767/llm-wiki-suite.git",
    "ssh://git@github.com/dake6767/llm-wiki-suite.git",
}


class SetupError(RuntimeError):
    pass


def emit(event: str, **values) -> None:
    print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


_progress_hook = None


def set_progress_hook(hook) -> None:
    """Install a GUI observer for install progress; the frozen exe has no
    console, so stdout events alone leave the window silent for minutes."""
    global _progress_hook
    _progress_hook = hook


def notify_progress(**info) -> None:
    if _progress_hook is None:
        return
    try:
        _progress_hook(info)
    except Exception:
        pass


def hidden_console_flags() -> int:
    """Child-process creation flags for console-subsystem children.

    The frozen exe is a console app whose bootloader hides the console it owns
    (double-clicked GUI launch); children then inherit that hidden console and
    stay invisible.  This guard covers the residual case where Setup has no
    console at all: without CREATE_NO_WINDOW each console child (python.exe,
    cmd.exe) would pop up its own window.  Inside a visible console (CLI use)
    it returns 0 so children keep inheriting it."""
    if os.name != "nt":
        return 0
    try:
        import ctypes

        if ctypes.windll.kernel32.GetConsoleWindow():
            return 0
    except Exception:  # noqa: BLE001 - fall back to inherited behaviour
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def inherited_std_streams() -> dict[str, object]:
    """Explicit std-stream arguments for children that must write to our streams.

    subprocess only sets ``STARTF_USESTDHANDLES`` when at least one stream is
    passed explicitly.  With all three left as ``None`` *and* CREATE_NO_WINDOW
    in the creation flags, Windows gives the child a fresh invisible console
    and its stdout/stderr go there instead of to our pipes — silently
    discarded.  Field report 2026-07-20: run from Git Bash (whose shell owns no
    console, so the CREATE_NO_WINDOW guard engages) every ``python run`` /
    ``tools run`` child ran and wrote its files but printed nothing, leaving
    failures undiagnosable.  Naming the streams pins the child to ours; a
    stream with no real descriptor (double-clicked GUI launch) is left out so
    subprocess keeps its default for that one."""
    streams: dict[str, object] = {}
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        try:
            if stream is not None and stream.fileno() >= 0:
                streams[name] = stream
        except (OSError, ValueError, AttributeError):
            continue
    return streams


def run_passthrough(command: list[str], **kwargs) -> int:
    """Run *command* with our own stdin/stdout/stderr and return its exit code."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()  # keep our buffered text ahead of the child's writes
        except (OSError, ValueError, AttributeError):
            pass
    return subprocess.run(
        command,
        check=False,
        creationflags=hidden_console_flags(),
        **inherited_std_streams(),
        **kwargs,
    ).returncode


def attach_parent_console() -> None:
    """Reconnect std streams if the exe ever runs without a console.

    The console build normally always has one (possibly hidden), making this a
    no-op; it is a safety net so CLI output cannot silently vanish if the exe
    is embedded or repackaged without a console.  Piped or redirected streams
    are already usable and are left untouched."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        if kernel32.GetConsoleWindow():
            return
    except Exception:  # noqa: BLE001 - console plumbing is best effort
        return

    def usable(stream) -> bool:
        try:
            return stream is not None and stream.fileno() >= 0
        except (OSError, ValueError, AttributeError):
            return False

    if usable(sys.stdout) and usable(sys.stderr):
        return
    if not kernel32.AttachConsole(0xFFFFFFFF):  # ATTACH_PARENT_PROCESS
        return
    try:
        if not usable(sys.stdout):
            sys.stdout = open(  # noqa: SIM115 - lives for the process
                "CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace"
            )
        if not usable(sys.stderr):
            sys.stderr = open(  # noqa: SIM115 - lives for the process
                "CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace"
            )
        if not usable(sys.stdin):
            sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")  # noqa: SIM115
    except OSError:
        pass


def enable_windows_dpi_awareness() -> None:
    """Opt in to DPI awareness before Tk starts.

    Without this, Windows bitmap-stretches the whole window on scaled displays:
    text is blurry and the fixed pixel geometry no longer fits the content."""
    if os.name != "nt":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001 - purely cosmetic
        return


def payload_dir(explicit: Path | None = None) -> Path:
    if explicit is not None:
        root = explicit.resolve()
    elif getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        root = Path(sys._MEIPASS) / "payload"
    else:
        configured = os.environ.get("LLM_WIKI_SETUP_PAYLOAD")
        root = Path(configured).expanduser().resolve() if configured else REPO_ROOT / ".setup-payload"
    required = {
        "suite.zip",
        "python.zip",
        "windows-toolchain.lock.json",
        "component-manifest.json",
        "setup-payload.json",
    }
    missing = sorted(name for name in required if not (root / name).is_file())
    if missing:
        raise SetupError(f"Setup payload is incomplete at {root}: {', '.join(missing)}")
    payload_manifest = load_json(root / "setup-payload.json")
    if payload_manifest.get("schema") != 1:
        raise SetupError("unsupported Setup payload manifest")
    files = payload_manifest.get("files")
    if not isinstance(files, dict):
        raise SetupError("Setup payload manifest has no files map")
    for name in sorted(required - {"setup-payload.json"}):
        expected = files.get(name)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise SetupError(f"Setup payload has no valid hash for {name}")
        actual = sha256_file(root / name)
        if actual != expected:
            raise SetupError(
                f"Setup payload hash mismatch for {name}: expected {expected}, got {actual}"
            )
    return root


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SetupError(f"expected JSON object: {path}")
    return value


def embedded_json(payload: Path, name: str) -> dict:
    return load_json(payload / name)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


VERSION_DIR_MAX = 32


def version_dir_name(version: str) -> str:
    """On-disk directory name for a managed version.

    Component versions concatenate every pinned package, and asr-zh's runs to
    77 characters over a tree whose own members reach 152 — together they push
    real files past Windows' 260-char MAX_PATH (9 of them overflow even under
    the shortest possible home path, 70 under a typical one), so extraction
    fails with ENOENT.  Long names collapse to a truncated prefix plus a
    digest, which stays unique and stable across runs; the exact version is
    still recorded in the tree's marker file."""
    if len(version) <= VERSION_DIR_MAX:
        return version
    digest = hashlib.sha256(version.encode("utf-8")).hexdigest()[:12]
    return f"{version[:VERSION_DIR_MAX - 13].rstrip('+.-')}.{digest}"


def long_path(path: Path | str) -> str:
    """Extended-length form of *path* so Windows APIs skip the MAX_PATH check.

    Applies to whole subtrees, so it keeps deep component trees extractable
    and removable regardless of the LongPathsEnabled machine policy."""
    text = os.path.abspath(str(path))
    if os.name != "nt" or text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def remove_tree(path: Path) -> None:
    shutil.rmtree(long_path(path), ignore_errors=True)


def safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    try:
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                member = Path(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    raise SetupError(f"unsafe zip member: {info.filename}")
                resolved = (root / member).resolve()
                if resolved != root and root not in resolved.parents:
                    raise SetupError(f"unsafe zip member: {info.filename}")
            bundle.extractall(long_path(destination))
    except zipfile.BadZipFile as exc:
        raise SetupError(f"invalid zip archive: {archive}") from exc


def replace_with_retries(source: Path, target: Path) -> None:
    """os.replace with backoff: antivirus and indexer scans hold handles inside
    freshly written trees, and Windows then fails the rename with WinError 5."""
    delay = 0.2
    deadline = time.monotonic() + 30.0
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            if time.monotonic() >= deadline:
                raise SetupError(
                    f"cannot move {source} to {target} ({exc}); another program "
                    "(likely antivirus or a sync client) is still holding the "
                    "path — close it or exclude the install directory, then "
                    "rerun Setup"
                ) from exc
            time.sleep(delay)
            delay = min(delay * 2, 5.0)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        temp.write_bytes(data)
        replace_with_retries(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def receipt_path(home: Path) -> Path:
    return home / SETUP_DIR / RECEIPT_NAME


def read_receipt(home: Path) -> dict | None:
    path = receipt_path(home)
    if not path.is_file():
        return None
    value = load_json(path)
    if value.get("schema") != 1 or value.get("platform") != "windows":
        raise SetupError(f"unsupported Setup receipt: {path}")
    declared_home = value.get("home")
    if not isinstance(declared_home, str) or not declared_home:
        raise SetupError(f"Setup receipt has no managed home: {path}")
    if Path(declared_home).expanduser().resolve() != home.expanduser().resolve():
        raise SetupError(f"Setup receipt home does not match its location: {path}")
    return value


def write_receipt(home: Path, receipt: dict) -> None:
    atomic_write(
        receipt_path(home),
        (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def read_suite_registry_from_zip(payload: Path, name: str) -> dict:
    try:
        with zipfile.ZipFile(payload / "suite.zip") as bundle:
            return json.loads(bundle.read(name).decode("utf-8"))
    except (KeyError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise SetupError(f"invalid embedded suite registry {name}: {exc}") from exc


def host_rows(payload: Path) -> list[dict]:
    bootstrap = read_suite_registry_from_zip(payload, "registry/bootstrap.json")
    rows = []
    for host, spec in (bootstrap.get("agent_hosts") or {}).items():
        if not isinstance(spec, dict):
            continue
        detect = Path(str(spec.get("detect_dir", ""))).expanduser()
        skills = Path(str(spec.get("skills_dir", ""))).expanduser()
        rows.append({
            "id": host,
            "detect_dir": str(detect),
            "skills_dir": str(skills),
            "detected": detect.is_dir(),
        })
    return rows


def is_reparse_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


def create_directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        # Junctions, not symlinks: junction creation needs no privilege and
        # supports absolute cross-volume targets, which is the whole point.
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            creationflags=hidden_console_flags(),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise SetupError(f"cannot create junction {link} -> {target}: {detail}")
    else:
        os.symlink(target, link, target_is_directory=True)


def ensure_data_root(home_link: Path, data_root: Path) -> None:
    """Anchor the managed home and the wikis root on another drive.

    Every consumer — skills, doctor, the Browser app, agent docs — keeps using
    the user-profile paths; only the bytes move to ``data_root`` behind NTFS
    junctions.  Existing real directories are never migrated: Setup stops and
    leaves them for the user to resolve."""
    data_root = data_root.expanduser().resolve()
    pairs = (
        (home_link, data_root / "home"),
        (Path(DEFAULT_WIKIS).expanduser(), data_root / "wikis"),
    )
    for link, target in pairs:
        if target == link or link in target.parents:
            raise SetupError(f"data root {data_root} cannot live inside {link}")
        target.mkdir(parents=True, exist_ok=True)
        if link.exists() and link.resolve() == target.resolve():
            continue
        if is_reparse_link(link):
            raise SetupError(
                f"{link} already links to {link.resolve()}; remove the link or "
                "choose that location instead"
            )
        if link.is_dir():
            if any(link.iterdir()):
                raise SetupError(
                    f"{link} already holds data on the system drive; move it "
                    "aside manually or keep the default install location"
                )
            link.rmdir()
        elif link.exists():
            raise SetupError(f"{link} exists and is not a directory")
        link.parent.mkdir(parents=True, exist_ok=True)
        create_directory_link(link, target)
        emit("data-root-linked", link=str(link), target=str(target))


STALE_HEX = re.compile(r"[0-9a-f]{32}")


def cleanup_stale_workdirs(home: Path) -> None:
    """Reclaim uuid-tagged hidden work paths that earlier aborted runs left
    behind (a scanner holding handles makes their cleanup silently fail)."""
    parents = [
        home / "suite" / "versions",
        home / "runtime",
        home / SETUP_DIR,
        home / SETUP_DIR / "downloads",
    ]
    components = home / "components"
    if components.is_dir():
        parents.extend(child / "versions" for child in components.iterdir())
    removed = []
    for parent in parents:
        if not parent.is_dir():
            continue
        for entry in parent.iterdir():
            if not entry.name.startswith(".") or not STALE_HEX.search(entry.name):
                continue
            try:
                if entry.is_dir() and not is_reparse_link(entry):
                    shutil.rmtree(long_path(entry))
                else:
                    entry.unlink()
                removed.append(str(entry))
            except OSError:
                continue
    if removed:
        emit("stale-workdirs-removed", paths=removed)


def preflight_disk_space(home: Path, manifest: dict, components: list[str]) -> None:
    # zip + staging extract + swap headroom; models compress poorly, so 3x the
    # archive size is the conservative per-component estimate.
    required = 300 * 1024 * 1024
    for component in components:
        spec = (manifest.get("components") or {}).get(component)
        if not isinstance(spec, dict):
            continue
        version = str(spec.get("version", ""))
        marker = (
            home / "components" / component / "versions"
            / version_dir_name(version) / ".llm-wiki-component.json"
        )
        if marker.is_file():
            continue
        required += int(spec.get("size", 0)) * 3
    probe = home
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < required:
        raise SetupError(
            f"not enough disk space for {home}: about "
            f"{required // (1024 * 1024)} MiB is needed but only "
            f"{free // (1024 * 1024)} MiB is free; free up space or choose "
            "another drive as the install location"
        )
    emit("disk-preflight", required=required, free=free)


IMAGE_FILE_MACHINE_AMD64 = 0x8664
IMAGE_FILE_MACHINE_ARM64 = 0xAA64


def pe_machine(path: str) -> int | None:
    """Machine field of a PE image header, or None when unreadable/not PE."""
    try:
        with open(path, "rb") as fh:
            if fh.read(2) != b"MZ":
                return None
            fh.seek(0x3C)
            offset = int.from_bytes(fh.read(4), "little")
            fh.seek(offset)
            if fh.read(4) != b"PE\0\0":
                return None
            return int.from_bytes(fh.read(2), "little")
    except OSError:
        return None


def running_x64_build() -> bool:
    """True when the currently executing image is an x64 PE.

    This is the load-bearing ARM64 signal: the Setup exe and the managed
    runtime are x64-only builds, so the fact that this code executes at all
    proves the machine runs x64 code (natively or through Windows-on-ARM
    emulation) — regardless of what ``platform.machine()`` or the WOW64 APIs
    claim.  Field report 2026-07-19: on a real ARM64 Win11 device both
    PROCESSOR_ARCHITEW6432 and the IsWow64Process2 probe missed the
    emulation, so architecture APIs cannot be the primary gate."""
    return os.name == "nt" and pe_machine(sys.executable) == IMAGE_FILE_MACHINE_AMD64


def wow64_process2_probe() -> tuple[bool, int, int]:
    """(call ok, processMachine, nativeMachine) from IsWow64Process2."""
    if os.name != "nt":
        return (False, 0, 0)
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.IsWow64Process2.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ushort),
            ctypes.POINTER(ctypes.c_ushort),
        ]
        kernel32.IsWow64Process2.restype = ctypes.c_int
        process_machine = ctypes.c_ushort()
        native_machine = ctypes.c_ushort()
        ok = kernel32.IsWow64Process2(
            kernel32.GetCurrentProcess(),
            ctypes.byref(process_machine),
            ctypes.byref(native_machine),
        )
        return (bool(ok), process_machine.value, native_machine.value)
    except Exception:  # noqa: BLE001 - best-effort platform probe
        return (False, 0, 0)


def x64_emulation_on_arm64() -> bool:
    """True when this x64 build runs under Windows-on-ARM x64 emulation.

    CPython 3.12's ``platform.machine()`` reports the *native* machine, so an
    emulated x64 process on ARM64 Windows still sees ``ARM64``.  Secondary
    signal behind ``running_x64_build``; kept for non-frozen script runs
    under an arbitrary interpreter."""
    if os.environ.get("PROCESSOR_ARCHITEW6432", "").lower() == "arm64":
        return True
    ok, _process_machine, native_machine = wow64_process2_probe()
    return ok and native_machine == IMAGE_FILE_MACHINE_ARM64


def validate_platform(allow_test_platform: bool = False) -> None:
    if platform.system().lower() != "windows" and not allow_test_platform:
        raise SetupError(
            "My-LLM-Wiki-Setup.exe is Windows-only; macOS and Linux keep bootstrap.sh v4"
        )
    if allow_test_platform:
        return
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64", "x64"}:
        return
    if running_x64_build():
        emit("x64-emulation", machine=platform.machine(), signal="pe-self-check")
        return
    if x64_emulation_on_arm64():
        emit("x64-emulation", machine=platform.machine(), signal="wow64-probe")
        return
    ok, process_machine, native_machine = wow64_process2_probe()
    raise SetupError(
        f"unsupported Windows architecture: {platform.machine()} "
        f"(exe-machine={pe_machine(sys.executable) or 0:#06x}, "
        f"arch-env={os.environ.get('PROCESSOR_ARCHITECTURE', '-')}, "
        f"wow64-env={os.environ.get('PROCESSOR_ARCHITEW6432', '-')}, "
        f"iswow64process2={'ok' if ok else 'fail'}"
        f":{process_machine:#06x}/{native_machine:#06x})"
    )


def ensure_suite(payload: Path, home: Path) -> tuple[Path, str]:
    registry = read_suite_registry_from_zip(payload, "registry/skills.json")
    version = registry.get("pack_version")
    if not isinstance(version, str) or not version:
        raise SetupError("embedded suite has no pack_version")
    root = home / "suite" / "versions" / version_dir_name(version)
    if root.is_dir():
        installed = load_json(root / "registry" / "skills.json")
        if installed.get("pack_version") != version:
            raise SetupError(f"managed suite version directory is corrupt: {root}")
        return root, version
    staging = root.parent / f".{uuid.uuid4().hex}.staging"
    notify_progress(phase="正在安装技能套件")
    try:
        safe_extract(payload / "suite.zip", staging)
        installed = load_json(staging / "registry" / "skills.json")
        if installed.get("pack_version") != version:
            raise SetupError("extracted suite pack_version mismatch")
        root.parent.mkdir(parents=True, exist_ok=True)
        replace_with_retries(staging, root)
    finally:
        remove_tree(staging)
    emit("suite-ready", version=version, path=str(root))
    return root, version


def enable_embedded_python(runtime: Path) -> None:
    pth = list(runtime.glob("python*._pth"))
    if len(pth) != 1:
        raise SetupError(f"private Python has an unexpected layout: {runtime}")
    pth[0].write_text(
        "python312.zip\n.\nLib\nLib/site-packages\nimport site\n",
        encoding="utf-8",
    )
    (runtime / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)


def ensure_runtime(payload: Path, home: Path, lock: dict) -> Path:
    expected = str((lock.get("python") or {}).get("sha256", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise SetupError("embedded Python lock has no valid sha256")
    actual = sha256_file(payload / "python.zip")
    if actual != expected:
        raise SetupError(
            f"embedded Python hash mismatch: expected {expected}, got {actual}"
        )
    runtime = home / "runtime" / "python"
    executable = runtime / "python.exe"
    marker = runtime / ".llm-wiki-runtime.json"
    if executable.is_file() and marker.is_file():
        current = load_json(marker)
        if current.get("version") == lock["python"]["version"]:
            return runtime
    staging = runtime.parent / f".python.{uuid.uuid4().hex}.staging"
    backup = runtime.parent / f".python.backup.{uuid.uuid4().hex}"
    notify_progress(phase="正在安装私有 Python 运行时")
    try:
        safe_extract(payload / "python.zip", staging)
        enable_embedded_python(staging)
        (staging / ".llm-wiki-runtime.json").write_text(
            json.dumps({
                "schema": 1,
                "version": lock["python"]["version"],
                "source_sha256": lock["python"]["sha256"],
            }, indent=2) + "\n",
            encoding="utf-8",
        )
        runtime.parent.mkdir(parents=True, exist_ok=True)
        if runtime.exists():
            replace_with_retries(runtime, backup)
        replace_with_retries(staging, runtime)
        remove_tree(backup)
    except Exception:
        if backup.exists() and not runtime.exists():
            replace_with_retries(backup, runtime)
        raise
    finally:
        remove_tree(staging)
    emit("runtime-ready", version=lock["python"]["version"], path=str(runtime))
    return runtime


def copy_setup_executable(home: Path) -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    source = Path(sys.executable).resolve()
    destination = home / SETUP_DIR / SETUP_EXE
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        if source == destination.resolve():
            return destination
    except OSError:
        pass
    temp = destination.parent / f".{destination.name}.{uuid.uuid4().hex}"
    shutil.copy2(source, temp)
    replace_with_retries(temp, destination)
    return destination


def download_component(
    component: str,
    spec: dict,
    manifest: dict,
    home: Path,
    asset_dir: Path | None,
) -> Path:
    asset = str(spec.get("asset", ""))
    expected = str(spec.get("sha256", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise SetupError(f"component {component} has no valid sha256")
    if Path(asset).name != asset or not asset.endswith(".zip"):
        raise SetupError(f"component {component} has an unsafe asset name")
    if asset_dir is not None:
        local = asset_dir / asset
        if not local.is_file():
            raise SetupError(f"component asset is missing: {local}")
        if sha256_file(local) != expected:
            raise SetupError(f"component asset hash mismatch: {local}")
        return local

    cache = home / SETUP_DIR / "downloads" / f"{expected}-{asset}"
    if cache.is_file() and sha256_file(cache) == expected:
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    failures = []
    tag = manifest.get("release_tag", "")
    for template in manifest.get("sources", []):
        if not isinstance(template, str):
            continue
        url = template.replace("{tag}", str(tag)).replace("{asset}", asset)
        if not url.startswith("https://"):
            failures.append(f"refused non-HTTPS source: {url}")
            continue
        temp = cache.parent / f".{asset}.{uuid.uuid4().hex}.part"
        digest = hashlib.sha256()
        total = 0
        emit("component-download", component=component, source=url)
        notify_progress(phase=f"正在下载 {component}（{asset}）", component=component)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "My-LLM-Wiki-Setup"})
            with urllib.request.urlopen(request, timeout=30) as response, temp.open("wb") as sink:
                declared = response.headers.get("Content-Length")
                expected_total = int(declared) if declared and declared.isdigit() else 0
                last_notice = 0.0
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD:
                        raise SetupError(f"component exceeds {MAX_DOWNLOAD} bytes")
                    digest.update(chunk)
                    sink.write(chunk)
                    now = time.monotonic()
                    if now - last_notice >= 0.25:
                        last_notice = now
                        notify_progress(
                            component=component, received=total, total=expected_total
                        )
                notify_progress(component=component, received=total, total=expected_total)
            if digest.hexdigest() != expected:
                raise SetupError(
                    f"component hash mismatch: expected {expected}, got {digest.hexdigest()}"
                )
            replace_with_retries(temp, cache)
            return cache
        except (OSError, urllib.error.URLError, SetupError) as exc:
            failures.append(f"{url}: {exc}")
        finally:
            temp.unlink(missing_ok=True)
    raise SetupError(
        f"all release sources failed for component {component}: " + "; ".join(failures)
    )


def expand_component_argv(
    raw: object,
    *,
    home: Path,
    suite: Path,
    runtime: Path,
    component: Path,
) -> list[str]:
    if not isinstance(raw, list) or not raw or any(
        not isinstance(arg, str) or not arg for arg in raw
    ):
        raise SetupError("component manifest has invalid argv")
    replacements = {
        "home": str(home),
        "suite": str(suite),
        "runtime": str(runtime),
        "component": str(component),
    }
    out = []
    for arg in raw:
        for key, value in replacements.items():
            arg = arg.replace("{" + key + "}", value)
        out.append(arg)
    return out


def selected_runtime_env(component: str, spec: dict) -> dict[str, str]:
    routes = spec.get("runtime_env") or {}
    if not isinstance(routes, dict) or not routes:
        return {}
    route = "global"
    if component == "asr-other":
        request = urllib.request.Request(
            "https://huggingface.co/", method="HEAD", headers={"User-Agent": "My-LLM-Wiki-Setup"}
        )
        try:
            with urllib.request.urlopen(request, timeout=3):
                pass
        except Exception:
            route = "cn"
    values = routes.get(route, {})
    return dict(values) if isinstance(values, dict) else {}


def postcheck_component(
    component: str,
    spec: dict,
    root: Path,
    home: Path,
    suite: Path,
    runtime: Path,
    *,
    skip: bool = False,
) -> tuple[dict[str, dict], dict[str, str], dict[str, dict[str, str]]]:
    if not skip:
        notify_progress(phase=f"正在校验 {component}", component=component)
    tools: dict[str, dict] = {}
    profiles: dict[str, str] = {}
    runtime_env: dict[str, dict[str, str]] = {}
    for name, tool in (spec.get("tools") or {}).items():
        if not isinstance(tool, dict):
            raise SetupError(f"invalid tool declaration for {component}/{name}")
        prefix = expand_component_argv(
            tool.get("argv"), home=home, suite=suite, runtime=runtime, component=root
        )
        if not Path(prefix[0]).is_file():
            raise SetupError(f"component {component} is missing {prefix[0]}")
        postcheck = tool.get("postcheck") or []
        if not skip:
            result = subprocess.run(
                [*prefix, *postcheck],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
                check=False,
                creationflags=hidden_console_flags(),
            )
            if result.returncode != 0:
                raise SetupError(
                    f"component {component} postcheck failed for {name}: {result.returncode}"
                )
        tools[name] = {"argv": prefix, "component": component}

    profile = spec.get("python_profile")
    if isinstance(profile, str) and profile:
        python = root / "python.exe"
        if not python.is_file():
            raise SetupError(f"component {component} has no private python.exe")
        env_values = selected_runtime_env(component, spec)
        if not skip:
            env = os.environ.copy()
            env.update(env_values)
            result = subprocess.run(
                [str(python), *(spec.get("postcheck") or [])],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
                check=False,
                creationflags=hidden_console_flags(),
            )
            if result.returncode != 0:
                raise SetupError(
                    f"component {component} Python postcheck failed: {result.returncode}"
                )
        profiles[profile] = str(python)
        runtime_env[profile] = env_values
    return tools, profiles, runtime_env


def ensure_component(
    component: str,
    manifest: dict,
    home: Path,
    suite: Path,
    runtime: Path,
    asset_dir: Path | None,
    *,
    skip_postcheck: bool = False,
) -> dict:
    spec = (manifest.get("components") or {}).get(component)
    if not isinstance(spec, dict):
        raise SetupError(f"unknown Windows component: {component}")
    version = str(spec.get("version", ""))
    if not version:
        raise SetupError(f"component {component} has no version")
    root = home / "components" / component / "versions" / version_dir_name(version)
    marker = root / ".llm-wiki-component.json"
    expected_marker = {
        "schema": 1,
        "component": component,
        "version": version,
        "asset": spec["asset"],
        "sha256": spec["sha256"],
    }
    marker_valid = False
    if marker.is_file():
        try:
            marker_valid = load_json(marker) == expected_marker
        except SetupError:
            marker_valid = False

    def replace_component_bytes() -> None:
        archive = download_component(component, spec, manifest, home, asset_dir)
        staging = root.parent / f".{uuid.uuid4().hex}.staging"
        backup = root.parent / f".{uuid.uuid4().hex}.backup"
        try:
            notify_progress(phase=f"正在解压 {component}", component=component)
            safe_extract(archive, staging)
            (staging / ".llm-wiki-component.json").write_text(
                json.dumps(expected_marker, indent=2) + "\n", encoding="utf-8"
            )
            root.parent.mkdir(parents=True, exist_ok=True)
            if root.exists():
                replace_with_retries(root, backup)
            replace_with_retries(staging, root)
            remove_tree(backup)
        except Exception:
            if backup.exists() and not root.exists():
                replace_with_retries(backup, root)
            raise
        finally:
            remove_tree(staging)

    if not marker_valid:
        replace_component_bytes()
    try:
        tools, profiles, runtime_env = postcheck_component(
            component, spec, root, home, suite, runtime, skip=skip_postcheck
        )
    except SetupError:
        if not marker_valid or skip_postcheck:
            raise
        emit("component-repair", component=component, reason="postcheck-failed")
        replace_component_bytes()
        tools, profiles, runtime_env = postcheck_component(
            component, spec, root, home, suite, runtime, skip=False
        )
    emit("component-ready", component=component, version=version, path=str(root))
    return {
        "version": version,
        "path": str(root),
        "asset": spec["asset"],
        "sha256": spec["sha256"],
        "tools": tools,
        "python_profiles": profiles,
        "runtime_env": runtime_env,
    }


def stage_opencli_extension(home: Path, component_state: dict) -> list[str]:
    root = Path(component_state["path"])
    extension = root / "extension"
    manifest = extension / "manifest.json"
    if not manifest.is_file():
        raise SetupError(f"web component has no Browser Bridge extension: {manifest}")
    try:
        version = str(json.loads(manifest.read_text(encoding="utf-8")).get("version", ""))
    except json.JSONDecodeError:
        version = ""
    destination = home / "opencli-extension"
    destination.mkdir(parents=True, exist_ok=True)
    pointer = {
        "schema": 1,
        "version": version,
        "path": str(extension),
        "asset": component_state["asset"],
        "source": "windows-setup-component",
    }
    atomic_write(
        destination / "current.json",
        (json.dumps(pointer, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return [
        "在 Chrome 地址栏打开 chrome://extensions",
        "打开右上角的「开发者模式」开关",
        f"点击「加载已解压的扩展程序」，选择：{extension}",
        "加载完成后重新运行 doctor 验证（安装器完成页可一键运行）",
    ]


def git_config_root(path: Path) -> Path | None:
    for parent in (path, *path.parents):
        config = parent / ".git" / "config"
        if config.is_file() and (parent / "registry" / "skills.json").is_file():
            return parent
    return None


def official_checkout(path: Path) -> bool:
    root = git_config_root(path)
    if root is None:
        return False
    try:
        text = (root / ".git" / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    urls = re.findall(r"^\s*url\s*=\s*(\S+)\s*$", text, flags=re.MULTILINE)
    return any(url in OFFICIAL_REMOTES for url in urls)


def owned_or_legacy_destination(path: Path, home: Path) -> bool:
    manifest = path / ".llm-wiki-install.json"
    if manifest.is_file():
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if value.get("installer") == "windows-setup":
            return True
        source = value.get("source_repo")
        if isinstance(source, str) and source and official_checkout(Path(source)):
            return True
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    managed_suite = home / "suite"
    if resolved == managed_suite or managed_suite in resolved.parents:
        return True
    return official_checkout(resolved)


def setup_copy_matches(path: Path, install_id: str) -> bool:
    try:
        value = json.loads(
            (path / ".llm-wiki-install.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return False
    return (
        value.get("schema") == 1
        and value.get("installer") == "windows-setup"
        and value.get("distribution") == "managed-pack"
        and value.get("install_id") == install_id
    )


def install_browser_app(suite: Path, runtime: Path) -> None:
    """Run the suite's release-first Browser installer silently.

    The NSIS build carries the Tauri updater, so after this one managed
    install the app keeps itself current; Setup never owns Browser updates.

    `--open-web` launches the app and opens its local page once the loopback
    server answers, so the user sees their wiki without a second step. It is
    best-effort inside the installer and cannot fail this call."""
    script = suite / "scripts" / "install-browser.py"
    if not script.is_file():
        raise SetupError(f"suite has no Browser installer: {script}")
    result = subprocess.run(
        [str(runtime / "python.exe"), str(script), "--windows-silent", "--open-web"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        # The child runs in UTF-8 mode; decoding with the parent's locale
        # codec (GBK on Chinese Windows) kills the reader threads.
        encoding="utf-8",
        errors="replace",
        timeout=1800,
        check=False,
        env={**os.environ, "PYTHONUTF8": "1"},
        creationflags=hidden_console_flags(),
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode != 0:
        raise SetupError(f"Browser installer exited with {result.returncode}")


def import_suite_modules(suite: Path):
    scripts = str(suite / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    for name in ("install", "initialize_wiki"):
        if name in sys.modules:
            del sys.modules[name]
    install = importlib.import_module("install")
    initialize = importlib.import_module("initialize_wiki")
    return install, initialize


def rollback_skill_plan(install, plan: dict) -> None:
    for item in reversed(plan.get("actions", [])):
        if item.get("state") != "installed":
            continue
        destination = Path(item["destination"])
        try:
            install.remove_path(destination)
            backup = item.get("backup")
            if backup and Path(backup).exists():
                os.replace(backup, destination)
        except OSError as exc:
            emit("rollback-error", destination=str(destination), error=str(exc))


def doctor_command(
    runtime: Path,
    suite: Path,
    hosts: list[str],
    custom_targets: list[str] | None = None,
) -> list[str]:
    command = [str(runtime / "python.exe"), str(suite / "scripts" / "doctor.py")]
    for host in hosts:
        command += ["--host", host]
    for target in custom_targets or []:
        command += ["--custom-target", target]
    return command


def run_doctor_capture(home: Path) -> tuple[int, str]:
    """Re-run the full doctor from the receipt, returning output for the GUI."""
    receipt = read_receipt(home)
    if receipt is None:
        return 2, "Setup receipt is missing; run the install first."
    result = subprocess.run(
        doctor_command(
            Path(receipt["runtime"]),
            Path(receipt["suite"]),
            list(receipt.get("hosts", [])),
            list(receipt.get("custom_targets", [])),
        ),
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=300,
        check=False,
        creationflags=hidden_console_flags(),
        env={
            **os.environ,
            "PYTHONUTF8": "1",
            "LLM_WIKI_SETUP_RECEIPT": str(receipt_path(home)),
        },
    )
    output = (result.stdout or "") + (f"\n{result.stderr}" if result.stderr else "")
    return result.returncode, output.strip()


def installed_browser_executable(home: Path) -> Path | None:
    """Resolve the Browser exe from the install receipt its installer wrote."""
    receipt = read_receipt(home)
    if receipt is None:
        return None
    suite = Path(str(receipt.get("suite", "")))
    try:
        config = load_json(suite / "registry" / "bootstrap.json")
        pointer = config["browser"]["install_receipt"]["path"]
        browser_receipt = load_json(Path(str(pointer)).expanduser())
    except (SetupError, KeyError, TypeError):
        return None
    target = Path(str(browser_receipt.get("target", "")))
    return target if target.is_file() else None


def chrome_executable() -> Path | None:
    """Locate chrome.exe so the GUI can open chrome://extensions directly;
    the chrome:// scheme has no shell handler, so a plain startfile cannot."""
    if os.name != "nt":
        return None
    try:
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(
                    hive,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                    r"\App Paths\chrome.exe",
                ) as key:
                    value = str(winreg.QueryValueEx(key, None)[0])
            except OSError:
                continue
            if value and Path(value).is_file():
                return Path(value)
    except ImportError:
        pass
    for base in (
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("LOCALAPPDATA"),
    ):
        if base:
            candidate = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if candidate.is_file():
                return candidate
    return None


def browser_bridge_extension_dir(home: Path) -> Path | None:
    receipt = read_receipt(home)
    row = ((receipt or {}).get("components") or {}).get("web")
    if not isinstance(row, dict):
        return None
    extension = Path(str(row.get("path", ""))) / "extension"
    return extension if extension.is_dir() else None


def run_doctor(
    runtime: Path,
    suite: Path,
    hosts: list[str],
    home: Path,
    custom_targets: list[str] | None = None,
) -> int:
    result = subprocess.run(
        doctor_command(runtime, suite, hosts, custom_targets),
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=180,
        check=False,
        creationflags=hidden_console_flags(),
        env={
            **os.environ,
            "PYTHONUTF8": "1",
            "LLM_WIKI_SETUP_RECEIPT": str(receipt_path(home)),
        },
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def build_receipt(
    *,
    old: dict | None,
    manifest: dict,
    lock: dict,
    home: Path,
    suite: Path,
    runtime: Path,
    pack_version: str,
    hosts: list[str],
    custom_targets: list[str],
    components: dict[str, dict],
) -> dict:
    tools: dict[str, dict] = {}
    profiles = {"core": str(runtime / "python.exe")}
    runtime_env: dict[str, dict[str, str]] = {}
    component_rows = {}
    for component, state in components.items():
        tools.update(state.get("tools", {}))
        profiles.update(state.get("python_profiles", {}))
        runtime_env.update(state.get("runtime_env", {}))
        component_rows[component] = {
            key: state[key] for key in ("version", "path", "asset", "sha256")
        }
    install_id = old.get("install_id") if old else None
    return {
        "schema": 1,
        "install_id": install_id or uuid.uuid4().hex,
        "installer_version": manifest.get("setup_version", lock.get("setup_version")),
        "platform": "windows",
        "architecture": "x86_64",
        "release_tag": manifest.get("release_tag", ""),
        "home": str(home),
        "suite": str(suite),
        # Store the concrete private-Python root used by component argv
        # expansion.  The purge boundary remains ``home/runtime``; this field
        # is an execution contract, not the ownership boundary.
        "runtime": str(runtime),
        "pack_version": pack_version,
        "hosts": sorted(set([*(old or {}).get("hosts", []), *hosts])),
        # Explicit skills directories the user picked in the UI. Kept beside
        # ``hosts`` (not folded into it) so uninstall and doctor can address a
        # path that no registry host id names.
        "custom_targets": sorted(
            set([*(old or {}).get("custom_targets", []), *custom_targets])
        ),
        "components": component_rows,
        "tools": tools,
        "python_profiles": profiles,
        "runtime_env": runtime_env,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def install_flow(
    *,
    hosts: list[str],
    custom_targets: list[str] | None = None,
    components: list[str],
    home: Path,
    payload: Path,
    asset_dir: Path | None,
    allow_test_platform: bool = False,
    skip_postcheck: bool = False,
    guidance: list[str] | None = None,
    browser: bool = False,
) -> int:
    validate_platform(allow_test_platform)
    rows = {row["id"]: row for row in host_rows(payload)}
    custom_targets = list(dict.fromkeys(custom_targets or []))
    if not hosts and not custom_targets:
        raise SetupError("select at least one agent host or skills directory")
    unknown = sorted(set(hosts) - set(rows))
    if unknown:
        raise SetupError("unknown host(s): " + ", ".join(unknown))

    lock = embedded_json(payload, "windows-toolchain.lock.json")
    manifest = embedded_json(payload, "component-manifest.json")
    if lock.get("schema") != 1 or manifest.get("schema") != 1:
        raise SetupError("unsupported Windows Setup manifest")
    cleanup_stale_workdirs(home)
    preflight_disk_space(home, manifest, list(dict.fromkeys(components)))
    suite, pack_version = ensure_suite(payload, home)
    runtime = ensure_runtime(payload, home, lock)
    copy_setup_executable(home)
    old = read_receipt(home)

    requested = list(dict.fromkeys(components))
    unknown_components = sorted(set(requested) - set(manifest.get("components", {})))
    if unknown_components:
        raise SetupError("unknown component(s): " + ", ".join(unknown_components))
    component_states: dict[str, dict] = {}
    for component in requested:
        component_states[component] = ensure_component(
            component,
            manifest,
            home,
            suite,
            runtime,
            asset_dir,
            skip_postcheck=skip_postcheck,
        )
    # Keep already installed components across a core/host repair.
    if old:
        for component, row in (old.get("components") or {}).items():
            if component in component_states:
                continue
            spec = (manifest.get("components") or {}).get(component)
            root = Path(str(row.get("path", ""))) if isinstance(row, dict) else Path()
            if isinstance(spec, dict) and root.is_dir():
                tools, profiles, env = postcheck_component(
                    component, spec, root, home, suite, runtime, skip=skip_postcheck
                )
                component_states[component] = {
                    **row,
                    "tools": tools,
                    "python_profiles": profiles,
                    "runtime_env": env,
                }

    receipt = build_receipt(
        old=old,
        manifest=manifest,
        lock=lock,
        home=home,
        suite=suite,
        runtime=runtime,
        pack_version=pack_version,
        hosts=hosts,
        custom_targets=custom_targets,
        components=component_states,
    )
    install, initialize = import_suite_modules(suite)
    config = install.load_json(suite / "registry" / "bootstrap.json")
    registry = install.load_json(suite / "registry" / "skills.json")
    old_receipt = receipt_path(home).read_bytes() if receipt_path(home).is_file() else None
    plan = None
    try:
        with install.install_lock(config):
            plan = install.build_plan(
                config, registry, hosts, custom_targets, [], "copy", True
            )
            # A digest-current generic v4 copy is not current for the Windows
            # hard-cutover contract. Every selected copy must carry this
            # receipt's install_id, otherwise repair it through backup/replace.
            for item in plan["actions"]:
                destination = Path(item["destination"])
                if item["state"] == "current" and not setup_copy_matches(
                    destination, receipt["install_id"]
                ):
                    item["state"] = "replace"
            foreign = [
                item["destination"]
                for item in plan["actions"]
                if item["state"] == "replace"
                and not owned_or_legacy_destination(Path(item["destination"]), home)
            ]
            if foreign:
                raise SetupError(
                    "foreign skill destinations require manual resolution; nothing was replaced: "
                    + ", ".join(foreign)
                )
            plan["copy_manifest"] = {
                "installer": "windows-setup",
                "install_id": receipt["install_id"],
                "distribution": "managed-pack",
            }
            notify_progress(phase="正在把技能安装到 agent 宿主")
            install.apply_plan(config, plan)
            write_receipt(home, receipt)
            notify_progress(phase="正在初始化 wiki")
            initialize.ensure_wiki(
                config,
                python_executable=str(runtime / "python.exe"),
            )
        if not skip_postcheck:
            notify_progress(phase="正在运行 doctor 检查")
        doctor_status = (
            0
            if skip_postcheck
            else run_doctor(runtime, suite, hosts, home, custom_targets)
        )
        if doctor_status not in {0, 3}:
            raise SetupError(f"doctor failed with status {doctor_status}")
    except Exception:
        if plan is not None:
            rollback_skill_plan(install, plan)
        if old_receipt is None:
            receipt_path(home).unlink(missing_ok=True)
        else:
            atomic_write(receipt_path(home), old_receipt)
        raise

    if "web" in component_states:
        steps = stage_opencli_extension(home, component_states["web"])
        for index, step in enumerate(steps, start=1):
            print(f"Browser Bridge {index}. {step}")
        if guidance is not None:
            guidance.append("Browser Bridge（Chrome 扩展）还需要手动加载一次：")
            guidance.extend(f"  {index}. {step}" for index, step in enumerate(steps, start=1))
    if browser:
        notify_progress(phase="正在安装 Browser 桌面应用")
        browser_error = ""
        try:
            install_browser_app(suite, runtime)
        except (SetupError, subprocess.SubprocessError, OSError) as exc:
            browser_error = str(exc)
            print(f"Browser install failed: {browser_error}", file=sys.stderr)
        if guidance is not None:
            guidance.append(
                "Browser 桌面应用已安装，此后会自动保持更新。"
                if not browser_error
                else "Browser 桌面应用本次未能安装；可稍后重新运行安装器，"
                "或到 release 页面手动下载。"
            )
        emit("browser-install", ok=not browser_error, error=browser_error)
    if guidance is not None:
        # Mirror the Browser FirstCaptureGuide copy so the first prompt the
        # user types matches what the app itself teaches.
        wiki_root = Path(
            str(config.get("default_wiki_root", "~/wikis/my-llm-wiki"))
        ).expanduser()
        guidance.extend([
            "",
            "第一次使用：在你的 agent（Claude Code / Codex / Hermes 等）里发送：",
            "  使用 my-llm-wiki 技能，把下面这篇内容抓取沉淀到我的知识库，并直接整理成 wiki 页面：",
            "  <把这一行换成你想收藏的链接>",
            f"  知识库 root：{wiki_root}",
        ])
    emit(
        "installed",
        hosts=hosts,
        custom_targets=custom_targets,
        components=sorted(component_states),
        pack_version=pack_version,
        doctor_status=doctor_status,
        receipt=str(receipt_path(home)),
    )
    return doctor_status


def install_components(
    *,
    components: list[str],
    home: Path,
    payload: Path,
    asset_dir: Path | None,
    allow_test_platform: bool = False,
    skip_postcheck: bool = False,
) -> int:
    validate_platform(allow_test_platform)
    old = read_receipt(home)
    if old is None:
        raise SetupError("install the core and at least one agent host first")
    return install_flow(
        hosts=list(old.get("hosts", [])),
        custom_targets=list(old.get("custom_targets", [])),
        components=[*old.get("components", {}).keys(), *components],
        home=home,
        payload=payload,
        asset_dir=asset_dir,
        allow_test_platform=allow_test_platform,
        skip_postcheck=skip_postcheck,
    )


def doctor_components(
    components: list[str], home: Path, payload: Path, *, skip: bool = False
) -> int:
    receipt = read_receipt(home)
    if receipt is None:
        raise SetupError("Windows Setup receipt is missing")
    manifest = embedded_json(payload, "component-manifest.json")
    failed = []
    for component in components:
        row = (receipt.get("components") or {}).get(component)
        spec = (manifest.get("components") or {}).get(component)
        if not isinstance(row, dict) or not isinstance(spec, dict):
            failed.append(component)
            continue
        try:
            postcheck_component(
                component,
                spec,
                Path(row["path"]),
                home,
                Path(receipt["suite"]),
                Path(receipt["runtime"]),
                skip=skip,
            )
            emit("component-ok", component=component, version=row.get("version"))
        except SetupError as exc:
            failed.append(component)
            emit("component-failed", component=component, error=str(exc))
    return 1 if failed else 0


def managed_argv(receipt: dict, name: str) -> list[str]:
    tools = receipt.get("tools") or {}
    spec = tools.get(name) if isinstance(tools, dict) else None
    raw = spec.get("argv") if isinstance(spec, dict) else None
    if not isinstance(raw, list) or not raw or any(
        not isinstance(arg, str) or not arg for arg in raw
    ):
        raise SetupError(f"managed tool is not installed: {name}")
    executable = Path(raw[0])
    raw_home = receipt.get("home")
    if not isinstance(raw_home, str) or not raw_home:
        raise SetupError("Setup receipt has no managed home")
    home = Path(raw_home).resolve()
    try:
        resolved = executable.resolve(strict=True)
    except OSError as exc:
        raise SetupError(f"managed tool executable is missing: {executable}") from exc
    if resolved != home and home not in resolved.parents:
        raise SetupError(f"managed tool executable escapes Setup home: {resolved}")
    return [str(resolved), *raw[1:]]


def _remainder(values: list[str]) -> list[str]:
    return values[1:] if values[:1] == ["--"] else values


_MSYS_PATH = re.compile(r"^/([A-Za-z])(/.*)?$")


def msys_to_windows_path(value: str) -> str | None:
    """Map /c/Users/... to C:/Users/... so bash-shaped agent paths keep working."""
    match = _MSYS_PATH.match(value)
    if match is None:
        return None
    return f"{match.group(1).upper()}:{match.group(2) or '/'}"


def normalize_script_path(value: str) -> str:
    if Path(value).is_file():
        return value
    converted = msys_to_windows_path(value)
    if converted is not None and Path(converted).is_file():
        return converted
    return value


# The private runtime is an embedded (``._pth``) Python: it neither prepends
# the script's directory to sys.path nor honors PYTHONPATH, so plain
# ``python.exe script.py`` breaks every suite script with sibling imports.
# Restore the standard file-form contract through a -c bootstrap.
_FILE_FORM_BOOTSTRAP = (
    "import os, runpy, sys\n"
    "script = sys.argv.pop(1)\n"
    "sys.argv[0] = script\n"
    "sys.path.insert(0, os.path.dirname(os.path.abspath(script)))\n"
    "runpy.run_path(script, run_name='__main__')\n"
)


def python_file_command(executable: str, values: list[str]) -> list[str]:
    if not values or values[0].startswith("-"):
        return [executable, *values]
    script = normalize_script_path(values[0])
    if not script.endswith(".py") or not Path(script).is_file():
        return [executable, script, *values[1:]]
    return [executable, "-c", _FILE_FORM_BOOTSTRAP, script, *values[1:]]


def list_managed_tools(home: Path, *, as_json: bool = False) -> int:
    """Report the receipt's managed tools and Python profiles.

    Without this the only way to see what ``tools run`` accepts was to read the
    receipt or browse the components directory by hand."""
    receipt = read_receipt(home)
    if receipt is None:
        raise SetupError("Windows Setup receipt is missing")
    tools = []
    for name in sorted((receipt.get("tools") or {})):
        try:
            argv = managed_argv(receipt, name)
        except SetupError as exc:
            tools.append({"name": name, "runnable": False, "detail": str(exc)})
        else:
            tools.append({"name": name, "runnable": True, "argv": argv})
    profiles = sorted((receipt.get("python_profiles") or {}))
    if as_json:
        print(json.dumps({"tools": tools, "python_profiles": profiles},
                         ensure_ascii=False, indent=2))
        return 0
    for tool in tools:
        mark = "ok  " if tool["runnable"] else "MISS"
        print(f"{mark} {tool['name']}: {tool.get('argv', [tool.get('detail')])[0]}")
    for profile in profiles:
        print(f"ok   python --profile {profile}")
    return 0


def run_managed_tool(name: str, values: list[str], home: Path) -> int:
    receipt = read_receipt(home)
    if receipt is None:
        raise SetupError("Windows Setup receipt is missing")
    command = [*managed_argv(receipt, name), *_remainder(values)]
    return run_passthrough(command)


def run_managed_python(profile: str, values: list[str], home: Path) -> int:
    receipt = read_receipt(home)
    if receipt is None:
        raise SetupError("Windows Setup receipt is missing")
    profiles = receipt.get("python_profiles") or {}
    executable = profiles.get(profile) if isinstance(profiles, dict) else None
    if not isinstance(executable, str) or not executable:
        raise SetupError(f"managed Python profile is not installed: {profile}")
    prefix = managed_argv(
        {**receipt, "tools": {"python": {"argv": [executable]}}}, "python"
    )
    env = os.environ.copy()
    env["LLM_WIKI_SETUP_RECEIPT"] = str(receipt_path(home))
    values_by_profile = (receipt.get("runtime_env") or {}).get(profile, {})
    if isinstance(values_by_profile, dict):
        env.update({
            key: value
            for key, value in values_by_profile.items()
            if isinstance(key, str) and isinstance(value, str)
        })
    command = python_file_command(prefix[0], [*prefix[1:], *_remainder(values)])
    return run_passthrough(command, env=env)


def uninstall_hosts(
    hosts: list[str],
    home: Path,
    payload: Path,
    *,
    custom_targets: list[str] | None = None,
    purge: bool = False,
) -> int:
    receipt = read_receipt(home)
    if receipt is None:
        raise SetupError("Windows Setup receipt is missing")
    installed_hosts = list(receipt.get("hosts", []))
    installed_customs = list(receipt.get("custom_targets", []))
    requested_customs = list(dict.fromkeys(custom_targets or []))
    # No selection at all means "everything this Setup owns"; naming either
    # kind narrows the removal to exactly what was named.
    if hosts or requested_customs:
        selected, selected_customs = hosts, requested_customs
    else:
        selected, selected_customs = installed_hosts, installed_customs
    unknown = sorted(set(selected) - set(installed_hosts))
    if unknown:
        raise SetupError("host(s) are not owned by this Setup: " + ", ".join(unknown))
    suite = Path(receipt["suite"])
    install, _ = import_suite_modules(suite)
    owned_customs = {install.expand(path) for path in installed_customs}
    unknown_customs = sorted(
        str(path)
        for path in {install.expand(raw) for raw in selected_customs} - owned_customs
    )
    if unknown_customs:
        raise SetupError(
            "skills directory is not owned by this Setup: " + ", ".join(unknown_customs)
        )
    if purge and (
        set(selected) != set(installed_hosts)
        or {install.expand(raw) for raw in selected_customs} != owned_customs
    ):
        raise SetupError("--purge requires uninstalling every Setup-owned target")
    bootstrap = install.load_json(suite / "registry" / "bootstrap.json")
    skills = install.load_json(suite / "registry" / "skills.json")
    targets = (
        install.resolve_targets(bootstrap, selected, selected_customs)
        if selected or selected_customs
        else []
    )
    removed = []
    for target in targets:
        for skill in skills.get("skills", []):
            destination = target["path"] / skill["slug"]
            manifest_path = destination / ".llm-wiki-install.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                manifest.get("installer") == "windows-setup"
                and manifest.get("install_id") == receipt.get("install_id")
            ):
                install.remove_path(destination)
                removed.append(str(destination))
    receipt["hosts"] = [host for host in installed_hosts if host not in selected]
    dropped = {install.expand(raw) for raw in selected_customs}
    receipt["custom_targets"] = [
        path for path in installed_customs if install.expand(path) not in dropped
    ]
    receipt["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    if purge:
        # Wikis and their registry are user data and deliberately remain.
        for managed in (
            home / "components",
            home / "runtime",
            home / "suite",
            home / "opencli-extension",
            home / SETUP_DIR / "downloads",
        ):
            if managed.is_dir():
                shutil.rmtree(long_path(managed))
            elif managed.exists():
                managed.unlink()
        receipt_path(home).unlink(missing_ok=True)
    else:
        write_receipt(home, receipt)
    emit(
        "uninstalled",
        hosts=selected,
        custom_targets=selected_customs,
        removed=removed,
        preserved=(
            [str(home / "wikis.json")]
            if purge
            else [str(home / "wikis.json"), str(home / "components")]
        ),
        purged=purge,
    )
    if purge:
        stable = home / SETUP_DIR / SETUP_EXE
        print(
            f"Managed runtime removed. Delete {stable} after this Setup process exits."
        )
    return 0


def component_choices(manifest: dict) -> list[dict]:
    out = []
    for component, spec in (manifest.get("components") or {}).items():
        label = spec.get("label", component)
        description = spec.get("description", "")
        out.append({
            "id": component,
            "label": label,
            "description": description,
            "label_zh": spec.get("label_zh", label),
            "description_zh": spec.get("description_zh", description),
            "default": bool(spec.get("default")),
            "size": int(spec.get("size", 0)),
        })
    return out


def launch_gui(home_link: Path, payload: Path, asset_dir: Path | None) -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError as exc:
        raise SetupError("Windows Setup GUI is unavailable; use the install command") from exc

    enable_windows_dpi_awareness()
    manifest = embedded_json(payload, "component-manifest.json")
    root = tk.Tk()
    root.title("My LLM Wiki 安装程序")
    icon_file = payload / "setup-icon.png"
    if icon_file.is_file():
        try:
            icon = tk.PhotoImage(file=str(icon_file))
            root.iconphoto(True, icon)
        except tk.TclError:
            pass

    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
    style.configure("Section.TLabel", font=("Segoe UI", 10, "bold"))
    style.configure("Muted.TLabel", foreground="#606060")
    style.configure("Status.TLabel", font=("Segoe UI", 10, "bold"))
    root.option_add("*Font", "{Segoe UI} 10")

    # Tk reports scaling in pixels per point; 96 DPI equals factor 1.0.  The
    # fixed geometry must grow with it or scaled displays clip the window.
    try:
        dpi_factor = float(root.tk.call("tk", "scaling")) * 72.0 / 96.0
    except tk.TclError:
        dpi_factor = 1.0

    def px(value: int) -> int:
        return int(round(value * dpi_factor))

    width = px(760)
    height = min(px(780), max(px(560), root.winfo_screenheight() - px(120)))
    root.geometry(f"{width}x{height}")
    root.minsize(px(680), px(540))

    header = ttk.Frame(root, padding=(px(28), px(18), px(28), px(10)))
    header.pack(fill="x")
    ttk.Label(header, text="My LLM Wiki Setup", style="Title.TLabel").pack(anchor="w")
    ttk.Label(
        header,
        text="Windows 原生安装 · 选择要接入的 agent 宿主与所需工具组件",
        style="Muted.TLabel",
    ).pack(anchor="w", pady=(px(2), 0))

    # The bottom bar packs before the content so the action buttons stay
    # visible no matter how short the window is.
    bottom = ttk.Frame(root)
    bottom.pack(side="bottom", fill="x")
    ttk.Separator(bottom, orient="horizontal").pack(fill="x")
    buttons = ttk.Frame(bottom, padding=(px(28), px(10), px(28), px(12)))
    buttons.pack(fill="x")
    close_button = ttk.Button(buttons, text="取消", command=root.destroy, width=14)
    close_button.pack(side="right")
    install_button = ttk.Button(buttons, text="安装", width=14, default="active")
    install_button.pack(side="right", padx=(0, px(8)))

    content = ttk.Frame(root)
    content.pack(fill="both", expand=True)

    # Page 1: selections, scrollable so short displays still reach everything.
    select_page = ttk.Frame(content)
    select_page.pack(fill="both", expand=True)
    canvas = tk.Canvas(select_page, highlightthickness=0, borderwidth=0)
    scrollbar = ttk.Scrollbar(select_page, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    form = ttk.Frame(canvas, padding=(px(28), 0, px(20), px(12)))
    form_window = canvas.create_window((0, 0), window=form, anchor="nw")
    form.bind(
        "<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all"))
    )
    canvas.bind(
        "<Configure>", lambda e: canvas.itemconfigure(form_window, width=e.width)
    )

    def on_mousewheel(event) -> None:
        canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    canvas.bind_all("<MouseWheel>", on_mousewheel)

    def section(title: str) -> ttk.Frame:
        ttk.Label(form, text=title, style="Section.TLabel").pack(
            anchor="w", pady=(px(12), px(4))
        )
        box = ttk.Frame(form)
        box.pack(fill="x")
        return box

    hosts_box = section("Agent 宿主")
    host_vars = {}
    for row in host_rows(payload):
        var = tk.BooleanVar(value=bool(row["detected"]))
        host_vars[row["id"]] = var
        suffix = "已检测到" if row["detected"] else "未检测到"
        ttk.Checkbutton(
            hosts_box,
            text=f"{row['id']} — {row['skills_dir']} ({suffix})",
            variable=var,
        ).pack(anchor="w", padx=(px(8), 0), pady=px(1))

    # Escape hatch for a real host the registry does not name, or one whose
    # skills directory the user has relocated. Checked automatically once a
    # directory is picked, so choosing a path is itself the consent.
    host_dir_var = tk.BooleanVar(value=False)
    host_dir = tk.StringVar(value="")
    host_dir_row = ttk.Frame(hosts_box)
    host_dir_row.pack(fill="x", padx=(px(8), 0), pady=(px(4), 0))
    ttk.Checkbutton(
        host_dir_row,
        text="其他目录 — 直接指定一个 skills 目录：",
        variable=host_dir_var,
    ).pack(side="left")

    def browse_host_dir() -> None:
        chosen = filedialog.askdirectory(title="选择 agent 宿主的 skills 目录")
        if chosen:
            host_dir.set(chosen)
            host_dir_var.set(True)

    ttk.Button(host_dir_row, text="浏览…", command=browse_host_dir).pack(
        side="left", padx=px(8)
    )
    ttk.Label(hosts_box, textvariable=host_dir, style="Muted.TLabel").pack(
        anchor="w", padx=(px(28), 0)
    )

    components_box = section("工具组件")
    component_vars = {}
    for row in component_choices(manifest):
        var = tk.BooleanVar(value=row["default"])
        component_vars[row["id"]] = var
        size = f" · {row['size'] / (1024 * 1024):.0f} MiB" if row["size"] else ""
        ttk.Checkbutton(
            components_box, text=f"{row['label_zh']}{size}", variable=var
        ).pack(anchor="w", padx=(px(8), 0), pady=(px(3), 0))
        if row["description_zh"]:
            ttk.Label(
                components_box, text=row["description_zh"], style="Muted.TLabel"
            ).pack(anchor="w", padx=(px(28), 0))

    desktop_box = section("桌面应用")
    browser_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(
        desktop_box,
        text="My LLM Wiki Browser — 静默安装，此后自动保持更新",
        variable=browser_var,
    ).pack(anchor="w", padx=(px(8), 0), pady=px(1))

    location_box = section("安装位置")
    location_var = tk.StringVar(value="default")
    custom_dir = tk.StringVar(value="")
    ttk.Radiobutton(
        location_box,
        text=f"用户目录（默认）— {home_link}",
        variable=location_var,
        value="default",
    ).pack(anchor="w", padx=(px(8), 0), pady=px(1))
    custom_row = ttk.Frame(location_box)
    custom_row.pack(fill="x", padx=(px(8), 0))
    ttk.Radiobutton(
        custom_row,
        text="其他磁盘 — 数据存放在所选位置，用户目录路径经 junction 指向：",
        variable=location_var,
        value="custom",
    ).pack(side="left")

    def browse() -> None:
        chosen = filedialog.askdirectory(title="选择 My LLM Wiki 的数据目录")
        if chosen:
            custom_dir.set(chosen)
            location_var.set("custom")

    ttk.Button(custom_row, text="浏览…", command=browse).pack(side="left", padx=px(8))
    ttk.Label(location_box, textvariable=custom_dir, style="Muted.TLabel").pack(
        anchor="w", padx=(px(28), 0)
    )

    # Page 2: progress and completion, swapped in when the install starts.
    progress_page = ttk.Frame(content, padding=(px(28), px(4), px(28), px(8)))
    result = {"code": 2}
    status = tk.StringVar(value="准备就绪")
    ttk.Label(
        progress_page,
        textvariable=status,
        style="Status.TLabel",
        wraplength=px(660),
        justify="left",
    ).pack(anchor="w", pady=(px(6), px(6)))
    progress_bar = ttk.Progressbar(progress_page, mode="determinate", maximum=100, value=0)
    progress_bar.pack(fill="x")
    progress_text = tk.StringVar(value="")
    ttk.Label(progress_page, textvariable=progress_text, style="Muted.TLabel").pack(
        anchor="w", pady=(px(2), px(6))
    )
    notes_frame = ttk.Frame(progress_page, relief="solid", borderwidth=1)
    notes_frame.pack(fill="both", expand=True, pady=(px(4), 0))
    notes_scroll = ttk.Scrollbar(notes_frame, orient="vertical")
    notes = tk.Text(
        notes_frame,
        height=6,
        wrap="word",
        state="disabled",
        relief="flat",
        borderwidth=0,
        padx=px(10),
        pady=px(8),
        yscrollcommand=notes_scroll.set,
    )
    notes_scroll.configure(command=notes.yview)
    notes_scroll.pack(side="right", fill="y")
    notes.pack(side="left", fill="both", expand=True)
    actions_row = ttk.Frame(progress_page)
    actions_row.pack(anchor="w", pady=(px(8), 0))
    launch_row = ttk.Frame(progress_page)
    launch_row.pack(anchor="w")

    def show_progress_page() -> None:
        canvas.unbind_all("<MouseWheel>")
        select_page.pack_forget()
        progress_page.pack(fill="both", expand=True)

    def show_select_page() -> None:
        progress_page.pack_forget()
        select_page.pack(fill="both", expand=True)
        canvas.bind_all("<MouseWheel>", on_mousewheel)

    speed_state = {"time": 0.0, "received": 0, "rate": 0.0}

    def apply_progress(info: dict) -> None:
        phase = info.get("phase")
        if phase:
            status.set(phase + "…")
            progress_bar.configure(maximum=100, value=0)
            progress_text.set("")
            speed_state.update(time=0.0, received=0, rate=0.0)
        received = info.get("received")
        if received is None:
            return
        total = info.get("total") or 0
        now = time.monotonic()
        if speed_state["time"]:
            elapsed = now - speed_state["time"]
            if elapsed > 0:
                instant = (received - speed_state["received"]) / elapsed
                rate = speed_state["rate"]
                speed_state["rate"] = instant if not rate else rate * 0.7 + instant * 0.3
        speed_state["time"] = now
        speed_state["received"] = received
        rate_text = f" · {speed_state['rate'] / 1048576:.1f} MiB/s" if speed_state["rate"] else ""
        if total:
            progress_bar.configure(maximum=total, value=received)
            progress_text.set(
                f"{received / 1048576:.1f} / {total / 1048576:.1f} MiB{rate_text}"
            )
        else:
            progress_text.set(f"{received / 1048576:.1f} MiB{rate_text}")

    set_progress_hook(lambda info: root.after(0, lambda info=info: apply_progress(info)))

    def show_notes(lines: list[str]) -> None:
        notes.configure(state="normal")
        notes.delete("1.0", "end")
        notes.insert("1.0", "\n".join(lines))
        notes.configure(state="disabled")

    def run_install() -> None:
        hosts = [name for name, var in host_vars.items() if var.get()]
        components = [name for name, var in component_vars.items() if var.get()]
        custom_targets = []
        if host_dir_var.get():
            if not host_dir.get():
                messagebox.showerror(
                    "My LLM Wiki 安装程序", "请先为「其他目录」选择一个 skills 目录。"
                )
                return
            custom_targets.append(host_dir.get())
        if not hosts and not custom_targets:
            messagebox.showerror(
                "My LLM Wiki 安装程序", "请至少选择一个 agent 宿主或一个 skills 目录。"
            )
            return
        data_root = None
        if location_var.get() == "custom":
            if not custom_dir.get():
                messagebox.showerror(
                    "My LLM Wiki 安装程序", "请先为自定义安装位置选择一个目录。"
                )
                return
            data_root = Path(custom_dir.get())
        status.set("正在安装……ASR 组件较大，可能需要几分钟。")
        install_button.configure(state="disabled")
        show_progress_page()
        show_notes([
            "My LLM Wiki — 把你看过的网页、视频、文档沉淀成本地知识库。",
            "",
            "正在部署：my-llm-wiki 技能套件、私有 Python 运行时，以及你勾选的工具组件。",
            "· 抓取与整理由你的 agent（Claude Code / Codex / Hermes 等）驱动，",
            "  wiki 页面全部落在本地磁盘。",
            "· Browser 桌面应用负责浏览与检索知识库，安装后自动保持更新。",
            "",
            "安全说明：",
            "· 所有数据只保存在本机（用户目录或你选择的磁盘），不会上传到任何服务器。",
            "· 组件均从项目官方 release 源下载，并逐个通过 SHA-256 校验；",
            "  Browser 安装包另经 minisign 签名校验。",
            "· 安装器只写入自己管理的目录，遇到不是它创建的文件会先停下，不会覆盖。",
        ])

        def worker() -> None:
            guidance: list[str] = []
            try:
                if data_root is not None:
                    ensure_data_root(home_link, data_root)
                code = install_flow(
                    hosts=hosts,
                    custom_targets=custom_targets,
                    components=components,
                    home=home_link.resolve(),
                    payload=payload,
                    asset_dir=asset_dir,
                    guidance=guidance,
                    browser=browser_var.get(),
                )
                result["code"] = code

                def finish() -> None:
                    status.set(
                        "安装完成。"
                        + ("部分能力还需手动操作，见下方说明。" if code == 3 else "")
                    )
                    progress_text.set("")
                    if guidance:
                        # Keep verification advice with the manual steps, ahead
                        # of the first-capture prompt block.
                        try:
                            insert_at = guidance.index("")
                        except ValueError:
                            insert_at = len(guidance)
                        guidance.insert(
                            insert_at,
                            "完成上述手动步骤后，点击下方「重新运行 doctor 检查」"
                            "做一次端到端验证。",
                        )
                    base_lines = list(guidance) or [
                        "没有需要手动处理的事项，可以关闭本窗口了。"
                    ]
                    show_notes(base_lines)
                    extension = browser_bridge_extension_dir(home_link.resolve())
                    if extension is not None:
                        def open_chrome_extensions() -> None:
                            chrome = chrome_executable()
                            if chrome is None:
                                messagebox.showinfo(
                                    "My LLM Wiki 安装程序",
                                    "未找到 Chrome；请在 Chrome 地址栏手动打开 "
                                    "chrome://extensions",
                                )
                                return
                            try:
                                subprocess.Popen(
                                    [str(chrome), "chrome://extensions/"],
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    close_fds=True,
                                )
                            except OSError as exc:
                                messagebox.showerror(
                                    "My LLM Wiki 安装程序", f"Chrome 启动失败：{exc}"
                                )

                        ttk.Button(
                            actions_row,
                            text="打开 Chrome 扩展设置",
                            command=open_chrome_extensions,
                        ).pack(side="left", padx=(0, 8))
                    if extension is not None and hasattr(os, "startfile"):
                        ttk.Button(
                            actions_row,
                            text="打开扩展文件夹",
                            command=lambda: os.startfile(extension),
                        ).pack(side="left", padx=(0, 8))

                    doctor_button = ttk.Button(actions_row, text="重新运行 doctor 检查")

                    def run_doctor_click() -> None:
                        doctor_button.configure(state="disabled")
                        status.set("正在运行 doctor 检查……大约需要一分钟。")

                        def doctor_worker() -> None:
                            try:
                                doctor_code, output = run_doctor_capture(home_link.resolve())
                            except Exception as exc:  # noqa: BLE001 - show any doctor failure
                                doctor_code, output = 2, str(exc)

                            def apply() -> None:
                                doctor_button.configure(state="normal")
                                if doctor_code == 0:
                                    status.set("doctor 检查全部通过。")
                                elif doctor_code == 3:
                                    status.set("doctor：部分能力还需处理，见下方输出。")
                                else:
                                    status.set(f"doctor 检查失败（退出码 {doctor_code}），见下方输出。")
                                lines = output.splitlines() or ["（doctor 无输出）"]
                                # 引导文案保留在上方，doctor 结果追加在分隔线之下
                                show_notes([
                                    *base_lines,
                                    "",
                                    "── doctor 检查结果 ──",
                                    *lines[-60:],
                                ])
                                notes.see("end")

                            root.after(0, apply)

                        threading.Thread(target=doctor_worker, daemon=True).start()

                    doctor_button.configure(command=run_doctor_click)
                    doctor_button.pack(side="left")

                    browser_exe = installed_browser_executable(home_link.resolve())
                    launch_var = tk.BooleanVar(value=True)
                    if browser_exe is not None:
                        ttk.Checkbutton(
                            launch_row,
                            text="关闭安装器后立即打开 My LLM Wiki Browser 浏览知识库",
                            variable=launch_var,
                        ).pack(anchor="w", pady=(4, 0))

                    def close_setup() -> None:
                        if browser_exe is not None and launch_var.get():
                            try:
                                subprocess.Popen(
                                    [str(browser_exe)],
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    close_fds=True,
                                )
                            except OSError as exc:
                                messagebox.showerror(
                                    "My LLM Wiki 安装程序", f"Browser 启动失败：{exc}"
                                )
                        root.destroy()

                    close_button.configure(text="关闭", command=close_setup)
                    root.protocol("WM_DELETE_WINDOW", close_setup)
                    messagebox.showinfo(
                        "My LLM Wiki 安装程序",
                        "安装完成。"
                        + ("请在关闭前查看窗口中的后续手动步骤。" if guidance else ""),
                    )

                root.after(0, finish)
            except Exception as exc:  # noqa: BLE001 - surface the full installer failure
                message = str(exc)
                root.after(0, lambda message=message: messagebox.showerror(
                    "My LLM Wiki 安装程序", message
                ))
                root.after(0, lambda: status.set("安装失败；未改动任何非本安装器管理的文件。"))
                root.after(0, show_select_page)
                root.after(0, lambda: install_button.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    install_button.configure(command=run_install)
    root.mainloop()
    set_progress_hook(None)
    return int(result["code"])


def show_fatal_error(message: str) -> None:
    """Surface a fatal pre-GUI error in a dialog.

    A double-clicked GUI launch owns only a hidden console, so a SetupError
    raised before ``launch_gui`` (platform gate, payload verification) would
    otherwise flash a console window and vanish without readable output."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("My LLM Wiki 安装程序", message)
        root.destroy()
    except Exception:  # noqa: BLE001 - the stderr print still happens
        pass


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", type=Path, default=Path(DEFAULT_HOME).expanduser())
    ap.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="keep managed home and wikis on this drive/folder via NTFS junctions",
    )
    ap.add_argument("--payload", type=Path, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--asset-dir", type=Path, default=None)
    ap.add_argument("--allow-test-platform", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--skip-postcheck", action="store_true", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="command")

    hosts = sub.add_parser("hosts", help="list supported agent hosts")
    hosts.add_argument("--json", action="store_true")

    plan = sub.add_parser("plan", help="render an offline install plan")
    plan.add_argument("--host", action="append", default=[])
    plan.add_argument("--custom-target", action="append", default=[])
    plan.add_argument("--component", action="append", default=[])
    plan.add_argument("--all-tools", action="store_true")

    install = sub.add_parser("install", help="install/update/repair core and selected components")
    install.add_argument("--host", action="append", default=[])
    install.add_argument(
        "--custom-target",
        action="append",
        default=[],
        metavar="DIR",
        help="install into an explicit skills directory not named by any registry host",
    )
    install.add_argument("--component", action="append", default=[])
    install.add_argument("--all-tools", action="store_true")
    install.add_argument(
        "--browser",
        action="store_true",
        help="also install the Browser desktop app silently (auto-updates itself)",
    )

    components = sub.add_parser("components", help="maintain installed tool components")
    component_sub = components.add_subparsers(dest="component_command", required=True)
    component_install = component_sub.add_parser("install")
    component_install.add_argument("--component", action="append", required=True)
    component_doctor = component_sub.add_parser("doctor")
    component_doctor.add_argument("--component", action="append", required=True)

    tools = sub.add_parser("tools", help="run a receipt-managed external tool")
    tool_sub = tools.add_subparsers(dest="tool_command", required=True)
    tool_run = tool_sub.add_parser("run")
    tool_run.add_argument("tool")
    tool_run.add_argument("args", nargs=argparse.REMAINDER)
    tool_list = tool_sub.add_parser("list", help="show which managed tools are runnable")
    tool_list.add_argument("--json", action="store_true")

    python = sub.add_parser("python", help="run a receipt-managed Python profile")
    python_sub = python.add_subparsers(dest="python_command", required=True)
    python_run = python_sub.add_parser("run")
    python_run.add_argument("--profile", required=True)
    python_run.add_argument("args", nargs=argparse.REMAINDER)

    status = sub.add_parser("status", help="show managed installation state")
    status.add_argument("--json", action="store_true")

    uninstall = sub.add_parser("uninstall", help="remove Setup-owned agent skill copies")
    uninstall.add_argument("--host", action="append", default=[])
    uninstall.add_argument("--custom-target", action="append", default=[], metavar="DIR")
    uninstall.add_argument(
        "--purge",
        action="store_true",
        help="after removing every host, remove managed suite/runtime/components; preserve Wikis",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    attach_parent_console()
    args = parser().parse_args(argv)
    try:
        home_link = args.home.expanduser()
        if args.data_root is not None:
            ensure_data_root(home_link, args.data_root)
        home = home_link.resolve()
        payload = payload_dir(args.payload)
        asset_dir = args.asset_dir.resolve() if args.asset_dir else None
        if args.command is None:
            validate_platform(args.allow_test_platform)
            return launch_gui(home_link, payload, asset_dir)
        if args.command == "hosts":
            rows = host_rows(payload)
            if args.json:
                print(json.dumps({"hosts": rows}, ensure_ascii=False, indent=2))
            else:
                for row in rows:
                    print(
                        f"{row['id']}: {row['skills_dir']} "
                        f"({'detected' if row['detected'] else 'not detected'})"
                    )
            return 0
        manifest = embedded_json(payload, "component-manifest.json")
        all_components = list((manifest.get("components") or {}).keys())
        if args.command == "plan":
            selected = all_components if args.all_tools else list(dict.fromkeys(args.component))
            print(json.dumps({
                "status": "planned",
                "platform": "windows",
                "hosts": args.host,
                "custom_targets": args.custom_target,
                "components": [
                    row for row in component_choices(manifest) if row["id"] in selected
                ],
                "home": str(home),
                "foreign_conflicts": "stop-before-write",
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "install":
            selected = all_components if args.all_tools else list(dict.fromkeys(args.component))
            return install_flow(
                hosts=list(dict.fromkeys(args.host)),
                custom_targets=list(dict.fromkeys(args.custom_target)),
                components=selected,
                home=home,
                payload=payload,
                asset_dir=asset_dir,
                allow_test_platform=args.allow_test_platform,
                skip_postcheck=args.skip_postcheck,
                browser=args.browser,
            )
        if args.command == "components":
            selected = list(dict.fromkeys(args.component))
            if args.component_command == "install":
                return install_components(
                    components=selected,
                    home=home,
                    payload=payload,
                    asset_dir=asset_dir,
                    allow_test_platform=args.allow_test_platform,
                    skip_postcheck=args.skip_postcheck,
                )
            return doctor_components(selected, home, payload, skip=args.skip_postcheck)
        if args.command == "tools":
            validate_platform(args.allow_test_platform)
            if args.tool_command == "list":
                return list_managed_tools(home, as_json=args.json)
            return run_managed_tool(args.tool, args.args, home)
        if args.command == "python":
            validate_platform(args.allow_test_platform)
            return run_managed_python(args.profile, args.args, home)
        if args.command == "status":
            receipt = read_receipt(home)
            result = receipt or {"status": "not-installed", "platform": "windows"}
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print("not installed" if receipt is None else json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if receipt else 3
        if args.command == "uninstall":
            validate_platform(args.allow_test_platform)
            return uninstall_hosts(
                list(dict.fromkeys(args.host)),
                home,
                payload,
                custom_targets=list(dict.fromkeys(args.custom_target)),
                purge=args.purge,
            )
        raise SetupError(f"unsupported command: {args.command}")
    except SetupError as exc:
        if args.command is None:
            show_fatal_error(str(exc))
        print(f"windows-setup: {exc}", file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError) as exc:
        if args.command is None:
            show_fatal_error(f"operation failed: {exc}")
        print(f"windows-setup: operation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
