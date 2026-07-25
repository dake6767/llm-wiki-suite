from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prefetch_asr_zh


class AsrZhPrefetchTests(unittest.TestCase):
    def test_unverified_cache_falls_back_to_remote_model_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(
                prefetch_asr_zh.resolved_model_references(root),
                (
                    "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
                    "iic/SenseVoiceSmall",
                ),
            )

    def test_download_uses_private_cache_and_explicit_local_directory(self) -> None:
        calls: list[dict[str, object]] = []
        modelscope = types.ModuleType("modelscope")

        def snapshot_download(model_id: str, **kwargs: object) -> str:
            calls.append({"model_id": model_id, **kwargs})
            destination = Path(str(kwargs["local_dir"]))
            destination.mkdir(parents=True)
            (destination / "config.yaml").write_text("model: fixture\n", encoding="utf-8")
            return str(destination)

        modelscope.snapshot_download = snapshot_download  # type: ignore[attr-defined]
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            sys.modules, {"modelscope": modelscope}
        ):
            root = Path(temporary)
            result = prefetch_asr_zh.download_component("fsmn-vad", root)

        self.assertEqual(result.name, "fsmn-vad")
        self.assertEqual(calls[0]["model_id"], prefetch_asr_zh.MODEL_SPECS["fsmn-vad"]["id"])
        self.assertEqual(Path(str(calls[0]["cache_dir"])).name, ".cache")
        self.assertEqual(Path(str(calls[0]["local_dir"])).name, "fsmn-vad")

    def test_progress_is_requested_through_the_environment(self) -> None:
        for value, expected in (("1", True), ("0", False), ("false", False), ("", False)):
            with mock.patch.dict(
                "os.environ", {prefetch_asr_zh.PROGRESS_ENV: value}, clear=False
            ):
                self.assertIs(prefetch_asr_zh.progress_requested(), expected)
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(prefetch_asr_zh.progress_requested())

    def test_progress_mode_reports_downloaded_bytes(self) -> None:
        payload = b"x" * 4096
        modelscope = types.ModuleType("modelscope")

        def snapshot_download(model_id: str, **kwargs: object) -> str:
            destination = Path(str(kwargs["local_dir"]))
            destination.mkdir(parents=True)
            (destination / "model.pt").write_bytes(payload)
            return str(destination)

        modelscope.snapshot_download = snapshot_download  # type: ignore[attr-defined]
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            sys.modules, {"modelscope": modelscope}
        ), mock.patch.object(
            prefetch_asr_zh, "expected_total_bytes", return_value=len(payload)
        ), contextlib.redirect_stdout(stdout):
            prefetch_asr_zh.download_component(
                "fsmn-vad", Path(temporary), prefetch_asr_zh.ProgressReporter(True)
            )

        events = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertTrue(events)
        final = events[-1]
        self.assertEqual(final["event"], "download")
        self.assertEqual(final["stage"], "fsmn-vad")
        self.assertEqual(final["downloaded_bytes"], len(payload))
        self.assertEqual(final["total_bytes"], len(payload))
        self.assertTrue(final["completed"])

    def test_observed_bytes_exclude_the_other_components_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vad = prefetch_asr_zh.component_path(root, "fsmn-vad")
            vad.mkdir(parents=True)
            (vad / "model.pt").write_bytes(b"a" * 64)
            other = root / ".cache" / "iic" / "SenseVoiceSmall"
            other.mkdir(parents=True)
            (other / "model.pt").write_bytes(b"b" * 4096)

            self.assertEqual(prefetch_asr_zh.observed_bytes(root, "fsmn-vad"), 64)

    def test_progress_mode_reports_each_verified_model(self) -> None:
        funasr = types.ModuleType("funasr")

        class AutoModel:
            def __init__(self, **kwargs: object) -> None:
                pass

        funasr.AutoModel = AutoModel  # type: ignore[attr-defined]
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            sys.modules, {"funasr": funasr}
        ), contextlib.redirect_stdout(stdout):
            root = Path(temporary)
            for component in prefetch_asr_zh.MODEL_SPECS:
                prefetch_asr_zh.component_path(root, component).mkdir(parents=True)
            prefetch_asr_zh.verify_models(root, prefetch_asr_zh.ProgressReporter(True))

        events = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(
            [(item["step"], item["index"], item["count"]) for item in events],
            [("fsmn-vad", 1, 2), ("sensevoice", 2, 2)],
        )

    def test_offline_verification_publishes_local_model_references(self) -> None:
        loaded: list[dict[str, object]] = []
        funasr = types.ModuleType("funasr")

        class AutoModel:
            def __init__(self, **kwargs: object) -> None:
                loaded.append(kwargs)

        funasr.AutoModel = AutoModel  # type: ignore[attr-defined]
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            sys.modules, {"funasr": funasr}
        ):
            root = Path(temporary)
            for component in prefetch_asr_zh.MODEL_SPECS:
                directory = prefetch_asr_zh.component_path(root, component)
                directory.mkdir(parents=True)
                (directory / "config.yaml").write_text(
                    "model: fixture\n", encoding="utf-8"
                )

            prefetch_asr_zh.verify_models(root)
            vad, recognizer = prefetch_asr_zh.resolved_model_references(root)
            marker = json.loads(
                prefetch_asr_zh.marker_path(root).read_text(encoding="utf-8")
            )

        self.assertEqual(Path(vad).name, "fsmn-vad")
        self.assertEqual(Path(recognizer).name, "SenseVoiceSmall")
        self.assertEqual(marker["schema"], 1)
        self.assertEqual(len(loaded), 2)
        self.assertTrue(all(item["disable_update"] for item in loaded))
        self.assertTrue(all(item["device"] == "cpu" for item in loaded))


if __name__ == "__main__":
    unittest.main(verbosity=2)
