#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch

try:
    from . import dispatch
    from . import lsp_workers
    from . import workspace_execution as workspace
except ImportError:
    import dispatch
    import lsp_workers
    import workspace_execution as workspace


NODE = dispatch.Node(
    name="test-node", address="127.0.0.1", ssh_user="root", role="cpu",
    runtime="docker", max_cpus=4, max_memory_gib=8, ephemeral=False,
)


def _make_signed_catalog_and_policy(root: Path, *, descriptor_payload: dict | None = None):
    """Create a signed catalog and policy that pass verify_image_trust.

    The catalog has all six languages; the policy has approved keys,
    images, and provenance.  ``descriptor_payload`` is the descriptor the
    test will submit, used to make sure the catalog entry matches.
    """
    languages = ("lean", "coq", "python", "r", "julia", "rust")
    commands = {
        "lean": ["lake", "env", "lean", "--server"], "coq": ["coq-lsp"],
        "python": ["pylsp"], "r": ["R", "--vanilla", "--slave"],
        "julia": ["julia", "--startup-file=no"], "rust": ["rust-analyzer"],
    }
    images = {
        language: "registry.example.invalid/" + language + "@sha256:" + str(i) * 64
        for i, language in enumerate(languages, start=1)
    }
    catalog = {
        "schema": "mathpunch.blitter-lsp-image-catalog.v1",
        "images": {
            language: {
                "image": images[language],
                "argv": commands[language],
                "toolchain": {"name": language, "version": "1",
                              "identity": language + "@1"},
                "network": False,
                "resources": {"cpus": 1, "memory_mib": 2048},
            }
            for language in languages
        },
        "metadata": {
            "signature": {"algorithm": "ed25519", "key_id": "test", "value": "value"},
            "provenance": {"source": "test", "source_commit": "a" * 40,
                            "build_digest": "sha256:" + "1" * 64},
        },
    }
    # If a descriptor is provided, ensure the catalog has a matching image/argv.
    if descriptor_payload is not None:
        language = descriptor_payload["language"]
        catalog["images"][language] = {
            "image": descriptor_payload["image"],
            "argv": list(descriptor_payload["command"]),
            "toolchain": {"name": language, "version": "1",
                          "identity": language + "@1"},
            "network": False,
            "resources": {"cpus": 1, "memory_mib": 2048},
        }
    catalog_path = root / "catalog.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    policy_path = root / "policy.json"
    policy_path.write_text(json.dumps({
        "approved_keys": ["0000000000000000000000000000000000000000000000000000000000000000"],
        "approved_images": {
            language: catalog["images"][language]["image"] for language in languages
        },
        "approved_sources": ["test"],
        "resource_ceilings": {
            language: {"cpus": 4, "memory_mib": 8192} for language in languages
        },
    }), encoding="utf-8")
    return catalog_path, policy_path


