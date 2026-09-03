// HTTP WebGPU Laurent daemon with observable occupancy.
//
// The daemon preserves the existing /health and /compute contract and adds
// GET /status so schedulers can observe whether the single GPU critical
// section is active or has queued work. Status is observational, not a lease:
// callers that require exclusion still need an atomic reservation protocol.
//
// Controlled-spawn metadata is deliberately narrow. Successful daemon startup
// establishes a working WebGPU API surface; it does not establish control of
// the host kernel, container runtime, physical accelerator, or provider.

use std::collections::BTreeMap;
use std::env;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use wgpu::util::DeviceExt;

const WIDTH: usize = 8;
const N: u32 = 64;
const MAX_BODY_BYTES: usize = 1 << 20;
const SPAWN_SEMANTICS_VERSION: u32 = 1;
const ACCESSIBILITY_PROFILE: &str = "sandbox";

struct DebugTrace {
    enabled: bool,
}

impl DebugTrace {
    fn from_args_and_env() -> Self {
        let env_enabled = env::var("BLITTER_DEBUG")
            .map(|value| {
                matches!(
                    value.to_ascii_lowercase().as_str(),
                    "1" | "true" | "yes" | "on"
                )
            })
            .unwrap_or(false);
        let arg_enabled = env::args().skip(1).any(|arg| arg == "--debug");
        Self {
            enabled: env_enabled || arg_enabled,
        }
    }

    fn event(&self, event: &str, fields: &str) {
        if !self.enabled {
            return;
        }
        if fields.is_empty() {
            eprintln!(
                "blitter-debug {{\"event\":\"{}\"}}",
                json_escape(event)
            );
        } else {
            eprintln!(
                "blitter-debug {{\"event\":\"{}\",{}}}",
                json_escape(event),
                fields
            );
        }
    }
}

#[repr(C)]
#[derive(Copy, Clone, Debug, Default, PartialEq, Eq, bytemuck::Pod, bytemuck::Zeroable)]
struct Term {
    qe: u32,
    te: u32,
    coef: i32,
    valid: u32,
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
struct Triple {
    qe: u32,
    te: u32,
    coef: i32,
}

struct GpuContext {
    device: wgpu::Device,
    queue: wgpu::Queue,
    adapter_name: String,
    backend_name: String,
    device_type: String,
    adapter_label: String,
    gate: Mutex<()>,
}

struct RuntimeState {
    started: Instant,
    started_unix_ms: u64,
    active_compute: AtomicU64,
    queued_compute: AtomicU64,
    compute_requests_total: AtomicU64,
    completed_compute: AtomicU64,
    failed_compute: AtomicU64,
    transition_seq: AtomicU64,
    current_job_started_unix_ms: Mutex<Option<u64>>,
    last_job_finished_unix_ms: AtomicU64,
}

impl RuntimeState {
    fn new() -> Self {
        Self {
            started: Instant::now(),
            started_unix_ms: unix_ms(),
            active_compute: AtomicU64::new(0),
            queued_compute: AtomicU64::new(0),
            compute_requests_total: AtomicU64::new(0),
            completed_compute: AtomicU64::new(0),
            failed_compute: AtomicU64::new(0),
            transition_seq: AtomicU64::new(0),
            current_job_started_unix_ms: Mutex::new(None),
            last_job_finished_unix_ms: AtomicU64::new(0),
        }
    }

    fn bump(&self) {
        self.transition_seq.fetch_add(1, Ordering::AcqRel);
    }

    fn begin_request(&self) -> u64 {
        self.compute_requests_total.fetch_add(1, Ordering::AcqRel) + 1
    }

    fn enter_queue(&self) {
        self.queued_compute.fetch_add(1, Ordering::AcqRel);
        self.bump();
    }

    fn leave_queue(&self) {
        self.queued_compute.fetch_sub(1, Ordering::AcqRel);
        self.bump();
    }

    fn begin_active(&self) {
        self.active_compute.fetch_add(1, Ordering::AcqRel);
        if let Ok(mut slot) = self.current_job_started_unix_ms.lock() {
            *slot = Some(unix_ms());
        }
        self.bump();
    }

    fn finish_active(&self, success: bool) {
        self.active_compute.fetch_sub(1, Ordering::AcqRel);
        if success {
            self.completed_compute.fetch_add(1, Ordering::AcqRel);
        } else {
            self.failed_compute.fetch_add(1, Ordering::AcqRel);
        }
        if let Ok(mut slot) = self.current_job_started_unix_ms.lock() {
            *slot = None;
        }
        self.last_job_finished_unix_ms
            .store(unix_ms(), Ordering::Release);
        self.bump();
    }

    fn record_failed_request(&self) {
        self.failed_compute.fetch_add(1, Ordering::AcqRel);
        self.bump();
    }

    fn is_busy(&self) -> bool {
        self.active_compute.load(Ordering::Acquire) != 0
            || self.queued_compute.load(Ordering::Acquire) != 0
    }

