#!/usr/bin/env python3
"""Regression tests for real image-trust verification.

Covers Ed25519 sign/verify (including structural and subgroup checks),
catalog signature verification and tamper rejection, provenance allowlist
binding, SBOM binding, deployment-owned policy loading, and the full
``verify_image_trust`` gate.
"""
from __future__ import annotations

import copy
import unittest

try:
    from . import image_catalog
    from . import image_trust
except ImportError:
    import image_catalog
    import image_trust


SEED = hashlib_seed = bytes(range(32))
OTHER_SEED = bytes([9]) * 32


def _pub(seed: bytes) -> str:
    return image_trust.ed25519_publickey_from_seed(seed).hex()


KEY_ID = "test-key-1"
PUB = _pub(SEED)
COMMIT = "a" * 40
BUILD = "sha256:" + "1" * 64
LANGUAGES = ("lean", "coq", "python", "r", "julia", "rust")
COMMANDS = {
    "lean": ["lake", "env", "lean", "--server"],
    "coq": ["coq-lsp"],
    "python": ["pylsp"],
    "r": ["R", "--vanilla", "--slave"],
    "julia": ["julia", "--startup-file=no"],
    "rust": ["rust-analyzer"],
}


def _images(tag: str = "ab"):
    return {
        language: f"registry.example.invalid/mathpunch/{language}-lsp@{ 'sha256:' + tag * 32 }"
        for language in LANGUAGES
    }


def _catalog(images, *, commit=COMMIT, build=BUILD, source="https://example.invalid/lsp-workers"):
    catalog = {
        "schema": image_trust.SCHEMA,
        "images": {
            language: {
                "image": images[language],
                "argv": COMMANDS[language],
                "toolchain": {
                    "name": language,
                    "version": "1.0",
                    "identity": language + "@1.0",
                },
                "network": False,
                "resources": {"cpus": 1, "memory_mib": 2048},
            }
            for language in LANGUAGES
        },
        "metadata": {
            "signature": {
                "algorithm": image_trust.SIGNATURE_ALGORITHM,
                "key_id": KEY_ID,
                "value": "",
            },
            "provenance": {
                "source": source,
                "source_commit": commit,
                "build_digest": build,
            },
        },
    }
    signature = image_trust.ed25519_sign(SEED, image_trust.signing_bytes(catalog)).hex()
    catalog["metadata"]["signature"]["value"] = signature
    return catalog


def _policy(images, *, commit=COMMIT, source="https://example.invalid/lsp-workers"):
    return {
        "approved_images": dict(images),
        "resource_ceilings": {
            language: {"cpus": 2, "memory_mib": 4096} for language in LANGUAGES
        },
        "approved_keys": {KEY_ID: PUB},
        "approved_sources": [source],
        "require_sbom": True,
        "sbom": {
            language: {
                "schema": image_trust.SBOM_SCHEMA,
                "language": language,
                "image": images[language],
                "sbom_digest": "sha256:" + "2" * 64,
                "sbom_media_type": "application/vnd.cyclonedx+json",
                "source": source,
                "source_commit": commit,
                "build_digest": BUILD,
            }
            for language in LANGUAGES
        },
    }


class Ed25519Tests(unittest.TestCase):
    def test_sign_verify_roundtrip_and_tamper_rejection(self):
        public = image_trust.ed25519_publickey_from_seed(SEED)
        signature = image_trust.ed25519_sign(SEED, b"catalog bytes")
        self.assertTrue(image_trust.ed25519_verify(public, b"catalog bytes", signature))
        self.assertFalse(image_trust.ed25519_verify(public, b"catalog byteS", signature))
        self.assertFalse(
            image_trust.ed25519_verify(
                image_trust.ed25519_publickey_from_seed(OTHER_SEED),
                b"catalog bytes",
                signature,
            )
        )

    def test_malformed_inputs_verify_false_without_raising(self):
        public = image_trust.ed25519_publickey_from_seed(SEED)
        signature = image_trust.ed25519_sign(SEED, b"m")
        self.assertFalse(image_trust.ed25519_verify(b"short", b"m", signature))
        self.assertFalse(image_trust.ed25519_verify(public, b"m", b"short"))
        self.assertFalse(image_trust.ed25519_verify(public, b"m", b"\xff" * 64))


