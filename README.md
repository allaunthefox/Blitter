# Blitter

Blitter is experimental execution machinery for MathPunch-compatible workloads.
It is not the MathPunch mathematical authority.

## Repository Boundary

This repository contains:

- backend adapters and WebGPU execution experiments;
- artifact transport, beacon, capability, and lease prototypes;
- checkpoint/recovery and placement experiments;
- hardware and protocol conformance tests;
- deployment-facing specifications for the execution fabric.

The canonical mathematical definitions, proofs, theorem artifacts, and
independent mathematical authority remain in `MathPunch-FiniteState`.
NixOS, Tailscale, Garage, Forgejo, and host deployment remain in `nixos-fleet`.

## Status

Most contents are experimental or design-stage. In particular, the archived
P2P mesh is not the active deployment model. The current direction is an
Internet-layer transport carrying artifact-addressed execution requests to
authenticated beacons, with local capacity gating and independent verification.

## Layout

```text
experiments/webgpu_blitter/   Rust/WebGPU daemon and lease experiments
experiments/p2p_legacy/       archived application-level P2P prototype
scripts/cluster/              bounded dispatch and presence experiments
docs/specs/                   execution-fabric contracts
docs/archive/                 superseded P2P/beacon design lineage
```

Run the local cluster protocol checks with:

```bash
python3 scripts/cluster/test_blitter_heartbeat.py
python3 scripts/cluster/test_slot_scheduler.py
python3 scripts/cluster/test_lease_protocol.py
```
