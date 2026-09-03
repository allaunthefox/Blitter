#!/usr/bin/env python3
"""Pure slot-pool routing policy for the MathPunch compute fabric.

Identity is historical/provenance state. Availability is soft state derived only
from a fresh heartbeat. A static inventory entry, old log, or remembered rental
can never by itself make a slot eligible for placement.

Controlled-spawn metadata is an additional hard eligibility dimension: a work
item may require particular user-controlled subsystems and/or one of a set of
validated execution surfaces.  It never replaces lease authority.
"""
from __future__ import annotations

import pathlib
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

_CLUSTER_DIR = str(pathlib.Path(__file__).resolve().parent)
if _CLUSTER_DIR not in sys.path:
    sys.path.insert(0, _CLUSTER_DIR)

from spawn_semantics import (  # noqa: E402
    CONTROLLED_SUBSYSTEMS,
    EXECUTION_SURFACES,
    SpawnEnvelope,
    envelope_from_mapping,
    spawn_compatible,
    surface_capability_tags,
)

DEFAULT_MAX_HEARTBEAT_AGE_MS = 3_000
DEFAULT_MAX_ABS_SKEW_MS = 60_000
DEFAULT_MIGRATION_GAIN = 1


@dataclass(frozen=True)
class SlotPool:
    pool_id: str
    node: str
    node_id: str
    lifecycle: str
    uptime_ms: int
    boot_started_unix_ms: int
    hardware: Mapping[str, Any]
    advertise_url: str
    capabilities: frozenset[str]
    accessibility_profile: str
    accessibility_rank: int
    controlled_subsystems: frozenset[str]
    execution_surfaces: frozenset[str]
    slots_total: int
    slots_active: int
    slots_queued: int
    slots_available: int
    status_ok: bool
    healthy: bool
    availability_live: bool
    heartbeat_age_ms: int
    availability_expires_unix_ms: int
    retire_at_unix_ms: int | None
    apparent_skew_ms: int
    status_rtt_ms: int | None = None
    heartbeat_seq: int | None = None
    reason: str | None = None

    @property
    def queue_pressure(self) -> float:
        return self.slots_queued / max(1, self.slots_total)

    @property
    def ephemeral(self) -> bool:
        return self.lifecycle == "ephemeral"

    @property
    def spawn_envelope(self) -> SpawnEnvelope:
        return SpawnEnvelope(
            accessibility_profile=self.accessibility_profile,
            accessibility_rank=self.accessibility_rank,
            controlled_subsystems=self.controlled_subsystems,
            execution_surfaces=self.execution_surfaces,
        )


@dataclass(frozen=True)
class WorkSpec:
    goal_id: str
    work_id: str
    requires: frozenset[str] = field(default_factory=frozenset)
    requires_control: frozenset[str] = field(default_factory=frozenset)
    acceptable_surfaces: frozenset[str] = field(default_factory=frozenset)
    preference_weights: Mapping[str, int] = field(default_factory=dict)
    checkpointable: bool = False
    state: str = "queued"  # queued | running | checkpointing
    current_pool_id: str | None = None
    min_migration_gain: int = DEFAULT_MIGRATION_GAIN

    def __post_init__(self) -> None:
        if self.state not in {"queued", "running", "checkpointing"}:
            raise ValueError(f"unsupported work state: {self.state}")
        if self.min_migration_gain < 0:
            raise ValueError("min_migration_gain must be non-negative")
        if not self.requires_control.issubset(CONTROLLED_SUBSYSTEMS):
            raise ValueError("requires_control contains unsupported subsystem tag")
        if not self.acceptable_surfaces.issubset(EXECUTION_SURFACES):
            raise ValueError("acceptable_surfaces contains unsupported surface tag")


@dataclass(frozen=True)
class PlacementDecision:
    action: str  # place | migrate | stay | stay_until_boundary | wait
    goal_id: str
    work_id: str
    from_pool_id: str | None
    to_pool_id: str | None
    reason: str


