# Controlled Spawn Semantics V0

Status: design/implementation contract on `feat/webgpu-blitter-occupancy-status`.

This contract defines how the MathPunch fabric decides whether a workload may be
spawned on a candidate execution surface. The escalation criterion is **user
control of the subsystems the workload semantically depends on**, not whether a
particular packaging technology is fashionable or whether the provider claims a
feature exists.

Isolation also serves a second purpose: **reproducibility**. The portable
reproducibility boundary is specified in
[`REPRODUCIBLE_ISOLATION_V0.md`](REPRODUCIBLE_ISOLATION_V0.md). A substrate such
as NixOS, OCI, LXC, KVM, WebGPU, or WASM may realize that boundary, but the
boundary MUST NOT be defined by the substrate name.

The controlling rule is:

```text
work.required_control <= surface.controlled_subsystems
```

If that inclusion fails, the workload must use a stronger controlled envelope or
another validated execution surface. Compatibility is subordinate to control.

## Orthogonal dimensions

The fabric MUST keep these concepts separate:

1. `accessibility_profile` — how the current execution envelope is reached;
2. `controlled_subsystems` — which semantic dependencies are under user control;
3. `execution_surfaces` — which validated ABI execution mechanisms are actually usable;
4. mathematical/codec capabilities — what operations the surface can perform;
5. lease state — whether the caller currently owns the slot;
6. reproducible isolation — which substrate-neutral observation/replay contract the surface can realize.

An accessibility tag never grants lease authority, never proves reproducibility,
and never promotes a mathematical result.

## Accessibility profile tag

`accessibility_profile` is a routing/provenance tag. V0 values are:

```text
direct      user-controlled process/userspace on the current host envelope
namespace   bounded namespace/FHS compatibility envelope
container   user-controlled container/LXC-like userspace envelope
guest       user-controlled guest kernel/userspace (for example KVM/QEMU)
emulated    user-controlled machine/CPU model in software emulation
sandbox     restricted ABI surface such as WebGPU or WASM
remote      only a validated remote/proxy ABI is reachable
unknown     the control depth has not been established
none        no validated execution surface remains
```

The profile is intentionally not a theorem of trust. It records the spawn
boundary. `unknown` MUST NOT be rewritten to `none`: a node may have a useful
validated WebGPU/WASM surface even when deeper provider control cannot be
established.

For deterministic routing V0 assigns a non-authoritative cost rank:

```text
direct=0, namespace=10, container=20, guest=30,
emulated=40, sandbox=50, remote=60, unknown=90, none=255
```

Lower is preferred only after hard requirements are satisfied. The rank is a
cost/encapsulation preference, not a security, correctness, or reproducibility
score.

## Controlled subsystems

V0 canonical subsystem tags are:

```text
userspace
loader
filesystem
process-tree
mount-namespace
pid-namespace
network-namespace
cgroup
kernel
virtual-devices
cpu-model
firmware
accelerator-api
```

A workload declares only the subsystems whose semantics it actually requires.
For example, an unpatched foreign ELF may require `{userspace, loader,
filesystem}`; a kernel-coupled workload additionally requires `kernel`; a
machine-sensitive legacy binary may additionally require `cpu-model` and
`firmware`.

Missing or malformed control declarations fail closed to the empty set. They do
not make the node disappear; they only prevent workloads with corresponding
hard control requirements from using it.

## Execution surfaces

V0 canonical execution surface tags are:

```text
native
fhs
lxc
kvm
qemu-tcg
bochs
webgpu
wasm-webgpu
wasm
rpc
```

These are validated mechanisms, not assumptions. A provider claim or static
inventory entry is insufficient. The surface must have current evidence that its
encode -> execute -> decode path is usable for the advertised MathPunch ABI.

The intended compatibility ladder is therefore conditional rather than blindly
linear. A MathPunch deployment may prefer:

```text
native execution
  -> bounded FHS namespace when loader/filesystem compatibility requires it
  -> LXC/container envelope when userspace/process isolation requires it
  -> KVM guest when an owned guest kernel is required and KVM exists
  -> QEMU-TCG / Bochs when a controlled machine is required but nested virt is absent
  -> WebGPU / WASM-WebGPU / WASM when deeper host control is unavailable but a
     validated portable ABI surface remains
  -> validated RPC proxy when only the protocol boundary is user-controlled
  -> none only when no conforming surface remains
```

The implementation may realize the same ordering with Nix, another hermetic
resolver, another container/runtime family, or another conforming substrate.
The substrate name is not the semantic ordering rule.

WebGPU/WASM are therefore graceful-degradation surfaces before terminal failure,
not evidence that the host itself is under user control.

## Heartbeat/status shape

The blitter status/heartbeat path carries the live spawn envelope beside ordinary
capabilities:

