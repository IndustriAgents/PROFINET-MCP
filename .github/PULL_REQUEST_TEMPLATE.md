## Summary

<!-- What does this PR do, and why? -->

## Part(s) affected

- [ ] MCP server (`profinet-python`)
- [ ] Mock IO device (`profinet-mock-server`)
- [ ] Docs / CI only

## Type of change

- [ ] Bug fix
- [ ] New tool
- [ ] Documentation
- [ ] Refactor / chore

## Does this change what can be written to a device?

- [ ] No
- [ ] Yes — describe the new capability (I/O writes, device name/IP changes) and how it is gated:

## Checklist

- [ ] Tools still return the shared `{ success, data, error, meta }` envelope, and never report success for something that did not happen.
- [ ] I tested against the mock IO device (`profinet-mock-server`, server started with `PROFINET_BACKEND=mock`).
- [ ] Nothing was tested against production equipment.
- [ ] `stdout` is still clean — all logging goes to `stderr`.
- [ ] Writes and DCP configuration commands are still off by default.
- [ ] I added or updated tests, and `pytest`, `ruff check` and `ruff format --check` pass locally (see [CONTRIBUTING.md](https://github.com/IndustriAgents/PROFINET-MCP/blob/main/CONTRIBUTING.md#testing-a-change)).
- [ ] If I changed dependencies, I committed the updated `uv.lock`.
- [ ] I updated the README (Status, Known issues, tool table) where relevant.

## How to test

<!-- The exact commands and tool calls a reviewer can run against the mock (PROFINET_BACKEND=mock). -->
