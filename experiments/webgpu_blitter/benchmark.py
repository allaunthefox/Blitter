#!/usr/bin/env python3
"""Fabric benchmark: N identical 7x4 Laurent jobs across the WebGPU nodes."""
import json, subprocess, sys, time, os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DISPATCH = os.path.join(ROOT, "scripts", "cluster", "dispatch.py")
NODES = ["qfox-1", "nasfox", "nixos-laptop", "cupfox"]

def main(n_jobs: int = 20) -> None:
    jobs = []
    for _ in range(n_jobs):
        jobs.append({
            "a": [[0,0,1],[1,0,-1],[0,1,2],[1,1,3],[2,0,-4],[0,2,5],[1,2,-6]],
            "b": [[0,0,1],[1,0,1],[0,1,-1],[2,1,2]],
        })
    batch = os.path.join("/tmp", "bench_batch.json")
    open(batch, "w").write(json.dumps({"jobs": jobs}))
    for node in NODES:
        t0 = time.time()
        r = subprocess.run(["python3", DISPATCH, "webgpu", node, "--batch-file", batch, "--timeout", "60"],
                           capture_output=True, text=True, timeout=300)
        dt = time.time() - t0
        ok = "jobs ok" in r.stdout
        print(f"{node:14s} {dt*1000:8.0f} ms  {'OK' if ok else 'FAIL'}")
        if not ok:
            print(r.stdout[-200:] or r.stderr[-200:])

if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 20)
