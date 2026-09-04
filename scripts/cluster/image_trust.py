#!/usr/bin/env python3
"""Real image-trust verification for the LSP image catalog.

This module closes the trust gap left by ``image_catalog.validate_catalog``,
which deliberately performs no cryptography.  It provides:

* pure-Python Ed25519 (RFC 8032) signature verification over the catalog's
  canonical bytes -- no third-party dependency, no registry contact;
* provenance allowlist binding (approved build sources);
* SBOM record verification and binding to the approved image set;
* a deployment-owned policy loader (``load_policy``) whose
  ``approved_images`` / ``approved_keys`` / ``approved_sources`` /
  ``resource_ceilings`` are the trust roots.  Values are never copied from the
  catalog itself.

A catalog signature is the Ed25519 signature of
``image_catalog.canonical_bytes(catalog)``.  The catalog metadata format is
unchanged; only the accepted ``signature.algorithm`` values are now
restricted to ``ed25519-sha512`` and ``signature.value`` must be 128 lowercase
hex characters.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

try:
    from .image_catalog import (
        SUPPORTED_LANGUAGES,
        canonical_bytes,
        validate_catalog,
    )
except ImportError:
    from image_catalog import (
        SUPPORTED_LANGUAGES,
        canonical_bytes,
        validate_catalog,
    )

SCHEMA = "mathpunch.blitter-lsp-image-catalog.v1"
SBOM_SCHEMA = "mathpunch.blitter-image-sbom.v1"
SIGNATURE_ALGORITHM = "ed25519-sha512"
SBOM_MEDIA_TYPES = frozenset(
    {"application/vnd.cyclonedx+json", "application/spdx+json"}
)
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
HEX128_RE = re.compile(r"^[0-9a-f]{128}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class TrustError(ValueError):
    """Raised when image trust cannot be established."""


# =====================================================================
# Ed25519 (RFC 8032), verification and test signing, pure Python.
# =====================================================================

_ED_P = 2**255 - 19
_ED_L = 2**252 + 27742317777372353535851937790883648493


def _ed_inv(x: int) -> int:
    return pow(x, _ED_P - 2, _ED_P)


_ED_D = (-121665 * _ed_inv(121666)) % _ED_P
_ED_I = pow(2, (_ED_P - 1) // 4, _ED_P)


def _ed_xrecover(y: int) -> int:
    xx = (y * y - 1) * _ed_inv((_ED_D * y * y + 1) % _ED_P) % _ED_P
    x = pow(xx, (_ED_P + 3) // 8, _ED_P)
    if (x * x - xx) % _ED_P != 0:
        x = (x * _ED_I) % _ED_P
    if x & 1:
        x = _ED_P - x
    return x


def _ed_decode_point(data: bytes) -> tuple[int, int]:
    if len(data) != 32:
        raise TrustError("ed25519 point must be 32 bytes")
    raw = bytearray(data)
    x_odd = (raw[31] >> 7) & 1
    raw[31] &= 0x7F
    y = int.from_bytes(bytes(raw), "little")
    if y >= _ED_P:
        raise TrustError("ed25519 point y-coordinate out of range")
    x = _ed_xrecover(y)
    if x & 1 != x_odd:
        x = _ED_P - x
    return (x, y)


def _ed_encode_point(x: int, y: int) -> bytes:
    raw = bytearray(y.to_bytes(32, "little"))
    if x & 1:
        raw[31] |= 0x80
    return bytes(raw)


def _ed_add(p: tuple[int, int], q: tuple[int, int]) -> tuple[int, int]:
    # Twisted Edwards addition for a = -1: the y-numerator is y1*y2 + x1*x2.
    x1, y1 = p
    x2, y2 = q
    x1x2 = (x1 * x2) % _ED_P
    y1y2 = (y1 * y2) % _ED_P
    dx1x2y1y2 = (_ED_D * x1x2 % _ED_P) * y1y2 % _ED_P
    x3 = ((x1 * y2 + y1 * x2) % _ED_P) * _ed_inv((1 + dx1x2y1y2) % _ED_P) % _ED_P
    y3 = ((y1y2 + x1x2) % _ED_P) * _ed_inv((1 - dx1x2y1y2) % _ED_P) % _ED_P
    return (x3, y3)


def _ed_base() -> tuple[int, int]:
    y = (4 * _ed_inv(5)) % _ED_P
    x = _ed_xrecover(y)
    if x & 1:
        x = _ED_P - x
    return (x, y)


_ED_BASE = _ed_base()
_ED_NEUTRAL = (0, 1)


def _ed_scalarmult(p: tuple[int, int], e: int) -> tuple[int, int]:
    result = _ED_NEUTRAL
    addend = p
    while e:
        if e & 1:
            result = _ed_add(result, addend)
        addend = _ed_add(addend, addend)
        e >>= 1
    return result


def ed25519_publickey_from_seed(seed: bytes) -> bytes:
    """Derive the 32-byte public key for a 32-byte private seed (test signing)."""
    if len(seed) != 32:
        raise TrustError("ed25519 seed must be 32 bytes")
    digest = hashlib.sha512(seed).digest()
    bits = bytearray(digest[:32])
    bits[0] &= 0xF8
    bits[31] &= 0x3F
    bits[31] |= 0x40
    scalar = int.from_bytes(bytes(bits), "little")
    x, y = _ed_scalarmult(_ED_BASE, scalar)
    return _ed_encode_point(x, y)


def ed25519_sign(seed: bytes, message: bytes) -> bytes:
    """Deterministic RFC 8032 signature (test/tooling use; deployment signs offline)."""
    if len(seed) != 32:
        raise TrustError("ed25519 seed must be 32 bytes")
    digest = hashlib.sha512(seed).digest()
    bits = bytearray(digest[:32])
    bits[0] &= 0xF8
    bits[31] &= 0x3F
    bits[31] |= 0x40
    scalar = int.from_bytes(bytes(bits), "little")
    prefix = digest[32:]
    public = ed25519_publickey_from_seed(seed)
    nonce = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % _ED_L
    rx, ry = _ed_scalarmult(_ED_BASE, nonce)
    commitment = _ed_encode_point(rx, ry)
    challenge = int.from_bytes(hashlib.sha512(commitment + public + message).digest(), "little") % _ED_L
    proof = (nonce + challenge * scalar) % _ED_L
    return commitment + proof.to_bytes(32, "little")


def ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Return True only for a valid RFC 8032 signature; never raises on bad input."""
    try:
        if len(public_key) != 32 or len(signature) != 64:
            return False
        r_bytes, s_bytes = signature[:32], signature[32:]
        s_int = int.from_bytes(s_bytes, "little")
        if s_int >= _ED_L:
            return False
        r_point = _ed_decode_point(r_bytes)
        a_point = _ed_decode_point(public_key)
        # Reject small-order public keys (subgroup confinement).
        if _ed_scalarmult(a_point, _ED_L) != _ED_NEUTRAL:
            return False
        challenge = int.from_bytes(
            hashlib.sha512(r_bytes + public_key + message).digest(), "little"
        ) % _ED_L
        left = _ed_scalarmult(_ED_BASE, s_int)
        right = _ed_add(r_point, _ed_scalarmult(a_point, challenge))
        return left == right
    except (TrustError, ValueError):
        return False