def _int_field(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if type(value) is not int or value < 0:
        raise ValueError(f"invalid heartbeat {key}: {value!r}")
    return value


def slot_pool_from_heartbeat(
    payload: Mapping[str, Any],
    *,
    now_unix_ms: int,
    max_age_ms: int = DEFAULT_MAX_HEARTBEAT_AGE_MS,
    max_abs_skew_ms: int = DEFAULT_MAX_ABS_SKEW_MS,
    default_capabilities: Iterable[str] = ("webgpu-blitter", "laurent-product-v1", "exact-i32"),
) -> SlotPool:
    """Turn one heartbeat into a fail-closed slot-pool view.

    The caller may retain returned objects for audit, but must re-evaluate from a
    fresh heartbeat before lease acquisition. In particular, ephemeral history
    is provenance only after availability_expires_unix_ms.

    Missing controlled-spawn metadata is normalized conservatively. A successful
    live WebGPU-blitter status observation may establish WebGPU as a fallback
    surface while leaving the control profile ``unknown``. A failed status read
    establishes no execution surface. Provider features such as LXC/KVM are
    never inferred from static inventory.
    """
    if max_age_ms < 0 or max_abs_skew_ms < 0:
        raise ValueError("heartbeat bounds must be non-negative")

    node = payload.get("node")
    node_id = payload.get("node_id")
    instance_id = payload.get("instance_id")
    advertise_url = payload.get("advertise_url")
    if not all(isinstance(x, str) and x for x in (node, node_id, instance_id, advertise_url)):
        raise ValueError("heartbeat node/node_id/instance_id/advertise_url must be non-empty strings")

    lifecycle = payload.get("lifecycle", "persistent")
    if lifecycle not in {"persistent", "ephemeral"}:
        raise ValueError("heartbeat lifecycle must be persistent or ephemeral")
    uptime_ms = _int_field(payload, "uptime_ms")
    boot_started_unix_ms = _int_field(payload, "boot_started_unix_ms")
    hardware = payload.get("hardware")
    if not isinstance(hardware, Mapping):
        raise ValueError("heartbeat hardware must be an object")

    status_ok = payload.get("status_ok") is True
    spawn = envelope_from_mapping(
        payload,
        default_surface="webgpu" if status_ok else None,
    )

    sent_unix_ms = _int_field(payload, "sent_unix_ms")
    availability_expires_unix_ms = _int_field(payload, "availability_expires_unix_ms")
    if availability_expires_unix_ms < sent_unix_ms:
        raise ValueError("heartbeat availability expiry precedes send time")
    retire_at = payload.get("retire_at_unix_ms")
    if retire_at is not None and (type(retire_at) is not int or retire_at < 0):
        raise ValueError("heartbeat retire_at_unix_ms is invalid")

    age_ms = max(0, now_unix_ms - sent_unix_ms)
    apparent_skew_ms = now_unix_ms - sent_unix_ms
    availability_expired = now_unix_ms > availability_expires_unix_ms
    retired = retire_at is not None and now_unix_ms >= retire_at

    total = payload.get("max_concurrent_compute")
    active = payload.get("active_compute")
    queued = payload.get("queued_compute")
    if status_ok:
        total = _int_field(payload, "max_concurrent_compute")
        active = _int_field(payload, "active_compute")
        queued = _int_field(payload, "queued_compute")
        if total < 1:
            raise ValueError("heartbeat max_concurrent_compute must be >= 1")
        available = max(0, total - active - queued)
    else:
        total = int(total) if type(total) is int and total > 0 else 1
        active = int(active) if type(active) is int and active >= 0 else 0
        queued = int(queued) if type(queued) is int and queued >= 0 else total
        available = 0

    sender_clock_invalid = bool(payload.get("sender_clock_invalid"))
    receiver_clock_invalid = bool(payload.get("receiver_clock_invalid"))
    time_anomaly = bool(payload.get("time_anomaly")) or sender_clock_invalid or receiver_clock_invalid
    stale = age_ms > max_age_ms
    skew_bad = abs(apparent_skew_ms) > max_abs_skew_ms
    availability_live = not availability_expired and not retired
    healthy = status_ok and availability_live and not time_anomaly and not stale and not skew_bad

    if not status_ok:
        reason = "status-unavailable"
    elif retired:
        reason = "node-retired"
    elif availability_expired:
        reason = "availability-expired"
    elif time_anomaly:
        reason = "time-anomaly"
    elif stale:
        reason = "heartbeat-stale"
    elif skew_bad:
        reason = "clock-or-network-skew"
    elif available == 0:
        reason = "no-slot-available"
    elif spawn.terminal:
        reason = "no-execution-surface"
    else:
        reason = None

    advertised_caps = payload.get("slot_capabilities")
    if isinstance(advertised_caps, list) and all(isinstance(x, str) and x for x in advertised_caps):
        capabilities = frozenset(advertised_caps)
    else:
        capabilities = frozenset(default_capabilities)
    capabilities = capabilities | surface_capability_tags(spawn)

    seq = payload.get("heartbeat_seq")
    rtt = payload.get("status_rtt_ms")
    return SlotPool(
        pool_id=f"{node_id}:{instance_id}:webgpu",
        node=node,
        node_id=node_id,
        lifecycle=lifecycle,
        uptime_ms=uptime_ms,
        boot_started_unix_ms=boot_started_unix_ms,
        hardware=dict(hardware),
        advertise_url=advertise_url,
        capabilities=capabilities,
        accessibility_profile=spawn.accessibility_profile,
        accessibility_rank=spawn.accessibility_rank,
        controlled_subsystems=spawn.controlled_subsystems,
        execution_surfaces=spawn.execution_surfaces,
        slots_total=total,
        slots_active=active,
        slots_queued=queued,
        slots_available=available,
        status_ok=status_ok,
        healthy=healthy,
        availability_live=availability_live,
        heartbeat_age_ms=age_ms,
        availability_expires_unix_ms=availability_expires_unix_ms,
        retire_at_unix_ms=retire_at,
        apparent_skew_ms=apparent_skew_ms,
        status_rtt_ms=rtt if type(rtt) is int and rtt >= 0 else None,
        heartbeat_seq=seq if type(seq) is int and seq >= 0 else None,
        reason=reason,
    )


def pool_compatible(work: WorkSpec, pool: SlotPool) -> bool:
    if not work.requires.issubset(pool.capabilities):
        return False
    return spawn_compatible(
        pool.spawn_envelope,
        requires_control=work.requires_control,
        acceptable_surfaces=work.acceptable_surfaces,
    )


def suitability_score(work: WorkSpec, pool: SlotPool) -> int:
    if not pool_compatible(work, pool):
        raise ValueError("pool does not satisfy hard capability/control/surface requirements")
    return sum(weight for capability, weight in work.preference_weights.items() if capability in pool.capabilities)


def rank_slot_pools(work: WorkSpec, pools: Iterable[SlotPool]) -> list[SlotPool]:
    """Rank live lease candidates. Historical identity never authorizes placement."""
    eligible = [
        pool
        for pool in pools
        if pool.healthy
        and pool.availability_live
        and pool.slots_available > 0
        and pool_compatible(work, pool)
    ]
    return sorted(
        eligible,
        key=lambda pool: (
            -suitability_score(work, pool),
            pool.accessibility_rank,
            pool.queue_pressure,
            pool.status_rtt_ms if pool.status_rtt_ms is not None else 2**31 - 1,
            abs(pool.apparent_skew_ms),
            pool.node,
            pool.pool_id,
        ),
    )


def decide_placement(
    work: WorkSpec,
    pools: Iterable[SlotPool],
    *,
    current_pool: SlotPool | None = None,
) -> PlacementDecision:
    ranked = rank_slot_pools(work, pools)
    best = ranked[0] if ranked else None

    if work.current_pool_id is None or work.state == "queued":
        if best is None:
            return PlacementDecision("wait", work.goal_id, work.work_id, work.current_pool_id, None, "no live healthy compatible controlled spawn surface is currently available")
        return PlacementDecision("place", work.goal_id, work.work_id, work.current_pool_id, best.pool_id, "best currently available compatible controlled live slot")

    if current_pool is None:
        return PlacementDecision("stay", work.goal_id, work.work_id, work.current_pool_id, None, "current placement exists but no current-pool observation was supplied")

    current_compatible = pool_compatible(work, current_pool)
    current_degraded = not current_pool.healthy or not current_pool.availability_live or not current_compatible
    current_score = suitability_score(work, current_pool) if current_compatible else -(2**31)

    if best is None:
        return PlacementDecision("stay", work.goal_id, work.work_id, current_pool.pool_id, None, "no live replacement lease candidate is available")
    if best.pool_id == current_pool.pool_id and not current_degraded:
        return PlacementDecision("stay", work.goal_id, work.work_id, current_pool.pool_id, None, "current pool remains best")

    gain = suitability_score(work, best) - current_score
    materially_better = gain >= work.min_migration_gain
    recovery_move = current_degraded and best.pool_id != current_pool.pool_id
    if not materially_better and not recovery_move:
        return PlacementDecision("stay", work.goal_id, work.work_id, current_pool.pool_id, None, f"fit gain {gain} is below migration hysteresis {work.min_migration_gain}")

    if work.state == "running" and not work.checkpointable:
        why = "current slot degraded" if recovery_move else f"better slot available (fit gain {gain})"
        return PlacementDecision("stay_until_boundary", work.goal_id, work.work_id, current_pool.pool_id, best.pool_id, f"{why}, but work is not checkpointable")

    why = "current slot degraded/revoked from live availability or spawn compatibility" if recovery_move else f"better slot available with fit gain {gain}"
    return PlacementDecision("migrate", work.goal_id, work.work_id, current_pool.pool_id, best.pool_id, f"{why}; acquire destination lease before checkpoint handoff")
