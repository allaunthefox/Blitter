# Compute Slot Fabric Operations V0

Status: operator guide for `feat/webgpu-blitter-occupancy-status`.

This runbook covers the branch tooling for configured inventory, bounded dispatch, WebGPU occupancy observation, heartbeat presence, slot ranking, expected-duration telemetry, and safe migration policy. It distinguishes **implemented client/policy behavior**, **deployment-dependent daemon behavior**, and **required-but-not-yet-implemented target-side lease/fencing**.

The operational invariant is:

> **Do not submit substantive work merely because a machine exists in inventory or answers a health check. Require fresh presence/load evidence, and require target-side execution authority before treating lease-required routing as authoritative.**

## 1. Current support matrix

| Capability | Status | Operator interpretation |
| --- | --- | --- |
| Static `nodes.json` inventory | Implemented | Configuration hints; not live availability |
| SSH inventory probe | Implemented | Current read-only observation for that invocation |
| `dispatch.py run` with `docker` | Implemented | Bounded direct container execution |
| `dispatch.py run` with `nix-podman` | Implemented | Bounded direct container execution through Nix-provided Podman |
| `dispatch.py run` with `podman-nvidia-cdi` | Implemented | Bounded NVIDIA CDI execution |
| `dispatch.py run` with `auto` / `bare` | **Unsupported** | Use a specialized path or add a tested runtime handler |
| `GET /health` | Client implemented; daemon deployment-dependent | Liveness only |
| `GET /status` | Client validation implemented; daemon deployment-dependent | Observational occupancy only |
| `--require-idle` | Implemented | Fail-closed status precheck; not atomic ownership |
| Heartbeat v3 | Implemented | Advisory presence/load/hardware/timing |
| Slot ranking/migration decision | Implemented | Advisory placement policy |
| `lease_protocol.py` | Implemented | Expected-duration/overrun telemetry only |
| Target-side atomic lease/fencing | **Required architecture, not implemented by current Python helpers** | Must be implemented/deployed/race-tested before lease-required routing is authoritative |

## 2. Components

| File | Role |
| --- | --- |
| `scripts/cluster/dispatch.py` | Static inventory, bounded SSH/container dispatch, WebGPU health/status/current compute client |
| `scripts/cluster/blitter_heartbeat.py` | UDP heartbeat announcer/listener with load, hardware, uptime, expiry, skew, and sequence diagnostics |
| `scripts/cluster/slot_scheduler.py` | Converts fresh heartbeats into slot pools, filters/ranks candidates, proposes migration decisions |
| `scripts/cluster/lease_protocol.py` | Expected-duration / profound-overrun telemetry; **not lease grant/fencing** |
| `scripts/cluster/nodes.json` | Static inventory hints only; never proof of live presence |
| `scripts/cluster/harbor_push.sh` | Build/publish helper; requires explicit operator intent and external credentials |
| `scripts/cluster/harbor_deploy.sh` | Deployment helper; use only after an explicit rollout decision |

Related contracts:

- `COMPUTE_SLOT_FABRIC_OVERVIEW_V0.md`
- `SLOT_AVAILABILITY_FABRIC_V0.md`
- `NODE_PRESENCE_IDENTITY_V0.md`
- `../../CONTRIBUTING.md`

## 3. Prerequisites and side effects

The branch cluster Python tools require Python 3.10+ syntax support and otherwise use the standard library. Networked commands require the relevant configured reachability and permissions.

Classify commands before running them:

| Command class | Side effect |
| --- | --- |
| `test_*.py` | Local process/test activity only |
| `dispatch.py inventory` | SSH reads from configured nodes |
| `dispatch.py webgpu <node> --health/--status` | HTTP reads from a configured daemon |
| heartbeat `listen` | Opens a UDP listener |
| heartbeat `announce` | Reads local status and sends UDP heartbeat datagrams |
| `dispatch.py run` | Starts a real remote container/process through SSH |
| `dispatch.py webgpu` without `--health/--status` | Submits real `/compute` work |
| `harbor_push.sh` / `harbor_deploy.sh` | Build/publish/deployment operations; may mutate live infrastructure |

