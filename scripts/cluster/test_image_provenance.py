#!/usr/bin/env python3
import copy
import json
import tempfile
import unittest
from pathlib import Path

from image_provenance import (
    ProvenanceError,
    build_record,
    canonical_bytes,
    load_canonical,
    validate_record,
)

D0 = "sha256:" + "0" * 64
D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64
D3 = "sha256:" + "3" * 64
C0 = "a" * 40


def example(*, stamped=False):
    return build_record(
        published_tag="harbor.example/mathpunch/blitter-daemon:test",
        immutable_ref="harbor.example/mathpunch/blitter-daemon@" + D0,
        registry_digest=D0,
        source_commit=C0,
        semantic_profile_digest=D1,
        blitter_daemon_sha256=D2,
        dockerfile_sha256=D3,
        stamp_requested=stamped,
        stamp_plugin_sha256=("sha256:" + "4" * 64) if stamped else None,
    )


class ImageProvenanceTests(unittest.TestCase):
    def test_build_validate_and_canonical_round_trip(self):
        record = example()
        info = validate_record(record, expected_profile_digest=D1)
        self.assertEqual(info["immutable_ref"], record["image"]["immutable_ref"])
        self.assertEqual(info["registry_digest"], D0)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "prov.json"
            path.write_bytes(canonical_bytes(record))
            loaded, loaded_info = load_canonical(path, expected_profile_digest=D1)
            self.assertEqual(loaded, record)
            self.assertEqual(loaded_info, info)

    def test_mutable_tag_is_recorded_but_never_substitutes_for_immutable_ref(self):
        record = example()
        bad = copy.deepcopy(record)
        bad["image"]["immutable_ref"] = bad["image"]["published_tag"]
        with self.assertRaisesRegex(ProvenanceError, "immutable_ref"):
            validate_record(bad)

    def test_digest_ref_mismatch_fails_closed(self):
        record = example()
        bad = copy.deepcopy(record)
        bad["image"]["registry_digest"] = D2
        with self.assertRaisesRegex(ProvenanceError, "immutable_ref"):
            validate_record(bad)

    def test_profile_mismatch_fails_closed(self):
        with self.assertRaisesRegex(ProvenanceError, "profile"):
            validate_record(example(), expected_profile_digest=D2)

    def test_stamp_plugin_digest_required_iff_stamping_requested(self):
        stamped = example(stamped=True)
        self.assertTrue(validate_record(stamped)["stamp_requested"])

        bad = copy.deepcopy(stamped)
        bad["secure_stamp"]["plugin_sha256"] = None
        with self.assertRaisesRegex(ProvenanceError, "plugin_sha256"):
            validate_record(bad)

        bad = example()
        bad["secure_stamp"]["plugin_sha256"] = "sha256:" + "4" * 64
        with self.assertRaisesRegex(ProvenanceError, "unstamped"):
            validate_record(bad)

    def test_noncanonical_json_bytes_are_rejected_even_when_values_match(self):
        record = example()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "prov.json"
            path.write_text(json.dumps(record, indent=2), encoding="ascii")
            with self.assertRaisesRegex(ProvenanceError, "canonical re-encoding"):
                load_canonical(path)

    def test_unknown_fields_fail_closed(self):
        bad = example()
        bad["image"]["tag_is_authority"] = True
        with self.assertRaisesRegex(ProvenanceError, "image provenance fields"):
            validate_record(bad)


if __name__ == "__main__":
    unittest.main()
