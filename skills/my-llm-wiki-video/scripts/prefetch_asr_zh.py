#!/usr/bin/env python3
"""Download and offline-verify the two models used by Chinese video ASR.

The Browser invokes this script with the official asr-zh Python Provider. Model
files live outside the immutable runtime pack so pack updates do not force a
second download. A readiness marker is written only after both local model
directories have been loaded successfully with network updates disabled.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


MODEL_ROOT_ENV = "MY_LLM_WIKI_ASR_ZH_MODEL_ROOT"
MARKER_NAME = ".my-llm-wiki-models.json"
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


def download_component(component: str, root: Path) -> Path:
    from modelscope import snapshot_download

    root.mkdir(parents=True, exist_ok=True)
    marker_path(root).unlink(missing_ok=True)
    spec = MODEL_SPECS[component]
    destination = component_path(root, component)
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
    return destination


def verify_models(root: Path) -> None:
    from funasr import AutoModel

    missing = [
        component
        for component in MODEL_SPECS
        if not component_path(root, component).is_dir()
    ]
    if missing:
        raise RuntimeError(f"model directories are missing: {', '.join(missing)}")

    vad = AutoModel(
        model=str(component_path(root, "fsmn-vad")),
        max_single_segment_time=30_000,
        disable_update=True,
        device="cpu",
    )
    del vad
    gc.collect()
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.model_root.expanduser().resolve()
    try:
        if args.stage == "verify":
            verify_models(root)
            print(f"verified Chinese ASR models at {root}", flush=True)
        else:
            path = download_component(args.stage, root)
            print(f"downloaded {args.stage} to {path}", flush=True)
    except Exception as error:
        print(f"prefetch_asr_zh: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
