# WebGPU Fabric — network-unified offloadable exact compute

**Status:** EXPERIMENT — verified mechanism, non-authorizing per charter.
**Branch context:** `research/interval-certificate-probes`; the kernels
below are exact-integer and fabric-verified byte-identical.

## The idea

WebGPU is the universal instruction set. Any node with a WebGPU device —
NVIDIA, AMD RADV, Intel iGPU, or *software Vulkan (llvmpipe) on a CPU-only
box* — executes the same exact kernels. The network unifies into
offloadable devices: GPU-only work (and CPU-fabric work) can be dispatched
to any node, including hardware CUDA cannot touch.

The practical consequence: a rented cluster with large CPU counts is
harnessed **instantly** — every node with mesa-vulkan-drivers exposes a
device; the same binaries run unmodified; throughput scales with cores.

## Fabric (live 2026-08-09)

| node | device | role | notes |
|---|---|---|---|
| qfox-1 | NVIDIA RTX 4070 SUPER | GPU | heavy exact merges |
| nasfox | AMD RX 580 (RADV POLARIS10, 8 GB) | GPU | CUDA-incompatible; WebGPU-only |
| nixos-laptop | AMD Radeon iGPU (RADV RENOIR) | iGPU | docker --device /dev/dri |
| cupfox | llvmpipe (LLVM, 2 vCPU) | CPU | software Vulkan in the blitter-daemon container |

Fabric benchmark (20 Laurent jobs): qfox-1 104 ms, iGPU 261 ms, RX 580
1085 ms, CPU 6058 ms — byte-identical reduced terms on all.

## Kernels

### `src/tailgain.wgsl` + `src/bin/tailgain.rs` — exact tail gain

For a job `(N2, P)`:

    G(P, N2) = max weight of a 3-AP-free A ⊆ [65, N2] with P ∪ A 3-AP-free

in L_N2 = lcm(1..N2) units, 6×u32 words (192-bit exact; covers L_N2 up to
~2^190). One thread per candidate mask; blocked singles, reflection pairs,
internal triples validated in-shader; carry-chain word addition.

Run:

    cargo build --release --bin tailgain
    ./target/release/tailgain --jobs jobs.json      # BLITTER_ADAPTER=<name> selects the device

### `src/merge.wgsl` + `src/bin/merge.rs` — sharded exact merge

The heavy interval-certificate merges (54,026,402-prefix space). Each
thread owns one prefix and scans the score-descending suffix list for the
first compatible suffix; totals in 6×u32 words. Modes:

    merge --detect            # hardware / NUMA topology report (JSON)
    merge --merge job.json    # one prefix-slice -> exact lexicographic max

The 42-bit prefix coordinate space is handled via lo/hi u32 pairs (the
int32 truncation trap documented in the split certifier is avoided by
construction).

### `fabric_merge_dispatch.py` — detection-driven, NUMA-aware partition

    python3 fabric_merge_dispatch.py --detect-all
    python3 fabric_merge_dispatch.py --prepare 64
    python3 fabric_merge_dispatch.py --plan 64
    python3 fabric_merge_dispatch.py --dispatch 64

- slices sized by **per-NUMA-node memory** (36 bytes/prefix state) and
  weighted by NUMA-local cores;
- workers bound `numactl --physcpubind/--membind` on multi-node
  topologies;
- aggregate = lexicographic max over worker maxima — exact, zero
  cross-node coupling.

## Verification (2026-08-09)

Tail gain, N=64 threat set over the [65,72] tail:

| device | G(P1,72) | G(P2,72) |
|---|---|---|
| RTX 4070 SUPER (NVIDIA Vulkan) | 1/72 | 1/70 |
| RX 580 (RADV POLARIS10) | 1/72 | 1/70 |
| llvmpipe (LLVM 22.1.8) | 1/72 | 1/70 |
| PyTorch/CUDA reference | 1/72 | 1/70 |

Sharded merge, N=64 slice containing the certified optimum:

| device | max_words | fraction |
|---|---|---|
| RTX 4070 SUPER | [3623462120,3377294491,161959329,0,0,0] | 364569583747/144268083960 |
| RX 580 (RADV) | byte-identical | same |
| llvmpipe | byte-identical | same |

The slice max equals the certified M3(64); the winner's prefix mask is
identified correctly on every device family.

## Deployment notes

- The nodes run the `webgpu-blitter` daemon on port 8790 (`/compute`,
  Laurent jobs). The kernels above are standalone binaries for the same
  fabric.
- **musl builds cannot dlopen glibc's libvulkan** — use the glibc build;
  on CPU nodes run it inside the `blitter-daemon` container (the host
  context lacks the Vulkan ICD environment).
- New nodes: install mesa-vulkan-drivers (or use an image with them),
  copy the binaries, add a `nodes.json` entry — done.
- `BLITTER_ADAPTER=<name-substring>` selects the device.

## Scaling property

The exact kernels are embarrassingly shardable by prefix-slice with zero
cross-node coupling. A rented CPU cluster (llvmpipe) of K nodes × C cores
provides K·C parallel workers for the same binaries. The interval
certificates' heavy merges — e.g. the N=70/72/82 crossings — become a
fan-out job the moment the cluster exists.

## Prior work

- `src/blitter.wgsl` + `src/main.rs`: the full Laurent product pipeline
  (product + sort + reduce + compact on-device, Rgba32Uint blitter
  surfaces) — the original proof of the CPU-node-as-virtual-GPU mechanism.
- `scripts/cluster/dispatch.py`: fail-closed Tailscale inventory and
  container dispatcher with `webgpu` batch mode.
