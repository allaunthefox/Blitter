# MathPunch compute slot fabric and cluster tools

`scripts/cluster/` contains the operational tooling for discovering, describing, ranking, and using heterogeneous compute without treating configured or historical machines as permanently available.

The subsystem rule is:

> **Inventory is memory. Heartbeat is presence. Lease is authority.**

The scheduling unit is a **slot**, not a host. A logical MathPunch goal may own many portable work items placed across different execution surfaces while its recoverable semantic state remains independent of any one machine.

## Status

This README describes the branch `feat/webgpu-blitter-occupancy-status`. It deliberately separates implemented branch behavior from deployment-dependent behavior and design requirements.

| Surface | Branch status | What it proves/permits |
| --- | --- | --- |
| Static `nodes.json` inventory | Implemented | Configuration hints only; never live presence |
| SSH inventory probes | Implemented | Read-only host observation when SSH is configured/reachable |
| Direct bounded container dispatch | Implemented for `docker`, `nix-podman`, and `podman-nvidia-cdi` | Executes explicitly requested work on those configured runtime types |
| Containerized LSP dispatch | Implemented as `dispatch.py lsp` | Validates a digest-pinned, networkless worker; interactive mode additionally requires fresh matching presence and a target-side lease |
| Target-side LSP lease service (`lsp_lease_server.py`) | Implemented; deployment topology is operator concern | Atomic acquire/renew/release with fenced session begin/commit, restart invalidation via `instance_id`, required `--auth-token` (HMAC bearer), worker cgroup termination on revoke/expire/restart; consumed by `lsp_lease.py` and `dispatch.py lsp --interactive`. See `docs/specs/BLITTER_LSP_LEASE_SERVICE_V1.md`. |
| LSP image trust (`image_trust.py`) | Implemented; production digests/keys are deployment-owned | Ed25519 catalog signatures, provenance allowlists, SBOM bindings; `insecureAcceptAnything` removed, Podman paths require `BLITTER_PODMAN_SIGNATURE_POLICY` |
| Hardened workspace execution (`workspace_execution.py`) | Implemented | Canonicalized paths, symlink-escape rejection, per-job source/build/cache/output mounts, non-root launch, quotas, no devices, `--pull=never`, guaranteed per-job cleanup |
| Language worker images (`workers/`) | Bootstrap Containerfiles for lean/coq/python/r/julia/rust; publication is deployment-dependent | Pinned bases, non-root user, HEALTHCHECK, canonical CMD, presence announcer with generation identity; `--require-published` blocks bootstrap publication |
| MathPunch binding (`mathpunch_binding.py`) | Implemented | Diagnostics/artifacts bound to project/commit/toolchain/verifier/request digests; direct kernel replay is the sole authority |
| Inventory entries with `runtime: auto` or `runtime: bare` | **Not supported by `dispatch.py run`** | Inventory/other specialized paths only unless a runtime handler is added |
| `GET /health` client | Implemented; endpoint deployment-dependent | Daemon liveness only |
| `GET /status` validation and `--require-idle` | Implemented in client; endpoint deployment-dependent | Observational occupancy only; not ownership |
| Heartbeat wire v3 | Implemented in `blitter_heartbeat.py` | Advisory presence/load/hardware/timing state |
| Slot filtering/ranking/migration decision | Implemented in `slot_scheduler.py` | Advisory placement decision only |
| Expected-duration / overrun telemetry | Implemented in `lease_protocol.py` | Capacity/anomaly telemetry only; **not fencing** |
| Target-side atomic lease/fencing (WebGPU blitter) | Implemented in `experiments/webgpu_blitter/lease_gate.py`; deployment topology is operator concern | Must be deployed in front of a loopback-only daemon before lease-required routing is authoritative |
| Mathematical result promotion | Outside scheduler authority | Still governed by the applicable MathPunch verifier/authority chain |

Do not infer that a currently deployed daemon exposes every branch endpoint. Verify the target version before relying on `/status` or any future lease interface.

## Read first

