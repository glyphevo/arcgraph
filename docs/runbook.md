# ArcGraph Runbook

This runbook covers source-checkout operation for CLI and MCP agent workflows.
ArcGraph is on PyPI. Git tags and GitHub releases carry the same two files as
the matching version on PyPI; creating further tags or Releases requires
separate approval.

## Missing Index

Symptoms:

- `arcgraph current` reports unavailable status.
- MCP `arcgraph_index_status` reports unavailable status or missing files.

Actions:

```bash
arcgraph doctor
arcgraph build
arcgraph current
```

The MCP server does not auto-build during startup. Build or rebuild explicitly.

## Stale Index

Symptoms:

- `freshness.stale` is true.
- `doctor` reports changed source files.

Actions:

```bash
arcgraph doctor
arcgraph sync --if-stale
arcgraph current
```

`sync --if-stale` is the canonical stale recovery command and returns a
machine-readable action when it needs a full rebuild. Use `arcgraph build` when
the index is missing or when schema, source roots, or evidence inputs changed.
Use direct `reindex --changed` only for a deliberately selected compatible
incremental development path; it is not the default recovery instruction.

## Schema Mismatch

Symptoms:

- CLI or MCP payloads report schema incompatibility.
- A client sees an unexpected `schema_version`.

Actions:

First distinguish the contracts: whole-index/raw payloads use index schema
`1.0.0`, while bounded target-scoped payloads use read schema `1.4.0` and report
the storage version in `index_schema_version`. An unsupported read schema
requires a client upgrade; it is not repaired by rebuilding the index.

```bash
arcgraph build
arcgraph current
arcgraph ci
```

Rebuild only for an actual index-schema mismatch. Do not guess a payload shape.

## Doctor Warnings

Warnings can be informational or blocking depending on the workflow. Missing
optional coverage, runtime trace, Pyright, or SCIP can be acceptable for a
workflow that does not require them. Missing TypeScript tooling is acceptable
only for Python-only operation; it means TS/JS semantic analysis did not run.
Missing source roots, missing index files, schema mismatch, and stale required
evidence should be treated as blockers for agent automation.

## Optional Precision Tools Missing

Default static builds work without precision tools. For a precision profile:

```bash
arcgraph evidence plan --profile python_full
```

Install or generate only the evidence needed for that profile.

## TypeScript Or npm Unavailable

If TypeScript analysis is required:

```bash
npm ci
```

Use that command when the analyzed project has a `package-lock.json`;
otherwise use the project's documented lockfile-preserving install command.
ArcGraph's Python wheel contains the `.mjs` extractor but does not contain
`node_modules/typescript`. If the compiler API is unavailable, confirm the
durable build evidence contains `typescript_frontend_unavailable` in both the
build's `summary.json` and `diagnostics.jsonl`; then either provide the runtime
or explicitly continue under the Python-only trial scope.

## Source Roots Missing

Run:

```bash
arcgraph init --dry-run
```

Review the proposed `[tool.arcgraph]` configuration before running `arcgraph
init`. Do not accept source roots that point at generated artifacts, caches, or
build output.

## Wrong Repo Root

Symptoms:

- No symbols found for expected targets.
- Output appears under a different repository.
- MCP tools reject paths or report unavailable index.

Actions:

```bash
arcgraph --repo-root /path/to/repo doctor
arcgraph --repo-root /path/to/repo build
arcgraph mcp serve --repo-root /path/to/repo --output-dir output/arcgraph
```

Use an absolute `--repo-root` in agent configs.

## Output Dir Mismatch

Symptoms:

- CLI build succeeds but MCP reports unavailable index.
- MCP reads an old or empty output directory.

Actions:

```bash
arcgraph --repo-root /path/to/repo --output-dir /path/to/repo/output/arcgraph current
arcgraph mcp serve --repo-root /path/to/repo --output-dir /path/to/repo/output/arcgraph
```

Keep the same output directory in build, current/status, and MCP server commands.

## Multiple Local Projects

Install ArcGraph once in a dedicated tool environment, then run one named stdio
server per project. Each server keeps `repo_id=default` and receives distinct
absolute `--repo-root`, `--output-dir`, `--metrics-log`, and `--feedback-log`
paths. Do not point two projects at one output or log and do not assign a custom
repo id unless the client is known to send it on every call.

If one project shows another project's symbols or events, stop both servers,
compare every configured path, and treat the result as an isolation failure.
MCP queries do not edit Agent client configuration. Explicit `arcgraph setup --client`
prepares a client entry; see [client-setup.md](client-setup.md) for scope and backups.

## MCP Server Starts But Index Is Unavailable

The server registers tools without building. Run:

```bash
arcgraph doctor
arcgraph build
arcgraph current
```

Then restart the MCP server if the client caches server state.

## Metrics Or Feedback Log Is Rejected

ArcGraph creates new sensitive local state privately on POSIX and refuses a
group/world-accessible existing log without changing its permissions. It also
rejects symbolic links in POSIX parent components, a final-component symlink,
a multiply linked file, and a non-regular path. The safest recovery is to select
a new canonical absolute path below a new private directory. If the operator
instead repairs a trusted existing POSIX path, first verify that neither the
file nor any state-path component is a symbolic link and that the file has one
link, then set the direct directory to mode `0700` and an active writable log to
mode `0600`. Do not use `0400` for a log that must accept more events. ArcGraph
never changes an existing path's permissions automatically.

