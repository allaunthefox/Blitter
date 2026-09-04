# Blitter LSP Lease Service V1

**Schema:** `mathpunch.blitter-lsp-lease.v1`  
**Status:** IMPLEMENTED — `scripts/cluster/lsp_lease_server.py`  
**Authority:** target-side atomic fencing for interactive LSP sessions

## 1. Purpose

This protocol is the authority-bearing fence for interactive language-server sessions.
It is distinct from `BLITTER_LEASE_FENCING_V1`, which governs the webgpu daemon's
atomic compute lease. Both use the same concepts (instance_id, epoch, bearer token,
in_flight serialization) but operate at different scopes:

| Dimension | WebGPU lease gate (`BLITTER_LEASE_FENCING_V1`) | LSP lease service (this spec) |
|---|---|---|
| Scope | One compute request | One interactive LSP session |
| Compute path | `POST /compute` (HTTP body) | SSH tunneled LSP protocol (byte-transparent) |
| Fence point | Commit at result return | `session_commit` before diagnostics may be committed |
| Expiry action | Discard result bytes, return 409 | Raise `LeaseProtocolError`, diagnostics dropped |

The LSP lease service does not run the LSP server itself. It holds the authoritative
lease while a dispatch process tunnels the LSP session over SSH. The lease service's
job is to ensure only one fenced session exists per worker at a time, and that when
a lease expires, is revoked, or the server restarts, no stale worker can commit
diagnostics.

## 2. Recommended deployment stack

```text
dispatch host (blitter)
    |
    | SSH + exec + container
    v
worker container
    |
    | localhost HTTP
    v
lsp_lease_server.py (loopback)
    |
    | Optional: behind operator's TLS/auth proxy
    v
external control plane
```

The lease service is designed to bind to loopback only (`127.0.0.1:8792` default).
The dispatcher runs on the same host as the lease server or reaches it via a loopback
alias. In production, operators may place an authenticated HTTPS proxy in front of it;
the lease service itself requires a bearer token on all authority-bearing endpoints.

## 3. Authentication

All authority-bearing endpoints require a bearer token in the `Authorization` header:

```text
Authorization: Bearer <secret>
```

The token is passed via `--auth-token` on the server or the `BLITTER_LSP_LEASE_TOKEN`
environment variable. The server refuses to start without one.

The token is compared using `hmac.compare_digest` to prevent timing attacks.
The token is a bearer secret: it must not appear in logs, status output, or
error messages.

The observational endpoints `GET /lsp/lease/status` and `GET /health` do **not**
require authentication and do not expose the bearer token.

## 4. Instance identity

Each lease-server process creates a fresh unpredictable `instance_id` at startup.
This is a cryptographically random 32-character hex string.

**Restart invalidation**: when the lease server process restarts (e.g., for an
upgrade), the new process gets a new `instance_id`. All previously issued leases
are immediately invalid even if an epoch number repeats. A stale worker holding
an old token cannot renew or release — the `instance_id` check fails.

The `instance_id` is exposed in the startup JSON and in `GET /status`.

## 5. Lease identity

A lease is identified by:

```text
(instance_id, lease_epoch, session_id, lease_token)
```

- `instance_id`: process-scoped random ID; changes on every server restart.
- `lease_epoch`: monotonic integer, incremented on each successful `acquire`.
- `session_id`: random 32-character hex string, created fresh per acquisition.
- `lease_token`: 64-character hex bearer secret, created fresh per acquisition.

All four components are required for `renew`, `release`, `session_begin`, and
`session_commit`. The token is a bearer secret and MUST NOT appear in status
output or logs.

## 6. Lifecycle

```text
dispatch (acquires lease)
    |
    v
POST /lsp/lease/acquire
    | -> issues lease_token + session_id
    v
SSH tunnel starts (worker is reachable)
    |
    v
POST /lsp/session/begin
    | -> marks fenced session in flight; blocks other acquires
    v
[worker processes LSP requests; diagnostics buffered]
    |
    v
POST /lsp/session/commit
    | -> re-validates lease; if OK, diagnostics may be committed
    v
POST /lsp/lease/release
    | -> clears in_flight; new acquire unblocked
    v
SSH tunnel ends
```

The fence is at `session_commit`: if the lease has expired, been revoked, or the
server has restarted (new `instance_id`), `session_commit` returns `409` with
`error: fenced_at_commit` and the caller must drop all buffered diagnostics.

## 7. Endpoints

### `POST /lsp/lease/acquire`

Acquire an authoritative lease for one interactive session.

Required fields:

```json
{
  "goal_id": "string",
  "work_id": "string",
  "worker_id": "string",
  "worker_generation": 0,
  "descriptor_digest": "sha256:<64 hex>",
  "image_digest": "sha256:<64 hex>",
  "holder": "string",
  "ttl_ms": 30000
}
```

`ttl_ms` must be between 100ms and 3600000ms (1 hour). The worker must already
be running and reachable before `acquire` is called.

Acquisition fails (409) if a lease already exists and is not expired. The `status`
endpoint may be queried observationally but never grants a lease.

Response on success:

```json
{
  "schema": "mathpunch.blitter-lsp-lease.v1",
  "ok": true,
  "session_id": "<32 hex>",
  "instance_id": "<32 hex>",
  "lease_token": "<64 hex>",
  "lease_epoch": 1,
  "expires_unix_ms": 1730000000000
}
```

The token is a bearer secret and MUST NOT be stored in logs.

### `POST /lsp/lease/renew`

Extend the TTL of an active lease. Must be called before the current TTL expires
if the session is still active.

Required fields: `session_id`, `instance_id`, `lease_epoch`, `lease_token`, `ttl_ms`.

