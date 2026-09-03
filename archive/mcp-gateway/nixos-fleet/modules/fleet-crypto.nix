{ config, pkgs, lib, ... }:

# Fleet crypto — Post-quantum cryptography for MCP Gateway and fleet services.
# Uses liboqs for ML-KEM-768, ML-DSA-65, SLH-DSA.
# Entropy from RAM jitter + network timing + kernel CSPRNG.
{
  config = {
    # ── Crypto library installed system-wide ──────────────────────────────────
    environment.etc."fleet_crypto.py".source = ./fleet_crypto.py;
    environment.sessionVariables.PYTHONPATH = "/etc";

    # ── Python packages ───────────────────────────────────────────────────────
    environment.systemPackages = with pkgs; [
      # Post-quantum crypto
      liboqs              # C library (ML-KEM, ML-DSA, SLH-DSA)
      liboqs-python       # Python bindings

      # Supporting crypto
      (python3.withPackages (ps: with ps; [
        cryptography      # Ed25519, X25519 (hybrid with ML-KEM)
      ]))
    ];

    # ── MCP Gateway crypto policy ─────────────────────────────────────────────
    # Installed as /etc/mcp-crypto-policy.json for the MCP Gateway
    environment.etc."mcp-crypto-policy.json".text = builtins.toJSON {
      version = 1;
      default = {
        kem = "ML-KEM-768";
        signature = "ML-DSA-65";
        hybrid = true;
        entropy = "mixed";
        rotation_days = 90;
      };
      # Per-service overrides
      services = {
        mcp-gateway = {
          kem = "ML-KEM-768";
          signature = "ML-DSA-65";
          hybrid = true;
          entropy = "mixed";
        };
        forgejo = {
          kem = "ML-KEM-768";
          signature = "ML-DSA-65";
          hybrid = false;  # Forgejo uses standard crypto
        };
        authentik = {
          kem = "ML-KEM-768";
          signature = "ML-DSA-65";
          hybrid = false;  # Authentik uses standard crypto
        };
      };
      # Algorithm allowlist (only these can be used)
      allowed_kems = [
        "ML-KEM-512"
        "ML-KEM-768"
        "ML-KEM-1024"
        "X25519+Kyber768"
      ];
      allowed_sigs = [
        "ML-DSA-44"
        "ML-DSA-65"
        "ML-DSA-87"
        "SLH-DSA-SHA2-128s"
        "SLH-DSA-SHA2-128f"
        "SLH-DSA-SHA2-192s"
        "SLH-DSA-SHA2-192f"
      ];
    };
  };
}
