"""Backends behind :class:`~profinet_mcp.pn_client.ProfinetClient`.

``PROFINET_BACKEND`` picks one:

* ``none`` (default) - nothing is contacted; device tools say how to choose a backend.
* ``mock`` - JSON-lines over TCP to ``profinet-mock-server`` (every tool works).
* ``network`` - the real PROFINET network on ``PROFINET_INTERFACE``. DCP Identify
  and gated unicast Set are implemented; cyclic I/O requires an external controller stack.

Every backend answers with ``(result, meta)``; ``meta["backend"]`` names it.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol

import anyio
from anyio.streams.buffered import BufferedByteReceiveStream

from . import dcp, dcp_set, validation
from .adapters.dcp_runtime import run_native
from .config import BACKEND_MOCK, BACKEND_NETWORK, BACKEND_NONE, ProfinetConfig
from .domain.errors import OperationRefused as OperationRefused
from .domain.errors import ProfinetClientError as ProfinetClientError

MAX_RESPONSE_BYTES = 1024 * 1024


class _Call:
    """One tool call: when it started, its single absolute deadline (``not_after``, monotonic), and the MCP
    request's cancel scope (whose ``cancel_called`` the pre-send gate reads)."""

    __slots__ = ("started", "not_after", "cancel_scope")

    def __init__(self, cancel_scope: Any | None = None) -> None:
        self.started = time.monotonic()
        self.not_after: float | None = None
        self.cancel_scope = cancel_scope


_CALL: ContextVar[_Call | None] = ContextVar("profinet_tool_call", default=None)


@contextmanager
def tool_call(cancel_scope: Any | None = None) -> Iterator[None]:
    """Mark the start of a tool call (used by the tool wrapper), with the MCP request's cancel scope."""
    token = _CALL.set(_Call(cancel_scope))
    try:
        yield
    finally:
        _CALL.reset(token)


def call_cancelled() -> bool:
    """True once the MCP client cancelled the current tool call (a plain bool read; safe anywhere).

    The SDK sets the request scope's ``cancel_called`` synchronously when notifications/cancelled
    arrives, but anyio does not interrupt a task whose wait (e.g. a connect) has just completed - it runs
    one more step and could send. The pre-send gate therefore reads the flag itself.
    """
    call = _CALL.get()
    return bool(call is not None and getattr(call.cancel_scope, "cancel_called", False))


def call_not_after(bound: float) -> float:
    """The current tool call's absolute deadline (monotonic seconds).

    ONE deadline per tool call: the first device request fixes it at ``start of the call + bound``, and
    every later request in the same call shares it - it is never restarted per request. Outside a tool
    call (direct use of the client) it is ``now + bound``.
    """
    call = _CALL.get()
    if call is None:
        return time.monotonic() + bound
    if call.not_after is None:
        call.not_after = call.started + bound
    return call.not_after


def elapsed_in_call() -> float:
    """Seconds since the current tool call started (0 outside a tool call)."""
    call = _CALL.get()
    return 0.0 if call is None else max(time.monotonic() - call.started, 0.0)


DEVICE_FIELDS = (
    "device_name",
    "mac_address",
    "ip_address",
    "subnet_mask",
    "gateway",
    "vendor",
    "vendor_id",
    "device_id",
    "device_type",
    "type_of_station",
)


class OperationNotImplemented(ProfinetClientError):
    """The selected backend cannot do this (yet)."""


class BackendNotConfigured(ProfinetClientError):
    """``PROFINET_BACKEND`` is unset or ``none``."""


class RateLimited(OperationRefused):
    """Refused because the same request went out too recently; ``retry_after`` is in seconds."""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RequestNotSent(ProfinetClientError):
    """The request failed before anything was sent (outcome ``not_sent``)."""


class BackendUnavailable(RequestNotSent):
    """The backend could not be reached, so the request was never sent."""


class ClientDisconnected(RequestNotSent):
    """The MCP client went away before the request was sent; nothing was sent."""


class ClientCancelled(RequestNotSent):
    """The MCP client cancelled the tool call before the request was sent; nothing was sent."""


class _ClientGone(Exception):
    """Raised instead of sending once the MCP client has disconnected."""


class _DeadlinePassed(Exception):
    """Raised instead of sending once the tool call's deadline has passed."""


class _CallCancelled(Exception):
    """Raised instead of sending once the MCP client has cancelled the tool call."""


class DeviceRejected(ProfinetClientError):
    """The device refused the request in a way that guarantees nothing was changed."""


