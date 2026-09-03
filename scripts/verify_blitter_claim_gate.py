#!/usr/bin/env python3
"""Fail-closed authority gate for docs/specs/BLITTER_CLAIM_REGISTRY_V1.md.

This gate does not promote claims. It prevents a scoped BLITTER row from being
marked PROVED_LOCALLY unless a machine-readable admitted evidence receipt exists.
It also pins the registry's frozen-ISA, normalization, and authority-separation
language so later prose edits cannot silently widen the claim surface.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ALLOWED_STATUS = {
    "PROVED_LOCALLY",
    "EXTERNALLY_ASSUMED",
    "ENGINEERING_CONVENTION",
    "EMPIRICAL_RESULT",
    "HYPOTHESIS",
    "REJECTED",
    "QUARANTINED",
}
CLAIM_RE = re.compile(r"^\| `(?P<claim>BLITTER-\d{2})` \|.*?\| `(?P<status>[A-Z_]+)` \|", re.M)
REQUIRED_CLAIMS = {f"BLITTER-{i:02d}" for i in range(1, 11)}


class GateError(RuntimeError):
    pass


def parse_registry(text: str) -> dict[str, str]:
    rows = {m.group("claim"): m.group("status") for m in CLAIM_RE.finditer(text)}
    missing = sorted(REQUIRED_CLAIMS - rows.keys())
    extra = sorted(rows.keys() - REQUIRED_CLAIMS)
    if missing:
        raise GateError(f"missing BLITTER registry rows: {missing}")
    if extra:
        raise GateError(f"unexpected BLITTER registry rows: {extra}")
    for claim, status in rows.items():
        if status not in ALLOWED_STATUS:
            raise GateError(f"{claim} has invalid status {status}")
    return rows


def validate_receipt(claim: str, path: Path) -> None:
    if not path.is_file():
        raise GateError(f"{claim} is PROVED_LOCALLY but receipt is missing: {path}")
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        raise GateError(f"{claim} receipt is not valid JSON: {path}: {exc}") from exc

    required = {
        "schema",
        "claim_id",
        "status",
        "source_commit",
        "evidence_hashes",
        "falsifiers",
        "independence_tier",
    }
    missing = sorted(required - data.keys())
    if missing:
        raise GateError(f"{claim} receipt missing fields: {missing}")
    if data["schema"] != "mathpunch.blitter-claim-evidence.v1":
        raise GateError(f"{claim} receipt schema mismatch")
    if data["claim_id"] != claim or data["status"] != "PASS":
        raise GateError(f"{claim} receipt identity/status mismatch")
    if not isinstance(data["source_commit"], str) or len(data["source_commit"]) != 40:
        raise GateError(f"{claim} receipt source_commit must be a full commit hash")
    if not isinstance(data["evidence_hashes"], dict) or not data["evidence_hashes"]:
        raise GateError(f"{claim} receipt has no bound evidence hashes")
    if not isinstance(data["falsifiers"], list) or not data["falsifiers"]:
        raise GateError(f"{claim} receipt has no falsifier results")
    if not isinstance(data["independence_tier"], str) or not data["independence_tier"]:
        raise GateError(f"{claim} receipt has no independence tier")


def validate_registry(registry: Path, receipt_dir: Path) -> dict[str, str]:
    text = registry.read_text()
    rows = parse_registry(text)

    # These phrases encode trust-boundary decisions that must not disappear in a
    # documentation-only edit. Explicit mixed layouts are supported when tagged;
    # ambient/native/auto interpretation is forbidden.
    required_phrases = [
        "no host-native, auto-detection, or generic mixed mode",
        "`BLITTER-ISA-V1` is the single semantic source",
        "`u192-format-matrix-v1` covers the `2×2` product",
        "`blitter.wgsl` is not part of this carry-chain claim",
        "ordered disjoint gap-free partition",
        "semantic-profile digest binds which specification was targeted; it is **not** a conformance proof",
        "lease/fencing has separate safety/liveness obligations",
        "contract-removal",
    ]
    for phrase in required_phrases:
        if phrase not in text:
            raise GateError(f"registry contract phrase missing: {phrase!r}")

    for claim, status in rows.items():
        if status == "PROVED_LOCALLY":
            validate_receipt(claim, receipt_dir / f"{claim}.json")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("docs/specs/BLITTER_CLAIM_REGISTRY_V1.md"),
    )
    parser.add_argument(
        "--receipt-dir",
        type=Path,
        default=Path("project/results/blitter_claims"),
    )
    args = parser.parse_args()
    try:
        rows = validate_registry(args.registry, args.receipt_dir)
    except (OSError, GateError) as exc:
        print(f"BLITTER_CLAIM_GATE: FAIL: {exc}")
        return 1

    proved = sorted(claim for claim, status in rows.items() if status == "PROVED_LOCALLY")
    print(f"BLITTER_CLAIM_GATE: PASS: rows={len(rows)} proved={proved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
