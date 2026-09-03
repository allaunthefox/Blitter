#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest
from unittest import mock

MODULE_PATH = pathlib.Path(__file__).with_name("blitter_heartbeat.py")
SPEC = importlib.util.spec_from_file_location("blitter_heartbeat", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
heartbeat = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = heartbeat
SPEC.loader.exec_module(heartbeat)


class HeartbeatTests(unittest.TestCase):
    def test_failed_status_fails_closed(self) -> None:
        payload = heartbeat.heartbeat_payload(
            node="nixos-laptop",
            instance_id="instance-1",
            seq=7,
            advertise_url="http://100.102.173.61:8790",
            status=None,
            status_error="timeout",
        )
        self.assertFalse(payload["status_ok"])
        self.assertTrue(payload["busy"])
        self.assertFalse(payload["idle"])
        self.assertEqual(payload["accessibility_profile"], "unknown")
        self.assertEqual(payload["controlled_subsystems"], [])
        self.assertEqual(payload["execution_surfaces"], [])
        self.assertEqual(payload["slot_capabilities"], [])
        heartbeat.validate_heartbeat(payload)

    def test_receiver_records_skew_and_sequence_gap(self) -> None:
        payload = heartbeat.heartbeat_payload(
            node="qfox-1",
            instance_id="instance-2",
            seq=9,
            advertise_url="http://100.88.57.96:8790",
            status={
                "status_rtt_ms": 3,
                "busy": False,
                "idle": True,
                "active_compute": 0,
                "queued_compute": 0,
                "max_concurrent_compute": 1,
                "status_seq": 44,
                "adapter": "test",
                "backend": "Vulkan",
            },
            status_error=None,
        )
        payload["sent_unix_ms"] = 1_000
        payload["availability_expires_unix_ms"] = 5_000
        enriched = heartbeat.annotate_received(
            heartbeat.validate_heartbeat(payload),
            received_unix_ms=1_037,
            previous_seq=6,
        )
        self.assertEqual(enriched["apparent_skew_ms"], 37)
        self.assertEqual(enriched["sequence_gap"], 2)
        self.assertFalse(enriched["reordered"])
        self.assertFalse(enriched["duplicate"])

    def test_receiver_marks_reordering(self) -> None:
        payload = heartbeat.heartbeat_payload(
            node="nasfox",
            instance_id="instance-3",
            seq=3,
            advertise_url="http://nasfox:8790",
            status=None,
            status_error="unavailable",
        )
        enriched = heartbeat.annotate_received(
            payload,
            received_unix_ms=payload["sent_unix_ms"],
            previous_seq=5,
        )
        self.assertTrue(enriched["reordered"])
        self.assertEqual(enriched["sequence_gap"], 0)

    def test_endpoint_parser(self) -> None:
        endpoint = heartbeat.parse_endpoint("100.79.14.103:8791")
        self.assertEqual(endpoint.host, "100.79.14.103")
        self.assertEqual(endpoint.port, 8791)

    def test_ephemeral_identity_is_human_visible_and_boot_scoped(self) -> None:
        identity = heartbeat.collect_node_identity("rental-h100", lifecycle="ephemeral")
        self.assertIn("rental-h100|ephemeral|", identity["node_id"])
        self.assertIn("|cpu=", identity["node_id"])
        self.assertIn("|mem=", identity["node_id"])
        self.assertIn("|boot=", identity["node_id"])
        self.assertTrue(identity["boot_id"])

    def test_ephemeral_presence_expires_but_identity_remains(self) -> None:
        identity = {
            "node_id": "rental-h100|ephemeral|x86_64|cpu=54|mem=262144MiB|boot=abcd1234",
            "boot_id": "abcd1234-0000-0000-0000-000000000000",
            "hardware": {
                "arch": "x86_64",
                "logical_cpus": 54,
                "memory_bytes": 274877906944,
                "cpu_model": "test-cpu",
            },
        }
        status = {
            "status_rtt_ms": 2,
            "busy": False,
            "idle": True,
            "active_compute": 0,
            "queued_compute": 0,
            "max_concurrent_compute": 1,
            "status_seq": 1,
            "adapter": "H100",
            "backend": "Vulkan",
        }
        with mock.patch.object(heartbeat, "unix_ms", return_value=10_000), mock.patch.object(heartbeat, "system_uptime_ms", return_value=5_000):
            payload = heartbeat.heartbeat_payload(
                node="rental-h100",
                instance_id="rental-instance",
                seq=1,
                advertise_url="http://rental-h100:8790",
                status=status,
                status_error=None,
                node_identity=identity,
                lifecycle="ephemeral",
                availability_ttl_ms=3_000,
            )
        validated = heartbeat.validate_heartbeat(payload)
        live = heartbeat.annotate_received(validated, received_unix_ms=12_000, previous_seq=None)
        expired = heartbeat.annotate_received(validated, received_unix_ms=13_001, previous_seq=None)
        self.assertEqual(live["node_id"], expired["node_id"])
        self.assertTrue(live["available_by_announcement"])
        self.assertFalse(live["availability_expired"])
        self.assertTrue(expired["availability_expired"])
        self.assertFalse(expired["available_by_announcement"])

    def test_epoch_era_sender_is_explicit_time_anomaly(self) -> None:
        payload = heartbeat.heartbeat_payload(
            node="bad-clock",
            instance_id="instance-bad-clock",
            seq=1,
            advertise_url="http://bad-clock:8790",
            status=None,
            status_error="unavailable",
        )
        payload["sent_unix_ms"] = 0
        payload["availability_expires_unix_ms"] = payload["availability_ttl_ms"]
        enriched = heartbeat.annotate_received(
            heartbeat.validate_heartbeat(payload),
            received_unix_ms=1_800_000_000_000,
            previous_seq=None,
        )
        self.assertTrue(enriched["sender_clock_invalid"])
        self.assertTrue(enriched["time_anomaly"])
        self.assertEqual(enriched["time_anomaly_reason"], "sender_clock_before_2000")

    def test_explicit_spawn_extension_requires_surface_capability_tag(self) -> None:
        status = {
            "status_rtt_ms": 2,
            "busy": False,
            "idle": True,
            "active_compute": 0,
            "queued_compute": 0,
            "max_concurrent_compute": 1,
            "status_seq": 2,
            "adapter": "RADV",
            "backend": "Vulkan",
            "spawn_semantics_version": 1,
            "accessibility_profile": "sandbox",
            "controlled_subsystems": ["accelerator-api"],
            "execution_surfaces": ["webgpu"],
            "slot_capabilities": ["webgpu-blitter", "exact-i32", "surface:webgpu"],
        }
        payload = heartbeat.heartbeat_payload(
            node="sandbox",
            instance_id="sandbox-1",
            seq=1,
            advertise_url="http://sandbox:8790",
            status=status,
            status_error=None,
        )
        heartbeat.validate_heartbeat(payload)
        payload["slot_capabilities"] = ["webgpu-blitter", "exact-i32"]
        with self.assertRaises(ValueError):
            heartbeat.validate_heartbeat(payload)

    def test_partial_spawn_extension_is_rejected(self) -> None:
        payload = heartbeat.heartbeat_payload(
            node="legacy",
            instance_id="legacy-1",
            seq=1,
            advertise_url="http://legacy:8790",
            status={
                "status_rtt_ms": 2,
                "busy": False,
                "idle": True,
                "active_compute": 0,
                "queued_compute": 0,
                "max_concurrent_compute": 1,
                "status_seq": 2,
                "adapter": "RADV",
                "backend": "Vulkan",
            },
            status_error=None,
        )
        payload["accessibility_profile"] = "sandbox"
        with self.assertRaises(ValueError):
            heartbeat.validate_heartbeat(payload)

    def test_legacy_status_snapshot_normalizes_to_unknown_control_webgpu(self) -> None:
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                return b'{"ok":true,"busy":false,"idle":true,"active_compute":0,"queued_compute":0,"max_concurrent_compute":1,"status_seq":9,"adapter":"RADV","backend":"Vulkan"}'

        with mock.patch.object(heartbeat.urllib.request, "urlopen", return_value=FakeResponse()):
            snapshot = heartbeat.status_snapshot("http://legacy:8790/status", 100)
        self.assertEqual(snapshot["accessibility_profile"], "unknown")
        self.assertEqual(snapshot["controlled_subsystems"], [])
        self.assertEqual(snapshot["execution_surfaces"], ["webgpu"])
        self.assertIn("surface:webgpu", snapshot["slot_capabilities"])


if __name__ == "__main__":
    unittest.main()