- [`../../docs/specs/COMPUTE_SLOT_FABRIC_OVERVIEW_V0.md`](../../docs/specs/COMPUTE_SLOT_FABRIC_OVERVIEW_V0.md) — architecture and authority model.
- [`../../docs/specs/COMPUTE_SLOT_FABRIC_OPERATIONS_V0.md`](../../docs/specs/COMPUTE_SLOT_FABRIC_OPERATIONS_V0.md) — operator runbook and rollout sequence.
- [`../../docs/specs/SLOT_AVAILABILITY_FABRIC_V0.md`](../../docs/specs/SLOT_AVAILABILITY_FABRIC_V0.md) — slot availability/migration contract.
- [`../../docs/specs/NODE_PRESENCE_IDENTITY_V0.md`](../../docs/specs/NODE_PRESENCE_IDENTITY_V0.md) — hardware identity, uptime, ephemeral presence, and historical-state rules.
- [`../../docs/specs/BLITTER_LEASE_FENCING_V1.md`](../../docs/specs/BLITTER_LEASE_FENCING_V1.md) — webgpu daemon atomic lease/fencing.
- [`../../docs/specs/BLITTER_LSP_LEASE_SERVICE_V1.md`](../../docs/specs/BLITTER_LSP_LEASE_SERVICE_V1.md) — LSP session atomic lease/fencing.
- [`../../docs/specs/BLITTER_LSP_TRANSPORT_V1.md`](../../docs/specs/BLITTER_LSP_TRANSPORT_V1.md) — LSP transport layer with session lifecycle hooks.
- [`../../docs/specs/BLITTER_LSP_WORKER_CONTRACT_V1.md`](../../docs/specs/BLITTER_LSP_WORKER_CONTRACT_V1.md) — worker descriptor and runtime envelope.
- [`../../docs/specs/BLITTER_LSP_IMAGE_CATALOG_V1.md`](../../docs/specs/BLITTER_LSP_IMAGE_CATALOG_V1.md) — image catalog and trust verification.
- [`../../docs/specs/BLITTER_LSP_ADMISSION_V1.md`](../../docs/specs/BLITTER_LSP_ADMISSION_V1.md) — LSP admission and presence.
- [`../../CONTRIBUTING.md`](../../CONTRIBUTING.md) — repository verification and documentation quality requirements.

## Prerequisites

The cluster Python tools use the standard library and require Python 3.10+ syntax support. Individual operations also require the resources they contact:

- `dispatch.py inventory` / `run`: an SSH client, configured SSH identity, and network reachability to the selected node;
- container `run`: one of the runtime types explicitly supported above;
- `dispatch.py webgpu`: network reachability to the configured `webgpu_port`;
- heartbeat announcing: a local `/status` endpoint compatible with the branch status contract and UDP reachability to the receiver;
- deployment helpers: explicit operator approval plus the appropriate registry/runtime credentials outside the repository.

Keep credentials out of `nodes.json`, examples, logs, and commits. `ssh_user` may reference an environment variable for a user name; secret material still belongs outside tracked configuration.

## Quick start

Start with local, non-deployment verification:

```bash
python3 scripts/cluster/test_blitter_heartbeat.py
python3 scripts/cluster/test_slot_scheduler.py
python3 scripts/cluster/test_lease_protocol.py
```

Then inspect configured nodes if SSH access is intentionally available:

```bash
python3 scripts/cluster/dispatch.py inventory
```

For a configured WebGPU endpoint, inspect liveness before occupancy:

```bash
python3 scripts/cluster/dispatch.py webgpu <node> --health
python3 scripts/cluster/dispatch.py webgpu <node> --status
```

`--health` and `--status` contact real network services. The following form additionally submits a real `/compute` request:

```bash
python3 scripts/cluster/dispatch.py webgpu <node> \
  --require-idle \
  --a 1,0,2 0,1,3 \
  --b 1,0,5 0,0,7
```

`--require-idle` is a fail-closed observational gate only. Another client can race between the status read and `POST /compute`; this path does **not** implement atomic slot ownership.

## Components

