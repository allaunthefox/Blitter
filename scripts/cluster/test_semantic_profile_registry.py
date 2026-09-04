#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

try:
    from . import semantic_profile_registry as registry
except ImportError:
    import semantic_profile_registry as registry


class SemanticProfileRegistryTests(unittest.TestCase):
    def test_registry_contains_two_distinct_versioned_profiles(self):
        self.assertEqual(
            registry.known_profile_ids(),
            (registry.U192_ADD_MOD_PROFILE_ID, registry.TERNARY_STEP_I32_PROFILE_ID),
        )
        u192 = registry.resolve_profile(
            registry.U192_ADD_MOD_PROFILE_ID, registry.U192_ADD_MOD_PROFILE_DIGEST
        )
        ternary = registry.resolve_profile(
            registry.TERNARY_STEP_I32_PROFILE_ID, registry.TERNARY_STEP_I32_PROFILE_DIGEST
        )
        self.assertNotEqual(u192.profile_id, ternary.profile_id)
        self.assertNotEqual(u192.semantic_digest, ternary.semantic_digest)
        self.assertNotEqual(u192.operation, ternary.operation)
        self.assertNotEqual(u192.request_codec, ternary.request_codec)
        self.assertNotEqual(u192.result_codec, ternary.result_codec)

    def test_ternary_profile_binds_the_exported_identity_tuple(self):
        profile = registry.PROFILES[registry.TERNARY_STEP_I32_PROFILE_ID]
        self.assertEqual(
            profile.semantic_digest_domain,
            "canonical-bytes:mathpunch.blitter-semantic-profile.v1",
        )
        self.assertEqual(profile.source_architecture_id, "bootstrap-braid-subleq-step-v0")
        self.assertTrue(profile.source_architecture_digest.startswith("sha256:"))

    def test_profiles_define_codec_and_terminal_contract_identity(self):
        for profile in registry.PROFILES.values():
            with self.subTest(profile=profile.profile_id):
                self.assertEqual(profile.version, 1)
                self.assertTrue(profile.request_codec.endswith(".v1"))
                self.assertTrue(profile.result_codec.endswith(".v1"))
                self.assertTrue(profile.terminal_contract.contract_id.endswith(".v1"))
                self.assertEqual(profile.terminal_contract.malformed_request, "reject-no-result")
                self.assertEqual(profile.terminal_contract.authority, "none")

    def test_ternary_terminal_statuses_match_exported_scalar_abi(self):
        profile = registry.PROFILES[registry.TERNARY_STEP_I32_PROFILE_ID]
        statuses = tuple(
            (status.code, status.name, status.state_effect)
            for status in profile.terminal_contract.statuses
        )
        self.assertEqual(
            statuses,
            (
                (0, "accepted", "next-state"),
                (1, "write-conflict", "input-state"),
                (2, "domain-exceeded", "input-state"),
            ),
        )

    def test_registry_matches_exported_profile_records_and_locks(self):
        root = Path(__file__).resolve().parents[2]
        profile_dir = root / "experiments" / "mcp_wasm_bridge" / "profiles"
        for filename, profile_id in (
            ("u192-v1", registry.U192_ADD_MOD_PROFILE_ID),
            ("ternary-v1", registry.TERNARY_STEP_I32_PROFILE_ID),
        ):
            with self.subTest(profile=profile_id):
                raw = (profile_dir / f"{filename}.json").read_bytes()
                lock = (profile_dir / f"{filename}.sha256").read_text(encoding="ascii")
                self.assertEqual(lock, "sha256:" + hashlib.sha256(raw).hexdigest() + "\n")
                exported = json.loads(raw)
                profile = registry.resolve_profile(profile_id, exported["semantic_digest"])
                self.assertEqual(exported["profile_id"], profile.profile_id)
                self.assertEqual(exported["semantic_digest"], profile.semantic_digest)
                self.assertEqual(exported["semantic_digest_domain"], profile.semantic_digest_domain)
                self.assertEqual(exported["operation"], profile.operation)
                self.assertEqual(exported["request_codec"], profile.request_codec)
                self.assertEqual(exported["result_codec"], profile.result_codec)
                self.assertEqual(exported["terminal_contract"], profile.terminal_contract.contract_id)
                self.assertEqual(exported["terminal_contract_digest"], profile.terminal_contract.digest)
                self.assertEqual(
                    exported["terminal_contract_digest_domain"],
                    profile.terminal_contract.digest_domain,
                )
                self.assertEqual(exported["version"], profile.version)
                self.assertEqual(exported.get("source_architecture_id"), profile.source_architecture_id)
                self.assertEqual(
                    exported.get("source_architecture_digest"), profile.source_architecture_digest
                )

    def test_unknown_id_and_cross_profile_digest_fail_closed(self):
        with self.assertRaisesRegex(registry.SemanticProfileError, "unknown semantic profile"):
            registry.resolve_profile("unknown-profile-v1", registry.U192_ADD_MOD_PROFILE_DIGEST)
        with self.assertRaisesRegex(registry.SemanticProfileError, "digest mismatch"):
            registry.resolve_profile(
                registry.U192_ADD_MOD_PROFILE_ID, registry.TERNARY_STEP_I32_PROFILE_DIGEST
            )

    def test_malformed_digest_and_profile_mutation_are_rejected(self):
        for digest in ("", "sha256:" + "A" * 64, "sha256:" + "0" * 63):
            with self.subTest(digest=digest):
                with self.assertRaisesRegex(registry.SemanticProfileError, "semantic_digest"):
                    registry.resolve_profile(registry.U192_ADD_MOD_PROFILE_ID, digest)
        profile = registry.PROFILES[registry.U192_ADD_MOD_PROFILE_ID]
        with self.assertRaises(FrozenInstanceError):
            profile.operation = "blitter.ternary-step-i32"
        with self.assertRaises(TypeError):
            registry.PROFILES["alias"] = profile


if __name__ == "__main__":
    unittest.main()
