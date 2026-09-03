# P2P Gossip Protocol V0

**Schema:** `mathpunch.p2p.gossip.v0`
**Status:** draft — spec + prototype on `p2p-blitter`
**Layer:** discovery, presence, and routing; explicitly outside the lease/fencing and arithmetic layers.

## 1. Purpose

The blitter fabric is no longer a single daemon guarded by one gate. Nodes join
a peer-to-peer mesh at runtime, advertise their **multi-axis capability and load
profile**, and clients route work to the **nearest compatible peer** by gossip
propagated designations. The gossip protocol carries:

1. **Node advertisements** — who a node is, what it can do, how loaded it is.
2. **Presence heartbeats** — liveness, expiry, timing diagnostics.
3. **Lease advertisements** — which node currently owns which slot.
4. **Compute passthrough** — a request can be forwarded hop-by-hop toward the
   leased executor.

The governing rule is:

> **Gossip is evidence. Lease is authority. Passthrough is transport.**

Gossip tells a client *where* to go. Only a granted lease at the destination
authorizes compute. Passthrough just moves bytes; intermediate hops are relays,
not owners.

## 2. Multi-axis node designation

Every node in the mesh carries a **designation** that is a tuple of orthogonal
axes. No single axis grants authority; they are combined to decide fit.

| axis | type | meaning | example |
|---|---|---|---|
| `node_id` | stable-within-boot string | `<name>\|<lifecycle>\|<arch>\|<cpu>\|<mem>\|<boot>` | `qfox-1\|persistent\|x86_64\|cpu=8\|mem=20GiB\|boot=abcd1234` |
| `capabilities` | frozenset of strings | mathematical/codec capabilities | `webgpu-blitter`, `exact-i32`, `laurent-product-v1` |
| `controlled_subsystems` | frozenset of strings | user-controlled semantic dependencies | `accelerator-api`, `userspace` |
| `execution_surfaces` | frozenset of strings | validated ABI surfaces | `webgpu`, `wasm` |
| `accessibility_profile` | enum | spawn envelope depth | `sandbox`, `direct`, `container`, `unknown` |
| `load` | object | current occupancy | `active_compute`, `queued_compute`, `max_concurrent_compute` |
| `proximity` | object | network proximity | `rtt_ms`, `hop_count`, `tailscale_subnet` |
| `lifecycle` | enum | persistence class | `persistent`, `ephemeral` |
| `availability_expires_unix_ms` | int | soft-state expiry | monotonic timestamp |
| `retire_at_unix_ms` | int or null | known retirement | |

`accessibility_profile` and `execution_surfaces` follow
[`CONTROLLED_SPAWN_SEMANTICS_V0.md`](CONTROLLED_SPAWN_SEMANTICS_V0.md).
`capabilities` mirrors the surface-capitalization scheme
(`surface:<name>` plus codec tags).

A node is **routable** only when:

- `availability_expires_unix_ms` has not passed;
- `lifecycle != ephemeral` OR the ephemeral TTL has not expired;
- `retire_at_unix_ms` is null or in the future;
- `proximity.rtt_ms` is sane (not epoch-era);
- `load.max_concurrent_compute > active_compute + queued_compute`.

A stale or failed advertisement is **routing evidence only**. It must never
grant ownership.

## 3. Wire format

Gossip messages are newline-delimited JSON over UDP. Each message is one JSON
object with a mandatory `type` field.

### Advertisement (`type: "advertise"`)

```json
{
  "type": "advertise",
  "schema": "mathpunch.p2p.gossip.v0",
  "node_id": "qfox-1|persistent|x86_64|cpu=8|mem=20GiB|boot=abcd1234",
  "instance_id": "qfox-1:1786700000000:1234",
  "capabilities": ["webgpu-blitter", "exact-i32", "laurent-product-v1"],
  "controlled_subsystems": ["accelerator-api"],
  "execution_surfaces": ["webgpu"],
  "accessibility_profile": "sandbox",
  "spawn_semantics_version": 1,
  "load": {
    "active_compute": 0,
    "queued_compute": 1,
    "max_concurrent_compute": 1,
    "status_seq": 42
  },
  "proximity": {
    "rtt_ms": 2,
    "hop_count": 1,
    "tailscale_subnet": "100.88.57.0/24"
  },
  "lifecycle": "persistent",
  "ephemeral": false,
  "availability_ttl_ms": 3500,
  "availability_expires_unix_ms": 1786700003500,
  "retire_at_unix_ms": null,
  "sent_unix_ms": 1786700000000,
  "heartbeat_seq": 7,
  "lease_epoch": 9,
  "lease_holder": "worker-a",
  "lease_active": true
}
```

### Lease advertisement (`type: "lease_advertise"`)

