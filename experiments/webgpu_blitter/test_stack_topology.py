#!/usr/bin/env python3
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent


class FencedImageTopologyTests(unittest.TestCase):
    def setUp(self):
        self.dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.entrypoint = (ROOT / "blitter-stack-entrypoint.sh").read_text(encoding="utf-8")

    def test_image_entrypoint_is_stack_supervisor_not_raw_daemon(self):
        self.assertIn('ENTRYPOINT ["/usr/local/bin/blitter-stack-entrypoint"]', self.dockerfile)
        self.assertNotIn('ENTRYPOINT ["/usr/local/bin/blitter-daemon"]', self.dockerfile)
        self.assertIn('COPY lease_gate.py /usr/local/libexec/blitter-lease-gate.py', self.dockerfile)
        self.assertIn('COPY blitter-stack-entrypoint.sh /usr/local/bin/blitter-stack-entrypoint', self.dockerfile)

    def test_inner_daemon_default_is_loopback_only_and_separate_port(self):
        self.assertIn('BLITTER_INNER_BIND:-127.0.0.1:8791', self.entrypoint)
        self.assertIn('export BLITTER_BIND="$INNER_BIND"', self.entrypoint)
        self.assertIn('BLITTER_INNER_BIND must remain IPv4 loopback', self.entrypoint)
        self.assertIn('--upstream "http://$INNER_BIND"', self.entrypoint)

    def test_external_container_service_is_lease_gate(self):
        self.assertIn('BLITTER_GATE_LISTEN:-0.0.0.0:8790', self.entrypoint)
        self.assertIn('/usr/local/libexec/blitter-lease-gate.py', self.entrypoint)
        self.assertIn('EXPOSE 8790', self.dockerfile)
        self.assertNotIn('EXPOSE 8791', self.dockerfile)

    def test_supervisor_has_no_direct_daemon_fallback(self):
        self.assertIn('wait -n "$daemon_pid" "$gate_pid"', self.entrypoint)
        self.assertIn('if [ "$rc" -eq 0 ]; then', self.entrypoint)
        self.assertIn('rc=1', self.entrypoint)
        self.assertIn('kill "$gate_pid" "$daemon_pid"', self.entrypoint)
        self.assertNotIn('exec /usr/local/bin/blitter-daemon', self.entrypoint)


if __name__ == "__main__":
    unittest.main()
