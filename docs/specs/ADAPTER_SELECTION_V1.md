# Adapter Selection Contract V1

**Status:** NORMATIVE_PROCESS_CONTRACT
**Process claim:** `G-ROUTE-ADAPTER-CONTRACT-V1`
**Date:** 2026-07-21

## 1. Scope and authority

This document defines how the KKT/M7.5 review core may select and invoke route
adapters. It is a governance and process contract, not a mathematical theorem,
proof receipt, implementation report, or convergence receipt.

The requirements below are normative. Their presence in this document does not
establish that the current scripts, schemas, fixtures, or historical run
directories implement them. A run may claim conformance only after its actual
configuration and execution artifacts satisfy the corresponding validation
gates.

`G-ROUTE-ADAPTER-CONTRACT-V1` states:

> Adapter use is optional at the project level and never implicit. Every
> adapter-backed operation is bound before execution to an explicit frozen role
> map and source-visible descriptor. The descriptor and all selected code,
> entrypoints, paths, digests, evidence schemas, network capabilities, and
> credential capabilities are validated before adapter import or network use.
> Missing, unknown, opaque, drifting, or undeclared selected-adapter state fails
> closed. Unselected adapters are irrelevant. No adapter or external model has
> mathematical-proof or status-promotion authority.

## 2. Frozen selection record

Every run has one semantic `adapter_mode`. The frozen selection artifact
serializes that field as `mode`:

```text
schema_id: mathpunch.adapter-selection.v1
mode: none | selected
```

The selection artifact also freezes a role map and digest-bound descriptor map:

```text
roles:
  review_route: <adapter_id>  # when active
  evidence: <adapter_id>      # when active
  synthesis: <adapter_id>     # when active
descriptors:
  <adapter_id>:
    path: <repository-relative descriptor path>
    sha256: sha256:<digest>
```

The following rules apply:

1. There is no default adapter, implicit adapter, `auto` adapter, discovery by
   import, or silent fallback.
2. With `mode: none`, both `roles` and `descriptors` are empty. The core must not
   load adapter code, inspect adapter credentials, perform adapter network calls,
   or claim adapter-backed review, evidence capture, synthesis, or M7.5
   convergence.
3. With `mode: selected`, every active operation must name its adapter
   independently in the corresponding role. Omission means the role is inactive;
   it does not imply reuse of another role's adapter.
4. A formal M7.5 run using external reviews requires explicit bindings for
   `review_route`, `evidence`, and `synthesis`. The same descriptor may occupy
   multiple roles, but each role remains explicit.
5. The mode, complete role map, descriptor identities, and their digests are
   frozen before any selected adapter is imported or any network request is
   attempted. A post-freeze change creates a new run configuration.
6. Only selected descriptors and the generic selector/validator are in the
   run's adapter gate surface. Missing, modified, misconfigured, or credential-
   starved unselected adapters do not affect the run.

## 3. Adapter descriptor contract

An adapter is selectable only through a data-only descriptor that can be parsed
without importing adapter-owned code. The selection binds the descriptor's
repository-relative path and digest. The frozen descriptor must declare:

- a stable schema ID and adapter ID;
- the supported subset of the exact roles `review_route`, `evidence`, and
  `synthesis`;
- one explicit module, callable, and source path for every declared role;
- a complete selected source set whose repository-relative path, digest, and
  kind identify every selected module, schema, configuration, policy, and
  documentation input;
- the adapter-neutral core evidence schema plus every adapter extension
  artifact, schema path, and required/optional classification; and
- explicit capabilities: network is `none | explicit`; credentials are
  `none | explicit_argument | explicit_file`; ambient environment access and
  home-directory fallback are both `false`.

All paths used for source, entrypoints, schemas, and run artifacts must be
canonical and confined to their declared roots. Absolute-path substitution,
`..` traversal, symlink escape, undeclared files, and digest mismatch fail
closed. A selected adapter may not expand its declared capabilities at runtime.

Secret values are never frozen into the descriptor. An adapter receives a
credential only as an explicit argument or through the exact explicit file
bound by the run. If outer orchestration sources a credential, it must convert
that explicit run binding into the declared argument or file mechanism; the
adapter may not inspect the ambient environment. Lookup through `HOME`, `~`,
implicit dotfiles, user config directories, credential scans, or undocumented
fallback chains is forbidden.

## 4. Validation and execution order

The generic core must perform these steps in order:

1. parse the frozen semantic `adapter_mode` (`mode`) and complete role map as
   data;
2. resolve only the selected descriptor IDs;
3. validate descriptor shape, role support, entrypoints, path confinement,
   selected source set, digests, evidence schemas, and declared
   capabilities;
