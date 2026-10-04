# Security Policy

## What this server is, in security terms

This repository is a bridge between a language model and a PROFINET IO
network. The MCP server speaks stdio to its client and is meant to speak
PROFINET (DCP for discovery and naming, I/O data exchange with IO devices) on
an Ethernet interface of the host. That puts the trust boundary *inside* the
server: whatever the model decides to call, the network is asked to do.

PROFINET IO was designed for an isolated control network. In its classic form
it carries no authentication or encryption: anything on the same Ethernet
segment can discover devices, and DCP lets it rename a device or change its IP
address. PROFINET's newer security classes are not implemented by this server.

So treat this server as equipment on the control network, not as an
application on the office network.

## Running it safely

- **Start with the mock.** `profinet-mock-server` simulates an IO device so
  PROFINET tooling can be exercised without touching real plant. Do that
  first, every time. The mock listens on `127.0.0.1` by default and accepts
  writes from anyone who can connect, so keep it there.
- **Turn writes off until you mean otherwise.** `write_io_data` and
  `write_device_by_alias` change physical outputs. They are currently enabled
  unless you set `PROFINET_WRITES_ENABLED=false` in the server's environment,
  so set it explicitly for anything that is not a lab bench. There is no undo,
  and a language model will call a tool it has been given.
- **Leave configuration commands off.** `set_device_name` and `set_device_ip`
  are refused unless `PROFINET_CONFIG_CMDS_ENABLED=true`. Renaming or
  re-addressing a device in a running plant takes it away from the IO
  controller that expects it.
- **Do not expose the server beyond the host running the client.** It is a
  stdio process meant to run next to the MCP client, not a network service.
- **Limit what the server's account can reach.** Real DCP and I/O traffic is
  raw Ethernet, which needs raw-socket privileges on the configured interface;
  grant them to the account running the server and nobody else, on an
  interface that sits on the control network only. That account should also
  read only what it needs: `load_gsd_file` opens whatever path it is given.
- **Never point it at a safety system.** Safety-instrumented functions,
  including PROFIsafe devices, are out of scope for this repository.

The PROFINET client in `profinet-python` is still a scaffold (see the
[README](README.md#status)): it does not send or receive PROFINET frames yet.
That does not make the advice above optional: the switches it describes are
the ones that will gate real traffic once it is sent.

## Supported versions

This project is pre-1.0. Security fixes land on `main`.

## Reporting a vulnerability

Please report privately, by email to hi@industriagents.com. If this
repository's **Security** tab shows a **Report a vulnerability** button, you
can use GitHub private vulnerability reporting instead.

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
