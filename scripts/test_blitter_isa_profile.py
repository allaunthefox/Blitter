#!/usr/bin/env python3
import copy
import unittest
from pathlib import Path

from verify_blitter_isa_profile import (
    ROOT,
    ProfileError,
    load_profile,
    semantic_digest,
    validate_profile,
)

PROFILE = ROOT / "docs/specs/BLITTER_ISA_PROFILE_V1.json"
LOCK = ROOT / "docs/specs/BLITTER_ISA_PROFILE_V1.sha256"


class BlitterISAProfileTests(unittest.TestCase):
    def test_frozen_profile_matches_lock_and_executable_codec_surface(self):
        profile = load_profile(PROFILE)
        digest = validate_profile(profile, lock_text=LOCK.read_text(), root=ROOT)
        self.assertEqual(digest, profile["semantic_digest"])

    def test_semantic_mutation_breaks_embedded_digest(self):
        profile = load_profile(PROFILE)
        mutated = copy.deepcopy(profile)
        mutated["opcodes"]["U192_ADD_MOD"]["relation"] = "value(output) = value(a) + value(b)"
        with self.assertRaisesRegex(ProfileError, "embedded semantic digest mismatch"):
            validate_profile(mutated, lock_text=LOCK.read_text(), root=ROOT)

    def test_security_plugin_cannot_become_an_arithmetic_opcode_without_breaking_gate(self):
        profile = load_profile(PROFILE)
        mutated = copy.deepcopy(profile)
        mutated["opcodes"]["TLS"] = {"kind": "semantic"}
        mutated["semantic_digest"] = semantic_digest(mutated)
        with self.assertRaisesRegex(ProfileError, "frozen opcode set changed"):
            validate_profile(mutated, root=ROOT)

    def test_hash_equality_remains_explicitly_nonconformance(self):
        profile = load_profile(PROFILE)
        self.assertIn(
            "implementation hash equals profile hash",
            profile["conformance"]["not_conformance"],
        )

    def test_security_plugin_remains_optional_but_requested_capability_fail_closed(self):
        profile = load_profile(PROFILE)
        text = profile["separate_protocol_layers"]["security_plugin"]
        self.assertIn("optional process-plugin ABI", text)
        self.assertIn("blocks rather than silently downgrades", text)


if __name__ == "__main__":
    unittest.main()
