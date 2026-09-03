#!/usr/bin/env python3
"""Process-ABI client for optional blitter security plugins.

Base compute never requires or auto-spawns this plugin. Callers instantiate this
client only when a secure capability is requested. Missing/incompatible plugins
raise SecurityPluginBlocked; callers must not silently downgrade the requested
secure operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess

ABI = "mathpunch.blitter-security-plugin.v1"
HANDSHAKE_SCHEMA = "mathpunch.blitter-security-plugin-handshake.v1"
CAP_TLS = "tls.server.http1.reverse-proxy"
CAP_STAMP = "stamp.ed25519.sha256"
CAP_VERIFY = "verify.ed25519.sha256"
DEFAULT_TIMEOUT_SECONDS = 3


class SecurityPluginBlocked(RuntimeError):
    pass


class SecurityPlugin:
    def __init__(self, path, *, expected_sha256=None, timeout=DEFAULT_TIMEOUT_SECONDS):
        self.path = pathlib.Path(path)
        self.expected_sha256 = expected_sha256
        self.timeout = timeout
        self._handshake = None

    @classmethod
    def from_env(cls, *, required=False, expected_sha256=None, timeout=DEFAULT_TIMEOUT_SECONDS):
        raw = os.environ.get("BLITTER_SECURITY_PLUGIN")
        if not raw:
            if required:
                raise SecurityPluginBlocked("BLITTER_SECURITY_PLUGIN is not configured")
            return None
        return cls(raw, expected_sha256=expected_sha256, timeout=timeout)

    def _validate_artifact(self):
        if not self.path.is_absolute():
            raise SecurityPluginBlocked("security plugin path must be absolute")
        if not self.path.is_file():
            raise SecurityPluginBlocked(f"security plugin does not exist: {self.path}")
        if not os.access(self.path, os.X_OK):
            raise SecurityPluginBlocked(f"security plugin is not executable: {self.path}")
        if self.expected_sha256 is not None:
            expected = self.expected_sha256.lower()
            if expected.startswith("sha256:"):
                expected = expected[7:]
            if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
                raise SecurityPluginBlocked("expected plugin sha256 is malformed")
            h = hashlib.sha256()
            with self.path.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != expected:
                raise SecurityPluginBlocked("security plugin artifact sha256 mismatch")

    def handshake(self):
        if self._handshake is not None:
            return dict(self._handshake)
        self._validate_artifact()
        try:
            proc = subprocess.run(
                [str(self.path), "handshake"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SecurityPluginBlocked(f"security plugin handshake failed: {exc}") from exc
        if proc.returncode != 0:
            raise SecurityPluginBlocked(
                f"security plugin handshake exited {proc.returncode}: "
                f"{proc.stderr.decode('utf-8', 'replace')[:300]}"
            )
        try:
            text = proc.stdout.decode("utf-8")
            decoder = json.JSONDecoder()
            data, end = decoder.raw_decode(text)
            if text[end:].strip():
                raise ValueError("trailing output")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise SecurityPluginBlocked(f"security plugin handshake is not one JSON object: {exc}") from exc
        if not isinstance(data, dict):
            raise SecurityPluginBlocked("security plugin handshake must be an object")
        if data.get("schema") != HANDSHAKE_SCHEMA:
            raise SecurityPluginBlocked("security plugin handshake schema mismatch")
        if data.get("abi") != ABI:
            raise SecurityPluginBlocked("security plugin ABI mismatch")
        capabilities = data.get("capabilities")
        if (
            not isinstance(capabilities, list)
            or not capabilities
            or any(not isinstance(cap, str) or not cap for cap in capabilities)
            or len(set(capabilities)) != len(capabilities)
        ):
            raise SecurityPluginBlocked("security plugin capability list is malformed")
        build = data.get("build")
        if not isinstance(build, dict) or not isinstance(build.get("cgo"), bool) or not isinstance(build.get("static"), bool):
            raise SecurityPluginBlocked("security plugin build descriptor is malformed")
        if build["static"] == build["cgo"]:
            raise SecurityPluginBlocked("security plugin static/cgo descriptor is inconsistent")
        self._handshake = data
        return dict(data)

    def require(self, capability):
        hs = self.handshake()
        if capability not in hs["capabilities"]:
            raise SecurityPluginBlocked(f"security plugin lacks required capability: {capability}")
        return hs

    def stamp(self, payload, *, key_path):
        self.require(CAP_STAMP)
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("payload must be bytes-like")
        key_path = pathlib.Path(key_path)
        if not key_path.is_absolute():
            raise SecurityPluginBlocked("stamp key path must be absolute")
        try:
            proc = subprocess.run(
                [str(self.path), "stamp", "--key", str(key_path)],
                input=bytes(payload),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SecurityPluginBlocked(f"security stamp invocation failed: {exc}") from exc
        if proc.returncode != 0:
            raise SecurityPluginBlocked(
                f"security stamp exited {proc.returncode}: {proc.stderr.decode('utf-8', 'replace')[:300]}"
            )
        try:
            result = json.loads(proc.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecurityPluginBlocked("security stamp returned malformed JSON") from exc
        if result.get("schema") != "mathpunch.secure-stamp.v1":
            raise SecurityPluginBlocked("security stamp schema mismatch")
        return result

    def verify(self, payload, *, stamp_path, public_key_path=None):
        self.require(CAP_VERIFY)
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("payload must be bytes-like")
        stamp_path = pathlib.Path(stamp_path)
        if not stamp_path.is_absolute():
            raise SecurityPluginBlocked("stamp path must be absolute")
        argv = [str(self.path), "verify", "--stamp", str(stamp_path)]
        if public_key_path is not None:
            public_key_path = pathlib.Path(public_key_path)
            if not public_key_path.is_absolute():
                raise SecurityPluginBlocked("public key path must be absolute")
            argv.extend(["--public-key", str(public_key_path)])
        try:
            proc = subprocess.run(
                argv,
                input=bytes(payload),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SecurityPluginBlocked(f"security verify invocation failed: {exc}") from exc
        try:
            result = json.loads(proc.stdout) if proc.stdout else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecurityPluginBlocked("security verify returned malformed JSON") from exc
        if proc.returncode != 0 or result.get("valid") is not True:
            raise SecurityPluginBlocked(
                f"security stamp verification failed: {proc.stderr.decode('utf-8', 'replace')[:300]}"
            )
        return result

    def tls_serve_argv(self, *, listen, upstream, cert_path, key_path, max_body_bytes=None):
        """Return validated argv for a supervisor to exec as the long-lived TLS plugin."""
        self.require(CAP_TLS)
        cert_path = pathlib.Path(cert_path)
        key_path = pathlib.Path(key_path)
        if not cert_path.is_absolute():
            raise SecurityPluginBlocked("TLS certificate path must be absolute")
        if not key_path.is_absolute():
            raise SecurityPluginBlocked("TLS key path must be absolute")
        argv = [
            str(self.path),
            "serve",
            "--listen", str(listen),
            "--upstream", str(upstream),
            "--cert", str(cert_path),
            "--key", str(key_path),
        ]
        if max_body_bytes is not None:
            if not isinstance(max_body_bytes, int) or isinstance(max_body_bytes, bool) or max_body_bytes <= 0:
                raise ValueError("max_body_bytes must be a positive integer")
            argv.extend(["--max-body-bytes", str(max_body_bytes)])
        return argv
