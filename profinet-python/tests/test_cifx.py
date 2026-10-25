"""Native ABI, commissioned image, cancellation and MCP tests; no hardware access."""

from __future__ import annotations

import ctypes as C
import json
import os
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import anyio
import pytest

from profinet_mcp.adapters.cifx import CifxBackend
from profinet_mcp.adapters.cifx_abi import ChannelInfo, Device, DmaBuffer, IOInfo, LinuxInit
from profinet_mcp.backends import OutcomeUnknown, RequestNotSent
from profinet_mcp.config import ConfigError, ProfinetConfig
from profinet_mcp.domain.cifx_map import ProcessMap
from profinet_mcp.pn_client import ProfinetClient
from profinet_mcp.server import ProfinetMCPServer

pytestmark = pytest.mark.anyio


def commissioned_map():
    return {
        "version": 1,
        "device_number": 1234,
        "serial_number": 5678,
        "firmware_name": "PROFINET IO Controller",
        "firmware_version": [3, 2, 0, 0],
        "input_size": 64,
        "output_size": 64,
        "bit_list_offset": 16,
        "max_age_ms": 1000,
        "mappings": [
            {
                "device_name": "line1-io",
                "slot": 1,
                "subslot": 1,
                "ar": 1,
                "direction": "input",
                "offset": 0,
                "length": 2,
                "status_offset": 2,
            },
            {
                "device_name": "line1-io",
                "slot": 1,
                "subslot": 1,
                "ar": 1,
                "direction": "output",
                "offset": 0,
                "length": 2,
                "status_offset": 3,
            },
        ],
    }


