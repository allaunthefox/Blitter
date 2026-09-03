# Intermediate Representation (IR) & MCP Selector Architecture

> **Deployed topology:** Garage is deployed on `quadfox`, `cupfox`, and
> `forgefox`. References in this roadmap to a NASFox Garage cluster are
> historical storage naming, not a claim that Garage runs only on NASFox.

> **Status: roadmap.** The selector, execution backends, telemetry, key
> lifecycle, and receipt verification described here are not implemented in
> this repository. The blitter options and daemon are declared by
> `roles/peer.nix`, but the role is disabled on the enabled hosts.

This document describes the design and operation of the **Intermediate Representation (IR)** and the **MCP Selector** within the multi-runtime computation pipeline.

---

## 1. Architectural Overview

The execution pipeline translates high-level mathematical operations (Laurent polynomial reductions, finite-state automaton transitions, FHE instruction streams) into a formal **Intermediate Representation (IR)**.

The **MCP Selector** evaluates real-time cluster telemetry, hardware availability, and security constraints to route IR payloads to the optimal execution target.

```
┌─────────────────────────────────────────────────────────────┐
│                 High-Level Math / Contract                  │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│             Canonical Intermediate Representation           │
│  - Bytecode Program / Polynomial Triples                     │
│  - Initial Register & Predicate State (State)               │
│  - Invariant Verification Vector (TerminalContract)         │
│  - Memory Dependency Graph (Sparse Capsules & PageStore)    │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                     MCP Selector                            │
│  Evaluates: Hardware Telemetry, Execution Backend Caps,    │
│             Occupancy, and Cryptographic Requirements       │
└──────┬──────────────────────┬──────────────────────┬────────┘
       │                      │                      │
       ▼                      ▼                      ▼
┌──────────────┐       ┌──────────────┐       ┌──────────────┐
│Blitter WebGPU│       │  Sealed FHE  │       │  CPU / C     │
│(ROCm/Vulkan) │       │   Runtime    │       │  Fallback    │
└──────┬───────┘       └──────┬───────┘       └──────┬───────┘
       │                      │                      │
       └──────────────────────┼──────────────────────┘
                              │
                              ▼
               Canonical Receipt Verification
```

---

## 2. The Intermediate Representation (IR)

The IR is a declarative, deterministic specification of a computation. It guarantees reproducible execution across heterogeneous hardware backends.

### Key IR Structures

1. **State Vector (`State`)**
   - `regs`: Fixed-width register tuple (`[r0, r1, r2, r3]`)
   - `pred`: Single-bit predicate boolean flag (`0` or `1`)
   - `out` / `out_len`: Output buffer array and length
   - `pc`: Program counter offset

2. **Terminal Contract (`TerminalContract`)**
   - `expected_out`: Reference invariant output vector
   - `expected_out_len`: Expected output byte count
   - `expected_valid`: Asserted valid state flag
   - **Role**: Allows backends to fail closed instantly if execution violates the mathematical contract.

3. **Sparse Capsule Dependency Graph (`PageStore` & `Capsules`)**
   - Memory is segmented into immutable content-addressed pages (`PageStore`).
   - `Capsules` declare memory offsets, lengths, and explicit dependency edges (`dependencies = ("CAPSULE_A",)`), allowing out-of-order parallel scheduling across compute nodes.

---

## 3. The MCP Selector

The **MCP Selector** acts as the dynamic scheduler and router. It inspects incoming IR contracts alongside live node status (gathered via REST `/status` and UDP heartbeats) to determine the execution route.

### Selection Criteria & Routing Logic

| Route Target | Criteria / Requirements | Execution Mechanism |
|---|---|---|
| **Blitter GPU** | Hardware GPU available (`ROCm`‑`RADV`), batch operations, high throughput | Submitted via POST `/compute` to `blitter-daemon` on port 8790 (declared in `roles/peer.nix`, with UDP presence heartbeats on 8791; disabled on the enabled hosts) |
| **Sealed FHE** | Confidential compute required, cryptographic proof of execution | Sealed binary execution payload (`seal_execution_input`), verified via sha256 receipt |
| **Sparse Capsule** | Graph-dependent memory workloads with out-of-order issue requirements | Dispatched via `sparse_runtime` worker pools with page store backing |
| **CPU Fallback** | GPU unavailable or busy (`active_compute > max_concurrent`), small batch | Executed locally via C runtime, `lavapipe` (software Vulkan), or SwiftShader |
| **Mobile ARM (phone)** | ARM64 mobile node on the tailnet; graph/CPU workloads or offload when desktop GPUs are busy | `Sparse Capsule` / `CPU Fallback` (ARM-tuned) dispatched to the phone; Mali Vulkan GPU is a candidate mobile WebGPU path |

