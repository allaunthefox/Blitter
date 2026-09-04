# Blitter Semantic Profile Registry v1

**Status:** minimal implemented registry

`scripts/cluster/semantic_profile_registry.py` is a closed registry of semantic
profile identities. A consumer must supply both `profile_id` and
`semantic_digest` to `resolve_profile`. Unknown IDs, malformed digests, stale
digests, and a digest belonging to a different known profile fail closed. There
are no aliases, defaults, profile negotiation, or operation-based fallback.

The digest identifies targeted semantics. It is not implementation-conformance,
execution-authority, lease, image, or provenance evidence.

## Registered profiles

| Profile ID | Version | Operation | Semantic digest |
| --- | ---: | --- | --- |
| `BLITTER-ISA-V1` | 1 | `u192.add-mod` | `sha256:cd60e19d6c3652e08d4ed36e3f8208de7f5d2c553cb7e8f5123175c63ff2f129` |
| `blitter-ternary-step-i32-v1` | 1 | `blitter.ternary-step-i32` | `sha256:987eed923534dcd7470dc45d72eb990bf454d9c52b6c074ec3a78371ae327d8d` |

The U192 digest is the existing lock in
`BLITTER_ISA_PROFILE_V1.sha256`. The ternary digest binds this exported v1
identity tuple, encoded as compact sorted-key ASCII JSON without a trailing
newline:

```json
{"operation":"blitter.ternary-step-i32","profile_id":"blitter-ternary-step-i32-v1","request_codec":"mathpunch.blitter-ternary-step-i32-request.v1","result_codec":"mathpunch.blitter-ternary-step-i32-result.v1","terminal_contract":"mathpunch.blitter-ternary-step-i32-terminal.v1","version":1}
```

Changing any element requires a new profile ID and digest.

## Codec and terminal metadata

| Profile ID | Request codec | Result codec | Terminal contract |
| --- | --- | --- | --- |
| `BLITTER-ISA-V1` | `mathpunch.u192-abi.v1` | `mathpunch.u192-canonical.v1` | `mathpunch.blitter-computation-result-terminal.v1` |
| `blitter-ternary-step-i32-v1` | `mathpunch.blitter-ternary-step-i32-request.v1` | `mathpunch.blitter-ternary-step-i32-result.v1` | `mathpunch.blitter-ternary-step-i32-terminal.v1` |

The U192 request codec is the current tagged U192 ABI consumed by the v1
computation envelope. Its accepted terminal status has no numeric status byte
and carries one canonical U192 result. A malformed request is rejected without
a result.

The exported ternary request and result records are each exactly 26 bytes. A
request contains four little-endian signed i32 rails (bytes 0..15), direction
`0xff`, `0x00`, or `0x01` (byte 16), a little-endian u64 cycle count (bytes
17..24), and one instruction byte (byte 25). The instruction's low and high
nibbles each select a source rail in bits 3..2 and target rail in bits 1..0.

A ternary result starts with one terminal status byte, followed by four
little-endian signed i32 rails, the direction byte, and the little-endian u64
cycle count:

| Status byte | Name | State payload |
| ---: | --- | --- |
| `0` | `accepted` | next state |
| `1` | `write-conflict` | unchanged input state |
| `2` | `domain-exceeded` | unchanged input state |

Invalid request length or a direction outside the three admitted bytes is a
malformed request and produces no result record. The status names and layout
mirror the exported scalar ABI in
`experiments/webgpu_blitter/src/ternary_scalar.rs`; they do not make the Rust
implementation the registry authority.

Both terminal contracts carry `authority: none`. Registry resolution does not
authorize or execute a request. The current U192 computation envelope and WASM
world do not define a ternary transport, so consumers must not route the
ternary profile through them or treat either profile as an alias for the other.
