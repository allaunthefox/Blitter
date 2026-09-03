// merge.rs — detection-driven sharded exact merge for the WebGPU fabric.
//
// Modes:
//   merge --detect                report hardware / NUMA topology as JSON
//   merge --merge job.json        run one prefix-slice merge; print the
//                                 slice's exact lexicographic max total
//
// The merge job:
//   { "N2": n, "slice_start": s, "slice_count": c,
//     "pmask": "...bin", "pweight": "...bin", "pblocked": "...bin",
//     "smask": "...bin", "sfe_lo": "...bin", "sfe_hi": "...bin",
//     "sweight": "...bin" }
// Binary formats (little-endian u32):
//   pmask    [c][2]  prefix masks (lo, hi)
//   pweight  [c][6]  prefix weights (L_N2 units)
//   pblocked [c]     blocked singles over the suffix domain
//   smask    [n]     suffix masks (sorted descending by score)
//   sfe_lo/sfe_hi    [n]  FE masks (lo, hi) per suffix
//   sweight  [n][6]  suffix weights
//
// The dispatcher partitions the prefix range using --detect output
// (core count, memory, NUMA nodes) and binds workers per NUMA node
// (numactl --cpunodebind / --membind).

use std::io::Read;
use wgpu::util::DeviceExt;

#[repr(C)]
#[derive(Copy, Clone, bytemuck::Pod, bytemuck::Zeroable)]
struct MergeJob {
    n_suffix: u32,
    slice_start: u32,
    slice_count: u32,
}

fn read_u32s(path: &str) -> Vec<u32> {
    let mut f = std::fs::File::open(path).unwrap_or_else(|e| {
        eprintln!("cannot open {path}: {e}");
        std::process::exit(3);
    });
    let mut bytes = Vec::new();
    f.read_to_end(&mut bytes).unwrap();
    assert!(bytes.len() % 4 == 0, "{path}: not u32-aligned");
    let mut out = Vec::with_capacity(bytes.len() / 4);
    for ch in bytes.chunks_exact(4) {
        out.push(u32::from_le_bytes([ch[0], ch[1], ch[2], ch[3]]));
    }
    out
}

fn cpu_count() -> usize {
    std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1)
}

fn mem_total_kb() -> u64 {
    let s = std::fs::read_to_string("/proc/meminfo").unwrap_or_default();
    for line in s.lines() {
        if line.starts_with("MemTotal:") {
            return line.split_whitespace().nth(1).unwrap_or("0").parse().unwrap_or(0);
        }
    }
    0
}

fn numa_nodes() -> Vec<(u32, Vec<u32>, u64)> {
    // (node id, cpus, mem total kB) from /sys/devices/system/node/node*
    let mut out = Vec::new();
    if let Ok(rd) = std::fs::read_dir("/sys/devices/system/node") {
        let mut entries: Vec<_> = rd.flatten().collect();
        entries.sort_by_key(|e| e.file_name());
        for e in entries {
            let name = e.file_name().to_string_lossy().into_owned();
            if !name.starts_with("node") || name == "node" {
                continue;
            }
            let id: u32 = name[4..].parse().unwrap_or(u32::MAX);
            if id == u32::MAX {
                continue;
            }
            let mut cpus = Vec::new();
            if let Ok(cs) = std::fs::read_to_string(e.path().join("cpulist")) {
                for part in cs.trim().split(',') {
                    let (a, b) = match part.split_once('-') {
                        Some((x, y)) => (x.parse::<u32>().unwrap_or(0), y.parse::<u32>().unwrap_or(0)),
                        None => (part.parse().unwrap_or(0), part.parse().unwrap_or(0)),
                    };
                    for c in a..=b {
                        cpus.push(c);
                    }
                }
            }
            let mem = std::fs::read_to_string(e.path().join("meminfo"))
                .unwrap_or_default();
            let mut mem_kb = 0u64;
            for line in mem.lines() {
                if line.starts_with("MemTotal:") {
                    mem_kb = line.split_whitespace().nth(1).unwrap_or("0").parse().unwrap_or(0);
                }
            }
            out.push((id, cpus, mem_kb));
        }
    }
    out
}

