# 0.3.0 local release verification

Verified on 2026-10-15. Package: `industriagents-profinet-mcp`.

This records local build verification, not registry publication. Check the
GitHub Actions runs and registry version pages for publication status. Registry
authentication/trusted publishers must be configured before release; GitHub
repository access alone does not authorize registry publication.

## Checks

- Full Python regression run: 339 passed, 1 skipped.
- Final architecture/contract tests: 15 passed (a separate run, not additional unique tests).
- Ruff lint and formatting checks passed for source and tests.
- `uv lock --check` and contract/package version parity passed.
- Three npm launcher tests passed.
- Installed wheel and installed npm tarball both initialized over stdio outside
  the checkout, reported version 0.3.0 and listed 11 canonical tools.
  Their schemas matched the contract; invalid mode calls returned MCP errors.
- `twine check --strict` passed for the wheel and source distribution.
- The npm tarball contains exactly the same wheel bytes as the PyPI artifact.
- `git diff --check` passed.

Skipped test: The local libxml2 build cannot fetch HTTP resources.

These are software and mock/loopback checks, not physical-device certification.
See [protocol capability limits](architecture.md#protocol-capability-limits).

## Artifact SHA-256

Artifacts are local build outputs in `dist/` and `npm/`, excluded from Git.
Rebuilding can change archive hashes; these identify the verified files.

| Artifact | SHA-256 |
|---|---|
| `industriagents_profinet_mcp-0.3.0-py3-none-any.whl` | `d134594289a5bf46f7d892977f756b7399639012273116d2058eec00fb2b3595` |
| `industriagents_profinet_mcp-0.3.0.tar.gz` | `dee869c75896daf1a590918c590f29f035e1c686e4a288bcca7d171356a29b82` |
| `industriagents-profinet-mcp-0.3.0.tgz` | `5f98b47a18ce30de10838242808ab3dfae728538e74a2d4c1a915a300e3ce414` |

See [release instructions](release.md) for independent registry publication.