    fn status_json(&self, gpu: &GpuContext) -> String {
        let active = self.active_compute.load(Ordering::Acquire);
        let queued = self.queued_compute.load(Ordering::Acquire);
        let busy = active != 0 || queued != 0;
        let current = self
            .current_job_started_unix_ms
            .lock()
            .ok()
            .and_then(|slot| *slot);
        let last = self.last_job_finished_unix_ms.load(Ordering::Acquire);
        format!(
            concat!(
                "{{\"ok\":true,\"service\":\"webgpu-blitter\",",
                "\"busy\":{},\"idle\":{},",
                "\"active_compute\":{},\"queued_compute\":{},",
                "\"max_concurrent_compute\":1,",
                "\"compute_requests_total\":{},",
                "\"completed_compute\":{},\"failed_compute\":{},",
                "\"status_seq\":{},",
                "\"started_unix_ms\":{},\"uptime_ms\":{},",
                "\"current_job_started_unix_ms\":{},",
                "\"last_job_finished_unix_ms\":{},",
                "\"adapter\":\"{}\",\"backend\":\"{}\",\"device_type\":\"{}\",{}}}"
            ),
            busy,
            !busy,
            active,
            queued,
            self.compute_requests_total.load(Ordering::Acquire),
            self.completed_compute.load(Ordering::Acquire),
            self.failed_compute.load(Ordering::Acquire),
            self.transition_seq.load(Ordering::Acquire),
            self.started_unix_ms,
            self.started.elapsed().as_millis(),
            json_optional_u64(current),
            if last == 0 { "null".to_string() } else { last.to_string() },
            json_escape(&gpu.adapter_name),
            json_escape(&gpu.backend_name),
            json_escape(&gpu.device_type),
            spawn_contract_json_fields(),
        )
    }
}

struct ActiveGuard {
    state: Arc<RuntimeState>,
    success: bool,
}

impl ActiveGuard {
    fn new(state: Arc<RuntimeState>) -> Self {
        state.begin_active();
        Self {
            state,
            success: false,
        }
    }

    fn mark_success(&mut self) {
        self.success = true;
    }
}

impl Drop for ActiveGuard {
    fn drop(&mut self) {
        self.state.finish_active(self.success);
    }
}

fn unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .min(u64::MAX as u128) as u64
}

fn json_optional_u64(value: Option<u64>) -> String {
    value.map_or_else(|| "null".to_string(), |v| v.to_string())
}

