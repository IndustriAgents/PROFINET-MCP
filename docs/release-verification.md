# 0.3.0 local release verification

Reverified on 2026-10-24 after the architecture cross-review.
Package: `industriagents-profinet-mcp`.

These are local build and installed-artifact results, not a registry release.
No tag, commit, push or publication was performed by this verification.

## Checks

- Full server regression suite: 369 passed, 1 skipped.
- Separate mock suite: 62 passed.
- The full suite includes the final native outcome/configuration regressions and
  the shared audit and policy-file tests; no additional focused runs are added
  to the counts above.
- Ruff lint and formatting checks passed for source and tests.
- `uv lock --check` and contract/package-version checks passed.
- Three npm launcher tests passed.
- Fresh wheel, source distribution and npm tarball built successfully.
- Both installed distribution routes initialized over stdio outside the source
  checkout and listed 11 canonical tools with the exact reviewed schemas.
  Invalid mode calls returned MCP error envelopes. Smoke tests selected the
  exact manifest-version artifact filenames.
- `twine check --strict` passed for wheel and source distribution.
- Artifact inspection confirmed current packaged sources/contracts, matching
  npm launcher bytes, and identical wheel bytes inside the npm archive.
- `git diff --check` passed.

Skipped tests: The optional libxml2 HTTP-fetch test is skipped because this build has no HTTP client.

The native checks use mock devices, loopback peers or fake driver APIs.
They do not certify physical hardware or add unsupported live operations.
See [architecture and protocol capability limits](architecture.md).

## Artifact SHA-256

The following artifacts are in `dist/` and `npm/`, excluded from Git.
These hashes identify the exact verified outputs; rebuilding can change them.

| Artifact | SHA-256 |
|---|---|
| `industriagents_profinet_mcp-0.3.0-py3-none-any.whl` | `bc6fbe31705fbc0167e7ab641721740aa804670897f84fffc7cde63744e9b25b` |
| `industriagents_profinet_mcp-0.3.0.tar.gz` | `c7927dec662161c22907490f129e8e424c8097215bc8af8b787f35bf2c3c0fd8` |
| `industriagents-profinet-mcp-0.3.0.tgz` | `270b7bd2ab1086d3af77a2a90894b7f126c1dd6bd79b7c2cbbdf552d65fbf9fa` |

See [release instructions](release.md) for independent registry publication.
