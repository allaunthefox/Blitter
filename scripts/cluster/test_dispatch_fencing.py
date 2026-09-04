#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import computation_blob
import dispatch


NODE = dispatch.Node(
    name="test-node", address="127.0.0.1", ssh_user="root", role="gpu",
    runtime="docker", max_cpus=4, max_memory_gib=8, ephemeral=False, webgpu_port=8790,
)


def args(**overrides):
    value = dict(
        node=NODE.name, health=False, status=False, lease_ttl_ms=None,
        lease_expected_ms=None, lease_holder=None, envelope_file=None, timeout=120,
    )
    value.update(overrides)
    return argparse.Namespace(**value)


def envelope():
    return {
        "schema": computation_blob.SCHEMA,
        "request_id": "dispatch-1",
        "operation": computation_blob.OPERATION,
        "profile_id": computation_blob.semantic_profile_registry.U192_ADD_MOD_PROFILE_ID,
        "profile_digest": computation_blob.semantic_profile_registry.U192_ADD_MOD_PROFILE_DIGEST,
        "codec": computation_blob.CODEC,
        "inputs": [
            {"schema": computation_blob.CODEC, "format_tag": 0,
             "payload_hex": "01000000" + "00" * 20},
            {"schema": computation_blob.CODEC, "format_tag": 0,
             "payload_hex": "02000000" + "00" * 20},
        ],
        "resource_budget": {"cpus": 1, "memory_mib": 64, "wall_ms": 1000},
        "terminal_contract_digest": computation_blob.registered_terminal_contract_digest(
            computation_blob.semantic_profile_registry.resolve_profile(
                computation_blob.semantic_profile_registry.U192_ADD_MOD_PROFILE_ID,
                computation_blob.semantic_profile_registry.U192_ADD_MOD_PROFILE_DIGEST,
            )
        ),
        "assembler": {"module_digest": "sha256:" + "a" * 64, "interface": "wasi-preview2-component-v1"},
        "authority": "none",
    }


class DispatchFencingTests(unittest.TestCase):
    def test_execution_requires_fenced_canonical_envelope(self):
        with self.assertRaisesRegex(ValueError, "requires --lease-ttl-ms"):
            dispatch.cmd_webgpu({NODE.name: NODE}, args())
        with self.assertRaisesRegex(ValueError, "requires --envelope-file"):
            dispatch.cmd_webgpu({NODE.name: NODE}, args(lease_ttl_ms=1000))

    def test_envelope_client_transmits_raw_bytes_and_checks_exact_result(self):
        raw = computation_blob.canonical_bytes(envelope())
        expected = computation_blob.add_mod_result(envelope())
        response_raw = computation_blob.canonical_bytes(expected)
        lease = {"instance_id": "gate", "lease_epoch": 4, "lease_token": "x" * 64}
        with patch.object(
            dispatch, "webgpu_post_canonical", return_value=(expected, response_raw)
        ) as post:
            self.assertEqual(dispatch.webgpu_submit_envelope(NODE, raw, timeout=9, lease=lease), expected)
        post.assert_called_once_with(
            NODE, "/compute-envelope", raw, timeout=9,
            headers={
                "X-Blitter-Lease-Instance": "gate",
                "X-Blitter-Lease-Epoch": "4",
                "X-Blitter-Lease-Token": "x" * 64,
            },
        )

    def test_noncanonical_request_never_reaches_transport(self):
        with patch.object(dispatch, "webgpu_post_canonical") as post:
            with self.assertRaisesRegex(ValueError, "invalid computation envelope|canonical"):
                dispatch.webgpu_submit_envelope(NODE, b'{"schema":"wrong"}\n', lease={})
        post.assert_not_called()

    def test_fenced_envelope_orders_acquire_compute_release(self):
        lease = {"ok": True, "instance_id": "gate", "lease_epoch": 9, "lease_token": "x" * 64}
        raw = computation_blob.canonical_bytes(envelope())
        expected = computation_blob.add_mod_result(envelope())
        events = []
        with patch.object(dispatch, "webgpu_acquire_lease", side_effect=lambda *a, **k: events.append("acquire") or lease), \
             patch.object(dispatch, "webgpu_submit_envelope", side_effect=lambda *a, **k: events.append("compute") or expected), \
             patch.object(dispatch, "webgpu_release_lease", side_effect=lambda *a, **k: events.append("release") or {"ok": True, "released": True}):
            self.assertEqual(
                dispatch.webgpu_submit_envelope_fenced(
                    NODE, raw, holder="worker", ttl_ms=1000, expected_ms=None, timeout=10
                ),
                expected,
            )
        self.assertEqual(events, ["acquire", "compute", "release"])

    def test_cli_prints_only_canonical_result(self):
        raw = computation_blob.canonical_bytes(envelope())
        expected = computation_blob.add_mod_result(envelope())
        with patch.object(dispatch, "webgpu_submit_envelope_fenced", return_value=expected), \
             redirect_stdout(io.StringIO()) as output:
            path = Mock()
            path.read_bytes.return_value = raw
            self.assertEqual(
                dispatch.cmd_webgpu({NODE.name: NODE}, args(lease_ttl_ms=1000, envelope_file=path)), 0
            )
        self.assertEqual(json.loads(output.getvalue()), expected)


if __name__ == "__main__":
    unittest.main()
