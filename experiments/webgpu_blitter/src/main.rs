// WebGPU blitter-surface pipeline — full Laurent product (sort + reduce + compact)
// Exact ints; surfaces as the data fabric; sort/reduce/compact all on-device WGSL.

use std::collections::BTreeMap;
use wgpu::util::DeviceExt;

#[repr(C)]
#[derive(Copy, Clone, bytemuck::Pod, bytemuck::Zeroable)]
struct Term { qe: u32, te: u32, coef: i32, valid: u32 }

const N: u32 = 64;

fn main() {
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
    println!("adapter: {} ({:?})", adapter.get_info().name, adapter.get_info().backend);
    let (device, queue) = pollster::block_on(adapter.request_device(&wgpu::DeviceDescriptor {
        label: Some("blitter-device"),
        required_features: wgpu::Features::empty(),
        required_limits: wgpu::Limits::default(),
        experimental_features: wgpu::ExperimentalFeatures::default(),
        memory_hints: wgpu::MemoryHints::Performance,
        trace: wgpu::Trace::Off,
    })).unwrap();

    let a_terms: [Term; 8] = [
        Term { qe: 0, te: 0, coef: 1, valid: 1 },   // 1
        Term { qe: 1, te: 0, coef: -1, valid: 1 },  // -q
        Term { qe: 0, te: 1, coef: 2, valid: 1 },   // 2t
        Term { qe: 1, te: 1, coef: 3, valid: 1 },   // 3qt
        Term { qe: 2, te: 0, coef: -4, valid: 1 },  // -4q^2
        Term { qe: 0, te: 2, coef: 5, valid: 1 },   // 5t^2
        Term { qe: 1, te: 2, coef: -6, valid: 1 },  // -6qt^2
        Term { qe: 0, te: 0, coef: 0, valid: 0 },
    ];
    let b_terms: [Term; 8] = [
        Term { qe: 0, te: 0, coef: 1, valid: 1 },   // 1
        Term { qe: 1, te: 0, coef: 1, valid: 1 },   // q
        Term { qe: 0, te: 1, coef: -1, valid: 1 },  // -t
        Term { qe: 2, te: 1, coef: 2, valid: 1 },   // 2q^2t
        Term { qe: 0, te: 0, coef: 0, valid: 0 },
        Term { qe: 0, te: 0, coef: 0, valid: 0 },
        Term { qe: 0, te: 0, coef: 0, valid: 0 },
        Term { qe: 0, te: 0, coef: 0, valid: 0 },
    ];

    // surfaces
    let tex_a = device.create_texture(&wgpu::TextureDescriptor {
        label: Some("surface-A"),
        size: wgpu::Extent3d { width: 8, height: 1, depth_or_array_layers: 1 },
        mip_level_count: 1, sample_count: 1, dimension: wgpu::TextureDimension::D2,
        format: wgpu::TextureFormat::Rgba32Uint,
        usage: wgpu::TextureUsages::TEXTURE_BINDING | wgpu::TextureUsages::COPY_DST,
        view_formats: &[],
    });
    let tex_b = device.create_texture(&wgpu::TextureDescriptor {
        label: Some("surface-B"),
        size: wgpu::Extent3d { width: N, height: 1, depth_or_array_layers: 1 },
        mip_level_count: 1, sample_count: 1, dimension: wgpu::TextureDimension::D2,
        format: wgpu::TextureFormat::Rgba32Uint,
        usage: wgpu::TextureUsages::STORAGE_BINDING | wgpu::TextureUsages::COPY_SRC,
        view_formats: &[],
    });
    let bpp: u32 = 16;

    queue.write_texture(
        wgpu::TexelCopyTextureInfo { texture: &tex_a, mip_level: 0, origin: wgpu::Origin3d::ZERO, aspect: wgpu::TextureAspect::All },
        bytemuck::cast_slice(&a_terms),
        wgpu::TexelCopyBufferLayout { offset: 0, bytes_per_row: Some(8 * bpp), rows_per_image: Some(1) },
        wgpu::Extent3d { width: 8, height: 1, depth_or_array_layers: 1 },
    );

    let shader = device.create_shader_module(wgpu::ShaderModuleDescriptor {
        label: Some("laurent-pipeline"), source: wgpu::ShaderSource::Wgsl(include_str!("blitter.wgsl").into()),
    });

    let bgl = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
        label: Some("bgl"),
        entries: &[
            wgpu::BindGroupLayoutEntry { binding: 0, visibility: wgpu::ShaderStages::COMPUTE,
                ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Storage { read_only: true }, has_dynamic_offset: false, min_binding_size: None }, count: None },
            wgpu::BindGroupLayoutEntry { binding: 1, visibility: wgpu::ShaderStages::COMPUTE,
                ty: wgpu::BindingType::Texture { sample_type: wgpu::TextureSampleType::Uint, view_dimension: wgpu::TextureViewDimension::D2, multisampled: false }, count: None },
            wgpu::BindGroupLayoutEntry { binding: 2, visibility: wgpu::ShaderStages::COMPUTE,
                ty: wgpu::BindingType::StorageTexture { access: wgpu::StorageTextureAccess::WriteOnly, format: wgpu::TextureFormat::Rgba32Uint, view_dimension: wgpu::TextureViewDimension::D2 }, count: None },
            wgpu::BindGroupLayoutEntry { binding: 3, visibility: wgpu::ShaderStages::COMPUTE,
                ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Storage { read_only: false }, has_dynamic_offset: false, min_binding_size: None }, count: None },
            wgpu::BindGroupLayoutEntry { binding: 4, visibility: wgpu::ShaderStages::COMPUTE,
                ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Storage { read_only: false }, has_dynamic_offset: false, min_binding_size: None }, count: None },
            wgpu::BindGroupLayoutEntry { binding: 5, visibility: wgpu::ShaderStages::COMPUTE,
                ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Storage { read_only: false }, has_dynamic_offset: false, min_binding_size: None }, count: None },
            wgpu::BindGroupLayoutEntry { binding: 6, visibility: wgpu::ShaderStages::COMPUTE,
                ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Storage { read_only: false }, has_dynamic_offset: false, min_binding_size: None }, count: None },
        ],
    });
    let pl = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
        label: Some("pl"), bind_group_layouts: &[Some(&bgl)], immediate_size: 0,
    });
    let mk_pipe = |label: &str, entry: &str| {
        device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
            label: Some(label), layout: Some(&pl), module: &shader,
            entry_point: Some(entry), compilation_options: Default::default(), cache: None,
        })
    };
    let p_product = mk_pipe("product", "product_pass");
    let p_sort = mk_pipe("sort", "sort_pass");
    let p_reduce = mk_pipe("reduce", "reduce_pass");
    let p_compact = mk_pipe("compact", "compact_pass");

    let buf_b = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
        label: Some("terms-B"), contents: bytemuck::cast_slice(&b_terms),
        usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC,
    });
    let prods = device.create_buffer(&wgpu::BufferDescriptor {
        label: Some("prods"), size: (N * bpp) as u64,
        usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST | wgpu::BufferUsages::COPY_SRC,
        mapped_at_creation: false,
    });
    let finals = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
        label: Some("finals"),
        contents: &vec![0u8; (N * bpp) as usize],
        usage: wgpu::BufferUsages::STORAGE,
    });
    let final_out = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
        label: Some("finalOut"),
        contents: &vec![0u8; (N * bpp) as usize],
        usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC,
    });
    let counters = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
        label: Some("counters"), contents: bytemuck::bytes_of(&[0u32, 0u32, 0u32, 0u32]),
        usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
    });
    let readback = device.create_buffer(&wgpu::BufferDescriptor {
        label: Some("readback"), size: (N * bpp) as u64,
        usage: wgpu::BufferUsages::COPY_DST | wgpu::BufferUsages::MAP_READ,
        mapped_at_creation: false,
    });
    let sortdbg = device.create_buffer(&wgpu::BufferDescriptor {
        label: Some("sortdbg"), size: (N * bpp) as u64,
        usage: wgpu::BufferUsages::COPY_DST | wgpu::BufferUsages::MAP_READ,
        mapped_at_creation: false,
    });
    let bgl_inst = device.create_bind_group(&wgpu::BindGroupDescriptor {
        label: Some("bg"), layout: &bgl,
        entries: &[
            wgpu::BindGroupEntry { binding: 0, resource: buf_b.as_entire_binding() },
            wgpu::BindGroupEntry { binding: 1, resource: wgpu::BindingResource::TextureView(&tex_a.create_view(&wgpu::TextureViewDescriptor::default())) },
            wgpu::BindGroupEntry { binding: 2, resource: wgpu::BindingResource::TextureView(&tex_b.create_view(&wgpu::TextureViewDescriptor::default())) },
            wgpu::BindGroupEntry { binding: 3, resource: prods.as_entire_binding() },
            wgpu::BindGroupEntry { binding: 4, resource: finals.as_entire_binding() },
            wgpu::BindGroupEntry { binding: 5, resource: counters.as_entire_binding() },
            wgpu::BindGroupEntry { binding: 6, resource: final_out.as_entire_binding() },
        ],
    });

    {
        let mut enc = device.create_command_encoder(&wgpu::CommandEncoderDescriptor { label: Some("enc") });
        // stage 1: products into surface B
        {
            let mut pass = enc.begin_compute_pass(&wgpu::ComputePassDescriptor { label: Some("p1"), timestamp_writes: None });
            pass.set_pipeline(&p_product); pass.set_bind_group(0, &bgl_inst, &[]);
            pass.dispatch_workgroups(N, 1, 1);
        }
        // stage 2: blit surface B -> prods buffer (the "blitter" copy)
        enc.copy_texture_to_buffer(
            wgpu::TexelCopyTextureInfo { texture: &tex_b, mip_level: 0, origin: wgpu::Origin3d::ZERO, aspect: wgpu::TextureAspect::All },
            wgpu::TexelCopyBufferInfo { buffer: &prods, layout: wgpu::TexelCopyBufferLayout { offset: 0, bytes_per_row: Some(N * bpp), rows_per_image: Some(1) } },
            wgpu::Extent3d { width: N, height: 1, depth_or_array_layers: 1 },
        );
        // reset counter before compact
        queue.write_buffer(&counters, 0, bytemuck::bytes_of(&[0u32, 0u32, 0u32, 0u32]));
        // stage 3: sort
        {
            let mut pass = enc.begin_compute_pass(&wgpu::ComputePassDescriptor { label: Some("p3"), timestamp_writes: None });
            pass.set_pipeline(&p_sort); pass.set_bind_group(0, &bgl_inst, &[]);
            pass.dispatch_workgroups(1, 1, 1);
        }
        // stage 4a: reduce
        {
            let mut pass = enc.begin_compute_pass(&wgpu::ComputePassDescriptor { label: Some("p4a"), timestamp_writes: None });
            pass.set_pipeline(&p_reduce); pass.set_bind_group(0, &bgl_inst, &[]);
            pass.dispatch_workgroups(1, 1, 1);
        }
        // stage 4b: compact
        {
            let mut pass = enc.begin_compute_pass(&wgpu::ComputePassDescriptor { label: Some("p4b"), timestamp_writes: None });
            pass.set_pipeline(&p_compact); pass.set_bind_group(0, &bgl_inst, &[]);
            pass.dispatch_workgroups(1, 1, 1);
        }
        // blit-out: finalOut -> readback
        enc.copy_buffer_to_buffer(&final_out, 0, &readback, 0, (N * bpp) as u64);
        queue.submit(Some(enc.finish()));
    }

    let slice = readback.slice(..);
    let (tx, rx) = std::sync::mpsc::channel();
    slice.map_async(wgpu::MapMode::Read, move |r| tx.send(r).unwrap());
    device.poll(wgpu::PollType::Wait { submission_index: None, timeout: None }).unwrap();
    rx.recv().unwrap().unwrap();
    let data = slice.get_mapped_range().unwrap();
    let out: &[Term] = bytemuck::cast_slice(&data);
    // CPU reference: full pipeline (product, fold by (qe,te), drop zeros), sorted by key
    let mut m: BTreeMap<(u32, u32), i64> = BTreeMap::new();
    for i in 0..8 {
        if a_terms[i].valid == 0 { continue; }
        for j in 0..8 {
            if b_terms[j].valid == 0 { continue; }
            let k = (a_terms[i].qe + b_terms[j].qe, a_terms[i].te + b_terms[j].te);
            *m.entry(k).or_insert(0) += a_terms[i].coef as i64 * b_terms[j].coef as i64;
        }
    }
    m.retain(|_, v| *v != 0);
    let refs: Vec<((u32, u32), i64)> = m.into_iter().collect();
    let gpu: Vec<((u32, u32), i64)> = out.iter().take(N as usize)
        .filter(|t| t.valid == 1 && t.coef != 0)
        .map(|t| ((t.qe, t.te), t.coef as i64)).collect();
    let mut mismatch = 0;
    if gpu.len() != refs.len() { mismatch = refs.len().abs_diff(gpu.len()); }
    for (i, (r, g)) in refs.iter().zip(gpu.iter()).enumerate() {
        if r != g { mismatch += 1; println!("  pos {i}: ref {:?} vs gpu {:?}", r, g); }
    }
    println!("products: {} -> reduced terms: CPU {} / GPU {}", 28, refs.len(), gpu.len());
    println!("mismatches: {}", mismatch);
    if mismatch == 0 && gpu.len() == refs.len() {
        println!("FULL PIPELINE OK: product + sort + reduce + compact on-device, byte-verified");
    } else {
        std::process::exit(1);
    }
}
