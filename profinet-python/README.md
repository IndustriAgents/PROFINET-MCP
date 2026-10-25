> **0.3.0 package and tool migration:** Install `industriagents-profinet-mcp`
> from npm (requires uv) or PyPI; the command remains `profinet-mcp`.
> The advertised catalog now contains **11 tools**. Older tool names in
> the examples below remain compatibility calls. See the
> [architecture and canonical tool guide](../docs/architecture.md)
> and [release guide](../docs/release.md).

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
| `network` | DCP Identify All on `PROFINET_INTERFACE` for `discover_devices`, `get_device_info` and `test_device_communication`, at most once every 2 s. Gated DCP Set name/IP/Signal use unicast requests without replay; cyclic I/O remains unsupported. |

`meta.backend` in each envelope names the backend that answered.

Tools that change a device (`write_io_data`, `write_device_by_alias`,
`set_device_name`, `set_device_ip`, `identify_device`) also report
`meta.outcome` and `meta.request_sent`:

| `outcome` | `request_sent` | Meaning |
|-----------|----------------|---------|
| `written` | `true` | The device confirmed the change |
| `rejected` | `true` | The device received the request and refused it |
| `unknown` | `true` | Sent, but no usable reply: it may have been applied; check before retrying. It is never re-sent automatically. |
| `not_sent` | `false` | Refused by a switch, invalid arguments, no backend, backend unreachable, or not implemented |

Connecting to the mock is retried (`PROFINET_MAX_RETRIES`); a read whose reply
is lost is sent again, but a write or DCP command never is. "rejected" is used
only when the mock marks a refusal `"rejected": true` (refused before anything
changed); any other failure after a send is "unknown".

Every tool call has **one absolute deadline**, fixed when the call starts
(all connect and reply timeouts, retries and backoff, plus slack: 45.5 s with
the defaults) and shared by every request the call makes; it is never
restarted per request. Waiting to reach a busy device counts against it, and
the deadline is checked again right before each send, so a change is never
sent after it: a request that never left is `not_sent`, a change that did is
`unknown`. There is no lock or queue in the server itself (each mock request
uses its own connection, and the DCP rate limit refuses rather than queues).

When the MCP client cancels a call (`notifications/cancelled`), a change that
has not been sent yet is never sent: the server captures the request's cancel
scope when the call starts and checks it right before each send, because the
SDK only sets a flag and anyio may let the call run one more step first.

When the MCP client disconnects (stdin closes), pending requests are stopped:
one still connecting or waiting to retry is never sent (`not_sent`), one
already sent is not repeated, no new request starts, and the server exits with
status 0.

## Configuration

Set these as environment variables, for example in your MCP client's `env`
block, or in a file passed with `--env-file PATH`. Values already in the
environment win over the file. Settings are read when the server starts, and
an unusable value (for example `PROFINET_WRITES_ENABLED=maybe`) stops it with
a message on stderr. [`.env.example`](.env.example) lists them all.

Without `--env-file`, the server also reads **`profinet-python/.env`** of a
source checkout, found relative to the package source. It never reads a `.env`
from the working directory or its parents (when an MCP client starts the
server, that is the client's project, which may not be trusted; settings such
as the mock address or the device map decide where a write goes), and an
installed package reads no implicit `.env` at all. That file is applied
defensively:

- **safety switches (`PROFINET_*_ENABLED`) in it are ignored** - they come
  only from the real environment or `--env-file`;
- it may set only `PROFINET_*` settings and `DEVICE_MAP_FILE`; anything else
  (`PATH`, `LD_PRELOAD`, `PYTHONPATH`, ...) is ignored;
- keys are compared case-insensitively (as Windows does), with `export` and
  quotes handled;
- each ignored key is named in a warning on stderr.

| Variable | Default | Meaning |
|----------|---------|---------|
| `PROFINET_BACKEND` | `none` | `none`, `mock` or `network`, see above |
| `PROFINET_WRITES_ENABLED` | `false` | `true` allows tools that change a device (`write_io_data`, `write_device_by_alias`; also required by the DCP commands) |
| `PROFINET_CONFIG_CMDS_ENABLED` | `false` | `true` (together with `PROFINET_WRITES_ENABLED`) allows `set_device_name`, `set_device_ip` and `identify_device` |
| `PROFINET_MOCK_HOST` | `127.0.0.1` | Address of the mock IO device |
| `PROFINET_MOCK_PORT` | `5600` | Port of the mock IO device |
| `PROFINET_INTERFACE` | `eth0` | Ethernet interface for DCP discovery and configuration (`network` backend) |
| `PROFINET_DCP_TIMEOUT` | `5` | Default discovery time, in seconds (0.5-60) |
| `PROFINET_TIMEOUT` | `5` | Timeout for each connect and each reply from the mock, in seconds |
| `PROFINET_MAX_RETRIES` | `3` | Extra connection attempts to the mock, and re-sends of a read whose reply was lost (0-10); changes are never re-sent |
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
    "device_name": "pn-device-01",
    "ip_address": "192.168.1.100",
    "description": "Line 1 digital inputs",
    "slot": 1,
    "subslot": 1,
    "data_length": 8
  }
}
```

`slot` is required (an alias without one is reported as an error rather than
guessed); `subslot` defaults to `1` and `data_length` to `8`. The file is
re-read when it changes; if it cannot be parsed, `list_devices` and `ping`
report the error.

## Structure

```
profinet-python/
├── pyproject.toml
├── uv.lock
├── .env.example        # every setting; safety switches only via the real env or --env-file
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
socket: DCP is tested against fake layer-2 transports. The safety tests are
mutation-checked: each one fails when the protection it covers is removed.

Dependency floors in `pyproject.toml` are the lowest versions the suite passes
on; CI's `lowest-direct` job checks that with `--resolution lowest-direct`.