fn json_escape(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    for ch in input.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c.is_control() => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

fn spawn_contract_json_fields() -> String {
    format!(
        concat!(
            "\"spawn_semantics_version\":{},",
            "\"accessibility_profile\":\"{}\",",
            "\"controlled_subsystems\":[\"accelerator-api\"],",
            "\"execution_surfaces\":[\"webgpu\"],",
            "\"slot_capabilities\":[",
            "\"exact-i32\",\"laurent-product-v1\",",
            "\"surface:webgpu\",\"webgpu-blitter\"]"
        ),
        SPAWN_SEMANTICS_VERSION,
        ACCESSIBILITY_PROFILE,
    )
}

fn adapter_priority(device_type: wgpu::DeviceType) -> u8 {
    match device_type {
        wgpu::DeviceType::DiscreteGpu => 0,
        wgpu::DeviceType::IntegratedGpu => 1,
        wgpu::DeviceType::VirtualGpu => 2,
        wgpu::DeviceType::Other => 3,
        wgpu::DeviceType::Cpu => 4,
    }
}

fn request_device(adapter: &wgpu::Adapter) -> Result<(wgpu::Device, wgpu::Queue), String> {
    pollster::block_on(adapter.request_device(&wgpu::DeviceDescriptor {
        label: Some("blitter-device"),
        required_features: wgpu::Features::empty(),
        required_limits: wgpu::Limits::default(),
        experimental_features: wgpu::ExperimentalFeatures::default(),
        memory_hints: wgpu::MemoryHints::Performance,
        trace: wgpu::Trace::Off,
    }))
    .map_err(|err| format!("request_device: {err}"))
}

fn init_gpu(debug: &DebugTrace) -> Result<GpuContext, String> {
    let instance = wgpu::Instance::new(wgpu::InstanceDescriptor {
        backends: wgpu::Backends::all(),
        flags: wgpu::InstanceFlags::default(),
        memory_budget_thresholds: wgpu::MemoryBudgetThresholds::default(),
        backend_options: wgpu::BackendOptions::default(),
        display: None,
    });
    let wanted = env::var("BLITTER_ADAPTER")
        .ok()
        .filter(|value| !value.is_empty());
    let adapters: Vec<_> = pollster::block_on(instance.enumerate_adapters(wgpu::Backends::all()));
    debug.event(
        "gpu.adapters_enumerated",
        &format!("\"count\":{}", adapters.len()),
    );
    let mut candidates: Vec<_> = adapters
        .into_iter()
        .filter(|adapter| {
            wanted.as_ref().is_none_or(|want| {
                adapter
                    .get_info()
                    .name
                    .to_lowercase()
                    .contains(&want.to_lowercase())
            })
        })
        .collect();
    if candidates.is_empty() {
        let error = wanted.as_ref().map_or_else(
            || "no WebGPU adapter".to_string(),
            |want| format!("no adapter matching BLITTER_ADAPTER={want}"),
        );
        debug.event("gpu.adapter_missing", &format!("\"reason\":\"{}\"", json_escape(&error)));
        return Err(error);
    }
    if wanted.is_none() {
        candidates.sort_by_key(|adapter| adapter_priority(adapter.get_info().device_type));
    }

    let mut selected = None;
    for adapter in candidates {
        let info = adapter.get_info();
        let backend_name = format!("{:?}", info.backend);
        let adapter_name = info.name.clone();
        let device_type = format!("{:?}", info.device_type);
        let priority = adapter_priority(info.device_type);
        debug.event(
            "gpu.adapter_candidate",
            &format!(
                "\"adapter\":\"{}\",\"backend\":\"{}\",\"device_type\":\"{}\",\"priority\":{}",
                json_escape(&adapter_name),
                json_escape(&backend_name),
                json_escape(&device_type),
                priority
            ),
        );
        match request_device(&adapter) {
            Ok((device, queue)) => {
                let adapter_label = format!("{} ({})", adapter_name, backend_name);
                selected = Some((
                    adapter_name,
                    backend_name,
                    device_type,
                    adapter_label,
                    device,
                    queue,
                ));
                break;
            }
            Err(error) => {
                debug.event(
                    "gpu.adapter_rejected",
                    &format!(
                        "\"adapter\":\"{}\",\"reason\":\"{}\"",
                        json_escape(&adapter_name),
                        json_escape(&error)
                    ),
                );
            }
        }
    }
    let (adapter_name, backend_name, device_type, adapter_label, device, queue) = selected
        .ok_or_else(|| "no usable WebGPU adapter".to_string())?;
    debug.event(
        "gpu.adapter_selected",
        &format!(
            "\"adapter\":\"{}\",\"backend\":\"{}\",\"device_type\":\"{}\"",
            json_escape(&adapter_name),
            json_escape(&backend_name),
            json_escape(&device_type)
        ),
    );
    debug.event("gpu.device_ready", "");
    Ok(GpuContext {
        device,
        queue,
        adapter_name,
        backend_name,
        device_type,
        adapter_label,
        gate: Mutex::new(()),
    })
}

fn exact_reference(a: &[Triple], b: &[Triple]) -> Result<Vec<Triple>, String> {
    let mut acc: BTreeMap<(u32, u32), i128> = BTreeMap::new();
    for left in a {
        for right in b {
            let qe = left
                .qe
                .checked_add(right.qe)
                .ok_or_else(|| "q exponent overflow".to_string())?;
            let te = left
                .te
                .checked_add(right.te)
                .ok_or_else(|| "t exponent overflow".to_string())?;
            let product = (left.coef as i128) * (right.coef as i128);
            if product < i32::MIN as i128 || product > i32::MAX as i128 {
                return Err("coefficient product exceeds exact i32 GPU domain".to_string());
            }
            *acc.entry((qe, te)).or_insert(0) += product;
        }
    }
    let mut out = Vec::new();
    for ((qe, te), coef) in acc {
        if coef == 0 {
            continue;
        }
        if coef < i32::MIN as i128 || coef > i32::MAX as i128 {
            return Err("reduced coefficient exceeds exact i32 GPU domain".to_string());
        }
        out.push(Triple {
            qe,
            te,
            coef: coef as i32,
        });
    }
    Ok(out)
}

fn padded_terms(input: &[Triple]) -> Result<[Term; WIDTH], String> {
    if input.len() > WIDTH {
        return Err(format!("at most {WIDTH} terms are supported per factor"));
    }
    let mut out = [Term::default(); WIDTH];
    for (slot, term) in out.iter_mut().zip(input.iter()) {
        *slot = Term {
            qe: term.qe,
            te: term.te,
            coef: term.coef,
            valid: 1,
        };
    }
    Ok(out)
}

fn gpu_product(gpu: &GpuContext, a: &[Triple], b: &[Triple]) -> Result<Vec<Triple>, String> {
    let a_terms = padded_terms(a)?;
    let b_terms = padded_terms(b)?;
    let device = &gpu.device;
    let queue = &gpu.queue;
    let bpp: u32 = 16;

    let tex_a = device.create_texture(&wgpu::TextureDescriptor {
        label: Some("surface-A"),
        size: wgpu::Extent3d {
            width: WIDTH as u32,
            height: 1,
            depth_or_array_layers: 1,
        },
        mip_level_count: 1,
        sample_count: 1,
        dimension: wgpu::TextureDimension::D2,
        format: wgpu::TextureFormat::Rgba32Uint,
        usage: wgpu::TextureUsages::TEXTURE_BINDING | wgpu::TextureUsages::COPY_DST,
        view_formats: &[],
    });
    let tex_b = device.create_texture(&wgpu::TextureDescriptor {
        label: Some("surface-B"),
        size: wgpu::Extent3d {
            width: N,
            height: 1,
            depth_or_array_layers: 1,
        },
        mip_level_count: 1,
        sample_count: 1,
        dimension: wgpu::TextureDimension::D2,
        format: wgpu::TextureFormat::Rgba32Uint,
        usage: wgpu::TextureUsages::STORAGE_BINDING | wgpu::TextureUsages::COPY_SRC,
        view_formats: &[],
    });
    queue.write_texture(
        wgpu::TexelCopyTextureInfo {
            texture: &tex_a,
            mip_level: 0,
            origin: wgpu::Origin3d::ZERO,
            aspect: wgpu::TextureAspect::All,
        },
        bytemuck::cast_slice(&a_terms),
        wgpu::TexelCopyBufferLayout {
            offset: 0,
            bytes_per_row: Some(WIDTH as u32 * bpp),
            rows_per_image: Some(1),
        },
        wgpu::Extent3d {
            width: WIDTH as u32,
            height: 1,
            depth_or_array_layers: 1,
        },
    );

    let shader = device.create_shader_module(wgpu::ShaderModuleDescriptor {
        label: Some("laurent-pipeline"),
        source: wgpu::ShaderSource::Wgsl(include_str!("../blitter.wgsl").into()),
    });
    let bgl = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
        label: Some("bgl"),
        entries: &[
            wgpu::BindGroupLayoutEntry { binding: 0, visibility: wgpu::ShaderStages::COMPUTE, ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Storage { read_only: true }, has_dynamic_offset: false, min_binding_size: None }, count: None },
            wgpu::BindGroupLayoutEntry { binding: 1, visibility: wgpu::ShaderStages::COMPUTE, ty: wgpu::BindingType::Texture { sample_type: wgpu::TextureSampleType::Uint, view_dimension: wgpu::TextureViewDimension::D2, multisampled: false }, count: None },
            wgpu::BindGroupLayoutEntry { binding: 2, visibility: wgpu::ShaderStages::COMPUTE, ty: wgpu::BindingType::StorageTexture { access: wgpu::StorageTextureAccess::WriteOnly, format: wgpu::TextureFormat::Rgba32Uint, view_dimension: wgpu::TextureViewDimension::D2 }, count: None },
            wgpu::BindGroupLayoutEntry { binding: 3, visibility: wgpu::ShaderStages::COMPUTE, ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Storage { read_only: false }, has_dynamic_offset: false, min_binding_size: None }, count: None },
            wgpu::BindGroupLayoutEntry { binding: 4, visibility: wgpu::ShaderStages::COMPUTE, ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Storage { read_only: false }, has_dynamic_offset: false, min_binding_size: None }, count: None },
            wgpu::BindGroupLayoutEntry { binding: 5, visibility: wgpu::ShaderStages::COMPUTE, ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Storage { read_only: false }, has_dynamic_offset: false, min_binding_size: None }, count: None },
            wgpu::BindGroupLayoutEntry { binding: 6, visibility: wgpu::ShaderStages::COMPUTE, ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Storage { read_only: false }, has_dynamic_offset: false, min_binding_size: None }, count: None },
        ],
    });
    let layout = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
        label: Some("pl"),
        bind_group_layouts: &[Some(&bgl)],
        immediate_size: 0,
    });
    let make_pipeline = |label: &str, entry: &str| {
        device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
            label: Some(label),
            layout: Some(&layout),
            module: &shader,
            entry_point: Some(entry),
            compilation_options: Default::default(),
            cache: None,
        })
    };
    let p_product = make_pipeline("product", "product_pass");
    let p_sort = make_pipeline("sort", "sort_pass");
    let p_reduce = make_pipeline("reduce", "reduce_pass");
    let p_compact = make_pipeline("compact", "compact_pass");

    let buf_b = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
        label: Some("terms-B"),
        contents: bytemuck::cast_slice(&b_terms),
        usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC,
    });
    let prods = device.create_buffer(&wgpu::BufferDescriptor {
        label: Some("prods"),
        size: (N * bpp) as u64,
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
        label: Some("counters"),
        contents: bytemuck::bytes_of(&[0u32, 0u32, 0u32, 0u32]),
        usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
    });
    let readback = device.create_buffer(&wgpu::BufferDescriptor {
        label: Some("readback"),
        size: (N * bpp) as u64,
        usage: wgpu::BufferUsages::COPY_DST | wgpu::BufferUsages::MAP_READ,
        mapped_at_creation: false,
    });
    let bind_group = device.create_bind_group(&wgpu::BindGroupDescriptor {
        label: Some("bg"),
        layout: &bgl,
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

    let mut encoder = device.create_command_encoder(&wgpu::CommandEncoderDescriptor {
        label: Some("enc"),
    });
    {
        let mut pass = encoder.begin_compute_pass(&wgpu::ComputePassDescriptor { label: Some("p1"), timestamp_writes: None });
        pass.set_pipeline(&p_product);
        pass.set_bind_group(0, &bind_group, &[]);
        pass.dispatch_workgroups(N, 1, 1);
    }
    encoder.copy_texture_to_buffer(
        wgpu::TexelCopyTextureInfo { texture: &tex_b, mip_level: 0, origin: wgpu::Origin3d::ZERO, aspect: wgpu::TextureAspect::All },
        wgpu::TexelCopyBufferInfo { buffer: &prods, layout: wgpu::TexelCopyBufferLayout { offset: 0, bytes_per_row: Some(N * bpp), rows_per_image: Some(1) } },
        wgpu::Extent3d { width: N, height: 1, depth_or_array_layers: 1 },
    );
    queue.write_buffer(&counters, 0, bytemuck::bytes_of(&[0u32, 0u32, 0u32, 0u32]));
    for (label, pipeline) in [("p3", &p_sort), ("p4a", &p_reduce), ("p4b", &p_compact)] {
        let mut pass = encoder.begin_compute_pass(&wgpu::ComputePassDescriptor { label: Some(label), timestamp_writes: None });
        pass.set_pipeline(pipeline);
        pass.set_bind_group(0, &bind_group, &[]);
        pass.dispatch_workgroups(1, 1, 1);
    }
    encoder.copy_buffer_to_buffer(&final_out, 0, &readback, 0, (N * bpp) as u64);
    queue.submit(Some(encoder.finish()));

    let slice = readback.slice(..);
    let (tx, rx) = std::sync::mpsc::channel();
    slice.map_async(wgpu::MapMode::Read, move |result| {
        let _ = tx.send(result);
    });
    device
        .poll(wgpu::PollType::Wait { submission_index: None, timeout: None })
        .map_err(|err| format!("device poll: {err}"))?;
    rx.recv()
        .map_err(|err| format!("map callback: {err}"))?
        .map_err(|err| format!("map readback: {err}"))?;
    let data = slice.get_mapped_range().map_err(|err| format!("mapped range: {err}"))?;
    let raw: &[Term] = bytemuck::cast_slice(&data);
    let mut out: Vec<Triple> = raw
        .iter()
        .take(N as usize)
        .filter(|term| term.valid == 1 && term.coef != 0)
        .map(|term| Triple { qe: term.qe, te: term.te, coef: term.coef })
        .collect();
    drop(data);
    readback.unmap();
    out.sort_by_key(|term| (term.qe, term.te));
    Ok(out)
}

fn compute_exact(gpu: &GpuContext, a: &[Triple], b: &[Triple]) -> Result<Vec<Triple>, String> {
    let expected = exact_reference(a, b)?;
    let actual = gpu_product(gpu, a, b)?;
    if actual != expected {
        return Err(format!(
            "GPU/CPU exactness mismatch: expected {} terms, got {}",
            expected.len(),
            actual.len()
        ));
    }
    Ok(actual)
}

struct JsonCursor<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> JsonCursor<'a> {
    fn new(input: &'a str) -> Self {
        Self { bytes: input.as_bytes(), pos: 0 }
    }

    fn skip_ws(&mut self) {
        while self.pos < self.bytes.len() && self.bytes[self.pos].is_ascii_whitespace() {
            self.pos += 1;
        }
    }

    fn peek(&mut self) -> Option<u8> {
        self.skip_ws();
        self.bytes.get(self.pos).copied()
    }

    fn expect(&mut self, byte: u8) -> Result<(), String> {
        self.skip_ws();
        if self.bytes.get(self.pos).copied() == Some(byte) {
            self.pos += 1;
            Ok(())
        } else {
            Err(format!("expected '{}' at byte {}", byte as char, self.pos))
        }
    }

    fn string(&mut self) -> Result<String, String> {
        self.expect(b'"')?;
        let mut out = String::new();
        while self.pos < self.bytes.len() {
            let byte = self.bytes[self.pos];
            self.pos += 1;
            match byte {
                b'"' => return Ok(out),
                b'\\' => {
                    let escaped = *self.bytes.get(self.pos).ok_or_else(|| "truncated escape".to_string())?;
                    self.pos += 1;
                    match escaped {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{0008}'),
                        b'f' => out.push('\u{000c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        _ => return Err("unsupported JSON string escape".to_string()),
                    }
                }
                b if b.is_ascii_control() => return Err("control byte in JSON string".to_string()),
                b => out.push(b as char),
            }
        }
        Err("unterminated JSON string".to_string())
    }

    fn integer(&mut self) -> Result<i64, String> {
        self.skip_ws();
        let start = self.pos;
        if self.bytes.get(self.pos) == Some(&b'-') {
            self.pos += 1;
        }
        let digits = self.pos;
        while self.pos < self.bytes.len() && self.bytes[self.pos].is_ascii_digit() {
            self.pos += 1;
        }
        if self.pos == digits {
            return Err(format!("expected integer at byte {start}"));
        }
        let text = std::str::from_utf8(&self.bytes[start..self.pos]).map_err(|err| err.to_string())?;
        text.parse::<i64>().map_err(|err| format!("integer parse: {err}"))
    }

    fn term_array(&mut self) -> Result<Vec<Triple>, String> {
        self.expect(b'[')?;
        let mut out = Vec::new();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(out);
        }
        loop {
            self.expect(b'[')?;
            let qe = self.integer()?;
            self.expect(b',')?;
            let te = self.integer()?;
            self.expect(b',')?;
            let coef = self.integer()?;
            self.expect(b']')?;
            if qe < 0 || qe > u32::MAX as i64 || te < 0 || te > u32::MAX as i64 {
                return Err("exponents must be u32".to_string());
            }
            if coef < i32::MIN as i64 || coef > i32::MAX as i64 {
                return Err("coefficient must be i32".to_string());
            }
            out.push(Triple { qe: qe as u32, te: te as u32, coef: coef as i32 });
            if out.len() > WIDTH {
                return Err(format!("at most {WIDTH} terms are supported per factor"));
            }
            match self.peek() {
                Some(b',') => { self.pos += 1; }
                Some(b']') => { self.pos += 1; break; }
                _ => return Err(format!("expected ',' or ']' at byte {}", self.pos)),
            }
        }
        Ok(out)
    }
}