class OutcomeUnknown(ProfinetClientError):
    """A write or configuration command was sent but not confirmed; it may have been applied.

    Such requests are never re-sent automatically, so a change cannot reach the device twice.
    """

    def __init__(self, message: str, *, request_sent: bool | None = True) -> None:
        super().__init__(message)
        self.request_sent = request_sent


Meta = dict[str, Any]


class Backend(Protocol):
    name: str

    def deadline(self, timeout: float | None = None) -> float:
        """Overall seconds one request may take, retries included (suite rule 12)."""

    def shutdown(self) -> None:
        """The MCP client disconnected: stop every pending request and send nothing new."""

    async def discover(self, timeout: float, *, allow_cached: bool = False) -> tuple[list[dict[str, Any]], Meta]: ...

    async def read_io(self, device: str, slot: int, subslot: int, length: int) -> tuple[bytes, Meta]: ...

    async def write_io(self, device: str, slot: int, subslot: int, data: bytes) -> tuple[dict[str, Any], Meta]: ...

    async def identify(self, mac: str, duration_s: int) -> tuple[dict[str, Any], Meta]: ...

    async def set_name(self, mac: str, name: str) -> tuple[dict[str, Any], Meta]: ...

    async def set_ip(self, mac: str, ip: str, subnet: str, gateway: str) -> tuple[dict[str, Any], Meta]: ...

    def describe(self) -> dict[str, Any]: ...

    async def probe(self) -> dict[str, Any]: ...


def create_backend(config: ProfinetConfig) -> Backend:
    if config.backend == BACKEND_MOCK:
        return MockBackend(
            host=config.mock_host,
            port=config.mock_port,
            timeout=config.timeout,
            max_retries=config.max_retries,
            backoff_base=config.retry_backoff_base,
        )
    if config.backend == BACKEND_NETWORK:
        return NetworkBackend(interface=config.interface, set_timeout=config.timeout)
    return UnconfiguredBackend()


# ---------------------------------------------------------------------------
# none
# ---------------------------------------------------------------------------

NO_BACKEND = (
    "No backend selected, so no device was contacted. Set PROFINET_BACKEND=mock to use the mock IO device "
    "(profinet-mock-server), or PROFINET_BACKEND=network for DCP discovery on PROFINET_INTERFACE "
    "(needs raw-socket privileges)."
)


class UnconfiguredBackend:
    name = BACKEND_NONE

    def _refuse(self) -> Any:
        raise BackendNotConfigured(NO_BACKEND)

    def deadline(self, timeout: float | None = None) -> float:
        return 5.0

    def shutdown(self) -> None:
        """Nothing is ever pending: this backend contacts nothing."""

    async def discover(self, timeout: float, *, allow_cached: bool = False) -> tuple[list[dict[str, Any]], Meta]:
        return self._refuse()

    async def read_io(self, device: str, slot: int, subslot: int, length: int) -> tuple[bytes, Meta]:
        return self._refuse()

    async def write_io(self, device: str, slot: int, subslot: int, data: bytes) -> tuple[dict[str, Any], Meta]:
        return self._refuse()

    async def identify(self, mac: str, duration_s: int) -> tuple[dict[str, Any], Meta]:
        return self._refuse()

    async def set_name(self, mac: str, name: str) -> tuple[dict[str, Any], Meta]:
        return self._refuse()

    async def set_ip(self, mac: str, ip: str, subnet: str, gateway: str) -> tuple[dict[str, Any], Meta]:
        return self._refuse()

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name}

    async def probe(self) -> dict[str, Any]:
        return {"reachable": None, "detail": NO_BACKEND}


# ---------------------------------------------------------------------------
# mock: JSON lines over TCP to profinet-mock-server
# ---------------------------------------------------------------------------


