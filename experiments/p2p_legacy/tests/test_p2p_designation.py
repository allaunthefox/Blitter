"""Tests for multi-axis node designation."""
from __future__ import annotations

import unittest
from p2p.designation import (
    Designation, Load, Proximity, SpawnEnvelope,
    envelope_from_mapping, spawn_compatible, surface_capability_tags,
    ACCESSIBILITY_RANK, CONTROLLED_SUBSYSTEMS, EXECUTION_SURFACES,
)


class DesignationTests(unittest.TestCase):
    def test_defaults(self):
        d = Designation(node_id="n1")
        self.assertEqual(d.capabilities.__class__, frozenset)
        self.assertEqual(d.accessibility_profile, "sandbox")

    def test_accessibility_rank(self):
        self.assertEqual(ACCESSIBILITY_RANK["direct"], 0)
        self.assertEqual(ACCESSIBILITY_RANK["sandbox"], 50)
        self.assertEqual(ACCESSIBILITY_RANK["none"], 255)

    def test_spawn_envelope_from_mapping(self):
        env = envelope_from_mapping({
            "accessibility_profile": "sandbox",
            "controlled_subsystems": ["accelerator-api"],
            "execution_surfaces": ["webgpu"],
        })
        self.assertEqual(env.accessibility_profile, "sandbox")
        self.assertEqual(env.controlled_subsystems, frozenset({"accelerator-api"}))
        self.assertEqual(env.execution_surfaces, frozenset({"webgpu"}))

    def test_envelope_terminal_none(self):
        with self.assertRaises(ValueError):
            envelope_from_mapping({"accessibility_profile": "none", "execution_surfaces": ["webgpu"]})

    def test_spawn_compatible(self):
        env = SpawnEnvelope("sandbox", 50, frozenset({"accelerator-api"}), frozenset({"webgpu"}))
        self.assertTrue(spawn_compatible(env, requires_control=frozenset({"accelerator-api"}), acceptable_surfaces=frozenset({"webgpu"})))
        self.assertFalse(spawn_compatible(env, requires_control=frozenset({"kernel"}), acceptable_surfaces=frozenset({"webgpu"})))

    def test_surface_capability_tags(self):
        env = SpawnEnvelope("sandbox", 50, frozenset(), frozenset({"webgpu"}))
        self.assertEqual(surface_capability_tags(env), frozenset({"surface:webgpu"}))

    def test_designation_availability(self):
        d = Designation(node_id="n1", availability_expires_unix_ms=1, sent_unix_ms=0)
        self.assertFalse(d.availability_live)


if __name__ == "__main__":
    unittest.main()
