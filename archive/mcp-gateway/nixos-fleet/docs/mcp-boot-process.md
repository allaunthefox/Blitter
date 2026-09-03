# Model Context Protocol (MCP) — Remote DevContainer & Pipeline Architecture

> **Status: roadmap.** No Remote DevContainer Connector, public endpoint, or
> MCP service described here is deployed by this repository.

> **Security boundary:** never expose shell execution, file reads, or file
> writes over HTTP without strong authentication, per-request authorization,
> workspace confinement, audit logging, and a deliberately scoped execution
> environment.

This document describes the custom Model Context Protocol (MCP) boot process and interface specification for the infrastructure, operating via the **Remote DevContainer Connector** (`mcp.researchstack.info`).

---

## 1. Architectural Overview

The custom MCP implementation acts as a bridge between client runners, agent orchestrators, and remote execution environments (DevContainers / NixOS hosts).

Rather than relying on proprietary client wrappers, the boot process exposes a standardized HTTP / OpenAPI 3.1 interface (`https://mcp.researchstack.info`) that allows execution, workspace file I/O, and health telemetry.

```
┌─────────────────────────────────────────────────────────────┐
│                 Client Runner / Orchestrator                │
└──────────────────────────────┬──────────────────────────────┘
                               │ HTTP / JSON (OpenAPI 3.1)
                               ▼
               https://mcp.researchstack.info
┌─────────────────────────────────────────────────────────────┐
│                Remote DevContainer Connector                │
│                                                             │
│   POST /execute      → Shell execution in devcontainer     │
│   POST /read_file    → Workspace file reading               │
│   POST /write_file   → Workspace file writing               │
│   GET  /status       → Container & toolchain telemetry      │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
               Target Environment / Host System
```

---

## 2. Boot & Service Management

### A. Background Service Initialization (Systemd User Services)
Custom MCP servers are managed via `systemd` user units on the target node.

*Example (`~/.config/systemd/user/autoproof-mcp.service`):*
```ini
[Unit]
Description=AutoProof MCP Server (HTTP mode)
After=network.target

[Service]
Type=simple
ExecStart=%h/SilverSight/scripts/mcp_autoproof.py --port 8767
Restart=on-failure
WorkingDirectory=%h/SilverSight

[Install]
WantedBy=default.target
```

### B. DevContainer Connector OpenAPI Spec (`mcp.researchstack.info`)
The core remote devcontainer interface defines four primary endpoints:

1. **`POST /execute`**
   - **Summary**: Execute a command in the devcontainer
   - **Payload**: `{"command": "<shell-cmd>", "cwd": "/workspace"}`
   - **Response**: `{"output": "<combined-stdout-stderr>"}`

2. **`POST /read_file`**
   - **Summary**: Read a file from the workspace
    - **Payload**: `{"path": "workspace-relative-path"}`
   - **Response**: `{"content": "<file-contents>"}`

3. **`POST /write_file`**
   - **Summary**: Write a file to the workspace
   - **Payload**: `{"path": "...", "content": "..."}`
   - **Response**: `{"result": "success"}`

4. **`GET /status`**
   - **Summary**: Container & toolchain status
   - **Response**: `{"status": "ready", "hardware": {...}}`

---

## 3. Transport Options

- **HTTP REST / OpenAPI**: Production transport for remote network nodes (`https://mcp.researchstack.info`).
- **Stdio Transport**: Used for local command-line tools executing via standard input/output.
- **FastMCP / Python**: Python-based MCP servers using `fastmcp` or `mcp` SDKs for custom domain tooling (e.g. proof verification, domain codecs).

---

## 4. Integration with Fleet Storage

Inputs, logs, and build artifacts produced during MCP execution connect directly to the Garage service deployed on `quadfox`, `cupfox`, and `forgefox` (the
fleet's Garage S3 endpoints):
- Build outputs → `artifacts`
- Execution logs → `container-volumes`
