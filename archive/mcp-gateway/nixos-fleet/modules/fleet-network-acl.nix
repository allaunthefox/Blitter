{ config, pkgs, lib, ... }:

# Fleet network ACL — per-service Tailscale network isolation.
# Defines which Tailscale peers each fleet service can reach.
# When Sandlock is available, these become HTTP ACL rules.
# When using legacy backend, these become Landlock port rules.
#
# Tailscale peers (by role):
#   quadfox  = 100.115.171.69  (k3s server + Longhorn + GPU)
#   cupfox   = 100.89.78.66   (k3s server + Longhorn)
#   forgefox = 100.83.225.59  (survivor services)
#   laptop   = 100.104.169.58 (k3s agent, ephemeral)

let
  cfg = config.fleet.network;

  # Tailscale peer definitions
  peers = {
    quadfox  = "100.115.171.69";
    cupfox   = "100.89.78.66";
    forgefox = "100.83.225.59";
    laptop   = "100.104.169.58";
  };

  # Service port definitions
  ports = {
    # Fleet services
    work-scheduler   = 8080;
    scar-api         = 8081;
    provenance-api   = 8082;
    history-api      = 8083;
    blitter-rest     = 8084;
    gossip-udp       = 8791;

    # Infrastructure
    postgresql       = 5432;
    ssh              = 22;
    https            = 443;
    http             = 80;

    # External APIs
    openai           = 443;
  };

  # Per-service network profiles
  # Each service gets a precise allowlist of Tailscale peers + ports
  serviceProfiles = {
    # MCP Gateway — external API calls + internal services
    mcp-gateway = {
      http_allow = [
        "POST api.openai.com/v1/chat/completions"
        "POST api.openai.com/v1/embeddings"
        "GET api.openai.com/v1/models"
      ];
      # Tailscale peers it can reach
      net_allow = [
        "${peers.quadfox}:${toString ports.postgresql}"   # PostgreSQL
        "${peers.cupfox}:${toString ports.postgresql}"    # PostgreSQL
        "${peers.forgefox}:${toString ports.https}"       # Vaultwarden
        "${peers.forgefox}:${toString ports.http}"        # Authentik
      ];
    };

    # Forgejo — only PostgreSQL
    forgejo = {
      net_allow = [
        "${peers.quadfox}:${toString ports.postgresql}"
        "${peers.cupfox}:${toString ports.postgresql}"
      ];
    };

    # Authentik — only PostgreSQL
    authentik = {
      net_allow = [
        "${peers.quadfox}:${toString ports.postgresql}"
        "${peers.cupfox}:${toString ports.postgresql}"
      ];
    };

    # Evidence services — PostgreSQL + other evidence nodes
    evidence-scar = {
      net_allow = [
        "${peers.quadfox}:${toString ports.postgresql}"
        "${peers.cupfox}:${toString ports.postgresql}"
        "${peers.quadfox}:${toString ports.scar-api}"
        "${peers.cupfox}:${toString ports.scar-api}"
      ];
    };

    evidence-provenance = {
      net_allow = [
        "${peers.quadfox}:${toString ports.postgresql}"
        "${peers.cupfox}:${toString ports.postgresql}"
        "${peers.quadfox}:${toString ports.provenance-api}"
        "${peers.cupfox}:${toString ports.provenance-api}"
      ];
    };

    evidence-history = {
      net_allow = [
        "${peers.quadfox}:${toString ports.postgresql}"
        "${peers.cupfox}:${toString ports.postgresql}"
        "${peers.quadfox}:${toString ports.history-api}"
        "${peers.cupfox}:${toString ports.history-api}"
      ];
    };

    # Work scheduler — other schedulers + verifiers
    work-scheduler = {
      net_allow = [
        "${peers.quadfox}:${toString ports.work-scheduler}"
        "${peers.cupfox}:${toString ports.work-scheduler}"
        "${peers.quadfox}:${toString ports.postgresql}"
        "${peers.cupfox}:${toString ports.postgresql}"
      ];
    };

    # Verifier services — PostgreSQL + other verifiers
    verifier-scar = {
      net_allow = [
        "${peers.quadfox}:${toString ports.postgresql}"
        "${peers.cupfox}:${toString ports.postgresql}"
        "${peers.quadfox}:${toString ports.scar-api}"
        "${peers.cupfox}:${toString ports.scar-api}"
      ];
    };

    verifier-provenance = {
      net_allow = [
        "${peers.quadfox}:${toString ports.postgresql}"
        "${peers.cupfox}:${toString ports.postgresql}"
        "${peers.quadfox}:${toString ports.provenance-api}"
        "${peers.cupfox}:${toString ports.provenance-api}"
      ];
    };

    verifier-verify = {
      net_allow = [
        "${peers.quadfox}:${toString ports.postgresql}"
        "${peers.cupfox}:${toString ports.postgresql}"
      ];
    };

    # Gossip daemon — all peers on gossip port
    peer-gossip = {
      net_allow = [
        "${peers.quadfox}:${toString ports.gossip-udp}"
        "${peers.cupfox}:${toString ports.gossip-udp}"
        "${peers.forgefox}:${toString ports.gossip-udp}"
      ];
    };

    # Blitter daemon — all peers on blitter port
    peer-blitter = {
      net_allow = [
        "${peers.quadfox}:${toString ports.blitter-rest}"
        "${peers.cupfox}:${toString ports.blitter-rest}"
        "${peers.forgefox}:${toString ports.blitter-rest}"
      ];
    };
  };

  # Convert profile to SandboxProfile kwargs
  profileToKwargs = profile: {
    inherit (profile) http_allow net_allow;
  };

in {
  options.fleet.network = {
    enable = lib.mkEnableOption "fleet network ACL profiles";

    peers = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = peers;
      description = "Tailscale peer IP addresses by hostname";
    };

    profiles = lib.mkOption {
      type = lib.types.attrsOf lib.types.anything;
      default = serviceProfiles;
      description = "Per-service network ACL profiles";
    };
  };

  config = lib.mkIf cfg.enable {
    # ── Export profiles for Python services ───────────────────────────────────
    environment.etc."fleet-network-acl.json".text = builtins.toJSON {
      peers = cfg.peers;
      ports = ports;
      profiles = cfg.profiles;
    };

    # ── Python helper to load profiles ────────────────────────────────────────
    environment.etc."fleet_network_acl.py".text = ''
      """Fleet network ACL — load per-service Tailscale network profiles."""
      import json
      from pathlib import Path

      ACL_PATH = "/etc/fleet-network-acl.json"

      def load_profiles():
          with open(ACL_PATH) as f:
              return json.load(f)

      def get_profile(service_name):
          """Get network profile for a service."""
          data = load_profiles()
          return data.get("profiles", {}).get(service_name, {})

      def get_sandbox_kwargs(service_name):
          """Convert profile to SandboxProfile kwargs for fleet_sandbox."""
          profile = get_profile(service_name)
          kwargs = {}
          if "net_allow" in profile:
              # Convert "ip:port" to port list for legacy backend
              ports = set()
              for entry in profile["net_allow"]:
                  if ":" in entry:
                      port = int(entry.rsplit(":", 1)[1])
                      ports.add(port)
              if ports:
                  kwargs["connect_ports"] = list(ports)
          if "http_allow" in profile:
              kwargs["http_allow"] = profile["http_allow"]
          if "http_deny" in profile:
              kwargs["http_deny"] = profile["http_deny"]
          return kwargs
    '';
  };
}
