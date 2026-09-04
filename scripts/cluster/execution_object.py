#!/usr/bin/env python3
"""Canonical, content-addressed execution objects and blob descriptors.

This module defines data identity only. It deliberately does not fetch blobs,
stream chunks, select workers, or confer execution authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

SCHEMA = "mathpunch.blitter-execution-object.v1"
BLOB_SCHEMA = "mathpunch.blitter-blob-descriptor.v1"
AUTHORITY = "none"

MAX_BLOB_BYTES = 1 << 40
MAX_CHUNK_BYTES = 16 * 1024 * 1024
MAX_CHUNKS = MAX_BLOB_BYTES // MAX_CHUNK_BYTES

CHUNK_MANIFEST_SCHEMA = "mathpunch.blitter-chunk-manifest.v1"
CHUNK_MANIFEST_DOMAIN = b"mathpunch.blitter-chunk-manifest.v1\x00"

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MEDIA_TYPE_RE = re.compile(r"[!-~]{1,127}\Z")
_PROFILE_ID_RE = re.compile(r"[!-~]{1,127}\Z")
_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_EXECUTION_FIELDS = {
    "schema",
    "semantic_digest",
    "profile_id",
    "profile_digest",
    "idempotency_key",
    "program_digest",
    "architecture_digest",
    "terminal_contract_digest",
    "inputs",
    "resource_budget",
    "authority",
}
_SEMANTIC_FIELDS = _EXECUTION_FIELDS - {"semantic_digest"}


class ExecutionObjectError(ValueError):
    """The execution object or one of its descriptors is noncanonical."""


def canonical_bytes(value: Mapping[str, Any]) -> bytes:
    """Encode one object as compact, sorted, newline-terminated ASCII JSON."""
    if not isinstance(value, Mapping):
        raise ExecutionObjectError("canonical JSON root must be an object")
    try:
        encoded = json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ExecutionObjectError(f"value is not canonical JSON: {exc}") from exc
    return (encoded + "\n").encode("ascii")


def _require_exact_fields(value: Any, fields: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ExecutionObjectError(f"{name} fields are noncanonical")
    return value


def _require_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ExecutionObjectError(f"{field} must be sha256:<64 lowercase hex>")
    return value


def _require_uint(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ExecutionObjectError(f"{field} must be an integer in [{minimum}, {maximum}]")
    return value


def _validate_chunks(value: Any, *, size: int, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_CHUNKS:
        raise ExecutionObjectError(f"{field} must contain at most {MAX_CHUNKS} entries")
    if (size == 0) != (len(value) == 0):
        raise ExecutionObjectError(f"{field} must be empty exactly when the blob is empty")

    checked_chunks = []
    next_offset = 0
    for index, chunk in enumerate(value):
        chunk_field = f"{field}[{index}]"
        chunk = _require_exact_fields(
            chunk, {"digest", "offset", "size_bytes"}, chunk_field
        )
        _require_digest(chunk["digest"], f"{chunk_field}.digest")
        offset = _require_uint(
            chunk["offset"], f"{chunk_field}.offset", minimum=0, maximum=MAX_BLOB_BYTES
        )
        chunk_size = _require_uint(
            chunk["size_bytes"],
            f"{chunk_field}.size_bytes",
            minimum=1,
            maximum=MAX_CHUNK_BYTES,
        )
        if offset != next_offset or offset + chunk_size > size:
            raise ExecutionObjectError(f"{chunk_field} does not form contiguous blob coverage")
        next_offset += chunk_size
        checked_chunks.append(dict(chunk))
    if next_offset != size:
        raise ExecutionObjectError(f"{field} does not cover size_bytes")
    return checked_chunks


def chunk_manifest_root(
    blob_digest: str, size_bytes: int, chunks: Sequence[Mapping[str, Any]]
) -> str:
    """Return the domain-separated root of one validated carrier chunk layout."""
    digest = _require_digest(blob_digest, "blob_digest")
    size = _require_uint(
        size_bytes, "size_bytes", minimum=0, maximum=MAX_BLOB_BYTES
    )
    if isinstance(chunks, (str, bytes)) or not isinstance(chunks, Sequence):
        raise ExecutionObjectError("chunks must be an ordered sequence")
    checked_chunks = _validate_chunks(list(chunks), size=size, field="chunks")
    manifest = {
        "schema": CHUNK_MANIFEST_SCHEMA,
        "blob_digest": digest,
        "size_bytes": size,
        "chunks": checked_chunks,
    }
    return "sha256:" + hashlib.sha256(
        CHUNK_MANIFEST_DOMAIN + canonical_bytes(manifest)
    ).hexdigest()


def validate_blob_descriptor(value: Any, *, field: str = "blob") -> dict[str, Any]:
    """Validate semantic metadata for a blob and its complete chunk coverage."""
    value = _require_exact_fields(
        value,
        {"schema", "digest", "size_bytes", "media_type", "chunks", "chunk_manifest_root"},
        field,
    )
    if value["schema"] != BLOB_SCHEMA:
        raise ExecutionObjectError(f"{field}.schema is unsupported")
    _require_digest(value["digest"], f"{field}.digest")
    size = _require_uint(
        value["size_bytes"], f"{field}.size_bytes", minimum=0, maximum=MAX_BLOB_BYTES
    )
    media_type = value["media_type"]
    if not isinstance(media_type, str) or _MEDIA_TYPE_RE.fullmatch(media_type) is None:
        raise ExecutionObjectError(f"{field}.media_type must be 1..127 visible ASCII bytes")

    checked_chunks = _validate_chunks(value["chunks"], size=size, field=f"{field}.chunks")
    claimed_root = _require_digest(
        value["chunk_manifest_root"], f"{field}.chunk_manifest_root"
    )
    expected_root = chunk_manifest_root(value["digest"], size, checked_chunks)
    if claimed_root != expected_root:
        raise ExecutionObjectError(f"{field}.chunk_manifest_root does not match its manifest")

    return {**dict(value), "chunks": checked_chunks}


def describe_blob(data: bytes, media_type: str, *, chunk_size: int = MAX_CHUNK_BYTES) -> dict[str, Any]:
    """Describe an in-memory blob; this helper is not a streaming transport."""
    if not isinstance(data, bytes):
        raise ExecutionObjectError("blob data must be bytes")
    _require_uint(chunk_size, "chunk_size", minimum=1, maximum=MAX_CHUNK_BYTES)
    if len(data) > MAX_BLOB_BYTES:
        raise ExecutionObjectError("blob exceeds the descriptor size bound")
    chunks = []
    for offset in range(0, len(data), chunk_size):
        chunk = data[offset : offset + chunk_size]
        chunks.append(
            {
                "digest": "sha256:" + hashlib.sha256(chunk).hexdigest(),
                "offset": offset,
                "size_bytes": len(chunk),
            }
        )
    if len(chunks) > MAX_CHUNKS:
        raise ExecutionObjectError("blob requires too many chunk descriptors")
    descriptor = {
        "schema": BLOB_SCHEMA,
        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "media_type": media_type,
        "chunks": chunks,
        "chunk_manifest_root": chunk_manifest_root(
            "sha256:" + hashlib.sha256(data).hexdigest(), len(data), chunks
        ),
    }
    return validate_blob_descriptor(descriptor)


def _validate_budget(value: Any) -> dict[str, int]:
    value = _require_exact_fields(
        value, {"cpus", "memory_mib", "wall_ms"}, "resource_budget"
    )
    return {
        "cpus": _require_uint(value["cpus"], "resource_budget.cpus", minimum=1, maximum=4096),
        "memory_mib": _require_uint(
            value["memory_mib"], "resource_budget.memory_mib", minimum=1, maximum=1 << 20
        ),
        "wall_ms": _require_uint(
            value["wall_ms"], "resource_budget.wall_ms", minimum=1, maximum=604_800_000
        ),
    }


def _validate_semantic_body(value: Any) -> dict[str, Any]:
    value = _require_exact_fields(value, _SEMANTIC_FIELDS, "execution semantic body")
    if value["schema"] != SCHEMA:
        raise ExecutionObjectError("execution object schema is unsupported")
    if (
        not isinstance(value["profile_id"], str)
        or _PROFILE_ID_RE.fullmatch(value["profile_id"]) is None
    ):
        raise ExecutionObjectError("profile_id must be 1..127 visible ASCII bytes")
    if (
        not isinstance(value["idempotency_key"], str)
        or _IDEMPOTENCY_KEY_RE.fullmatch(value["idempotency_key"]) is None
    ):
        raise ExecutionObjectError("idempotency_key is invalid")
    for field in (
        "profile_digest",
        "program_digest",
        "architecture_digest",
        "terminal_contract_digest",
    ):
        _require_digest(value[field], field)
    inputs = value["inputs"]
    if not isinstance(inputs, list):
        raise ExecutionObjectError("inputs must be an ordered JSON array")
    checked_inputs = [
        validate_blob_descriptor(item, field=f"inputs[{index}]")
        for index, item in enumerate(inputs)
    ]
    if value["authority"] != AUTHORITY:
        raise ExecutionObjectError("execution object authority must be none")
    return {
        **dict(value),
        "inputs": checked_inputs,
        "resource_budget": _validate_budget(value["resource_budget"]),
    }


def _semantic_projection(checked_body: Mapping[str, Any]) -> dict[str, Any]:
    projected = dict(checked_body)
    projected["inputs"] = [
        {
            "schema": item["schema"],
            "digest": item["digest"],
            "size_bytes": item["size_bytes"],
            "media_type": item["media_type"],
        }
        for item in checked_body["inputs"]
    ]
    return projected


def _digest_checked_body(checked_body: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(
        canonical_bytes(_semantic_projection(checked_body))
    ).hexdigest()


def semantic_digest(value: Mapping[str, Any]) -> str:
    """Return a body digest, validating any complete object's claimed digest."""
    if not isinstance(value, Mapping):
        raise ExecutionObjectError("execution object must be an object")
    if set(value) == _EXECUTION_FIELDS:
        return validate_execution_object(value)["semantic_digest"]
    checked = _validate_semantic_body(dict(value))
    return _digest_checked_body(checked)


