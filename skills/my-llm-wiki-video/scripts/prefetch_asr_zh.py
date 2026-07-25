#!/usr/bin/env python3
"""Download and offline-verify the two models used by Chinese video ASR.

The Browser invokes this script with the official asr-zh Python Provider. Model
files live outside the immutable runtime pack so pack updates do not force a
second download. A readiness marker is written only after both local model
directories have been loaded successfully with network updates disabled.

With --progress, or with MY_LLM_WIKI_ASR_PROGRESS set, the script writes NDJSON
progress lines to stdout so the caller can draw a real progress bar instead of
an endless spinner: ModelScope's own tqdm output is not machine readable, so
downloaded bytes are observed on disk instead.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable


MODEL_ROOT_ENV = "MY_LLM_WIKI_ASR_ZH_MODEL_ROOT"
PROGRESS_ENV = "MY_LLM_WIKI_ASR_PROGRESS"
MARKER_NAME = ".my-llm-wiki-models.json"
PROGRESS_POLL_SECONDS = 0.7
MODEL_SPECS = {
    "fsmn-vad": {
        "id": "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
        "directory": "fsmn-vad",
    },
    "sensevoice": {
        "id": "iic/SenseVoiceSmall",
        "directory": "SenseVoiceSmall",
    },
}


def default_model_root() -> Path:
    configured = os.environ.get(MODEL_ROOT_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".my-llm-wiki" / "models" / "asr-zh").resolve()


def progress_requested() -> bool:
    """The Browser asks for progress through the environment, not a flag.

    An installed skill copy can be older than the Browser that drives it, and an
    unknown environment variable degrades to a silent download while an unknown
    CLI flag would abort the install outright.
    """
    return os.environ.get(PROGRESS_ENV, "").strip() not in {"", "0", "false"}


def marker_path(root: Path) -> Path:
    return root / MARKER_NAME


def component_path(root: Path, component: str) -> Path:
    spec = MODEL_SPECS[component]
    return root / str(spec["directory"])


def marker_ready(root: Path) -> bool:
    try:
        marker = json.loads(marker_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if marker.get("schema") != 1 or not isinstance(marker.get("models"), dict):
        return False
    recorded = marker["models"]
    for component, spec in MODEL_SPECS.items():
        item = recorded.get(component)
        if (
            not isinstance(item, dict)
            or item.get("id") != spec["id"]
            or item.get("directory") != spec["directory"]
            or not component_path(root, component).is_dir()
        ):
            return False
    return True


def resolved_model_references(root: Path | None = None) -> tuple[str, str]:
    """Return (VAD, SenseVoice) references, preferring verified local models."""
    selected_root = (root or default_model_root()).resolve()
    if marker_ready(selected_root):
        return (
            str(component_path(selected_root, "fsmn-vad")),
            str(component_path(selected_root, "sensevoice")),
        )
    return (
        str(MODEL_SPECS["fsmn-vad"]["id"]),
        str(MODEL_SPECS["sensevoice"]["id"]),
    )


class ProgressReporter:
    """Write NDJSON progress lines for the caller; a no-op when disabled."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._lock = threading.Lock()

    def emit(self, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        line = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            print(line, flush=True)


def watched_paths(root: Path, component: str) -> list[Path]:
    """Directories where this component's bytes land while downloading.

    With local_dir set ModelScope stages files inside the destination, but the
    cache directory is watched too so a different staging layout still shows
    movement. Only paths that belong to this component are counted, otherwise a
    resumed second download would inherit the first model's bytes.
    """
    spec = MODEL_SPECS[component]
    leaf = str(spec["id"]).rsplit("/", 1)[-1]
    paths = [component_path(root, component)]
    cache = root / ".cache"
    if cache.is_dir():
        paths.extend(path for path in cache.glob(f"*/{leaf}*") if path.is_dir())
    return [path for path in paths if path.exists()]


def observed_bytes(root: Path, component: str) -> int:
    total = 0
    for path in watched_paths(root, component):
        for current, _directories, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(current) / name).stat().st_size
                except OSError:
                    continue
    return total


def expected_total_bytes(model_id: str) -> int | None:
    """Best-effort repository size, so the caller can show a percentage."""
    try:
        from modelscope.hub.api import HubApi

        files = HubApi().get_model_files(model_id=model_id, recursive=True)
    except Exception:
        return None
    total = 0
    for item in files or []:
        if not isinstance(item, dict) or item.get("Type") == "tree":
            continue
        try:
            total += int(item.get("Size") or 0)
        except (TypeError, ValueError):
            continue
    return total or None