Keep SSH keys, registry credentials, API tokens, signing keys, and other secrets outside tracked files and examples.

## 4. Safe quick start

Start with narrow local protocol checks:

```bash
python3 scripts/cluster/test_blitter_heartbeat.py
python3 scripts/cluster/test_slot_scheduler.py
python3 scripts/cluster/test_lease_protocol.py
```

Then inspect configured nodes only when SSH access is intentionally available:

```bash
python3 scripts/cluster/dispatch.py inventory
```

For a configured WebGPU node, inspect liveness and then occupancy:

```bash
python3 scripts/cluster/dispatch.py webgpu <node> --health
python3 scripts/cluster/dispatch.py webgpu <node> --status
```

Do not proceed from `/health` alone to a claim of idleness.

## 5. Operational states

Treat these as distinct:

```text
configured
    -> heartbeat-present
        -> compatible/available
            -> lease-granted        [required full architecture; not implemented by current helpers]
                -> executing
```

A node can be configured but offline. It can be heartbeat-present but busy. It can appear available but lose a future atomic lease race.

The current `--require-idle` path is an observational guard. Another client can race between `GET /status` and `POST /compute`, so it is not a substitute for target-side atomic ownership/fencing.

## 6. Static inventory and direct legacy dispatch

Inspect configured nodes:

```bash
python3 scripts/cluster/dispatch.py inventory
```

This command probes configured SSH destinations. The resulting observation applies to that invocation; the inventory entry itself remains non-authoritative.

Run an explicit bounded container on a supported runtime:

```bash
python3 scripts/cluster/dispatch.py run \
  --image alpine:3.22 \
  --cpus 1 \
  --memory-gib 1 \
  <node> -- uname -a
```

GPU form on a configured `podman-nvidia-cdi` node:

```bash
python3 scripts/cluster/dispatch.py run \
  --image nvidia/cuda:12.8.0-base-ubuntu24.04 \
  --cpus 4 \
  --memory-gib 8 \
  --gpu \
  <gpu-node> -- nvidia-smi
```

Resource requests above configured caps fail closed. GPU requests fail on CPU-only roles. Static ephemeral entries require explicit `--allow-ephemeral` opt-in.

**Important runtime boundary:** `dispatch.py run` only has handlers for `docker`, `nix-podman`, and `podman-nvidia-cdi`. An inventory entry with `runtime: auto` or `runtime: bare` is not automatically executable through this command.

## 7. Inspect the WebGPU daemon

Liveness:

```bash
python3 scripts/cluster/dispatch.py webgpu <node> --health
```

`/health` proves liveness only.

Occupancy on a daemon implementing the branch status contract:

```bash
python3 scripts/cluster/dispatch.py webgpu <node> --status
```

The client validates coherent fields including:

```text
ok
busy
idle
active_compute
queued_compute
max_concurrent_compute
```

To fail closed on observed occupancy before a current compute request:

```bash
python3 scripts/cluster/dispatch.py webgpu <node> \
  --require-idle \
  --a 1,0,2 0,1,3 \
  --b 1,0,5 0,0,7
```

This submits real work if the precheck reports idle. The current `/compute` codec uses integer triples:

```text
[q_exp, t_exp, coefficient]
```

It is a specific sparse bivariate/Laurent-style multiplication surface, not an arbitrary-WGSL submission endpoint.

If `/health` succeeds but `/status` is missing or invalid, treat the daemon as incompatible with occupancy-aware routing rather than inferring idleness.

## 8. Start a heartbeat receiver

```bash
python3 scripts/cluster/blitter_heartbeat.py listen \
  --bind 0.0.0.0:8791
```

The receiver prints enriched JSON observations, stamps local arrival time, and derives clock/network/sequence diagnostics.

Heartbeat traffic is advisory unicast UDP soft state. Use routed/Tailscale network policy to limit who can reach the listener. The heartbeat itself is neither authentication nor slot ownership.

## 9. Advertise a persistent node

On a node whose local daemon implements the branch `/status` contract:

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

If local status cannot be read and validated, the announcement fails closed as unusable capacity.

