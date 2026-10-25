"""Opt-in cifX/netX access to an already commissioned PROFINET IO controller.

The firmware owns cyclic communication. No reset, download, bus-on, host-ready,
watchdog reconfiguration or native write retry is performed here.
"""

from __future__ import annotations

import ctypes as C
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio

from ..backends import (
    OperationNotImplemented,
    OutcomeUnknown,
    ProfinetClientError,
    RequestNotSent,
    call_cancelled,
    call_not_after,
)
from ..config import ConfigError, ProfinetConfig
from ..domain.cifx_map import ProcessMap
from .cifx_abi import HANDLE, U32, ChannelInfo, IOInfo, LinuxInit, UnsupportedLibraryError, load_library, stderr_file

# nxdrvlinux owns process globals; two instances cannot independently initialize it.
_DRIVER_LEASE = threading.Lock()


def startup_name(uio_number: int) -> str:
    return Path(f"/sys/class/uio/uio{uio_number}/name").read_text(encoding="ascii").strip()


class Attempt:
    def __init__(self, backend, deadline, cancelled):
        self.backend = backend
        self.deadline = deadline
        self.cancelled_by_client = cancelled
        self.guard = threading.Lock()
        self.cancelled = False
        self.started = False
        self.released = False
        self.write_invoked = False

    def check(self):
        if self.cancelled or self.backend.closing or self.cancelled_by_client() or time.monotonic() >= self.deadline:
            raise RequestNotSent("cifX call stopped before its next native operation")

    def timeout_ms(self):
        self.check()
        return max(1, int((self.deadline - time.monotonic()) * 1000))

    def begin_write(self):
        with self.guard:
            self.check()
            self.write_invoked = True

    def abandon(self):
        with self.guard:
            self.cancelled = True
            if not self.started and not self.released:
                self.released = True
                self.backend._operation_lock.release()

    def run(self, operation):
        with self.guard:
            if self.released:
                raise RequestNotSent("Queued cifX call was cancelled before dispatch")
            self.started = True
        try:
            self.check()
            return operation(self)
        finally:
            if self.backend.closing:
                self.backend._close()
            with self.guard:
                self.released = True
                self.backend._operation_lock.release()


