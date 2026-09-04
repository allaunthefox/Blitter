#!/usr/bin/env python3
"""Pure LSP presence, admission, placement, and target-lease contracts.

This module validates messages and models state transitions only.  It does not
contact a node, launch a worker, or claim that a target-side implementation is
deployed.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping

LSP_PRESENCE_SCHEMA = "mathpunch.blitter-lsp-presence.v1"
DEFAULT_MAX_CLOCK_SKEW_MS = 5_000


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _positive_int(value: Any, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _capabilities(value: Any) -> frozenset[str]:
    if not isinstance(value, (list, tuple, set, frozenset)) or not value:
        raise ValueError("capabilities must be a non-empty list of strings")
    if any(not isinstance(item, str) or not item or "\x00" in item for item in value):
        raise ValueError("capabilities must contain non-empty strings")
    result = frozenset(value)
    if len(result) != len(value):
        raise ValueError("capabilities must not contain duplicates")
    return result


@dataclass(frozen=True)
class LSPPresence:
    node_id: str
    boot_id: str
    sent_unix_ms: int
    availability_ttl_ms: int
    availability_expires_unix_ms: int
    capabilities: frozenset[str]
    available: bool = True
    retire_at_unix_ms: int | None = None

    def is_fresh(self, now_unix_ms: int, *, max_clock_skew_ms: int = DEFAULT_MAX_CLOCK_SKEW_MS) -> bool:
        if type(now_unix_ms) is not int or now_unix_ms < 0:
            raise ValueError("now_unix_ms must be a non-negative integer")
        if type(max_clock_skew_ms) is not int or max_clock_skew_ms < 0:
            raise ValueError("max_clock_skew_ms must be a non-negative integer")
        return (
            self.available
            and self.sent_unix_ms <= now_unix_ms + max_clock_skew_ms
            and now_unix_ms < self.availability_expires_unix_ms
            and (self.retire_at_unix_ms is None or now_unix_ms < self.retire_at_unix_ms)
        )

    def matches_capabilities(self, required: Iterable[str]) -> bool:
        return set(required).issubset(self.capabilities)


def presence_from_mapping(
    payload: Mapping[str, Any], *, now_unix_ms: int | None = None,
    max_clock_skew_ms: int = DEFAULT_MAX_CLOCK_SKEW_MS,
) -> LSPPresence:
    """Parse a complete presence announcement and optionally require freshness."""
    if not isinstance(payload, Mapping) or payload.get("schema") != LSP_PRESENCE_SCHEMA:
        raise ValueError("unsupported or missing LSP presence schema")
    node_id = _text(payload.get("node_id"), "node_id")
    boot_id = _text(payload.get("boot_id"), "boot_id")
    sent = payload.get("sent_unix_ms")
    ttl = _positive_int(payload.get("availability_ttl_ms"), "availability_ttl_ms")
    expires = payload.get("availability_expires_unix_ms")
    if type(sent) is not int or sent < 0 or type(expires) is not int or expires != sent + ttl:
        raise ValueError("presence timestamps and expiry are inconsistent")
    capabilities = _capabilities(payload.get("capabilities"))
    available = payload.get("available")
    if type(available) is not bool:
        raise ValueError("available must be a boolean")
    retire = payload.get("retire_at_unix_ms")
    if retire is not None and (type(retire) is not int or retire < sent):
        raise ValueError("retire_at_unix_ms must be absent or a timestamp after sent_unix_ms")
    presence = LSPPresence(node_id, boot_id, sent, ttl, expires, capabilities, available, retire)
    if now_unix_ms is not None and not presence.is_fresh(now_unix_ms, max_clock_skew_ms=max_clock_skew_ms):
        raise ValueError("LSP presence is stale, retired, unavailable, or clock-invalid")
    return presence


def validate_presence(
    payload: Mapping[str, Any], *, now_unix_ms: int | None = None,
    max_clock_skew_ms: int = DEFAULT_MAX_CLOCK_SKEW_MS,
) -> LSPPresence:
    """Named validation entry point for callers that do not need parsing details."""
    return presence_from_mapping(payload, now_unix_ms=now_unix_ms, max_clock_skew_ms=max_clock_skew_ms)


@dataclass(frozen=True)
class LSPWorkSpec:
    goal_id: str
    work_id: str
    language: str
    required_capabilities: frozenset[str]
    cpus: int
    memory_gib: int
    lease_ttl_ms: int

    def __post_init__(self) -> None:
        _text(self.goal_id, "goal_id")
        _text(self.work_id, "work_id")
        _text(self.language, "language")
        if not self.required_capabilities:
            raise ValueError("required_capabilities must not be empty")
        if any(not isinstance(cap, str) or not cap for cap in self.required_capabilities):
            raise ValueError("required_capabilities must contain non-empty strings")
        _positive_int(self.cpus, "cpus")
        _positive_int(self.memory_gib, "memory_gib")
        _positive_int(self.lease_ttl_ms, "lease_ttl_ms")


@dataclass(frozen=True)
class PlacementDecision:
    admitted: bool
    node_id: str | None
    reason: str


def place_lsp_work(work: LSPWorkSpec, presences: Iterable[LSPPresence], *, now_unix_ms: int,
                   max_clock_skew_ms: int = DEFAULT_MAX_CLOCK_SKEW_MS) -> PlacementDecision:
    """Select the lexically smallest fresh compatible node; no lease is granted."""
    candidates = [p for p in presences if p.is_fresh(now_unix_ms, max_clock_skew_ms=max_clock_skew_ms)
                  and p.matches_capabilities(work.required_capabilities)]
    if not candidates:
        return PlacementDecision(False, None, "no fresh compatible LSP presence")
    selected = min(candidates, key=lambda presence: presence.node_id)
    return PlacementDecision(True, selected.node_id, "fresh compatible LSP presence")


def admit_lsp_work(work: LSPWorkSpec, presence: LSPPresence, *, now_unix_ms: int,
                   max_clock_skew_ms: int = DEFAULT_MAX_CLOCK_SKEW_MS) -> PlacementDecision:
    """Evaluate one target without granting its lease."""
    if not presence.is_fresh(now_unix_ms, max_clock_skew_ms=max_clock_skew_ms):
        return PlacementDecision(False, None, "LSP presence is not fresh")
    if not presence.matches_capabilities(work.required_capabilities):
        return PlacementDecision(False, None, "LSP capabilities do not satisfy work requirements")
    return PlacementDecision(True, presence.node_id, "fresh compatible LSP presence")


class LeaseStatus(str, Enum):
    AVAILABLE = "available"
    HELD = "held"
    RELEASED = "released"
    FENCED = "fenced"


@dataclass(frozen=True)
class TargetLeaseState:
    status: LeaseStatus = LeaseStatus.AVAILABLE
    holder: str | None = None
    token: int = 0
    expires_unix_ms: int | None = None
    in_flight: bool = False


@dataclass(frozen=True)
class LeaseGrant:
    holder: str
    token: int
    expires_unix_ms: int


class TargetLeaseProtocol(ABC):
    """Abstract target-side authority; transport and persistence are external."""

    @abstractmethod
    def acquire(self, holder: str, *, now_unix_ms: int, ttl_ms: int) -> LeaseGrant: ...

    @abstractmethod
    def renew(self, holder: str, token: int, *, now_unix_ms: int, ttl_ms: int) -> LeaseGrant: ...

    @abstractmethod
    def release(self, holder: str, token: int, *, now_unix_ms: int) -> None: ...

    @abstractmethod
    def fencing_token(self) -> int: ...


class LeaseStateMachine(TargetLeaseProtocol):
    """Deterministic reference model for acquire/renew/release/fencing rules."""

    def __init__(self) -> None:
        self.state = TargetLeaseState()

    @staticmethod
    def _check_time(now: int, ttl: int | None = None) -> None:
        if type(now) is not int or now < 0:
            raise ValueError("now_unix_ms must be a non-negative integer")
        if ttl is not None:
            _positive_int(ttl, "ttl_ms")

    def _expire(self, now: int) -> None:
        if self.state.status == LeaseStatus.HELD and self.state.expires_unix_ms is not None and now >= self.state.expires_unix_ms:
            self.state = TargetLeaseState(LeaseStatus.FENCED, token=self.state.token, in_flight=self.state.in_flight)

    def acquire(self, holder: str, *, now_unix_ms: int, ttl_ms: int) -> LeaseGrant:
        holder = _text(holder, "holder")
        self._check_time(now_unix_ms, ttl_ms)
        self._expire(now_unix_ms)
        if self.state.in_flight:
            raise RuntimeError("lease acquisition is blocked by in-flight work")
        if self.state.status == LeaseStatus.HELD:
            raise RuntimeError("lease is already held")
        token = self.state.token + 1
        expiry = now_unix_ms + ttl_ms
        self.state = TargetLeaseState(LeaseStatus.HELD, holder, token, expiry)
        return LeaseGrant(holder, token, expiry)

    def renew(self, holder: str, token: int, *, now_unix_ms: int, ttl_ms: int) -> LeaseGrant:
        holder = _text(holder, "holder")
        self._check_time(now_unix_ms, ttl_ms)
        if type(token) is not int or token < 1 or self.state.status != LeaseStatus.HELD:
            raise RuntimeError("lease is not renewable")
        if holder != self.state.holder or token != self.state.token:
            raise PermissionError("stale or foreign fencing token")
        self._expire(now_unix_ms)
        if self.state.status != LeaseStatus.HELD:
            raise RuntimeError("lease has expired and is fenced")
        expiry = now_unix_ms + ttl_ms
        self.state = TargetLeaseState(LeaseStatus.HELD, holder, token, expiry)
        return LeaseGrant(holder, token, expiry)

    def release(self, holder: str, token: int, *, now_unix_ms: int) -> None:
        holder = _text(holder, "holder")
        self._check_time(now_unix_ms)
        if self.state.status != LeaseStatus.HELD or holder != self.state.holder or token != self.state.token:
            raise PermissionError("stale or foreign fencing token")
        self.state = TargetLeaseState(
            LeaseStatus.FENCED if self.state.in_flight else LeaseStatus.RELEASED,
            token=self.state.token,
            in_flight=self.state.in_flight,
        )

    def fencing_token(self) -> int:
        return self.state.token

    def begin_compute(self, holder: str, token: int, *, now_unix_ms: int) -> None:
        """Mark the single target operation in flight after an authority check."""
        holder = _text(holder, "holder")
        self._check_time(now_unix_ms)
        self._expire(now_unix_ms)
        if (self.state.status != LeaseStatus.HELD or self.state.in_flight
                or holder != self.state.holder or token != self.state.token):
            raise PermissionError("lease is not valid for compute")
        self.state = TargetLeaseState(LeaseStatus.HELD, holder, token, self.state.expires_unix_ms, True)

    def commit_compute(self, holder: str, token: int, *, now_unix_ms: int) -> bool:
        """Return whether result bytes may be committed, then clear in-flight work."""
        holder = _text(holder, "holder")
        self._check_time(now_unix_ms)
        valid = (self.state.status == LeaseStatus.HELD and not self._expired(now_unix_ms)
                 and holder == self.state.holder and token == self.state.token)
        self.state = TargetLeaseState(LeaseStatus.RELEASED if valid else LeaseStatus.FENCED, token=self.state.token)
        return valid

    def abort_compute(self, holder: str, token: int, *, now_unix_ms: int) -> None:
        """Clear in-flight work without making its result authoritative."""
        self._check_time(now_unix_ms)
        if not self.state.in_flight or holder != self.state.holder or token != self.state.token:
            raise PermissionError("stale or foreign fencing token")
        self.state = TargetLeaseState(LeaseStatus.FENCED, token=self.state.token)

    def _expired(self, now: int) -> bool:
        return self.state.expires_unix_ms is None or now >= self.state.expires_unix_ms
