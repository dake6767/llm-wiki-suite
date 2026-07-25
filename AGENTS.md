# My LLM Wiki Agent Guide

My LLM Wiki has two supported product paths. Do not recreate the removed
bootstrap, Protocol 5, component-selection, receipt, or Windows Setup flows.

## 1. Choose the product path

### Recommended: Browser one-click path

Use the standard My LLM Wiki Browser package from the latest GitHub Release.
The first launch opens an embedded Setup page before the loopback web service
starts. The user selects only the Agent hosts that should receive the complete
Skills Pack, reviews exact foreign-skill conflicts, and may opt out of the
official toolchain. Setup then installs the project-tested toolchain, initializes
the Wiki, and opens it in the same app.

The Setup page is Tauri-local and calls the Rust Setup Core directly. It is not
served from `127.0.0.1`, is not reachable through relay, and remains available
later from the tray item **Skills 与工具链…**. The separate **Browser 设置…**
item owns Wiki roots, server port, autostart, relay, and sharing.

### Open: Skills-only path

Users may install one or more directories under `skills/` with their Agent's
normal skill installer or by copying them. This path does not require Browser,
Setup Core, or the official toolchain. Skills resolve capabilities through
their Provider Resolver and may use Agent, system, or custom tools that satisfy
the same SOP and RAW contracts. Do not claim that the project configures or
supports those third-party environments.

## 2. Headless Setup Core CLI

Each Release publishes `My-LLM-Wiki-CLI_<version>_<platform>_<arch>.zip`. The
Browser one-click path also installs the same binary at:

```text
~/.my-llm-wiki/bin/my-llm-wiki
```

On Windows the file is `my-llm-wiki.exe`. The CLI and Browser GUI call the same
Rust library and write the same state.

For an Agent-driven headless setup, inspect before writing:

```bash
my-llm-wiki inspect --json
```

Show the user one complete choice containing:

- one or more known host ids returned by inspection;
- every exact `foreign` destination that would be backed up and replaced;
- whether to use the recommended official toolchain or the open path;
- where to install, when the default volume is a poor fit. Inspection returns
  `install_root`, `install_anchor`, and `install_root_relocated`.

After one confirmation, run one command. Do not invent component or skill-subset
choices; the one-click path always installs the complete Skills Pack and the
official baseline is one product choice.

```bash
my-llm-wiki setup --host codex --replace /exact/foreign/path --json
```

`--install-root` moves packs, ASR models, and the Skills Pack off the home
volume, which matters most on Windows where they add several gigabytes to the
system drive and vanish with the profile on a reinstall. `~/.my-llm-wiki` then
becomes a directory link to that location, so every path Skills and MCP already
resolve keeps working unchanged; do not teach Skills to look anywhere else. The
target must be empty or already hold an installation — Setup Core never merges
into a directory holding unrelated files, and never moves or deletes an existing
install to satisfy a new choice.

Use `--without-toolchain` only when the user selected the open path. Setup Core
owns downloads, SHA-256 verification, size/free-space checks, pack postchecks,
exact backups, ownership markers, locking, and atomic activation. Never reproduce
those steps with shell recipes or global package managers.

## 3. State, repair, update, and uninstall

The minimal state is:

```text
~/.my-llm-wiki/setup-state.json
~/.my-llm-wiki/providers.json
```

`~/.my-llm-wiki` is a fixed path, not necessarily a directory: when the user
installed elsewhere it is a link to the install root, and these paths resolve
through it either way. Read the location from `inspect`/`status` rather than
assuming the two are the same.

Supported operations are:

```bash
my-llm-wiki status --json
my-llm-wiki repair --json
my-llm-wiki update --check --json
my-llm-wiki update --json
my-llm-wiki ensure-pack asr-zh --json
my-llm-wiki ensure-pack asr-other --json
my-llm-wiki uninstall --host codex --json
my-llm-wiki uninstall --all --json
```

