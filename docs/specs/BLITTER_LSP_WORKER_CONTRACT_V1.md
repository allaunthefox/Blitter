# Blitter LSP Worker Contract v1

**Status:** IMPLEMENTED CONTRACT; BOOTSTRAP WORKER IMAGES DEFINED, PRODUCTION PUBLICATION PENDING

Worker launch hardening is implemented in `scripts/cluster/workspace_execution.py`
(canonicalized live paths, symlink-escape rejection, per-job source/build/cache/output
mounts, non-root read-only launch, quotas, no devices, `--pull=never`, and failure
cleanup) and rendered by `lsp_workers.runtime_command`. Bootstrap images for all
six languages live in `scripts/cluster/workers/` with HEALTHCHECK probes,
`announce_presence.py` presence records carrying worker-generation identity, and
a `build_workers.sh` publication path with provenance/SBOM attestations.
Production publication (reviewed bases, Harbor digests, signed catalog) remains
an explicit deployment step gated by `--require-published`.

This contract defines the execution metadata for language-server workers used
by MathPunch. Blitter transports and runs the worker; MathPunch owns all
semantic interpretation and proof promotion.

## Worker descriptor

Every descriptor has schema `mathpunch.blitter-lsp-worker.v1` and declares:

- exactly one language: `lean`, `coq`, `python`, `r`, `julia`, or `rust`;
- a container image pinned by a full `sha256` digest;
- the canonical argv command for its language, never a shell command string;
- a host workspace beneath `/srv/mathpunch/workspaces` and an absolute container
  mount path;
- positive CPU and memory limits; and
- `network: false`.

Missing, unknown, unpinned, malformed, noncanonical, or network-enabled
descriptors fail closed before container execution. Unknown descriptor fields
are rejected rather than ignored.

## Runtime envelope

The worker runs in a disposable container with `--rm`, a fixed non-root UID,
read-only root filesystem, dropped capabilities, `no-new-privileges`, bounded
CPU/memory/PIDs/file descriptors, bounded scratch tmpfs, `--network=none`, and
explicitly scoped source/build/cache/output workspace mounts. The
currently supported runtime adapters are Docker, Nix/Podman, and
Podman/NVIDIA-CDI. NVIDIA access is not required by the initial six language
surfaces and is not implicitly enabled.

## Authority and lifecycle

The worker provides diagnostics, completion, and language tooling only. Its
responses are untrusted evidence and cannot promote a theorem or alter
MathPunch state. Presence and lease checks remain separate Blitter concerns;
the descriptor parser does not grant ownership or claim that an image is
deployed.

The canonical language commands/toolchains and image digests must be supplied
by deployment configuration and verified by CI before a worker is announced as
available. The explicit `dispatch.py lsp --interactive` path uses the
streaming SSH bridge for raw LSP stdin/stdout forwarding. It still does not
provide image-registry trust or target-side lease authority; transport success
is not proof or exclusive ownership.

## Trust gates (required)

The dispatch path enforces three independent trust gates; failure on any one
fails closed:

1. **Signed catalog**: `dispatch.py lsp --catalog CATALOG --catalog-policy
   POLICY` is required. The catalog must be a `mathpunch.blitter-lsp-image-catalog.v1`
   object with valid Ed25519 signature against `approved_keys` in the policy.
   See `BLITTER_LSP_IMAGE_CATALOG_V1.md`.

2. **Image-trust verification**: the policy must contain `approved_keys`
   (non-empty), `approved_images` (non-empty), and the catalog's signature,
   provenance, and SBOM bindings must verify. A bare digest-pinned image
   without a catalog is **never** accepted by `dispatch.py lsp`.

3. **Runtime signature enforcement**:
   - For the **docker** runtime, `DOCKER_CONTENT_TRUST=1` is required in
     the dispatcher's environment. Without it, the local image is not
     cryptographically authorized and rendering fails closed.
   - For **nix-podman** and **podman-nvidia-cdi** runtimes, the
     deployment-owned `BLITTER_PODMAN_SIGNATURE_POLICY` is required. The
     `--signature-policy` flag is rendered from this file; absence fails
     closed. There is no `insecureAcceptAnything` fallback (CI grep-gates
     its absence).

## Workspace hardening (symlink TOCTOU closure)

`workspace_execution.pin_workspace()` resolves the host workspace path
through `os.path.realpath` and captures the resolved `(device, inode)` pair.
The mounted path is the resolved path, never the raw symlink. Immediately
before mount flag rendering, the inode is re-verified; if the directory
was unlinked, renamed, or replaced between resolution and mount, the
operation fails closed with a `WorkspaceError`. This closes the
validation-to-mount symlink-swap race on the supported runtimes, which
do not accept directory file descriptors.

`setup_job_dirs` and `cleanup_job_dirs` guarantee that per-job `build/`
and `output/` directories are removed on context exit (including job
failure). `source/` and `cache/` are never deleted by the dispatcher.

## Session lifecycle (interactive mode only)

When `--interactive` is set, the dispatcher:

1. Calls `LSPLeaseClient.acquire()` to get a lease from the target-side
   lease service (`scripts/cluster/lsp_lease_server.py`).
2. Starts the SSH tunnel.
3. Calls `LSPLeaseClient.session_begin()` to mark the fenced session in
   flight. This is the **fence point**: another dispatch cannot acquire
   a lease for this worker until this session commits or is aborted.
4. Streams LSP bytes (the transport layer is byte-transparent; LSP
   semantics are the local client's concern).
5. Calls `LSPLeaseClient.session_commit()` before returning. This is the
   **commit fence**: a stale or expired lease causes `commit` to return
   `409 fenced_at_commit`, the transport raises `TransportError`, and the
   caller must drop all buffered diagnostics.
6. Calls `LSPLeaseClient.release()` to free the worker.

See `BLITTER_LSP_LEASE_SERVICE_V1.md` for the lease protocol and
`BLITTER_LSP_TRANSPORT_V1.md` for the transport-layer wiring.
