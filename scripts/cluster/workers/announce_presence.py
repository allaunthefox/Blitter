#!/usr/bin/env python3
"""Presence announcer for MathPunch LSP workers.

Emits one canonical ``mathpunch.blitter-lsp-presence.v1`` announcement bound
to this worker's identity:

* ``node_id``: the Blitter node name;
* ``boot_id``: boot-scoped identity (default: kernel random boot id);
* ``worker_generation``: monotonic deployment generation loaded from the
  generation file written by ``build_workers.sh``.  Schedulers prefer the
  highest generation; the lease service records the generation that acquired
  each lease (see ``lsp_lease_server`` status);
* ``image_digest``: the digest-pinned worker image actually announced;
* ``capabilities``: must include ``lsp`` and ``language:<lang>``.

The announcement is admission evidence only: it never grants a lease and says
nothing about registry trust (verified separately by ``image_trust``).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

SCHEMA = "mathpunch.blitter-lsp-presence.v1"


def read_boot_id(explicit: str | None) -> str:
    if explicit:
        if not explicit or "\x00" in explicit or any(ord(c) < 0x20 for c in explicit):
            raise ValueError("boot_id must be a non-empty string without control characters")
        return explicit
    for candidate in ("/proc/sys/kernel/random/boot_id", "/etc/machine-id"):
        try:
            value = pathlib.Path(candidate).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    raise ValueError("cannot determine boot_id: pass --boot-id explicitly")


def read_generation(path: pathlib.Path) -> int:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"cannot read generation file {path}: {exc}") from exc
    if not raw.isascii() or not raw.isdigit():
        raise ValueError(f"generation file {path} must contain a decimal integer")
    generation = int(raw, 10)
    if generation < 0:
        raise ValueError("worker generation must be non-negative")
    return generation


def build_announcement(
    *,
    node_id: str,
    boot_id: str,
    generation: int,
    language: str,
    image_digest: str,
    extra_capabilities: tuple[str, ...],
    ttl_ms: int,
    now_unix_ms: int,
) -> dict:
    if not node_id or "\x00" in node_id:
        raise ValueError("node_id must be a non-empty string")
    if language not in ("lean", "coq", "python", "r", "julia", "rust"):
        raise ValueError(f"unsupported language: {language}")
    if not image_digest or "\x00" in image_digest:
        raise ValueError("image_digest must be a non-empty string")
    if type(ttl_ms) is not int or ttl_ms < 1:
        raise ValueError("ttl_ms must be a positive integer")
    if type(now_unix_ms) is not int or now_unix_ms < 0:
        raise ValueError("now_unix_ms must be a non-negative integer")
    capabilities = ["lsp", f"language:{language}", *extra_capabilities]
    if len(set(capabilities)) != len(capabilities):
        raise ValueError("capabilities must not contain duplicates")
    if any(not cap or "\x00" in cap for cap in capabilities):
        raise ValueError("capabilities must contain non-empty strings")
    return {
        "schema": SCHEMA,
        "node_id": node_id,
        "boot_id": boot_id,
        "worker_generation": generation,
        "language": language,
        "image_digest": image_digest,
        "sent_unix_ms": now_unix_ms,
        "availability_ttl_ms": ttl_ms,
        "availability_expires_unix_ms": now_unix_ms + ttl_ms,
        "capabilities": capabilities,
        "available": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", required=True)
    parser.add_argument("--boot-id", default=None)
    parser.add_argument("--generation-file", type=pathlib.Path, required=True)
    parser.add_argument("--language", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--capability", action="append", default=[])
    parser.add_argument("--ttl-ms", type=int, default=30_000)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        announcement = build_announcement(
            node_id=args.node,
            boot_id=read_boot_id(args.boot_id),
            generation=read_generation(args.generation_file),
            language=args.language,
            image_digest=args.image_digest,
            extra_capabilities=tuple(args.capability),
            ttl_ms=args.ttl_ms,
            now_unix_ms=int(time.time() * 1000),
        )
    except ValueError as exc:
        print(f"announce_presence: {exc}", file=sys.stderr)
        return 2
    args.out.write_text(
        json.dumps(announcement, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"announced {args.language} generation {announcement['worker_generation']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
