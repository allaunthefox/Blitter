# Blitter Security Plugin ABI V1

**Schema:** `mathpunch.blitter-security-plugin-abi.v1`  
**Status:** frozen transport/provenance plugin contract; implementation evidence remains gated  
**Layer:** transport/provenance, explicitly outside `BLITTER-ISA-V1` arithmetic semantics

## 1. Purpose

The blitter must be able to run on a plain internal node with no security plugin installed, while a website-facing deployment may request authenticated HTTPS termination and a receipt/artifact workflow may request cryptographic stamping.

Those capabilities are supplied by a **drop-in process plugin**, not linked into the daemon. Upgrading TLS libraries or stamp implementation must therefore be replace-the-plugin, not rewrite/relink-the-blitter.

The core rule is:

```text
base blitter execution               plugin optional
secure website deployment requested  compatible tls.server plugin required
secure stamp requested               compatible stamp.sign plugin required
verification requested               compatible stamp.verify plugin required
```

There is no silent downgrade. If a requested security capability is absent, incompatible, malformed, or fails its handshake, that requested operation is `Blocked` while unrelated base compute remains available.

## 2. Why a process ABI

The plugin boundary is an executable process rather than a Rust/C FFI ABI. This gives:

- language/runtime independence;
- no daemon dependency-graph coupling;
- no in-process memory corruption authority;
- explicit capability negotiation;
- easy side-by-side replacement/rollback;
- per-artifact hashing and provenance;
- architecture-specific static binaries without changing semantic code.

The blitter core knows only the frozen plugin protocol and capability names.

## 3. Discovery

A caller may select a plugin by:

```text
BLITTER_SECURITY_PLUGIN=/absolute/path/to/blitter-security-plugin
```

or an equivalent explicit configuration field. Search-by-`PATH` is advisory convenience only and must not be used by an authority-bearing deployment receipt unless the resolved absolute path and artifact digest are recorded.

The plugin is invoked **only when a security capability is requested**. Base compute does not spawn it merely because it is installed.

## 4. Handshake

The executable MUST support:

```text
blitter-security-plugin handshake
```

and emit one JSON object to stdout:

```json
{
  "schema": "mathpunch.blitter-security-plugin-handshake.v1",
  "abi": "mathpunch.blitter-security-plugin.v1",
  "plugin_id": "mathpunch-go-security-plugin",
  "plugin_version": "0.1.0",
  "capabilities": [
    "tls.server.http1.reverse-proxy",
    "stamp.ed25519.sha256",
    "verify.ed25519.sha256"
  ],
  "build": {
    "goos": "linux",
    "goarch": "amd64",
    "cgo": false
  }
}
```

Unknown ABI versions fail closed. Capability strings are exact, case-sensitive tokens. A newer plugin may add capabilities without invalidating V1 callers. Removing or changing a V1 capability requires a new plugin ABI/version contract.

## 5. TLS capability

Capability:

```text
tls.server.http1.reverse-proxy
```

Reference invocation:

```text
blitter-security-plugin serve \
  --listen 0.0.0.0:443 \
  --upstream http://127.0.0.1:8790 \
  --cert /run/secrets/fullchain.pem \
  --key /run/secrets/key.pem
```

The reference plugin terminates TLS and reverse-proxies HTTP/1.x requests to the explicitly configured upstream. The upstream remains the ordinary blitter daemon and does not acquire TLS semantics.

Required properties:

- minimum TLS version is TLS 1.2 or stronger;
- certificate/key load failure is fatal to the secure listener;
- invalid upstream URL fails before serving;
- proxy errors return an explicit gateway failure rather than silently switching transport;
- the plugin does not modify blitter arithmetic payload semantics except ordinary HTTP transport metadata;
- secure deployment receipts bind the plugin binary digest, certificate identity/fingerprint as policy permits, resolved upstream, and listener configuration.

