# Blitter Route-Aware Durable Lease and Idempotency V2

**Schema:** `mathpunch.blitter-route-lease-idempotency.v2`  
**Status:** state/store layer implemented in `scripts/cluster/durable_lease_v2.py`  
**Scope:** durable request admission and exact response commit; no HTTP wiring

## 1. Authority and identity

The idempotency namespace is the tuple `(uppercase method, route, idempotency_key)`.
Routes are absolute paths without query strings or fragments. A key used on another
method or route is independent. Within one namespace, the key is permanently bound
to a SHA-256 digest of the canonical request record. Reuse with another digest is a
`binding_conflict` and cannot alter the original record.

Each executable reservation carries `(boot_generation, attempt_epoch, lease_token)`.
The database increments `boot_generation` transactionally whenever a store starts.
`attempt_epoch` increments whenever a safely retryable `FENCED` record is reserved
again. Tokens contain at least 256 random bits, are returned only on a new
reservation, and are stored only as salted scrypt hashes. Token checks are
constant-time. Tokens and token hashes are absent from inspection records and must
never be logged.

## 2. Deterministic binding

`request_binding_digest` hashes canonical ASCII JSON containing the schema, method,
route, and immutable request record. Object keys are sorted, separators are compact,
and the record ends in LF. Floats and non-JSON values are rejected so equivalent
integer request records cannot vary by encoder or platform.

Callers that bind transport headers, principals, or content encodings must include
those authority-relevant values in the immutable request record. The state layer
does not guess which HTTP fields are semantic.

## 3. States

| State | Meaning | Automatic retry |
|---|---|---|
| `RESERVED` | Admitted, but the execution boundary has not been crossed | Yes, after fencing/expiry |
| `RUNNING` | Execution may have produced effects | No |
| `UNKNOWN` | A running attempt was interrupted and its outcome is not proven | No |
| `COMMITTED` | Exact response is durably stored | Replay only |
| `FENCED` | Attempt authority is revoked and late begin/commit is rejected | Yes, with next epoch |

Normative transitions are:

```text
new -> RESERVED -> RUNNING -> COMMITTED
          |           |
          v           v
       FENCED       FENCED       (observed expiry at commit)
                      |
running + restart ----+----> UNKNOWN
UNKNOWN -- explicit operator proof of no effects --> FENCED
FENCED -- reserve same binding --> RESERVED (attempt_epoch + 1)
COMMITTED -- reserve same binding --> exact replay
```

Fencing a live `RUNNING` attempt produces `UNKNOWN`; it does not assert that effects
did not occur. The commit-expiry path can use `FENCED` because the still-present
caller observed and rejected the response at the durable commit boundary. An
`UNKNOWN` record requires explicit reconciliation before another attempt.

## 4. Durable commit rule

The caller must not expose result bytes before `commit` succeeds. One SQLite
`BEGIN IMMEDIATE` transaction stores status, ordered headers (including duplicates
and original case), body bytes, and `COMMITTED`. A later matching reservation returns
those exact values without issuing a lease token or running work. Errors and other
non-2xx responses may be committed and replayed in the same way.

A different binding conflicts in every state, including after commit. Concurrent
`RESERVED` or `RUNNING` use reports `in_progress`. `UNKNOWN` reports
`unknown_outcome`. Stale boot generations, epochs, states, bindings, and tokens fail
closed.

## 5. SQLite durability

The database uses:

```text
PRAGMA journal_mode=WAL
PRAGMA synchronous=FULL
PRAGMA foreign_keys=ON
BEGIN IMMEDIATE for every mutation
```

The database file and its WAL must reside on storage that honors SQLite fsync. This
is single-database coordination, not distributed consensus and not a guarantee for
broken filesystems or copied database files.

## 6. Startup recovery

Startup increments `boot_generation` and performs recovery in the same transaction:

- `RESERVED` becomes `FENCED`, because execution was never declared started.
- `RUNNING` becomes `UNKNOWN`, because a crash may have occurred after an external
  effect but before response commit.
- `UNKNOWN`, `COMMITTED`, and `FENCED` remain unchanged.

Old tokens cannot authorize work after startup. Committed response bytes remain
replayable across any number of starts. Recovery deliberately prefers safety over
automatic liveness for unknown outcomes.

## 7. Integration boundary and nonclaims

HTTP wiring must canonicalize its route before calling this layer, authenticate the
caller, enforce request/body limits, avoid secret logging, call `begin` immediately
before the effect boundary, and expose bytes only after `commit` returns. None of
that wiring is included in V2 yet.

This layer does not cancel backend work, make external side effects transactional,
resolve `UNKNOWN` outcomes automatically, provide distributed consensus, or prove
semantic correctness of a computation.
