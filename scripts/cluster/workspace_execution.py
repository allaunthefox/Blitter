#!/usr/bin/env python3
"""Hardened workspace execution for containerized language servers.

This module closes three workspace races:

* path escape: every host workspace is canonicalized (pure check) and, when
  the directory exists locally, resolved through symlinks (live check) before
  any mount flag is rendered.  A workspace that resolves outside the approved
  root fails closed;
* symlink TOCTOU between validation and launch: callers must pass the
  *resolved* path returned here into the rendered mount flags, and workspace
  setup must create the resolved directory before resolving;
* cross-job contamination: each job receives four per-job mounts instead of
  one shared read-write mount -- ``source`` (read-only), ``build`` and
  ``output`` (per-job read-write), and ``cache`` (shared read-write for
  toolchains only, never for build outputs).

Launch hardening (rendered by ``render_runtime_command``): fixed non-root
UID/GID, read-only root filesystem, ``--cap-drop=ALL``,
``no-new-privileges``, PID/file-descriptor/core limits, CPU/memory/swap
limits, bounded tmpfs, ``--network=none``, no ``--device`` flags (accelerator
access only through an explicit CDI runtime path), ``--pull=never`` so a
mutable tag can never substitute for the pinned digest, and ``--rm`` so
stopped containers cannot accumulate.

Cleanup is guaranteed by ``job_workspace`` (context manager) and
``cleanup_job_dirs``: per-job ``build``/``output`` directories are removed on
exit even when the job raises.  ``source``/``cache`` are never deleted here.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterator, Mapping

APPROVED_WORKSPACE_ROOT = PurePosixPath("/srv/mathpunch/workspaces")
CONTAINER_WORKSPACE_DEFAULT = "/workspace"
JOB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

WORKER_UID = 65532
WORKER_GID = 65532
WORKER_PIDS = 128
WORKER_NOFILE = 1024
WORKER_SCRATCH_MIB = 512
WORKER_SHM = "64m"
WORKER_RUN_MIB = 32


class WorkspaceError(ValueError):
    """Raised when a workspace path or job layout is not acceptable."""


def canonicalize_workspace(path: str) -> PurePosixPath:
    """Pure canonicalization: absolute, beneath the approved root, no escapes."""
    if not isinstance(path, str) or not path or "\x00" in path:
        raise WorkspaceError("workspace must be a non-empty string")
    if not path.startswith("/"):
        raise WorkspaceError("workspace must be an absolute host path")
    parsed = PurePosixPath(path)
    if ".." in parsed.parts:
        raise WorkspaceError("workspace must not contain '..'")
    if not parsed.is_relative_to(APPROVED_WORKSPACE_ROOT):
        raise WorkspaceError(f"workspace must be beneath {APPROVED_WORKSPACE_ROOT}")
    if parsed == APPROVED_WORKSPACE_ROOT:
        raise WorkspaceError("workspace must be a job directory beneath the root, not the root")
    if "," in path:
        raise WorkspaceError("workspace paths must not contain commas")
    if "//" in path or path.endswith("/."):
        raise WorkspaceError("workspace path is not canonical")
    return parsed


def resolve_workspace_live(path: str) -> str:
    """Resolve symlinks against the live filesystem and re-check containment.

    Returns the resolved absolute path.  Fails closed when the path does not
    exist, is not a directory, or resolves outside the approved root (symlink
    escape).  Callers must mount the *returned* path.
    """
    canonicalize_workspace(path)
    resolved = os.path.realpath(path)
    if "\x00" in resolved or not resolved.startswith("/"):
        raise WorkspaceError("workspace resolved to an invalid path")
    parts = PurePosixPath(resolved).parts
    if ".." in parts or PurePosixPath(resolved) == APPROVED_WORKSPACE_ROOT:
        raise WorkspaceError("workspace resolved outside its job directory")
    if not PurePosixPath(resolved).is_relative_to(APPROVED_WORKSPACE_ROOT):
        raise WorkspaceError("workspace symlink escapes the approved root")
    if not os.path.isdir(resolved):
        raise WorkspaceError("workspace is not an existing directory")
    return resolved


def check_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or JOB_ID_RE.fullmatch(job_id) is None:
        raise WorkspaceError(
            "job_id must match [a-z0-9][a-z0-9_-]{0,63} (lowercase, no path separators)"
        )
    return job_id


@dataclass(frozen=True)
class WorkspacePath:
    """An inode-pinned workspace path.

    After live symlink resolution, the inode and device of the resolved
    directory are captured.  Before any mount flag is rendered, the
    inode is re-verified.  A mismatch means the symlink was swapped
    between resolution and mount, and the operation fails closed.
    """
    resolved: str
    inode: int
    device: int

    def verify(self) -> None:
        """Re-verify the inode; raise WorkspaceError if the symlink escaped."""
        try:
            st = os.stat(self.resolved)
        except FileNotFoundError:
            raise WorkspaceError(
                f"workspace {self.resolved!r} disappeared after resolution; "
                "symlink may have been swapped"
            )
        if st.st_ino != self.inode or st.st_dev != self.device:
            raise WorkspaceError(
                f"workspace {self.resolved!r} inode/device changed "
                f"(expected {self.device}:{self.inode}, got {st.st_dev}:{st.st_ino}); "
                "symlink was swapped between resolution and mount"
            )


def pin_workspace(path: str) -> WorkspacePath:
    """Live-resolve and inode-pin a workspace path.

    This is the anchor for the symlink TOCTOU fix.  Callers must:
    1. Call ``pin_workspace`` once to get the pinned path.
    2. Call ``wp.verify()`` immediately before rendering mount flags.
    3. Mount the *returned* resolved path (``wp.resolved``), never the
       original ``path``.
    """
    resolved = resolve_workspace_live(path)
    try:
        st = os.stat(resolved)
    except OSError as exc:
        raise WorkspaceError(f"cannot stat resolved workspace {resolved!r}: {exc}") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise WorkspaceError(f"workspace {resolved!r} is not a directory")
    return WorkspacePath(resolved=resolved, inode=st.st_ino, device=st.st_dev)


@dataclass(frozen=True)
class JobMount:
    name: str
    source: str
    destination: str
    readonly: bool


def job_mounts(
    workspace: str,
    job_id: str,
    *,
    container_workspace: str = CONTAINER_WORKSPACE_DEFAULT,
) -> tuple[JobMount, ...]:
    """Build the four per-job mounts for a canonical host workspace."""
    root = canonicalize_workspace(workspace)
    check_job_id(job_id)
    if not isinstance(container_workspace, str) or not container_workspace.startswith("/"):
        raise WorkspaceError("container_workspace must be an absolute path")
    container_parts = PurePosixPath(container_workspace).parts
    if ".." in container_parts:
        raise WorkspaceError("container_workspace must not escape its mount")
    if "," in container_workspace:
        raise WorkspaceError("container workspace paths must not contain commas")
    container = PurePosixPath(container_workspace)
    return (
        JobMount("source", str(root / "source"), str(container / "source"), True),
        JobMount("build", str(root / "build" / job_id), str(container / "build"), False),
        JobMount("cache", str(root / "cache"), str(container / "cache"), False),
        JobMount("output", str(root / "output" / job_id), str(container / "output"), False),
    )


def render_mount_flag(mount: JobMount) -> str:
    mode = "readonly" if mount.readonly else "rw"
    return (
        "--mount "
        + shlex.quote(
            f"type=bind,source={mount.source},destination={mount.destination},"
            f"{mode},bind-propagation=rprivate"
        )
    )


def base_security_flags(
    *,
    cpus: int,
    memory_gib: int,
    workdir: str,
    mounts: tuple[JobMount, ...],
) -> str:
    """Render the shared hardened envelope (no runtime, image, or command)."""
    if type(cpus) is not int or cpus < 1:
        raise WorkspaceError("cpus must be a positive integer")
    if type(memory_gib) is not int or memory_gib < 1:
        raise WorkspaceError("memory_gib must be a positive integer")
    if not workdir.startswith("/") or ".." in PurePosixPath(workdir).parts:
        raise WorkspaceError("workdir must be an absolute contained path")
    flags = [
        "--rm",
        "--init",
        f"--user {WORKER_UID}:{WORKER_GID}",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        f"--pids-limit={WORKER_PIDS}",
        f"--ulimit nofile={WORKER_NOFILE}:{WORKER_NOFILE}",
        "--ulimit core=0",
        f"--cpus={cpus}",
        f"--memory={memory_gib}g",
        f"--memory-swap={memory_gib}g",
        f"--shm-size={WORKER_SHM}",
        "--network=none",
        "--pull=never",
        f"--tmpfs /tmp:rw,noexec,nosuid,nodev,size={WORKER_SCRATCH_MIB}m",
        f"--tmpfs /run:rw,noexec,nosuid,nodev,size={WORKER_RUN_MIB}m",
    ]
    flags.extend(render_mount_flag(mount) for mount in mounts)
    flags.append(f"--workdir {shlex.quote(workdir)}")
    return " ".join(flags)


def render_runtime_command(
    *,
    runtime: str,
    image: str,
    command: tuple[str, ...],
    cpus: int,
    memory_gib: int,
    workspace: str,
    job_id: str,
    container_workspace: str = CONTAINER_WORKSPACE_DEFAULT,
    signature_policy_flag: str | None = None,
    workspace_path: WorkspacePath | None = None,
) -> str:
    """Render a fully hardened container command for one job.

    ``image`` must be digest-pinned; ``command`` is an argv vector (never a
    shell string).  The nix-podman path requires the deployment-owned
    ``signature_policy_flag``; device flags are never rendered here.
    """
    if runtime not in ("docker", "nix-podman", "podman-nvidia-cdi"):
        raise WorkspaceError(f"unsupported container runtime: {runtime}")
    if not isinstance(image, str) or "@sha256:" not in image:
        raise WorkspaceError("image must be pinned by sha256 digest")
    if (
        not isinstance(command, (list, tuple))
        or not command
        or any(not isinstance(part, str) or not part or "\x00" in part for part in command)
    ):
        raise WorkspaceError("command must be a non-empty argv vector")
    # Inode-pinned symlink verification.  If the caller already resolved
    # and pinned the workspace, verify the inode now; otherwise resolve and pin.
    # The verified resolved path is then passed to job_mounts so the mount
    # flags always reference the already-verified target, not the raw symlink.
    if workspace_path is None:
        workspace_path = pin_workspace(workspace)
    workspace_path.verify()
    mounts = job_mounts(workspace_path.resolved, job_id, container_workspace=container_workspace)
    workdir = str(PurePosixPath(container_workspace) / "source")
    envelope = base_security_flags(
        cpus=cpus, memory_gib=memory_gib, workdir=workdir, mounts=mounts
    )
    rendered_command = " ".join(shlex.quote(part) for part in command)
    quoted_image = shlex.quote(image)
    if runtime == "docker":
        return f"docker run {envelope} {quoted_image} {rendered_command}"
    if runtime == "podman-nvidia-cdi":
        return f"podman run {envelope} {quoted_image} {rendered_command}"
    if not signature_policy_flag:
        raise WorkspaceError(
            "nix-podman launches require a deployment-owned signature policy flag"
        )
    return (
        "nix shell nixpkgs#podman --command "
        f"podman run {signature_policy_flag} {envelope} {quoted_image} {rendered_command}"
    )


def job_directories(workspace: str, job_id: str) -> Mapping[str, str]:
    """Return the host directories a job owns (created by ``setup_job_dirs``)."""
    root = canonicalize_workspace(workspace)
    check_job_id(job_id)
    return {
        "source": str(root / "source"),
        "build": str(root / "build" / job_id),
        "cache": str(root / "cache"),
        "output": str(root / "output" / job_id),
    }


def setup_job_dirs(workspace: str, job_id: str) -> Mapping[str, str]:
    """Create the job directory layout with least-privilege modes."""
    directories = job_directories(workspace, job_id)
    for name, path in directories.items():
        os.makedirs(path, mode=0o755, exist_ok=True)
        if name in ("build", "output"):
            os.chmod(path, 0o755)
    return directories


def cleanup_job_dirs(workspace: str, job_id: str) -> None:
    """Remove per-job build/output directories; never source/cache.

    Best-effort per directory: failures are collected and raised together so
    one leftover never masks another.  Each removal re-validates containment.
    """
    root = canonicalize_workspace(workspace)
    check_job_id(job_id)
    errors: list[str] = []
    for name in ("build", "output"):
        target = root / name / job_id
        target_str = str(target)
        try:
            resolved = os.path.realpath(target_str)
            if not PurePosixPath(resolved).is_relative_to(root / name):
                raise WorkspaceError(f"refusing to clean escaped path: {resolved}")
            import shutil

            shutil.rmtree(target_str, ignore_errors=False)
        except FileNotFoundError:
            continue
        except (OSError, WorkspaceError) as exc:
            errors.append(f"{name}: {exc}")
    if errors:
        raise WorkspaceError("job cleanup incomplete: " + "; ".join(errors))


@contextlib.contextmanager
def job_workspace(workspace: str, job_id: str) -> Iterator[Mapping[str, str]]:
    """Set up job directories, yield them, and guarantee per-job cleanup."""
    directories = setup_job_dirs(workspace, job_id)
    try:
        yield directories
    finally:
        cleanup_job_dirs(workspace, job_id)
