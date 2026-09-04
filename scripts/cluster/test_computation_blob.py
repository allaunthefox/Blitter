#!/usr/bin/env python3
from __future__ import annotations

import unittest
import json

try:
    from . import computation_blob
except ImportError:
    import computation_blob
try:
    from experiments.webgpu_blitter.u192_codec import encode_u192
except ModuleNotFoundError:
    from u192_codec import encode_u192


DIGEST = "sha256:" + "b" * 64
PROFILE_ID = computation_blob.semantic_profile_registry.U192_ADD_MOD_PROFILE_ID
REGISTERED_PROFILE = computation_blob.semantic_profile_registry.U192_ADD_MOD_PROFILE_DIGEST


def envelope(**overrides):
    values = {
        "schema": computation_blob.SCHEMA,
        "request_id": "req-1",
        "operation": "u192.add-mod",
        "profile_id": PROFILE_ID,
        "profile_digest": REGISTERED_PROFILE,
        "codec": computation_blob.CODEC,
        "inputs": [
            {"schema": "mathpunch.u192-abi.v1", "format_tag": 0,
             "payload_hex": encode_u192([1, 0, 0, 0, 0, 0], "le-u32-lsw-first").hex()},
            {"schema": "mathpunch.u192-abi.v1", "format_tag": 0,
             "payload_hex": encode_u192([2, 0, 0, 0, 0, 0], "le-u32-lsw-first").hex()},
        ],
        "resource_budget": {"cpus": 1, "memory_mib": 64, "wall_ms": 1000},
        "terminal_contract_digest": computation_blob.registered_terminal_contract_digest(
            computation_blob.semantic_profile_registry.resolve_profile(PROFILE_ID, REGISTERED_PROFILE)
        ),
        "assembler": {"module_digest": DIGEST, "interface": "wasi-preview2-component-v1"},
        "authority": "none",
    }
    values.update(overrides)
    return values


class ComputationBlobTests(unittest.TestCase):
    def test_valid_envelope_is_canonical_and_reference_result_is_non_authoritative(self):
        value = envelope()
        checked = computation_blob.validate_envelope(value)
        self.assertEqual(computation_blob.digest(value)[:7], "sha256:")
        self.assertEqual(computation_blob.add_mod_result(value)["words"][0], 3)
        self.assertEqual(checked["authority"], "none")

    def test_validation_is_idempotent_and_derived_words_are_not_on_the_wire(self):
        checked = computation_blob.validate_envelope(envelope())
        self.assertEqual(computation_blob.validate_envelope(checked), checked)
        self.assertNotIn("_canonical_words", checked["inputs"][0])
        self.assertNotIn(b"_canonical_words", computation_blob.canonical_bytes(checked))

    def test_canonical_raw_parser_rejects_duplicate_keys_and_noncanonical_bytes(self):
        value = envelope()
        self.assertEqual(
            computation_blob.parse_canonical(computation_blob.canonical_bytes(value)), value
        )
        duplicate = computation_blob.canonical_bytes(value).replace(
            b'"authority":"none",', b'"authority":"none","authority":"none",', 1
        )
        with self.assertRaisesRegex(computation_blob.ComputationBlobError, "duplicate"):
            computation_blob.parse_canonical(duplicate)
        with self.assertRaisesRegex(computation_blob.ComputationBlobError, "canonical re-encoding"):
            computation_blob.parse_canonical(json.dumps(value, indent=2).encode("ascii"))

    def test_v2_admits_only_exact_registered_profile_and_terminal_contract(self):
        profile = computation_blob.semantic_profile_registry.resolve_profile(
            PROFILE_ID, REGISTERED_PROFILE
        )
        value = envelope()
        self.assertEqual(
            value["terminal_contract_digest"],
            "sha256:5a9804978698791d50d937ba93b7f279344b2e0ea658f4e2ee7b2d5a3365fb7f",
        )
        self.assertEqual(computation_blob.validate_envelope(value), value)
        with self.assertRaisesRegex(computation_blob.ComputationBlobError, "digest mismatch"):
            computation_blob.validate_envelope({**value, "profile_digest": "sha256:" + "a" * 64})
        with self.assertRaisesRegex(computation_blob.ComputationBlobError, "terminal contract"):
            computation_blob.validate_envelope({**value, "terminal_contract_digest": DIGEST})

    def test_v1_wire_contract_remains_compatible(self):
        value = envelope(schema=computation_blob.SCHEMA_V1, profile_digest="sha256:" + "a" * 64,
                         terminal_contract_digest=DIGEST)
        value.pop("profile_id")
        self.assertEqual(computation_blob.validate_envelope(value), value)
        self.assertNotIn("profile_id", value)

    def test_rejects_unknown_fields_and_wrong_operation(self):
        with self.assertRaises(computation_blob.ComputationBlobError):
            computation_blob.validate_envelope({**envelope(), "extra": True})
        with self.assertRaises(computation_blob.ComputationBlobError):
            computation_blob.validate_envelope({**envelope(), "operation": "shell"})

    def test_rejects_invalid_input_and_assembler(self):
        with self.assertRaises(computation_blob.ComputationBlobError):
            computation_blob.validate_envelope({**envelope(), "inputs": [envelope()["inputs"][0]]})
        with self.assertRaises(computation_blob.ComputationBlobError):
            computation_blob.validate_envelope({**envelope(), "assembler": {
                "module_digest": DIGEST, "interface": "host-native"
            }})

    def test_digest_changes_when_request_changes(self):
        self.assertNotEqual(computation_blob.digest(envelope()), computation_blob.digest(
            envelope(request_id="req-2")
        ))

    def test_reference_executor_reduces_addition_modulo_u192(self):
        value = envelope()
        value["inputs"] = [
            {"schema": "mathpunch.u192-abi.v1", "format_tag": 0,
             "payload_hex": "ff" * 24},
            {"schema": "mathpunch.u192-abi.v1", "format_tag": 0,
             "payload_hex": "01" + "00" * 23},
        ]
        self.assertEqual(computation_blob.add_mod_result(value)["words"], [0] * 6)


if __name__ == "__main__":
    unittest.main()
