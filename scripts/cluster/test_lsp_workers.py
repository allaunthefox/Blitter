#!/usr/bin/env python3
from __future__ import annotations

import unittest
import unittest.mock

try:
    from . import lsp_workers
except ImportError:
    import lsp_workers


IMAGE = "registry.example.invalid/mathpunch/lean-lsp@sha256:" + "a" * 64


def descriptor(**overrides):
    value = {
        "schema": lsp_workers.LSP_WORKER_SCHEMA,
        "language": "lean",
        "image": IMAGE,
        "command": ["lake", "env", "lean", "--server"],
        "workspace": "/srv/mathpunch/workspaces/run-1",
        "container_workspace": "/workspace",
        "cpus": 2,
        "memory_gib": 4,
        "network": False,
    }
    value.update(overrides)
    return value


class LSPWorkerTests(unittest.TestCase):
    def test_valid_descriptor_renders_networkless_bounded_container(self):
        spec = lsp_workers.worker_from_mapping(descriptor())
        command = lsp_workers.runtime_command("docker", spec)
        self.assertIn("--network=none", command)
        self.assertIn("--cpus=2", command)
        self.assertIn("--memory=4g", command)
        self.assertIn(
            "source=/srv/mathpunch/workspaces/run-1/source,destination=/workspace/source,readonly",
            command,
        )
        self.assertIn("destination=/workspace/build,rw", command)
        self.assertIn("destination=/workspace/output,rw", command)
        self.assertIn("destination=/workspace/cache,rw", command)
        self.assertIn("--read-only", command)
        self.assertIn("--cap-drop=ALL", command)
        self.assertIn("--user 65532:65532", command)
        self.assertIn("--memory-swap=4g", command)
        self.assertIn("--pull=never", command)
        self.assertNotIn("--device", command)

    def test_all_supported_languages_are_explicit(self):
        for language in lsp_workers.SUPPORTED_LANGUAGES:
            with self.subTest(language=language):
                self.assertEqual(
                    lsp_workers.worker_from_mapping(
                        descriptor(language=language, command=list(lsp_workers.CANONICAL_COMMANDS[language]))
                    ).language,
                    language,
                )

    def test_rejects_unpinned_image_and_network(self):
        with self.assertRaisesRegex(ValueError, "pinned"):
            lsp_workers.worker_from_mapping(descriptor(image="lean:latest"))
        with self.assertRaisesRegex(ValueError, "network"):
            lsp_workers.worker_from_mapping(descriptor(network=True))
        with self.assertRaisesRegex(ValueError, "noncanonical"):
            lsp_workers.worker_from_mapping(
                {key: value for key, value in descriptor().items() if key != "network"}
            )

    def test_rejects_malformed_command_and_limits(self):
        with self.assertRaises(ValueError):
            lsp_workers.worker_from_mapping(descriptor(command=[]))
        with self.assertRaises(ValueError):
            lsp_workers.worker_from_mapping(descriptor(cpus=0))
        with self.assertRaises(ValueError):
            lsp_workers.worker_from_mapping(descriptor(command=["sh", "-c", "x\x00y"]))
        with self.assertRaisesRegex(ValueError, "not canonical"):
            lsp_workers.worker_from_mapping(descriptor(command=["sh", "-c", "lean"]))
        with self.assertRaisesRegex(ValueError, "beneath"):
            lsp_workers.worker_from_mapping(descriptor(workspace="/etc"))
        with self.assertRaisesRegex(ValueError, "comma"):
            lsp_workers.worker_from_mapping(descriptor(workspace="/srv/mathpunch/workspaces/a,b"))
        with self.assertRaisesRegex(ValueError, "noncanonical"):
            lsp_workers.worker_from_mapping(descriptor(extra="unexpected"))

    def test_unknown_runtime_fails_closed(self):
        spec = lsp_workers.worker_from_mapping(descriptor())
        with self.assertRaisesRegex(ValueError, "runtime"):
            lsp_workers.runtime_command("bare", spec)

    def test_nix_runtime_uses_nix_shell(self):
        spec = lsp_workers.worker_from_mapping(descriptor())
        with unittest.mock.patch.dict(
            __import__("os").environ,
            {lsp_workers.PODMAN_SIGNATURE_POLICY_ENV: "/etc/blitter/signature-policy.json"},
        ):
            command = lsp_workers.runtime_command("nix-podman", spec)
        self.assertIn("nix shell nixpkgs#podman", command)
        self.assertIn("--signature-policy=/etc/blitter/signature-policy.json", command)
        self.assertNotIn("insecureAcceptAnything", command)

    def test_nix_runtime_without_deployment_policy_fails_closed(self):
        spec = lsp_workers.worker_from_mapping(descriptor())
        with unittest.mock.patch.dict(__import__("os").environ, {}, clear=False):
            __import__("os").environ.pop(lsp_workers.PODMAN_SIGNATURE_POLICY_ENV, None)
            with self.assertRaisesRegex(ValueError, "without image trust"):
                lsp_workers.runtime_command("nix-podman", spec)


if __name__ == "__main__":
    unittest.main()
