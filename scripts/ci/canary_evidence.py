#!/usr/bin/env python3
"""Canonical, non-authorizing canary evidence receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
from collections.abc import Mapping
from typing import Any

SCHEMA = "blitter.canary-evidence.v1"
VERIFIER_RESPONSE_SCHEMA = "blitter.canary-external-verifier-response.v1"
VERIFICATION_SCHEMA = "blitter.canary-external-verification.v1"
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
IMAGE_RE = re.compile(
    r"[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}\Z"
)
SENSITIVE_KEY_RE = re.compile(
    r"(?:authorization|cookie|credential|password|private[_-]?key|secret|token)",
    re.IGNORECASE,
)
AUTH_VALUE_RE = re.compile(r"\b(?:basic|bearer)\s+\S+", re.IGNORECASE)
URL_USERINFO_RE = re.compile(r"(https?://)[^/@\s]+@", re.IGNORECASE)


class EvidenceError(ValueError):
    """Raised when evidence cannot be treated as canonical and fail closed."""


def canonical_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise EvidenceError(f"value is not canonical JSON: {exc}") from exc
    return (text + "\n").encode("ascii")


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def redact(value: Any) -> Any:
    """Return JSON-shaped data with common credential forms removed."""
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if SENSITIVE_KEY_RE.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = AUTH_VALUE_RE.sub("[REDACTED]", value)
        return URL_USERINFO_RE.sub(r"\1[REDACTED]@", value)
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise EvidenceError(f"{field} must be sha256:<64 lowercase hex>")
    return value


def _commit(value: Any) -> str:
    if not isinstance(value, str) or COMMIT_RE.fullmatch(value) is None:
        raise EvidenceError("source_commit must be 40 lowercase hex characters")
    return value


def _image(value: Any) -> str:
    if not isinstance(value, str) or IMAGE_RE.fullmatch(value) is None:
        raise EvidenceError("candidate_image must be a tag-free OCI reference pinned by sha256")
    return value


def _label(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or any(ord(ch) < 0x20 for ch in value):
        raise EvidenceError(f"{field} must be a non-empty string without control characters")
    if SENSITIVE_KEY_RE.search(field) or AUTH_VALUE_RE.search(value) or URL_USERINFO_RE.search(value):
        raise EvidenceError(f"{field} contains credential-shaped data")
    return value


def blocked_local_receipt(*, source_commit: str, candidate_image: str) -> dict[str, Any]:
    """Build the only valid local/no-adapter receipt."""
    receipt = {
        "schema": SCHEMA,
        "status": "blocked-local-no-adapter",
        "source_commit": _commit(source_commit),
        "candidate_image": _image(candidate_image),
        "live": False,
        "adapter": None,
        "evidence": {"scalar": None, "candidate": None},
        "authority": "diagnostic-only",
    }
    validate_receipt(receipt)
    return receipt


def validate_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(receipt, Mapping) or set(receipt) != {
        "schema", "status", "source_commit", "candidate_image", "live", "adapter",
        "evidence", "authority",
    }:
        raise EvidenceError("receipt fields are noncanonical")
    if receipt["schema"] != SCHEMA:
        raise EvidenceError("wrong receipt schema")
    if receipt["authority"] != "diagnostic-only":
        raise EvidenceError("canary evidence has diagnostic-only authority")
    _commit(receipt["source_commit"])
    _image(receipt["candidate_image"])
    if type(receipt["live"]) is not bool:
        raise EvidenceError("live must be boolean")

    evidence = receipt["evidence"]
    if not isinstance(evidence, Mapping) or set(evidence) != {"scalar", "candidate"}:
        raise EvidenceError("evidence fields are noncanonical")

    if receipt["status"] == "blocked-local-no-adapter":
        if receipt["live"] or receipt["adapter"] is not None:
            raise EvidenceError("blocked local receipt cannot claim a live adapter")
        if evidence["scalar"] is not None or evidence["candidate"] is not None:
            raise EvidenceError("blocked local receipt cannot claim execution evidence")
        return dict(receipt)

    if receipt["status"] != "captured-live" or not receipt["live"]:
        raise EvidenceError("unknown or inconsistent receipt status")
    adapter = receipt["adapter"]
    if not isinstance(adapter, Mapping) or set(adapter) != {"name", "backend"}:
        raise EvidenceError("live receipt requires canonical adapter identity")
    _label(adapter["name"], "adapter.name")
    _label(adapter["backend"], "adapter.backend")

    for kind in ("scalar", "candidate"):
        item = evidence[kind]
        if not isinstance(item, Mapping) or set(item) != {"request_sha256", "response_sha256"}:
            raise EvidenceError(f"{kind} evidence fields are noncanonical")
        _digest(item["request_sha256"], f"evidence.{kind}.request_sha256")
        _digest(item["response_sha256"], f"evidence.{kind}.response_sha256")
    if evidence["scalar"]["request_sha256"] != evidence["candidate"]["request_sha256"]:
        raise EvidenceError("scalar and candidate evidence must bind the same request")
    return dict(receipt)


def load_canonical(path: pathlib.Path) -> dict[str, Any]:
    try:
        raw = pathlib.Path(path).read_bytes()
        receipt = json.loads(raw.decode("ascii"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot load canonical receipt: {exc}") from exc
    validate_receipt(receipt)
    if raw != canonical_bytes(receipt):
        raise EvidenceError("receipt bytes differ from canonical encoding")
    return receipt


def invoke_external_verifier(
    receipt: Mapping[str, Any],
    *,
    verifier: pathlib.Path,
    verifier_sha256: str,
    timeout: float = 30,
) -> dict[str, Any]:
    """Invoke only an explicitly selected, digest-pinned external verifier."""
    validate_receipt(receipt)
    expected_digest = _digest(verifier_sha256, "verifier_sha256")
    verifier = pathlib.Path(verifier)
    try:
        metadata = verifier.lstat()
        if not stat.S_ISREG(metadata.st_mode) or verifier.is_symlink():
            raise EvidenceError("external verifier must be a regular non-symlink file")
        actual_digest = sha256_bytes(verifier.read_bytes())
    except OSError as exc:
        raise EvidenceError(f"cannot read external verifier: {exc}") from exc
    if actual_digest != expected_digest:
        raise EvidenceError("external verifier digest does not match required pin")

    receipt_bytes = canonical_bytes(receipt)
    try:
        completed = subprocess.run(
            [str(verifier.resolve())],
            input=receipt_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceError(f"external verifier did not complete: {exc}") from exc
    if completed.returncode != 0:
        raise EvidenceError("external verifier rejected the receipt")
    try:
        result = json.loads(completed.stdout.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"external verifier returned invalid JSON: {exc}") from exc
    if completed.stdout != canonical_bytes(result):
        raise EvidenceError("external verifier output is not canonical JSON")
    receipt_digest = sha256_bytes(receipt_bytes)
    if not isinstance(result, Mapping) or set(result) != {
        "schema", "receipt_sha256", "verdict", "authority",
    }:
        raise EvidenceError("external verifier result fields are noncanonical")
    if result != {
        "schema": VERIFIER_RESPONSE_SCHEMA,
        "receipt_sha256": receipt_digest,
        "verdict": "pass",
        "authority": "diagnostic-only",
    }:
        raise EvidenceError("external verifier did not return a bound diagnostic pass")
    return {
        "schema": VERIFICATION_SCHEMA,
        "receipt_sha256": receipt_digest,
        "verifier_sha256": actual_digest,
        "verdict": "pass",
        "authority": "diagnostic-only",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("validate")
    check.add_argument("receipt", type=pathlib.Path)
    verify = sub.add_parser("verify")
    verify.add_argument("receipt", type=pathlib.Path)
    verify.add_argument("--verifier", type=pathlib.Path, required=True)
    verify.add_argument("--verifier-sha256", required=True)
    args = parser.parse_args()
    try:
        receipt = load_canonical(args.receipt)
        if args.command == "verify":
            print(canonical_bytes(invoke_external_verifier(
                receipt, verifier=args.verifier, verifier_sha256=args.verifier_sha256
            )).decode("ascii"), end="")
        return 0
    except EvidenceError as exc:
        print(f"BLITTER_CANARY_EVIDENCE: FAIL: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
