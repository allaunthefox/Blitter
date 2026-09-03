{ lib, config, pkgs, ... }:

let
  cfg = config.services.fleet.mcp-gateway;
  pythonEnv = pkgs.python3.withPackages (ps: with ps; [
    fastapi
    uvicorn
    httpx
    pydantic
    cryptography
    netifaces
    (ps.callPackage ../../overlays/acp-protocol.nix { })
    (ps.callPackage ../../overlays/fastapi-mcp.nix { })
    (ps.callPackage ../../overlays/uuid-v9.nix { })
  ]);
in {
  options.services.fleet.mcp-gateway = {
    enable = lib.mkEnableOption "Fleet MCP+ACP Gateway";
    port = lib.mkOption {
      type = lib.types.port;
      default = 8443;
      description = "Gateway listen port";
    };
    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Gateway listen address";
    };
    tls = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Enable PQ TLS via X25519Kyber768";
      };
      certFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "TLS cert file (null = auto-generate)";
      };
      keyFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "TLS key file";
      };
    };
    auth = {
      pqKeyFile = lib.mkOption {
        type = lib.types.path;
        default = "/etc/fleet/mcp-gateway-pq-key.json";
        description = "Path to PQ signing key JSON";
      };
      allowedPeers = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        description = "Tailscale hostnames allowed to connect";
      };
    };
    acp = {
      enable = lib.mkEnableOption "ACP agent communication layer";
      agentName = lib.mkOption {
        type = lib.types.str;
        default = config.networking.hostName or "fleet-gateway";
        description = "ACP agent name for this node";
      };
      capabilities = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ "tool_execution" "key_management" "routing" ];
        description = "ACP capabilities this agent advertises";
      };
      trustDb = lib.mkOption {
        type = lib.types.path;
        default = "/var/lib/fleet/acp-trust.json";
        description = "Trust database path";
      };
    };
    mcp = {
      enable = lib.mkEnableOption "MCP tools server";
      name = lib.mkOption {
        type = lib.types.str;
        default = "Fleet MCP Gateway";
        description = "MCP server name";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      { assertion = cfg.mcp.enable || cfg.acp.enable;
        message = "At least one of MCP or ACP must be enabled"; }
    ];

    environment.systemPackages = [ pythonEnv ];

    systemd.services.fleet-mcp-gateway = {
      description = "Fleet MCP+ACP Gateway";
      after = [ "network.target" "tailscale.service" ];
      wants = [ "tailscale.service" ];
      wantedBy = [ "multi-user.target" ];

      serviceConfig = {
        ExecStart = ''
          ${pythonEnv}/bin/uvicorn fleet_mcp_gateway.app:app \
            --host ${cfg.host} \
            --port ${toString cfg.port} \
            ${if cfg.tls.enable then "--ssl-keyfile /etc/fleet/tls/key.pem --ssl-certfile /etc/fleet/tls/cert.pem" else ""}
        '';
        Restart = "always";
        RestartSec = 5;

        # Adaptive start policies — DDoS protection
        StartLimitBurst = 10;
        StartLimitIntervalSec = 300;
        WatchdogSec = 30;

        # Security
        DynamicUser = true;
        StateDirectory = "fleet";
        StateDirectoryMode = "0700";
        ProtectSystem = "strict";
        ProtectHome = true;
        NoNewPrivileges = true;
        PrivateDevices = true;
        PrivateTmp = true;
        RestrictAddressFamilies = "AF_INET AF_INET6 AF_UNIX AF_NETLINK";
        CapabilityBoundingSet = "";
        SystemCallFilter = "@system-service";

        # Tailscale access
        SupplementaryGroups = [ "tailscale" ];

        # Resource limits
        MemoryMax = "1G";
        CPUQuota = "100%";
      };

      environment = {
        FLEET_GATEWAY_PORT = toString cfg.port;
        FLEET_PQ_KEY_FILE = cfg.auth.pqKeyFile;
        FLEET_ACP_ENABLE = if cfg.acp.enable then "1" else "0";
        FLEET_ACP_AGENT_NAME = cfg.acp.agentName;
        FLEET_ACP_CAPABILITIES = lib.concatStringsSep "," cfg.acp.capabilities;
        FLEET_ACP_TRUST_DB = cfg.acp.trustDb;
        FLEET_MCP_ENABLE = if cfg.mcp.enable then "1" else "0";
        FLEET_MCP_NAME = cfg.mcp.name;
        PYTHONPATH = "${pythonEnv}/lib/python${pkgs.python3.version}/site-packages";
      };
    };

    # Create the gateway app
    environment.etc."fleet/mcp-gateway/app.py".text = ''
      import os
      import json
      import asyncio
      import logging
      from contextlib import asynccontextmanager
      from typing import Optional

      from fastapi import FastAPI, Depends, HTTPException, Request
      from fastapi.responses import JSONResponse
      from pydantic import BaseModel

      # MCP integration
      from fastapi_mcp import FastApiMCP

      # Fleet crypto
      import sys
      sys.path.insert(0, "${lib.cleanSource ../../modules}")
      from fleet_crypto import (
          KeyGenPolicy, generate_keypair, encapsulate, decapsulate,
          sign, verify, load_keypair, EntropyCollector
      )

      # ACP integration
      ACP_ENABLED = os.environ.get("FLEET_ACP_ENABLE", "0") == "1"
      if ACP_ENABLED:
          from acp import ACPMessage, Intent, Constraints, Context
          from acp import ACPValidator, TrustManager, Router, AgentCapability

      logger = logging.getLogger("fleet-gateway")

      # --- Configuration ---
      GATEWAY_PORT = int(os.environ.get("FLEET_GATEWAY_PORT", "8443"))
      PQ_KEY_FILE = os.environ.get("FLEET_PQ_KEY_FILE", "/etc/fleet/mcp-gateway-pq-key.json")
      ACP_AGENT_NAME = os.environ.get("FLEET_ACP_AGENT_NAME", "fleet-gateway")
      ACP_CAPABILITIES = os.environ.get("FLEET_ACP_CAPABILITIES", "tool_execution").split(",")
      ACP_TRUST_DB = os.environ.get("FLEET_ACP_TRUST_DB", "/var/lib/fleet/acp-trust.json")

      # --- Global State ---
      _pq_keypair = None
      _acp_trust = None
      _acp_router = None
      _acp_validator = None
      _entropy = None

      @asynccontextmanager
      async def lifespan(app: FastAPI):
          global _pq_keypair, _acp_trust, _acp_router, _acp_validator, _entropy

          # Load or generate PQ keypair
          try:
              _pq_keypair = load_keypair(PQ_KEY_FILE)
              logger.info("Loaded PQ keypair from %s", PQ_KEY_FILE)
          except Exception:
              policy = KeyGenPolicy(kem="ML-KEM-768", signature="ML-DSA-65", hybrid=True)
              pub, priv = generate_keypair(policy)
              _pq_keypair = {"public": pub, "private": priv}
              os.makedirs(os.path.dirname(PQ_KEY_FILE), exist_ok=True)
              with open(PQ_KEY_FILE, "w") as f:
                  json.dump(_pq_keypair, f)
              logger.info("Generated new PQ keypair at %s", PQ_KEY_FILE)

          # Initialize entropy collector
          _entropy = EntropyCollector()

          # Initialize ACP
          if ACP_ENABLED:
              _acp_validator = ACPValidator(strict_mode=True)
              _acp_trust = TrustManager()
              _acp_router = Router()

              # Register this agent
              _acp_router.register_agent(AgentCapability(
                  agent_id=ACP_AGENT_NAME,
                  capabilities=set(ACP_CAPABILITIES),
                  min_deadline_ms=1000,
                  max_tokens=4096,
              ))

              # Load trust database
              if os.path.exists(ACP_TRUST_DB):
                  try:
                      with open(ACP_TRUST_DB) as f:
                          data = json.load(f)
                      for agent_id, level in data.get("trust_levels", {}).items():
                          _acp_trust._trust_levels[agent_id] = level
                      logger.info("Loaded trust database for %d agents", len(data.get("trust_levels", {})))
                  except Exception as e:
                      logger.warning("Failed to load trust DB: %s", e)

              logger.info("ACP enabled: agent=%s capabilities=%s", ACP_AGENT_NAME, ACP_CAPABILITIES)

          yield

          # Save trust database
          if ACP_ENABLED and _acp_trust:
              try:
                  os.makedirs(os.path.dirname(ACP_TRUST_DB), exist_ok=True)
                  with open(ACP_TRUST_DB, "w") as f:
                      json.dump({"trust_levels": dict(_acp_trust._trust_levels)}, f)
                  logger.info("Saved trust database")
              except Exception as e:
                  logger.warning("Failed to save trust DB: %s", e)

      # --- FastAPI App ---
      app = FastAPI(
          title="Fleet MCP+ACP Gateway",
          version="1.0.0",
          lifespan=lifespan,
      )

      # Mount MCP
      mcp = FastApiMCP(app, name=os.environ.get("FLEET_MCP_NAME", "Fleet MCP Gateway"))
      mcp.mount()

      # --- Auth Dependency ---
      async def verify_pq_auth(request: Request):
          """Verify post-quantum signature on request headers."""
          sig = request.headers.get("X-Fleet-Sig")
          ts = request.headers.get("X-Fleet-Ts")
          if not sig or not ts:
              raise HTTPException(status_code=401, detail="Missing PQ auth headers")

          # Check timestamp freshness (5 min window)
          import time
          try:
              req_time = float(ts)
          except ValueError:
              raise HTTPException(status_code=401, detail="Invalid timestamp")

          if abs(time.time() - req_time) > 300:
              raise HTTPException(status_code=401, detail="Request expired")

          # Verify signature
          body = await request.body()
          msg = f"{request.method}:{request.url.path}:{ts}:{body.hex()}"
          try:
              if not verify(_pq_keypair["public"], msg.encode(), bytes.fromhex(sig)):
                  raise HTTPException(status_code=401, detail="Invalid signature")
          except Exception as e:
              raise HTTPException(status_code=401, detail=f"Signature verification failed: {e}")

          return True

      # --- Health ---
      @app.get("/health")
      async def health():
          return {
              "status": "healthy",
              "mcp": os.environ.get("FLEET_MCP_ENABLE", "0") == "1",
              "acp": ACP_ENABLED,
              "pq_key_loaded": _pq_keypair is not None,
          }

      # --- Crypto Endpoints ---
      @app.post("/crypto/keygen")
      async def crypto_keygen(policy: Optional[dict] = None):
          """Generate a new PQ keypair."""
          p = KeyGenPolicy(**(policy or {}))
          pub, priv = generate_keypair(p)
          return {"public": pub, "private": priv, "policy": p.__dict__}

      @app.post("/crypto/encapsulate")
      async def crypto_encapsulate(request: dict):
          """KEM encapsulation."""
          ct, ss = encapsulate(request["public_key"])
          return {"ciphertext": ct, "shared_secret": ss}

      @app.post("/crypto/decapsulate")
      async def crypto_decapsulate(request: dict):
          """KEM decapsulation."""
          ss = decapsulate(request["private_key"], request["ciphertext"])
          return {"shared_secret": ss}

      @app.post("/crypto/sign")
      async def crypto_sign(request: dict):
          """Sign a message."""
          sig = sign(request["private_key"], request["message"].encode())
          return {"signature": sig.hex()}

      @app.post("/crypto/verify")
      async def crypto_verify(request: dict):
          """Verify a signature."""
          valid = verify(request["public_key"], request["message"].encode(), bytes.fromhex(request["signature"]))
          return {"valid": valid}

      # --- ACP Endpoints ---
      if ACP_ENABLED:
          class ACPMessageRequest(BaseModel):
              sender_id: str
              recipient_id: str = ACP_AGENT_NAME
              intent: str
              content: dict
              confidence: float = 0.5
              constraints: Optional[dict] = None
              context: Optional[dict] = None

          @app.post("/acp/message")
          async def acp_message(msg: ACPMessageRequest, auth = Depends(verify_pq_auth)):
              """Receive and process an ACP message."""
              intent = Intent(msg.intent)

              acp_msg = ACPMessage(
                  sender_id=msg.sender_id,
                  recipient_id=msg.recipient_id,
                  intent=intent,
                  content=msg.content,
                  confidence=msg.confidence,
                  constraints=Constraints(**msg.constraints) if msg.constraints else None,
                  context=Context(**msg.context) if msg.context else None,
              )

              # Validate
              result = _acp_validator.validate(acp_msg)
              if not result.valid:
                  raise HTTPException(status_code=400, detail={"errors": result.errors})

              # Process based on intent
              if intent == Intent.REQUEST:
                  return {"status": "received", "message": "Request acknowledged"}
              elif intent == Intent.DELEGATE:
                  return {"status": "accepted", "task_id": f"task-{id(acp_msg)}"}
              elif intent == Intent.REPORT:
                  _acp_trust.record_success(msg.sender_id)
                  return {"status": "logged"}
              elif intent == Intent.CHALLENGE:
                  return {"status": "under_review"}
              else:
                  return {"status": "received", "intent": msg.intent}

          @app.get("/acp/agents")
          async def acp_list_agents(auth = Depends(verify_pq_auth)):
              """List known agents and their trust levels."""
              agents = {}
              for agent_id in _acp_trust._trust_levels:
                  level = _acp_trust.get_trust_level(agent_id)
                  agents[agent_id] = {"trust_level": level.name if hasattr(level, 'name') else str(level)}
              return {"agents": agents, "self": ACP_AGENT_NAME}

          @app.post("/acp/trust")
          async def acp_update_trust(request: dict, auth = Depends(verify_pq_auth)):
              """Update trust level for an agent."""
              agent_id = request["agent_id"]
              action = request["action"]
              if action == "success":
                  _acp_trust.record_success(agent_id)
              elif action == "failure":
                  _acp_trust.record_failure(agent_id, request.get("reason", ""))
              return {"status": "updated", "agent": agent_id}

          @app.get("/acp/capabilities")
          async def acp_capabilities(auth = Depends(verify_pq_auth)):
              """List this agent's capabilities."""
              return {
                  "agent": ACP_AGENT_NAME,
                  "capabilities": ACP_CAPABILITIES,
              }

      # --- MCP Tool Endpoints (explicit) ---
      @app.post("/fleet/keygen")
      async def fleet_keygen(policy: Optional[dict] = None, auth = Depends(verify_pq_auth)):
          """Generate PQ keypair — MCP tool."""
          p = KeyGenPolicy(**(policy or {}))
          pub, priv = generate_keypair(p)
          return {"public": pub, "private": priv}

      @app.post("/fleet/sign")
      async def fleet_sign(request: dict, auth = Depends(verify_pq_auth)):
          """Sign message — MCP tool."""
          sig = sign(request["private_key"], request["message"].encode())
          return {"signature": sig.hex()}

      @app.post("/fleet/verify")
      async def fleet_verify(request: dict, auth = Depends(verify_pq_auth)):
          """Verify signature — MCP tool."""
          valid = verify(request["public_key"], request["message"].encode(), bytes.fromhex(request["signature"]))
          return {"valid": valid}

      @app.post("/fleet/encapsulate")
      async def fleet_encapsulate(request: dict, auth = Depends(verify_pq_auth)):
          """KEM encapsulate — MCP tool."""
          ct, ss = encapsulate(request["public_key"])
          return {"ciphertext": ct, "shared_secret": ss}

      @app.post("/fleet/decapsulate")
      async def fleet_decapsulate(request: dict, auth = Depends(verify_pq_auth)):
          """KEM decapsulate — MCP tool."""
          ss = decapsulate(request["private_key"], request["ciphertext"])
          return {"shared_secret": ss}
    '';

    # Create the __init__.py for the package
    environment.etc."fleet/mcp-gateway/__init__.py".text = "";

    # Polkit rule for gateway management
    security.polkit.extraConfig = ''
      polkit.addRule(function(action, subject) {
        if (action.id == "org.freedesktop.systemd1.manage-units" &&
            action.lookup("unit") == "fleet-mcp-gateway.service" &&
            action.lookup("verb") == "start" &&
            subject.user == "fleet") {
          return polkit.Result.YES;
        }
      });
    '';
  };
}
