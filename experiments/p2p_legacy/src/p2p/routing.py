"""Nearest-neighbor routing with QoS-tiered scoring."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, List, Mapping, Optional, Sequence

from p2p.designation import Designation, Load, SpawnEnvelope, envelope_from_mapping, ACCESSIBILITY_RANK


QoS_WEIGHTS = {
    "latency_sensitive": {"load_weight": 3.0, "rtt_weight": 2.0},
    "throughput_sensitive": {"load_weight": 0.25, "rtt_weight": 0.25},
    "default": {"load_weight": 1.0, "rtt_weight": 1.0},
}


@dataclass(frozen=True)
class WorkSpec:
    goal_id: str
    work_id: str
    requires: FrozenSet[str] = frozenset()
    requires_control: FrozenSet[str] = frozenset()
    acceptable_surfaces: FrozenSet[str] = frozenset()
    preference_weights: Mapping[str, int] = field(default_factory=dict)
    checkpointable: bool = False
    qos_tier: str = "default"
    min_migration_gain: int = 1

    def __post_init__(self) -> None:
        if self.qos_tier not in QoS_WEIGHTS:
            raise ValueError(f"unsupported qos_tier: {self.qos_tier}")


@dataclass(frozen=True)
class RouteCandidate:
    node_id: str
    score: float
    proximity_rtt_ms: float
    proximity_hop_count: int
    load_active: int
    load_queued: int
    load_available: int
    accessibility_rank: int


def suitability_score(work: WorkSpec, node: Designation) -> int:
    return sum(weight for cap, weight in work.preference_weights.items() if cap in node.capabilities)


def _load_penalty(node: Designation, tier: str) -> float:
    w = QoS_WEIGHTS[tier]["load_weight"]
    pressure = (node.load.active_compute + node.load.queued_compute) / max(1, node.load.max_concurrent_compute)
    return pressure * w


def _rtt_penalty(node: Designation, tier: str) -> float:
    w = QoS_WEIGHTS[tier]["rtt_weight"]
    return node.proximity.rtt_ms * w


def _skew_penalty(node: Designation) -> float:
    return max(0.0, node.proximity.rtt_ms - 60_000)


def _is_compatible(work: WorkSpec, node: Designation) -> bool:
    if not work.requires.issubset(node.capabilities):
        return False
    if not work.requires_control.issubset(node.controlled_subsystems):
        return False
    if work.acceptable_surfaces and not work.acceptable_surfaces.intersection(node.execution_surfaces):
        return False
    if node.accessibility_profile == "none":
        return False
    return True


def _is_routable(node: Designation) -> bool:
    return node.availability_live and node.load.max_concurrent_compute > 0


def route(work: WorkSpec, peers) -> List[RouteCandidate]:
    candidates = []
    for entry in peers:
        node = entry.designation if hasattr(entry, "designation") else entry
        if not _is_compatible(work, node) or not _is_routable(node):
            continue
        score = (suitability_score(work, node)
                 - _load_penalty(node, work.qos_tier)
                 - _rtt_penalty(node, work.qos_tier)
                 - _skew_penalty(node))
        candidates.append(RouteCandidate(
            node_id=node.node_id,
            score=score,
            proximity_rtt_ms=node.proximity.rtt_ms,
            proximity_hop_count=node.proximity.hop_count,
            load_active=node.load.active_compute,
            load_queued=node.load.queued_compute,
            load_available=node.load.available,
            accessibility_rank=node.accessibility_rank,
        ))
    candidates.sort(key=lambda c: (-c.score, c.accessibility_rank, c.node_id))
    return candidates


def best_route(work: WorkSpec, peers) -> Optional[RouteCandidate]:
    candidates = route(work, peers)
    return candidates[0] if candidates else None
