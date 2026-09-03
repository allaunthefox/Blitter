# Blitter Security Claims V1

**Schema:** `mathpunch.blitter-security-claims.v1`  
**Status:** evidence-gated companion to `BLITTER_SECURITY_PLUGIN_ABI_V1.md`  
**Authority boundary:** transport security, signer identity, deployed artifact identity, and arithmetic semantics are independent claims.

| claim_id | exact_claim | status | authority | required evidence | forbidden interpretation |
|---|---|---|---:|---|---|
| `BLITTER-SEC-01` | A configured security plugin implements the V1 process ABI and advertises the requested capability. | `HYPOTHESIS` | 7 | executable artifact SHA-256; successful strict handshake; capability negative tests; ABI/version rejection tests | Handshake alone proves TLS or signature cryptography correct. |
| `BLITTER-SEC-02` | The reference `CGO_ENABLED=0` Go build is a self-contained target-specific executable with no dynamic OpenSSL dependency. | `HYPOTHESIS` | 7 | pinned Go version; build command; target; executable SHA-256; dynamic-dependency inspection; handshake receipt | Source says `cgo=false`, therefore a particular deployed binary is known-static. |
| `BLITTER-SEC-03` | A website-facing deployment using capability `tls.server.http1.reverse-proxy` terminates TLS at the plugin and proxies only to its explicitly configured HTTP upstream. | `HYPOTHESIS` | 7 | exact plugin digest; certificate/key policy; loopback/upstream integration test; TLS-version negative test; plaintext-port policy; deployment receipt | ISA conformance proves HTTPS endpoint identity or certificate trust. |
| `BLITTER-SEC-04` | Capability `stamp.ed25519.sha256` signs the domain-separated SHA-256 digest of the exact caller-supplied bytes and `verify.ed25519.sha256` rejects payload/key/signature tampering. | `HYPOTHESIS` | 7 | reference test vectors; tamper tests; pinned plugin digest; independent Ed25519 verification; canonical stamp parser negatives | A valid stamp proves the stamped mathematical claim is true. |
| `BLITTER-SEC-05` | A secure stamp authenticates possession/use of the corresponding signing key for the exact stamped bytes under the V1 domain; signer identity is established only by a separate key policy. | `ENGINEERING_CONVENTION` | 7 | explicit trust/key policy where identity is claimed | Public key embedded in a stamp automatically identifies a human, organization, or theorem authority. |
| `BLITTER-SEC-06` | The security plugin is optional for base compute but mandatory for any operation that explicitly requests one of its secure capabilities; missing/incompatible capability blocks rather than silently downgrades. | `ENGINEERING_CONVENTION` | 7 | plugin-client negative tests; deployment policy tests | Failure to load TLS may fall back to plaintext, or failed signing may emit an unsigned object as if stamped. |
| `BLITTER-SEC-07` | A plugin satisfying the same V1 ABI/capability contract may replace another plugin without relinking or rewriting the blitter core. | `HYPOTHESIS` | 7 | two independently built plugin implementations or versions; same caller conformance suite; artifact-digest change recorded | ABI compatibility means cryptographic implementation quality is identical. |

## Identity tuple

A secure deployment receipt SHOULD bind these identities separately:

```text
semantic_profile_digest   = hash(BLITTER-ISA-V1 semantics)
backend_source_digest     = hash(WGSL/Rust/backend source or build input)
container_image_digest    = immutable deployed image identity
security_plugin_digest    = exact process-plugin executable identity
certificate_identity      = policy-selected certificate/SPKI fingerprint
stamp_public_key_identity = policy-selected signing-key identity
```

No equality or derivation between these digests is assumed. In particular:

```text
semantic_profile_digest != implementation conformance
semantic_profile_digest != image digest
semantic_profile_digest != security plugin digest
security plugin digest   != certificate identity
valid stamp              != theorem authority
```

## Drop-in upgrade rule

A replacement plugin is eligible only if:

1. its artifact digest is newly recorded;
2. strict V1 handshake succeeds;
3. requested V1 capability names are present;
4. the same capability conformance and negative tests pass;
5. deployment receipts name the replacement digest; and
6. no secure operation silently falls back when the replacement is absent or rejected.

The blitter core does not need a dependency update merely because the TLS or signing implementation is replaced.