# =====================================================================
# Catalog / provenance / SBOM trust.
# =====================================================================

def _policy_string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise TrustError(f"{where} must be a non-empty string")
    return value


def load_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a deployment-owned trust policy; fail closed on any defect."""
    if not isinstance(policy, Mapping):
        raise TrustError("image trust policy must be a JSON object")
    allowed = {
        "approved_images", "resource_ceilings", "approved_keys",
        "approved_sources", "sbom", "require_sbom", "status", "note",
    }
    unknown = set(policy) - allowed
    if unknown:
        raise TrustError(f"image trust policy has unknown fields: {sorted(unknown)}")
    for field in ("approved_images", "resource_ceilings", "approved_keys"):
        if field not in policy:
            raise TrustError(f"image trust policy is missing {field}")
    approved_images = policy["approved_images"]
    if not isinstance(approved_images, Mapping) or set(approved_images) != set(SUPPORTED_LANGUAGES):
        raise TrustError("approved_images must cover exactly the six languages")
    for language, image in approved_images.items():
        _policy_string(image, f"approved_images.{language}")
    ceilings = policy["resource_ceilings"]
    if not isinstance(ceilings, Mapping) or set(ceilings) != set(SUPPORTED_LANGUAGES):
        raise TrustError("resource_ceilings must cover exactly the six languages")
    approved_keys = policy["approved_keys"]
    if (
        not isinstance(approved_keys, Mapping)
        or not approved_keys
        or any(not isinstance(k, str) or not k for k in approved_keys)
    ):
        raise TrustError("approved_keys must be a non-empty map of key_id to public key")
    for key_id, public in approved_keys.items():
        if not isinstance(public, str) or HEX64_RE.fullmatch(public) is None:
            raise TrustError(f"approved_keys.{key_id} must be 64 lowercase hex (32-byte key)")
    approved_sources: tuple[str, ...] = ()
    if "approved_sources" in policy:
        sources = policy["approved_sources"]
        if (
            not isinstance(sources, (list, tuple))
            or not sources
            or any(not isinstance(s, str) or not s for s in sources)
        ):
            raise TrustError("approved_sources must be a non-empty list of strings")
        approved_sources = tuple(sources)
    require_sbom = policy.get("require_sbom", True)
    if type(require_sbom) is not bool:
        raise TrustError("require_sbom must be boolean")
    sbom = policy.get("sbom", {})
    if not isinstance(sbom, Mapping):
        raise TrustError("sbom must be a map of language to SBOM record")
    if require_sbom and set(sbom) != set(SUPPORTED_LANGUAGES):
        raise TrustError("sbom must cover exactly the six languages when require_sbom is true")
    for language in sbom:
        if language not in SUPPORTED_LANGUAGES:
            raise TrustError(f"sbom has an unsupported language: {language}")
    return {
        "approved_images": dict(approved_images),
        "resource_ceilings": {k: dict(v) for k, v in ceilings.items()},
        "approved_keys": dict(approved_keys),
        "approved_sources": approved_sources,
        "sbom": dict(sbom),
        "require_sbom": require_sbom,
    }


def verify_catalog_signature(
    catalog: Mapping[str, Any], *, approved_keys: Mapping[str, str]
) -> str:
    """Verify the catalog's Ed25519 signature; return the authorizing key_id."""
    if not isinstance(catalog, Mapping):
        raise TrustError("catalog must be a JSON object")
    metadata = catalog.get("metadata")
    if not isinstance(metadata, Mapping):
        raise TrustError("catalog metadata is missing")
    signature = metadata.get("signature")
    if not isinstance(signature, Mapping) or set(signature) != {"algorithm", "key_id", "value"}:
        raise TrustError("catalog signature fields are noncanonical")
    if signature.get("algorithm") != SIGNATURE_ALGORITHM:
        raise TrustError(f"catalog signature algorithm must be {SIGNATURE_ALGORITHM}")
    key_id = signature.get("key_id")
    if not isinstance(key_id, str) or key_id not in approved_keys:
        raise TrustError("catalog signature key_id is not approved")
    value = signature.get("value")
    if not isinstance(value, str) or HEX128_RE.fullmatch(value) is None:
        raise TrustError("catalog signature value must be 128 lowercase hex")
    try:
        public_key = bytes.fromhex(approved_keys[key_id])
        raw_signature = bytes.fromhex(value)
    except ValueError as exc:
        raise TrustError("catalog signature material is not valid hex") from exc
    message = signing_bytes(catalog)
    if not ed25519_verify(public_key, message, raw_signature):
        raise TrustError("catalog signature verification failed")
    return key_id


