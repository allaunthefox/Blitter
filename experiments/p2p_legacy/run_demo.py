"""Demo: gossip mesh, QoS-tiered routing, lease at nearest peer, compute passthrough."""
from __future__ import annotations

import json
import sys
import time

sys.path.insert(0, "/tmp/opencode/p2p-blitter")

from p2p.designation import Designation, Load, Proximity
from p2p.gateway import Gateway
from p2p.routing import WorkSpec


def demo():
    print("=== P2P Blitter Fabric Demo ===\n")

    gw = Gateway(node_id="router")

    peers = [
        Designation(
            node_id="qfox-1|persistent|x86_64|cpu=8|mem=20GiB|boot=abcd",
            capabilities=frozenset({"webgpu-blitter", "exact-i32", "laurent-product-v1"}),
            controlled_subsystems=frozenset({"accelerator-api"}),
            execution_surfaces=frozenset({"webgpu"}),
            accessibility_profile="sandbox",
            load=Load(active_compute=1, queued_compute=0, max_concurrent_compute=1),
            proximity=Proximity(rtt_ms=2, hop_count=1),
            availability_expires_unix_ms=int(time.time_ns() / 1_000_000) + 3500,
            sent_unix_ms=int(time.time_ns() / 1_000_000),
        ),
        Designation(
            node_id="cupfox|persistent|x86_64|cpu=2|mem=3GiB|boot=wxyz",
            capabilities=frozenset({"webgpu-blitter", "exact-i32"}),
            controlled_subsystems=frozenset({"accelerator-api"}),
            execution_surfaces=frozenset({"webgpu"}),
            accessibility_profile="sandbox",
            load=Load(active_compute=0, queued_compute=0, max_concurrent_compute=1),
            proximity=Proximity(rtt_ms=15, hop_count=2),
            availability_expires_unix_ms=int(time.time_ns() / 1_000_000) + 3500,
            sent_unix_ms=int(time.time_ns() / 1_000_000),
        ),
        Designation(
            node_id="nasfox|persistent|x86_64|cpu=8|mem=32GiB|boot=efgh",
            capabilities=frozenset({"webgpu-blitter", "exact-i32", "laurent-product-v1"}),
            controlled_subsystems=frozenset({"accelerator-api"}),
            execution_surfaces=frozenset({"webgpu"}),
            accessibility_profile="sandbox",
            load=Load(active_compute=0, queued_compute=0, max_concurrent_compute=1),
            proximity=Proximity(rtt_ms=8, hop_count=1),
            availability_expires_unix_ms=int(time.time_ns() / 1_000_000) + 3500,
            sent_unix_ms=int(time.time_ns() / 1_000_000),
        ),
    ]
    for p in peers:
        gw.add_peer(p)

    print(f"Mesh has {len(gw.mesh.peers)} peers:\n")
    for nid, entry in gw.mesh.peers.items():
        d = entry.designation
        print(f"  {d.node_id}")
        print(f"    load: {d.load.active_compute}/{d.load.max_concurrent_compute}, "
              f"rtt: {d.proximity.rtt_ms}ms, "
              f"caps: {sorted(d.capabilities)}")
    print()

    verify_work = WorkSpec(
        goal_id="moore-3250", work_id="verify-0001",
        requires=frozenset({"webgpu-blitter", "exact-i32"}),
        qos_tier="latency_sensitive",
    )
    route = gw.query(verify_work)
    print(f"[latency_sensitive] verify work -> {route[0].node_id if route else 'NONE'}")
    if route:
        c = route[0]
        print(f"  score: {c.score:.1f}, load: {c.load_active}/{c.load_queued}, rtt: {c.proximity_rtt_ms}ms")
    print()

    bulk_work = WorkSpec(
        goal_id="moore-3250", work_id="bulk-0001",
        requires=frozenset({"webgpu-blitter", "exact-i32"}),
        preference_weights={"laurent-product-v1": 100},
        qos_tier="throughput_sensitive",
    )
    route = gw.query(bulk_work)
    print(f"[throughput_sensitive] bulk compile -> {route[0].node_id if route else 'NONE'}")
    if route:
        c = route[0]
        print(f"  score: {c.score:.1f}, load: {c.load_active}/{c.load_queued}, rtt: {c.proximity_rtt_ms}ms")
    print()

    print("--- Lease at nearest peer ---")
    result = gw.compute({}, holder="worker-a", ttl_ms=30000)
    print(f"Lease acquired: epoch={result['lease_epoch']}, holder={result['holder']}")
    print(f"Token present: {'lease_token' in result}")
    print()

    print("--- Compute passthrough ---")
    pt = gw.passthrough(
        job={"a": [[0, 0, 1]], "b": [[0, 0, 1]]},
        path=["router", "cupfox", "qfox-1"],
        ttl=10,
        lease=result,
        query_id="pt-0001",
    )
    print(f"Passthrough: ok={pt['ok']}, path={pt['path']}, "
          f"executor={pt['executor_node_id']}, query_id={pt['query_id']}")
    print()

    status = gw.get_status()
    print(f"Gateway status: {json.dumps(status, indent=2)}")

    gw.shutdown()
    print("\n=== Demo complete ===")


if __name__ == "__main__":
    demo()
