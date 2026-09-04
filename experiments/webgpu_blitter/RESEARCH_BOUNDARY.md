# Research Target Boundary

`merge`, `tailgain`, and `webgpu-blitter` are local research executables. They
are not deployment targets, daemon routes, mathematical authority, or accepted
computation profiles.

They require Cargo's explicit `research-tools` feature. Standard builds expose
only `blitter-daemon` and `blitter-ternary-scalar`; the Harbor publish script
builds only `blitter-daemon`.

A research tool may become deployable only after all of the following exist:

1. A versioned semantic profile registered in the closed profile registry.
2. Canonical request and result ABIs with byte-level rejection tests.
3. A frozen differential conformance corpus and digest lock.
4. An independent MathPunch verifier path.
5. Fenced daemon target-route and replay/idempotency tests.

Until then, its results are non-authoritative research evidence only.
