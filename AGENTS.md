# Agent Bootstrap Protocol 4

This repository has one deterministic install protocol for Codex, Claude,
Hermes, agents-compatible hosts, and WorkBuddy. macOS and Linux use bootstrap
protocol 4. Windows is a hard cutover to the native
`My-LLM-Wiki-Setup.exe`; the legacy Git-Bash/Python/Git flow is unsupported.
Neither path accepts the pre-v4 `--target`, `--force`, `--yes`, or
implicit-host flows.

## 0. Windows Native Setup Is The Only Windows Entry Point

On Windows, download the latest official `My-LLM-Wiki-Setup.exe` and run it.
Mainland-China networks should use the project-owned Worker download first;
GitHub Releases remains canonical and is the fallback. Do not run
`bootstrap.sh`, `scripts/install.sh`, or
`scripts/install.py`, and do not install Git, Python, Node, npm, pip, winget,
or Git Bash as prerequisites. The old scripts deliberately stop on MSYS,
MINGW, and Cygwin before parsing install arguments:

Mainland China:

```text
https://wiki.htmlgo.to/_setup/latest/My-LLM-Wiki-Setup.exe
```

Matching SHA-256 manifest:

```text
https://wiki.htmlgo.to/_setup/latest/SHA256SUMS-windows-setup.txt
```

Canonical GitHub fallback:

```text
https://github.com/dake6767/llm-wiki-suite/releases/latest/download/My-LLM-Wiki-Setup.exe
```

The Setup UI must show every `registry/bootstrap.json` → `agent_hosts` entry
with expanded skills path and detection state, start with no host selected,
and require at least one explicit selection. It installs immutable skill
copies plus a private CPython core under `~/.my-llm-wiki`, and may install the
pinned Documents, Web, Video, Chinese ASR, and non-Chinese ASR components.
Windows skills resolve external tools exclusively from the atomic Setup
receipt at `~/.my-llm-wiki/setup/install-state.json`; they never fall back to
global `PATH`, an old venv, npm, pip, winget, or a development checkout.

Setup can anchor the managed data on another drive: the user picks an install
location (GUI selector or `--data-root`), bytes live under that folder's
`home`/`wikis` subdirectories, and `~/.my-llm-wiki` plus `~/wikis` become NTFS
junctions to them. The profile-path contract every skill, doctor, and agent
document relies on is unchanged, so never teach a different path. Rerunning
Setup with the same location after an OS reinstall relinks the junctions and
reuses the surviving components and wikis. Setup refuses to migrate an
existing real profile directory; before installing it also preflights free
disk space against the selected components and sweeps uuid-tagged leftover
staging/backup/partial paths from aborted runs.

Setup owns install, update, repair, component maintenance, exact postchecks,
and uninstall for paths carrying its `install_id`. It may replace a verified
older official checkout/link or an older Setup-owned copy through the normal
backup mechanism. A foreign link or ordinary directory stops the whole plan
before destination writes. Wiki initialization before doctor remains
mandatory. `registry/windows-toolchain.lock.json` is the reviewed upstream
input lock; released component archive hashes in `component-manifest.json`
are the user-install boundary.

Windows upstream updates are release engineering work, not user-side
upgrades. The scheduled candidate check opens or refreshes a labeled issue,
but it must not mutate the lock, an existing release, or silently promote
versions. A version counts as a candidate only when its required Windows asset
exists (for example Python source-only security releases are not embeddable
runtime candidates). Coupled runtimes are promoted as compatibility sets:
`torch` and `torchaudio`, for example, move only to their newest shared version
that provides Windows CPython 3.12 wheels, never to unrelated individual latest
versions. Review the version, source, hash/integrity and compatibility in a PR,
rebuild every affected component, pass the Windows clean-room workflow, merge,
then publish a new immutable release.
macOS/Linux recipes and bootstrap behavior must not change as a side effect.

## 1. Ask For The Host Before Writing

Read `registry/bootstrap.json` → `agent_hosts`. Present every host id and its
expanded `skills_dir`, marking whether its `detect_dir` currently exists. The
agent running this install is the natural default, but directory presence is
not consent. The user may select multiple hosts.

