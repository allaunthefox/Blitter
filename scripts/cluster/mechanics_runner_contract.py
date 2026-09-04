#!/usr/bin/env python3
"""Render a local, secret-free mechanics-runner command proposal.

This module never invokes a container runtime or contacts an inventory node.
Its output is review material, not an execution request.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

try:
    from .node_bootstrap_inventory import DEFAULT_NODES, bootstrap_inventory
except ImportError:
    from node_bootstrap_inventory import DEFAULT_NODES, bootstrap_inventory


SCHEMA = "mathpunch.blitter-mechanics-runner-proposal.v1"
INVENTORY_SCHEMA = "mathpunch.blitter-node-bootstrap-inventory.v1"
IMMUTABLE_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
JOB_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
MAXIMUMS = {"cpus": 8, "memory_mib": 32768, "pids": 512, "nofile": 65536,
            "timeout_seconds": 3600, "scratch_mib": 1024}


class ContractError(ValueError):
    """The requested workload cannot be safely rendered."""


def _path(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise ContractError(f"{name} must be an absolute, NUL-free path")
    path = Path(value)
    if ".." in path.parts:
        raise ContractError(f"{name} must not contain parent traversal")
    return str(path)


def _positive_int(resources: dict[str, Any], name: str) -> int:
    value = resources.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAXIMUMS[name]:
        raise ContractError(f"resources.{name} must be an integer from 1 through {MAXIMUMS[name]}")
    return value


def _cpus(resources: dict[str, Any]) -> str:
    value = resources.get("cpus")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= MAXIMUMS["cpus"]:
        raise ContractError(f"resources.cpus must be greater than 0 and at most {MAXIMUMS['cpus']}")
    return str(value)


def render_mechanics_runner_proposal(inventory: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """Return an auditable argv/config proposal or fail closed before execution."""
    if inventory.get("schema") != INVENTORY_SCHEMA or not isinstance(inventory.get("nodes"), list):
        raise ContractError("a current bootstrap inventory is required")
    required = {"node", "job_id", "image", "argv", "source_dir", "result_dir", "runner_user", "runner_uid", "resources"}
    if set(request) != required:
        raise ContractError("request must contain exactly node, job_id, image, argv, source_dir, result_dir, runner_user, runner_uid, and resources")
    node_name = request["node"]
    node = next((item for item in inventory["nodes"] if item.get("name") == node_name), None)
    if node is None or not node.get("deployment_eligible"):
        raise ContractError("requested node is absent from, or ineligible in, the active inventory")
    if node.get("runtime") == "docker":
        runtime = "docker"
    elif node.get("runtime") == "nix-podman":
        runtime = "podman"
    else:
        raise ContractError(f"runtime={node.get('runtime')} is unsupported for mechanics runners")
    if node.get("role") == "gpu":
        raise ContractError("GPU nodes and device requests are not supported by mechanics runners")
    if request["runner_user"] != node.get("ssh", {}).get("resolved_user") or request["runner_user"] == "root":
        raise ContractError("runner_user must match the active inventory's dedicated non-root runner")
    if not isinstance(request["job_id"], str) or not JOB_ID.fullmatch(request["job_id"]):
        raise ContractError("job_id must be a short lowercase identifier")
    if not isinstance(request["image"], str) or not IMMUTABLE_IMAGE.fullmatch(request["image"]):
        raise ContractError("image must be digest-pinned as name@sha256:<64 lowercase hex characters>")
    if not isinstance(request["argv"], list) or not request["argv"] or not all(isinstance(arg, str) and arg and "\x00" not in arg for arg in request["argv"]):
        raise ContractError("argv must be a non-empty list of non-empty, NUL-free strings")
    uid = request["runner_uid"]
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        raise ContractError("runner_uid must be a non-root numeric UID")
    source_dir = _path(request["source_dir"], "source_dir")
    result_dir = _path(request["result_dir"], "result_dir")
    if source_dir == result_dir:
        raise ContractError("source_dir and result_dir must be isolated paths")
    resources = request["resources"]
    if not isinstance(resources, dict) or set(resources) != set(MAXIMUMS):
        raise ContractError("resources must declare exactly cpus, memory_mib, pids, nofile, timeout_seconds, and scratch_mib")
    cpus = _cpus(resources)
    memory_mib = _positive_int(resources, "memory_mib")
    pids = _positive_int(resources, "pids")
    nofile = _positive_int(resources, "nofile")
    timeout_seconds = _positive_int(resources, "timeout_seconds")
    scratch_mib = _positive_int(resources, "scratch_mib")
    name = f"blitter-mechanics-{request['job_id']}"
    scratch_dir = f"/var/tmp/blitter-mechanics/{request['job_id']}/scratch"
    run_argv = [
        "timeout", "--signal=TERM", "--kill-after=10s", f"{timeout_seconds}s", runtime, "run", "--rm",
        "--name", name, "--pull=never", "--user", f"{uid}:{uid}", "--network=none",
        "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
        "--pid=private", "--ipc=private", "--uts=private", "--cpus", cpus,
        "--memory", f"{memory_mib}m", "--pids-limit", str(pids), "--ulimit", f"nofile={nofile}:{nofile}",
        "--tmpfs", f"/scratch:rw,noexec,nosuid,nodev,size={scratch_mib}m",
        "--mount", f"type=bind,src={source_dir},dst=/workspace,readonly",
        "--mount", f"type=bind,src={result_dir},dst=/results",
        "--workdir", "/workspace", request["image"], *request["argv"],
    ]
    return {
        "schema": SCHEMA, "proposal_only": True, "inventory_schema": INVENTORY_SCHEMA,
        "node": node_name, "runtime": runtime, "image": request["image"],
        "security": {"network": "none", "root_filesystem": "read-only", "user": f"{uid}:{uid}",
                     "capabilities": "drop-all", "no_new_privileges": True,
                     "host_namespaces": "forbidden", "devices": "forbidden",
                     "socket_mounts": "forbidden", "credential_mounts": "forbidden"},
        "mounts": {"source": {"path": source_dir, "target": "/workspace", "read_only": True},
                   "results": {"path": result_dir, "target": "/results", "workload_access": "write-only",
                               "host_precondition": {"owner_uid": uid, "mode": "0300"}},
                   "scratch": {"path": "/scratch", "tmpfs": True, "size_mib": scratch_mib}},
        "limits": {"cpus": cpus, "memory_mib": memory_mib, "pids": pids, "nofile": nofile,
                   "timeout_seconds": timeout_seconds},
        "run_argv": run_argv,
        "cleanup_required": True,
        "cleanup_argv": [[runtime, "rm", "-f", name], ["rm", "-rf", scratch_dir]],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=Path, default=DEFAULT_NODES)
    parser.add_argument("--request", type=Path, required=True, help="local JSON job request; secrets are forbidden")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    inventory = bootstrap_inventory(json.loads(args.nodes.read_text(encoding="utf-8")))
    proposal = render_mechanics_runner_proposal(inventory, json.loads(args.request.read_text(encoding="utf-8")))
    rendered = json.dumps(proposal, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
