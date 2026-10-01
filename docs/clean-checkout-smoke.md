# Clean-Checkout Smoke

The clean-checkout smoke validates ArcGraph from a temporary Git checkout at a
specific commit. It is meant to prove the alpha source-checkout product
path without relying on the developer's current working tree, local indexes,
generated artifacts, or an already-installed package.

It is not a package readiness gate, public release gate, tag/release creator, or
agent configuration installer.

## What It Proves

- A temporary checkout can be created from a concrete Git commit.
- ArcGraph can be installed from that checkout with editable source install.
- The optional MCP extra can be installed for the alpha local stdio MCP
  server path.
- CLI help, built-in docs topics, MCP help, and the MCP server module help
  render from the installed checkout.
- The executable source-checkout smoke script runs from the clean checkout.
- The clean checkout has no tracked or staged changes after the smoke.

## What It Does Not Prove

- It does not authorize public repository visibility.
- It does not authorize package publishing.
- It does not authorize public/packaged MCP distribution.
- It does not authorize PyPI, npm, Docker/GHCR, GitHub Release, or tag creation.
- It does not prove wheel or sdist package installation readiness.
- It does not prove public or packaged MCP distribution readiness.
- It does not auto-configure Claude Code, Codex, Cursor, Aider, VS Code, or any
  other agent client.
- It does not validate HTTP/network MCP transport.
- It does not replace the final public pre-cutover gate.
- It does not claim full OS, Python-version, or package-manager matrix coverage.

## Commands

Run the full source-checkout smoke from the repository root:

```bash
python scripts/arcgraph_clean_checkout_smoke.py
```

Run a faster path that skips the inner `arcgraph ci` step inside the existing
source-checkout smoke:

```bash
python scripts/arcgraph_clean_checkout_smoke.py --quick
```

Preview the command plan without cloning, creating a virtual environment, or
installing dependencies:

```bash
python scripts/arcgraph_clean_checkout_smoke.py --dry-run
```

By default the script uses a local temporary clone:

```bash
git clone --no-hardlinks <current-checkout> <temp-checkout>
```

That validates committed repository state while avoiding private remote
authentication requirements. When private GitHub credentials are available, run
against `origin` explicitly:

```bash
python scripts/arcgraph_clean_checkout_smoke.py --clone-source origin
```

Use `--keep-temp` only for debugging. The default behavior removes the temporary
workspace after the run.

## Matrix Status

| Dimension | Validated By This Smoke | Deferred |
| --- | --- | --- |
| Checkout source | Local clean checkout from a concrete Git commit by default; remote origin clone when `--clone-source origin` is used. | Remote-origin validation when the default local clone mode is used. |
| OS | The host OS on which this invocation is actually run; inspect the JSON `platform` field. | Every other OS unless a separate invocation or CI lane records it. |
| Python | The host Python executable used to create the temporary virtual environment. | Other Python versions unless separately executed. |
| Install mode | Source-checkout editable install with `python -m pip install -e .`. | Wheel and sdist installation, package manager installs, PyPI, npm, Docker/GHCR. |
| MCP runtime | Source-checkout editable install with `.[mcp]` when not using `--skip-mcp-extra`. | Public/packaged MCP distribution and HTTP/network MCP transport. |
| Agent surface | CLI help/docs, subprocess-friendly CLI path, MCP help, MCP module help, and executable source-checkout smoke. | Automatic agent config installer and full MCP protocol client handshake unless separately run. |
| Release path | None. This smoke is alpha verification only. | Public visibility, tags, GitHub Releases, package publishing, and final public pre-cutover approval. |

## JSON Summary

The script prints JSON with:

- `status`: `pass`, `fail`, or `planned`.
- `commit_sha`: the concrete Git commit under validation.
- `platform` and `python`: local execution environment.
- `install_mode`: currently `source-checkout-editable`.
- `mcp_extra_installed`: whether `.[mcp]` was installed.
- `commands`: command names, arguments, exit codes, timing, and stdout/stderr
  tails.
- `matrix`: executed, skipped, and deferred matrix cells.
- `clean_checkout`: tracked and staged Git status from the temporary checkout.
  Present only after a real run; a `--dry-run` creates no checkout to inspect.
- `warnings`: explicit diagnostics. `failures` is likewise added only by a real
  run.