class MockBackend:
    """Client for the JSON-lines protocol of ``profinet-mock-server``."""

    name = BACKEND_MOCK

    def __init__(
        self, host: str, port: int, timeout: float = 10.0, max_retries: int = 3, backoff_base: float = 0.5
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        #: Set when the MCP client disconnects; checked right before every send.
        self.closing = False
        self._scopes: set[anyio.CancelScope] = set()

    def shutdown(self) -> None:
        """The MCP client disconnected: cancel every pending request.

        A request still connecting or waiting to retry ends without being sent (``not_sent``); one
        already sent ends ``unknown`` for a change. No new request is sent after this.
        """
        self.closing = True
        for scope in list(self._scopes):
            scope.cancel()

    @property
    def endpoint(self) -> str:
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, "mock_endpoint": self.endpoint}

    def _meta(self, op: str) -> Meta:
        return {"backend": self.name, "mock_endpoint": self.endpoint, "mock_op": op}

    def deadline(self, timeout: float | None = None) -> float:
        """Worst case of :meth:`call`: every attempt connects and waits for a reply, plus backoff."""
        attempts = self.max_retries + 1
        backoff = sum(self.backoff_base * (2**attempt) for attempt in range(self.max_retries))
        return attempts * (self.timeout + (timeout or self.timeout)) + backoff + 1.0

    async def _connect(self) -> Any:
        last: BaseException | None = None
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            try:
                with anyio.fail_after(self.timeout):
                    return await anyio.connect_tcp(self.host, self.port)
            except (OSError, TimeoutError) as exc:
                last = exc
                if attempt + 1 < attempts:
                    await anyio.sleep(self.backoff_base * (2**attempt))
        raise BackendUnavailable(
            f"Cannot reach the mock IO device at {self.endpoint} ({last or 'timeout'}; {attempts} attempt(s)). "
            "Start it with `uv run --directory profinet-mock-server profinet-mock-server`, or check "
            "PROFINET_MOCK_HOST / PROFINET_MOCK_PORT."
        )

    async def call(self, request: dict[str, Any], timeout: float | None = None, *, changes_device: bool) -> Any:
        """Send one request and return the ``data`` of a successful answer.

        Connecting is retried (``max_retries``). After the request has been sent, a read that gets no
        reply is sent again, but a request that ``changes_device`` (write, identify, set_name, set_ip)
        never is: the device may already have applied it, so the caller gets :class:`OutcomeUnknown`.
        The whole call is bounded by :meth:`deadline`, and :meth:`shutdown` stops it; either way a
        request that was never sent is reported as not sent.
        """
        op = request["op"]
        try:
            payload = json.dumps(request, allow_nan=False).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:  # built locally; nothing has been sent
            raise RequestNotSent(f"Could not encode the {op!r} request: {exc}") from exc
        if self.closing:
            raise ClientDisconnected(f"The MCP client has disconnected; the {op!r} request was not sent.")
        if call_cancelled():
            raise ClientCancelled(f"The MCP client cancelled the call; the {op!r} request was not sent.")
        total = self.deadline(timeout)
        not_after = call_not_after(total)  # shared by every request of this tool call
        budget = not_after - time.monotonic()
        if budget <= 0:
            raise RequestNotSent(
                f"The {op!r} request was not sent: the tool call's deadline ({total:g} s) had already expired"
            )
        state = {"sent": False, "not_after": not_after}
        line: bytes | None = None
        scope = anyio.CancelScope()
        self._scopes.add(scope)
        try:
            with scope, anyio.fail_after(budget):
                line = await self._exchange(op, payload, timeout, state, changes_device)
        except _ClientGone:
            pass
        except _CallCancelled:
            raise ClientCancelled(
                f"The MCP client cancelled the call before the {op!r} request was sent; nothing was sent."
            ) from None
        except _DeadlinePassed:
            raise RequestNotSent(
                f"The {op!r} request was not sent: the tool call's deadline ({total:g} s) expired before it could "
                "be sent"
            ) from None
        except TimeoutError:
            if changes_device and state["sent"]:
                raise self._unknown(op, f"the {total:g} s deadline expired") from None
            if not state["sent"]:
                raise RequestNotSent(
                    f"The {op!r} request to the mock at {self.endpoint} was not sent within its {total:g} s deadline"
                ) from None
            raise ProfinetClientError(f"The mock at {self.endpoint} did not answer {op!r} in time") from None
        finally:
            self._scopes.discard(scope)
        if line is None:  # stopped by shutdown(): the MCP client disconnected
            if not state["sent"]:
                raise ClientDisconnected(
                    f"The MCP client disconnected before the {op!r} request was sent; nothing was sent."
                )
            if changes_device:
                raise self._unknown(op, "the MCP client disconnected while the request was waiting for its reply")
            raise ProfinetClientError(f"The MCP client disconnected while {op!r} was waiting for its reply")

        try:
            response = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            response = None
        if not isinstance(response, dict) or not isinstance(response.get("success"), bool):
            problem = "its answer is not JSON with a success flag"
            if changes_device:
                raise self._unknown(op, problem)
            raise ProfinetClientError(f"The mock answered {op!r} badly: {problem}")
        if not response["success"]:
            error = response.get("error") or "request failed"
            if response.get("rejected") is True:  # refused before any change, by the mock's contract
                raise DeviceRejected(f"Mock IO device: {error}")
            if changes_device:
                raise self._unknown(op, f"it reported a failure without confirming that nothing changed: {error}")
            raise ProfinetClientError(f"Mock IO device: {error}")
        return response.get("data")

    async def _exchange(
        self, op: str, payload: bytes, timeout: float | None, state: dict[str, Any], changes_device: bool
    ) -> bytes:
        attempts = 1 if changes_device else self.max_retries + 1
        for attempt in range(attempts):
            stream = await self._connect()  # nothing has been sent yet, so this may retry
            try:
                async with stream:
                    if self.closing:  # no await between these checks and the send
                        raise _ClientGone
                    if time.monotonic() >= state["not_after"]:  # never send after the call's deadline
                        raise _DeadlinePassed
                    if call_cancelled():  # cancelled in the same step the connect completed
                        raise _CallCancelled
                    state["sent"] = True  # from here on the request may have left
                    with anyio.fail_after(timeout or self.timeout):
                        await stream.send(payload)
                        return await BufferedByteReceiveStream(stream).receive_until(b"\n", MAX_RESPONSE_BYTES)
            except anyio.DelimiterNotFound:
                problem = f"its answer was larger than {MAX_RESPONSE_BYTES} bytes"
            except TimeoutError:
                problem = f"no reply within {timeout or self.timeout:g} s"
            except (OSError, anyio.EndOfStream, anyio.BrokenResourceError, anyio.IncompleteRead) as exc:
                problem = f"no reply; the connection was lost ({type(exc).__name__})"
            if changes_device:
                raise self._unknown(op, problem)
            if attempt + 1 < attempts and not problem.startswith("its answer"):
                await anyio.sleep(self.backoff_base * (2**attempt))
                continue
            raise ProfinetClientError(f"The mock at {self.endpoint} did not answer {op!r}: {problem}")
        raise AssertionError("unreachable")

    def _unknown(self, op: str, problem: str) -> OutcomeUnknown:
        return OutcomeUnknown(
            f"No usable reply from the mock IO device at {self.endpoint} to {op!r} ({problem}). The change "
            "may have been applied; check before retrying (for example read it back). It was not sent again."
        )

    @staticmethod
    def _device_record(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict) or not isinstance(raw.get("device_name"), str):
            raise ProfinetClientError("The mock returned a malformed device entry")
        record = {key: raw.get(key) for key in DEVICE_FIELDS}
        if "io_areas" in raw:
            record["io_areas"] = raw["io_areas"]
        if "identifying" in raw:
            record["identifying"] = raw["identifying"]
        return record

    async def discover(self, timeout: float, *, allow_cached: bool = False) -> tuple[list[dict[str, Any]], Meta]:
        data = await self.call({"op": "discover"}, timeout=timeout, changes_device=False)
        if not isinstance(data, list):
            raise ProfinetClientError("The mock's discover answer is not a list")
        return [self._device_record(item) for item in data], self._meta("discover")

    async def read_io(self, device: str, slot: int, subslot: int, length: int) -> tuple[bytes, Meta]:
        data = await self.call(
            {"op": "read", "device": device, "slot": slot, "subslot": subslot, "length": length}, changes_device=False
        )
        try:
            payload = bytes.fromhex(data["raw_data_hex"])
        except (TypeError, KeyError, ValueError):
            raise ProfinetClientError("The mock's read answer has no valid raw_data_hex") from None
        if len(payload) != length:
            raise ProfinetClientError(f"The mock returned {len(payload)} bytes, expected {length}")
        meta = self._meta("read")
        if isinstance(data, dict) and data.get("direction"):
            meta["direction"] = data["direction"]
        return payload, meta

    async def write_io(self, device: str, slot: int, subslot: int, data: bytes) -> tuple[dict[str, Any], Meta]:
        answer = await self.call(
            {"op": "write", "device": device, "slot": slot, "subslot": subslot, "data": list(data)},
            changes_device=True,
        )
        written = answer.get("written_bytes") if isinstance(answer, dict) else None
        if written != len(data):
            raise self._unknown("write", f"it confirmed {written!r} written bytes, expected {len(data)}")
        return {"written_bytes": written}, self._meta("write")

    async def identify(self, mac: str, duration_s: int) -> tuple[dict[str, Any], Meta]:
        answer = await self.call({"op": "identify", "mac": mac, "duration_s": duration_s}, changes_device=True)
        return self._confirmed("identify", answer, mac), self._meta("identify")

    async def set_name(self, mac: str, name: str) -> tuple[dict[str, Any], Meta]:
        answer = await self.call({"op": "set_name", "mac": mac, "name": name}, changes_device=True)
        result = self._confirmed("set_name", answer, mac)
        if result.get("device_name") != name:
            raise self._unknown("set_name", f"it did not confirm the new name {name!r}")
        return result, self._meta("set_name")

    async def set_ip(self, mac: str, ip: str, subnet: str, gateway: str) -> tuple[dict[str, Any], Meta]:
        answer = await self.call(
            {"op": "set_ip", "mac": mac, "ip": ip, "subnet": subnet, "gateway": gateway}, changes_device=True
        )
        result = self._confirmed("set_ip", answer, mac)
        if (result.get("ip_address"), result.get("subnet_mask"), result.get("gateway")) != (ip, subnet, gateway):
            raise self._unknown("set_ip", "it did not confirm the requested IP settings")
        return result, self._meta("set_ip")

    def _confirmed(self, op: str, answer: Any, mac: str) -> dict[str, Any]:
        if not isinstance(answer, dict) or answer.get("mac_address") != mac:
            raise self._unknown(op, f"it did not confirm the operation for {mac}")
        return answer

    async def probe(self) -> dict[str, Any]:
        try:
            with anyio.fail_after(min(self.timeout, 5.0)):
                stream = await anyio.connect_tcp(self.host, self.port)
            await stream.aclose()
        except (OSError, TimeoutError) as exc:
            return {"reachable": False, "detail": f"TCP connect to {self.endpoint} failed: {exc or 'timeout'}"}
        return {"reachable": True, "detail": f"TCP connect to {self.endpoint} succeeded"}


