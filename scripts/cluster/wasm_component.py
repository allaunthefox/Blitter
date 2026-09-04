#!/usr/bin/env python3
"""Fail-closed validator for the MCP/WASM assembler component manifest."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

MANIFEST_SCHEMA = "mathpunch.blitter-wasm-component-manifest.v1"
WIT_PACKAGE = "mathpunch:blitter-blob@1.0.0"
ENTRYPOINT = "mathpunch:blitter-blob/assembler#assemble"
MODULE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")


class WasmManifestError(ValueError):
    """The component is not permitted by the execution contract."""


def _exact_object(value: Any, fields: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise WasmManifestError(f"{name} fields are noncanonical")
    return value


def validate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    root = _exact_object(
        manifest,
        {"schema", "component_id", "module", "wit", "entrypoint", "imports",
         "capabilities", "limits", "authority"},
        "manifest",
    )
    if root["schema"] != MANIFEST_SCHEMA:
        raise WasmManifestError("unsupported WASM manifest schema")
    for field in ("component_id", "module"):
        if not isinstance(root[field], str) or not root[field]:
            raise WasmManifestError(f"{field} must be a non-empty string")
    if MODULE_RE.fullmatch(root["module"]) is None:
        raise WasmManifestError("module must be a digest-pinned module reference")
    if root["wit"] != WIT_PACKAGE or root["entrypoint"] != ENTRYPOINT:
        raise WasmManifestError("unsupported WASM interface or entrypoint")
    if root["imports"] != []:
        raise WasmManifestError("WASM assembler imports must be empty")
    capabilities = _exact_object(
        root["capabilities"], {"network", "filesystem", "process", "devices"}, "capabilities"
    )
    if capabilities["network"] is not False or capabilities["filesystem"] != "none":
        raise WasmManifestError("WASM host capabilities are not permitted")
    if capabilities["process"] is not False or capabilities["devices"] != []:
        raise WasmManifestError("WASM process/device capabilities are not permitted")
    limits = _exact_object(
        root["limits"], {"memory_mib", "fuel", "wall_ms", "output_bytes"}, "limits"
    )
    for field in limits:
        if type(limits[field]) is not int or limits[field] < 1:
            raise WasmManifestError(f"limits.{field} must be positive")
    if limits["memory_mib"] > 256 or limits["wall_ms"] > 60_000 or limits["output_bytes"] > 16 * 1024 * 1024:
        raise WasmManifestError("WASM limits exceed assembler policy")
    if root["authority"] != "none":
        raise WasmManifestError("WASM component authority must be none")
    return dict(root)
