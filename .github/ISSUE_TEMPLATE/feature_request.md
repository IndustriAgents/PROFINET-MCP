---
name: Feature request
about: Suggest a new tool or an improvement
title: "[Feature] "
labels: enhancement
assignees: ""
---

**What problem does this solve?**
The use case, from the point of view of someone asking an agent to do something.

**Which part?**
- [ ] MCP server (`profinet-python`)
- [ ] Mock IO device (`profinet-mock-server`)

**Proposed solution**
If it is a new tool: its name, arguments, and what it returns. Tool names and
arguments are kept consistent across the
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) servers,
so say how it lines up with the equivalent tool elsewhere in the suite.

**Does it write to a device?**
- [ ] No — read-only
- [ ] Yes — it changes physical equipment, or a device's name or address

**Can the mock answer it?**
What the mock IO device would need to be able to simulate for this to be
testable without real plant (`PROFINET_BACKEND=mock`).

**Real network**
Does it need something the server does not have yet on `PROFINET_BACKEND=network`
(raw-socket DCP beyond Identify, an IO-controller stack for cyclic I/O)?

**Additional context**
Links to the PROFINET specification, GSDML files, example devices, or related
issues.
