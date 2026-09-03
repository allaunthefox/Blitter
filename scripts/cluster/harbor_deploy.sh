#!/usr/bin/env bash
# Deploy a digest-bound fenced blitter stack to a cluster node.
#
# The published image is expected to expose the target-side lease gate on 8790
# while the arithmetic daemon binds only to 127.0.0.1:8791 inside the container.
# Host port 8790 binds to the node's configured tailnet IP by default, never
# 0.0.0.0. For a website-facing node set BLITTER_PUBLISH_ADDR=127.0.0.1 and put
# the drop-in TLS security plugin in front of that loopback endpoint.
#
# Usage: ./scripts/cluster/harbor_deploy.sh <node> <provenance.json>
# Optional secure provenance verification:
#   BLITTER_REQUIRE_DEPLOY_STAMP=1
#   BLITTER_SECURITY_PLUGIN=/abs/path/to/blitter-security-plugin
#   BLITTER_TRUSTED_STAMP_KEY=/abs/path/to/public-ed25519.pem
set -euo pipefail

[ "$#" -eq 2 ] || {
  echo "usage: $0 <node> <provenance.json>" >&2
  exit 2
}

NODE="$1"
PROVENANCE="$2"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PROFILE_LOCK="$ROOT/docs/specs/BLITTER_ISA_PROFILE_V1.sha256"
PROVENANCE_CODEC="$ROOT/scripts/cluster/image_provenance.py"
[ -f "$PROVENANCE" ] || { echo "missing provenance file: $PROVENANCE" >&2; exit 2; }
[ -f "$PROVENANCE_CODEC" ] || { echo "missing provenance codec: $PROVENANCE_CODEC" >&2; exit 2; }

for tool in python3 ssh; do
  command -v "$tool" >/dev/null 2>&1 || { echo "required tool missing: $tool" >&2; exit 2; }
done

