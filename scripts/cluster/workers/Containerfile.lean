# LSP worker image: Lean 4 language server.
#
# BOOTSTRAP BASE: the FROM digest below is a deterministic structural
# placeholder. Production deployment MUST rebuild from a reviewed base digest
# and record the published immutable ref in the deployment-owned image policy
# (scripts/cluster/lsp_image_policy.example.json). build_workers.sh
# --require-published refuses placeholder bases.
#
# Canonical argv (must match lsp_workers.CANONICAL_COMMANDS["lean"]):
#   lake env lean --server
FROM docker.io/library/debian:bookworm-slim@sha256:956c08fee7056ecb026a5a21100e5514d7db117f7040a96a0d128dc46147a520

ENV DEBIAN_FRONTEND=noninteractive \
    ELAN_HOME=/opt/elan \
    PATH=/opt/elan/bin:/usr/local/bin:/usr/bin:/bin \
    LEAN_TOOLCHAIN=leanprover/lean4:v4.15.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -o /tmp/elan-init.sh \
    && sh /tmp/elan-init.sh -y --default-toolchain "$LEAN_TOOLCHAIN" --no-modify-path \
    && rm -f /tmp/elan-init.sh \
    && ln -s /opt/elan/bin/lake /usr/local/bin/lake \
    && ln -s /opt/elan/bin/lean /usr/local/bin/lean \
    && lake --version && lean --version \
    && useradd --uid 65532 --user-group --no-create-home --shell /usr/sbin/nologin lsp \
    && mkdir -p /workspace && chown 65532:65532 /workspace

USER 65532:65532
WORKDIR /workspace/source
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD ["lake", "env", "lean", "--version"]
CMD ["lake", "env", "lean", "--server"]
