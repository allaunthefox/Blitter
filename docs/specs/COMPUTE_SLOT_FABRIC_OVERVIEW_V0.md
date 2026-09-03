# Compute Slot Fabric Overview V0

Status: branch architecture and implementation map for `feat/webgpu-blitter-occupancy-status`.

This document defines the compute slot fabric model and records which parts are implemented on the branch. It does **not** imply that every endpoint described here is deployed on every configured machine.

The compute slot fabric treats distributed computation as **one recoverable goal with many movable work items**, rather than as a process permanently attached to one machine. Machines expose candidate **slot pools**. Fresh heartbeats advertise current capacity and suitability; placement ranks candidates; the full architecture requires target-side atomic lease/fencing before a selected slot becomes execution authority.

The governing rule is:

> **Inventory is memory. Heartbeat is presence. Lease is authority.**

Equivalently:

```text
known(node) != present(node) != leaseable(node)
```

A machine may remain in configuration, receipts, provenance, or an LLM-visible log indefinitely without being considered currently available.

## 1. Implementation status

| Surface | Branch state | Authority |
| --- | --- | --- |
| Static inventory | Implemented | Configuration/history only |
| WebGPU `/health` client | Implemented; deployed endpoint must be verified | Liveness only |
| WebGPU `/status` validation / `--require-idle` | Implemented in client; deployment-dependent | Observational occupancy only |
| Heartbeat wire v3 | Implemented in `blitter_heartbeat.py` | Advisory presence/load/timing |
| Heartbeat-to-slot conversion and ranking | Implemented in `slot_scheduler.py` | Advisory placement only |
| Migration decision policy | Implemented in `slot_scheduler.py` | Advisory transition proposal |
| Expected occupancy / overrun telemetry | Implemented in `lease_protocol.py` | Scheduling telemetry only; **not lease fencing** |
| Target-side atomic lease/fencing | **Required design; not implemented by the current Python helpers** | Future execution ownership/fencing |
| Mathematical result promotion | Existing MathPunch authority path | Outside scheduler authority |

The current Python helper named `lease_protocol.py` models expected-duration/overrun telemetry. It does **not** grant lease tokens, fencing epochs, or authoritative TTLs.

## 2. Design goal

A logical goal may use many heterogeneous execution surfaces at once:

```text
                         +-- CPU slot pool
                         +-- WebGPU / Vulkan slot pool
recoverable goal state --+-- NVIDIA slot pool
                         +-- software / verification slot pool
                         `-- future codec-compatible target
                                  |
                                  v
                        receipts / checkpoints / scars
                                  |
                                  v
                         same recoverable goal state
