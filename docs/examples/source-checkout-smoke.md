# Example: Source-Checkout Smoke

Use this smoke path to validate the current source-checkout product surface
without publishing a package or changing repository visibility.

## Install Assumption

This smoke installs ArcGraph from a source checkout, not from PyPI. npm,
Docker/GHCR, and GitHub Release paths remain unpublished; the 0.1.0rc7
pre-release is on PyPI but is not exercised here.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
npm ci
```

On Windows PowerShell, replace the first two lines with:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If `arcgraph` is not on `PATH`, run the source-checkout wrapper:

```bash
python scripts/arcgraph.py <command>
```

## Smoke Commands

For an executable local smoke that creates and cleans up a temporary project:

```bash
python scripts/arcgraph_source_checkout_smoke.py
```

To verify the same alpha path from a temporary clean Git checkout and
fresh virtual environment:

```bash
python scripts/arcgraph_clean_checkout_smoke.py
```

See [docs/clean-checkout-smoke.md](../clean-checkout-smoke.md) for the executed
and deferred matrix cells.

Run from the repository root:

```bash
arcgraph --help
python -m pip show arcgraph
arcgraph doctor
arcgraph init --dry-run
arcgraph build
arcgraph current
arcgraph status
arcgraph stats
arcgraph context arcgraph.pipeline.indexer.ArcGraphIndexer --detail-level summary
arcgraph explain arcgraph.pipeline.indexer.ArcGraphIndexer --detail-level summary
arcgraph ci
arcgraph mcp serve --help
```

For agent subprocess checks, also render the contract:

```bash
arcgraph docs agent-cli-contract
arcgraph docs mcp-server
```

For the alpha local MCP server, install the optional runtime and start
stdio serving only after a local index exists:

```bash
python -m pip install -e '.[mcp]'
arcgraph mcp serve --repo-root . --output-dir output/arcgraph
```

For the lower-level read-only MCP facade example:

```bash
python docs/examples/mcp_readonly_host.py --repo-root . --output-dir output/arcgraph
```

This smoke does not auto-configure agent clients. Explicit client setup is a
separate workflow documented in [client-setup.md](../client-setup.md). The MCP server is an alpha source-checkout path, not
a separately packaged MCP product (the MCP server also ships in the PyPI package's `mcp` extra).

## Expected Results

- `doctor` reports environment, source-root, index, optional evidence,
  precision-tool, and Python-version status.
- `init --dry-run` previews configuration without writing project files.
- `build` writes a local index under `output/arcgraph`.
- `current` or `status` reports schema, freshness, capabilities, language tiers,
  warnings, and evidence state.
- `stats` reports file, node, and edge counts.
- `context` and `explain` return JSON payloads for a known target.
- `ci` returns exit code 0 only when the local graph health gate passes.
- `mcp serve --help` renders without the optional MCP runtime installed.
- The executable smoke checks direct MCP tool-group construction, index status,
  context, explain, risk, default source-snippet disablement, and path escape
  rejection.
- Optional evidence, precision-tool, TypeScript toolchain, runtime trace, or
  coverage warnings can be expected on a default static checkout; stale schema,
  missing source roots, build failures, JSON parse failures, or `ci` failures are
  blockers for agent use.

Generated indexes, evidence manifests, reports, visual workbench output, and
smoke artifacts under `output/arcgraph` are local ignored artifacts and should
not be committed.

## Troubleshooting

- If `arcgraph` is not found, activate the virtual environment or use
  `python scripts/arcgraph.py <command>`.
- If Python is outside 3.11 or 3.12, install Python 3.11 or 3.12 and recreate
  `.venv`.
- If Node/npm is missing, skip the TypeScript development checks or install
  Node.js/npm and then run `npm ci`. Builds still succeed without a resolvable
  TypeScript compiler API and record `typescript_frontend_unavailable`.
- If the index is missing or stale, run `arcgraph doctor`, then `arcgraph build`
  or `arcgraph sync --if-stale`; see [runbook.md](../runbook.md) for the full
  recovery paths, including when `reindex --changed` is appropriate.
- If precision tools are missing, keep using the default static build or run
  `arcgraph evidence plan --profile python_full` for the optional evidence steps.

## Package And Release Decisions

- PyPI: the 0.1.0rc7 pre-release is published; this smoke neither publishes nor tests it.
- npm package publishing remains private/dev-only unless separately authorized.
- Docker/GHCR publishing remains unapproved.
- GitHub Release and tag creation remain unapproved.
- Any future package channel requires a separate package readiness gate.
- Automatic agent config installers and HTTP/network MCP transport remain
  deferred.
