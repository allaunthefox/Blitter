# MCP Gateway Archive

This archive preserves the current MCP Gateway implementation and its related
design material as a reference for the Blitter artifact-fabric integration.

## Contents

```text
nixos-fleet/modules/fleet-mcp-gateway.nix
nixos-fleet/modules/fleet-crypto.nix
nixos-fleet/modules/fleet-network-acl.nix
nixos-fleet/docs/mcp-boot-process.md
nixos-fleet/docs/mcp-ir-selector-architecture.md
```

The gateway implementation is Nix-generated FastAPI code embedded in
`fleet-mcp-gateway.nix`; it is not a standalone Python source tree.

## Status

This is an archival copy, not an active deployment source. The gateway is not
currently enabled on the main fleet hosts. Credentials, generated keys, runtime
state, and host-specific configuration are intentionally excluded.

## Intended Integration

The future Blitter integration should expose authenticated capability offers and
artifact execution requests to this gateway. Blitter should not introduce a
second MCP server or independent control surface.
