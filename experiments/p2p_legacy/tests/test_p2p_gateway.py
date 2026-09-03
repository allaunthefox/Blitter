"""Tests for the p2p gateway: query, compute, passthrough."""
from __future__ import annotations

import unittest

from p2p.gateway import Gateway
from p2p.designation import Designation, Load, Proximity
from p2p.routing import WorkSpec


class GatewayTests(unittest.TestCase):
    def test_query_returns_best_peer(self):
        gw = Gateway(node_id="router")
        peer = Designation(
            node_id="worker-1", capabilities=frozenset({"webgpu-blitter", "exact-i32"}),
            controlled_subsystems=frozenset({"accelerator-api"}),
            execution_surfaces=frozenset({"webgpu"}),
            accessibility_profile="sandbox",
            load=Load(active_compute=0, queued_compute=0, max_concurrent_compute=1),
            proximity=Proximity(rtt_ms=2, hop_count=1),
            availability_expires_unix_ms=9999999999999,
            sent_unix_ms=0,
        )
        gw.add_peer(peer)
        work = WorkSpec(goal_id="g", work_id="w", requires=frozenset({"webgpu-blitter"}))
        candidates = gw.query(work)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].node_id, "worker-1")

    def test_query_returns_empty_when_no_compatible(self):
        gw = Gateway(node_id="router")
        work = WorkSpec(goal_id="g", work_id="w", requires=frozenset({"wasm"}))
        candidates = gw.query(work)
        self.assertEqual(len(candidates), 0)

    def test_compute_acquires_lease(self):
        gw = Gateway(node_id="router")
        result = gw.compute({"a": [[0, 0, 1]]}, holder="worker-a", ttl_ms=10000)
        self.assertTrue(result["ok"])
        self.assertIn("lease_token", result)

    def test_passthrough_enforces_ttl(self):
        gw = Gateway(node_id="router")
        result = gw.passthrough({}, path=[], ttl=0, lease={}, query_id="q1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "ttl_exceeded")

    def test_passthrough_fills_path(self):
        gw = Gateway(node_id="router")
        result = gw.passthrough({}, path=["a", "b"], ttl=5, lease={}, query_id="q1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], ["a", "b"])

    def test_get_status(self):
        gw = Gateway(node_id="router")
        status = gw.get_status()
        self.assertEqual(status["gateway"], "ok")
        self.assertEqual(status["peers"], 0)

    def test_shutdown(self):
        gw = Gateway(node_id="router")
        gw.start()
        gw.shutdown()


if __name__ == "__main__":
    unittest.main()
