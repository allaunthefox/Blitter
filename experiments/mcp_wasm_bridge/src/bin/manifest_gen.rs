use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::{env, fs, path::Path, process::ExitCode};

fn run() -> Result<(), String> {
    let mut args = env::args_os().skip(1);
    let artifact = args
        .next()
        .ok_or("usage: manifest-gen <component.wasm> [module-name]")?;
    let module_name = args
        .next()
        .map(|value| value.to_string_lossy().into_owned())
        .unwrap_or_else(|| "local/mathpunch/mcp-u192-assembler".into());
    if args.next().is_some() {
        return Err("usage: manifest-gen <component.wasm> [module-name]".into());
    }
    if !module_name
        .bytes()
        .next()
        .is_some_and(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
        || !module_name
            .bytes()
            .all(|byte| {
                byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"._/-".contains(&byte)
            })
    {
        return Err("module-name must match [a-z0-9][a-z0-9._/-]*".into());
    }
    let bytes = fs::read(Path::new(&artifact)).map_err(|error| error.to_string())?;
    let digest = format!("sha256:{:x}", Sha256::digest(bytes));

    let mut manifest = Map::new();
    manifest.insert("authority".into(), "none".into());
    manifest.insert(
        "capabilities".into(),
        serde_json::json!({"devices": [], "filesystem": "none", "network": false, "process": false}),
    );
    manifest.insert("component_id".into(), "mcp-u192-assembler-v1".into());
    manifest.insert(
        "entrypoint".into(),
        "mathpunch:blitter-blob/assembler#assemble".into(),
    );
    manifest.insert("imports".into(), Value::Array(Vec::new()));
    manifest.insert(
        "limits".into(),
        serde_json::json!({"fuel": 1_000_000, "memory_mib": 64, "output_bytes": 1_048_576, "wall_ms": 1000}),
    );
    manifest.insert("module".into(), format!("{module_name}@{digest}").into());
    manifest.insert(
        "schema".into(),
        "mathpunch.blitter-wasm-component-manifest.v1".into(),
    );
    manifest.insert("wit".into(), "mathpunch:blitter-blob@1.0.0".into());
    println!("{}", Value::Object(manifest));
    Ok(())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("manifest-gen: {error}");
            ExitCode::from(2)
        }
    }
}
