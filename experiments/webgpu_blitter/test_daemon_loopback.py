#!/usr/bin/env python3
"""Black-box loopback contract for the real blitter-daemon binary.

The test intentionally knows only HTTP/JSON. It does not import Rust code or call
GPU helpers. CI builds the tracked daemon, runs it against a software Vulkan
adapter (Mesa llvmpipe/lavapipe where available), and supplies BLITTER_DAEMON_BIN.
"""

from __future__ import annotations

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
    PROFILE_ID = "BLITTER-ISA-V1"
    PROFILE_DIGEST = "sha256:cd60e19d6c3652e08d4ed36e3f8208de7f5d2c553cb7e8f5123175c63ff2f129"
    TERMINAL_DIGEST = "sha256:5a9804978698791d50d937ba93b7f279344b2e0ea658f4e2ee7b2d5a3365fb7f"
    ASSEMBLER_DIGEST = "sha256:" + "c" * 64

    @classmethod
    def setUpClass(cls):
        raw = os.environ.get("BLITTER_DAEMON_BIN")
        if not raw:
            raise unittest.SkipTest("BLITTER_DAEMON_BIN not configured")
        binary = Path(raw)
        if not binary.is_absolute() or not binary.is_file():
            raise RuntimeError("BLITTER_DAEMON_BIN must be an existing absolute path")
        cls.daemon = DaemonProcess(binary)
        cls.daemon.start(extra_env={
            "BLITTER_ENVELOPE_PROFILE_ID": cls.PROFILE_ID,
            "BLITTER_ENVELOPE_PROFILE_DIGEST": cls.PROFILE_DIGEST,
            "BLITTER_ENVELOPE_TERMINAL_CONTRACT_DIGEST": cls.TERMINAL_DIGEST,
            "BLITTER_ENVELOPE_ASSEMBLER_MODULE_DIGEST": cls.ASSEMBLER_DIGEST,
            "BLITTER_ENVELOPE_MAX_CPUS": "2",
            "BLITTER_ENVELOPE_MAX_MEMORY_MIB": "128",
            "BLITTER_ENVELOPE_MAX_WALL_MS": "1000",
        })

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
        status, result = self.daemon.request("POST", "/compute-envelope", raw_body='{"a":[]}')
        self.assertEqual(status, 400)
        self.assertIs(result.get("ok"), False)
        _, after = self.daemon.request("GET", "/status")
        self.assertEqual(after["compute_requests_total"], before["compute_requests_total"] + 1)
        self.assertEqual(after["failed_compute"], before["failed_compute"] + 1)
        self.assertEqual(after["active_compute"], 0)
        self.assertEqual(after["queued_compute"], 0)

    def test_legacy_compute_route_is_not_exposed(self):
        status, result = self.daemon.request("POST", "/compute", raw_body="{}")
        self.assertEqual(status, 404)
        self.assertIs(result.get("ok"), False)

    def envelope(self, left_hex, right_hex):
        return {
            "schema": "mathpunch.blitter-computation-envelope.v2",
            "request_id": "loopback-1",
            "operation": "u192.add-mod",
            "profile_digest": self.PROFILE_DIGEST,
            "profile_id": self.PROFILE_ID,
            "codec": "mathpunch.u192-abi.v1",
            "inputs": [
                {"schema": "mathpunch.u192-abi.v1", "format_tag": 0,
                 "payload_hex": left_hex},
                {"schema": "mathpunch.u192-abi.v1", "format_tag": 0,
                 "payload_hex": right_hex},
            ],
            "resource_budget": {"cpus": 1, "memory_mib": 64, "wall_ms": 1000},
            "terminal_contract_digest": self.TERMINAL_DIGEST,
            "assembler": {"module_digest": self.ASSEMBLER_DIGEST,
                          "interface": "wasi-preview2-component-v1"},
            "authority": "none",
        }

    def test_compute_envelope_is_distinct_canonical_u192_addition(self):
        envelope = self.envelope("ff" * 24, "01" + "00" * 23)
        raw = (json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        status, result = self.daemon.request(
            "POST", "/compute-envelope", raw_body=raw, timeout=30
        )
        self.assertEqual(status, 200)
        self.assertEqual(result, {
            "authority": "none",
            "codec": "mathpunch.u192-canonical.v1",
            "request_digest": "sha256:" + __import__("hashlib").sha256(raw).hexdigest(),
            "schema": "mathpunch.blitter-computation-result.v1",
            "words": [0] * 6,
        })

        status, rejected = self.daemon.request(
            "POST", "/compute-envelope", raw_body=raw.rstrip(b"\n"), timeout=30
        )
        self.assertEqual(status, 400)
        self.assertIs(rejected.get("ok"), False)

        status, legacy = self.daemon.request("POST", "/compute", raw_body=raw, timeout=30)
        self.assertEqual(status, 404)
        self.assertIs(legacy.get("ok"), False)

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
