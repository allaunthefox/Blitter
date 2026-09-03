# Node Presence and Identity V0

Status: design/implementation contract on `feat/webgpu-blitter-occupancy-status`.

## Core rule

**Inventory is memory. Heartbeat is presence. Lease is authority.**

A node may remain in configuration, receipts, provenance, an LLM-visible log, or a historical database indefinitely without being considered currently available. No static record can authorize scheduling.

Formally:

```text
known(node) != present(node) != leaseable(node)
```

`present(node)` requires a fresh, unexpired heartbeat. `leaseable(node)` additionally requires a healthy compatible slot and a successful atomic lease grant.

## Boot-scoped node identity

The heartbeat exposes a human-readable `node_id` containing basic provenance without changing every heartbeat:

```text
<name>|<lifecycle>|<arch>|cpu=<logical>|mem=<MiB>MiB|boot=<boot-prefix>
```

Example:

```text
rental-h100|ephemeral|x86_64|cpu=54|mem=262144MiB|boot=abcd1234
```

The full heartbeat carries alongside that ID:

```json
{
  "node_id": "rental-h100|ephemeral|x86_64|cpu=54|mem=262144MiB|boot=abcd1234",
  "boot_id": "abcd1234-...",
  "lifecycle": "ephemeral",
  "uptime_ms": 1234567,
  "boot_started_unix_ms": 1786700000000,
  "hardware": {
    "arch": "x86_64",
    "logical_cpus": 54,
    "memory_bytes": 274877906944,
    "cpu_model": "...",
    "accelerator_adapter": "...",
    "accelerator_backend": "Vulkan"
  }
}
```

`uptime_ms` is deliberately not embedded in `node_id`; otherwise the identity would change every heartbeat. `boot_id` identifies the uptime generation, while `uptime_ms` records current uptime.

## Lifecycle

`lifecycle` is one of:

- `persistent`: expected long-lived fabric member;
- `ephemeral`: rented/disposable/spot capacity.

This field describes lifecycle only. It never implies current availability.

## Availability is soft state

Every heartbeat carries an explicit presence expiry:

```json
{
  "sent_unix_ms": 1786700000000,
  "availability_ttl_ms": 3500,
  "availability_expires_unix_ms": 1786700003500,
  "retire_at_unix_ms": null
}
```

The default TTL is at least three heartbeat intervals. A receiver must treat the node as unavailable when:

```text
now > availability_expires_unix_ms
```

or, when present:

```text
now >= retire_at_unix_ms
```

A known retirement time is useful for rented machines whose provider lease/end time is known in advance.

An ephemeral node whose heartbeat stops therefore disappears from placement automatically while its historical `node_id`, hardware description, receipts, and prior work remain valid provenance.

## Scheduler rule

Only live heartbeat-derived `SlotPool` objects can enter candidate ranking. The scheduler rejects pools whose heartbeat is:

- expired;
- retired;
- stale;
- clock-invalid / time-anomalous;
- status-unavailable;
- incompatible with the work requirements.

A configuration entry such as `nodes.json` may identify a machine and its expected properties, but it is not evidence that the machine currently exists or is schedulable.

## Failure/recovery implication

If a current ephemeral slot stops renewing its presence, its pool becomes degraded. Checkpointable work may migrate to a live compatible destination after acquiring the destination lease. Non-checkpointable work waits for a semantic/checkpoint boundary or replays from the last certified checkpoint according to the enclosing goal policy.

## LLM/log interpretation rule

Human- and LLM-facing summaries should use time-scoped language:

```text
Observed available at <timestamp>, heartbeat valid until <expiry>.
```

They must not convert a historical statement such as:

```text
rental-h100 participated in goal X
```

into:

```text
rental-h100 is currently available
```

without a current heartbeat/lease observation.
