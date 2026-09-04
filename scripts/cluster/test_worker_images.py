#!/usr/bin/env python3
"""Static gates for the six LSP worker images and presence announcements.

Verifies without a container runtime, registry, or toolchain:

* every language has a Containerfile pinning its base by sha256 digest,
  running as non-root UID 65532, declaring a HEALTHCHECK, and using the
  canonical argv as CMD;
* no Containerfile accepts insecure signatures or runs as root;
* the bootstrap generation file exists and is a non-negative integer;
* presence announcements bind node/boot/generation/language/image and are
  accepted as fresh by the admission contract.
"""
from __future__ import annotations

import pathlib
import re
import tempfile
import time
import unittest

WORKERS = pathlib.Path(__file__).with_name("workers")
LANGUAGES = ("lean", "coq", "python", "r", "julia", "rust")
CANONICAL_CMD = {
    "lean": ["lake", "env", "lean", "--server"],
    "coq": ["coq-lsp"],
    "python": ["pylsp"],
    "r": ["R", "--vanilla", "--slave"],
    "julia": ["julia", "--startup-file=no"],
    "rust": ["rust-analyzer"],
}
FROM_RE = re.compile(r"^FROM\s+\S+@sha256:[0-9a-f]{64}\s*$", re.MULTILINE)


def load_announce():
    import sys

    workers_dir = str(WORKERS)
    if workers_dir not in sys.path:
        sys.path.insert(0, workers_dir)
    try:
        import announce_presence
    finally:
        sys.path.remove(workers_dir)
    return announce_presence


def load_admission():
    try:
        from . import lsp_admission
    except ImportError:
        import lsp_admission
    return lsp_admission


class WorkerImageTests(unittest.TestCase):
    def test_each_language_has_a_pinned_nonroot_healthchecked_image(self):
        for language in LANGUAGES:
            with self.subTest(language=language):
                text = (WORKERS / f"Containerfile.{language}").read_text(encoding="utf-8")
                self.assertIsNotNone(
                    FROM_RE.search(text), "base image must be pinned by sha256 digest"
                )
                self.assertIn("USER 65532:65532", text)
                self.assertIn("HEALTHCHECK", text)
                self.assertNotIn("insecureAcceptAnything", text)
                self.assertNotRegex(text, r"(?m)^USER\s+(0|root)\b")

    def test_cmd_matches_canonical_argv(self):
        for language in LANGUAGES:
            with self.subTest(language=language):
                text = (WORKERS / f"Containerfile.{language}").read_text(encoding="utf-8")
                match = re.search(r"(?m)^CMD\s+(\[.*\])\s*$", text)
                self.assertIsNotNone(match, "Containerfile must declare a JSON CMD")
                import json

                argv = json.loads(match.group(1))
                self.assertEqual(argv, CANONICAL_CMD[language])

    def test_build_script_blocks_bootstrap_publication(self):
        script = (WORKERS / "build_workers.sh").read_text(encoding="utf-8")
        self.assertIn("PRODUCTION-BASE-REVIEWED", script)
        self.assertIn("--provenance=true", script)
        self.assertIn("--sbom=true", script)

    def test_generation_file_is_a_nonnegative_integer(self):
        raw = (WORKERS / "generation").read_text(encoding="utf-8").strip()
        self.assertTrue(raw.isdigit())
        self.assertGreaterEqual(int(raw, 10), 0)


class PresenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.announce = load_announce()
        cls.admission = load_admission()

    def test_announcement_carries_generation_identity_and_is_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            gen = pathlib.Path(directory) / "generation"
            gen.write_text("7\n", encoding="utf-8")
            now = int(time.time() * 1000)
            announcement = self.announce.build_announcement(
                node_id="cupfox",
                boot_id="boot-abc",
                generation=self.announce.read_generation(gen),
                language="lean",
                image_digest="sha256:" + "a" * 64,
                extra_capabilities=(),
                ttl_ms=30_000,
                now_unix_ms=now,
            )
            self.assertEqual(announcement["worker_generation"], 7)
            self.assertEqual(
                announcement["capabilities"], ["lsp", "language:lean"]
            )
            presence = self.admission.presence_from_mapping(
                announcement, now_unix_ms=now + 1_000
            )
            self.assertTrue(
                presence.is_fresh(now + 1_000, max_clock_skew_ms=5_000)
            )
            self.assertTrue(presence.matches_capabilities({"lsp", "language:lean"}))

    def test_generation_file_must_be_decimal(self):
        with tempfile.TemporaryDirectory() as directory:
            gen = pathlib.Path(directory) / "generation"
            gen.write_text("not-a-number\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                self.announce.read_generation(gen)

    def test_duplicate_capabilities_fail_closed(self):
        with self.assertRaises(ValueError):
            self.announce.build_announcement(
                node_id="n", boot_id="b", generation=0, language="coq",
                image_digest="sha256:" + "b" * 64,
                extra_capabilities=("lsp",), ttl_ms=1_000, now_unix_ms=0,
            )


if __name__ == "__main__":
    unittest.main()
