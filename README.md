# ArcGraph

ArcGraph is a local-first code semantic graph engine for AI agents and code
intelligence. It turns a repository, plus optional evidence inputs, into
queryable and verifiable engineering context for humans and agent workflows.

ArcGraph is built around evidence, confidence, and capability tiers. It does not
try to look like a universal compiler or a hosted code search service. Its job is
to make local repository structure, semantic relationships, framework facts,
resource usage, test signals, and imported evidence available through documented
CLI and tool-facing payloads.

## Why ArcGraph Exists

AI coding agents need more than a file list. They need to know which symbols,
routes, resources, tests, framework registrations, and protocol artifacts are
likely relevant to a change, and they need to see the confidence behind those
answers.

Without ArcGraph:

- Agents rebuild context from ad hoc grep, partial AST scans, stale snippets, and
  one-off shell commands.
- Dynamic or unsupported behavior often gets hidden behind overconfident answers.
- Evidence such as coverage, runtime traces, SCIP, OpenAPI, or external semantic
  extractor payloads is hard to connect back to a code review task.

With ArcGraph:

- Agents can ask for current index status, target context, explanations, impact,
  risk, related tests, architecture, and evidence health.
- Static findings, protocol facts, runtime-only evidence, and warnings retain
  their confidence and provenance.
- Language support is reported by tier, not collapsed into a vague supported/not
  supported flag.

## Install

