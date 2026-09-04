#!/usr/bin/env python3
"""Validate the active node bootstrap contract without contacting a node."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_NODES = ROOT / "scripts" / "cluster" / "nodes.json"
ENV_USER = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
RUNTIME_REQUIREMENTS = {
    "docker": (["ssh", "docker", "docker-content-trust"], ["oci-container", "signed-image"]),
    "nix-podman": (["ssh", "nix", "podman", "podman-signature-policy"], ["oci-container", "signed-image"]),
    "podman-nvidia-cdi": (["ssh", "podman", "nvidia-smi", "nvidia-cdi"], ["oci-container", "nvidia-gpu"]),
    "bare": (["ssh", "host-command"], []),
}


def resolve_ssh_user(expected_user: str, environ: dict[str, str]) -> tuple[str | None, str | None]:
    """Return a user and its variable name; unresolved substitutions fail closed."""
    match = ENV_USER.fullmatch(expected_user)
    if match:
        variable = match.group(1)
        user = environ.get(variable, "")
        return (user if user and "$" not in user else None), variable
    if "$" in expected_user or not expected_user:
        return None, None
    return expected_user, None


def bootstrap_inventory(source: dict[str, Any], environ: dict[str, str] | None = None) -> dict[str, Any]:
    if source.get("version") != 1 or not isinstance(source.get("nodes"), list):
        raise ValueError("nodes.json must be version 1 with a nodes array")
    environ = dict(os.environ if environ is None else environ)
    names: set[str] = set()
    nodes = []
    for item in source["nodes"]:
        name = item.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise ValueError(f"invalid or duplicate node name: {name!r}")
        names.add(name)
        expected_user = item.get("runner_user")
        runner_user_missing = not isinstance(expected_user, str)
        if runner_user_missing:
            expected_user = ""
        resolved_user, variable = resolve_ssh_user(expected_user, environ)
        runtime = item.get("runtime")
        if not isinstance(runtime, str):
            raise ValueError(f"node {name}: runtime must be a string")
        runner, container = RUNTIME_REQUIREMENTS.get(runtime, ([], []))
        blockers = []
        if runner_user_missing:
            blockers.append("runner_user must name a dedicated non-root account")
        if resolved_user is None:
            blockers.append("runner user environment expansion is unresolved")
        elif resolved_user == "root":
            blockers.append("runner_user must name a dedicated non-root account")
        if runtime == "auto":
            blockers.append("runtime=auto is not an accepted bootstrap runtime")
        elif runtime == "bare":
            blockers.append("runtime=bare has no container bootstrap contract")
        elif runtime not in RUNTIME_REQUIREMENTS:
            blockers.append(f"runtime={runtime} is not an accepted bootstrap runtime")
        port = item.get("webgpu_port")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            port = None
            blockers.append("WebGPU port is missing or invalid")
        nodes.append({
            "name": name,
            "address": item.get("address"),
            "ssh": {
                "expected_user": expected_user,
                "resolved_user": resolved_user,
                **({"user_environment_variable": variable} if variable else {}),
            },
            "role": item.get("role"),
            "runtime": runtime,
            "arch": item.get("arch"),
            "webgpu_port": port,
            "required_runner_capabilities": runner,
            "required_container_capabilities": container,
            "deployment_eligible": not blockers,
            "blockers": blockers,
        })
    return {"schema": "mathpunch.blitter-node-bootstrap-inventory.v1", "source": "scripts/cluster/nodes.json", "nodes": nodes}


def validation_errors(inventory: dict[str, Any]) -> list[str]:
    errors = []
    for node in inventory["nodes"]:
        if node["deployment_eligible"] != (not node["blockers"]):
            errors.append(f"{node['name']}: eligibility must match blockers")
        if node["runtime"] == "auto" and not any("runtime=auto" in blocker for blocker in node["blockers"]):
            errors.append(f"{node['name']}: runtime=auto must be rejected")
        ssh = node["ssh"]
        if ssh.get("user_environment_variable") and not ssh["resolved_user"] and not any(
            "runner user environment expansion is unresolved" in blocker
            for blocker in node["blockers"]
        ):
            errors.append(f"{node['name']}: unresolved runner user must be rejected")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=Path, default=DEFAULT_NODES)
    parser.add_argument("--output", type=Path, help="write the derived inventory locally")
    args = parser.parse_args()
    inventory = bootstrap_inventory(json.loads(args.nodes.read_text(encoding="utf-8")))
    errors = validation_errors(inventory)
    if args.output:
        args.output.write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(inventory, indent=2))
    for node in inventory["nodes"]:
        for blocker in node["blockers"]:
            print(f"REJECT {node['name']}: {blocker}")
    return 1 if errors or any(node["blockers"] for node in inventory["nodes"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
