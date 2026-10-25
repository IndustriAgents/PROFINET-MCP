"""Native exchange ownership survives coroutine cancellation."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import anyio

from ..dcp import DcpError, ScapyL2Transport


class NativeAttempt:
    """One lease over an interface; a late worker cannot reuse a released lease."""

    def __init__(self, lock: Any, deadline: float, stopped: Callable[[], bool]) -> None:
        self.lock = lock
        self.deadline = deadline
        self.stopped = stopped
        self.mutex = threading.Lock()
        self.cancelled = False
        self.started = False
        self.released = False

    def check(self) -> None:
        if self.cancelled or self.stopped() or time.monotonic() >= self.deadline:
            raise DcpError(
                "The DCP request was cancelled or expired before dispatch; nothing was sent", request_sent=False
            )

    def finish(self) -> None:
        with self.mutex:
            if not self.released:
                self.released = True
                self.lock.release()

    def abandon(self) -> None:
        with self.mutex:
            self.cancelled = True
            if not self.started and not self.released:
                self.released = True
                self.lock.release()

    def run(self, operation: Callable[[], Any]) -> Any:
        with self.mutex:
            if self.released:
                raise DcpError("The queued DCP request was cancelled; nothing was sent", request_sent=False)
            self.started = True
        try:
            self.check()
            return operation()
        finally:
            self.finish()


class GuardedTransport:
    def __init__(self, transport: Any, attempt: NativeAttempt) -> None:
        self.transport = transport
        self.attempt = attempt
        self.interface = transport.interface

    def local_mac(self) -> str:
        self.attempt.check()
        return self.transport.local_mac()

    def exchange(self, frame: bytes, timeout: float, accept: Callable[[bytes], bool]) -> list[bytes]:
        self.attempt.check()
        if isinstance(self.transport, ScapyL2Transport):
            # Capture startup can block: check again immediately before sendp.
            return self.transport.exchange(frame, timeout, accept, before_send=self.attempt.check)
        # Injected transports are responsible for their own blocking pre-send preparation.
        return self.transport.exchange(frame, timeout, accept)


async def run_native(
    operation: Callable[[GuardedTransport], Any],
    transport: Any,
    lock: Any,
    deadline: float,
    stopped: Callable[[], bool],
) -> Any:
    if not lock.acquire(blocking=False):
        raise DcpError("A previous DCP exchange is still running; nothing was sent", request_sent=False)
    attempt = NativeAttempt(lock, deadline, stopped)
    guarded = GuardedTransport(transport, attempt)
    try:
        return await anyio.to_thread.run_sync(lambda: attempt.run(lambda: operation(guarded)), abandon_on_cancel=True)
    finally:
        attempt.abandon()
