// Canonical-envelope WebGPU daemon with observable occupancy.
//
// The daemon exposes /health, /status, and /compute-envelope. Status lets
// schedulers observe whether the single GPU critical
// section is active or has queued work. Status is observational, not a lease:
// callers that require exclusion still need an atomic reservation protocol.
//
// Controlled-spawn metadata is deliberately narrow. Successful daemon startup
// establishes a working WebGPU API surface; it does not establish control of
// the host kernel, container runtime, physical accelerator, or provider.

use std::env;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::Deserialize;
use sha2::{Digest, Sha256};

const MAX_BODY_BYTES: usize = 1 << 20;
const SPAWN_SEMANTICS_VERSION: u32 = 1;
const ACCESSIBILITY_PROFILE: &str = "sandbox";
const ENVELOPE_SCHEMA: &str = "mathpunch.blitter-computation-envelope.v2";
const ENVELOPE_OPERATION: &str = "u192.add-mod";
const ENVELOPE_PROFILE_ID: &str = "BLITTER-ISA-V1";
const ENVELOPE_PROFILE_DIGEST: &str = "sha256:cd60e19d6c3652e08d4ed36e3f8208de7f5d2c553cb7e8f5123175c63ff2f129";
const ENVELOPE_TERMINAL_CONTRACT_DIGEST: &str = "sha256:5a9804978698791d50d937ba93b7f279344b2e0ea658f4e2ee7b2d5a3365fb7f";
const U192_ABI_CODEC: &str = "mathpunch.u192-abi.v1";
const U192_RESULT_CODEC: &str = "mathpunch.u192-canonical.v1";
const ENVELOPE_RESULT_SCHEMA: &str = "mathpunch.blitter-computation-result.v1";
const ASSEMBLER_INTERFACE: &str = "wasi-preview2-component-v1";

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

struct GpuContext {
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