**Mobile computation platform.** The fleet's phone (`oneplus-nord-n300-5g`, MediaTek Dimensity 810 ARM64; Nix-on-Droid flake, Python MCP SDK 2.0.0, `distcc`, ARM Mali Vulkan) is configured as a mobile computation node. It extends the Selector's reach with an ARM64 backend (`Sparse Capsule` / CPU-tier, ARM-vector-tuned), joins the `distcc` build pool alongside the Zen 5 boxes, and can act as an MCP endpoint via the 2.0.0 SDK — which dovetails with the Tier 1 DevContainer Connector (§5.9): a bounded external actor could be delegated to it. Trust treatment is Tier 2 (§5.9): as a *mobile* tailnet member its non-expiring `mcp` key is auto-provisioned on join and auto-revoked on network departure.

### Verification & Dual-Execution (`compare_backends`)
To ensure total exact authority, the selector can execute an IR payload concurrently across multiple backends (e.g. Clear C backend vs. Sealed FHE backend) and assert equality of the resulting `semantic_receipt_sha256`. If the receipts disagree, the system fails closed.

---

## 4. Storage Integration

All IR execution artifacts, sealed binary payloads, and verification receipts are persisted to the canonical Garage S3 fleet storage:
- Execution inputs / outputs → `blitter` bucket
- Build artifacts & Nix closures → `artifacts` bucket
- Telemetry & volume state → `container-volumes` bucket

The canonical bucket set is managed on the **fleet Garage S3 service** (deployed on `quadfox`, `cupfox`, and `forgefox`) (12 scoped buckets,
`replication_factor = 1`); the IR-relevant buckets above are a subset. The Remote DevContainer
Connector (`docs/mcp-boot-process.md` §4) also writes build outputs → `artifacts` and logs →
`container-volumes`.

---

## 5. Trust & Key Management

This section defines how the MCP Selector assigns and protects the `mcp` keys that
authorize compute actors, given that an actor enters with an *unknown trust level*.

### 5.1 The `mcp` key — bounded, ephemeral, per-actor
Rather than issue durable credentials to an unverified actor, the Selector assigns an
`mcp` key: a **temporary signing key** whose authority is scoped and expires.
Analogized as a *house key with a date assignment* — the lease length encodes the
actor's trust tier:

| TTL | Trust tier |
|---|---|
| 1h / 1d | unknown / unverified actor |
| 30d / 90d | graduated trust |
| non-expiring | standing / high-trust |

**Revocation supersedes expiry** — a lease can be torn up before its date.

### 5.2 Leveraging the existing SSH-key substrate
The fleet's SSH-key design is reused as the trust root — no separate KMS is introduced:
- **Host `ssh-ed25519` = age identity**; **cupfox = recovery key**; age provides
  encryption at rest.
- The `mcp` key is delivered encrypted to the host's `ssh-ed25519` recipient (+ cupfox),
  identically to the vaultwarden-puller's known-good cache. It is decrypted by the host's
  SSH private key.
- **Expiry maps onto SSH certificate validity windows** (`-V +1h/+1d/…+90d/always`).
  This makes enforcement **host-aware**: the host validates the cert's validity window
  locally (as `sshd` rejects an expired cert) and fails closed without needing Selector
  reachability. cupfox holds the recovery copy.

### 5.3 Post-quantum hardening (first)
The SSH substrate is upgraded to post-quantum **before** it becomes the `mcp` trust root.
OpenSSH **10.5p1** is available in the pinned nixpkgs.
- **Recommended first step (scope A):** enable the hybrid KEX `mlkem768x25519-sha256`.
  Transport is post-quantum; host keys stay `ed25519`, so the age-to-`ssh`-recipient
  delivery (§5.2) is unaffected.
- **Scope B (deferred):** PQ host keys/certs (`mlkem768`). Future-proofs the credential
  itself but requires verifying `age`'s `ssh`-recipient support for `mlkem768` (an
  `ed25519` key may need to be retained alongside for the age transport).

### 5.4 Key-generation entropy — hardware-rooted salt
The salt for `mcp` key derivation is drawn from the **generating system's own physical
noise** (decentralized, non-replicable). Three independent hardware sources are fused:
- **RTC clock** jitter
- **Thermal probes** (`/sys/class/thermal/thermal_zone*/temp`)
- **JEDEC/SPD RAM-timer** readings (DRAM timing + thermal)