## 10. Advertise rented or ephemeral capacity

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
TTL expires      -> historical only, not schedulable
retirement time  -> historical only, not schedulable
```

Historical node identity and receipts may persist. The scheduler must not infer continued availability from history.

## 11. Heartbeat identity and hardware

Wire v3 carries:

```text
node / boot-scoped node_id
boot_id
lifecycle: persistent | ephemeral
uptime_ms
boot_started_unix_ms
CPU model / logical CPUs / memory
accelerator adapter / backend when known
heartbeat sequence
send timestamp
presence TTL / expiry
optional retirement timestamp
active / queued / total compute slots
advertised compute URL
```

Readable ID form:

```text
<name>|<lifecycle>|<arch>|cpu=<logical>|mem=<MiB>MiB|boot=<boot-prefix>
```

`uptime_ms` must not be embedded in `node_id`; otherwise identity would change every heartbeat. `boot_id` identifies the uptime generation.

## 12. Clock and network faults

The receiver computes:

```text
apparent_skew_ms
sequence_gap
duplicate
reordered
sender_clock_invalid
receiver_clock_invalid
time_anomaly
```

A timestamp before `2000-01-01T00:00:00Z` is anomalous, catching Unix epoch zero / 1969-style clock failures.

Typical interpretation:

```text
stable small apparent skew -> ordinary clock offset/network delay is plausible
large sudden skew change   -> investigate clock or network path
sequence gap               -> missed beats / packet loss / outage
reordered=true             -> reordering or stale delivery
time_anomaly=true          -> exclude from routing until clock is fixed
```

`apparent_skew_ms` is not pure one-way latency; it combines clock offset and network delay.

## 13. Placement policy

`slot_scheduler.py` only ranks pools whose heartbeat is live and whose hard requirements match the work item.

Eligibility includes:

```text
status valid
heartbeat fresh
availability TTL not expired
retirement time not reached
clock/timing sane
required capabilities present
slots_available > 0
```

Among eligible candidates, V0 ranks by application fit, queue pressure, status RTT, absolute apparent skew, then deterministic IDs.

This is a placement recommendation, not execution authority.

## 14. Expected duration / overrun telemetry

A work item may declare:

```json
{
  "goal_id": "goal-...",
  "work_id": "work-...",
  "requires": ["webgpu-blitter", "exact-i32"],
  "checkpointable": true,
  "expected_lease_ms": 30000,
  "profound_overrun_factor": 4
}
```

Despite the existing field/file naming, `expected_lease_ms` is an **expected occupancy estimate**, not an authoritative lease TTL.

At the default threshold:

```text
elapsed >= 4 * expected_lease_ms
```

`lease_protocol.py` emits one structured `lease_budget_overrun` note with log-only semantics. It does not grant, renew, revoke, expire, or fence execution authority.

## 15. Migration when a better slot appears

The placement policy may continuously reevaluate candidates as heartbeat state changes.

Queued work can select a newly available better-fit target, but in the full design execution must wait for a successful destination lease.

Checkpointable running work uses destination-first semantics:

```text
1. rank destination
2. acquire destination lease             [required target-side authority]
3. checkpoint at safe semantic boundary
4. verify checkpoint/state digest
5. resume on destination
6. release source lease
```

Ordinary migrations use hysteresis (`min_migration_gain`) to avoid thrashing. If the current pool is stale, expired, incompatible, or otherwise nonviable, recovery takes priority over ordinary fit hysteresis.

`slot_scheduler.py` implements the decision policy, not lease acquisition or arbitrary process-memory migration.

Non-checkpointable work is not killed solely to chase a better score. Reevaluate at its next safe semantic boundary or replay from the last certified checkpoint according to goal policy.

## 16. Required atomic leases/fencing

The architecture requires target-side atomic ownership/fencing to close the `status -> submit` race. Heartbeat and `/status` are not sufficient.

Required sequence:

```text
heartbeat -> rank candidate -> target-side atomic lease attempt -> compute -> receipt/release
```

Required migration sequence:

```text
lease destination -> checkpoint/handoff -> resume -> release source
```

Before lease-required routing is enabled, the target should demonstrate:

- atomic one-owner-per-slot grant/reject behavior;
- lease token/fencing epoch checked by the compute path;
- expiry and explicit release;
- stale-token rejection;
- concurrent race tests;
- receipts that bind the actual lease identity.

**Current implementation boundary:** the Python `lease_protocol.py` file does not implement those operations. `/status` plus `--require-idle` remains observational. Treat any lease endpoint as `UNVERIFIED` until the deployed daemon/version and race behavior have been checked.

## 17. One logical goal across many machines

A goal may split into independent or dependent work items:

```text
goal G
  |- W1 -> CPU frontier generation
  |- W2 -> GPU/WGPU batch evaluation
  |- W3 -> independent exact replay
  |- W4 -> provenance/receipt processing
  `- W5 -> waiting for compatible slot
```