Renewal fails (409) if the lease has expired, was released, or the `instance_id`
does not match the current server.

### `POST /lsp/lease/release`

Relinquish a lease and free the worker for a new acquisition.

Required fields: `session_id`, `instance_id`, `lease_epoch`, `lease_token`.

If a fenced session is still in flight (no `session_commit` yet), the lease is
marked revoked but the `in_flight` state is not cleared. A replacement lease
cannot be acquired until the stale session commits or the server restarts.
This is the `release_while_in_flight` safety property.

Response includes `release_pending_in_flight: true` when a stale session is still
executing.

### `POST /lsp/session/begin`

Mark the fenced session in flight. This is the **fence point**: after `begin`,
no other `acquire` can succeed until this session commits or is aborted.

Required fields: `session_id`, `instance_id`, `lease_epoch`, `lease_token`.

Fails (409) if another session is already in flight, if the lease has expired,
or if the identity does not match.

**This is the critical gap closed by the session lifecycle.** Without `begin`,
a second dispatch could acquire a lease while the first worker's SSH tunnel is
still open, resulting in two workers producing diagnostics for the same goal.

### `POST /lsp/session/commit`

Re-validate the lease before committing any diagnostics.

Required fields: `session_id`, `instance_id`, `lease_epoch`, `lease_token`.

Returns `200` with `committed: true` only if all of these hold:

- The session is the current `in_flight` session (matches `instance_id`, `session_id`,
  `lease_epoch`, and `lease_token`);
- The lease has not expired;
- The lease has not been revoked (by `release` or server restart).

Returns `409` with `error: fenced_at_commit` if the lease was revoked or expired
before commit. The caller **must** drop all buffered diagnostics in this case.

**This is the second fence point.** It prevents a worker that survived a lease
expiry from committing stale results.

### `GET /lsp/lease/status`

Observational status. Never requires authentication. Never exposes the bearer token.

```json
{
  "schema": "mathpunch.blitter-lsp-lease-status.v1",
  "ok": true,
  "instance_id": "<32 hex>",
  "lease_epoch": 1,
  "lease_active": false,
  "lease_expired": false,
  "lease_holder": null,
  "lease_remaining_ms": null,
  "in_flight": false
}
```

`status` is observational only. It does not grant a lease or prove exclusive
ownership. Possession of status data does not confer authority.

### `GET /health`

Liveness probe. Returns `200` with `{"ok": true}`. Never requires authentication.

## 8. Worker termination on revoke/restart

When the lease server detects a revoke, TTL expiry, or process shutdown, it
optionally signals the worker process tree via a cgroup path specified with
`--worker-cgroup` or `BLITTER_LSP_LEASE_CGROUP`.

The termination uses:

1. Linux cgroup-v2 `cgroup.kill` (preferred): writing `"1"` to the cgroup's
   `cgroup.kill` file signals all processes in the cgroup atomically;
2. Per-process fallback: read `cgroup.procs`, send `SIGTERM` to each PID, then
   `SIGKILL` after a grace period.

On process shutdown (`atexit`), the server also calls the termination handler so
that a graceful restart (new server starting) tears down the old worker.

## 9. Atomicity

All state transitions are serialized by one process-local `threading.Lock`:

```text
acquire
renew
release
session_begin
session_commit
```

Network forwarding (SSH tunnel) happens outside the mutex, but `in_flight` state
is held for the complete duration and re-checked atomically at `commit`.

## 10. TTL authority

TTL uses the process monotonic clock (`time.monotonic_ns`). Wall clock,
NTP, VM wall-clock jumps, and the ratchet execution clock do not define
lease expiry.

The `expires_unix_ms` field is advisory wall-clock provenance only.

## 11. Failure table

| Condition | Result |
|---|---|
| Missing Authorization header | `401 missing_auth` |
| Wrong bearer token | `403 invalid_auth` |
| `acquire` while lease active | `409 compute_in_flight` |
| `acquire` while stale session in flight | `409 stale_session_in_flight` |
| `renew` after expiry | `409 lease_expired` |
| `renew` with wrong `instance_id` | `409 wrong_instance` |
| `renew` with wrong token | `409 wrong_token` |
| `session_begin` while another session in flight | `409 compute_in_flight` |
| `session_commit` with revoked/expired lease | `409 fenced_at_commit` |
| `session_commit` without prior `begin` | `409 no_in_flight_session` |
| `release` with wrong `instance_id` | `409 wrong_instance` |
| Malformed JSON | `400 invalid_field` |

## 12. Relation to `BLITTER_LEASE_FENCING_V1`

This service uses the same conceptual primitives (instance_id, epoch, token,
in_flight serialization, monotonic TTL) but replaces the webgpu `/compute` body
forwarding with an LSP-specific two-phase session lifecycle:

- `session_begin` replaces "mark in flight at compute start";
- `session_commit` replaces "fence at result commit";
- The LSP protocol runs over an SSH tunnel, not HTTP body forwarding.

Both contracts share the same `BLITTER_LEASE_FENCING_V1` authority model: one
process-local mutex, process-local `instance_id`, monotonic fencing epochs, and
monotonic TTL clock.

## 13. Nonclaims

This service does not claim:

- That the lease server itself is the authority for the LSP server's correctness;
- Arithmetic semantics of diagnostics (governed by `BLITTER-ISA-V1`);
- Worker uptime or that a worker survives a lease revoke (the operator must
  configure `--worker-cgroup` and ensure the worker process is in the specified
  cgroup);
- Distributed consensus across multiple lease servers (one server per worker,
  or partitioned by worker_id in a multi-worker deployment);
- Protection when the dispatcher bypasses the lease service and reaches the
  worker directly.
