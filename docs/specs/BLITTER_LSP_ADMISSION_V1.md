# Blitter LSP Admission Contract v1

**Status:** validation/reference model plus deployed target-side lease service

Interactive dispatch must supply a fresh presence record matching the selected
node and required language capabilities before starting SSH. This is admission
only and does not grant a target-side lease.

This contract defines how an LSP target announces presence, how a scheduler
admits work, and the authority boundary for a target-side lease. Presence and
placement remain endpoint-free models; the authority-bearing endpoints are
implemented by `scripts/cluster/lsp_lease_server.py` (`POST /lsp/lease/acquire`,
`/renew`, `/release`, plus fenced `/lsp/session/begin` and `/lsp/session/commit`),
consumed by `scripts/cluster/lsp_lease.py` and exercised by
`scripts/cluster/test_lsp_lease_server.py` (atomic ownership, fencing, restart
invalidation, concurrent-acquire races).

## Presence

An announcement has schema `mathpunch.blitter-lsp-presence.v1` and must contain
non-empty `node_id` and `boot_id`, non-negative `sent_unix_ms`, a positive
`availability_ttl_ms`, and `availability_expires_unix_ms` exactly equal to
`sent_unix_ms + availability_ttl_ms`. `capabilities` is a non-empty,
duplicate-free list of non-empty strings. `available` is explicitly boolean.

`retire_at_unix_ms`, when present, must not precede `sent_unix_ms`. A presence
is fresh only when `now < availability_expires_unix_ms`, `now < retire_at` (if
present), `available` is true, and `sent_unix_ms` is not more than the allowed
clock-skew window in the future. Boundary expiry is invalid. Malformed,
future-anomalous, expired, retired, and unavailable announcements fail closed.

## Work and placement

`LSPWorkSpec` carries non-empty `goal_id`, `work_id`, `language`, and required
capabilities, plus positive CPU, memory GiB, and authoritative lease TTL
values. A placement decision is admitted only for a fresh presence whose
capabilities are a superset of the work requirements. Selection is
deterministic (lexicographically smallest `node_id`). Placement does not grant
a lease and does not contact or imply a deployed target.

## Target-side lease

`TargetLeaseProtocol` is an abstract interface with `acquire`, `renew`,
`release`, and `fencing_token` operations. A target must:

- atomically acquire only when no unexpired lease is held;
- issue a monotonically increasing fencing token on each acquisition;
- renew only for the current holder and exact current token;
- treat `now >= expiry` as expired, fence the old token, and reject its renew;
- release only for the current holder and exact current token; and
- block replacement acquisition while revoked or expired work remains in flight;
- check the same identity and deadline again at compute commit, discarding a
  fenced result.
- require the fencing token on every authority-bearing target operation.

The included `LeaseStateMachine` is a deterministic reference model for these
rules. It is not a network server, endpoint adapter, or claim of deployment.

Lease expiry and fencing are authority semantics. Advisory expected duration,
worker diagnostics, presence records, and placement decisions cannot revoke or
grant authority.
