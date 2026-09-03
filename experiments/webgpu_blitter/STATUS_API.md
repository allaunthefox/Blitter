# WebGPU blitter occupancy status

Status: implementation contract on `feat/webgpu-blitter-occupancy-status`.
The branch source now includes the controlled-spawn extension described below;
the Rust daemon has **not yet been rebuilt or deployed from these changes**, so
operators must verify the target version before relying on the new fields.

The blitter service separates **liveness**, **occupancy**, and **spawn
accessibility**:

- `GET /health` answers whether the daemon and selected adapter are alive.
- `GET /status` answers whether the daemon currently owns or is waiting for its compute critical section and, on the updated branch source, reports the narrow execution surface that the daemon itself has established.
- `POST /compute` retains the Laurent-product request/response contract.
- `scripts/cluster/blitter_heartbeat.py` can actively announce validated `/status` snapshots to explicit network peers without changing the compute ABI.

The controlled-spawn model is defined in
[`../../docs/specs/CONTROLLED_SPAWN_SEMANTICS_V0.md`](../../docs/specs/CONTROLLED_SPAWN_SEMANTICS_V0.md).

## `GET /status`

Example idle response from the updated branch contract:

```json
{
  "ok": true,
  "service": "webgpu-blitter",
  "busy": false,
  "idle": true,
  "active_compute": 0,
  "queued_compute": 0,
  "max_concurrent_compute": 1,
  "compute_requests_total": 143,
  "completed_compute": 141,
  "failed_compute": 2,
  "status_seq": 584,
  "started_unix_ms": 1786700000000,
  "uptime_ms": 812345,
  "current_job_started_unix_ms": null,
  "last_job_finished_unix_ms": 1786700812000,
  "adapter": "AMD Radeon Graphics (RADV RENOIR)",
  "backend": "Vulkan",
  "device_type": "IntegratedGpu",
  "spawn_semantics_version": 1,
  "accessibility_profile": "sandbox",
  "controlled_subsystems": ["accelerator-api"],
  "execution_surfaces": ["webgpu"],
  "slot_capabilities": [
    "exact-i32",
    "laurent-product-v1",
    "surface:webgpu",
    "webgpu-blitter"
  ]
}
```

The numeric values are illustrative. The controlled-spawn fields above reflect
the **narrow claim made by this daemon source**: successful WebGPU adapter/device
initialization establishes a usable WebGPU API path. It does not establish that
the process controls the host kernel, container runtime, CPU model, firmware,
physical accelerator, or provider. `accessibility_profile="sandbox"` is a spawn
envelope/routing classification, not a trust or security score.

When `BLITTER_ADAPTER` is unset, the daemon ranks usable adapters by device
type, preferring discrete, integrated, and virtual GPUs over CPU/software
adapters. It retries device initialization for each candidate and uses the
LLVM-backed llvmpipe adapter when no higher-ranked candidate is usable. Setting
`BLITTER_ADAPTER` is an explicit node-surface override and disables fallback to
other adapter names.

Occupancy invariants:

```text
busy == (active_compute != 0 || queued_compute != 0)
idle == !busy
max_concurrent_compute == 1   # current daemon implementation
```

`active_compute` covers the interval in which a request owns the process-wide GPU compute mutex. `queued_compute` covers valid compute requests that have entered the daemon but are waiting for that mutex. Status requests do not acquire that mutex, so they remain answerable while a compute request is in flight.

`status_seq` increases on occupancy/counter transitions and is useful for detecting that state changed between observations. It is not a lease token and is not a ratchet execution clock.

## Debug lifecycle trace

Set `BLITTER_DEBUG=1` or pass `--debug` to enable newline-delimited JSON
diagnostics on stderr. The daemon traces adapter startup, request IDs, parsing,
queue entry, dispatch, completion, failures, and socket errors. The lease gate
traces request decisions, lease acquire/renew/release, upstream dispatch,
fencing, and upstream failures.

Trace records contain sizes, counters, epochs, and error types, but never compute
payloads, lease tokens, or bearer credentials. Debug tracing is opt-in and does
not change the HTTP contract or grant any host/device authority.

## Controlled-spawn extension

The four spawn-envelope fields form one versioned extension:

```text
spawn_semantics_version
accessibility_profile
controlled_subsystems
execution_surfaces
```

The heartbeat validator rejects a **partial** extension. When the extension is
present, every advertised execution surface must also have a matching
`surface:<name>` entry in `slot_capabilities`.

For this WebGPU daemon:

```text
accessibility_profile = sandbox
controlled_subsystems = { accelerator-api }
execution_surfaces    = { webgpu }
```