def build_execution_object(
    *,
    profile_id: str,
    profile_digest: str,
    idempotency_key: str,
    program_digest: str,
    architecture_digest: str,
    terminal_contract_digest: str,
    inputs: Sequence[Mapping[str, Any]],
    resource_budget: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a digest-sealed execution object from semantic fields only."""
    if isinstance(inputs, (str, bytes)) or not isinstance(inputs, Sequence):
        raise ExecutionObjectError("inputs must be an ordered sequence")
    body = _validate_semantic_body(
        {
            "schema": SCHEMA,
            "profile_id": profile_id,
            "profile_digest": profile_digest,
            "idempotency_key": idempotency_key,
            "program_digest": program_digest,
            "architecture_digest": architecture_digest,
            "terminal_contract_digest": terminal_contract_digest,
            "inputs": list(inputs),
            "resource_budget": dict(resource_budget),
            "authority": AUTHORITY,
        }
    )
    return {**body, "semantic_digest": semantic_digest(body)}


def validate_execution_object(value: Any) -> dict[str, Any]:
    value = _require_exact_fields(value, _EXECUTION_FIELDS, "execution object")
    claimed = _require_digest(value["semantic_digest"], "semantic_digest")
    checked_body = _validate_semantic_body({key: value[key] for key in _SEMANTIC_FIELDS})
    expected = _digest_checked_body(checked_body)
    if claimed != expected:
        raise ExecutionObjectError("semantic_digest does not match the exact semantic body")
    return {**checked_body, "semantic_digest": claimed}


def parse_canonical(raw: bytes) -> dict[str, Any]:
    """Parse only the canonical wire representation of an execution object."""
    if not isinstance(raw, bytes):
        raise ExecutionObjectError("canonical input must be bytes")
    try:
        value = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionObjectError(f"execution object is not ASCII JSON: {exc}") from exc
    checked = validate_execution_object(value)
    if raw != canonical_bytes(checked):
        raise ExecutionObjectError("execution object bytes are not the canonical re-encoding")
    return checked
