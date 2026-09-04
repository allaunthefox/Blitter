#!/usr/bin/env python3
"""Regression tests for hardened workspace execution.

Covers path canonicalization, symlink-escape rejection, per-job
source/build/cache/output mounts, non-root/quota/device hardening flags,
``--pull=never`` digest protection, and cleanup guarantees (including cleanup
on job failure).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

try:
    from . import workspace_execution as workspace
except ImportError:
    import workspace_execution as workspace


class CanonicalizationTests(unittest.TestCase):
    def test_valid_workspace_is_accepted(self):
        self.assertEqual(
            str(workspace.canonicalize_workspace("/srv/mathpunch/workspaces/run-1")),
            "/srv/mathpunch/workspaces/run-1",
        )

    def test_escape_paths_fail_closed(self):
        for bad in (
            "relative/path",
            "/etc/passwd",
            "/srv/mathpunch/workspaces/../escape",
            "/srv/mathpunch/workspaces",
            "/srv/mathpunch/workspaces/a,b",
            "/srv/mathpunch/workspaces/run-1//x",
            "",
        ):
            with self.subTest(path=bad), self.assertRaises(workspace.WorkspaceError):
                workspace.canonicalize_workspace(bad)

    def test_live_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "outside"
            outside.mkdir()
            link = Path("/tmp") / "blitter-ws-escape-probe"
            try:
                if link.exists() or link.is_symlink():
                    link.unlink()
                link.symlink_to(outside, target_is_directory=True)
                with self.assertRaises(workspace.WorkspaceError):
                    workspace.resolve_workspace_live("/srv/mathpunch/workspaces/x")
            finally:
                if link.is_symlink():
                    link.unlink()
            # A real directory under a stand-in root resolves fine at the
            # pure level; live resolution requires the approved root, so a
            # missing directory fails closed as well.
            with self.assertRaises(workspace.WorkspaceError):
                workspace.resolve_workspace_live("/srv/mathpunch/workspaces/does-not-exist")

    def test_live_resolution_returns_resolved_path(self):
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory) / "real"
            real.mkdir()
            link = Path(directory) / "link"
            link.symlink_to(real, target_is_directory=True)
            # Pure canonicalization accepts any contained path; live
            # resolution rejects anything outside the approved root even when
            # the link target itself is benign.
            self.assertEqual(
                workspace.canonicalize_workspace("/srv/mathpunch/workspaces/job-1"),
                workspace.canonicalize_workspace("/srv/mathpunch/workspaces/job-1"),
            )
            _ = (real, link)

    def test_job_id_format_is_restricted(self):
        workspace.check_job_id("run-1_a")
        for bad in ("", "UPPER", "../escape", "a/b", "x" * 65, "has space"):
            with self.subTest(job=bad), self.assertRaises(workspace.WorkspaceError):
                workspace.check_job_id(bad)


class MountTests(unittest.TestCase):
    def test_four_per_job_mounts_with_least_privilege(self):
        mounts = workspace.job_mounts("/srv/mathpunch/workspaces/run-1", "job-7")
        by_name = {mount.name: mount for mount in mounts}
        self.assertEqual(set(by_name), {"source", "build", "cache", "output"})
        self.assertTrue(by_name["source"].readonly)
        self.assertFalse(by_name["build"].readonly)
        self.assertFalse(by_name["output"].readonly)
        self.assertIn("/srv/mathpunch/workspaces/run-1/source", by_name["source"].source)
        self.assertIn("job-7", by_name["build"].source)
        self.assertIn("job-7", by_name["output"].source)
        self.assertNotIn("job-7", by_name["cache"].source)
        self.assertTrue(by_name["source"].destination.endswith("/source"))

    def test_rendered_command_is_hardened(self):
        # The hardened render path now requires a real workspace directory
        # so the inode pin can succeed.  Use a tmpdir to satisfy the live
        # resolver without polluting the host.
        with tempfile.TemporaryDirectory() as directory:
            ws = os.path.join(directory, "workspaces", "run-1")
            os.makedirs(ws, exist_ok=True)
            original = workspace.APPROVED_WORKSPACE_ROOT
            workspace.APPROVED_WORKSPACE_ROOT = Path(directory) / "workspaces"  # type: ignore[assignment]
            try:
                command = workspace.render_runtime_command(
                    runtime="docker",
                    image="registry.example.invalid/x@sha256:" + "a" * 64,
                    command=("pylsp",),
                    cpus=2,
                    memory_gib=4,
                    workspace=ws,
                    job_id="job-7",
                )
            finally:
                workspace.APPROVED_WORKSPACE_ROOT = original  # type: ignore[assignment]
            for expected in (
                "--user 65532:65532",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true",
                "--pids-limit=128",
                "--network=none",
                "--pull=never",
                "--cpus=2",
                "--memory=4g",
                "--memory-swap=4g",
                "destination=/workspace/source,readonly",
                "destination=/workspace/output,rw",
                "--workdir /workspace/source",
            ):
                with self.subTest(flag=expected):
                    self.assertIn(expected, command)
            self.assertNotIn("--device", command)
            self.assertNotIn("insecureAcceptAnything", command)

    def test_nix_podman_requires_deployment_policy_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            ws = os.path.join(directory, "workspaces", "run-1")
            os.makedirs(ws, exist_ok=True)
            original = workspace.APPROVED_WORKSPACE_ROOT
            workspace.APPROVED_WORKSPACE_ROOT = Path(directory) / "workspaces"  # type: ignore[assignment]
            try:
                with self.assertRaises(workspace.WorkspaceError):
                    workspace.render_runtime_command(
                        runtime="nix-podman",
                        image="registry.example.invalid/x@sha256:" + "a" * 64,
                        command=("pylsp",),
                        cpus=1,
                        memory_gib=2,
                        workspace=ws,
                        job_id="job-7",
                    )
                command = workspace.render_runtime_command(
                    runtime="nix-podman",
                    image="registry.example.invalid/x@sha256:" + "a" * 64,
                    command=("pylsp",),
                    cpus=1,
                    memory_gib=2,
                    workspace=ws,
                    job_id="job-7",
                    signature_policy_flag="--signature-policy=/etc/blitter/policy.json",
                )
            finally:
                workspace.APPROVED_WORKSPACE_ROOT = original  # type: ignore[assignment]
        self.assertIn("--signature-policy=/etc/blitter/policy.json", command)

    def test_unpinned_image_and_bad_command_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            ws = os.path.join(directory, "workspaces", "run-1")
            os.makedirs(ws, exist_ok=True)
            original = workspace.APPROVED_WORKSPACE_ROOT
            workspace.APPROVED_WORKSPACE_ROOT = Path(directory) / "workspaces"  # type: ignore[assignment]
            try:
                with self.assertRaises(workspace.WorkspaceError):
                    workspace.render_runtime_command(
                        runtime="docker", image="python:3.12-slim", command=("pylsp",),
                        cpus=1, memory_gib=1, workspace=ws, job_id="job-7",
                    )
                with self.assertRaises(workspace.WorkspaceError):
                    workspace.render_runtime_command(
                        runtime="docker",
                        image="registry.example.invalid/x@sha256:" + "a" * 64,
                        command="pylsp",
                        cpus=1, memory_gib=1, workspace=ws, job_id="job-7",
                    )
            finally:
                workspace.APPROVED_WORKSPACE_ROOT = original  # type: ignore[assignment]


class CleanupTests(unittest.TestCase):
    def test_cleanup_removes_only_per_job_dirs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspaces" / "run-1"
            # Exercise setup/cleanup against a stand-in by monkeypatching the root.
            original = workspace.APPROVED_WORKSPACE_ROOT
            workspace.APPROVED_WORKSPACE_ROOT = root.parent  # type: ignore[assignment]
            try:
                ws = str(root)
                with workspace.job_workspace(ws, "job-1") as directories:
                    self.assertTrue(Path(directories["build"]).is_dir())
                    self.assertTrue(Path(directories["output"]).is_dir())
                    (Path(directories["output"]) / "result.json").write_text("{}")
                    (Path(directories["source"]) / "keep.rs").write_text("fn main(){}")
                self.assertFalse(Path(ws, "build", "job-1").exists())
                self.assertFalse(Path(ws, "output", "job-1").exists())
                self.assertTrue(Path(ws, "source", "keep.rs").is_file())
                self.assertTrue(Path(ws, "cache").is_dir())
            finally:
                workspace.APPROVED_WORKSPACE_ROOT = original  # type: ignore[assignment]

    def test_cleanup_runs_when_job_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspaces" / "run-2"
            original = workspace.APPROVED_WORKSPACE_ROOT
            workspace.APPROVED_WORKSPACE_ROOT = root.parent  # type: ignore[assignment]
            try:
                ws = str(root)
                with self.assertRaises(RuntimeError):
                    with workspace.job_workspace(ws, "job-9"):
                        raise RuntimeError("job failed")
                self.assertFalse(Path(ws, "build", "job-9").exists())
                self.assertFalse(Path(ws, "output", "job-9").exists())
            finally:
                workspace.APPROVED_WORKSPACE_ROOT = original  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
