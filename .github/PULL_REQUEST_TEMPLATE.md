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

- [ ] Tools still return the shared `{ success, data, error, meta }` envelope.
- [ ] I tested against the mock IO device (`profinet-mock-server`).
- [ ] Nothing was tested against production equipment.
- [ ] `stdout` is still clean — all logging goes to `stderr`.
- [ ] The CI checks pass locally (see [CONTRIBUTING.md](https://github.com/IndustriAgents/PROFINET-MCP/blob/main/CONTRIBUTING.md#testing-a-change)).
- [ ] If I changed dependencies, I committed the updated `uv.lock`.
- [ ] I updated the README where relevant.

## How to test

<!-- The exact commands and tool calls a reviewer can run against the mock. -->
