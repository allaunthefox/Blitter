# Compute Slot Fabric P2P V0

**Status:** architecture and implementation map for `p2p-blitter`.

The compute slot fabric becomes a **gossip mesh**: nodes self-advertise multi-axis
capability and load profiles, clients route work to the nearest compatible peer,
and compute flows through the mesh via passthrough. The existing lease/fencing
gate remains the authoritative ownership layer — it simply lives on every node
that can execute work.

## 1. Architecture overview

```text
  client
    |
    |  query (requires, control, surfaces)
    v
  [ gateway / router ]  ← nearest-neighbor query
    |
    |  query_id + ttl + scope
    v
  [ gossip mesh ]  ← advertise + lease_advertise + heartbeat + query + response
    |
    |  compute_passthrough (path, ttl, job, lease)
    v
  [ nearest peer ]  ← gate validates lease, daemon executes
    |
    |  compute_response (reverse path)
    v
  client
```

The mesh is **not** a broadcast storm. Gossip is periodic unicast/multicast
with bounded scope. Queries are TTL-limited floods. Passthrough follows a
deterministic path recorded in the frame.

## 2. Core objects

### Peer

A `node_id` plus a live **designation** (multi-axis capability + load +
proximity). Designation is derived from a successful `/status` read, local
`/proc` probes, and spawn-envelope normalization.

### Peer table

Each node maintains a local peer table:

```text
node_id → {
  designation,
  received_unix_ms,
  apparent_skew_ms,
  sequence_gap,
  duplicate,
  reordered,
  stale,
  retired,
  lease_state   // mirrored from lease_advertise frames
}
```

The peer table is **evidence**, not authority. It informs routing and is
refreshed every advertisement interval.

### Work spec

```text
goal_id
work_id
requires               ⊆ capabilities
requires_control       ⊆ controlled_subsystems
acceptable_surfaces    ⊆ execution_surfaces
preference_weights     { capability → weight }
checkpointable
qos_tier               "latency_sensitive" | "throughput_sensitive" | "default"
```

`qos_tier` controls how aggressively load and latency affect routing:

- `latency_sensitive` — verification, check, receipt work. Penalizes load
  and RTT heavily so the fastest idle node is chosen.
- `throughput_sensitive` — bulk compile, heavy merge. Tolerates higher
  load and latency; capability fit and raw capacity matter more.
- `default` — balanced behavior.

A single goal can split work across tiers: a multi-gigabyte compile can
send the heavy passes to a loaded but capable node while routing its
verification to a lightly loaded one.

### Route

A `route` is the result of nearest-neighbor search:

```text
query_id
candidates: [ {node_id, score, proximity, load} ]
selected: node_id
path: []        // filled as the request traverses the mesh
```

### Lease

Unchanged from V1. A node acquires a lease from the **destination** gate before
compute. The gate validates identity/deadline twice (before and after upstream
dispatch). Expiry/release/revocation fences at commit. Lease advertisements
mirror this state across the mesh but do not grant ownership.

## 3. Authority layers

| Layer | Purpose | May rank work? | May authorize execution? |
|---|---|---|---|
| Static inventory/config | Names, addresses, expected capabilities | No, by itself | No |
| Historical receipt/log | Provenance | No | No |
| Gossip advertisement | Presence, load, capability | Yes | No |
| Suitability score | Placement preference | Yes | No |
| Lease grant at destination | Ownership/fencing | N/A | **Yes** |
| Daemon execution | Arithmetic | N/A | Governs result |
| MathPunch verifier | Mathematical promotion | N/A | Governs result |

The fabric fails closed whenever these layers disagree.

## 4. Multi-axis designation in detail

Each node advertises:

- **Capability axis**: `capabilities` set (e.g., `webgpu-blitter`, `exact-i32`).
  Hard requirement for routing.
- **Control axis**: `controlled_subsystems`. Work must require a subset.
- **Surface axis**: `execution_surfaces`. Work must intersect.
- **Accessibility axis**: `accessibility_profile`. `none` is terminal.
- **Load axis**: `active_compute`, `queued_compute`, `max_concurrent_compute`.
  Higher pressure → lower score.
- **Proximity axis**: `rtt_ms`, `hop_count`, `tailscale_subnet`. Lower →
  better score.
- **Lifecycle axis**: `persistent` vs `ephemeral`. Ephemeral nodes are usable
  but expire quickly.

No axis alone grants authority. A direct host with `accelerator-api` control is
preferred to a sandbox, but only if it satisfies the hard control and surface
requirements.

## 5. Gossip mesh behavior

### Advertisement interval