Use a registry id whenever possible:

```bash
bash bootstrap.sh --host codex
bash bootstrap.sh --host codex --host claude my-llm-wiki-video
```

`--custom-target DIR` is only for a real host not represented by the registry.
Never translate a known host into a custom path, create unselected agent homes,
or infer consent from stale `~/.codex`, `~/.claude`, `~/.hermes`, `~/.agents`,
or `~/.workbuddy` directories.

## 2. Release-First Entry Point

For a macOS/Linux URL-only installation, download the root `bootstrap.sh`,
inspect it, and then execute it. Never pipe a remote script into a shell and
never open an interactive pager during an unattended run. Windows uses §0.

Canonical:

```bash
curl -fsSLo bootstrap.sh --connect-timeout 5 --max-time 30 --retry 1 \
  https://raw.githubusercontent.com/dake6767/llm-wiki-suite/main/bootstrap.sh
sed -n '1,260p' bootstrap.sh
bash bootstrap.sh --host codex
```

Mainland China:

```bash
curl -fsSLo bootstrap.sh --connect-timeout 5 --max-time 30 --retry 1 \
  https://gitee.com/dake6767/llm-wiki-suite/raw/main/bootstrap.sh
sed -n '1,260p' bootstrap.sh
bash bootstrap.sh --repo-url https://gitee.com/dake6767/llm-wiki-suite.git \
  --host codex
```

GitHub is canonical. Gitee is a read-only Pull mirror; never make mirror-only
commits or rewrite repository links for it.

## 3. Checkout Selection Is Explicit

The checkout is load-bearing because link mode is the default. Bootstrap uses
exactly one of these locations:

1. `--repo DIR`, when supplied;
2. the directory containing `bootstrap.sh`, if it is a valid checkout;
3. `registry/bootstrap.json` → `default_repo_home` for a managed install.

It does not scan the current directory, project folders, old skill links, or
other agent homes. If the user has a development checkout, pass it explicitly:

```bash
bash bootstrap.sh --repo ~/projects/llm-wiki-suite --host codex
```

A fresh clone goes to `~/.my-llm-wiki/suite` through a temporary directory and
is moved into place only after its protocol/layout is validated. Git is
non-interactive, has low-speed and total-operation deadlines, and never reads
credentials from a TTY. Without an explicit `--repo-url`, bootstrap briefly
probes GitHub, tries the reachable source first, and falls back once to the
other configured source. Dry-run is offline and reports both candidates.

An existing checkout is never pulled implicitly. `--update` requires a clean
`main` branch, upstream `origin/main`, and the canonical GitHub origin or the
declared Gitee mirror. It runs bounded `git pull --ff-only` and succeeds only if
`HEAD == origin/main`; local-ahead, divergent, detached, dirty, or foreign
checkouts stop without replacement. Do not overwrite local changes.

## 4. Skill Installation Contract

On macOS/Linux, `scripts/install.py` owns the low-level target resolution, dependency/bundle
closure, conflicts, provenance, locking, rollback, and exact doctor command.
`bootstrap.sh` owns the complete install sequence, and `scripts/install.sh` is
its local-checkout launcher. Do not treat a direct `scripts/install.py` run as a
completed installation. Run the launchers as-is on macOS and Linux. Windows
uses §0 and never uses these launchers.

Default link mode creates POSIX directory symlinks. A destination is current
only when it resolves to this exact skill source. A foreign/broken link or
ordinary directory is a conflict. Windows Setup always installs verified
immutable copies and does not create junctions.

Copy mode is explicit:

```bash
scripts/install.sh --host codex --copy
```

Every copy is built in a temporary directory, excludes runtime state, and gets
`.llm-wiki-install.json` with its pack version, source commit, and content
digest. A copy is current only when both the manifest and installed bytes match
the current source. Copy mode never merges into a stale directory.

Conflicts stop the whole plan before destination writes. When the user explicitly
approves replacement, use `--replace`; every displaced path is atomically renamed into
the target registry's `.llm-wiki-backups/` directory on the same filesystem. If
a later operation fails, the installer rolls back every destination changed in
that run. Never rename or repoint a foreign path outside this mechanism.

