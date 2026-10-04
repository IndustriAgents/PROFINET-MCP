# profinet-mock-server

Mock PROFINET IO device for local development and integration tests. It does
not speak PROFINET on the wire: it answers JSON requests over TCP, one object
per line, so the MCP server and its tests can exercise every tool without
hardware or raw-socket privileges.

The MCP server in [`../profinet-python`](../profinet-python) talks to it when
started with `PROFINET_BACKEND=mock` (see the
[repository README](../README.md#try-it-against-the-mock)). Every answer the
server gets from the mock is labelled `"backend": "mock"` in the tool
response's `meta`.

## Usage

From the repository root:

```bash
uv sync --directory profinet-mock-server
uv run --directory profinet-mock-server profinet-mock-server   # listens on 127.0.0.1:5600
```

`--port 0` picks any free port; the first line the mock prints is
`PROFINET mock listening on HOST:PORT`.

| Flag | Environment variable | Default |
|------|----------------------|---------|
| `--host` | `MOCK_PROFINET_HOST` | `127.0.0.1` |
| `--port` | `MOCK_PROFINET_PORT` | `5600` |
| `--update-interval` | `MOCK_PROFINET_UPDATE_INTERVAL` | `1.0` (seconds) |
| `--devices` | `MOCK_PROFINET_DEVICES_FILE` | built-in device, below |
| `--verbose` | `MOCK_PROFINET_VERBOSE` | off |
| `--exit-on-stdin-eof` | – | off: stop when stdin closes (the tests use it so a killed test run leaves no mock behind) |

The environment variables can also come from a `.env` file in the working
directory (or a parent directory); only `MOCK_PROFINET_*` keys are taken from
it, anything else is ignored with a warning. Flags win over the environment.

## Simulated devices

By default the mock simulates one IO device, `pn-device-01` (MAC
`00:11:22:33:44:55`, IP `192.168.1.100/24`, vendor `MockVendor`), with two I/O
areas:

| Slot / subslot | Length | Direction | Behaviour |
|----------------|--------|-----------|-----------|
| 1 / 1 | 8 bytes | input | Bytes change every update interval |
| 1 / 2 | 4 bytes | output | Holds whatever was last written |

Pass `--devices FILE` (or set `MOCK_PROFINET_DEVICES_FILE`) to simulate your
own devices instead:

```json
{
  "devices": [
    {
      "name": "line1-io",
      "mac": "02:00:00:00:00:01",
      "ip": "10.0.0.10",
      "subnet": "255.255.255.0",
      "gateway": "0.0.0.0",
      "vendor": "MockVendor",
      "vendor_id": "0x0000",
      "device_id": "0x0001",
      "type_of_station": "Mock IO device",
      "areas": [
        { "slot": 1, "subslot": 1, "length": 8, "direction": "input" },
        { "slot": 2, "subslot": 1, "length": 2, "direction": "output" }
      ]
    }
  ]
}
```

`name`, `mac` and `ip` are required; names and MAC addresses must be unique,
and names must follow the same PROFINET station-name rules that `set_name`
enforces (see below).
Slots run 0-32767, subslots 1-65535, and areas hold 1-1440 bytes.

## Protocol

Send one JSON object per line; the mock answers each with one line:

- `{"success": true, "data": ...}`;
- `{"success": false, "error": "...", "rejected": true}` when the request was
  refused before anything changed (malformed request, unknown device or area,
  a rule broken);
- `{"success": false, "error": "Internal mock error: ..."}` without
  `"rejected"` when something failed while handling the request: the change
  may or may not have been applied, and the MCP server reports it as
  `outcome: "unknown"`.

Malformed requests get an error answer and the connection stays open.

| Request | What it does |
|---------|--------------|
| `{"op": "discover"}` | Lists every device (name, MAC, IP settings, vendor, I/O areas, whether it is flashing) |
| `{"op": "read", "device": "pn-device-01", "slot": 1, "subslot": 1, "length": 8}` | Reads an I/O area as `raw_data_hex`; `length` is optional (whole area) and may not exceed the area |
| `{"op": "write", "device": "pn-device-01", "slot": 1, "subslot": 2, "data": [1, 2, 3, 4]}` | Writes the start of an **output** area; inputs are refused, as are values outside 0-255 and payloads longer than the area |
| `{"op": "identify", "mac": "00:11:22:33:44:55", "duration_s": 5}` | Simulates the DCP identification signal (flashing LED) |
| `{"op": "set_name", "mac": "00:11:22:33:44:55", "name": "line1-io"}` | Simulates a DCP Set of the station name; enforces the PROFINET name rules (lowercase a-z, 0-9, `-`, dot-separated labels of up to 63 characters, 240 in total, not `port-xyz`, not an IP address) |
| `{"op": "set_ip", "mac": "00:11:22:33:44:55", "ip": "10.0.0.20", "subnet": "255.255.255.0", "gateway": "10.0.0.1"}` | Simulates a DCP Set of the IP parameters; the gateway is optional (`0.0.0.0` means none) and must be inside the subnet |

Device names are matched case-insensitively. Changes (names, IP settings,
output values) last until the mock is restarted.

## Tests

```bash
cd profinet-mock-server
uv sync --locked
uv run --no-sync ruff check . && uv run --no-sync ruff format --check .
uv run --no-sync pytest
```
