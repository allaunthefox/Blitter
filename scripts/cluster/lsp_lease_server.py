#!/usr/bin/env python3
"""Target-side LSP session lease service.

Exposes the authority-bearing endpoints consumed by
``scripts/cluster/lsp_lease.py`` (``LSPLeaseClient``):

```text
POST /lsp/lease/acquire
POST /lsp/lease/renew
POST /lsp/lease/release
POST /lsp/session/begin    (mark the single fenced session in flight)
POST /lsp/session/commit   (re-validate before diagnostics may commit)
GET  /lsp/lease/status      (observational; never exposes the bearer token)
GET  /health                (liveness only)
```

Authority model (mirrors ``BLITTER_LEASE_FENCING_V1`` ownership semantics for
the LSP session scope):

* one process-local mutex serializes acquire/renew/release/session
  begin/session commit;
* each process creates a fresh unpredictable ``instance_id`` at startup, so a
  restart invalidates every previously issued lease even if an epoch repeats;
* every successful acquisition increments a monotonic ``lease_epoch`` and
  creates a fresh ``session_id`` plus a cryptographically random bearer
  ``lease_token``;
* TTL authority uses the process monotonic clock; ``expires_unix_ms`` is
  advisory wall-clock provenance only;
* a revoked/expired session is fenced at commit and no replacement lease is
  granted while stale work remains in flight.

The token is a bearer secret and MUST NOT appear in status output or logs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from hmac import compare_digest

SCHEMA = "mathpunch.blitter-lsp-lease.v1"
STATUS_SCHEMA = "mathpunch.blitter-lsp-lease-status.v1"

DEFAULT_MIN_TTL_MS = 100
DEFAULT_MAX_TTL_MS = 60 * 60 * 1000
MAX_REQUEST_BYTES = 1 << 20
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class LeaseProtocolError(RuntimeError):
    def __init__(self, code: str, message: str, http_status: int = 409):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclass
class Lease:
    session_id: str
    holder: str
    goal_id: str
    work_id: str
    worker_id: str
    worker_generation: int
    descriptor_digest: str
    image_digest: str
    token: str
    epoch: int
    deadline_ns: int
    expires_unix_ms: int
    ttl_ms: int
    revoked: bool = False


@dataclass(frozen=True)
class LeaseIdentity:
    session_id: str
    instance_id: str
    epoch: int
    token: str


def _label(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256:
        raise LeaseProtocolError(
            "invalid_field", f"{field} must be a non-empty UTF-8 string <=256 bytes", 400
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise LeaseProtocolError(
            "invalid_field", f"{field} contains control characters", 400
        )
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise LeaseProtocolError(
            "invalid_digest", f"{field} must be sha256:<64 lowercase hex>", 400
        )
    return value


def _generation(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise LeaseProtocolError(
            "invalid_generation", "worker_generation must be a non-negative integer", 400
        )
    return value


class LSPLeaseState:
    """Single-process authoritative LSP lease state guarded by one mutex."""

    def __init__(
        self,
        *,
        clock_ns=time.monotonic_ns,
        wall_ms=lambda: time.time_ns() // 1_000_000,
        token_factory=lambda: secrets.token_hex(32),
        session_factory=lambda: secrets.token_hex(16),
        instance_factory=lambda: secrets.token_hex(16),
        min_ttl_ms: int = DEFAULT_MIN_TTL_MS,
        max_ttl_ms: int = DEFAULT_MAX_TTL_MS,
    ):
        if not isinstance(min_ttl_ms, int) or isinstance(min_ttl_ms, bool) or min_ttl_ms <= 0:
            raise ValueError("min_ttl_ms must be a positive integer")
        if not isinstance(max_ttl_ms, int) or isinstance(max_ttl_ms, bool) or max_ttl_ms < min_ttl_ms:
            raise ValueError("max_ttl_ms must be an integer >= min_ttl_ms")
        self._clock_ns = clock_ns
        self._wall_ms = wall_ms
        self._token_factory = token_factory
        self._session_factory = session_factory
        self.instance_id = instance_factory()
        if not isinstance(self.instance_id, str) or len(self.instance_id) < 16:
            raise ValueError("instance_factory must return a non-trivial string")
        self.min_ttl_ms = min_ttl_ms
        self.max_ttl_ms = max_ttl_ms
        self._lock = threading.Lock()
        self._epoch = 0
        self._lease: Lease | None = None
        self._in_flight: LeaseIdentity | None = None

    def _validate_ttl(self, ttl_ms: object) -> int:
        if not isinstance(ttl_ms, int) or isinstance(ttl_ms, bool):
            raise LeaseProtocolError("invalid_ttl", "ttl_ms must be an integer", 400)
        if ttl_ms < self.min_ttl_ms or ttl_ms > self.max_ttl_ms:
            raise LeaseProtocolError(
                "invalid_ttl",
                f"ttl_ms must be in {self.min_ttl_ms}..{self.max_ttl_ms}",
                400,
            )
        return ttl_ms

    def _active_locked(self, now_ns: int) -> bool:
        return (
            self._lease is not None
            and not self._lease.revoked
            and now_ns < self._lease.deadline_ns
        )

    def _cleanup_if_idle_locked(self, now_ns: int) -> None:
        if self._in_flight is None and self._lease is not None:
            if self._lease.revoked or now_ns >= self._lease.deadline_ns:
                self._lease = None

    def acquire(
        self,
        *,
        holder: str,
        goal_id: str,
        work_id: str,
        worker_id: str,
        worker_generation: int,
        descriptor_digest: str,
        image_digest: str,
        ttl_ms: int,
    ) -> dict:
        holder = _label(holder, "holder")
        goal_id = _label(goal_id, "goal_id")
        work_id = _label(work_id, "work_id")
        worker_id = _label(worker_id, "worker_id")
        worker_generation = _generation(worker_generation)
        descriptor_digest = _digest(descriptor_digest, "descriptor_digest")
        image_digest = _digest(image_digest, "image_digest")
        ttl_ms = self._validate_ttl(ttl_ms)
        with self._lock:
            now = self._clock_ns()
            self._cleanup_if_idle_locked(now)
            if self._in_flight is not None:
                raise LeaseProtocolError(
                    "compute_in_flight",
                    "cannot acquire while prior work remains in flight",
                )
            if self._active_locked(now):
                raise LeaseProtocolError("lease_held", "an unexpired lease is already held")
            self._lease = None
            self._epoch += 1
            token = self._token_factory()
            session_id = self._session_factory()
            if not isinstance(token, str) or len(token) < 32:
                raise RuntimeError("token_factory returned an invalid token")
            if not isinstance(session_id, str) or len(session_id) < 16:
                raise RuntimeError("session_factory returned an invalid session id")
            expires_unix_ms = int(self._wall_ms()) + ttl_ms
            self._lease = Lease(
                session_id=session_id,
                holder=holder,
                goal_id=goal_id,
                work_id=work_id,
                worker_id=worker_id,
                worker_generation=worker_generation,
                descriptor_digest=descriptor_digest,
                image_digest=image_digest,
                token=token,
                epoch=self._epoch,
                deadline_ns=now + ttl_ms * 1_000_000,
                expires_unix_ms=expires_unix_ms,
                ttl_ms=ttl_ms,
            )
            return self._lease_response_locked()

    def _validate_identity_locked(self, identity: LeaseIdentity, now_ns: int) -> Lease:
        lease = self._lease
        if identity.instance_id != self.instance_id:
            raise LeaseProtocolError("wrong_instance", "lease instance_id does not match")
        if lease is None:
            raise LeaseProtocolError("no_lease", "no lease is currently held")
        if identity.session_id != lease.session_id:
            raise LeaseProtocolError("wrong_session", "lease session_id does not match")
        if identity.epoch != lease.epoch:
            raise LeaseProtocolError("stale_epoch", "lease epoch does not match")
        if not secrets.compare_digest(lease.token, identity.token):
            raise LeaseProtocolError("wrong_token", "lease token does not match")
        if lease.revoked:
            raise LeaseProtocolError("lease_revoked", "lease has been released/revoked")
        if now_ns >= lease.deadline_ns:
            if self._in_flight is None:
                self._lease = None
            raise LeaseProtocolError("lease_expired", "lease deadline has expired")
        return lease

    def renew(self, identity: LeaseIdentity, *, ttl_ms: int) -> dict:
        ttl_ms = self._validate_ttl(ttl_ms)
        with self._lock:
            now = self._clock_ns()
            lease = self._validate_identity_locked(identity, now)
            lease.ttl_ms = ttl_ms
            lease.deadline_ns = now + ttl_ms * 1_000_000
            lease.expires_unix_ms = int(self._wall_ms()) + ttl_ms
            return self._lease_response_locked()

    def release(self, identity: LeaseIdentity) -> dict:
        with self._lock:
            lease = self._lease
            if identity.instance_id != self.instance_id:
                raise LeaseProtocolError("wrong_instance", "lease instance_id does not match")
            if lease is None:
                raise LeaseProtocolError("no_lease", "no lease is currently held")
            if identity.session_id != lease.session_id:
                raise LeaseProtocolError("wrong_session", "lease session_id does not match")
            if identity.epoch != lease.epoch:
                raise LeaseProtocolError("stale_epoch", "lease epoch does not match")
            if not secrets.compare_digest(lease.token, identity.token):
                raise LeaseProtocolError("wrong_token", "lease token does not match")
            lease.revoked = True
            pending = self._in_flight is not None
            if not pending:
                self._lease = None
            return {
                "schema": SCHEMA,
                "ok": True,
                "released": True,
                "release_pending_in_flight": pending,
                "session_id": identity.session_id,
                "instance_id": self.instance_id,
                "lease_epoch": identity.epoch,
            }

    def begin_session(self, identity: LeaseIdentity) -> None:
        with self._lock:
            now = self._clock_ns()
            self._validate_identity_locked(identity, now)
            if self._in_flight is not None:
                raise LeaseProtocolError(
                    "compute_in_flight", "only one fenced session may be in flight"
                )
            self._in_flight = identity

    def commit_session(self, identity: LeaseIdentity) -> bool:
        """Clear in-flight state; return whether diagnostics may be committed."""
        with self._lock:
            now = self._clock_ns()
            if self._in_flight is None or self._in_flight != identity:
                raise LeaseProtocolError(
                    "no_in_flight_session", "no matching session is in flight"
                )
            lease = self._lease
            authorized = (
                lease is not None
                and identity.instance_id == self.instance_id
                and identity.session_id == lease.session_id
                and lease.epoch == identity.epoch
                and secrets.compare_digest(lease.token, identity.token)
                and not lease.revoked
                and now < lease.deadline_ns
            )
            self._in_flight = None
            if lease is not None and (lease.revoked or now >= lease.deadline_ns):
                if not lease.revoked:
                    lease.revoked = True
                    if self._on_revoke is not None:
                        try:
                            self._on_revoke()
                        except Exception:
                            pass
                self._lease = None
            return authorized

    def abort_session(self, identity: LeaseIdentity) -> None:
        with self._lock:
            if self._in_flight is None or self._in_flight != identity:
                raise LeaseProtocolError(
                    "no_in_flight_session", "no matching session is in flight"
                )
            self._in_flight = None
            if self._lease is not None:
                self._lease.revoked = True
                if self._on_revoke is not None:
                    try:
                        self._on_revoke()
                    except Exception:
                        pass

    def status(self) -> dict:
        with self._lock:
            now = self._clock_ns()
            self._cleanup_if_idle_locked(now)
            lease = self._lease
            active = self._active_locked(now)
            expired = lease is not None and not lease.revoked and now >= lease.deadline_ns
            remaining_ms = None
            holder = worker_generation = session_id = None
            epoch = self._epoch
            if lease is not None:
                holder = lease.holder
                worker_generation = lease.worker_generation
                session_id = lease.session_id
                remaining_ms = max(0, (lease.deadline_ns - now + 999_999) // 1_000_000)
                epoch = lease.epoch
            return {
                "schema": STATUS_SCHEMA,
                "ok": True,
                "instance_id": self.instance_id,
                "lease_epoch": epoch,
                "lease_active": active,
                "lease_expired": expired,
                "lease_holder": holder,
                "lease_remaining_ms": remaining_ms,
                "lease_session_id": session_id,
                "worker_generation": worker_generation,
                "in_flight": self._in_flight is not None,
                "min_ttl_ms": self.min_ttl_ms,
                "max_ttl_ms": self.max_ttl_ms,
            }

    def _lease_response_locked(self) -> dict:
        lease = self._lease
        assert lease is not None
        return {
            "schema": SCHEMA,
            "ok": True,
            "session_id": lease.session_id,
            "instance_id": self.instance_id,
            "lease_token": lease.token,
            "lease_epoch": lease.epoch,
            "expires_unix_ms": lease.expires_unix_ms,
            "worker_generation": lease.worker_generation,
        }


def parse_identity(session_id: object, instance_id: object, epoch: object, token: object) -> LeaseIdentity:
    if not isinstance(session_id, str) or not session_id:
        raise LeaseProtocolError("missing_session", "session_id is required", 400)
    if not isinstance(instance_id, str) or not instance_id:
        raise LeaseProtocolError("missing_instance", "instance_id is required", 400)
    if isinstance(epoch, str):
        if not epoch.isascii() or not epoch.isdigit():
            raise LeaseProtocolError("invalid_epoch", "lease_epoch must be decimal", 400)
        epoch = int(epoch, 10)
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch <= 0:
        raise LeaseProtocolError("invalid_epoch", "lease_epoch must be a positive integer", 400)
    if not isinstance(token, str) or not token:
        raise LeaseProtocolError("missing_token", "lease_token is required", 400)
    return LeaseIdentity(session_id, instance_id, epoch, token)


def parse_listen(raw: str) -> tuple[str, int]:
    if ":" not in raw:
        raise ValueError("listen address must be host:port")
    host, port_raw = raw.rsplit(":", 1)
    try:
        port = int(port_raw, 10)
    except ValueError as exc:
        raise ValueError("listen port must be decimal") from exc
    if not host or not 0 <= port <= 65535:
        raise ValueError("invalid listen host/port")
    return host, port


class LSPLeaseServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, state: LSPLeaseState):
        super().__init__(server_address, LSPLeaseHandler)
        self.lease_state = state


class LSPLeaseHandler(BaseHTTPRequestHandler):
    server_version = "MathPunchBlitterLSPLease/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Never log bearer tokens or request bodies.
        return

    def _send_json(self, status: int, value: dict) -> None:
        data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc: LeaseProtocolError) -> None:
        self._send_json(
            exc.http_status,
            {"schema": SCHEMA, "ok": False, "error": exc.code, "message": str(exc)},
        )

    def _read_body(self) -> bytes:
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            raise LeaseProtocolError("length_required", "Content-Length is required", 411)
        try:
            length = int(raw_len, 10)
        except ValueError as exc:
            raise LeaseProtocolError("invalid_length", "invalid Content-Length", 400) from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise LeaseProtocolError("body_too_large", "request body exceeds limit", 413)
        data = self.rfile.read(length)
        if len(data) != length:
            raise LeaseProtocolError("truncated_body", "request body was truncated", 400)
        return data

    def _read_json_object(self, *, required: set[str]) -> dict:
        data = self._read_body()
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LeaseProtocolError("invalid_json", "request is not valid UTF-8 JSON", 400) from exc
        if not isinstance(value, dict):
            raise LeaseProtocolError("invalid_json", "request JSON must be an object", 400)
        keys = set(value)
        missing = required - keys
        extra = keys - required
        if missing:
            raise LeaseProtocolError("missing_fields", f"missing fields: {sorted(missing)}", 400)
        if extra:
            raise LeaseProtocolError("unknown_fields", f"unknown fields: {sorted(extra)}", 400)
        return value

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(
                200,
                {
                    "schema": SCHEMA,
                    "ok": True,
                    "service": "blitter-lsp-lease",
                    "instance_id": self.server.lease_state.instance_id,
                },
            )
            return
        if self.path == "/lsp/lease/status":
            self._send_json(200, self.server.lease_state.status())
            return
        self._send_json(404, {"schema": SCHEMA, "ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        try:
            if self.path == "/lsp/lease/acquire":
                value = self._read_json_object(
                    required={
                        "goal_id", "work_id", "worker_id", "worker_generation",
                        "descriptor_digest", "image_digest", "holder", "ttl_ms",
                    }
                )
                result = self.server.lease_state.acquire(
                    goal_id=value["goal_id"],
                    work_id=value["work_id"],
                    worker_id=value["worker_id"],
                    worker_generation=value["worker_generation"],
                    descriptor_digest=value["descriptor_digest"],
                    image_digest=value["image_digest"],
                    holder=value["holder"],
                    ttl_ms=value["ttl_ms"],
                )
                self._send_json(200, result)
                return

            if self.path == "/lsp/lease/renew":
                value = self._read_json_object(
                    required={"session_id", "instance_id", "lease_epoch", "lease_token", "ttl_ms"}
                )
                identity = parse_identity(
                    value["session_id"], value["instance_id"],
                    value["lease_epoch"], value["lease_token"],
                )
                result = self.server.lease_state.renew(identity, ttl_ms=value["ttl_ms"])
                self._send_json(200, result)
                return

            if self.path == "/lsp/lease/release":
                value = self._read_json_object(
                    required={"session_id", "instance_id", "lease_epoch", "lease_token"}
                )
                identity = parse_identity(
                    value["session_id"], value["instance_id"],
                    value["lease_epoch"], value["lease_token"],
                )
                result = self.server.lease_state.release(identity)
                self._send_json(200, result)
                return

            if self.path == "/lsp/session/begin":
                value = self._read_json_object(
                    required={"session_id", "instance_id", "lease_epoch", "lease_token"}
                )
                identity = parse_identity(
                    value["session_id"], value["instance_id"],
                    value["lease_epoch"], value["lease_token"],
                )
                self.server.lease_state.begin_session(identity)
                self._send_json(
                    200,
                    {"schema": SCHEMA, "ok": True, "in_flight": True,
                     "session_id": identity.session_id,
                     "lease_epoch": identity.epoch},
                )
                return

            if self.path == "/lsp/session/commit":
                value = self._read_json_object(
                    required={"session_id", "instance_id", "lease_epoch", "lease_token"}
                )
                identity = parse_identity(
                    value["session_id"], value["instance_id"],
                    value["lease_epoch"], value["lease_token"],
                )
                allowed = self.server.lease_state.commit_session(identity)
                if not allowed:
                    self._send_json(
                        409,
                        {"schema": SCHEMA, "ok": False, "error": "fenced_at_commit",
                         "message": "lease authority changed or expired before commit"},
                    )
                    return
                self._send_json(
                    200,
                    {"schema": SCHEMA, "ok": True, "committed": True,
                     "session_id": identity.session_id,
                     "lease_epoch": identity.epoch},
                )
                return

            self._send_json(404, {"schema": SCHEMA, "ok": False, "error": "not_found"})
        except LeaseProtocolError as exc:
            self._error(exc)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default="127.0.0.1:8792")
    parser.add_argument("--min-ttl-ms", type=int, default=DEFAULT_MIN_TTL_MS)
    parser.add_argument("--max-ttl-ms", type=int, default=DEFAULT_MAX_TTL_MS)
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("BLITTER_LSP_LEASE_TOKEN", ""),
        help=(
            "required shared secret for all authority-bearing endpoints. "
            "Set BLITTER_LSP_LEASE_TOKEN or pass --auth-token; the server "
            "refuses to start without one. The token is compared with "
            "hmac.compare_digest on every non-status request."
        ),
    )
    parser.add_argument(
        "--worker-cgroup",
        default=os.environ.get("BLITTER_LSP_LEASE_CGROUP", ""),
        help=(
            "optional cgroup path of the worker process tree. When set, "
            "any restart or revoke tears the worker down by signalling the "
            "cgroup so a stale worker cannot continue producing diagnostics."
        ),
    )
    args = parser.parse_args()
    if not args.auth_token or len(args.auth_token) < 16:
        print(
            "blitter-lsp-lease: refusing to start without an --auth-token "
            "(>=16 chars) or BLITTER_LSP_LEASE_TOKEN. The lease endpoint "
            "is unsafe to expose unauthenticated.",
            flush=True,
        )
        return 2
    try:
        listen = parse_listen(args.listen)

        def _signal_cgroup() -> None:
            """Signal the worker cgroup to tear down on revoke/expire/restart."""
            cgroup = args.worker_cgroup
            if not cgroup:
                return
            with contextlib.suppress(FileNotFoundError, ProcessLookupError, PermissionError, OSError):
                # Linux cgroup v2: write "1" to cgroup.kill to signal every process.
                try:
                    with open(cgroup.rstrip("/") + "/cgroup.kill", "w") as fh:
                        fh.write("1")
                    return
                except FileNotFoundError:
                    pass
                # Fallback: signal every PID in cgroup.procs.
                pids = []
                with open(cgroup.rstrip("/") + "/cgroup.procs") as fh:
                    pids = [int(line) for line in fh.read().split() if line.strip().isdigit()]
                for pid in pids:
                    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                        os.kill(pid, 15)  # SIGTERM first.
                return

        state = LSPLeaseState(
            min_ttl_ms=args.min_ttl_ms, max_ttl_ms=args.max_ttl_ms, on_revoke=_signal_cgroup,
        )
        server = LSPLeaseServer(listen, state)
        server.auth_token = args.auth_token
        server.worker_cgroup = args.worker_cgroup
    except (ValueError, OSError) as exc:
        print(f"blitter-lsp-lease: {exc}", flush=True)
        return 2
    actual = server.server_address
    print(
        json.dumps(
            {
                "schema": SCHEMA,
                "ok": True,
                "service": "blitter-lsp-lease",
                "listen": f"{actual[0]}:{actual[1]}",
                "instance_id": state.instance_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )
    import atexit
    # On graceful shutdown (including a new server starting), the instance_id
    # is already invalid.  Tear the worker down so it cannot continue
    # producing diagnostics against a released lease.
    def _shutdown() -> None:
        try:
            _signal_cgroup()
        except Exception:
            pass
    atexit.register(_shutdown)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