class CifxBackend:
    name = "cifx"

    def __init__(self, config: ProfinetConfig, *, startup_reader: Callable[[int], str] = startup_name):
        self.config = config
        if config.cifx_map_path is None:
            raise ConfigError("PROFINET_CIFX_MAP_FILE is required for cifx")
        self.map = ProcessMap.load(config.cifx_map_path)
        self._startup_reader = startup_reader
        self._operation_lock = threading.Lock()
        self._library = None
        self._device = None
        self._driver = HANDLE()
        self._channel = HANDLE()
        self._initialized = False
        self._owns_lease = False
        self._faulted = False
        self.closing = False

    def deadline(self, timeout: float | None = None) -> float:
        return self.config.timeout

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "cifx": {
                "init_enabled": self.config.cifx_init_enabled,
                "card_number": self.config.cifx_card_number,
                "channel": self.config.cifx_channel,
                "initialized": self._initialized,
                "faulted": self._faulted,
                "mapping_count": len(self.map.mappings),
                "firmware_name": self.map.firmware_name,
                "firmware_version": list(self.map.firmware_version),
                "input_size": self.map.input_size,
                "output_size": self.map.output_size,
                "max_age_ms": self.map.max_age_ms,
                "mode": "precommissioned_donttouch",
            },
        }

    @staticmethod
    def _check_code(code: int, operation: str):
        if code != 0:
            raise RequestNotSent(f"cifX {operation} failed (0x{code & 0xFFFFFFFF:08x})")

    def _call(self, name, *args):
        self._check_code(getattr(self._library, name)(*args), name)

    def _open(self, attempt):
        if self._initialized:
            return
        if self._faulted:
            raise RequestNotSent("cifX initialization failed; operator review and server restart required")
        if not self.config.cifx_init_enabled:
            raise RequestNotSent("cifX initialization requires PROFINET_CIFX_INIT_ENABLED=true after commissioning")
        if not _DRIVER_LEASE.acquire(blocking=False):
            raise RequestNotSent("Another cifX backend owns the process-global driver")
        self._owns_lease = True
        try:
            attempt.check()
            if not self.config.cifx_base_dir.is_dir() or next(self.config.cifx_base_dir.iterdir(), None) is not None:
                raise RequestNotSent(
                    "cifX requires an empty dedicated base directory; driver configuration loading refused"
                )
            self._library = load_library(str(self.config.cifx_library))
            self._device = self._library.cifXFindDevice(self.config.cifx_card_number, 1)
            if not self._device:
                raise RequestNotSent("Selected cifX card unavailable or already owned")
            device = self._device.contents
            if device.uio_num < 0 or device.uio_fd < 0 or device.force_ram:
                raise RequestNotSent("cifX requires a locked UIO device without force_ram")
            mode = self._startup_reader(device.uio_num).split(",")
            if len(mode) < 2 or mode[1] != "donttouch":
                raise RequestNotSent("Selected UIO card is not commissioned in donttouch mode; initialization refused")
            attempt.check()
            settings = LinuxInit()
            settings.init_options = 0  # NOSCAN; the selected descriptor remains locked.
            settings.base_dir = str(self.config.cifx_base_dir).encode()
            settings.user_card_cnt = 1
            settings.user_cards = self._device
            settings.fEnableCardLocking = 1
            settings.logfd = stderr_file()
            # The API has no initialization timeout. Its worker retains ownership
            # after cancellation; no second init/reset is attempted.
            self._initialized = True  # deinit also cleans partially initialized toolkit state
            self._call("cifXDriverInit", C.byref(settings))
            attempt.check()
            self._call("xDriverOpen", C.byref(self._driver))
            if not self._driver.value:
                raise RequestNotSent("cifX returned an empty driver handle")
            self._call("xChannelOpen", self._driver, b"cifX0", self.config.cifx_channel, C.byref(self._channel))
            if not self._channel.value:
                raise RequestNotSent("cifX returned an empty channel handle")
            self._state(attempt)
        except Exception as exc:
            self._faulted = True
            self._close()
            if isinstance(exc, RequestNotSent):
                raise
            if isinstance(exc, UnsupportedLibraryError):
                raise RequestNotSent(str(exc)) from exc
            raise RequestNotSent(f"cifX initialization unavailable ({type(exc).__name__})") from exc

    def _close(self):
        # Called only under this backend's operation lease, never by a cancelled
        # caller while its worker is still in native code.
        try:
            if self._library is not None:
                if self._channel.value:
                    self._library.xChannelClose(self._channel)
                if self._driver.value:
                    self._library.xDriverClose(self._driver)
                if self._initialized:
                    self._library.cifXDriverDeinit()
                if self._device:
                    self._library.cifXDeleteDevice(self._device)
        finally:
            self._channel = HANDLE()
            self._driver = HANDLE()
            self._device = None
            self._initialized = False
            if self._owns_lease:
                self._owns_lease = False
                _DRIVER_LEASE.release()

    def shutdown(self):
        self.closing = True
        if self._operation_lock.acquire(blocking=False):
            try:
                self._close()
            finally:
                self._operation_lock.release()

    def _state(self, attempt):
        attempt.check()
        info = ChannelInfo()
        self._call("xChannelInfo", self._channel, C.sizeof(info), C.byref(info))
        if info.fw_name_length > 63:
            raise RequestNotSent("Invalid cifX firmware identity length")
        name = bytes(info.fw_name[: info.fw_name_length]).decode("ascii", errors="replace")
        version = (info.fw_major, info.fw_minor, info.fw_build, info.fw_revision)
        if (
            info.device_number != self.map.device_number
            or info.serial_number != self.map.serial_number
            or name != self.map.firmware_name
            or version != self.map.firmware_version
        ):
            raise RequestNotSent("cifX card/firmware identity differs from the commissioned map")
        # READY | RUN | BUS_ON and NCF_COMMUNICATING, not merely one open channel.
        if (
            info.channel_error
            or info.device_cos_flags & 7 != 7
            or not info.netx_flags & 1
            or info.device_cos_flags & 0xB0
        ):
            raise RequestNotSent("cifX channel is not running and communicating without error")
        if info.open_count != 1:
            raise RequestNotSent("cifX channel is not exclusively owned")
        bus = U32()
        self._call("xChannelBusState", self._channel, 2, C.byref(bus), 0)  # GETSTATE only
        if bus.value != 1:
            raise RequestNotSent("cifX bus is not already on; MCP does not start it")
        for direction, size, count in (
            (1, self.map.input_size, info.input_areas),
            (2, self.map.output_size, info.output_areas),
        ):
            if not size:
                continue
            if count != 1:
                raise RequestNotSent("cifX requires exactly one commissioned I/O area per direction")
            io = IOInfo()
            self._call("xChannelIOInfo", self._channel, direction, 0, C.sizeof(io), C.byref(io))
            if io.total_size != size or io.mode not in (0, 4):
                raise RequestNotSent("cifX process-image size or buffered host-controlled mode differs from map")
        attempt.check()

    def _snapshot(self, attempt):
        self._state(attempt)
        buffer = (C.c_uint8 * self.map.input_size)()
        # First read requests an update; second waits for the next firmware
        # snapshot. An idle DPM image from a previous tool call is never returned.
        self._call("xChannelIORead", self._channel, 0, 0, len(buffer), buffer, attempt.timeout_ms())
        started = time.monotonic()
        self._call("xChannelIORead", self._channel, 0, 0, len(buffer), buffer, attempt.timeout_ms())
        self._state(attempt)
        age = (time.monotonic() - started) * 1000
        if age > self.map.max_age_ms:
            raise RequestNotSent("cifX process-image freshness budget exceeded")
        return bytes(buffer), started

    def _quality(self, snapshot, mapping):
        offset = (mapping.ar - 1) // 8
        mask = 1 << ((mapping.ar - 1) % 8)
        base = self.map.bit_list_offset + offset
        if not snapshot[base] & mask or not snapshot[base + 16] & mask or snapshot[base + 32] & mask:
            raise RequestNotSent("cifX AR is unconfigured, inactive, or reports diagnosis/configuration difference")
        # Good bit is bit 7 for PROFINET IOPS / IOCS. Lower bits identify the source.
        status = snapshot[mapping.status_offset]
        if not status & 0x80:
            raise RequestNotSent("cifX submodule provider/consumer quality is bad or unknown")
        return status

    async def _run(self, operation, *, write=False):
        if not self._operation_lock.acquire(blocking=False):
            raise RequestNotSent("A previous cifX native attempt still owns the channel")
        attempt = Attempt(self, call_not_after(self.deadline()), call_cancelled)
        try:
            with anyio.fail_after(max(0, attempt.deadline - time.monotonic())):
                return await anyio.to_thread.run_sync(lambda: attempt.run(operation), abandon_on_cancel=True)
        except TimeoutError:
            if write and attempt.write_invoked:
                raise OutcomeUnknown("cifX write timed out after invocation; outcome unknown, no replay") from None
            raise RequestNotSent("cifX call timed out before an output write was invoked") from None
        finally:
            attempt.abandon()

    def _mapping(self, device, slot, subslot, direction, length):
        try:
            return self.map.resolve(device, slot, subslot, direction, length)
        except ConfigError as exc:
            raise RequestNotSent(str(exc)) from exc

    async def read_io(self, device, slot, subslot, length):
        mapping = self._mapping(device, slot, subslot, "input", length)

        def read(attempt):
            self._open(attempt)
            snapshot, started = self._snapshot(attempt)
            status = self._quality(snapshot, mapping)
            return snapshot[mapping.offset : mapping.offset + length], {
                "backend": self.name,
                "complete": True,
                "quality": "good",
                "iops": status,
                "freshness": "new_firmware_handshake",
                "age_ms": round((time.monotonic() - started) * 1000, 3),
                "device_verified": True,
                "ar": mapping.ar,
            }

        return await self._run(read)

    async def write_io(self, device, slot, subslot, data):
        if not self.config.writes_enabled:
            raise RequestNotSent("cifX output writes are disabled")
        mapping = self._mapping(device, slot, subslot, "output", len(data))

        def write(attempt):
            self._open(attempt)
            snapshot, started = self._snapshot(attempt)
            status = self._quality(snapshot, mapping)
            buffer = (C.c_uint8 * (len(data) + 1)).from_buffer_copy(data + bytes([0x80]))
            timeout = attempt.timeout_ms()
            if (time.monotonic() - started) * 1000 > self.map.max_age_ms:
                raise RequestNotSent("cifX output quality snapshot expired before write")
            attempt.begin_write()
            code = self._library.xChannelIOWrite(self._channel, 0, mapping.offset, len(buffer), buffer, timeout)
            if code != 0:
                raise OutcomeUnknown(
                    f"cifX output invocation returned 0x{code & 0xFFFFFFFF:08x}; outcome unknown, no replay"
                )
            return {"written_bytes": len(data), "raw_data_hex": data.hex()}, {
                "backend": self.name,
                "outcome": "written",
                "request_sent": True,
                "confirmation_scope": "controller_process_image",
                "device_applied": None,
                "quality": "good",
                "iocs": status,
                "ar": mapping.ar,
            }

        return await self._run(write, write=True)

    async def probe(self):
        # Status inspection never initializes the driver or changes its ownership.
        if not self._initialized:
            return {"reachable": None, "detail": "cifX not initialized; ping/status do not initialize hardware"}

        def probe(attempt):
            self._state(attempt)
            return {
                "reachable": True,
                "detail": "Configured cifX controller channel is running; device quality checked per I/O",
            }

        try:
            return await self._run(probe)
        except ProfinetClientError as exc:
            return {"reachable": False, "detail": str(exc)}

    async def discover(self, timeout, *, allow_cached=False):
        raise OperationNotImplemented("cifX does not perform network discovery; use the separate network DCP backend")

    async def identify(self, mac, duration_s):
        raise OperationNotImplemented("cifX does not configure stations; use the separate network DCP backend")

    async def set_name(self, mac, name):
        return await self.identify(mac, 3)

    async def set_ip(self, mac, ip, subnet, gateway):
        return await self.identify(mac, 3)