| File | Purpose |
| --- | --- |
| `dispatch.py` | Static inventory, bounded SSH/container dispatch, LSP dispatch, WebGPU health/status/current compute client. Requires `--catalog` + `--catalog-policy` and enforces `DOCKER_CONTENT_TRUST=1` for docker runtime |
| `lsp_lease_server.py` | Target-side atomic lease/fencing service for LSP sessions: `POST /lsp/lease/acquire`, `renew`, `release`, fenced `/lsp/session/begin` and `/lsp/session/commit`, plus `GET /lsp/lease/status` and `GET /health`. Requires `--auth-token`; supports `--worker-cgroup` for revoke/expire/restart termination |
| `lsp_lease.py` | HTTP client for the lease service: `LSPLeaseClient.acquire`, `renew`, `release`, `session_begin`, `session_commit`. Sends `Authorization: Bearer` header |
| `lsp_transport.py` | Byte-transparent SSH transport with `LeaseHooks` for `acquire`/`renew`/`release`/`begin`/`commit` session lifecycle. `begin` runs after SSH starts; `commit` runs before release and fences stale workers |
| `lsp_workers.py` | Digest-pinned, networkless language-server descriptor validation and container command rendering. Enforces `DOCKER_CONTENT_TRUST=1` for docker and `BLITTER_PODMAN_SIGNATURE_POLICY` for podman. Wires `pin_workspace()` for symlink TOCTOU closure |
| `lsp_admission.py` | LSP presence/admission reference: `LSPWorkSpec`, `presence_from_mapping`, `admit_lsp_work` |
| `image_catalog.py` | Pure shape validator for the LSP image catalog (no cryptography) |
| `image_trust.py` | Pure-Python Ed25519 catalog signature verification, provenance allowlists, SBOM bindings, deployment-owned policy loader |
| `workspace_execution.py` | Hardened per-job mount rendering: `pin_workspace()` inode-pinned symlink resolution, four per-job mounts (`source` ro / `build` rw / `cache` shared / `output` rw), non-root launch, `--pull=never`, guaranteed cleanup via `job_workspace` context manager |
| `workspace_execution.py` symlink safety | `pin_workspace` resolves symlinks, captures `(device, inode)`, and re-verifies before mount flag render. A symlink swap between validation and mount fails closed |
| `sign_lsp_catalog.py` | Offline sign/verify CLI; `--require-published` blocks bootstrap digests |
| `mathpunch_binding.py` | Diagnostics/artifacts bound to project/commit/toolchain/verifier/request digests; direct kernel replay is the sole authority, worker claims are telemetry-only |
| `blitter_heartbeat.py` | Unicast UDP heartbeat announcer/listener with load, hardware, uptime, expiry, skew, and sequence telemetry |
| `slot_scheduler.py` | Converts fresh heartbeats to slot pools, filters/ranks compatible pools, and proposes migration decisions |
| `lease_protocol.py` | Expected-duration and profound-overrun telemetry; despite the filename, it does not grant/fence leases |
| `nodes.json` | Static inventory hints; not proof that a node is present or schedulable |
| `harbor_push.sh` | Build/publish helper; verify source/artifact and credentials before use |
| `harbor_deploy.sh` | Deployment helper; use only under an explicit rollout decision |
| `test_blitter_heartbeat.py` | Heartbeat/identity/expiry/timing regression tests |
| `test_slot_scheduler.py` | Slot eligibility/rental/migration regression tests |
| `test_lease_protocol.py` | Expected-duration/overrun telemetry tests |
| `test_lsp_lease_server.py` | 15 race/fencing/expiry tests incl. live HTTP + `LSPLeaseClient` roundtrip and concurrent-acquire races |
| `test_lsp_transport.py` | Transport with lease hook lifecycle tests |
| `test_lsp_workers.py` | Descriptor validation, runtime rendering, DCT/Podman signature enforcement |
| `test_lsp_dispatch.py` | LSP dispatch path with catalog+policy enforcement |
| `test_workspace_execution.py` | Symlink TOCTOU closure, per-job mount layout, hardened render |
| `test_image_trust.py` | Ed25519, provenance, SBOM, policy loader |
| `test_worker_images.py` | Statically gates the six Containerfiles (digest pinning, non-root, HEALTHCHECK, canonical CMD, presence announcer) |

## Mental model

Keep the state transitions distinct:

```text
configured
    -> fresh heartbeat
        -> compatible available slot
            -> target-side lease grant   [implemented: `lsp_lease_server.py` for LSP sessions, `experiments/webgpu_blitter/lease_gate.py` for WebGPU compute]
                -> execute
                    -> receipt/checkpoint
```

