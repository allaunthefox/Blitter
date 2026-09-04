#!/usr/bin/env python3
"""Regression tests for the MathPunch diagnostic/artifact binding.

Covers scope binding (project/commit/toolchain/verifier/request digests),
stale-scope rejection, payload tamper rejection, and the core authority rule:
direct kernel replay is authoritative and worker diagnostics can never
override it in either direction.
"""
from __future__ import annotations

import copy
import unittest

try:
    from . import mathpunch_binding as binding
except ImportError:
    import mathpunch_binding as binding


SCOPE = {
    "project": "MathPunchFiniteState",
    "commit": "a" * 40,
    "toolchain": "lean@4.15.0",
    "verifier": "u192-normalization-matrix-v1",
    "request_digest": "sha256:" + "c" * 64,
}


def kernel_pass(_scope) -> bool:
    return True


def kernel_fail(_scope) -> bool:
    return False


class BindingTests(unittest.TestCase):
    def test_bind_and_verify_roundtrip(self):
        record = binding.bind_record(
            kind="diagnostic", payload={"verdict": True, "messages": []}, **SCOPE
        )
        info = binding.verify_binding(record)
        self.assertEqual(info["scope_key"], record["scope_key"])
        self.assertEqual(record["authority"], "kernel-replay-only")

    def test_each_scope_field_is_bound(self):
        record = binding.bind_record(
            kind="artifact", payload={"uri": "output/result.json"}, **SCOPE
        )
        for field, bad in (
            ("project", "OtherProject"),
            ("commit", "b" * 40),
            ("toolchain", "lean@4.16.0"),
            ("verifier", "other-verifier-v1"),
            ("request_digest", "sha256:" + "d" * 64),
        ):
            with self.subTest(field=field):
                tampered = copy.deepcopy(record)
                tampered["scope"][field] = bad
                with self.assertRaises(binding.BindingError):
                    binding.verify_binding(tampered)

    def test_payload_tamper_fails_closed(self):
        record = binding.bind_record(
            kind="diagnostic", payload={"verdict": True}, **SCOPE
        )
        tampered = copy.deepcopy(record)
        tampered["payload"]["verdict"] = False
        with self.assertRaises(binding.BindingError):
            binding.verify_binding(tampered)

    def test_malformed_digests_fail_closed(self):
        bad = dict(SCOPE)
        bad["commit"] = "not-a-commit"
        with self.assertRaises(binding.BindingError):
            binding.bind_record(kind="diagnostic", payload={}, **bad)
        bad = dict(SCOPE)
        bad["request_digest"] = "md5:abc"
        with self.assertRaises(binding.BindingError):
            binding.bind_record(kind="diagnostic", payload={}, **bad)

    def test_kernel_replay_is_authoritative_over_worker_claims(self):
        # Worker claims PASS but the kernel fails: kernel wins.
        record = binding.bind_record(
            kind="diagnostic", payload={"verdict": True}, **SCOPE
        )
        result = binding.kernel_replay(record, kernel_fail)
        self.assertFalse(result["authoritative_verdict"])
        self.assertFalse(result["diagnostic_agrees"])
        # Worker claims FAIL but the kernel passes: kernel still wins.
        record = binding.bind_record(
            kind="diagnostic", payload={"verdict": False}, **SCOPE
        )
        result = binding.kernel_replay(record, kernel_pass)
        self.assertTrue(result["authoritative_verdict"])
        self.assertFalse(result["diagnostic_agrees"])

    def test_replay_agreement_is_telemetry_only(self):
        record = binding.bind_record(
            kind="diagnostic", payload={"verdict": True}, **SCOPE
        )
        result = binding.kernel_replay(record, kernel_pass)
        self.assertTrue(result["authoritative_verdict"])
        self.assertTrue(result["diagnostic_agrees"])

    def test_stale_scope_replay_fails_closed(self):
        record = binding.bind_record(
            kind="diagnostic", payload={"verdict": True}, **SCOPE
        )
        with self.assertRaisesRegex(binding.BindingError, "stale scope"):
            binding.kernel_replay(record, kernel_pass, expected_scope_key="sha256:" + "0" * 64)

    def test_kernel_exceptions_and_nonboolean_verdicts_fail_closed(self):
        record = binding.bind_record(
            kind="diagnostic", payload={"verdict": True}, **SCOPE
        )

        def exploding(_scope):
            raise RuntimeError("boom")

        with self.assertRaises(binding.BindingError):
            binding.kernel_replay(record, exploding)

        def vague(_scope):
            return "probably"

        with self.assertRaises(binding.BindingError):
            binding.kernel_replay(record, vague)


if __name__ == "__main__":
    unittest.main()
