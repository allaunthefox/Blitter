#!/usr/bin/env python3
"""Authority gate for the current BLITTER claim registry V2.

V1 remains historical; V2 is the current branch claim surface. Reuse the same
closed status vocabulary, receipt schema, and trust-boundary validator so the
supersession changes claim content without weakening promotion mechanics.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from verify_blitter_claim_gate import GateError, validate_registry


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("docs/specs/BLITTER_CLAIM_REGISTRY_V2.md"),
    )
    parser.add_argument(
        "--receipt-dir",
        type=Path,
        default=Path("project/results/blitter_claims"),
    )
    args = parser.parse_args()
    try:
        rows = validate_registry(args.registry, args.receipt_dir)
    except (OSError, GateError) as exc:
        print(f"BLITTER_CLAIM_GATE_V2: FAIL: {exc}")
        return 1
    proved = sorted(k for k, v in rows.items() if v == "PROVED_LOCALLY")
    print(f"BLITTER_CLAIM_GATE_V2: PASS: rows={len(rows)} proved={proved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
