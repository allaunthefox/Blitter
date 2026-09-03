# Blitter Gauntlet Remediation V1

**Date:** 2026-08-14  
**Branch:** `feat/webgpu-blitter-occupancy-status`  
**Current claim registry:** `BLITTER_CLAIM_REGISTRY_V2.md`  
**Frozen semantic profile:** `BLITTER-ISA-V1`  
**Purpose:** track the reviewed defects without conflating source changes, executed evidence, and deployed authority.

Status vocabulary in this ledger is operational only:

- **SOURCE FIXED** — the identified source/design defect has been corrected, but stronger authority may still require executed evidence.
- **GATE ADDED** — a mechanical test/workflow/falsifier exists in source but has not been admitted merely by existing.
- **EVIDENCE PENDING** — execution/receipt/formal-check/deployment evidence is still required.
- **DEPLOYMENT PENDING** — source contract exists but the production topology has not been changed/verified in this work.

No row here promotes a `BLITTER-*` claim; `BLITTER_CLAIM_REGISTRY_V2.md` remains authoritative for claim status.

| gap | reviewed defect | remediation | source state | evidence still required |
|---|---|---|---|---|
| `GAP-01` | Scope overstatement around “universal” WebGPU behavior. | Frozen `BLITTER-ISA-V1` defines semantics; WebGPU is one lowering. Registry limits compatibility to explicitly evidenced adapters. | **SOURCE FIXED** | Tested-adapter inventory and scope-negative checks before empirical promotion. |
| `GAP-02` | No mechanical authority/evidence gate. | Added frozen-profile digest lock, profile validator, claim gates, V2 registry, required evidence/falsifier/contract-removal discipline. | **SOURCE FIXED / GATE ADDED** | Execute gates against exact branch and preserve receipts. |
| `GAP-03` | No independent arithmetic verifier. | Added independent C++ U192 verifier using three u64 limbs + `unsigned __int128`, separate from WGSL/Rust/Python; Lean relation files are separate formal surface. | **SOURCE FIXED / GATE ADDED** | Execute pinned ≥1,000,000-case C++ run; check Lean files with exact toolchain; bind source hashes. |
| `GAP-04` | Six-u32 carry-chain exactness unproved. | Frozen `U192_ADD_MOD`; Lean radix/carry reconstruction; independent C++ edge/random verifier; explicit tagged endian matrix. Claim narrowed to `merge.wgsl`/`tailgain.wgsl`, not Laurent `blitter.wgsl`. | **SOURCE FIXED / GATE ADDED** | Sorry-free Lean check, independent verifier receipt, backend-lowering/source-hash evidence. |
| `GAP-05` | Prefix-slice/global aggregation exactness gap. | Fixed planner under-allocation bug; plan/dispatch now fail closed unless exact `[0,54026402)` partition; fixed U192 worker aggregation to numeric high-limb-first order; added Lean max-of-complete-cover theorem and incomplete-cover counterexample. | **SOURCE FIXED / GATE ADDED** | Execute regressions/formal theorem; exact worker-local-max evidence and complete worker receipt set for any global result. |
| `GAP-06` | `/status` preflight was observational and could not provide atomic lease/fencing. | Added `BLITTER_LEASE_FENCING_V1`, target-side `lease_gate.py`, monotonic TTL, random instance/token + epoch, atomic acquire/renew/release, no replacement owner while stale work remains in flight, and second authority check before result commit. Default cluster compute now requires fenced lease; unfenced path is explicit legacy/testing only. | **SOURCE FIXED / GATE ADDED** | Execute state/delayed-upstream and dispatch tests; integrate with real daemon; **DEPLOYMENT PENDING:** inner daemon must bind loopback and all external compute must traverse gate. TTL remains fencing, not preemption. |
| `GAP-07` | No reproducible CI gate for blitter exactness/support artifacts. | Added `blitter-contract`, `blitter-runtime`, and `blitter-fencing` workflows: frozen profile/claims, endian matrix, partition, C++ verifier, static security plugin, llvmpipe daemon loopback/concurrency, fencing, dispatch, provenance. | **GATE ADDED** | Workflows must actually run/pass on a pinned commit; existence is not evidence of success. |
| `GAP-08` | Daemon contract only source/unit-tested; no real socket integration/concurrency gate. | Added black-box `test_daemon_loopback.py` using real built daemon + independent Python HTTP client; includes health/status, malformed compute, exact fixture, overflow/error cleanup, concurrent clients, 404, adapter-selection startup failure. | **GATE ADDED** | Execute on pinned daemon build/llvmpipe; record build/environment/results. |
| `GAP-09` | Mutable image tag and weak deployment provenance. | Publish captures registry `@sha256`; canonical `image_provenance.py` binds source commit, frozen-profile digest, daemon/Dockerfile hashes; optional drop-in secure stamp; deploy validates canonical bytes/profile lock and uses immutable ref. Replacement preflight refuses to kill a running daemon unless live `/status` proves idle. | **SOURCE FIXED / GATE ADDED** | Actual Harbor push, registry-side digest reinspection, immutable pull/deploy receipt, optional independently verified stamp, tamper negatives. No publication/deployment was performed here. |
| `GAP-10` | Resource boundary could be bypassed or inferred from coarse status. | Default WebGPU cluster compute requires target-side lease; explicit unfenced mode always does a live idle preflight and is documented non-authoritative. Deployment refuses replacement on busy/unknown/unparseable occupancy. Heartbeat remains advisory only. | **SOURCE FIXED / GATE ADDED** | Execute dispatch tests and deployment negatives; production gate topology still pending. Multi-plane resource policy still requires independent live Ray/WebGPU checks before substantive work. |

