#!/usr/bin/env python3
import random
import unittest

from merge_partition import (
    aggregate_worker_results,
    allocate_slices,
    u192_key,
    u192_to_int,
    validate_partition,
)


class MergePartitionTests(unittest.TestCase):
    def test_weighted_allocation_closes_full_cover(self):
        # Regression for the former sequential formula, which allocated only 77/101.
        slices = [
            {"node": "a", "numa": 0, "max_prefixes": 101, "weight": 12},
            {"node": "b", "numa": 0, "max_prefixes": 101, "weight": 4},
            {"node": "c", "numa": 0, "max_prefixes": 101, "weight": 2},
        ]
        jobs = allocate_slices(slices, 101)
        self.assertTrue(validate_partition(jobs, 101))
        self.assertEqual(sum(j["slice_count"] for j in jobs), 101)
        self.assertEqual(jobs[-1]["slice_start"] + jobs[-1]["slice_count"], 101)

    def test_caps_are_respected_while_cover_is_preserved(self):
        slices = [
            {"node": "a", "numa": 0, "max_prefixes": 5, "weight": 100},
            {"node": "b", "numa": 0, "max_prefixes": 30, "weight": 1},
            {"node": "c", "numa": 0, "max_prefixes": 30, "weight": 1},
        ]
        jobs = allocate_slices(slices, 40)
        self.assertTrue(validate_partition(jobs, 40))
        self.assertLessEqual(jobs[0]["slice_count"], 5)
        for job in jobs:
            self.assertLessEqual(job["slice_count"], job["max_prefixes"])

    def test_insufficient_capacity_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "insufficient prefix capacity"):
            allocate_slices(
                [
                    {"max_prefixes": 5, "weight": 1},
                    {"max_prefixes": 6, "weight": 1},
                ],
                12,
            )

    def test_partition_validator_rejects_gap(self):
        with self.assertRaisesRegex(ValueError, "partition discontinuity"):
            validate_partition(
                [
                    {"slice_start": 0, "slice_count": 3},
                    {"slice_start": 4, "slice_count": 2},
                ],
                5,
            )

    def test_u192_uses_high_word_first_numeric_order(self):
        low_word_large = [0xFFFFFFFF, 0, 0, 0, 0, 0]
        next_word_one = [0, 1, 0, 0, 0, 0]
        self.assertLess(u192_key(low_word_large), u192_key(next_word_one))
        self.assertLess(u192_to_int(low_word_large), u192_to_int(next_word_one))
        best, source = aggregate_worker_results(
            {
                "wrong-under-python-tuple-order": {"max_words": low_word_large},
                "numeric-maximum": {"max_words": next_word_one},
            }
        )
        self.assertEqual(best, next_word_one)
        self.assertEqual(source, "numeric-maximum")

    def test_u192_key_matches_scalar_integer_reference(self):
        rng = random.Random(0xB1177E2)
        values = []
        for _ in range(10_000):
            words = [rng.getrandbits(32) for _ in range(6)]
            values.append(words)
            expected = sum(word << (32 * i) for i, word in enumerate(words))
            self.assertEqual(u192_to_int(words), expected)

        by_key = sorted(values, key=u192_key)
        by_int = sorted(values, key=lambda w: sum(x << (32 * i) for i, x in enumerate(w)))
        self.assertEqual(by_key, by_int)


if __name__ == "__main__":
    unittest.main()
