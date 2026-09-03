#!/usr/bin/env python3
"""Fail-closed Tailscale cluster inventory and container dispatcher."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

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
        return f"docker run --rm --cpus={cpus} --memory={memory_gib}g {image_q} {quoted_command}"
    if node.runtime == "podman-nvidia-cdi":
        return (
            "command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null && "
            f"podman run --rm --device nvidia.com/gpu=all --cpus={cpus} --memory={memory_gib}g {image_q} {quoted_command}"
        )
    if node.runtime == "nix-podman":
        return (
            "policy=$(mktemp) && trap 'rm -f \"$policy\"' EXIT && "
            "printf '%s\n' '{\"default\":[{\"type\":\"insecureAcceptAnything\"}]}' > \"$policy\" && "
            "nix shell nixpkgs#podman --command podman run --signature-policy=\"$policy\" --rm "
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
    remote = runtime_command(node, args.image, args.command, cpus, memory)
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
    run.add_argument("command", nargs=argparse.REMAINDER)
    wg = sub.add_parser("webgpu")
    wg.add_argument("node")
    wg.add_argument("--health", action="store_true")
    wg.add_argument("--status", action="store_true",
                    help="report daemon/gate occupancy without submitting compute")
    wg.add_argument("--require-idle", action="store_true",
                    help="legacy observational preflight; not an ownership fence")
    wg.add_argument("--lease-ttl-ms", type=int,
                    help="authoritative target-side lease TTL; default compute path requires this")
    wg.add_argument("--lease-expected-ms", type=int,
                    help="optional advisory expected duration; distinct from authoritative TTL")
    wg.add_argument("--lease-holder",
                    help="lease holder label; default identifies this dispatcher process")
    wg.add_argument("--allow-unfenced", action="store_true",
                    help="explicit legacy escape hatch; still requires live idle preflight and has a TOCTOU race")
    wg.add_argument("--job-file", type=Path)
    wg.add_argument("--batch-file", type=Path,
                    help='JSON: {"jobs": [{"a": [[qe,te,coef]...], "b": [...]}, ...]}')
    wg.add_argument("--timeout", type=int, default=120)
    wg.add_argument("--a", nargs="+", default=[],
                    help="A terms as qe,te,coef triples: --a 0,0,1 1,0,-1 ...")
    wg.add_argument("--b", nargs="+", default=[],
                    help="B terms as qe,te,coef triples")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        nodes = load_nodes(args.config)
        if args.action == "inventory":
            return cmd_inventory(nodes)
        if args.action == "webgpu":
            return cmd_webgpu(nodes, args)
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


def webgpu_require_idle(node: Node, timeout: int = 120) -> dict:
    status = webgpu_status(node, timeout=timeout)
    if status["busy"] or not status["idle"] or status["active_compute"] or status["queued_compute"]:
        raise RuntimeError(
            f"webgpu({node.name}) busy: active={status['active_compute']} "
            f"queued={status['queued_compute']}"
        )
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


def webgpu_submit(node: Node, job: dict, timeout: int = 120, *, lease: dict | None = None) -> dict:
    """POST one compute job, optionally under a target-side fencing identity."""
    headers = None
    if lease is not None:
        headers = {
            "X-Blitter-Lease-Instance": lease["instance_id"],
            "X-Blitter-Lease-Epoch": str(lease["lease_epoch"]),
            "X-Blitter-Lease-Token": lease["lease_token"],
        }
    result = webgpu_post_json(node, "/compute", job, timeout=timeout, headers=headers)
    if not isinstance(result.get("ok"), bool):
        raise RuntimeError("webgpu compute returned invalid response object")
    return result


def webgpu_submit_fenced(
    node: Node,
    job: dict,
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
        result = webgpu_submit(node, job, timeout=timeout, lease=lease)
    except Exception:
        try:
            webgpu_release_lease(node, lease, timeout=timeout)
        except Exception:
            # Preserve the primary compute/fence failure. The target gate itself
            # remains authoritative and expires/revokes independently.
            pass
        raise
    # A successful result commit is followed by explicit release. Failure to
    # confirm release makes orchestration fail closed even though the result bytes
    # may already be mathematically valid.
    webgpu_release_lease(node, lease, timeout=timeout)
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

    if args.lease_ttl_ms is not None and args.allow_unfenced:
        raise ValueError("--lease-ttl-ms and --allow-unfenced are mutually exclusive")
    if args.lease_expected_ms is not None and args.lease_ttl_ms is None:
        raise ValueError("--lease-expected-ms requires --lease-ttl-ms")
    if args.require_idle and not args.allow_unfenced:
        raise ValueError("--require-idle is only for the explicit --allow-unfenced legacy path")
    if args.lease_ttl_ms is None and not args.allow_unfenced:
        raise ValueError(
            "webgpu compute requires --lease-ttl-ms; use --allow-unfenced only for explicit legacy/testing access"
        )

    holder = args.lease_holder or f"cluster-dispatch:{socket.gethostname()}:{os.getpid()}"

    def parse_triples(items: list[str]) -> list[list[int]]:
        out = []
        for item in items:
            parts = [p.strip() for p in item.split(",")]
            if len(parts) != 3:
                raise ValueError(f"term must be qe,te,coef: got {item!r}")
            out.append([int(parts[0]), int(parts[1]), int(parts[2])])
        return out

    def submit_job(job: dict) -> dict:
        if args.lease_ttl_ms is not None:
            return webgpu_submit_fenced(
                node,
                job,
                holder=holder,
                ttl_ms=args.lease_ttl_ms,
                expected_ms=args.lease_expected_ms,
                timeout=args.timeout,
            )
        # Explicit legacy/testing escape hatch. Even here we do not submit unless
        # live observational occupancy says idle; this remains TOCTOU-racy and is
        # never authority-bearing.
        webgpu_require_idle(node, timeout=args.timeout)
        return webgpu_submit(node, job, timeout=args.timeout)

    if args.batch_file:
        batch = json.loads(args.batch_file.read_text(encoding="utf-8"))
        jobs = batch.get("jobs", batch if isinstance(batch, list) else [])
        if not isinstance(jobs, list):
            raise ValueError("batch jobs must be a list")
        nok = 0
        for i, job in enumerate(jobs):
            try:
                result = submit_job(job)
                n = len(result.get("terms", []))
                print(f"  job[{i}] {n} reduced terms")
                if not result.get("ok"):
                    nok += 1
            except Exception as exc:
                print(f"  job[{i}] FAILED: {exc}")
                nok += 1
        print(f"webgpu({node.name}): {len(jobs) - nok}/{len(jobs)} jobs ok")
        return 1 if nok else 0

    job = json.loads(args.job_file.read_text(encoding="utf-8")) if args.job_file else {
        "a": parse_triples(args.a), "b": parse_triples(args.b)
    }
    result = submit_job(job)
    print(f"webgpu({node.name} [{result.get('adapter')}]): {len(result.get('terms', []))} reduced terms")
    for term in result.get("terms", []):
        print(f"  (q^q, t^t, coef) = {term}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
