#!/usr/bin/env python3
"""Target-side lease/fencing gate for the WebGPU blitter.

The gate is a protocol layer, not arithmetic semantics. It must sit in front of a
loopback-only blitter-daemon for its exclusive-ownership claim to apply.

Safety rule: a lease is checked immediately before upstream compute dispatch and
again before result bytes are committed to the caller. Expired/released work is
fenced at commit, and no replacement lease is granted while any old work remains
in flight through this gate. The upstream is loopback by default; split-container
deployments must explicitly allow one private service hostname.
"""

from __future__ import annotations

import argparse
import dataclasses
import http.client
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCHEMA = "mathpunch.blitter-lease-fencing.v1"
STATUS_SCHEMA = "mathpunch.blitter-lease-status.v1"
LEASE_SCHEMA = "mathpunch.blitter-lease.v1"

DEFAULT_MIN_TTL_MS = 100
DEFAULT_MAX_TTL_MS = 60 * 60 * 1000
MAX_REQUEST_BYTES = 1 << 20
MAX_UPSTREAM_RESPONSE_BYTES = 4 << 20

HEADER_INSTANCE = "X-Blitter-Lease-Instance"
HEADER_EPOCH = "X-Blitter-Lease-Epoch"
HEADER_TOKEN = "X-Blitter-Lease-Token"