```json
{
  "spawn_semantics_version": 1,
  "accessibility_profile": "sandbox",
  "controlled_subsystems": ["accelerator-api"],
  "execution_surfaces": ["webgpu"],
  "slot_capabilities": [
    "webgpu-blitter",
    "laurent-product-v1",
    "exact-i32",
    "surface:webgpu"
  ]
}
```

A richer controlled host may instead advertise, for example:

```json
{
  "accessibility_profile": "direct",
  "controlled_subsystems": [
    "userspace", "loader", "filesystem", "process-tree",
    "mount-namespace", "pid-namespace", "network-namespace", "cgroup",
    "accelerator-api"
  ],
  "execution_surfaces": ["native", "fhs", "lxc", "webgpu", "wasm"]
}
```

`execution_surfaces` are also mirrored as `surface:<name>` capability tags so
existing suitability weights can prefer one usable surface without conflating
surface presence with control authority.

These heartbeat fields describe placement capability only. They MUST NOT be
used as a substitute for a reproducible-isolation receipt. The latter binds the
semantic environment and a realization witness and is a separate design target.

## Work declaration

A work item may add hard control and surface requirements to the existing
mathematical capabilities:

```json
{
  "requires": ["webgpu-blitter", "exact-i32"],
  "requires_control": ["accelerator-api"],
  "acceptable_surfaces": ["webgpu", "wasm-webgpu", "wasm"]
}
```

Eligibility is:

```text
capability_ok = requires <= slot.capabilities
control_ok    = requires_control <= slot.controlled_subsystems
surface_ok    = acceptable_surfaces is empty
             or intersection(acceptable_surfaces, slot.execution_surfaces) is nonempty
spawn_ok      = slot.accessibility_profile != none and surface_ok and control_ok
eligible      = healthy and available and capability_ok and spawn_ok
```

An empty `requires_control` preserves existing jobs: they do not acquire stronger
assumptions merely because the new metadata exists.

A future reproducibility-aware declaration may additionally require a portable
isolation-contract identity and minimum reproducibility class. Until that runtime
support exists, existing work declarations MUST NOT be relabeled as proving
portable reproducibility.

## Reproducible isolation interpretation

The controlled-spawn substrate is a **realization** of a substrate-independent
isolation contract.

A preferred MathPunch deployment may realize the same contract through:

```text
Nix/native derivation
FHS capsule
NixOS-managed or other LXC/container envelope
NixOS-managed or other KVM/QEMU guest
software emulator
portable WASM/WebGPU surface
validated RPC boundary
```

Another implementation may use an entirely different resolver or operating
system while satisfying the same isolation contract.

The portable reproducibility identity binds normalized inputs, normalized
dependency/environment semantics, the frozen ABI/ISA/codec contract, and the
isolation policy. It **does not bind the implementation substrate** unless the
workload explicitly declares that substrate as a semantic dependency.

Conceptually:

```text
reproducibility_identity = H(
    isolation_contract,
    input_manifest,
    environment_manifest,
    abi_isa_codec_contract
)

realization_witness = {
    reproducibility_identity,
    substrate,
    artifact_identity,
    implementation_versions,
    conformance_evidence
}
```

This distinction is what permits the same semantic execution to be reproduced
across, for example, native CPU, WebGPU, WASM, LXC, or KVM realizations.

For MathPunch, a root Nix flake remains the preferred dependency-authority
realization because it gives one updateable closure and consolidates deployment
artifacts. That preference MUST NOT turn NixOS into the definition of
reproducibility or make an independent conforming implementation impossible.

A single-substrate replay proves only local replay. A portable reproducibility
claim SHOULD be exercised against materially different realization families
when available.

## Reproducibility and nondeterministic inputs

Environmental jitter, timing, thermal observations, RAS/ECC events, scheduler
behavior, and similar surfaces may improve routing or search. They do not force
replay onto the original machine.

If such data is advisory only, it remains outside the semantic reproducibility
identity. If it can influence semantic action order or search choices, the run
must use a declared deterministic seed or record the consumed observation stream
for replay.

The future ratchet execution clock can bind those observations to semantic
actions without using wall-clock time as ordering authority. Existing
heartbeat/status counters are not ratchet history and are not reproducibility
receipts.

## Authority

- accessibility/profile metadata: routing/provenance;
- controlled subsystem declaration: hard placement eligibility, but not proof authority;
- execution-surface validation: hard placement eligibility for that surface;
- reproducible isolation contract: semantic observation/replay boundary;
- realization witness: evidence that one substrate implemented that boundary;
- capability tags: operation/codec fit;
- heartbeat/status: advisory freshness/load evidence;
- lease grant/epoch/expiry: execution ownership authority;
- decoded result: candidate/evidence until independent verifier promotion.

A disagreement between layers fails closed for placement or the stronger
reproducibility claim. It does not rewrite or invalidate already verified
mathematical evidence.
