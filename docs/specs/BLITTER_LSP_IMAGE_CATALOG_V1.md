# Blitter LSP Image Catalog v1

## Purpose

This is a pure, fail-closed validator for the trusted image selection made by
deployment. `image_catalog.validate_catalog` checks shape and policy
conformance but performs no cryptography; real trust is established by
`scripts/cluster/image_trust.py` (Ed25519 catalog signatures over the
canonical bytes, provenance source allowlists, per-language SBOM bindings
against the deployment-owned policy). Neither module pulls images, contacts a
registry, or starts a worker. The six and only six language keys are `lean`,
`coq`, `python`, `r`, `julia`, and `rust`.

## Schema

The root object has exactly `schema`, `images`, and `metadata`. `schema` is
`mathpunch.blitter-lsp-image-catalog.v1`. `images` has exactly one entry for
each supported language. Each entry has exactly:

- `image`: a lower-case registry/repository reference ending in a full lower-case
  `@sha256:<64 hex>` digest;
- `argv`: the exact canonical argv vector from the validator, never a shell string;
- `toolchain`: `{name, version, identity}`, where `identity` is exactly
  `name@version`;
- `network`: exactly `false`; and
- `resources`: positive integer `cpus` and `memory_mib` values.

The caller supplies the deployment-owned `approved_images` map. An image is
accepted only when the complete immutable reference, including registry and
digest, exactly matches that map. The caller also supplies per-language resource
ceilings; values above a ceiling fail closed. No digest in this specification is
a claim about a real registry image.

Dispatch requires a separate deployment-owned policy when a catalog is used:
`dispatch.py lsp --catalog CATALOG --catalog-policy POLICY`. The policy must
contain `approved_images` and `resource_ceilings`; values are never copied from
the catalog itself. Omitting the policy fails closed.

`metadata` has exactly `signature` and `provenance`. Signature has required
`algorithm`, `key_id`, and `value` strings. Provenance has required
non-empty `source` and `source_commit` strings plus a SHA-256 `build_digest`.
For shape validation these are required binding metadata only. Trust
verification additionally requires `algorithm == ed25519-sha512`, `key_id` in
the deployment-owned `approved_keys`, `value` a valid signature over
`signing_bytes(catalog)` (canonical bytes with the value blanked), `source`
in `approved_sources`, and per-language SBOM records binding the approved
image and catalog commit. See `image_trust.verify_image_trust` and
`sign_lsp_catalog.py verify`.

Unknown fields, missing fields, extra languages, mutable tags, malformed digests,
noncanonical argv, mismatched toolchain identity, enabled networking, and invalid
resource values are rejected.

## Canonical digest

`canonical_bytes` encodes the validated object as UTF-8-compatible ASCII JSON
with sorted keys, no insignificant whitespace, no NaN/Infinity, and one trailing
newline. `catalog_digest` is `sha256:` followed by the lowercase SHA-256 of those
bytes. The digest covers metadata and all image entries, so changing any catalog
value changes its digest.

## Trust boundary

Shape validation proves only that catalog data matches the supplied local
policy. Full trust verification additionally proves the catalog was signed by
an approved offline key, claims an approved build source, and carries SBOM
records bound to the approved images. Neither proves that a registry currently
serves a digest; dispatch pins immutable digest references and launches with
`--pull=never`. Image execution must still apply the worker contract's
non-root, read-only, capability, resource, and network isolation controls.
