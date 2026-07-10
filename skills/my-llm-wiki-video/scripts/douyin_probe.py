#!/usr/bin/env python3
"""Read public Douyin mobile-share metadata without generating inline code."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
    "Mobile/15E148 Safari/604.1"
)
ROUTER_DATA = re.compile(r"window\._ROUTER_DATA\s*=\s*({.*?})\s*</script>", re.DOTALL)
MAX_DOCUMENT_BYTES = 5 * 1024 * 1024


def extract_metadata(document: str) -> dict[str, Any]:
    match = ROUTER_DATA.search(document)
    if not match:
        raise ValueError("mobile share page has no _ROUTER_DATA payload")
    payload = json.loads(match.group(1))
    try:
        item = payload["loaderData"]["video_(id)/page"]["videoInfoRes"]["item_list"][0]
        video = item["video"]
        cover = video.get("cover") or video["origin_cover"]
        return {
            "id": item.get("aweme_id") or item.get("item_id") or "",
            "title": item.get("desc") or "",
            "author": item["author"].get("nickname") or "",
            "sec_uid": item["author"].get("sec_uid") or "",
            "create_time": item.get("create_time"),
            "duration_ms": video.get("duration") or item.get("duration"),
            "play_url": video["play_addr"]["url_list"][0],
            "cover_url": cover["url_list"][0],
        }
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("unexpected Douyin mobile metadata shape") from exc


def fetch_document(aweme_id: str, timeout: int) -> str:
    if not re.fullmatch(r"\d{8,32}", aweme_id):
        raise ValueError("aweme_id must contain 8-32 digits")
    url = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
    request = urllib.request.Request(url, headers={"User-Agent": IPHONE_UA})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        if response.geturl().split(":", 1)[0].lower() != "https":
            raise ValueError("Douyin redirected to a non-HTTPS URL")
        payload = response.read(MAX_DOCUMENT_BYTES + 1)
        if len(payload) > MAX_DOCUMENT_BYTES:
            raise ValueError("Douyin metadata document exceeds 5 MiB")
        return payload.decode("utf-8", errors="replace")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--aweme-id")
    source.add_argument("--from-html", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = (args.from_html.read_text(encoding="utf-8", errors="replace")
                    if args.from_html else fetch_document(args.aweme_id, args.timeout))
        result = extract_metadata(document)
        rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            atomic_write(args.output, rendered)
        print(rendered, end="")
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        print(f"douyin_probe: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
