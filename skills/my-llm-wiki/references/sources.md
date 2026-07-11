# Default source capture SOPs — probe first, then the best available recipe

This reference covers the facade's default Web/WeChat/document paths. X and
online video are owned by the sibling `my-llm-wiki-x` and
`my-llm-wiki-video` skills. This skill owns the **RAW contract**, not the
fetchers. Every scenario below is
one job: lay the source down in a temp dir in the `adapter-contract.md` shape
(markdown + local media), then hand off to `normalize_raw.py`. *Which tool does
the fetching is a property of the machine, not of this skill* — so each scenario
starts from a capability probe and lists recipes per tool, best first. Any tool
that produces the shape is a valid adapter; when tools disagree with this file,
trust their `--help` and note the drift.

## Contents

- [Common adapter rules](#common-adapter-rules)
- [WeChat](#wechat-公众号--mpweixinqqcom)
- [Ordinary web pages and Xiaohongshu articles](#any-web-page-incl-小红书-blogs-news--the-universal-fallback)
- [Local documents](#local-documents-pdf-docx-pptx-xlsx-epub---markitdown)
- [Source types](#choosing-source_type)

## Common adapter rules

**Probe before fetching** (once per session; especially on a fresh machine):

```bash
python3 <skill>/scripts/preflight.py --profile capture.web
# or, for a local document:
python3 <skill>/scripts/preflight.py --profile capture.doc
agent-reach doctor --json 2>/dev/null       # per-platform availability, if installed
```

**The probe is a hard gate, not a suggestion.** Do not pick a fetcher from
habit before it runs. In particular, a generic text extractor
(tavily-extract / WebFetch / built-in browser) is only a valid choice after
the probe shows the scenario's preferred adapter missing or broken — reaching
for it while opencli is installed is exactly the silent degradation the rule
below forbids (remote images, missing `公众号:/发布时间:` metadata). Measured
live: a WeChat capture went straight to `tvly extract`, skipped the probe, and
committed a RAW with a remote cover and an empty publish time while opencli
sat unused on the machine.

Common fetch stacks, in rough order of capture cleanliness:

| Stack | What it gives you | Watch out |
|------|--------------------|-----------|
| **opencli** | real logged-in browser: JS pages, auth-gated content, auto image download | an npm CLI; needs its sibling `node` on PATH, not only the binary |
| **agent-reach** | maintained per-platform access with its own doctor | output is text/Markdown; media usually needs separate localization |
| **bare CLIs** | video/audio/subtitles and local-document conversion | compose the Adapter Contract shape yourself |
| **agent built-ins** | zero-install text extraction for ordinary URLs | text-mostly; images, JS, and auth pages are weaker |

**When a scenario's best tool is missing, don't silently degrade** — tell the
user what the recommended tool is, what it would improve for *this* capture, and
give the install command **with the project's home URL** from the structured
preflight report so they can vet it. Then proceed with the best available path
(or wait, their call).

General rules, every scenario:

- **Temp dir first** (`/tmp/llmwiki-<x>`), then `normalize_raw.py` commits. A
  failed fetch never touches RAW.
- **Disk-first, context-cheap.** Fetch output goes into the temp dir via the
  tool's own output flag, never to stdout — a dumped page/API payload becomes
  prompt prefix re-billed on every later call (`| head` does not fix this).
  Verify from the adapter's status/file size/grep, and let only compact
  metadata excerpts into the conversation.
- **Media must end up local.** The core does not download remote media — a
  capture whose images are still `https://` links is degraded (the core flags it
  via `capture_health`, but localizing is the fetch step's job).
- **Don't fight auth.** Login walls → surface the tool's login step
  (`opencli twitter login`, browser login for cookie-based tools). Never scrape
  around auth.
- **PATH gotcha:** tools are frequently installed but missing from a sandboxed
  agent's PATH (nvm/Homebrew/pipx bins). Resolve the absolute path in the *same*
  shell block as the call; install only if genuinely absent — and in the right
  ecosystem (opencli = npm; markitdown = pip/pipx).

---

## WeChat 公众号 — `mp.weixin.qq.com`

source_type `wechat` · `original_id` = the `/s/<token>` URL segment.

- **opencli** (best — localizes images):
  ```bash
  opencli weixin download --url "<url>" --output /tmp/llmwiki-wx --download-images true -f yaml
  ```
  Output folder is exactly the `--from` shape, with a `>`-header (`公众号:` /
  `发布时间:` / `原文链接:`) the core parses. **Verify `images/`:** newer 图文
  (image-text) posts keep images in a JS gallery `weixin download` doesn't see —
  full text, empty images. Then redo with the generic reader
  (`opencli web read --url … --download-images true --wait 5`) and normalize
  from that folder instead, carrying `--author` / `--publish-time` from the
  first pass (web read's header lacks them).
- **No browser tool** (only after the probe shows opencli
  unavailable/broken): WebFetch / tavily-extract → save markdown to
  `/tmp/llmwiki-wx/page.md`, download the visible images into the dir, rewrite
  to relative links, pass metadata via flags. mp.weixin pages are largely
  static, so text comes through well — but these extractors do not localize
  images or parse the WeChat header, so downloading media and passing
  `--author` / `--publish-time` is on you; a commit with `https://mmbiz.qpic.cn`
  links left in the body is a degraded capture and will be flagged.

A pure-text article legitimately has no images; when in doubt a web-read retry
is harmless. web read pulls a little chrome (avatar, 打赏 UI) — trim the obvious
boilerplate if easy; RAW favors faithful over tidy.

---

## Any web page (incl. 小红书, blogs, news) — the universal fallback

source_type: `xiaohongshu` for xiaohongshu.com, else `web`.

- **opencli**:
  ```bash
  opencli web read --url "<url>" --output /tmp/llmwiki-web --download-images true -f yaml
  ```
  Slow/JS pages: `--wait-until networkidle`, `--wait <s>`,
  `--wait-for "<css>"`, `--frames all-same-origin` for iframe articles.
- **agent-reach / WebFetch / tavily-extract** (only after the probe shows
  opencli unavailable/broken): get clean markdown → write to
  `/tmp/llmwiki-web/page.md` → download must-keep images into the dir and
  rewrite to relative links (or accept text-only; the core will flag it) →
  `normalize_raw.py --md … --source-type web` with metadata flags.

小红书 notes are image-centric and login-gated — a fresh session hits an
"IP at risk" login wall before content renders, so the generic readers above
usually fail. The working recipe is the dedicated adapter,
`opencli xiaohongshu` (this is also what agent-reach's xiaohongshu backend
delegates to): check `whoami`, then `note '<full signed URL with xsec_token>'`
for text/metadata (resolve the `xhslink.com` short link with
`curl -w '%{url_effective}'` to get one — a bare note id is rejected) and
`download '<xhslink or signed URL>' --output <tmpdir>` for images/video.
**Video notes** are a video capture — use the sibling `my-llm-wiki-video`
skill and its `references/video-xiaohongshu.md`. If none of that is available, you may
only get text + cover; say so rather than pretending the capture is complete.

---

## Local documents (PDF, docx, pptx, xlsx, epub, …) — `markitdown`

source_type `doc`. markitdown (pip/pipx, **not** npm) converts office/document
files to structure-preserving Markdown:

```bash
markitdown "/path/to/file.pdf" -o /tmp/llmwiki-doc/doc.md
# stable id from content so re-ingesting the same file dedups:
id=$(shasum -a 256 "/path/to/file.pdf" | cut -c1-16)
python3 <skill>/scripts/normalize_raw.py \
  --md /tmp/llmwiki-doc/doc.md \
  --source-type doc \
  --source-file "/path/to/file.pdf" \
  --source-url "file:///path/to/file.pdf" \
  --original-id "$id" \
  --title "<document title or filename>" \
  --captured-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

- **Always pass `--source-file`** — markitdown's text is a lossy extraction;
  the flag archives the real original into `raw/assets/` next to its searchable
  text, and stops the core from false-flagging a sparse extraction.
- A scanned/image-only PDF yields little text — that's expected; the original
  is archived.
- `source_url` is a `file://` URI unless the doc has a real online origin.

---

## Choosing source_type

`source_type` = the `raw/sources/<source_type>/` bucket + frontmatter field.
Canonical buckets: `wechat` · `x` · `xiaohongshu` · `web` · `doc` · `video` ·
`note`. Add a new bucket (`zhihu`, `bloomberg`, …) only when a source recurs
enough to deserve its own shelf — and prefer a dedicated adapter/channel over
the generic web path when the installed tools have one (`opencli list`,
`agent-reach doctor`), since it yields a cleaner capture.
