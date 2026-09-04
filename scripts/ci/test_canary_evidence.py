#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import tempfile
import unittest

try:
    from . import canary_evidence as evidence
except ImportError:
    import canary_evidence as evidence


ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "canary" / "scalar-candidate-v1.json"
D0 = "sha256:" + "0" * 64
D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64


class CanaryEvidenceTests(unittest.TestCase):
    def test_canonical_receipt_fixture_round_trips(self):
        receipt = evidence.load_canonical(FIXTURE)
        self.assertEqual(FIXTURE.read_bytes(), evidence.canonical_bytes(receipt))
        self.assertEqual(receipt["status"], "captured-live")
        self.assertEqual(
            receipt["evidence"]["scalar"]["request_sha256"],
            receipt["evidence"]["candidate"]["request_sha256"],
        )

    def test_noncanonical_receipt_is_rejected(self):
        receipt = json.loads(FIXTURE.read_text(encoding="ascii"))
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "receipt.json"
            path.write_text(json.dumps(receipt, indent=2), encoding="ascii")
            with self.assertRaisesRegex(evidence.EvidenceError, "canonical encoding"):
                evidence.load_canonical(path)

    def test_redaction_removes_nested_credentials_and_authorization(self):
        raw = {
            "token": "top-secret",
            "nested": {"password": "also-secret", "note": "Bearer abc123"},
            "url": "https://user:pass@/path",
        }
        redacted = evidence.redact(raw)
        rendered = json.dumps(redacted, sort_keys=True)
        self.assertNotIn("top-secret", rendered)
        self.assertNotIn("also-secret", rendered)
        self.assertNotIn("abc123", rendered)
        self.assertNotIn("user:pass", rendered)
        self.assertEqual(redacted["token"], "[REDACTED]")

    def test_only_tag_free_digest_pinned_image_references_are_accepted(self):
        receipt = evidence.blocked_local_receipt(
            source_commit="a" * 40, candidate_image="blitter-canary@" + D0
        )
        self.assertEqual(receipt["candidate_image"], "blitter-canary@" + D0)
        for bad in ("blitter-canary:latest", "blitter-canary:latest@" + D0, "blitter-canary"):
            with self.subTest(image=bad), self.assertRaises(evidence.EvidenceError):
                evidence.blocked_local_receipt(source_commit="a" * 40, candidate_image=bad)

    def test_local_no_adapter_is_blocked_without_evidence(self):
        receipt = evidence.blocked_local_receipt(
            source_commit="a" * 40, candidate_image="blitter-canary@" + D0
        )
        self.assertEqual(receipt["status"], "blocked-local-no-adapter")
        self.assertFalse(receipt["live"])
        self.assertIsNone(receipt["adapter"])
        self.assertEqual(receipt["evidence"], {"scalar": None, "candidate": None})

        tampered = copy.deepcopy(receipt)
        tampered["evidence"]["scalar"] = {"request_sha256": D1, "response_sha256": D2}
        with self.assertRaises(evidence.EvidenceError):
            evidence.validate_receipt(tampered)

    def test_external_verifier_requires_matching_digest_and_bound_pass(self):
        receipt = evidence.load_canonical(FIXTURE)
        with tempfile.TemporaryDirectory() as directory:
            verifier = pathlib.Path(directory) / "verifier"
            receipt_digest = evidence.sha256_bytes(evidence.canonical_bytes(receipt))
            result = {
                "schema": evidence.VERIFIER_RESPONSE_SCHEMA,
                "receipt_sha256": receipt_digest,
                "verdict": "pass",
                "authority": "diagnostic-only",
            }
            verifier.write_text(
                "#!/bin/sh\nread ignored\nprintf '%s\\n' '" + json.dumps(
                    result, sort_keys=True, separators=(",", ":")
                ) + "'\n",
                encoding="ascii",
            )
            verifier.chmod(0o700)
            pin = "sha256:" + hashlib.sha256(verifier.read_bytes()).hexdigest()
            verified = evidence.invoke_external_verifier(
                receipt, verifier=verifier, verifier_sha256=pin
            )
            self.assertEqual(verified["schema"], evidence.VERIFICATION_SCHEMA)
            self.assertEqual(verified["verifier_sha256"], pin)
            with self.assertRaisesRegex(evidence.EvidenceError, "digest"):
                evidence.invoke_external_verifier(
                    receipt, verifier=verifier, verifier_sha256="sha256:" + "f" * 64
                )


if __name__ == "__main__":
    unittest.main()
