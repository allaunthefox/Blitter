#!/usr/bin/env python3
"""Fail-closed Tailscale cluster inventory and container dispatcher."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from .lsp_workers import runtime_command as lsp_runtime_command
    from .lsp_workers import worker_from_mapping
    from .lsp_workers import podman_signature_policy_flag
    from .lsp_transport import LeaseHooks, TransportConfig, run_lsp_transport
    from .lsp_admission import LSPWorkSpec, admit_lsp_work, presence_from_mapping
    from .image_catalog import validate_catalog
    from .image_trust import verify_image_trust
    from .lsp_lease import LSPLeaseClient
    from . import computation_blob
except ImportError:
    from lsp_workers import runtime_command as lsp_runtime_command
    from lsp_workers import worker_from_mapping
    from lsp_workers import podman_signature_policy_flag
    from lsp_transport import LeaseHooks, TransportConfig, run_lsp_transport
    from lsp_admission import LSPWorkSpec, admit_lsp_work, presence_from_mapping
    from image_catalog import validate_catalog
    from image_trust import verify_image_trust
    from lsp_lease import LSPLeaseClient
    import computation_blob

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "scripts" / "cluster" / "nodes.json"


@dataclass(frozen=True)
class Node:
    name: str
    address: str
    ssh_user: str
    role: str
    runtime: str
    max_cpus: int
    max_memory_gib: int
    ephemeral: bool
    gpu: str | None = None
    webgpu_port: int | None = None

    @property
    def destination(self) -> str:
        user = os.path.expandvars(self.ssh_user)
        if not user or "$" in user:
            raise ValueError(f"node {self.name}: SSH user is not configured")
        return f"{user}@{self.address}"


def load_nodes(path: Path) -> dict[str, Node]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("version") != 1:
        raise ValueError("unsupported cluster configuration version")
    nodes: dict[str, Node] = {}
    for item in raw.get("nodes", []):
        node = Node(
            name=item["name"], address=item["address"], ssh_user=item["ssh_user"],
            role=item["role"], runtime=item["runtime"], max_cpus=int(item["max_cpus"]),
            max_memory_gib=int(item["max_memory_gib"]), ephemeral=bool(item["ephemeral"]),
            gpu=item.get("gpu"),
            webgpu_port=(int(item["webgpu_port"]) if item.get("webgpu_port") is not None else None),
        )
        if node.name in nodes:
            raise ValueError(f"duplicate node: {node.name}")
        if node.max_cpus < 1 or node.max_memory_gib < 1:
            raise ValueError(f"node {node.name}: invalid resource cap")
        nodes[node.name] = node
    return nodes


def ssh(node: Node, remote_command: str, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", node.destination, remote_command],
        check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
    )


def runtime_command(node: Node, image: str, command: list[str], cpus: int, memory_gib: int) -> str:
    quoted_command = " ".join(shlex.quote(part) for part in command)
    image_q = shlex.quote(image)
    if node.runtime == "docker":
        # Docker Content Trust is mandatory.  Without it, the local image
        # is not cryptographically authorized and must not be launched.
        from lsp_workers import docker_content_trust_check
        docker_content_trust_check()
        return f"docker run --rm --cpus={cpus} --memory={memory_gib}g {image_q} {quoted_command}"
    if node.runtime == "podman-nvidia-cdi":
        return (
            "command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null && "
            f"podman run --rm --device nvidia.com/gpu=all --cpus={cpus} --memory={memory_gib}g {image_q} {quoted_command}"
        )
    if node.runtime == "nix-podman":
        # Deployment-owned signature policy only; no insecure fallback.
        return (
            "nix shell nixpkgs#podman --command podman run "
            f"{podman_signature_policy_flag()} --rm "
            f"--cpus={cpus} --memory={memory_gib}g {image_q} {quoted_command}"
        )
    raise ValueError(f"node {node.name}: unsupported runtime {node.runtime}")


def cmd_inventory(nodes: dict[str, Node]) -> int:
    failed = False
    for node in nodes.values():
        try:
            result = ssh(node, "hostname; nproc; awk '/MemTotal/{print $2}' /proc/meminfo; command -v nvidia-smi || true")
        except (ValueError, subprocess.TimeoutExpired) as exc:
            print(f"{node.name}: unavailable ({exc})")
            failed = True
            continue
        status = "ready" if result.returncode == 0 else "unavailable"
        print(f"{node.name}: {status} role={node.role} ephemeral={str(node.ephemeral).lower()}")
        if result.stdout:
            print("  " + " | ".join(result.stdout.strip().splitlines()))
        if result.returncode != 0:
            print(f"  {result.stderr.strip()}")
            failed = True
    return 1 if failed else 0


def cmd_run(node: Node, args: argparse.Namespace) -> int:
    if node.ephemeral and not args.allow_ephemeral:
        raise ValueError(f"node {node.name} is ephemeral; pass --allow-ephemeral explicitly")
    if args.gpu and node.role != "gpu":
        raise ValueError(f"node {node.name} is not configured as a GPU node")
    cpus = args.cpus or node.max_cpus
    memory = args.memory_gib or node.max_memory_gib
    if cpus < 1 or cpus > node.max_cpus:
        raise ValueError(f"CPU request exceeds {node.name} cap ({node.max_cpus})")
    if memory < 1 or memory > node.max_memory_gib:
        raise ValueError(f"memory request exceeds {node.name} cap ({node.max_memory_gib} GiB)")
    # Catalog + policy are MANDATORY.  cmd_run previously accepted any
    # digest-pinned image, which is the security regression: only an image
    # whose digest is in the deployment-owned approved list AND whose
    # catalog signature/provenance passes verify_image_trust may be launched.
    if not getattr(args, "catalog", None):
        raise ValueError(
            "run dispatch requires --catalog (the deployment-owned signed "
            "image catalog). Bare digest-pinned images are no longer accepted."
        )
    if not getattr(args, "catalog_policy", None):
        raise ValueError("--catalog requires --catalog-policy")
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    policy = json.loads(args.catalog_policy.read_text(encoding="utf-8"))
    if not isinstance(policy, dict):
        raise ValueError("catalog policy must be a JSON object")
    if "approved_keys" not in policy:
        raise ValueError(
            "catalog policy is missing 'approved_keys'; only signed catalogs "
            "are accepted by the dispatcher."
        )
    if not policy.get("approved_images"):
        raise ValueError("catalog policy is missing 'approved_images' allowlist")
    verify_image_trust(catalog, policy)
    # Look up the image in the catalog by digest.  The catalog is the only
    # source of truth for which images are allowed.
    image_digest = args.image.rsplit("@", 1)[-1] if "@" in args.image else args.image
    if not any(
        entry.get("image", "").rsplit("@", 1)[-1] == image_digest
        for entry in catalog.get("images", {}).values()
    ):
        raise ValueError(
            f"image {args.image!r} is not in the approved catalog; refusing to run"
        )
    remote = runtime_command(node, args.image, args.command, cpus, memory)
    result = ssh(node, remote, timeout=args.timeout)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def cmd_lsp(node: Node, args: argparse.Namespace) -> int:
    """Run one validated language-server container over SSH."""
    if node.ephemeral and not args.allow_ephemeral:
        raise ValueError(f"node {node.name} is ephemeral; pass --allow-ephemeral explicitly")
    descriptor_payload = json.loads(args.descriptor.read_text(encoding="utf-8"))
    spec = worker_from_mapping(descriptor_payload)
    if spec.cpus > node.max_cpus:
        raise ValueError(f"CPU request exceeds {node.name} cap ({node.max_cpus})")
    if spec.memory_gib > node.max_memory_gib:
        raise ValueError(f"memory request exceeds {node.name} cap ({node.max_memory_gib} GiB)")
    # Catalog + policy are MANDATORY.  Without them, the dispatcher would
    # accept any syntactically digest-pinned image, which is the security
    # regression: only an image whose digest is in the deployment-owned
    # approved list AND whose catalog signature/provenance passes verify_image_trust
    # may be launched.
    catalog_path = getattr(args, "catalog", None)
    if catalog_path is None:
        raise ValueError(
            "LSP dispatch requires --catalog (the deployment-owned signed "
            "image catalog). Bare digest-pinned images are no longer accepted; "
            "the catalog and policy are the trust roots."
        )
    policy_path = getattr(args, "catalog_policy", None)
    if policy_path is None:
        raise ValueError(
            "--catalog requires --catalog-policy (deployment-owned approved "
            "keys/images/sources/provenance allowlist). Without a policy the "
            "trust verification is refused."
        )
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    if not isinstance(policy, dict):
        raise ValueError("catalog policy must be a JSON object")
    # Always run the cryptographic verification path when approved_keys is
    # present.  The legacy "approved_images only" path that skips signature
    # verification is no longer accepted at this layer; deployments must
    # publish a signed catalog.
    if "approved_keys" not in policy:
        raise ValueError(
            "catalog policy is missing 'approved_keys'; only signed catalogs "
            "are accepted by the dispatcher. Use sign_lsp_catalog.py to sign "
            "the catalog and place the verification key into the policy."
        )
    if not policy.get("approved_images"):
        raise ValueError(
            "catalog policy is missing 'approved_images'; a non-empty allowlist "
            "is required to bind the approved set of image digests."
        )
    # Deployment-owned trust roots: verify signature, provenance
    # allowlist, and SBOM bindings, not just catalog shape.
    verify_image_trust(catalog, policy)
    entry = catalog["images"][spec.language]
    if entry["image"] != spec.image or tuple(entry["argv"]) != spec.command:
        raise ValueError("LSP descriptor does not match catalog language entry")
    remote = lsp_runtime_command(node.runtime, spec)
    if getattr(args, "interactive", False):
        presence_path = getattr(args, "presence", None)
        if presence_path is None:
            raise ValueError("interactive LSP requires --presence")
        now_unix_ms = int(__import__("time").time() * 1000)
        presence = presence_from_mapping(
            json.loads(presence_path.read_text(encoding="utf-8")),
            now_unix_ms=now_unix_ms,
        )
        decision = admit_lsp_work(
            LSPWorkSpec(
                goal_id="dispatch",
                work_id=f"lsp:{spec.language}",
                language=spec.language,
                required_capabilities=frozenset({"lsp", f"language:{spec.language}"}),
                cpus=spec.cpus,
                memory_gib=spec.memory_gib,
                lease_ttl_ms=int(args.total_timeout * 1000),
            ),
            presence,
            now_unix_ms=now_unix_ms,
        )
        if not decision.admitted or decision.node_id != node.name:
            raise ValueError(f"LSP presence does not admit node {node.name}")
        if args.lease_url is None:
            raise ValueError("interactive LSP requires --lease-url")
        image_digest = spec.image.rsplit("@", 1)[-1]
        lease_client = LSPLeaseClient(
            args.lease_url,
            goal_id="dispatch",
            work_id=f"lsp:{spec.language}",
            worker_id=node.name,
            worker_generation=args.worker_generation,
            descriptor_digest="sha256:" + hashlib.sha256(
                json.dumps(descriptor_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            image_digest=image_digest,
            holder=args.lease_holder,
            ttl_ms=args.lease_ttl_ms,
            auth_token=os.environ.get("BLITTER_LSP_LEASE_TOKEN", ""),
        )
        return run_lsp_transport(
            node.destination,
            "exec " + remote,
            config=TransportConfig(
                startup_timeout=args.startup_timeout,
                idle_timeout=args.idle_timeout,
                total_timeout=args.total_timeout,
                lease_renew_interval=args.lease_ttl_ms / 3000,
            ),
            lease_hooks=LeaseHooks(
                acquire=lease_client.acquire,
                release=lease_client.release,
                renew=lease_client.renew,
                begin=lease_client.session_begin,
                commit=lease_client.session_commit,
            ),
        )
    result = ssh(node, remote, timeout=args.timeout)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("inventory")
    run = sub.add_parser("run")
    run.add_argument("node")
    run.add_argument("--image", required=True)
    run.add_argument("--cpus", type=int)
    run.add_argument("--memory-gib", type=int)
    run.add_argument("--timeout", type=int, default=1800)
    run.add_argument("--gpu", action="store_true")
    run.add_argument("--allow-ephemeral", action="store_true")
    run.add_argument("--catalog", type=Path, required=True,
                     help="REQUIRED deployment-owned signed image catalog")
    run.add_argument("--catalog-policy", type=Path, required=True,
                     help="REQUIRED deployment-owned policy (approved keys/images/sources)")
    run.add_argument("command", nargs=argparse.REMAINDER)
    lsp = sub.add_parser("lsp", help="run a validated containerized language server")
    lsp.add_argument("node")
    lsp.add_argument("--descriptor", type=Path, required=True)
    lsp.add_argument("--catalog", type=Path, required=True,
                     help="REQUIRED six-language catalog used to check descriptor drift")
    lsp.add_argument("--catalog-policy", type=Path, required=True,
                     help="REQUIRED deployment-owned approved keys/images/sources/provenance for --catalog")
    lsp.add_argument("--timeout", type=int, default=1800)
    lsp.add_argument("--interactive", action="store_true",
                     help="stream LSP stdin/stdout over SSH instead of buffering")
    lsp.add_argument("--startup-timeout", type=float, default=15.0)
    lsp.add_argument("--idle-timeout", type=float, default=300.0)
    lsp.add_argument("--total-timeout", type=float, default=1800.0)
    lsp.add_argument("--presence", type=Path,
                     help="fresh JSON LSP presence record; required for --interactive")
    lsp.add_argument("--lease-url", type=str,
                     help="target LSP lease endpoint; required for --interactive")
    lsp.add_argument("--lease-ttl-ms", type=int, default=30000)
    lsp.add_argument("--lease-auth-token", type=str, default="",
                     help="bearer token for the lease endpoint (or BLITTER_LSP_LEASE_TOKEN env)")
    lsp.add_argument("--lease-holder", default="cluster-dispatch")
    lsp.add_argument("--worker-generation", type=int, default=0,
                     help="worker generation identity announced by announce_presence.py")
    lsp.add_argument("--allow-ephemeral", action="store_true")
    wg = sub.add_parser("webgpu")
    wg.add_argument("node")
    wg.add_argument("--health", action="store_true")
    wg.add_argument("--status", action="store_true",
                    help="report daemon/gate occupancy without submitting compute")
    wg.add_argument("--lease-ttl-ms", type=int,
                    help="authoritative target-side lease TTL; required for envelope execution")
    wg.add_argument("--lease-expected-ms", type=int,
                    help="optional advisory expected duration; distinct from authoritative TTL")
    wg.add_argument("--lease-holder",
                    help="lease holder label; default identifies this dispatcher process")
    wg.add_argument("--envelope-file", type=Path,
                    help="canonical computation envelope bytes; required for fenced /compute-envelope execution")
    wg.add_argument("--timeout", type=int, default=120)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        nodes = load_nodes(args.config)
        if args.action == "inventory":
            return cmd_inventory(nodes)
        if args.action == "webgpu":
            return cmd_webgpu(nodes, args)
        if args.action == "lsp":
            return cmd_lsp(nodes[args.node], args)
        if not args.command:
            raise ValueError("container command is required after --")
        return cmd_run(nodes[args.node], args)
    except (KeyError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"cluster-dispatch: {exc}", file=sys.stderr)
        return 2


# =====================================================================
# webgpu role: submit Laurent-multiplication jobs through the node's
# blitter surface. Authority-bearing compute uses the target-side lease gate.
# An explicitly requested legacy unfenced mode remains observational only.
# =====================================================================

WEBGPU_DEFAULT_PORT = 8790
LEASE_STATUS_SCHEMA = "mathpunch.blitter-lease-status.v1"


def _webgpu_url(node: Node, path: str) -> str:
    port = node.webgpu_port or WEBGPU_DEFAULT_PORT
    return f"http://{node.address}:{port}{path}"


def webgpu_get_json(node: Node, path: str, timeout: int = 120) -> dict:
    """GET one blitter/gate JSON endpoint and fail closed on protocol errors."""
    import urllib.error
    import urllib.request

    url = _webgpu_url(node, path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"webgpu GET {url}: HTTP {resp.status}")
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", "replace")
        raise RuntimeError(f"webgpu GET {url}: HTTP {exc.code}: {detail}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"webgpu GET {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"webgpu GET {url}: response is not a JSON object")
    return payload


def webgpu_post_json(
    node: Node,
    path: str,
    payload: dict,
    *,
    timeout: int = 120,
    headers: dict[str, str] | None = None,
) -> dict:
    import urllib.error
    import urllib.request

    url = _webgpu_url(node, path)
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=request_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            result = json.loads(data.decode("utf-8"))
            if resp.status != 200:
                raise RuntimeError(f"webgpu POST {url}: HTTP {resp.status}: {result}")
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", "replace")
        raise RuntimeError(f"webgpu POST {url}: HTTP {exc.code}: {detail}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"webgpu POST {url}: {exc}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"webgpu POST {url}: response is not a JSON object")
    return result


def webgpu_post_canonical(
    node: Node,
    path: str,
    body: bytes,
    *,
    timeout: int = 120,
    headers: dict[str, str] | None = None,
) -> tuple[dict, bytes]:
    """POST exact canonical bytes without letting a JSON encoder alter the wire form."""
    import urllib.error
    import urllib.request

    if not isinstance(body, bytes):
        raise ValueError("canonical request body must be bytes")
    url = _webgpu_url(node, path)
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=body, headers=request_headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if response.status != 200:
                raise RuntimeError(f"webgpu POST {url}: HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", "replace")
        raise RuntimeError(f"webgpu POST {url}: HTTP {exc.code}: {detail}") from exc
    except OSError as exc:
        raise RuntimeError(f"webgpu POST {url}: {exc}") from exc
    try:
        result = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"webgpu POST {url}: response is not ASCII JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"webgpu POST {url}: response is not a JSON object")
    return result, raw


def webgpu_submit_envelope(
    node: Node, raw: bytes, timeout: int = 120, *, lease: dict | None = None
) -> dict:
    """Submit one validated envelope and require the exact canonical result bytes."""
    envelope = computation_blob.parse_canonical(raw)
    headers = None
    if lease is not None:
        headers = {
            "X-Blitter-Lease-Instance": lease["instance_id"],
            "X-Blitter-Lease-Epoch": str(lease["lease_epoch"]),
            "X-Blitter-Lease-Token": lease["lease_token"],
        }
    result, response_raw = webgpu_post_canonical(
        node, "/compute-envelope", raw, timeout=timeout, headers=headers
    )
    expected = computation_blob.add_mod_result(envelope)
    if result != expected or response_raw != computation_blob.canonical_bytes(expected):
        raise RuntimeError("webgpu envelope result is not canonical or does not match the exact result")
    return result


def webgpu_submit_envelope_fenced(
    node: Node,
    raw: bytes,
    *,
    holder: str,
    ttl_ms: int,
    expected_ms: int | None,
    timeout: int,
) -> dict:
    lease = webgpu_acquire_lease(
        node, holder=holder, ttl_ms=ttl_ms, expected_ms=expected_ms, timeout=timeout
    )
    try:
        result = webgpu_submit_envelope(node, raw, timeout=timeout, lease=lease)
    except Exception:
        try:
            webgpu_release_lease(node, lease, timeout=timeout)
        except Exception:
            pass
        raise
    webgpu_release_lease(node, lease, timeout=timeout)
    return result


def webgpu_status(node: Node, timeout: int = 120) -> dict:
    """Return validated observational occupancy from daemon or lease gate.

    A lease-gate status treats an active reservation as busy even when no compute
    is currently in flight. Status is still observational and never grants a lease.
    """
    status = webgpu_get_json(node, "/status", timeout=timeout)
    if status.get("schema") == LEASE_STATUS_SCHEMA:
        required = {
            "ok": bool,
            "lease_active": bool,
            "lease_expired": bool,
            "in_flight": bool,
            "lease_epoch": int,
        }
        for key, expected_type in required.items():
            value = status.get(key)
            if type(value) is not expected_type:
                raise RuntimeError(f"webgpu lease /status: invalid {key!r}: {value!r}")
        if not status["ok"]:
            raise RuntimeError("webgpu lease /status reported ok=false")
        busy = status["lease_active"] or status["in_flight"]
        return {
            "ok": True,
            "busy": busy,
            "idle": not busy,
            "active_compute": 1 if status["in_flight"] else 0,
            "queued_compute": 0,
            "max_concurrent_compute": 1,
            "lease_gate": status,
        }

    required = {
        "ok": bool,
        "busy": bool,
        "idle": bool,
        "active_compute": int,
        "queued_compute": int,
        "max_concurrent_compute": int,
    }
    for key, expected_type in required.items():
        value = status.get(key)
        if type(value) is not expected_type:
            raise RuntimeError(f"webgpu /status: missing or invalid {key!r}: {value!r}")
    if not status["ok"]:
        raise RuntimeError("webgpu /status reported ok=false")
    if status["active_compute"] < 0 or status["queued_compute"] < 0:
        raise RuntimeError("webgpu /status reported negative occupancy")
    if status["max_concurrent_compute"] < 1:
        raise RuntimeError("webgpu /status reported invalid concurrency capacity")
    derived_busy = status["active_compute"] != 0 or status["queued_compute"] != 0
    if status["busy"] != derived_busy or status["idle"] == status["busy"]:
        raise RuntimeError("webgpu /status occupancy booleans are inconsistent with counters")
    return status


def webgpu_acquire_lease(
    node: Node,
    *,
    holder: str,
    ttl_ms: int,
    expected_ms: int | None = None,
    timeout: int = 120,
) -> dict:
    if not isinstance(ttl_ms, int) or isinstance(ttl_ms, bool) or ttl_ms <= 0:
        raise ValueError("--lease-ttl-ms must be a positive integer")
    if expected_ms is not None and (
        not isinstance(expected_ms, int) or isinstance(expected_ms, bool) or expected_ms < 0
    ):
        raise ValueError("--lease-expected-ms must be a non-negative integer")
    payload = {"holder": holder, "ttl_ms": ttl_ms}
    if expected_ms is not None:
        payload["expected_ms"] = expected_ms
    lease = webgpu_post_json(node, "/lease/acquire", payload, timeout=timeout)
    required = {
        "ok": bool,
        "instance_id": str,
        "lease_epoch": int,
        "lease_token": str,
    }
    for key, expected_type in required.items():
        value = lease.get(key)
        if type(value) is not expected_type or (expected_type is str and not value):
            raise RuntimeError(f"webgpu lease acquire: invalid {key!r}: {value!r}")
    if not lease["ok"] or lease["lease_epoch"] <= 0:
        raise RuntimeError("webgpu lease acquire returned invalid authority")
    return lease


def webgpu_release_lease(node: Node, lease: dict, timeout: int = 120) -> dict:
    result = webgpu_post_json(
        node,
        "/lease/release",
        {
            "instance_id": lease["instance_id"],
            "lease_epoch": lease["lease_epoch"],
            "lease_token": lease["lease_token"],
        },
        timeout=timeout,
    )
    if result.get("ok") is not True or result.get("released") is not True:
        raise RuntimeError("webgpu lease release did not confirm release")
    return result


def cmd_webgpu(nodes: dict[str, Node], args: argparse.Namespace) -> int:
    node = nodes[args.node]
    if args.health and args.status:
        raise ValueError("--health and --status are mutually exclusive")
    if args.health:
        health = webgpu_get_json(node, "/health", timeout=args.timeout)
        if not health.get("ok"):
            raise RuntimeError(f"webgpu({node.name}) health reported ok=false")
        print(json.dumps(health, sort_keys=True))
        return 0
    if args.status:
        print(json.dumps(webgpu_status(node, timeout=args.timeout), sort_keys=True))
        return 0

    if args.lease_expected_ms is not None and args.lease_ttl_ms is None:
        raise ValueError("--lease-expected-ms requires --lease-ttl-ms")
    if args.lease_ttl_ms is None:
        raise ValueError("webgpu envelope execution requires --lease-ttl-ms")
    if args.envelope_file is None:
        raise ValueError("webgpu execution requires --envelope-file")

    holder = args.lease_holder or f"cluster-dispatch:{socket.gethostname()}:{os.getpid()}"
    result = webgpu_submit_envelope_fenced(
        node,
        args.envelope_file.read_bytes(),
        holder=holder,
        ttl_ms=args.lease_ttl_ms,
        expected_ms=args.lease_expected_ms,
        timeout=args.timeout,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
