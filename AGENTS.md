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
  --target ~/.agents/skills
```

Skip targets that do not exist only if the user clearly does not use that agent.

## 3. Initialize A Wiki

If the user does not already have a wiki registry (`~/.my-llm-wiki/wikis.json`),
initialize a first wiki under the default root from `registry/bootstrap.json`:

```text
~/wikis/my-llm-wiki
```

Use the `my-llm-wiki` / `my-llm-wiki-maintainer` scripts already installed from
this repo to create the wiki scaffold and register it.

## 4. Install Browser: Release First

Always prefer release artifacts over source builds:

```bash
python3 scripts/install-browser.py --open
```

If the repo is private or the release is private, set `GITHUB_TOKEN` first.

Only fall back to local source build when:

- no release exists yet,
- no asset matches the user's OS/arch,
- the user explicitly asks for a dev build.

Fallback command:

```bash
python3 scripts/install-browser.py --fallback-source
```

## 5. Verify The Whole Suite

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

## 6. Guide First Capture

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

## 7. MCP Setup

Once Browser exposes `/mcp/`, generate a client config for the user's current
agent. Prefer `Authorization: Bearer <token>` over putting long-lived tokens in
URLs. For remote access, use the relay URL shown by the Browser tray/settings UI.
