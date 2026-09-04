#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import json
import unittest

try:
    from . import execution_object as contract
except ImportError:
    import execution_object as contract


D0 = "sha256:" + "0" * 64
D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64
D3 = "sha256:" + "3" * 64


def example():
    return contract.build_execution_object(
        profile_id="BLITTER-ISA-V1",
        profile_digest=D0,
        idempotency_key="job-123",
        program_digest=D1,
        architecture_digest=D2,
        terminal_contract_digest=D3,
        inputs=[
            contract.describe_blob(b"abc", "application/octet-stream", chunk_size=2),
            contract.describe_blob(b"xyz", "application/octet-stream", chunk_size=2),
        ],
        resource_budget={"cpus": 1, "memory_mib": 64, "wall_ms": 1000},
    )


class ExecutionObjectTests(unittest.TestCase):
    def test_canonical_ascii_round_trip_and_exact_digest(self):
        value = example()
        encoded = contract.canonical_bytes(value)
        self.assertTrue(encoded.isascii())
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertEqual(contract.parse_canonical(encoded), value)

        body = {key: item for key, item in value.items() if key != "semantic_digest"}
        expected = contract.semantic_digest(body)
        self.assertEqual(value["semantic_digest"], expected)

    def test_all_identity_digests_are_required_and_sealed(self):
        for field in (
            "profile_digest",
            "program_digest",
            "architecture_digest",
            "terminal_contract_digest",
        ):
            bad = copy.deepcopy(example())
            bad[field] = "sha256:" + "f" * 64
            with self.assertRaisesRegex(contract.ExecutionObjectError, "semantic_digest"):
                contract.validate_execution_object(bad)

    def test_input_order_is_semantic(self):
        first = example()
        second = contract.build_execution_object(
            profile_id="BLITTER-ISA-V1",
            profile_digest=D0,
            idempotency_key="job-123",
            program_digest=D1,
            architecture_digest=D2,
            terminal_contract_digest=D3,
            inputs=list(reversed(first["inputs"])),
            resource_budget=first["resource_budget"],
        )
        self.assertNotEqual(first["semantic_digest"], second["semantic_digest"])

    def test_profile_id_and_idempotency_key_are_required_and_semantic(self):
        for field, replacement in (("profile_id", "other-profile"), ("idempotency_key", "job-124")):
            bad = copy.deepcopy(example())
            bad[field] = replacement
            with self.subTest(field=field), self.assertRaisesRegex(
                contract.ExecutionObjectError, "semantic_digest"
            ):
                contract.validate_execution_object(bad)

        for field in ("profile_id", "idempotency_key"):
            bad = copy.deepcopy(example())
            bad[field] = ""
            with self.subTest(field=field), self.assertRaises(contract.ExecutionObjectError):
                contract.validate_execution_object(bad)

    def test_carrier_details_are_outside_semantic_identity(self):
        value = example()
        carriers = {value["inputs"][0]["digest"]: {"url": "https://a.invalid/blob"}}
        before = value["semantic_digest"]
        carriers[value["inputs"][0]["digest"]]["url"] = "file:///cache/blob"
        self.assertEqual(contract.semantic_digest(value), before)

        embedded = copy.deepcopy(value)
        embedded["inputs"][0]["url"] = "https://a.invalid/blob"
        with self.assertRaisesRegex(contract.ExecutionObjectError, "fields"):
            contract.validate_execution_object(embedded)

    def test_blob_descriptor_binds_content_and_bounded_contiguous_chunks(self):
        blob = contract.describe_blob(b"abcdef", "application/octet-stream", chunk_size=2)
        self.assertEqual(blob["digest"], "sha256:" + hashlib.sha256(b"abcdef").hexdigest())
        self.assertEqual([chunk["offset"] for chunk in blob["chunks"]], [0, 2, 4])

        tampered = copy.deepcopy(blob)
        tampered["chunks"][0]["digest"] = D0
        with self.assertRaisesRegex(contract.ExecutionObjectError, "chunk_manifest_root"):
            contract.validate_blob_descriptor(tampered)

        gap = copy.deepcopy(blob)
        gap["chunks"][1]["offset"] = 3
        with self.assertRaisesRegex(contract.ExecutionObjectError, "contiguous"):
            contract.validate_blob_descriptor(gap)

        oversized = copy.deepcopy(blob)
        oversized["chunks"][0]["size_bytes"] = contract.MAX_CHUNK_BYTES + 1
        with self.assertRaises(contract.ExecutionObjectError):
            contract.validate_blob_descriptor(oversized)

    def test_chunk_manifest_is_domain_separated_and_chunking_is_not_semantic(self):
        data = b"abcdef"
        two = contract.describe_blob(data, "application/octet-stream", chunk_size=2)
        three = contract.describe_blob(data, "application/octet-stream", chunk_size=3)
        self.assertNotEqual(two["chunk_manifest_root"], three["chunk_manifest_root"])

        manifest = {
            "schema": contract.CHUNK_MANIFEST_SCHEMA,
            "blob_digest": two["digest"],
            "size_bytes": two["size_bytes"],
            "chunks": two["chunks"],
        }
        undomained = "sha256:" + hashlib.sha256(contract.canonical_bytes(manifest)).hexdigest()
        self.assertNotEqual(two["chunk_manifest_root"], undomained)

        first = example()
        second_inputs = copy.deepcopy(first["inputs"])
        second_inputs[0] = contract.describe_blob(b"abc", "application/octet-stream", chunk_size=3)
        second = contract.build_execution_object(
            profile_id=first["profile_id"],
            profile_digest=first["profile_digest"],
            idempotency_key=first["idempotency_key"],
            program_digest=first["program_digest"],
            architecture_digest=first["architecture_digest"],
            terminal_contract_digest=first["terminal_contract_digest"],
            inputs=second_inputs,
            resource_budget=first["resource_budget"],
        )
        self.assertEqual(first["semantic_digest"], second["semantic_digest"])

    def test_semantic_digest_rejects_a_bad_claimed_complete_object(self):
        bad = copy.deepcopy(example())
        bad["semantic_digest"] = D0
        with self.assertRaisesRegex(contract.ExecutionObjectError, "semantic_digest"):
            contract.semantic_digest(bad)

        bad = copy.deepcopy(example())
        bad["inputs"][0]["chunk_manifest_root"] = D0
        with self.assertRaisesRegex(contract.ExecutionObjectError, "chunk_manifest_root"):
            contract.semantic_digest(bad)

    def test_blob_bound_is_exactly_representable_by_chunk_limits(self):
        self.assertEqual(
            contract.MAX_BLOB_BYTES, contract.MAX_CHUNKS * contract.MAX_CHUNK_BYTES
        )

    def test_budget_authority_unknown_fields_and_noncanonical_bytes_fail_closed(self):
        bad = copy.deepcopy(example())
        bad["resource_budget"]["cpus"] = 0
        with self.assertRaises(contract.ExecutionObjectError):
            contract.validate_execution_object(bad)

        bad = copy.deepcopy(example())
        bad["authority"] = "gateway"
        with self.assertRaisesRegex(contract.ExecutionObjectError, "authority"):
            contract.validate_execution_object(bad)

        bad = {**example(), "request_id": "carrier-correlation-id"}
        with self.assertRaisesRegex(contract.ExecutionObjectError, "fields"):
            contract.validate_execution_object(bad)

        pretty = json.dumps(example(), indent=2).encode("ascii")
        with self.assertRaisesRegex(contract.ExecutionObjectError, "canonical re-encoding"):
            contract.parse_canonical(pretty)


if __name__ == "__main__":
    unittest.main()