fn parse_compute_request(body: &str) -> Result<(Vec<Triple>, Vec<Triple>), String> {
    let mut cursor = JsonCursor::new(body);
    cursor.expect(b'{')?;
    let mut a = None;
    let mut b = None;
    if cursor.peek() == Some(b'}') {
        return Err("compute object must contain a and b".to_string());
    }
    loop {
        let key = cursor.string()?;
        cursor.expect(b':')?;
        match key.as_str() {
            "a" => {
                if a.is_some() { return Err("duplicate a field".to_string()); }
                a = Some(cursor.term_array()?);
            }
            "b" => {
                if b.is_some() { return Err("duplicate b field".to_string()); }
                b = Some(cursor.term_array()?);
            }
            _ => return Err(format!("unsupported compute field: {key}")),
        }
        match cursor.peek() {
            Some(b',') => { cursor.pos += 1; }
            Some(b'}') => { cursor.pos += 1; break; }
            _ => return Err(format!("expected ',' or '}}' at byte {}", cursor.pos)),
        }
    }
    cursor.skip_ws();
    if cursor.pos != cursor.bytes.len() {
        return Err("trailing bytes after compute JSON".to_string());
    }
    Ok((a.ok_or_else(|| "missing a field".to_string())?, b.ok_or_else(|| "missing b field".to_string())?))
}