def debug_enabled_from_env() -> bool:
    return os.environ.get("BLITTER_DEBUG", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class DebugTrace:
    """Opt-in lifecycle tracing that deliberately omits lease secrets and payloads."""

    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self._lock = threading.Lock()

    def event(self, event: str, **fields) -> None:
        if not self.enabled:
            return
        record = {"component": "blitter-lease-gate", "event": event, **fields}
        with self._lock:
            print(
                json.dumps(record, sort_keys=True, separators=(",", ":")),
                file=sys.stderr,
                flush=True,
            )


class LeaseProtocolError(RuntimeError):
    def __init__(self, code: str, message: str, http_status: int = 409):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclasses.dataclass
class Lease:
    holder: str
    token: str
    epoch: int
    deadline_ns: int
    ttl_ms: int
    expected_ms: int | None
    revoked: bool = False


@dataclasses.dataclass(frozen=True)
class LeaseIdentity:
    instance_id: str
    epoch: int
    token: str


class LeaseState:
    """Single-process authoritative lease state guarded by one mutex."""

    def __init__(
        self,
        *,
        clock_ns=time.monotonic_ns,
        token_factory=lambda: secrets.token_hex(32),
        instance_factory=lambda: secrets.token_hex(16),
        min_ttl_ms=DEFAULT_MIN_TTL_MS,
        max_ttl_ms=DEFAULT_MAX_TTL_MS,
    ):
        if not isinstance(min_ttl_ms, int) or isinstance(min_ttl_ms, bool) or min_ttl_ms <= 0:
            raise ValueError("min_ttl_ms must be a positive integer")
        if not isinstance(max_ttl_ms, int) or isinstance(max_ttl_ms, bool) or max_ttl_ms < min_ttl_ms:
            raise ValueError("max_ttl_ms must be an integer >= min_ttl_ms")
        self._clock_ns = clock_ns
        self._token_factory = token_factory
        self.instance_id = instance_factory()
        if not isinstance(self.instance_id, str) or len(self.instance_id) < 16:
            raise ValueError("instance_factory must return a non-trivial string")
        self.min_ttl_ms = min_ttl_ms
        self.max_ttl_ms = max_ttl_ms
        self._lock = threading.Lock()
        self._epoch = 0
        self._lease: Lease | None = None
        self._in_flight: LeaseIdentity | None = None

    def _validate_ttl(self, ttl_ms: int) -> int:
        if not isinstance(ttl_ms, int) or isinstance(ttl_ms, bool):
            raise LeaseProtocolError("invalid_ttl", "ttl_ms must be an integer", 400)
        if ttl_ms < self.min_ttl_ms or ttl_ms > self.max_ttl_ms:
            raise LeaseProtocolError(
                "invalid_ttl",
                f"ttl_ms must be in {self.min_ttl_ms}..{self.max_ttl_ms}",
                400,
            )
        return ttl_ms

    @staticmethod
    def _validate_expected(expected_ms):
        if expected_ms is None:
            return None
        if not isinstance(expected_ms, int) or isinstance(expected_ms, bool) or expected_ms < 0:
            raise LeaseProtocolError(
                "invalid_expected_ms", "expected_ms must be null or a non-negative integer", 400
            )
        return expected_ms

    @staticmethod
    def _validate_holder(holder: str) -> str:
        if not isinstance(holder, str) or not holder or len(holder.encode("utf-8")) > 256:
            raise LeaseProtocolError(
                "invalid_holder", "holder must be a non-empty UTF-8 string <=256 bytes", 400
            )
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in holder):
            raise LeaseProtocolError("invalid_holder", "holder contains control characters", 400)
        return holder

    def _identity_for(self, lease: Lease) -> LeaseIdentity:
        return LeaseIdentity(self.instance_id, lease.epoch, lease.token)

    @staticmethod
    def _same_identity(a: LeaseIdentity, b: LeaseIdentity) -> bool:
        return a == b

    def _lease_active_locked(self, now_ns: int) -> bool:
        return (
            self._lease is not None
            and not self._lease.revoked
            and now_ns < self._lease.deadline_ns
        )

    def _cleanup_expired_if_idle_locked(self, now_ns: int) -> None:
        if self._in_flight is None and self._lease is not None:
            if self._lease.revoked or now_ns >= self._lease.deadline_ns:
                self._lease = None

    def acquire(self, *, holder: str, ttl_ms: int, expected_ms=None) -> dict:
        holder = self._validate_holder(holder)
        ttl_ms = self._validate_ttl(ttl_ms)
        expected_ms = self._validate_expected(expected_ms)
        with self._lock:
            now = self._clock_ns()
            self._cleanup_expired_if_idle_locked(now)
            if self._in_flight is not None:
                raise LeaseProtocolError(
                    "compute_in_flight",
                    "cannot acquire while prior work remains in flight",
                )
            if self._lease_active_locked(now):
                raise LeaseProtocolError("lease_held", "an unexpired lease is already held")
            # With no in-flight work, any expired/revoked lease can be discarded.
            self._lease = None
            self._epoch += 1
            token = self._token_factory()
            if not isinstance(token, str) or len(token) < 32:
                raise RuntimeError("token_factory returned an invalid token")
            self._lease = Lease(
                holder=holder,
                token=token,
                epoch=self._epoch,
                deadline_ns=now + ttl_ms * 1_000_000,
                ttl_ms=ttl_ms,
                expected_ms=expected_ms,
            )
            return self._lease_response_locked(now, include_token=True)

    def _validate_identity_locked(self, identity: LeaseIdentity, now_ns: int) -> Lease:
        if identity.instance_id != self.instance_id:
            raise LeaseProtocolError("wrong_instance", "lease gate instance_id does not match")
        lease = self._lease
        if lease is None:
            raise LeaseProtocolError("no_lease", "no lease is currently held")
        if lease.epoch != identity.epoch:
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
            # Renewal changes only the deadline. The epoch/token remain stable so
            # one logical ownership interval is one fence identity.
            lease.ttl_ms = ttl_ms
            lease.deadline_ns = now + ttl_ms * 1_000_000
            return self._lease_response_locked(now, include_token=True)

    def release(self, identity: LeaseIdentity) -> dict:
        with self._lock:
            now = self._clock_ns()
            lease = self._lease
            if identity.instance_id != self.instance_id:
                raise LeaseProtocolError("wrong_instance", "lease gate instance_id does not match")
            if lease is None:
                raise LeaseProtocolError("no_lease", "no lease is currently held")
            if lease.epoch != identity.epoch:
                raise LeaseProtocolError("stale_epoch", "lease epoch does not match")
            if not secrets.compare_digest(lease.token, identity.token):
                raise LeaseProtocolError("wrong_token", "lease token does not match")
            lease.revoked = True
            lease.deadline_ns = min(lease.deadline_ns, now)
            pending = self._in_flight is not None
            if not pending:
                self._lease = None
            return {
                "schema": LEASE_SCHEMA,
                "ok": True,
                "released": True,
                "release_pending_in_flight": pending,
                "instance_id": self.instance_id,
                "lease_epoch": identity.epoch,
            }

    def begin_compute(self, identity: LeaseIdentity) -> None:
        with self._lock:
            now = self._clock_ns()
            self._validate_identity_locked(identity, now)
            if self._in_flight is not None:
                raise LeaseProtocolError(
                    "compute_in_flight", "only one fenced computation may be in flight"
                )
            self._in_flight = identity

    def finish_compute(self, identity: LeaseIdentity, *, result_ready: bool) -> bool:
        """Clear in-flight state and return whether result commit is still authorized."""
        with self._lock:
            now = self._clock_ns()
            if self._in_flight is None or not self._same_identity(self._in_flight, identity):
                raise RuntimeError("in-flight identity corruption")
            lease = self._lease
            authorized = (
                result_ready
                and lease is not None
                and identity.instance_id == self.instance_id
                and lease.epoch == identity.epoch
                and secrets.compare_digest(lease.token, identity.token)
                and not lease.revoked
                and now < lease.deadline_ns
            )
            self._in_flight = None
            if lease is not None and (lease.revoked or now >= lease.deadline_ns):
                self._lease = None
            return authorized

    def status(self) -> dict:
        with self._lock:
            now = self._clock_ns()
            self._cleanup_expired_if_idle_locked(now)
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
                "schema": STATUS_SCHEMA,
                "ok": True,
                "instance_id": self.instance_id,
                "lease_epoch": epoch,
                "lease_active": active,
                "lease_expired": expired,
                "lease_holder": holder,
                "lease_remaining_ms": remaining_ms,
                "in_flight": self._in_flight is not None,
                "min_ttl_ms": self.min_ttl_ms,
                "max_ttl_ms": self.max_ttl_ms,
            }

    def _lease_response_locked(self, now_ns: int, *, include_token: bool) -> dict:
        lease = self._lease
        assert lease is not None
        result = {
            "schema": LEASE_SCHEMA,
            "ok": True,
            "instance_id": self.instance_id,
            "lease_epoch": lease.epoch,
            "holder": lease.holder,
            "ttl_ms": lease.ttl_ms,
            "expected_ms": lease.expected_ms,
            "remaining_ms": max(0, (lease.deadline_ns - now_ns + 999_999) // 1_000_000),
        }
        if include_token:
            result["lease_token"] = lease.token
        return result


def parse_identity(instance, epoch, token) -> LeaseIdentity:
    if not isinstance(instance, str) or not instance:
        raise LeaseProtocolError("missing_instance", "lease instance is required", 400)
    if isinstance(epoch, str):
        if not epoch.isascii() or not epoch.isdigit():
            raise LeaseProtocolError("invalid_epoch", "lease epoch must be decimal", 400)
        epoch = int(epoch, 10)
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch <= 0:
        raise LeaseProtocolError("invalid_epoch", "lease epoch must be a positive integer", 400)
    if not isinstance(token, str) or not token:
        raise LeaseProtocolError("missing_token", "lease token is required", 400)
    return LeaseIdentity(instance, epoch, token)


def parse_upstream(raw: str, *, allowed_hosts=frozenset()) -> tuple[str, int]:
    try:
        u = urllib.parse.urlsplit(raw)
    except ValueError as exc:
        raise ValueError(f"invalid upstream URL: {exc}") from exc
    if u.scheme != "http" or u.username is not None or u.password is not None:
        raise ValueError("upstream must be an unauthenticated http URL")
    if u.hostname not in {"127.0.0.1", "localhost", "::1"} and u.hostname not in allowed_hosts:
        raise ValueError("upstream host must be loopback or explicitly allowlisted")
    if u.path not in {"", "/"} or u.query or u.fragment:
        raise ValueError("upstream URL must name only the loopback origin")
    try:
        port = u.port or 80
    except ValueError as exc:
        raise ValueError(f"invalid upstream port: {exc}") from exc
    if not 1 <= port <= 65535:
        raise ValueError("upstream port out of range")
    return u.hostname, port


def parse_loopback_upstream(raw: str) -> tuple[str, int]:
    return parse_upstream(raw)


def parse_listen(raw: str) -> tuple[str, int]:
    if raw.startswith("["):
        end = raw.find("]")
        if end <= 0 or end + 2 > len(raw) or raw[end + 1] != ":":
            raise ValueError("invalid listen address")
        host = raw[1:end]
        port_raw = raw[end + 2 :]
    else:
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


class LeaseGateServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address,
        state: LeaseState,
        upstream: tuple[str, int],
        debug: DebugTrace | None = None,
    ):
        super().__init__(server_address, LeaseGateHandler)
        self.lease_state = state
        self.upstream = upstream
        self.debug = debug or DebugTrace()


