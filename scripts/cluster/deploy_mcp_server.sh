#!/usr/bin/env bash
# Deploy the Blitter MCP assembler as a host-level service on a fleet node.
#
# This is NOT deployed via Harbor/Docker. The MCP server runs on the host
# so new nodes can connect to it for envelope assembly. The blitter-daemon
# runs inside its own container on port 8790; the MCP server runs on port
# 8791 on the host. Bearer tokens are obtained out-of-band from the
# operator and stored in a restricted systemd credential.
#
# Usage: ./scripts/cluster/deploy_mcp_server.sh <node>
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MCP_SERVER="${ROOT}/experiments/mcp_wasm_bridge/mcp_server.py"
SYSTEMD_UNIT="blitter-mcp.service"
SERVICE_USER="${MCP_SERVICE_USER:-forgejo-runner}"
SERVICE_GROUP="${MCP_SERVICE_GROUP:-forgejo-runner}"
MCP_PORT="${MCP_PORT:-8791}"
MCP_HOST="${MCP_HOST:-0.0.0.0}"
MCP_ISSUER_URL="${MCP_ISSUER_URL:-https://auth.researchstack.info}"
MCP_SERVER_URL="${MCP_SERVER_URL:-https://mcp.researchstack.info}"

[ -f "$MCP_SERVER" ] || { echo "missing MCP server: $MCP_SERVER" >&2; exit 2; }
[ -n "${MCP_BEARER_TOKENS:?MCP_BEARER_TOKENS is required}" ]

install_service() {
    local node="$1"
    local service_file="/etc/systemd/system/${SYSTEMD_UNIT}"
    local remote_script="/tmp/blitter-mcp-server.py"
    local credential_dir="/run/credentials/${SYSTEMD_UNIT}"

    ssh -o BatchMode=yes "${SERVICE_USER}@${node}" \
        "mkdir -p /var/lib/forgejo-runner"
    scp -o BatchMode=yes "$MCP_SERVER" "${SERVICE_USER}@${node}:${remote_script}"

    ssh -o BatchMode=yes "${SERVICE_USER}@${node}" <<REMOTE_INSTALL
set -euo pipefail
mkdir -p "${credential_dir}"
chmod 700 "${credential_dir}"
cat > "${credential_dir}/mcp-bearer-tokens" <<TOKENEOF
MCP_BEARER_TOKENS=${MCP_BEARER_TOKENS}
TOKENEOF
chmod 600 "${credential_dir}/mcp-bearer-tokens"

cat > "${service_file}" <<'EOF'
[Unit]
Description=Blitter MCP Assembler Service
After=network.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
Environment=MCP_TRANSPORT=streamable-http
Environment=MCP_HOST=${MCP_HOST}
Environment=MCP_PORT=${MCP_PORT}
Environment=MCP_ISSUER_URL=${MCP_ISSUER_URL}
Environment=MCP_SERVER_URL=${MCP_SERVER_URL}
LoadCredential=mcp-bearer-tokens:${credential_dir}/mcp-bearer-tokens
ExecStart=/home/allaun/.local/share/headroom-venv/bin/python ${remote_script}
Restart=always
RestartSec=10s
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictNamespaces=user pid net ipc uts
CapabilityBoundingSet=
AmbientCapabilities=
ReadWritePaths=/tmp /var/lib/forgejo-runner
InaccessiblePaths=/root /home
TasksMax=4096
MemoryMax=4G
MemoryHigh=3.5G
CPUQuota=300%

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SYSTEMD_UNIT}"
systemctl restart "${SYSTEMD_UNIT}"
systemctl status "${SYSTEMD_UNIT}" --no-pager
REMOTE_INSTALL
}

verify_service() {
    local node="$1"
    ssh -o BatchMode=yes "${SERVICE_USER}@${node}" \
        "systemctl is-active ${SYSTEMD_UNIT}"
    ssh -o BatchMode=yes "${SERVICE_USER}@${node}" \
        "curl -s -o /dev/null -w '%{http_code}' http://localhost:${MCP_PORT}/mcp"
}

case "${1:-}" in
    "")
        echo "usage: $0 <node>" >&2
        exit 2
        ;;
    *)
        NODE="$1"
        echo "==> deploying MCP assembler to ${NODE}"
        install_service "$NODE"
        echo "==> verifying MCP service on ${NODE}"
        verify_service "$NODE"
        echo "==> MCP assembler reachable at http://${NODE}:${MCP_PORT}/mcp"
        echo "==> token-protected: present MCP_BEARER_TOKENS to authenticate"
        ;;
esac