fn detect_json() -> serde_json::Value {
    let nodes: Vec<serde_json::Value> = numa_nodes()
        .iter()
        .map(|(id, cpus, mem)| serde_json::json!({
            "id": id, "cpus": cpus.len(), "cpu_list": cpus, "mem_total_kb": mem,
        }))
        .collect();
    serde_json::json!({
        "cpu_count": cpu_count(),
        "mem_total_kb": mem_total_kb(),
        "numa_nodes": nodes,
        "numa_detected": !nodes.is_empty(),
    })
}

fn words_to_bigint(w: &[u32; 6]) -> num_bigint::BigUint {
    let mut v = num_bigint::BigUint::from(0u32);
    for i in (0..6).rev() {
        v = v << 32 | num_bigint::BigUint::from(w[i]);
    }
    v
}

fn gcd_big(a: &num_bigint::BigUint, b: &num_bigint::BigUint) -> num_bigint::BigUint {
    let (mut a, mut b) = (a.clone(), b.clone());
    while b != num_bigint::BigUint::from(0u32) {
        let t = a % &b;
        a = b;
        b = t;
    }
    a
}

fn run_merge(job: &serde_json::Value) {
    let n2 = job["N2"].as_u64().unwrap() as u32;
    let slice_start = job["slice_start"].as_u64().unwrap() as u32;
    let slice_count = job["slice_count"].as_u64().unwrap() as u32;
    let pmask = read_u32s(job["pmask"].as_str().unwrap());
    let pweight = read_u32s(job["pweight"].as_str().unwrap());
    let pblocked = read_u32s(job["pblocked"].as_str().unwrap());
    let smask = read_u32s(job["smask"].as_str().unwrap());
    let sfe_lo = read_u32s(job["sfe_lo"].as_str().unwrap());
    let sfe_hi = read_u32s(job["sfe_hi"].as_str().unwrap());
    let mut sfe = Vec::with_capacity(sfe_lo.len() * 2);
    for i in 0..sfe_lo.len() {
        sfe.push(sfe_lo[i]);
        sfe.push(sfe_hi[i]);
    }
    let sweight = read_u32s(job["sweight"].as_str().unwrap());
    let n_suffix = smask.len() as u32;
    assert!(pweight.len() as u32 >= slice_count * 6);
    assert!(sweight.len() as u32 >= n_suffix * 6);

    // adapter (same selection as tailgain)
    let instance = wgpu::Instance::new(wgpu::InstanceDescriptor {
        backends: wgpu::Backends::all(),
        flags: wgpu::InstanceFlags::default(),
        memory_budget_thresholds: wgpu::MemoryBudgetThresholds::default(),
        backend_options: wgpu::BackendOptions::default(),
        display: None,
    });
    let want = std::env::var("BLITTER_ADAPTER").ok();
    let adapters: Vec<_> = pollster::block_on(instance.enumerate_adapters(wgpu::Backends::all()));
    let chosen = if let Some(w) = &want {
        adapters.iter().find(|a| a.get_info().name.to_lowercase().contains(&w.to_lowercase()))
            .expect("no adapter matching BLITTER_ADAPTER")
    } else {
        adapters.first().expect("no WebGPU adapter")
    };
    let adapter = chosen.clone();
    let adapter_name = adapter.get_info().name.clone();
    // raise buffer limits to the adapter's maximums (slice-sized state)
    let supported = adapter.limits();
    let mut limits = wgpu::Limits::default();
    limits.max_buffer_size = supported.max_buffer_size;
    limits.max_storage_buffer_binding_size = supported.max_storage_buffer_binding_size;
    let (device, queue) = pollster::block_on(adapter.request_device(&wgpu::DeviceDescriptor {
        label: Some("merge-device"),
        required_features: wgpu::Features::empty(),
        required_limits: limits,
        experimental_features: wgpu::ExperimentalFeatures::default(),
        memory_hints: wgpu::MemoryHints::Performance,
        trace: wgpu::Trace::Off,
    })).unwrap();

    let shader = device.create_shader_module(wgpu::ShaderModuleDescriptor {
        label: Some("merge"),
        source: wgpu::ShaderSource::Wgsl(include_str!("merge.wgsl").into()),
    });
    let bgl = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
        label: Some("bgl"),
        entries: &[
            bind_storage(&device, 0),
            bind_storage(&device, 1),
            bind_storage(&device, 2),
            bind_storage(&device, 3),
            bind_storage(&device, 4),
            bind_storage(&device, 5),
            bind_storage(&device, 6),
            bind_storage(&device, 7),
        ],
    });
    let pl = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
        label: Some("pl"),
        bind_group_layouts: &[Some(&bgl)],
        immediate_size: 0,
    });
    let pipeline = device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
        label: Some("merge-pipe"),
        layout: Some(&pl),
        module: &shader,
        entry_point: Some("merge_pass"),
        compilation_options: Default::default(),
        cache: None,
    });

    let job_s = MergeJob { n_suffix, slice_start, slice_count };
    let job_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
        label: Some("job"), contents: bytemuck::bytes_of(&job_s),
        usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
    });
    let mk = |label: &str, data: &[u32]| {
        device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some(label), contents: bytemuck::cast_slice(data),
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
        })
    };
    let b_pm = mk("pmask", &pmask);
    let b_pw = mk("pweight", &pweight);
    let b_pb = mk("pblocked", &pblocked);
    let b_sm = mk("smask", &smask);
    let b_sf = mk("sfe", &sfe);
    let b_sw = mk("sweight", &sweight);

    // chunked execution: process the slice in chunks to bound memory
    let chunk = (1usize << 20).min(slice_count as usize);
    let out_size = (chunk * 8 * 4) as u64;
    let out_buf = device.create_buffer(&wgpu::BufferDescriptor {
        label: Some("out"),
        size: out_size,
        usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC,
        mapped_at_creation: false,
    });
    let read_buf = device.create_buffer(&wgpu::BufferDescriptor {
        label: Some("read"),
        size: out_size,
        usage: wgpu::BufferUsages::COPY_DST | wgpu::BufferUsages::MAP_READ,
        mapped_at_creation: false,
    });

    let mut best: Option<[u32; 6]> = None;
    let mut best_mask: u32 = 0;
    let mut processed = 0u32;
    while processed < slice_count {
        let n = ((slice_count - processed) as usize).min(chunk);
        let off = processed as usize * 8 * 4;
        let job_c = MergeJob { n_suffix, slice_start: processed, slice_count: n as u32 };
        let job_c_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("jobc"), contents: bytemuck::bytes_of(&job_c),
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
        });
        let bgroup = device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: Some("bg"), layout: &bgl,
            entries: &[
                wgpu::BindGroupEntry { binding: 0, resource: job_c_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 1, resource: b_pm.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 2, resource: b_pb.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 3, resource: b_pw.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 4, resource: b_sm.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 5, resource: b_sf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 6, resource: b_sw.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 7, resource: out_buf.as_entire_binding() },
            ],
        });
        let mut enc = device.create_command_encoder(&wgpu::CommandEncoderDescriptor { label: None });
        {
            let mut pass = enc.begin_compute_pass(&wgpu::ComputePassDescriptor { label: Some("m"), timestamp_writes: None });
            pass.set_pipeline(&pipeline);
            pass.set_bind_group(0, &bgroup, &[]);
            pass.dispatch_workgroups(((n as u32) + 63) / 64, 1, 1);
        }
        enc.copy_buffer_to_buffer(&out_buf, 0, &read_buf, 0, out_size);
        queue.submit(Some(enc.finish()));
        let slice = read_buf.slice(..);
        let (tx, rx) = std::sync::mpsc::channel();
        slice.map_async(wgpu::MapMode::Read, move |r| { tx.send(r).unwrap(); });
        device.poll(wgpu::PollType::Wait { submission_index: None, timeout: None }).unwrap();
        rx.recv().unwrap().unwrap();
        let data = slice.get_mapped_range().unwrap();
        let words: &[u32] = bytemuck::cast_slice(&data);
        for i in 0..n {
            let base = i * 8;
            if words[base] == 1 {
                let v = [words[base+1], words[base+2], words[base+3],
                         words[base+4], words[base+5], words[base+6]];
                if best.is_none() || lex_gt(&v, best.as_ref().unwrap()) {
                    best = Some(v);
                    best_mask = pmask[(processed as usize + i) * 2];
                }
            }
        }
        drop(data);
        processed += n as u32;
    }
    let b = best.expect("no valid prefix total");
    let val = words_to_bigint(&b);
    // L_N2 for the fraction: recompute via prime powers
    let l = lcm_words(n2);
    let lval = words_to_bigint(&l);
    let g = gcd_big(&val, &lval);
    let num = val / &g;
    let den = lval / &g;
    println!("{}", serde_json::json!({
        "slice_start": slice_start,
        "slice_count": slice_count,
        "N2": n2,
        "max_words": b,
        "max_prefix_mask_lo": best_mask,
        "as_fraction": format!("{}/{}", num, den),
        "adapter": adapter_name,
    }));
}

