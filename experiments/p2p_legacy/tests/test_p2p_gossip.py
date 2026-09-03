"""Tests for gossip mesh propagation and timing diagnostics."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from p2p.gossip import GossipMesh, annotate_received, MIN_SANE_UNIX_MS


class GossipTests(unittest.TestCase):
    def test_ingest_advertise(self):
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
            "availability_ttl_ms": 3500, "availability_expires_unix_ms": 9999999999999,
            "sent_unix_ms": 1000, "heartbeat_seq": 1,
        }
        entry = mesh.ingest_advertise(payload, 1100)
        self.assertEqual(entry.designation.node_id, "n2|persistent|x86_64|cpu=4|mem=8GiB|boot=abcd")
        self.assertEqual(len(mesh.peers), 1)

    def test_stale_peer_discarded(self):
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
            "availability_ttl_ms": 3500, "availability_expires_unix_ms": 9999999999999,
            "sent_unix_ms": 1000, "heartbeat_seq": 5,
        }
        mesh.ingest_advertise(payload, 1100)
        payload["heartbeat_seq"] = 3
        entry = mesh.ingest_advertise(payload, 1200)
        self.assertEqual(entry.designation.heartbeat_seq, 5)

    def test_annotate_reorder(self):
        payload = {"node_id": "n1", "heartbeat_seq": 2, "sent_unix_ms": 1000,
                   "availability_expires_unix_ms": 9999999999999}
        enriched = annotate_received(payload, 1300, previous_seq=5)
        self.assertTrue(enriched["reordered"])
        self.assertEqual(enriched["sequence_gap"], 0)

    def test_annotate_gap(self):
        payload = {"node_id": "n1", "heartbeat_seq": 10, "sent_unix_ms": 1000,
                   "availability_expires_unix_ms": 9999999999999}
        enriched = annotate_received(payload, 1300, previous_seq=5)
        self.assertEqual(enriched["sequence_gap"], 4)
        self.assertFalse(enriched["duplicate"])

    def test_annotate_duplicate(self):
        payload = {"node_id": "n1", "heartbeat_seq": 5, "sent_unix_ms": 1000,
                   "availability_expires_unix_ms": 9999999999999}
        enriched = annotate_received(payload, 1300, previous_seq=5)
        self.assertTrue(enriched["duplicate"])

    def test_epoch_clock_anomaly(self):
        payload = {"node_id": "n1", "heartbeat_seq": 1, "sent_unix_ms": 0,
                   "availability_expires_unix_ms": 9999999999999}
        enriched = annotate_received(payload, 1300, None)
        self.assertTrue(enriched["sender_clock_invalid"])
        self.assertTrue(enriched["time_anomaly"])

    def test_serialize_advertise(self):
        mesh = GossipMesh("n1")
        adv = mesh.serialize_advertise()
        self.assertEqual(adv["type"], "advertise")
        self.assertEqual(adv["node_id"], "n1")
        self.assertIn("capabilities", adv)

    def test_routable_peers(self):
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
            "availability_ttl_ms": 3500, "availability_expires_unix_ms": 9999999999999,
            "sent_unix_ms": 1000, "heartbeat_seq": 1,
        }
        mesh.ingest_advertise(payload, 1100)
        self.assertEqual(len(mesh.routable_peers()), 1)

    def test_lease_advertise_propagates(self):
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
            "availability_ttl_ms": 3500, "availability_expires_unix_ms": 9999999999999,
            "sent_unix_ms": 1000, "heartbeat_seq": 1,
        }
        mesh.ingest_advertise(payload, 1100)
        lease_payload = {
            "type": "lease_advertise", "schema": "mathpunch.p2p.gossip.v0",
            "node_id": "n2|persistent|x86_64|cpu=4|mem=8GiB|boot=abcd",
            "lease_epoch": 3, "lease_holder": "worker-a",
            "lease_active": True, "lease_expired": False, "in_flight": False,
            "sent_unix_ms": 1100, "heartbeat_seq": 2, "availability_expires_unix_ms": 9999999999999,
        }
        mesh.ingest_lease_advertise(lease_payload, 1200)
        entry = mesh.get_peer("n2|persistent|x86_64|cpu=4|mem=8GiB|boot=abcd")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.designation.lease_epoch, 3)
        self.assertTrue(entry.designation.lease_active)

    def test_deserialize(self):
        mesh = GossipMesh("n1")
        obj = mesh.deserialize('{"type":"advertise","schema":"mathpunch.p2p.gossip.v0"}')
        self.assertEqual(obj["type"], "advertise")


if __name__ == "__main__":
    unittest.main()
