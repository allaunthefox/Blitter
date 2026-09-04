#!/usr/bin/env python3
"""Regression tests for the target-side LSP session lease service.

Covers atomic ownership (concurrent acquire race), fencing (commit fence and
in-flight replacement block), restart invalidation (fresh ``instance_id``),
TTL expiry, and the exact ``LSPLeaseClient`` wire shapes, against both the
state machine and a live loopback HTTP server.
"""
from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.error
import urllib.request

try:
    from . import lsp_lease_server as server_mod
    from . import lsp_lease as client_mod
except ImportError:
    import lsp_lease_server as server_mod
    import lsp_lease as client_mod


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def acquire_kwargs(**overrides):
    payload = {
        "holder": "test-holder",
        "goal_id": "goal-1",
        "work_id": "work-1",
        "worker_id": "node-1",
        "worker_generation": 3,
        "descriptor_digest": DIGEST_A,
        "image_digest": DIGEST_B,
        "ttl_ms": 30_000,
    }
    payload.update(overrides)
    return payload


class StateMachineTests(unittest.TestCase):
    def test_acquire_issues_monotonic_epoch_and_unique_sessions(self):
        state = server_mod.LSPLeaseState()
        first = state.acquire(**acquire_kwargs())
        self.assertEqual(first["lease_epoch"], 1)
        state.release(
            server_mod.parse_identity(
                first["session_id"], first["instance_id"],
                first["lease_epoch"], first["lease_token"],
            )
        )
        second = state.acquire(**acquire_kwargs())
        self.assertEqual(second["lease_epoch"], 2)
        self.assertNotEqual(second["session_id"], first["session_id"])
        self.assertNotEqual(second["lease_token"], first["lease_token"])

    def test_double_acquire_conflicts_without_replacement(self):
        state = server_mod.LSPLeaseState()
        state.acquire(**acquire_kwargs())
        with self.assertRaises(server_mod.LeaseProtocolError) as ctx:
            state.acquire(**acquire_kwargs(holder="intruder"))
        self.assertEqual(ctx.exception.code, "lease_held")

    def test_renew_requires_exact_identity_before_deadline(self):
        state = server_mod.LSPLeaseState()
        lease = state.acquire(**acquire_kwargs())
        identity = server_mod.parse_identity(
            lease["session_id"], lease["instance_id"],
            lease["lease_epoch"], lease["lease_token"],
        )
        renewed = state.renew(identity, ttl_ms=60_000)
        self.assertEqual(renewed["session_id"], lease["session_id"])
        self.assertEqual(renewed["lease_epoch"], lease["lease_epoch"])
        bad = server_mod.LeaseIdentity(
            lease["session_id"], lease["instance_id"], lease["lease_epoch"], "0" * 64
        )
        with self.assertRaises(server_mod.LeaseProtocolError) as ctx:
            state.renew(bad, ttl_ms=60_000)
        self.assertEqual(ctx.exception.code, "wrong_token")

    def test_expired_lease_cannot_be_renewed_and_frees_ownership(self):
        now = [1_000_000_000]
        state = server_mod.LSPLeaseState(
            clock_ns=lambda: now[0], wall_ms=lambda: 1_700_000_000_000, min_ttl_ms=100
        )
        lease = state.acquire(**acquire_kwargs(ttl_ms=100))
        identity = server_mod.parse_identity(
            lease["session_id"], lease["instance_id"],
            lease["lease_epoch"], lease["lease_token"],
        )
        now[0] += 200 * 1_000_000
        with self.assertRaises(server_mod.LeaseProtocolError) as ctx:
            state.renew(identity, ttl_ms=30_000)
        self.assertEqual(ctx.exception.code, "lease_expired")
        # Expired idle lease no longer blocks a replacement owner.
        second = state.acquire(**acquire_kwargs())
        self.assertEqual(second["lease_epoch"], 2)

    def test_restart_invalidates_old_leases(self):
        first = server_mod.LSPLeaseState()
        lease = first.acquire(**acquire_kwargs())
        second = server_mod.LSPLeaseState()
        self.assertNotEqual(second.instance_id, first.instance_id)
        identity = server_mod.parse_identity(
            lease["session_id"], lease["instance_id"],
            lease["lease_epoch"], lease["lease_token"],
        )
        with self.assertRaises(server_mod.LeaseProtocolError) as ctx:
            second.renew(identity, ttl_ms=30_000)
        self.assertEqual(ctx.exception.code, "wrong_instance")

    def test_release_while_in_flight_fences_commit_and_blocks_replace(self):
        state = server_mod.LSPLeaseState()
        lease = state.acquire(**acquire_kwargs())
        identity = server_mod.parse_identity(
            lease["session_id"], lease["instance_id"],
            lease["lease_epoch"], lease["lease_token"],
        )
        state.begin_session(identity)
        result = state.release(identity)
        self.assertTrue(result["released"])
        self.assertTrue(result["release_pending_in_flight"])
        with self.assertRaises(server_mod.LeaseProtocolError):
            state.acquire(**acquire_kwargs(holder="replacement"))
        self.assertFalse(state.commit_session(identity))
        # Once the stale session leaves the gate, ownership is grantable again.
        replacement = state.acquire(**acquire_kwargs(holder="replacement"))
        self.assertEqual(replacement["lease_epoch"], 2)

    def test_concurrent_acquire_grants_exactly_one_owner(self):
        state = server_mod.LSPLeaseState()
        winners: list[dict] = []
        failures: list[BaseException] = []
        barrier = threading.Barrier(16)

        def attempt(i: int) -> None:
            try:
                barrier.wait(timeout=10)
                winners.append(state.acquire(**acquire_kwargs(holder=f"holder-{i}")))
            except BaseException as exc:  # noqa: BLE001 - collected for assertions
                failures.append(exc)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(failures), 15)
        self.assertTrue(all(isinstance(exc, server_mod.LeaseProtocolError) for exc in failures))

    def test_status_never_exposes_bearer_token(self):
        state = server_mod.LSPLeaseState()
        lease = state.acquire(**acquire_kwargs())
        status = state.status()
        self.assertEqual(status["lease_session_id"], lease["session_id"])
        self.assertEqual(status["worker_generation"], 3)
        self.assertNotIn("lease_token", json.dumps(status))


class LiveServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.state = server_mod.LSPLeaseState()
        cls.server = server_mod.LSPLeaseServer(("127.0.0.1", 0), cls.state)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.05})
        cls.thread.daemon = True
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base_url = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=10)

    def setUp(self) -> None:
        # Each live test starts from idle: release any lease left by a prior test.
        status = self._get("/lsp/lease/status")
        if status["lease_active"]:
            raise AssertionError("live lease server is not idle between tests")

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base_url + path, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def _acquire(self, **overrides) -> dict:
        status, body = self._post("/lsp/lease/acquire", acquire_kwargs(**overrides))
        self.assertEqual(status, 200, body)
        return body

    def _release(self, lease: dict) -> None:
        status, body = self._post("/lsp/lease/release", {
            "session_id": lease["session_id"],
            "instance_id": lease["instance_id"],
            "lease_epoch": lease["lease_epoch"],
            "lease_token": lease["lease_token"],
        })
        self.assertEqual(status, 200, body)
        self.assertTrue(body["released"])

    def test_client_roundtrip_against_live_target(self):
        client = client_mod.LSPLeaseClient(
            self.base_url,
            goal_id="goal-1", work_id="work-1", worker_id="node-1",
            worker_generation=7, descriptor_digest=DIGEST_A, image_digest=DIGEST_B,
            holder="live-holder", ttl_ms=30_000,
        )
        lease = client.acquire()
        for field in ("session_id", "instance_id", "lease_token"):
            self.assertTrue(lease[field])
        renewed = client.renew(lease)
        self.assertEqual(renewed["session_id"], lease["session_id"])
        client.release(lease)

    def test_live_double_acquire_is_rejected(self):
        lease = self._acquire()
        try:
            status, body = self._post("/lsp/lease/acquire", acquire_kwargs(holder="intruder"))
            self.assertEqual(status, 409)
            self.assertEqual(body["error"], "lease_held")
        finally:
            self._release(lease)

    def test_live_forged_token_is_rejected(self):
        lease = self._acquire()
        try:
            status, body = self._post("/lsp/lease/renew", {
                "session_id": lease["session_id"],
                "instance_id": lease["instance_id"],
                "lease_epoch": lease["lease_epoch"],
                "lease_token": "f" * 64,
                "ttl_ms": 30_000,
            })
            self.assertEqual(status, 409)
            self.assertEqual(body["error"], "wrong_token")
        finally:
            self._release(lease)

    def test_live_unknown_fields_fail_closed(self):
        payload = acquire_kwargs()
        payload["unexpected"] = True
        status, body = self._post("/lsp/lease/acquire", payload)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "unknown_fields")

    def test_live_health_and_status_shape(self):
        health = self._get("/health")
        self.assertTrue(health["ok"])
        self.assertEqual(health["service"], "blitter-lsp-lease")
        status = self._get("/lsp/lease/status")
        self.assertTrue(status["ok"])
        self.assertFalse(status["lease_active"])
        self.assertNotIn("lease_token", json.dumps(status))

    def test_live_concurrent_acquire_race(self):
        outcomes: list[tuple[int, dict]] = []
        barrier = threading.Barrier(8)

        def attempt(i: int) -> None:
            barrier.wait(timeout=10)
            outcomes.append(self._post("/lsp/lease/acquire", acquire_kwargs(holder=f"racer-{i}")))

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        winners = [body for code, body in outcomes if code == 200]
        losers = [(code, body) for code, body in outcomes if code != 200]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 7)
        self.assertTrue(all(code == 409 for code, _ in losers))
        self._release(winners[0])

    def test_live_fenced_commit_after_release(self):
        lease = self._acquire(ttl_ms=30_000)
        identity = {
            "session_id": lease["session_id"],
            "instance_id": lease["instance_id"],
            "lease_epoch": lease["lease_epoch"],
            "lease_token": lease["lease_token"],
        }
        status, _ = self._post("/lsp/session/begin", identity)
        self.assertEqual(status, 200)
        # Replacement owner is blocked while the session is in flight.
        status, body = self._post("/lsp/lease/acquire", acquire_kwargs(holder="replacement"))
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "compute_in_flight")
        status, _ = self._post("/lsp/lease/release", identity)
        self.assertEqual(status, 200)
        status, body = self._post("/lsp/session/commit", identity)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "fenced_at_commit")
        # Gate is grantable again once stale work leaves.
        self._release(self._acquire(holder="replacement"))


if __name__ == "__main__":
    unittest.main()