struct HttpRequest {
    method: String,
    path: String,
    body: String,
}

fn read_http_request(stream: &mut TcpStream) -> Result<HttpRequest, String> {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(30)));
    let mut bytes = Vec::new();
    let mut buffer = [0u8; 4096];
    let header_end = loop {
        if bytes.len() > 64 * 1024 {
            return Err("HTTP headers too large".to_string());
        }
        let read = stream.read(&mut buffer).map_err(|err| format!("read request: {err}"))?;
        if read == 0 {
            return Err("connection closed before headers".to_string());
        }
        bytes.extend_from_slice(&buffer[..read]);
        if let Some(index) = bytes.windows(4).position(|window| window == b"\r\n\r\n") {
            break index + 4;
        }
    };
    let header_text = std::str::from_utf8(&bytes[..header_end]).map_err(|err| format!("HTTP headers UTF-8: {err}"))?;
    let mut lines = header_text.split("\r\n");
    let request_line = lines.next().ok_or_else(|| "missing request line".to_string())?;
    let mut parts = request_line.split_whitespace();
    let method = parts.next().ok_or_else(|| "missing HTTP method".to_string())?.to_string();
    let raw_path = parts.next().ok_or_else(|| "missing HTTP path".to_string())?;
    let path = raw_path.split('?').next().unwrap_or(raw_path).to_string();
    let mut content_length = 0usize;
    for line in lines {
        if let Some((name, value)) = line.split_once(':') {
            if name.trim().eq_ignore_ascii_case("content-length") {
                content_length = value.trim().parse::<usize>().map_err(|err| format!("Content-Length: {err}"))?;
            }
        }
    }
    if content_length > MAX_BODY_BYTES {
        return Err(format!("request body exceeds {MAX_BODY_BYTES} bytes"));
    }
    let body_start = header_end;
    while bytes.len() - body_start < content_length {
        let read = stream.read(&mut buffer).map_err(|err| format!("read body: {err}"))?;
        if read == 0 {
            return Err("connection closed before body".to_string());
        }
        bytes.extend_from_slice(&buffer[..read]);
        if bytes.len() - body_start > MAX_BODY_BYTES {
            return Err("request body too large".to_string());
        }
    }
    let body = std::str::from_utf8(&bytes[body_start..body_start + content_length])
        .map_err(|err| format!("request body UTF-8: {err}"))?
        .to_string();
    Ok(HttpRequest { method, path, body })
}

