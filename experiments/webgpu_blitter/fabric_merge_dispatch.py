#!/usr/bin/env python3
"""fabric_merge_dispatch.py — detection-driven, NUMA-aware sharded exact merge.

Partitions the 54,026,402-prefix space across the WebGPU fabric:
  - --detect-all   probe every node's hardware / NUMA topology (merge --detect)
  - --prepare N2   build the suffix state (sorted descending) once per N2
  - --plan N2      partition the prefix range into per-(node, NUMA node)
                   slices sized by detected memory and cores
  - --dispatch N2  ship slice state, run the merge on each NUMA node
                   (numactl --cpunodebind/--membind when available),
                   aggregate worker maxima in exact U192 numeric order

Exact aggregation is authorized only after the planner validates an ordered,
disjoint, gap-free cover of the complete prefix range.
"""
import argparse, json, math, os, struct, subprocess, sys, time

from merge_partition import allocate_slices, aggregate_worker_results, validate_partition

ROOT = os.path.dirname(os.path.abspath(__file__))
NODES = {
    "qfox-1":  "100.88.57.96",
    "nasfox":  "100.119.121.83",
    "cupfox":  "100.115.119.40",
}
BIN = "/tmp/merge"          # deployed merge binary path on each node
PREFIX_TOTAL = 54026402
BYTES_PER_PREFIX = 36       # masks(8) + weights(24) + blocked(4)
N2 = None


def ssh(node, cmd, timeout=600):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                        f"root@{NODES[node]}", cmd], capture_output=True, text=True,
                       timeout=timeout)
    return r


def detect_all():
    out = {}
    for node in NODES:
        r = ssh(node, f"{BIN} --detect")
        try:
            out[node] = json.loads(r.stdout)
        except json.JSONDecodeError:
            out[node] = {"error": r.stdout[:200] or r.stderr[:200]}
        print(f"{node}: {json.dumps(out[node])}")
    return out


def words6(x):
    return [(x >> (32 * k)) & 0xFFFFFFFF for k in range(6)]


def write_u32s(path, data):
    with open(path, "wb") as f:
        for v in data:
            f.write(struct.pack("<I", v))


