# profinet-python

Python MCP server for PROFINET IO networks. Built with `uv`, `scapy`, `lxml`, and `mcp[cli]`, it exposes the `profinet-mcp` CLI for AI agents and MCP clients.

The PROFINET client is still a scaffold: tools return placeholder data and no PROFINET frames are exchanged yet. See [Status](../README.md#status) in the repository README.

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)

## Quick Start

From the repository root:

```bash
cd profinet-python
uv sync
PROFINET_INTERFACE=eth0 uv run profinet-mcp
```

## Configuration

Set these as environment variables, for example in your MCP client's `env` block:

| Variable | Default | Meaning |
|----------|---------|---------|
| `PROFINET_INTERFACE` | `eth0` | Network interface for PROFINET traffic |
| `PROFINET_CONTROLLER_IP` | `192.168.1.1` | IP address of the controller side |
| `PROFINET_NETWORK` | `192.168.1.0/24` | PROFINET network (CIDR) |
| `PROFINET_DCP_PORT` | `34964` | Port reported for DCP |
| `PROFINET_DCP_TIMEOUT` | `5` | Default discovery timeout, in seconds |
| `PROFINET_TIMEOUT` | `10` | Operation timeout, in seconds (not used yet) |
| `PROFINET_MAX_RETRIES` | `3` | Retry count (not used yet) |
| `PROFINET_RETRY_BACKOFF_BASE` | `0.5` | Retry backoff base, in seconds (not used yet) |
| `PROFINET_WRITES_ENABLED` | `true` | Set to `false` to refuse `write_io_data` and `write_device_by_alias` |
| `PROFINET_CONFIG_CMDS_ENABLED` | `false` | Set to `true` to allow `set_device_name` and `set_device_ip` |
| `DEVICE_MAP_FILE` | unset | JSON file of device aliases, see below |
| `PROFINET_GSD_PATH` | unset | Directory searched by `load_gsd_file` when the given path does not exist |

The CLI also loads a `.env` file from `profinet-python/` (or a parent directory), but it does so after the connection settings have been read, so only `DEVICE_MAP_FILE`, `PROFINET_GSD_PATH` and `PROFINET_WRITES_ENABLED` take effect from it. `PROFINET_CONFIG_CMDS_ENABLED=true` in `.env` shows as enabled in `ping`, but `set_device_name` and `set_device_ip` are still refused by the client. Set it, and the rest, in the real environment.

`DEVICE_MAP_FILE` maps aliases to a device and slot, used by `list_devices`, `read_device_by_alias` and `write_device_by_alias`:

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

`slot`, `subslot` and `data_length` default to `0`, `1` and `8`; the file is re-read when it changes.

## Structure

```
profinet-python/
├── pyproject.toml
├── .python-version
└── src/profinet_mcp
    ├── __init__.py
    ├── cli.py
    ├── server.py
    ├── tools.py
    ├── pn_client.py
    └── gsd_parser.py
```

`pn_client.py` wraps low-level discovery/I/O helpers (currently scaffolded with placeholders), `gsd_parser.py` covers GSDML parsing and caching, and `tools.py` registers MCP tools for discovery, device map aliases, and diagnostics. Extend these modules to implement DCP packets, cyclic I/O, module records, and the rest of the [planned highlights](../README.md#planned-highlights).
