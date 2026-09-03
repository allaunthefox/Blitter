#!/usr/bin/env python3
"""Lease-intent telemetry for slot-routed MathPunch work.

Expected lease time is an estimate announced by the process.  It is useful for
placement, capacity planning, and anomaly logging, but it is not a fencing TTL
and does not revoke authority by itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_PROFOUND_OVERRUN_FACTOR = 4


@dataclass(frozen=True)
class LeaseIntent:
    goal_id: str
    work_id: str
    expected_lease_ms: int
    checkpointable: bool = False
    profound_overrun_factor: int = DEFAULT_PROFOUND_OVERRUN_FACTOR

    def __post_init__(self) -> None:
        if not self.goal_id or not self.work_id:
            raise ValueError("goal_id and work_id must be non-empty")
        if self.expected_lease_ms < 1:
            raise ValueError("expected_lease_ms must be positive")
        if self.profound_overrun_factor < 2:
            raise ValueError("profound_overrun_factor must be >= 2")

    @property
    def profound_overrun_ms(self) -> int:
        return self.expected_lease_ms * self.profound_overrun_factor

    def announcement(self, *, announced_unix_ms: int) -> dict[str, Any]:
        if announced_unix_ms < 0:
            raise ValueError("announced_unix_ms must be non-negative")
        return {
            "goal_id": self.goal_id,
            "work_id": self.work_id,
            "expected_lease_ms": self.expected_lease_ms,
            "profound_overrun_factor": self.profound_overrun_factor,
            "profound_overrun_ms": self.profound_overrun_ms,
            "checkpointable": self.checkpointable,
            "announced_unix_ms": announced_unix_ms,
        }


@dataclass(frozen=True)
class LeaseBudgetObservation:
    goal_id: str
    work_id: str
    slot_id: str
    started_unix_ms: int
    observed_unix_ms: int
    elapsed_ms: int
    expected_lease_ms: int
    budget_ratio: float
    over_budget: bool
    profoundly_over_budget: bool
    log_note: dict[str, Any] | None


def observe_lease_budget(
    intent: LeaseIntent,
    *,
    slot_id: str,
    started_unix_ms: int,
    observed_unix_ms: int,
    overrun_note_already_emitted: bool = False,
) -> LeaseBudgetObservation:
    """Observe lease runtime and emit at most one advisory overrun log note.

    The returned note is deliberately non-authoritative: an estimate miss does
    not cancel, preempt, or revoke the lease.  Fencing/TTL remains a separate
    protocol concern.
    """
    if not slot_id:
        raise ValueError("slot_id must be non-empty")
    if started_unix_ms < 0 or observed_unix_ms < 0:
        raise ValueError("timestamps must be non-negative")

    elapsed_ms = max(0, observed_unix_ms - started_unix_ms)
    ratio = elapsed_ms / intent.expected_lease_ms
    over_budget = elapsed_ms > intent.expected_lease_ms
    profound = elapsed_ms >= intent.profound_overrun_ms

    note: dict[str, Any] | None = None
    if profound and not overrun_note_already_emitted:
        note = {
            "event": "lease_budget_overrun",
            "severity": "note",
            "goal_id": intent.goal_id,
            "work_id": intent.work_id,
            "slot_id": slot_id,
            "started_unix_ms": started_unix_ms,
            "observed_unix_ms": observed_unix_ms,
            "elapsed_ms": elapsed_ms,
            "expected_lease_ms": intent.expected_lease_ms,
            "profound_overrun_factor": intent.profound_overrun_factor,
            "budget_ratio": ratio,
            "checkpointable": intent.checkpointable,
            "action": "log-only",
        }

    return LeaseBudgetObservation(
        goal_id=intent.goal_id,
        work_id=intent.work_id,
        slot_id=slot_id,
        started_unix_ms=started_unix_ms,
        observed_unix_ms=observed_unix_ms,
        elapsed_ms=elapsed_ms,
        expected_lease_ms=intent.expected_lease_ms,
        budget_ratio=ratio,
        over_budget=over_budget,
        profoundly_over_budget=profound,
        log_note=note,
    )
