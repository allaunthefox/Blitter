#!/usr/bin/env bash
# Build tracked blitter-daemon source, publish it to Harbor, and bind the
# publication to an immutable registry digest. Optional secure stamping uses the
# drop-in BLITTER_SECURITY_PLUGIN; it never changes daemon/ISA semantics.
#
# Usage: ./scripts/cluster/harbor_push.sh [tag] [provenance-json]
# Optional secure stamping:
#   BLITTER_SECURITY_PLUGIN=/abs/path/to/blitter-security-plugin \
#   BLITTER_STAMP_KEY=/abs/path/to/ed25519-private.pem \
#     ./scripts/cluster/harbor_push.sh release-123 provenance.json
set -euo pipefail

REG="harbor.researchstack.info"
PROJ="mathpunch"
TAG="${1:-latest}"
IMAGE="$REG/$PROJ/blitter-daemon:$TAG"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC_DIR="$ROOT/experiments/webgpu_blitter"
DAEMON_BIN="$SRC_DIR/blitter-daemon"
PROFILE_LOCK="$ROOT/docs/specs/BLITTER_ISA_PROFILE_V1.sha256"
PROVENANCE_CODEC="$ROOT/scripts/cluster/image_provenance.py"
PROVENANCE_OUT="${2:-$PWD/blitter-image-provenance.json}"
STAMP_OUT="$PROVENANCE_OUT.stamp.json"

cleanup() {
  rm -f "$DAEMON_BIN"
}
trap cleanup EXIT

for tool in cargo podman skopeo python3 sha256sum git; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "required tool missing: $tool" >&2
    exit 2
  }
done

[ -f "$PROFILE_LOCK" ] || { echo "missing semantic profile lock: $PROFILE_LOCK" >&2; exit 2; }
[ -f "$PROVENANCE_CODEC" ] || { echo "missing provenance codec: $PROVENANCE_CODEC" >&2; exit 2; }
PROFILE_DIGEST=$(tr -d '\r\n' < "$PROFILE_LOCK")
python3 - "$PROFILE_DIGEST" <<'PY'
import re, sys
if re.fullmatch(r"sha256:[0-9a-f]{64}", sys.argv[1]) is None:
    raise SystemExit("invalid semantic profile digest lock")
PY

SOURCE_COMMIT=$(git -C "$ROOT" rev-parse HEAD)
python3 - "$SOURCE_COMMIT" <<'PY'
import re, sys
if re.fullmatch(r"[0-9a-f]{40}", sys.argv[1]) is None:
    raise SystemExit("could not resolve full lowercase source commit")
PY

echo "==> building tracked blitter-daemon source"
cargo build --locked --release --manifest-path "$SRC_DIR/Cargo.toml" --bin blitter-daemon
install -m 0755 "$SRC_DIR/target/release/blitter-daemon" "$DAEMON_BIN"
DAEMON_SHA256=$(sha256sum "$DAEMON_BIN" | awk '{print $1}')
DOCKERFILE_SHA256=$(sha256sum "$SRC_DIR/Dockerfile" | awk '{print $1}')

echo "==> building and tagging $IMAGE"
podman build -t "$IMAGE" "$SRC_DIR"

echo "==> login (Harbor user / robot account)"
podman login "$REG"
echo "==> pushing"
podman push "$IMAGE"

# The registry, not the mutable local tag, is authoritative for the deployed
# image identity. skopeo shares the containers auth file written by podman login.
REMOTE_DIGEST=$(skopeo inspect --format '{{.Digest}}' "docker://$IMAGE")
python3 - "$REMOTE_DIGEST" <<'PY'
import re, sys
if re.fullmatch(r"sha256:[0-9a-f]{64}", sys.argv[1]) is None:
    raise SystemExit("registry returned invalid immutable digest")
PY
IMMUTABLE_REF="$REG/$PROJ/blitter-daemon@$REMOTE_DIGEST"

STAMP_REQUESTED=false
STAMP_PLUGIN_SHA256=""
if [ -n "${BLITTER_STAMP_KEY:-}" ] || [ -n "${BLITTER_SECURITY_PLUGIN:-}" ]; then
  STAMP_REQUESTED=true
  : "${BLITTER_STAMP_KEY:?BLITTER_STAMP_KEY is required when secure stamping is requested}"
  : "${BLITTER_SECURITY_PLUGIN:?BLITTER_SECURITY_PLUGIN is required when secure stamping is requested}"
  case "$BLITTER_STAMP_KEY" in /*) ;; *) echo "BLITTER_STAMP_KEY must be absolute" >&2; exit 4;; esac
  case "$BLITTER_SECURITY_PLUGIN" in /*) ;; *) echo "BLITTER_SECURITY_PLUGIN must be absolute" >&2; exit 4;; esac
  [ -x "$BLITTER_SECURITY_PLUGIN" ] || { echo "security plugin is not executable" >&2; exit 4; }
  STAMP_PLUGIN_SHA256=$(sha256sum "$BLITTER_SECURITY_PLUGIN" | awk '{print $1}')
  "$BLITTER_SECURITY_PLUGIN" handshake | python3 -c '
import json,sys
h=json.load(sys.stdin)
assert h.get("abi")=="mathpunch.blitter-security-plugin.v1"
assert "stamp.ed25519.sha256" in h.get("capabilities", [])
' || { echo "security plugin does not satisfy stamp capability" >&2; exit 4; }
fi

PROVENANCE_ARGS=(
  build
  --out "$PROVENANCE_OUT"
  --published-tag "$IMAGE"
  --immutable-ref "$IMMUTABLE_REF"
  --registry-digest "$REMOTE_DIGEST"
  --source-commit "$SOURCE_COMMIT"
  --semantic-profile-digest "$PROFILE_DIGEST"
  --blitter-daemon-sha256 "sha256:$DAEMON_SHA256"
  --dockerfile-sha256 "sha256:$DOCKERFILE_SHA256"
)
if [ "$STAMP_REQUESTED" = true ]; then
  PROVENANCE_ARGS+=(
    --stamp-requested
    --stamp-plugin-sha256 "sha256:$STAMP_PLUGIN_SHA256"
  )
fi
python3 "$PROVENANCE_CODEC" "${PROVENANCE_ARGS[@]}"

# Self-validate the exact canonical bytes before they can be signed or handed to
# deployment. This is the same parser used by harbor_deploy.sh.
python3 "$PROVENANCE_CODEC" validate \
  --provenance "$PROVENANCE_OUT" \
  --profile-lock "$PROFILE_LOCK" >/dev/null

if [ "$STAMP_REQUESTED" = true ]; then
  "$BLITTER_SECURITY_PLUGIN" stamp --key "$BLITTER_STAMP_KEY" \
    < "$PROVENANCE_OUT" > "$STAMP_OUT"
  echo "==> secure stamp: $STAMP_OUT"
else
  rm -f "$STAMP_OUT"
fi

echo "==> pushed tag:       $IMAGE"
echo "==> immutable image:  $IMMUTABLE_REF"
echo "==> provenance:       $PROVENANCE_OUT"
echo "    deploy from the immutable_ref in the provenance record; do not deploy the mutable tag"
