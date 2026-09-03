#!/usr/bin/env python3
"""Plan, execute, collect, and verify distributed rank-2 searches."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import shlex
import math
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN_ROOT = ROOT / ".cluster-runs"
NODE_CONFIG = ROOT / "scripts" / "cluster" / "nodes.json"
SEARCH_MODULES = {
    "gauge": "experiments.ramanujan_25.search_rank2_gauge_variants",
    "rational": "experiments.ramanujan_25.search_rank2_rational_intertwiner",
}


@dataclass(frozen=True)
class Worker:
    name: str
    destination: str
    runtime: str
    cpus: int
    memory_gib: int
    ephemeral: bool
    online_cpus: int | None = None
    available_memory_gib: float | None = None
    load1: float = 0.0
    llc_groups: int | None = None
    llc_total_mib: float | None = None

    @property
    def effective_cpus(self) -> int:
        online = self.online_cpus or self.cpus
        load_headroom = max(1, math.floor(online - max(0.0, self.load1)))
        memory = self.available_memory_gib if self.available_memory_gib is not None else float(self.memory_gib)
        memory_workers = math.floor(min(float(self.memory_gib), memory * 0.8) / 0.75)
        if memory_workers < 1:
            return 0
        cache_workers = self.cpus
        if self.llc_total_mib is not None:
            cache_workers = max(1, math.floor(self.llc_total_mib / 4.0))
        return max(0, min(self.cpus, online, load_headroom, memory_workers, cache_workers))

    @property
    def effective_memory_gib(self) -> int:
        available = self.available_memory_gib if self.available_memory_gib is not None else float(self.memory_gib)
        return max(0, min(self.memory_gib, math.floor(available * 0.8)))

    @property
    def scheduling_capacity(self) -> float:
        cpus = float(self.effective_cpus)
        if cpus <= 0 or self.effective_memory_gib <= 0:
            return 0.0
        cache_per_worker = (self.llc_total_mib or (4.0 * cpus)) / cpus
        cache_bonus = 1.0 + min(cache_per_worker, 32.0) / 64.0
        memory_per_worker = self.effective_memory_gib / cpus
        memory_bonus = 1.0 + min(memory_per_worker, 4.0) / 20.0
        return cpus * cache_bonus * memory_bonus


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_digest(payload: dict) -> str:
    body = dict(payload)
    body.pop("receipt_sha256", None)
    body.pop("plan_sha256", None)
    return sha256_bytes(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())


def git_output(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def _cache_size_mib(text: str) -> float:
    value = text.strip().upper()
    if not value:
        raise ValueError("empty cache size")
    units = {"K": 1.0 / 1024.0, "M": 1.0, "G": 1024.0}
    try:
        return float(value[:-1]) * units[value[-1]]
    except (ValueError, KeyError) as exc:
        raise ValueError(f"invalid cache size: {text!r}") from exc


def probe_worker(worker: Worker) -> Worker:
    probe = r"""
set -eu
cpus=$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 1)
mem_available_kib=$(awk '/^MemAvailable:/{print $2; found=1; exit} END{if(!found) print 0}' /proc/meminfo)
if [ "$mem_available_kib" = 0 ]; then
  mem_available_kib=$(awk '/^MemTotal:/{print $2; exit}' /proc/meminfo)
fi
load1=$(cut -d' ' -f1 /proc/loadavg)
printf 'online_cpus=%s\n' "$cpus"
printf 'available_memory_kib=%s\n' "$mem_available_kib"
printf 'load1=%s\n' "$load1"
for cpu_dir in /sys/devices/system/cpu/cpu[0-9]*; do
  [ -d "$cpu_dir/cache" ] || continue
  best_level=-1
  best_index=
  for index in "$cpu_dir"/cache/index*; do
    [ -d "$index" ] || continue
    level=$(cat "$index/level" 2>/dev/null || echo -1)
    case $level in *[!0-9]*|'') continue;; esac
    if [ "$level" -gt "$best_level" ]; then
      best_level=$level
      best_index=$index
    fi
  done
  [ -n "$best_index" ] || continue
  shared=$(cat "$best_index/shared_cpu_list" 2>/dev/null || basename "$cpu_dir" | sed 's/cpu//')
  size=$(cat "$best_index/size" 2>/dev/null || echo 0K)
  printf 'llc=%s|%s\n' "$shared" "$size"