The readings are concatenated and hashed (BLAKE2b/SHA-256) into a uniform salt — *not*
averaged — to preserve entropy. This seeds the `mcp` key derivation.

### 5.5 Signing — vaultwarden-generated SSH + GPG
vaultwarden natively generates **both** the SSH material (for identity and expiry via
the cert `-V` window, §5.2) and a **GPG key** used to sign IR artifacts and receipts
(`semantic_receipt_sha256`, sealed payloads). This means **no custom key-generation
layer is required**: the `mcp` key is produced by vaultwarden's built-in SSH/GPG
generation, seeded by the hardware-rooted salt (§5.4, harvested from the *issuer's*
own RTC / thermal / SPD noise). The host receives the generated SSH cert and GPG
private key via the puller (§5.2 delivery) and signs locally with GPG.

### 5.6 Key lifecycle — cycle adopted from Vault / SPIRE
The `mcp` key follows the canonical short-lived-credential lifecycle used by auth systems
built around this exact problem (HashiCorp Vault PKI lease/revocation; SPIFFE/SPIRE SVID
rotation). The cycle below was empirically pulled from a Vault dev instance:

> **issue** (bounded TTL baked into the cert) → **live** (listed in the authority) →
> **rotate-before-expiry** (reissue while the old key stays valid until its `not_after`) →
> **revoke** (tear-up-before-date) → **revocation live** (present in the CRL).

Stages mapped to the `mcp` key:

1. **Issue** — vaultwarden generates the SSH cert + GPG key with a bounded TTL (§5.1
   tiers); the window is baked into the SSH cert `-V` and the GPG key validity.
2. **Live** — delivered to the host via the puller (§5.2); honored while inside its
   validity window.
3. **Rotate-before-expiry** — reissue a fresh key *before* the old lapses (SPIRE rotates
   at 50% TTL automatically); the old key stays valid through the overlap, so there is no
   gap in authority.
4. **Revoke** — on demand (compromise, task end, trust withdrawn); revocation supersedes
   expiry.
5. **Revocation live** — vaultwarden marks the key revoked and the host SSH-cert `-V`
   window enforces it locally (host-aware fail-closed, §5.2); cupfox holds the recovery
   copy.

**OIDC signing-key note.** If an `mcp` key is ever used as an IdP signing key, it must
follow the JWKS-overlap rotation (publish new + old keys during handover) and use a
long-TTL / standing tier — a 1h ephemeral key would invalidate already-issued tokens
hourly. Recommended: keep the `mcp` key as a *client credential* to authentik; authentik
retains its own stable signing key (standard OIDC token lifecycle).

### 5.7 Authentication — Tailscale network membership
Tailscale network membership is the authentication primitive that resolves the
"unknown trust" case (§5.1). **If an actor is authenticated on the Tailscale network, a
non-expiring `mcp` key is auto-generated for it and remains valid for as long as it is
part of the network.** Leaving or being removed from the network revokes the key
(revocation supersedes expiry, §5.6).

- Tailscale membership maps to the **standing / high-trust (non-expiring)** tier of §5.1.
  This is the concrete signal that grants the long-lived key; actors *not* on the tailnet
  fall back to the bounded ephemeral tiers (1h / 1d / 30d / 90d).
- **Auto-provisioning** is performed by vaultwarden (§5.5): on tailnet join, generate the
  SSH cert + GPG key with a non-expiring window; on departure, revoke (§5.6 stage 4).
- **Control-plane glue — the Tailscale service account key.** The promise "valid as long as
  you are part of the network" is enforced by a provisioning loop authenticated to the
  Tailscale API with a **service account key** (scopes: `devices:read` to detect join/leave).
  The loop polls tailnet membership (or consumes device webhooks) and instructs vaultwarden
  to issue the `mcp` key on join and revoke it on leave/removal (§5.6 stage 4). The service
  account key itself is stored in vaultwarden — closing the loop: vaultwarden holds the
  Tailscale credential that authorizes provisioning `mcp` keys for tailnet members. Without
  this hook, "non-expiring" would only lift via manual revocation.
- The SSH cert `-V` window for this tier is `always`; the key still rotates for hygiene
  (§5.6 stage 3, e.g. post-quantum upgrade), keeping overlap so validity stays continuous
  while membership holds.
- Composes with OIDC (§5.6): Tailscale can act as an identity source for authentik, so a
  tailnet member is both auto-provisioned an `mcp` key and able to present it as an OIDC
  client credential.