A machine can be configured but offline, present but busy, or available but lose a future lease race. Historical participation never implies current availability.

```text
recoverable goal
  |- work A -> CPU/control slot
  |- work B -> WebGPU/Vulkan slot
  |- work C -> NVIDIA/accelerator slot
  |- work D -> exact verification slot
  `- work E -> queued until a compatible slot appears
```

Hardware placement is temporary. `goal_id`, `work_id`, state/checkpoint digests, and receipts are the durable semantic layer.

## Static inventory and direct dispatch

Inspect configured nodes:

```bash
python3 scripts/cluster/dispatch.py inventory
```

The command probes each configured SSH destination. A successful probe is a current observation for that invocation; `nodes.json` itself remains configuration, not presence authority.

CPU container example on a supported runtime:

```bash
python3 scripts/cluster/dispatch.py run \
  --image alpine:3.22 --cpus 1 --memory-gib 1 \
  <node> -- uname -a
```

GPU example on a configured `podman-nvidia-cdi` node:

```bash
python3 scripts/cluster/dispatch.py run \
  --image nvidia/cuda:12.8.0-base-ubuntu24.04 \
  --cpus 4 --memory-gib 8 --gpu \
  <gpu-node> -- nvidia-smi
```

Configured resource caps fail closed. GPU requests are rejected for CPU-only roles. Ephemeral static entries require explicit `--allow-ephemeral` opt-in.

`runtime: auto` and `runtime: bare` entries do **not** make `dispatch.py run` auto-detect or execute a container. Add a supported runtime handler or use the appropriate specialized service instead.

## WebGPU daemon interface

Liveness:

```bash
python3 scripts/cluster/dispatch.py webgpu <node> --health
```

Occupancy on a compatible daemon:

```bash
python3 scripts/cluster/dispatch.py webgpu <node> --status
```

The status client requires coherent fields including:

```text
ok
busy
idle
active_compute
queued_compute
max_concurrent_compute
```

Inconsistent counters/booleans fail closed.

The current `/compute` client uses sparse bivariate/Laurent-style integer terms:

```text
[q_exp, t_exp, coefficient]
```

This is a specific codec surface. It is **not** an arbitrary-WGSL submission API.

## Heartbeat receiver

Start a receiver:

```bash
python3 scripts/cluster/blitter_heartbeat.py listen \
  --bind 0.0.0.0:8791
```

Heartbeat traffic is advisory unicast UDP soft state. The receiver stamps local arrival time and derives sequence and timing diagnostics. Network reachability should be constrained by the surrounding routed/Tailscale policy; the heartbeat itself is not slot ownership or mathematical authority.

## Advertise a persistent node

```bash
python3 scripts/cluster/blitter_heartbeat.py announce \
  --target <router-or-peer>:8791 \
  --node <node-name> \
  --status-url http://127.0.0.1:8790/status \
  --advertise-url http://<reachable-address>:8790
```

Defaults:

```text
heartbeat interval: 1000 ms
status timeout:      750 ms
presence TTL:        max(3500 ms, 3 * heartbeat interval)
```

If `/status` cannot be read and validated, the announcement fails closed as unusable capacity.

## Advertise rented or ephemeral capacity

```bash
python3 scripts/cluster/blitter_heartbeat.py announce \
  --target <router-or-peer>:8791 \
  --node rental-h100 \
  --ephemeral \
  --status-url http://127.0.0.1:8790/status \
  --advertise-url http://<rental-address>:8790 \
  --availability-ttl-ms 5000 \
  --retire-at-unix-ms <provider-lease-end-ms>
