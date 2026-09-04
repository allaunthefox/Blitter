# MCP/WASM Computation Blob Contract v1

**Status:** implemented reference contract, deployment pending

MCP is the request protocol. WASM is an optional assembler/runtime surface.
This contract defines the canonical envelope sent toward Blitter; it does not
give MCP or a WASM module mathematical or execution authority.

## Envelope

The envelope has schema `mathpunch.blitter-computation-envelope.v1` and exactly
these fields:

- `request_id`
- `operation`, currently only `u192.add-mod`
- `profile_digest`
- `codec`, currently `mathpunch.u192-abi.v1`
- exactly two tagged U192 `inputs`
- positive CPU, memory, and wall-time `resource_budget`
- `terminal_contract_digest`
- an `assembler` containing the WASM module digest and `wasi-preview2-component-v1`
- `authority`, which must be `none`

Canonical bytes are sorted-key, compact ASCII JSON with one trailing newline.
The request digest covers the complete envelope. Unknown fields, noncanonical
commands, invalid U192 encodings, unsupported operations, and authority claims
fail closed.

Raw envelope and MCP assembly-request parsers reject duplicate object keys at
every nesting depth and require the input bytes to equal their canonical
re-encoding. Validation returns wire fields only and is idempotent; decoded U192
words are derived transiently and are never inserted into an envelope.

## Versioned registry binding

The v1 schema and its field set remain unchanged for compatibility. In
particular, v1 validates digest syntax but does not claim registry admission.

The opt-in schema `mathpunch.blitter-computation-envelope.v2` adds exactly one
field, `profile_id`. Its exact `(profile_id, profile_digest)` pair must resolve
through `semantic_profile_registry.py`; the registered operation and request
codec must equal the envelope values. Its `terminal_contract_digest` is the
locked SHA-256 digest for the terminal contract identified by that registry
profile. For `BLITTER-ISA-V1`, this is the digest in
`BLITTER_COMPUTATION_RESULT_TERMINAL_V1.sha256`. A registry terminal contract
without an admitted locked digest fails closed. No profile fallback, alias, or
negotiation is allowed. The reference assembler emits v1 by default and emits
v2 only when explicitly requested with a v2 envelope schema (the CLI uses
`--envelope-version 2`).

## Execution boundary

The MCP/WASM assembler may construct and normalize the envelope. Blitter must
revalidate its bytes, profile, codec, limits, module digest, and lease before
execution. The CPU reference result is a conformance oracle only. A WASM result
is diagnostic/untrusted output until independently verified by MathPunch.

The WASM component must receive only declared input bytes and explicit output
capability. Network, ambient filesystem, host process control, and arbitrary
device imports are disabled by default.

Its manifest is validated by `scripts/cluster/wasm_component.py` before module
selection. The current profile requires a digest-pinned module, the exact WIT
entrypoint, no imports, no host capabilities, bounded memory/fuel/time/output,
and `authority: none`.
