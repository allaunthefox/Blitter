#!/usr/bin/env python3
"""Advisory WebGPU blitter load heartbeat over unicast UDP.

The heartbeat advertises load, a boot-scoped hardware identity, soft-state
availability, and the live controlled-spawn envelope exposed by the daemon.
Heartbeats rank lease candidates but never authorize work. Historical identity
may persist forever; current availability never does.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

_CLUSTER_DIR = str(Path(__file__).resolve().parent)
if _CLUSTER_DIR not in sys.path:
    sys.path.insert(0, _CLUSTER_DIR)

from spawn_semantics import (  # noqa: E402
    SPAWN_SEMANTICS_VERSION,
    envelope_from_mapping,
    surface_capability_tags,
)

DEFAULT_STATUS_URL = "http://127.0.0.1:8790/status"
DEFAULT_BIND = "0.0.0.0:8791"
DEFAULT_INTERVAL_MS = 1000
DEFAULT_TIMEOUT_MS = 750
DEFAULT_AVAILABILITY_TTL_MS = 3500
WIRE_VERSION = 3
MIN_SANE_UNIX_MS = 946_684_800_000  # 2000-01-01T00:00:00Z
DEFAULT_SLOT_CAPABILITIES = ("webgpu-blitter", "laurent-product-v1", "exact-i32")
SPAWN_EXTENSION_KEYS = frozenset(
    {
        "spawn_semantics_version",
        "accessibility_profile",
        "controlled_subsystems",
        "execution_surfaces",
    }
)


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int


def unix_ms() -> int:
    return time.time_ns() // 1_000_000


def system_uptime_ms() -> int:
    """Return system uptime in milliseconds without trusting wall-clock time."""
    try:
        raw = Path("/proc/uptime").read_text(encoding="utf-8").split()[0]
        return max(0, int(float(raw) * 1000))
    except (OSError, ValueError, IndexError):
        return max(0, time.monotonic_ns() // 1_000_000)


def _read_text(path: str) -> str | None:
    try:
        value = Path(path).read_text(encoding="utf-8", errors="replace").strip()
        return value or None
    except OSError:
        return None


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            if key.strip().lower() in {"model name", "hardware", "processor"} and value.strip():
                return value.strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _memory_bytes() -> int:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def collect_node_identity(node: str, *, lifecycle: str = "persistent") -> dict[str, Any]:
    """Collect stable-within-a-boot hardware identity fields.

    The live uptime counter is deliberately not embedded in node_id because
    doing so would change identity every heartbeat. boot_id identifies the
    uptime generation; uptime_ms travels beside the ID. lifecycle is included
    so an ephemeral rental cannot be mistaken for a permanent fabric member in
    a human/LLM log.
    """
    if lifecycle not in {"persistent", "ephemeral"}:
        raise ValueError("lifecycle must be persistent or ephemeral")
    arch = platform.machine() or "unknown"
    logical_cpus = os.cpu_count() or 0
    memory_bytes = _memory_bytes()
    memory_mib = memory_bytes // (1024 * 1024) if memory_bytes else 0
    boot_id = _read_text("/proc/sys/kernel/random/boot_id")
    if not boot_id:
        approximate_boot_ms = max(0, unix_ms() - system_uptime_ms())
        boot_id = f"boot-{approximate_boot_ms // 60_000}"
    boot_short = boot_id.replace("-", "")[:8] or "unknown"
    node_id = f"{node}|{lifecycle}|{arch}|cpu={logical_cpus}|mem={memory_mib}MiB|boot={boot_short}"
    return {
        "node_id": node_id,
        "boot_id": boot_id,
        "hardware": {
            "arch": arch,
            "logical_cpus": logical_cpus,
            "memory_bytes": memory_bytes,
            "cpu_model": _cpu_model(),
        },
    }


def parse_endpoint(value: str) -> Endpoint:
    host, sep, raw_port = value.rpartition(":")
    if not sep or not host:
        raise argparse.ArgumentTypeError("endpoint must be HOST:PORT")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("endpoint port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("endpoint port must be in 1..65535")
    return Endpoint(host=host, port=port)


def _status_capabilities(payload: Mapping[str, Any], spawn: Any) -> list[str]:
    advertised = payload.get("slot_capabilities")
    if advertised is None:
        capabilities = set(DEFAULT_SLOT_CAPABILITIES)
    elif isinstance(advertised, list) and all(isinstance(x, str) and x for x in advertised):
        capabilities = set(advertised)
    else:
        raise RuntimeError("/status slot_capabilities must be a list of non-empty strings")
    capabilities.update(surface_capability_tags(spawn))
    return sorted(capabilities)


def status_snapshot(status_url: str, timeout_ms: int) -> dict[str, Any]:
    started = time.monotonic_ns()
    try:
        with urllib.request.urlopen(status_url, timeout=timeout_ms / 1000.0) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        raise RuntimeError(str(exc)) from exc
    elapsed_ms = (time.monotonic_ns() - started) // 1_000_000
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError("/status did not return ok=true JSON")

    required = {
        "busy": bool,
        "idle": bool,
        "active_compute": int,
        "queued_compute": int,
        "max_concurrent_compute": int,
        "status_seq": int,
    }
    for key, expected in required.items():
        value = payload.get(key)
        if type(value) is not expected:
            raise RuntimeError(f"/status missing or invalid {key!r}: {value!r}")
    derived_busy = payload["active_compute"] != 0 or payload["queued_compute"] != 0
    if payload["busy"] != derived_busy or payload["idle"] == payload["busy"]:
        raise RuntimeError("/status occupancy booleans disagree with counters")
    if payload["active_compute"] < 0 or payload["queued_compute"] < 0:
        raise RuntimeError("/status reported negative occupancy")
    if payload["max_concurrent_compute"] < 1:
        raise RuntimeError("/status reported invalid capacity")

    try:
        # A successful read of this daemon establishes WebGPU as the legacy
        # fallback surface even if an older daemon does not emit spawn metadata.
        # It does not establish any deeper host control.
        spawn = envelope_from_mapping(payload, default_surface="webgpu")
    except ValueError as exc:
        raise RuntimeError(f"/status invalid spawn envelope: {exc}") from exc

    return {
        "status_rtt_ms": int(elapsed_ms),
        "busy": payload["busy"],
        "idle": payload["idle"],
        "active_compute": payload["active_compute"],
        "queued_compute": payload["queued_compute"],
        "max_concurrent_compute": payload["max_concurrent_compute"],
        "status_seq": payload["status_seq"],
        "adapter": payload.get("adapter"),
        "backend": payload.get("backend"),
        "spawn_semantics_version": SPAWN_SEMANTICS_VERSION,
        "accessibility_profile": spawn.accessibility_profile,
        "controlled_subsystems": sorted(spawn.controlled_subsystems),
        "execution_surfaces": sorted(spawn.execution_surfaces),
        "slot_capabilities": _status_capabilities(payload, spawn),
    }


def heartbeat_payload(
    *,
    node: str,
    instance_id: str,
    seq: int,
    advertise_url: str,
    status: dict[str, Any] | None,
    status_error: str | None,
    node_identity: Mapping[str, Any] | None = None,
    lifecycle: str = "persistent",
    availability_ttl_ms: int = DEFAULT_AVAILABILITY_TTL_MS,
    retire_at_unix_ms: int | None = None,
) -> dict[str, Any]:
    if lifecycle not in {"persistent", "ephemeral"}:
        raise ValueError("lifecycle must be persistent or ephemeral")
    if availability_ttl_ms < 1:
        raise ValueError("availability_ttl_ms must be positive")
    if retire_at_unix_ms is not None and retire_at_unix_ms < 0:
        raise ValueError("retire_at_unix_ms must be non-negative")

    sent = unix_ms()
    uptime_ms = system_uptime_ms()
    identity = dict(node_identity or collect_node_identity(node, lifecycle=lifecycle))
    hardware = dict(identity.get("hardware") or {})
    if status is not None:
        hardware["accelerator_adapter"] = status.get("adapter")
        hardware["accelerator_backend"] = status.get("backend")
    else:
        hardware.setdefault("accelerator_adapter", None)
        hardware.setdefault("accelerator_backend", None)

    base: dict[str, Any] = {
        "wire_version": WIRE_VERSION,
        "service": "webgpu-blitter-heartbeat",
        "node": node,
        "node_id": identity["node_id"],
        "boot_id": identity["boot_id"],
        "lifecycle": lifecycle,
        "ephemeral": lifecycle == "ephemeral",
        "uptime_ms": uptime_ms,
        "boot_started_unix_ms": max(0, sent - uptime_ms),
        "availability_ttl_ms": availability_ttl_ms,
        "availability_expires_unix_ms": sent + availability_ttl_ms,
        "retire_at_unix_ms": retire_at_unix_ms,
        "hardware": hardware,
        "instance_id": instance_id,
        "heartbeat_seq": seq,
        "sent_unix_ms": sent,
        "advertise_url": advertise_url,
        "status_ok": status is not None,
    }
    if status is not None:
        base.update(status)
        base["status_error"] = None
    else:
        base.update(
            {
                "busy": True,
                "idle": False,
                "active_compute": None,
                "queued_compute": None,
                "max_concurrent_compute": None,
                "status_seq": None,
                "status_rtt_ms": None,
                "adapter": None,
                "backend": None,
                "spawn_semantics_version": SPAWN_SEMANTICS_VERSION,
                "accessibility_profile": "unknown",
                "controlled_subsystems": [],
                "execution_surfaces": [],
                "slot_capabilities": [],
                "status_error": status_error or "status unavailable",
            }
        )
    return base


def encode_heartbeat(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _validate_spawn_extension(payload: Mapping[str, Any]) -> None:
    present = SPAWN_EXTENSION_KEYS.intersection(payload.keys())
    if present and present != SPAWN_EXTENSION_KEYS:
        missing = sorted(SPAWN_EXTENSION_KEYS - present)
        raise ValueError(f"partial controlled-spawn heartbeat extension; missing {missing}")
    if not present:
        # Backward-compatible wire-v3 receiver: pre-extension v3 heartbeats are
        # still valid. The scheduler normalizes their live WebGPU surface to
        # profile=unknown, control={}, surfaces={webgpu}.
        return
    try:
        spawn = envelope_from_mapping(payload)
    except ValueError as exc:
        raise ValueError(f"invalid controlled-spawn heartbeat extension: {exc}") from exc
    capabilities = payload.get("slot_capabilities")
    if not isinstance(capabilities, list) or not all(isinstance(x, str) and x for x in capabilities):
        raise ValueError("controlled-spawn heartbeat requires string slot_capabilities")
    missing_surface_caps = surface_capability_tags(spawn) - frozenset(capabilities)
    if missing_surface_caps:
        raise ValueError(f"heartbeat missing surface capability tags: {sorted(missing_surface_caps)}")


def validate_heartbeat(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("heartbeat must be a JSON object")
    if payload.get("wire_version") != WIRE_VERSION:
        raise ValueError("unsupported heartbeat wire_version")
    if payload.get("service") != "webgpu-blitter-heartbeat":
        raise ValueError("unexpected heartbeat service")
    for key in ("node", "node_id", "boot_id", "instance_id", "advertise_url"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise ValueError(f"invalid heartbeat {key}")
    lifecycle = payload.get("lifecycle")
    if lifecycle not in {"persistent", "ephemeral"}:
        raise ValueError("invalid heartbeat lifecycle")
    if payload.get("ephemeral") is not (lifecycle == "ephemeral"):
        raise ValueError("heartbeat ephemeral flag disagrees with lifecycle")
    for key in (
        "heartbeat_seq",
        "sent_unix_ms",
        "uptime_ms",
        "boot_started_unix_ms",
        "availability_ttl_ms",
        "availability_expires_unix_ms",
    ):
        if type(payload.get(key)) is not int or payload[key] < 0:
            raise ValueError(f"invalid heartbeat {key}")
    if payload["availability_ttl_ms"] < 1:
        raise ValueError("heartbeat availability_ttl_ms must be positive")
    if payload["availability_expires_unix_ms"] < payload["sent_unix_ms"]:
        raise ValueError("heartbeat availability expiry precedes send time")
    retire_at = payload.get("retire_at_unix_ms")
    if retire_at is not None and (type(retire_at) is not int or retire_at < 0):
        raise ValueError("invalid heartbeat retire_at_unix_ms")

    hardware = payload.get("hardware")
    if not isinstance(hardware, dict):
        raise ValueError("invalid heartbeat hardware")
    if not isinstance(hardware.get("arch"), str) or not hardware["arch"]:
        raise ValueError("invalid heartbeat hardware.arch")
    for key in ("logical_cpus", "memory_bytes"):
        if type(hardware.get(key)) is not int or hardware[key] < 0:
            raise ValueError(f"invalid heartbeat hardware.{key}")
    if not isinstance(hardware.get("cpu_model"), str) or not hardware["cpu_model"]:
        raise ValueError("invalid heartbeat hardware.cpu_model")

    if type(payload.get("status_ok")) is not bool:
        raise ValueError("invalid heartbeat status_ok")
    if payload["status_ok"]:
        for key in ("busy", "idle"):
            if type(payload.get(key)) is not bool:
                raise ValueError(f"invalid heartbeat {key}")
        for key in ("active_compute", "queued_compute", "max_concurrent_compute", "status_seq"):
            if type(payload.get(key)) is not int or payload[key] < 0:
                raise ValueError(f"invalid heartbeat {key}")
        derived_busy = payload["active_compute"] != 0 or payload["queued_compute"] != 0
        if payload["busy"] != derived_busy or payload["idle"] == payload["busy"]:
            raise ValueError("heartbeat occupancy booleans disagree with counters")
    else:
        if payload.get("busy") is not True or payload.get("idle") is not False:
            raise ValueError("failed status heartbeat must fail closed as busy")

    _validate_spawn_extension(payload)
    return payload


def annotate_received(
    payload: dict[str, Any],
    *,
    received_unix_ms: int,
    previous_seq: int | None,
) -> dict[str, Any]:
    seq = payload["heartbeat_seq"]
    gap = 0
    reordered = False
    duplicate = False
    if previous_seq is not None:
        if seq > previous_seq:
            gap = max(0, seq - previous_seq - 1)
        elif seq == previous_seq:
            duplicate = True
        else:
            reordered = True

    sent_unix_ms = payload["sent_unix_ms"]
    apparent_skew_ms = received_unix_ms - sent_unix_ms
    sender_clock_invalid = sent_unix_ms < MIN_SANE_UNIX_MS
    receiver_clock_invalid = received_unix_ms < MIN_SANE_UNIX_MS
    if sender_clock_invalid and receiver_clock_invalid:
        time_anomaly_reason = "sender_and_receiver_clock_before_2000"
    elif sender_clock_invalid:
        time_anomaly_reason = "sender_clock_before_2000"
    elif receiver_clock_invalid:
        time_anomaly_reason = "receiver_clock_before_2000"
    else:
        time_anomaly_reason = None

    availability_expired = received_unix_ms > payload["availability_expires_unix_ms"]
    retire_at = payload.get("retire_at_unix_ms")
    retired = retire_at is not None and received_unix_ms >= retire_at

    enriched = dict(payload)
    enriched.update(
        {
            "received_unix_ms": received_unix_ms,
            "apparent_skew_ms": apparent_skew_ms,
            "sender_clock_invalid": sender_clock_invalid,
            "receiver_clock_invalid": receiver_clock_invalid,
            "time_anomaly": sender_clock_invalid or receiver_clock_invalid,
            "time_anomaly_reason": time_anomaly_reason,
            "availability_expired": availability_expired,
            "retired": retired,
            "available_by_announcement": payload["status_ok"] and not availability_expired and not retired,
            "sequence_gap": gap,
            "duplicate": duplicate,
            "reordered": reordered,
        }
    )
    return enriched


def run_announce(args: argparse.Namespace) -> int:
    node = args.node or socket.gethostname()
    lifecycle = "ephemeral" if args.ephemeral else "persistent"
    identity = collect_node_identity(node, lifecycle=lifecycle)
    started = unix_ms()
    instance_id = args.instance_id or f"{identity['node_id']}:{started}:{os.getpid()}"
    ttl_ms = args.availability_ttl_ms or max(DEFAULT_AVAILABILITY_TTL_MS, args.interval_ms * 3)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    seq = 0
    while True:
        seq += 1
        try:
            status = status_snapshot(args.status_url, args.timeout_ms)
            status_error = None
        except RuntimeError as exc:
            status = None
            status_error = str(exc)
        payload = heartbeat_payload(
            node=node,
            instance_id=instance_id,
            seq=seq,
            advertise_url=args.advertise_url,
            status=status,
            status_error=status_error,
            node_identity=identity,
            lifecycle=lifecycle,
            availability_ttl_ms=ttl_ms,
            retire_at_unix_ms=args.retire_at_unix_ms,
        )
        wire = encode_heartbeat(payload)
        for target in args.target:
            try:
                sock.sendto(wire, (target.host, target.port))
            except OSError as exc:
                print(f"blitter-heartbeat: send {target.host}:{target.port}: {exc}", file=sys.stderr)
        if args.stdout:
            print(wire.decode("utf-8").rstrip())
        if args.once:
            return 0 if status is not None else 1
        time.sleep(args.interval_ms / 1000.0)


def run_listen(args: argparse.Namespace) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.bind.host, args.bind.port))
    previous: dict[tuple[str, str], int] = {}
    while True:
        data, source = sock.recvfrom(args.max_datagram_bytes)
        received = unix_ms()
        try:
            payload = validate_heartbeat(json.loads(data.decode("utf-8")))
            key = (payload["node_id"], payload["instance_id"])
            enriched = annotate_received(
                payload,
                received_unix_ms=received,
                previous_seq=previous.get(key),
            )
            if not enriched["reordered"] and not enriched["duplicate"]:
                previous[key] = payload["heartbeat_seq"]
            enriched["source_ip"] = source[0]
            enriched["source_port"] = source[1]
            print(json.dumps(enriched, sort_keys=True), flush=True)
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            error = {
                "ok": False,
                "received_unix_ms": received,
                "source_ip": source[0],
                "source_port": source[1],
                "error": str(exc),
            }
            print(json.dumps(error, sort_keys=True), flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)

    announce = sub.add_parser("announce", help="announce local blitter occupancy to peers")
    announce.add_argument("--target", type=parse_endpoint, action="append", required=True, metavar="HOST:PORT")
    announce.add_argument("--status-url", default=DEFAULT_STATUS_URL)
    announce.add_argument("--advertise-url", default=DEFAULT_STATUS_URL.removesuffix("/status"))
    announce.add_argument("--node")
    announce.add_argument("--instance-id")
    announce.add_argument("--ephemeral", action="store_true", help="mark this node as rented/disposable soft-state capacity")
    announce.add_argument("--availability-ttl-ms", type=int, help="presence expiry; defaults to max(3500, 3*interval)")
    announce.add_argument("--retire-at-unix-ms", type=int, help="optional known retirement timestamp for ephemeral capacity")
    announce.add_argument("--interval-ms", type=int, default=DEFAULT_INTERVAL_MS)
    announce.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    announce.add_argument("--once", action="store_true")
    announce.add_argument("--stdout", action="store_true")

    listen = sub.add_parser("listen", help="receive and annotate blitter heartbeats")
    listen.add_argument("--bind", type=parse_endpoint, default=parse_endpoint(DEFAULT_BIND), metavar="HOST:PORT")
    listen.add_argument("--max-datagram-bytes", type=int, default=16 * 1024)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.action == "announce":
        if args.interval_ms < 1:
            raise SystemExit("--interval-ms must be positive")
        if args.timeout_ms < 1:
            raise SystemExit("--timeout-ms must be positive")
        if args.availability_ttl_ms is not None and args.availability_ttl_ms < args.interval_ms:
            raise SystemExit("--availability-ttl-ms must be at least --interval-ms")
        if args.retire_at_unix_ms is not None and args.retire_at_unix_ms < 0:
            raise SystemExit("--retire-at-unix-ms must be non-negative")
        return run_announce(args)
    if args.max_datagram_bytes < 512:
        raise SystemExit("--max-datagram-bytes must be at least 512")
    return run_listen(args)


if __name__ == "__main__":
    raise SystemExit(main())
