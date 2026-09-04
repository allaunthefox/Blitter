#!/usr/bin/env python3
"""Deterministic reference assembler for the MCP-to-Blitter blob ABI."""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:
    from .computation_blob import canonical_bytes, validate_envelope
    from . import computation_blob
    from .wasm_component import validate_manifest
except ImportError:
    from computation_blob import canonical_bytes, validate_envelope
    import computation_blob
    from wasm_component import validate_manifest

try:
    from experiments.webgpu_blitter.u192_codec import make_tagged_u192
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "experiments" / "webgpu_blitter"))
    from u192_codec import make_tagged_u192


def assemble(
    request: Mapping[str, Any], *, manifest: Mapping[str, Any] | None = None,
    envelope_schema: str = computation_blob.SCHEMA_V2,
) -> dict[str, Any]:
    """Assemble one MCP request without invoking a compiler or host capability."""
    required = {
        "request_id", "profile_digest", "terminal_contract_digest", "assembler",
        "left_words", "right_words", "resource_budget",
    }
    if envelope_schema == computation_blob.SCHEMA_V2:
        required |= {"profile_id"}
    elif envelope_schema != computation_blob.SCHEMA_V1:
        raise computation_blob.ComputationBlobError("unsupported computation envelope schema")
    if not isinstance(request, Mapping) or set(request) != required:
        raise computation_blob.ComputationBlobError("MCP request fields are noncanonical")
    if manifest is not None:
        try:
            checked_manifest = validate_manifest(manifest)
        except ValueError as exc:
            raise computation_blob.ComputationBlobError(str(exc)) from exc
        if checked_manifest["module"].rsplit("@", 1)[-1] != request["assembler"]["module_digest"]:
            raise computation_blob.ComputationBlobError("assembler module does not match manifest")
        if checked_manifest["entrypoint"] != "mathpunch:blitter-blob/assembler#assemble":
            raise computation_blob.ComputationBlobError("assembler entrypoint does not match ABI")
    def input_blob(words: Any) -> dict[str, Any]:
        if not isinstance(words, list) or len(words) != 6:
            raise computation_blob.ComputationBlobError("U192 words must contain six limbs")
        if any(type(word) is not int or word < 0 or word >= 2**32 for word in words):
            raise computation_blob.ComputationBlobError("U192 words must be unsigned u32 values")
        encoded = make_tagged_u192(words, "le-u32-lsw-first")
        return {"schema": "mathpunch.u192-abi.v1", "format_tag": 0,
                "payload_hex": encoded["payload_hex"]}
    envelope = {
        "schema": envelope_schema,
        "request_id": request["request_id"],
        "operation": computation_blob.OPERATION,
        "profile_digest": request["profile_digest"],
        "codec": computation_blob.CODEC,
        "inputs": [input_blob(request["left_words"]), input_blob(request["right_words"])],
        "resource_budget": request["resource_budget"],
        "terminal_contract_digest": request["terminal_contract_digest"],
        "assembler": request["assembler"],
        "authority": "none",
    }
    if envelope_schema == computation_blob.SCHEMA_V2:
        envelope["profile_id"] = request["profile_id"]
    validate_envelope(envelope)
    return envelope


def parse_canonical_request(raw: bytes, *, envelope_schema: str) -> dict[str, Any]:
    """Parse canonical request bytes without allowing duplicate object keys."""
    if not isinstance(raw, bytes):
        raise computation_blob.ComputationBlobError("canonical input must be bytes")
    try:
        value = json.loads(
            raw.decode("ascii"), object_pairs_hook=computation_blob._reject_duplicate_keys
        )
    except UnicodeDecodeError as exc:
        raise computation_blob.ComputationBlobError(f"MCP request is not ASCII JSON: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise computation_blob.ComputationBlobError(f"MCP request is not JSON: {exc}") from exc
    # Assembly performs the semantic validation; this check seals the raw representation.
    assemble(value, envelope_schema=envelope_schema)
    if raw != canonical_bytes(value):
        raise computation_blob.ComputationBlobError("MCP request bytes are not the canonical re-encoding")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path, help="canonical MCP request JSON")
    parser.add_argument("--manifest", type=Path, required=True,
                        help="validated WASM component manifest")
    args = parser.parse_args()
    try:
        schema = computation_blob.SCHEMA_V2
        request = parse_canonical_request(args.request.read_bytes(), envelope_schema=schema)
        manifest = json.loads(
            args.manifest.read_bytes().decode("utf-8"),
            object_pairs_hook=computation_blob._reject_duplicate_keys,
        )
        sys.stdout.buffer.write(canonical_bytes(assemble(
            request, manifest=manifest, envelope_schema=schema
        )))
        return 0
    except (OSError, UnicodeError, json.JSONDecodeError, computation_blob.ComputationBlobError) as exc:
        print(f"mcp-blob-assembler: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
