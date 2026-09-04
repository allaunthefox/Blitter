#!/usr/bin/env python3
"""Local-only tests for runtime-specific bootstrap plan rendering."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

try:
    from . import node_bootstrap_plan as bootstrap_plan
except ImportError:
    import node_bootstrap_plan as bootstrap_plan


HERE = Path(__file__).resolve().parent


class NodeBootstrapPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = json.loads((HERE / "nodes.json").read_text(encoding="utf-8"))

    def test_active_source_renders_only_supported_eligible_runtimes(self):
        plan = bootstrap_plan.render_bootstrap_plan(self.source, environ={})
        rendered = {item["name"]: item for item in plan["plans"]}
        self.assertIn("cupfox", rendered)
        self.assertIn("nixos-laptop", rendered)
        self.assertEqual(rendered["cupfox"]["runner_user"], "forgejo-runner")
        self.assertEqual(rendered["nixos-laptop"]["runner_user"], "forgejo-runner")
        self.assertTrue(plan["local_only"])
        rejected = {item["name"]: item for item in plan["blockers"]}
        self.assertIn("WebGPU port is missing or invalid", rejected["qfox-1"]["blockers"])
        self.assertIn("runtime=bare has no bootstrap plan", rejected["nasfox"]["blockers"])

    def test_rejects_auto_and_bare_with_explicit_remediation(self):
        plan = bootstrap_plan.render_bootstrap_plan(self.source, environ={})
        rejected = {item["name"]: item for item in plan["blockers"]}
        self.assertIn("runtime=auto has no bootstrap plan", rejected["neon-rs1000"]["blockers"])
        self.assertIn("Declare docker, nix-podman, or podman-nvidia-cdi", rejected["neon-rs1000"]["remediation"])
        self.assertIn("runtime=bare has no bootstrap plan", rejected["nasfox"]["blockers"])
        self.assertIn("bare host execution is not supported", rejected["nasfox"]["remediation"])

    def test_gpu_plan_requires_gpu_role_and_cdi_conditions(self):
        source = {"version": 1, "nodes": [{
            "name": "gpu", "address": "100.64.0.1", "runner_user": "runner",
            "role": "gpu", "runtime": "podman-nvidia-cdi", "arch": "x86_64", "webgpu_port": 8790,
        }]}
        plan = bootstrap_plan.render_bootstrap_plan(source, environ={})
        self.assertEqual(plan["blockers"], [])
        gpu = plan["plans"][0]
        self.assertIn("nvidia-ctk cdi list", gpu["commands"])
        self.assertIn("reviewed CDI device selector", gpu["requirements"]["devices"])

    def test_gpu_role_without_gpu_runtime_is_rejected(self):
        source = {"version": 1, "nodes": [{
            "name": "incorrect", "address": "100.64.0.2", "runner_user": "runner",
            "role": "gpu", "runtime": "docker", "arch": "x86_64", "webgpu_port": 8790,
        }]}
        plan = bootstrap_plan.render_bootstrap_plan(source, environ={})
        self.assertEqual(plan["plans"], [])
        self.assertIn("runtime=docker has no declared GPU device contract", plan["blockers"][0]["blockers"])


if __name__ == "__main__":
    unittest.main()
