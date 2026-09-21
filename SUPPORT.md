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
- Public package publishing is not approved.
- Public or packaged MCP distribution is not approved.
- GitHub Actions is currently disabled. Workflow files in this source tree are
  not a running remote matrix or a green cross-platform result.
- Branch protection has not been verified. GitHub's protection and ruleset
  APIs returned plan/feature errors when they were checked; do not assume
  `main` is protected.
- Tags, GitHub Releases, and branch-protection changes require separate
  maintainer approval.
