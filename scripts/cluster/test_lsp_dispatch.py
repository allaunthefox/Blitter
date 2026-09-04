#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from . import dispatch
except ImportError:
    import dispatch


NODE = dispatch.Node(
    name="test-node", address="127.0.0.1", ssh_user="root", role="cpu",
    runtime="docker", max_cpus=4, max_memory_gib=8, ephemeral=False,
)


class LSPDispatchTests(unittest.TestCase):
    def test_lsp_validates_descriptor_before_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.json"
            path.write_text(json.dumps({
                "schema": "mathpunch.blitter-lsp-worker.v1",
                "language": "rust",
                "image": "registry.example.invalid/rust@sha256:" + "b" * 64,
                "command": ["rust-analyzer"],
                "workspace": "/srv/mathpunch/workspaces/run-1",
                "container_workspace": "/workspace",
                "cpus": 1,
                "memory_gib": 2,
                "network": False,
            }), encoding="utf-8")
            args = argparse.Namespace(descriptor=path, timeout=30, allow_ephemeral=False)
            result = type("Result", (), {"stdout": "", "stderr": "", "returncode": 0})()
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
            path = Path(directory) / "worker.json"
            value = {
                "schema": "mathpunch.blitter-lsp-worker.v1",
                "language": "lean",
                "image": "registry.example.invalid/lean@sha256:" + "c" * 64,
                "command": ["lake", "env", "lean", "--server"],
                "workspace": "/srv/mathpunch/workspaces/run-1",
                "container_workspace": "/workspace",
                "cpus": 5,
                "memory_gib": 2,
                "network": False,
            }
            path.write_text(json.dumps(value), encoding="utf-8")
            args = argparse.Namespace(descriptor=path, timeout=30, allow_ephemeral=False)
            with patch.object(dispatch, "ssh") as ssh:
                with self.assertRaisesRegex(ValueError, "CPU request"):
                    dispatch.cmd_lsp(NODE, args)
            ssh.assert_not_called()

    def test_lsp_catalog_must_match_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            descriptor_path = root / "worker.json"
            descriptor_path.write_text(json.dumps({
                "schema": "mathpunch.blitter-lsp-worker.v1",
                "language": "rust",
                "image": "registry.example.invalid/rust@sha256:" + "f" * 64,
                "command": ["rust-analyzer"],
                "workspace": "/srv/mathpunch/workspaces/run-1",
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
                "approved_images": {
                    language: catalog["images"][language]["image"] for language in languages
                },
                "resource_ceilings": {
                    language: {"cpus": 2, "memory_mib": 4096} for language in languages
                },
            }), encoding="utf-8")
            args = argparse.Namespace(descriptor=descriptor_path, catalog=catalog_path,
                                      catalog_policy=policy_path,
                                      timeout=30, allow_ephemeral=False)
            with patch.object(dispatch, "ssh") as ssh:
                with self.assertRaisesRegex(ValueError, "does not match catalog"):
                    dispatch.cmd_lsp(NODE, args)
            ssh.assert_not_called()

    def test_interactive_lsp_uses_streaming_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.json"
            path.write_text(json.dumps({
                "schema": "mathpunch.blitter-lsp-worker.v1",
                "language": "rust",
                "image": "registry.example.invalid/rust@sha256:" + "d" * 64,
                "command": ["rust-analyzer"],
                "workspace": "/srv/mathpunch/workspaces/run-1",
                "container_workspace": "/workspace",
                "cpus": 1,
                "memory_gib": 2,
                "network": False,
            }), encoding="utf-8")
            presence = Path(directory) / "presence.json"
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
                descriptor=path, timeout=30, allow_ephemeral=False, interactive=True,
                presence=presence,
                lease_url="http://127.0.0.1:8790",
                lease_ttl_ms=30000, lease_holder="test-holder",
                worker_generation=7,
                startup_timeout=3.0, idle_timeout=4.0, total_timeout=5.0,
            )
            with patch.object(dispatch, "run_lsp_transport", return_value=0) as transport:
                self.assertEqual(dispatch.cmd_lsp(NODE, args), 0)
            transport.assert_called_once()
            self.assertIn("exec docker run", transport.call_args.args[1])
            self.assertEqual(transport.call_args.kwargs["config"].total_timeout, 5.0)


if __name__ == "__main__":
    unittest.main()
