# profinet-python

Python MCP server for PROFINET IO networks. Built with `uv`, `mcp[cli]`,
`scapy` and `lxml`, it exposes the `profinet-mcp` CLI for AI agents and MCP
clients. Which tools work depends on the backend; see
[Status](../README.md#status) in the repository README.

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)
- For `PROFINET_BACKEND=mock`: the [mock IO device](../profinet-mock-server/README.md)
- For `PROFINET_BACKEND=network`: an Ethernet interface on the PROFINET network
  and raw-socket privileges on it (root or `CAP_NET_RAW` on Linux, `/dev/bpf*`
  access on macOS)

## Quick start

From the repository root:

```bash
uv sync --directory profinet-python
PROFINET_BACKEND=mock uv run --directory profinet-python profinet-mcp      # against the mock
uv run --directory profinet-python profinet-mcp --help                     # options and settings
```

The server speaks MCP over stdio, so it waits for a client on stdin; see the
[repository README](../README.md#quick-start) for an MCP client configuration.
Only JSON-RPC goes to stdout; logs go to stderr.

## Backends

| `PROFINET_BACKEND` | Behaviour |
|--------------------|-----------|
| `none` (default) | Contacts nothing. Device tools return `success: false` explaining how to choose a backend. |
| `mock` | Sends the mock's JSON-lines requests to `PROFINET_MOCK_HOST:PROFINET_MOCK_PORT`. One TCP connection per call; answers are checked (byte counts, confirmations) before a tool reports success. |
| `network` | DCP Identify All on `PROFINET_INTERFACE` for `discover_devices`, `get_device_info` and `test_device_communication`. I/O and DCP Set tools return `success: false` ("Not implemented ...") and send nothing. |

`meta.backend` in each envelope names the backend that answered.

## Configuration

Set these as environment variables, for example in your MCP client's `env`
block, or in a `.env` file in the working directory (or a parent directory).
`--env-file PATH` loads a specific file instead. Values already in the
environment win over the file. Settings are read when the server starts, and
an unusable value (for example `PROFINET_WRITES_ENABLED=maybe`) stops it with
a message on stderr.

| Variable | Default | Meaning |
|----------|---------|---------|
| `PROFINET_BACKEND` | `none` | `none`, `mock` or `network`, see above |
| `PROFINET_WRITES_ENABLED` | `false` | Set to `true` to allow `write_io_data` and `write_device_by_alias` |
| `PROFINET_CONFIG_CMDS_ENABLED` | `false` | Set to `true` to allow `set_device_name` and `set_device_ip` |
| `PROFINET_MOCK_HOST` | `127.0.0.1` | Address of the mock IO device |
| `PROFINET_MOCK_PORT` | `5600` | Port of the mock IO device |
| `PROFINET_INTERFACE` | `eth0` | Ethernet interface for DCP discovery (`network` backend) |
| `PROFINET_DCP_TIMEOUT` | `5` | Default discovery time, in seconds (0.1-60) |
| `PROFINET_TIMEOUT` | `10` | Timeout for each request to the mock, in seconds |
| `PROFINET_MAX_RETRIES` | `3` | Extra connection attempts to the mock (0-10) |
| `PROFINET_RETRY_BACKOFF_BASE` | `0.5` | First retry delay in seconds; doubles each attempt |
| `PROFINET_CONTROLLER_IP` | `192.168.1.1` | Reported by `ping` only |
| `PROFINET_NETWORK` | `192.168.1.0/24` | Reported by `ping` only |
| `PROFINET_DCP_PORT` | `34964` | Reported by `ping` only (DCP itself is layer 2 and has no port) |
| `DEVICE_MAP_FILE` | unset | JSON file of device aliases, see below |
| `PROFINET_GSD_PATH` | unset | Directory searched by `load_gsd_file` for relative paths |

Booleans accept `true`/`false`, `1`/`0`, `yes`/`no`, `y`/`n`, `on`/`off` and
`t`/`f`.

`DEVICE_MAP_FILE` maps aliases to a device and slot, used by `list_devices`,
`read_device_by_alias` and `write_device_by_alias`:

```json
{
  "line1_inputs": {
    "device_name": "PN-DEVICE-01",
    "ip_address": "192.168.1.100",
    "description": "Line 1 digital inputs",
    "slot": 1,
    "subslot": 1,
    "data_length": 8
  }
}
```

`slot`, `subslot` and `data_length` default to `0`, `1` and `8`. The file is
re-read when it changes; if it cannot be parsed, `list_devices` and `ping`
report the error.

## Structure

```
profinet-python/
├── pyproject.toml
├── uv.lock
├── .python-version
├── src/profinet_mcp
│   ├── __init__.py
│   ├── cli.py          # argument parsing, .env loading, startup
│   ├── config.py       # ProfinetConfig.from_env()
│   ├── server.py       # FastMCP wiring; bad arguments -> envelopes
│   ├── tools.py        # the 14 MCP tools
│   ├── validation.py   # MAC, station name, IP suite, slot checks
│   ├── pn_client.py    # safety switches, timing, routing to the backend
│   ├── backends.py     # none / mock (JSON over TCP) / network (DCP)
│   ├── dcp.py          # DCP Identify codec and Scapy layer-2 transport
│   └── gsd_parser.py   # GSDML parsing and cache
└── tests/              # pytest: unit tests and mock-backed integration tests
```

## Tests

```bash
cd profinet-python
uv sync --locked
uv run --no-sync ruff check . && uv run --no-sync ruff format --check .
uv run --no-sync pytest
```

The integration tests start `../profinet-mock-server` on an OS-assigned port
and drive the server over stdio with the MCP client SDK. No test opens a raw
socket: DCP is tested against fake layer-2 transports.
