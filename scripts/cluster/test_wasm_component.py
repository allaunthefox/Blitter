#!/usr/bin/env python3
from __future__ import annotations

import json
import unittest
from pathlib import Path

try:
    from .wasm_component import WasmManifestError, validate_manifest
except ImportError:
    from wasm_component import WasmManifestError, validate_manifest


MANIFEST = Path(__file__).with_name("mcp_wasm_component_manifest.example.json")


class WasmComponentTests(unittest.TestCase):
    def test_example_manifest_is_valid(self):
        value = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(validate_manifest(value)["authority"], "none")

    def test_rejects_imports_and_host_capabilities(self):
        value = json.loads(MANIFEST.read_text(encoding="utf-8"))
        with self.assertRaises(WasmManifestError):
            validate_manifest({**value, "imports": ["wasi:io"]})
        with self.assertRaises(WasmManifestError):
            validate_manifest({**value, "capabilities": {
                **value["capabilities"], "network": True
            }})

    def test_rejects_mutable_module_and_excessive_limits(self):
        value = json.loads(MANIFEST.read_text(encoding="utf-8"))
        with self.assertRaises(WasmManifestError):
            validate_manifest({**value, "module": "registry/tool:latest"})
        with self.assertRaises(WasmManifestError):
            validate_manifest({**value, "limits": {**value["limits"], "memory_mib": 257}})

    def test_rejects_unknown_fields_and_authority_claims(self):
        value = json.loads(MANIFEST.read_text(encoding="utf-8"))
        with self.assertRaises(WasmManifestError):
            validate_manifest({**value, "extra": True})
        with self.assertRaises(WasmManifestError):
            validate_manifest({**value, "authority": "verified"})


if __name__ == "__main__":
    unittest.main()