The installer is non-interactive. It never prompts, installs external tools, or
changes host configuration. Exit codes are:

- `0`: installed/verified (optional degradation may remain);
- `1`: invalid installation or failed operation;
- `2`: bad invocation/protocol/configuration;
- `3`: installation succeeded but a selected capability needs user action.

## 5. Required Wiki Initialization

Immediately after installing skills, bootstrap runs
`scripts/initialize_wiki.py` before doctor. This is a required base-install
step, not an optional feature and not a follow-up question:

- when a registered usable Wiki already exists, reuse it and create nothing;
- otherwise initialize and register the first Wiki at
  `registry/bootstrap.json` → `default_wiki_root` (`~/wikis/my-llm-wiki`);
- if the registry is unreadable or initialization cannot be verified, fail the
  installation instead of reporting `status: installed`;
- `--no-doctor` skips only doctor; it never skips Wiki initialization.

The initializer is idempotent, uses the suite registry, and runs under the
install lock. Do not ask whether to initialize, defer it until after doctor,
invent a second registry, or write a synthetic RAW item. Seeing
`wiki-init: ready` or `wiki-init: existing` is the gate for continuing to
doctor.

## 6. Toolchain Detection And Network Routing

After skill installation and required Wiki initialization, bootstrap runs the
scoped doctor automatically. For manual runs, use the host ids and the same
selected skills:

```bash
python3 scripts/doctor.py --host codex --skills my-llm-wiki-x
python3 scripts/doctor.py --host codex --skills my-llm-wiki-video --json
python3 scripts/doctor.py --host codex
```

The toolchain catalog is
`skills/my-llm-wiki/references/toolchain.json`. It contains structured argv
arrays per OS and per network route. Do not turn them into shell strings, use
`eval`, guess another package manager, or invent an installation command.

`preflight.py` validates executable postchecks and Python modules with bounded,
parallel probes. It also runs `skills/cn-mirrors/scripts/net_probe.py` once and
routes GitHub, PyPI, npm, and Hugging Face independently. `global`, `cn`, and
`unavailable` are per-ecosystem results; do not use one GitHub result as a proxy
for every package source.

System packages on macOS/Linux are host-specific: ffmpeg recipes distinguish
Homebrew, apt, dnf, pacman, and root/non-root Linux. If no declared package
manager exists, report `unavailable`; never substitute a guessed distro
command. On Windows every tool recommendation is instead a structured
`My-LLM-Wiki-Setup.exe components install --component ...` argv followed by
the matching Setup component doctor. Never surface or execute the historical
winget, portable-ffmpeg, npm, or pip recipes on Windows. The Video component
contains the release-pinned yt-dlp and verified Gyan FFmpeg bytes and is
downloaded from the project htmlgo release route with GitHub Releases as the
canonical fallback.

Interpret capability states exactly:

- `ok`: selected capability is complete;
- `degraded`: a stated fallback works with the reported limitation;
- `unavailable`: the selected capability cannot complete;
- doctor `action-required` / exit 3: show the structured recipe and project
  home, explain the affected capability, and ask for consent.

Never install a recommended tool silently. The consent list you present is a
verbatim transcription of the report's `recommendations` array: every
reported row with its stated priority, no filtering, no re-ranking, and no
dropping rows that look unrelated to the capture the user happens to be
asking about (`markitdown` guards `capture.doc` even when the first capture
is a web page). Count your rows against the report before presenting — a
consent list that omits any reported row is invalid. Relay the doctor
`tools:` inventory line alongside it, so tools that are already installed
are visibly satisfied rather than silently absent, and append the Browser
Bridge extension row described below to this same list. After consent,
execute each reported
argv array directly with an argv-capable runner, apply only its reported `env`,
enforce its exact `step_timeout_seconds`, and run its `postcheck` with
`postcheck_timeout_seconds`. All reported commands are non-interactive; do not
remove those flags or concatenate/evaluate the argv as shell input.