A richer NixOS-controlled worker may advertise more controlled subsystems and
more validated surfaces, such as `native`, `fhs`, `lxc`, `kvm`, `qemu-tcg`, or
`wasm`. Those capabilities must be established by their own probes/resolvers;
the WebGPU daemon does not infer them from provider or inventory metadata.

The terminal condition is not "native access unavailable". A validated portable
surface such as WebGPU, WASM-WebGPU, or WASM may remain schedulable even when
deeper host control is unavailable. `accessibility_profile="none"` plus no
validated execution surfaces is the terminal spawn state.

## Advisory network heartbeat

The heartbeat sidecar turns local occupancy and spawn accessibility into a small active network announcement. It uses explicit **unicast UDP peers** rather than LAN broadcast/multicast so the contract works across routed/Tailscale links as well as a local subnet.

The current heartbeat wire contract in `scripts/cluster/blitter_heartbeat.py` remains **wire version 3**. The controlled-spawn fields are an additive extension identified by `spawn_semantics_version=1`; the outer heartbeat wire version is intentionally not churned merely to add this orthogonal contract.

A pre-extension version-3 heartbeat is still accepted. When a successful live
read comes from an older WebGPU daemon that lacks the extension, the sidecar
normalizes only what that read establishes:

```text
accessibility_profile = unknown
controlled_subsystems = {}
execution_surfaces    = { webgpu }
slot_capabilities     += { surface:webgpu }
```

That preserves portable WebGPU work without inventing deeper control. If the
local `/status` read fails or is malformed, the announcement instead fails
closed as:

```text
status_ok=false
busy=true
idle=false
controlled_subsystems={}
execution_surfaces={}
slot_capabilities={}
```

Thus an old identity or stale inventory entry never resurrects a previously
observed execution surface.

Example sender:

```bash
python3 scripts/cluster/blitter_heartbeat.py announce \
  --status-url http://127.0.0.1:8790/status \
  --advertise-url http://100.79.14.103:8790 \
  --node nixos-laptop \
  --target 100.88.57.96:8791
```

Example receiver:

```bash
python3 scripts/cluster/blitter_heartbeat.py listen --bind 0.0.0.0:8791
```

An updated version-3 heartbeat may carry:

```json
{
  "wire_version": 3,
  "service": "webgpu-blitter-heartbeat",
  "node": "nixos-laptop",
  "node_id": "nixos-laptop|persistent|x86_64|cpu=8|mem=16384MiB|boot=abcd1234",
  "boot_id": "abcd1234-0000-0000-0000-000000000000",
  "lifecycle": "persistent",
  "ephemeral": false,
  "uptime_ms": 812345,
  "boot_started_unix_ms": 1786700000000,
  "availability_ttl_ms": 3500,
  "availability_expires_unix_ms": 1786700815845,
  "retire_at_unix_ms": null,
  "hardware": {
    "arch": "x86_64",
    "logical_cpus": 8,
    "memory_bytes": 17179869184,
    "cpu_model": "example",
    "accelerator_adapter": "AMD Radeon Graphics (RADV RENOIR)",
    "accelerator_backend": "Vulkan"
  },
  "instance_id": "nixos-laptop|persistent|x86_64|cpu=8|mem=16384MiB|boot=abcd1234:1786700000000:1234",
  "heartbeat_seq": 418,
  "sent_unix_ms": 1786700812345,
  "status_ok": true,
  "busy": false,
  "idle": true,
  "active_compute": 0,
  "queued_compute": 0,
  "max_concurrent_compute": 1,
  "status_seq": 584,
  "status_rtt_ms": 2,
  "spawn_semantics_version": 1,
  "accessibility_profile": "sandbox",
  "controlled_subsystems": ["accelerator-api"],
  "execution_surfaces": ["webgpu"],
  "slot_capabilities": [
    "exact-i32",
    "laurent-product-v1",
    "surface:webgpu",
    "webgpu-blitter"
  ],
  "advertise_url": "http://100.79.14.103:8790"
}
```

The receiver adds diagnostics including:

```text
received_unix_ms
apparent_skew_ms = received_unix_ms - sent_unix_ms
sender_clock_invalid
receiver_clock_invalid
time_anomaly
time_anomaly_reason
availability_expired
retired
available_by_announcement
sequence_gap
reordered
duplicate
source_ip
source_port
```

`apparent_skew_ms` is intentionally named **apparent** skew. A one-way heartbeat cannot separate sender/receiver wall-clock offset from network delay. A stable offset with a sudden excursion is still useful evidence of network trouble; `heartbeat_seq`, `sequence_gap`, `reordered`, and `duplicate` distinguish missing/reordered traffic from a simple steady clock offset. `instance_id` prevents a sender restart and sequence reset from being misclassified as reordering.

