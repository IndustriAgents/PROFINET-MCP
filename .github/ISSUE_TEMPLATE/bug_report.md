---
name: Bug report
about: Report a problem with the PROFINET MCP server or the mock IO device
title: "[Bug] "
labels: bug
assignees: ""
---

**Which part?**
- [ ] MCP server (`profinet-python`)
- [ ] Mock IO device (`profinet-mock-server`)

**Describe the bug**
What happens, and what you expected instead.

**To reproduce**
The MCP client you used (Claude Desktop / Claude Code / Cursor / Inspector /
IndustriConnect's mcp-manager-ui), the tool you called, and the arguments you
passed.

1.
2.
3.

**Tool output**
```json
paste the { success, data, error, meta } envelope, or the error
```

**What was on the other end?**
- [ ] Nothing — the server on its own
- [ ] The mock IO device from this repo
- [ ] Real equipment — IO device vendor and model, and the controller if any:

**Environment**
- OS:
- Python version:
- Commit SHA:
- Relevant settings (`PROFINET_INTERFACE`, `PROFINET_CONTROLLER_IP`, `PROFINET_NETWORK`, ...):

**Additional context**
Device names, slot/subslot, device map, GSDML file, stderr logs — whatever
would let someone else reproduce it against the mock. Do not paste credentials
or production process data.
