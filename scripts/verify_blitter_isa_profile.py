#!/usr/bin/env python3
"""Mechanical validator for the frozen BLITTER-ISA-V1 semantic profile.

The semantic digest identifies the exact profile. It is deliberately not an
implementation-conformance proof. This gate also compares the profile's finite
U192 representation surface with the executable codec/matrix used by the
reference tooling, and checks that operational/security protocols remain outside
the arithmetic opcode set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BLITTER = ROOT / "experiments" / "webgpu_blitter"
if str(BLITTER) not in sys.path:
    sys.path.insert(0, str(BLITTER))

from u192_codec import (  # noqa: E402
    LAYOUT_TO_TAG,
    SUPPORTED_FORMAT_TAGS,
    decode_format_tag_raw,
    encode_format_tag_raw,
    validate_format_tag,
)
from u192_normalization_matrix import matrix_is_exact_v1, normalization_matrix  # noqa: E402

PROFILE_SCHEMA = "mathpunch.blitter-isa-profile.v1"
PROFILE_ID = "BLITTER-ISA-V1"
DIGEST_PREFIX = "sha256:"
EXPECTED_OPCODES = {
    "U192_NORMALIZE",
    "U192_ADD_MOD",
    "U192_MAX",
    "PREFIX_COVER_VALIDATE",
    "GLOBAL_MAX_FROM_COVER",
}
REQUIRED_PROTOCOL_LAYERS = {
    "lease_and_fencing",
    "heartbeat",
    "ratchet_clock",
    "environmental_jitter",
    "thermal_ras_ecc",
    "security_plugin",
}


class ProfileError(RuntimeError):
    pass


def canonical_semantic_bytes(profile: dict) -> bytes:
    if not isinstance(profile, dict):
        raise ProfileError("profile must be a JSON object")
    semantic = dict(profile)
    semantic.pop("semantic_digest", None)
    return json.dumps(
        semantic,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def semantic_digest(profile: dict) -> str:
    return DIGEST_PREFIX + hashlib.sha256(canonical_semantic_bytes(profile)).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProfileError(message)


def validate_profile(profile: dict, *, lock_text: str | None = None, root: Path = ROOT) -> str:
    _require(profile.get("schema") == PROFILE_SCHEMA, "profile schema mismatch")
    _require(profile.get("profile_id") == PROFILE_ID, "profile id mismatch")

    digest = semantic_digest(profile)
    _require(profile.get("semantic_digest") == digest, "embedded semantic digest mismatch")
    if lock_text is not None:
        _require(lock_text == digest + "\n", "semantic digest lock mismatch")

    rule = profile.get("semantic_digest_rule", {})
    _require(rule.get("algorithm") == "sha256", "semantic digest algorithm changed")
    _require(
        "not an implementation-conformance proof" in rule.get("meaning", ""),
        "semantic digest must remain explicitly non-conformance",
    )

    closed = profile.get("closed_universe", {})
    for field in (
        "mid_execution_environment_reads",
        "ambient_endianness",
        "ambient_word_size",
        "host_native_layout",
        "clock_required",
        "sensor_required",
    ):
        _require(closed.get(field) is False, f"closed-universe field must remain false: {field}")

    fmt = profile.get("canonical_types", {}).get("U192FormatByteV1", {})
    _require(fmt.get("bits") == "vv l b rrrr", "U192 format-byte layout changed")
    declared = fmt.get("admitted_tags")
    executable = {f"0x{tag:02x}": layout for layout, tag in LAYOUT_TO_TAG.items()}
    _require(declared == executable, "profile/executable U192 tag map mismatch")
    _require(
        SUPPORTED_FORMAT_TAGS == frozenset({0x00, 0x10, 0x20, 0x30}),
        "executable admitted U192 tag set changed",
    )

    # ISA-style rule: all raw bytes structurally decode/encode, while the current
    # profile admits only four canonical V1 tags.
    for tag in range(256):
        fields = decode_format_tag_raw(tag)
        rebuilt = encode_format_tag_raw(
            fields["version"],
            fields["limb_order"],
            fields["byte_order"],
            fields["reserved"],
        )
        _require(rebuilt == tag, f"raw format-byte round trip failed at {tag:#04x}")
        should_admit = tag in SUPPORTED_FORMAT_TAGS
        try:
            validate_format_tag(tag)
            did_admit = True
        except ValueError:
            did_admit = False
        _require(should_admit == did_admit, f"format admission mismatch at {tag:#04x}")

    _require(matrix_is_exact_v1(), "U192 normalization matrix is not exact 2x2 coverage")
    _require(len(normalization_matrix()) == 4, "U192 normalization matrix row count changed")

    opcodes = profile.get("opcodes", {})
    _require(set(opcodes) == EXPECTED_OPCODES, "frozen opcode set changed")
    _require(
        opcodes["U192_ADD_MOD"].get("relation")
        == "value(output) = (value(a) + value(b)) mod 2^192",
        "U192_ADD_MOD relation changed",
    )
    _require(
        opcodes["U192_MAX"].get("comparison_order")
        == "w5,w4,w3,w2,w1,w0 unsigned lexicographic",
        "U192_MAX comparison order changed",
    )

    layers = profile.get("separate_protocol_layers", {})
    _require(set(layers) == REQUIRED_PROTOCOL_LAYERS, "separate protocol-layer set changed")
    security = layers.get("security_plugin", "")
    _require("optional process-plugin ABI" in security, "security plugin must remain process-plugin based")
    _require("blocks rather than silently downgrades" in security, "secure capability must remain fail-closed")
    for forbidden_opcode in ("TLS", "STAMP", "LEASE", "HEARTBEAT", "THERMAL", "ECC", "JITTER"):
        _require(
            all(forbidden_opcode not in opcode for opcode in opcodes),
            f"operational/security layer leaked into arithmetic opcode set: {forbidden_opcode}",
        )

    b4 = profile.get("B4_scope", {})
    forbidden = set(b4.get("forbidden", []))
    _require(
        "claim merge.wgsl is an LKB/Burau computation" in forbidden,
        "B4 scope must exclude merge.wgsl from braid-theoretic interpretation",
    )
    _require(
        "claim tailgain.wgsl is an LKB/Burau computation" in forbidden,
        "B4 scope must exclude tailgain.wgsl from braid-theoretic interpretation",
    )

    conformance = profile.get("conformance", {})
    _require(
        "implementation hash equals profile hash" in conformance.get("not_conformance", []),
        "hash equality must remain explicitly non-conformance",
    )

    # Required source/spec anchors: profile validation should fail if its declared
    # witness/security machinery is deleted from the tree.
    for relative in (
        "experiments/webgpu_blitter/u192_codec.py",
        "experiments/webgpu_blitter/u192_normalization_matrix.py",
        "docs/specs/BLITTER_SECURITY_PLUGIN_ABI_V1.md",
        "MathPunchFiniteState/BlitterU192Transport.lean",
    ):
        _require((root / relative).is_file(), f"required profile artifact missing: {relative}")

    return digest


def load_profile(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"cannot load profile {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProfileError("profile root must be an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile",
        type=Path,
        default=ROOT / "docs/specs/BLITTER_ISA_PROFILE_V1.json",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=ROOT / "docs/specs/BLITTER_ISA_PROFILE_V1.sha256",
    )
    parser.add_argument(
        "--print-digest",
        action="store_true",
        help="print the computed digest even when the embedded/lock value is not yet installed",
    )
    args = parser.parse_args()

    try:
        profile = load_profile(args.profile)
        if args.print_digest:
            print(semantic_digest(profile))
            return 0
        lock_text = args.lock.read_text()
        digest = validate_profile(profile, lock_text=lock_text, root=ROOT)
    except (OSError, ProfileError) as exc:
        print(f"BLITTER_ISA_PROFILE_GATE: FAIL: {exc}")
        return 1

    print(f"BLITTER_ISA_PROFILE_GATE: PASS: {PROFILE_ID} {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
