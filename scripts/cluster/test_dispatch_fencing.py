#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import call, patch

import dispatch


NODE = dispatch.Node(
    name="test-node",
    address="127.0.0.1",
    ssh_user="root",
    role="gpu",
    runtime="docker",
    max_cpus=4,
    max_memory_gib=8,
    ephemeral=False,
    webgpu_port=8790,
)


def args(**overrides):
    base = dict(
        node=NODE.name,
        health=False,
        status=False,
        require_idle=False,
        lease_ttl_ms=None,
        lease_expected_ms=None,
        lease_holder=None,
        allow_unfenced=False,
        job_file=None,
        batch_file=None,
        timeout=120,
        a=["0,0,1"],
        b=["0,0,1"],
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class DispatchFencingTests(unittest.TestCase):
    def test_default_compute_refuses_without_authoritative_lease(self):
        with self.assertRaisesRegex(ValueError, "requires --lease-ttl-ms"):
            dispatch.cmd_webgpu({NODE.name: NODE}, args())

    def test_unfenced_escape_hatch_always_preflights_idle_before_submit(self):
        events = []

        def idle(node, timeout=120):
            events.append("idle")
            return {"ok": True, "idle": True, "busy": False,
                    "active_compute": 0, "queued_compute": 0}

        def submit(node, job, timeout=120, *, lease=None):
            events.append("submit")
            self.assertIsNone(lease)
            return {"ok": True, "adapter": "test", "terms": [[0, 0, 1]]}

        with patch.object(dispatch, "webgpu_require_idle", side_effect=idle), \
             patch.object(dispatch, "webgpu_submit", side_effect=submit), \
             redirect_stdout(io.StringIO()):
            rc = dispatch.cmd_webgpu(
                {NODE.name: NODE}, args(allow_unfenced=True)
            )
        self.assertEqual(rc, 0)
        self.assertEqual(events, ["idle", "submit"])

    def test_unfenced_busy_preflight_prevents_submit(self):
        with patch.object(
            dispatch,
            "webgpu_require_idle",
            side_effect=RuntimeError("busy"),
        ), patch.object(dispatch, "webgpu_submit") as submit:
            with self.assertRaisesRegex(RuntimeError, "busy"):
                dispatch.cmd_webgpu(
                    {NODE.name: NODE}, args(allow_unfenced=True)
                )
        submit.assert_not_called()

    def test_fenced_helper_orders_acquire_compute_release(self):
        events = []
        lease = {
            "ok": True,
            "instance_id": "gate-instance",
            "lease_epoch": 9,
            "lease_token": "a" * 64,
        }

        def acquire(node, *, holder, ttl_ms, expected_ms=None, timeout=120):
            events.append(("acquire", holder, ttl_ms, expected_ms, timeout))
            return lease

        def submit(node, job, timeout=120, *, lease=None):
            events.append(("compute", lease, timeout))
            return {"ok": True, "terms": [[0, 0, 1]]}

        def release(node, received, timeout=120):
            events.append(("release", received, timeout))
            return {"ok": True, "released": True}

        with patch.object(dispatch, "webgpu_acquire_lease", side_effect=acquire), \
             patch.object(dispatch, "webgpu_submit", side_effect=submit), \
             patch.object(dispatch, "webgpu_release_lease", side_effect=release):
            result = dispatch.webgpu_submit_fenced(
                NODE,
                {"a": [[0, 0, 1]], "b": [[0, 0, 1]]},
                holder="worker-a",
                ttl_ms=30000,
                expected_ms=12000,
                timeout=77,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            events,
            [
                ("acquire", "worker-a", 30000, 12000, 77),
                ("compute", lease, 77),
                ("release", lease, 77),
            ],
        )

    def test_acquire_failure_prevents_compute_and_release(self):
        with patch.object(
            dispatch,
            "webgpu_acquire_lease",
            side_effect=RuntimeError("lease held"),
        ), patch.object(dispatch, "webgpu_submit") as submit, \
             patch.object(dispatch, "webgpu_release_lease") as release:
            with self.assertRaisesRegex(RuntimeError, "lease held"):
                dispatch.webgpu_submit_fenced(
                    NODE, {"a": [], "b": []},
                    holder="worker", ttl_ms=1000,
                    expected_ms=None, timeout=10,
                )
        submit.assert_not_called()
        release.assert_not_called()

    def test_compute_failure_attempts_release_but_preserves_primary_error(self):
        lease = {
            "ok": True,
            "instance_id": "gate-instance",
            "lease_epoch": 3,
            "lease_token": "b" * 64,
        }
        with patch.object(dispatch, "webgpu_acquire_lease", return_value=lease), \
             patch.object(
                 dispatch, "webgpu_submit",
                 side_effect=RuntimeError("fenced_at_commit"),
             ), \
             patch.object(
                 dispatch, "webgpu_release_lease",
                 side_effect=RuntimeError("release also failed"),
             ) as release:
            with self.assertRaisesRegex(RuntimeError, "fenced_at_commit"):
                dispatch.webgpu_submit_fenced(
                    NODE, {"a": [], "b": []},
                    holder="worker", ttl_ms=1000,
                    expected_ms=None, timeout=10,
                )
        release.assert_called_once_with(NODE, lease, timeout=10)

    def test_release_failure_after_success_fails_orchestration_closed(self):
        lease = {
            "ok": True,
            "instance_id": "gate-instance",
            "lease_epoch": 4,
            "lease_token": "c" * 64,
        }
        with patch.object(dispatch, "webgpu_acquire_lease", return_value=lease), \
             patch.object(
                 dispatch, "webgpu_submit",
                 return_value={"ok": True, "terms": []},
             ), \
             patch.object(
                 dispatch, "webgpu_release_lease",
                 side_effect=RuntimeError("release not confirmed"),
             ):
            with self.assertRaisesRegex(RuntimeError, "release not confirmed"):
                dispatch.webgpu_submit_fenced(
                    NODE, {"a": [], "b": []},
                    holder="worker", ttl_ms=1000,
                    expected_ms=None, timeout=10,
                )

    def test_lease_gate_status_counts_active_reservation_as_busy(self):
        raw = {
            "schema": dispatch.LEASE_STATUS_SCHEMA,
            "ok": True,
            "instance_id": "gate-instance",
            "lease_epoch": 7,
            "lease_active": True,
            "lease_expired": False,
            "lease_holder": "worker",
            "lease_remaining_ms": 500,
            "in_flight": False,
        }
        with patch.object(dispatch, "webgpu_get_json", return_value=raw):
            normalized = dispatch.webgpu_status(NODE)
        self.assertTrue(normalized["busy"])
        self.assertFalse(normalized["idle"])
        self.assertEqual(normalized["active_compute"], 0)
        self.assertIs(normalized["lease_gate"], raw)

    def test_cli_contract_rejects_conflicting_or_incomplete_modes(self):
        bad = [
            (args(lease_ttl_ms=1000, allow_unfenced=True), "mutually exclusive"),
            (args(lease_expected_ms=10), "requires --lease-ttl-ms"),
            (args(require_idle=True, lease_ttl_ms=1000), "only for"),
        ]
        for namespace, message in bad:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    dispatch.cmd_webgpu({NODE.name: NODE}, namespace)


if __name__ == "__main__":
    unittest.main()
