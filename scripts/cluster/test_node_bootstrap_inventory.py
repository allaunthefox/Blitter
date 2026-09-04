#!/usr/bin/env python3
"""Local contract tests; these never contact configured nodes."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

try:
    from . import node_bootstrap_inventory as bootstrap
except ImportError:
    import node_bootstrap_inventory as bootstrap


HERE = Path(__file__).resolve().parent


class NodeBootstrapInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = json.loads((HERE / "nodes.json").read_text(encoding="utf-8"))

    def test_current_active_inventory_derives_expected_contract(self):
        inventory = bootstrap.bootstrap_inventory(self.source, environ={})
        self.assertEqual(inventory["schema"], "mathpunch.blitter-node-bootstrap-inventory.v1")
        nodes = {node["name"]: node for node in inventory["nodes"]}
        self.assertEqual(nodes["cupfox"]["required_runner_capabilities"], ["ssh", "docker", "docker-content-trust"])
        self.assertFalse(nodes["qfox-1"]["deployment_eligible"])
        self.assertIn("WebGPU port is missing or invalid", nodes["qfox-1"]["blockers"])
        self.assertFalse(nodes["neon-rs1000"]["deployment_eligible"])
        self.assertIn("runtime=auto is not an accepted bootstrap runtime", nodes["neon-rs1000"]["blockers"])
        self.assertIn("runner user environment expansion is unresolved", nodes["neon-rs1000"]["blockers"])

    def test_resolved_environment_user_remains_blocked_by_auto_runtime(self):
        source = {"version": 1, "nodes": [{
            "name": "neon", "address": "100.64.0.1", "runner_user": "${RUNNER}",
            "role": "overflow", "runtime": "auto", "arch": "x86_64", "webgpu_port": 8790,
        }]}
        inventory = bootstrap.bootstrap_inventory(source, environ={"RUNNER": "operator"})
        self.assertEqual(inventory["nodes"][0]["ssh"]["resolved_user"], "operator")
        self.assertEqual(inventory["nodes"][0]["blockers"], ["runtime=auto is not an accepted bootstrap runtime"])

    def test_checked_in_example_matches_current_source_without_secrets(self):
        example = json.loads((HERE / "node_bootstrap_inventory.example.json").read_text(encoding="utf-8"))
        expected = bootstrap.bootstrap_inventory(self.source, environ={})
        self.assertEqual(example, expected)
        self.assertEqual(bootstrap.validation_errors(example), [])

    def test_missing_required_rejection_is_invalid_contract(self):
        source = {"version": 1, "nodes": [{
            "name": "node", "address": "100.64.0.1", "runner_user": "${RUNNER}",
            "role": "cpu", "runtime": "docker", "arch": "x86_64", "webgpu_port": 8790,
        }]}
        inventory = bootstrap.bootstrap_inventory(source, environ={})
        inventory["nodes"][0]["blockers"] = []
        self.assertTrue(any("unresolved runner user must be rejected" in error
                            for error in bootstrap.validation_errors(inventory)))

    def test_root_is_not_a_runner_identity(self):
        source = {"version": 1, "nodes": [{
            "name": "node", "address": "100.64.0.1", "runner_user": "root",
            "role": "cpu", "runtime": "docker", "arch": "x86_64", "webgpu_port": 8790,
        }]}
        inventory = bootstrap.bootstrap_inventory(source, environ={})
        self.assertFalse(inventory["nodes"][0]["deployment_eligible"])
        self.assertIn("runner_user must name a dedicated non-root account", inventory["nodes"][0]["blockers"])


if __name__ == "__main__":
    unittest.main()
