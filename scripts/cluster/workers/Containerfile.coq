# LSP worker image: Coq language server (coq-lsp).
#
# BOOTSTRAP BASE: placeholder digest; production must rebuild from a reviewed
# base and record the published ref in the deployment-owned image policy.
#
# Canonical argv (must match lsp_workers.CANONICAL_COMMANDS["coq"]):
#   coq-lsp
FROM docker.io/library/debian:bookworm-slim@sha256:8fa5ac2b88250b9ee35d553efc41833ac3c313ce1821faca7a40af2119cfd428

ENV DEBIAN_FRONTEND=noninteractive \
    OPAMROOT=/opt/opam \
    PATH=/opt/opam/bin:/usr/local/bin:/usr/bin:/bin

# Reproducible build: pin apt package versions, checksum the opam binary,
# and lock the opam switch, coq, and coq-lsp versions.
ARG CURL_VERSION=7.88.1-10+deb12u14
ARG GIT_VERSION=1:2.39.5-0+deb12u2
ARG M4_VERSION=1.4.19-3
ARG UNZIP_VERSION=6.0-28
ARG BUBBLEWRAP_VERSION=0.8.0-2+deb12u1
ARG CA_CERTIFICATES_VERSION=20230311+deb12u1
ARG OPAM_VERSION=2.2.0
ARG OPAM_SHA256=fdf1c1fa7f9bf1c1303fcdf6a35b5b8b8a7eba9ff6cf3c1d1b5b9c5e4ab1d5c5

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates="${CA_CERTIFICATES_VERSION}" \
        curl="${CURL_VERSION}" \
        git="${GIT_VERSION}" \
        m4="${M4_VERSION}" \
        unzip="${UNZIP_VERSION}" \
        bubblewrap="${BUBBLEWRAP_VERSION}" \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL -o /usr/local/bin/opam \
        "https://github.com/ocaml/opam/releases/download/${OPAM_VERSION}/opam-${OPAM_VERSION}-x86_64-linux" \
    && echo "${OPAM_SHA256}  /usr/local/bin/opam" | sha256sum -c - \
    && chmod +x /usr/local/bin/opam \
    && opam init --disable-sandboxing -y \
    && eval "$(opam env)" \
    && opam switch create 5.2.0 \
    && eval "$(opam env)" \
    && opam install -y coq.8.19.2 coq-lsp.8.19.2 \
    && ln -s "$HOME/.opam/5.2.0/bin/coq-lsp" /usr/local/bin/coq-lsp \
    && coq-lsp --version \
    && useradd --uid 65532 --user-group --no-create-home --shell /usr/sbin/nologin lsp \
    && mkdir -p /workspace && chown 65532:65532 /workspace

USER 65532:65532
WORKDIR /workspace/source
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["coq-lsp", "--version"]
CMD ["coq-lsp"]
