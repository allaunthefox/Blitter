#!/usr/bin/env python3
"""Render local-only, runtime-specific node bootstrap requirements.

This renderer does not open SSH connections, execute commands, or read secrets.
It derives the active inventory from nodes.json instead of accepting archived
inventory output as an execution authority.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from .node_bootstrap_inventory import DEFAULT_NODES, bootstrap_inventory
except ImportError:
    from node_bootstrap_inventory import DEFAULT_NODES, bootstrap_inventory


SUPPORTED_RUNTIMES = {"docker", "nix-podman", "podman-nvidia-cdi"}
COMMON_COMMANDS = [
    "tailscale status --json",
    "tailscale set --ssh",
    "sshd -T | grep -Ex 'passwordauthentication no'",
    "sshd -T | grep -Ex 'permitrootlogin no'",
    "test \"$(id -u)\" -ne 0",
    "loginctl enable-linger \"$(id -un)\"",
]
MECHANICS_REQUIREMENTS = {
    "runner_user": "Use a dedicated non-root runner account; do not use root or password SSH.",
    "tailscale_ssh": "Tailnet ACL/grant policy must allow only the reviewed operator identity to SSH to the dedicated runner account. Do not expose public SSH ingress.",
    "ssh_host_identity": "Pin the target SSH host key in the operator SSH configuration before any operator-run bootstrap session.",
    "runner_service": "Run the mechanics runner as the dedicated account with a minimal environment; keep image-signature policy paths deployment-owned and outside repository configuration.",
    "network": "Mechanics containers use --network=none; no host network, PID, IPC, or UTS namespaces.",
    "privileges": "Use --user <non-root-uid>, --cap-drop=ALL, --security-opt=no-new-privileges, and a read-only root filesystem.",
    "images": "Allow only pre-approved digest-pinned images; use --pull=never and runtime signature verification.",
    "mounts": "Mount only a reviewed source tree read-only and per-job build/cache/output directories; never mount host sockets or the container runtime socket.",
    "limits": "Set explicit CPU, memory, PID, and writable tmpfs/output limits for every mechanics runner invocation.",
    "mcp_server": "Deploy the Blitter MCP assembler as a host-level systemd service on port 8791, reachable by new nodes for envelope assembly. Run as forgejo-runner with strict systemd sandboxing.",
}
RUNTIME_DETAILS = {
    "docker": {
        "commands": [
            "docker info --format '{{json .SecurityOptions}}' | grep -F rootless",
            "test \"${DOCKER_CONTENT_TRUST:-}\" = 1",
        ],
        "requirements": {
            "runtime": "Docker rootless mode must be available to the dedicated runner account.",
            "image_trust": "Set DOCKER_CONTENT_TRUST=1 in the runner service environment; do not put credentials in this plan.",
            "devices": "No GPU or host devices may be passed to mechanics containers.",
        },
    },
    "nix-podman": {
        "commands": [
            "nix --version",
            "podman info --format '{{.Host.Security.Rootless}}' | grep -Ex 'true'",
            "test -n \"${BLITTER_PODMAN_SIGNATURE_POLICY:-}\" && test -r \"${BLITTER_PODMAN_SIGNATURE_POLICY}\"",
        ],
        "requirements": {
            "runtime": "Nix must provide the reviewed Podman version; Podman must run rootless for the dedicated runner account.",
            "image_trust": "BLITTER_PODMAN_SIGNATURE_POLICY must name a readable deployment-owned policy that rejects unsigned images.",
            "devices": "No GPU or host devices may be passed to mechanics containers.",
        },
    },
    "podman-nvidia-cdi": {
        "commands": [
            "podman info --format '{{.Host.Security.Rootless}}' | grep -Ex 'true'",
            "nvidia-smi",
            "nvidia-ctk cdi list",
            "test -n \"${BLITTER_PODMAN_SIGNATURE_POLICY:-}\" && test -r \"${BLITTER_PODMAN_SIGNATURE_POLICY}\"",
        ],
        "requirements": {
            "runtime": "Podman must run rootless for the dedicated runner account.",
            "image_trust": "BLITTER_PODMAN_SIGNATURE_POLICY must name a readable deployment-owned policy that rejects unsigned images.",
            "devices": "NVIDIA driver health and a current CDI specification are required. Grant only the reviewed CDI device selector required by a GPU job; mechanics runners receive no devices.",
        },
    },
}


def remediation(runtime: str) -> str:
    if runtime == "auto":
        return "Declare docker, nix-podman, or podman-nvidia-cdi explicitly after local operator review; runtime detection is not supported."
    if runtime == "bare":
        return "Migrate this node to docker, nix-podman, or podman-nvidia-cdi with a reviewed container contract; bare host execution is not supported."
    return "Correct the active nodes.json declaration and regenerate this local plan."


def render_bootstrap_plan(source: dict[str, Any], environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Render review requirements from active source without contacting a node."""
    inventory = bootstrap_inventory(source, environ=environ)
    plans = []
    blockers = []
    for node in inventory["nodes"]:
        node_blockers = list(node["blockers"])
        runtime = node["runtime"]
        if runtime not in SUPPORTED_RUNTIMES:
            node_blockers.append(f"runtime={runtime} has no bootstrap plan")
        if runtime == "podman-nvidia-cdi" and node["role"] != "gpu":
            node_blockers.append("podman-nvidia-cdi requires role=gpu")
        if runtime in {"docker", "nix-podman"} and node["role"] == "gpu":
            node_blockers.append(f"runtime={runtime} has no declared GPU device contract")
        if node_blockers:
            blockers.append({
                "name": node["name"],
                "runtime": runtime,
                "blockers": node_blockers,
                "remediation": remediation(runtime),
            })
            continue
        details = RUNTIME_DETAILS[runtime]
        plans.append({
            "name": node["name"],
            "address": node["address"],
        "runner_user": node["ssh"]["resolved_user"],
            "runtime": runtime,
            "role": node["role"],
            "commands": COMMON_COMMANDS + details["commands"],
            "requirements": {**MECHANICS_REQUIREMENTS, **details["requirements"]},
        })
    return {
        "schema": "mathpunch.blitter-node-bootstrap-plan.v1",
        "source": "scripts/cluster/nodes.json",
        "local_only": True,
        "plans": plans,
        "blockers": blockers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=Path, default=DEFAULT_NODES,
                        help="active nodes.json source; derived inventories are intentionally not accepted")
    parser.add_argument("--output", type=Path, help="write the local review plan")
    args = parser.parse_args()
    plan = render_bootstrap_plan(json.loads(args.nodes.read_text(encoding="utf-8")))
    rendered = json.dumps(plan, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    for blocker in plan["blockers"]:
        print(f"REJECT {blocker['name']}: {'; '.join(blocker['blockers'])}")
        print(f"REMEDIATE {blocker['name']}: {blocker['remediation']}")
    return 1 if plan["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
