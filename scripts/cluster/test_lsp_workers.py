#!/usr/bin/env python3
from __future__ import annotations

import os
import tempfile
import unittest
import unittest.mock

try:
    from . import lsp_workers
    from . import workspace_execution as workspace
except ImportError:
    import lsp_workers
    import workspace_execution as workspace


IMAGE = "registry.example.invalid/mathpunch/lean-lsp@sha256:" + "a" * 64


def descriptor(workspace=None, **overrides):
    if workspace is None:
        workspace = "/srv/mathpunch/workspaces/run-1"
    value = {
        "schema": lsp_workers.LSP_WORKER_SCHEMA,
        "language": "lean",
        "image": IMAGE,
        "command": ["lake", "env", "lean", "--server"],
        "workspace": workspace,
        "container_workspace": "/workspace",
        "cpus": 2,
        "memory_gib": 4,
        "network": False,
    }
    value.update(overrides)
    return value


class LSPWorkerTests(unittest.TestCase):
    def setUp(self):
        # The hardening layer now requires the workspace path to exist on
        # disk so the inode pin can succeed.  We create a real tmpdir and
        # point the approved workspace root to it.  This avoids requiring
        # write access to /srv/mathpunch/workspaces.
        from pathlib import PurePosixPath
        self._ws_tmpdir = tempfile.mkdtemp(prefix="blitter-ws-")
        self._original_ws_root = workspace.APPROVED_WORKSPACE_ROOT
        workspace.APPROVED_WORKSPACE_ROOT = PurePosixPath(self._ws_tmpdir)
        lsp_workers._refresh_approved_root()
        self.addCleanup(self._cleanup_workspace)

    def _cleanup_workspace(self):
        import shutil
        workspace.APPROVED_WORKSPACE_ROOT = self._original_ws_root
        lsp_workers._refresh_approved_root()
        with __import__("contextlib").suppress(Exception):
            shutil.rmtree(self._ws_tmpdir, ignore_errors=True)

    def test_valid_descriptor_renders_networkless_bounded_container(self):
        ws_path = os.path.join(self._ws_tmpdir, "docker-job")
        os.makedirs(ws_path, exist_ok=True)
        spec = lsp_workers.worker_from_mapping(descriptor(workspace=ws_path))
        with unittest.mock.patch.dict(
            __import__("os").environ,
            {lsp_workers.DOCKER_CONTENT_TRUST_ENV: "1"},
        ):
            command = lsp_workers.runtime_command("docker", spec)
        self.assertIn("--network=none", command)
        self.assertIn("--cpus=2", command)
        self.assertIn("--memory=4g", command)
        # The source path in the rendered command is the resolved (real)
        # path, not the symlink.  We check that the source/destination
        # pair is present.
        self.assertIn(
            f"source={ws_path.rstrip('/')}/source,destination=/workspace/source,readonly",
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
        ws_path = os.path.join(self._ws_tmpdir, "lang-test")
        os.makedirs(ws_path, exist_ok=True)
        for language in lsp_workers.SUPPORTED_LANGUAGES:
            with self.subTest(language=language):
                self.assertEqual(
                    lsp_workers.worker_from_mapping(
                        descriptor(workspace=ws_path, language=language, command=list(lsp_workers.CANONICAL_COMMANDS[language]))
                    ).language,
                    language,
                )

    def test_rejects_unpinned_image_and_network(self):
        ws_path = os.path.join(self._ws_tmpdir, "reject-job")
        with self.assertRaisesRegex(ValueError, "pinned"):
            lsp_workers.worker_from_mapping(descriptor(workspace=ws_path, image="lean:latest"))
        with self.assertRaisesRegex(ValueError, "network"):
            lsp_workers.worker_from_mapping(descriptor(workspace=ws_path, network=True))
        with self.assertRaisesRegex(ValueError, "noncanonical"):
            lsp_workers.worker_from_mapping(
                {key: value for key, value in descriptor(workspace=ws_path).items() if key != "network"}
            )

    def test_rejects_malformed_command_and_limits(self):
        ws_path = os.path.join(self._ws_tmpdir, "malformed-job")
        os.makedirs(ws_path, exist_ok=True)
        with self.assertRaises(ValueError):
            lsp_workers.worker_from_mapping(descriptor(workspace=ws_path, command=[]))
        with self.assertRaises(ValueError):
            lsp_workers.worker_from_mapping(descriptor(workspace=ws_path, cpus=0))
        with self.assertRaises(ValueError):
            lsp_workers.worker_from_mapping(descriptor(workspace=ws_path, command=["sh", "-c", "x\x00y"]))
        with self.assertRaisesRegex(ValueError, "not canonical"):
            lsp_workers.worker_from_mapping(descriptor(workspace=ws_path, command=["sh", "-c", "lean"]))
        # "beneath" test: /etc is not under the patched tmp workspace root.
        with self.assertRaisesRegex(ValueError, "beneath"):
            lsp_workers.worker_from_mapping(descriptor(workspace="/etc"))
        # "comma" test: use a path under the patched root that contains a comma.
        comma_ws = os.path.join(self._ws_tmpdir, "a,b")
        os.makedirs(comma_ws, exist_ok=True)
        with self.assertRaisesRegex(ValueError, "comma"):
            lsp_workers.worker_from_mapping(descriptor(workspace=comma_ws))
        with self.assertRaisesRegex(ValueError, "noncanonical"):
            lsp_workers.worker_from_mapping(descriptor(workspace=ws_path, extra="unexpected"))

    def test_unknown_runtime_fails_closed(self):
        ws_path = os.path.join(self._ws_tmpdir, "unknown-job")
        spec = lsp_workers.worker_from_mapping(descriptor(workspace=ws_path))
        with self.assertRaisesRegex(ValueError, "runtime"):
            lsp_workers.runtime_command("bare", spec)

    def test_nix_runtime_uses_nix_shell(self):
        ws_path = os.path.join(self._ws_tmpdir, "nix-job")
        os.makedirs(ws_path, exist_ok=True)
        spec = lsp_workers.worker_from_mapping(descriptor(workspace=ws_path))
        with unittest.mock.patch.dict(
            __import__("os").environ,
            {lsp_workers.PODMAN_SIGNATURE_POLICY_ENV: "/etc/blitter/signature-policy.json"},
        ):
            command = lsp_workers.runtime_command("nix-podman", spec)
        self.assertIn("nix shell nixpkgs#podman", command)
        self.assertIn("--signature-policy=/etc/blitter/signature-policy.json", command)
        self.assertNotIn("insecureAcceptAnything", command)

    def test_nix_runtime_without_deployment_policy_fails_closed(self):
        ws_path = os.path.join(self._ws_tmpdir, "nix-no-policy")
        spec = lsp_workers.worker_from_mapping(descriptor(workspace=ws_path))
        with unittest.mock.patch.dict(__import__("os").environ, {}, clear=False):
            __import__("os").environ.pop(lsp_workers.PODMAN_SIGNATURE_POLICY_ENV, None)
            with self.assertRaisesRegex(ValueError, "without image trust"):
                lsp_workers.runtime_command("nix-podman", spec)


if __name__ == "__main__":
    unittest.main()
