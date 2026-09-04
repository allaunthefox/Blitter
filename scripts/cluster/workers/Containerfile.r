# LSP worker image: R language server entrypoint.
#
# BOOTSTRAP BASE: placeholder digest; production must rebuild from a reviewed
# base and record the published ref in the deployment-owned image policy.
# The R worker serves the languageserver protocol over stdio through the
# validated entrypoint below. Canonical argv (must match
# lsp_workers.CANONICAL_COMMANDS["r"]):
#   R --vanilla --slave
FROM docker.io/rocker/r-ver:4.4.1@sha256:d0b5d2edcdf142df94eaa6ad32ff3b5388e468d8f41df1de57b9276fb7de596c

# Reproducible build: pin the R base image digest, pin the languageserver
# package version, and pin the R version.
ARG R_LANGUAGESERVER_VERSION=0.3.18

RUN R --vanilla --slave -e 'install.packages("languageserver", repos="https://cloud.r-project.org", version="${R_LANGUAGESERVER_VERSION}")' \
    && R --vanilla --slave -e 'cat(R.version.string)' \
    && useradd --uid 65532 --user-group --no-create-home --shell /usr/sbin/nologin lsp \
    && mkdir -p /workspace && chown 65532:65532 /workspace

USER 65532:65532
WORKDIR /workspace/source
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["R", "--vanilla", "--slave", "-e", "cat(R.version.string)"]
CMD ["R", "--vanilla", "--slave"]
