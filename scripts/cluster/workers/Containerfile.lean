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

# Reproducible build: pin apt package versions and checksum the elan installer.
# elan-init.sh is checksummed against the version pinned below to prevent
# a tampered upstream script from being installed. The version matches the
# Lean toolchain defined in ENV.
ARG ELAN_VERSION=1.0.27
ARG ELAN_SHA256=8c0834c0bbab898f9d7ef7ed17d655d59f8b08b5c9315d8d40e7d9d74d35d2d9
ARG CURL_VERSION=7.88.1-10+deb12u14
ARG GIT_VERSION=1:2.39.5-0+deb12u2
ARG CA_CERTIFICATES_VERSION=20230311+deb12u1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates="${CA_CERTIFICATES_VERSION}" \
        curl="${CURL_VERSION}" \
        git="${GIT_VERSION}" \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL -o /tmp/elan-init.sh \
        "https://github.com/leanprover/elan/releases/download/v${ELAN_VERSION}/elan-init.sh" \
    && echo "${ELAN_SHA256}  /tmp/elan-init.sh" | sha256sum -c - \
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
