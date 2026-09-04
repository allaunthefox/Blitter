use serde::Deserialize;
use serde_json::{Map, Value};

pub const U192_PROFILE_ID: &str = "BLITTER-ISA-V1";
pub const U192_PROFILE_DIGEST: &str =
    "sha256:cd60e19d6c3652e08d4ed36e3f8208de7f5d2c553cb7e8f5123175c63ff2f129";
pub const TERNARY_PROFILE_ID: &str = "blitter-ternary-step-i32-v1";
pub const TERNARY_PROFILE_DIGEST: &str =
    "sha256:987eed923534dcd7470dc45d72eb990bf454d9c52b6c074ec3a78371ae327d8d";

const DIGEST_PREFIX: &str = "sha256:";
const ENVELOPE_SCHEMA: &str = "mathpunch.blitter-computation-envelope.v1";
const OPERATION: &str = "u192.add-mod";
const CODEC: &str = "mathpunch.u192-abi.v1";
const INTERFACE: &str = "wasi-preview2-component-v1";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    request_id: String,
    profile_digest: String,
    terminal_contract_digest: String,
    assembler: Assembler,
    left_words: [u64; 6],
    right_words: [u64; 6],
    resource_budget: ResourceBudget,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Assembler {
    module_digest: String,
    interface: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ResourceBudget {
    cpus: u64,
    memory_mib: u64,
    wall_ms: u64,
}

fn valid_digest(value: &str) -> bool {
    value.len() == 71
        && value.starts_with(DIGEST_PREFIX)
        && value[DIGEST_PREFIX.len()..]
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

fn input(words: [u64; 6]) -> Result<Value, String> {
    let mut payload = String::with_capacity(48);
    for word in words {
        let word = u32::try_from(word).map_err(|_| "U192 words must be unsigned u32 values")?;
        for byte in word.to_le_bytes() {
            use std::fmt::Write;
            write!(&mut payload, "{byte:02x}").expect("writing to a String cannot fail");
        }
    }
    Ok(serde_json::json!({
        "format_tag": 0,
        "payload_hex": payload,
        "schema": CODEC,
    }))
}

fn validate(request: &Request) -> Result<(), String> {
    if !valid_request_id(&request.request_id) {
        return Err("request_id is invalid".into());
    }
    if !valid_digest(&request.profile_digest) {
        return Err("profile_digest must be sha256:<64 lowercase hex>".into());
    }
    if request.profile_digest != U192_PROFILE_DIGEST {
        return Err(format!(
            "unsupported profile digest; this component implements only {U192_PROFILE_ID}"
        ));
    }
    if !valid_digest(&request.terminal_contract_digest) {
        return Err("terminal_contract_digest must be sha256:<64 lowercase hex>".into());
    }
    if !valid_digest(&request.assembler.module_digest) {
        return Err("assembler.module_digest must be sha256:<64 lowercase hex>".into());
    }
    if request.assembler.interface != INTERFACE {
        return Err("unsupported assembler interface".into());
    }
    if request.resource_budget.cpus == 0
        || request.resource_budget.memory_mib == 0
        || request.resource_budget.wall_ms == 0
    {
        return Err("resource budget values must be positive".into());
    }
    Ok(())
}

/// Pure assembler entrypoint. It reads no clock, environment, filesystem, or network state.
pub fn assemble(request_json: &str) -> Result<String, String> {
    let request: Request = serde_json::from_str(request_json)
        .map_err(|error| format!("invalid request JSON: {error}"))?;
    validate(&request)?;

    let mut assembler = Map::new();
    assembler.insert("interface".into(), request.assembler.interface.into());
    assembler.insert("module_digest".into(), request.assembler.module_digest.into());

    let mut budget = Map::new();
    budget.insert("cpus".into(), request.resource_budget.cpus.into());
    budget.insert("memory_mib".into(), request.resource_budget.memory_mib.into());
    budget.insert("wall_ms".into(), request.resource_budget.wall_ms.into());

    let mut envelope = Map::new();
    envelope.insert("assembler".into(), assembler.into());
    envelope.insert("authority".into(), "none".into());
    envelope.insert("codec".into(), CODEC.into());
    envelope.insert(
        "inputs".into(),
        vec![input(request.left_words)?, input(request.right_words)?].into(),
    );
    envelope.insert("operation".into(), OPERATION.into());
    envelope.insert("profile_digest".into(), request.profile_digest.into());
    envelope.insert("request_id".into(), request.request_id.into());
    envelope.insert("resource_budget".into(), budget.into());
    envelope.insert("schema".into(), ENVELOPE_SCHEMA.into());
    envelope.insert(
        "terminal_contract_digest".into(),
        request.terminal_contract_digest.into(),
    );

    let encoded = serde_json::to_string(&Value::Object(envelope))
        .map_err(|error| format!("could not encode envelope: {error}"))?;
    debug_assert!(encoded.is_ascii());
    Ok(encoded + "\n")
}

#[cfg(target_arch = "wasm32")]
mod component {
    wit_bindgen::generate!({ path: "wit", world: "blob-assembler" });

    struct Bridge;

    impl exports::mathpunch::blitter_blob::assembler::Guest for Bridge {
        fn assemble(request_json: String) -> Result<String, String> {
            super::assemble(&request_json)
        }
    }

    export!(Bridge);
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn request() -> Value {
        json!({
            "assembler": {
                "interface": INTERFACE,
                "module_digest": format!("sha256:{}", "c".repeat(64)),
            },
            "left_words": [1, 0, 0, 0, 0, 0],
            "profile_digest": U192_PROFILE_DIGEST,
            "request_id": "mcp-1",
            "resource_budget": {"cpus": 1, "memory_mib": 64, "wall_ms": 1000},
            "right_words": [2, 0, 0, 0, 0, 0],
            "terminal_contract_digest": format!("sha256:{}", "b".repeat(64)),
        })
    }

    #[test]
    fn emits_sorted_compact_ascii_json_with_one_newline() {
        let output = assemble(&request().to_string()).unwrap();
        assert!(output.is_ascii());
        assert!(output.ends_with('\n'));
        assert!(!output[..output.len() - 1].contains('\n'));
        assert!(output.starts_with("{\"assembler\":"));
        assert!(output.contains("\"payload_hex\":\"01000000"));
        assert!(output.contains("\"authority\":\"none\""));
        assert_eq!(output, assemble(&request().to_string()).unwrap());

        let reordered = format!(
            "{{\"terminal_contract_digest\":\"sha256:{}\",\"right_words\":[2,0,0,0,0,0],\"resource_budget\":{{\"wall_ms\":1000,\"memory_mib\":64,\"cpus\":1}},\"request_id\":\"mcp-1\",\"profile_digest\":\"{}\",\"left_words\":[1,0,0,0,0,0],\"assembler\":{{\"module_digest\":\"sha256:{}\",\"interface\":\"{}\"}}}}",
            "b".repeat(64),
            U192_PROFILE_DIGEST,
            "c".repeat(64),
            INTERFACE,
        );
        assert_eq!(output, assemble(&reordered).unwrap());
    }

    #[test]
    fn rejects_unknown_fields_bad_limbs_and_zero_budget() {
        let mut extra = request();
        extra["extra"] = true.into();
        assert!(assemble(&extra.to_string()).is_err());

        let mut limb = request();
        limb["left_words"][0] = (u64::from(u32::MAX) + 1).into();
        assert_eq!(
            assemble(&limb.to_string()).unwrap_err(),
            "U192 words must be unsigned u32 values"
        );

        let mut budget = request();
        budget["resource_budget"]["wall_ms"] = 0.into();
        assert!(assemble(&budget.to_string()).is_err());
    }

    #[test]
    fn does_not_conflate_ternary_with_u192() {
        assert_ne!(TERNARY_PROFILE_ID, U192_PROFILE_ID);
        let mut value = request();
        value["profile_digest"] = TERNARY_PROFILE_DIGEST.into();
        assert!(assemble(&value.to_string())
            .unwrap_err()
            .contains("implements only BLITTER-ISA-V1"));
    }

    #[test]
    fn wraps_carry_inputs_modulo_u32_encoding_without_executing_them() {
        let mut value = request();
        value["left_words"] = json!([
            u32::MAX,
            u32::MAX,
            u32::MAX,
            u32::MAX,
            u32::MAX,
            u32::MAX
        ]);
        value["right_words"] = json!([1, 0, 0, 0, 0, 0]);
        let output: Value = serde_json::from_str(&assemble(&value.to_string()).unwrap()).unwrap();
        assert_eq!(output["inputs"][0]["payload_hex"], "ff".repeat(24));
        assert_eq!(output["operation"], OPERATION);
    }
}
