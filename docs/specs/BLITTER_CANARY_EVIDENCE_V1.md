# Blitter Canary Evidence V1

**Status:** PROVISIONAL_SCAFFOLDING

## Scope

This contract defines credential-free receipt handling for a future canary CI
lane. It does not modify or activate a workflow, deployment, dispatch, daemon,
registry, or lease path. A receipt is diagnostic engineering evidence only. It
does not prove a mathematical statement, promote a claim, establish deployment
safety, or confer MathPunch authority.

## Receipt

`blitter.canary-evidence.v1` is ASCII JSON, sorted by key, encoded with compact
separators, and terminated by one newline. Unknown fields and noncanonical bytes
fail closed. The canonical example is
`fixtures/canary/scalar-candidate-v1.json`.

The receipt binds:

- an exact 40-character lowercase source commit;
- a tag-free OCI candidate image reference ending in `@sha256:<digest>`;
- `live` execution state and the observed adapter name/backend;
- scalar and candidate request/response SHA-256 digests; and
- `authority: diagnostic-only`.

Scalar evidence is the response produced by the designated scalar reference
path for one request. Candidate evidence is the response produced by the
candidate image for that same request. Both request digests must match. The
receipt deliberately records observations rather than asserting equivalence;
comparison policy belongs to the external verifier.

Raw requests, responses, environment dumps, credentials, authorization headers,
URLs with user information, hostnames, registry credentials, and runner
inventory do not belong in this schema. `redact()` exists for diagnostic data
that must be handled before logging, but redaction is not permission to add
undeclared receipt fields.

## Blocked local status

A credential-free local lane with no real adapter must emit
`blocked-local-no-adapter`. Such a receipt has `live: false`, `adapter: null`,
and null scalar/candidate evidence. It must not be relabeled as skipped, passed,
captured, or verified. Synthetic fixtures test the codec; they are not live-run
evidence.

## Live-run requirements

`captured-live` is valid only when all of the following are true for the exact
run:

1. The source revision and tag-free digest-pinned candidate image are recorded.
2. A real adapter initialized and its observed name and backend are recorded.
3. The same canonical request was executed through the scalar and candidate
   paths, with request and response bytes hashed before receipt construction.
4. No secret or hostname is included in the receipt or verifier output.
5. The canonical receipt is passed to an independently supplied external
   verifier using `scripts/ci/canary_evidence.py verify`.
6. The verifier is an explicit regular, non-symlink executable and its bytes
   match the mandatory `--verifier-sha256` pin before execution.
7. The verifier exits zero and emits a canonical result bound to the receipt
   digest, with `verdict: pass` and `authority: diagnostic-only`.

Missing verifier path/pin, digest drift, process failure, timeout, malformed or
noncanonical output, receipt mismatch, or any non-pass verdict fails closed.
The helper launches the verifier without a shell and with a minimal environment,
so ambient CI credentials are not forwarded. A verifier pass remains external
diagnostic evidence and never establishes mathematical or MathPunch authority.

## Local commands

Validate the fixture without network access or credentials:

```sh
python3 scripts/ci/canary_evidence.py validate fixtures/canary/scalar-candidate-v1.json
```

Run focused unit tests:

```sh
python3 scripts/ci/test_canary_evidence.py
```
