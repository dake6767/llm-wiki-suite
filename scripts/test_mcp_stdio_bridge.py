#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("mcp-stdio-bridge.py")
SPEC = importlib.util.spec_from_file_location("mcp_stdio_bridge", SCRIPT)
bridge = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(bridge)


class McpHandler(BaseHTTPRequestHandler):
    auth_headers: list[str] = []

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        message = json.loads(self.rfile.read(length))
        type(self).auth_headers.append(self.headers.get("Authorization", ""))
        if "id" not in message:
            self.send_response(202)
            self.end_headers()
            return
        if message.get("method") == "tools/list":
            result = {"tools": [{"name": "list_wikis"}, {"name": "search_wiki"}]}
        else:
            result = {"echo": message.get("method")}
        payload = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


class BridgeTests(unittest.TestCase):
    def setUp(self):
        McpHandler.auth_headers = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), McpHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/mcp"
        self.tmp = tempfile.TemporaryDirectory()
        self.token_file = Path(self.tmp.name) / "token with spaces"
        self.token_file.write_text("first", encoding="utf-8")
        self.mcp = {
            "endpoint": self.endpoint,
            "token_file": str(self.token_file),
            "port_resolution": {"default": self.server.server_port},
        }

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_proxy_is_disabled_and_token_rotation_is_dynamic(self):
        with mock.patch.dict(
            os.environ,
            {"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1"},
        ):
            client = bridge.LocalHttpClient(self.mcp, timeout=1)
            status, _ = client.post(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
            self.assertEqual(status, 200)
            self.token_file.write_text("second", encoding="utf-8")
            status, _ = client.post(b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
            self.assertEqual(status, 200)
        self.assertEqual(McpHandler.auth_headers, ["Bearer first", "Bearer second"])

    def test_stdio_jsonl_and_notification_semantics(self):
        source = io.BytesIO(
            b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            b'{not-json}\n'
            b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
        )
        output = io.BytesIO()
        client = bridge.LocalHttpClient(self.mcp, timeout=1)
        self.assertEqual(bridge.bridge_stream(client, source, output), 0)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([reply.get("id") for reply in replies], [1, None, 2])
        self.assertIn("invalid JSON", replies[1]["error"]["message"])
        self.assertEqual(len(replies[2]["result"]["tools"]), 2)

    def test_probe_lists_tools(self):
        result = bridge.probe(bridge.LocalHttpClient(self.mcp, timeout=1))
        self.assertTrue(result["ok"])
        self.assertEqual(result["tools"], ["list_wikis", "search_wiki"])

    def test_lifecycle_diagnostics_are_secret_free(self):
        source = io.BytesIO(b"")
        output = io.BytesIO()
        stderr = io.StringIO()
        client = bridge.LocalHttpClient(self.mcp, timeout=1)
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(bridge.bridge_stream(client, source, output), 0)
        logs = stderr.getvalue()
        self.assertIn('"event":"start"', logs)
        self.assertIn('"event":"stdin-eof"', logs)
        self.assertIn('"tokenPresent":true', logs)
        self.assertNotIn(self.token_file.read_text(encoding="utf-8").strip(), logs)


if __name__ == "__main__":
    unittest.main()
