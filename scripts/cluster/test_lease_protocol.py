#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

MODULE_PATH = pathlib.Path(__file__).with_name("lease_protocol.py")
SPEC = importlib.util.spec_from_file_location("lease_protocol", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
lease_protocol = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = lease_protocol
SPEC.loader.exec_module(lease_protocol)


class LeaseProtocolTests(unittest.TestCase):
    def test_announcement_carries_expected_budget(self) -> None:
        intent = lease_protocol.LeaseIntent(
            goal_id="goal-1",
            work_id="work-7",
            expected_lease_ms=30_000,
            checkpointable=True,
        )
        payload = intent.announcement(announced_unix_ms=123_000)
        self.assertEqual(payload["expected_lease_ms"], 30_000)
        self.assertEqual(payload["profound_overrun_factor"], 4)
        self.assertEqual(payload["profound_overrun_ms"], 120_000)
        self.assertTrue(payload["checkpointable"])

    def test_profound_overrun_emits_log_only_note(self) -> None:
        intent = lease_protocol.LeaseIntent("goal", "work", 10_000)
        observation = lease_protocol.observe_lease_budget(
            intent,
            slot_id="qfox:gpu0",
            started_unix_ms=1_000,
            observed_unix_ms=41_000,
        )
        self.assertTrue(observation.over_budget)
        self.assertTrue(observation.profoundly_over_budget)
        self.assertIsNotNone(observation.log_note)
        assert observation.log_note is not None
        self.assertEqual(observation.log_note["event"], "lease_budget_overrun")
        self.assertEqual(observation.log_note["action"], "log-only")
        self.assertEqual(observation.log_note["budget_ratio"], 4.0)

    def test_ordinary_overrun_does_not_emit_profound_note(self) -> None:
        intent = lease_protocol.LeaseIntent("goal", "work", 10_000)
        observation = lease_protocol.observe_lease_budget(
            intent,
            slot_id="slot",
            started_unix_ms=1_000,
            observed_unix_ms=21_000,
        )
        self.assertTrue(observation.over_budget)
        self.assertFalse(observation.profoundly_over_budget)
        self.assertIsNone(observation.log_note)

    def test_overrun_note_is_suppressible_after_first_emit(self) -> None:
        intent = lease_protocol.LeaseIntent("goal", "work", 1_000)
        observation = lease_protocol.observe_lease_budget(
            intent,
            slot_id="slot",
            started_unix_ms=0,
            observed_unix_ms=9_000,
            overrun_note_already_emitted=True,
        )
        self.assertTrue(observation.profoundly_over_budget)
        self.assertIsNone(observation.log_note)


if __name__ == "__main__":
    unittest.main()