```

The unit of scheduling is a **slot**, not a host. A node may expose zero, one, or many slot pools. A work item may move when a more suitable slot becomes available, provided its semantic state is checkpointable or reaches a safe replay boundary.

This is not a distributed Unix PID. It is a distributed goal state machine whose transitions may execute on interchangeable targets.

## 3. Core objects

### Goal

A goal is the durable semantic owner of the computation. Typical goal state includes:

```text
goal_id
carrier/state digest
frontier or pending operations
completed receipts
checkpoints
exact/advisory authority state
termination predicate
```

Goal identity does not change when hardware placement changes.

### Work item

A work item is a portable transition request belonging to one goal. It carries, as applicable:

```text
goal_id
work_id
required capabilities
preferred capabilities / weights
checkpointability
current semantic state
expected occupancy time
input/carrier digest
codec / operation identity
```

### Node

A node is a transport/location and provenance object. It is not itself scheduling authority.

A heartbeat carries a boot-scoped human-readable `node_id`, for example:

```text
rental-h100|ephemeral|x86_64|cpu=54|mem=262144MiB|boot=abcd1234
```

The heartbeat also carries the full `boot_id`, live `uptime_ms`, boot-start time, CPU model, logical CPU count, memory, and accelerator/backend identity when known.

`uptime_ms` is deliberately **not** embedded in `node_id`: identity must not change every heartbeat. The boot token scopes one uptime generation.

### Slot pool

A slot pool is a placement candidate derived from a fresh heartbeat. It records data such as:

```text
pool_id
node_id
lifecycle
capabilities
slots_total
slots_active
slots_queued
slots_available
heartbeat age
availability expiry
status RTT / apparent skew
hardware metadata
```

The current WebGPU status model naturally maps to a one-slot pool when `max_concurrent_compute == 1`, but the abstraction does not assume one slot per machine or accelerator.

### Heartbeat

A heartbeat is **advisory soft state**. It answers:

> Is this execution surface currently present, what can it do, and how loaded does it appear to be?

It never grants ownership.

### Lease

A lease is the architecture's **target-side execution authority**. A stale heartbeat may cause a failed lease attempt; it must never create duplicate ownership.

The current branch Python helpers do not implement this authoritative lease operation. Until a target-side lease/fencing interface is implemented, deployed, and race-tested, heartbeat/ranking may guide placement but cannot close the status-to-submit race.

### Receipt / checkpoint

A receipt records what ran, where, under which declared codec/backend and state, and what output/state digest resulted. When authoritative lease support exists, the receipt should also bind the concrete lease/fencing identity. Checkpoints make work resumable or movable without changing goal identity.

## 4. Authority layers

| Layer | Purpose | May rank work? | May authorize execution? |
| --- | --- | ---: | ---: |
| Static inventory/config | Names, addresses, expected capabilities | No, by itself | No |
| Historical receipt/log | Provenance | No | No |
| Heartbeat | Presence, load, slot advertisement | Yes | No |
| Suitability score | Placement preference | Yes | No |
| Expected occupancy time | Capacity prediction / anomaly telemetry | Yes | No |
| Target-side atomic lease/fencing | Ownership/fencing | N/A | **Yes, when implemented and verified** |
| MathPunch verifier/authority rules | Mathematical promotion | N/A | Governs result authority |

The scheduler must fail closed whenever an advisory layer is missing, stale, inconsistent, or outside its declared scope.

## 5. Heartbeat wire model

The heartbeat sidecar is implemented in `scripts/cluster/blitter_heartbeat.py`. The branch wire format is version 3.

Conceptually:

```json
{
  "wire_version": 3,
  "service": "webgpu-blitter-heartbeat",
  "node": "rental-h100",
  "node_id": "rental-h100|ephemeral|x86_64|cpu=54|mem=262144MiB|boot=abcd1234",
  "boot_id": "abcd1234-...",
  "lifecycle": "ephemeral",
  "ephemeral": true,
  "uptime_ms": 1234567,
  "boot_started_unix_ms": 1786700000000,
  "heartbeat_seq": 418,
  "sent_unix_ms": 1786701234567,
  "availability_ttl_ms": 5000,
  "availability_expires_unix_ms": 1786701239567,
  "retire_at_unix_ms": 1786710000000,
  "hardware": {
    "arch": "x86_64",
    "logical_cpus": 54,
    "memory_bytes": 274877906944,
    "cpu_model": "...",
    "accelerator_adapter": "...",
    "accelerator_backend": "Vulkan"
  },
  "status_ok": true,
  "busy": false,
  "idle": true,
  "active_compute": 0,
  "queued_compute": 0,
  "max_concurrent_compute": 1
}
```

If the local `/status` read fails, the heartbeat fails closed as unusable capacity.

## 6. Persistent and ephemeral presence

`lifecycle` is either:

- `persistent`: expected long-lived member of the fabric;
- `ephemeral`: rented, disposable, spot, or otherwise temporary capacity.

Lifecycle is provenance, **not presence**. Both require fresh heartbeats to be schedulable.

Every heartbeat has a presence TTL. The receiver treats a node as unavailable when:

```text
now > availability_expires_unix_ms
```

or, when a rental end time is supplied:

```text
now >= retire_at_unix_ms
```

The historical `node_id`, hardware description, receipts, and prior participation remain valid provenance after live availability expires. This prevents a historical statement such as `rental-h100 participated in goal X` from becoming `rental-h100 is currently available` without a fresh observation.

See `NODE_PRESENCE_IDENTITY_V0.md` for the full identity/presence contract.

## 7. Clock and network diagnostics

The sender includes `sent_unix_ms` and monotonically increasing `heartbeat_seq`. The receiver stamps `received_unix_ms` and derives:

```text
apparent_skew_ms = received_unix_ms - sent_unix_ms
sequence_gap
duplicate
reordered
```

`apparent_skew_ms` contains clock offset plus network delay; it is not pure one-way latency.

Epoch-era clocks are explicitly rejected as timing evidence. A sender or receiver timestamp before `2000-01-01T00:00:00Z` is marked as a time anomaly, catching the common Unix-epoch-zero/1969-style failure.

Sequence gaps, duplicate/reordered beats, skew excursions, and invalid clocks are routing-health signals. They do not grant or revoke mathematical authority.

## 8. Placement and slot ranking

`slot_scheduler.py` derives fail-closed slot-pool views from heartbeats. Candidate pools must be:

- fresh and unexpired;
- not retired;
- status-valid;
- timing-sane;
- capability-compatible;
- currently advertising positive available capacity.

Among eligible pools V0 prefers, in order:

1. higher application suitability score;
2. lower queue pressure;
3. lower status RTT;
4. lower absolute apparent skew;
5. deterministic node/pool tie-breaks.

The score expresses **fit**, not truth or authority.

## 9. Dynamic migration

A running goal may be reevaluated as slot announcements change.

For queued work, the architecture may select a better compatible slot immediately; execution still requires that destination's authoritative lease once such a target-side interface exists.

For checkpointable running work, the required safe order is:

```text
materially better slot appears
  -> acquire destination lease FIRST      [required target-side authority]
  -> checkpoint at a safe boundary
  -> verify checkpoint/state digest
  -> resume under destination lease
  -> release source lease