if [ "${BLITTER_REQUIRE_DEPLOY_STAMP:-0}" = 1 ]; then
  : "${BLITTER_SECURITY_PLUGIN:?BLITTER_SECURITY_PLUGIN is required for stamped deployment}"
  : "${BLITTER_TRUSTED_STAMP_KEY:?BLITTER_TRUSTED_STAMP_KEY is required for stamped deployment}"
  case "$BLITTER_SECURITY_PLUGIN" in /*) ;; *) echo "BLITTER_SECURITY_PLUGIN must be absolute" >&2; exit 3;; esac
  case "$BLITTER_TRUSTED_STAMP_KEY" in /*) ;; *) echo "BLITTER_TRUSTED_STAMP_KEY must be absolute" >&2; exit 3;; esac
  [ -x "$BLITTER_SECURITY_PLUGIN" ] || { echo "security plugin not executable" >&2; exit 3; }
  STAMP="$PROVENANCE.stamp.json"
  [ -f "$STAMP" ] || { echo "required provenance stamp missing: $STAMP" >&2; exit 3; }
  "$BLITTER_SECURITY_PLUGIN" handshake | python3 -c '
import json,sys
h=json.load(sys.stdin)
assert h.get("abi")=="mathpunch.blitter-security-plugin.v1"
assert "verify.ed25519.sha256" in h.get("capabilities", [])
' || { echo "security plugin does not satisfy verify capability" >&2; exit 3; }
  "$BLITTER_SECURITY_PLUGIN" verify \
    --stamp "$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$STAMP")" \
    --public-key "$BLITTER_TRUSTED_STAMP_KEY" < "$PROVENANCE" >/dev/null
fi

readarray -t META < <(
  python3 "$PROVENANCE_CODEC" validate \
    --provenance "$PROVENANCE" \
    --profile-lock "$PROFILE_LOCK"
)
[ "${#META[@]}" -eq 4 ] || { echo "provenance validator did not return four identity fields" >&2; exit 2; }
IMAGE_REF="${META[0]}"
IMAGE_DIGEST="${META[1]}"
SOURCE_COMMIT="${META[2]}"
PROFILE_DIGEST="${META[3]}"

ADDR=$(python3 - "$NODE" "$ROOT/scripts/cluster/nodes.json" <<'PY'
import json, sys
name, path = sys.argv[1:]
d = json.load(open(path))
for n in d["nodes"]:
    if n["name"] == name:
        print(n["address"])
        break
else:
    raise SystemExit(f"unknown node: {name}")
PY
)

# Default exposure is tailnet-only. The only admitted override is loopback, which
# is the intended secure-website mode: TLS sidecar :443 -> 127.0.0.1:8790.
PUBLISH_ADDR="${BLITTER_PUBLISH_ADDR:-$ADDR}"
python3 - "$ADDR" "$PUBLISH_ADDR" <<'PY'
import ipaddress, sys
target = ipaddress.ip_address(sys.argv[1])
publish = ipaddress.ip_address(sys.argv[2])
if publish != target and not publish.is_loopback:
    raise SystemExit(
        f"BLITTER_PUBLISH_ADDR must equal configured node address {target} or be loopback; got {publish}"
    )
PY

# Read-only preflight. If an old or new stack is running, replacement is allowed
# only after a live status endpoint proves no work/lease is active. Unknown status
# blocks. Try the intended publish address, configured tailnet address, then
# loopback to support safe replacement of historical deployment modes.
ssh -o BatchMode=yes "root@$ADDR" \
  "TARGET_ADDR='$ADDR' PUBLISH_ADDR='$PUBLISH_ADDR' bash -s" <<'REMOTE_PREFLIGHT'
set -euo pipefail
if docker inspect -f '{{.State.Running}}' blitter-daemon 2>/dev/null | grep -qx true; then
  python3 - <<'PY'
import json, os, urllib.request
candidates=[]
for host in (os.environ["PUBLISH_ADDR"], os.environ["TARGET_ADDR"], "127.0.0.1"):
    if host not in candidates:
        candidates.append(host)
last=None
for host in candidates:
    url=f"http://{host}:8790/status"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            s=json.load(r)
        break
    except Exception as exc:
        last=f"{url}: {exc}"
else:
    raise SystemExit(f"BLOCKED: running blitter occupancy cannot be established: {last}")

if s.get("schema") == "mathpunch.blitter-lease-status.v1":
    required={"ok":bool,"lease_active":bool,"lease_expired":bool,"in_flight":bool,"lease_epoch":int}
    for k,t in required.items():
        if type(s.get(k)) is not t:
            raise SystemExit(f"BLOCKED: invalid lease-gate /status field {k}={s.get(k)!r}")
    if not s["ok"] or s["lease_expired"]:
        raise SystemExit("BLOCKED: lease-gate status is not clean/current")
    if s["lease_active"] or s["in_flight"]:
        raise SystemExit(
            f"BLOCKED: fenced blitter occupied lease_active={s['lease_active']} in_flight={s['in_flight']}"
        )
    print("PRECHECK: running fenced blitter reports no active lease/work")
else:
    required={"ok":bool,"busy":bool,"idle":bool,"active_compute":int,"queued_compute":int}
    for k,t in required.items():
        if type(s.get(k)) is not t:
            raise SystemExit(f"BLOCKED: invalid legacy /status field {k}={s.get(k)!r}")
    derived=s["active_compute"] != 0 or s["queued_compute"] != 0
    if not s["ok"] or s["busy"] != derived or s["idle"] == s["busy"]:
        raise SystemExit("BLOCKED: inconsistent legacy /status occupancy")
    if derived:
        raise SystemExit(
            f"BLOCKED: legacy blitter busy active={s['active_compute']} queued={s['queued_compute']}"
        )
    print("PRECHECK: running legacy blitter reports idle")
PY
else
  echo 'PRECHECK: no running blitter-daemon container'
fi
REMOTE_PREFLIGHT

echo "==> pulling immutable image $IMAGE_REF"
ssh -o BatchMode=yes "root@$ADDR" "docker pull '$IMAGE_REF' >/dev/null"

# Replacement occurs only after the independent preflight. The digest-bound image
# itself enforces daemon-loopback + lease-gate topology. Only gate port 8790 is
# published, and only on tailnet or explicit loopback.
ssh -o BatchMode=yes "root@$ADDR" bash -s -- "$NODE" "$IMAGE_REF" "$PUBLISH_ADDR" <<'REMOTE_DEPLOY'
set -euo pipefail
NODE="$1"
IMAGE_REF="$2"
PUBLISH_ADDR="$3"
extra=()
case "$NODE" in
  nasfox)
    extra+=(--device /dev/dri --device /dev/dri/renderD128)
    ;;
  nixos-laptop)
    extra+=(--device /dev/dri --env BLITTER_ADAPTER=RADV)
    ;;
esac

docker rm -f blitter-daemon 2>/dev/null || true
docker run -d \
  --name blitter-daemon \
  --publish "$PUBLISH_ADDR:8790:8790" \
  --restart unless-stopped \
  "${extra[@]}" \
  "$IMAGE_REF" >/dev/null

# The externally published endpoint must be the lease gate, not the raw daemon.
python3 - "$PUBLISH_ADDR" <<'PY'
import json, sys, time, urllib.request
host=sys.argv[1]
url=f"http://{host}:8790/status"
last=None
for _ in range(60):
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            s=json.load(r)
        if s.get("schema") != "mathpunch.blitter-lease-status.v1":
            raise RuntimeError(f"published endpoint is not lease gate: {s.get('schema')!r}")
        if s.get("ok") is not True or s.get("lease_active") is not False or s.get("in_flight") is not False:
            raise RuntimeError(f"lease gate not idle after deploy: {s!r}")
        if "lease_token" in s or "token" in s:
            raise RuntimeError("lease-gate status leaked bearer material")
        print("POSTCHECK: published endpoint is idle target-side lease gate")
        break
    except Exception as exc:
        last=exc
        time.sleep(0.25)
else:
    raise SystemExit(f"POSTCHECK failed: {last}")
PY

docker port blitter-daemon 8790/tcp
REMOTE_DEPLOY

echo "==> deployed fenced stack to $NODE"
echo "    published gate:   http://$PUBLISH_ADDR:8790"
echo "    immutable image:  $IMAGE_REF"
echo "    source commit:    $SOURCE_COMMIT"
echo "    ISA profile:      $PROFILE_DIGEST"
echo "    registry digest:  $IMAGE_DIGEST"
if [ "$PUBLISH_ADDR" = "127.0.0.1" ] || [ "$PUBLISH_ADDR" = "::1" ]; then
  echo "    mode:             loopback-only gate; attach TLS security plugin for website exposure"
else
  echo "    mode:             tailnet-only gate"
fi
