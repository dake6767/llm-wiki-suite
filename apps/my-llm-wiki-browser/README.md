# My LLM Wiki Browser

The desktop application, local Wiki server, native Setup Core, and shared
`my-llm-wiki` CLI live in this Rust workspace.

Browser starts with an application-native Setup window. Setup installs the full
embedded Skills Pack into selected agent hosts, optionally activates the
project-verified toolchain, initializes the default Wiki, and installs the
shared CLI. The local HTTP Wiki server starts only after Setup reaches a healthy
state.

## Layout

```text
apps/my-llm-wiki-browser/
├── crates/
│   ├── llm-wiki-core       # registry, parser, indexer, and full-text search
│   ├── llm-wiki-server     # local HTTP API, Wiki UI, and MCP endpoint
│   ├── llm-wiki-connector  # optional outbound relay connector
│   ├── llm-wiki-mcp        # MCP tool contract
│   └── llm-wiki-setup      # Setup Core and the shared my-llm-wiki CLI
├── desktop/src-tauri/      # Tauri windows, tray, updater, and Setup commands
└── frontend/               # Wiki UI plus the embedded Setup entry point
```

The native CLI provides `inspect`, `setup`, `status`, `repair`, `ensure-pack`,
`update`, `uninstall`, and `mcp-bridge`. The MCP bridge uses structured stdio
JSON-RPC and the authenticated local Rust server; it does not require Python or
a repository checkout.

## Local verification

From the repository root:

```bash
python3 scripts/stage_cli.py --debug

cd apps/my-llm-wiki-browser/frontend
npm ci
npm run build

cd ..
cargo test --workspace
cargo check -p llm-wiki-desktop
```

`stage_cli.py` builds the exact target-specific CLI sidecar name required by
Tauri. The staged binary is ignored by Git.

To run the app in development:

```bash
cd apps/my-llm-wiki-browser/desktop
cargo tauri dev
```

To produce a local platform installer, first stage a release sidecar and then
build Tauri:

```bash
python3 scripts/stage_cli.py
cd apps/my-llm-wiki-browser/desktop
cargo tauri build
```

## Runtime state

Product-owned state lives under `~/.my-llm-wiki/`:

- `setup-state.json` records Setup Core ownership and active pack versions;
- `providers.json` records user capability-provider choices;
- `packs/` contains immutable official capability packs;
- `models/asr-zh/` contains explicitly prewarmed Chinese ASR model snapshots;
- `skills/` holds the one installed copy of the Skills Pack;
- `bin/my-llm-wiki` is the shared CLI;
- `connector/` contains Browser port, token, and optional relay state;
- `wikis.json` is the Wiki registry.

Setup can install all of that on another volume — the common case on Windows,
where packs and ASR models add several gigabytes to the system drive and are
lost with the profile on a reinstall. `~/.my-llm-wiki` then becomes a directory
link (a junction on Windows) to the chosen location, so Skills running under a
third-party agent host keep resolving that fixed path without knowing anything
about the choice, and an MCP server already registered against
`%USERPROFILE%\.my-llm-wiki\bin\my-llm-wiki.exe` keeps working. State records
the real location, so pointing a rebuilt `~/.my-llm-wiki` at an intact volume
re-adopts the installation instead of downloading it again.

Each selected agent host receives `<host>/skills/<slug>` as a link to
`skills/<slug>` under the install root, so one Skills Pack serves every host.
A destination that cannot hold a link falls back to a private copy, recorded as
`mode: "copy"` in `setup-state.json`. Uninstall detaches links and never
follows them.

Wiki and RAW content live outside product state, normally at
`~/wikis/my-llm-wiki`, and are preserved by repair and uninstall.

## Remote access and logs

Remote access uses the optional hosted relay and is disabled by default. Browser
logs are written under `~/.my-llm-wiki/logs/`, rotate daily, and retain the seven
most recent files. Before first-time Setup activates the install root, logs use
the platform-local application-data directory so the Browser does not
pre-create the fixed `~/.my-llm-wiki` anchor. Connector keys, bearer tokens, URL
credentials, and token query values must never be logged.

Release builds currently use updater artifact signing but do not yet use Apple
Developer ID notarization or Windows Authenticode signing. First launch can
therefore show the platform's unverified-developer warning.
