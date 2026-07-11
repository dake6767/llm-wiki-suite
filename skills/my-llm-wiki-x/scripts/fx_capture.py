#!/usr/bin/env python3
"""Deterministic fxtwitter fallback fetcher — disk-first, context-cheap.

Fetches https://api.fxtwitter.com/status/<id>, saves the raw API JSON to disk,
converts the tweet (plain or long-form article) to Markdown, downloads media
into images/, and prints only a compact JSON summary. The full payload never
enters the conversation: a measured ad-hoc `curl | head` of the same endpoint
put ~49KB of raw JSON into an agent context and re-billed it on every later
call in that session.

The output folder is a valid `--md/--assets` adapter shape for the sibling
core's normalize_raw.py.

Usage:
  fx_capture.py --tweet <numeric-id or x.com/twitter.com URL> --out <temp-dir>
                [--from-json <saved fx.json>] [--no-media] [--timeout 30]

Prints one JSON object: status, tweet metadata, written paths, media counts,
and warnings. Exit 0 with status:"ok", exit 1 with status:"error".
"""
import argparse
import json
import os
import re
import sys
import urllib.request

UA = "Mozilla/5.0 (compatible; llm-wiki-capture/1.0)"


def die(msg, **extra):
    print(json.dumps({"status": "error", "error": msg, **extra}, ensure_ascii=False))
    sys.exit(1)


def tweet_id_from(arg):
    m = re.search(r"(?:status(?:es)?|article)/(\d+)", arg)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d+", arg):
        return arg
    return None


