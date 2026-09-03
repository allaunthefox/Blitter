"""Multi-axis node designation for the p2p blitter fabric."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, FrozenSet, Mapping, Optional

ACCESSIBILITY_RANK: dict[str, int] = {
    "direct": 0, "namespace": 10, "container": 20, "guest": 30,
    "emulated": 40, "sandbox": 50, "remote": 60, "unknown": 90, "none": 255,
}

CONTROLLED_SUBSYSTEMS = frozenset({
    "userspace", "loader", "filesystem", "process-tree", "mount-namespace",
    "pid-namespace", "network-namespace", "cgroup", "kernel", "virtual-devices",
    "cpu-model", "firmware", "accelerator-api",
})
EXECUTION_SURFACES = frozenset({
    "native", "fhs", "lxc", "kvm", "qemu-tcg", "bochs", "webgpu",
    "wasm-webgpu", "wasm", "rpc",
})
DEFAULT_CAPABILITIES = frozenset({"webgpu-blitter", "laurent-product-v1", "exact-i32"})


@dataclass(frozen=True)
class SpawnEnvelope:
    accessibility_profile: str
    accessibility_rank: int
    controlled_subsystems: frozenset[str]
    execution_surfaces: frozenset[str]

    @property
    def terminal(self) -> bool:
        return self.accessibility_profile == "none" or not self.execution_surfaces


@dataclass(frozen=True)
class Load:
    active_compute: int = 0
    queued_compute: int = 0
    max_concurrent_compute: int = 1

    @property
    def queue_pressure(self) -> float:
        return self.queued_compute / max(1, self.max_concurrent_compute)

    @property
    def available(self) -> int:
        return max(0, self.max_concurrent_compute - self.active_compute - self.queued_compute)


@dataclass(frozen=True)
class Proximity:
    rtt_ms: float = 0.0
    hop_count: int = 0
    tailscale_subnet: str = ""


@dataclass(frozen=True)
class Designation:
    node_id: str = ""
    instance_id: str = ""
    capabilities: frozenset[str] = DEFAULT_CAPABILITIES
    controlled_subsystems: frozenset[str] = frozenset({"accelerator-api"})
    execution_surfaces: frozenset[str] = frozenset({"webgpu"})
    accessibility_profile: str = "sandbox"
    load: Load = field(default_factory=Load)
    proximity: Proximity = field(default_factory=Proximity)
    lifecycle: str = "persistent"
    ephemeral: bool = False
    availability_ttl_ms: int = 3500
    availability_expires_unix_ms: int = 0
    retire_at_unix_ms: Optional[int] = None
    sent_unix_ms: int = 0
    heartbeat_seq: int = 0
    lease_epoch: int = 0
    lease_holder: str = ""
    lease_active: bool = False
    lease_expired: bool = False
    in_flight: bool = False
    hardware: str = ""

    @property
    def accessibility_rank(self) -> int:
        return ACCESSIBILITY_RANK.get(self.accessibility_profile, 90)

    @property
    def available_by_announcement(self) -> bool:
        return bool(self.availability_expires_unix_ms) and not self.lease_expired

    @property
    def availability_live(self) -> bool:
        now_ms = time.time_ns() // 1_000_000
        return not self.lease_expired and self.availability_expires_unix_ms > now_ms


def envelope_from_mapping(payload: Mapping[str, Any]) -> SpawnEnvelope:
    version = payload.get("spawn_semantics_version", 1)
    if version != 1:
        raise ValueError("unsupported spawn_semantics_version")
    profile = payload.get("accessibility_profile", "unknown")
    if profile not in ACCESSIBILITY_RANK:
        raise ValueError("invalid accessibility_profile")
    raw_control = payload.get("controlled_subsystems", [])
    controlled = frozenset(raw_control) & CONTROLLED_SUBSYSTEMS
    raw_surfaces = payload.get("execution_surfaces", [])
    surfaces = frozenset(raw_surfaces) & EXECUTION_SURFACES
    if profile == "none" and surfaces:
        raise ValueError("accessibility_profile=none cannot advertise execution surfaces")
    return SpawnEnvelope(
        accessibility_profile=profile,
        accessibility_rank=ACCESSIBILITY_RANK[profile],
        controlled_subsystems=controlled,
        execution_surfaces=surfaces,
    )


def spawn_compatible(envelope: SpawnEnvelope, *, requires_control=frozenset(), acceptable_surfaces=frozenset()) -> bool:
    if envelope.terminal:
        return False
    if not requires_control.issubset(envelope.controlled_subsystems):
        return False
    if acceptable_surfaces and not envelope.execution_surfaces.intersection(acceptable_surfaces):
        return False
    return True


def surface_capability_tags(envelope: SpawnEnvelope) -> frozenset[str]:
    return frozenset(f"surface:{s}" for s in envelope.execution_surfaces)
