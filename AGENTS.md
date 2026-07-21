# Agent Installation Protocol 5

This repository has one installation interaction on every supported platform:

```text
inspect (read-only) → one complete user selection → plan → apply → result
```

The agent owns the conversation. The deterministic installer owns paths,
downloads, hashes, locks, backups, rollback, receipts, repair, and uninstall.
Never reproduce installer policy in conversation or execute a tool recipe by
hand. Protocol 4 flags and conversational install steps are unsupported.

## 1. Platform entry points

### macOS and Linux

Use `bootstrap.sh` only to acquire/validate the suite and dispatch Protocol 5.
Never pipe a remote script into a shell. For a URL-only install, download and
inspect it first:

```bash
curl -fsSLo bootstrap.sh --connect-timeout 5 --max-time 30 --retry 1 \
  https://raw.githubusercontent.com/dake6767/llm-wiki-suite/main/bootstrap.sh
sed -n '1,240p' bootstrap.sh
bash bootstrap.sh inspect --out inspection.json --json
```

Mainland-China fallback:

```bash
curl -fsSLo bootstrap.sh --connect-timeout 5 --max-time 30 --retry 1 \
  https://gitee.com/dake6767/llm-wiki-suite/raw/main/bootstrap.sh
sed -n '1,240p' bootstrap.sh
bash bootstrap.sh --repo-url https://gitee.com/dake6767/llm-wiki-suite.git \
  inspect --out inspection.json --json
```

When this checkout is explicitly intended, use:

```bash
bash bootstrap.sh --repo /absolute/path/to/llm-wiki-suite \
  inspect --out inspection.json --json
```

An existing checkout is never pulled implicitly. `--update` only fast-forwards
a clean `main` checkout. Python 3.10+ is the POSIX execution runtime; no Setup
GUI is used.

### Windows

Windows uses the frozen native core without opening its GUI. Download the exact
release named by `registry/bootstrap.json` → `agent_installer.release_tag`.
Mainland-China networks try the project Worker first; GitHub Releases is the
canonical fallback. Verify `SHA256SUMS-windows-setup.txt`, then invoke:

```text
My-LLM-Wiki-Setup.exe inspect --out inspection.json --json
```

Current download routes:

```text
https://wiki.htmlgo.to/_setup/latest/My-LLM-Wiki-Setup.exe
https://wiki.htmlgo.to/_setup/latest/SHA256SUMS-windows-setup.txt
https://github.com/dake6767/llm-wiki-suite/releases/latest/download/My-LLM-Wiki-Setup.exe
```

Do not run `bootstrap.sh`, install Git Bash/Python/Node, or open Setup merely to
drive the normal install. Double-clicking the EXE remains an optional recovery
frontend; it is not the documented agent path.

## 2. Inspect before asking

Run `inspect` first. It is read-only. Do not create an agent home, Wiki,
component directory, or selection file before showing the result.

Inspection returns every `registry/bootstrap.json` → `agent_hosts` entry with:

- expanded `detect_dir` and `skills_dir`;
- detected/not-detected state;
- every resolved skill destination and action;
- exact conflict paths;
- optional host configuration offers.

Directory presence is context, not consent. Select no host by default unless
the host UI can visibly mark the currently running agent as a recommendation
without committing it.

Inspection also returns the released managed components, download sizes,
installed state, Browser choice, network route, Wiki state, warnings, and
blockers. Global pip/npm/Homebrew/apt tools never satisfy a production
component.

## 3. Ask once

Present one native selection surface containing every choice below. The user
must be able to review the complete selection before confirming it:

1. one or more named agent hosts;
2. optional custom skills directories not represented by the registry;
3. all skills or an explicit skill subset;
4. every exact conflict destination that may be replaced;
5. managed components: Documents, Web, Video, Chinese ASR, non-Chinese ASR;
6. optional Browser desktop app;
7. optional Hermes hardening when Hermes is selected;
8. optional-component failure policy (`continue` is recommended).

Use registry host ids whenever possible. A custom target is only for a real
host the registry does not name. Never translate a known host into a custom
path. Start with no replacement authority; each conflict path must be selected
explicitly.

If the host supports a native multi-select/confirmation control, use it. Never
split these choices into later questions and never demand typed `yes`/`no`.
If a native control is unavailable, clearly present one complete structured
choice and obtain one confirmation before writing the Selection object.

The Selection schema is `registry/agent-install-v5.schema.json`. An empty
`skills` list means every active skill. Production mode is always `copy`.

## 4. Freeze, summarize, apply

Write the confirmed Selection to a temporary file, then freeze it:

```bash
bash bootstrap.sh plan \
  --inspection inspection.json \
  --selection selection.json \
  --out plan.json --json
```

