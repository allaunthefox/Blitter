#!/usr/bin/env python3
from __future__ import annotations

import io
import shlex
import subprocess
import sys
import threading
import unittest

try:
    from .lsp_transport import LSPTransport, LeaseHooks, TransportCancelled, TransportConfig
except ImportError:
    from lsp_transport import LSPTransport, LeaseHooks, TransportCancelled, TransportConfig


FAKE_CODE = """import sys, time
mode = sys.argv[1]
sys.stderr.buffer.write(b'fake-stderr\\n')
sys.stderr.flush()
if mode == 'echo':
    sys.stdout.buffer.write(sys.stdin.buffer.read())
    sys.stdout.buffer.flush()
elif mode == 'exit':
    sys.stdout.buffer.write(b'bye')
    sys.stdout.buffer.flush()
    sys.exit(7)
else:
    time.sleep(10)
"""


class LSPTransportTests(unittest.TestCase):
    @staticmethod
    def local_popen(mode: str):
        def factory(_argv, **kwargs):
            return subprocess.Popen([sys.executable, "-c", FAKE_CODE, mode], **kwargs)
        return factory

    def run_child(self, mode: str, **kwargs):
        config = kwargs.pop("config", TransportConfig(startup_timeout=1, idle_timeout=1, total_timeout=3))
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(FAKE_CODE)} {mode}"
        input_stream = kwargs.pop("input_stream", io.BytesIO())
        output_stream = kwargs.pop("output_stream", io.BytesIO())
        error_stream = kwargs.pop("error_stream", io.BytesIO())
        return LSPTransport(
            "fake-host", command, config=config, popen_factory=self.local_popen(mode), **kwargs
        ).run(
            input_stream=input_stream, output_stream=output_stream, error_stream=error_stream,
        )

    def test_binary_bidirectional_and_separate_stderr(self):
        source, output, errors = io.BytesIO(b"\x00\xffhello\n"), io.BytesIO(), io.BytesIO()
        transport = LSPTransport("host", "fake-server", config=TransportConfig(total_timeout=3), popen_factory=self.local_popen("echo"))
        self.assertEqual(transport.run(input_stream=source, output_stream=output, error_stream=errors), 0)
        self.assertEqual(output.getvalue(), b"\x00\xffhello\n")
        self.assertEqual(errors.getvalue(), b"fake-stderr\n")

    def test_ssh_disables_pty_and_process_group_is_requested(self):
        transport = LSPTransport("user@host", "server", config=TransportConfig())
        self.assertIn("-T", transport.ssh_argv())

        class Fake:
            pid = 123
            stdin = io.BytesIO()
            stdout = io.BytesIO()
            stderr = io.BytesIO()
            def poll(self): return 0

        seen = {}
        def popen(argv, **kwargs):
            seen.update(kwargs)
            return Fake()
        LSPTransport("host", "server", popen_factory=popen).run()
        self.assertTrue(seen["start_new_session"])

    def test_hooks_are_opt_in_and_released(self):
        calls, output = [], io.BytesIO()
        hooks = LeaseHooks(lambda: calls.append("acquire") or object(), lambda lease: calls.append("release"))
        self.assertEqual(self.run_child("exit", lease_hooks=hooks, output_stream=output), 7)
        self.assertEqual(calls, ["acquire", "release"])

    def test_renewal_failure_cancels_session_and_releases(self):
        calls = []
        hooks = LeaseHooks(
            lambda: calls.append("acquire") or {},
            lambda lease: calls.append("release"),
            lambda lease: (_ for _ in ()).throw(RuntimeError("renew failed")),
        )
        config = TransportConfig(
            startup_timeout=1, idle_timeout=None, total_timeout=2,
            lease_renew_interval=.01,
        )
        with self.assertRaisesRegex(Exception, "cancelled"):
            self.run_child("sleep", config=config, lease_hooks=hooks)
        self.assertEqual(calls, ["acquire", "release"])

    def test_no_hooks_means_no_lease_claim(self):
        self.assertEqual(self.run_child("exit"), 7)

    def test_idle_timeout_after_initial_activity(self):
        with self.assertRaisesRegex(Exception, "idle timeout"):
            self.run_child(
                "sleep",
                input_stream=io.BytesIO(b"request"),
                config=TransportConfig(startup_timeout=1, idle_timeout=.05, total_timeout=2),
            )

    def test_startup_timeout(self):
        with self.assertRaisesRegex(Exception, "startup timeout"):
            self.run_child("sleep", config=TransportConfig(startup_timeout=.05, idle_timeout=None, total_timeout=2))

    def test_cancellation_kills_session(self):
        cancelled = threading.Event()
        transport = LSPTransport("host", "fake-server", config=TransportConfig(total_timeout=3), popen_factory=self.local_popen("sleep"), cancel_event=cancelled)
        timer = threading.Timer(.05, cancelled.set)
        timer.start()
        try:
            with self.assertRaises(TransportCancelled):
                transport.run(input_stream=io.BytesIO(), output_stream=io.BytesIO(), error_stream=io.BytesIO())
        finally:
            timer.cancel()


if __name__ == "__main__":
    unittest.main()
