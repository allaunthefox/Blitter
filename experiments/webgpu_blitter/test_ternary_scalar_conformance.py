#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import unittest


HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
LOCK_PATH = FIXTURES / "ternary_scalar_v1.lock.json"

REQUEST_SIZE = 26
RESULT_SIZE = 26
EXPECTED_RECORDS = 3_072
EXPECTED_REQUEST_SHA256 = (
    "49056e9d1de8a6697eca9329d3b74127b63257b75fdb38345bf86996009711c8"
)
EXPECTED_RESULT_SHA256 = (
    "f0fd3b364ef7ee378106187d62898e110f109d9f69ed9dcff30f303725cea800"
)


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class TernaryScalarConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.lock = json.loads(LOCK_PATH.read_text(encoding="ascii"))
        fixture_name = cls.lock["request_fixture"]
        cls.requests = (FIXTURES / fixture_name).read_bytes()

    def test_fixture_and_result_lock_are_frozen(self) -> None:
        self.assertEqual(self.lock["schema"], "blitter-ternary-scalar-conformance-lock-v1")
        self.assertEqual(self.lock["request_records"], EXPECTED_RECORDS)
        self.assertEqual(self.lock["result_records"], EXPECTED_RECORDS)
        self.assertEqual(self.lock["request_bytes"], EXPECTED_RECORDS * REQUEST_SIZE)
        self.assertEqual(self.lock["result_bytes"], EXPECTED_RECORDS * RESULT_SIZE)
        self.assertEqual(self.lock["request_sha256"], EXPECTED_REQUEST_SHA256)
        self.assertEqual(self.lock["result_sha256"], EXPECTED_RESULT_SHA256)
        self.assertEqual(len(self.requests), self.lock["request_bytes"])
        self.assertEqual(sha256(self.requests), EXPECTED_REQUEST_SHA256)

    def test_process_matches_complete_canonical_result_stream(self) -> None:
        completed = subprocess.run(
            ["cargo", "run", "--quiet", "--bin", "blitter-ternary-scalar"],
            cwd=HERE,
            input=self.requests,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", errors="replace"),
        )
        self.assertEqual(len(completed.stdout), self.lock["result_bytes"])
        self.assertEqual(
            len(completed.stdout) // RESULT_SIZE,
            self.lock["result_records"],
        )
        self.assertEqual(sha256(completed.stdout), self.lock["result_sha256"])


if __name__ == "__main__":
    unittest.main()
