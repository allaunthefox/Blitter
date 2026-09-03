#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET=${1:-linux/amd64}
OUT=${2:-$ROOT/dist/blitter-security-plugin-${TARGET%/*}-${TARGET#*/}}

case "$TARGET" in
  */*) ;;
  *) echo "target must be GOOS/GOARCH, got: $TARGET" >&2; exit 2 ;;
esac

GOOS=${TARGET%/*}
GOARCH=${TARGET#*/}
mkdir -p "$(dirname -- "$OUT")"

(
  cd "$ROOT"
  CGO_ENABLED=0 GOOS="$GOOS" GOARCH="$GOARCH" \
    go build -trimpath -buildvcs=true -ldflags='-s -w' -o "$OUT" .
)

if command -v sha256sum >/dev/null 2>&1; then
  DIGEST=$(sha256sum "$OUT" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
  DIGEST=$(shasum -a 256 "$OUT" | awk '{print $1}')
else
  echo "no sha256sum/shasum available to bind the artifact" >&2
  exit 3
fi

HANDSHAKE=$($OUT handshake)
printf '%s\n' "$HANDSHAKE" > "$OUT.handshake.json"
cat > "$OUT.manifest.json" <<EOF
{
  "schema": "mathpunch.blitter-security-plugin-manifest.v1",
  "abi": "mathpunch.blitter-security-plugin.v1",
  "plugin_id": "mathpunch-go-security-plugin",
  "artifact": {
    "target": "$TARGET",
    "sha256": "$DIGEST"
  },
  "handshake_file": "$(basename -- "$OUT").handshake.json"
}
EOF

printf 'built %s\nsha256:%s\n' "$OUT" "$DIGEST"
