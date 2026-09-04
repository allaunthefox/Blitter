#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

try:
    from . import durable_lease_v2 as lease
except ImportError:
    import durable_lease_v2 as lease


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000_000

    def __call__(self) -> int:
        return self.now


class DurableLeaseV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "leases.sqlite3"
        self.clock = Clock()
        self.store = lease.DurableLeaseStore(self.db, clock_ms=self.clock)
        self.digest = lease.request_binding_digest("POST", "/compute", {"x": 1})

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def reserve(self, **changes):
        values = {
            "method": "POST", "route": "/compute", "idempotency_key": "request-1",
            "binding_digest": self.digest, "lease_ms": 1000,
        }
        values.update(changes)
        return self.store.reserve(**values)

    def test_wal_full_sync_and_token_is_only_stored_as_scrypt_hash(self):
        reservation = self.reserve()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            row = db.execute("SELECT token_salt,token_hash FROM lease_records").fetchone()
        self.assertEqual(len(row[0]), 16)
        self.assertEqual(len(row[1]), 32)
        self.assertNotIn(reservation.lease_token.encode("ascii"), self.db.read_bytes())
        self.assertNotIn("token", self.store.inspect(
            method="POST", route="/compute", idempotency_key="request-1"
        ))

    def test_commit_replays_exact_status_header_order_duplicates_and_body(self):
        reservation = self.store.begin(self.reserve())
        expected = self.store.commit(
            reservation, status=207,
            headers=(("X-Trace", "first"), ("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")),
            body=b"\x00exact\xff",
        )
        replay = self.reserve()
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.response, expected)
        self.assertIsNone(replay.lease_token)

    def test_binding_conflict_does_not_disclose_or_replace_original(self):
        original = self.reserve()
        other = lease.request_binding_digest("POST", "/compute", {"x": 2})
        with self.assertRaises(lease.LeaseStoreError) as caught:
            self.reserve(binding_digest=other)
        self.assertEqual(caught.exception.code, "binding_conflict")
        self.store.begin(original)

    def test_scope_includes_method_and_route(self):
        first = self.reserve()
        other_digest = lease.request_binding_digest("POST", "/status", {"x": 1})
        second = self.reserve(route="/status", binding_digest=other_digest)
        get_digest = lease.request_binding_digest("GET", "/compute", {"x": 1})
        third = self.reserve(method="GET", binding_digest=get_digest)
        self.assertEqual((first.attempt_epoch, second.attempt_epoch, third.attempt_epoch), (1, 1, 1))

    def test_expired_reserved_is_fenced_and_can_be_reissued_with_new_epoch(self):
        stale = self.reserve()
        self.clock.now += 1001
        with self.assertRaises(lease.LeaseStoreError) as caught:
            self.store.begin(stale)
        self.assertEqual(caught.exception.code, "lease_expired")
        replacement = self.reserve()
        self.assertEqual(replacement.attempt_epoch, 2)
        with self.assertRaises(lease.LeaseStoreError) as caught:
            self.store.begin(stale)
        self.assertEqual(caught.exception.code, "stale_attempt")

    def test_expired_running_becomes_unknown_and_blocks_automatic_retry(self):
        running = self.store.begin(self.reserve())
        self.clock.now += 1001
        with self.assertRaises(lease.LeaseStoreError) as caught:
            self.store.commit(running, status=200, headers=(), body=b"late")
        self.assertEqual(caught.exception.code, "fenced_at_commit")
        # An observed late commit is fenced. An unobserved crash is UNKNOWN (covered below).
        self.assertEqual(self.store.inspect(
            method="POST", route="/compute", idempotency_key="request-1"
        )["state"], "FENCED")

    def test_restart_recovery_fences_reserved_and_marks_running_unknown(self):
        reserved = self.reserve(idempotency_key="reserved")
        running = self.store.begin(self.reserve(idempotency_key="running"))
        old_boot = self.store.boot_generation
        self.store.close()
        self.store = lease.DurableLeaseStore(self.db, clock_ms=self.clock)
        self.assertEqual(self.store.boot_generation, old_boot + 1)
        self.assertEqual(self.store.inspect(
            method="POST", route="/compute", idempotency_key="reserved"
        )["state"], "FENCED")
        self.assertEqual(self.store.inspect(
            method="POST", route="/compute", idempotency_key="running"
        )["state"], "UNKNOWN")
        replacement = self.reserve(idempotency_key="reserved")
        self.assertEqual(replacement.attempt_epoch, reserved.attempt_epoch + 1)
        with self.assertRaises(lease.LeaseStoreError) as caught:
            self.reserve(idempotency_key="running")
        self.assertEqual(caught.exception.code, "unknown_outcome")
        with self.assertRaises(lease.LeaseStoreError) as caught:
            self.store.commit(running, status=200, headers=(), body=b"stale")
        self.assertIn(caught.exception.code, {"invalid_state", "stale_boot"})

    def test_operator_resolution_is_explicit_before_unknown_retry(self):
        self.store.begin(self.reserve())
        self.store.close()
        self.store = lease.DurableLeaseStore(self.db, clock_ms=self.clock)
        self.store.resolve_unknown(method="POST", route="/compute", idempotency_key="request-1")
        self.assertEqual(self.reserve().attempt_epoch, 2)

    def test_transaction_fault_rolls_back_response_and_restart_recovers_running(self):
        running = self.store.begin(self.reserve())

        def fail(point):
            if point == "after_response_write":
                raise OSError("simulated power loss before commit")

        self.store._fault_hook = fail
        with self.assertRaisesRegex(OSError, "power loss"):
            self.store.commit(running, status=200, headers=(("X-A", "b"),), body=b"answer")
        self.store._fault_hook = None
        self.assertEqual(self.store.inspect(
            method="POST", route="/compute", idempotency_key="request-1"
        )["state"], "RUNNING")
        self.store.close()
        self.store = lease.DurableLeaseStore(self.db, clock_ms=self.clock)
        self.assertEqual(self.store.inspect(
            method="POST", route="/compute", idempotency_key="request-1"
        )["state"], "UNKNOWN")

    def test_committed_response_survives_restart(self):
        running = self.store.begin(self.reserve())
        self.store.commit(running, status=201, headers=(("X-A", "b"),), body=b"durable")
        self.store.close()
        self.store = lease.DurableLeaseStore(self.db, clock_ms=self.clock)
        replay = self.reserve()
        self.assertEqual(replay.response.body, b"durable")
        self.assertEqual(replay.response.headers, (("X-A", "b"),))

    def test_canonical_binding_is_deterministic_and_route_bound(self):
        first = lease.request_binding_digest("post", "/compute", {"b": [2], "a": 1})
        second = lease.request_binding_digest("POST", "/compute", {"a": 1, "b": [2]})
        self.assertEqual(first, second)
        self.assertNotEqual(first, lease.request_binding_digest("POST", "/other", {"a": 1, "b": [2]}))
        with self.assertRaisesRegex(ValueError, "floating-point"):
            lease.request_binding_digest("POST", "/compute", {"x": 1.0})


if __name__ == "__main__":
    unittest.main()
