# Fabric benchmark — 2026-08-09 (20 identical 7x4 = 28-product Laurent jobs)

| node | device | total | per job | adapter |
|---|---|---|---|---|
| qfox-1 | RTX 4070 SUPER | 104 ms | 5.2 ms | NVIDIA Vulkan |
| nixos-laptop | AMD Radeon iGPU (5700U) | 261 ms | 13.1 ms | RADV RENOIR |
| nasfox | AMD RX 580 (Polaris) | 1,085 ms | 54.3 ms | RADV POLARIS10 |
| cupfox | CPU-only (2 vCPU) | 6,058 ms | 302.9 ms | llvmpipe |

All nodes return byte-identical reduced terms (verified). The RX 580 and the
laptop iGPU are CUDA-incompatible hardware that only the WebGPU fabric can
harness; the CPU node absorbs the same jobs. Per-job cost is dominated by
HTTP + wgpu pipeline setup at this size; real FAMM batches would be tiled
larger per job (pipeline cap: 8x8 = 64 products; batch mode fans out).
