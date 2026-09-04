# Digest-pinned Rust WASM MCP assembler bridge

This isolated prototype implements the repository's
`mathpunch:blitter-blob@1.0.0` WIT assembler export. It converts one strict MCP
JSON request into the canonical, non-authoritative
`mathpunch.blitter-computation-envelope.v1` used by
`docs/specs/BLITTER_MCP_WASM_COMPUTATION_BLOB_V1.md`.

Nothing in this directory is wired into `experiments/webgpu_blitter` or the
shared Python implementation.

## Boundary

- `assemble` is a pure string-to-string function. It performs no filesystem,
  network, process, device, clock, random, or environment access.
- `wit/world.wit` has no imports. A deployment must still inspect the built
  component and instantiate it without undeclared host capabilities.
- Output is compact, ASCII, key-sorted JSON with exactly one trailing newline.
- Unknown request fields, duplicate fields, malformed IDs/digests, non-u32
  limbs, zero budgets, wrong interfaces, and non-U192 profile digests fail
  closed.
- The component assembles transport bytes only. It does not execute U192
  addition and always emits `"authority":"none"`.
- A module cannot securely discover or attest its own artifact digest. The
  host generates a digest-pinned manifest from the final bytes and must require
  the request's `assembler.module_digest` to equal that selected manifest.

## Profiles

Profiles are deliberately separate files with separate integrity sidecars:

- `profiles/u192-v1.json` records the implemented `BLITTER-ISA-V1` identity,
  semantic digest, `u192.add-mod`, and
  `mathpunch.u192-abi.v1`.
- `profiles/ternary-v1.json` independently records the registered
  `blitter-ternary-step-i32-v1` identity, digest, operation, request/result
  codecs, and terminal contract.

The Rust component accepts only the U192 semantic profile digest. The registry
defines ternary semantics, but the inspected computation-envelope and WIT
assembler contracts define only U192. A ternary assembler therefore needs its
own future envelope and WIT contract; it is not treated as an alias, mode, or
extension of the U192 v1 world.

The semantic registry requires consumers to resolve a `profile_id` plus digest,
while the inspected v1 computation envelope carries only `profile_digest`.
This assembler can pin the known U192 digest but cannot add the missing field
without breaking the exact v1 envelope. Deployment admission must resolve the
U192 ID/digest pair out of band until a new envelope version closes that gap.

Verify profile-file integrity from this directory with:

```sh
sha256sum profiles/u192-v1.json profiles/ternary-v1.json
```

## Test

```sh
cargo test --locked
```

The native tests cover deterministic canonical output, little-endian U192 limb
encoding, strict fields and limits, invalid limbs, authority, and rejection of
non-U192 profile digests. `examples/request.json` is a host-input example; its
placeholder module digest must be replaced with the generated artifact digest
before deployment.

## Build

The intended target is Rust's component-aware WASI Preview 2 target:

```sh
rustup target add wasm32-wasip2
cargo build --locked --release --target wasm32-wasip2 --lib
```

The artifact is
`target/wasm32-wasip2/release/mcp_wasm_bridge.wasm`. The checked-in WIT is an
isolated copy of the inspected v1 WIT so this crate does not depend on or alter
files outside this directory.

Inspect the produced component before selection:

```sh
wasm-tools validate --features component-model target/wasm32-wasip2/release/mcp_wasm_bridge.wasm
wasm-tools component wit target/wasm32-wasip2/release/mcp_wasm_bridge.wasm
```

The reported world must export
`mathpunch:blitter-blob/assembler@1.0.0` and report no imports. A
component-capable host should additionally invoke `assemble` with
`examples/request.json` and compare the result with `cargo test`'s canonical
fixture behavior.

## Manifest generation

Generate the manifest only after the final release artifact is built:

```sh
cargo run --locked --quiet --bin manifest-gen -- \
  target/wasm32-wasip2/release/mcp_wasm_bridge.wasm \
  registry.example.invalid/mathpunch/mcp-u192-assembler \
  > target/wasm32-wasip2/release/manifest.json
```

`manifest-gen` hashes the exact artifact bytes with SHA-256 and emits compact,
sorted JSON conforming to
`mathpunch.blitter-wasm-component-manifest.v1`. Its manifest declares empty
imports, no ambient capabilities, bounded memory/fuel/time/output, and no
authority. Rebuilding requires regenerating the manifest; never reuse a digest
from another artifact.

The repository's existing validator can check the generated file without this
prototype importing or modifying shared Python code:

```sh
python -c 'import json,sys; from scripts.cluster.wasm_component import validate_manifest; validate_manifest(json.load(open(sys.argv[1], encoding="utf-8")))' \
  target/wasm32-wasip2/release/manifest.json
```

## Local tooling blockers

On the environment used to create this prototype, `cargo test` passes, but the
component build cannot complete because the Rust sysroot does not contain the
`wasm32-wasip2` standard library (`E0463: can't find crate for core`). `rustup`
is not installed, so the target cannot be added with the command above.

`wasm-tools`, `cargo-component`, and `wasmtime` are also absent. Consequently,
the component exports/imports and runtime call could not be independently
inspected or exercised here. `cargo fmt`/`rustfmt` are absent as well. These are
tooling blockers, not silently skipped successful checks.

The registered ternary profile currently has no corresponding canonical
computation-envelope schema or WIT assembler world. This prototype records and
rejects that distinct profile rather than guessing those missing contracts.
