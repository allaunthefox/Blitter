#!/usr/bin/env python3
"""Strict HTTP client for the target-side LSP session lease contract."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class LSPLeaseClient:
    base_url: str
    goal_id: str
    work_id: str
    worker_id: str
    worker_generation: int
    descriptor_digest: str
    image_digest: str
    holder: str
    ttl_ms: int
    auth_token: str = ""

    def __post_init__(self) -> None:
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("lease URL must use HTTP or HTTPS")
        if self.ttl_ms < 1 or self.worker_generation < 0:
            raise ValueError("lease TTL and worker generation are invalid")
        if not self.auth_token and not self.base_url.startswith("http://127.0.0.1") and not self.base_url.startswith("http://localhost"):
            # Production HTTP URLs require a bearer token.
            raise ValueError(
                "LSP lease client requires auth_token for non-loopback URLs; "
                "set BLITTER_LSP_LEASE_TOKEN or pass auth_token explicitly"
            )

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        # Add auth token if configured.  The server requires this header on
        # all authority-bearing endpoints; without it, the server rejects the
        # request with 401.  The token is only sent over loopback or behind
        # the deployment's authenticated/TLS control plane.
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        request = urllib.request.Request(
            self.base_url.rstrip("/") + path,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"LSP lease request failed: {exc}") from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("LSP lease endpoint returned an invalid or unsuccessful response")
        return result

    def acquire(self) -> dict[str, Any]:
        result = self._post("/lsp/lease/acquire", {
            "goal_id": self.goal_id,
            "work_id": self.work_id,
            "worker_id": self.worker_id,
            "worker_generation": self.worker_generation,
            "descriptor_digest": self.descriptor_digest,
            "image_digest": self.image_digest,
            "holder": self.holder,
            "ttl_ms": self.ttl_ms,
        })
        for field in ("session_id", "instance_id", "lease_token"):
            if not isinstance(result.get(field), str) or not result[field]:
                raise RuntimeError(f"LSP lease acquire missing {field}")
        for field in ("lease_epoch", "expires_unix_ms"):
            if type(result.get(field)) is not int or result[field] < 1:
                raise RuntimeError(f"LSP lease acquire has invalid {field}")
        return result

    def renew(self, lease: dict[str, Any]) -> dict[str, Any]:
        result = self._post("/lsp/lease/renew", {
            "session_id": lease["session_id"],
            "instance_id": lease["instance_id"],
            "lease_epoch": lease["lease_epoch"],
            "lease_token": lease["lease_token"],
            "ttl_ms": self.ttl_ms,
        })
        if result.get("session_id") != lease["session_id"]:
            raise RuntimeError("LSP lease renewal changed session identity")
        lease.update(result)
        return result

    def release(self, lease: dict[str, Any]) -> None:
        result = self._post("/lsp/lease/release", {
            "session_id": lease["session_id"],
            "instance_id": lease["instance_id"],
            "lease_epoch": lease["lease_epoch"],
            "lease_token": lease["lease_token"],
        })
        if result.get("released") is not True:
            raise RuntimeError("LSP lease release was not confirmed")

    def session_begin(self, lease: dict[str, Any]) -> dict[str, Any]:
        """Mark the fenced session as in flight.

        This must be called after acquiring a lease and before starting
        any computation. Only one fenced session may be in flight at a time.
        Raises RuntimeError if another session is already in flight.
        """
        result = self._post("/lsp/session/begin", {
            "session_id": lease["session_id"],
            "instance_id": lease["instance_id"],
            "lease_epoch": lease["lease_epoch"],
            "lease_token": lease["lease_token"],
        })
        for field in ("session_id", "lease_epoch"):
            if field not in result:
                raise RuntimeError(f"LSP session begin missing {field}")
        if result.get("in_flight") is not True:
            raise RuntimeError("LSP session begin did not confirm in-flight state")
        lease.update(result)
        return result

    def session_commit(self, lease: dict[str, Any]) -> dict[str, Any]:
        """Re-validate the lease before committing diagnostics.

        This must be called after completing computation and before
        committing any diagnostics or results. Returns the commit result
        indicating whether diagnostics may be committed.

        Raises RuntimeError if the lease is revoked, expired, or a stale
        worker holds the session. A RuntimeError here means diagnostics
        must NOT be committed.
        """
        result = self._post("/lsp/session/commit", {
            "session_id": lease["session_id"],
            "instance_id": lease["instance_id"],
            "lease_epoch": lease["lease_epoch"],
            "lease_token": lease["lease_token"],
        })
        # A 409 response is raised as RuntimeError by _post.
        # On success, committed=True means diagnostics may be committed.
        if result.get("ok") is not True:
            raise RuntimeError(
                f"LSP session commit rejected: {result.get('error', 'unknown')}"
            )
        if result.get("committed") is not True:
            raise RuntimeError("LSP session commit returned false: lease revoked or expired")
        return result