```json
{
  "type": "lease_advertise",
  "schema": "mathpunch.p2p.gossip.v0",
  "node_id": "qfox-1|persistent|x86_64|cpu=8|mem=20GiB|boot=abcd1234",
  "lease_epoch": 9,
  "lease_holder": "worker-a",
  "lease_active": true,
  "lease_expires_unix_ms": 1786700120000,
  "in_flight": false
}
```

### Heartbeat-only (`type: "heartbeat"`)

A minimal liveness frame carrying only the sequence and timing diagnostics.
Used when a full advertisement would be redundant.

```json
{
  "type": "heartbeat",
  "schema": "mathpunch.p2p.gossip.v0",
  "node_id": "qfox-1|persistent|x86_64|cpu=8|mem=20GiB|boot=abcd1234",
  "sent_unix_ms": 1786700005000,
  "heartbeat_seq": 8,
  "availability_expires_unix_ms": 1786700008500,
  "active_compute": 0,
  "queued_compute": 1
}
```

### Query (`type: "query"`)

A client or peer asks the mesh for a nearest compatible node.

```json
{
  "type": "query",
  "schema": "mathpunch.p2p.gossip.v0",
  "requires": ["webgpu-blitter", "exact-i32"],
  "requires_control": ["accelerator-api"],
  "acceptable_surfaces": ["webgpu"],
  "qos_tier": "latency_sensitive",
  "max_results": 1,
  "scope": "local"
}
```

`qos_tier` (`latency_sensitive` | `throughput_sensitive` | `default`)
modulates how aggressively load and RTT penalize candidates.
`scope` (`local` | `neighbor` | `mesh`) bounds the flood depth;
`mesh` requires `ttl`.

### Response (`type: "response"`)

```json
{
  "type": "response",
  "schema": "mathpunch.p2p.gossip.v0",
  "query_id": "<uuid>",
  "nodes": [
    {
      "node_id": "qfox-1|persistent|x86_64|cpu=8|mem=20GiB|boot=abcd1234",
      "score": 150,
      "proximity": {"rtt_ms": 2, "hop_count": 1},
      "load": {"active_compute": 0, "queued_compute": 1}
    }
  ]
}
```

## 4. Gossip behavior

### Advertisement propagation

- Each node multicasts or unicast-gossips its full `advertise` frame on a
  configured interval (default 1000 ms).
- On receipt, a node validates the frame and merges the sender into its local
  **peer table** keyed by `node_id`.
- If the sender already exists and the new `heartbeat_seq` is older than or
  equal to the stored sequence, the message is discarded (or recorded as
  reordered/duplicate for diagnostics).
- If `heartbeat_seq` is newer, the peer table is updated.
- A peer whose `availability_expires_unix_ms` has passed is marked **stale**.
  Stale peers remain in the table as historical provenance but are excluded from
  routing.
- A peer whose `retire_at_unix_ms` has been reached is marked **retired** and
  removed from routing.

### Lease advertisement propagation

- When a lease is acquired, renewed, released, or expired at a node, that node
  gossips a `lease_advertise` frame.
- Receiving a `lease_advertise` updates the local view of that node's lease
  state. It does **not** grant the lease to the receiver; it only mirrors the
  destination's state.
- Lease advertisements are scoped: a node only forwards a lease advertisement
  to peers that have previously sent it an `advertise` or `query` frame.
  This limits blast radius and prevents lease state from flooding the mesh.

### Query propagation

- A `query` is flooded with a **time-to-live (TTL)** and a **scope** field.
- `scope = "local"`: only direct peers.
- `scope = "neighbor"`: peers of peers (two hops).
- `scope = "mesh"`: no TTL limit (flood the mesh).
- Each hop decrements TTL. When TTL reaches 0, the node stops forwarding.
- A responding node sends a `response` directly back to the query originator.
- Responses are deduplicated by `query_id`.

### Timing diagnostics

A receiver stamps `received_unix_ms` and derives, exactly as
[`blitter_heartbeat.py`](../../scripts/cluster/blitter_heartbeat.py):

```text
apparent_skew_ms = received_unix_ms - sent_unix_ms
sequence_gap, duplicate, reordered
sender_clock_invalid, receiver_clock_invalid, time_anomaly
```

Epoch-era timestamps (before 2000-01-01T00:00:00Z) are marked as anomalies and
exclude the peer from routing.

## 5. Nearest-neighbor routing

Given a `WorkSpec` with `requires`, `requires_control`, `acceptable_surfaces`,
and `preference_weights`, the router:

