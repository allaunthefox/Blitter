// tailgain.rs — exact tail-gain kernel runner for the WebGPU fabric.
// Fabric-verified 2026-08-09: byte-identical words and fractions on
//   qfox-1 RTX 4070 SUPER (NVIDIA Vulkan)
//   nasfox RX 580 (RADV POLARIS10)
//   cupfox llvmpipe (LLVM 22.1.8) — glibc build, run inside the
//     blitter-daemon container (musl builds cannot dlopen libvulkan)
// cross-checked against the PyTorch/CUDA reference (1/72, 1/70 at
// the N=64 threat set, [65,72] tail).
//
// Reads a job file:  {"jobs": [{"N2": 72, "P": [1,2,4,...]}, ...]}
// For each job computes the exact tail gain
//   G = max weight of 3-AP-free A subset [65, N2] with P ∪ A 3-AP-free,
// in L_N2 = lcm(1..N2) units (6x u32 words, 192-bit exact), via the
// tailgain.wgsl compute kernel.  Outputs the lexicographic max as JSON.
//
// The same binary runs on every fabric node:
//   BLITTER_ADAPTER=<name-substring> selects the WebGPU adapter
//   (default: first adapter).  Byte-identical outputs are expected
//   across NVIDIA / RADV / llvmpipe.

use std::collections::BTreeSet;
use wgpu::util::DeviceExt;

#[repr(C)]
#[derive(Copy, Clone, bytemuck::Pod, bytemuck::Zeroable)]
struct Job {
    d: u32,
    n_triples: u32,
    n_pairs: u32,
    blocked: u32,
    triples: [u32; 512],
    pairs: [u32; 256],
}

const WORDS: usize = 6;
const OUT_STRIDE: usize = 7;