```

A migration hysteresis threshold avoids thrashing between nearly equivalent targets. If the current pool becomes stale, expired, incompatible, or otherwise nonviable, recovery takes priority over ordinary fit hysteresis.

Non-checkpointable work is not killed merely to chase a better score. It re-evaluates at the next semantic boundary or follows the enclosing goal's certified replay policy.

`slot_scheduler.py` implements the placement/migration **decision policy**; it does not itself acquire target-side leases or move arbitrary process memory.

## 10. Expected duration is not lease TTL

A work item may announce:

```json
{
  "expected_lease_ms": 30000,
  "profound_overrun_factor": 4
}
```

Despite the field/file naming, this is an **expected occupancy estimate** for scheduling/capacity prediction and anomaly logs. It is not a fencing timeout.

At the default four-times threshold, `lease_protocol.py` emits one structured `lease_budget_overrun` note. It does not kill, revoke, grant, renew, or fence work.

Authoritative lease TTL/expiry belongs to the target-side atomic lease contract in `SLOT_AVAILABILITY_FABRIC_V0.md` and must be implemented separately.

## 11. Required atomic lease/fencing contract

The architecture requires a target-side atomic operation to close the `status -> submit` race. The current Python helpers do not implement this operation.

An intended request may carry:

```json
{
  "goal_id": "moore-3250",
  "work_id": "frontier-000173",
  "expected_lease_ms": 30000,
  "requested_ttl_ms": 120000,
  "carrier_digest": "...",
  "codec_id": "...",
  "operation_id": "..."
}
```

An authoritative implementation should return a concrete slot/token and fencing epoch, for example:

```json
{
  "ok": true,
  "slot_id": "node:webgpu:0",
  "lease_id": "...",
  "lease_epoch": 91,
  "granted_unix_ms": 1786700000123,
  "expires_unix_ms": 1786700120123
}
```

These examples define the intended contract shape, not a claim that the endpoint is deployed.

Before lease-required routing becomes authoritative, the target implementation needs at least:

- atomic grant/reject behavior per slot;
- target-enforced token/epoch validation on compute;
- expiry/release semantics;
- stale-token fencing;
- concurrent race tests demonstrating one owner per slot;
- receipts that bind the lease identity used for execution.

## 12. Codec-mediated execution

WebGPU/Vulkan is one blitter realization, not the architectural limit.

For a semantic domain `X_T` and target representation `B_T`, a target participates when it has a supported codec and operation contract:

```text
E_T : X_T -> B_T
D_T : B_T -> X_T
```

and an exact operation satisfies its declared commuting relation:

```text
D_T(f_T(E_T(x))) = f(x)
```

or an explicitly documented weaker/projection relation for advisory operations.

This permits heterogeneous arithmetic surfaces to participate when the required blitter algebra and codec witness exist. Scheduling should route by capabilities, exactness, and codec/operation cost rather than by a hard-coded CPU/GPU dichotomy.

A codec declaration does not promote approximate output to exact mathematical authority.

## 13. Failure model

The recoverable goal must survive worker loss. Intended behavior includes:

- heartbeat stops -> availability expires -> pool leaves placement;
- rental reaches known retirement -> remove from placement;
- clock announces epoch/1969-era time -> time anomaly -> exclude from routing;
- heartbeat reordered/dropped -> diagnostics visible; no ownership inference;
- better slot appears -> migrate only through a safe semantic checkpoint/handoff;
- expected-duration estimate is profoundly exceeded -> log anomaly, do not revoke solely from estimate error;
- once target-side leases exist: target loss/lease expiry -> stale lease fenced and recovery follows certified checkpoint/replay policy.

The final bullet is architectural target behavior, not a claim about current Python helper enforcement.

## 14. Implementation files

- `scripts/cluster/blitter_heartbeat.py` — implemented UDP presence/load heartbeat and receive-side timing/sequence diagnostics.
- `scripts/cluster/slot_scheduler.py` — implemented heartbeat-to-slot conversion, ranking, and migration decisions.
- `scripts/cluster/lease_protocol.py` — implemented expected-duration/overrun telemetry only.
- `scripts/cluster/dispatch.py` — bounded node/container dispatcher plus WebGPU health/status/current compute client.
- `scripts/cluster/nodes.json` — static inventory hints; not evidence of presence.
- `scripts/cluster/test_blitter_heartbeat.py` — heartbeat/identity/expiry regression tests.
- `scripts/cluster/test_slot_scheduler.py` — slot presence/rental/migration regression tests.
- `scripts/cluster/test_lease_protocol.py` — expected-duration/overrun tests.

Related specs:

- `SLOT_AVAILABILITY_FABRIC_V0.md`
- `NODE_PRESENCE_IDENTITY_V0.md`
- `NKERNEL_FAMM_*` documents for the broader recoverable-state/authority architecture.

## 15. Verification and reproducibility

Narrow branch checks are:

```bash
python3 scripts/cluster/test_blitter_heartbeat.py
python3 scripts/cluster/test_slot_scheduler.py
python3 scripts/cluster/test_lease_protocol.py
```

Verification reports must name the exact Git revision used. Passing local tests do not prove that Forgejo Actions ran for that revision.

Behavioral/deployment statements must be either exercised against the relevant implementation or marked `UNVERIFIED`/deployment-dependent. In particular, successful `/health` or idle `/status` is not evidence that target-side lease/fencing exists.

## 16. Security and trust boundaries

- Keep credentials and signing material outside tracked configuration and documentation examples.
- Static inventory and historical logs are not live presence.
- Heartbeats and status snapshots are advisory and may be stale or raced.
- Target-side ownership must be enforced by the execution target rather than trusted from a client-side note.
- Receipts/provenance describe what occurred; they do not automatically promote mathematical authority.

## 17. Nonclaims

This layer does not by itself:

- make arbitrary processes migratable;
- turn a heartbeat or `/status` into an exact occupancy reservation;
- implement target-side atomic leases in the current Python helpers;
- make stale inventory trustworthy;
- make approximate target output mathematically authoritative;
- imply that currently deployed daemons implement every branch protocol;
- make a difficult search problem tractable merely by distributing it.

It defines and partially implements a recoverable, fail-closed scheduling substrate in which heterogeneous and ephemeral compute can participate without becoming permanent assumptions in the system's memory. Authoritative execution fencing remains a separate required implementation step.
