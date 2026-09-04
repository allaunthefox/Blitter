# Blitter Immutable Execution Object v1

**Status:** minimal reference contract

This contract defines execution identity and content-addressed input metadata. It
does not define blob transfer, streaming, scheduling, leases, execution, or
result promotion. Those mechanisms may consume an execution object but do not
change its identity.

## Canonical representation

An execution object uses schema `mathpunch.blitter-execution-object.v1` and has
exactly these fields:

```text
schema
semantic_digest
profile_id
profile_digest
idempotency_key
program_digest
architecture_digest
terminal_contract_digest
inputs
resource_budget
authority
```

Canonical bytes are compact JSON with lexicographically sorted object keys,
JSON arrays retained in their declared order, ASCII output, no NaN or infinity,
and exactly one trailing LF byte. Unknown fields fail closed.

`semantic_digest` is `sha256:` followed by 64 lowercase hexadecimal characters.
It is SHA-256 over canonical bytes of the object with `semantic_digest` omitted
and each input projected to only `schema`, `digest`, `size_bytes`, and
`media_type`. Thus it seals semantic content and input order without making a
carrier's chunk boundaries part of content identity. The reference
`semantic_digest` function validates a supplied complete object's claimed
digest; it never silently discards an untrusted claim.

`profile_id` is an explicit, non-empty visible-ASCII profile identity and
`profile_digest` is its exact content digest. Consumers must resolve the pair
against their admitted profile set; neither value substitutes for the other.
The profile, program, architecture, and terminal-contract digests are distinct
canonical SHA-256 identities. None is a claim of implementation conformance.

`idempotency_key` is 1 through 128 ASCII characters from
`[A-Za-z0-9._:-]`, begins with an alphanumeric character, and identifies one
logical submission across retries. It is a semantic field: changing it creates
a distinct execution identity even when computation content is otherwise equal.

`resource_budget` contains exactly positive integer `cpus`, `memory_mib`, and
`wall_ms` values. The reference codec imposes defensive maxima; deployment may
apply stricter admission limits. `authority` is exactly `none`.

## Blob descriptors

Each member of the ordered `inputs` array is a
`mathpunch.blitter-blob-descriptor.v1` object with exactly:

```text
schema
digest
size_bytes
media_type
chunks
chunk_manifest_root
```

`digest` is the SHA-256 digest of the complete blob bytes. `size_bytes` is in
the range 0 through 2^40 (1 TiB). This maximum is exactly representable by the
65,536-chunk and 16-MiB-per-chunk limits. `media_type` is 1 through 127 visible
ASCII bytes.
`chunks` is an ordered array of at most 65,536 records, each containing exactly
`digest`, `offset`, and `size_bytes`. Chunk digests bind the bytes of that chunk.
Chunks are non-empty, at most 16 MiB each, start at offset zero, are contiguous
and non-overlapping, and cover exactly the blob size. An empty blob has no
chunks. A consumer still verifies received bytes against both chunk and whole
blob digests.

`chunk_manifest_root` commits to one carrier layout without changing semantic
content identity. It is SHA-256 over the ASCII bytes
`mathpunch.blitter-chunk-manifest.v1` followed by one NUL byte and then the
canonical bytes of this manifest:

```json
{"blob_digest":"sha256:...","chunks":[...],"schema":"mathpunch.blitter-chunk-manifest.v1","size_bytes":123}
```

The manifest repeats the whole-blob digest and size, preventing a valid chunk
list from being transplanted to another descriptor. Validation recomputes the
root and rejects any mismatch. Different valid chunkings of the same blob have
different manifest roots but the same execution `semantic_digest`.

## Carrier separation

URLs, filesystem paths, registry references, peer identities, transport
protocols, compression negotiation, credentials, and retry attempt IDs are
carrier details. They are not fields of a blob descriptor or execution object
and therefore cannot alter semantic identity. A caller may maintain a separate
mapping from blob digest to one or more carriers. Embedding carrier data in the
execution object is an unknown-field error, not a digest exclusion shortcut.
Chunk records and their manifest root are validated carrier metadata explicitly
excluded by the semantic projection; they are the sole such exception.

The reference implementation is `scripts/cluster/execution_object.py`. Its
in-memory blob helper exists only to produce conformance fixtures; it is not a
streaming runtime or transport implementation.
