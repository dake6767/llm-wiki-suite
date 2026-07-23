from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import video_probe


def _bilibili_formats() -> list[dict]:
    """A DASH format set shaped like a real Bilibili --dump-single-json."""
    return [
        # video-only DASH streams (must be ignored)
        {"format_id": "100026", "vcodec": "av01", "acodec": "none", "ext": "mp4"},
        {"format_id": "30080", "vcodec": "avc1", "acodec": "none", "ext": "mp4"},
        # a legacy merged progressive stream (has video: must be ignored)
        {"format_id": "0", "vcodec": "avc1", "acodec": "mp4a.40.2", "ext": "mp4"},
        # audio-only lossy tiers
        {"format_id": "30216", "vcodec": "none", "acodec": "mp4a.40.2",
         "ext": "m4a", "abr": 64.0, "filesize": 4_800_000},
        {"format_id": "30232", "vcodec": "none", "acodec": "mp4a.40.2",
         "ext": "m4a", "abr": 132.0, "filesize": 9_900_000},
        {"format_id": "30280", "vcodec": "none", "acodec": "mp4a.40.2",
         "ext": "m4a", "abr": 192.0, "filesize": 14_400_000},
        # premium / lossless tiers with HIGHER bitrate (the trap)
        {"format_id": "30250", "vcodec": "none", "acodec": "ec-3",
         "ext": "m4a", "abr": 256.0, "filesize": 19_000_000},
        {"format_id": "30251", "vcodec": "none", "acodec": "flac",
         "ext": "flac", "abr": 1000.0, "filesize": 75_000_000},
    ]


class SelectAudioFormatsTests(unittest.TestCase):
    def test_prefers_best_lossy_over_higher_bitrate_premium(self) -> None:
        recommended, compact = video_probe.select_audio_formats(_bilibili_formats())
        # 30280 (192k AAC) beats FLAC/Dolby despite their higher bitrate.
        self.assertEqual(recommended, "30280")
        ids = [entry["format_id"] for entry in compact]
        # Only audio-only streams survive; video/merged formats are dropped.
        self.assertEqual(set(ids), {"30216", "30232", "30280", "30250", "30251"})
        # Every lossy tier ranks ahead of every premium/lossless tier.
        lossy = {"30216", "30232", "30280"}
        premium = {"30250", "30251"}
        self.assertLess(
            max(ids.index(fid) for fid in lossy),
            min(ids.index(fid) for fid in premium),
        )

    def test_lossless_only_still_recommends_something(self) -> None:
        formats = [
            {"format_id": "30251", "vcodec": "none", "acodec": "flac",
             "ext": "flac", "abr": 1000.0},
        ]
        recommended, compact = video_probe.select_audio_formats(formats)
        self.assertEqual(recommended, "30251")
        self.assertEqual(len(compact), 1)

    def test_no_audio_only_streams_returns_empty(self) -> None:
        formats = [
            {"format_id": "18", "vcodec": "avc1", "acodec": "mp4a.40.2", "ext": "mp4"},
            {"format_id": "137", "vcodec": "avc1", "acodec": "none", "ext": "mp4"},
        ]
        recommended, compact = video_probe.select_audio_formats(formats)
        self.assertEqual(recommended, "")
        self.assertEqual(compact, [])

    def test_missing_or_malformed_formats_is_safe(self) -> None:
        self.assertEqual(video_probe.select_audio_formats(None), ("", []))
        self.assertEqual(video_probe.select_audio_formats("nope"), ("", []))
        # entries without a format_id are skipped rather than crashing.
        self.assertEqual(
            video_probe.select_audio_formats([{"vcodec": "none", "acodec": "opus"}]),
            ("", []),
        )

    def test_normalize_metadata_surfaces_audio_fields(self) -> None:
        record = {
            "id": "BV1ZGTX68Ej1",
            "title": "为什么杨梅很难走出中国",
            "formats": _bilibili_formats(),
        }
        metadata = video_probe.normalize_metadata(record)
        self.assertEqual(metadata["audio_format_id"], "30280")
        self.assertEqual(metadata["audio_formats"][0]["format_id"], "30280")
        self.assertEqual(metadata["audio_formats"][0]["ext"], "m4a")

    def test_normalize_metadata_without_formats_key(self) -> None:
        metadata = video_probe.normalize_metadata({"id": "x", "title": "t"})
        self.assertEqual(metadata["audio_format_id"], "")
        self.assertEqual(metadata["audio_formats"], [])


if __name__ == "__main__":
    unittest.main()
