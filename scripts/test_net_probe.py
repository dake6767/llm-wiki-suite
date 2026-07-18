#!/usr/bin/env python3
"""Tests for net_probe ecosystem routing (cn-mirrors)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "cn-mirrors" / "scripts"))
import net_probe  # noqa: E402


def rows(statuses: dict[str, str]) -> list[dict]:
    return [{"host": host, "status": status} for host, status in statuses.items()]


class EcosystemRouteTests(unittest.TestCase):
    def test_all_ok_routes_global(self) -> None:
        dev = rows({
            "github.com": "ok",
            "api.github.com": "ok",
            "objects.githubusercontent.com": "ok",
        })
        routes = net_probe.ecosystem_routes(dev, [])
        self.assertEqual(routes["github"], "global")

    def test_blocked_dev_with_ok_mirror_routes_cn(self) -> None:
        dev = rows({"github.com": "blocked"})
        mirrors = rows({"gitee.com": "ok"})
        routes = net_probe.ecosystem_routes(dev, mirrors)
        self.assertEqual(routes["github"], "cn")

    def test_slow_mirror_still_routes_cn_not_unavailable(self) -> None:
        # A slow mirror proved connectivity; unavailable would dead-end every
        # github-routed recipe (the observed win+CN doctor failure mode).
        dev = rows({"github.com": "blocked", "api.github.com": "blocked"})
        mirrors = rows({"gitee.com": "slow"})
        routes = net_probe.ecosystem_routes(dev, mirrors)
        self.assertEqual(routes["github"], "cn")

    def test_blocked_mirror_is_unavailable(self) -> None:
        dev = rows({"github.com": "blocked"})
        mirrors = rows({"gitee.com": "blocked"})
        routes = net_probe.ecosystem_routes(dev, mirrors)
        self.assertEqual(routes["github"], "unavailable")

    def test_slow_global_prefers_mirror(self) -> None:
        dev = rows({
            "pypi.org": "slow",
            "files.pythonhosted.org": "ok",
        })
        mirrors = rows({"pypi.tuna.tsinghua.edu.cn": "ok"})
        routes = net_probe.ecosystem_routes(dev, mirrors)
        self.assertEqual(routes["pypi"], "cn")

    def test_system_is_always_global(self) -> None:
        routes = net_probe.ecosystem_routes([], [])
        self.assertEqual(routes["system"], "global")


if __name__ == "__main__":
    unittest.main()
