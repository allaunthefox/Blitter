# Blitter Target-Side Lease Fencing V1

**Schema:** `mathpunch.blitter-lease-fencing.v1`  
**Status:** protocol contract; source implementation/evidence gated  
**Layer:** ownership/fencing, explicitly outside `BLITTER-ISA-V1` arithmetic semantics

## 1. Purpose

`GET /status` is observational and cannot close the time-of-check/time-of-use race between “idle” and `POST /compute`. V1 therefore places a target-side **fencing gate** in front of a loopback-only blitter daemon.

Recommended deployment stack:

```text
public client
    |
    | HTTPS (optional security plugin)
    v
blitter-security-plugin                 optional transport/stamping layer
    |
    | HTTP on loopback
    v
blitter-lease-gate                      authoritative ownership boundary
    |
    | HTTP on loopback
    v
blitter-daemon                          arithmetic backend
```

For an authority-bearing fenced deployment, direct network access to the inner daemon is forbidden. The daemon must bind only to loopback (for example `127.0.0.1:8791`) and the gate is the only path allowed to submit `/compute`.

If clients can bypass the gate and reach the daemon directly, this specification provides **no exclusive-ownership claim**.

## 2. Separation from expected lease budgets

Existing `expected_seconds` / expected-duration telemetry remains advisory. It is not a TTL and never revokes work.

This protocol introduces a distinct authoritative field:

```text
ttl_ms
```

The difference is normative:

```text
expected duration   telemetry / scheduling hint
ttl_ms              fencing deadline for result authority
```

Expiry does not kill, preempt, or cancel an already-running backend computation. It only prevents that computation from committing a result under the expired lease and prevents a new lease from being granted until the old in-flight computation leaves the gate.

## 3. Lease identity

Each gate process creates a fresh unpredictable `instance_id` at startup. Every successful acquisition increments a process-local monotonically increasing `lease_epoch` and creates a cryptographically random `lease_token`.

A lease identity is the tuple:

```text
(instance_id, lease_epoch, lease_token)
```

All three components are required for renew/release/compute. The token is a bearer secret and MUST NOT appear in heartbeat/status telemetry or logs.

A gate restart changes `instance_id`, invalidating all previously issued leases even if an epoch number repeats.

## 4. Endpoints

### `POST /lease/acquire`

Canonical request:

```json
{
  "holder": "worker-17",
  "ttl_ms": 30000,
  "expected_ms": 12000
}
```

`expected_ms` is optional advisory telemetry and may be `null`. `ttl_ms` is mandatory authority.

Acquisition succeeds atomically only when:

1. no unexpired current lease exists; and
2. no computation from a prior/expired/released lease is still in flight through the gate.

Otherwise it returns a conflict and does not issue a token.

### `POST /lease/renew`

Requires exact current identity and a new `ttl_ms`. Renewal is allowed only **before** the current deadline. An expired lease cannot be resurrected.

### `POST /lease/release`

Requires exact current identity. If no computation is in flight, release clears the lease immediately. If a computation is in flight, release marks it revoked: the gate blocks a replacement lease until that computation returns, and its result is fenced at commit.

### `POST /compute`

The compute body is the existing daemon compute body. Lease identity is carried in headers:

```text
X-Blitter-Lease-Instance: <instance_id>
X-Blitter-Lease-Epoch: <decimal epoch>
X-Blitter-Lease-Token: <bearer token>
```

The gate validates identity/deadline atomically immediately before forwarding to the daemon. It then marks that identity as the single in-flight computation.

When the daemon returns, the gate validates the same identity/deadline **again before releasing any result bytes to the caller**.

If the lease expired, was released/revoked, or no longer matches at result time:

```text
upstream result bytes are discarded
HTTP 409 fenced_at_commit is returned
```

This second check is the commit fence.

## 5. No replacement owner while stale work is still executing

A critical invariant is:

```text
in_flight != null  =>  acquire is blocked
```

This remains true even if the in-flight lease has expired or been released. Therefore a new caller is never told it owns the gated backend while a stale computation is still physically executing through the gate.

V1 deliberately prefers safety over liveness. A backend computation that never returns can block lease acquisition indefinitely. Watchdog/preemption/daemon-restart policy is a separate operational layer and is not claimed here.

## 6. Atomic state machine

The target gate serializes these state transitions under one process-local mutex:

```text
acquire
renew
release/revoke
compute begin
compute commit/abort
```

Network forwarding to the daemon occurs outside the mutex, but `in_flight` remains set for the complete forwarding interval.

The authoritative state is target-local. Heartbeats and external schedulers may mirror it, but cannot issue or reconstruct a valid lease.

## 7. Expiry clock

TTL authority uses a monotonic clock local to the gate process. Wall time, NTP, VM wall-clock jumps, thermal sensors, RAS/ECC telemetry, and the ratchet execution clock do not define lease expiry.

The gate may expose wall-clock timestamps as advisory provenance only.

## 8. Status

`GET /status` may expose:

```text
instance_id
lease_epoch
lease_active
lease_expired
lease_holder
lease_remaining_ms
in_flight
```

It MUST NOT expose `lease_token`.

Status remains observational. Possession of status data does not grant ownership.

## 9. Security plugin composition

The lease gate and security plugin are independent process ABIs/layers.

For a secure website node:

```text
TLS sidecar :443 -> lease gate 127.0.0.1:8790 -> daemon 127.0.0.1:8791
```

TLS authenticates/protects transport according to certificate policy. Lease fencing controls compute ownership. Neither proves arithmetic semantics; arithmetic remains governed by `BLITTER-ISA-V1` and backend conformance evidence.

## 10. Failure semantics

The following fail closed:

- missing/malformed lease headers;
- wrong gate `instance_id`;
- stale/wrong epoch;
- wrong token;
- expired lease;
- second compute while one is in flight;
- acquire while stale/revoked work is still in flight;
- malformed lease JSON;
- non-loopback upstream configuration;
- upstream transport failure;
- lease expiry/release between compute begin and result commit.

There is no automatic fallback to unfenced `/compute`.

## 11. Nonclaims

V1 does not claim:

- TTL preemption or cancellation;
- liveness under a hung daemon;
- multi-gate distributed consensus;
- durability across gate restart;
- protection when the inner daemon is directly reachable by untrusted clients;
- arithmetic correctness of the backend;
- secure transport unless a suitable transport policy/plugin is separately active.
