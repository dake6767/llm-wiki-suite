from __future__ import annotations

import builtins
import sys
import tempfile
import types
import unittest
from array import array
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sensevoice_to_srt as runner


def fake_dependencies(
    *,
    sample_rate: int = 16_000,
    samples: object | None = None,
) -> tuple[dict[str, types.ModuleType], list[dict[str, object]]]:
    soundfile = types.ModuleType("soundfile")
    reads: list[dict[str, object]] = []

    def read(path: str, **kwargs: object) -> tuple[object, int]:
        reads.append({"path": path, **kwargs})
        wav = samples if samples is not None else array("f", [0.0]) * 32_000
        return wav, sample_rate

    soundfile.read = read  # type: ignore[attr-defined]

    funasr = types.ModuleType("funasr")

    class AutoModel:
        recognizer_calls = 0

        def __init__(self, *, model: str, **_kwargs: object) -> None:
            self.model = model

        def generate(self, **_kwargs: object) -> list[dict[str, object]]:
            if "fsmn_vad" in self.model or self.model.endswith("fsmn-vad"):
                return [{"value": [[0, 1_000], [1_000, 2_000]]}]
            AutoModel.recognizer_calls += 1
            return [{"text": f"第{AutoModel.recognizer_calls}段"}]

    funasr.AutoModel = AutoModel  # type: ignore[attr-defined]
    utils = types.ModuleType("funasr.utils")
    postprocess = types.ModuleType("funasr.utils.postprocess_utils")
    postprocess.rich_transcription_postprocess = lambda text: text  # type: ignore[attr-defined]
    return {
        "soundfile": soundfile,
        "funasr": funasr,
        "funasr.utils": utils,
        "funasr.utils.postprocess_utils": postprocess,
    }, reads


class SenseVoiceRunnerTests(unittest.TestCase):
    def test_transcribe_uses_soundfile_without_importing_librosa(self) -> None:
        modules, reads = fake_dependencies()
        real_import = builtins.__import__

        def guarded_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "librosa" or name.startswith("librosa."):
                raise AssertionError("SenseVoice runner must not import librosa")
            return real_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = root / "audio.wav"
            audio.write_bytes(b"test fixture")
            output = root / "transcript.srt"
            with mock.patch.dict(sys.modules, modules), mock.patch(
                "builtins.__import__", side_effect=guarded_import
            ):
                cues, chars = runner.transcribe(audio, output, "zh", 30_000)

            self.assertEqual((cues, chars), (2, len("第1段第2段")))
            self.assertEqual(
                reads,
                [{
                    "path": str(audio),
                    "dtype": "float32",
                    "always_2d": False,
                }],
            )
            text = output.read_text(encoding="utf-8")
            self.assertIn("00:00:00,000 --> 00:00:01,000\n第1段", text)
            self.assertIn("00:00:01,000 --> 00:00:02,000\n第2段", text)

    def test_transcribe_rejects_non_16_khz_audio(self) -> None:
        modules, _reads = fake_dependencies(sample_rate=44_100)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = root / "audio.wav"
            audio.write_bytes(b"test fixture")
            with mock.patch.dict(sys.modules, modules):
                with self.assertRaisesRegex(RuntimeError, "requires 16 kHz"):
                    runner.transcribe(audio, root / "transcript.srt", "zh", 30_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