fn lex_gt(a: &[u32; 6], b: &[u32; 6]) -> bool {
    for i in (0..6).rev() {
        if a[i] != b[i] {
            return a[i] > b[i];
        }
    }
    false
}

fn bind_storage(device: &wgpu::Device, binding: u32) -> wgpu::BindGroupLayoutEntry {
    wgpu::BindGroupLayoutEntry {
        binding,
        visibility: wgpu::ShaderStages::COMPUTE,
        ty: wgpu::BindingType::Buffer {
            ty: wgpu::BufferBindingType::Storage { read_only: binding != 7 },
            has_dynamic_offset: false,
            min_binding_size: None,
        },
        count: None,
    }
}

fn is_prime(x: u32) -> bool {
    if x < 2 {
        return false;
    }
    let mut d = 2u32;
    while d * d <= x {
        if x % d == 0 {
            return false;
        }
        d += 1;
    }
    true
}

fn mul_word(l: &mut [u32; 6], m: u32) {
    let mut carry: u64 = 0;
    for w in l.iter_mut() {
        let t = (*w as u64) * (m as u64) + carry;
        *w = (t & 0xFFFF_FFFF) as u32;
        carry = t >> 32;
    }
}

fn lcm_words(n: u32) -> [u32; 6] {
    let mut l = [0u32; 6];
    l[0] = 1;
    for p in 2..=n {
        if !is_prime(p) {
            continue;
        }
        let mut pe: u64 = p as u64;
        while pe * p as u64 <= n as u64 {
            pe *= p as u64;
        }
        mul_word(&mut l, pe as u32);
    }
    l
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 {
        eprintln!("usage: merge --detect | --merge <job.json>");
        std::process::exit(2);
    }
    match args[1].as_str() {
        "--detect" => {
            println!("{}", serde_json::to_string(&detect_json()).unwrap());
        }
        "--merge" => {
            if args.len() < 3 {
                eprintln!("--merge needs a job file");
                std::process::exit(2);
            }
            let job: serde_json::Value =
                serde_json::from_str(&std::fs::read_to_string(&args[2]).unwrap()).unwrap();
            run_merge(&job);
        }
        _ => {
            eprintln!("unknown mode {}", args[1]);
            std::process::exit(2);
        }
    }
}