`runtime_env` is distinct from install `env`: apply it when the capture backend
first downloads/loads a model (for example `HF_ENDPOINT`), not merely during
pip installation. Persist it in a profile only with separate user consent.

For `capture.video`, always relay `asr routing:`. Chinese routes to SenseVoice;
other languages route to faster-whisper. A one-backend machine is intentionally
reported as incomplete for the other language route.

Daemon agents must see tools on their actual startup `PATH`. In particular, an
npm global bin exported only from `~/.zprofile` may be absent from a host that
launches `bash -l`; add it to that host's login environment only with user
consent, then rerun doctor. `opencli` logins are per-platform and one-time;
surface `opencli <platform> login` only when the first real capture needs it.

`opencli` browser adapters additionally require the OpenCLI Browser Bridge
Chrome extension. It is a **required** item of this section's consent list,
not an optional extra: whenever `opencli` is installed or part of the
consented plan, append a `browser-bridge-extension [required]` row
immediately after `opencli` when presenting the tool recommendations. Its
staging is a network download, so it obeys the same never-install-silently
rule — and like every row in the list, it is individually skippable. After
consent, follow this sequence:

1. Install the consented CLI tools first (`opencli`, `yt-dlp`, …) and run
   their postchecks.
2. Stage the extension: `python3 scripts/opencli_extension.py` downloads the
   official `opencli-extension-v*.zip` release asset and unzips it into
   `~/.my-llm-wiki/opencli-extension/`. When GitHub is unreachable the
   script automatically falls back to the project's own relay mirror
   (`wiki.htmlgo.to/_mirror/opencli-extension`), so mainland networks need
   no extra flags; pass `--mirror-prefix` only when both channels fail,
   choosing the accelerator at runtime per `cn-mirrors`; never hardcode one.
3. Relay the script's printed `chrome://extensions` steps verbatim, always
   including the staged folder path — loading the unpacked folder is the
   user's manual browser action, never automated.
4. Ask whether loading is done with a host-native single-select control
   (same structured style as §7's Browser choice; never demand typed
   yes/no): **Loaded — verify now** / **Skip for now**.
5. On "Loaded", run `opencli doctor` and relay its verdict — it is the sole
   authority on the live bridge. If it reports the bridge missing, re-show
   the load steps and offer one retry or skip.