fn write_json(stream: &mut TcpStream, status: u16, reason: &str, body: &str) {
    let header = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n",
        body.len()
    );
    let _ = stream.write_all(header.as_bytes());
    let _ = stream.write_all(body.as_bytes());
    let _ = stream.flush();
}

fn terms_json(terms: &[Triple]) -> String {
    terms
        .iter()
        .map(|term| format!("[{},{},{}]", term.qe, term.te, term.coef))
        .collect::<Vec<_>>()
        .join(",")
}

fn error_json(gpu: &GpuContext, error: &str) -> String {
    format!(
        "{{\"ok\":false,\"terms\":[],\"adapter\":\"{}\",\"error\":\"{}\"}}",
        json_escape(&gpu.adapter_label),
        json_escape(error)
    )
}

fn handle_compute(
    mut stream: TcpStream,
    request: HttpRequest,
    gpu: Arc<GpuContext>,
    state: Arc<RuntimeState>,
    debug: &DebugTrace,
) {
    let request_id = state.begin_request();
    let started = Instant::now();
    debug.event(
        "compute.received",
        &format!(
            "\"request_id\":{},\"body_bytes\":{}",
            request_id,
            request.body.len()
        ),
    );
    let (a, b) = match parse_compute_request(&request.body) {
        Ok(parsed) => parsed,
        Err(error) => {
            state.record_failed_request();
            debug.event(
                "compute.rejected",
                &format!(
                    "\"request_id\":{},\"reason\":\"{}\"",
                    request_id,
                    json_escape(&error)
                ),
            );
            write_json(&mut stream, 400, "Bad Request", &error_json(&gpu, &error));
            return;
        }
    };
    debug.event(
        "compute.parsed",
        &format!(
            "\"request_id\":{},\"a_terms\":{},\"b_terms\":{}",
            request_id,
            a.len(),
            b.len()
        ),
    );

    state.enter_queue();
    debug.event("compute.queued", &format!("\"request_id\":{}", request_id));
    let gpu_lock = match gpu.gate.lock() {
        Ok(lock) => lock,
        Err(_) => {
            state.leave_queue();
            state.record_failed_request();
            debug.event(
                "compute.lock_error",
                &format!("\"request_id\":{}", request_id),
            );
            write_json(&mut stream, 500, "Internal Server Error", &error_json(&gpu, "GPU compute lock poisoned"));
            return;
        }
    };
    state.leave_queue();
    let mut active = ActiveGuard::new(state);
    debug.event(
        "compute.dispatch_started",
        &format!("\"request_id\":{}", request_id),
    );
    let result = compute_exact(&gpu, &a, &b);
    drop(gpu_lock);
    match result {
        Ok(terms) => {
            active.mark_success();
            debug.event(
                "compute.completed",
                &format!(
                    "\"request_id\":{},\"status\":200,\"terms\":{},\"duration_ms\":{}",
                    request_id,
                    terms.len(),
                    started.elapsed().as_millis()
                ),
            );
            let body = format!(
                "{{\"ok\":true,\"terms\":[{}],\"adapter\":\"{}\",\"error\":null}}",
                terms_json(&terms),
                json_escape(&gpu.adapter_label)
            );
            write_json(&mut stream, 200, "OK", &body);
        }
        Err(error) => {
            debug.event(
                "compute.failed",
                &format!(
                    "\"request_id\":{},\"status\":500,\"duration_ms\":{},\"reason\":\"{}\"",
                    request_id,
                    started.elapsed().as_millis(),
                    json_escape(&error)
                ),
            );
            write_json(&mut stream, 500, "Internal Server Error", &error_json(&gpu, &error));
        }
    }
}