ArcGraph is published on PyPI, and
[RELEASE_NOTES.md](https://github.com/glyphevo/arcgraph/blob/main/RELEASE_NOTES.md)
lists the published versions. This source version, 0.1.0, is a beta release.
Install ArcGraph into a dedicated virtual environment or tool environment and
reuse that one `arcgraph` executable across projects:

```bash
python -m pip install "arcgraph[mcp]"
```

or, with uv:

```bash
uv tool install --python 3.11 "arcgraph[mcp]"
```

Python 3.11 or 3.12 is required; pip refuses other versions. Pin the exact
version you tested, for example `arcgraph[mcp]==0.1.0`. An unpinned install
picks 0.1.0; pip installs one of the earlier 0.1.0rc7–0.1.0rc10 pre-releases
only when you name its version or pass `--pre`. The `mcp` extra is only needed
to run the MCP server. Installs of 0.1.0 from PyPI were tested: pip with the
`mcp` extra on macOS, Windows and Linux (Ubuntu 24.04 under WSL), each with
Python 3.11 and 3.12, and `uv tool install --python 3.11` on macOS.
`arcgraph version --json` reports the commit the installed copy was built from.

To install the development version from this repository instead, replace the
requirement with `"arcgraph[mcp] @ git+https://github.com/glyphevo/arcgraph.git"`.
Maintainers and contributors can use an editable source checkout, described
below. Git tags (`v` followed by the version) and GitHub releases carry the
same two files as the matching version on PyPI. npm and Docker/GHCR are not
published, and there is no separate packaged MCP
distribution: the MCP server is part of the PyPI package.
The external-trial scope is Python analysis through the installed CLI
plus local stdio MCP. TypeScript/JavaScript analysis is
outside that trial's acceptance scope.

ArcGraph is beta software. GitHub Actions runs the
`CI` workflow (Ubuntu, Windows, and macOS; Python 3.11 and 3.12). A passing run
is evidence only for the commit it ran on, so check the run for the exact
commit you are using.

Requirements:

- Python 3.11 or 3.12.
- Node.js and a resolvable TypeScript compiler API when analyzing
  TypeScript/JavaScript projects. The Python wheel includes ArcGraph's `.mjs`
  extractor but does not include `node_modules/typescript`; provide the runtime
  from the analyzed project or another documented local Node installation.
- Optional precision tools (`scip-python`, `scip`, and `pyright-langserver`)
  only when you want Python precision evidence beyond the default static index.

From a source checkout, create and activate a virtual environment if you do not
already have one:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

Install the CLI in editable mode:

```bash
python -m pip install -e .
```

For core CLI development and TypeScript/JavaScript analysis tests:

```bash
python -m pip install -e ".[dev]"
npm ci
```

Install the optional MCP runtime when you need to start or test the local MCP
server:

```bash
python -m pip install -e '.[mcp]'
```

For the repository's complete validation and release/security command set, use:

```bash
python -m pip install -e '.[dev,mcp,security]'
npm ci
```

For a controlled Agent trial with a checksum-verified wheel, follow
[docs/external-trial-guide.md](https://github.com/glyphevo/arcgraph/blob/main/docs/external-trial-guide.md) instead. Maintainers
assemble that candidate bundle; it is not distributed publicly. Verify it, create
one tool virtual environment outside the analyzed projects, and install the
bundled wheel with its `mcp` extra. Do not substitute an editable checkout. Each
project then gets its own repository root, index output, stdio server process,
metrics log, and optional feedback log; the installed executable itself is
shared.

To connect a coding agent to a project, run `arcgraph setup --client CLIENT`;
[docs/client-setup.md](https://github.com/glyphevo/arcgraph/blob/main/docs/client-setup.md) lists the hosts that were verified
and under which conditions.

Confirm the CLI:

```bash
arcgraph --version
arcgraph version --json
arcgraph --help
python -m pip show arcgraph
```

`--version` reports the stable product version. `version --json` identifies the
exact running source checkout or installed wheel. For wheel installs, compare
`artifact_provenance.sha256` with the candidate manifest; do not infer a Git
commit from the product version alone.

To validate the source-checkout path from a temporary clean Git
checkout, run:

```bash
python scripts/arcgraph_clean_checkout_smoke.py
```

This checks source-checkout editable install, CLI docs/help, MCP help, and the
source-checkout smoke path. It does not authorize package publishing, public
release, public/packaged MCP distribution, or automatic agent configuration.

To validate local wheel/sdist readiness without publishing anything, run:

```bash
python scripts/arcgraph_package_readiness_smoke.py
```

This builds temporary package artifacts, installs the built wheel in a
temporary virtual environment, and checks the installed CLI, built-in docs, MCP
server and a sample-repository workflow there. It then cleans up by default.
It is a check only: it publishes nothing, creates no tag or GitHub Release,
uploads no package (PyPI, npm, Docker/GHCR), and does not change repository
visibility. See [docs/package-readiness.md](https://github.com/glyphevo/arcgraph/blob/main/docs/package-readiness.md).

External trial users should start with
[docs/external-trial-guide.md](https://github.com/glyphevo/arcgraph/blob/main/docs/external-trial-guide.md). Maintainers can
assemble a local external-trial bundle with
`scripts/arcgraph_external_trial_bundle.py` without publishing it; it requires
saved evidence of a completed successful remote CI push run on `main` for the
exact candidate commit. See
[docs/release-tooling.md](https://github.com/glyphevo/arcgraph/blob/main/docs/release-tooling.md).

If `arcgraph` is not on `PATH`, use the source checkout wrapper:

```bash
python scripts/arcgraph.py <command>
```

On a clean Windows machine, install Python 3.11 or 3.12 and Node.js/npm first. Optional
precision tools can be installed with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-arcgraph-precision-tools.ps1
```

On macOS/Linux, install Node.js/npm with your system package manager or version
manager when TypeScript analysis is required. Install the optional precision
tools directly:

```bash
npm install -g @sourcegraph/scip-python@0.6.6 pyright@1.1.409
go install github.com/scip-code/scip/cmd/scip@v0.7.1
```

The first command provides `scip-python`, `pyright`, and `pyright-langserver`;
the second requires a local Go toolchain and provides `scip`. Then pass the
generated JSON artifacts to `arcgraph build`. The GitHub Actions `CI`
workflow does not install these tools, so it is not a fallback for generating
this evidence.

## Quickstart

Build and inspect an index for the current repository:

```bash
arcgraph doctor
arcgraph init --dry-run
arcgraph build
arcgraph sync --if-stale
arcgraph current
arcgraph status
arcgraph stats
arcgraph context arcgraph.pipeline.indexer.ArcGraphIndexer --detail-level summary
arcgraph explain arcgraph.pipeline.indexer.ArcGraphIndexer --detail-level summary
arcgraph ci
```

For a guided local path:

```bash
arcgraph docs quickstart
```

Common queries:

```bash
arcgraph architecture
arcgraph symbol <symbol-or-id>
arcgraph callers <symbol-or-id>
arcgraph callees <symbol-or-id>
arcgraph impact <target> --profile review_default
arcgraph tests <target>
arcgraph references <target>
arcgraph similar <target>
arcgraph route GET /path
arcgraph report html "<target>" --output output/arcgraph/reports/arcgraph-report.html
```

Ordinary symbol/path targets share one safe resolver: stable id, exact
qualname, a normalized-id retry (the query tried as a bare qualname missing
its stable-id kind prefix), exact path, then a unique bare name or qualname
suffix. Ambiguous names return candidate definitions instead of selecting the
first match. Route-shaped and entrypoint/worker-flow targets resolve
separately, before or instead of this chain. See
[docs/change-preflight.md](https://github.com/glyphevo/arcgraph/blob/main/docs/change-preflight.md) for the full precedence
and its exceptions.

`callers`, `callees`, and `impact` return a bounded, agent-safe payload by
default and report what they dropped in `truncation`. Use `--max-results` and
`--detail-level` to widen it. `--raw` returns the unbounded QueryEngine debug
payload instead; it is meant for human debugging, has no size bound, and
rejects the payload-shaping options.

Bounded target-scoped responses use read schema `1.4.0` and identify the
underlying index/storage schema separately as `index_schema_version` (`1.0.0`
today). Raw QueryEngine responses remain on the index schema. Target lists are
de-duplicated and capped at 100 unique values per request.

Read schema `1.3.0` adds ambiguity-safe `target_resolution`, structured
`assurance`, and separate analysis-versus-presentation truncation signals.
Consumers must treat `ambiguous` and `unresolved` resolutions as non-results;
suggestions and candidates are never selected automatically.

Index warnings unrelated to the files an answer covers are folded into a single
`index_warnings_omitted` entry that keeps per-kind counts. Folding stops and
every warning is reported unfiltered whenever the answer cannot vouch for its
own file set: no targets were requested, a requested target did not resolve, or
the definition paths could not be read. Only that last case adds a
`warning_scope_unavailable` warning, because only there did something fail — the
other two are ordinary states already visible in the payload.

Since read schema `1.1.0`, `warnings` is a heterogeneous JSON array: an element may
be a string or a structured object. Consumers must branch on the element type
instead of calling string-only operations such as `"\n".join(warnings)`; objects
carry `message`, `kind`, and an optional `path`.

Agent-oriented context and provenance:

```bash
arcgraph context <target> --detail-level summary
arcgraph explain <target> --detail-level summary
arcgraph evidence status
arcgraph evidence plan --profile python_full
arcgraph benchmark suite --iterations 5 --warmups 1 --output output/arcgraph/reports/benchmark-suite.json
```

Use human-readable output by placing global flags before the subcommand:

```bash
arcgraph --human current
arcgraph --human impact <target>
```

Generated indexes, evidence manifests, reports, and visual workbench output are
written under `output/arcgraph` by default. They are local artifacts and should
not be committed. `arcgraph build` publishes `output/arcgraph/current.json`;
`arcgraph current` and `arcgraph status` read that current snapshot. If the
index is missing or stale, run `arcgraph doctor` for remediation guidance. Use
`arcgraph build` for a missing or incompatible index and
`arcgraph sync --if-stale` for the normal stale-index recovery path.

Common first-run issues:

- `arcgraph` command not found: activate the virtual environment or use
  `python scripts/arcgraph.py <command>` from the checkout.
- Wrong Python version: install Python 3.11 or 3.12 and recreate the virtual
  environment.
- Missing Node/npm or TypeScript compiler API: install TypeScript in the
  analyzed project (for example with its locked npm install) when TS/JS
  analysis is required. Builds still succeed and persist
  `typescript_frontend_unavailable` in the build summary and diagnostics when
  that runtime cannot be resolved.
- Missing `pyproject.toml` or source roots: run `arcgraph init --dry-run` first,
  then `arcgraph init` when the suggested `[tool.arcgraph]` config is correct.
- Missing optional precision tools: this is not a blocker for default local
  builds. Use `arcgraph evidence plan --profile python_full` for generation
  commands when a precision profile is required.

## Agent Workflows

Use `current` or `status` before planning a task. They report schema, freshness,
capabilities, language tiers, evidence status, warnings, and toolchain status.

Use `context` when an agent needs a compact task package across one or more
targets. Use `explain` when the agent needs target-specific provenance,
resolution strategy, confidence sources, and direct edges.

Use `arcgraph_get_risk` as the default Change Preflight before editing. One
bounded response includes target resolution, impact, tests, similar siblings,
entrypoints, unknowns, evidence assurance, and recommended reads. Opt in to
TypeScript Language Service or SCIP confirmation with `verify_references=true`
for a high-risk target. `impact`, `tests`, `references`, `similar`, and `route`
remain focused CLI surfaces for deeper follow-up.

In read schema `1.3.0`, `unknowns.kind=truncated_scope` means only that response
presentation omitted records. Traversal or analysis-data truncation is reported
separately as `analysis_truncated_scope`; clients that previously treated every
truncation as `truncated_scope` must handle both values.

Read-only queries never rebuild an index. Run `arcgraph sync --if-stale` for an
explicit one-shot incremental publication, or `arcgraph watch` for debounced
local synchronization. A failed sync keeps the previous `current.json`
published, while stale query responses return a machine-readable
`recovery_action`.

Use `architecture`, `evidence status`, `evidence plan`, `ci`, and `visual`
surfaces when a human or agent needs a broader project health view.

Use `arcgraph help` for bounded Agent-oriented discovery from a CLI subprocess.
It complements `arcgraph --help` syntax and the longer `arcgraph docs` topics.
For MCP, protocol `list_tools` is the authoritative list actually registered in
that server instance, and `arcgraph_help` provides selection, cost, result, and
recovery guidance on demand. A client may choose how much of that protocol
metadata it exposes to its model, so configured-client discovery still needs
an end-to-end check.
Agent help can explain the known opt-in feedback tool while it is disabled, but
the returned registration fields remain false until the operator supplies a
feedback log.

For subprocess/JSON integration, run `arcgraph docs agent-cli-contract`.
For local MCP server integration, run `arcgraph docs mcp-server`.
For the full source-checkout smoke path, run `arcgraph docs source-checkout-smoke`.
For clean-checkout source-install verification, see
[docs/clean-checkout-smoke.md](https://github.com/glyphevo/arcgraph/blob/main/docs/clean-checkout-smoke.md).

See [docs/agent-reading-guide.md](https://github.com/glyphevo/arcgraph/blob/main/docs/agent-reading-guide.md) and
[docs/change-preflight.md](https://github.com/glyphevo/arcgraph/blob/main/docs/change-preflight.md), plus
[docs/mcp-usage.md](https://github.com/glyphevo/arcgraph/blob/main/docs/mcp-usage.md). The external-trial surface includes
the local stdio server from the installed wheel:

```bash
arcgraph mcp serve --repo-root . --output-dir output/arcgraph
```

Explicit onboarding is available through `arcgraph setup --client
claude|codex|cursor|hermes|pi`. It prepares the selected project and client;
MCP query calls never change client configuration. See
[client setup](https://github.com/glyphevo/arcgraph/blob/main/docs/client-setup.md) for preview mode, config scopes, host trust
and the distinction between protocol verification and actual model use.
The installed package also provides `arcgraph docs client-setup`.
For a standalone protocol example, see
[the minimal read-only host](https://github.com/glyphevo/arcgraph/blob/main/docs/examples/mcp_readonly_host.py).
npm and Docker/GHCR distribution remain separate release decisions.

Optional per-tool MCP metrics are local and disabled by default. Start the
server with `--metrics-log /private/local/path/mcp.jsonl` to opt in. Events
contain only timing, status, payload-size/token estimates, truncation, and an
enum-only freshness status. Use `arcgraph metrics PATH --trial-summary` for a
path- and timestamp-free aggregate. Raw metrics JSONL should not be shared;
events do not contain tool arguments, repository ids, paths, source, returned
payload text, or raw exceptions.

Optional Agent feedback is also disabled by default and stored separately from
metrics. Supplying an absolute `--feedback-log` registers one disclosed local
append tool, `arcgraph_record_trial_feedback`; omitting the option leaves the
default MCP analysis/change/help surface read-only. Feedback accepts only
bounded enum and identifier fields—never free text, paths, targets, repository
ids, source, prompts, or raw errors. Review a path-free aggregate with
`arcgraph feedback summarize ABSOLUTE_PATH` before sharing anything.
The [External Trial Guide](https://github.com/glyphevo/arcgraph/blob/main/docs/external-trial-guide.md#feedback-machine-contract)
defines the exact feedback bounds and stable machine `error_code` values;
automation should not branch on recovery-message text.

## Change Safety

Surgical Change Safety is a local, fail-closed contract workflow under
`arcgraph change`. It captures a baseline and a durable pin, plans an explicit
edit scope from graph targets, computes graph deltas, and records verification
evidence against one exact plan revision.

ArcGraph does not edit code, run commands supplied in evidence metadata, push
Git state, or change remotes. Every `arcgraph change` command requires an
explicit `--repo-id`; there is no implicit default repository identity.

```bash
arcgraph change --repo-id my-repo plan --task "Adjust the items route" --target "route:GET /items"
```

A route target is `route:METHOD PATH` (a space between method and path, quoted
because of that space) -- `--target route:GET:/items` parses as method
`GET:/items` and never matches, since `arcgraph change` only splits the target
on its first `:`.

On macOS/Linux:

```bash
arcgraph change --repo-id my-repo plan --task "Adjust the items route" --target "route:GET /items"
```

The default Change Safety MCP facade exposes read-only preview and compute tools
only.
Approval, evidence persistence, purge, and audit export remain CLI-only.

See `arcgraph docs change-safety` and
[docs/change-safety.md](https://github.com/glyphevo/arcgraph/blob/main/docs/change-safety.md).

## Language Support

ArcGraph reports language capability by tier:

| Area | Current public-safe wording |
| --- | --- |
| Python | L3 native semantic static frontend. |
| TypeScript / JavaScript | L3 native semantic static frontend when a TypeScript compiler API is resolvable; outside the external-trial acceptance scope. |
| Next.js / Vue | Framework semantics layered over TypeScript / JavaScript. |
| Go / C# / Java / Rust / C / C++ / Swift | L3 through validated external semantic extractor payloads. |
| SCIP | L2 explicit protocol evidence. |
| OpenAPI | L2 explicit protocol evidence. |

No language is claimed as L4. External L3 payload support is explicit and
payload-backed; it is not default live compiler extraction for those languages.
SCIP and OpenAPI facts remain protocol evidence and are not relabeled as L3
language semantics.

See [docs/language-support.md](https://github.com/glyphevo/arcgraph/blob/main/docs/language-support.md).

## Evidence Inputs

Fast local checks work with default static analysis:

```bash
arcgraph build
arcgraph ci
```

Optional evidence can be imported explicitly:

```bash
python -m pytest arcgraph/tests -q --cov=arcgraph --cov-report=xml:output/arcgraph/coverage.xml --cov-report=term
arcgraph precision scip-python --output output/arcgraph/scip-index.json --index-file output/arcgraph/index.scip --project-name ArcGraph --project-version local --target-only arcgraph
arcgraph precision pyright --output output/arcgraph/pyright-export.json --python-version 3.11 --timeout-seconds 300 --target-only arcgraph
arcgraph trace run --output output/arcgraph/runtime-trace.json --root arcgraph --max-events 5000 --max-seconds 30 -- arcgraph/tests/test_imports.py::test_import_analyzer_marks_function_local_import_edges -q
arcgraph build --coverage output/arcgraph/coverage.xml --runtime-trace output/arcgraph/runtime-trace.json --scip-index output/arcgraph/scip-index.json --pyright-export output/arcgraph/pyright-export.json
```

SCIP protocol graph input is separate from Python precision SCIP:

```bash
arcgraph build --scip-graph-index output/arcgraph/scip-protocol.json
```

OpenAPI input is artifact-only and explicit:

```bash
arcgraph build --openapi-spec PATH_TO_YOUR_OPENAPI_SPEC
```

See [docs/examples/evidence-inputs.md](https://github.com/glyphevo/arcgraph/blob/main/docs/examples/evidence-inputs.md).

## Security And Trust

ArcGraph is local-first. Default CLI workflows read local repository files and
write generated artifacts under `output/arcgraph`. The project has no telemetry
exporter, hosted service dependency, or background collector in default
workflows, and ArcGraph configures no automatic or remote telemetry. The
optional MCP SDK includes OpenTelemetry API hooks; an operator-configured global
provider is host behavior, not an ArcGraph collector or outbound destination.
The explicit local MCP `--metrics-log` option writes privacy-bounded JSONL only
to the operator-selected path and does not install or configure an exporter.
The separate optional `--feedback-log` enables a privacy-bounded append-only
local feedback record and no network action. New POSIX log files and newly
created state directories use private modes; unsafe existing log files or
direct log directories are rejected without silently changing them, and
symbolic parent components or multiply linked log files fail closed. Use
canonical physical paths for POSIX trial state. Windows behavior does not claim
that POSIX mode bits describe ACL or reparse-point guarantees.

External evidence and protocol artifacts are opt-in. Importers and read-side
surfaces enforce path containment, source snippets are disabled by default in
agent payloads, and `visual serve` is intended for loopback-only local use.

Report security issues to [security@glyphevo.com](mailto:security@glyphevo.com).
See [SECURITY.md](https://github.com/glyphevo/arcgraph/blob/main/SECURITY.md) for the full security policy and boundaries.

## Current Maturity

ArcGraph is beta-stage infrastructure. The current focus is deterministic,
evidence-aware local context for agent and review workflows.

Current limitations include dynamic Python behavior, framework magic,
TypeScript bundler/plugin behavior, complex package-manager linking,
relationship-heavy ORM behavior, decorator-heavy frameworks, external language
toolchain enablement, and full release/package automation. Unsupported behavior
should remain visible as warnings, diagnostics, lower-confidence edges, or
unresolved records.

See:

- [docs/language-support.md](https://github.com/glyphevo/arcgraph/blob/main/docs/language-support.md)
- [docs/runbook.md](https://github.com/glyphevo/arcgraph/blob/main/docs/runbook.md)
- [docs/clean-checkout-smoke.md](https://github.com/glyphevo/arcgraph/blob/main/docs/clean-checkout-smoke.md)
- [docs/package-readiness.md](https://github.com/glyphevo/arcgraph/blob/main/docs/package-readiness.md)
- [docs/external-trial-guide.md](https://github.com/glyphevo/arcgraph/blob/main/docs/external-trial-guide.md)
- [docs/agent-reading-guide.md](https://github.com/glyphevo/arcgraph/blob/main/docs/agent-reading-guide.md)
- [docs/examples/source-checkout-smoke.md](https://github.com/glyphevo/arcgraph/blob/main/docs/examples/source-checkout-smoke.md)
- `arcgraph docs limitations`
- `arcgraph docs security-model`
- `arcgraph docs frontend-contract`
- `arcgraph docs schema-governance`
- `arcgraph docs mcp-server`
- `arcgraph docs source-checkout-smoke`
- `arcgraph docs package-readiness`

## Visualization

Generate and open the local ArcGraph Explorer workbench:

```bash
arcgraph visual workbench --output-dir output/arcgraph/reports/workbench --open
```

For large repositories or on-demand audit data, use the local read-only server:

```bash
arcgraph visual serve --host 127.0.0.1 --port 8765 --open
```

Maintainers can run the browser smoke baseline when Playwright CLI is available:

```bash
arcgraph visual smoke --output-dir output/arcgraph/reports/visual-smoke
```

## Release And Governance

Releases are published on PyPI; Git tags and GitHub releases (GitHub
pre-releases for the 0.1.0rcN versions) carry the same files, and npm and
Docker/GHCR are not used. The release gate is a fixed,
ordered command sequence that starts from a clean working tree. Run
`arcgraph docs release-checklist` for the current list rather than copying
commands from this page, because a hand-copied list drifts from the gate.
[docs/release-tooling.md](https://github.com/glyphevo/arcgraph/blob/main/docs/release-tooling.md) describes the release
scripts, artifact verification and bundle assembly, and
[RELEASE_NOTES.md](https://github.com/glyphevo/arcgraph/blob/main/RELEASE_NOTES.md) indexes the per-version notes.

## Built-In Documentation

ArcGraph ships its user-facing reference docs inside the CLI. Render any topic
with `arcgraph docs TOPIC`, or `arcgraph docs TOPIC --json` for a structured
payload. The complete topic list is:

| Topic | Covers |
| --- | --- |
| `arcgraph docs cli-reference` | All top-level commands and the complete `change` tree, with required and commonly used options. |
| `arcgraph docs quickstart` | First index, first query, first agent handoff. |
| `arcgraph docs client-setup` | Prepare one project for Claude Code, Codex, Cursor, Hermes or Pi with `arcgraph setup`: preview, config scopes, and what a prepared entry does not prove. |
| `arcgraph docs capabilities` | Reported capability flags and what they gate. |
| `arcgraph docs change-safety` | Surgical Change Safety workflow, evidence, audit, and MCP scope. |
| `arcgraph docs change-preflight` | Default read-only pre-edit workflow, assurance, exact references, and index lifecycle. |
| `arcgraph docs evidence-cookbook` | Recipes for supplying and checking evidence inputs. |
| `arcgraph docs frontend-contract` | Language frontend tiers and payload contract. |
| `arcgraph docs agent-cli-contract` | Subprocess/JSON integration contract for agents. |
| `arcgraph docs mcp-server` | Local stdio MCP setup, default read-only tools, and optional feedback. |
| `arcgraph docs visualization` | Workbench, local read-only server, and export surfaces. |
| `arcgraph docs schema-governance` | Schema versioning and compatibility rules. |
| `arcgraph docs security-model` | Trust boundaries, containment, and redaction posture. |
| `arcgraph docs limitations` | What ArcGraph does not claim or guarantee. |
| `arcgraph docs troubleshooting` | Common failures and recovery paths. |
| `arcgraph docs migration-notes` | Behavior changes that affect existing callers. |
| `arcgraph docs source-checkout-smoke` | Source-checkout smoke path. |
| `arcgraph docs package-readiness` | Local wheel/sdist build and install verification. |
| `arcgraph docs release-checklist` | Release gate steps and required evidence. |

## Repository Layout

```text
arcgraph/      Python package, tests, and packaged workbench assets
scripts/       Source checkout wrapper, release gate, and helper scripts
docs/          Public documentation and per-version release notes
viz/           Legacy compatibility wrapper for the source-checkout viewer
output/        Generated local index/evidence/report data, ignored by Git
```

See [docs/provenance.md](https://github.com/glyphevo/arcgraph/blob/main/docs/provenance.md) for the standalone extraction
boundary and legacy integration policy.
