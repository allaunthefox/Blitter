#!/usr/bin/env bash
# Deploy a digest-bound fenced blitter stack to a cluster node.
#
# The published image is expected to expose the target-side lease gate on 8790
# while the arithmetic daemon binds only to 127.0.0.1:8791 inside the container.
# This helper only consumes an active, eligible Docker bootstrap plan. It does
# not configure a runtime, an account, image trust, or published-gate access.
#
# Usage: ./scripts/cluster/harbor_deploy.sh <node> <provenance.json>
# Required local operator inputs: BLITTER_SECURITY_PLUGIN,
# BLITTER_TRUSTED_STAMP_KEY, BLITTER_RUNTIME_IMAGE_TRUST_POLICY,
# BLITTER_RUNTIME_IMAGE_TRUST_POLICY_DIGEST, BLITTER_PUBLISHED_GATE_ACCESS_POLICY,
# BLITTER_PUBLISHED_GATE_ACCESS_POLICY_DIGEST, and BLITTER_PUBLISH_ADDR.
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

require_digest() {
  local name="$1" value="${!1:-}"
  [[ "$value" =~ ^sha256:[0-9a-f]{64}$ ]] || {
    echo "$name must be sha256:<64 lowercase hex>" >&2
    exit 2
  }
}
require_positive() {
  local name="$1" value="${!1:-}"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || {
    echo "$name must be a positive integer" >&2
    exit 2
  }
}
require_verified_reference() {
  local path_name="$1" digest_name="$2" path="${!1:-}" expected="${!2:-}" actual
  : "${!path_name:?$path_name is required}"
  case "$path" in /*) ;; *) echo "$path_name must be absolute" >&2; exit 3;; esac
  [ -f "$path" ] && [ -r "$path" ] || { echo "$path_name must name a readable local policy file" >&2; exit 3; }
  require_digest "$digest_name"
  actual="sha256:$(sha256sum "$path" | cut -d' ' -f1)"
  [ "$actual" = "$expected" ] || { echo "$path_name digest does not match $digest_name" >&2; exit 3; }
}

: "${BLITTER_ENVELOPE_PROFILE_ID:?BLITTER_ENVELOPE_PROFILE_ID is required}"
require_digest BLITTER_ENVELOPE_PROFILE_DIGEST
require_digest BLITTER_ENVELOPE_TERMINAL_CONTRACT_DIGEST
require_digest BLITTER_ENVELOPE_ASSEMBLER_MODULE_DIGEST
require_positive BLITTER_ENVELOPE_MAX_CPUS
require_positive BLITTER_ENVELOPE_MAX_MEMORY_MIB
require_positive BLITTER_ENVELOPE_MAX_WALL_MS
if [ "$BLITTER_ENVELOPE_PROFILE_ID" != "BLITTER-ISA-V1" ] \
  || [ "$BLITTER_ENVELOPE_PROFILE_DIGEST" != "sha256:cd60e19d6c3652e08d4ed36e3f8208de7f5d2c553cb7e8f5123175c63ff2f129" ] \
  || [ "$BLITTER_ENVELOPE_TERMINAL_CONTRACT_DIGEST" != "sha256:5a9804978698791d50d937ba93b7f279344b2e0ea658f4e2ee7b2d5a3365fb7f" ]; then
  echo "envelope profile and terminal contract must match the deployed registry binding" >&2
  exit 2
fi

for tool in python3 ssh sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || { echo "required tool missing: $tool" >&2; exit 2; }
done

 : "${BLITTER_SECURITY_PLUGIN:?BLITTER_SECURITY_PLUGIN is required}"
 : "${BLITTER_TRUSTED_STAMP_KEY:?BLITTER_TRUSTED_STAMP_KEY is required}"
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
require_verified_reference BLITTER_RUNTIME_IMAGE_TRUST_POLICY BLITTER_RUNTIME_IMAGE_TRUST_POLICY_DIGEST
require_verified_reference BLITTER_PUBLISHED_GATE_ACCESS_POLICY BLITTER_PUBLISHED_GATE_ACCESS_POLICY_DIGEST

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

python3 - "$PROVENANCE" <<'PY'
import json, sys
if json.load(open(sys.argv[1], encoding="utf-8")).get("secure_stamp", {}).get("requested") is not True:
    raise SystemExit("provenance must declare the required secure stamp")
PY

readarray -t BOOTSTRAP < <(python3 - "$ROOT" "$NODE" <<'PY'
import json, sys
from pathlib import Path
root, name = sys.argv[1:]
sys.path.insert(0, str(Path(root) / "scripts" / "cluster"))
from node_bootstrap_plan import render_bootstrap_plan
plan = render_bootstrap_plan(json.loads((Path(root) / "scripts" / "cluster" / "nodes.json").read_text()))
node = next((item for item in plan["plans"] if item["name"] == name), None)
if node is None:
    rejected = next((item for item in plan["blockers"] if item["name"] == name), None)
    raise SystemExit(f"node {name!r} has no active eligible bootstrap plan: {rejected!r}")
if node["runtime"] != "docker":
    raise SystemExit(f"node {name!r} runtime={node['runtime']!r} is unsupported by harbor_deploy.sh")
print(node["address"])
print(node["runner_user"])
PY
)
 [ "${#BOOTSTRAP[@]}" -eq 2 ] || { echo "active bootstrap plan did not return address and runner user" >&2; exit 2; }
ADDR="${BOOTSTRAP[0]}"
RUNNER_USER="${BOOTSTRAP[1]}"

# The access-control reference above is the approval boundary. Only the
# operator-supplied configured address or loopback may be rendered here.
: "${BLITTER_PUBLISH_ADDR:?BLITTER_PUBLISH_ADDR is required}"
PUBLISH_ADDR="$BLITTER_PUBLISH_ADDR"
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
ssh -o BatchMode=yes "$RUNNER_USER@$ADDR" \
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
ssh -o BatchMode=yes "$RUNNER_USER@$ADDR" "docker pull '$IMAGE_REF' >/dev/null"

# Replacement occurs only after the independent preflight. The digest-bound image
# itself enforces daemon-loopback + lease-gate topology. Only gate port 8790 is
# published, and only on tailnet or explicit loopback.
ssh -o BatchMode=yes "$RUNNER_USER@$ADDR" bash -s -- \
  "$NODE" "$IMAGE_REF" "$PUBLISH_ADDR" \
  "$BLITTER_ENVELOPE_PROFILE_ID" "$BLITTER_ENVELOPE_PROFILE_DIGEST" \
  "$BLITTER_ENVELOPE_TERMINAL_CONTRACT_DIGEST" "$BLITTER_ENVELOPE_ASSEMBLER_MODULE_DIGEST" \
  "$BLITTER_ENVELOPE_MAX_CPUS" "$BLITTER_ENVELOPE_MAX_MEMORY_MIB" \
  "$BLITTER_ENVELOPE_MAX_WALL_MS" <<'REMOTE_DEPLOY'
set -euo pipefail
NODE="$1"
IMAGE_REF="$2"
PUBLISH_ADDR="$3"
PROFILE_ID="$4"
PROFILE_DIGEST="$5"
TERMINAL_CONTRACT_DIGEST="$6"
ASSEMBLER_MODULE_DIGEST="$7"
MAX_CPUS="$8"
MAX_MEMORY_MIB="$9"
MAX_WALL_MS="${10}"
docker rm -f blitter-daemon 2>/dev/null || true
docker run -d \
  --name blitter-daemon \
  --publish "$PUBLISH_ADDR:8790:8790" \
  --restart unless-stopped \
  --env "BLITTER_ENVELOPE_PROFILE_ID=$PROFILE_ID" \
  --env "BLITTER_ENVELOPE_PROFILE_DIGEST=$PROFILE_DIGEST" \
  --env "BLITTER_ENVELOPE_TERMINAL_CONTRACT_DIGEST=$TERMINAL_CONTRACT_DIGEST" \
  --env "BLITTER_ENVELOPE_ASSEMBLER_MODULE_DIGEST=$ASSEMBLER_MODULE_DIGEST" \
  --env "BLITTER_ENVELOPE_MAX_CPUS=$MAX_CPUS" \
  --env "BLITTER_ENVELOPE_MAX_MEMORY_MIB=$MAX_MEMORY_MIB" \
  --env "BLITTER_ENVELOPE_MAX_WALL_MS=$MAX_WALL_MS" \
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
echo "    runner:           $RUNNER_USER (from active bootstrap plan)"
echo "    image trust:      verified local policy reference"
echo "    gate access:      verified local access-control reference"
