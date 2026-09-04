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

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git m4 unzip bubblewrap \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://github.com/ocaml/opam/releases/download/2.2.0/opam-2.2.0-x86_64-linux -o /usr/local/bin/opam \
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
