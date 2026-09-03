"""Tests for distributed lease/fencing in the p2p fabric."""
from __future__ import annotations

import unittest

from p2p.lease import LeaseState, LeaseProtocolError, LeaseIdentity


class LeaseTests(unittest.TestCase):
    def test_acquire_grants_lease(self):
        state = LeaseState()
        result = state.acquire(holder="worker-a", ttl_ms=10000)
        self.assertTrue(result["ok"])
        self.assertIn("lease_token", result)
        self.assertGreater(result["lease_epoch"], 0)

    def test_acquire_while_held_fails(self):
        state = LeaseState()
        state.acquire(holder="worker-a", ttl_ms=10000)
        with self.assertRaisesRegex(LeaseProtocolError, "lease_held"):
            state.acquire(holder="worker-b", ttl_ms=10000)

    def test_renew(self):
        state = LeaseState()
        result = state.acquire(holder="worker-a", ttl_ms=10000)
        ident = LeaseIdentity(result["instance_id"], result["lease_epoch"], result["lease_token"])
        renewed = state.renew(ident, ttl_ms=20000)
        self.assertTrue(renewed["ok"])
        self.assertGreater(renewed["remaining_ms"], 19000)

    def test_release(self):
        state = LeaseState()
        result = state.acquire(holder="worker-a", ttl_ms=10000)
        ident = LeaseIdentity(result["instance_id"], result["lease_epoch"], result["lease_token"])
        rel = state.release(ident)
        self.assertTrue(rel["ok"])
        with self.assertRaisesRegex(LeaseProtocolError, "no_lease"):
            state.begin_compute(ident)

    def test_wrong_instance_fails_closed(self):
        state = LeaseState()
        result = state.acquire(holder="worker-a", ttl_ms=10000)
        bad = LeaseIdentity("other-instance", result["lease_epoch"], result["lease_token"])
        with self.assertRaisesRegex(LeaseProtocolError, "wrong_instance"):
            state.begin_compute(bad)

    def test_wrong_epoch_fails_closed(self):
        state = LeaseState()
        result = state.acquire(holder="worker-a", ttl_ms=10000)
        bad = LeaseIdentity(result["instance_id"], result["lease_epoch"] + 1, result["lease_token"])
        with self.assertRaisesRegex(LeaseProtocolError, "stale_epoch"):
            state.begin_compute(bad)

    def test_wrong_token_fails_closed(self):
        state = LeaseState()
        result = state.acquire(holder="worker-a", ttl_ms=10000)
        bad = LeaseIdentity(result["instance_id"], result["lease_epoch"], "x" * 64)
        with self.assertRaisesRegex(LeaseProtocolError, "wrong_token"):
            state.begin_compute(bad)

    def test_begin_compute_fences_while_in_flight(self):
        state = LeaseState()
        r1 = state.acquire(holder="worker-a", ttl_ms=10000)
        id1 = LeaseIdentity(r1["instance_id"], r1["lease_epoch"], r1["lease_token"])
        state.begin_compute(id1)
        with self.assertRaisesRegex(LeaseProtocolError, "compute_in_flight"):
            state.acquire(holder="worker-b", ttl_ms=10000)

    def test_finish_compute_validates_twice(self):
        state = LeaseState()
        r = state.acquire(holder="worker-a", ttl_ms=10000)
        ident = LeaseIdentity(r["instance_id"], r["lease_epoch"], r["lease_token"])
        state.begin_compute(ident)
        self.assertTrue(state.finish_compute(ident, result_ready=True))
        with self.assertRaisesRegex(LeaseProtocolError, "no_lease"):
            state.finish_compute(ident, result_ready=True)

    def test_expired_lease_fences_at_commit(self):
        state = LeaseState(min_ttl_ms=1)
        r = state.acquire(holder="worker-a", ttl_ms=1)
        ident = LeaseIdentity(r["instance_id"], r["lease_epoch"], r["lease_token"])
        state.begin_compute(ident)
        import time
        time.sleep(0.02)
        self.assertFalse(state.finish_compute(ident, result_ready=True))

    def test_status_never_exposes_token(self):
        state = LeaseState()
        state.acquire(holder="worker-a", ttl_ms=10000)
        s = state.status()
        self.assertNotIn("lease_token", s)
        self.assertTrue(s["lease_active"])

    def test_restart_invalidates_old_leases(self):
        state1 = LeaseState()
        r = state1.acquire(holder="worker-a", ttl_ms=10000)
        state2 = LeaseState()
        with self.assertRaisesRegex(LeaseProtocolError, "wrong_instance"):
            state2.begin_compute(LeaseIdentity(r["instance_id"], r["lease_epoch"], r["lease_token"]))


if __name__ == "__main__":
    unittest.main()