def http_get(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
# Draft.js-style rich text -> Markdown (article tweets)
#
# Offsets in entityRanges/inlineStyleRanges are UTF-16 code units; slice on a
# UTF-16 code-unit array so CJK/emoji text does not shift ranges.
# ---------------------------------------------------------------------------

def _u16(text):
    b = text.encode("utf-16-le")
    return [b[i:i + 2] for i in range(0, len(b), 2)]


def _u16_join(units):
    return b"".join(units).decode("utf-16-le", errors="replace")


def _entity_map(article):
    """Normalize entityMap (dict keyed by str, or list of {key,value}) to a dict."""
    raw = (article.get("content") or {}).get("entityMap") or article.get("entityMap") or {}
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            out[str(k)] = v
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and "key" in item:
                out[str(item["key"])] = item.get("value", item)
    return out


def _media_index(article):
    """media_id -> media_entities entry."""
    idx = {}
    for ent in article.get("media_entities") or []:
        mid = ent.get("media_id") or ent.get("mediaId") or ent.get("id")
        if mid is not None:
            idx[str(mid)] = ent
    return idx


def _entity_media_ids(entity):
    data = entity.get("data") or entity
    ids = []
    for item in data.get("mediaItems") or data.get("media_items") or []:
        mid = item.get("mediaId") or item.get("media_id") or item.get("localMediaId")
        if mid is not None:
            ids.append(str(mid))
    for key in ("mediaId", "media_id"):
        if data.get(key) is not None:
            ids.append(str(data[key]))
    return ids


def _entity_url(entity):
    data = entity.get("data") or entity
    return data.get("url") or data.get("href")


def _block_text_md(block, entity_map, media_refs, warnings):
    """Render one block's text with LINK entities as Markdown links."""
    text = block.get("text") or ""
    units = _u16(text)
    ranges = sorted(block.get("entityRanges") or [], key=lambda r: r.get("offset", 0))
    out, pos = [], 0
    for r in ranges:
        off, length = r.get("offset", 0), r.get("length", 0)
        key = str(r.get("key"))
        ent = entity_map.get(key) or {}
        etype = (ent.get("type") or (ent.get("value") or {}).get("type") or "").upper()
        if off < pos or off + length > len(units):
            continue
        seg = _u16_join(units[off:off + length])
        if etype == "LINK":
            url = _entity_url(ent) or _entity_url(ent.get("value") or {})
            out.append(_u16_join(units[pos:off]))
            out.append(f"[{seg}]({url})" if url else seg)
            pos = off + length
        elif etype == "MEDIA":
            ids = _entity_media_ids(ent) or _entity_media_ids(ent.get("value") or {})
            if ids:
                media_refs.extend(ids)
            out.append(_u16_join(units[pos:off + length]))
            pos = off + length
        # other entity types: leave text as-is
    out.append(_u16_join(units[pos:]))
    return "".join(out)


_BLOCK_PREFIX = {
    "header-one": "# ",
    "header-two": "## ",
    "header-three": "### ",
    "unordered-list-item": "- ",
    "ordered-list-item": "1. ",
    "blockquote": "> ",
}


def article_to_markdown(article, entity_map, warnings):
    """Return (markdown_body, ordered media_id list referenced by blocks)."""
    blocks = (article.get("content") or {}).get("blocks") or article.get("blocks") or []
    lines, media_order = [], []
    for block in blocks:
        btype = block.get("type") or "unstyled"
        refs = []
        text = _block_text_md(block, entity_map, refs, warnings)
        if btype == "atomic":
            if not refs:  # atomic block with no resolvable media
                for r in block.get("entityRanges") or []:
                    ent = entity_map.get(str(r.get("key"))) or {}
                    ids = _entity_media_ids(ent) or _entity_media_ids(ent.get("value") or {})
                    refs.extend(ids)
            for mid in refs:
                media_order.append(mid)
                lines.append(f"@@MEDIA:{mid}@@")
                lines.append("")
            continue
        media_order.extend(refs)
        if btype == "code-block":
            lines += ["```", block.get("text") or "", "```", ""]
            continue
        prefix = _BLOCK_PREFIX.get(btype, "")
        if btype not in _BLOCK_PREFIX and btype != "unstyled":
            warnings.append(f"unknown block type '{btype}' rendered as paragraph")
        lines.append(prefix + text if text or prefix else "")
        if btype not in ("unordered-list-item", "ordered-list-item"):
            lines.append("")
    return "\n".join(lines).strip() + "\n", media_order


# ---------------------------------------------------------------------------
# Media download
# ---------------------------------------------------------------------------

def _best_mp4(info):
    """Highest-bitrate video/mp4 variant URL from any nested 'variants' list."""
    variants = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("variants"), list):
                variants.extend(node["variants"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(info)
    mp4s = [v for v in variants
            if isinstance(v, dict) and v.get("content_type") == "video/mp4" and v.get("url")]
    if mp4s:
        return max(mp4s, key=lambda v: v.get("bitrate") or 0)["url"]
    return None


def _ext_from_url(url, default):
    path = url.split("?")[0]
    m = re.search(r"\.(jpg|jpeg|png|gif|webp|mp4)$", path, re.I)
    return "." + m.group(1).lower() if m else default


def download(url, dest, timeout, warnings):
    try:
        data = http_get(url, timeout)
        with open(dest, "wb") as f:
            f.write(data)
        return True
    except Exception as e:  # noqa: BLE001 — record and continue; capture stays partial, not failed
        warnings.append(f"media download failed: {url} ({type(e).__name__})")
        return False


# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tweet", required=True, help="numeric tweet id or x.com/twitter.com URL")
    ap.add_argument("--out", required=True, help="temp output dir (created if missing)")
    ap.add_argument("--from-json", help="parse an already-saved fx.json instead of fetching")
    ap.add_argument("--no-media", action="store_true", help="skip media downloads")
    ap.add_argument("--timeout", type=int, default=30)
    a = ap.parse_args()

    tid = tweet_id_from(a.tweet)
    if not tid:
        die(f"cannot extract a numeric tweet id from {a.tweet!r}")

    out = os.path.abspath(a.out)
    images = os.path.join(out, "images")
    os.makedirs(images, exist_ok=True)
    warnings = []

    # 1. Land the full API payload on disk first; only excerpts go further.
    fx_path = os.path.join(out, "fx.json")
    if a.from_json:
        fx_path = os.path.abspath(a.from_json)
        raw = open(fx_path, "rb").read()
    else:
        try:
            raw = http_get(f"https://api.fxtwitter.com/status/{tid}", a.timeout)
        except Exception as e:  # noqa: BLE001
            die(f"fxtwitter fetch failed: {type(e).__name__}: {e}", tweet_id=tid)
        with open(fx_path, "wb") as f:
            f.write(raw)

    try:
        payload = json.loads(raw)
    except ValueError:
        die("fxtwitter response is not JSON", fx_json=fx_path, tweet_id=tid)
    if payload.get("code") not in (200, None) and not payload.get("tweet"):
        die(f"fxtwitter returned code {payload.get('code')}: {payload.get('message')}",
            fx_json=fx_path, tweet_id=tid)
    tweet = payload.get("tweet") or {}
    if not tweet:
        die("no tweet object in fxtwitter response", fx_json=fx_path, tweet_id=tid)

    author = tweet.get("author") or {}
    author_str = f"{author.get('name', '-')} (@{author.get('screen_name', '-')})"
    canonical = tweet.get("url") or f"https://x.com/i/status/{tid}"
    created = tweet.get("created_at") or ""
    article = tweet.get("article")

    saved = {"images": 0, "videos": 0}

    def save_media(url, stem, default_ext):
        ext = _ext_from_url(url, default_ext)
        name = f"{stem}{ext}"
        if a.no_media:
            return name, False
        ok = download(url, os.path.join(images, name), a.timeout, warnings)
        if ok:
            saved["videos" if ext == ".mp4" else "images"] += 1
        return name, ok

    if article:
        title = article.get("title") or tweet.get("text", "").split("\n")[0][:80] or "untitled"
        entity_map = _entity_map(article)
        media_idx = _media_index(article)
        try:
            body, media_order = article_to_markdown(article, entity_map, warnings)
        except Exception as e:  # noqa: BLE001 — never lose the text over rich formatting
            warnings.append(f"rich conversion failed ({type(e).__name__}); plain-text fallback")
            blocks = (article.get("content") or {}).get("blocks") or []
            body = "\n\n".join(b.get("text", "") for b in blocks).strip() + "\n"
            media_order = list(media_idx)

        # Resolve @@MEDIA:<id>@@ placeholders against media_entities.
        for mid in dict.fromkeys(media_order):
            ent = media_idx.get(mid) or {}
            info = ent.get("media_info") or ent
            img = info.get("original_img_url") or info.get("url")
            mp4 = _best_mp4(info)
            if mp4:
                name, _ = save_media(mp4, f"video-{mid}", ".mp4")
                md_ref = f"![video](images/{name})"
            elif img:
                name, _ = save_media(img, f"image-{mid}", ".jpg")
                md_ref = f"![image](images/{name})"
            else:
                warnings.append(f"media entity {mid} has no downloadable URL")
                md_ref = ""
            body = body.replace(f"@@MEDIA:{mid}@@", md_ref)
        body = re.sub(r"@@MEDIA:[^@]*@@", "", body)

        cover = ((article.get("cover_media") or {}).get("media_info") or {}).get(
            "original_img_url")
        if cover:
            name, ok = save_media(cover, "cover", ".jpg")
            if ok or a.no_media:
                body = f"![cover](images/{name})\n\n" + body
    else:
        title = tweet.get("text", "").split("\n")[0][:80] or "untitled"
        body = (tweet.get("text") or "").strip() + "\n"
        media = tweet.get("media") or {}
        parts = []
        for i, photo in enumerate(media.get("photos") or [], 1):
            if photo.get("url"):
                name, _ = save_media(photo["url"], f"image-{i}", ".jpg")
                parts.append(f"![image](images/{name})")
        for i, video in enumerate(media.get("videos") or [], 1):
            url = _best_mp4(video) or video.get("url")
            if url:
                name, _ = save_media(url, f"video-{i}", ".mp4")
                parts.append(f"![video](images/{name})")
        if parts:
            body += "\n" + "\n\n".join(parts) + "\n"

    md_path = os.path.join(out, "tweet.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n{body}")

    print(json.dumps({
        "status": "ok",
        "tweet_id": tid,
        "source_url": canonical,
        "title": title,
        "author": author_str,
        "publish_time": created,
        "is_article": bool(article),
        "md": md_path,
        "assets_dir": images,
        "fx_json": fx_path,
        "body_chars": len(body),
        "images_saved": saved["images"],
        "videos_saved": saved["videos"],
        "warnings": warnings,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
