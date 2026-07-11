#!/usr/bin/env python3
"""Bridge MCP stdio clients to the local My LLM Wiki Browser HTTP endpoint.

The Browser intentionally owns the MCP implementation.  This process is only a
small, dependency-free transport adapter for hosts whose HTTP stack may send
loopback traffic through a system proxy.  It reads one JSON-RPC message per line
from stdin, POSTs it directly to the Browser, and writes JSON-RPC replies to
stdout.  Notifications receive HTTP 202 and therefore produce no stdout line.

Runtime settings are resolved for every request so Browser port changes and
token rotations do not require re-registering each host:

    registry/bootstrap.json -> mcp.port_resolution / mcp.token_file

The HTTP opener explicitly disables proxies.  stdout is protocol-only; all
diagnostics go to stderr.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import BinaryIO


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BOOTSTRAP = ROOT / "registry" / "bootstrap.json"
PROTOCOL_ERROR = -32000


class BridgeError(RuntimeError):
    """A runtime/configuration error that can be returned as JSON-RPC."""


def diagnostic(event: str, **fields: object) -> None:
    """Write one secret-free lifecycle record to stderr."""
    record = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "event": event,
        "pid": os.getpid(),
        **fields,
    }
    print(
        "mcp-stdio-bridge: " + json.dumps(record, ensure_ascii=False, separators=(",", ":")),
        file=sys.stderr,
        flush=True,
    )


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def load_mcp_config(path: Path = DEFAULT_BOOTSTRAP) -> dict:
    try:
        bootstrap = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeError(f"cannot read MCP bootstrap config {path}: {exc}") from exc
    mcp = bootstrap.get("mcp")
    if not isinstance(mcp, dict):
        raise BridgeError(f"missing mcp config in {path}")
    return mcp


def resolve_port(mcp: dict) -> int:
    resolution = mcp.get("port_resolution") or {}
    pref = resolution.get("pref_file")
    if pref:
        try:
            value = int(_expand(pref).read_text(encoding="utf-8").strip())
            if value >= 1024:
                return value
        except (OSError, ValueError):
            pass
    env_name = resolution.get("env", "PORT")
    env_value = os.environ.get(env_name, "").strip()
    if env_value.isdigit() and int(env_value) >= 1024:
        return int(env_value)
    value = int(resolution.get("default", 8800))
    if value < 1024:
        raise BridgeError(f"invalid MCP port: {value}")
    return value


def resolve_endpoint(mcp: dict, endpoint_override: str | None = None) -> str:
    if endpoint_override:
        return endpoint_override.rstrip("/")
    template = mcp.get("endpoint", "http://127.0.0.1:{port}/mcp")
    return str(template).replace("{port}", str(resolve_port(mcp))).rstrip("/")


def read_token(mcp: dict, token_file_override: str | None = None) -> str:
    token_file = token_file_override or mcp.get("token_file")
    if not token_file:
        return ""
    try:
        return _expand(str(token_file)).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def rpc_id(message: object) -> object | None:
    if isinstance(message, dict) and message.get("id") is not None:
        return message["id"]
    return None


def error_reply(message_id: object, detail: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": PROTOCOL_ERROR, "message": detail},
    }


class LocalHttpClient:
    """Proxy-free HTTP client with config re-resolution on every request."""

    def __init__(
        self,
        mcp: dict,
        *,
        endpoint: str | None = None,
        token_file: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.mcp = mcp
        self.endpoint_override = endpoint
        self.token_file_override = token_file
        self.timeout = timeout
        # Do not trust NO_PROXY or platform proxy-bypass implementations here.
        # The bridge exists specifically to guarantee that loopback stays local.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(self, payload: bytes) -> tuple[int, bytes]:
        endpoint = resolve_endpoint(self.mcp, self.endpoint_override)
        token = read_token(self.mcp, self.token_file_override)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "my-llm-wiki-mcp-stdio-bridge/1",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise BridgeError(f"Browser MCP is unreachable at {endpoint}: {exc}") from exc


def bridge_stream(client: LocalHttpClient, stdin: BinaryIO, stdout: BinaryIO) -> int:
    request_count = 0
    diagnostic(
        "start",
        ppid=os.getppid(),
        endpoint=resolve_endpoint(client.mcp, client.endpoint_override),
        tokenPresent=bool(read_token(client.mcp, client.token_file_override)),
    )
    for raw_line in stdin:
        payload = raw_line.strip()
        if not payload:
            continue
        request_count += 1
        try:
            message = json.loads(payload)
        except json.JSONDecodeError as exc:
            diagnostic("invalid-json", request=request_count, detail=exc.msg)
            reply = error_reply(None, f"invalid JSON from MCP client: {exc.msg}")
            try:
                stdout.write(json.dumps(reply, separators=(",", ":")).encode("utf-8") + b"\n")
                stdout.flush()
            except (BrokenPipeError, OSError) as write_exc:
                diagnostic("stdout-closed", request=request_count, error=type(write_exc).__name__)
                return 1
            continue

        message_id = rpc_id(message)
        method = message.get("method") if isinstance(message, dict) else None
        try:
            status, body = client.post(payload)
            if status == 202:
                continue
            if status == 200 and body:
                # Validate before writing so stdout always remains valid MCP JSONL.
                reply = json.loads(body)
            else:
                detail = body.decode("utf-8", errors="replace").strip()
                detail = detail or f"Browser MCP returned HTTP {status}"
                reply = error_reply(message_id, f"HTTP {status}: {detail}")
        except (BridgeError, json.JSONDecodeError) as exc:
            diagnostic(
                "request-error",
                request=request_count,
                method=method,
                error=type(exc).__name__,
                detail=str(exc),
            )
            if message_id is None:
                print(f"mcp-stdio-bridge: {exc}", file=sys.stderr, flush=True)
                continue
            reply = error_reply(message_id, str(exc))

        # Notifications have no response even if an intermediary returned an
        # unexpected body.  This preserves stdio JSON-RPC notification semantics.
        if message_id is None:
            continue
        try:
            stdout.write(json.dumps(reply, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
            stdout.flush()
        except (BrokenPipeError, OSError) as exc:
            diagnostic(
                "stdout-closed",
                request=request_count,
                method=method,
                error=type(exc).__name__,
            )
            return 1
    diagnostic("stdin-eof", requests=request_count)
    return 0


def probe(client: LocalHttpClient) -> dict:
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        separators=(",", ":"),
    ).encode("utf-8")
    status, body = client.post(payload)
    if status != 200:
        detail = body.decode("utf-8", errors="replace").strip()
        raise BridgeError(f"Browser MCP probe returned HTTP {status}: {detail}")
    try:
        reply = json.loads(body)
    except json.JSONDecodeError as exc:
        raise BridgeError(f"Browser MCP probe returned invalid JSON: {exc}") from exc
    tools = reply.get("result", {}).get("tools") if isinstance(reply, dict) else None
    if not isinstance(tools, list):
        raise BridgeError(f"Browser MCP probe did not return tools/list: {reply}")
    return {
        "ok": True,
        "endpoint": resolve_endpoint(client.mcp, client.endpoint_override),
        "tool_count": len(tools),
        "tools": [tool.get("name") for tool in tools if isinstance(tool, dict)],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_BOOTSTRAP)
    parser.add_argument("--endpoint", help="Override the local HTTP endpoint (mainly for tests).")
    parser.add_argument("--token-file", help="Override the bearer-token file.")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--probe", action="store_true", help="Call tools/list and exit.")
    parser.add_argument("--json", action="store_true", help="With --probe, print JSON output.")
    args = parser.parse_args(argv)

    try:
        mcp = load_mcp_config(args.config)
        client = LocalHttpClient(
            mcp,
            endpoint=args.endpoint,
            token_file=args.token_file,
            timeout=args.timeout,
        )
        if args.probe:
            result = probe(client)
            print(json.dumps(result, ensure_ascii=False) if args.json else (
                f"ok: {result['tool_count']} tools at {result['endpoint']}"
            ))
            return 0
        return bridge_stream(client, sys.stdin.buffer, sys.stdout.buffer)
    except (BridgeError, OSError, ValueError) as exc:
        print(f"mcp-stdio-bridge: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
