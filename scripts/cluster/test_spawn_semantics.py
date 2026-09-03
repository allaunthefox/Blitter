#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

MODULE_PATH = pathlib.Path(__file__).with_name("spawn_semantics.py")
SPEC = importlib.util.spec_from_file_location("spawn_semantics", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
spawn = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = spawn
SPEC.loader.exec_module(spawn)


class SpawnSemanticsTests(unittest.TestCase):
    def test_unknown_control_still_allows_validated_webgpu_for_unrestricted_work(self) -> None:
        envelope = spawn.envelope_from_mapping({}, default_surface="webgpu")
        self.assertEqual(envelope.accessibility_profile, "unknown")
        self.assertEqual(envelope.execution_surfaces, frozenset({"webgpu"}))
        self.assertTrue(spawn.spawn_compatible(envelope))
        self.assertTrue(
            spawn.spawn_compatible(
                envelope,
                acceptable_surfaces={"webgpu", "wasm-webgpu", "wasm"},
            )
        )

    def test_unknown_control_does_not_satisfy_hard_control_requirement(self) -> None:
        envelope = spawn.envelope_from_mapping({}, default_surface="webgpu")
        self.assertFalse(
            spawn.spawn_compatible(
                envelope,
                requires_control={"accelerator-api"},
                acceptable_surfaces={"webgpu"},
            )
        )

    def test_sandboxed_webgpu_can_explicitly_control_accelerator_api(self) -> None:
        envelope = spawn.envelope_from_mapping(
            {
                "spawn_semantics_version": 1,
                "accessibility_profile": "sandbox",
                "controlled_subsystems": ["accelerator-api"],
                "execution_surfaces": ["webgpu"],
            }
        )
        self.assertTrue(
            spawn.spawn_compatible(
                envelope,
                requires_control={"accelerator-api"},
                acceptable_surfaces={"webgpu", "wasm-webgpu"},
            )
        )
        self.assertEqual(spawn.surface_capability_tags(envelope), frozenset({"surface:webgpu"}))

    def test_wasm_fallback_prevents_terminal_failure(self) -> None:
        envelope = spawn.envelope_from_mapping(
            {
                "accessibility_profile": "sandbox",
                "controlled_subsystems": ["userspace"],
                "execution_surfaces": ["wasm"],
            }
        )
        self.assertFalse(envelope.terminal)
        self.assertTrue(spawn.spawn_compatible(envelope, acceptable_surfaces={"webgpu", "wasm"}))

    def test_none_is_terminal_and_cannot_advertise_surfaces(self) -> None:
        terminal = spawn.envelope_from_mapping(
            {
                "accessibility_profile": "none",
                "controlled_subsystems": [],
                "execution_surfaces": [],
            }
        )
        self.assertTrue(terminal.terminal)
        self.assertFalse(spawn.spawn_compatible(terminal))
        with self.assertRaises(ValueError):
            spawn.envelope_from_mapping(
                {
                    "accessibility_profile": "none",
                    "controlled_subsystems": [],
                    "execution_surfaces": ["wasm"],
                }
            )

    def test_requires_control_and_surface_tags_are_strict(self) -> None:
        envelope = spawn.envelope_from_mapping(
            {
                "accessibility_profile": "direct",
                "controlled_subsystems": ["userspace", "loader", "filesystem"],
                "execution_surfaces": ["native", "fhs"],
            }
        )
        self.assertTrue(
            spawn.spawn_compatible(
                envelope,
                requires_control={"userspace", "loader"},
                acceptable_surfaces={"native"},
            )
        )
        self.assertFalse(
            spawn.spawn_compatible(
                envelope,
                requires_control={"kernel"},
                acceptable_surfaces={"native"},
            )
        )
        with self.assertRaises(ValueError):
            spawn.spawn_compatible(envelope, requires_control={"made-up-subsystem"})
        with self.assertRaises(ValueError):
            spawn.spawn_compatible(envelope, acceptable_surfaces={"made-up-surface"})


if __name__ == "__main__":
    unittest.main()
