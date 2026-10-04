# Security Policy

## What this server is, in security terms

This repository is a bridge between a language model and a PROFINET IO
network. The MCP server speaks stdio to its client and, with
`PROFINET_BACKEND=network`, speaks PROFINET on an Ethernet interface of the
host. That puts the trust boundary *inside* the server: whatever the model
decides to call, the network is asked to do.

PROFINET IO was designed for an isolated control network. In its classic form
it carries no authentication or encryption: anything on the same Ethernet
segment can discover devices, and DCP lets it rename a device or change its IP
address. PROFINET's newer security classes are not implemented by this server.

So treat this server as equipment on the control network, not as an
application on the office network.

## Running it safely

- **Start with the mock.** `profinet-mock-server` simulates an IO device so
  PROFINET tooling can be exercised without touching real plant
  (`PROFINET_BACKEND=mock`). Do that first, every time. The mock listens on
  `127.0.0.1` by default and accepts writes, renames and IP changes from
  anyone who can connect, so keep it there.
- **Leave writes off unless you mean otherwise.** `write_io_data` and
  `write_device_by_alias` change physical outputs. They are refused unless
  `PROFINET_WRITES_ENABLED=true` is set in the server's environment (the
  default is `false`). Only enable it on a lab bench. There is no undo, and a
  language model will call a tool it has been given.
- **Leave configuration commands off.** `set_device_name`, `set_device_ip` and
  `identify_device` (DCP Set) need `PROFINET_CONFIG_CMDS_ENABLED=true` *and*
  `PROFINET_WRITES_ENABLED=true`. Renaming or re-addressing a device in a
  running plant takes it away from the IO controller that expects it.
- **Set the switches where you control them.** The safety switches
  (`PROFINET_*_ENABLED`) are honoured only from the real process environment
  or a file you pass with `--env-file`.
- **No settings from the client's project.** Without `--env-file` the server
  reads only `profinet-python/.env` of a source checkout, found relative to
  the package; never a `.env` in its working directory or a parent, which
  for a server launched by an MCP client is the client's project and may not
  be trusted (the mock address and device map decide where a write goes). An
  installed package reads no implicit `.env`. Even that file may set only
  `PROFINET_*` settings and `DEVICE_MAP_FILE` - not safety switches, not
  `PATH`, `LD_PRELOAD`, `PYTHON*` or anything else - and keys are compared
  case-insensitively, as Windows does. See
  [`profinet-python/.env.example`](profinet-python/.env.example).
- **A change is sent at most once.** Connecting may be retried, but once a
  write or DCP command has gone out the server never re-sends it. If the reply
  is lost or garbled, or the overall deadline expires, the tool returns
  `success: false` with `meta.outcome: "unknown"`: the change may have been
  applied, so read the device back before trying again.
- **A disconnected client sends nothing more.** When the MCP client goes away
  (stdin closes), changes that are still connecting or waiting to retry are
  cancelled before they are sent, and no new request starts.
- **GSDML files are untrusted input.** `load_gsd_file` decides the encoding
  itself, from the byte-order mark or the complete XML declaration, and
  accepts only UTF-8, UTF-16, UTF-32, ISO-8859-1 and US-ASCII; encodings that
  could hide markup from that check (UTF-7, EBCDIC, unusual UCS-4 byte
  orders) are refused. It decodes the file, refuses any `DOCTYPE` or `ENTITY`
  declaration in the decoded text (blocking XXE and entity-expansion bombs on
  every libxml2 version), and hands libxml2 only the decoded text without its
  XML declaration, so libxml2 cannot switch encodings. Files over 8 MiB are
  refused, and the parser runs with entity expansion, DTD loading and network
  access off. Parsing holds the Python GIL, so these input limits - not a
  timeout - are what keep a parse short (an 8 MiB file takes about a second).
- **Do not expose the server beyond the host running the client.** It is a
  stdio process meant to run next to the MCP client, not a network service.
- **Limit what the server's account can reach.** Real DCP and I/O traffic is
  raw Ethernet, which needs raw-socket privileges on the configured interface;
  grant them to the account running the server and nobody else, on an
  interface that sits on the control network only. That account should also
  read only what it needs: `load_gsd_file` opens whatever path it is given.
- **Never point it at a safety system.** Safety-instrumented functions,
  including PROFIsafe devices, are out of scope for this repository.

What the server sends on a real network today (see the
[README](README.md#status)): with `PROFINET_BACKEND=network`, only DCP
Identify All requests, a read-only multicast that any engineering tool sends,
only when `discover_devices`, `get_device_info` or `test_device_communication`
is called, and at most one every 2 seconds. Cyclic I/O and DCP Set (name, IP,
identification signal) are not implemented there and are refused without
sending anything. That does not make the advice above optional: the switches
it describes are the ones that will gate real traffic once more of PROFINET
is implemented. With the default `PROFINET_BACKEND=none` the server contacts
nothing.

## Supported versions

This project is pre-1.0. Security fixes land on `main`.

## Reporting a vulnerability

Please report privately through GitHub's private vulnerability reporting:
[open a draft security advisory](https://github.com/IndustriAgents/PROFINET-MCP/security/advisories/new).
You can also email hi@industriagents.com.

Please do not open a public issue for a vulnerability.

Include the version or commit, what an attacker would gain, and a reproduction
if you have one. We will acknowledge within a week and keep you updated as we
work on a fix.

## Scope

In scope: the MCP server (`profinet-python`) and the mock IO device
(`profinet-mock-server`) in this repository. The same code ships inside
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) as the
`PROFINET-Project` submodule; report it here either way.

Out of scope: vulnerabilities in PROFINET itself (the lack of authentication
in classic PROFINET IO is a property of the protocol), in third-party
libraries such as `scapy`, `lxml` or the `mcp` SDK (report those upstream),
and in IO device or controller firmware.