Directory durability failures need different recovery. If feedback reports that
parent directories could not be inspected, verify that every path component is
still present, readable, canonical, and mounted before retrying the same
`client_event_id`. If it reports that a parent directory entry could not be made
durable, changing `0700`/`0600` modes or choosing another directory on the same
filesystem is not a remedy: select a private path on a filesystem that supports
directory `fsync`. The record bytes may already be present after either failure,
so preserve the log and reuse the same event id rather than creating a second
event.

An invalid feedback record or unsupported record schema is not silently
skipped. Within feedback record schema `1.0.0`, warning kinds emitted by an
earlier ArcGraph build remain readable even after the current request allowlist
stops accepting them. Preserve an incompatible log locally for diagnosis and
select a new log only when the record schema version changes; do not mix record
versions. MCP metrics are best-effort and emit one bounded stderr warning;
feedback reports a structured storage failure and never claims success.

Use `arcgraph metrics PATH --trial-summary` and `arcgraph feedback summarize
PATH` for reviewed aggregates. Do not attach either raw JSONL log to an issue.
`arcgraph report metrics-html PATH --output REPORT.html` writes a local visual
aggregate without the input-log path, first/last event timestamps, raw events,
raw error text, or warning text; only the warning count remains. Review its
aggregate command and semantic statistics before sharing it.

## Source Snippet Policy

Source snippets are disabled by default in CLI compact payloads and MCP tools.
Only use `--include-source` or `--expose-source-snippets` when the workflow
explicitly allows source exposure. Review payloads for `source_snippets` before
forwarding them to external systems.

## Clean-Checkout Smoke

Use the clean-checkout smoke when you need to prove the
source-checkout path from committed repository state instead of the current
working tree:

```bash
python scripts/arcgraph_clean_checkout_smoke.py
```

Run it on a supported interpreter. The script builds its virtual environment
from the interpreter that invoked it and then installs the package into it, so
on an interpreter outside `requires-python` (`>=3.11,<3.13`) pip refuses the
install and the smoke fails on its own environment rather than on the code it
is meant to exercise. The same applies to
`scripts/arcgraph_package_readiness_smoke.py` and
`scripts/arcgraph_release_candidate_check.py`.

The script creates a temporary Git checkout, creates a temporary virtual
environment, installs ArcGraph with editable source install, installs the
optional MCP extra unless skipped, renders CLI/docs/MCP help, runs the
source-checkout smoke, and verifies the temporary checkout has no tracked or
staged changes.

For a faster local check:

```bash
python scripts/arcgraph_clean_checkout_smoke.py --quick
```

For command-plan review without cloning or installing:

```bash
python scripts/arcgraph_clean_checkout_smoke.py --dry-run
```

This smoke does not authorize public release, package publishing,
public/packaged MCP distribution, automatic agent configuration, or final
pre-cutover approval. It validates only the matrix cells actually executed on
the current host. The GitHub Actions test and package matrices are defined to
provide separate Ubuntu, Windows, macOS, Python 3.11, and Python 3.12 evidence
when `CI Gate` passes for the commit being claimed. The package matrix also runs
installed-wheel MCP v2 auto/legacy and real v1.28.1 client handshakes, calls
the exact registered default surface, proves one-install/two-project isolation,
and requires clean shutdown. npm and Docker/GHCR distribution remain separately
deferred.

## CI Or Release Gate Failure

Run focused tests first when a failure is scoped. The full local gate is a
fixed command sequence that starts from a clean working tree; run
`arcgraph docs release-checklist` for the current, complete list rather than
copying commands from this page, because a hand-copied list drifts from the
gate.

`scripts/arcgraph_release_gate.py` and `scripts/arcgraph_package_readiness_smoke.py`
reject dirty source state. If a provenance check fails,
inspect `git status --short`, do not reuse the artifacts, and rerun from a new
clean commit. Package-readiness JSON includes the source commit/tree/status
digest plus wheel and sdist SHA-256 values.

Do not create tags, GitHub Releases, packages, or visibility changes as a
workaround for a failed gate.

## Clean Rebuild

Use `arcgraph ops prune --apply` only when you intentionally want to remove old
ArcGraph build output. Then run:

```bash
arcgraph build
arcgraph current
arcgraph ci
```

Do not commit generated `output/arcgraph` files.

## Windows Path, venv, And PATH Issues

- Activate the expected virtual environment before invoking `arcgraph`.
- Use `python scripts/arcgraph.py <command>` from the checkout when the console
  cannot find `arcgraph`.
- Prefer absolute paths in agent configuration.
- Keep PowerShell quoting around paths that contain spaces.

## Before Filing An Issue

Collect:

- ArcGraph commit SHA.
- `arcgraph doctor` output.
- `arcgraph current` output.
- Exact `--repo-root` and `--output-dir` values.
- Whether optional evidence or TypeScript tooling is required.
- Whether the issue occurs through CLI, MCP, or both.
- A reviewed metrics/feedback aggregate when enabled; never attach raw logs,
  source, prompts, targets, repository ids, or absolute paths.
