#!/usr/bin/env python3
"""Validate a local-only deployment readiness manifest without contacting a node.

This is a review gate for ``harbor_deploy.sh`` inputs. It does not invoke SSH,
container tooling, security plugins, or read credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

try:
    from .image_provenance import ProvenanceError, load_canonical
    from .node_bootstrap_inventory import DEFAULT_NODES, bootstrap_inventory
    from .node_bootstrap_plan import render_bootstrap_plan
except ImportError:
    from image_provenance import ProvenanceError, load_canonical
    from node_bootstrap_inventory import DEFAULT_NODES, bootstrap_inventory
    from node_bootstrap_plan import render_bootstrap_plan


SCHEMA = "mathpunch.blitter-node-readiness-manifest.v1"
REPORT_SCHEMA = "mathpunch.blitter-node-readiness-report.v1"
ENVELOPE_SCHEMA = "mathpunch.blitter-computation-envelope.v2"
PROFILE_ID = "BLITTER-ISA-V1"
PROFILE_DIGEST = "sha256:cd60e19d6c3652e08d4ed36e3f8208de7f5d2c553cb7e8f5123175c63ff2f129"
TERMINAL_DIGEST = "sha256:5a9804978698791d50d937ba93b7f279344b2e0ea658f4e2ee7b2d5a3365fb7f"
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
SSH_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}=?$")


def _error(blockers: list[str], message: str) -> None:
    blockers.append(message)


def _policy_reference(value: Any, name: str, blockers: list[str]) -> dict[str, str | None]:
    if not isinstance(value, dict) or set(value) != {"path", "digest"}:
        _error(blockers, f"{name} must contain exactly path and digest")
        return {"path": None, "digest": None}
    path, digest = value["path"], value["digest"]
    if not isinstance(path, str) or not Path(path).is_absolute():
        _error(blockers, f"{name}.path must be an absolute local path")
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        _error(blockers, f"{name}.digest must be sha256:<64 lowercase hex>")
    if isinstance(path, str) and Path(path).is_absolute() and isinstance(digest, str) and SHA256.fullmatch(digest):
        try:
            actual = "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
            if actual != digest:
                _error(blockers, f"{name}.path content does not match {name}.digest")
        except OSError as exc:
            _error(blockers, f"{name}.path is not a readable local file: {exc}")
    return {"path": path if isinstance(path, str) else None, "digest": digest if isinstance(digest, str) else None}


def _envelope_policy(value: Any, blockers: list[str]) -> dict[str, Any]:
    fields = {"envelope_schema", "profile_id", "profile_digest", "terminal_contract_digest", "assembler_module_digest", "max_cpus", "max_memory_mib", "max_wall_ms"}
    if not isinstance(value, dict) or set(value) != fields:
        _error(blockers, "envelope_policy must contain exactly the canonical v2 policy values")
        return {}
    expected = {
        "envelope_schema": ENVELOPE_SCHEMA,
        "profile_id": PROFILE_ID,
        "profile_digest": PROFILE_DIGEST,
        "terminal_contract_digest": TERMINAL_DIGEST,
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            _error(blockers, f"envelope_policy.{key} must equal the deployed canonical v2 binding")
    if not isinstance(value["assembler_module_digest"], str) or SHA256.fullmatch(value["assembler_module_digest"]) is None:
        _error(blockers, "envelope_policy.assembler_module_digest must be sha256:<64 lowercase hex>")
    for key in ("max_cpus", "max_memory_mib", "max_wall_ms"):
        if isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] <= 0:
            _error(blockers, f"envelope_policy.{key} must be a positive integer")
    return value


def readiness_report(manifest: dict[str, Any], nodes: dict[str, Any]) -> dict[str, Any]:
    """Return a reviewable report. Any absent or inconsistent binding blocks readiness."""
    blockers: list[str] = []
    required = {"schema", "node", "expected_ssh_host_fingerprint", "provenance_path", "runtime_image_trust_policy", "published_gate_access_policy", "envelope_policy"}
    if not isinstance(manifest, dict) or set(manifest) != required:
        return {"schema": REPORT_SCHEMA, "ready": False, "blockers": ["manifest must contain exactly the readiness v1 fields"]}
    if manifest["schema"] != SCHEMA:
        _error(blockers, "manifest schema is not supported")
    node_name = manifest["node"]
    if not isinstance(node_name, str) or not node_name:
        _error(blockers, "node must be a non-empty active node name")
    fingerprint = manifest["expected_ssh_host_fingerprint"]
    if not isinstance(fingerprint, str) or SSH_FINGERPRINT.fullmatch(fingerprint) is None:
        _error(blockers, "expected_ssh_host_fingerprint must be an explicit SHA256 SSH host-key fingerprint")
    inventory = bootstrap_inventory(nodes, environ={})
    plan = render_bootstrap_plan(nodes, environ={})
    inventory_node = next((item for item in inventory["nodes"] if item["name"] == node_name), None)
    plan_node = next((item for item in plan["plans"] if item["name"] == node_name), None)
    if inventory_node is None:
        _error(blockers, "node is absent from the active bootstrap inventory")
    elif not inventory_node["deployment_eligible"]:
        _error(blockers, "node is not deployment-eligible in the active bootstrap inventory")
    if plan_node is None:
        _error(blockers, "node has no active bootstrap plan")
    elif plan_node["runtime"] != "docker":
        _error(blockers, "node runtime is not docker, required by the local harbor deployment contract")
    if inventory_node and plan_node:
        runner = inventory_node["ssh"]["resolved_user"]
        if not runner or runner == "root" or runner != plan_node["runner_user"]:
            _error(blockers, "active inventory and plan must agree on a dedicated non-root runner_user")
    runtime_policy = _policy_reference(manifest["runtime_image_trust_policy"], "runtime_image_trust_policy", blockers)
    gate_policy = _policy_reference(manifest["published_gate_access_policy"], "published_gate_access_policy", blockers)
    envelope = _envelope_policy(manifest["envelope_policy"], blockers)
    provenance: dict[str, Any] = {}
    provenance_path = manifest["provenance_path"]
    if not isinstance(provenance_path, str) or not Path(provenance_path).is_absolute():
        _error(blockers, "provenance_path must be an absolute local path")
    else:
        try:
            _, provenance = load_canonical(provenance_path, expected_profile_digest=PROFILE_DIGEST)
            if not provenance["stamp_requested"]:
                _error(blockers, "provenance must declare a requested secure stamp")
        except (OSError, ProvenanceError) as exc:
            _error(blockers, f"provenance_path is invalid: {exc}")
    return {
        "schema": REPORT_SCHEMA,
        "local_only": True,
        "ready": not blockers,
        "node": node_name,
        "address": inventory_node["address"] if inventory_node else None,
        "runner_user": inventory_node["ssh"]["resolved_user"] if inventory_node else None,
        "runtime": plan_node["runtime"] if plan_node else None,
        "expected_ssh_host_fingerprint": fingerprint,
        "immutable_image": provenance.get("immutable_ref"),
        "image_provenance_registry_digest": provenance.get("registry_digest"),
        "runtime_image_trust_policy": runtime_policy,
        "published_gate_access_policy": gate_policy,
        "envelope_policy": envelope,
        "blockers": blockers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--nodes", type=Path, default=DEFAULT_NODES)
    parser.add_argument("--output", type=Path, help="write the local readiness report")
    args = parser.parse_args()
    try:
        report = readiness_report(json.loads(args.manifest.read_text(encoding="utf-8")), json.loads(args.nodes.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        report = {"schema": REPORT_SCHEMA, "ready": False, "blockers": [f"local input is unreadable or invalid: {exc}"]}
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
