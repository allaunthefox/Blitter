#!/usr/bin/env python3
from __future__ import annotations

import http.client
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lease_gate import (
    HEADER_EPOCH,
    HEADER_INSTANCE,
    HEADER_TOKEN,
    LeaseGateServer,
    LeaseIdentity,
    LeaseProtocolError,
    LeaseState,
    parse_upstream,
    parse_loopback_upstream,
)


class FakeClock:
    def __init__(self):
        self.now = 1_000_000_000

    def __call__(self):
        return self.now

    def advance_ms(self, milliseconds):
        self.now += milliseconds * 1_000_000


def identity_from(reply):
    return LeaseIdentity(
        reply["instance_id"], reply["lease_epoch"], reply["lease_token"]
    )


class LeaseStateTests(unittest.TestCase):
    def make_state(self):
        clock = FakeClock()
        tokens = iter(["a" * 64, "b" * 64, "c" * 64, "d" * 64])
        state = LeaseState(
            clock_ns=clock,
            token_factory=lambda: next(tokens),
            instance_factory=lambda: "instance-0000000000000001",
            min_ttl_ms=10,
            max_ttl_ms=10000,
        )
        return state, clock

    def test_acquire_conflict_renew_release_and_epoch_fence(self):
        state, clock = self.make_state()
        first = state.acquire(holder="worker-a", ttl_ms=100, expected_ms=50)
        first_id = identity_from(first)
        self.assertNotIn("lease_token", state.status())
        self.assertTrue(state.status()["lease_active"])

        with self.assertRaisesRegex(LeaseProtocolError, "already held"):
            state.acquire(holder="worker-b", ttl_ms=100)

        clock.advance_ms(50)
        renewed = state.renew(first_id, ttl_ms=200)
        self.assertGreaterEqual(renewed["remaining_ms"], 199)

        released = state.release(first_id)
        self.assertFalse(released["release_pending_in_flight"])
        second = state.acquire(holder="worker-b", ttl_ms=100)
        self.assertEqual(second["lease_epoch"], first["lease_epoch"] + 1)
        with self.assertRaisesRegex(LeaseProtocolError, "epoch"):
            state.begin_compute(first_id)

    def test_expired_lease_cannot_be_renewed_or_used(self):
        state, clock = self.make_state()
        lease = state.acquire(holder="worker", ttl_ms=10)
        ident = identity_from(lease)
        clock.advance_ms(11)
        with self.assertRaisesRegex(LeaseProtocolError, "expired"):
            state.renew(ident, ttl_ms=100)
        with self.assertRaisesRegex(LeaseProtocolError, "no lease|expired"):
            state.begin_compute(ident)

    def test_expiry_during_compute_fences_commit_and_blocks_replacement_until_finish(self):
        state, clock = self.make_state()
        lease = state.acquire(holder="old", ttl_ms=10)
        ident = identity_from(lease)
        state.begin_compute(ident)
        clock.advance_ms(11)

        with self.assertRaisesRegex(LeaseProtocolError, "prior work remains in flight"):
            state.acquire(holder="new", ttl_ms=100)

        self.assertFalse(state.finish_compute(ident, result_ready=True))
        new_lease = state.acquire(holder="new", ttl_ms=100)
        self.assertGreater(new_lease["lease_epoch"], lease["lease_epoch"])

    def test_release_during_compute_revokes_commit_and_blocks_replacement(self):
        state, _ = self.make_state()
        lease = state.acquire(holder="old", ttl_ms=1000)
        ident = identity_from(lease)
        state.begin_compute(ident)
        released = state.release(ident)
        self.assertTrue(released["release_pending_in_flight"])
        with self.assertRaisesRegex(LeaseProtocolError, "prior work remains in flight"):
            state.acquire(holder="new", ttl_ms=100)
        self.assertFalse(state.finish_compute(ident, result_ready=True))
        self.assertEqual(state.acquire(holder="new", ttl_ms=100)["holder"], "new")

    def test_wrong_instance_token_and_epoch_fail_closed(self):
        state, _ = self.make_state()
        lease = state.acquire(holder="worker", ttl_ms=100)
        ident = identity_from(lease)
        bad = [
            LeaseIdentity("other-instance-00000000", ident.epoch, ident.token),
            LeaseIdentity(ident.instance_id, ident.epoch + 1, ident.token),
            LeaseIdentity(ident.instance_id, ident.epoch, "f" * 64),
        ]
        codes = ["wrong_instance", "stale_epoch", "wrong_token"]
        for candidate, code in zip(bad, codes):
            with self.subTest(code=code):
                with self.assertRaises(LeaseProtocolError) as ctx:
                    state.begin_compute(candidate)
                self.assertEqual(ctx.exception.code, code)

    def test_restart_instance_invalidates_old_identity_even_if_epoch_repeats(self):
        clock = FakeClock()
        a = LeaseState(
            clock_ns=clock,
            token_factory=lambda: "a" * 64,
            instance_factory=lambda: "instance-A-000000000000",
            min_ttl_ms=10,
            max_ttl_ms=1000,
        )
        old = identity_from(a.acquire(holder="worker", ttl_ms=100))
        b = LeaseState(
            clock_ns=clock,
            token_factory=lambda: "a" * 64,
            instance_factory=lambda: "instance-B-000000000000",
            min_ttl_ms=10,
            max_ttl_ms=1000,
        )
        b.acquire(holder="worker", ttl_ms=100)
        with self.assertRaisesRegex(LeaseProtocolError, "instance"):
            b.begin_compute(old)


class ControlledUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        if self.path != "/compute":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n)
        self.server.seen_bodies.append(body)
        self.server.started.set()
        self.server.release.wait(timeout=5)
        data = b'{"ok":true,"terms":[[0,0,1]]}\n'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ControlledUpstream(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), ControlledUpstreamHandler)
        self.started = threading.Event()
        self.release = threading.Event()
        self.seen_bodies = []
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def origin(self):
        return ("127.0.0.1", self.server_address[1])

    def close(self):
        self.release.set()
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)


class GateHarness:
    def __init__(self, upstream, *, min_ttl_ms=10, max_ttl_ms=10000):
        self.state = LeaseState(min_ttl_ms=min_ttl_ms, max_ttl_ms=max_ttl_ms)
        self.server = LeaseGateServer(("127.0.0.1", 0), self.state, upstream)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, payload=None, *, headers=None, timeout=5):
        body = None if payload is None else json.dumps(payload, separators=(",", ":"))
        request_headers = dict(headers or {})
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=request_headers)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, json.loads(data.decode("utf-8"))
        finally:
            conn.close()

    def acquire(self, *, ttl_ms=1000, holder="worker"):
        status, reply = self.request(
            "POST", "/lease/acquire", {"holder": holder, "ttl_ms": ttl_ms}
        )
        assert status == 200, reply
        return reply

    @staticmethod
    def headers(lease):
        return {
            HEADER_INSTANCE: lease["instance_id"],
            HEADER_EPOCH: str(lease["lease_epoch"]),
            HEADER_TOKEN: lease["lease_token"],
        }


class LeaseGateLoopbackTests(unittest.TestCase):
    def setUp(self):
        self.upstream = ControlledUpstream()
        self.gate = GateHarness(self.upstream.origin)

    def tearDown(self):
        self.gate.close()
        self.upstream.close()

    def test_compute_requires_lease_and_preserves_body(self):
        status, body = self.gate.request("POST", "/compute", {"a": [], "b": []})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

        lease = self.gate.acquire()
        self.upstream.release.set()
        payload = {"a": [[0, 0, 1]], "b": [[0, 0, 1]]}
        status, body = self.gate.request(
            "POST", "/compute", payload, headers=self.gate.headers(lease)
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(
            json.loads(self.upstream.seen_bodies[-1].decode("utf-8")), payload
        )

    def test_expiry_during_upstream_work_discards_result_and_blocks_new_lease(self):
        lease = self.gate.acquire(ttl_ms=30, holder="old")
        result = {}

        def run_compute():
            result["response"] = self.gate.request(
                "POST",
                "/compute",
                {"a": [], "b": []},
                headers=self.gate.headers(lease),
                timeout=5,
            )

        thread = threading.Thread(target=run_compute)
        thread.start()
        self.assertTrue(self.upstream.started.wait(timeout=2))
        time.sleep(0.05)

        status, blocked = self.gate.request(
            "POST", "/lease/acquire", {"holder": "new", "ttl_ms": 1000}
        )
        self.assertEqual(status, 409)
        self.assertEqual(blocked["error"], "compute_in_flight")

        self.upstream.release.set()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        status, fenced = result["response"]
        self.assertEqual(status, 409)
        self.assertEqual(fenced["error"], "fenced_at_commit")

        status, fresh = self.gate.request(
            "POST", "/lease/acquire", {"holder": "new", "ttl_ms": 1000}
        )
        self.assertEqual(status, 200)
        self.assertGreater(fresh["lease_epoch"], lease["lease_epoch"])

    def test_release_during_upstream_work_discards_result(self):
        lease = self.gate.acquire(ttl_ms=1000)
        result = {}

        def run_compute():
            result["response"] = self.gate.request(
                "POST", "/compute", {"a": [], "b": []}, headers=self.gate.headers(lease)
            )

        thread = threading.Thread(target=run_compute)
        thread.start()
        self.assertTrue(self.upstream.started.wait(timeout=2))

        status, released = self.gate.request(
            "POST",
            "/lease/release",
            {
                "instance_id": lease["instance_id"],
                "lease_epoch": lease["lease_epoch"],
                "lease_token": lease["lease_token"],
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(released["release_pending_in_flight"])

        status, blocked = self.gate.request(
            "POST", "/lease/acquire", {"holder": "new", "ttl_ms": 1000}
        )
        self.assertEqual(status, 409)
        self.assertEqual(blocked["error"], "compute_in_flight")

        self.upstream.release.set()
        thread.join(timeout=3)
        self.assertEqual(result["response"][0], 409)
        self.assertEqual(result["response"][1]["error"], "fenced_at_commit")

    def test_status_never_contains_bearer_token(self):
        lease = self.gate.acquire()
        status, body = self.gate.request("GET", "/status")
        self.assertEqual(status, 200)
        encoded = json.dumps(body)
        self.assertNotIn(lease["lease_token"], encoded)
        self.assertNotIn("lease_token", body)


class UpstreamBoundaryTests(unittest.TestCase):
    def test_only_loopback_http_origin_is_admitted(self):
        self.assertEqual(parse_loopback_upstream("http://127.0.0.1:8791"), ("127.0.0.1", 8791))
        self.assertEqual(parse_loopback_upstream("http://localhost:8791"), ("localhost", 8791))
        self.assertEqual(parse_loopback_upstream("http://[::1]:8791"), ("::1", 8791))
        for bad in (
            "https://127.0.0.1:8791",
            "http://10.0.0.1:8791",
            "http://example.com:8791",
            "http://user@127.0.0.1:8791",
            "http://127.0.0.1:8791/compute",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_loopback_upstream(bad)

    def test_split_container_upstream_requires_exact_allowlist(self):
        self.assertEqual(
            parse_upstream(
                "http://blitter-worker:8791",
                allowed_hosts={"blitter-worker"},
            ),
            ("blitter-worker", 8791),
        )
        with self.assertRaises(ValueError):
            parse_upstream("http://blitter-worker:8791")


if __name__ == "__main__":
    unittest.main()