done
"""
    result = subprocess.run(
        [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            worker.destination, f"sh -lc {shlex.quote(probe)}",
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
    )
    if result.returncode != 0:
        raise ValueError(f"node {worker.name}: live resource probe failed: {result.stderr.strip()}")
    values: dict[str, str] = {}
    cache_groups: dict[str, float] = {}
    for line in result.stdout.splitlines():
        if line.startswith("llc="):
            try:
                shared, size = line[4:].split("|", 1)
                cache_groups[shared] = _cache_size_mib(size)
            except ValueError as exc:
                raise ValueError(f"node {worker.name}: invalid LLC probe output") from exc
        elif "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    try:
        online_cpus = int(values["online_cpus"])
        available_memory_gib = int(values["available_memory_kib"]) / 1048576.0
        load1 = float(values["load1"])
    except (KeyError, ValueError) as exc:
        raise ValueError(f"node {worker.name}: incomplete resource probe output") from exc
    return Worker(
        name=worker.name,
        destination=worker.destination,
        runtime=worker.runtime,
        cpus=worker.cpus,
        memory_gib=worker.memory_gib,
        ephemeral=worker.ephemeral,
        online_cpus=online_cpus,
        available_memory_gib=available_memory_gib,
        load1=load1,
        llc_groups=len(cache_groups) or None,
        llc_total_mib=sum(cache_groups.values()) if cache_groups else None,
    )


def load_workers(names: list[str], allow_ephemeral: bool) -> list[Worker]:
    raw = json.loads(NODE_CONFIG.read_text(encoding="utf-8"))
    configured = {node["name"]: node for node in raw["nodes"]}
    workers = []
    for name in names:
        node = configured[name]
        if node["ephemeral"] and not allow_ephemeral:
            raise ValueError(f"node {name} is ephemeral; pass --allow-ephemeral")
        user = os.path.expandvars(node["ssh_user"])
        if not user or "$" in user:
            raise ValueError(f"node {name}: SSH user is not configured")
        configured_worker = Worker(
            name=name,
            destination=f"{user}@{node['address']}",
            runtime=node["runtime"],
            cpus=int(node["max_cpus"]),
            memory_gib=int(node["max_memory_gib"]),
            ephemeral=bool(node["ephemeral"]),
        )
        workers.append(probe_worker(configured_worker))
    return workers


def weighted_assignments(workers: list[Worker], shard_count: int) -> list[str]:
    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    eligible = [worker for worker in workers if worker.scheduling_capacity > 0]
    if not eligible:
        raise ValueError("no workers have sufficient live CPU, RAM, and cache capacity")
    assignments: list[str] = []
    load = {worker.name: 0.0 for worker in eligible}
    by_name = {worker.name: worker for worker in eligible}
    for _ in range(shard_count):
        chosen = min(eligible, key=lambda worker: (load[worker.name], worker.name))
        assignments.append(chosen.name)
        load[chosen.name] += 1.0 / chosen.scheduling_capacity
    assert set(assignments).issubset(by_name)
    return assignments


def cmd_plan(args: argparse.Namespace) -> int:
    workers = load_workers(args.nodes, args.allow_ephemeral)
    commit = git_output("rev-parse", "HEAD")
    dirty = git_output("status", "--porcelain")
    if dirty:
        raise ValueError("working tree must be clean before planning a distributed run")
    run_id = args.run_id or f"rank2-{args.search}-{commit[:10]}-{int(time.time())}"
    run_dir = RUN_ROOT / run_id
    if run_dir.exists():
        raise ValueError(f"run already exists: {run_id}")
    (run_dir / "results").mkdir(parents=True)
    (run_dir / "logs").mkdir()
    assignments = weighted_assignments(workers, args.shards)
    jobs = []
    for index, node_name in enumerate(assignments):
        worker = next(worker for worker in workers if worker.name == node_name)
        jobs.append(
            {
                "job_id": f"{args.search}-{index:04d}-of-{args.shards:04d}",
                "search": args.search,
                "shard_index": index,
                "shard_count": args.shards,
                "degree_start": args.degree_start,
                "degree_end": args.degree_end,
                "node": node_name,
                "cpus": worker.effective_cpus,
                "memory_gib": worker.effective_memory_gib,
                "resource_snapshot": {
                    "online_cpus": worker.online_cpus,
                    "available_memory_gib": worker.available_memory_gib,
                    "load1": worker.load1,
                    "llc_groups": worker.llc_groups,
                    "llc_total_mib": worker.llc_total_mib,
                    "scheduling_capacity": worker.scheduling_capacity,
                },
                "rank_backend": args.rank_backend,
                "status": "pending",
            }
        )
    manifest = {
        "schema": "mathpunch-cluster-run-v1",
        "run_id": run_id,
        "git_commit": commit,
        "search": args.search,
        "rank_backend": args.rank_backend,
        "rank_target_cpu": "x86-64-v3" if args.rank_backend == "rust" else None,
        "created_unix": int(time.time()),
        "nodes": [worker.__dict__ for worker in workers],
        "jobs": jobs,
    }
    manifest["plan_sha256"] = canonical_digest(manifest)
    (run_dir / "plan.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(run_dir)
    return 0


def create_rank_binary(run_dir: Path, manifest: dict) -> Path | None:
    if manifest.get("rank_backend") != "rust":
        return None
    binary_dir = run_dir / "bin"
    binary_dir.mkdir(exist_ok=True)
    binary = binary_dir / "modular_rank_batch"
    if binary.exists():
        return binary
    target_dir = run_dir / "cargo-target"
    environment = os.environ.copy()
    environment["CARGO_TARGET_DIR"] = str(target_dir)
    environment["RUSTFLAGS"] = "-C target-cpu=x86-64-v3"
    sccache = os.environ.get("MATHPUNCH_SCCACHE_BIN") or shutil.which("sccache")
    if sccache:
        cache_dir = Path(os.environ.get("MATHPUNCH_SCCACHE_DIR", ROOT / ".cache" / "sccache"))
        cache_dir.mkdir(parents=True, exist_ok=True)
        environment["RUSTC_WRAPPER"] = sccache
        environment["SCCACHE_DIR"] = str(cache_dir)
    cargo = os.environ.get("MATHPUNCH_CARGO_BIN")
    if not cargo:
        cargo_home = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo"))
        rustup_cargo = cargo_home / "bin" / "cargo"
        cargo = str(rustup_cargo) if rustup_cargo.is_file() else shutil.which("cargo")
    if not cargo:
        raise ValueError("Cargo executable not found")
    subprocess.run(
        [cargo, "build", "--release", "--locked", "--bin", "modular_rank_batch"],
        cwd=ROOT,
        env=environment,
        check=True,
    )
    built = target_dir / "release" / "modular_rank_batch"
    shutil.copy2(built, binary)
    binary.chmod(0o755)
    shutil.rmtree(target_dir)
    return binary


def create_archive(commit: str, destination: Path) -> None:
    with destination.open("wb") as output:
        subprocess.run(["git", "archive", "--format=tar.gz", commit], cwd=ROOT, check=True, stdout=output)


def ssh(destination: str, command: str, *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", destination, f"sh -lc {shlex.quote(command)}"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def scp(source: Path, destination: str, remote_path: str) -> None:
    subprocess.run(["scp", "-q", str(source), f"{destination}:{remote_path}"], check=True)


def remote_runtime_command(worker: Worker, workspace: str, module: str, job: dict) -> str:
    command = (
        f"python3 -m {module} --degree-start {job['degree_start']} --degree-end {job['degree_end']} "
        f"--shard-index {job['shard_index']} --shard-count {job['shard_count']} "
        f"--workers {job['cpus']} --output /output/result.json"
    )
    image = "docker.io/library/python:3.12-slim"
    setup = "python3 -m pip install --disable-pip-version-check --no-cache-dir sympy==1.14.0 >/dev/null"
    backend_environment = ""
    if job.get("rank_backend") == "rust":
        backend_environment = (
            "MATHPUNCH_RANK_BACKEND=rust "
            "MATHPUNCH_RANK_BINARY=/work/bin/modular_rank_batch "
        )
    command = backend_environment + command
    if worker.runtime == "docker":
        return (
            f"docker run --rm --cpus={job['cpus']} --memory={job['memory_gib']}g "
            f"-v {workspace}:/work:ro -v {workspace}/output:/output -w /work {image} "
            f"sh -lc '{setup} && {command}'"
        )
    if worker.runtime == "nix-podman":
        return (
            "policy=$(mktemp) && trap 'rm -f \"$policy\"' EXIT && "
            "printf '%s\n' '{\"default\":[{\"type\":\"insecureAcceptAnything\"}]}' > \"$policy\" && "
            "nix shell nixpkgs#podman --command podman run --signature-policy=\"$policy\" --rm "
            f"--cpus={job['cpus']} --memory={job['memory_gib']}g "
            f"-v {workspace}:/work:ro -v {workspace}/output:/output -w /work {image} "
            f"sh -lc '{setup} && {command}'"
        )
    if worker.runtime == "podman-nvidia-cdi":
        return (
            f"podman run --rm --cpus={job['cpus']} --memory={job['memory_gib']}g "
            f"-v {workspace}:/work:ro -v {workspace}/output:/output -w /work {image} "
            f"sh -lc '{setup} && {command}'"
        )
    raise ValueError(f"unsupported runtime for rank2 execution: {worker.runtime}")


def run_job(run_dir: Path, manifest: dict, job: dict, timeout: int) -> None:
    worker_map = {worker["name"]: Worker(**worker) for worker in manifest["nodes"]}
    worker = worker_map[job["node"]]
    live = probe_worker(
        Worker(
            name=worker.name,
            destination=worker.destination,
            runtime=worker.runtime,
            cpus=worker.cpus,
            memory_gib=worker.memory_gib,
            ephemeral=worker.ephemeral,
        )
    )
    if live.effective_cpus < int(job["cpus"]):
        raise RuntimeError(
            f"node {worker.name}: CPU headroom changed ({live.effective_cpus} < {job['cpus']}); replan"
        )
    if live.effective_memory_gib < int(job["memory_gib"]):
        raise RuntimeError(
            f"node {worker.name}: memory headroom changed ({live.effective_memory_gib} < {job['memory_gib']} GiB); replan"
        )
    remote_root = f"/tmp/mathpunch-{manifest['run_id']}-{job['job_id']}"
    archive = run_dir / "source.tar.gz"
    scp(archive, worker.destination, f"{remote_root}.tar.gz")
    prepare = (
        f"rm -rf {remote_root} && mkdir -p {remote_root}/output && "
        f"tar -xzf {remote_root}.tar.gz -C {remote_root} && rm -f {remote_root}.tar.gz"
    )
    prepared = ssh(worker.destination, prepare, timeout=60)
    if prepared.returncode:
        raise RuntimeError(prepared.stderr)
    if job.get("rank_backend") == "rust":
        rank_binary = run_dir / "bin" / "modular_rank_batch"
        ssh(worker.destination, f"mkdir -p {remote_root}/bin", timeout=30)
        scp(rank_binary, worker.destination, f"{remote_root}/bin/modular_rank_batch")
        chmod = ssh(worker.destination, f"chmod 755 {remote_root}/bin/modular_rank_batch", timeout=30)
        if chmod.returncode:
            raise RuntimeError(chmod.stderr)
    remote_command = remote_runtime_command(worker, remote_root, SEARCH_MODULES[job["search"]], job)
    result = ssh(worker.destination, remote_command, timeout=timeout)
    (run_dir / "logs" / f"{job['job_id']}.stdout").write_text(result.stdout, encoding="utf-8")
    (run_dir / "logs" / f"{job['job_id']}.stderr").write_text(result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"remote exit {result.returncode}: {result.stderr[-1000:]}")
    local_result = run_dir / "results" / f"{job['job_id']}.json"
    subprocess.run(
        ["scp", "-q", f"{worker.destination}:{remote_root}/output/result.json", str(local_result)],
        check=True,
    )
    ssh(worker.destination, f"rm -rf {remote_root}", timeout=30)


def load_plan(run_dir: Path) -> dict:
    plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    if canonical_digest(plan) != plan.get("plan_sha256"):
        raise ValueError("plan digest mismatch")
    return plan


def cmd_submit(args: argparse.Namespace) -> int:
    run_dir = RUN_ROOT / args.run_id
    manifest = load_plan(run_dir)
    archive = run_dir / "source.tar.gz"
    if not archive.exists():
        create_archive(manifest["git_commit"], archive)
    create_rank_binary(run_dir, manifest)
    queues: dict[str, list[dict]] = {}
    for job in manifest["jobs"]:
        result_path = run_dir / "results" / f"{job['job_id']}.json"
        if result_path.exists() and not args.rerun:
            continue
        queues.setdefault(job["node"], []).append(job)

    def run_queue(node: str, jobs: list[dict]) -> tuple[str, int]:
        completed = 0
        for job in jobs:
            print(f"running {job['job_id']} on {node}", flush=True)
            run_job(run_dir, manifest, job, args.timeout)
            completed += 1
        return node, completed

    if not queues:
        return 0
    with ThreadPoolExecutor(max_workers=len(queues)) as executor:
        futures = [executor.submit(run_queue, node, jobs) for node, jobs in sorted(queues.items())]
        for future in as_completed(futures):
            node, completed = future.result()
            print(f"completed {completed} job(s) on {node}", flush=True)
    return 0


def verify_result(job: dict, payload: dict) -> None:
    if payload.get("schema") != "mathpunch-rank2-shard-v1":
        raise ValueError(f"{job['job_id']}: invalid result schema")
    for key in ("search", "shard_index", "shard_count", "degree_start", "degree_end"):
        if payload.get(key) != job.get(key):
            raise ValueError(f"{job['job_id']}: field mismatch: {key}")
    if payload.get("receipt_sha256") != canonical_digest(payload):
        raise ValueError(f"{job['job_id']}: receipt digest mismatch")
    keys = payload.get("task_keys")
    if not isinstance(keys, list) or len(keys) != payload.get("task_count") or len(keys) != len(set(keys)):
        raise ValueError(f"{job['job_id']}: invalid task-key set")


def cmd_verify(args: argparse.Namespace) -> int:
    run_dir = RUN_ROOT / args.run_id
    manifest = load_plan(run_dir)
    all_keys: set[str] = set()
    all_hits: list[str] = []
    for job in manifest["jobs"]:
        path = run_dir / "results" / f"{job['job_id']}.json"
        if not path.exists():
            raise ValueError(f"missing result: {job['job_id']}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        verify_result(job, payload)
        overlap = all_keys.intersection(payload["task_keys"])
        if overlap:
            raise ValueError(f"duplicate task coverage: {next(iter(overlap))}")
        all_keys.update(payload["task_keys"])
        all_hits.extend(payload["hits"])
    merged = {
        "schema": "mathpunch-rank2-merged-v1",
        "run_id": manifest["run_id"],
        "git_commit": manifest["git_commit"],
        "search": manifest["search"],
        "rank_backend": manifest.get("rank_backend", "python"),
        "rank_target_cpu": manifest.get("rank_target_cpu"),
        "shard_count": len(manifest["jobs"]),
        "task_count": len(all_keys),
        "hits": sorted(set(all_hits)),
        "job_receipts": [
            json.loads((run_dir / "results" / f"{job['job_id']}.json").read_text())["receipt_sha256"]
            for job in manifest["jobs"]
        ],
    }
    merged["receipt_sha256"] = canonical_digest(merged)
    (run_dir / "merged.json").write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
    print(json.dumps(merged, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--search", choices=sorted(SEARCH_MODULES), required=True)
    plan.add_argument("--degree-start", type=int, default=0)
    plan.add_argument("--degree-end", type=int, default=18)
    plan.add_argument("--shards", type=int, default=14)
    plan.add_argument("--nodes", nargs="+", default=["cupfox", "nixos-laptop", "qfox-1"])
    plan.add_argument("--allow-ephemeral", action="store_true")
    plan.add_argument("--rank-backend", choices=["python", "rust"], default="rust")
    plan.add_argument("--run-id")
    submit = sub.add_parser("submit")
    submit.add_argument("run_id")
    submit.add_argument("--timeout", type=int, default=3600)
    submit.add_argument("--rerun", action="store_true")
    verify = sub.add_parser("verify")
    verify.add_argument("run_id")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.action == "plan":
            return cmd_plan(args)
        if args.action == "submit":
            return cmd_submit(args)
        if args.action == "verify":
            return cmd_verify(args)
        raise ValueError("unsupported action")
    except (KeyError, ValueError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"rank2-run: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
