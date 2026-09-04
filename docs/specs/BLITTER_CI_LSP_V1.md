# Blitter CI and LSP Contract V1

**Status:** CONTRACT WORKFLOW (`.forgejo/workflows/contract.yml`)

`.forgejo/workflows/contract.yml` is the credential-free CI lane for repository
contracts. It runs on pushes, pull requests, and manual dispatch.

## Covered lanes

- Cluster protocol regression tests use only local Python standard-library code.
- Python files under `scripts/` and `experiments/` are compiled in memory, so CI
  does not modify the worktree with bytecode files.
- Shell scripts are checked with `bash -n`, including the worker build script.
- A grep gate refuses the `insecureAcceptAnything` container signature policy
  anywhere under `scripts/`, `experiments/`, or `MathPunchFiniteState/`.
  Podman-based runtimes require the deployment-owned signature policy
  (`BLITTER_PODMAN_SIGNATURE_POLICY`).
- LSP descriptor/dispatch/admission/transport tests validate command rendering
  and fail-closed behavior without starting Lean, contacting SSH, or pulling an
  image.
- Worker image tests statically gate the six language Containerfiles (digest
  pinning, non-root user, HEALTHCHECK, canonical CMD) and the presence
  announcer's generation identity.
- Image provenance, catalog, and trust tests validate canonical digest-bound
  records, Ed25519 catalog signatures, provenance allowlists, and SBOM
  bindings locally.
- The example catalog is verified against the example deployment-owned policy,
  and the `--require-published` negative checks prove bootstrap digests cannot
  pass as production (both for catalogs and for worker publication).
- The target-side LSP lease service job runs atomic-ownership, fencing,
  restart-invalidation, and concurrent-acquire race tests, plus a loopback
  HTTP smoke check of `/health` and `/lsp/lease/status` (which must never
  expose the bearer token).
- The lease service refuses to start without `--auth-token` (or
  `BLITTER_LSP_LEASE_TOKEN`); a missing token is a test failure. The token
  is required on all authority-bearing endpoints and compared with
  `hmac.compare_digest`.
- The lease service worker cgroup termination path is exercised: setting
  `--worker-cgroup` to a test cgroup and triggering a revoke or expiry
  causes the cgroup to be signalled (a cgroup-v2 `cgroup.kill` write is
  attempted with a per-process fallback). The lease service on graceful
  shutdown (`atexit`) also runs the termination path.
- The dispatch path requires `--catalog` and `--catalog-policy`; a missing
  catalog or policy is a test failure. The signed catalog is verified
  against `approved_keys` and the policy's `approved_images` allowlist. A
  bare digest-pinned image without a catalog is **never** accepted.
- The dispatch path enforces `DOCKER_CONTENT_TRUST=1` for the docker
  runtime; a missing env var is a test failure. The local image is
  not cryptographically authorized without it.
- The workspace hardening layer is exercised: `pin_workspace` captures
  the resolved `(device, inode)` and re-verifies before mount. A symlink
  swap between validation and mount is a test failure (`WorkspaceError`).
- The transport layer wires the lease hooks: `begin` after SSH start,
  `commit` before release. A stale worker that has been revoked triggers
  `commit` to raise, and the transport propagates `TransportError` to the
  caller.
- The `lean` job builds the root `MathPunchFiniteState` lake project
  (`lake build` under `leanprover/lean4:v4.31.0`).
- The `isa-profile` job runs the frozen mechanical profile gate. The gate
  passing is necessary but not sufficient: the profile remains
  `PROVISIONAL_PENDING_REVIEW` until the semantic review in
  `BLITTER_ISA_REVIEW_V1.md` is completed and signed.
- The six language Containerfiles have pinned apt package versions,
  checksummed binary installers (elan, opam), and locked opam switch /
  pip / R / Julia / Rust toolchain versions. Build-time dependency drift
  is a test failure (the bash heredoc content is checked in
  `test_worker_images.py`).
- `scripts/cluster/nodes.json` is checked as JSON syntax only; it is not probed.

The LSP tests intentionally use synthetic digest-pinned descriptors and do not
require a Lean installation, a registry, a container runtime, or GPU hardware.

## Separate deployment lane

The following are not part of the contract workflow and remain deployment-
dependent operator checks:

- Harbor publication, registry digest inspection, and immutable image pulls;
- SSH inventory, remote dispatch, and live daemon health/status checks;
- GPU/WebGPU execution and hardware adapter validation;
- Lean or any other language-server toolchain execution beyond the lake build;
- production deployment, lease-gate topology, and rollout/rollback checks.

Those checks require explicit infrastructure, credentials, and approval. Their
absence from this workflow is deliberate: a passing contract run must not claim
that an image is published, a node is reachable, a GPU is available, or a
deployment is safe.

## Verification interpretation

Workflow existence is not execution evidence. Reports should record the exact
Git revision and the Forgejo run URL or run identifier. A successful contract
run establishes local source-contract evidence only; it does not promote
deployment or mathematical authority.