A skip at any step is not a failure, but it leaves the doctor
`opencli_extension` component at `warn`: the offer is unmet and must be
re-presented on the next doctor run, never silently dropped. Do not steer
users to the Chrome Web Store, which mainland-China networks generally
cannot reach. (An overseas user who did install from the store leaves no
staged folder; that edge case is why the doctor signal is a `warn` rather
than `action-required`.) On macOS and Windows, also mention the official
OpenCLIApp desktop bundle (https://opencli.info/download): it ships the CLI
with a menu-bar manager and login keep-alive, but it is a manual GUI
install, still requires the same extension, and the automated install route
in this repository remains npm.

If Hermes is selected, offer `approvals.mode: smart` and
`security.redact_secrets: true`, while retaining `approvals.cron_mode: deny` and
`security.tirith_enabled: true`. Never use `off`, `yolo`, unconditional cron
approval, or edit an existing Hermes config without consent. Repository
examples must pass `python3 scripts/check_approval_safety.py`.

## 7. Browser Installation

Browser is optional and is offered only after the required Wiki initialization
and doctor have completed. Present a host-native single-select control, using
the same structured interaction style as agent-host selection, with exactly
these choices:

- **Continue and install Browser** — run the release-first installer below.
- **Skip Browser** — finish the skills-only installation.

Do not ask the user to type `yes`, `no`, or any other confirmation text. The
bootstrap remains non-interactive and does not contain this choice. If the host
cannot render a native selection control, leave Browser skipped and explain
that the optional choice was unavailable; do not replace it with a free-text
prompt. Run the installer only after the user selects the install choice:

```bash
python3 scripts/install-browser.py --open
```

The installer tries the project-owned htmlgo Tauri manifest first. A source is
successful only after its asset finishes downloading; metadata success followed
by a stalled/failed asset continues to GitHub. Downloads use HTTPS, a 20-second
socket timeout, a 5-minute total deadline, a 2 GiB limit, `.part` files, and an
atomic rename. `GITHUB_TOKEN` is sent only to the GitHub host allowlist and is
stripped on redirects to other hosts.

Browser operations use a non-waiting advisory lock. A concurrent agent exits
immediately with an active-operation error; never retry it in a tight loop.

Unsupported OS/architecture combinations fail rather than selecting an x64
asset. A Windows setup.exe/MSI is launched normally and the command immediately
returns `status: installer-launched` with exit code 0. Do not wait for the
installer process, poll its exit code, inspect the registry, search for an
executable, rerun doctor automatically, or otherwise monitor completion. The
Windows installer UI owns the remaining user flow. Portable ZIP extraction is
synchronous, rejects path traversal, and requires the exact executable. macOS
requires an installed `.app`, and Linux requires an executable AppImage;
downloading a DMG/deb/rpm alone is never reported as an install.

Only an installation completed by this process atomically writes
`~/.my-llm-wiki/browser/install.json`. Starting a Windows setup.exe/MSI does not
write an install receipt or claim that Browser is installed. `--open` launches
the installed target after a synchronous install; for setup.exe/MSI, the native
installer is the only process launched and the command does not wait to open
Browser afterward. A source build is a development artifact, returns
action-required, and does not create an install receipt.

If htmlgo and GitHub both fail, stop retrying, explain that Browser is optional,
and finish the skills-only installation. `wiki_ops.py local-search` remains the
equivalent local path. Gitee mirrors source code, not Browser assets. A public
third-party relay requires separate user approval and is never the default.

Use a source build only when no release exists, no asset matches the exact
OS/architecture, or the user explicitly asks for a development build:

```bash
python3 scripts/install-browser.py --fallback-source
```

## 8. Final Verification And First Capture

Doctor verifies exact skill provenance, dependency closure, wiki registry,
Browser health, and the scoped capture toolchain. The default doctor deliberately
does not inspect, propose, or mention MCP. A missing explicit target, foreign
link, stale/mutated copy, missing required skill, or invalid registry reference
is an error. Browser absence is only a warning when skills-only use was selected.

For an installation-only request, complete the Browser choice above, then offer:

> Send me one article, webpage, video, or note you want to preserve. I will save
> it to RAW, compile it into your wiki, and show where to view it.

Only perform the first capture when the user supplies a real source and asks for
it. The loop is `capture → RAW → maintain/ingest → browse/search`; never create
a demonstration capture on the user's behalf.

The initial installation ends here. Do not discuss or configure MCP unless the
user makes a separate, explicit request for MCP after installation.

## 9. MCP Maintenance (Outside Initial Installation)

Enter this section only when the user explicitly asks to configure, inspect, or
remove MCP. An install request, Browser request, first capture, or default doctor
run is not consent. Local hosts default to the suite-owned stdio bridge; do not
register direct loopback HTTP or reintroduce `npx mcp-remote`.

Registration and inspection require an explicit host id:

```bash
python3 scripts/install-browser.py --register-mcp --host codex
python3 scripts/install-browser.py --unregister-mcp --host codex
python3 scripts/doctor.py --check-mcp --host codex
```

Registration first validates the Browser install receipt and its target. If the
receipt is missing/stale, it exits without reading or changing any host config.
Unregistration remains available so stale host entries can always be removed.

There is no prompt/TTY branch and no `--yes`. Host CLI commands run with closed
stdin and a 30-second timeout. A conflicting existing registration is an error:
unregister it explicitly, then register the v4 entry. Every attempted host-config
write is snapshotted and restored if the host CLI fails. WorkBuddy has no
registration CLI: the command prints the exact JSON and exits 3 for manual
action; never edit it directly.

The bridge resolves `~/.my-llm-wiki/connector/server-port`, then
`$LLM_WIKI_PORT`, then 8800, and reads the token file on every request. It
disables proxies. Remote relay access remains native HTTPS with an Authorization
header; do not put long-lived tokens in URLs.
