# Agent Bootstrap Protocol

This repository is meant to be installable by an agent from a GitHub URL.

When a user says "install this project" and provides the suite URL, follow this
release-first flow.

## 1. Clone Or Open The Repo

The default install mode is **symlink** (`scripts/install.sh`), so the checkout is
not throwaway — every agent's skill dir links back into it, and its location is
**load-bearing and permanent**. Updates are a single `git pull` here that
propagates to all agents through the symlinks. So the clone needs a stable home,
not a random CWD or `/tmp`.

Resolve where the repo lives, in this order:

1. **Existing dev checkout** — the user already works on the suite (e.g. a clone
   under `~/projects/…`, or the skill symlinks already resolve into one). **Reuse
   it.** Do not clone a second copy into the canonical home below — that would
   shadow their working copy and split the source of truth.
2. **Fresh install** — no checkout exists. Clone to the canonical home from
   `registry/bootstrap.json` → `default_repo_home` (`~/.my-llm-wiki/suite`), then
   run `install.sh` from there. This keeps the symlinks stable and lets `doctor` /
   updates always find the repo root. Everything the suite persists lives under
   the one branded home `~/.my-llm-wiki/` (registry `wikis.json`, `connector/`
   runtime, `suite/` checkout).

If a checkout is already present, `git pull` (or ask) before overwriting local
changes. Never clone into the agent workspace / current working directory.

## 2. Install Skills

Install active skills into the user's agent skill directories:

```bash
scripts/install.sh \
  --target ~/.codex/skills \
  --target ~/.claude/skills \
  --target ~/.hermes/skills \
  --target ~/.agents/skills \
  --target ~/.workbuddy/skills
```

Skip targets that do not exist only if the user clearly does not use that agent.

The script works on Windows under Git Bash: it converts paths for native
Windows Python and installs links as directory junctions (`mklink /J`, no
admin / Developer Mode needed) because Git Bash's `ln -s` silently degrades to
a copy. Run it as-is — no manual path fixing.

## 3. Install The Capture Toolchain (strongly recommend it)

The skills ship no fetchers — capture quality is gated on the machine's tools,
and two of them are **near-mandatory** for a smooth flow. Probe first
(`which opencli yt-dlp ffmpeg`, or `python3 scripts/doctor.py` after step 2),
then for anything missing, **strongly recommend the install to the user** —
say concretely what breaks without it, give the command *with the project home
URL* so they can vet it, and get their go-ahead. Never install silently: a
toolchain install is the user's call.

| Tool | Install | Without it |
|------|---------|------------|
| **opencli** | `npm i -g @jackwener/opencli` · [npm](https://www.npmjs.com/package/@jackwener/opencli) | 公众号 images stay remote; 小红书/抖音 captures fail outright (login-walled); X degrades |
| **yt-dlp** | `brew install yt-dlp` · [github.com/yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp) | no video→transcript path at all (captions and audio both come through it) |
| **ffmpeg** | `brew install ffmpeg` · [ffmpeg.org](https://ffmpeg.org) | no audio extraction / covers for the video path |

(`registry/bootstrap.json` → `capture_toolchain` carries the same list
machine-readable. `markitdown` is optional — only needed for local PDF/docx.)

Two follow-ups that bite later if skipped now:

- **PATH for daemon agents**: npm global bins usually get exported only in
  `~/.zprofile`, but daemon-launched agents snapshot a `bash -l` env, which
  reads `~/.profile` / `~/.bash_profile` — export the npm-global bin dir there
  too, or opencli will look "not installed" to those agents.
- **opencli logins are per-platform and one-time** (`opencli xiaohongshu login`,
  `opencli douyin login`, …). Don't front-load them all — surface the login
  step when a capture first needs that platform.

## 4. Initialize A Wiki

If the user does not already have a wiki registry (`~/.my-llm-wiki/wikis.json`),
initialize a first wiki under the default root from `registry/bootstrap.json`:

```text
~/wikis/my-llm-wiki
```

Use the `my-llm-wiki` / `my-llm-wiki-maintainer` scripts already installed from
this repo to create the wiki scaffold and register it.

## 5. Install Browser: Release First

Always prefer release artifacts over source builds:

```bash
python3 scripts/install-browser.py --open
```

If the repo is private or the release is private, set `GITHUB_TOKEN` first.

On Windows the script prefers the portable zip over `*-setup.exe` and
auto-extracts it — extraction IS the install; just run the exe inside (pin to
taskbar / create a shortcut if the user wants one). It needs the WebView2
runtime, which Windows 10/11 ships by default; only fall back to setup.exe on
machines without WebView2 (the installer bootstraps it).

Only fall back to local source build when:

- no release exists yet,
- no asset matches the user's OS/arch,
- the user explicitly asks for a dev build.

Fallback command:

```bash
python3 scripts/install-browser.py --fallback-source
```

## 6. Verify The Whole Suite

Run the suite-level health check — one shot covering skills linkage, the wiki
registry, Browser reachability, and capture adapters:

```bash
python3 scripts/doctor.py          # human summary
python3 scripts/doctor.py --json   # machine-readable (for scripting / watch)
```

Each component reports `ok` / `warn` / `error` / `skip`; the process exits
non-zero only on `error` (today: skills not linked into any agent dir — the one
thing install must get right). Fix any `error`, and relay `warn`s the user cares
about (e.g. a skill installed as a `--copy` won't track repo edits; Browser not
running is fine if they only want the skills).

The Browser row resolves the port the way the app itself does
(`~/.my-llm-wiki/connector/server-port` > `$PORT` > default 8800) and treats any
HTTP response as "up" — an auth-gated `401` (the API requires a token by default)
still proves the server is running, so the probe needs no token. To hand-check,
use the port doctor prints:

```bash
curl -s "http://127.0.0.1:$(cat ~/.my-llm-wiki/connector/server-port 2>/dev/null || echo 8800)/api/v1/healthz"
```

## 7. Guide First Capture

Do not stop after installation. Ask the user for one URL or note to save, then
walk the first successful loop:

```text
capture -> RAW -> maintain/ingest -> browse/search in My LLM Wiki Browser
```

Suggested user prompt:

```text
Send me one article, webpage, video, or note you want to preserve. I will save it
to RAW, compile it into your wiki, and show where to view it in My LLM Wiki Browser.
```

## 8. MCP Setup

Once Browser exposes `/mcp/`, generate a client config for the user's current
agent. Prefer `Authorization: Bearer <token>` over putting long-lived tokens in
URLs. For remote access, use the relay URL shown by the Browser tray/settings UI.
