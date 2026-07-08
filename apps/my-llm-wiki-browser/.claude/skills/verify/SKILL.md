---
name: verify
description: Build, launch, and drive the my-llm-wiki-browser desktop app in an isolated sandbox HOME to verify changes end-to-end without touching the user's real instance or system state.
---

# Verify my-llm-wiki-browser changes

The app is a Tauri 2 tray app whose settings UI is a web page served by the
embedded axum server (`/desktop/config`). Verify at two surfaces: the HTTP API
(`/api/v1/...`) and the settings page in a real browser.

## Build & launch (isolated)

The user usually has a real instance running (often port 8081). Never launch
against the real HOME — token, port prefs, wiki registry, and the macOS
LaunchAgent plist all live under `$HOME`.

```bash
cd apps/my-llm-wiki-browser
npm run build --prefix frontend          # server falls back to frontend/dist in dev
cargo build -p llm-wiki-desktop
SANDBOX=$(mktemp -d)/home && mkdir -p "$SANDBOX"
HOME="$SANDBOX" PORT=8899 target/debug/llm-wiki-desktop > "$SANDBOX/app.log" 2>&1 &
```

Gotchas:
- `resolve_port`: persisted `~/.my-llm-wiki/connector/server-port` beats `PORT`
  env — another reason for the sandbox HOME.
- If the port is already bound the app silently skips starting its server
  (`app.log` says "port is already in use").
- Main window is hidden (`visible: false`, Accessory activation policy);
  launching is non-disruptive apart from a tray icon.

## Drive the HTTP surface

Auth token is generated on first launch:

```bash
TOKEN=$(cat "$SANDBOX/.my-llm-wiki/connector/token")
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8899/api/v1/config/server
```

## Drive the GUI surface

The settings page accepts the token as a query param:

```bash
OPENCLI_WINDOW=background opencli browser <session> open \
  "http://127.0.0.1:8899/desktop/config?token=$TOKEN"
```

Then `state` / `find` / `click` per the opencli-browser skill.

## Observe side effects

- Autostart: `$SANDBOX/Library/LaunchAgents/My LLM Wiki Browser.plist`
  appears/disappears on toggle.
- Prefs: `$SANDBOX/.my-llm-wiki/connector/` (token, server-port, relay-enabled).

## Cleanup

`kill` the launched pid, `opencli browser <session> close`. The sandbox HOME is
throwaway.