- **Threat model.** Tailscale network membership is the primary trust boundary; the `mcp` key is a capability granted *within* that boundary, not an independent security perimeter. If an actor is already authenticated on the tailnet, the breach is at the network layer — far beyond `mcp` assignment — so defending assignment beyond reasonable bounds is not where effort belongs. The key therefore stays "good" (bounded, revocable, standard) rather than maximally hardened, and the deferred FSM key-position hiding (§5.8) is unnecessary under this model.

### 5.8 Deferred: key-position obfuscation via finite state machine
A considered future enhancement is to hide the `mcp` key's storage **position** within
the IR's Sparse Capsule / PageStore graph using an existing finite state machine, so the
key is not at a fixed address. This is **deferred** until it can be *provably recovered
across more than one session* — the obfuscation must not outrun the ability to recover
the key (e.g. via cupfox's recovery key) before it is adopted.

### 5.9 Two-tier key cycle
The trust model resolves into **two distinct key cycles**, selected by whether the actor is
authenticated on the Tailscale network:

**Tier 1 — unknown trust (time-bounded).** For actors *not* on the tailnet. vaultwarden
issues an ephemeral `mcp` key with a bounded TTL (1h / 1d / 30d / 90d, §5.1). It follows
the Vault/SPIRE lifecycle (§5.6): issue → live → rotate-before-expiry (overlap kept) →
revoke. Revocation is time- or event-driven (compromise, task end); expiry is the backstop.

**Tier 2 — Tailscale member (membership-bounded).** For actors authenticated on the
tailnet. vaultwarden **auto-provisions a non-expiring `mcp` key** on join (§5.7) and
revokes it on network departure. The lifecycle is driven by **membership state**, not a
clock: join → generate, leave/removed → revoke — enforced by the service-account
provisioning loop (§5.7). The trust anchor is Tailscale membership itself (threat model,
§5.7): while that boundary holds the key is valid; if it breaks, `mcp` assignment is moot.

**Motivating example (Tier 1).** Suppose you are conversing with an LLM and want it to interact with a software package that cannot be provisioned inside its own container (e.g. it needs hardware, secrets, or a heavy runtime your fleet provides). Rather than bring that package — or the LLM — onto your tailnet, you grant the LLM **bounded access to the `mcp` network** via a Tier 1 key: a short-lived, revocable credential scoped to the task. The LLM reaches the compute backend to do the work; the lease lapses or is torn up when the task ends. That is precisely the "house key with a date assignment" for an unknown actor — no standing trust, small blast radius. **Concretely**, this is realized by the Remote DevContainer Connector at `mcp.researchstack.info` (see `docs/mcp-boot-process.md`), which grants an external actor bounded `/execute`, `/read_file`, `/write_file`, `/status` access to the `mcp` network.

**Motivating example (Tier 2).** The fleet's phone (`oneplus-nord-n300-5g`) is a *mobile* Tailscale member. Because it is authenticated on the tailnet, vaultwarden auto-provisions a non-expiring `mcp` key for it (§5.7) and it is usable as an ARM64 compute backend (§3). The boundary is membership, not a clock: the moment the phone leaves the tailnet the service-account provisioning loop (§5.7) revokes the key. This is the concrete realization of "membership-bounded" — and it is exactly why a *mobile* node is safe to admit as Tier 2: its credential evaporates the instant it is no longer on the network.

Both tiers share the same substrate: vaultwarden generates the SSH cert + GPG key from the
hardware-rooted salt (§5.4/5.5); the puller delivers it encrypted to the host (§5.2,
cupfox recovery); the host enforces expiry/revocation locally via the SSH cert `-V` window
(host-aware fail-closed); signing uses GPG (§5.5); and the SSH substrate is post-quantum
hardened first (§5.3).

---

## 6. End-to-End Flow

This section consolidates §1–§5 into a single walkthrough of how work moves through the
system — from an untrusted actor to a verified receipt — and shows where the `mcp` key
(§5) gates and signs each step. It exists so the whole pipeline can be recalled without
re-deriving it from the component sections.

### 6.1 System diagram (compute + trust)

```
 High-Level Math / Contract
          │
          ▼
 Canonical IR  (State, TerminalContract, Sparse Capsules / PageStore)        §2
          │
          ▼
 ┌──────────────┐      (unknown trust)      ┌──────────────────────┐
 │ MCP Selector │◄────────── actor ─────────│   untrusted actor    │
 └──────┬───────┘                           └──────────────────────┘
        │ 1. scope work + assign trust tier (TTL)                      §5.1
        ▼
 ┌──────────────┐
 │ vaultwarden  │ 2. generate SSH cert + GPG key from hardware-rooted
 │  (issuer)    │    salt (RTC / thermal / SPD); cupfox = recovery    §5.4 / 5.5 / 5.2
 └──────┬───────┘
        │ 3. puller delivers encrypted to host ssh-ed25519 recipient (+ cupfox)
        ▼
 ┌──────────────┐  decrypts with SSH private key → holds key live
 │   Host(s)    │  (host-aware SSH cert -V enforcement)              §5.2 / 5.6
 │  ├ blitter (GPU)         routes per telemetry + constraints       §3
 │  ├ Sealed FHE
 │  ├ Sparse Capsule
 │  └ CPU fallback
 │   → signs receipts with GPG (mcp key)                             §5.5
 └──────┬───────┘
        │ 4. compare_backends asserts semantic_receipt_sha256 equality;
        │    TerminalContract fails closed on contract violation     §2 / 3
        ▼
 ┌──────────────┐  inputs/outputs → blitter · builds → artifacts ·
 │ Garage S3    │  telemetry     → container-volumes                 §4
 └──────────────┘
 Auth : Tailscale membership => autogen NON-EXPIRING mcp key; leave net => revoke  §5.7
 OIDC : host/actor → authentik using mcp key as CLIENT CREDENTIAL;
        authentik signs tokens with its OWN stable key (JWKS overlap)  §5.6
 PQ   : SSH substrate upgraded to mlkem768x25519-sha256 KEX (scope A)  §5.3
 Cycle: rotate before TTL lapse (overlap kept) · revoke supersedes
        expiry · recover via cupfox                                    §5.6 / 5.2
```

### 6.2 Walkthrough (actor lifecycle)

1. **Arrival / authentication.** An actor presents itself. If it is authenticated on the
   Tailscale network, vaultwarden auto-provisions a **non-expiring** `mcp` key (§5.7);
   if not, it is treated as unknown trust and enters the bounded-tier path (§5.1).
2. **Assign + tier.** For Tailscale members the tier is standing (non-expiring), revoked on
   network departure (§5.7). For unknown actors the Selector scopes the work and assigns a
   bounded TTL (1h / 1d / 30d / 90d, §5.1).
3. **Key generation (issuer).** The Selector requests the key from vaultwarden, which
   generates an SSH cert + GPG key from its hardware-rooted salt (§5.4, §5.5). No custom
   key-generation layer; no key material leaves vaultwarden in plaintext.
4. **Delivery.** The puller fetches the key from vaultwarden and delivers it encrypted to
   the host's `ssh-ed25519` age recipient (+ cupfox recovery, §5.2). The host decrypts it
   with its SSH private key.
5. **Live + host-aware enforcement.** The host holds the key live; the SSH cert `-V`
   window is validated locally, so expiry/revocation is enforced fail-closed without
   Selector reachability (§5.2, §5.6).
6. **Execute.** The Selector routes the IR to a backend (blitter / Sealed FHE / Sparse /
   CPU, §3) based on telemetry, capability, and occupancy.
7. **Sign.** The host signs execution artifacts and receipts (`semantic_receipt_sha256`,
   sealed payloads) with the GPG half of the `mcp` key (§5.5).
8. **Verify.** `compare_backends` executes across backends and asserts receipt equality;
   the `TerminalContract` lets any backend fail closed on contract violation (§2, §3).
9. **Persist.** Inputs/outputs → `blitter`, builds → `artifacts`, telemetry →
   `container-volumes` (§4).
10. **Rotate / revoke.** Before the TTL lapses the key is reissued (overlap kept, §5.6);
    on compromise or task-end it is revoked, which supersedes expiry and is enforced via
    vaultwarden + the cert `-V` window, with cupfox holding the recovery copy (§5.2/5.6).
11. **Auth to services.** To reach protected services, the actor/host presents the `mcp`
    key as an OIDC *client credential* to authentik; authentik issues tokens from its own
    stable signing key (§5.6 OIDC note).

### 6.3 Fail-closed guarantees (recap)

- **Contract violation** → backend fails closed via `TerminalContract` (§2).
- **Receipt mismatch** (dual-execution) → system fails closed via `compare_backends` (§3).
- **Expired / revoked key** → host rejects locally via the SSH cert `-V` window
  (host-aware, §5.2).
- **Lost key** → recoverable via the cupfox recovery copy (§5.2).
- **Unreachable Selector** → host still enforces expiry/revocation locally (§5.6).

> **Deferred:** key-position obfuscation via finite state machine remains deferred (§5.7)
> until it is provably recoverable across more than one session.