class LeaseGateHandler(BaseHTTPRequestHandler):
    server_version = "MathPunchBlitterLeaseGate/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Do not log bearer headers/tokens. Operational callers can add a safe
        # outer access log if needed.
        return

    def _send_json(self, status: int, value: dict):
        data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _trace(self, event: str, **fields) -> None:
        self.server.debug.event(event, **fields)

    def _error(self, exc: LeaseProtocolError):
        self._trace(
            "request.rejected",
            path=self.path,
            code=exc.code,
            status=exc.http_status,
        )
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

    def _read_json_object(self, *, required, optional=frozenset()):
        data = self._read_body()
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LeaseProtocolError("invalid_json", "request is not valid UTF-8 JSON", 400) from exc
        if not isinstance(value, dict):
            raise LeaseProtocolError("invalid_json", "request JSON must be an object", 400)
        keys = set(value)
        missing = set(required) - keys
        extra = keys - set(required) - set(optional)
        if missing:
            raise LeaseProtocolError("missing_fields", f"missing fields: {sorted(missing)}", 400)
        if extra:
            raise LeaseProtocolError("unknown_fields", f"unknown fields: {sorted(extra)}", 400)
        return value

    def do_GET(self):
        self._trace("http.request", method="GET", path=self.path)
        if self.path == "/health":
            self._send_json(
                200,
                {
                    "schema": SCHEMA,
                    "ok": True,
                    "service": "blitter-lease-gate",
                    "instance_id": self.server.lease_state.instance_id,
                },
            )
            return
        if self.path == "/status":
            self._send_json(200, self.server.lease_state.status())
            return
        self._send_json(404, {"schema": SCHEMA, "ok": False, "error": "not_found"})

    def do_POST(self):
        self._trace("http.request", method="POST", path=self.path)
        try:
            if self.path == "/lease/acquire":
                value = self._read_json_object(
                    required={"holder", "ttl_ms"}, optional={"expected_ms"}
                )
                result = self.server.lease_state.acquire(
                    holder=value["holder"],
                    ttl_ms=value["ttl_ms"],
                    expected_ms=value.get("expected_ms"),
                )
                self._trace(
                    "lease.acquired",
                    epoch=result["lease_epoch"],
                    ttl_ms=result["ttl_ms"],
                )
                self._send_json(200, result)
                return

            if self.path == "/lease/renew":
                value = self._read_json_object(
                    required={"instance_id", "lease_epoch", "lease_token", "ttl_ms"}
                )
                identity = parse_identity(
                    value["instance_id"], value["lease_epoch"], value["lease_token"]
                )
                result = self.server.lease_state.renew(identity, ttl_ms=value["ttl_ms"])
                self._trace("lease.renewed", epoch=result["lease_epoch"], ttl_ms=result["ttl_ms"])
                self._send_json(200, result)
                return

            if self.path == "/lease/release":
                value = self._read_json_object(
                    required={"instance_id", "lease_epoch", "lease_token"}
                )
                identity = parse_identity(
                    value["instance_id"], value["lease_epoch"], value["lease_token"]
                )
                result = self.server.lease_state.release(identity)
                self._trace("lease.released", epoch=result["lease_epoch"])
                self._send_json(200, result)
                return

            if self.path == "/compute":
                self._compute()
                return

            self._send_json(404, {"schema": SCHEMA, "ok": False, "error": "not_found"})
        except LeaseProtocolError as exc:
            self._error(exc)

    def _compute(self):
        identity = parse_identity(
            self.headers.get(HEADER_INSTANCE),
            self.headers.get(HEADER_EPOCH),
            self.headers.get(HEADER_TOKEN),
        )
        body = self._read_body()
        state = self.server.lease_state
        state.begin_compute(identity)
        self._trace(
            "compute.started",
            epoch=identity.epoch,
            body_bytes=len(body),
        )
        result_ready = False
        upstream_status = 502
        upstream_body = b""
        upstream_content_type = "application/json"
        try:
            host, port = self.server.upstream
            conn = http.client.HTTPConnection(host, port, timeout=300)
            try:
                conn.request(
                    "POST",
                    "/compute",
                    body=body,
                    headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
                )
                response = conn.getresponse()
                upstream_status = response.status
                upstream_content_type = response.getheader("Content-Type") or "application/json"
                upstream_body = response.read(MAX_UPSTREAM_RESPONSE_BYTES + 1)
                if len(upstream_body) > MAX_UPSTREAM_RESPONSE_BYTES:
                    raise OSError("upstream response exceeds gate limit")
                result_ready = True
            finally:
                conn.close()
        except Exception as exc:
            commit_allowed = state.finish_compute(identity, result_ready=False)
            assert not commit_allowed
            self._trace(
                "compute.upstream_failed",
                epoch=identity.epoch,
                error_type=type(exc).__name__,
            )
            self._send_json(
                502,
                {
                    "schema": SCHEMA,
                    "ok": False,
                    "error": "upstream_failure",
                    "message": str(exc)[:300],
                },
            )
            return

        commit_allowed = state.finish_compute(identity, result_ready=result_ready)
        if not commit_allowed:
            self._trace("compute.fenced", epoch=identity.epoch)
            self._send_json(
                409,
                {
                    "schema": SCHEMA,
                    "ok": False,
                    "error": "fenced_at_commit",
                    "message": "lease authority changed or expired before result commit",
                },
            )
            return

        self._trace(
            "compute.completed",
            epoch=identity.epoch,
            status=upstream_status,
            response_bytes=len(upstream_body),
        )
        self.send_response(upstream_status)
        self.send_header("Content-Type", upstream_content_type)
        self.send_header("Content-Length", str(len(upstream_body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(upstream_body)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default="127.0.0.1:8790")
    parser.add_argument("--upstream", default="http://127.0.0.1:8791")
    parser.add_argument("--allow-upstream-host", action="append", default=[])
    parser.add_argument("--min-ttl-ms", type=int, default=DEFAULT_MIN_TTL_MS)
    parser.add_argument("--max-ttl-ms", type=int, default=DEFAULT_MAX_TTL_MS)
    parser.add_argument("--debug", action="store_true", default=debug_enabled_from_env())
    args = parser.parse_args()
    debug = DebugTrace(args.debug)
    try:
        listen = parse_listen(args.listen)
        upstream = parse_upstream(args.upstream, allowed_hosts=frozenset(args.allow_upstream_host))
        state = LeaseState(min_ttl_ms=args.min_ttl_ms, max_ttl_ms=args.max_ttl_ms)
        server = LeaseGateServer(listen, state, upstream, debug=debug)
    except (ValueError, OSError) as exc:
        print(f"blitter-lease-gate: {exc}", flush=True)
        return 2

    actual = server.server_address
    debug.event(
        "gate.started",
        listen=f"{actual[0]}:{actual[1]}",
        upstream=args.upstream,
        min_ttl_ms=args.min_ttl_ms,
        max_ttl_ms=args.max_ttl_ms,
    )
    print(
        json.dumps(
            {
                "schema": SCHEMA,
                "ok": True,
                "service": "blitter-lease-gate",
                "listen": f"{actual[0]}:{actual[1]}",
                "upstream": args.upstream,
                "instance_id": state.instance_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
