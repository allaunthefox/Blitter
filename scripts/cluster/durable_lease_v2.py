#!/usr/bin/env python3
"""Route-scoped durable leases and idempotent response storage.

This module deliberately contains no HTTP integration. Callers reserve a
canonical request binding, mark execution running, and durably commit the exact
response before exposing it to a client.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

SCHEMA = "mathpunch.blitter-route-lease-idempotency.v2"
_TOKEN_BYTES = 32
_SALT_BYTES = 16
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_MAX_LABEL_BYTES = 1024


class State(str, Enum):
    RESERVED = "RESERVED"
    RUNNING = "RUNNING"
    UNKNOWN = "UNKNOWN"
    COMMITTED = "COMMITTED"
    FENCED = "FENCED"


class LeaseStoreError(RuntimeError):
    """A fail-closed state transition or validation failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExactResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True)
class Reservation:
    state: State
    method: str
    route: str
    idempotency_key: str
    binding_digest: str
    boot_generation: int
    attempt_epoch: int
    lease_token: str | None = None
    response: ExactResponse | None = None

    @property
    def replayed(self) -> bool:
        return self.state is State.COMMITTED


def canonical_bytes(record: Mapping[str, Any]) -> bytes:
    """Encode a deterministic ASCII JSON record, rejecting ambiguous values."""
    _validate_json(record)
    return (
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def request_binding_digest(method: str, route: str, request: Mapping[str, Any]) -> str:
    """Bind an immutable request record to its normalized method and route."""
    method = _method(method)
    route = _route(route)
    value = {"schema": SCHEMA, "method": method, "route": route, "request": request}
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _validate_json(value: Any) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        raise ValueError("canonical records do not permit floating-point values")
    if isinstance(value, list):
        for item in value:
            _validate_json(item)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("canonical record keys must be strings")
            _validate_json(item)
        return
    raise ValueError(f"value of type {type(value).__name__} is not canonical JSON")


def _label(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise LeaseStoreError("invalid_field", f"{name} must be a non-empty string")
    if len(value.encode("utf-8")) > _MAX_LABEL_BYTES:
        raise LeaseStoreError("invalid_field", f"{name} is too long")
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
        raise LeaseStoreError("invalid_field", f"{name} contains control characters")
    return value


def _method(value: object) -> str:
    method = _label(value, "method").upper()
    if not method.isascii() or not method.isalpha():
        raise LeaseStoreError("invalid_method", "method must contain only ASCII letters")
    return method


def _route(value: object) -> str:
    route = _label(value, "route")
    if not route.startswith("/") or "?" in route or "#" in route:
        raise LeaseStoreError(
            "invalid_route", "route must be an absolute path without query or fragment"
        )
    return route


def _digest(value: object) -> str:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        raise LeaseStoreError("invalid_binding", "binding_digest must be sha256:<64 hex>")
    try:
        bytes.fromhex(value[7:])
    except ValueError as exc:
        raise LeaseStoreError("invalid_binding", "binding_digest must be sha256:<64 hex>") from exc
    if value != value.lower():
        raise LeaseStoreError("invalid_binding", "binding_digest must use lowercase hex")
    return value


def _token_hash(token: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        token.encode("ascii"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=32,
    )


class DurableLeaseStore:
    """Transactional SQLite authority for route-scoped idempotent attempts."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        token_factory: Callable[[], str] = lambda: secrets.token_hex(_TOKEN_BYTES),
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.path = Path(path)
        if str(self.path) == ":memory:":
            raise ValueError("a filesystem path is required for durable state")
        if not self.path.parent.is_dir():
            raise ValueError("database parent directory does not exist")
        self._clock_ms = clock_ms
        self._token_factory = token_factory
        self._fault_hook = fault_hook
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            mode = self._db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise RuntimeError("SQLite refused WAL journal mode")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._initialize()
        except BaseException:
            self._db.close()
            raise

    def _initialize(self) -> None:
        with self._transaction():
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS lease_meta ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton=1), boot_generation INTEGER NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS lease_records ("
                "method TEXT NOT NULL, route TEXT NOT NULL, idempotency_key TEXT NOT NULL, "
                "binding_digest TEXT NOT NULL, state TEXT NOT NULL, boot_generation INTEGER NOT NULL, "
                "attempt_epoch INTEGER NOT NULL, lease_expires_ms INTEGER, token_salt BLOB, "
                "token_hash BLOB, response_status INTEGER, response_headers BLOB, response_body BLOB, "
                "updated_ms INTEGER NOT NULL, PRIMARY KEY(method, route, idempotency_key), "
                "CHECK(state IN ('RESERVED','RUNNING','UNKNOWN','COMMITTED','FENCED')))"
            )
            self._db.execute(
                "INSERT INTO lease_meta(singleton, boot_generation) VALUES(1, 0) "
                "ON CONFLICT(singleton) DO NOTHING"
            )
            self._db.execute(
                "UPDATE lease_meta SET boot_generation=boot_generation+1 WHERE singleton=1"
            )
            self.boot_generation = int(
                self._db.execute(
                    "SELECT boot_generation FROM lease_meta WHERE singleton=1"
                ).fetchone()[0]
            )
            now = self._now()
            # A RUNNING attempt may have crossed its side-effect boundary. Never retry it automatically.
            self._db.execute(
                "UPDATE lease_records SET state='UNKNOWN', lease_expires_ms=NULL, "
                "token_salt=NULL, token_hash=NULL, updated_ms=? WHERE state='RUNNING'",
                (now,),
            )
            self._db.execute(
                "UPDATE lease_records SET state='FENCED', lease_expires_ms=NULL, "
                "token_salt=NULL, token_hash=NULL, updated_ms=? WHERE state='RESERVED'",
                (now,),
            )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._fault("before_transaction_commit")
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _fault(self, point: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point)

    def _now(self) -> int:
        value = self._clock_ms()
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RuntimeError("clock_ms returned an invalid timestamp")
        return value

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> "DurableLeaseStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def reserve(
        self,
        *,
        method: str,
        route: str,
        idempotency_key: str,
        binding_digest: str,
        lease_ms: int,
    ) -> Reservation:
        method, route = _method(method), _route(route)
        key, digest = _label(idempotency_key, "idempotency_key"), _digest(binding_digest)
        if not isinstance(lease_ms, int) or isinstance(lease_ms, bool) or lease_ms <= 0:
            raise LeaseStoreError("invalid_lease", "lease_ms must be a positive integer")
        with self._transaction():
            now = self._now()
            row = self._row(method, route, key)
            if row is not None:
                if not hmac.compare_digest(row["binding_digest"], digest):
                    raise LeaseStoreError(
                        "binding_conflict", "idempotency key is bound to a different request"
                    )
                state = State(row["state"])
                if state is State.COMMITTED:
                    return self._reservation(row, response=self._response(row))
                if state is State.RUNNING and now >= row["lease_expires_ms"]:
                    self._set_terminal(row, State.UNKNOWN, now)
                    state = State.UNKNOWN
                if state is State.RESERVED and now >= row["lease_expires_ms"]:
                    self._set_terminal(row, State.FENCED, now)
                    state = State.FENCED
                if state in (State.RESERVED, State.RUNNING):
                    raise LeaseStoreError("in_progress", "an attempt is already in progress")
                if state is State.UNKNOWN:
                    raise LeaseStoreError("unknown_outcome", "prior attempt outcome is unknown")
                epoch = int(row["attempt_epoch"]) + 1
            else:
                epoch = 1
            token = self._token_factory()
            if not isinstance(token, str) or len(token) < 43 or not token.isascii():
                raise RuntimeError("token_factory returned an invalid token")
            salt = secrets.token_bytes(_SALT_BYTES)
            token_hash = _token_hash(token, salt)
            expires = now + lease_ms
            self._db.execute(
                "INSERT INTO lease_records(method,route,idempotency_key,binding_digest,state,"
                "boot_generation,attempt_epoch,lease_expires_ms,token_salt,token_hash,updated_ms) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(method,route,idempotency_key) DO UPDATE SET "
                "binding_digest=excluded.binding_digest,state=excluded.state,"
                "boot_generation=excluded.boot_generation,attempt_epoch=excluded.attempt_epoch,"
                "lease_expires_ms=excluded.lease_expires_ms,token_salt=excluded.token_salt,"
                "token_hash=excluded.token_hash,response_status=NULL,response_headers=NULL,"
                "response_body=NULL,updated_ms=excluded.updated_ms",
                (method, route, key, digest, State.RESERVED.value, self.boot_generation,
                 epoch, expires, salt, token_hash, now),
            )
            self._fault("after_reserve_write")
            return Reservation(
                State.RESERVED, method, route, key, digest, self.boot_generation, epoch, token
            )

    def begin(self, reservation: Reservation) -> Reservation:
        expired = False
        with self._transaction():
            row = self._authorize(reservation, State.RESERVED)
            now = self._now()
            if now >= row["lease_expires_ms"]:
                self._set_terminal(row, State.FENCED, now)
                expired = True
            else:
                self._db.execute(
                    "UPDATE lease_records SET state='RUNNING', updated_ms=? "
                    "WHERE method=? AND route=? AND idempotency_key=?",
                    (now, row["method"], row["route"], row["idempotency_key"]),
                )
                self._fault("after_begin_write")
                return Reservation(
                    State.RUNNING, reservation.method, reservation.route,
                    reservation.idempotency_key, reservation.binding_digest,
                    reservation.boot_generation, reservation.attempt_epoch, reservation.lease_token,
                )
        if expired:
            raise LeaseStoreError("lease_expired", "reservation lease has expired")

    def commit(
        self,
        reservation: Reservation,
        *,
        status: int,
        headers: Iterable[tuple[str, str]],
        body: bytes,
    ) -> ExactResponse:
        response = _exact_response(status, headers, body)
        encoded_headers = canonical_bytes({"headers": [list(item) for item in response.headers]})
        expired = False
        with self._transaction():
            row = self._authorize(reservation, State.RUNNING)
            now = self._now()
            if now >= row["lease_expires_ms"]:
                self._set_terminal(row, State.FENCED, now)
                expired = True
            else:
                self._db.execute(
                    "UPDATE lease_records SET state='COMMITTED', lease_expires_ms=NULL,token_salt=NULL,"
                    "token_hash=NULL,response_status=?,response_headers=?,response_body=?,updated_ms=? "
                    "WHERE method=? AND route=? AND idempotency_key=?",
                    (response.status, encoded_headers, response.body, now, row["method"], row["route"],
                     row["idempotency_key"]),
                )
                self._fault("after_response_write")
        if expired:
            raise LeaseStoreError("fenced_at_commit", "lease expired before durable commit")
        return response

    def fence(self, reservation: Reservation) -> None:
        """Fence a known attempt. Running work becomes UNKNOWN, never retryable by accident."""
        with self._transaction():
            row = self._authorize(reservation, (State.RESERVED, State.RUNNING))
            target = State.UNKNOWN if State(row["state"]) is State.RUNNING else State.FENCED
            self._set_terminal(row, target, self._now())

    def resolve_unknown(self, *, method: str, route: str, idempotency_key: str) -> None:
        """Operator assertion that an UNKNOWN attempt did not commit external effects."""
        method, route = _method(method), _route(route)
        key = _label(idempotency_key, "idempotency_key")
        with self._transaction():
            row = self._row(method, route, key)
            if row is None or State(row["state"]) is not State.UNKNOWN:
                raise LeaseStoreError("invalid_state", "record is not UNKNOWN")
            self._set_terminal(row, State.FENCED, self._now())

    def inspect(self, *, method: str, route: str, idempotency_key: str) -> dict[str, Any] | None:
        """Return a deterministic, secret-free record for diagnostics."""
        method, route = _method(method), _route(route)
        key = _label(idempotency_key, "idempotency_key")
        with self._lock:
            row = self._row(method, route, key)
            if row is None:
                return None
            return {
                "schema": SCHEMA,
                "method": row["method"],
                "route": row["route"],
                "idempotency_key": row["idempotency_key"],
                "binding_digest": row["binding_digest"],
                "state": row["state"],
                "boot_generation": row["boot_generation"],
                "attempt_epoch": row["attempt_epoch"],
                "updated_ms": row["updated_ms"],
            }

    def _row(self, method: str, route: str, key: str) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT * FROM lease_records WHERE method=? AND route=? AND idempotency_key=?",
            (method, route, key),
        ).fetchone()

    def _authorize(
        self, reservation: Reservation, expected: State | tuple[State, ...]
    ) -> sqlite3.Row:
        method, route = _method(reservation.method), _route(reservation.route)
        key, digest = _label(reservation.idempotency_key, "idempotency_key"), _digest(
            reservation.binding_digest
        )
        row = self._row(method, route, key)
        if row is None:
            raise LeaseStoreError("not_found", "reservation does not exist")
        expected_states = (expected,) if isinstance(expected, State) else expected
        if State(row["state"]) not in expected_states:
            raise LeaseStoreError("invalid_state", "reservation is not in the required state")
        if row["boot_generation"] != self.boot_generation or reservation.boot_generation != self.boot_generation:
            raise LeaseStoreError("stale_boot", "reservation belongs to a stale boot generation")
        if row["attempt_epoch"] != reservation.attempt_epoch:
            raise LeaseStoreError("stale_attempt", "reservation attempt epoch is stale")
        if not hmac.compare_digest(row["binding_digest"], digest):
            raise LeaseStoreError("binding_conflict", "request binding does not match")
        token = reservation.lease_token
        if not isinstance(token, str):
            raise LeaseStoreError("wrong_token", "lease token does not match")
        supplied = _token_hash(token, bytes(row["token_salt"]))
        if not hmac.compare_digest(bytes(row["token_hash"]), supplied):
            raise LeaseStoreError("wrong_token", "lease token does not match")
        return row

    def _set_terminal(self, row: sqlite3.Row, state: State, now: int) -> None:
        self._db.execute(
            "UPDATE lease_records SET state=?,lease_expires_ms=NULL,token_salt=NULL,token_hash=NULL,"
            "updated_ms=? WHERE method=? AND route=? AND idempotency_key=?",
            (state.value, now, row["method"], row["route"], row["idempotency_key"]),
        )

    def _response(self, row: sqlite3.Row) -> ExactResponse:
        decoded = json.loads(bytes(row["response_headers"]).decode("ascii"))
        return ExactResponse(
            int(row["response_status"]),
            tuple((str(name), str(value)) for name, value in decoded["headers"]),
            bytes(row["response_body"]),
        )

    def _reservation(self, row: sqlite3.Row, *, response: ExactResponse | None = None) -> Reservation:
        return Reservation(
            State(row["state"]), row["method"], row["route"], row["idempotency_key"],
            row["binding_digest"], int(row["boot_generation"]), int(row["attempt_epoch"]),
            response=response,
        )


def _exact_response(
    status: object, headers: Iterable[tuple[str, str]], body: object
) -> ExactResponse:
    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
        raise LeaseStoreError("invalid_response", "response status must be in 100..599")
    if not isinstance(body, bytes):
        raise LeaseStoreError("invalid_response", "response body must be bytes")
    normalized: list[tuple[str, str]] = []
    for item in headers:
        if not isinstance(item, tuple) or len(item) != 2:
            raise LeaseStoreError("invalid_response", "headers must be (name, value) tuples")
        name, value = item
        if not isinstance(name, str) or not name or not name.isascii() or any(
            char in name for char in "\r\n:"
        ):
            raise LeaseStoreError("invalid_response", "response header name is invalid")
        if not isinstance(value, str) or "\r" in value or "\n" in value:
            raise LeaseStoreError("invalid_response", "response header value is invalid")
        normalized.append((name, value))
    return ExactResponse(status, tuple(normalized), body)