Every item carries the same `goal_id`. Results, scars, checkpoints, and receipts merge into the goal's recoverable state. Hardware ownership is temporary; semantic ownership belongs to the goal.

## 18. Troubleshooting

| Symptom | Response |
| --- | --- |
| `unsupported runtime` | `dispatch.py run` has no handler for that inventory runtime; do not assume `auto`/`bare` works |
| SSH user unresolved | Set the referenced user-name environment variable; keep credentials outside the repo |
| `/health` works, `/status` missing | Deployed daemon is not occupancy-status compatible; do not infer idle |
| `/status` inconsistent | Client fails closed; inspect daemon/version before routing |
| busy/queued status | Do not submit via `--require-idle`; choose/reassess another slot |
| heartbeat status read fails | Announcement is unusable capacity by design |
| stale or expired heartbeat | Historical only; remove from placement |
| retirement time reached | Historical only; remove from placement |
| epoch/1969 timestamp | Fix sender/receiver clock before routing |
| sequence gaps/reordering | Investigate network/path health; no ownership inference |
| expected-duration overrun | Inspect estimate/workload; telemetry is log-only |
| better target but no checkpoint boundary | Do not kill just for score; wait/replay according to goal policy |

## 19. Testing and reproducibility

Run the narrow cluster protocol suites:

```bash
python3 scripts/cluster/test_blitter_heartbeat.py
python3 scripts/cluster/test_slot_scheduler.py
python3 scripts/cluster/test_lease_protocol.py
```

Verification reports must include the exact Git revision. Local/unit success does not prove that Forgejo Actions ran on that revision.

If a deployment endpoint or command path cannot be exercised, label it `UNVERIFIED`/deployment-dependent. Do not report design intent as working behavior.

## 20. Safe rollout and rollback

Before deployment-affecting work:

```text
1. confirm relevant compute resources are not carrying unrelated work
2. run narrow protocol tests
3. build the intended daemon/artifact from the tracked source and locked dependency path
4. publish the intended artifact
5. deploy one canary
6. verify /health
7. verify /status and heartbeat identity/uptime/TTL/timing fields when applicable
8. deliberately test stale TTL and epoch/1969 anomaly handling
9. verify stale heartbeat alone cannot authorize work
10. implement and race-test target-side atomic lease/fencing before enabling lease-required routing
11. expand only after canary evidence matches the declared interface
```

Rollback to the last verified artifact/configuration and disable routing to endpoints whose current protocol cannot be confirmed. Historical logs, `nodes.json`, DNS, or a successful TCP connection must not bypass fresh presence/authority gates.

## 21. Security and trust boundaries

- Keep secrets out of tracked configuration, examples, test logs, and receipts unless the receipt contract explicitly permits a non-secret identifier.
- Heartbeat UDP and status reads are advisory telemetry, not authentication or ownership.
- Surround network endpoints with the appropriate routed/Tailscale/firewall policy.
- A future lease/fencing token must be enforced by the execution target.
- Compute results retain exact/advisory scope and still require the applicable MathPunch verifier/authority path before mathematical promotion.

## 22. Operator shorthand

```text
inventory = where compute might exist
heartbeat = where compute appears usable now
lease     = where this work is allowed to run
```

For temporary hardware:

```text
history may persist forever; availability must expire quickly
```
