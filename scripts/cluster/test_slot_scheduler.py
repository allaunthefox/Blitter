#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

MODULE_PATH = pathlib.Path(__file__).with_name("slot_scheduler.py")
SPEC = importlib.util.spec_from_file_location("slot_scheduler", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
scheduler = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = scheduler
SPEC.loader.exec_module(scheduler)


def heartbeat(
    *,
    node: str,
    node_id: str,
    sent: int,
    expires: int,
    lifecycle: str = "ephemeral",
    capabilities: list[str] | None = None,
    accessibility_profile: str | None = None,
    controlled_subsystems: list[str] | None = None,
    execution_surfaces: list[str] | None = None,
    status_ok: bool = True,
) -> dict:
    payload = {
        "node": node,
        "node_id": node_id,
        "instance_id": f"{node}-instance",
        "advertise_url": f"http://{node}:8790",
        "lifecycle": lifecycle,
        "uptime_ms": 123_000,
        "boot_started_unix_ms": max(0, sent - 123_000),
        "hardware": {
            "arch": "x86_64",
            "logical_cpus": 54 if "h100" in node else 8,
            "memory_bytes": 274877906944,
            "cpu_model": "test-cpu",
            "accelerator_adapter": "H100" if "h100" in node else "RADV",
            "accelerator_backend": "Vulkan",
        },
        "sent_unix_ms": sent,
        "availability_expires_unix_ms": expires,
        "retire_at_unix_ms": None,
        "status_ok": status_ok,
        "max_concurrent_compute": 1,
        "active_compute": 0,
        "queued_compute": 0,
        "heartbeat_seq": 1,
        "status_rtt_ms": 2,
        "slot_capabilities": capabilities or ["webgpu-blitter", "exact-i32"],
    }
    if accessibility_profile is not None:
        payload["spawn_semantics_version"] = 1
        payload["accessibility_profile"] = accessibility_profile
        payload["controlled_subsystems"] = controlled_subsystems or []
        payload["execution_surfaces"] = execution_surfaces or []
    return payload


class SlotSchedulerTests(unittest.TestCase):
    def test_ephemeral_rental_history_does_not_imply_current_availability(self) -> None:
        payload = heartbeat(
            node="rental-h100",
            node_id="rental-h100|ephemeral|x86_64|cpu=54|mem=262144MiB|boot=abcd1234",
            sent=10_000,
            expires=13_000,
        )
        live = scheduler.slot_pool_from_heartbeat(payload, now_unix_ms=12_000)
        expired = scheduler.slot_pool_from_heartbeat(payload, now_unix_ms=13_001)
        self.assertEqual(live.node_id, expired.node_id)
        self.assertTrue(live.ephemeral)
        self.assertTrue(live.availability_live)
        self.assertFalse(expired.availability_live)
        self.assertEqual(expired.reason, "availability-expired")

        work = scheduler.WorkSpec(goal_id="g", work_id="w", requires=frozenset({"webgpu-blitter"}))
        self.assertEqual(scheduler.rank_slot_pools(work, [live]), [live])
        self.assertEqual(scheduler.rank_slot_pools(work, [expired]), [])

    def test_known_retirement_removes_rental_even_if_heartbeat_ttl_remains(self) -> None:
        payload = heartbeat(
            node="rental-h100",
            node_id="rental-h100|ephemeral|x86_64|cpu=54|mem=262144MiB|boot=abcd1234",
            sent=10_000,
            expires=20_000,
        )
        payload["retire_at_unix_ms"] = 15_000
        pool = scheduler.slot_pool_from_heartbeat(payload, now_unix_ms=15_000)
        self.assertFalse(pool.availability_live)
        self.assertFalse(pool.healthy)
        self.assertEqual(pool.reason, "node-retired")

    def test_degraded_current_pool_can_move_without_fit_gain(self) -> None:
        old_payload = heartbeat(
            node="old-rental",
            node_id="old-rental|ephemeral|x86_64|cpu=8|mem=32768MiB|boot=11111111",
            sent=10_000,
            expires=11_000,
        )
        new_payload = heartbeat(
            node="new-rental",
            node_id="new-rental|ephemeral|x86_64|cpu=8|mem=32768MiB|boot=22222222",
            sent=12_000,
            expires=20_000,
        )
        old = scheduler.slot_pool_from_heartbeat(old_payload, now_unix_ms=12_500)
        new = scheduler.slot_pool_from_heartbeat(new_payload, now_unix_ms=12_500)
        work = scheduler.WorkSpec(
            goal_id="g",
            work_id="w",
            requires=frozenset({"webgpu-blitter"}),
            checkpointable=True,
            state="running",
            current_pool_id=old.pool_id,
            min_migration_gain=100,
        )
        decision = scheduler.decide_placement(work, [new], current_pool=old)
        self.assertEqual(decision.action, "migrate")
        self.assertEqual(decision.to_pool_id, new.pool_id)

    def test_legacy_webgpu_heartbeat_is_nonterminal_but_control_unknown(self) -> None:
        payload = heartbeat(
            node="legacy-blitter",
            node_id="legacy-blitter|persistent|x86_64|cpu=8|mem=32768MiB|boot=aaaaaaaa",
            sent=20_000,
            expires=25_000,
            lifecycle="persistent",
        )
        pool = scheduler.slot_pool_from_heartbeat(payload, now_unix_ms=21_000)
        self.assertEqual(pool.accessibility_profile, "unknown")
        self.assertEqual(pool.controlled_subsystems, frozenset())
        self.assertEqual(pool.execution_surfaces, frozenset({"webgpu"}))
        self.assertIn("surface:webgpu", pool.capabilities)

        ordinary = scheduler.WorkSpec(
            goal_id="g",
            work_id="legacy-ok",
            requires=frozenset({"webgpu-blitter"}),
            acceptable_surfaces=frozenset({"webgpu", "wasm"}),
        )
        controlled = scheduler.WorkSpec(
            goal_id="g",
            work_id="legacy-control-denied",
            requires=frozenset({"webgpu-blitter"}),
            requires_control=frozenset({"accelerator-api"}),
            acceptable_surfaces=frozenset({"webgpu"}),
        )
        self.assertEqual(scheduler.rank_slot_pools(ordinary, [pool]), [pool])
        self.assertEqual(scheduler.rank_slot_pools(controlled, [pool]), [])

    def test_failed_legacy_status_does_not_infer_webgpu_surface(self) -> None:
        payload = heartbeat(
            node="failed-legacy",
            node_id="failed-legacy|persistent|x86_64|cpu=8|mem=32768MiB|boot=abababab",
            sent=22_000,
            expires=27_000,
            lifecycle="persistent",
            status_ok=False,
        )
        pool = scheduler.slot_pool_from_heartbeat(payload, now_unix_ms=23_000)
        self.assertFalse(pool.status_ok)
        self.assertFalse(pool.healthy)
        self.assertEqual(pool.accessibility_profile, "unknown")
        self.assertEqual(pool.controlled_subsystems, frozenset())
        self.assertEqual(pool.execution_surfaces, frozenset())
        self.assertNotIn("surface:webgpu", pool.capabilities)
        self.assertEqual(pool.reason, "status-unavailable")

    def test_explicit_sandbox_webgpu_satisfies_accelerator_api_control(self) -> None:
        payload = heartbeat(
            node="sandbox-blitter",
            node_id="sandbox-blitter|ephemeral|x86_64|cpu=8|mem=32768MiB|boot=bbbbbbbb",
            sent=30_000,
            expires=35_000,
            accessibility_profile="sandbox",
            controlled_subsystems=["accelerator-api"],
            execution_surfaces=["webgpu"],
        )
        pool = scheduler.slot_pool_from_heartbeat(payload, now_unix_ms=31_000)
        work = scheduler.WorkSpec(
            goal_id="g",
            work_id="sandbox-ok",
            requires=frozenset({"webgpu-blitter"}),
            requires_control=frozenset({"accelerator-api"}),
            acceptable_surfaces=frozenset({"webgpu", "wasm-webgpu"}),
        )
        self.assertEqual(scheduler.rank_slot_pools(work, [pool]), [pool])

    def test_wasm_surface_is_valid_fallback_before_terminal_failure(self) -> None:
        payload = heartbeat(
            node="wasm-worker",
            node_id="wasm-worker|ephemeral|x86_64|cpu=8|mem=32768MiB|boot=cccccccc",
            sent=40_000,
            expires=45_000,
            capabilities=["portable-blitter", "exact-i32"],
            accessibility_profile="sandbox",
            controlled_subsystems=["userspace"],
            execution_surfaces=["wasm"],
        )
        pool = scheduler.slot_pool_from_heartbeat(payload, now_unix_ms=41_000)
        work = scheduler.WorkSpec(
            goal_id="g",
            work_id="wasm-fallback",
            requires=frozenset({"portable-blitter", "exact-i32"}),
            requires_control=frozenset({"userspace"}),
            acceptable_surfaces=frozenset({"webgpu", "wasm-webgpu", "wasm"}),
        )
        self.assertEqual(scheduler.rank_slot_pools(work, [pool]), [pool])
        self.assertIn("surface:wasm", pool.capabilities)

    def test_accessibility_rank_breaks_equal_fit_in_favor_of_lower_cost_envelope(self) -> None:
        direct_payload = heartbeat(
            node="direct-worker",
            node_id="direct-worker|persistent|x86_64|cpu=8|mem=32768MiB|boot=dddddddd",
            sent=50_000,
            expires=55_000,
            lifecycle="persistent",
            accessibility_profile="direct",
            controlled_subsystems=["userspace", "accelerator-api"],
            execution_surfaces=["native", "webgpu"],
        )
        sandbox_payload = heartbeat(
            node="sandbox-worker",
            node_id="sandbox-worker|persistent|x86_64|cpu=8|mem=32768MiB|boot=eeeeeeee",
            sent=50_000,
            expires=55_000,
            lifecycle="persistent",
            accessibility_profile="sandbox",
            controlled_subsystems=["userspace", "accelerator-api"],
            execution_surfaces=["webgpu"],
        )
        direct = scheduler.slot_pool_from_heartbeat(direct_payload, now_unix_ms=51_000)
        sandbox = scheduler.slot_pool_from_heartbeat(sandbox_payload, now_unix_ms=51_000)
        work = scheduler.WorkSpec(
            goal_id="g",
            work_id="prefer-direct",
            requires=frozenset({"webgpu-blitter"}),
            requires_control=frozenset({"accelerator-api"}),
            acceptable_surfaces=frozenset({"webgpu"}),
        )
        ranked = scheduler.rank_slot_pools(work, [sandbox, direct])
        self.assertEqual([pool.pool_id for pool in ranked], [direct.pool_id, sandbox.pool_id])

    def test_none_profile_is_not_schedulable_even_when_status_is_healthy(self) -> None:
        payload = heartbeat(
            node="dead-surface",
            node_id="dead-surface|ephemeral|x86_64|cpu=8|mem=32768MiB|boot=ffffffff",
            sent=60_000,
            expires=65_000,
            accessibility_profile="none",
            controlled_subsystems=[],
            execution_surfaces=[],
        )
        pool = scheduler.slot_pool_from_heartbeat(payload, now_unix_ms=61_000)
        self.assertTrue(pool.healthy)  # liveness/load health is orthogonal to spawn eligibility
        self.assertEqual(pool.reason, "no-execution-surface")
        work = scheduler.WorkSpec(goal_id="g", work_id="no-surface")
        self.assertEqual(scheduler.rank_slot_pools(work, [pool]), [])


if __name__ == "__main__":
    unittest.main()
