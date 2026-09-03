#!/usr/bin/env python3
"""Pure controlled-spawn semantics for MathPunch slot placement.

This module has no authority to start work.  It only normalizes a live execution
envelope and answers whether that envelope satisfies a workload's hard control
and surface requirements.  Lease acquisition remains a separate authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

SPAWN_SEMANTICS_VERSION = 1

ACCESSIBILITY_RANK: dict[str, int] = {
    "direct": 0,
    "namespace": 10,
    "container": 20,
    "guest": 30,
    "emulated": 40,
    "sandbox": 50,
    "remote": 60,
    "unknown": 90,
    "none": 255,
}

CONTROLLED_SUBSYSTEMS = frozenset(
    {
        "userspace",
        "loader",
        "filesystem",
        "process-tree",
        "mount-namespace",
        "pid-namespace",
        "network-namespace",
        "cgroup",
        "kernel",
        "virtual-devices",
        "cpu-model",
        "firmware",
        "accelerator-api",
    }
)

EXECUTION_SURFACES = frozenset(
    {
        "native",
        "fhs",
        "lxc",
        "kvm",
        "qemu-tcg",
        "bochs",
        "webgpu",
        "wasm-webgpu",
        "wasm",
        "rpc",
    }
)


@dataclass(frozen=True)
class SpawnEnvelope:
    accessibility_profile: str
    accessibility_rank: int
    controlled_subsystems: frozenset[str]
    execution_surfaces: frozenset[str]

    @property
    def terminal(self) -> bool:
        return self.accessibility_profile == "none" or not self.execution_surfaces


def _canonical_set(value: Any, *, allowed: frozenset[str], field: str) -> frozenset[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError(f"{field} must be a sequence of canonical strings")
    items: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError(f"{field} contains a non-string or empty value")
        if item not in allowed:
            raise ValueError(f"unsupported {field} value: {item}")
        items.add(item)
    return frozenset(items)


def envelope_from_mapping(
    payload: Mapping[str, Any],
    *,
    default_surface: str | None = None,
) -> SpawnEnvelope:
    """Normalize live spawn metadata.

    `default_surface` is only for a surface established by the caller itself.
    For example, the WebGPU heartbeat may use ``default_surface="webgpu"``
    after successfully reading the WebGPU daemon status endpoint.  It MUST NOT
    be used to infer provider capabilities such as KVM or LXC from inventory.
    """
    version = payload.get("spawn_semantics_version", SPAWN_SEMANTICS_VERSION)
    if type(version) is not int or version != SPAWN_SEMANTICS_VERSION:
        raise ValueError("unsupported spawn_semantics_version")

    profile = payload.get("accessibility_profile", "unknown")
    if not isinstance(profile, str) or profile not in ACCESSIBILITY_RANK:
        raise ValueError("invalid accessibility_profile")

    raw_control = payload.get("controlled_subsystems", [])
    controlled = _canonical_set(raw_control, allowed=CONTROLLED_SUBSYSTEMS, field="controlled_subsystems")

    raw_surfaces = payload.get("execution_surfaces")
    if raw_surfaces is None:
        raw_surfaces = [default_surface] if default_surface is not None else []
    surfaces = _canonical_set(raw_surfaces, allowed=EXECUTION_SURFACES, field="execution_surfaces")

    if profile == "none" and surfaces:
        raise ValueError("accessibility_profile=none cannot advertise execution surfaces")

    return SpawnEnvelope(
        accessibility_profile=profile,
        accessibility_rank=ACCESSIBILITY_RANK[profile],
        controlled_subsystems=controlled,
        execution_surfaces=surfaces,
    )


def spawn_compatible(
    envelope: SpawnEnvelope,
    *,
    requires_control: Iterable[str] = (),
    acceptable_surfaces: Iterable[str] = (),
) -> bool:
    required = frozenset(requires_control)
    acceptable = frozenset(acceptable_surfaces)
    if not required.issubset(CONTROLLED_SUBSYSTEMS):
        raise ValueError("requires_control contains unsupported subsystem tag")
    if not acceptable.issubset(EXECUTION_SURFACES):
        raise ValueError("acceptable_surfaces contains unsupported surface tag")
    if envelope.terminal:
        return False
    if not required.issubset(envelope.controlled_subsystems):
        return False
    if acceptable and envelope.execution_surfaces.isdisjoint(acceptable):
        return False
    return True


def surface_capability_tags(envelope: SpawnEnvelope) -> frozenset[str]:
    return frozenset(f"surface:{surface}" for surface in envelope.execution_surfaces)
