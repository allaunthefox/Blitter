#!/usr/bin/env python3
"""Black-box loopback contract for the real blitter-daemon binary.

The test intentionally knows only HTTP/JSON. It does not import Rust code or call
GPU helpers. CI builds the tracked daemon, runs it against a software Vulkan
adapter (Mesa llvmpipe/lavapipe where available), and supplies BLITTER_DAEMON_BIN.
"""

from __future__ import annotations

import concurrent.futures
import http.client
import json
import os
import socket
import subprocess
import time
import unittest
from pathlib import Path


class DaemonProcess:
    def __init__(self, binary: Path):
        self.binary = binary
        self.port = self._reserve_port()
        self.proc = None

    @staticmethod
    def _reserve_port():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def start(self, *, extra_env=None):
        env = os.environ.copy()
        env["BLITTER_BIND"] = f"127.0.0.1:{self.port}"
        if extra_env:
            env.update(extra_env)
        self.proc = subprocess.Popen(
            [str(self.binary)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        deadline = time.monotonic() + 20
        last_error = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                _, err = self.proc.communicate(timeout=1)
                raise RuntimeError(f"daemon exited during startup rc={self.proc.returncode}: {err[-2000:]}")
            try:
                status, body = self.request("GET", "/health")
                if status == 200 and body.get("ok") is True:
                    return
            except Exception as exc:  # socket not listening yet
                last_error = exc
            time.sleep(0.05)
        raise RuntimeError(f"daemon did not become healthy: {last_error}")

    def stop(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc = None

    def request(self, method, path, payload=None, *, raw_body=None, timeout=10):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            if raw_body is not None:
                body = raw_body
            elif payload is None:
                body = None
            else:
                body = json.dumps(payload, separators=(",", ":"))
            headers = {}
            if body is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            parsed = json.loads(data.decode("utf-8"))
            return resp.status, parsed
        finally:
            conn.close()


class BlitterDaemonLoopbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw = os.environ.get("BLITTER_DAEMON_BIN")
        if not raw:
            raise unittest.SkipTest("BLITTER_DAEMON_BIN not configured")
        binary = Path(raw)
        if not binary.is_absolute() or not binary.is_file():
            raise RuntimeError("BLITTER_DAEMON_BIN must be an existing absolute path")
        cls.daemon = DaemonProcess(binary)
        cls.daemon.start()

    @classmethod
    def tearDownClass(cls):
        cls.daemon.stop()

    def test_health_and_idle_status_contract(self):
        status, health = self.daemon.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertIs(health.get("ok"), True)
        self.assertIsInstance(health.get("adapter"), str)

        status, state = self.daemon.request("GET", "/status")
        self.assertEqual(status, 200)
        self.assertIs(state.get("ok"), True)
        self.assertIs(state.get("busy"), False)
        self.assertIs(state.get("idle"), True)
        self.assertEqual(state.get("active_compute"), 0)
        self.assertEqual(state.get("queued_compute"), 0)
        self.assertEqual(state.get("max_concurrent_compute"), 1)

    def test_malformed_request_fails_and_is_counted(self):
        _, before = self.daemon.request("GET", "/status")
        status, result = self.daemon.request("POST", "/compute", raw_body='{"a":[]}')
        self.assertEqual(status, 400)
        self.assertIs(result.get("ok"), False)
        _, after = self.daemon.request("GET", "/status")
        self.assertEqual(after["compute_requests_total"], before["compute_requests_total"] + 1)
        self.assertEqual(after["failed_compute"], before["failed_compute"] + 1)
        self.assertEqual(after["active_compute"], 0)
        self.assertEqual(after["queued_compute"], 0)

    def test_exact_compute_matches_independent_expected_value(self):
        job = {
            "a": [[1, 0, 2], [0, 1, 3]],
            "b": [[1, 0, 5], [0, 0, 7]],
        }
        status, result = self.daemon.request("POST", "/compute", payload=job, timeout=30)
        self.assertEqual(status, 200)
        self.assertIs(result.get("ok"), True)
        self.assertEqual(
            result.get("terms"),
            [[0, 1, 21], [1, 0, 14], [1, 1, 15], [2, 0, 10]],
        )

    def test_exact_domain_overflow_returns_failure_without_stuck_occupancy(self):
        job = {"a": [[0, 0, 2147483647]], "b": [[0, 0, 2]]}
        status, result = self.daemon.request("POST", "/compute", payload=job, timeout=30)
        self.assertEqual(status, 500)
        self.assertIs(result.get("ok"), False)
        self.assertIn("exact i32 GPU domain", result.get("error", ""))
        _, state = self.daemon.request("GET", "/status")
        self.assertEqual(state["active_compute"], 0)
        self.assertEqual(state["queued_compute"], 0)
        self.assertIs(state["idle"], True)

    def test_concurrent_clients_are_serialized_and_all_results_are_exact(self):
        job = {
            "a": [[0, 0, 1], [1, 0, 1], [2, 0, 1], [3, 0, 1]],
            "b": [[0, 1, 2], [1, 1, -1], [2, 1, 3], [3, 1, 1]],
        }

        def submit(_):
            return self.daemon.request("POST", "/compute", payload=job, timeout=60)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(submit, range(16)))
        self.assertTrue(all(status == 200 and body.get("ok") is True for status, body in responses))
        first_terms = responses[0][1]["terms"]
        self.assertTrue(all(body["terms"] == first_terms for _, body in responses))
        _, state = self.daemon.request("GET", "/status")
        self.assertEqual(state["active_compute"], 0)
        self.assertEqual(state["queued_compute"], 0)
        self.assertIs(state["idle"], True)

    def test_unknown_route_is_explicit_404(self):
        status, result = self.daemon.request("GET", "/not-a-route")
        self.assertEqual(status, 404)
        self.assertIs(result.get("ok"), False)


class BlitterDaemonStartupFailureTests(unittest.TestCase):
    def test_missing_adapter_selector_fails_closed_when_binary_is_available(self):
        raw = os.environ.get("BLITTER_DAEMON_BIN")
        if not raw:
            raise unittest.SkipTest("BLITTER_DAEMON_BIN not configured")
        binary = Path(raw)
        env = os.environ.copy()
        env["BLITTER_ADAPTER"] = "this-adapter-should-not-exist-4a26d0f7"
        env["BLITTER_BIND"] = "127.0.0.1:0"
        proc = subprocess.run(
            [str(binary)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            timeout=20,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no adapter matching BLITTER_ADAPTER", proc.stderr)


if __name__ == "__main__":
    unittest.main()