`update --check` is read-only apart from network access. `update` reads one
jointly tested distribution manifest and updates only owned artifacts. When a
new Browser must replace the running app it returns `restart-required`; the
embedded Setup page drives the signed platform updater and resumes after
restart. `repair` restores the current distribution and never upgrades.

Uninstall requires explicit hosts or `--all`. It removes only paths carrying
the active ownership identity. Wiki, RAW, Provider configuration, and foreign
Skills are user data and are never removed by these commands. A host
destination that is a link is detached, never followed: the target is the one
installed Skills Pack the remaining hosts still read.

## 4. Official packs and network behavior

The internal immutable packs are:

```text
toolchain-base  # FFmpeg, yt-dlp, aria2c, Node/OpenCLI, document conversion
asr-zh          # SenseVoice/FunASR runtime; installed on first need
asr-other       # faster-whisper runtime; installed on first need
```

User machines never assemble these with pip, npm, Homebrew, apt, winget, or a
PATH fallback. Release CI builds and postchecks them on each supported target
once per immutable `pack_version`. Browser releases reuse the published
`packs-v<pack_version>` prerelease and compose a fresh jointly tested
distribution manifest; they never copy the large pack assets into every
application Release. Setup downloads from the project CDN first and canonical
GitHub Releases second, then verifies exact size and SHA-256 before extraction.
Model weights are never bundled into the immutable runtime pack. The Browser
completion page may explicitly prewarm the Chinese ASR models into the private
`~/.my-llm-wiki/models/` area; otherwise they remain a first-use download.
Model operations use only the pack-scoped environment; do not edit shell
profiles or global proxy/package-manager configuration.

## 5. Provider choice

Skills follow this precedence:

```text
task-explicit Provider
→ saved capability override
→ healthy official Provider
→ matching system Provider
→ configured custom Provider
```

The official toolchain is recommended and preferred because it is tested with
the published SOP. It is not mandatory. A task-level user choice always wins
and is not persisted unless the user asks for a lasting preference. Custom
providers use structured argv with an absolute executable; never store or run
shell command strings.

If no Provider satisfies a capability, offer the relevant supported fallback:

```text
my-llm-wiki ensure-pack toolchain-base
my-llm-wiki ensure-pack asr-zh
my-llm-wiki ensure-pack asr-other
```

Do not silently install a large ASR pack during capture. Ask before a meaningful
download, or use the Provider the user selected.

## 6. MCP boundary

MCP configuration is separate from initial setup. The Browser owns the MCP
server. Local stdio clients can use:

```text
~/.my-llm-wiki/bin/my-llm-wiki mcp-bridge
```

The bridge forces proxy-free loopback and reads the current Browser port/token
at runtime. Configure MCP only after an explicit request; never register it as a
side effect of setup, doctor, capture, or update.

## 7. Repository development

The product runtime lives in `apps/my-llm-wiki-browser/`; Skills live in
`skills/`. Release-only pack inputs live under `registry/`: the two component
specs, hashed platform requirements, the complete OpenCLI npm lock, and
`pack-release.json`. The small `scripts/` directory only locks, builds, and
verifies release artifacts. Any pack input change requires a new
`pack-release.json` version and digest; published pack releases are never
rewritten.

Useful checks:

```bash
python3 -m unittest scripts.test_distribution scripts.test_approval_safe
python3 -m unittest discover -s skills/my-llm-wiki/scripts -p 'test_*.py'
python3 -m unittest discover -s skills/my-llm-wiki-video/scripts -p 'test_*.py'
python3 scripts/check_approval_safety.py

cd apps/my-llm-wiki-browser
cargo test --workspace
cd frontend && npm ci && npm run build
```

Before checking or building the desktop crate locally, stage the shared CLI
sidecar from the repository root:

```bash
python3 scripts/stage_cli.py --debug
```

Do not add compatibility readers, dual writes, old CLI flags, or alternate
installation state machines. This major version intentionally starts from the
new state model; an existing Wiki remains ordinary user data and can be opened
without importing an old installer receipt.
