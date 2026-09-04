# Blitter LSP Worker Contract v1

**Status:** IMPLEMENTED CONTRACT; BOOTSTRAP WORKER IMAGES DEFINED, PRODUCTION PUBLICATION PENDING

Worker launch hardening is implemented in `scripts/cluster/workspace_execution.py`
(canonicalized paths, symlink-escape rejection, per-job source/build/cache/output
mounts, non-root read-only launch, quotas, no devices, `--pull=never`, guaranteed
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
one explicitly declared read-write workspace mount. The
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
