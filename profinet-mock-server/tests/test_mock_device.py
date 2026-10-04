"""Tests for the mock PROFINET IO device."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import profinet_mock_device as mock
from profinet_mock_device import MockConfig, MockError, ProfinetMockServer

MAC = "00:11:22:33:44:55"


@pytest.fixture
def server() -> ProfinetMockServer:
    return ProfinetMockServer(MockConfig(update_interval=3600))


def ok(response: dict) -> dict:
    assert response["success"] is True, response
    return response["data"]


def fails(response: dict, fragment: str) -> None:
    assert response["success"] is False, response
    assert fragment.lower() in response["error"].lower(), response


# -- default device ----------------------------------------------------------


def test_discover_lists_default_device(server: ProfinetMockServer) -> None:
    (device,) = ok(server.handle_request({"op": "discover"}))
    assert device["device_name"] == "pn-device-01"
    assert mock.check_station_name(device["device_name"])  # the default obeys the rules set_name enforces
    assert device["mac_address"] == MAC
    assert device["vendor"] == "MockVendor"
    assert {(a["slot"], a["subslot"], a["direction"]) for a in device["io_areas"]} == {
        (1, 1, "input"),
        (1, 2, "output"),
    }


def test_read_defaults_to_whole_area_and_lookup_ignores_case(server: ProfinetMockServer) -> None:
    data = ok(server.handle_request({"op": "read", "device": "PN-Device-01", "slot": 1, "subslot": 1}))
    assert data["raw_data_hex"] == "00" * 8
    assert data["device_name"] == "pn-device-01"
    assert data["direction"] == "input"


def test_write_then_read_back_output(server: ProfinetMockServer) -> None:
    wrote = ok(
        server.handle_request({"op": "write", "device": "pn-device-01", "slot": 1, "subslot": 2, "data": [1, 2, 3, 4]})
    )
    assert wrote["written_bytes"] == 4
    back = ok(server.handle_request({"op": "read", "device": "pn-device-01", "slot": 1, "subslot": 2, "length": 4}))
    assert back["raw_data_hex"] == "01020304"


# -- validation: bad requests never crash the handler ----------------------------


@pytest.mark.parametrize(
    ("request_", "fragment"),
    [
        ([1, 2, 3], "JSON object"),
        ("discover", "JSON object"),
        ({"op": "reboot"}, "Unknown op"),
        ({"op": None}, "Unknown op"),
        ({"op": "read", "device": "NOPE", "slot": 1, "subslot": 1}, "not found"),
        ({"op": "read", "device": "pn-device-01", "slot": "x", "subslot": 1}, "slot must be an integer"),
        ({"op": "read", "device": "pn-device-01", "slot": True, "subslot": 1}, "slot must be an integer"),
        ({"op": "read", "device": "pn-device-01", "slot": 1, "subslot": 0}, "subslot must be between"),
        ({"op": "read", "device": "pn-device-01", "slot": 0, "subslot": 1}, "not found"),
        ({"op": "read", "device": "pn-device-01", "slot": 1, "subslot": 1, "length": 9}, "cannot read 9"),
        ({"op": "read", "device": "pn-device-01", "slot": 1, "subslot": 1, "length": 0}, "length must be between"),
    ],
)
def test_bad_requests_return_errors(server: ProfinetMockServer, request_, fragment: str) -> None:
    fails(server.handle_request(request_), fragment)


def test_write_rejects_oversized_payload_without_growing_buffer(server: ProfinetMockServer) -> None:
    fails(
        server.handle_request({"op": "write", "device": "pn-device-01", "slot": 1, "subslot": 2, "data": [1] * 5}),
        "cannot write 5",
    )
    area = server.devices["pn-device-01"].areas["1:2"]
    assert len(area.buffer) == 4


def test_write_rejects_out_of_range_bytes_and_input_areas(server: ProfinetMockServer) -> None:
    fails(
        server.handle_request({"op": "write", "device": "pn-device-01", "slot": 1, "subslot": 2, "data": [256]}),
        "between 0 and 255",
    )
    fails(
        server.handle_request({"op": "write", "device": "pn-device-01", "slot": 1, "subslot": 1, "data": [1]}),
        "input area",
    )
    fails(server.handle_request({"op": "write", "device": "pn-device-01", "slot": 1, "subslot": 2, "data": []}), "list")


# -- DCP-style operations --------------------------------------------------------


def test_identify_marks_device_identifying(server: ProfinetMockServer) -> None:
    data = ok(server.handle_request({"op": "identify", "mac": MAC.upper().replace(":", "-"), "duration_s": 3}))
    assert data == {"device_name": "pn-device-01", "mac_address": MAC, "identify_duration_s": 3}
    (device,) = ok(server.handle_request({"op": "discover"}))
    assert device["identifying"] is True
    fails(server.handle_request({"op": "identify", "mac": "00:00:00:00:00:01"}), "No device with MAC")
    fails(server.handle_request({"op": "identify", "mac": "not-a-mac"}), "Invalid MAC")


def test_set_name_renames_device(server: ProfinetMockServer) -> None:
    data = ok(server.handle_request({"op": "set_name", "mac": MAC, "name": "line1-io.cell-2"}))
    assert data == {"mac_address": MAC, "device_name": "line1-io.cell-2", "previous_name": "pn-device-01"}
    ok(server.handle_request({"op": "read", "device": "line1-io.cell-2", "slot": 1, "subslot": 1}))
    fails(server.handle_request({"op": "read", "device": "pn-device-01", "slot": 1, "subslot": 1}), "not found")


@pytest.mark.parametrize(
    ("name", "fragment"),
    [
        ("", "non-empty"),
        ("Upper", "lowercase"),
        ("-leading", "must not start or end"),
        ("under_score", "may only contain"),
        ("a..b", "1-63"),
        ("x" * 64, "1-63"),
        ("port-001", "port-xyz"),
        ("port-001-00002", "port-xyz"),
        ("192.168.1.1", "IP address"),
        (".".join(["abcdefgh"] * 30), "240"),
    ],
)
def test_set_name_enforces_station_name_rules(server: ProfinetMockServer, name: str, fragment: str) -> None:
    fails(server.handle_request({"op": "set_name", "mac": MAC, "name": name}), fragment)


def test_set_ip_updates_and_validates(server: ProfinetMockServer) -> None:
    data = ok(
        server.handle_request(
            {"op": "set_ip", "mac": MAC, "ip": "10.0.0.20", "subnet": "255.255.255.0", "gateway": "10.0.0.1"}
        )
    )
    assert (data["ip_address"], data["subnet_mask"], data["gateway"]) == ("10.0.0.20", "255.255.255.0", "10.0.0.1")
    fails(server.handle_request({"op": "set_ip", "mac": MAC, "ip": "10.0.0.300", "subnet": "255.255.255.0"}), "IPv4")
    fails(server.handle_request({"op": "set_ip", "mac": MAC, "ip": "10.0.0.20", "subnet": "255.0.255.0"}), "contiguous")
    fails(
        server.handle_request(
            {"op": "set_ip", "mac": MAC, "ip": "10.0.0.20", "subnet": "255.255.255.0", "gateway": "10.0.1.1"}
        ),
        "not in",
    )
    fails(
        server.handle_request({"op": "set_ip", "mac": MAC, "ip": "10.0.0.255", "subnet": "255.255.255.0"}), "broadcast"
    )


# -- configurable devices ----------------------------------------------------------


def test_devices_file_defines_custom_devices(tmp_path) -> None:
    spec = {
        "devices": [
            {
                "name": "dap-test",
                "mac": "02:00:00:00:00:01",
                "ip": "10.1.1.10",
                "areas": [
                    {"slot": 0, "subslot": 1, "length": 2, "direction": "input"},
                    {"slot": 3, "subslot": 1, "length": 16, "direction": "output"},
                ],
            },
            {"name": "second", "mac": "02:00:00:00:00:02", "ip": "10.1.1.11"},
        ]
    }
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    server = ProfinetMockServer(MockConfig(), mock.load_devices_file(path))
    names = [d["device_name"] for d in ok(server.handle_request({"op": "discover"}))]
    assert names == ["dap-test", "second"]
    data = ok(server.handle_request({"op": "read", "device": "dap-test", "slot": 0, "subslot": 1}))
    assert data["raw_data_hex"] == "0000"


@pytest.mark.parametrize(
    ("spec", "fragment"),
    [
        ([], "non-empty list"),
        ([{"name": "a", "mac": "zz", "ip": "10.0.0.1"}], "Invalid MAC"),
        ([{"name": "PN-DEVICE-01", "mac": "02:00:00:00:00:01", "ip": "10.0.0.1"}], "lowercase"),
        ([{"name": "bad_name", "mac": "02:00:00:00:00:01", "ip": "10.0.0.1"}], "may only contain"),
        ([{"name": "a", "mac": "02:00:00:00:00:01", "ip": "nope"}], "IPv4"),
        (
            [{"name": "a", "mac": "02:00:00:00:00:01", "ip": "10.0.0.1"}] * 2,
            "unique",
        ),
        (
            [
                {
                    "name": "a",
                    "mac": "02:00:00:00:00:01",
                    "ip": "10.0.0.1",
                    "areas": [{"slot": 1, "subslot": 1, "length": 2000}],
                }
            ],
            "length must be between",
        ),
    ],
)
def test_bad_devices_files_are_rejected(spec, fragment: str) -> None:
    with pytest.raises(MockError, match=fragment):
        mock.build_devices(spec)


# -- configuration is read at run time, after load_dotenv ---------------------------


def test_main_reads_settings_from_dotenv(tmp_path, monkeypatch, capsys) -> None:
    for key in ("MOCK_PROFINET_PORT", "MOCK_PROFINET_UPDATE_INTERVAL", "MOCK_PROFINET_DEVICES_FILE", "LD_PRELOAD"):
        # set + delete so monkeypatch removes whatever load_dotenv() adds
        monkeypatch.setenv(key, "placeholder")
        monkeypatch.delenv(key)
    devices = tmp_path / "devices.json"
    devices.write_text(json.dumps([{"name": "from-env", "mac": "02:00:00:00:00:09", "ip": "10.0.0.9"}]))
    (tmp_path / ".env").write_text(
        f"MOCK_PROFINET_PORT=5999\nmock_profinet_update_interval=2.5\nMOCK_PROFINET_DEVICES_FILE={devices}\n"
        "LD_PRELOAD=/tmp/evil.so\n"
    )
    monkeypatch.chdir(tmp_path)
    seen = {}

    async def fake_run_server(config, devices=None, *, exit_on_stdin_eof=False):
        seen["config"] = config
        seen["devices"] = devices

    monkeypatch.setattr(mock, "run_server", fake_run_server)
    mock.main([])
    assert seen["config"].port == 5999
    assert seen["config"].update_interval == 2.5
    assert [d.name for d in seen["devices"]] == ["from-env"]
    assert "LD_PRELOAD" not in os.environ  # only MOCK_PROFINET_* is taken from a discovered .env
    assert "ignoring LD_PRELOAD" in capsys.readouterr().err


def test_config_rejects_bad_values() -> None:
    with pytest.raises(mock.ConfigError, match="MOCK_PROFINET_PORT"):
        MockConfig.from_env({"MOCK_PROFINET_PORT": "http"})
    with pytest.raises(mock.ConfigError, match="MOCK_PROFINET_VERBOSE"):
        MockConfig.from_env({"MOCK_PROFINET_VERBOSE": "maybe"})
    assert MockConfig.from_env({}).port == 5600


# -- the real TCP JSON-lines protocol -------------------------------------------------


def test_tcp_round_trip_on_ephemeral_port() -> None:
    async def scenario() -> None:
        server = ProfinetMockServer(MockConfig(host="127.0.0.1", port=0, update_interval=3600))
        await server.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)

        async def ask(payload) -> dict:
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            writer.write(raw + b"\n")
            await writer.drain()
            return json.loads(await asyncio.wait_for(reader.readline(), 10))

        try:
            found = await ask({"op": "discover"})
            assert [d["device_name"] for d in found["data"]] == ["pn-device-01"]
            assert (await ask(b"{not json"))["error"] == "Invalid JSON"
            assert (await ask([1]))["success"] is False
            wrote = await ask({"op": "write", "device": "pn-device-01", "slot": 1, "subslot": 2, "data": [9, 8]})
            assert wrote["data"]["written_bytes"] == 2
            back = await ask({"op": "read", "device": "pn-device-01", "slot": 1, "subslot": 2, "length": 2})
            assert back["data"]["raw_data_hex"] == "0908"
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()

    asyncio.run(scenario())


def test_inputs_change_but_outputs_hold() -> None:
    async def scenario() -> None:
        server = ProfinetMockServer(MockConfig(host="127.0.0.1", port=0, update_interval=0.01))
        device = server.devices["pn-device-01"]
        device.areas["1:2"].buffer[:] = b"\x07\x07\x07\x07"
        await server.start()
        try:
            for _ in range(200):
                await asyncio.sleep(0.01)
                if any(device.areas["1:1"].buffer):
                    break
        finally:
            await server.stop()
        assert any(device.areas["1:1"].buffer), "simulated inputs never changed"
        assert bytes(device.areas["1:2"].buffer) == b"\x07\x07\x07\x07"

    asyncio.run(scenario())


# -- "rejected" means nothing changed; anything else may have ------------------------


def snapshot(server: ProfinetMockServer) -> list:
    return [
        (d.name, d.mac, d.ip, d.subnet, d.gateway, d.identify_until, {k: bytes(a.buffer) for k, a in d.areas.items()})
        for d in server.devices.values()
    ]


@pytest.mark.parametrize(
    "request_",
    [
        {"op": "write", "device": "pn-device-01", "slot": 1, "subslot": 2, "data": [1] * 5},
        {"op": "write", "device": "pn-device-01", "slot": 1, "subslot": 1, "data": [1]},
        {"op": "write", "device": "pn-device-01", "slot": 1, "subslot": 2, "data": [1, 300]},
        {"op": "identify", "mac": MAC, "duration_s": 0},
        {"op": "set_name", "mac": MAC, "name": "Bad_Name"},
        {"op": "set_ip", "mac": MAC, "ip": "10.0.0.20", "subnet": "255.255.255.0", "gateway": "10.9.9.9"},
        {"op": "set_ip", "mac": "02:00:00:00:00:99", "ip": "10.0.0.20", "subnet": "255.255.255.0"},
        {"op": "reboot"},
        [1],
    ],
)
def test_refusals_are_marked_rejected_and_change_nothing(server: ProfinetMockServer, request_) -> None:
    before = snapshot(server)
    response = server.handle_request(request_)
    assert response["success"] is False and response["rejected"] is True, response
    assert snapshot(server) == before


def test_internal_errors_are_not_marked_rejected(server: ProfinetMockServer, monkeypatch) -> None:
    """A failure after the change was applied must not claim that nothing happened."""

    def write_then_fail(request):
        server.devices["pn-device-01"].areas["1:2"].buffer[0] = 9
        raise RuntimeError("disk full")

    monkeypatch.setattr(server, "_op_write", write_then_fail)
    response = server.handle_request({"op": "write", "device": "pn-device-01", "slot": 1, "subslot": 2, "data": [9]})
    assert response["success"] is False and "Internal mock error" in response["error"]
    assert "rejected" not in response


def test_a_broken_console_does_not_fail_identify(server: ProfinetMockServer, monkeypatch) -> None:
    """Regression: identify applied the change, then a failing print turned it into an error."""

    def broken_print(*args, **kwargs):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(mock.console, "print", broken_print)
    data = ok(server.handle_request({"op": "identify", "mac": MAC, "duration_s": 3}))
    assert data["identify_duration_s"] == 3
    assert server.devices["pn-device-01"].identify_until is not None


# -- the mock does not outlive the process that started it ------------------------------

MOCK_SCRIPT = Path(mock.__file__).resolve()
ORPHAN_TIMEOUT_S = float(os.environ.get("ORPHAN_TIMEOUT_S", "15"))


def wait_gone(pid: int, timeout: float = ORPHAN_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def kill_if_alive(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def test_exits_when_stdin_closes(tmp_path) -> None:
    proc = subprocess.Popen(
        [sys.executable, str(MOCK_SCRIPT), "--port", "0", "--exit-on-stdin-eof"],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert "listening on" in proc.stdout.readline()
        proc.stdin.close()
        assert proc.wait(timeout=ORPHAN_TIMEOUT_S) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


@pytest.mark.skipif(sys.platform == "win32", reason="uses SIGKILL")
def test_exits_when_its_parent_is_killed(tmp_path) -> None:
    """Rule 11: a killed test run must not leave the mock running."""
    parent_code = textwrap.dedent(
        f"""
        import subprocess, sys, time
        mock = subprocess.Popen(
            [sys.executable, {str(MOCK_SCRIPT)!r}, "--port", "0", "--exit-on-stdin-eof"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        mock.stdout.readline()  # listening
        print(mock.pid, flush=True)
        time.sleep(600)
        """
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code], cwd=tmp_path, stdout=subprocess.PIPE, text=True)
    mock_pid = None
    try:
        mock_pid = int(parent.stdout.readline())
        os.kill(parent.pid, signal.SIGKILL)  # no cleanup code runs in the parent
        parent.wait(timeout=10)
        assert wait_gone(mock_pid), "the mock outlived its killed parent"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)
        if mock_pid is not None:
            kill_if_alive(mock_pid)
