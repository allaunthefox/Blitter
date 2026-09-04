#!/usr/bin/env python3
from __future__ import annotations

import unittest

try:
    from .mcp_blob_assembler import assemble, parse_canonical_request
    from . import computation_blob
    from .computation_blob import ComputationBlobError, canonical_bytes, digest
except ImportError:
    from mcp_blob_assembler import assemble, parse_canonical_request
    import computation_blob
    from computation_blob import ComputationBlobError, canonical_bytes, digest


def request(**overrides):
    value = {
        "request_id": "mcp-1",
        "profile_id": computation_blob.semantic_profile_registry.U192_ADD_MOD_PROFILE_ID,
        "profile_digest": computation_blob.semantic_profile_registry.U192_ADD_MOD_PROFILE_DIGEST,
        "terminal_contract_digest": computation_blob.registered_terminal_contract_digest(
            computation_blob.semantic_profile_registry.resolve_profile(
                computation_blob.semantic_profile_registry.U192_ADD_MOD_PROFILE_ID,
                computation_blob.semantic_profile_registry.U192_ADD_MOD_PROFILE_DIGEST,
            )
        ),
        "assembler": {"module_digest": "sha256:" + "c" * 64,
                       "interface": "wasi-preview2-component-v1"},
        "left_words": [1, 0, 0, 0, 0, 0],
        "right_words": [2, 0, 0, 0, 0, 0],
        "resource_budget": {"cpus": 1, "memory_mib": 64, "wall_ms": 1000},
    }
    value.update(overrides)
    return value


class MCPBlobAssemblerTests(unittest.TestCase):
    def test_assembly_is_canonical_and_non_authoritative(self):
        envelope = assemble(request())
        self.assertEqual(envelope["authority"], "none")
        self.assertEqual(envelope["inputs"][0]["payload_hex"][:8], "01000000")
        self.assertTrue(digest(envelope).startswith("sha256:"))

    def test_rejects_extra_fields_and_invalid_limbs(self):
        with self.assertRaises(ComputationBlobError):
            assemble(request(extra=True))
        with self.assertRaises(ComputationBlobError):
            assemble(request(left_words=[2**32, 0, 0, 0, 0, 0]))

    def test_changes_to_mcp_request_change_blob(self):
        self.assertNotEqual(
            digest(assemble(request(request_id="mcp-1"))),
            digest(assemble(request(request_id="mcp-2"))),
        )

    def test_manifest_must_bind_assembler_module(self):
        manifest = {
            "schema": "mathpunch.blitter-wasm-component-manifest.v1",
            "component_id": "mcp-u192-assembler",
            "module": "registry.example.invalid/assembler@sha256:" + "d" * 64,
            "wit": "mathpunch:blitter-blob@1.0.0",
            "entrypoint": "mathpunch:blitter-blob/assembler#assemble",
            "imports": [],
            "capabilities": {"network": False, "filesystem": "none", "process": False, "devices": []},
            "limits": {"memory_mib": 64, "fuel": 1000, "wall_ms": 1000, "output_bytes": 1024},
            "authority": "none",
        }
        with self.assertRaisesRegex(ComputationBlobError, "does not match"):
            assemble(request(), manifest=manifest)

    def test_v2_assembly_is_explicit_and_registry_bound(self):
        profile = computation_blob.semantic_profile_registry.resolve_profile(
            computation_blob.semantic_profile_registry.U192_ADD_MOD_PROFILE_ID,
            computation_blob.semantic_profile_registry.U192_ADD_MOD_PROFILE_DIGEST,
        )
        value = request(
            profile_id=profile.profile_id,
            profile_digest=profile.semantic_digest,
            terminal_contract_digest=computation_blob.registered_terminal_contract_digest(profile),
        )
        envelope = assemble(value, envelope_schema=computation_blob.SCHEMA_V2)
        self.assertEqual(envelope["schema"], computation_blob.SCHEMA_V2)
        self.assertEqual(envelope["profile_id"], profile.profile_id)
        self.assertEqual(assemble(value), envelope)

    def test_raw_request_parser_requires_canonical_bytes_and_rejects_duplicates(self):
        legacy = request(profile_id=None, profile_digest="sha256:" + "a" * 64,
                         terminal_contract_digest="sha256:" + "b" * 64)
        legacy.pop("profile_id")
        raw = canonical_bytes(legacy)
        self.assertEqual(
            parse_canonical_request(raw, envelope_schema=computation_blob.SCHEMA_V1), legacy
        )
        duplicate = raw.replace(
            b'"request_id":"mcp-1",',
            b'"request_id":"mcp-1","request_id":"mcp-1",',
            1,
        )
        with self.assertRaisesRegex(ComputationBlobError, "duplicate"):
            parse_canonical_request(duplicate, envelope_schema=computation_blob.SCHEMA_V1)


if __name__ == "__main__":
    unittest.main()