```

Expected lifecycle:

```text
fresh heartbeat -> present
heartbeats stop  -> present only until availability expiry
TTL expires      -> historical provenance only
retirement time  -> historical provenance only
```

The historical node identity may remain in receipts forever. Placement still requires fresh, unexpired presence.

## Node identity, hardware, and uptime

Heartbeat wire v3 carries a boot-scoped readable identity such as:

```text
<name>|<lifecycle>|<arch>|cpu=<logical>|mem=<MiB>MiB|boot=<boot-prefix>
```

The message separately carries the full `boot_id`, `uptime_ms`, boot-start timestamp, CPU model, logical CPU count, memory, and accelerator adapter/backend when known.

Do not put live uptime in `node_id`: identity must remain stable across heartbeats within one boot generation.

## Clock and network diagnostics

The receiver derives:

```text
apparent_skew_ms
sequence_gap
duplicate
reordered
sender_clock_invalid
receiver_clock_invalid
time_anomaly
```

A timestamp before `2000-01-01T00:00:00Z` is treated as anomalous timing evidence, catching epoch-zero/1969-style clock failures. `apparent_skew_ms` combines clock offset and network delay; it is not pure one-way latency.

Timing anomalies and stale/expired presence exclude a pool from routing. They do not change mathematical authority.

## Placement and migration

`slot_scheduler.py` only considers pools whose heartbeat is fresh, unexpired, not retired, timing-sane, status-valid, capability-compatible, and advertising positive available capacity.

V0 then prefers higher suitability, lower queue pressure, lower status RTT, lower absolute apparent skew, and deterministic IDs.

This ranking is advisory.

For checkpointable work, the architecture uses destination-first handoff:

```text
select better compatible destination
  -> acquire destination lease          [future/required target-side authority]
  -> checkpoint at safe semantic boundary
  -> verify checkpoint/state digest
  -> resume on destination
  -> release source lease
