#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path

from verify_blitter_claim_gate import GateError, parse_registry, validate_registry


CONTRACT = (
    "no host-native, auto-detection, or generic mixed mode "
    "`BLITTER-ISA-V1` is the single semantic source "
    "`u192-format-matrix-v1` covers the `2×2` product "
    "ordered disjoint gap-free partition "
    "semantic-profile digest binds which specification was targeted; it is **not** a conformance proof "
    "lease/fencing has separate safety/liveness obligations "
    "contract-removal"
)

BASE_ROWS = """# test registry
| claim_id | exact_claim | status | authority_level | artifact_or_test | falsifier | required_evidence | dependencies | forbidden_interpretations | last_reviewed |
|---|---|---|---:|---|---|---|---|---|---|
""" + "\n".join(
    f"| `BLITTER-{i:02d}` | test {CONTRACT} "
    f"{'`blitter.wgsl` is not part of this carry-chain claim ' if i == 10 else ''}| "
    f"`{'PROVED_LOCALLY' if i == 10 else 'HYPOTHESIS'}` | 1 | x | f | e | none | no | 2026-08-14 |"
    for i in range(1, 11)
)


class BlitterClaimGateTests(unittest.TestCase):
    def test_all_ten_claims_are_required(self):
        rows = parse_registry(BASE_ROWS)
        self.assertEqual(len(rows), 10)
        with self.assertRaisesRegex(GateError, "missing BLITTER registry rows"):
            parse_registry(BASE_ROWS.replace("| `BLITTER-09`", "| `NOT-BLITTER-09`"))

    def test_contract_language_removal_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "registry.md"
            receipts = root / "receipts"
            receipts.mkdir()
            for removed in (
                "no host-native, auto-detection, or generic mixed mode",
                "`BLITTER-ISA-V1` is the single semantic source",
                "`u192-format-matrix-v1` covers the `2×2` product",
                "semantic-profile digest binds which specification was targeted; it is **not** a conformance proof",
                "lease/fencing has separate safety/liveness obligations",
            ):
                with self.subTest(removed=removed):
                    registry.write_text(BASE_ROWS.replace(removed, "REMOVED", 1))
                    with self.assertRaisesRegex(GateError, "contract phrase missing"):
                        validate_registry(registry, receipts)

    def test_promotion_without_receipt_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "registry.md"
            receipts = root / "receipts"
            registry.write_text(BASE_ROWS)
            receipts.mkdir()
            with self.assertRaisesRegex(GateError, "PROVED_LOCALLY but receipt is missing"):
                validate_registry(registry, receipts)

    def test_malformed_receipt_does_not_authorize(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "registry.md"
            receipts = root / "receipts"
            registry.write_text(BASE_ROWS)
            receipts.mkdir()
            (receipts / "BLITTER-10.json").write_text(json.dumps({"status": "PASS"}))
            with self.assertRaisesRegex(GateError, "missing fields"):
                validate_registry(registry, receipts)

    def test_structurally_bound_receipt_authorizes_gate_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "registry.md"
            receipts = root / "receipts"
            registry.write_text(BASE_ROWS)
            receipts.mkdir()
            (receipts / "BLITTER-10.json").write_text(json.dumps({
                "schema": "mathpunch.blitter-claim-evidence.v1",
                "claim_id": "BLITTER-10",
                "status": "PASS",
                "source_commit": "a" * 40,
                "evidence_hashes": {"proof": "sha256:" + "0" * 64},
                "falsifiers": ["tagged-mixed-endian-negative-control:PASS"],
                "independence_tier": "I2",
            }))
            rows = validate_registry(registry, receipts)
            self.assertEqual(rows["BLITTER-10"], "PROVED_LOCALLY")


if __name__ == "__main__":
    unittest.main()
