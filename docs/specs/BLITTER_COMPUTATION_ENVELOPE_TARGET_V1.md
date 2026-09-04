# Blitter Computation Envelope Target Contract v1

**Status:** implemented canonical v2 target path; deployment policy remains operator-owned

`POST /compute-envelope` accepts only
`mathpunch.blitter-computation-envelope.v2`. The daemon exposes no `/compute`
route and no v1 envelope compatibility path.

## Required endpoint

An eventual target implementation shall expose `POST /compute-envelope` through
the lease gate and forward the same path to the loopback daemon. It must:

- validate canonical envelope bytes and reject unknown fields;
- validate the profile, codec, assembler manifest, and resource budget against
  deployment-owned policy;
- decode both tagged U192 inputs;
- compute addition modulo `2^192`;
- return `mathpunch.blitter-computation-result.v1` with the request digest and
  six canonical words; and
- perform lease validation before execution and at commit.

The target must not unwrap an envelope into any removed or legacy executor,
because that would execute a different operation and produce a different result
ABI.

The daemon admits envelope execution only when deployment-owned policy is
explicitly configured with `BLITTER_ENVELOPE_PROFILE_DIGEST`,
`BLITTER_ENVELOPE_TERMINAL_CONTRACT_DIGEST`, and
`BLITTER_ENVELOPE_ASSEMBLER_MODULE_DIGEST`, plus positive maxima in
`BLITTER_ENVELOPE_MAX_CPUS`, `BLITTER_ENVELOPE_MAX_MEMORY_MIB`, and
`BLITTER_ENVELOPE_MAX_WALL_MS`. Missing or malformed policy fails closed. No
signature field or implicit signer is interpreted by this target. The deployed
v2 path additionally requires `BLITTER_ENVELOPE_PROFILE_ID=BLITTER-ISA-V1` and
matches its digest and terminal contract digest against the closed semantic
profile registry; policy cannot substitute another profile identity.