fn handle_connection(
    mut stream: TcpStream,
    gpu: Arc<GpuContext>,
    state: Arc<RuntimeState>,
    debug: Arc<DebugTrace>,
) {
    let request = match read_http_request(&mut stream) {
        Ok(request) => request,
        Err(error) => {
            debug.event(
                "http.rejected",
                &format!("\"reason\":\"{}\"", json_escape(&error)),
            );
            write_json(&mut stream, 400, "Bad Request", &format!("{{\"ok\":false,\"error\":\"{}\"}}", json_escape(&error)));
            return;
        }
    };
    debug.event(
        "http.request",
        &format!(
            "\"method\":\"{}\",\"path\":\"{}\"",
            json_escape(&request.method),
            json_escape(&request.path)
        ),
    );
    match (request.method.as_str(), request.path.as_str()) {
        ("GET", "/health") => {
            let body = format!(
                "{{\"ok\":true,\"adapter\":\"{}\",\"device_type\":\"{}\"}}",
                json_escape(&gpu.adapter_label),
                json_escape(&gpu.device_type)
            );
            write_json(&mut stream, 200, "OK", &body);
        }
        ("GET", "/status") => {
            write_json(&mut stream, 200, "OK", &state.status_json(&gpu));
        }
        ("POST", "/compute") => handle_compute(stream, request, gpu, state, &debug),
        _ => write_json(&mut stream, 404, "Not Found", "{\"ok\":false,\"error\":\"not found\"}"),
    }
}