@pytest.fixture(scope="session")
def shim_path(tmp_path_factory):
    compiler = shutil.which("cc")
    assert compiler, "cifX ABI regression requires a C compiler (no hardware/driver required)"
    output = tmp_path_factory.mktemp("cifx") / "libcifx-test.so"
    command = [
        compiler,
        "-std=c11",
        "-D_POSIX_C_SOURCE=200809L",
        "-shared",
        "-fPIC",
        str(Path(__file__).parent / "native/cifx_shim.c"),
        "-o",
        str(output),
    ]
    # Optional independent check against the actual pinned vendor headers.
    if source := os.environ.get("CIFX_SDK_HEADERS"):
        command += [
            "-DCIFX_SDK_HEADERS",
            "-I",
            str(Path(source) / "libcifx"),
            "-I",
            str(Path(source) / "libcifx/Toolkit/Common/cifXAPI"),
        ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    return output


@pytest.fixture
def native(tmp_path, shim_path):
    library = C.CDLL(str(shim_path))
    library.shim_layout.argtypes = [C.c_int]
    library.shim_layout.restype = C.c_size_t
    library.shim_reset()
    path = tmp_path / "image.json"
    path.write_text(json.dumps(commissioned_map()))
    base = tmp_path / "empty-driver-config"
    base.mkdir()
    config = ProfinetConfig(
        backend="cifx",
        cifx_library=shim_path,
        cifx_map_path=path,
        cifx_base_dir=base,
        cifx_card_number=2,
        cifx_init_enabled=True,
        writes_enabled=True,
        timeout=1,
    )
    backend = CifxBackend(config, startup_reader=lambda _: "netx,donttouch,test")
    yield backend, library
    library.shim_set(21, 1)
    backend.shutdown()
    # Wait for an abandoned native worker to clean up before resetting its fixture.
    end = time.monotonic() + 2
    while backend._initialized and time.monotonic() < end:
        time.sleep(0.001)
    assert not backend._initialized


def test_abi_layouts_match_compiled_c(native):
    _, lib = native
    layouts = [
        C.sizeof(LinuxInit),
        C.sizeof(Device),
        C.sizeof(ChannelInfo),
        C.sizeof(IOInfo),
        C.sizeof(DmaBuffer),
        LinuxInit.user_cards.offset,
        LinuxInit.logfd.offset,
        Device.uio_num.offset,
        Device.force_ram.offset,
        Device.dma_buffer.offset,
        Device.hwif_write.offset,
        ChannelInfo.fw_name_length.offset,
        ChannelInfo.channel_error.offset,
        ChannelInfo.device_cos_flags.offset,
    ]
    assert layouts == [lib.shim_layout(i) for i in range(len(layouts))]


async def test_real_ctypes_read_discards_old_image_and_write_has_one_call(native):
    backend, lib = native
    data, meta = await backend.read_io("line1-io", 1, 1, 2)
    assert data == b"\x01\x02" and meta["quality"] == "good"
    assert lib.shim_get(1) == 2 and lib.shim_get(2) == 1
    result, meta = await backend.write_io("line1-io", 1, 1, b"\x09\x08")
    assert result["written_bytes"] == 2
    assert lib.shim_get(0) == 1 and [lib.shim_output(i) for i in range(3)] == [9, 8, 128]
    assert meta["confirmation_scope"] == "controller_process_image" and meta["device_applied"] is None
    assert lib.shim_get(6) == 0


async def test_status_never_initializes_even_when_opted_in(native):
    backend, lib = native
    assert (await backend.probe())["reachable"] is None
    assert backend.describe()["cifx"]["initialized"] is False
    assert lib.shim_get(5) == 0


@pytest.mark.parametrize(
    "key,value",
    [(10, 0), (12, 0), (13, 0), (14, 1), (15, -1), (30, 42), (31, 0), (32, 3), (33, 63), (34, 2), (35, 3), (36, 1)],
)
async def test_bad_unknown_or_mismatched_input_is_never_returned(native, key, value):
    backend, lib = native
    lib.shim_set(key, value)
    with pytest.raises(RequestNotSent):
        await backend.read_io("line1-io", 1, 1, 2)
    assert lib.shim_get(0) == 0


@pytest.mark.parametrize("key,value", [(11, 0), (12, 0), (13, 0), (14, 1), (31, 0)])
async def test_output_quality_refuses_before_native_write(native, key, value):
    backend, lib = native
    lib.shim_set(key, value)
    with pytest.raises(RequestNotSent):
        await backend.write_io("line1-io", 1, 1, b"ab")
    assert lib.shim_get(0) == 0


async def test_native_write_error_is_unknown_and_never_replayed(native):
    backend, lib = native
    lib.shim_set(16, -1)
    with pytest.raises(OutcomeUnknown):
        await backend.write_io("line1-io", 1, 1, b"ab")
    assert lib.shim_get(0) == 1 and lib.shim_output(0) == ord("a")


@pytest.mark.parametrize("startup", ["netx,auto,test", "netx,ram", "netx", "netx,donttouchX"])
async def test_non_donttouch_card_released_before_initialization(native, startup):
    backend, lib = native
    backend._startup_reader = lambda _: startup
    with pytest.raises(RequestNotSent, match="donttouch"):
        await backend.read_io("line1-io", 1, 1, 2)
    assert lib.shim_get(3) == 1 and lib.shim_get(2) == 0
    with pytest.raises(RequestNotSent, match="restart"):
        await backend.read_io("line1-io", 1, 1, 2)
    assert lib.shim_get(5) == 1


async def test_missing_startup_file_releases_card(native):
    backend, lib = native

    def missing(_):
        raise FileNotFoundError

    backend._startup_reader = missing
    with pytest.raises(RequestNotSent):
        await backend.read_io("line1-io", 1, 1, 2)
    assert lib.shim_get(3) == 1 and lib.shim_get(2) == 0


async def test_failed_partial_initialization_is_cleaned_without_retry(native):
    backend, lib = native
    lib.shim_set(37, -1)
    with pytest.raises(RequestNotSent):
        await backend.read_io("line1-io", 1, 1, 2)
    assert lib.shim_get(4) == 1 and lib.shim_get(3) == 1


async def test_initialization_and_write_gates_are_enforced_in_backend(native):
    backend, lib = native
    backend.config = replace(backend.config, cifx_init_enabled=False)
    with pytest.raises(RequestNotSent):
        await backend.read_io("line1-io", 1, 1, 2)
    assert lib.shim_get(5) == 0
    backend.config = replace(backend.config, cifx_init_enabled=True, writes_enabled=False)
    with pytest.raises(RequestNotSent):
        await backend.write_io("line1-io", 1, 1, b"ab")
    assert lib.shim_get(5) == 0


async def test_cancelled_native_worker_keeps_ownership_and_prevents_late_write(native):
    backend, lib = native
    lib.shim_set(17, 1)
    with anyio.move_on_after(0.08):
        await backend.write_io("line1-io", 1, 1, b"ab")
    assert lib.shim_get(19) == 1
    with pytest.raises(RequestNotSent, match="still owns"):
        await backend.write_io("line1-io", 1, 1, b"cd")
    backend.shutdown()
    assert lib.shim_get(4) == 0  # never deinit a session still in native code
    lib.shim_set(21, 1)
    with anyio.fail_after(2):
        while backend._initialized:
            await anyio.sleep(0.001)
    assert lib.shim_get(0) == 0 and lib.shim_get(4) == 1 and lib.shim_get(3) == 1


async def test_write_deadline_retains_worker_and_reports_unknown(native):
    backend, lib = native
    backend.config = replace(backend.config, timeout=0.08)
    lib.shim_set(18, 1)
    with pytest.raises(OutcomeUnknown):
        await backend.write_io("line1-io", 1, 1, b"ab")
    assert lib.shim_get(0) == 1
    with pytest.raises(RequestNotSent, match="still owns"):
        await backend.write_io("line1-io", 1, 1, b"cd")
    lib.shim_set(21, 1)


async def test_expired_snapshot_cannot_authorize_write(native):
    backend, lib = native
    backend.map = replace(backend.map, max_age_ms=1)
    original = backend._quality

    def slow_quality(*args):
        status = original(*args)
        time.sleep(0.01)
        return status

    backend._quality = slow_quality
    with pytest.raises(RequestNotSent, match="expired"):
        await backend.write_io("line1-io", 1, 1, b"ab")
    assert lib.shim_get(0) == 0


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(input_size=16),
        lambda d: d.update(bit_list_offset=17),
        lambda d: d.update(version=True),
        lambda d: d.update(firmware_version=[3, 3, 0, 0]),
        lambda d: d.update(extra=True),
        lambda d: d["mappings"][0].update(direction="both"),
        lambda d: d["mappings"][0].update(offset=16),
        lambda d: d["mappings"][0].update(status_offset=1),
        lambda d: d["mappings"][1].update(status_offset=2),
        lambda d: d["mappings"][1].update(offset=63),
        lambda d: d["mappings"][1].update(ar=129),
        lambda d: d["mappings"].append(dict(d["mappings"][0])),
    ],
)
def test_invalid_mapping_fails_before_library_access(change):
    data = commissioned_map()
    change(data)
    with pytest.raises((ConfigError, ValueError)):
        ProcessMap.parse(data)


