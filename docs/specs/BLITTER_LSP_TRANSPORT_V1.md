# Blitter LSP Transport V1

**Schema:** `mathpunch.blitter-lsp-transport.v1`  
**Status:** IMPLEMENTED — `scripts/cluster/lsp_transport.py`  
**Layer:** byte-transparent SSH transport; not a wire-level protocol

## 1. Purpose

This document specifies the `LSPTransport` byte-transparent bridge used by
the Blitter dispatcher to tunnel an interactive language-server session over
SSH to a remote worker container.

The transport is not an LSP wire protocol. It pipes bytes between a local
stdin/stdout and a remote `ssh ... "exec <container-command>"` process. All
LSP semantics (LSP request/response, JSON-RPC envelopes) are managed by the
LSP client on the local end; the transport is protocol-agnostic.

The transport also implements the **session lifecycle hooks** that integrate
the byte tunnel with the lease service. These hooks are the only authority-bearing
mechanism the transport provides.

## 2. Lease hooks (the contract)

The transport accepts an optional `LeaseHooks` object with optional callbacks:

```python
@dataclass(frozen=True)
class LeaseHooks:
    acquire:  Callable[[], Any]          # required
    release:  Callable[[Any], None]      # required
    renew:    Callable[[Any], Any] | None # optional
    begin:    Callable[[Any], Any] | None # optional
    commit:   Callable[[Any], Any] | None # optional
```

The `acquire` callback is called **before SSH starts** and returns an opaque
lease value. The `release` callback is called **after SSH exits** with the
same lease value. The `renew` callback, if provided, runs on a separate
thread at a configurable interval.

The `begin` and `commit` callbacks implement the **fenced session lifecycle**:

- `begin(lease)` is called **after SSH starts** (i.e., the worker is reachable).
  It must call `POST /lsp/session/begin` on the lease server. This marks the
  fenced session in flight and blocks any other `acquire` until the session
  commits or is aborted.

- `commit(lease)` is called **before `release`** (i.e., the worker is done and
  diagnostics are about to be returned to the caller). It must call
  `POST /lsp/session/commit` on the lease server. This re-validates the lease
  before diagnostics may be committed.

  If `commit` raises, the transport raises `TransportError("lease fenced at
  commit: diagnostics must not be committed: ...")`. The caller **must** drop
  all buffered diagnostics in this case.

The hooks are deliberately opt-in: a transport without `begin`/`commit` does
not get the fenced-session safety property. The `lsp_lease.LSPLeaseClient`
wires all four callbacks (acquire, release, renew, begin, commit) so that
production traffic gets fencing.

## 3. Lifecycle

```text
run() invoked
    |
    v
lease_hooks.acquire() -> lease      [acquired=True]
    |
    v
spawn ssh ... "exec <container>"
    |
    v
lease_hooks.begin(lease)            [began=True]
    |
    v
[input/output copy threads run;
 lease_hooks.renew runs every N seconds on a separate thread]
    |
    v
ssh process exits (returncode set)
    |
    v
finally:
    lease_hooks.commit(lease)        [if began]
    lease_hooks.release(lease)       [if acquired]
```

If `begin` raises (e.g., the worker was revoked by another dispatcher), the
SSH child is killed and the exception propagates. The `release` hook still
runs in the `finally` block.

If `commit` raises (e.g., the lease expired during the session), the
exception is raised from `run()` and the caller must drop all output.

## 4. State machine

```text
IDLE
 | run() called
 v
ACQUIRING
 | acquire() returns
 v
SSH_STARTED
 | ssh child spawned
 v
BEGIN
 | begin() returns
 v
IN_FLIGHT
 | (ssh runs; renew thread runs if provided)
 v
SSH_EXITED
 | ssh child returned
 v
COMMIT (in finally)
 | commit() returns
 v
RELEASE (in finally)
 | release() returns
 v
IDLE
```

## 5. Timeouts

The transport applies four timeouts:

| Timeout | Default | Meaning |
|---|---|---|
| `startup_timeout` | 15s | No activity from worker → fail |
| `idle_timeout` | 300s | No activity for too long → fail |
| `total_timeout` | 1800s | Total run elapsed → fail |
| `terminate_grace` | 1s | Time to wait for SSH after SIGTERM before SIGKILL |

These are operational safety nets, not authoritative fences. The authoritative
fence is the lease service.

## 6. Cancellation

The transport accepts a `threading.Event` for cancellation. Setting the event
cancels the run, terminates the SSH process, and raises `TransportCancelled`.

When `renew` raises, the cancel event is set automatically. The transport
will then terminate the SSH child and raise.

## 7. Errors

| Class | Meaning |
|---|---|
| `TransportError` | Base error for the transport |
| `TransportCancelled` | The caller cancelled the session via `cancel_event` |

The transport is byte-transparent: protocol errors are not transport errors.
A `ssh` exit code of 0 is reported as success; non-zero is reported as the
return code from `run()`.

## 8. Nonclaims

The transport does not claim:

- That the remote worker is a real LSP server (it pipes bytes; if you point
  it at `cat` it will pipe `cat` bytes);
- That the lease service is operating correctly (the hooks are just calls;
  the lease service has its own authority);
- That the SSH connection is encrypted beyond what the user's `ssh` config
  provides (operators must configure `ssh` host keys / certificates);
- That the container command runs (the transport runs whatever argv you give it).

The transport's only authority is to wire the lease hooks to the session
lifecycle at exactly the right points. The lease service provides the actual
authority.
