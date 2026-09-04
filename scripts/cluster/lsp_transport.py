#!/usr/bin/env python3
"""Standalone byte-transparent transport for an interactive LSP over SSH."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import BinaryIO, Callable, Any


class TransportError(RuntimeError):
    """The transport could not complete its session."""


class TransportCancelled(TransportError):
    """The caller cancelled the session."""


@dataclass(frozen=True)
class TransportConfig:
    startup_timeout: float = 15.0
    idle_timeout: float | None = 300.0
    total_timeout: float | None = 1800.0
    terminate_grace: float = 1.0
    chunk_size: int = 64 * 1024
    lease_renew_interval: float | None = None

    def __post_init__(self) -> None:
        if self.startup_timeout <= 0:
            raise ValueError("startup_timeout must be positive")
        for name in ("idle_timeout", "total_timeout"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive or None")
        if self.terminate_grace < 0 or self.chunk_size < 1:
            raise ValueError("terminate_grace must be non-negative and chunk_size positive")
        if self.lease_renew_interval is not None and self.lease_renew_interval <= 0:
            raise ValueError("lease_renew_interval must be positive or None")


@dataclass(frozen=True)
class LeaseHooks:
    """Optional authority hooks.  ``acquire`` returns an opaque lease value."""

    acquire: Callable[[], Any]
    release: Callable[[Any], None]
    renew: Callable[[Any], Any] | None = None
    begin: Callable[[Any], Any] | None = None
    commit: Callable[[Any], Any] | None = None


class LSPTransport:
    """Bridge local binary streams to one remote command run through SSH.

    The lease callbacks are deliberately opt-in.  They are called as:

    * ``acquire()`` before SSH starts
    * ``begin(lease)`` after the SSH child process is created and the
      worker is confirmed reachable (marks the fenced session in flight)
    * ``commit(lease)`` after the worker has finished its work but before
      ``release(lease)`` -- this is the fence that prevents a stale worker
      from committing diagnostics after lease expiry or restart
    * ``release(lease)`` after the SSH process exits

    If ``begin`` or ``commit`` is provided, diagnostics must NOT be
    committed to the authoritative surface when they raise.
    """

    def __init__(
        self,
        destination: str,
        remote_command: str,
        *,
        config: TransportConfig | None = None,
        lease_hooks: LeaseHooks | None = None,
        cancel_event: threading.Event | None = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not destination or not remote_command:
            raise ValueError("destination and remote_command must be non-empty")
        if lease_hooks is not None and not callable(lease_hooks.acquire):
            raise TypeError("lease acquire hook must be callable")
        self.destination = destination
        self.remote_command = remote_command
        self.config = config or TransportConfig()
        self.lease_hooks = lease_hooks
        self.cancel_event = cancel_event or threading.Event()
        self.popen_factory = popen_factory
        self.clock = clock
        self.process: Any = None
        self._stop = threading.Event()
        self._activity = threading.Event()
        self._errors: list[BaseException] = []
        self._errors_lock = threading.Lock()

    def ssh_argv(self) -> list[str]:
        return [
            "ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=" + str(max(1, int(self.config.startup_timeout))),
            self.destination, self.remote_command,
        ]

    def _copy_input(self, source: BinaryIO) -> None:
        try:
            while not self._stop.is_set():
                data = source.read(self.config.chunk_size)
                if not data:
                    break
                self._activity.set()
                try:
                    self.process.stdin.write(data)
                    self.process.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    break
        except BaseException as exc:
            with self._errors_lock:
                self._errors.append(exc)
        finally:
            try:
                self.process.stdin.close()
            except (OSError, ValueError, AttributeError):
                pass

    def _copy_output(self, source: BinaryIO, target: BinaryIO) -> None:
        try:
            while not self._stop.is_set():
                data = source.read(self.config.chunk_size)
                if not data:
                    break
                self._activity.set()
                target.write(data)
                target.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            with self._errors_lock:
                self._errors.append(exc)
        finally:
            try:
                source.close()
            except (OSError, ValueError, AttributeError):
                pass

    def _stop_process(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            try:
                process.terminate()
            except (ProcessLookupError, OSError):
                pass
        try:
            process.wait(timeout=self.config.terminate_grace)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                try:
                    process.kill()
                except (ProcessLookupError, OSError):
                    pass
            process.wait()

    def run(
        self,
        *,
        input_stream: BinaryIO | None = None,
        output_stream: BinaryIO | None = None,
        error_stream: BinaryIO | None = None,
    ) -> int:
        """Run until SSH exits, returning its exact exit status."""
        source = input_stream or sys.stdin.buffer
        output = output_stream or sys.stdout.buffer
        errors = error_stream or sys.stderr.buffer
        lease: Any = None
        acquired = False
        began = False
        started = self.clock()
        try:
            if self.lease_hooks is not None:
                lease = self.lease_hooks.acquire()
                acquired = True
            self.process = self.popen_factory(
                self.ssh_argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True,
            )
            # Mark the fenced session in flight as soon as the worker is reachable.
            # If a stale worker still holds a lease, ``begin`` will fail and we
            # tear the new SSH child down before any diagnostics can be
            # committed.
            if self.lease_hooks is not None and self.lease_hooks.begin is not None:
                self.lease_hooks.begin(lease)
                began = True
            threads = [
                threading.Thread(target=self._copy_input, args=(source,), daemon=True),
                threading.Thread(target=self._copy_output, args=(self.process.stdout, output), daemon=True),
                threading.Thread(target=self._copy_output, args=(self.process.stderr, errors), daemon=True),
            ]
            for thread in threads:
                thread.start()

            renewal_stop = threading.Event()
            renewal_thread = None
            if self.lease_hooks is not None and self.lease_hooks.renew is not None:
                interval = self.config.lease_renew_interval
                if interval is None:
                    interval = 10.0

                def renew_loop() -> None:
                    while not renewal_stop.wait(interval):
                        try:
                            self.lease_hooks.renew(lease)  # type: ignore[union-attr]
                        except BaseException as exc:
                            with self._errors_lock:
                                self._errors.append(exc)
                            self.cancel_event.set()
                            return

                renewal_thread = threading.Thread(target=renew_loop, daemon=True)
                renewal_thread.start()

            activity_seen = False
            last_activity = started
            while True:
                if self.cancel_event.is_set():
                    raise TransportCancelled("LSP transport cancelled")
                returncode = self.process.poll()
                now = self.clock()
                if self._activity.is_set():
                    activity_seen = True
                    last_activity = now
                if returncode is not None:
                    break
                if now - started >= (self.config.total_timeout or float("inf")):
                    raise TransportError("LSP transport total timeout")
                if not activity_seen and now - started >= self.config.startup_timeout:
                    raise TransportError("LSP transport startup timeout")
                if self.config.idle_timeout is not None and activity_seen:
                    if now - last_activity >= self.config.idle_timeout:
                        raise TransportError("LSP transport idle timeout")
                if self._activity.is_set():
                    self._activity.clear()
                self.cancel_event.wait(0.02)
            self._stop.set()
            renewal_stop.set()
            if renewal_thread is not None:
                renewal_thread.join(timeout=1.0)
            for thread in threads:
                thread.join(timeout=1.0)
            with self._errors_lock:
                if self._errors:
                    raise TransportError(str(self._errors[0])) from self._errors[0]
            return int(returncode)
        except BaseException:
            self._stop.set()
            self._stop_process()
            raise
        finally:
            if began and self.lease_hooks is not None and self.lease_hooks.commit is not None:
                # Fence at commit: re-validate the lease before diagnostics
                # may be committed by the caller. If commit raises, the caller
                # MUST treat the response as untrusted and drop the output.
                try:
                    self.lease_hooks.commit(lease)  # type: ignore[union-attr]
                except BaseException as exc:
                    raise TransportError(
                        f"lease fenced at commit: diagnostics must not be committed: {exc}"
                    ) from exc
            if acquired:
                try:
                    self.lease_hooks.release(lease)  # type: ignore[union-attr]
                except BaseException as exc:
                    if sys.exc_info()[0] is None:
                        raise TransportError(f"lease release failed: {exc}") from exc


def run_lsp_transport(destination: str, remote_command: str, **kwargs: Any) -> int:
    """Convenience wrapper around :class:`LSPTransport`."""
    return LSPTransport(destination, remote_command, **kwargs).run()
