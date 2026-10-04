# Contributing to PROFINET MCP

Thanks for taking an interest. This repository is an MCP server for PROFINET
IO networks plus a mock IO device for local testing.

It is also one of the ten servers in
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect), which
includes this repository as its `PROFINET-Project` submodule. **Changes land
here, not in IndustriConnect**: open issues and pull requests against
[IndustriAgents/PROFINET-MCP](https://github.com/IndustriAgents/PROFINET-MCP).
When `main` moves, IndustriConnect bumps its submodule pointer to pick the
change up.

## Repository layout

| Path | What it is |
|------|------------|
| `profinet-python/` | The MCP server: `pyproject.toml` + `src/profinet_mcp/`, console script `profinet-mcp`, tests in `tests/` |
| `profinet-mock-server/` | Mock PROFINET IO device speaking a TCP JSON-lines protocol (`127.0.0.1:5600`), tests in `tests/` |
| `.github/workflows/ci.yml` | The checks every pull request has to pass |

Each directory is its own [`uv`](https://docs.astral.sh/uv/) project with a
committed `uv.lock`, so work inside the one you are changing.

## What holds the suite together

IndustriConnect's value is that all ten servers behave the same way. A change
that breaks one of these needs a reason in the pull request:

1. **One response envelope.** Every tool returns `{ success, data, error, meta }`.
   A client that can read one server's output can read all ten.
2. **Read-only by default.** Tools that write I/O data change physical
   equipment. They are separated from the read tools and refused unless
   `PROFINET_WRITES_ENABLED=true`; the DCP configuration tools
   (`set_device_name`, `set_device_ip`, `identify_device`) need that *and*
   `PROFINET_CONFIG_CMDS_ENABLED=true`. Both default to `false`, and both are
   read only from the real environment or `--env-file`, never from a `.env`
   the server discovers by itself.
3. **A mock for every protocol.** If you add a tool, the mock has to be able to
   answer it (with `PROFINET_BACKEND=mock`), or nobody can test it without a
   plant.
4. **No invented results.** A tool reports `success: true` only for something
   that actually happened and was confirmed by the backend. If a backend cannot
   do something, the tool says so (`success: false`, "Not implemented ...")
   and sends nothing.
5. **A change reaches the device at most once.** Only connecting may be
   retried. Once a write or DCP command may have gone out, a lost reply is
   reported as `meta.outcome: "unknown"`, never retried.

## Getting set up

Prerequisites: Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). No
PROFINET hardware is needed for development.

```bash
git clone https://github.com/IndustriAgents/PROFINET-MCP.git
cd PROFINET-MCP
```

The mock IO device, from the repository root:

```bash
cd profinet-mock-server
uv sync
uv run profinet-mock-server           # --port / MOCK_PROFINET_PORT to move it off 5600
```

The MCP server, in a second terminal, also from the repository root, with the
mock as its backend (this needs no privileges):

```bash
cd profinet-python
uv sync
PROFINET_BACKEND=mock uv run profinet-mcp
```

`PROFINET_BACKEND=network` talks to a real PROFINET network instead; it needs
raw-socket privileges and implements DCP discovery only. See the
[repository README](README.md#status) for what each backend can do.

## Testing a change

Both projects have a pytest suite, and both are linted and formatted with
ruff. CI runs the checks below; run them locally, from the repository root,
before pushing:

```bash
cd profinet-python
uv sync --locked
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pytest

cd ../profinet-mock-server
uv sync --locked
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pytest
```

The server's suite includes integration tests that start the mock on an
OS-assigned port and drive the real MCP server over stdio with the MCP client
SDK; no test opens a raw socket (DCP is tested against fake transports). A
third CI job, `lowest-direct`, installs every direct dependency at its lowest
declared version and runs both suites, so raise a floor in `pyproject.toml`
when you start relying on something newer. The exact CI steps are in
[`.github/workflows/ci.yml`](.github/workflows/ci.yml).

Safety tests must be able to fail: when you add or change a safety switch,
check that its test fails with the protection removed.
If you add or rename a tool, update `EXPECTED_TOOLS` in
`profinet-python/tests/test_tools.py` and the integration tests.

Then drive the server the way a user would, through the
[MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector) or a
real client. From the repository root:

```bash
cd profinet-python
npx @modelcontextprotocol/inspector -e PROFINET_BACKEND=mock uv run profinet-mcp
```

Call the tool you changed, and check the envelope, not just the value:
`meta.backend` should say which backend answered.

If you change dependencies, run `uv lock` in that project and commit the
updated `uv.lock`; CI installs with `--locked` and fails if the lockfile is out
of date.

**Do not test against production equipment.** If a change genuinely cannot be
verified against the mock, say so in the pull request and describe the bench
you used.

## House rules for the code

- MCP speaks over **stdio**, so `stdout` belongs to the protocol. All logging
  goes to `stderr`. A stray `print()` corrupts the session.
- Fail at startup, not at the first tool call, when configuration is unusable.
- An error is a returned `{ success: false, error }`, not an exception that
  kills the server. The model needs to be told what went wrong.
- Give each tool a docstring: FastMCP sends it to the client as the tool's
  description, which is what the model reads to decide when to call it.
  Describe each parameter too (`Annotated[..., Field(description=...)]`).
- Read configuration through `ProfinetConfig.from_env()`, which the CLI calls
  after loading `.env`; never read `os.environ` at import time.
- Keep tool names and argument names aligned with the other IndustriConnect
  servers.

## Pull requests

- Branch off `main` and keep commits focused, with descriptive messages.
- Say how you tested, and whether the change touches anything that can write
  to a device or change its name or address.
- Reference related issues (e.g. "Fixes #1").
- If a change was AI-assisted, keep the `Co-Authored-By:` trailer.
- PRs from forks: enable **"Allow edits by maintainers"** so reviewers can
  rebase.

## Code of Conduct

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
