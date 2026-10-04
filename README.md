# PROFINET MCP Server

[![CI](https://github.com/IndustriAgents/PROFINET-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/IndustriAgents/PROFINET-MCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple)](https://modelcontextprotocol.io)

MCP server for PROFINET IO networks (PROFINET is a PROFIBUS & PROFINET
International standard), plus a mock IO device to develop and test against.
It lets AI agents discover devices, parse GSDML device descriptions, and -
against the mock - read and write I/O data and run the DCP configuration
services. On a real network only DCP discovery is implemented so far (see
[Status](#status)). It follows the same shape as the other
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
| `network` | The PROFINET network on `PROFINET_INTERFACE` (raw Ethernet) | DCP discovery: `discover_devices`, `get_device_info`, `test_device_communication` |

The server is **read-only by default**: `write_io_data` and
`write_device_by_alias` are refused unless `PROFINET_WRITES_ENABLED=true`, and
`set_device_name` / `set_device_ip` unless `PROFINET_CONFIG_CMDS_ENABLED=true`.
A refused call sends nothing.

### Known issues and limitations

- **No cyclic I/O on a real network.** `read_io_data`, `write_io_data` and the
  alias tools need an IO controller with an application relationship (AR) to
  the device; this server has no IO-controller stack. On the `network`
  backend they return `success: false` ("Not implemented ...") without
  sending anything.
- **No DCP Set on a real network.** `set_device_name`, `set_device_ip` and
  `identify_device` (the flashing LED) are DCP Set services. They are
  implemented against the mock only; on the `network` backend they return
  `success: false` without sending anything.
- **DCP discovery is not yet verified against hardware.** The Identify
  request/response codec is unit-tested against hand-written frames and
  cross-checked with Scapy's PROFINET dissector, but it has not been run
  against real devices. It needs raw-socket privileges (root or
  `CAP_NET_RAW` on Linux, `/dev/bpf*` access on macOS). Reports from real
  networks are welcome.
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

Ask it to discover devices: the answer comes from the mock's `PN-DEVICE-01`
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
| `discover_devices(timeout?)` | Lists the devices that answer DCP Identify | ✓ | ✓ |
| `get_device_info(device_name)` | Discovery details for one device, by station name, IP or MAC | ✓ | ✓ |
| `test_device_communication(device_name)` | Checks that a device answers discovery | ✓ | ✓ |
| `read_io_data(device_name, slot, subslot, data_length)` | Reads I/O data, returned as hex and byte values | ✓ | – |
| `write_io_data(device_name, slot, subslot, data)` | Writes output bytes; needs `PROFINET_WRITES_ENABLED=true` | ✓ | – |
| `identify_device(device_mac, duration_s=5)` | Makes a device flash its identification LED (DCP Signal) | ✓ | – |
| `set_device_name(device_mac, name)` | DCP Set of the station name; needs `PROFINET_CONFIG_CMDS_ENABLED=true` | ✓ | – |
| `set_device_ip(device_mac, ip_address, subnet_mask, gateway?)` | DCP Set of the IP suite; same switch as `set_device_name` | ✓ | – |
| `list_devices` | Lists the aliases defined in `DEVICE_MAP_FILE` | local | local |
| `read_device_by_alias(alias)` | `read_io_data` for the device and slot behind an alias | ✓ | – |
| `write_device_by_alias(alias, data)` | `write_io_data` for an alias; same write switch | ✓ | – |
| `load_gsd_file(filepath)` | Parses a GSDML file: identity, access points, modules and I/O sizes | local | local |
| `ping` | Version, backend, settings and safety switches; contacts nothing | local | local |
| `get_connection_status` | Settings plus a reachability check of the mock | ✓ | not checked |

"–" means the tool returns `success: false` with a "Not implemented" error
and sends nothing. Every tool has a description and documented, range-checked
parameters in `tools/list`; bad arguments come back as `success: false`
envelopes that name the argument.

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
