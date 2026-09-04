# Blitter ISA Profile Semantic Review V1

**Profile:** `BLITTER-ISA-V1`
**Profile digest:** `sha256:cd60e19d6c3652e08d4ed36e3f8208de7f5d2c553cb7e8f5123175c63ff2f129`
**Status:** `PROVISIONAL_PENDING_REVIEW` — **not authoritative**
**Mechanical gate:** PASS (`scripts/verify_blitter_isa_profile.py`,
`scripts/test_blitter_isa_profile.py`, 5/5)

## What the mechanical gate proves

The gate is byte-exact and fully green, but its claims are deliberately
narrow:

1. the profile JSON is self-consistent (embedded `semantic_digest` matches the
   canonical encoding, lock file matches);
2. the declared U192 format-byte tag map equals the executable codec's tag map
   (`experiments/webgpu_blitter/u192_codec.py`, `LENGTH_TO_TAG` round-trip for
   all 256 raw bytes);
3. the normalization matrix is exact (`u192_normalization_matrix.matrix_is_exact_v1`);
4. the closed-universe fields remain `false` (no ambient endianness/word-size,
   no mid-execution environment reads, no clock/sensor requirements);
5. lease/fencing, heartbeat, ratchet-clock, jitter, thermal/RAS/ECC, and the
   security plugin remain separate protocol layers outside the arithmetic
   opcode set;
6. the B4 scope exclusions (`merge.wgsl` / `tailgain.wgsl` are not LKB/Burau
   computations) are present.

The gate does **not** prove that any implementation conforms to the profile,
that the opcodes mean what a mathematician expects, or that the profile is
safe to treat as authority. The profile itself states this in
`semantic_digest_rule` and `provisional_notice`.

## Required semantic review (human authority, all open)

No reviewer has signed the items below. Until each is completed, signed, and
recorded here, the profile MUST NOT be cited as semantic authority, MUST NOT
gate deployments, and MUST NOT promote claims.

| # | Review item | Owner | Verdict |
|---|-------------|-------|---------|
| R1 | `U192_ADD_MOD` relation `value(output) = (value(a)+value(b)) mod 2^192` matches the executable codec's limb order (le-u32-lsw-first vs be variants) for all four admitted tags | — | OPEN |
| R2 | `U192_MAX` comparison order (`w5..w0` unsigned lexicographic) matches the executable max reduction, including mixed-format inputs after normalization | — | OPEN |
| R3 | `PREFIX_COVER_VALIDATE` / `GLOBAL_MAX_FROM_COVER` aggregation semantics match the merge/partition implementation's disjoint gap-free partition claim | — | OPEN |
| R4 | Closed-universe claim holds on all deployment targets (no endianness/word-size/host-layout dependence in `verify_u192_carry.cpp`, WGSL shaders, or the Rust codec) | — | OPEN |
| R5 | B4 scope exclusions are complete: no other shader, kernel, or helper in the tree can be mistaken for an LKB/Burau computation | — | OPEN |
| R6 | Protocol-layer separation is operationally real: lease/heartbeat/clock paths cannot alter arithmetic bytes (review `lease_gate.py` commit fence and daemon wiring) | — | OPEN |
| R7 | `MathPunchFiniteState/BlitterU192Transport.lean` witness (`u192AddMod_lt`) is typechecked by the pinned toolchain (`lake build`, CI `lean` lane) and its statement matches the profile relation | — | OPEN |

## Record

- Mechanical evidence: `BLITTER_ISA_PROFILE_GATE: PASS` at the profile digest
  above; CI `isa-profile` lane re-runs it per revision.
- Semantic authority: **none claimed**. This document is a review ledger, not
  an approval.
- Promotion rule: flip `status` to an authoritative value only in the same
  commit that fills every row above with a reviewer identity, a verdict, and
  pointers to the reviewed evidence. A mechanical PASS alone never qualifies.