Default 1000 ms. Each node multicasts its full `advertise` frame. On receipt,
peers validate and merge into the local peer table. Stale entries expire;
retired entries are removed.

### Lease advertisement

Triggered on every lease state change (acquire, renew, release, expiry).
Scopes propagation to peers that have already exchanged advertisements.

### Query scope

- `local`: direct peers only.
- `neighbor`: two hops.
- `mesh`: flood (with TTL to prevent storms).

Queries carry `requires`, `requires_control`, `acceptable_surfaces`, and
`max_results`. Responders return ranked candidate lists with proximity scores.

### Passthrough

Compute requests carry a `path` and `ttl`. Each hop appends its `node_id` and
decrements `ttl`. The final hop executes. Responses reverse the path.

## 6. Nearest-neighbor routing

1. Hard-filter by `requires ⊆ capabilities`, `requires_control ⊆ controlled_subsystems`,
   `acceptable_surfaces ∩ execution_surfaces ≠ ∅`, `accessibility_profile ≠ none`,
   and routable status.
2. Compute a tier-aware score:
    ```text
    score = suitability_score(work, node)
           - load_penalty(node, qos_tier)
           - rtt_penalty(node, qos_tier)
           - skew_penalty(node)
    ```
     where `qos_tier` modulates the penalty weights:
     - `latency_sensitive` — `load_weight = 3.0`, `rtt_weight = 2.0`;
       verification/check work routes to the fastest idle node.
     - `throughput_sensitive` — `load_weight = 0.25`, `rtt_weight = 0.25`;
       bulk compile can tolerate a loaded but capable node.
     - `default` — `load_weight = 1.0`, `rtt_weight = 1.0`.
     ```

     A single goal can split work across tiers — heavy passes route
     throughput-sensitive nodes while checks route latency-sensitive ones.

 3. Tie-break by `accessibility_rank` then `node_id`.
 4. Return top `max_results`.

 "Nearest" is multi-axis: capability fit is hard; load, latency, and
 QoS tier jointly determine the ranking.

## 7. Deployment

- Each node runs: `p2p-gateway` (HTTP + gossip listener) + `blitter-daemon`
  (loopback) + `blitter-lease-gate` (loopback).
- The gateway exposes:
  - `GET /health` — liveness.
  - `GET /status` — local lease + occupancy.
  - `POST /query` — nearest-neighbor search (returns ranked candidates).
  - `POST /compute` — accepts a job, acquires a local lease, executes locally
    OR forwards via passthrough to the leased executor.
  - `POST /passthrough` — relay a passthrough frame to the next hop.
- Gossip runs on UDP port 8792 (default). HTTP on 8790 (gateway) and 8791
  (daemon). Lease gate on 8790 in front of the daemon.

## 8. Required atomic lease/fencing

The architecture **requires** target-side atomic lease/fencing — unchanged from
V1. Every mesh node that can execute work runs a `blitter-lease-gate`. The
lease is acquired at the destination, not at the gateway or any relay.

## 9. Migration and re-routing

- For queued work: if a better nearest neighbor appears, re-route to it.
  Execution still requires the destination's lease.
- For checkpointable running work: acquire the new destination's lease first,
  then checkpoint/handoff, then resume.
- Non-checkpointable work waits for the next semantic boundary.

## 10. Verification

Narrow checks:

```bash
python3 -m unittest -v tests/test_p2p_designation.py
python3 -m unittest -v tests/test_p2p_gossip.py
python3 -m unittest -v tests/test_p2p_routing.py
python3 -m unittest -v tests/test_p2p_lease.py
python3 -m unittest -v tests/test_p2p_gateway.py
```

Verify the exact Git revision. Passing local tests do not prove CI ran for
that revision.

## 11. Security and trust boundaries

- Gossip is advisory. Stale or forged advertisements must not grant ownership.
- Lease state is authoritative only at the destination gate.
- Passthrough relays must not inspect job payloads or lease tokens.
- Lease tokens are bearer secrets and must not appear in gossip frames.
- A future mesh-level authentication layer (e.g., Tailscale node keys,
  mTLS between gates) is out of scope for V0.

## 12. Nonclaims

This fabric does not by itself:

- make arbitrary processes migratable;
- turn a heartbeat or status into an exact occupancy reservation;
- implement target-side atomic leases in the current Python helpers;
- make stale inventory trustworthy;
- make approximate target output mathematically authoritative;
- imply that currently deployed daemons implement every branch protocol;
- make a difficult search problem tractable merely by distributing it.

It provides a gossip-based, multi-axis, nearest-neighbor routing substrate on
which heterogeneous and ephemeral compute can participate without becoming
permanent assumptions in the system's memory.
