# My LLM Wiki Browser

Tauri-based desktop/browser app for the `my-llm-wiki` ecosystem.

It reads local LLM-WIKI Markdown repositories, serves the browser UI and API from a
local Rust server, and can expose the same wiki through the relay connector for
remote browser/agent access.

## Layout

```text
apps/my-llm-wiki-browser/
├── crates/
│   ├── llm-wiki-core       # registry, parser, indexer, FTS search
│   ├── llm-wiki-server     # axum HTTP API + frontend serving
│   ├── llm-wiki-connector  # outbound relay connector (connects to the hosted relay)
│   └── llm-wiki-mcp        # MCP tool names/types; full Rust transport parity pending
├── desktop/src-tauri/      # Tauri shell, tray, token/port/relay controls
└── frontend/               # React/Vite UI
```

Remote access goes through a hosted relay service (off by default; enable it from
the tray). The relay's server side is operated separately and is not part of this
repository.

## Build

From this directory:

```bash
cd frontend
npm ci
npm run build

cd ..
cargo test --workspace

cd desktop/src-tauri
cargo tauri build
```

Verified output on macOS:

```text
target/release/bundle/macos/My LLM Wiki Browser.app
target/release/bundle/dmg/My LLM Wiki Browser_0.1.0_aarch64.dmg
```

Build outputs (`target/`, `frontend/dist/`, `node_modules/`) are intentionally ignored.

## Run In Development

```bash
cd frontend
npm ci

cd ../desktop/src-tauri
cargo tauri dev
```

The desktop app starts the local Rust server, opens the settings webview, and
provides tray actions for opening the local wiki, enabling/disabling the relay,
and copying the online wiki URL.

Agents can query the authenticated local API endpoint
`/api/v1/config/share` to check whether the relay is connected and, when it is,
retrieve the tokenized online wiki URL for the user-facing final response.

## Relay Reliability And Logs

The desktop app persists tracing output under
`~/.my-llm-wiki/logs/browser-relay.YYYY-MM-DD.log`. Logs rotate daily and the
seven most recent files are retained. Relay lifecycle entries carry a
`connection_id` and record reconnect reasons, heartbeats, request counts,
in-flight work, and send-queue capacity. Connector keys, Bearer tokens, URL
credentials, and `token` query values are never written.

The connector treats the relay as unhealthy and reconnects when any of these
conditions occurs:

- no inbound Worker frame arrives for 60 seconds;
- a WebSocket write exceeds 15 seconds, the writer task exits, or the bounded
  send queue remains blocked for 10 seconds;
- the Worker deployment version changes;
- two consecutive one-minute end-to-end probes fail. The probe calls the public
  relay URL's `/api/v1/healthz` route with the token in the Authorization header,
  so it covers Worker routing, WebSocket transport, local origin, and the return
  path rather than merely checking that heartbeat frames still arrive.

## Current Release Gates

- Rust/Tauri desktop build works from inside the suite.
- Local Web/API behavior is implemented in Rust.
- Relay connector is implemented in Rust and can be managed from the tray.
- Full MCP transport parity in Rust is still a release gate. The old Python
  backend was intentionally not migrated into this app; use the archived source
  project/history only as behavior reference if needed.
- macOS public binaries still need Developer ID signing, notarization, and
  stapling before being promoted from development builds.