async def test_unmapped_or_wrong_direction_and_length_never_initializes(native):
    backend, lib = native
    with pytest.raises(RequestNotSent):
        await backend.read_io("unknown", 1, 1, 2)
    with pytest.raises(RequestNotSent):
        await backend.write_io("line1-io", 1, 1, b"abc")
    assert lib.shim_get(5) == 0


async def test_real_mcp_wire_uses_cifx_result_schemas(native):
    from mcp.shared.memory import create_connected_server_and_client_session

    backend, lib = native
    server = ProfinetMCPServer(client=ProfinetClient(backend.config, backend))
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as session:
        result = await session.call_tool("ping", {})
        assert result.structuredContent["data"]["capabilities"]["operations"]["read_io_data"]["supported"]
        assert lib.shim_get(5) == 0
        result = await session.call_tool(
            "read_io_data", {"target": "address", "device_name": "line1-io", "slot": 1, "subslot": 1, "data_length": 2}
        )
        assert not result.isError, result
        assert result.structuredContent["data"]["bytes"] == [1, 2]
        result = await session.call_tool(
            "write_io_data", {"target": "address", "device_name": "line1-io", "slot": 1, "subslot": 1, "data": [9, 8]}
        )
        assert not result.isError, result
        assert result.structuredContent["meta"]["device_applied"] is None