```

Migration hysteresis avoids chasing negligible score changes. Non-checkpointable work is not killed simply to obtain a better score; it waits for a safe boundary or follows the enclosing goal's certified replay policy.

## Expected duration is not a lease

`lease_protocol.py` currently models an expected occupancy budget:

```json
{
  "goal_id": "goal-...",
  "work_id": "work-...",
  "expected_lease_ms": 30000,
  "profound_overrun_factor": 4
}
```

At the default four-times threshold it emits one structured `lease_budget_overrun` note. The action is log-only. This estimate does **not** grant ownership, set a fencing epoch, or revoke work.

A real authoritative lease TTL/expiry belongs to the target-side atomic lease/fencing contract described in the fabric specs and must be implemented and verified separately.

## Security and trust boundaries

- Do not store SSH keys, registry credentials, API tokens, signing keys, or secrets in this directory.
- Static inventory is not trusted as live state.
- Heartbeats and `/status` are advisory routing evidence, not ownership.
- `--require-idle` does not close the check/submit race.
- A future lease token/fencing epoch must be validated by the execution target, not merely recorded by the client.
- Compute output remains within its declared exact/advisory scope until consumed by the applicable MathPunch verification/authority path.
- Deployment helpers can mutate live infrastructure; treat them as explicit operator actions, not test commands.

### LSP dispatch trust gates

The `dispatch.py lsp` path enforces three independent trust gates; failure on
any one fails closed:

1. **Signed catalog**: `--catalog` and `--catalog-policy` are required. The
   catalog must be a `mathpunch.blitter-lsp-image-catalog.v1` object with
   Ed25519 signature against `approved_keys` in the policy.
2. **Image trust verification**: `approved_images` (non-empty allowlist) and
   `approved_keys` are required. A bare digest-pinned image without a
   catalog is **never** accepted.
3. **Runtime signature enforcement**:
   - Docker: `DOCKER_CONTENT_TRUST=1` is required in the dispatcher's
     environment. Without it, the local image is not cryptographically
     authorized and rendering fails closed.
   - Nix-Podman / Podman-NVIDIA-CDI: the deployment-owned
     `BLITTER_PODMAN_SIGNATURE_POLICY` is required; absence fails closed.
     There is no `insecureAcceptAnything` fallback (CI grep-gates its
     absence).

### LSP session fence (interactive mode)

When `--interactive` is set, the transport wires the lease service's
session lifecycle:

- `LSPLeaseClient.session_begin()` is called after SSH starts. A second
  dispatch cannot acquire a lease for the same worker until this session
  commits or aborts.
- `LSPLeaseClient.session_commit()` is called before diagnostics are
  returned. A stale or expired lease causes the transport to raise
  `TransportError` and the caller must drop all buffered output.
- The lease service is bound to loopback by default and requires a bearer
  token on all authority-bearing endpoints.

### Workspace hardening (symlink TOCTOU)

`workspace_execution.pin_workspace()` captures the resolved `(device, inode)`
of the host workspace. The mounted path is the resolved path, never the raw
symlink. If the directory is unlinked, renamed, or replaced between
validation and mount, the operation fails closed with a `WorkspaceError`.

### Worker termination

When the lease server detects a revoke, TTL expiry, or graceful shutdown,
it optionally signals the worker cgroup via `--worker-cgroup` (or
`BLITTER_LSP_LEASE_CGROUP`). This uses Linux cgroup-v2 `cgroup.kill` with
a per-process `SIGTERM` fallback. Without this, a worker that survives a
lease revoke could continue producing diagnostics against a stale lease.

## Troubleshooting

| Symptom | Interpretation / action |
| --- | --- |
| `unsupported runtime` from `dispatch.py run` | The inventory runtime is not one of the three direct handlers; use a supported/specialized path or add a handler with tests |
| SSH user “not configured” | Resolve the referenced environment variable; do not put credentials into `nodes.json` |
| `/health` works but `/status` is 404/invalid | The deployed daemon does not match the occupancy-status contract; do not infer idleness |
| `/status` says busy or has queued work | Do not submit through `--require-idle`; reassess another slot |
| heartbeat status read fails | Announcement fails closed as unusable capacity |
| stale heartbeat / TTL expired | Historical only; exclude from placement |
| `time_anomaly=true` or epoch/1969 timestamp | Fix sender/receiver clock before routing |
| sequence gaps / reordered beats | Investigate network/path health; do not promote heartbeat to ownership |
| profound expected-duration overrun | Scheduling telemetry only; inspect estimate/workload, do not revoke solely from the estimate |
| better slot appears but work is not checkpointable | Wait for a safe semantic boundary or replay according to goal policy |

## Testing and reproducibility

Run the three narrow protocol suites after cluster/fabric changes:

```bash
python3 scripts/cluster/test_blitter_heartbeat.py
python3 scripts/cluster/test_slot_scheduler.py
python3 scripts/cluster/test_lease_protocol.py
```

Report the exact Git revision used. Unit tests do not prove that Forgejo Actions ran for that revision; CI must be checked independently.

Behavioral examples in this README must be exercised against the relevant implementation before being reported as successful. If a target endpoint or deployment path cannot be exercised, label it `UNVERIFIED`/deployment-dependent rather than converting design intent into a success claim.

## Safe rollout

For deployment-affecting changes:

1. confirm relevant compute resources are not carrying unrelated work;
2. run narrow protocol tests;
3. build the daemon/artifact from the intended tracked source and locked dependency path;
4. publish the intended artifact;
5. deploy one canary;
6. verify `/health`;
7. verify `/status`, heartbeat identity, uptime, TTL, and timing fields if that interface is part of the canary;
8. deliberately test stale TTL and epoch/1969 anomaly handling;
9. verify stale heartbeat alone cannot authorize work;
10. implement and race-test target-side atomic lease/fencing before enabling lease-required routing;
11. expand only after the canary evidence matches the declared interface.

Rollback by returning to the last verified artifact/configuration and disabling routing to any endpoint whose current protocol cannot be confirmed. Historical logs or inventory entries must not be used to bypass live presence/authority gates.

## Rust compilation cache

When `sccache` is installed, repository Rust build helpers may use `RUSTC_WRAPPER` with reusable content-addressed objects under `.cache/sccache/`. `MATHPUNCH_SCCACHE_BIN` and `MATHPUNCH_SCCACHE_DIR` may override the executable/cache directory. The cache is local optimization only and does not confer scheduler or build authority.

## Contributing

Follow [`../../CONTRIBUTING.md`](../../CONTRIBUTING.md) and [`../../AGENTS.md`](../../AGENTS.md). In particular:

- do not document an unexecuted behavior as working;
- preserve status/authority distinctions;
- version changed interfaces;
- add or update narrow regression tests with behavior changes;
- keep stable documentation paths intact when consumers may bind them;
- keep secrets out of commits and logs.

## Operator shorthand

```text
inventory = where compute might exist
heartbeat = where compute appears usable now
lease     = where this work is allowed to run
```

For temporary hardware:

```text
history may persist forever; availability must expire quickly
```
