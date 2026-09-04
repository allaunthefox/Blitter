#!/usr/bin/env python3
import copy
import unittest

try:
    from .image_catalog import CatalogError, CANONICAL_ARGV, SCHEMA, catalog_digest, canonical_bytes, validate_catalog
except ImportError:
    from image_catalog import CatalogError, CANONICAL_ARGV, SCHEMA, catalog_digest, canonical_bytes, validate_catalog

D0 = "sha256:" + "0" * 64
D1 = "sha256:" + "1" * 64
LANGUAGES = ("lean", "coq", "python", "r", "julia", "rust")
APPROVED = {language: f"registry.test/blitter/{language}@{D0}" for language in LANGUAGES}
CEILINGS = {language: {"cpus": 4, "memory_mib": 4096} for language in LANGUAGES}


def example():
    return {
        "schema": SCHEMA,
        "images": {
            language: {
                "image": APPROVED[language],
                "argv": list(CANONICAL_ARGV[language]),
                "toolchain": {"name": language, "version": "fixture-1", "identity": f"{language}@fixture-1"},
                "network": False,
                "resources": {"cpus": 1, "memory_mib": 512},
            }
            for language in LANGUAGES
        },
        "metadata": {
            "signature": {"algorithm": "fixture-detached-v1", "key_id": "fixture-key", "value": "fixture-signature"},
            "provenance": {"source": "fixture", "source_commit": "fixture-commit", "build_digest": D1},
        },
    }


class ImageCatalogTests(unittest.TestCase):
    def test_valid_catalog_and_deterministic_digest(self):
        catalog = example()
        self.assertEqual(tuple(validate_catalog(catalog, approved_images=APPROVED, resource_ceilings=CEILINGS)["languages"]), LANGUAGES)
        self.assertEqual(catalog_digest(catalog, approved_images=APPROVED, resource_ceilings=CEILINGS), catalog_digest(copy.deepcopy(catalog), approved_images=APPROVED, resource_ceilings=CEILINGS))
        self.assertTrue(canonical_bytes(catalog).endswith(b"\n"))

    def test_exact_languages_and_strict_schema(self):
        bad = example()
        bad["images"]["go"] = copy.deepcopy(bad["images"]["rust"])
        with self.assertRaises(CatalogError):
            validate_catalog(bad, approved_images=APPROVED, resource_ceilings=CEILINGS)
        bad = example()
        bad["metadata"]["extra"] = True
        with self.assertRaises(CatalogError):
            validate_catalog(bad, approved_images=APPROVED, resource_ceilings=CEILINGS)

    def test_approval_pin_argv_identity_network_and_ceiling(self):
        mutations = (("image", "registry.test/blitter/lean@" + D1), ("argv", ["sh", "-c", "lean"]), ("network", True))
        for field, value in mutations:
            bad = example(); bad["images"]["lean"][field] = value
            with self.assertRaises(CatalogError):
                validate_catalog(bad, approved_images=APPROVED, resource_ceilings=CEILINGS)
        bad = example(); bad["images"]["lean"]["toolchain"]["identity"] = "wrong"
        with self.assertRaises(CatalogError):
            validate_catalog(bad, approved_images=APPROVED, resource_ceilings=CEILINGS)
        bad = example(); bad["images"]["lean"]["resources"]["cpus"] = 5
        with self.assertRaises(CatalogError):
            validate_catalog(bad, approved_images=APPROVED, resource_ceilings=CEILINGS)

    def test_required_attestation_fields_and_digest_change(self):
        bad = example(); del bad["metadata"]["signature"]["value"]
        with self.assertRaises(CatalogError):
            validate_catalog(bad, approved_images=APPROVED, resource_ceilings=CEILINGS)
        changed = example(); changed["images"]["rust"]["resources"]["memory_mib"] = 513
        self.assertNotEqual(catalog_digest(example(), approved_images=APPROVED, resource_ceilings=CEILINGS), catalog_digest(changed, approved_images=APPROVED, resource_ceilings=CEILINGS))


if __name__ == "__main__":
    unittest.main()
