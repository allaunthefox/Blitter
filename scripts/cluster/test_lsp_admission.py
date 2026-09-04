#!/usr/bin/env python3
from __future__ import annotations

import unittest

try:
    from . import lsp_admission as admission
except ImportError:
    import lsp_admission as admission


def presence(**overrides):
    value = {
        "schema": admission.LSP_PRESENCE_SCHEMA, "node_id": "node-a", "boot_id": "boot-1",
        "sent_unix_ms": 1_000, "availability_ttl_ms": 3_000,
        "availability_expires_unix_ms": 4_000, "capabilities": ["lean", "cpu:2"],
        "available": True, "retire_at_unix_ms": None,
    }
    value.update(overrides)
    return value


def work(**overrides):
    value = admission.LSPWorkSpec("goal", "work", "lean", frozenset({"lean"}), 1, 2, 1000)
    return admission.LSPWorkSpec(**{**value.__dict__, **overrides})


class AdmissionTests(unittest.TestCase):
    def test_presence_requires_consistent_fresh_announcement(self):
        parsed = admission.presence_from_mapping(presence(), now_unix_ms=2_000)
        self.assertTrue(parsed.is_fresh(3_999))
        with self.assertRaises(ValueError):
            admission.presence_from_mapping(presence(availability_expires_unix_ms=9_000))
        with self.assertRaises(ValueError):
            admission.presence_from_mapping(presence(), now_unix_ms=4_000)

    def test_placement_requires_capabilities_and_picks_deterministically(self):
        first = admission.presence_from_mapping(presence(node_id="z"))
        second = admission.presence_from_mapping(presence(node_id="a"))
        self.assertEqual(admission.place_lsp_work(work(), [first, second], now_unix_ms=2_000).node_id, "a")
        self.assertFalse(admission.place_lsp_work(work(required_capabilities=frozenset({"coq"})), [first], now_unix_ms=2_000).admitted)

    def test_lease_fences_expiry_and_rejects_stale_token(self):
        lease = admission.LeaseStateMachine()
        grant = lease.acquire("worker", now_unix_ms=0, ttl_ms=10)
        with self.assertRaises(RuntimeError):
            lease.acquire("other", now_unix_ms=1, ttl_ms=10)
        with self.assertRaises(RuntimeError):
            lease.renew("worker", grant.token, now_unix_ms=10, ttl_ms=10)
        self.assertEqual(lease.state.status, admission.LeaseStatus.FENCED)
        next_grant = lease.acquire("other", now_unix_ms=11, ttl_ms=10)
        self.assertGreater(next_grant.token, grant.token)
        with self.assertRaises(PermissionError):
            lease.release("worker", grant.token, now_unix_ms=12)

    def test_release_requires_current_fencing_token(self):
        lease = admission.LeaseStateMachine()
        grant = lease.acquire("worker", now_unix_ms=1, ttl_ms=10)
        lease.release("worker", grant.token, now_unix_ms=2)
        self.assertEqual(lease.state.status, admission.LeaseStatus.RELEASED)
        with self.assertRaises(PermissionError):
            lease.release("worker", grant.token, now_unix_ms=3)

    def test_in_flight_work_blocks_replacement_and_fences_commit(self):
        lease = admission.LeaseStateMachine()
        grant = lease.acquire("worker", now_unix_ms=0, ttl_ms=10)
        lease.begin_compute("worker", grant.token, now_unix_ms=1)
        with self.assertRaises(RuntimeError):
            lease.acquire("other", now_unix_ms=11, ttl_ms=10)
        self.assertFalse(lease.commit_compute("worker", grant.token, now_unix_ms=11))
        self.assertFalse(lease.state.in_flight)
        replacement = lease.acquire("other", now_unix_ms=12, ttl_ms=10)
        self.assertGreater(replacement.token, grant.token)


if __name__ == "__main__":
    unittest.main()