@pytest.mark.parametrize("flag", [0x10, 0x20, 0x80])
async def test_new_or_restart_required_configuration_refuses_io(native, flag):
    backend, lib = native
    lib.shim_set(42, flag)
    with pytest.raises(RequestNotSent):
        await backend.write_io("line1-io", 1, 1, b"ab")
    assert lib.shim_get(0) == 0


async def test_nonempty_driver_configuration_refused_before_card_access(native):
    backend, lib = native
    (backend.config.cifx_base_dir / "device.conf").write_text("Ethernet=1")
    with pytest.raises(RequestNotSent, match="empty dedicated"):
        await backend.read_io("line1-io", 1, 1, 2)
    assert lib.shim_get(5) == 0


async def test_process_global_driver_lease_prevents_other_instance_init(native):
    backend, lib = native
    await backend.read_io("line1-io", 1, 1, 2)
    other = CifxBackend(backend.config, startup_reader=lambda _: "netx,donttouch,test")
    try:
        with pytest.raises(RequestNotSent, match="process-global"):
            await other.read_io("line1-io", 1, 1, 2)
        assert lib.shim_get(5) == 1
    finally:
        other.shutdown()
    assert lib.shim_get(4) == 0  # the first backend still owns its driver


def test_implicit_env_cannot_enable_cifx_initialization(tmp_path, monkeypatch):
    from profinet_mcp.cli import load_project_dotenv

    monkeypatch.delenv("PROFINET_CIFX_INIT_ENABLED", raising=False)
    path = tmp_path / ".env"
    path.write_text("PROFINET_CIFX_INIT_ENABLED=true\n")
    switches, _ = load_project_dotenv(path)
    assert switches == ["PROFINET_CIFX_INIT_ENABLED"]
    assert "PROFINET_CIFX_INIT_ENABLED" not in os.environ


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"PROFINET_CIFX_LIBRARY": "libcifx.so"},
        {"PROFINET_CIFX_LIBRARY": "/lib/libcifx.so", "PROFINET_CIFX_MAP_FILE": "map.json"},
    ],
)
def test_cifx_configuration_requires_explicit_absolute_paths(settings):
    with pytest.raises(ConfigError):
        ProfinetConfig.from_env({"PROFINET_BACKEND": "cifx", **settings})


@pytest.mark.parametrize("feature", ["VIRTETH_FIXTURE", "TIME_FIXTURE", "DMA_FIXTURE"])
async def test_initialization_side_effect_build_refused_before_card_access(native, tmp_path, feature):
    backend, _ = native
    output = tmp_path / f"libcifx-{feature}.so"
    subprocess.run(
        [
            shutil.which("cc"),
            "-std=c11",
            "-D_POSIX_C_SOURCE=200809L",
            "-shared",
            "-fPIC",
            f"-D{feature}",
            str(Path(__file__).parent / "native/cifx_shim.c"),
            "-o",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    library = C.CDLL(str(output))
    library.shim_reset()
    backend.config = replace(backend.config, cifx_library=output)
    with pytest.raises(RequestNotSent, match="VIRTETH=OFF"):
        await backend.read_io("line1-io", 1, 1, 2)
    assert library.shim_get(5) == 0 and library.shim_get(2) == 0
