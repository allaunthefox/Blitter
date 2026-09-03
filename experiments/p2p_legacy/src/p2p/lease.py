"""Target-side lease/fencing gate, adapted for the p2p fabric."""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Optional


class LeaseProtocolError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code


@dataclass
class Lease:
    holder: str
    token: str
    epoch: int
    deadline_ns: int
    ttl_ms: int
    expected_ms: Optional[int]
    revoked: bool = False


@dataclass(frozen=True)
class LeaseIdentity:
    instance_id: str
    epoch: int
    token: str


class LeaseState:
    """Single-process authoritative lease state guarded by one mutex."""

    def __init__(self, *, min_ttl_ms: int = 100, max_ttl_ms: int = 3_600_000, clock_ns=None):
        self._clock_ns = clock_ns or time.monotonic_ns
        self._token_factory = lambda: secrets.token_hex(32)
        self._instance_factory = lambda: secrets.token_hex(16)
        self.instance_id = self._instance_factory()
        self.min_ttl_ms = min_ttl_ms
        self.max_ttl_ms = max_ttl_ms
        self._lock = threading.Lock()
        self._epoch = 0
        self._lease: Optional[Lease] = None
        self._in_flight: Optional[LeaseIdentity] = None

    def acquire(self, *, holder: str, ttl_ms: int, expected_ms=None) -> dict:
        if not isinstance(ttl_ms, int) or isinstance(ttl_ms, bool) or ttl_ms <= 0:
            raise LeaseProtocolError("invalid_ttl", "ttl_ms must be a positive integer")
        if ttl_ms < self.min_ttl_ms or ttl_ms > self.max_ttl_ms:
            raise LeaseProtocolError("invalid_ttl", f"ttl_ms must be in {self.min_ttl_ms}..{self.max_ttl_ms}")
        now = self._clock_ns()
        self._cleanup_idle_locked(now)
        if self._in_flight is not None:
            raise LeaseProtocolError("compute_in_flight", "cannot acquire while prior work remains in flight")
        if self._lease_active_locked(now):
            raise LeaseProtocolError("lease_held", "an unexpired lease is already held")
        self._lease = None
        self._epoch += 1
        token = self._token_factory()
        self._lease = Lease(
            holder=holder, token=token, epoch=self._epoch,
            deadline_ns=now + ttl_ms * 1_000_000, ttl_ms=ttl_ms, expected_ms=expected_ms,
        )
        lease = self._lease
        remaining = max(0, (lease.deadline_ns - now + 999_999) // 1_000_000)
        return {
            "ok": True, "instance_id": self.instance_id, "lease_epoch": lease.epoch,
            "holder": lease.holder, "ttl_ms": lease.ttl_ms, "remaining_ms": remaining,
            "lease_token": lease.token,
        }

    def _lease_active_locked(self, now_ns: int) -> bool:
        lease = self._lease
        return lease is not None and not lease.revoked and now_ns < lease.deadline_ns

    def _cleanup_idle_locked(self, now_ns: int) -> None:
        if self._in_flight is None and self._lease is not None:
            if self._lease.revoked or now_ns >= self._lease.deadline_ns:
                self._lease = None

    def _validate_identity_locked(self, identity: LeaseIdentity, now_ns: int) -> Lease:
        if identity.instance_id != self.instance_id:
            raise LeaseProtocolError("wrong_instance", "instance_id mismatch")
        lease = self._lease
        if lease is None:
            raise LeaseProtocolError("no_lease", "no lease held")
        if lease.epoch != identity.epoch:
            raise LeaseProtocolError("stale_epoch", "stale epoch")
        if not secrets.compare_digest(lease.token, identity.token):
            raise LeaseProtocolError("wrong_token", "wrong token")
        if lease.revoked:
            raise LeaseProtocolError("lease_revoked", "lease revoked")
        if now_ns >= lease.deadline_ns:
            if self._in_flight is None:
                self._lease = None
            raise LeaseProtocolError("lease_expired", "lease expired")
        return lease

    def renew(self, identity: LeaseIdentity, *, ttl_ms: int) -> dict:
        now = self._clock_ns()
        lease = self._validate_identity_locked(identity, now)
        lease.ttl_ms = ttl_ms
        lease.deadline_ns = now + ttl_ms * 1_000_000
        return {
            "ok": True, "instance_id": self.instance_id, "lease_epoch": lease.epoch,
            "remaining_ms": max(0, (lease.deadline_ns - now + 999_999) // 1_000_000),
            "lease_token": lease.token,
        }

    def release(self, identity: LeaseIdentity) -> dict:
        with self._lock:
            now = self._clock_ns()
            lease = self._lease
            if identity.instance_id != self.instance_id or lease is None or lease.epoch != identity.epoch:
                raise LeaseProtocolError("wrong_identity", "identity mismatch")
            if not secrets.compare_digest(lease.token, identity.token):
                raise LeaseProtocolError("wrong_token", "wrong token")
            lease.revoked = True
            lease.deadline_ns = min(lease.deadline_ns, now)
            pending = self._in_flight is not None
            if not pending:
                self._lease = None
            return {"ok": True, "released": True, "release_pending_in_flight": pending}

    def begin_compute(self, identity: LeaseIdentity) -> None:
        with self._lock:
            now = self._clock_ns()
            self._validate_identity_locked(identity, now)
            if self._in_flight is not None:
                raise LeaseProtocolError("compute_in_flight", "only one fenced computation")
            self._in_flight = identity

    def finish_compute(self, identity: LeaseIdentity, *, result_ready: bool) -> bool:
        with self._lock:
            now = self._clock_ns()
            if self._in_flight is None or self._in_flight != identity:
                raise LeaseProtocolError("no_lease", "no in-flight computation")
            lease = self._lease
            authorized = (result_ready and lease is not None
                          and identity.instance_id == self.instance_id
                          and lease.epoch == identity.epoch
                          and secrets.compare_digest(lease.token, identity.token)
                          and not lease.revoked and now < lease.deadline_ns)
            self._in_flight = None
            if lease is not None and (lease.revoked or now >= lease.deadline_ns):
                self._lease = None
            return authorized

    def status(self) -> dict:
        with self._lock:
            now = self._clock_ns()
            self._cleanup_idle_locked(now)
            lease = self._lease
            active = self._lease_active_locked(now)
            expired = lease is not None and not lease.revoked and now >= lease.deadline_ns
            remaining_ms = None
            holder = None
            epoch = self._epoch
            if lease is not None:
                holder = lease.holder
                remaining_ms = max(0, (lease.deadline_ns - now + 999_999) // 1_000_000)
                epoch = lease.epoch
            return {
                "ok": True, "instance_id": self.instance_id, "lease_epoch": epoch,
                "lease_active": active, "lease_expired": expired,
                "lease_holder": holder, "lease_remaining_ms": remaining_ms,
                "in_flight": self._in_flight is not None,
            }