4. validate that every requested network and credential capability was
   explicitly authorized for the run, without reading or exposing secret
   values unnecessarily;
5. only after all prior checks pass, import or execute the selected role
   entrypoint; and
6. permit only the validated role's declared network and filesystem effects.

Validation failure returns `NOT_EXECUTED` / `BLOCKED`. Validation must not call
adapter code to decide whether the adapter is valid, and no import, credential
lookup, preflight call, or other network operation may precede validation.

## 5. Shell and library boundaries

All shell orchestration in this protocol must be POSIX `sh` only. Shell entry
scripts must use POSIX syntax and must not rely on Bash-only arrays, conditionals,
process substitution, brace expansion, or shell-specific fallback behavior.

Library boundaries are mandatory:

- the review core may depend only on the generic selection, descriptor,
  receipt, and validation interfaces;
- provider SDKs, Fusion logic, provider quirks, roster construction, route
  preflight, and credential handling remain adapter-owned;
- adapter-owned libraries may be reached only through a validated declared
  entrypoint for the selected role; and
- shell is orchestration, not an alternate path around descriptor validation or
  library APIs.

## 6. Evidence and authority

External LLM responses, panel outputs, judge outputs, route metadata, and
adapter/provider assertions are untrusted diagnostic evidence. They may propose
findings, counterexamples, experiments, or candidate state patches. They do not
constitute a proof, accept a proof receipt, promote a claim, authorize a
milestone transition, or override deterministic validation.

Provider-reported and harness-attested values retain their declared provenance
class. Opaque or unavailable upstream facts remain `UNVERIFIED`; adapter code
must not upgrade them by assertion. A source-visible adapter does not make an
opaque provider independently verifiable.

## 7. Independent count domains

The review corpus and synthesis panel are distinct domains:

```text
review_corpus_count: 100
synthesis_panel_size: adapter policy
FreeLLMAPI Fusion synthesis_panel_size: 8
```

`review_corpus_count` belongs to the review-core run policy. A synthesis panel
size belongs to the selected synthesis adapter's policy. Neither value may be
derived from, substituted for, or validated against the other. For the current
FreeLLMAPI profile, eight panel members plus a separately selected judge consume
the already completed 100-review corpus; they do not redefine its size.

## 8. FreeLLMAPI profile boundary

FreeLLMAPI is one optional, non-default adapter. When explicitly selected, its
adapter specification owns its Fusion implementation and acceptance gate,
formal eight-member synthesis roster and judge, route preflight, catalog and
routing evidence, provider quirks, and credential contract. The generic review
core owns none of those details.

The current FreeLLMAPI evidence-layer implementation statements are scoped to
the artifacts described in
[`FREELLMAPI_ROUTE_ADAPTER_V1.md`](FREELLMAPI_ROUTE_ADAPTER_V1.md). They do not
by themselves establish implementation of this selection contract, descriptor
validation, pre-import enforcement, POSIX-shell conformance, or mandatory
library boundaries.

## 9. Implementation status — non-authorizing

The installable Python core now implements this selection contract for
snapshot, manifest, review, evidence, receipt, and controller validation.
`adapter_mode: none` has an empty provider closure. Selected mode validates the
exact role-to-descriptor set, confined source paths, and source digests before
provider import, credential access, or network use.

The generic, frozen review policy fixes `review_corpus_count: 100`.
FreeLLMAPI's selected descriptor separately owns
`synthesis_panel_size: 8`; neither value is inferred from the other. A
non-empty controller manifest and M7.5 validation must match the generic
count, while provider validation independently checks its panel policy.

This implementation status is not evidence of M7.5 convergence. External
reviews, adapter evidence, and synthesis remain diagnostic and have neither
proof nor promotion authority; an exact run must still satisfy every
applicable condition below.

## 10. Conformance conditions

A run conforms to `G-ROUTE-ADAPTER-CONTRACT-V1` only if all applicable checks
pass for its exact frozen state:

1. mode and all role bindings are explicit and internally consistent;
2. no default, `auto`, discovery, or fallback selected an adapter;
3. only selected descriptors were resolved or loaded;
4. descriptor, source, entrypoint, configuration, and schema digests recompute;
5. all selected paths are confined and all capabilities were declared;
6. validation completed before adapter import, credential lookup, or network;
7. no `HOME` or implicit credential fallback occurred;
8. review-corpus and synthesis-panel counts were validated independently;
9. every external output remained diagnostic and non-authorizing; and
10. all shell orchestration and library crossings obeyed §5.

Failure of any applicable condition is a process failure. It neither proves nor
refutes a mathematical claim.