fn main() {
    let debug = Arc::new(DebugTrace::from_args_and_env());
    debug.event("daemon.starting", "");
    let gpu = Arc::new(init_gpu(&debug).unwrap_or_else(|error| {
        debug.event(
            "daemon.startup_failed",
            &format!("\"reason\":\"{}\"", json_escape(&error)),
        );
        eprintln!("blitter-daemon: {error}");
        std::process::exit(1);
    }));
    let state = Arc::new(RuntimeState::new());
    let bind = env::var("BLITTER_BIND").unwrap_or_else(|_| "127.0.0.1:8791".to_string());
    let listener = TcpListener::bind(&bind).unwrap_or_else(|error| {
        debug.event(
            "daemon.bind_failed",
            &format!(
                "\"bind\":\"{}\",\"reason\":\"{}\"",
                json_escape(&bind),
                json_escape(&error.to_string())
            ),
        );
        eprintln!("blitter-daemon: bind {bind}: {error}");
        std::process::exit(1);
    });
    eprintln!("blitter-daemon: listening on {bind}; adapter={}", gpu.adapter_label);
    debug.event(
        "daemon.listening",
        &format!("\"bind\":\"{}\"", json_escape(&bind)),
    );
    for incoming in listener.incoming() {
        match incoming {
            Ok(stream) => {
                let gpu = Arc::clone(&gpu);
                let state = Arc::clone(&state);
                let debug = Arc::clone(&debug);
                std::thread::spawn(move || handle_connection(stream, gpu, state, debug));
            }
            Err(error) => {
                debug.event(
                    "http.accept_failed",
                    &format!("\"reason\":\"{}\"", json_escape(&error.to_string())),
                );
                eprintln!("blitter-daemon: accept: {error}");
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn adapter_priority_prefers_hardware_over_cpu() {
        assert!(adapter_priority(wgpu::DeviceType::DiscreteGpu) < adapter_priority(wgpu::DeviceType::Cpu));
        assert!(adapter_priority(wgpu::DeviceType::IntegratedGpu) < adapter_priority(wgpu::DeviceType::Cpu));
        assert!(adapter_priority(wgpu::DeviceType::VirtualGpu) < adapter_priority(wgpu::DeviceType::Cpu));
    }

    #[test]
    fn parses_compute_contract() {
        let (a, b) = parse_compute_request(r#"{"a":[[0,0,1],[1,0,-2]],"b":[[0,1,3]]}"#).unwrap();
        assert_eq!(a, vec![Triple { qe: 0, te: 0, coef: 1 }, Triple { qe: 1, te: 0, coef: -2 }]);
        assert_eq!(b, vec![Triple { qe: 0, te: 1, coef: 3 }]);
    }

    #[test]
    fn occupancy_transitions_are_fail_closed() {
        let state = RuntimeState::new();
        assert!(!state.is_busy());
        state.enter_queue();
        assert!(state.is_busy());
        assert_eq!(state.queued_compute.load(Ordering::Acquire), 1);
        state.leave_queue();
        state.begin_active();
        assert!(state.is_busy());
        assert_eq!(state.active_compute.load(Ordering::Acquire), 1);
        state.finish_active(true);
        assert!(!state.is_busy());
        assert_eq!(state.completed_compute.load(Ordering::Acquire), 1);
        assert_eq!(state.failed_compute.load(Ordering::Acquire), 0);
    }

    #[test]
    fn exact_reference_reduces_and_sorts() {
        let a = [Triple { qe: 0, te: 0, coef: 1 }, Triple { qe: 1, te: 0, coef: 2 }];
        let b = [Triple { qe: 0, te: 0, coef: 3 }, Triple { qe: 1, te: 0, coef: -1 }];
        let out = exact_reference(&a, &b).unwrap();
        assert_eq!(out, vec![
            Triple { qe: 0, te: 0, coef: 3 },
            Triple { qe: 1, te: 0, coef: 5 },
            Triple { qe: 2, te: 0, coef: -2 },
        ]);
    }

    #[test]
    fn spawn_contract_is_narrow_and_portable() {
        let fields = spawn_contract_json_fields();
        assert!(fields.contains("\"spawn_semantics_version\":1"));
        assert!(fields.contains("\"accessibility_profile\":\"sandbox\""));
        assert!(fields.contains("\"controlled_subsystems\":[\"accelerator-api\"]"));
        assert!(fields.contains("\"execution_surfaces\":[\"webgpu\"]"));
        assert!(fields.contains("\"surface:webgpu\""));
        assert!(!fields.contains("kernel"));
        assert!(!fields.contains("cpu-model"));
        assert!(!fields.contains("firmware"));
        assert!(!fields.contains("kvm"));
        assert!(!fields.contains("lxc"));
    }
}
