#!/usr/bin/env python3
"""Canonical codec/validator for blitter image provenance V1.

This is deployment provenance, not ISA semantics. The record is deliberately
small and byte-canonical so the drop-in security plugin can stamp the exact bytes
that deployment later validates.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re

SCHEMA = "mathpunch.blitter-image-provenance.v1"
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")


class ProvenanceError(ValueError):
    pass


def _require_sha256(value, field):
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ProvenanceError(f"{field} must be canonical sha256:<64 lowercase hex>")
    return value


def _require_commit(value):
    if not isinstance(value, str) or COMMIT_RE.fullmatch(value) is None:
        raise ProvenanceError("source_commit must be 40 lowercase hex characters")
    return value


def canonical_bytes(record):
    if not isinstance(record, dict):
        raise ProvenanceError("provenance root must be an object")
    return (
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("ascii")


def build_record(
    *,
    published_tag,
    immutable_ref,
    registry_digest,
    source_commit,
    semantic_profile_digest,
    blitter_daemon_sha256,
    dockerfile_sha256,
    stamp_requested=False,
    stamp_plugin_sha256=None,
):
    registry_digest = _require_sha256(registry_digest, "registry_digest")
    semantic_profile_digest = _require_sha256(
        semantic_profile_digest, "semantic_profile_digest"
    )
    blitter_daemon_sha256 = _require_sha256(
        blitter_daemon_sha256, "blitter_daemon_sha256"
    )
    dockerfile_sha256 = _require_sha256(dockerfile_sha256, "dockerfile_sha256")
    source_commit = _require_commit(source_commit)
    if not isinstance(published_tag, str) or not published_tag:
        raise ProvenanceError("published_tag must be non-empty")
    if not isinstance(immutable_ref, str) or not immutable_ref.endswith("@" + registry_digest):
        raise ProvenanceError("immutable_ref must end with @registry_digest")
    if type(stamp_requested) is not bool:
        raise ProvenanceError("stamp_requested must be boolean")
    if stamp_requested:
        stamp_plugin_sha256 = _require_sha256(
            stamp_plugin_sha256, "stamp_plugin_sha256"
        )
    elif stamp_plugin_sha256 is not None:
        raise ProvenanceError("unstamped provenance must not claim a stamp plugin digest")

    return {
        "schema": SCHEMA,
        "source_commit": source_commit,
        "semantic_profile_digest": semantic_profile_digest,
        "image": {
            "published_tag": published_tag,
            "immutable_ref": immutable_ref,
            "registry_digest": registry_digest,
        },
        "build_inputs": {
            "blitter_daemon_sha256": blitter_daemon_sha256,
            "dockerfile_sha256": dockerfile_sha256,
        },
        "secure_stamp": {
            "requested": stamp_requested,
            "plugin_sha256": stamp_plugin_sha256,
        },
    }


def validate_record(record, *, expected_profile_digest=None):
    if not isinstance(record, dict) or set(record) != {
        "schema",
        "source_commit",
        "semantic_profile_digest",
        "image",
        "build_inputs",
        "secure_stamp",
    }:
        raise ProvenanceError("provenance top-level fields are noncanonical")
    if record["schema"] != SCHEMA:
        raise ProvenanceError("wrong provenance schema")

    _require_commit(record["source_commit"])
    profile = _require_sha256(
        record["semantic_profile_digest"], "semantic_profile_digest"
    )
    if expected_profile_digest is not None and profile != expected_profile_digest:
        raise ProvenanceError("provenance semantic profile does not match expected lock")

    image = record["image"]
    if not isinstance(image, dict) or set(image) != {
        "published_tag",
        "immutable_ref",
        "registry_digest",
    }:
        raise ProvenanceError("image provenance fields are noncanonical")
    digest = _require_sha256(image["registry_digest"], "registry_digest")
    if not isinstance(image["published_tag"], str) or not image["published_tag"]:
        raise ProvenanceError("published_tag must be non-empty")
    if not isinstance(image["immutable_ref"], str) or not image["immutable_ref"].endswith(
        "@" + digest
    ):
        raise ProvenanceError("immutable_ref does not bind registry_digest")

    build = record["build_inputs"]
    if not isinstance(build, dict) or set(build) != {
        "blitter_daemon_sha256",
        "dockerfile_sha256",
    }:
        raise ProvenanceError("build_inputs fields are noncanonical")
    _require_sha256(build["blitter_daemon_sha256"], "blitter_daemon_sha256")
    _require_sha256(build["dockerfile_sha256"], "dockerfile_sha256")

    stamp = record["secure_stamp"]
    if not isinstance(stamp, dict) or set(stamp) != {"requested", "plugin_sha256"}:
        raise ProvenanceError("secure_stamp fields are noncanonical")
    if type(stamp["requested"]) is not bool:
        raise ProvenanceError("secure_stamp.requested must be boolean")
    if stamp["requested"]:
        _require_sha256(stamp["plugin_sha256"], "secure_stamp.plugin_sha256")
    elif stamp["plugin_sha256"] is not None:
        raise ProvenanceError("unstamped provenance must have plugin_sha256=null")

    return {
        "immutable_ref": image["immutable_ref"],
        "registry_digest": digest,
        "source_commit": record["source_commit"],
        "semantic_profile_digest": profile,
        "stamp_requested": stamp["requested"],
        "stamp_plugin_sha256": stamp["plugin_sha256"],
    }


def load_canonical(path, *, expected_profile_digest=None):
    path = pathlib.Path(path)
    raw = path.read_bytes()
    try:
        text = raw.decode("ascii")
        record = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"provenance is not canonical ASCII JSON: {exc}") from exc
    info = validate_record(record, expected_profile_digest=expected_profile_digest)
    if raw != canonical_bytes(record):
        raise ProvenanceError("provenance bytes are not canonical re-encoding")
    return record, info


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)

    build = sub.add_parser("build")
    build.add_argument("--out", type=pathlib.Path, required=True)
    build.add_argument("--published-tag", required=True)
    build.add_argument("--immutable-ref", required=True)
    build.add_argument("--registry-digest", required=True)
    build.add_argument("--source-commit", required=True)
    build.add_argument("--semantic-profile-digest", required=True)
    build.add_argument("--blitter-daemon-sha256", required=True)
    build.add_argument("--dockerfile-sha256", required=True)
    build.add_argument("--stamp-requested", action="store_true")
    build.add_argument("--stamp-plugin-sha256")

    validate = sub.add_parser("validate")
    validate.add_argument("--provenance", type=pathlib.Path, required=True)
    validate.add_argument("--profile-lock", type=pathlib.Path, required=True)
    validate.add_argument("--json", action="store_true")

    args = parser.parse_args()
    try:
        if args.action == "build":
            record = build_record(
                published_tag=args.published_tag,
                immutable_ref=args.immutable_ref,
                registry_digest=args.registry_digest,
                source_commit=args.source_commit,
                semantic_profile_digest=args.semantic_profile_digest,
                blitter_daemon_sha256=args.blitter_daemon_sha256,
                dockerfile_sha256=args.dockerfile_sha256,
                stamp_requested=args.stamp_requested,
                stamp_plugin_sha256=args.stamp_plugin_sha256,
            )
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_bytes(canonical_bytes(record))
            return 0

        expected = args.profile_lock.read_text(encoding="ascii").strip()
        _require_sha256(expected, "profile lock")
        _, info = load_canonical(
            args.provenance, expected_profile_digest=expected
        )
        if args.json:
            print(json.dumps(info, sort_keys=True, separators=(",", ":")))
        else:
            for key in (
                "immutable_ref",
                "registry_digest",
                "source_commit",
                "semantic_profile_digest",
            ):
                print(info[key])
        return 0
    except (OSError, ProvenanceError) as exc:
        print(f"BLITTER_IMAGE_PROVENANCE: FAIL: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
