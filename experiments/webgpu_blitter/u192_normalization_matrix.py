"""B4-style certificate layer for ISA-tagged U192 normalization.

The four admitted U192 transport formats form a finite normalization matrix.  A
row tells the ABI how to map one tagged representation to the canonical internal
carrier.  Certificates are digest-bound projections, deliberately mirroring the
B4 matrix-certificate rule: a verified projection authorizes the representation
conversion only; it does not prove downstream arithmetic or a mathematical claim.
"""

from __future__ import annotations

import hashlib
import json

from u192_codec import (
    CANONICAL_SCHEMA,
    SUPPORTED_FORMAT_TAGS,
    format_tag_for_layout,
    make_tagged_u192,
    normalize_tagged_u192,
    validate_format_tag,
)

CERT_SCHEMA = "mathpunch.u192-normalization-certificate.v1"
MATRIX_ID = "u192-format-matrix-v1"
VERIFIED_PROJECTION = "verifiedProjection"
MISSING_PROJECTION = "missingProjection"
DIGEST_MISMATCH = "digestMismatch"
UNSUPPORTED_REPRESENTATION = "unsupportedRepresentation"


def _canonical_json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _sha256(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def normalization_matrix():
    """Return the complete four-row admitted V1 normalization matrix."""
    rows = []
    for tag in sorted(SUPPORTED_FORMAT_TAGS):
        fields = validate_format_tag(tag)
        rows.append({
            "format_tag": tag,
            "layout": fields["layout"],
            "limb_order": fields["limb_order"],
            "byte_order": fields["byte_order"],
            "canonical_schema": CANONICAL_SCHEMA,
        })
    return rows


def matrix_is_exact_v1():
    """Structural exactness: four unique tags cover the 2x2 order product once."""
    rows = normalization_matrix()
    coordinates = {(row["limb_order"], row["byte_order"]) for row in rows}
    return (
        len(rows) == 4
        and len({row["format_tag"] for row in rows}) == 4
        and coordinates == {
            ("lsw", "little"),
            ("lsw", "big"),
            ("msw", "little"),
            ("msw", "big"),
        }
    )


def normalize_with_certificate(structure):
    """Normalize one descriptor and emit a digest-bound projection certificate."""
    canonical = normalize_tagged_u192(structure)
    fields = validate_format_tag(structure["format_tag"])
    reencoded = make_tagged_u192(canonical["words"], fields["layout"])
    roundtrip = reencoded == structure
    cert = {
        "schema": CERT_SCHEMA,
        "matrix_id": MATRIX_ID,
        "format_tag": structure["format_tag"],
        "layout": fields["layout"],
        "input_digest": _sha256(_canonical_json_bytes(structure)),
        "canonical_digest": _sha256(_canonical_json_bytes(canonical)),
        "input_digest_bound": True,
        "canonical_digest_bound": True,
        "roundtrip_verified": roundtrip,
        "representation": fields["layout"],
        "status": VERIFIED_PROJECTION if roundtrip else DIGEST_MISMATCH,
    }
    return canonical, cert


def normalization_certificate_authorized(certificate, structure=None, canonical=None):
    """Authorize only the representation projection, never arithmetic/theorem truth."""
    if not isinstance(certificate, dict):
        return False
    required = {
        "schema",
        "matrix_id",
        "format_tag",
        "layout",
        "input_digest",
        "canonical_digest",
        "input_digest_bound",
        "canonical_digest_bound",
        "roundtrip_verified",
        "representation",
        "status",
    }
    if set(certificate) != required:
        return False
    if certificate["schema"] != CERT_SCHEMA or certificate["matrix_id"] != MATRIX_ID:
        return False
    try:
        fields = validate_format_tag(certificate["format_tag"])
    except ValueError:
        return False
    if certificate["layout"] != fields["layout"] or certificate["representation"] != fields["layout"]:
        return False
    if not certificate["input_digest_bound"] or not certificate["canonical_digest_bound"]:
        return False
    if not certificate["roundtrip_verified"] or certificate["status"] != VERIFIED_PROJECTION:
        return False

    if structure is not None:
        if certificate["input_digest"] != _sha256(_canonical_json_bytes(structure)):
            return False
        try:
            normalized = normalize_tagged_u192(structure)
        except ValueError:
            return False
        if canonical is None:
            canonical = normalized
        elif normalized != canonical:
            return False
    if canonical is not None:
        if certificate["canonical_digest"] != _sha256(_canonical_json_bytes(canonical)):
            return False
    return True