Epoch-era wall clocks are explicitly flagged. The current sidecar treats timestamps before 2000-01-01 UTC as invalid clock evidence and annotates a time anomaly rather than silently accepting them.

Availability is soft state. `availability_expires_unix_ms` and optional `retire_at_unix_ms` prevent historical identity or an old rental from being interpreted as current capacity. Identity/provenance may persist after availability expires; schedulability does not.

Heartbeats are **advisory presence/load transport**. Their live controlled-subsystem and execution-surface declarations participate in hard placement eligibility, but a heartbeat still does not reserve a device, grant a lease, or promote mathematical evidence.

## Placement use

A work item may require both mathematical capabilities and a controlled spawn
envelope:

```json
{
  "requires": ["webgpu-blitter", "exact-i32"],
  "requires_control": ["accelerator-api"],
  "acceptable_surfaces": ["webgpu", "wasm-webgpu", "wasm"]
}
```

`slot_scheduler.py` first checks the mathematical capability set, then requires
`requires_control` to be a subset of the pool's `controlled_subsystems`, and
requires an intersection with `acceptable_surfaces` when that set is nonempty.
Only after hard eligibility does accessibility rank participate as a cost
preference. Existing work with an empty `requires_control` remains compatible
with a legacy live WebGPU heartbeat.

## Ratchet-clock relation

[`../../docs/specs/RATCHET_EXECUTION_CLOCK_V0.md`](../../docs/specs/RATCHET_EXECUTION_CLOCK_V0.md) defines a separate design-only semantic action clock. `heartbeat_seq` and `status_seq` are intentionally **not** that clock.

When a future ratchet implementation records all three counters, gaps or regressions can be cross-correlated:

```text
ratchet gap + heartbeat gap        -> possible transport/provider interruption
ratchet gap + heartbeat contiguous -> possible execution/process discontinuity
heartbeat reordering + ratchet ok  -> likely network/observation disorder
```

These correlations are diagnostics, not proof authority, and no fixed ratio between the counter domains is required.

## Dispatcher preflight

Inspect without submitting work:

```bash
python3 scripts/cluster/dispatch.py webgpu nixos-laptop --status
```

Require an idle observation before a single job:

```bash
python3 scripts/cluster/dispatch.py webgpu nixos-laptop --require-idle --job-file job.json
```

For a batch, `--require-idle` rechecks before every item. The dispatcher fails closed if `/status` is unavailable, malformed, internally inconsistent, reports `ok=false`, or reports any active/queued compute.

The dispatcher also honors the optional per-node `webgpu_port` field from `scripts/cluster/nodes.json`; otherwise it uses port 8790.

## Authority and race boundary

The status endpoint and heartbeat provide **observability and placement evidence**, not mutual exclusion between independent clients. A client can observe `idle=true` and another client can submit work before the first client posts its own request. Therefore:

- `/status` is sufficient for the requested "do not knowingly steal a busy node" preflight policy.
- Timestamped heartbeats are sufficient for advisory presence/load ranking and network-funk/skew detection.
- Controlled-spawn fields can make a pool eligible or ineligible for a workload's hard control/surface requirements, but they do not grant execution ownership.
- Neither `/status` nor a heartbeat is sufficient for strict exclusive scheduling.
- Strict exclusion needs an atomic reservation/lease endpoint, or all compute submissions must pass through one scheduler that owns the device lease.
- Decoded output remains candidate/evidence until the applicable MathPunch verifier promotes it.

The daemon serializes its own `/compute` requests through one process-wide GPU mutex, so simultaneous callers are visible as one active request plus queued requests rather than silently overlapping inside the daemon.

## Build/deploy provenance

`scripts/cluster/harbor_push.sh` builds the tracked daemon source before creating the Harbor image:

```text
src/bin/blitter-daemon.rs
    -> cargo build --locked --release --bin blitter-daemon
    -> experiments/webgpu_blitter/blitter-daemon (temporary image input)
    -> harbor.researchstack.info/mathpunch/blitter-daemon:<tag>
```

The temporary binary is removed on script exit. Deployment remains a separate explicit action. Editing this branch does not rebuild, restart, or replace the live daemon or start the heartbeat sidecar.

For the controlled-spawn changes documented here, the branch source has been
updated but a Rust build and live deployment have not yet been verified. Do not
interpret this document as evidence that an already-running daemon exposes the
new spawn fields.