# ---------------------------------------------------------------------------
# network: DCP discovery and explicit unicast configuration
# ---------------------------------------------------------------------------

IO_NOT_IMPLEMENTED = (
    "Not implemented on the network backend: cyclic PROFINET IO data is exchanged inside an application "
    "relationship (AR) owned by an IO controller, and this server has no IO-controller stack. Nothing was "
    "sent. Use PROFINET_BACKEND=mock to exercise this tool against profinet-mock-server."
)
#: Minimum time between two DCP Identify All multicasts, so a model calling discovery in a loop cannot
#: flood the PROFINET network (every device answers every request).
MIN_IDENTIFY_INTERVAL_S = 2.0


class NetworkBackend:
    """DCP discovery and gated unicast configuration; no cyclic IO-controller stack."""

    name = BACKEND_NETWORK

    def __init__(
        self,
        interface: str,
        transport: dcp.L2Transport | None = None,
        *,
        min_interval: float = MIN_IDENTIFY_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        slack: float = 5.0,
        set_timeout: float = 5.0,
    ) -> None:
        self.interface = interface
        self.transport = transport if transport is not None else dcp.ScapyL2Transport(interface)
        self.min_interval = min_interval
        self.slack = slack
        self.set_timeout = set_timeout
        self._native_lock = threading.Lock()
        self._generation = 0
        self._clock = clock
        self._last_identify: float | None = None
        # (send time, completion time, devices, info) of the last finished Identify All
        self._last_result: tuple[float, float, list[dict[str, Any]], dict[str, Any]] | None = None
        self.closing = False

    def shutdown(self) -> None:
        """The MCP client disconnected: start no new DCP exchange (discovery is read-only anyway)."""
        self.closing = True

    def deadline(self, timeout: float | None = None) -> float:
        return (timeout if timeout is not None else self.set_timeout) + dcp.SNIFFER_START_TIMEOUT + self.slack

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, "interface": self.interface}

    def _meta(self) -> Meta:
        return {"backend": self.name, "interface": self.interface}

    async def discover(self, timeout: float, *, allow_cached: bool = False) -> tuple[list[dict[str, Any]], Meta]:
        """DCP Identify All. ``allow_cached`` (device lookups) may reuse the last answer within the interval."""
        if self.closing:
            raise ClientDisconnected("The MCP client has disconnected; no DCP request was sent.")
        if self._native_lock.locked():
            raise RateLimited("A previous DCP exchange is still running; nothing was sent", self.min_interval)
        now = self._clock()
        previous = self._last_identify
        if previous is not None and now - previous < self.min_interval:
            cached = self._last_result
            if allow_cached and cached is not None and cached[0] == previous:
                _, finished, devices, info = cached
                meta = {**self._meta(), **info, "cached": True, "age_s": round(max(now - finished, 0.0), 3)}
                return copy.deepcopy(devices), meta
            wait = self.min_interval - (now - previous)
            raise RateLimited(
                f"DCP Identify All was sent {now - previous:.1f} s ago; discovery is limited to one request every "
                f"{self.min_interval:g} s so the PROFINET network is not flooded. Nothing was sent; try again in "
                f"{wait:.1f} s.",
                wait,
            )
        # Claimed before the (threaded) exchange, so concurrent calls are refused too.
        self._last_identify = now
        self._generation += 1
        generation = self._generation
        try:
            # abandon_on_cancel: the client's overall deadline can end the call even if capture hangs.
            devices, info = await run_native(
                lambda transport: dcp.identify_all(transport, timeout),
                self.transport,
                self._native_lock,
                call_not_after(self.deadline(timeout)),
                lambda: self.closing or call_cancelled(),
            )
        except dcp.DcpError as exc:
            if exc.request_sent is False:
                self._last_identify = previous
            # A capture failure can follow a transmitted Identify frame. Keep
            # its rate-limit reservation unless nothing was provably sent.
            raise ProfinetClientError(str(exc)) from exc
        info = {**info, "timeout_s": timeout}
        if self._generation == generation and not self.closing:
            self._last_result = (now, self._clock(), copy.deepcopy(devices), info)
        return devices, {**self._meta(), **info, "cached": False}

    async def read_io(self, device: str, slot: int, subslot: int, length: int) -> tuple[bytes, Meta]:
        raise OperationNotImplemented(IO_NOT_IMPLEMENTED)

    async def write_io(self, device: str, slot: int, subslot: int, data: bytes) -> tuple[dict[str, Any], Meta]:
        raise OperationNotImplemented(IO_NOT_IMPLEMENTED)

    async def identify(self, mac: str, duration_s: int) -> tuple[dict[str, Any], Meta]:
        if duration_s != 3:
            raise OperationRefused(
                "Live DCP Signal uses one fixed flash sequence; duration_s must be 3. Nothing was sent."
            )
        meta = await self._set(mac, 5, 3, bytes.fromhex("0100"), qualifier=0)
        return {"mac_address": mac, "identify_duration_s": 3, "duration_verified": False}, meta

    async def set_name(self, mac: str, name: str) -> tuple[dict[str, Any], Meta]:
        name = validation.station_name(name)
        meta = await self._set(mac, 2, 2, name.encode("ascii"))
        return {"mac_address": mac, "device_name": name, "permanent": True}, meta

    async def set_ip(self, mac: str, ip: str, subnet: str, gateway: str) -> tuple[dict[str, Any], Meta]:
        ip, subnet, gateway = validation.ip_settings(ip, subnet, gateway)
        payload = b"".join(ipaddress.IPv4Address(value).packed for value in (ip, subnet, gateway))
        meta = await self._set(mac, 1, 2, payload)
        return {
            "mac_address": mac,
            "ip_address": ip,
            "subnet_mask": subnet,
            "gateway": gateway,
            "permanent": True,
        }, meta

    async def _set(self, mac: str, option: int, suboption: int, value: bytes, *, qualifier: int = 1) -> Meta:
        mac = validation.mac_address(mac)
        try:
            dcp_set.unicast_mac(mac)
        except ValueError as exc:
            raise OperationRefused(str(exc)) from exc
        if self.closing or call_cancelled():
            raise ClientCancelled("The DCP request was cancelled before dispatch; nothing was sent")
        # A set may apply even if its reply is lost. Never reuse earlier discovery metadata.
        self._generation += 1
        self._last_result = None
        try:
            meta = await run_native(
                lambda transport: dcp_set.set_value(
                    transport, self.set_timeout, mac, option, suboption, value, qualifier=qualifier
                ),
                self.transport,
                self._native_lock,
                call_not_after(self.deadline()),
                lambda: self.closing or call_cancelled(),
            )
        except dcp_set.SetRejected as exc:
            raise DeviceRejected(str(exc)) from exc
        except dcp.DcpError as exc:
            if exc.request_sent is False:
                raise RequestNotSent(str(exc)) from exc
            raise OutcomeUnknown(
                f"{exc}. The configuration may have been applied; it was not sent again.",
                request_sent=exc.request_sent,
            ) from exc
        return {**self._meta(), **meta}

    async def probe(self) -> dict[str, Any]:
        return {
            "reachable": None,
            "detail": "Not checked: the network backend only sends DCP Identify, which discover_devices does.",
        }
