#!/usr/bin/env python3
"""Static local gates for Harbor deployment's required trust inputs."""
from __future__ import annotations

import unittest
from pathlib import Path


DEPLOY = Path(__file__).with_name("harbor_deploy.sh")


class HarborDeployContractTests(unittest.TestCase):
    def test_deploy_consumes_active_bootstrap_plan_and_non_root_runner(self):
        script = DEPLOY.read_text(encoding="utf-8")
        self.assertIn("render_bootstrap_plan", script)
        self.assertIn('"$RUNNER_USER@$ADDR"', script)
        self.assertNotIn('"root@$ADDR"', script)
        self.assertIn('runtime={node[\'runtime\']!r} is unsupported', script)

    def test_deploy_requires_verified_provenance_runtime_and_access_controls(self):
        script = DEPLOY.read_text(encoding="utf-8")
        for name in (
            "BLITTER_SECURITY_PLUGIN",
            "BLITTER_TRUSTED_STAMP_KEY",
            "BLITTER_RUNTIME_IMAGE_TRUST_POLICY",
            "BLITTER_RUNTIME_IMAGE_TRUST_POLICY_DIGEST",
            "BLITTER_PUBLISHED_GATE_ACCESS_POLICY",
            "BLITTER_PUBLISHED_GATE_ACCESS_POLICY_DIGEST",
            "BLITTER_PUBLISH_ADDR",
        ):
            with self.subTest(name=name):
                self.assertIn(name, script)
        self.assertIn("provenance must declare the required secure stamp", script)
        self.assertNotIn("BLITTER_REQUIRE_DEPLOY_STAMP", script)


if __name__ == "__main__":
    unittest.main()
