#!/usr/bin/env python3
"""Local-only regression tests for node deployment readiness validation."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

try:
    from .image_provenance import build_record, canonical_bytes
    from .node_readiness import PROFILE_DIGEST, readiness_report
except ImportError:
    from image_provenance import build_record, canonical_bytes
    from node_readiness import PROFILE_DIGEST, readiness_report


D0 = "sha256:" + "0" * 64
D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64
D3 = "sha256:" + "3" * 64


class NodeReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.runtime_policy = root / "image-policy.json"
        self.gate_policy = root / "gate-policy.json"
        self.runtime_policy.write_text('{"default":"reject"}\n', encoding="ascii")
        self.gate_policy.write_text('{"allow":[]}\n', encoding="ascii")
        provenance = build_record(published_tag="registry/blitter:test", immutable_ref="registry/blitter@" + D0, registry_digest=D0, source_commit="a" * 40, semantic_profile_digest=PROFILE_DIGEST, blitter_daemon_sha256=D1, dockerfile_sha256=D2, stamp_requested=True, stamp_plugin_sha256=D3)
        self.provenance = root / "provenance.json"
        self.provenance.write_bytes(canonical_bytes(provenance))
        self.nodes = {"version": 1, "nodes": [{"name": "canary", "address": "100.64.0.1", "runner_user": "blitter", "role": "cpu", "runtime": "docker", "arch": "x86_64", "webgpu_port": 8790}]}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def manifest(self):
        policy = lambda path: {"path": str(path), "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}
        return {"schema": "mathpunch.blitter-node-readiness-manifest.v1", "node": "canary", "expected_ssh_host_fingerprint": "SHA256:" + "A" * 43, "provenance_path": str(self.provenance), "runtime_image_trust_policy": policy(self.runtime_policy), "published_gate_access_policy": policy(self.gate_policy), "envelope_policy": {"envelope_schema": "mathpunch.blitter-computation-envelope.v2", "profile_id": "BLITTER-ISA-V1", "profile_digest": PROFILE_DIGEST, "terminal_contract_digest": "sha256:5a9804978698791d50d937ba93b7f279344b2e0ea658f4e2ee7b2d5a3365fb7f", "assembler_module_digest": D1, "max_cpus": 2, "max_memory_mib": 128, "max_wall_ms": 1000}}

    def test_valid_local_bindings_produce_ready_report(self):
        report = readiness_report(self.manifest(), self.nodes)
        self.assertTrue(report["ready"])
        self.assertEqual(report["runner_user"], "blitter")
        self.assertEqual(report["immutable_image"], "registry/blitter@" + D0)

    def test_rejects_policy_digest_mismatch_and_missing_host_identity(self):
        manifest = self.manifest()
        manifest["runtime_image_trust_policy"]["digest"] = D1
        manifest["expected_ssh_host_fingerprint"] = ""
        report = readiness_report(manifest, self.nodes)
        self.assertFalse(report["ready"])
        self.assertTrue(any("host-key fingerprint" in item for item in report["blockers"]))
        self.assertTrue(any("content does not match" in item for item in report["blockers"]))

    def test_rejects_root_inventory_and_noncanonical_envelope_binding(self):
        manifest = self.manifest()
        manifest["envelope_policy"]["profile_id"] = "other"
        self.nodes["nodes"][0]["runner_user"] = "root"
        report = readiness_report(manifest, self.nodes)
        self.assertFalse(report["ready"])
        self.assertTrue(any("deployment-eligible" in item for item in report["blockers"]))
        self.assertTrue(any("canonical v2 binding" in item for item in report["blockers"]))


if __name__ == "__main__":
    unittest.main()
