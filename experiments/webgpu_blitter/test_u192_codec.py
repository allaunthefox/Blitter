#!/usr/bin/env python3
import random
import unittest

from u192_codec import (
    ABI_SCHEMA,
    BE_U192_MSB_FIRST,
    BE_U32_LSW_FIRST,
    CANONICAL_SCHEMA,
    LE_U32_LSW_FIRST,
    LE_U32_MSW_FIRST,
    SUPPORTED_FORMAT_TAGS,
    decode_format_tag_raw,
    decode_u192,
    encode_format_tag_raw,
    encode_u192,
    format_tag_for_layout,
    make_tagged_u192,
    normalize_tagged_u192,
    u192_from_int,
    u192_key,
    u192_to_int,
    validate_format_tag,
)


class U192CodecTests(unittest.TestCase):
    def test_all_256_format_bytes_have_total_raw_decode_round_trip(self):
        for tag in range(256):
            with self.subTest(tag=tag):
                fields = decode_format_tag_raw(tag)
                rebuilt = encode_format_tag_raw(
                    fields["version"],
                    fields["limb_order"],
                    fields["byte_order"],
                    fields["reserved"],
                )
                self.assertEqual(rebuilt, tag)

    def test_only_four_current_profile_tags_are_admitted(self):
        self.assertEqual(SUPPORTED_FORMAT_TAGS, frozenset({0x00, 0x10, 0x20, 0x30}))
        for tag in range(256):
            with self.subTest(tag=tag):
                if tag in SUPPORTED_FORMAT_TAGS:
                    self.assertEqual(validate_format_tag(tag)["raw"], tag)
                else:
                    with self.assertRaises(ValueError):
                        validate_format_tag(tag)

    def test_known_layout_vectors_and_explicit_mixed_layouts(self):
        words = [
            0x03020100,
            0x07060504,
            0x0B0A0908,
            0x0F0E0D0C,
            0x13121110,
            0x17161514,
        ]
        expected = {
            LE_U32_LSW_FIRST: bytes(range(24)),
            BE_U192_MSB_FIRST: bytes(reversed(range(24))),
            LE_U32_MSW_FIRST: bytes([
                20, 21, 22, 23, 16, 17, 18, 19, 12, 13, 14, 15,
                8, 9, 10, 11, 4, 5, 6, 7, 0, 1, 2, 3,
            ]),
            BE_U32_LSW_FIRST: bytes([
                3, 2, 1, 0, 7, 6, 5, 4, 11, 10, 9, 8,
                15, 14, 13, 12, 19, 18, 17, 16, 23, 22, 21, 20,
            ]),
        }
        for layout, expected_bytes in expected.items():
            with self.subTest(layout=layout):
                encoded = encode_u192(words, layout)
                self.assertEqual(encoded, expected_bytes)
                self.assertEqual(decode_u192(encoded, layout), words)
                tagged = make_tagged_u192(words, layout)
                self.assertEqual(tagged["format_tag"], format_tag_for_layout(layout))
                normalized = normalize_tagged_u192(tagged)
                self.assertEqual(normalized, {"schema": CANONICAL_SCHEMA, "words": words})

    def test_all_layouts_normalize_to_same_integer(self):
        rng = random.Random(0xE11D1A)
        layouts = (
            LE_U32_LSW_FIRST,
            BE_U192_MSB_FIRST,
            LE_U32_MSW_FIRST,
            BE_U32_LSW_FIRST,
        )
        for _ in range(10_000):
            words = [rng.getrandbits(32) for _ in range(6)]
            expected = u192_to_int(words)
            for layout in layouts:
                tagged = make_tagged_u192(words, layout)
                normalized = normalize_tagged_u192(tagged)
                self.assertEqual(u192_to_int(normalized["words"]), expected)

    def test_same_payload_wrong_tag_is_not_silently_equivalent(self):
        words = [
            0x00112233,
            0x44556677,
            0x8899AABB,
            0xCCDDEEFF,
            0x10203040,
            0x50607080,
        ]
        source = make_tagged_u192(words, LE_U32_MSW_FIRST)
        for wrong_layout in (LE_U32_LSW_FIRST, BE_U32_LSW_FIRST, BE_U192_MSB_FIRST):
            hostile = dict(source)
            hostile["format_tag"] = format_tag_for_layout(wrong_layout)
            normalized = normalize_tagged_u192(hostile)
            self.assertNotEqual(normalized["words"], words)

    def test_missing_invalid_future_and_reserved_tags_fail_closed(self):
        good = make_tagged_u192([1, 2, 3, 4, 5, 6], LE_U32_LSW_FIRST)
        bad_cases = []
        missing = dict(good)
        missing.pop("format_tag")
        bad_cases.append(missing)
        for tag in (0x01, 0x0F, 0x40, 0x80, 0xC0, -1, 256, True, "0x00", None):
            bad = dict(good)
            bad["format_tag"] = tag
            bad_cases.append(bad)
        for bad in bad_cases:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    normalize_tagged_u192(bad)

    def test_descriptor_schema_fields_and_hex_are_canonical(self):
        good = make_tagged_u192([1, 2, 3, 4, 5, 6], LE_U32_LSW_FIRST)
        self.assertEqual(good["schema"], ABI_SCHEMA)
        mutations = []
        wrong_schema = dict(good); wrong_schema["schema"] = "wrong"; mutations.append(wrong_schema)
        extra = dict(good); extra["extra"] = 1; mutations.append(extra)
        upper = dict(good); upper["payload_hex"] = upper["payload_hex"].upper(); mutations.append(upper)
        short = dict(good); short["payload_hex"] = short["payload_hex"][:-2]; mutations.append(short)
        nonhex = dict(good); nonhex["payload_hex"] = "g" + nonhex["payload_hex"][1:]; mutations.append(nonhex)
        spaced = dict(good); spaced["payload_hex"] = " " + spaced["payload_hex"][1:]; mutations.append(spaced)
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):
                    normalize_tagged_u192(mutation)

    def test_boundary_bits_and_carry_boundaries(self):
        bit_positions = [0, 1, 31, 32, 33, 63, 64, 95, 96, 127, 128, 159, 160, 191]
        layouts = (
            LE_U32_LSW_FIRST,
            BE_U192_MSB_FIRST,
            LE_U32_MSW_FIRST,
            BE_U32_LSW_FIRST,
        )
        for bit in bit_positions:
            with self.subTest(bit=bit):
                value = 1 << bit
                words = u192_from_int(value)
                self.assertEqual(u192_to_int(words), value)
                for layout in layouts:
                    normalized = normalize_tagged_u192(make_tagged_u192(words, layout))
                    self.assertEqual(normalized["words"], words)

        carry_vectors = [
            [0xFFFFFFFF, 0, 0, 0, 0, 0],
            [0, 1, 0, 0, 0, 0],
            [0xFFFFFFFF, 0xFFFFFFFF, 0, 0, 0, 0],
            [0, 0, 1, 0, 0, 0],
            [0xFFFFFFFF] * 6,
        ]
        self.assertLess(u192_key(carry_vectors[0]), u192_key(carry_vectors[1]))
        self.assertLess(u192_key(carry_vectors[2]), u192_key(carry_vectors[3]))
        for words in carry_vectors:
            self.assertEqual(u192_from_int(u192_to_int(words)), words)

    def test_zero_max_and_invalid_numeric_values(self):
        self.assertEqual(u192_from_int(0), [0] * 6)
        self.assertEqual(u192_from_int((1 << 192) - 1), [0xFFFFFFFF] * 6)
        for value in (-1, 1 << 192, True, None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    u192_from_int(value)

    def test_invalid_word_shapes_fail_closed(self):
        invalid = [
            [], [0] * 5, [0] * 7,
            [-1, 0, 0, 0, 0, 0],
            [1 << 32, 0, 0, 0, 0, 0],
            [True, 0, 0, 0, 0, 0],
        ]
        for words in invalid:
            with self.subTest(words=words):
                with self.assertRaises(ValueError):
                    u192_to_int(words)

    def test_numeric_order_is_transport_codec_independent(self):
        rng = random.Random(0x192)
        values = [[rng.getrandbits(32) for _ in range(6)] for _ in range(5_000)]
        by_words = sorted(values, key=u192_key)
        for layout in (
            LE_U32_LSW_FIRST,
            BE_U192_MSB_FIRST,
            LE_U32_MSW_FIRST,
            BE_U32_LSW_FIRST,
        ):
            with self.subTest(layout=layout):
                by_normalized = sorted(
                    values,
                    key=lambda w: u192_key(normalize_tagged_u192(make_tagged_u192(w, layout))["words"]),
                )
                self.assertEqual(by_words, by_normalized)


if __name__ == "__main__":
    unittest.main()
