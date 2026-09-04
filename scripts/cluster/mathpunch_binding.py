#!/usr/bin/env python3
"""MathPunch binding for Blitter LSP diagnostics and artifacts.

Blitter transports and runs language workers; MathPunch owns all semantic
interpretation.  This module is the boundary:

* every diagnostic or artifact captured from a worker is bound to the exact
  ``project``, ``commit``, ``toolchain``, ``verifier``, and ``request_digest``
  it was produced under.  The binding is canonical JSON with a scope key; a
  replay under a different scope never applies;
* ``kernel_replay`` re-executes the MathPunch verifier directly against the
  bound scope and is the sole authority for the verdict.  Worker diagnostics
  are recorded as advisory evidence only and can never promote, demote, or
  otherwise alter the replay verdict.

This mirrors the scope-aware scar pattern: a failed representation is
replayable evidence under its original scope, never a rejection of the
underlying dynamics, and never transferable to a changed scope.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

SCHEMA = "mathpunch.blitter-mathpunch-binding.v1"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class BindingError(ValueError):
    """Raised when a diagnostic/artifact binding is not acceptable."""


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise BindingError(f"value is not canonical JSON: {exc}") from exc


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("ascii")).hexdigest()


def _label(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise BindingError(f"{field} must be a non-empty string")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise BindingError(f"{field} contains control characters")
    return value


def _commit(value: Any) -> str:
    if not isinstance(value, str) or COMMIT_RE.fullmatch(value) is None:
        raise BindingError("commit must be 40 lowercase hex characters")
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise BindingError(f"{field} must be sha256:<64 lowercase hex>")
    return value


def scope_key(
    *,
    project: str,
    commit: str,
    toolchain: str,
    verifier: str,
    request_digest: str,
) -> str:
    """Digest identifying the exact scope a diagnostic was produced under."""
    return digest(
        {
            "project": _label(project, "project"),
            "commit": _commit(commit),
            "toolchain": _label(toolchain, "toolchain"),
            "verifier": _label(verifier, "verifier"),
            "request_digest": _digest(request_digest, "request_digest"),
        }
    )


def bind_record(
    *,
    kind: str,
    project: str,
    commit: str,
    toolchain: str,
    verifier: str,
    request_digest: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind a worker diagnostic or artifact to its full provenance scope."""
    if kind not in ("diagnostic", "artifact"):
        raise BindingError("kind must be 'diagnostic' or 'artifact'")
    if not isinstance(payload, Mapping):
        raise BindingError("payload must be a JSON object")
    key = scope_key(
        project=project,
        commit=commit,
        toolchain=toolchain,
        verifier=verifier,
        request_digest=request_digest,
    )
    record = {
        "schema": SCHEMA,
        "kind": kind,
        "scope_key": key,
        "scope": {
            "project": project,
            "commit": commit,
            "toolchain": toolchain,
            "verifier": verifier,
            "request_digest": request_digest,
        },
        "payload_digest": digest(dict(payload)),
        "payload": dict(payload),
        "authority": "kernel-replay-only",
    }
    # Round-trip through canonical JSON so non-canonical payloads fail here.
    canonical_json(record)
    return record


def verify_binding(record: Mapping[str, Any]) -> dict[str, Any]:
    """Re-validate a bound record; return its scope on success."""
    if not isinstance(record, Mapping) or set(record) != {
        "schema", "kind", "scope_key", "scope",
        "payload_digest", "payload", "authority",
    }:
        raise BindingError("bound record fields are noncanonical")
    if record.get("schema") != SCHEMA:
        raise BindingError("bound record has the wrong schema")
    if record.get("kind") not in ("diagnostic", "artifact"):
        raise BindingError("bound record has an unknown kind")
    if record.get("authority") != "kernel-replay-only":
        raise BindingError("bound record must declare kernel-replay-only authority")
    scope = record.get("scope")
    if not isinstance(scope, Mapping) or set(scope) != {
        "project", "commit", "toolchain", "verifier", "request_digest",
    }:
        raise BindingError("bound record scope fields are noncanonical")
    expected_key = scope_key(
        project=scope["project"],
        commit=scope["commit"],
        toolchain=scope["toolchain"],
        verifier=scope["verifier"],
        request_digest=scope["request_digest"],
    )
    if record.get("scope_key") != expected_key:
        raise BindingError("bound record scope_key does not match its scope")
    if record.get("payload_digest") != digest(dict(record.get("payload", {}))):
        raise BindingError("bound record payload_digest does not match its payload")
    return {"scope_key": expected_key, "kind": record["kind"], "scope": dict(scope)}


def kernel_replay(
    record: Mapping[str, Any],
    kernel_fn: Callable[[dict[str, Any]], bool],
    *,
    expected_scope_key: str | None = None,
) -> dict[str, Any]:
    """Run the authoritative MathPunch verifier and report the verdict.

    ``kernel_fn`` receives the verified scope plus the payload and returns the
    authoritative boolean verdict.  The worker payload's own claimed verdict
    (``payload["verdict"]``, when present) is compared for telemetry only:
    agreement or disagreement never changes the authoritative outcome.
    ``expected_scope_key``, when given, pins the scope the caller intended to
    replay; a mismatch fails closed as a stale scope.
    """
    info = verify_binding(record)
    if expected_scope_key is not None and info["scope_key"] != expected_scope_key:
        raise BindingError("stale scope: bound record does not match the replay scope")
    scope = dict(info["scope"])
    try:
        authoritative = kernel_fn({**scope, "payload": dict(record["payload"])})
    except BindingError:
        raise
    except Exception as exc:
        raise BindingError(f"kernel replay raised: {exc}") from exc
    if type(authoritative) is not bool:
        raise BindingError("kernel replay must return a boolean verdict")
    claimed = record["payload"].get("verdict") if isinstance(record["payload"], dict) else None
    return {
        "schema": SCHEMA,
        "authoritative_verdict": authoritative,
        "scope_key": info["scope_key"],
        "worker_claimed_verdict": claimed if isinstance(claimed, bool) else None,
        "diagnostic_agrees": (claimed == authoritative) if isinstance(claimed, bool) else None,
        "authority": "kernel-replay-only",
    }