On Windows replace `bash bootstrap.sh` with `My-LLM-Wiki-Setup.exe`.

Planning must fail if selection refers to another inspection, a conflict lacks
exact replacement authority, an authority path is unused, a released component
is unavailable, or a destination changed. Do not broaden the selection to make
planning pass.

After the user's single confirmation, run exactly one unattended apply:

```bash
bash bootstrap.sh apply --plan plan.json --json-events
```

Do not ask questions while apply is running. Relay useful phase progress and
keep monitoring until the terminal result exists. JSON events are informational;
`result.json` is authoritative.

## 5. Execution guarantees

The installer, not the agent, guarantees:

- closed stdin and argv-array execution;
- plan hash/platform/source/destination revalidation before writes;
- one non-waiting machine install lock;
- immutable skill copies with provenance manifests;
- exact-path backups and rollback;
- required idempotent Wiki initialization before final doctor;
- released per-platform component archives with size and SHA-256 verification;
- pre-write free-space validation against download plus expanded component size;
- private versioned Python/Node/tool roots under `~/.my-llm-wiki`;
- component postchecks before receipt activation;
- bounded project-mirror-first downloads and canonical GitHub fallback;
- atomic session journal, result, and install receipt;
- recovery of an interrupted core transaction on the next apply;
- Browser failure isolation from the valid core.

macOS/Linux never install global pip/npm packages or invoke brew, sudo, apt,
dnf, or pacman. Windows never falls back to PATH, winget, pip, npm, or a
development checkout. Skills on every OS resolve tools only from:

```text
~/.my-llm-wiki/install-state.json
```

The user may choose another Windows data root through the optional Setup
recovery UI. Setup preserves the public `~/.my-llm-wiki` and `~/wikis` paths by
using NTFS junctions; do not teach alternate profile paths.

## 6. Components and deferred actions

Stable component ids are:

```text
documents  web  video  asr-zh  asr-other
```

The release component manifest is the user-install trust boundary. Each
component is isolated and receipt-managed. ASR models may still download on
first real use; the receipt supplies route-specific runtime environment such as
`HF_ENDPOINT` without editing the user's shell profile.

The OpenCLI Browser Bridge is packaged with `web`. Loading an unpacked Chrome
extension requires the user's GUI identity, so apply does not pause for it. At
the end, show the returned folder and instructions, then offer verification
with the returned argv. A skipped or incomplete action yields
`action-required`, not a failed core. Do not direct mainland-China users to the
Chrome Web Store.

Browser is selected in the same initial surface. macOS/Linux use
`install-browser.py --open-web`; Windows Setup uses the silent native Browser
route. Browser launch/page-open failure is optional degradation and never
invalidates skills or Wiki initialization. Never print a token-bearing local
URL.

## 7. Terminal results

Interpret states exactly:

- `complete`: every selected automated item passed;
- `degraded`: core is valid and an optional item failed;
- `action-required`: automation finished and returned deferred manual work;
- `rolled-back`: core activation was restored after failure;
- `failed`: no valid new receipt was activated or rollback is incomplete.

Report the receipt path, session id, selected hosts/components, Wiki path,
Browser result, and every returned manual action. Do not infer hidden success
from console text.

For an installation-only request, finish with:

> Send me one article, webpage, video, or note you want to preserve. I will save
> it to RAW, compile it into your wiki, and show where to view it.

Do not create a demonstration capture. Capture only when the user supplies a
real source and asks for it.

## 8. Status, repair, and uninstall

Status is read-only:

```bash
bash bootstrap.sh status --json
bash bootstrap.sh status --session SESSION_ID --json
```

Repair re-inspects and reapplies only receipt-owned selections. It may replace a
mutated copy only when its Protocol 5 `install_id` still proves ownership:

```bash
bash bootstrap.sh repair --json-events
```

Uninstall requires an explicit structured selection or `--all`. It removes only
paths carrying the active receipt's ownership identity. It preserves all Wikis
and RAW data. A modified Hermes config is preserved rather than overwritten.

```bash
bash bootstrap.sh uninstall --selection uninstall-selection.json --json
bash bootstrap.sh uninstall --all --json
```

Use the corresponding EXE commands on Windows.

## 9. Boundaries outside installation

MCP is not part of initial installation, Browser installation, doctor, or first
capture. Configure or remove MCP only after a separate explicit user request.
The existing suite-owned stdio bridge remains the local default; never register
direct loopback HTTP or `npx mcp-remote` implicitly.

Gitee is a pull-only source mirror. GitHub remains canonical. Never create
mirror-only commits or silently substitute an undeclared third-party relay.