def prepare_suffix(N2_, workdir):
    """Sorted-descending suffix state for [43, N2] + shared prefix state."""
    global N2
    N2 = N2_
    sys.path.insert(0, ROOT)
    sys.path.insert(0, "/tmp/opencode")
    import torch
    from exact_optimum_split_certifier_v34 import (
        frontier_to, suffix_enumeration, fe_mask, blocked_mask, prefix_weights)
    dev = "cuda"
    L = math.lcm(*range(1, N2 + 1))
    WL = {e: L // e for e in range(1, N2 + 1)}
    t0 = time.time()
    cnt, masks = frontier_to(42, dev, report_progress=False)
    mlist = masks.tolist()
    print(f"prefixes {cnt} ({time.time()-t0:.0f}s)")
    Sm, Sw = suffix_enumeration(N2, dev, WL)
    order = torch.argsort(Sm, stable=True)
    for k in (0, 1, 2):
        order = order[torch.argsort(Sw[order, k], stable=True, descending=True)]
    Sm, Sw = Sm[order], Sw[order]
    FE = fe_mask(Sm, dev)
    sm = Sm.tolist(); sw = Sw.tolist(); fe = FE.tolist()
    write_u32s(f"{workdir}/s_smask.bin", sm)
    write_u32s(f"{workdir}/s_sfe_lo.bin", [f & 0xFFFFFFFF for f in fe])
    write_u32s(f"{workdir}/s_sfe_hi.bin", [(f >> 32) & 0xFFFFFFFF for f in fe])
    sw6 = []
    for r in sw:
        x = (int(r[2]) << 64) + (int(r[1]) << 32) + int(r[0])
        sw6.extend(words6(x))
    write_u32s(f"{workdir}/s_sweight.bin", sw6)
    os.makedirs(f"{workdir}/prefix", exist_ok=True)
    write_u32s(f"{workdir}/prefix/p_all_masks.bin", sum(
        [[m & 0xFFFFFFFF, (m >> 32) & 0xFFFFFFFF] for m in mlist], []))
    torch.cuda.empty_cache()
    pw3 = prefix_weights(masks, dev)
    pw6 = []
    for r in pw3.tolist():
        x = (int(r[2]) << 64) + (int(r[1]) << 32) + int(r[0])
        pw6.extend(words6(x))
    write_u32s(f"{workdir}/prefix/p_all_weights.bin", pw6)
    print(f"suffix+prefix state for N2={N2} ready ({time.time()-t0:.0f}s)")


def plan(workdir):
    det = json.load(open(f"{workdir}/detect.json")) if os.path.exists(f"{workdir}/detect.json") else detect_all()
    json.dump(det, open(f"{workdir}/detect.json", "w"), indent=1)
    candidates = []
    for node, d in det.items():
        if "error" in d:
            continue
        usable_mem_b = d.get("mem_total_kb", 0) * 1024 * 0.5
        cores = d.get("cpu_count", 1)
        numa_nodes = d.get("numa_nodes", [{"id": 0, "cpus": cores, "mem_total_kb": d.get("mem_total_kb", 0)}])
        for nd in numa_nodes:
            ncores = max(1, len(nd.get("cpu_list", [])) or 1)
            mem_b = nd.get("mem_total_kb", 0) * 1024 * 0.5 or usable_mem_b / max(1, len(numa_nodes))
            cap = max(1, int(mem_b / BYTES_PER_PREFIX))
            candidates.append({
                "node": node,
                "numa": nd["id"],
                "cpus": nd.get("cpu_list", []),
                "max_prefixes": cap,
                "weight": ncores,
            })

    jobs = allocate_slices(candidates, PREFIX_TOTAL)
    validate_partition(jobs, PREFIX_TOTAL)
    json.dump(jobs, open(f"{workdir}/plan.json", "w"), indent=1)
    allocated = sum(job["slice_count"] for job in jobs)
    print(f"plan: {len(jobs)} workers over {allocated}/{PREFIX_TOTAL} prefixes (exact cover)")
    return jobs


def dispatch(workdir):
    jobs = json.load(open(f"{workdir}/plan.json"))
    validate_partition(jobs, PREFIX_TOTAL)
    results = {}
    for j in jobs:
        node, lo, n = j["node"], j["slice_start"], j["slice_count"]
        job = {"N2": N2, "slice_start": lo, "slice_count": n}
        for k, fn in [("pmask", "p_masks.bin"), ("pweight", "p_weights.bin"),
                      ("pblocked", "p_blocked.bin"), ("smask", "s_smask.bin"),
                      ("sfe_lo", "s_sfe_lo.bin"), ("sfe_hi", "s_sfe_hi.bin"),
                      ("sweight", "s_sweight.bin")]:
            job[k] = f"/tmp/fm/{fn}"
        jobf = f"{workdir}/job_{node}_{j['numa']}.json"
        json.dump(job, open(jobf, "w"))
        # ship the suffix state once (cache on the node) + slice state
        ssh(node, "mkdir -p /tmp/fm")
        for fn in ["s_smask.bin", "s_sfe_lo.bin", "s_sfe_hi.bin", "s_sweight.bin"]:
            subprocess.run(["scp", "-q", "-o", "BatchMode=yes", f"{workdir}/{fn}",
                            f"root@{NODES[node]}:/tmp/fm/{fn}"])
        # slice state generation on the dispatcher (batched GPU) then ship
        slice_state(workdir, lo, n)
        for fn in ["p_masks.bin", "p_weights.bin", "p_blocked.bin"]:
            subprocess.run(["scp", "-q", "-o", "BatchMode=yes", f"{workdir}/{fn}",
                            f"root@{NODES[node]}:/tmp/fm/{fn}"])
        subprocess.run(["scp", "-q", "-o", "BatchMode=yes", jobf, f"root@{NODES[node]}:/tmp/fm/job.json"])
        # numa binding
        bind = ""
        if j["cpus"] and len(j["cpus"]) > 1:
            cpus = ",".join(str(c) for c in j["cpus"])
            bind = f"numactl --physcpubind={cpus} --membind={j['numa']} "
        cmd = f"chmod +x {BIN} 2>/dev/null; {bind}{BIN} --merge /tmp/fm/job.json"
        r = ssh(node, cmd)
        try:
            res = json.loads(r.stdout[r.stdout.index("{"):])
            results[f"{node}/numa{j['numa']}"] = res
            print(f"{node} numa{j['numa']}: {res['as_fraction']} ({res['max_words']})")
        except Exception:
            print(f"{node} numa{j['numa']}: FAIL {r.stdout[-150:] or r.stderr[-150:]}")

    best, best_k = aggregate_worker_results(results)
    print(f"AGGREGATE: max_words {best} from {best_k}")
    json.dump({"results": results, "aggregate": best, "aggregate_from": best_k},
              open(f"{workdir}/aggregate.json", "w"), indent=1)
    return best


def slice_state(workdir, lo, n):
    sys.path.insert(0, ROOT)
    sys.path.insert(0, "/tmp/opencode")
    import torch
    from exact_optimum_split_certifier_v34 import blocked_mask, prefix_weights
    dev = "cuda"
    # the masks bin is u32 pairs
    raw = open(f"{workdir}/prefix/p_all_masks.bin", "rb").read()
    words = [struct.unpack("<I", raw[i:i+4])[0] for i in range(0, len(raw), 4)]
    sel = [words[2*i] | (words[2*i+1] << 32) for i in range(lo, lo + n)]
    t = torch.tensor(sel, dtype=torch.int64, device=dev)
    pm = torch.stack([t & 0xFFFFFFFF, t >> 32], 1)
    write_u32s(f"{workdir}/p_masks.bin", pm.reshape(-1).tolist())
    pw3 = prefix_weights(t, dev)
    pw6 = []
    for r in pw3.tolist():
        x = (int(r[2]) << 64) + (int(r[1]) << 32) + int(r[0])
        pw6.extend(words6(x))
    write_u32s(f"{workdir}/p_weights.bin", pw6)
    blk = blocked_mask(t, N2, dev)
    write_u32s(f"{workdir}/p_blocked.bin", blk.tolist())
    return


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--detect-all", action="store_true")
    ap.add_argument("--prepare", type=int)
    ap.add_argument("--plan", type=int)
    ap.add_argument("--dispatch", type=int)
    ap.add_argument("--workdir", default="/tmp/opencode/fm")
    args = ap.parse_args()
    os.makedirs(args.workdir, exist_ok=True)
    if args.detect_all:
        detect_all()
    if args.prepare:
        prepare_suffix(args.prepare, args.workdir)
    if args.plan:
        N2 = args.plan
        plan(args.workdir)
    if args.dispatch:
        N2 = args.dispatch
        dispatch(args.workdir)
