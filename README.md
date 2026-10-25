> **0.3.0 package and tool migration:** Install `industriagents-profinet-mcp`
> from npm (requires uv) or PyPI; the command remains `profinet-mcp`.
> The advertised catalog now contains **11 tools**. Older tool names in
> the examples below remain compatibility calls. See the
> [architecture and canonical tool guide](docs/architecture.md)
> and [release guide](docs/release.md).

# PROFINET MCP Server

[![CI](https://github.com/IndustriAgents/PROFINET-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/IndustriAgents/PROFINET-MCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple)](https://modelcontextprotocol.io)

MCP server for PROFINET IO networks (PROFINET is a PROFIBUS & PROFINET
International standard), plus a mock IO device to develop and test against.
It lets AI agents discover devices, parse GSDML device descriptions, and run
gated unicast DCP configuration services. Cyclic I/O is available against the
mock; the network backend needs an external IO-controller stack for cyclic I/O
(see [Status](#status)). It follows the same shape as the other
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) servers
(Modbus, S7comm and the rest): one `uv` project for the server, one for the
mock, and the shared `{ success, data, error, meta }` response envelope.

## Status

The server talks to one backend, chosen with `PROFINET_BACKEND`. Every
envelope from a tool that talks to devices says which one answered in
`meta.backend`.

| Backend | What it talks to | What works |
|---------|------------------|------------|
| `none` (default) | Nothing | `ping`, `get_connection_status`, `list_devices`, `load_gsd_file`; device tools return `success: false` and say how to pick a backend |
| `mock` | [`profinet-mock-server`](profinet-mock-server/README.md) over TCP (JSON lines) | Every tool |
| `network` | The PROFINET network on `PROFINET_INTERFACE` (raw Ethernet) | DCP discovery plus gated `set_device_name`, `set_device_ip`, and `identify_device` |

The server is **read-only by default**:

- `write_io_data` and `write_device_by_alias` are refused unless
  `PROFINET_WRITES_ENABLED=true`.
- The DCP configuration commands `set_device_name`, `set_device_ip` and
  `identify_device` need both `PROFINET_WRITES_ENABLED=true` and
  `PROFINET_CONFIG_CMDS_ENABLED=true`.
- These safety switches are read only from the real process environment (for
  example the MCP client's `env` block) or a file passed with `--env-file`.
- Without `--env-file`, the only `.env` the server reads is
  `profinet-python/.env` in a source checkout (located from the package
  itself). A `.env` in the working directory or any parent is never read:
  when a client starts the server, that directory is the client's project,
  which may not be trusted, and settings such as the mock address or the
  device map decide where a write goes. An installed package reads no `.env`
  at all. Even `profinet-python/.env` may set only `PROFINET_*` settings and
  `DEVICE_MAP_FILE`, never a safety switch; anything else is ignored with a
  warning on stderr (keys are compared case-insensitively).
- A refused call sends nothing, and a change is sent **at most once**: if the
  reply to a write or DCP command is lost, the server does not retry; it
  returns `success: false` with `meta.outcome: "unknown"` and an error saying
  the change may have been applied. Every device-changing tool reports
  `meta.outcome` (`written`, `rejected`, `unknown` or `not_sent`) and
  `meta.request_sent`; "rejected" is used only when the device refused the
  request before applying anything.
- Every tool call has one absolute deadline (under 50 s with the default
  settings), fixed when the call starts and shared by everything the call
  does, including any wait to reach the device; no call can hang. A change
  is never sent after that deadline: one that runs out of time after it was
  sent is reported as `unknown`, one that never left as `not_sent`. This
  matters because the MCP client SDK does not tell the server when its own
  request times out. Tools that change a device are annotated as destructive and not
  idempotent, so clients are not invited to retry them.
- If the MCP client disconnects (stdin closes) while a change is still
  connecting or waiting to retry, the change is cancelled and never sent; one
  already sent is not repeated. The server then exits cleanly.

### Known issues and limitations

- **No cyclic I/O on a real network.** `read_io_data`, `write_io_data` and the
  alias tools need an IO controller with an application relationship (AR) to
  the device; this server has no IO-controller stack. On the `network`
  backend they return `success: false` ("Not implemented ...") without
  sending anything.
- **DCP Set uses explicit unicast MAC addresses.** Name and IP settings request
  permanent storage. Both configuration gates must be enabled. A correlated
  protocol acknowledgement does not verify physical completion. Live Signal
  sends one flash sequence and accepts only `duration_s=3`; the device controls
  actual LED timing. The default changes from 5 to 3 seconds for all backends;
  explicit mock durations remain supported.
- **DCP is not yet verified against hardware.** Identify and Set
  request/response codecs are tested against packet fixtures and
  cross-checked with Scapy's PROFINET dissector, but it has not been run
  against real devices. It needs raw-socket privileges (root or
  `CAP_NET_RAW` on Linux, `/dev/bpf*` access on macOS). Reports from real
  networks are welcome. See [protocol research and verification](docs/protocol-research.md).
- Discovery on the `network` backend sends at most one DCP Identify All every
  2 seconds so a model cannot flood the network. `discover_devices` calls that
  come sooner are refused (`meta.retry_after_s`); `get_device_info` and
  `test_device_communication` reuse the last answer instead
  (`meta.cached: true`, `meta.age_s`). Native capture is serialized even after
  cancellation and bounded to 1,024 accepted frames / 1 MiB; discovery reports
  `meta.complete=false` and `meta.capture_truncated=true` when the capture loses data.
- `load_gsd_file` accepts only files encoded in UTF-8, UTF-16, UTF-32,
  ISO-8859-1 or US-ASCII (decided from the byte-order mark or the XML
  declaration), up to 8 MiB, and refuses any `DOCTYPE` or `ENTITY`
  declaration (GSDML never needs one; they enable XXE and entity bombs).
  Other encodings, including windows-1252, are refused: convert such a file
  to UTF-8 first.
- `PROFINET_CONTROLLER_IP`, `PROFINET_NETWORK` and `PROFINET_DCP_PORT` are
  reported by `ping` and `get_connection_status` but not used for anything.
- No alarms, diagnostics, record data, topology (LLDP) or PROFINET security
  classes.

## Components

- **profinet-python** – the MCP server (`uv` project, `profinet-mcp` CLI). It
  uses [Scapy](https://scapy.net) to send and capture raw Ethernet for DCP
  discovery and lxml for GSDML parsing. See its [README](profinet-python/README.md).
- **profinet-mock-server** – a simulated PROFINET IO device that answers
  JSON requests over TCP (discover, read, write, identify, set name, set IP),
  with configurable devices and I/O areas. See its
  [README](profinet-mock-server/README.md).

## Layout

```
PROFINET-MCP/
├── README.md
├── LICENSE
├── CONTRIBUTING.md · CODE_OF_CONDUCT.md · SECURITY.md
├── .github/                # CI, issue/PR templates, IndustriConnect notification
├── profinet-python/        # Python MCP server (uv managed) and its tests
└── profinet-mock-server/   # Mock PROFINET IO device (TCP JSON protocol)
```

Inside IndustriConnect this repository is mounted as the `PROFINET-Project/`
submodule, so the same tree appears there under that name.

## Install

Requirements: Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). No PROFINET
hardware is needed to use the server with the mock.

```bash
git clone https://github.com/IndustriAgents/PROFINET-MCP.git
cd PROFINET-MCP
uv sync --directory profinet-python
uv sync --directory profinet-mock-server
```

The commands below run from the repository root (`PROFINET-MCP/`), where the
install left you. `--directory` makes `uv` work in that project folder without
changing your shell's directory, the same way the MCP client config below does.

## Quick start

### Try it against the mock

Start the mock IO device in one terminal:

```bash
uv run --directory profinet-mock-server profinet-mock-server   # listens on 127.0.0.1:5600
```

Then point an MCP client such as Claude Desktop or Claude Code at the server,
with the mock as its backend:

```json
{
  "mcpServers": {
    "profinet": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/PROFINET-MCP/profinet-python", "run", "profinet-mcp"],
      "env": { "PROFINET_BACKEND": "mock" }
    }
  }
}
```

Ask it to discover devices: the answer comes from the mock's `pn-device-01`
(vendor `MockVendor`) with `"backend": "mock"` in `meta`. To try the write and
DCP configuration tools against the mock, add `"PROFINET_WRITES_ENABLED": "true"`
and `"PROFINET_CONFIG_CMDS_ENABLED": "true"` to `env`.

The server speaks MCP over stdio, so on its own it just waits for a client:

```bash
PROFINET_BACKEND=mock uv run --directory profinet-python profinet-mcp
uv run --directory profinet-python profinet-mcp --help
```

### On a real network

```bash
PROFINET_BACKEND=network PROFINET_INTERFACE=eth0 uv run --directory profinet-python profinet-mcp
```

Only discovery is available there (see [Status](#status)), and the process
needs raw-socket privileges on that interface. Read [SECURITY.md](SECURITY.md)
first. All settings are listed under
[Configuration](profinet-python/README.md#configuration).

## Tools

| Tool | What it does | `mock` | `network` |
|------|--------------|:------:|:---------:|
| `discover_devices(timeout?)` | Lists the devices that answer DCP Identify (timeout 0.5-60 s) | ✓ | ✓ |
| `get_device_info(device_name)` | Discovery details for one device, by station name, IP or MAC | ✓ | ✓ |
| `test_device_communication(device_name)` | Checks that a device answers discovery | ✓ | ✓ |
| `read_io_data(device_name, slot, subslot, data_length)` | Reads I/O data, returned as hex and byte values | ✓ | – |
| `write_io_data(device_name, slot, subslot, data)` | Writes output bytes; needs `PROFINET_WRITES_ENABLED=true` | ✓ | – |
| `identify_device(device_mac, duration_s=5)` | Makes a device flash its identification LED (DCP Signal); needs both switches | ✓ | – |
| `set_device_name(device_mac, name)` | DCP Set of the station name; needs `PROFINET_WRITES_ENABLED=true` and `PROFINET_CONFIG_CMDS_ENABLED=true` | ✓ | – |
| `set_device_ip(device_mac, ip_address, subnet_mask, gateway?)` | DCP Set of the IP suite; same switches as `set_device_name` | ✓ | – |
| `list_devices` | Lists the aliases defined in `DEVICE_MAP_FILE` | local | local |
| `read_device_by_alias(alias)` | `read_io_data` for the device and slot behind an alias | ✓ | – |
| `write_device_by_alias(alias, data)` | `write_io_data` for an alias; same write switch | ✓ | – |
| `load_gsd_file(filepath)` | Parses a GSDML file: identity, access points, modules, fixed and pluggable submodules, I/O sizes | local | local |
| `ping` | Version, backend, settings and safety switches; contacts nothing | local | local |
| `get_connection_status` | Settings plus a reachability check of the mock | ✓ | not checked |

"–" means the tool returns `success: false` with a "Not implemented" error
and sends nothing. Every tool has a description and documented, range-checked
parameters in `tools/list`. Numbers are strict (`true`, `"1"` or `2.0` are not
accepted as a slot or a byte value), and bad arguments come back as
`success: false` envelopes that name the argument.

## Third-party licences

This repository is MIT-licensed. It depends on [Scapy](https://scapy.net),
which is licensed GPL-2.0-or-later. Scapy is not vendored or modified here: it
is installed as a regular dependency from PyPI, and the server imports it only
when `PROFINET_BACKEND=network` sends a DCP request. If you redistribute a
bundle that includes Scapy, its licence applies to that part.

## Part of IndustriConnect

This server is one of the protocol servers in
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect), a suite of
MCP servers for industrial automation (Modbus, MQTT/Sparkplug B, OPC UA, BACnet,
DNP3, EtherCAT, EtherNet/IP, PROFIBUS, PROFINET and S7comm). IndustriConnect
includes this repository as its `PROFINET-Project` submodule and tracks `main`.

Development happens here: please open issues and pull requests in
[IndustriAgents/PROFINET-MCP](https://github.com/IndustriAgents/PROFINET-MCP),
not in IndustriConnect.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests and the checks CI runs,
and [SECURITY.md](SECURITY.md) for running the server safely and reporting
vulnerabilities. Everyone taking part agrees to the
[Code of Conduct](CODE_OF_CONDUCT.md).

## License

MIT — see [LICENSE](LICENSE).