def signing_bytes(catalog: Mapping[str, Any]) -> bytes:
    """Canonical bytes covered by the catalog signature.

    The signature's own ``value`` is blanked before encoding so signing is
    well-defined: the signature authenticates every catalog field except its
    own value.  Signers and verifiers must both use this exact projection.
    """
    if not isinstance(catalog, Mapping):
        raise TrustError("catalog must be a JSON object")
    try:
        unsigned = dict(catalog)
        unsigned["metadata"] = dict(catalog["metadata"])
        unsigned["metadata"]["signature"] = dict(catalog["metadata"]["signature"])
        unsigned["metadata"]["signature"]["value"] = ""
    except (KeyError, TypeError, AttributeError) as exc:
        raise TrustError(f"catalog signature block is missing: {exc}") from exc
    try:
        return canonical_bytes(unsigned)
    except Exception as exc:
        raise TrustError(f"catalog is not canonical JSON: {exc}") from exc


def verify_catalog_provenance(
    catalog: Mapping[str, Any], *, approved_sources: tuple[str, ...] | list[str]
) -> dict[str, str]:
    """Bind catalog provenance to the deployment-approved source allowlist."""
    if not isinstance(catalog, Mapping):
        raise TrustError("catalog must be a JSON object")
    metadata = catalog.get("metadata")
    if not isinstance(metadata, Mapping):
        raise TrustError("catalog metadata is missing")
    provenance = metadata.get("provenance")
    if (
        not isinstance(provenance, Mapping)
        or set(provenance) != {"source", "source_commit", "build_digest"}
    ):
        raise TrustError("catalog provenance fields are noncanonical")
    source = provenance.get("source")
    if not isinstance(source, str) or source not in approved_sources:
        raise TrustError("catalog provenance source is not approved")
    commit = provenance.get("source_commit")
    if not isinstance(commit, str) or COMMIT_RE.fullmatch(commit) is None:
        raise TrustError("catalog provenance source_commit must be 40 lowercase hex")
    digest = provenance.get("build_digest")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise TrustError("catalog provenance build_digest must be sha256:<64 hex>")
    return {"source": source, "source_commit": commit, "build_digest": digest}


