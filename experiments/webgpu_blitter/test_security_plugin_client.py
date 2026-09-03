#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import tempfile
import textwrap
import unittest

from security_plugin_client import (
    ABI,
    CAP_STAMP,
    CAP_TLS,
    CAP_VERIFY,
    HANDSHAKE_SCHEMA,
    SecurityPlugin,
    SecurityPluginBlocked,
)


def _fake_plugin(root, *, abi=ABI, capabilities=None, static=True, cgo=False):
    if capabilities is None:
        capabilities = [CAP_TLS, CAP_STAMP, CAP_VERIFY]
    path = pathlib.Path(root) / "fake-security-plugin"
    handshake = {
        "schema": HANDSHAKE_SCHEMA,
        "abi": abi,
        "plugin_id": "fake",
        "plugin_version": "1.0.0",
        "capabilities": capabilities,
        "build": {"goos": "linux", "goarch": "amd64", "cgo": cgo, "static": static},
    }
    script = textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        import json, sys
        HANDSHAKE = {handshake!r}
        if len(sys.argv) >= 2 and sys.argv[1] == "handshake":
            print(json.dumps(HANDSHAKE, sort_keys=True))
            raise SystemExit(0)
        if len(sys.argv) >= 2 and sys.argv[1] == "stamp":
            sys.stdin.buffer.read()
            print(json.dumps({{"schema":"mathpunch.secure-stamp.v1","algorithm":"fake"}}))
            raise SystemExit(0)
        if len(sys.argv) >= 2 and sys.argv[1] == "verify":
            sys.stdin.buffer.read()
            print(json.dumps({{"schema":"mathpunch.secure-stamp-verification.v1","valid":True}}))
            raise SystemExit(0)
        raise SystemExit(2)
        """
    )
    path.write_text(script)
    path.chmod(0o755)
    return path


class SecurityPluginClientTests(unittest.TestCase):
    def test_plugin_is_optional_until_secure_capability_is_requested(self):
        old = os.environ.pop("BLITTER_SECURITY_PLUGIN", None)
        try:
            self.assertIsNone(SecurityPlugin.from_env(required=False))
            with self.assertRaisesRegex(SecurityPluginBlocked, "not configured"):
                SecurityPlugin.from_env(required=True)
        finally:
            if old is not None:
                os.environ["BLITTER_SECURITY_PLUGIN"] = old

    def test_handshake_and_capability_selection(self):
        with tempfile.TemporaryDirectory() as td:
            path = _fake_plugin(td)
            plugin = SecurityPlugin(path)
            hs = plugin.handshake()
            self.assertEqual(hs["abi"], ABI)
            self.assertIn(CAP_TLS, hs["capabilities"])
            self.assertEqual(plugin.require(CAP_STAMP)["plugin_id"], "fake")

    def test_wrong_abi_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = _fake_plugin(td, abi="future-or-wrong")
            with self.assertRaisesRegex(SecurityPluginBlocked, "ABI mismatch"):
                SecurityPlugin(path).handshake()

    def test_missing_capability_fails_only_that_requested_operation(self):
        with tempfile.TemporaryDirectory() as td:
            path = _fake_plugin(td, capabilities=[CAP_STAMP])
            plugin = SecurityPlugin(path)
            self.assertEqual(plugin.require(CAP_STAMP)["plugin_id"], "fake")
            with self.assertRaisesRegex(SecurityPluginBlocked, "lacks required capability"):
                plugin.require(CAP_TLS)

    def test_inconsistent_static_descriptor_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = _fake_plugin(td, static=True, cgo=True)
            with self.assertRaisesRegex(SecurityPluginBlocked, "static/cgo"):
                SecurityPlugin(path).handshake()

    def test_pinned_artifact_digest_is_checked_before_handshake(self):
        with tempfile.TemporaryDirectory() as td:
            path = _fake_plugin(td)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            SecurityPlugin(path, expected_sha256=digest).handshake()
            with self.assertRaisesRegex(SecurityPluginBlocked, "sha256 mismatch"):
                SecurityPlugin(path, expected_sha256="0" * 64).handshake()

    def test_secure_calls_use_process_plugin_when_requested(self):
        with tempfile.TemporaryDirectory() as td:
            path = _fake_plugin(td)
            key = pathlib.Path(td) / "key.pem"
            key.write_text("fake")
            stamp_path = pathlib.Path(td) / "stamp.json"
            stamp_path.write_text(json.dumps({"schema": "mathpunch.secure-stamp.v1"}))
            plugin = SecurityPlugin(path)
            stamp = plugin.stamp(b"receipt", key_path=key.resolve())
            self.assertEqual(stamp["schema"], "mathpunch.secure-stamp.v1")
            verified = plugin.verify(b"receipt", stamp_path=stamp_path.resolve())
            self.assertIs(verified["valid"], True)

    def test_tls_supervisor_command_is_drop_in_binary_path_plus_frozen_flags(self):
        with tempfile.TemporaryDirectory() as td:
            path = _fake_plugin(td)
            plugin = SecurityPlugin(path)
            argv = plugin.tls_serve_argv(
                listen="0.0.0.0:443",
                upstream="http://127.0.0.1:8790",
                cert_path="/run/secrets/cert.pem",
                key_path="/run/secrets/key.pem",
                max_body_bytes=1048576,
            )
            self.assertEqual(argv[0], str(path.resolve()))
            self.assertEqual(argv[1], "serve")
            self.assertIn("--upstream", argv)
            self.assertIn("http://127.0.0.1:8790", argv)

    def test_tls_supervisor_rejects_relative_secret_paths(self):
        with tempfile.TemporaryDirectory() as td:
            path = _fake_plugin(td)
            plugin = SecurityPlugin(path)
            with self.assertRaisesRegex(SecurityPluginBlocked, "certificate path must be absolute"):
                plugin.tls_serve_argv(
                    listen="0.0.0.0:443",
                    upstream="http://127.0.0.1:8790",
                    cert_path="cert.pem",
                    key_path="/run/secrets/key.pem",
                )
            with self.assertRaisesRegex(SecurityPluginBlocked, "key path must be absolute"):
                plugin.tls_serve_argv(
                    listen="0.0.0.0:443",
                    upstream="http://127.0.0.1:8790",
                    cert_path="/run/secrets/cert.pem",
                    key_path="key.pem",
                )


if __name__ == "__main__":
    unittest.main()