class DownloadWatcher:
    """Poll bytes on disk in the background while a download blocks.

    The repository size is resolved on its own thread: it is a network call, and
    neither the download nor the byte reports may wait on it. Until it lands the
    caller receives volume-only updates, which is already enough to show that
    something is happening.
    """

    def __init__(
        self,
        reporter: ProgressReporter,
        component: str,
        root: Path,
        resolve_total: Callable[[], int | None],
    ) -> None:
        self.reporter = reporter
        self.component = component
        self.root = root
        self.total: int | None = None
        self._resolve_total = resolve_total
        self._done = threading.Event()
        self._poll = threading.Thread(target=self._run, daemon=True)
        self._sizing = threading.Thread(target=self._resolve, daemon=True)

    def __enter__(self) -> DownloadWatcher:
        if self.reporter.enabled:
            self._sizing.start()
            self._poll.start()
        return self

    def __exit__(self, *_exception: object) -> None:
        self._done.set()
        for thread in (self._poll, self._sizing):
            if thread.is_alive():
                thread.join(timeout=PROGRESS_POLL_SECONDS * 2)

    def _resolve(self) -> None:
        total = self._resolve_total()
        if total:
            self.total = total

    def _run(self) -> None:
        reported: tuple[int, int | None] | None = None
        while True:
            current = (observed_bytes(self.root, self.component), self.total)
            if current != reported:
                reported = current
                self.reporter.emit(
                    {
                        "event": "download",
                        "stage": self.component,
                        "downloaded_bytes": current[0],
                        "total_bytes": current[1],
                    }
                )
            if self._done.wait(PROGRESS_POLL_SECONDS):
                return


def download_component(
    component: str,
    root: Path,
    reporter: ProgressReporter | None = None,
) -> Path:
    from modelscope import snapshot_download

    reporter = reporter or ProgressReporter(False)
    root.mkdir(parents=True, exist_ok=True)
    marker_path(root).unlink(missing_ok=True)
    spec = MODEL_SPECS[component]
    destination = component_path(root, component)
    watcher = DownloadWatcher(
        reporter,
        component,
        root,
        lambda: expected_total_bytes(str(spec["id"])),
    )
    with watcher:
        downloaded = Path(
            snapshot_download(
                str(spec["id"]),
                cache_dir=str(root / ".cache"),
                local_dir=str(destination),
                max_workers=4,
            )
        )
    if not downloaded.is_dir() or not destination.is_dir():
        raise RuntimeError(
            f"ModelScope did not materialize {spec['id']} at {destination}"
        )
    reporter.emit(
        {
            "event": "download",
            "stage": component,
            "downloaded_bytes": observed_bytes(root, component),
            "total_bytes": watcher.total,
            "completed": True,
        }
    )
    return destination


def verify_models(root: Path, reporter: ProgressReporter | None = None) -> None:
    from funasr import AutoModel

    reporter = reporter or ProgressReporter(False)
    missing = [
        component
        for component in MODEL_SPECS
        if not component_path(root, component).is_dir()
    ]
    if missing:
        raise RuntimeError(f"model directories are missing: {', '.join(missing)}")

    reporter.emit({"event": "verify", "step": "fsmn-vad", "index": 1, "count": 2})
    vad = AutoModel(
        model=str(component_path(root, "fsmn-vad")),
        max_single_segment_time=30_000,
        disable_update=True,
        device="cpu",
    )
    del vad
    gc.collect()
    reporter.emit({"event": "verify", "step": "sensevoice", "index": 2, "count": 2})
    recognizer = AutoModel(
        model=str(component_path(root, "sensevoice")),
        disable_update=True,
        device="cpu",
    )
    del recognizer
    gc.collect()

    marker = {
        "schema": 1,
        "models": {
            component: {
                "id": spec["id"],
                "directory": spec["directory"],
            }
            for component, spec in MODEL_SPECS.items()
        },
    }
    atomic_write_json(marker_path(root), marker)


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage",
        required=True,
        choices=["fsmn-vad", "sensevoice", "verify"],
    )
    parser.add_argument("--model-root", type=Path, default=default_model_root())
    parser.add_argument(
        "--progress",
        action="store_true",
        default=progress_requested(),
        help=f"emit NDJSON progress lines on stdout (or set {PROGRESS_ENV})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.model_root.expanduser().resolve()
    reporter = ProgressReporter(bool(args.progress))
    try:
        if args.stage == "verify":
            verify_models(root, reporter)
            print(f"verified Chinese ASR models at {root}", flush=True)
        else:
            path = download_component(args.stage, root, reporter)
            print(f"downloaded {args.stage} to {path}", flush=True)
    except Exception as error:
        print(f"prefetch_asr_zh: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
