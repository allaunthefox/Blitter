#!/usr/bin/env python3
import copy
import unittest

from u192_codec import (
    BE_U192_MSB_FIRST,
    BE_U32_LSW_FIRST,
    LE_U32_LSW_FIRST,
    LE_U32_MSW_FIRST,
    make_tagged_u192,
)
from u192_normalization_matrix import (
    DIGEST_MISMATCH,
    VERIFIED_PROJECTION,
    matrix_is_exact_v1,
    normalization_certificate_authorized,
    normalization_matrix,
    normalize_with_certificate,
)


class U192NormalizationMatrixTests(unittest.TestCase):
    def test_matrix_covers_exact_2x2_order_product_once(self):
        rows = normalization_matrix()
        self.assertTrue(matrix_is_exact_v1())
        self.assertEqual(len(rows), 4)
        self.assertEqual({row["format_tag"] for row in rows}, {0x00, 0x10, 0x20, 0x30})
        self.assertEqual(
            {(row["limb_order"], row["byte_order"]) for row in rows},
            {("lsw", "little"), ("lsw", "big"), ("msw", "little"), ("msw", "big")},
        )

    def test_each_representation_round_trips_and_authorizes_only_projection(self):
        words = [0x00112233, 0x44556677, 0x8899AABB, 0xCCDDEEFF, 0x10203040, 0x50607080]
        for layout in (
            LE_U32_LSW_FIRST,
            BE_U32_LSW_FIRST,
            LE_U32_MSW_FIRST,
            BE_U192_MSB_FIRST,
        ):
            with self.subTest(layout=layout):
                structure = make_tagged_u192(words, layout)
                canonical, certificate = normalize_with_certificate(structure)
                self.assertEqual(canonical["words"], words)
                self.assertEqual(certificate["status"], VERIFIED_PROJECTION)
                self.assertTrue(
                    normalization_certificate_authorized(certificate, structure, canonical)
                )
                # The certificate schema intentionally has no arithmetic/theorem
                # authority field. Projection authority must stay scoped.
                self.assertNotIn("arithmetic_verified", certificate)
                self.assertNotIn("theorem_authority", certificate)
                self.assertNotIn("u192_add_mod", certificate)

    def test_mutating_any_bound_identity_breaks_authorization(self):
        words = [1, 2, 3, 4, 5, 6]
        structure = make_tagged_u192(words, LE_U32_LSW_FIRST)
        canonical, certificate = normalize_with_certificate(structure)
        self.assertTrue(normalization_certificate_authorized(certificate, structure, canonical))

        mutations = []
        for key, value in (
            ("input_digest", "sha256:" + "0" * 64),
            ("canonical_digest", "sha256:" + "1" * 64),
            ("matrix_id", "wrong-matrix"),
            ("format_tag", 0x10),
            ("layout", BE_U32_LSW_FIRST),
            ("representation", BE_U32_LSW_FIRST),
            ("input_digest_bound", False),
            ("canonical_digest_bound", False),
            ("roundtrip_verified", False),
            ("status", DIGEST_MISMATCH),
        ):
            bad = copy.deepcopy(certificate)
            bad[key] = value
            mutations.append((key, bad))

        extra = copy.deepcopy(certificate)
        extra["arithmetic_verified"] = True
        mutations.append(("extra-authority-field", extra))

        for name, bad in mutations:
            with self.subTest(name=name):
                self.assertFalse(normalization_certificate_authorized(bad, structure, canonical))

    def test_input_and_output_binding_are_both_required(self):
        a = make_tagged_u192([1, 2, 3, 4, 5, 6], LE_U32_LSW_FIRST)
        b = make_tagged_u192([1, 2, 3, 4, 5, 7], LE_U32_LSW_FIRST)
        canonical_a, cert_a = normalize_with_certificate(a)
        canonical_b, _ = normalize_with_certificate(b)

        self.assertFalse(normalization_certificate_authorized(cert_a, b, canonical_a))
        self.assertFalse(normalization_certificate_authorized(cert_a, a, canonical_b))

    def test_wrong_but_admitted_tag_produces_different_projection_not_magic_recovery(self):
        words = [0x00112233, 0x44556677, 0x8899AABB, 0xCCDDEEFF, 0x10203040, 0x50607080]
        original = make_tagged_u192(words, LE_U32_MSW_FIRST)
        hostile = dict(original)
        hostile["format_tag"] = 0x00
        canonical, certificate = normalize_with_certificate(hostile)
        self.assertNotEqual(canonical["words"], words)
        # The certificate can truthfully bind the declared transport projection;
        # it cannot prove that the producer tagged its intended mathematical value.
        self.assertTrue(normalization_certificate_authorized(certificate, hostile, canonical))


if __name__ == "__main__":
    unittest.main()
