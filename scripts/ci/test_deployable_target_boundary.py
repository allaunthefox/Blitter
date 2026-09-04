#!/usr/bin/env python3
"""Keep local research executables outside the deployable Cargo surface."""
from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CARGO_TOML = ROOT / "experiments" / "webgpu_blitter" / "Cargo.toml"
HARBOR_PUSH = ROOT / "scripts" / "cluster" / "harbor_push.sh"
HARBOR_DEPLOY = ROOT / "scripts" / "cluster" / "harbor_deploy.sh"
DAEMON = ROOT / "experiments" / "webgpu_blitter" / "src" / "bin" / "blitter-daemon.rs"
LEASE_GATE = ROOT / "experiments" / "webgpu_blitter" / "lease_gate.py"


class DeployableTargetBoundaryTests(unittest.TestCase):
    def test_only_daemon_and_scalar_are_standard_targets(self):
        cargo = tomllib.loads(CARGO_TOML.read_text(encoding="utf-8"))
        self.assertFalse(cargo["package"].get("autobins", True))
        targets = {target["name"]: target for target in cargo["bin"]}
        self.assertEqual(set(targets), {
            "blitter-daemon", "blitter-ternary-scalar", "webgpu-blitter", "merge", "tailgain",
        })
        for name in ("blitter-daemon", "blitter-ternary-scalar"):
            with self.subTest(target=name):
                self.assertNotIn("required-features", targets[name])
        for name in ("webgpu-blitter", "merge", "tailgain"):
            with self.subTest(target=name):
                self.assertEqual(targets[name].get("required-features"), ["research-tools"])

    def test_harbor_publish_builds_only_the_daemon(self):
        script = HARBOR_PUSH.read_text(encoding="utf-8")
        self.assertIn("--bin blitter-daemon", script)
        for research_target in ("webgpu-blitter", "merge", "tailgain", "research-tools"):
            with self.subTest(target=research_target):
                self.assertNotIn(research_target, script)

    def test_deployable_route_tables_admit_only_canonical_envelopes(self):
        daemon = DAEMON.read_text(encoding="utf-8")
        gate = LEASE_GATE.read_text(encoding="utf-8")
        self.assertIn('("POST", "/compute-envelope")', daemon)
        self.assertNotIn('("POST", "/compute")', daemon)
        self.assertIn('self.path == "/compute-envelope"', gate)
        self.assertNotIn('self.path in {"/compute", "/compute-envelope"}', gate)

    def test_deployment_requires_and_propagates_envelope_policy(self):
        deploy = HARBOR_DEPLOY.read_text(encoding="utf-8")
        for name in (
            "BLITTER_ENVELOPE_PROFILE_ID",
            "BLITTER_ENVELOPE_PROFILE_DIGEST",
            "BLITTER_ENVELOPE_TERMINAL_CONTRACT_DIGEST",
            "BLITTER_ENVELOPE_ASSEMBLER_MODULE_DIGEST",
            "BLITTER_ENVELOPE_MAX_CPUS",
            "BLITTER_ENVELOPE_MAX_MEMORY_MIB",
            "BLITTER_ENVELOPE_MAX_WALL_MS",
        ):
            with self.subTest(name=name):
                self.assertIn(f'require_digest {name}' if "DIGEST" in name else name, deploy)
                self.assertIn(f'--env "{name}=', deploy)


if __name__ == "__main__":
    unittest.main()
