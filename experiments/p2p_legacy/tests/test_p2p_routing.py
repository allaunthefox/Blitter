"""Tests for QoS-tiered nearest-neighbor routing."""
from __future__ import annotations

import unittest

from p2p.designation import Designation
from p2p.routing import WorkSpec, best_route, route, QoS_WEIGHTS


def _node(node_id, active=0, queued=0, max_c=1, rtt=5, capabilities=None, profile="sandbox"):
    caps = capabilities or ["webgpu-blitter", "exact-i32"]
    return Designation(
        node_id=node_id, instance_id="",
        capabilities=frozenset(caps),
        controlled_subsystems=frozenset({"accelerator-api"}),
        execution_surfaces=frozenset({"webgpu"}),
        accessibility_profile=profile,
        load=__import__('p2p.designation').designation.Load(active_compute=active, queued_compute=queued, max_concurrent_compute=max_c),
        proximity=__import__('p2p.designation').designation.Proximity(rtt_ms=rtt),
        availability_expires_unix_ms=9999999999999,
        sent_unix_ms=0,
    )


class RoutingTests(unittest.TestCase):
    def test_basic_route(self):
        nodes = [_node("a", rtt=2), _node("b", rtt=10)]
        work = WorkSpec(goal_id="g", work_id="w", requires=frozenset({"webgpu-blitter"}))
        best = best_route(work, nodes)
        self.assertEqual(best.node_id, "a")

    def test_latency_sensitive_picks_lowest_load(self):
        nodes = [
            _node("loaded", active=1, queued=0, rtt=2, capabilities=["webgpu-blitter"]),
            _node("idle", active=0, queued=0, rtt=3, capabilities=["webgpu-blitter"]),
        ]
        work = WorkSpec(goal_id="g", work_id="w", requires=frozenset({"webgpu-blitter"}), qos_tier="latency_sensitive")
        best = best_route(work, nodes)
        self.assertEqual(best.node_id, "idle")

    def test_throughput_sensitive_prefers_capability_over_rtt(self):
        nodes = [
            _node("fast_limited", active=0, queued=0, rtt=1, capabilities=["webgpu-blitter"]),
            _node("slow_capable", active=0, queued=0, rtt=100, capabilities=["webgpu-blitter", "exact-i32"]),
        ]
        work = WorkSpec(
            goal_id="g", work_id="w",
            requires=frozenset({"webgpu-blitter", "exact-i32"}),
            preference_weights={"exact-i32": 100},
            qos_tier="throughput_sensitive",
        )
        best = best_route(work, nodes)
        self.assertEqual(best.node_id, "slow_capable")

    def test_throughput_sensitive_tolerates_load(self):
        nodes = [
            _node("idle", active=0, queued=0, rtt=10, capabilities=["webgpu-blitter"]),
            _node("loaded", active=1, queued=0, rtt=1, capabilities=["webgpu-blitter"]),
        ]
        work = WorkSpec(goal_id="g", work_id="w", requires=frozenset({"webgpu-blitter"}), qos_tier="throughput_sensitive")
        best = best_route(work, nodes)
        self.assertEqual(best.node_id, "loaded")

    def test_default_is_balanced(self):
        nodes = [
            _node("idle", active=0, queued=0, rtt=2, capabilities=["webgpu-blitter"]),
            _node("loaded", active=1, queued=0, rtt=1, capabilities=["webgpu-blitter"]),
        ]
        work = WorkSpec(goal_id="g", work_id="w", requires=frozenset({"webgpu-blitter"}), qos_tier="default")
        best = best_route(work, nodes)
        self.assertEqual(best.node_id, "idle")

    def test_no_compatible_returns_none(self):
        nodes = [_node("a", capabilities=["wasm"])]
        work = WorkSpec(goal_id="g", work_id="w", requires=frozenset({"webgpu-blitter"}))
        self.assertIsNone(best_route(work, nodes))

    def test_stale_peer_excluded(self):
        from p2p.gossip import GossipMesh
        mesh = GossipMesh("n1")
        payload = {
            "type": "advertise", "schema": "mathpunch.p2p.gossip.v0",
            "node_id": "n2|persistent|x86_64|cpu=4|mem=8GiB|boot=abcd",
            "instance_id": "n2:1:1",
            "capabilities": ["webgpu-blitter"],
            "controlled_subsystems": ["accelerator-api"],
            "execution_surfaces": ["webgpu"],
            "accessibility_profile": "sandbox",
            "spawn_semantics_version": 1,
            "load": {"active_compute": 0, "queued_compute": 0, "max_concurrent_compute": 1},
            "proximity": {"rtt_ms": 5, "hop_count": 1},
            "lifecycle": "persistent", "ephemeral": False,
            "availability_ttl_ms": 100, "availability_expires_unix_ms": 100,
            "sent_unix_ms": 0, "heartbeat_seq": 1,
        }
        mesh.ingest_advertise(payload, 0)
        routed = [e.designation for e in mesh.routable_peers()]
        self.assertEqual(len(routed), 0)

    def test_qos_weights_exist(self):
        self.assertIn("latency_sensitive", QoS_WEIGHTS)
        self.assertIn("throughput_sensitive", QoS_WEIGHTS)
        self.assertIn("default", QoS_WEIGHTS)
        self.assertLess(QoS_WEIGHTS["throughput_sensitive"]["load_weight"], QoS_WEIGHTS["default"]["load_weight"])
        self.assertGreater(QoS_WEIGHTS["latency_sensitive"]["load_weight"], QoS_WEIGHTS["default"]["load_weight"])

    def test_invalid_qos_tier_rejected(self):
        with self.assertRaises(ValueError):
            WorkSpec(goal_id="g", work_id="w", qos_tier="bogus")

    def test_accessibility_rank_tiebreak(self):
        nodes = [
            _node("sandbox", profile="sandbox", capabilities=["webgpu-blitter", "exact-i32"]),
            _node("direct", profile="direct", capabilities=["webgpu-blitter", "exact-i32"]),
        ]
        work = WorkSpec(goal_id="g", work_id="w", requires=frozenset({"webgpu-blitter", "exact-i32"}))
        best = best_route(work, nodes)
        self.assertEqual(best.node_id, "direct")


if __name__ == "__main__":
    unittest.main()
