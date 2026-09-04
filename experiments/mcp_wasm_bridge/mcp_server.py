#!/usr/bin/env python3
"""MCP server exposing the Blitter MCP blob assembler as a tool.

Supports stdio (for opencode CLI) and streamable-http (for locked-down
container clients) transports. Requires OAuth2 bearer token authentication
for HTTP transport.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "cluster"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "experiments" / "webgpu_blitter"))

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp_blob_assembler import assemble
from computation_blob import SCHEMA_V1


class BearerTokenVerifier:
    """Verify bearer tokens against deployment-owned secrets."""

    def __init__(self) -> None:
        self._valid_tokens = set()
        # Read tokens from environment
        for raw in os.environ.get("MCP_BEARER_TOKENS", "").split(","):
            token = raw.strip()
            if token:
                self._valid_tokens.add(token)
        # Read tokens from systemd credential file (deployed via credential)
        cred_path = os.environ.get("MCP_CREDENTIAL_PATH", "/run/credentials/blitter-mcp.service/mcp-bearer-tokens")
        try:
            with open(cred_path) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line:
                        val = line.split("=", 1)[1].strip()
                        if val:
                            self._valid_tokens.add(val)
        except OSError:
            pass

    async def verify_token(self, token: str) -> AccessToken | None:
        if token in self._valid_tokens:
            return AccessToken(
                token=token,
                client_id="blitter-mcp-client",
                scopes=["mcp:assemble"],
                resource="mcp.researchstack.info",
                subject="blitter-runner",
            )
        return None


mcp = FastMCP(
    "blitter-mcp-assembler",
    auth=AuthSettings(
        issuer_url=os.environ.get("MCP_ISSUER_URL", "https://auth.researchstack.info"),
        resource_server_url=os.environ.get("MCP_SERVER_URL", "https://mcp.researchstack.info"),
        required_scopes=["mcp:assemble"],
    ),
    token_verifier=BearerTokenVerifier(),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
    ),
)

@mcp.tool()
async def assemble_envelope(
    request_id: str,
    profile_digest: str,
    terminal_contract_digest: str,
    module_digest: str,
    left_words: list[int],
    right_words: list[int],
    cpus: int,
    memory_mib: int,
    wall_ms: int,
    profile_id: str = "BLITTER-ISA-V1",
    interface: str = "wasi-preview2-component-v1",
) -> dict:
    """Assemble a canonical Blitter computation envelope from MCP inputs.

    Validates all fields and produces a compact, key-sorted JSON envelope
    with authority: none. The assembler is pure transport — it does not
    execute arithmetic.
    """
    request = {
        "request_id": request_id,
        "profile_digest": profile_digest,
        "terminal_contract_digest": terminal_contract_digest,
        "assembler": {"module_digest": module_digest, "interface": interface},
        "left_words": left_words,
        "right_words": right_words,
        "resource_budget": {"cpus": cpus, "memory_mib": memory_mib, "wall_ms": wall_ms},
        "profile_id": profile_id,
    }
    envelope = assemble(request, envelope_schema=SCHEMA_V1)
    return envelope

if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8791"))
    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport in ("sse", "streamable-http"):
        import uvicorn
        app = mcp.streamable_http_app() if transport == "streamable-http" else mcp.sse_app()
        uvicorn.run(app, host=host, port=port)
    else:
        raise SystemExit(f"unknown transport: {transport}")