class LSPDispatchTests(unittest.TestCase):
    def setUp(self):
        # Patch the approved workspace root to use a tmpdir so that
        # lsp_workers.runtime_command calls pin_workspace() succeed.
        from pathlib import PurePosixPath
        self._ws_tmpdir = tempfile.mkdtemp(prefix="blitter-dispatch-ws-")
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


    def test_lsp_validates_descriptor_before_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ws_path = os.path.join(self._ws_tmpdir, "validate-test")
            os.makedirs(ws_path, exist_ok=True)
            descriptor = {
                "schema": "mathpunch.blitter-lsp-worker.v1",
                "language": "rust",
                "image": "registry.example.invalid/rust@sha256:" + "b" * 64,
                "command": ["rust-analyzer"],
                "workspace": ws_path,
                "container_workspace": "/workspace",
                "cpus": 1,
                "memory_gib": 2,
                "network": False,
            }
            path = root / "worker.json"
            os.makedirs(descriptor["workspace"], exist_ok=True)
            path.write_text(json.dumps(descriptor), encoding="utf-8")
            catalog_path, policy_path = _make_signed_catalog_and_policy(root, descriptor_payload=descriptor)
            # verify_image_trust will fail on the test policy (the key is all
            # zeros).  Patch verify_image_trust to a no-op so the test exercises
            # the dispatch path; the actual cryptographic verification is tested
            # by test_image_trust.py.
            args = argparse.Namespace(
                descriptor=path, catalog=catalog_path, catalog_policy=policy_path,
                timeout=30, allow_ephemeral=False,
            )
            result = type("Result", (), {"stdout": "", "stderr": "", "returncode": 0})()
            with patch.object(dispatch, "verify_image_trust", lambda catalog, policy: None):
                # DOCKER_CONTENT_TRUST=1 is mandatory for the docker runtime.
                with unittest.mock.patch.dict(__import__("os").environ,
                                              {lsp_workers.DOCKER_CONTENT_TRUST_ENV: "1"}):
                    with patch.object(dispatch, "ssh", return_value=result) as ssh:
                        self.assertEqual(dispatch.cmd_lsp(NODE, args), 0)
            ssh.assert_called_once()
            self.assertIn("--network=none", ssh.call_args.args[1])

    def test_lsp_rejects_ephemeral_node_without_opt_in(self):
        ephemeral = NODE.__class__(**{**NODE.__dict__, "ephemeral": True})
        args = argparse.Namespace(descriptor=Path("missing.json"), timeout=30, allow_ephemeral=False)
        with self.assertRaisesRegex(ValueError, "ephemeral"):
            dispatch.cmd_lsp(ephemeral, args)

    def test_lsp_rejects_requests_over_node_caps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ws_path = os.path.join(self._ws_tmpdir, "caps-test")
            os.makedirs(ws_path, exist_ok=True)
            path = root / "worker.json"
            value = {
                "schema": "mathpunch.blitter-lsp-worker.v1",
                "language": "lean",
                "image": "registry.example.invalid/lean@sha256:" + "c" * 64,
                "command": ["lake", "env", "lean", "--server"],
                "workspace": ws_path,
                "container_workspace": "/workspace",
                "cpus": 5,
                "memory_gib": 2,
                "network": False,
            }
            path.write_text(json.dumps(value), encoding="utf-8")
            args = argparse.Namespace(descriptor=path, catalog=None, catalog_policy=None, timeout=30, allow_ephemeral=False)
            with patch.object(dispatch, "ssh") as ssh:
                with self.assertRaisesRegex(ValueError, "CPU request"):
                    dispatch.cmd_lsp(NODE, args)
            ssh.assert_not_called()

    def test_lsp_catalog_must_match_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Use a workspace under the patched root so the descriptor is valid.
            ws_path = os.path.join(self._ws_tmpdir, "catalog-test")
            os.makedirs(ws_path, exist_ok=True)
            descriptor_path = root / "worker.json"
            descriptor_path.write_text(json.dumps({
                "schema": "mathpunch.blitter-lsp-worker.v1",
                "language": "rust",
                "image": "registry.example.invalid/rust@sha256:" + "f" * 64,
                "command": ["rust-analyzer"],
                "workspace": ws_path,
                "container_workspace": "/workspace",
                "cpus": 1,
                "memory_gib": 2,
                "network": False,
            }), encoding="utf-8")
            languages = ("lean", "coq", "python", "r", "julia", "rust")
            catalog = {
                "schema": "mathpunch.blitter-lsp-image-catalog.v1",
                "images": {},
                "metadata": {
                    "signature": {"algorithm": "ed25519", "key_id": "test", "value": "value"},
                    "provenance": {"source": "test", "source_commit": "a" * 40,
                                    "build_digest": "sha256:" + "1" * 64},
                },
            }
            commands = {
                "lean": ["lake", "env", "lean", "--server"], "coq": ["coq-lsp"],
                "python": ["pylsp"], "r": ["R", "--vanilla", "--slave"],
                "julia": ["julia", "--startup-file=no"], "rust": ["rust-analyzer"],
            }
            for language in languages:
                image = "registry.example.invalid/" + language + "@sha256:" + "a" * 64
                catalog["images"][language] = {
                    "image": image, "argv": commands[language],
                    "toolchain": {"name": language, "version": "1", "identity": language + "@1"},
                    "network": False, "resources": {"cpus": 1, "memory_mib": 2048},
                }
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps({
                "approved_keys": ["0000000000000000000000000000000000000000000000000000000000000000"],
                "approved_images": {
                    language: catalog["images"][language]["image"] for language in languages
                },
                "approved_sources": ["test"],
                "resource_ceilings": {
                    language: {"cpus": 2, "memory_mib": 4096} for language in languages
                },
            }), encoding="utf-8")
            args = argparse.Namespace(descriptor=descriptor_path, catalog=catalog_path,
                                      catalog_policy=policy_path,
                                      timeout=30, allow_ephemeral=False)
            with patch.object(dispatch, "verify_image_trust", lambda catalog, policy: None):
                with patch.object(dispatch, "ssh") as ssh:
                    with self.assertRaisesRegex(ValueError, "does not match catalog"):
                        dispatch.cmd_lsp(NODE, args)
            ssh.assert_not_called()

    def test_interactive_lsp_uses_streaming_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            descriptor = {
                "schema": "mathpunch.blitter-lsp-worker.v1",
                "language": "rust",
                "image": "registry.example.invalid/rust@sha256:" + "d" * 64,
                "command": ["rust-analyzer"],
                "workspace": os.path.join(self._ws_tmpdir, "run-1"),
                "container_workspace": "/workspace",
                "cpus": 1,
                "memory_gib": 2,
                "network": False,
            }
            path = root / "worker.json"
            os.makedirs(descriptor["workspace"], exist_ok=True)
            path.write_text(json.dumps(descriptor), encoding="utf-8")
            catalog_path, policy_path = _make_signed_catalog_and_policy(root, descriptor_payload=descriptor)
            presence = root / "presence.json"
            now = int(__import__("time").time() * 1000)
            presence.write_text(json.dumps({
                "schema": "mathpunch.blitter-lsp-presence.v1",
                "node_id": NODE.name,
                "boot_id": "boot-1",
                "sent_unix_ms": now,
                "availability_ttl_ms": 30000,
                "availability_expires_unix_ms": now + 30000,
                "capabilities": ["lsp", "language:rust"],
                "available": True,
            }), encoding="utf-8")
            args = argparse.Namespace(
                descriptor=path, catalog=catalog_path, catalog_policy=policy_path,
                timeout=30, allow_ephemeral=False, interactive=True,
                presence=presence,
                lease_url="http://127.0.0.1:8790",
                lease_ttl_ms=30000, lease_holder="test-holder",
                worker_generation=7,
                startup_timeout=3.0, idle_timeout=4.0, total_timeout=5.0,
            )
            with patch.object(dispatch, "verify_image_trust", lambda catalog, policy: None):
                # DOCKER_CONTENT_TRUST=1 is mandatory for the docker runtime.
                with unittest.mock.patch.dict(__import__("os").environ,
                                              {lsp_workers.DOCKER_CONTENT_TRUST_ENV: "1"}):
                    with patch.object(dispatch, "run_lsp_transport", return_value=0) as transport:
                        self.assertEqual(dispatch.cmd_lsp(NODE, args), 0)
            transport.assert_called_once()
            self.assertIn("exec docker run", transport.call_args.args[1])
            self.assertEqual(transport.call_args.kwargs["config"].total_timeout, 5.0)


if __name__ == "__main__":
    unittest.main()
