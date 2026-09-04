#!/usr/bin/env python3
"""Unit tests for the local-only mechanics runner proposal renderer."""
from __future__ import annotations

import unittest

try:
    from . import mechanics_runner_contract as contract
    from .node_bootstrap_inventory import bootstrap_inventory
except ImportError:
    import mechanics_runner_contract as contract
    from node_bootstrap_inventory import bootstrap_inventory


DIGEST = "sha256:" + "a" * 64


def inventory(runtime="docker", role="cpu"):
    return bootstrap_inventory({"version": 1, "nodes": [{
        "name": "runner", "address": "127.0.0.1", "runner_user": "runner", "role": role,
        "runtime": runtime, "arch": "x86_64", "webgpu_port": 8790,
    }]}, environ={})


def request(**changes):
    value = {"node": "runner", "job_id": "proof-42", "image": f"registry.test/mechanics@{DIGEST}",
             "argv": ["prove", "input"], "source_dir": "/reviewed/source", "result_dir": "/jobs/proof-42/results",
              "runner_user": "runner", "runner_uid": 65532,
             "resources": {"cpus": 2, "memory_mib": 512, "pids": 64, "nofile": 1024,
                           "timeout_seconds": 60, "scratch_mib": 32}}
    value.update(changes)
    return value


class MechanicsRunnerContractTests(unittest.TestCase):
    def test_docker_proposal_is_immutable_locked_down_and_cleanup_required(self):
        proposal = contract.render_mechanics_runner_proposal(inventory(), request())
        argv = proposal["run_argv"]
        self.assertTrue(proposal["proposal_only"])
        self.assertIn("--pull=never", argv)
        for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--pid=private", "--ipc=private", "--uts=private"):
            self.assertIn(flag, argv)
        self.assertIn("--security-opt=no-new-privileges:true", argv)
        self.assertIn("--user", argv)
        self.assertIn("65532:65532", argv)
        self.assertIn("type=bind,src=/reviewed/source,dst=/workspace,readonly", argv)
        self.assertIn("--tmpfs", argv)
        self.assertEqual(proposal["mounts"]["results"]["host_precondition"], {"owner_uid": 65532, "mode": "0300"})
        self.assertTrue(proposal["cleanup_required"])
        self.assertEqual(proposal["cleanup_argv"][0][:3], ["docker", "rm", "-f"])

    def test_nix_podman_uses_podman_without_executing_it(self):
        proposal = contract.render_mechanics_runner_proposal(inventory("nix-podman"), request())
        self.assertEqual(proposal["runtime"], "podman")
        self.assertEqual(proposal["run_argv"][4], "podman")

    def test_rejects_mutable_images_gpu_devices_and_unsupported_runtimes(self):
        with self.assertRaises(contract.ContractError):
            contract.render_mechanics_runner_proposal(inventory(), request(image="registry.test/mechanics:latest"))
        with self.assertRaises(contract.ContractError):
            contract.render_mechanics_runner_proposal(inventory(), request(devices=["/dev/nvidia0"]))
        with self.assertRaises(contract.ContractError):
            contract.render_mechanics_runner_proposal(inventory("podman-nvidia-cdi", "gpu"), request())
        with self.assertRaises(contract.ContractError):
            contract.render_mechanics_runner_proposal(inventory("bare"), request())

    def test_rejects_missing_bounds_and_non_isolated_mounts(self):
        with self.assertRaises(contract.ContractError):
            contract.render_mechanics_runner_proposal(inventory(), request(resources={"cpus": 1}))
        with self.assertRaises(contract.ContractError):
            contract.render_mechanics_runner_proposal(inventory(), request(result_dir="/reviewed/source"))

    def test_rejects_runner_identity_not_supplied_by_active_inventory(self):
        with self.assertRaisesRegex(contract.ContractError, "dedicated non-root runner"):
            contract.render_mechanics_runner_proposal(inventory(), request(runner_user="root"))


if __name__ == "__main__":
    unittest.main()