class CatalogTrustTests(unittest.TestCase):
    def test_full_trust_gate_passes_for_signed_bound_catalog(self):
        images = _images()
        catalog = _catalog(images)
        result = image_trust.verify_image_trust(catalog, _policy(images))
        self.assertEqual(result["signature_key_id"], KEY_ID)
        self.assertEqual(result["provenance"]["source_commit"], COMMIT)
        self.assertEqual(set(result["sbom_digests"]), set(LANGUAGES))

    def test_tampered_image_fails_signature(self):
        images = _images()
        catalog = _catalog(images)
        bad = copy.deepcopy(catalog)
        bad["images"]["lean"]["resources"]["cpus"] = 2
        with self.assertRaisesRegex(image_trust.TrustError, "signature verification failed"):
            image_trust.verify_image_trust(bad, _policy(images))

    def test_unapproved_image_fails_policy_before_signature(self):
        images = _images()
        catalog = _catalog(images)
        catalog["images"]["lean"]["image"] = (
            "registry.example.invalid/mathpunch/lean-lsp@sha256:" + "f" * 64
        )
        with self.assertRaisesRegex(image_catalog.CatalogError, "is not approved"):
            # Deployment policy rejects the image before signature verification.
            image_trust.verify_image_trust(catalog, _policy(images))
        policy = _policy(images)
        policy["approved_images"]["lean"] = catalog["images"]["lean"]["image"]
        with self.assertRaisesRegex(image_trust.TrustError, "signature verification failed"):
            # Even an approved-image swap breaks the signature binding.
            image_trust.verify_image_trust(catalog, policy)

    def test_unknown_signing_key_fails_closed(self):
        images = _images()
        catalog = _catalog(images)
        policy = _policy(images)
        policy["approved_keys"] = {"other-key": _pub(OTHER_SEED)}
        with self.assertRaisesRegex(image_trust.TrustError, "not approved"):
            image_trust.verify_image_trust(catalog, policy)

    def test_wrong_algorithm_fails_closed(self):
        images = _images()
        catalog = _catalog(images)
        catalog["metadata"]["signature"]["algorithm"] = "ed25519"
        with self.assertRaisesRegex(image_trust.TrustError, "algorithm"):
            image_trust.verify_image_trust(catalog, _policy(images))

    def test_unapproved_provenance_source_fails_closed(self):
        images = _images()
        # Signed under an unapproved source: the signature is valid, but the
        # provenance allowlist still rejects the catalog.
        catalog = _catalog(images, source="https://evil.invalid/workers")
        with self.assertRaisesRegex(image_trust.TrustError, "provenance source is not approved"):
            image_trust.verify_image_trust(catalog, _policy(images))

    def test_sbom_binding_failures_fail_closed(self):
        images = _images()
        catalog = _catalog(images)
        policy = _policy(images)
        policy["sbom"]["coq"]["image"] = images["lean"]
        with self.assertRaisesRegex(image_trust.TrustError, "does not bind the approved image"):
            image_trust.verify_image_trust(catalog, policy)
        policy = _policy(images)
        policy["sbom"]["julia"]["source_commit"] = "b" * 40
        with self.assertRaisesRegex(image_trust.TrustError, "does not bind the catalog"):
            image_trust.verify_image_trust(catalog, policy)

    def test_policy_with_placeholders_is_rejected(self):
        policy = _policy(_images())
        del policy["approved_keys"]
        with self.assertRaisesRegex(image_trust.TrustError, "missing approved_keys"):
            image_trust.verify_image_trust(_catalog(_images()), policy)
        policy = _policy(_images())
        policy["approved_keys"] = {}
        with self.assertRaisesRegex(image_trust.TrustError, "non-empty map"):
            image_trust.verify_image_trust(_catalog(_images()), policy)

    def test_legacy_unsigned_catalog_fails_signature_gate(self):
        images = _images()
        catalog = _catalog(images)
        catalog["metadata"]["signature"]["value"] = "0" * 128
        with self.assertRaisesRegex(image_trust.TrustError, "signature verification failed"):
            image_trust.verify_image_trust(catalog, _policy(images))


if __name__ == "__main__":
    unittest.main()
