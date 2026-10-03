# Support

ArcGraph is currently an alpha developer-preview source candidate. It is not
a published package or a cross-platform support claim. Support is best effort
and focused on source-checkout usage, local CLI workflows, documentation gaps,
and reproducible bugs.

## Before Asking

Check [docs/runbook.md](docs/runbook.md) first — it covers the common failure
modes (missing or stale index, schema mismatch, wrong repo root, rejected
metrics/feedback logs) with the exact recovery commands. Run `arcgraph doctor`
and `arcgraph help` for local diagnostics.

## Where To Ask

- Use GitHub issues for reproducible bugs and public feature requests.
- Use the security reporting path in [SECURITY.md](SECURITY.md) for
  vulnerabilities, credential exposure, or sensitive repository data concerns.
  Use the same address for Code of Conduct reports; see
  [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Current Boundaries

- No production support SLA is offered.
- ArcGraph is published on PyPI only as developer-preview pre-releases. npm, Docker/GHCR and GitHub Release distribution are not
  published.
- The MCP server ships inside the PyPI package (`arcgraph[mcp]`); there is no
  separate packaged MCP distribution.
- GitHub Actions runs the `CI` workflow. A passing run is evidence only for the
  commit it ran on; a workflow file alone is not a cross-platform result.
- `main` is covered by a ruleset that blocks force pushes and branch deletion.
  It does not require pull requests, reviews, or passing status checks.
- Tags, GitHub Releases, and branch-protection changes require separate
  maintainer approval.
