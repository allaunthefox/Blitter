#!/usr/bin/env python3
"""Offline catalog signing and verification for LSP image trust.

Signing is a deployment-time, offline operation: the release key never leaves
the signer's machine.  Verification (``verify_image_trust``) needs only the
deployment-owned policy's public keys.

Usage:
  # Sign a catalog file in place (seed from a file; never commit the seed):
  python3 scripts/cluster/sign_lsp_catalog.py sign \
    --catalog scripts/cluster/lsp_image_catalog.example.json \
    --seed-file /run/secrets/lsp-catalog-seed \
    --key-id blitter-lsp-release-1

  # Verify a catalog against a deployment-owned policy:
  python3 scripts/cluster/sign_lsp_catalog.py verify \
    --catalog scripts/cluster/lsp_image_catalog.example.json \
    --policy scripts/cluster/lsp_image_policy.example.json

The seed file must contain exactly 32 raw bytes.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

try:
    from . import image_trust
except ImportError:
    import image_trust


def cmd_sign(args: argparse.Namespace) -> int:
    catalog_path = pathlib.Path(args.catalog)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    seed = pathlib.Path(args.seed_file).read_bytes()
    if len(seed) != 32:
        print("sign_lsp_catalog: seed file must contain exactly 32 bytes", file=sys.stderr)
        return 2
    metadata = catalog.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("signature"), dict):
        print("sign_lsp_catalog: catalog has no metadata.signature block", file=sys.stderr)
        return 2
    signature = metadata["signature"]
    if signature.get("algorithm") != image_trust.SIGNATURE_ALGORITHM:
        print(
            f"sign_lsp_catalog: signature.algorithm must be {image_trust.SIGNATURE_ALGORITHM}",
            file=sys.stderr,
        )
        return 2
    signature["key_id"] = args.key_id
    signature["value"] = ""
    raw = image_trust.ed25519_sign(seed, image_trust.signing_bytes(catalog)).hex()
    signature["value"] = raw
    catalog_path.write_bytes(
        (json.dumps(catalog, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    )
    derived = image_trust.ed25519_publickey_from_seed(seed).hex()
    print(f"signed {catalog_path} with key_id={args.key_id} public={derived}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    catalog = json.loads(pathlib.Path(args.catalog).read_text(encoding="utf-8"))
    policy = json.loads(pathlib.Path(args.policy).read_text(encoding="utf-8"))
    if args.require_published:
        status = policy.get("status")
        if status != "PUBLISHED_HARBOR":
            print(
                f"sign_lsp_catalog: policy status is {status!r}, not PUBLISHED_HARBOR",
                file=sys.stderr,
            )
            return 1
        for language, image in policy.get("approved_images", {}).items():
            if "example.invalid" in image:
                print(
                    f"sign_lsp_catalog: {language} image is still an example reference",
                    file=sys.stderr,
                )
                return 1
    try:
        result = image_trust.verify_image_trust(catalog, policy)
    except (image_trust.TrustError, ValueError) as exc:
        print(f"sign_lsp_catalog: FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "signature_key_id": result["signature_key_id"],
                "provenance": result["provenance"],
                "sbom_languages": sorted(result["sbom_digests"]),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    sign = sub.add_parser("sign")
    sign.add_argument("--catalog", required=True)
    sign.add_argument("--seed-file", required=True)
    sign.add_argument("--key-id", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--catalog", required=True)
    verify.add_argument("--policy", required=True)
    verify.add_argument("--require-published", action="store_true")
    args = parser.parse_args()
    if args.action == "sign":
        return cmd_sign(args)
    return cmd_verify(args)


if __name__ == "__main__":
    raise SystemExit(main())
