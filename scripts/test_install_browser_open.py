#!/usr/bin/env python3
"""First-run open: launch Browser, then show its local page."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("install-browser.py")
SPEC = importlib.util.spec_from_file_location("install_browser", SCRIPT)
installer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(installer)

BOOTSTRAP = json.loads(
    (Path(__file__).resolve().parent.parent / "registry" / "bootstrap.json").read_text(
        encoding="utf-8"
    )
)


class FirstRunUrlTests(unittest.TestCase):
    def test_registry_first_run_carries_everything_the_open_needs(self):
        first_run = BOOTSTRAP["first_run"]
        for key in ("host", "port_pref_file", "default_port", "health_path",
                    "token_file", "ready_timeout_seconds"):
            self.assertIn(key, first_run)

    def test_port_resolution_prefers_pref_file_then_env_then_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            pref = Path(tmp) / "server-port"
            first_run = {"port_pref_file": str(pref), "port_env": "LLM_WIKI_PORT",
                         "default_port": 8800}

            with mock.patch.dict(installer.os.environ, {}, clear=False):
                installer.os.environ.pop("LLM_WIKI_PORT", None)
                self.assertEqual(installer.resolve_first_run_port(first_run), 8800)

            with mock.patch.dict(installer.os.environ, {"LLM_WIKI_PORT": "9001"}):
                self.assertEqual(installer.resolve_first_run_port(first_run), 9001)

            pref.write_text("9100\n", encoding="utf-8")
            with mock.patch.dict(installer.os.environ, {"LLM_WIKI_PORT": "9001"}):
                self.assertEqual(installer.resolve_first_run_port(first_run), 9100)

    def test_privileged_and_unparseable_ports_fall_through_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            pref = Path(tmp) / "server-port"
            pref.write_text("80\n", encoding="utf-8")
            first_run = {"port_pref_file": str(pref), "port_env": "LLM_WIKI_PORT",
                         "default_port": 8800}
            with mock.patch.dict(installer.os.environ, {"LLM_WIKI_PORT": "not-a-port"}):
                self.assertEqual(installer.resolve_first_run_port(first_run), 8800)

    def test_url_carries_the_token_because_a_bare_loopback_url_is_401(self):
        first_run = {"host": "127.0.0.1"}
        self.assertEqual(
            installer.first_run_url(first_run, 8800, "s3cret"),
            "http://127.0.0.1:8800/?token=s3cret",
        )

    def test_url_percent_encodes_the_token(self):
        self.assertEqual(
            installer.first_run_url({"host": "127.0.0.1"}, 8800, "a b/c"),
            "http://127.0.0.1:8800/?token=a+b%2Fc",
        )

    def test_url_stays_plain_when_no_token_is_persisted_yet(self):
        self.assertEqual(
            installer.first_run_url({"host": "127.0.0.1"}, 8800, ""),
            "http://127.0.0.1:8800/",
        )

    def test_token_is_read_from_the_registry_path_and_missing_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "token"
            self.assertEqual(installer.read_first_run_token({"token_file": str(token)}), "")
            token.write_text("abc123\n", encoding="utf-8")
            self.assertEqual(
                installer.read_first_run_token({"token_file": str(token)}), "abc123"
            )


class ReadinessTests(unittest.TestCase):
    FIRST_RUN = {"host": "127.0.0.1", "health_path": "/api/v1/healthz",
                 "ready_timeout_seconds": 5}

    def test_auth_gated_response_counts_as_ready(self):
        error = urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
        opener = mock.Mock()
        opener.open.side_effect = error
        with mock.patch.object(installer.urllib.request, "build_opener", return_value=opener):
            self.assertTrue(installer.wait_for_browser_ready(self.FIRST_RUN, 8800))

    def test_refused_connection_retries_until_the_deadline_then_gives_up(self):
        opener = mock.Mock()
        opener.open.side_effect = OSError("connection refused")
        clock = iter([0.0, 0.0, 3.0, 9.0])
        with mock.patch.object(installer.urllib.request, "build_opener", return_value=opener), \
             mock.patch.object(installer.time, "monotonic", lambda: next(clock)), \
             mock.patch.object(installer.time, "sleep"):
            self.assertFalse(installer.wait_for_browser_ready(self.FIRST_RUN, 8800))
        self.assertEqual(opener.open.call_count, 3)

    def test_server_error_is_not_treated_as_ready(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError("u", 500, "boom", {}, None)
        clock = iter([0.0, 0.0, 99.0])
        with mock.patch.object(installer.urllib.request, "build_opener", return_value=opener), \
             mock.patch.object(installer.time, "monotonic", lambda: next(clock)), \
             mock.patch.object(installer.time, "sleep"):
            self.assertFalse(installer.wait_for_browser_ready(self.FIRST_RUN, 8800))


class OpenFirstRunPageTests(unittest.TestCase):
    def test_open_uses_the_token_written_during_the_launch_it_just_waited_for(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "token"
            config = {"first_run": {"host": "127.0.0.1", "default_port": 8800,
                                    "health_path": "/api/v1/healthz",
                                    "token_file": str(token)}}

            def ready(*_args, **_kwargs):
                token.write_text("late-token\n", encoding="utf-8")
                return True

            with mock.patch.object(installer, "wait_for_browser_ready", side_effect=ready), \
                 mock.patch.object(installer, "open_in_system_browser") as opened:
                self.assertTrue(installer.open_first_run_page(config, dry_run=False))
            opened.assert_called_once_with("http://127.0.0.1:8800/?token=late-token")

    def test_a_server_that_never_answers_is_reported_without_opening_anything(self):
        config = {"first_run": {"host": "127.0.0.1", "default_port": 8800,
                                "health_path": "/api/v1/healthz"}}
        with mock.patch.object(installer, "wait_for_browser_ready", return_value=False), \
             mock.patch.object(installer, "open_in_system_browser") as opened:
            self.assertFalse(installer.open_first_run_page(config, dry_run=False))
        opened.assert_not_called()

    def test_a_failed_opener_never_leaks_the_token_or_fails_the_install(self):
        config = {"first_run": {"host": "127.0.0.1", "default_port": 8800,
                                "health_path": "/api/v1/healthz"}}
        with mock.patch.object(installer, "wait_for_browser_ready", return_value=True), \
             mock.patch.object(installer, "read_first_run_token", return_value="s3cret"), \
             mock.patch.object(installer, "open_in_system_browser",
                               side_effect=RuntimeError("no xdg-open")), \
             mock.patch.object(installer.sys, "stderr") as err:
            self.assertFalse(installer.open_first_run_page(config, dry_run=False))
        printed = "".join(str(c.args[0]) for c in err.write.call_args_list if c.args)
        self.assertNotIn("s3cret", printed)

    def test_dry_run_opens_nothing(self):
        config = {"first_run": {"host": "127.0.0.1", "default_port": 8800,
                                "health_path": "/api/v1/healthz"}}
        with mock.patch.object(installer, "wait_for_browser_ready") as wait, \
             mock.patch.object(installer, "open_in_system_browser") as opened:
            self.assertTrue(installer.open_first_run_page(config, dry_run=True))
        wait.assert_not_called()
        opened.assert_not_called()


class CliWiringTests(unittest.TestCase):
    def test_open_web_implies_open_so_the_app_is_running_before_the_page_opens(self):
        with mock.patch.object(installer.sys, "argv", ["install-browser.py", "--open-web"]), \
             mock.patch.object(installer, "load_bootstrap", return_value={
                 "browser": {"download_policy": {"directory": "/tmp/x"},
                             "operation_lock_file": "/tmp/lock"}}), \
             mock.patch.object(installer, "advisory_lock"), \
             mock.patch.object(installer, "perform_browser_install",
                               return_value=0) as perform:
            installer.main()
        args = perform.call_args[0][1]
        self.assertTrue(args.open_web)
        self.assertTrue(args.open)

    def test_page_opens_only_after_a_successful_launch(self):
        config = {"browser": {}}
        args = mock.Mock(open=True, open_web=True, dry_run=False, version="latest",
                         download_dir="/tmp/x", repo=None, fallback_source=False)
        with mock.patch.object(installer, "infer_repo", return_value=None), \
             mock.patch.object(installer, "install_release",
                               return_value=(Path("/tmp/a"), Path("/tmp/b"), "htmlgo")), \
             mock.patch.object(installer, "write_install_receipt"), \
             mock.patch.object(installer, "maybe_launch_installed",
                               side_effect=RuntimeError("launch failed")), \
             mock.patch.object(installer, "open_first_run_page") as opened:
            self.assertEqual(installer.perform_browser_install(config, args), 3)
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