    #[cfg(test)]
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

fn request_device(adapter: &wgpu::Adapter) -> Result<(), String> {
    pollster::block_on(adapter.request_device(&wgpu::DeviceDescriptor {
        label: Some("blitter-device"),
        required_features: wgpu::Features::empty(),
        required_limits: wgpu::Limits::default(),
        experimental_features: wgpu::ExperimentalFeatures::default(),
        memory_hints: wgpu::MemoryHints::Performance,
        trace: wgpu::Trace::Off,
    }))
    .map(|_| ())
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
            Ok(()) => {
                let adapter_label = format!("{} ({})", adapter_name, backend_name);
                selected = Some((
                    adapter_name,
                    backend_name,
                    device_type,
                    adapter_label,
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
    let (adapter_name, backend_name, device_type, adapter_label) = selected
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
        adapter_name,
        backend_name,
        device_type,
        adapter_label,
        gate: Mutex::new(()),
    })
}

#[derive(Debug)]
struct EnvelopePolicy {
    profile_id: String,
    profile_digest: String,
    terminal_contract_digest: String,
    assembler_module_digest: String,
    max_cpus: u64,
    max_memory_mib: u64,
    max_wall_ms: u64,
}

impl EnvelopePolicy {
    fn from_env() -> Result<Self, String> {
        fn digest(name: &str) -> Result<String, String> {
            let value = env::var(name).map_err(|_| format!("{name} is required"))?;
            if !valid_digest(&value) {
                return Err(format!("{name} must be sha256:<64 lowercase hex>"));
            }
            Ok(value)
        }
        fn positive(name: &str) -> Result<u64, String> {
            let value = env::var(name).map_err(|_| format!("{name} is required"))?;
            value
                .parse::<u64>()
                .ok()
                .filter(|value| *value > 0)
                .ok_or_else(|| format!("{name} must be a positive integer"))
        }
        Ok(Self {
            profile_id: env::var("BLITTER_ENVELOPE_PROFILE_ID")
                .map_err(|_| "BLITTER_ENVELOPE_PROFILE_ID is required".to_string())?,
            profile_digest: digest("BLITTER_ENVELOPE_PROFILE_DIGEST")?,
            terminal_contract_digest: digest("BLITTER_ENVELOPE_TERMINAL_CONTRACT_DIGEST")?,
            assembler_module_digest: digest("BLITTER_ENVELOPE_ASSEMBLER_MODULE_DIGEST")?,
            max_cpus: positive("BLITTER_ENVELOPE_MAX_CPUS")?,
            max_memory_mib: positive("BLITTER_ENVELOPE_MAX_MEMORY_MIB")?,
            max_wall_ms: positive("BLITTER_ENVELOPE_MAX_WALL_MS")?,
        })
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EnvelopeRequest {
    schema: String,
    request_id: String,
    operation: String,
    profile_id: String,
    profile_digest: String,
    codec: String,
    inputs: Vec<TaggedU192>,
    resource_budget: ResourceBudget,
    terminal_contract_digest: String,
    assembler: EnvelopeAssembler,
    authority: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TaggedU192 {
    schema: String,
    format_tag: u8,
    payload_hex: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ResourceBudget {
    cpus: u64,
    memory_mib: u64,
    wall_ms: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EnvelopeAssembler {
    module_digest: String,
    interface: String,
}

fn valid_digest(value: &str) -> bool {
    value.len() == 71
        && value.starts_with("sha256:")
        && value[7..]
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn valid_request_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 128
        && bytes[0].is_ascii_alphanumeric()
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._:-".contains(byte))
}

fn decode_hex_24(value: &str) -> Result<[u8; 24], String> {
    if value.len() != 48 || !value.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)) {
        return Err("U192 payload_hex must be 24-byte lowercase hex".to_string());
    }
    let mut out = [0u8; 24];
    for (index, slot) in out.iter_mut().enumerate() {
        *slot = u8::from_str_radix(&value[index * 2..index * 2 + 2], 16)
            .map_err(|_| "invalid U192 payload_hex".to_string())?;
    }
    Ok(out)
}

fn decode_tagged_u192(value: &TaggedU192) -> Result<[u32; 6], String> {
    if value.schema != U192_ABI_CODEC {
        return Err("tagged U192 schema mismatch".to_string());
    }
    if !matches!(value.format_tag, 0x00 | 0x10 | 0x20 | 0x30) {
        return Err("unsupported or noncanonical U192 format tag".to_string());
    }
    let raw = decode_hex_24(&value.payload_hex)?;
    let big_endian = value.format_tag & 0x10 != 0;
    let msw_first = value.format_tag & 0x20 != 0;
    let mut transport_words = [0u32; 6];
    for (index, word) in transport_words.iter_mut().enumerate() {
        let bytes: [u8; 4] = raw[index * 4..index * 4 + 4].try_into().expect("fixed slice");
        *word = if big_endian { u32::from_be_bytes(bytes) } else { u32::from_le_bytes(bytes) };
    }
    if msw_first {
        transport_words.reverse();
    }
    Ok(transport_words)
}

fn canonical_json_bytes(value: &serde_json::Value) -> Result<Vec<u8>, String> {
    let mut bytes = serde_json::to_vec(value).map_err(|error| format!("JSON encode: {error}"))?;
    bytes.push(b'\n');
    Ok(bytes)
}

fn parse_envelope_request(body: &str, policy: &EnvelopePolicy) -> Result<([u32; 6], [u32; 6]), String> {
    if !body.is_ascii() {
        return Err("envelope must be canonical ASCII JSON".to_string());
    }
    let value: serde_json::Value = serde_json::from_str(body)
        .map_err(|error| format!("invalid envelope JSON: {error}"))?;
    if body.as_bytes() != canonical_json_bytes(&value)? {
        return Err("envelope bytes are not canonical sorted compact JSON with one newline".to_string());
    }
    let request: EnvelopeRequest = serde_json::from_value(value)
        .map_err(|error| format!("invalid envelope: {error}"))?;
    if request.schema != ENVELOPE_SCHEMA
        || request.operation != ENVELOPE_OPERATION
        || request.codec != U192_ABI_CODEC
        || request.authority != "none"
    {
        return Err("unsupported computation envelope".to_string());
    }
    if !valid_request_id(&request.request_id) {
        return Err("request_id is invalid".to_string());
    }
    if request.profile_id != ENVELOPE_PROFILE_ID
        || policy.profile_id != ENVELOPE_PROFILE_ID
        || !valid_digest(&request.profile_digest)
        || request.profile_digest != ENVELOPE_PROFILE_DIGEST
        || request.profile_digest != policy.profile_digest
    {
        return Err("profile identity is not registry-bound and deployment-approved".to_string());
    }
    if !valid_digest(&request.terminal_contract_digest)
        || request.terminal_contract_digest != ENVELOPE_TERMINAL_CONTRACT_DIGEST
        || request.terminal_contract_digest != policy.terminal_contract_digest
    {
        return Err("terminal_contract_digest is not deployment-approved".to_string());
    }
    if request.assembler.interface != ASSEMBLER_INTERFACE
        || !valid_digest(&request.assembler.module_digest)
        || request.assembler.module_digest != policy.assembler_module_digest
    {
        return Err("assembler is not deployment-approved".to_string());
    }
    let budget = request.resource_budget;
    if budget.cpus == 0
        || budget.memory_mib == 0
        || budget.wall_ms == 0
        || budget.cpus > policy.max_cpus
        || budget.memory_mib > policy.max_memory_mib
        || budget.wall_ms > policy.max_wall_ms
    {
        return Err("resource_budget exceeds deployment policy".to_string());
    }
    if request.inputs.len() != 2 {
        return Err("u192.add-mod requires exactly two inputs".to_string());
    }
    Ok((decode_tagged_u192(&request.inputs[0])?, decode_tagged_u192(&request.inputs[1])?))
}

fn add_u192_mod(left: [u32; 6], right: [u32; 6]) -> [u32; 6] {
    let mut words = [0u32; 6];
    let mut carry = 0u64;
    for index in 0..6 {
        let sum = u64::from(left[index]) + u64::from(right[index]) + carry;
        words[index] = sum as u32;
        carry = sum >> 32;
    }
    words
}

fn envelope_result(body: &str, words: [u32; 6]) -> Result<String, String> {
    let request_digest = format!("sha256:{:x}", Sha256::digest(body.as_bytes()));
    let value = serde_json::json!({
        "authority": "none",
        "codec": U192_RESULT_CODEC,
        "request_digest": request_digest,
        "schema": ENVELOPE_RESULT_SCHEMA,
        "words": words,
    });
    String::from_utf8(canonical_json_bytes(&value)?).map_err(|error| error.to_string())
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

fn error_json(gpu: &GpuContext, error: &str) -> String {
    format!(
        "{{\"ok\":false,\"adapter\":\"{}\",\"error\":\"{}\"}}",
        json_escape(&gpu.adapter_label),
        json_escape(error)
    )
}

fn handle_compute_envelope(
    mut stream: TcpStream,
    request: HttpRequest,
    gpu: Arc<GpuContext>,
    state: Arc<RuntimeState>,
    policy: Arc<Result<EnvelopePolicy, String>>,
) {
    state.begin_request();
    let policy = match policy.as_ref() {
        Ok(policy) => policy,
        Err(error) => {
            state.record_failed_request();
            write_json(&mut stream, 503, "Service Unavailable", &error_json(&gpu, error));
            return;
        }
    };
    let (left, right) = match parse_envelope_request(&request.body, policy) {
        Ok(inputs) => inputs,
        Err(error) => {
            state.record_failed_request();
            write_json(&mut stream, 400, "Bad Request", &error_json(&gpu, &error));
            return;
        }
    };
    state.enter_queue();
    let lock = match gpu.gate.lock() {
        Ok(lock) => lock,
        Err(_) => {
            state.leave_queue();
            state.record_failed_request();
            write_json(&mut stream, 500, "Internal Server Error", &error_json(&gpu, "compute lock poisoned"));
            return;
        }
    };
    state.leave_queue();
    let mut active = ActiveGuard::new(state);
    let result = envelope_result(&request.body, add_u192_mod(left, right));
    drop(lock);
    match result {
        Ok(body) => {
            active.mark_success();
            write_json(&mut stream, 200, "OK", &body);
        }
        Err(error) => write_json(&mut stream, 500, "Internal Server Error", &error_json(&gpu, &error)),
    }
}

fn handle_connection(
    mut stream: TcpStream,
    gpu: Arc<GpuContext>,
    state: Arc<RuntimeState>,
    debug: Arc<DebugTrace>,
    envelope_policy: Arc<Result<EnvelopePolicy, String>>,
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
        ("POST", "/compute-envelope") => {
            handle_compute_envelope(stream, request, gpu, state, envelope_policy)
        }
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
    let envelope_policy = Arc::new(EnvelopePolicy::from_env());
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
                let envelope_policy = Arc::clone(&envelope_policy);
                std::thread::spawn(move || handle_connection(stream, gpu, state, debug, envelope_policy));
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

    fn test_policy() -> EnvelopePolicy {
        EnvelopePolicy {
            profile_id: ENVELOPE_PROFILE_ID.to_string(),
            profile_digest: ENVELOPE_PROFILE_DIGEST.to_string(),
            terminal_contract_digest: ENVELOPE_TERMINAL_CONTRACT_DIGEST.to_string(),
            assembler_module_digest: format!("sha256:{}", "c".repeat(64)),
            max_cpus: 2,
            max_memory_mib: 128,
            max_wall_ms: 1000,
        }
    }

    fn envelope(left: &str, right: &str) -> String {
        format!(concat!(
            "{{\"assembler\":{{\"interface\":\"wasi-preview2-component-v1\",\"module_digest\":\"sha256:{}\"}},",
            "\"authority\":\"none\",\"codec\":\"mathpunch.u192-abi.v1\",",
            "\"inputs\":[{{\"format_tag\":0,\"payload_hex\":\"{}\",\"schema\":\"mathpunch.u192-abi.v1\"}},",
            "{{\"format_tag\":0,\"payload_hex\":\"{}\",\"schema\":\"mathpunch.u192-abi.v1\"}}],",
            "\"operation\":\"u192.add-mod\",\"profile_digest\":\"{}\",\"profile_id\":\"BLITTER-ISA-V1\",\"request_id\":\"req-1\",",
            "\"resource_budget\":{{\"cpus\":1,\"memory_mib\":64,\"wall_ms\":1000}},",
            "\"schema\":\"mathpunch.blitter-computation-envelope.v2\",",
            "\"terminal_contract_digest\":\"{}\"}}\n"
        ), "c".repeat(64), left, right, ENVELOPE_PROFILE_DIGEST, ENVELOPE_TERMINAL_CONTRACT_DIGEST)
    }

    #[test]
    fn envelope_addition_wraps_and_binds_canonical_request() {
        let max = "ff".repeat(24);
        let one = format!("01{}", "00".repeat(23));
        let body = envelope(&max, &one);
        let (left, right) = parse_envelope_request(&body, &test_policy()).unwrap();
        let words = add_u192_mod(left, right);
        assert_eq!(words, [0; 6]);
        let result = envelope_result(&body, words).unwrap();
        assert!(result.starts_with("{\"authority\":\"none\",\"codec\":"));
        assert!(result.ends_with('\n'));
        assert!(result.contains("\"request_digest\":\"sha256:"));
    }

    #[test]
    fn envelope_rejects_noncanonical_bytes_policy_and_tags() {
        let zero = "00".repeat(24);
        let body = envelope(&zero, &zero);
        assert!(parse_envelope_request(body.trim_end(), &test_policy()).is_err());
        let bad_profile = body.replace(ENVELOPE_PROFILE_DIGEST, &format!("sha256:{}", "d".repeat(64)));
        assert!(parse_envelope_request(&bad_profile, &test_policy()).is_err());
        let bad_tag = body.replacen("\"format_tag\":0", "\"format_tag\":1", 1);
        assert!(parse_envelope_request(&bad_tag, &test_policy()).is_err());
    }
}