def verify_sbom_record(
    sbom: Mapping[str, Any], *, language: str, approved_image: str
) -> dict[str, str]:
    """Verify one SBOM record and its binding to the approved image."""
    if not isinstance(sbom, Mapping) or set(sbom) != {
        "schema", "language", "image", "sbom_digest",
        "sbom_media_type", "source", "source_commit", "build_digest",
    }:
        raise TrustError(f"sbom record for {language} has noncanonical fields")
    if sbom.get("schema") != SBOM_SCHEMA:
        raise TrustError(f"sbom record for {language} has the wrong schema")
    if sbom.get("language") != language:
        raise TrustError(f"sbom record language mismatch: {sbom.get('language')}")
    image = sbom.get("image")
    if image != approved_image:
        raise TrustError(f"sbom record for {language} does not bind the approved image")
    digest = sbom.get("sbom_digest")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise TrustError(f"sbom record for {language} has an invalid sbom_digest")
    media_type = sbom.get("sbom_media_type")
    if media_type not in SBOM_MEDIA_TYPES:
        raise TrustError(f"sbom record for {language} has an unsupported media type")
    for field in ("source", "source_commit", "build_digest"):
        value = sbom.get(field)
        if not isinstance(value, str) or not value:
            raise TrustError(f"sbom record for {language} has an invalid {field}")
    if COMMIT_RE.fullmatch(sbom["source_commit"]) is None:
        raise TrustError(f"sbom record for {language} has an invalid source_commit")
    if SHA256_RE.fullmatch(sbom["build_digest"]) is None:
        raise TrustError(f"sbom record for {language} has an invalid build_digest")
    return {"language": language, "image": image, "sbom_digest": digest}


def verify_image_trust(catalog: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    """Full trust verification: catalog policy, signature, provenance, SBOM."""
    trusted = load_policy(policy)
    validate_catalog(
        catalog,
        approved_images=trusted["approved_images"],
        resource_ceilings=trusted["resource_ceilings"],
    )
    key_id = verify_catalog_signature(catalog, approved_keys=trusted["approved_keys"])
    provenance: dict[str, str] | None = None
    if trusted["approved_sources"]:
        provenance = verify_catalog_provenance(
            catalog, approved_sources=trusted["approved_sources"]
        )
    sbom_digests: dict[str, str] = {}
    if trusted["require_sbom"]:
        for language in SUPPORTED_LANGUAGES:
            info = verify_sbom_record(
                trusted["sbom"][language],
                language=language,
                approved_image=trusted["approved_images"][language],
            )
            sbom_digests[language] = info["sbom_digest"]
        catalog_commit = catalog["metadata"]["provenance"]["source_commit"]
        for language in SUPPORTED_LANGUAGES:
            if trusted["sbom"][language]["source_commit"] != catalog_commit:
                raise TrustError(
                    f"sbom record for {language} does not bind the catalog source_commit"
                )
    return {
        "languages": SUPPORTED_LANGUAGES,
        "signature_key_id": key_id,
        "provenance": provenance,
        "sbom_digests": sbom_digests,
    }
