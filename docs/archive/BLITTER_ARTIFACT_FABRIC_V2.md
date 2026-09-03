# Blitter Artifact Fabric V2

**Status:** proposed architecture; implementation not present in this workspace.

This design incorporates the artifact-fabric material in
`ChatGPT-Revamp_Blitter_Design.md` and supersedes node-centric Beacon V1 for
semantic work. Beacon V1 remains the minimal transport/session boundary.

## 1. Core Model

```text
canonical request
    + artifact digest
    + dependency digests
    + operation/profile
    + terminal contract
    + resource budget
             |
             v
      execution offers
       /      |      \
    worker A worker B worker C
       \      |      /
        candidate receipts
                |
                v
       deterministic commit
                |
                v
       recoverable semantic state
```

The network answers:

```text
where can this artifact be evaluated efficiently?
```

It does not answer:

```text
which node owns the computation?
```

Workers are disposable execution surfaces. The artifact, checkpoint lineage,
receipt, and commit record hold semantic state outside the worker.

## 2. Internet-Layer Boundary

```text
IP / Tailscale       reachability and routing
TCP / QUIC            ordered transport and backpressure
TLS / mTLS            session and participant authentication
NKAT                  canonical artifact/request/receipt transfer
Beacon session        capability offers and liveness
Capacity gate         local resource admission
Executor              backend evaluation
Verifier/committer    semantic authority
```

No NKAT object contains an application route, relay path, or network authority.
Transport can later be direct, relayed, object-store-backed, or privacy-wrapped
without changing the request or receipt digest.

## 3. Stable Semantic Objects

### Capability offer

An ephemeral, signed offer describes a verified execution profile:

```text
instance identity
executor ABI/profile
operation and codec versions
exactness class
execution surfaces
resource ceilings
current pressure
expiry
implementation/probe digest
```

The offer is evidence of possible execution, not a lease and not a claim of
mathematical authority.

### Execution request

The request is immutable and content-addressed:

```text
request_id
artifact_digest
capsule_id
generation
dependency_receipt_digests
required_capabilities
terminal_contract_digest
resource_budget
```

### Execution receipt

The receipt binds what happened without automatically promoting the result:

```text
request_digest
artifact_digest
executor/profile version
attempt epoch
terminal digest
resource observations
accepted/rejected state
```

## 4. Capacity Versus Authority

V2 separates two concepts:

```text
capacity reservation:
    avoid wasting resources on duplicate work

semantic authority:
    verifier + terminal contract + deterministic commit
```

Duplicate execution is allowed for deterministic work. Conflicting commits are
not. A local capacity gate may limit concurrency, but it must not be described
as the source of semantic ownership.

External side effects or non-idempotent operations still require stronger
destination fencing and must not use duplicate execution as a shortcut.

## 5. Recovery

The intended execution guarantee is:

```text
at-least-once physical execution
exactly-once canonical commit
```

Worker loss follows this sequence:

```text
heartbeat/session expires
  -> offer removed from placement
  -> current attempt fenced
  -> latest accepted checkpoint retained
  -> replacement offer selected
  -> checkpoint verified or artifact replayed
  -> new attempt executed
  -> receipt committed idempotently
```

Attempt epochs reject late results. Checkpoint manifests form a content-addressed
lineage:

```text
C0 <- C1 <- C2 <- C3
```

Pulse and progress are separate boot-scoped counters:

```text
pulse     local executor completed a sweep
progress  canonical semantic state advanced
```

Neither is a distributed clock. The tic clock must be generated at the adapter's
semantic commit boundary, not by a network heartbeat or wall-clock tick.

## 6. Backend Profiles

Blitter is a canonical transition machine, not a graphics API. A backend may be
CPU scalar, SIMD, WASM, WebGPU, Vulkan, NPU, FPGA, or another authorized surface.

Each backend advertises a profile only after a conformance probe demonstrates:

```text
Decode_backend(Execute_backend(Encode_backend(state), operation))
    == canonical_step(state, operation)
```

Hardware labels such as `has_gpu` or `has_canvas` are insufficient. Offers must
name the semantic operation, exactness, limits, checkpoint support, and probe
digest.

## 7. MCP Projection

The MCP Gateway exposes stable semantic tools, not physical nodes:

```text
blitter.execute_matrix_u32
blitter.execute_exact_matrix
blitter.verify_capsule
```

It must not expose node-specific tools such as:

```text
execute_on_node_abc_gpu0
```

The gateway may change the underlying placement without changing the semantic
tool contract. Tool availability changes only when the aggregate capability
class changes.

## 8. V2 Does Not Require

- UDP gossip;
- worker-maintained route tables;
- application-level hop paths;
- lease advertisements;
- a worker owning durable goal state;
- a permanently assigned physical node;
- a graphics surface as the semantic primitive.

The minimal deployment is an authenticated gateway session, a signed capability
offer, an immutable artifact request, a local capacity gate, a loopback executor,
and an independently verified deterministic commit.

## 9. Extreme Conformance Target: Atari over SIO

An Atari 8-bit computer attached through SIO is a valid execution surface. It is
slow and bandwidth-constrained, but those constraints affect placement cost, not
semantic membership.

```text
MCP Gateway
    -> authenticated serial bridge / beacon
    -> framed compact request
    -> Atari 6502 execution surface
    -> framed checkpoint or result
    -> bridge receipt
    -> gateway verifier
```

The Atari is not an Internet participant and does not need to implement TLS,
MCP, leases, or peer discovery. The serial bridge owns those responsibilities
and treats the Atari as an untrusted local accelerator. The bridge must enforce
resource limits, request framing, timeout, reset, and attempt fencing.

The request should carry a compact canonical computation rather than a rendered
framebuffer or large state image:

```text
operation profile
coefficients / tile bounds
initial accumulator state
input and artifact digests
checkpoint boundary
attempt epoch
```

For a triangle edge function reduced to `E(x,y) = ax + by + c`, the hot loop can
use incremental integer updates:

```text
E(x + 1, y) = E(x, y) + a
E(x, y + 1) = E(x, y) + b
```

A capability offer must describe the actual profile and measured limits, not the
platform name:

```text
triangle-edge-i16-v1
matrix-cell-u8-v1
checksum-u8-v1
checkpoint-tile-v1
working_memory_bytes
max_tile_width / max_tile_height
transport_cost
```

The semantic tic is backend-independent:

```text
pulse     one completed local execution batch
progress  one verified semantic tile or transition committed
```

It is not an Atari CPU cycle, GPU wavefront, serial packet, or wall-clock tick.
Counters remain scoped to `(node_id, boot_id)` and are evidence only.

This target is useful because it forces the design to prove that:

- canonical inputs are smaller than their rendered representations;
- transport framing is separate from computation;
- output can be checked independently;
- checkpoint state is portable;
- dropped execution is recoverable;
- hardware category is not part of semantic identity.

The expected execution guarantee remains:

```text
at-least-once physical execution
exactly-once canonical commit
```