1. **Hard-filter** the peer table:
   - `requires <= node.capabilities`
   - `requires_control <= node.controlled_subsystems`
   - `acceptable_surfaces` intersects `node.execution_surfaces` (or is empty)
   - `accessibility_profile != none`
   - node is routable (fresh, not retired, timing-sane, has available slots)
 2. **Compute proximity score**:
    ```text
    score = suitability_score(work, node)
           - load_penalty(node, qos_tier)
           - rtt_penalty(node, qos_tier)
           - skew_penalty(node)
    ```
    where:
    - `suitability_score` = sum of `preference_weights[cap]` for each matching
      capability (same as `slot_scheduler.py`).
    - `load_penalty(node, tier)` = normalized queue pressure × `load_weight[tier]`:
      - `latency_sensitive`: `load_weight = 3.0`
      - `default`: `load_weight = 1.0`
      - `throughput_sensitive`: `load_weight = 0.25`
    - `rtt_penalty(node, tier)` = `proximity.rtt_ms` × `rtt_weight[tier]`:
      - `latency_sensitive`: `rtt_weight = 2.0`
      - `default`: `rtt_weight = 1.0`
      - `throughput_sensitive`: `rtt_weight = 0.25`
    - `skew_penalty` = `max(0, apparent_skew_ms - 60_000)` (constant across tiers).
    This lets a heavy compile (`throughput_sensitive`) land on a loaded
    but capable node while its check (`latency_sensitive`) routes to the
    fastest idle peer.
 3. **Rank** descending by score, then by `accessibility_rank` (lower is
    preferred — `direct=0`, `sandbox=50`, etc.), then deterministically by
    `node_id`.
 4. Return the top `max_results` candidates.

"Nearest" is therefore a **multi-axis proximity** modulated by QoS tier:
capability fit is hard; load and latency are ranking criteria whose
weights shift with the work's urgency class.

## 6. Compute passthrough

Once the router selects a target node, the client (or an intermediate hop)
forwards the compute request toward that node.

### Passthrough request (`type: "compute_passthrough"`)

```json
{
  "type": "compute_passthrough",
  "schema": "mathpunch.p2p.gossip.v0",
  "query_id": "<uuid>",
  "path": ["node-a", "node-b"],
  "ttl": 10,
  "job": { "a": [[0,0,1]], "b": [[0,0,1]] },
  "lease": {
    "instance_id": "...",
    "lease_epoch": 9,
    "lease_token": "..."
  }
}
```

- `path` records the ordered list of `node_id`s the request has traversed.
- `ttl` is decremented at each hop; when 0 the request is dropped and a
  `"ttl_exceeded"` error is sent back.
- Each hop appends its own `node_id` to `path`.
- The **final node** in the path is the leased executor. It validates the
  lease against its local `LeaseState` and executes the job through its
  daemon.
- Intermediate hops **must not** inspect or modify the job payload or lease
  token. They relay the frame verbatim.
- The response is routed back along the reverse path.

### Passthrough response (`type: "compute_response"`)

```json
{
  "type": "compute_response",
  "schema": "mathpunch.p2p.gossip.v0",
  "query_id": "<uuid>",
  "path": ["node-a", "node-b", "node-c"],
  "ok": true,
  "terms": [[0,1,21], ...],
  "adapter": "AMD Radeon Graphics (RADV RENOIR)",
  "executor_node_id": "qfox-1|persistent|x86_64|cpu=8|mem=20GiB|boot=abcd1234"
}
```

## 7. Authority boundaries

| Layer | Purpose | May route? | May authorize compute? |
|---|---|---|---|
| Gossip advertisement | Presence, load, capability | Yes | No |
| Lease advertisement | Lease state mirror | Yes | No |
| Lease grant at destination | Ownership/fencing | No | **Yes** |
| Compute passthrough | Transport | Yes | No |
| Daemon execution | Arithmetic | No | **Yes** |

A stale heartbeat may cause a failed lease attempt; it must never cause duplicate
ownership. Lease state is authoritative only at the destination node's gate.

## 8. Failure model

- **Peer departure**: advertisements expire; the peer leaves the routing table.
- **Partition**: the mesh splits; each partition continues with its own peers.
  A client in a partition can only route to peers it can reach.
- **Loop**: TTL and `path` tracking prevent infinite forwarding.
- **Stale lease**: the destination gate checks the lease epoch/token/deadline
  before and after compute. A stale lease fences at commit.
- **Passthrough failure**: if a relay node is unreachable, the client re-queries
  the mesh for an alternative nearest neighbor.

## 9. Nonclaims

This protocol does not by itself:

- implement target-side atomic leases (that remains the gate's job);
- make a heartbeat or `/status` into an ownership reservation;
- guarantee that the "nearest" peer remains available during compute;
- provide durable consensus across the mesh;
- promote decoded output to mathematical authority.

It provides a gossip-based discovery and routing substrate on which the
existing lease/fencing and daemon layers can operate in a distributed mesh.
