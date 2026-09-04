#!/usr/bin/env python3
"""Canonical MCP-to-Blitter computation envelopes.

The envelope is a transportable request assembled by an MCP/WASM surface. It is
not an execution receipt or mathematical authority.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

try:
    from experiments.webgpu_blitter.u192_codec import normalize_tagged_u192, u192_from_int, u192_to_int
except ModuleNotFoundError:
    _CODEC_DIR = Path(__file__).resolve().parents[2] / "experiments" / "webgpu_blitter"
    if str(_CODEC_DIR) not in sys.path:
        sys.path.insert(0, str(_CODEC_DIR))
    from u192_codec import normalize_tagged_u192, u192_from_int, u192_to_int

try:
    from . import semantic_profile_registry
except ImportError:
    import semantic_profile_registry

SCHEMA_V1 = "mathpunch.blitter-computation-envelope.v1"
SCHEMA_V2 = "mathpunch.blitter-computation-envelope.v2"
SCHEMA = SCHEMA_V2
CODEC = "mathpunch.u192-abi.v1"
OPERATION = "u192.add-mod"
PROFILE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
HEX_RE = re.compile(r"^[0-9a-f]{48}$")
TERMINAL_CONTRACT_DIGESTS: Mapping[str, str] = MappingProxyType({
    "mathpunch.blitter-computation-result-terminal.v1":
        "sha256:5a9804978698791d50d937ba93b7f279344b2e0ea658f4e2ee7b2d5a3365fb7f",
})


class ComputationBlobError(ValueError):
    """The computation envelope is malformed or outside the ABI."""


def canonical_bytes(value: Mapping[str, Any]) -> bytes:
    if not isinstance(value, Mapping):
        raise ComputationBlobError("canonical JSON root must be an object")
    try:
        encoded = json.dumps(dict(value), sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ComputationBlobError(f"envelope is not canonical JSON: {exc}") from exc
    return (encoded + "\n").encode("ascii")


def digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or PROFILE_DIGEST_RE.fullmatch(value) is None:
        raise ComputationBlobError(f"{field} must be sha256:<64 lowercase hex>")
    return value


def _input(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema", "format_tag", "payload_hex"}:
        raise ComputationBlobError(f"inputs[{index}] fields are noncanonical")
    if value["schema"] != "mathpunch.u192-abi.v1":
        raise ComputationBlobError(f"inputs[{index}] has unsupported codec schema")
    if type(value["format_tag"]) is not int or not 0 <= value["format_tag"] <= 255:
        raise ComputationBlobError(f"inputs[{index}].format_tag is invalid")
    payload = value["payload_hex"]
    if not isinstance(payload, str) or HEX_RE.fullmatch(payload) is None:
        raise ComputationBlobError(f"inputs[{index}].payload_hex must be 24-byte lowercase hex")
    try:
        normalize_tagged_u192(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise ComputationBlobError(f"inputs[{index}] is not a valid U192 value") from exc
    return dict(value)


def registered_terminal_contract_digest(profile: Any) -> str:
    """Return the locked artifact digest for a registry terminal contract."""
    try:
        return TERMINAL_CONTRACT_DIGESTS[profile.terminal_contract.contract_id]
    except (AttributeError, KeyError) as exc:
        raise ComputationBlobError("semantic profile has no admitted terminal contract digest") from exc


def validate_envelope(envelope: Mapping[str, Any]) -> dict[str, Any]:
    common = {
        "schema", "request_id", "operation", "profile_digest", "codec",
        "inputs", "resource_budget", "terminal_contract_digest", "assembler",
        "authority",
    }
    if not isinstance(envelope, Mapping):
        raise ComputationBlobError("envelope fields are noncanonical")
    schema = envelope.get("schema")
    required = common if schema == SCHEMA_V1 else common | {"profile_id"}
    if schema not in (SCHEMA_V1, SCHEMA_V2) or set(envelope) != required:
        raise ComputationBlobError("envelope fields are noncanonical")
    if envelope["operation"] != OPERATION:
        raise ComputationBlobError("unsupported computation envelope")
    request_id = envelope["request_id"]
    if not isinstance(request_id, str) or REQUEST_ID_RE.fullmatch(request_id) is None:
        raise ComputationBlobError("request_id is invalid")
    _digest(envelope["profile_digest"], "profile_digest")
    if envelope["codec"] != CODEC:
        raise ComputationBlobError("unsupported computation codec")
    inputs = envelope["inputs"]
    if not isinstance(inputs, list) or len(inputs) != 2:
        raise ComputationBlobError("u192.add-mod requires exactly two inputs")
    checked_inputs = [_input(item, index) for index, item in enumerate(inputs)]
    budget = envelope["resource_budget"]
    if not isinstance(budget, dict) or set(budget) != {"cpus", "memory_mib", "wall_ms"}:
        raise ComputationBlobError("resource_budget fields are noncanonical")
    for field in ("cpus", "memory_mib", "wall_ms"):
        if type(budget[field]) is not int or budget[field] < 1:
            raise ComputationBlobError(f"resource_budget.{field} must be positive")
    _digest(envelope["terminal_contract_digest"], "terminal_contract_digest")
    assembler = envelope["assembler"]
    if not isinstance(assembler, dict) or set(assembler) != {"module_digest", "interface"}:
        raise ComputationBlobError("assembler fields are noncanonical")
    _digest(assembler["module_digest"], "assembler.module_digest")
    if assembler["interface"] != "wasi-preview2-component-v1":
        raise ComputationBlobError("unsupported assembler interface")
    if envelope["authority"] != "none":
        raise ComputationBlobError("computation envelope authority must be none")
    if schema == SCHEMA_V2:
        try:
            profile = semantic_profile_registry.resolve_profile(
                envelope["profile_id"], envelope["profile_digest"]
            )
        except semantic_profile_registry.SemanticProfileError as exc:
            raise ComputationBlobError(str(exc)) from exc
        if profile.operation != envelope["operation"] or profile.request_codec != envelope["codec"]:
            raise ComputationBlobError("semantic profile does not match operation and codec")
        if envelope["terminal_contract_digest"] != registered_terminal_contract_digest(profile):
            raise ComputationBlobError("terminal contract digest does not match semantic profile")
    return {
        **dict(envelope),
        "inputs": checked_inputs,
        "resource_budget": dict(budget),
        "assembler": dict(assembler),
    }


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ComputationBlobError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def parse_canonical(raw: bytes) -> dict[str, Any]:
    """Parse an envelope only from its exact canonical ASCII JSON bytes."""
    if not isinstance(raw, bytes):
        raise ComputationBlobError("canonical input must be bytes")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicate_keys)
    except UnicodeDecodeError as exc:
        raise ComputationBlobError(f"envelope is not ASCII JSON: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ComputationBlobError(f"envelope is not JSON: {exc}") from exc
    checked = validate_envelope(value)
    if raw != canonical_bytes(checked):
        raise ComputationBlobError("envelope bytes are not the canonical re-encoding")
    return checked


def add_mod_result(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Reference result for conformance tests; this does not authorize a result."""
    checked = validate_envelope(envelope)
    left = u192_to_int(normalize_tagged_u192(checked["inputs"][0])["words"])
    right = u192_to_int(normalize_tagged_u192(checked["inputs"][1])["words"])
    words = u192_from_int((left + right) % (1 << 192))
    result = {
        "schema": "mathpunch.blitter-computation-result.v1",
        "request_digest": digest(envelope),
        "codec": "mathpunch.u192-canonical.v1",
        "words": words,
        "authority": "none",
    }
    return result