## Additional defect found during remediation

The original merge planner had a correctness bug not merely an evidence gap: sequential proportional allocation reduced the remaining prefix count while retaining the original total weight denominator, so ordinary multi-worker plans could leave prefixes unassigned. An incomplete cover invalidates the global-maximum conclusion. `merge_partition.allocate_slices` now reserves enough capacity for later slices and validates exact closure before dispatch.

The original cross-worker aggregate also compared Python `[w0..w5]` lists lexicographically, effectively comparing the least-significant word first. `u192_key` now compares `[w5..w0]` unsigned numeric order, matching the frozen `U192_MAX` relation and the WGSL/Rust representation.

## Endianness / ABI remediation

U192 transport follows the same structural/canonical discipline as the nKernel ISA rather than ambient host interpretation:

```text
format byte: vv l b rrrr
0x00 LE-u32 / LSW-first
0x10 BE-u32 / LSW-first
0x20 LE-u32 / MSW-first
0x30 BE-u32 / MSW-first
```

All 256 bytes have a structural decode; V1 admits only version 0 with zero reserved bits. Mixed layouts are valid only when explicitly tagged. The ABI normalizes them to canonical `[w0..w5]` and downstream arithmetic never reasons about endianness. `native`, `auto`, untagged mixed layouts, future versions, reserved bits, malformed lengths, and noncanonical descriptors fail closed.

B4 machinery is reused as the witness architecture: reversible codec law, explicit finite relation/matrix, digest-bound projection certificate, collision/negative witnesses, and strict authority scope. It is not used to claim braid mathematics for merge/tailgain.

## Security plugin remediation

Security support is a drop-in process ABI, not linked into the daemon or arithmetic ISA:

```text
base compute                    plugin optional
secure website TLS requested    tls.server.http1.reverse-proxy required
secure stamp requested          stamp.ed25519.sha256 required
stamp verification requested    verify.ed25519.sha256 required
```

The reference Go implementation is standard-library only and intended for `CGO_ENABLED=0` static target-specific builds. It can terminate TLS in front of the local service and sign/verify domain-separated SHA-256 receipt digests with Ed25519. Plugin, container image, certificate, stamp key, backend source, and semantic profile have separate identities/digests. Secure-operation failure blocks; it never silently downgrades to plaintext or unsigned output.

## Remaining narrow obstruction

The principal remaining production obstruction is **deployment evidence and topology**, not representation design:

```text
secure/public client
    -> optional TLS security plugin
    -> target-side lease gate
    -> loopback-only blitter daemon
```

Until the inner daemon is actually deployed loopback-only behind the lease gate and a real integration receipt proves clients cannot bypass it, `BLITTER-07` remains `HYPOTHESIS`. Likewise, no Harbor, live-daemon, GPU, or production deployment result is promoted merely because source/workflow support now exists.
