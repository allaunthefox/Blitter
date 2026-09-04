#!/usr/bin/env bash
# Build and publish the six LSP worker images with provenance/SBOM attestations.
#
# Usage: ./scripts/cluster/workers/build_workers.sh <harbor-project>
#
# Each image is built from scripts/cluster/workers/Containerfile.<lang> with
# buildx attestations (--provenance, --sbom), pushed by immutable digest, and
# recorded together with a bumped worker generation. After publication the
# operator copies the published digests into the deployment-owned image policy
# and signs a catalog with sign_lsp_catalog.py.
#
# --require-published refuses BOOTSTRAP placeholder bases: all six
# Containerfiles must pin reviewed production base digests first.
set -euo pipefail

WORKERS_DIR="$(cd "$(dirname "$0")" && pwd)"
LANGUAGES="lean coq python r julia rust"

REQUIRE_PUBLISHED=0
if [ "${1:-}" = "--require-published" ]; then
  REQUIRE_PUBLISHED=1
  shift
fi
[ "$#" -eq 1 ] || { echo "usage: $0 [--require-published] <harbor-project>" >&2; exit 2; }
PROJECT="$1"

# Production publication requires each Containerfile to carry a
# PRODUCTION-BASE-REVIEWED marker naming the base-digest review. Bootstrap
# files deliberately lack it, so --require-published fails closed until the
# reviewed production bases land.
if [ "$REQUIRE_PUBLISHED" = 1 ]; then
  for lang in $LANGUAGES; do
    if ! grep -Eq "^# PRODUCTION-BASE-REVIEWED: " "$WORKERS_DIR/Containerfile.$lang"; then
      echo "Containerfile.$lang has no PRODUCTION-BASE-REVIEWED marker; refusing to publish" >&2
      exit 3
    fi
  done
fi

command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 2; }
docker buildx version >/dev/null 2>&1 || { echo "docker buildx is required" >&2; exit 2; }

GEN_FILE="$WORKERS_DIR/generation"
GEN=0
if [ -f "$GEN_FILE" ]; then
  GEN="$(cat "$GEN_FILE")"
  case "$GEN" in ''|*[!0-9]*) echo "generation file is not a decimal integer" >&2; exit 3;; esac
fi
GEN=$((GEN + 1))

DIGEST_LOCK="$WORKERS_DIR/published-digests.json"
printf '{\n' > "$DIGEST_LOCK.tmp"
FIRST=1
for lang in $LANGUAGES; do
  TAG="$PROJECT/mathpunch-$lang-lsp:gen$GEN"
  echo "building $TAG from Containerfile.$lang"
  docker buildx build \
    --provenance=true --sbom=true \
    --platform linux/amd64 \
    -f "$WORKERS_DIR/Containerfile.$lang" \
    -t "$TAG" --push \
    "$WORKERS_DIR" >/dev/null
  DIGEST="$(docker buildx imagetools inspect "$TAG" --raw | sha256sum | awk '{print $1}')"
  REF="$PROJECT/mathpunch-$lang-lsp@sha256:$DIGEST"
  echo "  published $REF"
  if [ "$FIRST" = 1 ]; then FIRST=0; else printf ',\n' >> "$DIGEST_LOCK.tmp"; fi
  printf '  %s: %s' "$(printf '%s' "$lang" | sed 's/.*/"&"/')" "$(printf '%s' "$REF" | sed 's/.*/"&"/')" >> "$DIGEST_LOCK.tmp"
done
printf '\n}\n' >> "$DIGEST_LOCK.tmp"
mv "$DIGEST_LOCK.tmp" "$DIGEST_LOCK"
echo "$GEN" > "$GEN_FILE"
echo "worker generation $GEN published; digests in $DIGEST_LOCK"
echo "next: copy digests into the deployment-owned image policy and sign a catalog."