fn lcm_words(n: u32) -> [u32; 6] {
    // L = lcm(1..=n) as 6x u32 little-endian words: exact prime-power lcm.
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

fn div_word(l: &mut [u32; 6], d: u32) {
    let mut rem: u64 = 0;
    for w in l.iter_mut().rev() {
        let cur = (rem << 32) | (*w as u64);
        *w = (cur / d as u64) as u32;
        rem = cur % d as u64;
    }
}

fn weight_words(n2: u32, e: u32, l: &[u32; 6]) -> [u32; 6] {
    let mut w = *l;
    div_word(&mut w, e);
    w
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 3 || args[1] != "--jobs" {
        eprintln!("usage: tailgain --jobs <file.json>");
        std::process::exit(2);
    }
    let jobs: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&args[2]).unwrap()).unwrap();
    let jobs = jobs["jobs"].as_array().unwrap().clone();

    // adapter
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
    println!("adapter: {} ({:?})", adapter.get_info().name, adapter.get_info().backend);
    let (device, queue) = pollster::block_on(adapter.request_device(&wgpu::DeviceDescriptor {
        label: Some("tailgain-device"),
        required_features: wgpu::Features::empty(),
        required_limits: wgpu::Limits::default(),
        experimental_features: wgpu::ExperimentalFeatures::default(),
        memory_hints: wgpu::MemoryHints::Performance,
        trace: wgpu::Trace::Off,
    })).unwrap();

    let shader = device.create_shader_module(wgpu::ShaderModuleDescriptor {
        label: Some("tailgain"),
        source: wgpu::ShaderSource::Wgsl(include_str!("tailgain.wgsl").into()),
    });
    let bgl = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
        label: Some("bgl"),
        entries: &[
            wgpu::BindGroupLayoutEntry {
                binding: 0,
                visibility: wgpu::ShaderStages::COMPUTE,
                ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Storage { read_only: true }, has_dynamic_offset: false, min_binding_size: None },
                count: None,
            },
            wgpu::BindGroupLayoutEntry {
                binding: 1,
                visibility: wgpu::ShaderStages::COMPUTE,
                ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Storage { read_only: true }, has_dynamic_offset: false, min_binding_size: None },
                count: None,
            },
            wgpu::BindGroupLayoutEntry {
                binding: 2,
                visibility: wgpu::ShaderStages::COMPUTE,
                ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Storage { read_only: false }, has_dynamic_offset: false, min_binding_size: None },
                count: None,
            },
        ],
    });
    let pl = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
        label: Some("pl"),
        bind_group_layouts: &[Some(&bgl)],
        immediate_size: 0,
    });
    let pipeline = device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
        label: Some("tailgain-pipe"),
        layout: Some(&pl),
        module: &shader,
        entry_point: Some("tail_gain_pass"),
        compilation_options: Default::default(),
        cache: None,
    });

    let mut out_list: Vec<serde_json::Value> = Vec::new();

    for job_val in &jobs {
        let n2 = job_val["N2"].as_u64().unwrap() as u32;
        let p_elems: Vec<u32> = job_val["P"].as_array().unwrap()
            .iter().map(|v| v.as_u64().unwrap() as u32).collect();
        let d = if n2 >= 65 { n2 - 64 } else { 0 } as usize;
        let l = lcm_words(n2);

        // blocked singles: c = 2b-a, a < b in P, 65 <= c <= N2
        let mut blocked: u32 = 0;
        let mut pairs: Vec<u32> = Vec::new();
        for &a in &p_elems {
            for &b in &p_elems {
                if b <= a {
                    continue;
                }
                let c = 2 * b - a;
                if (65..=n2).contains(&c) {
                    blocked |= 1 << (c - 65);
                }
            }
        }
        // reflection pairs {b, 2b-p} subset of the domain, for p in P
        let mut pair_set: BTreeSet<u32> = BTreeSet::new();
        for &p in &p_elems {
            for b in 65..=n2 {
                let c = 2 * b - p;
                if c >= 65 && c <= n2 {
                    pair_set.insert((1 << (b - 65)) | (1 << (c - 65)));
                }
            }
        }
        pairs.extend(pair_set.iter());
        // internal triples of [65, N2]
        let mut triples: Vec<u32> = Vec::new();
        for b in 66..=n2.saturating_sub(1) {
            for a in 65..b {
                let c = 2 * b - a;
                if c <= n2 {
                    triples.push((1 << (a - 65)) | (1 << (b - 65)) | (1 << (c - 65)));
                }
            }
        }
        if d > 24 {
            eprintln!("N2={n2}: domain too large (d={d} > 24)");
            std::process::exit(3);
        }
        let job = Job {
            d: d as u32,
            n_triples: triples.len() as u32,
            n_pairs: pairs.len() as u32,
            blocked,
            triples: {
                let mut t = [0u32; 512];
                for (i, v) in triples.iter().enumerate() {
                    t[i] = *v;
                }
                t
            },
            pairs: {
                let mut t = [0u32; 256];
                for (i, v) in pairs.iter().enumerate() {
                    t[i] = *v;
                }
                t
            },
        };
        let weights: Vec<u32> = (65..=n2)
            .flat_map(|e| weight_words(n2, e, &l).into_iter())
            .collect();
        let n_cand: u32 = 1u32 << d;

        let job_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("job"), contents: bytemuck::bytes_of(&job),
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
        });
        let w_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("weights"), contents: bytemuck::cast_slice(&weights),
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
        });
        let out_size = n_cand as usize * OUT_STRIDE * 4;
        let out_buf = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("out"),
            size: out_size as u64,
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC,
            mapped_at_creation: false,
        });
        let read_buf = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("read"),
            size: out_size as u64,
            usage: wgpu::BufferUsages::COPY_DST | wgpu::BufferUsages::MAP_READ,
            mapped_at_creation: false,
        });
        let bgroup = device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: Some("bg"), layout: &bgl,
            entries: &[
                wgpu::BindGroupEntry { binding: 0, resource: job_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 1, resource: w_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 2, resource: out_buf.as_entire_binding() },
            ],
        });
        let mut enc = device.create_command_encoder(&wgpu::CommandEncoderDescriptor { label: None });
        {
            let mut pass = enc.begin_compute_pass(&wgpu::ComputePassDescriptor { label: Some("tg"), timestamp_writes: None });
            pass.set_pipeline(&pipeline);
            pass.set_bind_group(0, &bgroup, &[]);
            pass.dispatch_workgroups((n_cand + 63) / 64, 1, 1);
        }
        enc.copy_buffer_to_buffer(&out_buf, 0, &read_buf, 0, out_size as u64);
        queue.submit(Some(enc.finish()));
        let slice = read_buf.slice(..);
        let (tx, rx) = std::sync::mpsc::channel();
        slice.map_async(wgpu::MapMode::Read, move |r| { tx.send(r).unwrap(); });
        device.poll(wgpu::PollType::Wait { submission_index: None, timeout: None }).unwrap();
        rx.recv().unwrap().unwrap();
        let data = slice.get_mapped_range().unwrap();
        let words: &[u32] = bytemuck::cast_slice(&data);

        // lexicographic max over valid rows
        let mut best: Option<(u32, u32, u32, u32, u32, u32, u32)> = None;
        for i in 0..n_cand as usize {
            let base = i * OUT_STRIDE;
            if words[base] == 1 {
                let v = (words[base], words[base+1], words[base+2], words[base+3],
                        words[base+4], words[base+5], words[base+6]);
                if best.is_none() || {
                    let b = best.unwrap();
                    (v.1, v.2, v.3, v.4, v.5, v.6) > (b.1, b.2, b.3, b.4, b.5, b.6)
                } {
                    best = Some(v);
                }
            }
        }
        let b = best.expect("no valid candidate (empty suffix must be valid)");
        // reduce to fraction
        let val = words_to_bigint(&[b.1, b.2, b.3, b.4, b.5, b.6]);
        let lval = words_to_bigint(&l);
        let g = gcd_big(&val, &lval);
        let num = val / &g;
        let den = lval / &g;
        out_list.push(serde_json::json!({
            "N2": n2,
            "P": p_elems,
            "valid": b.0,
            "words": [b.1, b.2, b.3, b.4, b.5, b.6],
            "as_fraction": format!("{}/{}", num, den),
            "adapter": adapter_name,
        }));
    }
    let out_json = serde_json::json!({ "results": out_list });
    println!("{}", serde_json::to_string(&out_json).unwrap());
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

