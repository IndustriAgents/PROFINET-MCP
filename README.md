# PROFINET MCP Server

[![CI](https://github.com/IndustriAgents/PROFINET-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/IndustriAgents/PROFINET-MCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple)](https://modelcontextprotocol.io)

MCP server scaffold for PROFINET IO networks (PROFINET is a PROFIBUS &
PROFINET International standard): a Python MCP runtime plus a mock IO device.
The aim is to let AI agents discover devices, inspect modules, and exchange I/O
data safely; the server does not talk to the network yet (see
[Status](#status)). It follows the same shape as the other
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) servers
(Modbus, S7comm and the rest): one `uv` project for the server, one for the
mock, and the shared `{ success, data, error, meta }` response envelope.

## Status

**This is a scaffold.** The package metadata, entry points, client wrappers,
tool registration, device-alias map and the mock IO device are in place, so
feature work can focus on the protocol itself. PROFINET communication is not
implemented yet:

- The PROFINET client (`profinet-python/src/profinet_mcp/pn_client.py`) sends
  and receives no frames. `discover_devices` and `get_device_info` return one
  built-in sample device (`PN-DEVICE-01`), `read_io_data` returns zero bytes,
  and `write_io_data`, `set_device_name`, `set_device_ip` and `identify_device`
  report success without sending anything.
- The MCP server does not talk to the mock IO device yet. The mock is a
  standalone JSON-over-TCP simulator you can drive directly.
- `load_gsd_file` reads `Module`/`Subslot` elements without an XML namespace
  and takes the device ID, vendor, name and version from attributes of the root
  element, so standard GSDML files, which declare a namespace and keep the
  device identity in `DeviceIdentity`, come back with no modules and empty
  metadata (every field `null`). Parsed files are cached for the life of the
  process, so an edited file is not re-read until the server restarts.

## Planned highlights

- DCP discovery and configuration (device name/IP, flashing identification)
- I/O data access per slot/subslot, cyclic read/write helpers
- Module/diagnostic/record tools backed by GSDML parsing
- Device map aliases with engineering scaling metadata
- Network scan/topology probes and communication tests
- Mock IO device with configurable modules for local development

### Components

- **profinet-python** – uv-based Python server: Scapy for DCP packets (still to
  be implemented) and lxml for GSDML parsing. Ships the `profinet-mcp` CLI.
- **profinet-mock-server** – Python script that simulates a PROFINET IO device,
  answering JSON discover/read/write requests over TCP for integration testing.

## Layout

```
PROFINET-MCP/
├── README.md
├── LICENSE
├── CONTRIBUTING.md · CODE_OF_CONDUCT.md · SECURITY.md
├── .github/                # CI, issue/PR templates, IndustriConnect notification
├── profinet-python/        # Python MCP server (uv managed)
└── profinet-mock-server/   # Mock PROFINET IO device (TCP JSON protocol)
```

Inside IndustriConnect this repository is mounted as the `PROFINET-Project/`
submodule, so the same tree appears there under that name.

## Install

Requirements: Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). No PROFINET
hardware is needed while the client is a scaffold.

```bash
git clone https://github.com/IndustriAgents/PROFINET-MCP.git
cd PROFINET-MCP
uv sync --directory profinet-python
```

The commands below run from the repository root (`PROFINET-MCP/`), where the
install left you. `--directory` makes `uv` work in that project folder without
changing your shell's directory, the same way the MCP client config below does.

## Quick start

Run the server over stdio, from the repository root:

```bash
PROFINET_INTERFACE=eth0 uv run --directory profinet-python profinet-mcp
```

`PROFINET_INTERFACE` and the other connection settings are reported by `ping`
and `get_connection_status`; no interface is opened yet. All settings are
listed under [Configuration](profinet-python/README.md#configuration).

To use it from an MCP client such as Claude Desktop or Claude Code, point the
client at the project directory:

```json
{
  "mcpServers": {
    "profinet": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/PROFINET-MCP/profinet-python", "run", "profinet-mcp"],
      "env": { "PROFINET_WRITES_ENABLED": "false" }
    }
  }
}
```

The mock IO device runs on its own; see its
[README](profinet-mock-server/README.md). From the repository root:

```bash
uv sync --directory profinet-mock-server
uv run --directory profinet-mock-server profinet-mock-server   # listens on 127.0.0.1:5600
```

## Tools

| Tool | What it does |
|------|--------------|
| `discover_devices(timeout?)` | Lists the PROFINET devices found by DCP discovery |
| `get_device_info(device_name)` | Discovery details for one device, by name or IP address |
| `read_io_data(device_name, slot, subslot, data_length)` | Reads I/O data from a slot/subslot, returned as hex |
| `write_io_data(device_name, slot, subslot, data)` | Writes I/O bytes; refused when `PROFINET_WRITES_ENABLED=false` |
| `test_device_communication(device_name)` | Reads 4 bytes from slot 0, subslot 1 as a reachability check |
| `identify_device(device_mac, duration_s=5)` | Asks a device to flash its identification LED |
| `set_device_name(device_mac, name)` | DCP set of the device name; refused unless `PROFINET_CONFIG_CMDS_ENABLED=true` |
| `set_device_ip(device_mac, ip_address, subnet_mask, gateway?)` | DCP set of the IP configuration; same gate as `set_device_name` |
| `list_devices` | Lists the aliases defined in `DEVICE_MAP_FILE` |
| `read_device_by_alias(alias)` | `read_io_data` for the device and slot behind an alias |
| `write_device_by_alias(alias, data)` | `write_io_data` for the device and slot behind an alias; same write gate |
| `load_gsd_file(filepath)` | Parses a GSDML file for device metadata and modules |
| `ping` | Connection settings, write/config flags and the number of device aliases |
| `get_connection_status` | Interface, controller IP, network and DCP port |

See [Status](#status) for what these return today.

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

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and the checks CI runs, and
[SECURITY.md](SECURITY.md) for running the server safely and reporting
vulnerabilities. Everyone taking part agrees to the
[Code of Conduct](CODE_OF_CONDUCT.md).

## License

MIT — see [LICENSE](LICENSE).
