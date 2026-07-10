# Agent Bootstrap Protocol

This repository is meant to be installable by an agent from a GitHub URL.

When a user says "install this project" and provides the suite URL, follow this
release-first flow.

The preferred skills-only entry point is the standalone root `bootstrap.sh`.
Download and inspect it, then run it; do not pipe a remote script directly into
a shell:

```bash
curl -fsSLo bootstrap.sh \
  https://raw.githubusercontent.com/dake6767/llm-wiki-suite/main/bootstrap.sh
less bootstrap.sh
bash bootstrap.sh
```

It implements steps 1–3 below: reuse/clone the permanent checkout, sync active
skills into the registry's default agent targets, and run the scoped doctor. It
detects external capture tools but never installs them. Use the manual commands
below when an agent needs to control an individual step.

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
`bootstrap.sh` is conservative for developer checkouts: it reuses them without
changing them, and only pulls when `--update` is explicit and the worktree is
clean.

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

## 3. Detect And Offer The Selected Capture Toolchain

The skills ship no fetchers. After step 2, run the exact `toolchain check:`
command printed by `install.sh`. It scopes detection to feature skills the user
requested plus their `bundles`; runtime-only `requires` are installed but do not
contribute unrelated capability profiles. Examples:

```bash
python3 scripts/doctor.py --skills my-llm-wiki-x
python3 scripts/doctor.py --skills my-llm-wiki-video
python3 scripts/doctor.py                       # default full-suite check
```

Read the `toolchain` component. `unavailable` means the selected capability has
no viable path; `degraded` means a fallback works with the stated limitation;
`ok` may still carry an optional quality recommendation. Explain exactly which
selected capability improves or breaks, give the reported install command and
project home URL, and get the user's go-ahead. Never install silently.

The single machine-readable catalog is
`skills/my-llm-wiki/references/toolchain.json`; skills declare only capability
ids in `registry/skills.json`, while `registry/bootstrap.json` points to the
catalog. Do not re-create a hardcoded tool table in AGENTS.md or doctor.py.

**Mainland-China networks:** before recommending any command above, check
reachability — `python3 skills/cn-mirrors/scripts/net_probe.py` (or the
`network:` line of my-llm-wiki's `preflight.py`). If github/PyPI/npm are
blocked or crawling, recommend the `install_cn` variants from the toolchain
report instead of the defaults (they route through official
domestic mirrors, and for yt-dlp switch channels entirely so self-update
keeps working). The full playbook — including cloning this repo itself via a
mirror — is the `cn-mirrors` skill.

Two follow-ups that bite later if skipped now:

- **PATH for daemon agents**: npm global bins usually get exported only in
  `~/.zprofile`, but daemon-launched agents snapshot a `bash -l` env, which
  reads `~/.profile` / `~/.bash_profile` — export the npm-global bin dir there
  too, or opencli will look "not installed" to those agents.
- **opencli logins are per-platform and one-time** (`opencli xiaohongshu login`,
  `opencli douyin login`, …). Don't front-load them all — surface the login
  step when a capture first needs that platform.
- **Hermes unattended runs**: when Hermes is one of the selected targets, offer
  `approvals.mode: smart` plus `security.redact_secrets: true`, while keeping
  `approvals.cron_mode: deny` and `security.tirith_enabled: true`. Never switch
  to `off` / `yolo` or unconditional cron approval as an installation shortcut,
  and never edit an existing Hermes config without user consent. Repository
  examples must pass `python3 scripts/check_approval_safety.py`.

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
non-zero only on `error` (skills not linked into any agent dir, a declared skill
runtime dependency missing, or an invalid registry reference). Fix any `error`, and relay `warn`s the user cares
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

The Browser serves MCP at `http://127.0.0.1:<port>/mcp` behind the same Bearer
token as the Web API (`~/.my-llm-wiki/connector/token`), but **local hosts must
default to the suite-owned stdio bridge**, not a direct loopback URL. Hermes and
WorkBuddy can send loopback through the system proxy; the bridge disables all
proxies and resolves the current port/token at runtime. Remote relay access
continues to use native streamable HTTP.

Registration recipes for every host live in `registry/bootstrap.json` → `mcp`
(the single source of truth, parallel to `default_skill_targets`). They are argv
arrays, not shell strings: `install-browser.py` substitutes the absolute current
Python executable and permanent-checkout bridge path, preserving Windows drive
letters/backslashes and paths containing spaces. Do not guess an app-bundle,
portable-extraction, or Linux package path, and do not reintroduce `npx
mcp-remote`.

```bash
python3 scripts/install-browser.py --register-mcp     # propose per detected host; runs only on explicit consent
python3 scripts/install-browser.py --unregister-mcp   # cleanup: no stale entries after a Browser uninstall
```

`install-browser.py` offers the same proposal automatically after a successful
install (suppress with `--skip-mcp`). Consent rules: show the exact command,
run it only after the user confirms, skip without error otherwise — the
generalization of "never edit an existing Hermes config without user consent"
to all hosts. WorkBuddy has no registration CLI, so print the exact JSON for the
user to merge manually; never edit its config directly. `doctor.py` reports
unregistered/stale entries, legacy direct-loopback registrations, and the result
of a real bridge `tools/list` probe when the Browser is running.

MCP is an access form, not the capability itself: hosts without MCP reach the
same backend through `wiki_ops.py browser-search` / `local-search` /
`read-pages`, fully equivalent. Local stdio host configs contain neither the
port nor the token; the bridge reads them from `~/.my-llm-wiki/connector` on
every request, so rotations need no re-registration. For remote access, use the
relay URL shown by the Browser tray/settings UI and prefer an Authorization
header over putting long-lived tokens in URLs.
