# profinet-mock-server

Toy PROFINET IO device for local testing. Instead of speaking raw PROFINET on the wire (planned), the current scaffold exposes a JSON-over-TCP bridge that mimics device discovery and I/O payloads so PROFINET tooling can be exercised without hardware.

The MCP server in [`../profinet-python`](../profinet-python) does not connect to this mock yet (its PROFINET client is still a scaffold); for now, drive the mock directly over TCP.

## Usage

From the repository root:

```bash
cd profinet-mock-server
uv sync
uv run profinet-mock-server
```

Defaults:

- Listens on `127.0.0.1:5600`
- Simulates one IO device, `PN-DEVICE-01` (MAC `00:11:22:33:44:55`, IP `192.168.1.100`), with two I/O areas: slot 1/subslot 1 (8 bytes) and slot 1/subslot 2 (4 bytes), whose bytes change every update interval
- Accepts one JSON object per line and answers with one JSON line (`{"success": ..., "data": ...}` or `{"success": false, "error": ...}`):
  - `{ "op": "discover" }`
  - `{ "op": "read", "device": "PN-DEVICE-01", "slot": 1, "subslot": 1, "length": 8 }`
  - `{ "op": "write", "device": "PN-DEVICE-01", "slot": 1, "subslot": 1, "data": [1,0,0,0] }`

Use `--help` to configure port, host, and auto-update cadence, or the matching environment variables:

| Flag | Environment variable | Default |
|------|----------------------|---------|
| `--host` | `MOCK_PROFINET_HOST` | `127.0.0.1` |
| `--port` | `MOCK_PROFINET_PORT` | `5600` |
| `--update-interval` | `MOCK_PROFINET_UPDATE_INTERVAL` | `1.0` (seconds) |
| `--verbose` | `MOCK_PROFINET_VERBOSE` | off |

The device and its modules are fixed in code. Extend `profinet_mock_device.py` to add actual DCP handling, diagnostics, and richer module behavior once the MCP server requires it.
