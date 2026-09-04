#!/usr/bin/env python3
"""Pure validator and canonical digest for the trusted LSP image catalog."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

SCHEMA = "mathpunch.blitter-lsp-image-catalog.v1"
SUPPORTED_LANGUAGES = ("lean", "coq", "python", "r", "julia", "rust")
LANGUAGES = frozenset(SUPPORTED_LANGUAGES)

# This is the command policy, not a shell command.  Catalogs must use these
# exact argv vectors; deployments approve image digests separately.
CANONICAL_ARGV = {
    "lean": ("lake", "env", "lean", "--server"),
    "coq": ("coq-lsp",),
    "python": ("pylsp",),
    "r": ("R", "--vanilla", "--slave"),
    "julia": ("julia", "--startup-file=no"),
    "rust": ("rust-analyzer",),
}

SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_RE = re.compile(r"^(?P<name>[a-z0-9](?:[a-z0-9._/-]*[a-z0-9])?)@(?P<digest>sha256:[0-9a-f]{64})$")


class CatalogError(ValueError):
    """Raised when a catalog is not an acceptable trusted input."""


def _object(value: Any, fields: set[str], where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise CatalogError(f"{where} fields are noncanonical")
    return value


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise CatalogError(f"{where} must be a non-empty string")
    return value


def _digest(value: Any, where: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise CatalogError(f"{where} must be sha256:<64 lowercase hex>")
    return value


def _argv(value: Any, language: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise CatalogError(f"{language}.argv must be a non-empty array")
    if any(not isinstance(part, str) or not part or "\x00" in part for part in value):
        raise CatalogError(f"{language}.argv must contain non-empty strings")
    result = tuple(value)
    if result != CANONICAL_ARGV[language]:
        raise CatalogError(f"{language}.argv is not canonical")
    return result


def validate_catalog(
    catalog: Mapping[str, Any],
    *,
    approved_images: Mapping[str, str],
    resource_ceilings: Mapping[str, Mapping[str, int]],
) -> dict[str, Any]:
    """Validate a catalog against deployment-owned image and resource policy.

    No registry, signature, or provenance is contacted or cryptographically
    verified.  Signature/provenance fields are required attestations whose
    syntax and binding are checked only.
    """
    root = _object(catalog, {"schema", "images", "metadata"}, "catalog")
    if root["schema"] != SCHEMA:
        raise CatalogError("unsupported or missing catalog schema")
    images = _object(root["images"], set(SUPPORTED_LANGUAGES), "catalog.images")
    if set(approved_images) != set(SUPPORTED_LANGUAGES):
        raise CatalogError("approved_images must cover exactly the six languages")
    if set(resource_ceilings) != set(SUPPORTED_LANGUAGES):
        raise CatalogError("resource_ceilings must cover exactly the six languages")

    for language in SUPPORTED_LANGUAGES:
        entry = _object(
            images[language],
            {"image", "argv", "toolchain", "network", "resources"},
            f"{language} image entry",
        )
        image = _string(entry["image"], f"{language}.image")
        match = IMAGE_RE.fullmatch(image)
        if match is None:
            raise CatalogError(f"{language}.image must be a pinned registry digest")
        if image != approved_images[language]:
            raise CatalogError(f"{language}.image is not approved")
        _argv(entry["argv"], language)

        toolchain = _object(entry["toolchain"], {"name", "version", "identity"}, f"{language}.toolchain")
        name = _string(toolchain["name"], f"{language}.toolchain.name")
        version = _string(toolchain["version"], f"{language}.toolchain.version")
        identity = _string(toolchain["identity"], f"{language}.toolchain.identity")
        if identity != f"{name}@{version}":
            raise CatalogError(f"{language}.toolchain.identity must equal name@version")

        if entry["network"] is not False:
            raise CatalogError(f"{language}.network must be false")
        resources = _object(entry["resources"], {"cpus", "memory_mib"}, f"{language}.resources")
        ceiling = _object(resource_ceilings[language], {"cpus", "memory_mib"}, f"{language} resource ceiling")
        for field in ("cpus", "memory_mib"):
            value = resources[field]
            limit = ceiling[field]
            if type(value) is not int or value < 1 or type(limit) is not int or limit < 1:
                raise CatalogError(f"{language}.resources.{field} must be a positive integer")
            if value > limit:
                raise CatalogError(f"{language}.resources.{field} exceeds ceiling")

    metadata = _object(root["metadata"], {"signature", "provenance"}, "catalog.metadata")
    signature = _object(metadata["signature"], {"algorithm", "key_id", "value"}, "signature")
    _string(signature["algorithm"], "signature.algorithm")
    _string(signature["key_id"], "signature.key_id")
    _string(signature["value"], "signature.value")
    provenance = _object(metadata["provenance"], {"source", "source_commit", "build_digest"}, "provenance")
    _string(provenance["source"], "provenance.source")
    _string(provenance["source_commit"], "provenance.source_commit")
    _digest(provenance["build_digest"], "provenance.build_digest")
    return {"languages": SUPPORTED_LANGUAGES, "images": tuple(images)}


def canonical_bytes(catalog: Mapping[str, Any]) -> bytes:
    """Return the one permitted JSON encoding (ASCII, sorted, newline-terminated)."""
    try:
        encoded = json.dumps(catalog, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise CatalogError(f"catalog is not canonical JSON: {exc}") from exc
    return (encoded + "\n").encode("ascii")


def catalog_digest(
    catalog: Mapping[str, Any],
    *,
    approved_images: Mapping[str, str],
    resource_ceilings: Mapping[str, Mapping[str, int]],
) -> str:
    validate_catalog(catalog, approved_images=approved_images, resource_ceilings=resource_ceilings)
    return "sha256:" + hashlib.sha256(canonical_bytes(catalog)).hexdigest()
