#!/bin/bash
# Run the arithmetic daemon only on loopback and expose the target-side fencing
# gate as the container service. If either process exits, terminate the other and
# fail the container rather than silently degrading to an unfenced daemon.
set -euo pipefail

INNER_BIND="${BLITTER_INNER_BIND:-127.0.0.1:8791}"
GATE_LISTEN="${BLITTER_GATE_LISTEN:-0.0.0.0:8790}"
LEASE_MIN_TTL_MS="${BLITTER_LEASE_MIN_TTL_MS:-100}"
LEASE_MAX_TTL_MS="${BLITTER_LEASE_MAX_TTL_MS:-3600000}"

case "$INNER_BIND" in
  127.0.0.1:*) ;;
  *)
    echo "blitter-stack: BLITTER_INNER_BIND must remain IPv4 loopback in V1" >&2
    exit 2
    ;;
esac

export BLITTER_BIND="$INNER_BIND"

DAEMON_BIN="${BLITTER_DAEMON_BIN:-/usr/local/bin/blitter-daemon}"
LEASE_GATE_BIN="${BLITTER_LEASE_GATE_BIN:-/usr/local/libexec/blitter-lease-gate.py}"

"$DAEMON_BIN" &
daemon_pid=$!

python3 "$LEASE_GATE_BIN" \
  --listen "$GATE_LISTEN" \
  --upstream "http://$INNER_BIND" \
  --min-ttl-ms "$LEASE_MIN_TTL_MS" \
  --max-ttl-ms "$LEASE_MAX_TTL_MS" &
gate_pid=$!

cleanup() {
  trap - EXIT INT TERM
  kill "$gate_pid" "$daemon_pid" 2>/dev/null || true
  wait "$gate_pid" 2>/dev/null || true
  wait "$daemon_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Any child exit invalidates the stack, even an exit code 0: a missing gate or
# missing daemon is not a healthy service. There is no direct-daemon fallback.
set +e
wait -n "$daemon_pid" "$gate_pid"
child_rc=$?
set -e
rc=$child_rc
if [ "$rc" -eq 0 ]; then
  rc=1
fi

echo "blitter-stack: child exited rc=$child_rc; terminating fenced stack" >&2
exit "$rc"