A future plugin may implement HTTP/2, ACME, mTLS, hardware-backed keys, or another TLS library by advertising additional capabilities. V1 callers need no rewrite as long as the required V1 capability remains conforming.

## 6. Secure stamping capability

Capabilities:

```text
stamp.ed25519.sha256
verify.ed25519.sha256
```

The stamp signs the domain-separated SHA-256 digest of the **exact input bytes** supplied on stdin. Canonicalization of a receipt/document is the caller's responsibility and must occur before stamping.

Domain separation string:

```text
mathpunch-secure-stamp-v1\0
```

Signing invocation:

```text
cat receipt.canonical.json | \
  blitter-security-plugin stamp --key /run/secrets/stamp-ed25519.pkcs8.pem
```

Output schema:

```json
{
  "schema": "mathpunch.secure-stamp.v1",
  "algorithm": "ed25519-sha256-domain-v1",
  "payload_sha256": "<64 lowercase hex chars>",
  "public_key_hex": "<64 lowercase hex chars>",
  "signature_hex": "<128 lowercase hex chars>"
}
```

Verification consumes the exact candidate bytes on stdin and a stamp JSON file:

```text
cat receipt.canonical.json | \
  blitter-security-plugin verify --stamp stamp.json
```

A stamp establishes only that the holder of the corresponding private key signed the recorded payload digest under this domain. It does **not** promote a mathematical claim, prove ISA conformance, prove a benchmark, or identify a human/legal entity unless a separate key-identity policy says so.

## 7. Key handling

The reference plugin accepts Ed25519 PKCS#8 private keys from explicit files. Private keys are never placed in the frozen ISA profile, plugin manifest, heartbeat, or compute payload.

A website node may use filesystem secrets, an injected secret mount, or a future hardware-backed plugin. Hardware-backed signing is deliberately a capability-extension problem rather than a blitter rewrite.

## 8. Static binary requirement

The reference implementation is Go standard-library only and is built with:

```text
CGO_ENABLED=0
```

for a selected `GOOS/GOARCH`. The resulting executable has no dynamic OpenSSL dependency and can be copied onto a compatible node as one artifact. Static binaries remain architecture-specific; `linux/amd64` and `linux/arm64` are distinct artifacts and must have distinct digests.

The phrase “static SSL support” in this repository means a self-contained TLS support executable, not statically linking OpenSSL into the blitter daemon.

## 9. Plugin manifest and provenance

A deployment may pair the executable with a manifest containing:

```json
{
  "schema": "mathpunch.blitter-security-plugin-manifest.v1",
  "abi": "mathpunch.blitter-security-plugin.v1",
  "plugin_id": "mathpunch-go-security-plugin",
  "capabilities": ["..."],
  "artifact": {
    "target": "linux/amd64",
    "sha256": "..."
  }
}
```

The manifest is descriptive until its artifact digest is checked. A semantic ISA/profile hash is not a substitute for this plugin artifact digest.

## 10. Failure and upgrade semantics

The caller MUST treat these as `Blocked` for the requested capability:

- executable missing or not executable;
- handshake timeout/failure;
- malformed JSON handshake;
- wrong ABI;
- required capability absent;
- plugin binary digest mismatch when a digest is pinned;
- signing/verification failure;
- TLS certificate/key/upstream initialization failure.

Unrelated compute remains eligible when policy allows it.

An upgrade is drop-in when:

1. the new executable passes the same V1 handshake;
2. it advertises the required V1 capabilities;
3. conformance/negative tests pass;
4. the deployment updates the pinned artifact digest/receipt;
5. no caller relies on removed, renamed, or behaviorally changed capability semantics.

## 11. Authority separation

```text
BLITTER-ISA-V1              arithmetic/representation semantics
security plugin ABI         transport + stamp mechanism
TLS certificate policy      endpoint identity/trust policy
stamp key policy            signer identity/trust policy
container/image provenance  deployed artifact identity
lease/fencing               ownership protocol
```

None of these layers silently inherits authority from another.
