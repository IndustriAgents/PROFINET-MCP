from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

import anyio.lowlevel
from pydantic import Field

from profinet_mcp import validation
from profinet_mcp.application.resources import ToolResources
from profinet_mcp.backends import (
    BackendNotConfigured,
    ClientCancelled,
    DeviceRejected,
    OperationNotImplemented,
    OperationRefused,
    OutcomeUnknown,
    ProfinetClientError,
    RateLimited,
    RequestNotSent,
    tool_call,
)
from profinet_mcp.domain.model import (
    Alias,
    ByteValues,
    DataLength,
    DeviceMac,
    DeviceName,
    DeviceRef,
    DiscoveryTimeout,
    Envelope,
    FilePath,
    IdentifySeconds,
    IPv4,
    Slot,
    StationName,
    Subslot,
    log,
    outcome,
)

from ..application.operations import Operations
from .support import LOCAL, READ, WRITE, FastMCP, ToolAnnotations, request_cancel_scope


def register_tools(server: FastMCP, resources: ToolResources) -> None:
    operations = Operations(resources, request_cancel_scope)

    def guarded(fn: Callable[..., Awaitable[Envelope]]) -> Callable[..., Awaitable[Envelope]]:
        """Turn every exception into a ``success: false`` envelope."""
        tool = fn.__name__
        not_sent = outcome(tool, "not_sent", False)

        async def run(scope: Any, args: tuple, kwargs: dict) -> Envelope:
            try:
                with tool_call(cancel_scope=scope):
                    result = await fn(*args, **kwargs)
            except validation.InvalidArgument as exc:
                return operations.err(tool, f"Invalid arguments: {exc}", not_sent)
            except RateLimited as exc:
                retry = round(exc.retry_after, 1)
                return operations.err(
                    tool, str(exc), operations.backend_meta(refused=True, retry_after_s=retry, **not_sent)
                )
            except OperationRefused as exc:
                return operations.err(tool, str(exc), operations.backend_meta(refused=True, **not_sent))
            except OperationNotImplemented as exc:
                return operations.err(tool, str(exc), operations.backend_meta(not_implemented=True, **not_sent))
            except ClientCancelled as exc:
                log.warning("%s: %s", tool, exc)
                return operations.err(tool, str(exc), operations.backend_meta(**not_sent))
            except (BackendNotConfigured, RequestNotSent) as exc:
                return operations.err(tool, str(exc), operations.backend_meta(**not_sent))
            except DeviceRejected as exc:
                return operations.err(tool, str(exc), operations.backend_meta(**outcome(tool, "rejected", True)))
            except OutcomeUnknown as exc:
                return operations.err(
                    tool,
                    str(exc),
                    operations.backend_meta(outcome_unknown=True, **outcome(tool, "unknown", exc.request_sent)),
                )
            except ProfinetClientError as exc:
                return operations.err(tool, str(exc), operations.backend_meta(**outcome(tool, "unknown", None)))
            except Exception as exc:
                log.exception("Tool %s failed", tool)
                return operations.err(
                    tool, f"Internal error: {type(exc).__name__}: {exc}", outcome(tool, "unknown", None)
                )
            if result.get("success"):
                result["meta"].update(outcome(tool, "written", True))
            return result

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Envelope:
            scope = operations.cancel_scope()
            envelope_ = await run(scope, args, kwargs)
            if getattr(scope, "cancel_called", False):
                log.warning("%s: the MCP client cancelled the call; %s", tool, envelope_.get("error") or "done")
                await anyio.lowlevel.checkpoint_if_cancelled()
            return envelope_

        return wrapper

    def tool(annotations: ToolAnnotations) -> Callable[[Callable[..., Awaitable[Envelope]]], Any]:
        def decorate(fn: Callable[..., Awaitable[Envelope]]) -> Any:
            return server.tool(annotations=annotations)(guarded(fn))

        return decorate

    @tool(READ)
    async def discover_devices(timeout: DiscoveryTimeout = None) -> Envelope:
        """Discover PROFINET IO devices.

        Network backend: multicasts a DCP Identify All request on PROFINET_INTERFACE and lists every
        device that answers within `timeout` seconds (needs raw-socket privileges). Mock backend: lists
        the devices simulated by profinet-mock-server. Each device reports device_name, mac_address,
        ip_address, subnet_mask, gateway, vendor_id, device_id, device_type and type_of_station where
        known (null otherwise). meta.backend says which backend answered.
        """
        return await operations.discover_devices(timeout)

    @tool(READ)
    async def get_device_info(device_name: DeviceRef) -> Envelope:
        """Discovery details for one device, looked up by station name, IP address or MAC address.

        Runs discovery (see discover_devices) and returns the matching device, or success=false if no
        device with that name or address answered.
        """
        return await operations.get_device_info(device_name)

    @tool(READ)
    async def test_device_communication(device_name: DeviceRef) -> Envelope:
        """Check that a device answers, by station name, IP address or MAC address.

        Uses discovery (DCP Identify on the network backend), so it works for any IO device without
        knowing its modules. Returns reachable=true with the round trip in meta.duration_ms, or
        success=false if the device did not answer.
        """
        return await operations.test_device_communication(device_name)

    @tool(WRITE)
    async def identify_device(device_mac: DeviceMac, duration_s: IdentifySeconds = 3) -> Envelope:
        """Make a device flash its identification LED (DCP Set Signal) so it can be found in the cabinet.

        A DCP configuration command: refused unless both PROFINET_WRITES_ENABLED=true and
        PROFINET_CONFIG_CMDS_ENABLED=true, like set_device_name. Live DCP sends one flash sequence
        (duration_s=3 only); the device controls physical LED timing. Mock supports arbitrary durations.
        """
        return await operations.identify_device(device_mac, duration_s)

    @tool(WRITE)
    async def set_device_name(device_mac: DeviceMac, name: StationName) -> Envelope:
        """Assign a new station name (DCP Set NameOfStation) to the device with this MAC address.

        Refused unless both PROFINET_WRITES_ENABLED=true and PROFINET_CONFIG_CMDS_ENABLED=true:
        renaming a device in a running plant takes it away from the IO controller that expects it.
        The name must follow the PROFINET rules (lowercase a-z, 0-9, '-', dot-separated labels of
        1-63 characters, at most 240 in total). Network DCP requests permanent storage and requires
        an explicit unicast MAC. Acknowledgement does not verify physical completion.
        """
        return await operations.set_device_name(device_mac, name)

    @tool(WRITE)
    async def set_device_ip(
        device_mac: DeviceMac,
        ip_address: IPv4,
        subnet_mask: IPv4,
        gateway: Annotated[str | None, Field(description="Default gateway; omit or 0.0.0.0 for none")] = None,
    ) -> Envelope:
        """Assign IP address, subnet mask and gateway (DCP Set IP) to the device with this MAC address.

        Refused unless both PROFINET_WRITES_ENABLED=true and PROFINET_CONFIG_CMDS_ENABLED=true. The
        gateway, if given, must be inside the subnet. Network DCP requests permanent storage and
        requires an explicit unicast MAC. An unknown result must not be automatically retried.
        """
        return await operations.set_device_ip(device_mac, ip_address, subnet_mask, gateway)

    @tool(READ)
    async def read_io_data(device_name: DeviceName, slot: Slot, subslot: Subslot, data_length: DataLength) -> Envelope:
        """Read I/O data from a slot/subslot of a device; returns raw_data_hex and the byte values.

        Supported by the mock backend. The network backend returns success=false: cyclic PROFINET IO
        needs an IO controller with an application relationship, which this server does not have.
        """
        return await operations.read_io_data(device_name, slot, subslot, data_length)

    @tool(WRITE)
    async def write_io_data(device_name: DeviceName, slot: Slot, subslot: Subslot, data: ByteValues) -> Envelope:
        """Write bytes to an output slot/subslot of a device. This changes physical outputs.

        Refused unless PROFINET_WRITES_ENABLED=true (the server is read-only by default). Supported by
        the mock backend; the network backend returns success=false without sending anything.
        """
        return await operations.write_io_data(device_name, slot, subslot, data)

    @tool(LOCAL)
    async def list_devices() -> Envelope:
        """List the aliases defined in DEVICE_MAP_FILE (device name, address, slot, subslot, length).

        Reads only the local file; no device is contacted.
        """
        return await operations.list_devices()

    @tool(READ)
    async def read_device_by_alias(alias: Alias) -> Envelope:
        """read_io_data for the device, slot, subslot and length behind an alias from DEVICE_MAP_FILE."""
        return await operations.read_device_by_alias(alias)

    @tool(WRITE)
    async def write_device_by_alias(alias: Alias, data: ByteValues) -> Envelope:
        """write_io_data for the device, slot and subslot behind an alias from DEVICE_MAP_FILE.

        Refused unless PROFINET_WRITES_ENABLED=true, like write_io_data.
        """
        return await operations.write_device_by_alias(alias, data)

    @tool(LOCAL)
    async def load_gsd_file(filepath: FilePath) -> Envelope:
        """Parse a GSDML device description file: device identity, access points and modules.

        Returns metadata (vendor, vendor/device IDs, device name, GSDML version), the device access
        points with their usable modules, every module with its fixed submodules and its pluggable
        submodules (resolved from the SubmoduleList; unresolved references are flagged and the module
        gets submodules_incomplete=true), and input/output sizes in bytes (null when a data type's size
        is unknown). Relative paths are also looked up in
        PROFINET_GSD_PATH. Files are cached and re-parsed when they change on disk.
        """
        return await operations.load_gsd_file(filepath)

    @tool(LOCAL)
    async def ping() -> Envelope:
        """Server health and settings: version, backend, connection settings and safety switches.

        Does not contact any device; use get_connection_status to check the backend.
        """
        return await operations.ping()

    @tool(READ)
    async def get_connection_status() -> Envelope:
        """Connection settings plus a reachability check of the backend.

        Mock backend: opens and closes a TCP connection to the mock (reachable true/false). Network
        backend: not checked (reachable null) - run discover_devices instead. No backend: reachable null.
        """
        return await operations.get_connection_status()

    from .contract import install_contract

    install_contract(server)
