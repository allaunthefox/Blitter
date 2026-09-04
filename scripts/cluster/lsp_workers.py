#!/usr/bin/env python3
"""Validated container launch metadata for MathPunch language servers.

This module only validates and renders an execution request.  It does not start
containers, grant leases, or treat a language-server response as proof.
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping

LSP_WORKER_SCHEMA = "mathpunch.blitter-lsp-worker.v1"
SUPPORTED_LANGUAGES = frozenset({"lean", "coq", "python", "r", "julia", "rust"})
SUPPORTED_RUNTIMES = frozenset({"docker", "nix-podman", "podman-nvidia-cdi"})
CANONICAL_COMMANDS: dict[str, tuple[str, ...]] = {
    "lean": ("lake", "env", "lean", "--server"),
    "coq": ("coq-lsp",),
    "python": ("pylsp",),
    "r": ("R", "--vanilla", "--slave"),
    "julia": ("julia", "--startup-file=no"),
    "rust": ("rust-analyzer",),
}
# Reference the canonical approved root from workspace_execution so test
# patches and the production layout stay aligned.  We import lazily to avoid
# a hard dependency cycle at module load time.
def _get_approved_root() -> PurePosixPath:
    try:
        from . import workspace_execution as _ws
    except ImportError:
        import workspace_execution as _ws
    return _ws.APPROVED_WORKSPACE_ROOT
# Note: this is a *reference* to the live value, but the tests and the
# dispatcher only need to call _approved_workspace_root() to get the current
# value.  We do not expose APPROVED_WORKSPACE_ROOT as a property because
# that would be unconventional in module globals.
APPROVED_WORKSPACE_ROOT: PurePosixPath = _get_approved_root()


def _approved_workspace_root() -> PurePosixPath:
    """Return the current approved workspace root, looking up live.

    This is the canonical accessor for the approved root inside
    lsp_workers.  Tests that patch workspace_execution.APPROVED_WORKSPACE_ROOT
    must also call this function (or call _refresh_approved_root() to
    re-import) to see the patched value.
    """
    return _get_approved_root()


def _refresh_approved_root() -> PurePosixPath:
    """Re-resolve APPROVED_WORKSPACE_ROOT from workspace_execution.

    Call this in test setUp() after patching
    workspace_execution.APPROVED_WORKSPACE_ROOT so that lsp_workers sees
    the new value.
    """
    global APPROVED_WORKSPACE_ROOT
    APPROVED_WORKSPACE_ROOT = _get_approved_root()
    return APPROVED_WORKSPACE_ROOT
WORKER_UID = 65532
WORKER_GID = 65532
WORKER_PIDS = 128
WORKER_NOFILE = 1024
WORKER_SCRATCH_MIB = 512
_IMAGE_DIGEST = re.compile(r"^.+@sha256:[0-9a-f]{64}$")

# Deployment-owned containers signature policy for Podman-based runtimes.
# There is no insecure fallback: rendering a nix-podman/podman command without
# this variable fails closed instead of accepting any image signature.
PODMAN_SIGNATURE_POLICY_ENV = "BLITTER_PODMAN_SIGNATURE_POLICY"
# Docker Content Trust is mandatory for all docker runtimes.  The deployment
# must configure DCT with its own root key (DOCKER_CONTENT_TRUST_ROOT_PASSPHRASE
# + DOCKER_CONTENT_TRUST_REPOSITORY_PASSPHRASE) and the registry must be
# configured to enforce signed tags.  There is no insecure fallback.
DOCKER_CONTENT_TRUST_ENV = "DOCKER_CONTENT_TRUST"
DOCKER_CONTENT_TRUST_VAL = "1"


def podman_signature_policy_flag() -> str:
    """Render ``--signature-policy`` from the deployment-owned policy file."""
    policy = os.environ.get(PODMAN_SIGNATURE_POLICY_ENV, "")
    if not policy or "\x00" in policy:
        raise ValueError(
            "refusing to render a Podman command without image trust: set "
            f"{PODMAN_SIGNATURE_POLICY_ENV} to a deployment-owned containers "
            "signature policy file"
        )
    if not policy.startswith("/"):
        raise ValueError("the Podman signature policy must be an absolute path")
    return f"--signature-policy={shlex.quote(policy)}"


def docker_content_trust_check() -> None:
    """Fail closed if Docker Content Trust is not enabled.

    With DCT enabled, the local Docker client refuses to pull or run
    images that do not carry a signature from a configured trust root.
    The deployment is responsible for installing the trust root
    (notary key material) on every dispatch host.
    """
    if os.environ.get(DOCKER_CONTENT_TRUST_ENV) != DOCKER_CONTENT_TRUST_VAL:
        raise ValueError(
            f"refusing to render a Docker command without image trust: set "
            f"{DOCKER_CONTENT_TRUST_ENV}={DOCKER_CONTENT_TRUST_VAL} and ensure "
            "the deployment trust root is installed in the local Docker trust "
            "store. A bare --pull=never does not prove the local image is "
            "authorized."
        )


@dataclass(frozen=True)
class LSPWorkerSpec:
    language: str
    image: str
    command: tuple[str, ...]
    workspace: str
    container_workspace: str
    cpus: int
    memory_gib: int
    network: bool = False


def worker_from_mapping(payload: Mapping[str, Any]) -> LSPWorkerSpec:
    """Parse a complete worker descriptor; missing values fail closed."""
    if not isinstance(payload, Mapping):
        raise ValueError("LSP worker descriptor must be a JSON object")
    allowed_fields = {
        "schema", "language", "image", "command", "workspace",
        "container_workspace", "cpus", "memory_gib", "network",
    }
    if set(payload) != allowed_fields:
        raise ValueError("LSP worker descriptor fields are noncanonical")
    if payload.get("schema") != LSP_WORKER_SCHEMA:
        raise ValueError("unsupported or missing LSP worker schema")
    language = payload.get("language")
    if not isinstance(language, str) or language not in SUPPORTED_LANGUAGES:
        raise ValueError("unsupported LSP language")
    image = payload.get("image")
    if not isinstance(image, str) or not _IMAGE_DIGEST.fullmatch(image):
        raise ValueError("LSP image must be pinned by sha256 digest")
    command = payload.get("command")
    if not isinstance(command, (list, tuple)) or not command or any(
        not isinstance(part, str) or not part or "\x00" in part for part in command
    ):
        raise ValueError("LSP command must be a non-empty list of strings")
    if tuple(command) != CANONICAL_COMMANDS[language]:
        raise ValueError(f"{language} LSP command is not canonical")
    workspace = payload.get("workspace")
    container_workspace = payload.get("container_workspace")
    if not isinstance(workspace, str) or not workspace.startswith("/"):
        raise ValueError("workspace must be an absolute host path")
    workspace_path = PurePosixPath(workspace)
    root = _approved_workspace_root()
    if ".." in workspace_path.parts or not workspace_path.is_relative_to(root):
        raise ValueError(f"workspace must be beneath {root}")
    if not isinstance(container_workspace, str) or not container_workspace.startswith("/"):
        raise ValueError("container_workspace must be an absolute path")
    if ".." in PurePosixPath(container_workspace).parts:
        raise ValueError("container_workspace must not escape its mount")
    if "," in workspace or "," in container_workspace:
        raise ValueError("workspace paths must not contain commas")
    cpus = payload.get("cpus")
    memory = payload.get("memory_gib")
    if type(cpus) is not int or cpus < 1 or type(memory) is not int or memory < 1:
        raise ValueError("LSP resource limits must be positive integers")
    network = payload.get("network")
    if network is not False:
        raise ValueError("LSP workers must not request network access")
    return LSPWorkerSpec(
        language=language,
        image=image,
        command=tuple(command),
        workspace=workspace,
        container_workspace=container_workspace,
        cpus=cpus,
        memory_gib=memory,
        network=network,
    )


def runtime_command(runtime: str, spec: LSPWorkerSpec, job_id: str | None = None) -> str:
    """Render a hardened per-job container command for a known runtime.

    The workspace resolves to four per-job mounts (read-only ``source`` plus
    ``build``/``cache``/``output``); see ``workspace_execution``.  ``job_id``
    defaults to the workspace directory name.
    """
    if runtime not in SUPPORTED_RUNTIMES:
        raise ValueError(f"unsupported LSP container runtime: {runtime}")
    try:
        from . import workspace_execution as workspace
    except ImportError:
        import workspace_execution as workspace
    if job_id is None:
        job_id = PurePosixPath(spec.workspace).name
    policy_flag: str | None = None
    if runtime == "nix-podman":
        policy_flag = podman_signature_policy_flag()
    elif runtime == "docker":
        # All docker runtimes must have DCT enabled.  There is no insecure
        # fallback.
        docker_content_trust_check()
    elif runtime == "podman-nvidia-cdi":
        # The nvidia-cdi runtime uses raw podman, which is also gated by
        # the deployment-owned signature policy (no insecure fallback).
        policy_flag = podman_signature_policy_flag()
    # Pin the workspace at the symlink-rendered layer.  This captures the
    # resolved inode and re-verifies it before mount, closing the
    # validation-to-mount symlink swap race.
    try:
        workspace_path = workspace.pin_workspace(spec.workspace)
    except workspace.WorkspaceError as exc:
        raise ValueError(str(exc)) from exc
    try:
        return workspace.render_runtime_command(
            runtime=runtime,
            image=spec.image,
            command=spec.command,
            cpus=spec.cpus,
            memory_gib=spec.memory_gib,
            workspace=spec.workspace,
            job_id=job_id,
            container_workspace=spec.container_workspace,
            signature_policy_flag=policy_flag,
            workspace_path=workspace_path,
        )
    except workspace.WorkspaceError as exc:
        raise ValueError(str(exc)) from exc
