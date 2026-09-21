# Agent Reading Guide

Use ArcGraph for structural evidence alongside source reading and normal text
search. Analysis/change/help tools are read-only. The optional local feedback
append is a disclosed write enabled by the operator. Respect the permissions
and language scope of the task; installing a tool grants no additional access.

## Discover Before Selecting

Use MCP `list_tools` to learn what this server instance actually exposes, then
call `arcgraph_help` with `topic="workflow"` or `topic="tool_selection"`.
Descriptions answer what a tool does; Agent help adds prerequisites, relative
cost, signals to inspect, recovery, CLI-only fallbacks, and when to record
feedback. Do not infer that a tool exists from this document if it is absent
from the protocol listing.

If only a CLI subprocess is available, run `arcgraph help`. CLI has no
protocol-level automatic inventory, `arcgraph --help` is syntax help, and
`arcgraph docs` is the long-form reference. CLI and MCP do not expose identical
capabilities.

## Trust A Read Payload

Call `arcgraph_index_status` at the start of a task and after source changes.
Also inspect the `freshness` object returned by every bounded read. Treat
`stale` or `unknown` freshness as not current. MCP exposes no index writer, so
request or perform an authorized `arcgraph sync --if-stale` outside MCP before
relying on another result. Use `arcgraph build` when the index is missing or
incompatible.

Before acting on a payload, inspect:

- `status` and both schema versions;
- `freshness`;
- `warnings`, whose elements may be strings or structured objects;
- `truncation`;
- reported capability degradations;
- target-resolution results.

`warning_scope_unavailable` means ArcGraph could not confirm which index
warnings belong to the requested targets. It retains the warnings instead of
folding them; inspect the result rather than treating it as a scoped clean bill
of health. A resolved request can still be incomplete when the payload is
truncated, a capability is unavailable, or some targets did not resolve.

## Choose An MCP Tool

- `arcgraph_index_status`: index freshness and capability preflight.
- `arcgraph_get_context`: broad structural context before editing or review.
- `arcgraph_explain`: target resolution and direct structural evidence.
- `arcgraph_get_risk`: the default Change Preflight for target resolution,
  known impact, test candidates, similar pattern-family members, entrypoints,
  unknowns, assurance boundaries, and recommended next reads. Optional exact
  references are target-level verification, not an index-wide completeness
  claim. In read schema `1.3.0`, `truncated_scope` means response omission and
  `analysis_truncated_scope` means traversal or analysis-data incompleteness;
  record either kind as a limitation.
- `arcgraph_entrypoint_flow`: route, MCP tool, worker, or queue flow.
- `arcgraph_find_similar`: structurally similar indexed implementations.
- `arcgraph_get_why`: structural reasons; historical memory is present only
  when the host configured an external connector.

For a proposed edit, start with one `arcgraph_get_risk` call with a small
`max_results`. This tool does not accept `detail_level`; use the schema returned
by `list_tools`. Example tool name and arguments (replace the target):

```json
{
  "tool": "arcgraph_get_risk",
  "arguments": {
    "targets": ["your_package.target_function"],
    "max_results": 3
  }
}
```

Follow its recommended reads or choose a narrower tool when the bounded
preflight is insufficient. Do not use change-plan tools unless the task
explicitly evaluates the separate Change Safety workflow.

### Recover From Truncation

`arcgraph_entrypoint_flow` returns compact nodes and edges, with at most two
source-location evidence records per edge and a 64 KiB serialized JSON budget.
Internal bindings, type references and callsite tables are not included.
`max_results` caps traversed edges; endpoint nodes are included for retained edges,
so node count may exceed that number. Inspect `analysis_limits` for traversal
limits and `truncation` for omitted evidence, nodes, edges or envelope fields.
A null remaining-edge count means unknown, not zero. Increase traversal limits
within the advertised bounds, or request a narrower entrypoint when the byte
budget is reached. Use `arcgraph_explain` on a returned node id for direct
relations and evidence. No bounded response establishes runtime completeness.


For `arcgraph_explain` and `arcgraph_get_context`, `max_results` is an upper
bound, not the sole limit. `detail_level="summary"` caps each context section
at 6 items; `standard` at 8; `detailed` follows the explicit `max_results`
(up to 100 and subject to the server limit). Invalid values such as `full`
are rejected. Check `truncation.context_limit`
and `truncated_counts`. Raising `max_results` alone cannot lift the summary
cap. To recover a moderately sized direct-caller list, use a focused request:

```json
{
  "tool": "arcgraph_explain",
  "arguments": {
    "targets": ["your_package.target_function"],
    "detail_level": "detailed",
    "max_results": 12
  }
}
```

Check the returned truncation again: this example permits 12, not an unlimited
list. If results remain omitted, narrow the question to individual symbols or
files and corroborate with source. `arcgraph_get_risk` instead takes
`max_results` without `detail_level`; increasing it can recover presentation
omissions but does not by itself resolve `analysis_truncated_scope` or unknown
runtime relationships. Do not treat a filtered follow-up as proof that the
original broad analysis was complete. These read-only recovery steps do not
require operator intervention or CLI fallback.

### Direct Resource Relations and Risk Scope

`explain` keeps `callers`, `callees`, `incoming_edges` and `outgoing_edges`
within the advertised `call_scope.edge_kinds`. Use `explanations[].relations`
for direct `reads`, `writes`, `enqueues` and `consumes`. These relations obey
confidence filtering, each direction's context limit, two source locations
per edge and a 32 KiB budget per target section. Inspect its scope, totals,
confidence exclusions and truncation. This is not a global explain byte budget
or all graph edge kinds. An empty caller list is not proof of no table writers.

`get_risk` orders non-test entrypoints before tests and labels both categories.
`entrypoint_summary` counts only the graph reached within traversal limits;
non-test does not establish actual production execution. No omitted set should
be treated as empty. Risk diagnostics carry `source_scope` and `inclusion_scope`:
`target`, `affected_symbol`, `affected_module`, or `same_file`. The last category
is conservative file expansion, not a proven dependency. Its presence and an
individual diagnostic's `release_blocking` classification do not establish a
release verdict for the proposed change. Counts retain omitted diagnostics.

For flows use `METHOD /path`, `mcp_tool:name`, or `worker:queue:name`, or select
an entrypoint ID from suggestions. A function qualname may suggest multiple
registered entries; choose explicitly. Wrong HTTP methods remain unresolved
but can suggest other methods at the same path.

### Interpret Empty Callers

An empty, untruncated caller list means no matching incoming call edges were
found in this index, not that the symbol is unused. Nested Python sync/async
functions are indexed with their enclosing qualified names. Stable captured
receiver bindings can resolve, but reassignment, shadowing by unknown values,
conditional/late bindings and nonlocal mutation remain conservative unresolved
cases. A conditional local function can resolve after its definition within the
same branch; captured local class construction can retain the defining binding
identity. Nested class methods and lambda bodies remain outside this extraction; eager
class-body expressions can contribute calls. Indexing a callback does not prove
that a framework invokes it.

Pytest fixtures can supply receiver types to unannotated test parameters when
there is a unique provider and a simple constructor return/yield or a normal
return annotation. Lookup respects class, module and nearest ancestor conftest
precedence, literal fixture names and imported decorator aliases. Direct
parametrization overrides fixtures; explicit test parameter annotations retain
precedence. Unknown or ambiguous inputs stay unresolved instead of acquiring a
type from their name. Inherited/re-exported fixtures, provider chains and
conditional/try-wrapped yields and return/yield inside `with`/`async with`
context managers are outside this bounded inference; check their
source before interpreting empty callers. Ordinary async fixtures require
pytest-asyncio decorator evidence for propagation.

After upgrading, perform a full index rebuild before comparing results; an
unchanged-file refresh cannot add new analysis facts. Thereafter Python changes
refresh collected test files, conftests and known fixture-provider files because
fixture discovery has dependencies beyond imports. Independent type-input refresh
can still make the work approach a full build. Plain business functions named
`test_*` and the word `fixture` alone do not identify a pytest project. Static
repository-root pytest collection patterns are supported; changing that
configuration requires a full rebuild.

For local conditional assignments, ArcGraph can join two proven instances of
the same class. Supported evidence is a named constructor or an annotated
parameter, with an explicit `is None` / `is not None` guard for nullable inputs.
An unannotated `config=None` parameter remains unknown even when its fallback
branch constructs `Config()`. Do not interpret this extension as interprocedural
parameter inference or complete production caller recovery.

Configuration resources have no defining source path: consult each reference
edge for its source location and syntax. A `config:dynamic:` target records an
access with `key_resolution=unknown`, not a known shared key or a resolved runtime
value. Literal environment keys still share one resource identity.

When a target is ambiguous, select an explicit typed candidate ID. The failed
query returns no target-scoped unresolved diagnostics; this empty result does
not establish that the intended symbol has no unresolved calls. Only an omitted
target in the unresolved query requests project-wide diagnostics.

The MCP surface does not expose the CLI commands `symbol`, `callers`,
`callees`, `tests`, or `impact`. Do not silently open a shell to simulate those
tools when a task permits MCP only. When shell access is authorized, use
CLI for these fine-grained reads, index build/sync and local diagnostics.

## Know When ArcGraph Is Not The Right Tool

Use the host's normal text search for exact strings and direct file reading for
exact source text. Use ArcGraph for structural relationships, target
resolution, impact, entrypoint flow, and cross-file evidence. Check the reported language capabilities in mixed-language repositories.

Do not request raw, unbounded graph exports by default. A human-authorized,
offline diagnostic may use a raw CLI payload, but it is outside the MCP-only
task and must not be treated as normal agent context.

## Record Feedback

When `arcgraph_record_trial_feedback` appears in `list_tools`, call it when
ArcGraph is inaccurate, unavailable, stale, truncated, slow, hard to discover,
missing a needed capability, or causes a fallback. The tool appends one
operator-enabled local record; it is non-destructive and has no network access,
but it is not a read-only analysis call. Use a stable `client_event_id` for a
retry so the append remains idempotent.

For each ordinary task, record:

- success or failure and expected versus observed behavior;
- any human rescue;
- which MCP tools were used;
- whether the index and decisive payloads were fresh;
- truncation, unresolved targets, or capability degradation;
- where grep or direct file reading was chosen instead, and why;
- where a missing low-cost MCP query changed the workflow.

Do not copy source, prompts, paths, repository identifiers, raw payloads, or raw
metrics into feedback. The tool intentionally has no free-text field. Choose
only its documented enum values and bounded tool identifier. If the feedback
tool is absent, record the same bounded facts through an authorized CLI
`arcgraph feedback record` call or leave them for the operator; do
not open a shell silently when the task permits MCP only. Missing optional feedback
is not a blocker and does not require enabling it to finish the task. Record
actual human interventions separately from suggestions to change setup.
`estimated_tokens` is a tool estimate of response size, not measured Agent
usage or billed tokens.

## Rules And Boundaries

These rules apply to humans and AI agents using ArcGraph in private alpha.
They are written for local source-checkout workflows and do not authorize
public release, package publishing, tags, GitHub Releases, branch protection,
or GitHub settings changes.

### Default Scope

- Use ArcGraph as a local-first read-side context engine.
- Prefer `current`, `context`, `explain`, `impact`, `tests`, and `ci`
  for agent workflows.
- Use MCP tools through `arcgraph mcp serve` only as a local stdio server.
- Treat generated `output/arcgraph` artifacts as local ignored state.

### Read-Only Defaults

- MCP analysis, Change Safety preview/read/compute, and Agent-help tools are
  read-only. An operator-selected absolute `--feedback-log` conditionally adds
  one disclosed non-destructive local append tool; it has no network access and
  must not be described as read-only.
- `arcgraph_record_learning` is proposal-only and does not persist external
  memory unless a host explicitly wires a connector.
- Server startup must not auto-build, write source files, edit agent configs,
  publish packages, or change repository settings.

### Source Snippets

- Keep source snippets disabled by default.
- Do not pass `--include-source` or `--expose-source-snippets` unless the human
  workflow explicitly approves source exposure.
- Inspect the `source_snippets` payload field before forwarding context to
  external systems.

### Before Editing Shared Code

- Run `arcgraph current` and inspect freshness, schema, warnings, and language
  capabilities.
- Run `arcgraph explain TARGET --detail-level summary` for the specific symbol,
  route, resource, or protocol fact.
- Run `arcgraph impact TARGET --profile review_default` before
  changing shared symbols.
- Run `arcgraph tests TARGET` to identify related tests and coverage gaps.

### Before Commit

- Run focused tests for the changed behavior.
- Run `arcgraph ci` when graph behavior or docs gates changed.
- Run Black and Ruff for changed Python files.
- Stage files explicitly by path.
- Do not stage `output/`, `dist/`, `node_modules/`, caches, generated artifacts,
  smoke output, or local indexes.

### Capability Claims

- Do not claim any language is L4.
- Do not claim Go, C#, Java, Rust, C, C++, or Swift are default live
  compiler-backed extractors.
- Do not treat SCIP, OpenAPI, tree-sitter, or syntax-only facts as L3 language
  semantics.
- Do not treat external payload-backed L3 as live compiler-backed extraction.
- Keep dynamic, unsupported, ambiguous, stale, or missing evidence visible as
  warnings, lower confidence, or unresolved records.

### Stop And Ask A Human

Stop before:

- Destructive filesystem operations outside generated ArcGraph output.
- Security-sensitive changes, credential handling, or source snippet exposure.
- Schema, storage, resolver, or confidence model changes.
- Public visibility, package publishing, tags, GitHub Releases, branch
  protection, GitHub settings, or backup branch deletion.
- Network/HTTP MCP transport.
- Automatic agent config installers.
- Any change whose evidence cannot be verified locally.

## Private-Alpha CLI Contract

For private-alpha agent use, call ArcGraph as a local subprocess from the
repository root, or pass global `--repo-root PATH` before the subcommand. The
default stdout for dictionary payloads is JSON. The global `--human` flag is for
interactive humans and should not be used by JSON-parsing agents.

In the default JSON output mode, a recognized command's stdout carries a JSON
payload on success (exit 0) and also when its handler raises an unhandled
runtime/file/schema exception (exit 1 or 2); do not gate JSON parsing on exit
code zero for that case. Argument-parsing failures for any non-`change`
command instead exit 2 with empty stdout and only an argparse usage message on
stderr, before any handler runs; `arcgraph change`'s own argument-parsing
failures are the exception to that -- they exit 1 with a versioned JSON error
envelope on stdout regardless of `--human`. A command whose handler completes
normally but reports a nonzero status through its own payload (for example
`arcgraph help --tool UNKNOWN_TOOL`, or `ci --fail-on-warnings`) still prints
its normal payload in whatever mode is active. See
`arcgraph docs agent-cli-contract` for the exact boundaries between these
cases.

Callers should capture stderr, keep stdout for diagnostics, and use their
own subprocess timeout.

Before relying on a payload, check `schema_version`, then inspect `status`,
`freshness`, `warnings`, and `truncation`. Whole-index and raw QueryEngine
payloads use index schema `1.0.0`; bounded target-scoped payloads use
read schema `1.4.0` and expose the storage version as `index_schema_version`. Use
`--detail-level` and `--max-results` deliberately to bound payload size.
Since read schema `1.1.0`, `warnings` is a heterogeneous JSON array: each element
may be either a string or a structured object. Branch on the element type before
display or aggregation; do not pass the list directly to string-only operations
such as `"\n".join(warnings)`.
Since read schema `1.3.0`, `unknowns.kind=truncated_scope` is limited to omitted
response presentation; incomplete traversal or analysis data uses
`analysis_truncated_scope`. Branch on both kinds. Raising `max_results` cannot
repair an analysis limit.
Avoid `--include-source` by default; source snippets are off unless explicitly
requested. Generated indexes, evidence manifests, reports, and workbench
artifacts under `output/arcgraph` are local ignored artifacts and should not be
committed.

If `current` reports a missing, stale, unavailable, or schema-incompatible
index, run `arcgraph doctor` for diagnostics and then run `arcgraph build` or
`arcgraph sync --if-stale` as appropriate. Queries remain read-only; they never
silently rebuild the index. If JSON parsing fails despite exit
code 0, treat the response as an incompatible CLI surface and fall back to
`arcgraph doctor` plus a fresh build.

Recommended private-alpha agent-facing commands:

| Need | Command |
| --- | --- |
| Index state and freshness | `arcgraph current` or `arcgraph status` |
| Counts and graph shape | `arcgraph stats` |
| Environment and index troubleshooting | `arcgraph doctor` |
| Missing or stale index remediation | `arcgraph build` or `arcgraph sync --if-stale` |
| Debounced explicit synchronization | `arcgraph watch` |
| Bounded task package | `arcgraph context TARGET --detail-level summary` |
| Target-specific provenance | `arcgraph explain TARGET --detail-level summary` |
| Review blast radius | `arcgraph impact TARGET --profile review_default` |
| Directional traversal | `arcgraph callers TARGET` / `arcgraph callees TARGET` |
| Related tests | `arcgraph tests TARGET` |
| Exact high-risk reference verification | `arcgraph references TARGET` |
| Similar implementation family | `arcgraph similar TARGET` |
| Route mount and middleware chain | `arcgraph route GET /path` |
| Optional evidence state and plan | `arcgraph evidence status` / `arcgraph evidence plan` |
| Local graph health gate | `arcgraph ci` |
| CLI-per-call latency check | `arcgraph benchmark agent-startup` or `arcgraph benchmark suite` |

The commands above are the recommended private-alpha product-facing agent
surface. Debug and compatibility commands such as `bindings`, `types`,
`callsites`, raw non-compact relation queries, and report-generation commands
can still be useful for maintainers, but agents should prefer the compact
contracted surfaces above.

Compact Python caller example:

```python
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

INDEX_SCHEMA_VERSION = "1.0.0"
READ_SCHEMA_VERSION = "1.4.0"


def run_arcgraph(
    repo: Path,
    *args: str,
    expected_schema: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    completed = subprocess.run(
        ["arcgraph", "--repo-root", str(repo), *args],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    # Parse stdout before looking at the exit code: a handled failure prints a
    # JSON error envelope on stdout. An argument-parsing failure (non-`change`)
    # leaves stdout empty, so the exit code and stderr are all there is.
    payload = None
    if completed.stdout.strip():
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload = None
    if completed.returncode != 0:
        detail = (
            payload
            if payload is not None
            else completed.stderr.strip() or completed.stdout
        )
        raise RuntimeError(
            f"arcgraph {' '.join(args)} failed with exit "
            f"{completed.returncode}: {detail}"
        )
    if payload is None:
        raise RuntimeError("arcgraph returned empty or non-JSON stdout")
    if payload.get("schema_version") != expected_schema:
        raise RuntimeError(f"unsupported ArcGraph schema: {payload.get('schema_version')}")
    return payload


def context_for(repo: Path, target: str) -> dict[str, Any]:
    current = run_arcgraph(
        repo,
        "current",
        expected_schema=INDEX_SCHEMA_VERSION,
    )
    freshness = current.get("freshness", {})
    if current.get("status") == "unavailable" or freshness.get("stale"):
        raise RuntimeError(
            "ArcGraph index is missing or stale; run `arcgraph sync --if-stale`, "
            "or `arcgraph build` when the schema or resolver changed"
        )
    if current.get("warnings"):
        print("ArcGraph warnings:", current["warnings"])
    return run_arcgraph(
        repo,
        "context",
        target,
        "--detail-level",
        "summary",
        expected_schema=READ_SCHEMA_VERSION,
    )
```

## Private-Alpha MCP Server

When an agent client can host a local MCP stdio server, use the installed
candidate executable (or the source checkout for maintainer work):

```bash
arcgraph mcp serve --repo-root . --output-dir output/arcgraph
```

The default analysis/change/help surface is read-only and is documented in
`docs/mcp-usage.md`. Protocol `list_tools` reports the exact registered set and
`arcgraph_help` supplies bounded selection and recovery guidance. An absolute
`--feedback-log` conditionally adds one non-destructive local feedback append;
it does not write code or the index and has no network access. The server does
not auto-build indexes, persist external memory by default, auto-configure
agent clients, publish packages, or change repository settings. Source snippets
are disabled by default; only start the server with
`--expose-source-snippets` when the workflow explicitly allows source exposure.

If an MCP client is unavailable or unsuitable, keep using the subprocess JSON
contract above. Manual client examples live in the Client Integration Examples
section below.

## Task Context

Use `context` when an agent needs a bounded task package:

```bash
arcgraph context arcgraph.pipeline.indexer.ArcGraphIndexer --task "review indexing change" --detail-level summary
```

Use `explain` when the task is about one target and the agent needs direct edge
evidence, resolution strategy, fallback metadata, and confidence sources:

```bash
arcgraph explain arcgraph.pipeline.indexer.ArcGraphIndexer --detail-level detailed
```

Use `--include-source` only when source snippets are acceptable for the workflow.
Snippet exposure is off by default for compact agent payloads.

## Review And Risk

The default Agent workflow is one MCP `arcgraph_get_risk` call before editing.
It is the Change Preflight response: resolved target/candidates, blast radius,
test mapping, similar siblings, entrypoints, unknowns, evidence assurance, and
recommended next reads. Set `verify_references=true` only when a high-risk
TypeScript/JavaScript target warrants the additional Language Service work, or
when an imported SCIP index can answer it.

Use impact analysis before changing shared code:

```bash
arcgraph impact arcgraph.pipeline.indexer.ArcGraphIndexer --profile review_default
```

Use related tests before or after a change:

```bash
arcgraph tests arcgraph.pipeline.indexer.ArcGraphIndexer
```

Use CI checks when deciding whether the local graph is healthy enough for a
review or release gate:

```bash
arcgraph ci
```

The MCP facade exposes risk through `arcgraph_get_risk`; the CLI impact and
tests commands are focused shell surfaces for review workflows. `references`
confirms exact callsites, `similar` reports the pattern family and highlights
when only one member is stale, and `route` reports static mount context and
ordered middleware. All three preserve explicit non-claims for dynamic or
heuristic evidence.

## Architecture And Evidence

Use `architecture` for a high-level structure view:

```bash
arcgraph architecture
```

Use `evidence plan` when an agent needs to know which optional artifacts could
improve confidence:

```bash
arcgraph evidence plan --profile python_full
```

Use `unresolved` to keep dynamic or unsupported behavior visible:

```bash
arcgraph unresolved --release-blocking-only
arcgraph unresolved arcgraph.pipeline --category true_dynamic_call
```

Classification follows recorded evidence, not names or directory location. A
direct call through a visible runtime binding, such as a function parameter,
loop target, unresolved local alias, or module-level callback, adds bounded
evidence to its already-unresolved diagnostic and classifies as
`true_dynamic_call`. Classification does not manufacture a `dynamic_call` edge
or claim that the runtime target is known. An ordinary unresolved call remains
a release-blocking `static_candidate` even under `tests/`; only explicit
mock/assertion helper expressions use `mock_or_assertion`.

## Visual Audit Surfaces

Use a static workbench for local human inspection:

```bash
arcgraph visual workbench --output-dir output/arcgraph/reports/workbench --open
```

Use the loopback server for larger indexes:

```bash
arcgraph visual serve --host 127.0.0.1 --port 8765 --open
```

The visual surface is a local audit interface. It does not replace compact agent
payloads from `context`, `explain`, `impact`, or MCP tools.

## Practical Agent Loop

1. Run `arcgraph current` and check freshness, warnings, and language tiers.
2. Run `arcgraph context TARGET --task "..."`
3. Run `arcgraph explain TARGET` for the exact symbol, route, or resource.
4. Run `arcgraph impact TARGET`.
5. Run `arcgraph tests TARGET`.
6. Make the code change.
7. Rebuild or reindex as appropriate.
8. Run `arcgraph ci` and the project test command.

## Client Integration Examples

ArcGraph supports two local agent integration paths: the CLI subprocess JSON
contract above, and the local stdio MCP server. Use the explicit
`arcgraph setup --client claude|codex|cursor|hermes|pi` onboarding command to
build/refresh one index and write the selected client configuration -- see
[client-setup.md](client-setup.md) for scopes, backups, client trust
requirements, and the current support matrix. Ordinary MCP reads never edit
client configuration; legacy `trial setup` does not auto-edit client
configuration either. CLI and MCP are not feature-equivalent: CLI agents use
`arcgraph help`, while MCP agents discover the registered surface through
`list_tools` and call `arcgraph_help` for usage guidance.

The examples below are manual templates only. For generated configuration, use
the explicit setup command above.

```bash
arcgraph mcp serve --repo-root /path/to/repo --output-dir output/arcgraph
```

Install the optional runtime only when serving MCP. From a source checkout:

```bash
python -m pip install -e '.[mcp]'
```

For a local candidate wheel, install the wheel's `mcp` extra before using the
same command templates:

```bash
python -m pip install '/path/to/arcgraph-VERSION-py3-none-any.whl[mcp]'
```

Keep `--expose-source-snippets` off unless the repository and workflow explicitly
allow source exposure.

### Claude Code

Example MCP server entry:

```json
{
  "mcpServers": {
    "arcgraph": {
      "command": "arcgraph",
      "args": [
        "mcp",
        "serve",
        "--name",
        "ArcGraph Project",
        "--repo-root",
        "/path/to/repo",
        "--output-dir",
        "/path/to/repo/.arcgraph-trial/index"
      ]
    }
  }
}
```

Build the index before starting the server:

```bash
arcgraph --repo-root /path/to/repo --output-dir /path/to/repo/.arcgraph-trial/index build
arcgraph --repo-root /path/to/repo --output-dir /path/to/repo/.arcgraph-trial/index current
```

### Codex Or Generic MCP Client

For Codex use `arcgraph setup --client codex`; it writes a real TOML
`[mcp_servers.NAME]` entry. The following JSON only describes a generic launcher,
not the contents of Codex config.toml.

Use the same stdio command shape:

```json
{
  "name": "arcgraph",
  "transport": "stdio",
  "command": "arcgraph",
  "args": [
    "mcp",
    "serve",
    "--repo-root",
    "/path/to/repo",
    "--output-dir",
    "/path/to/repo/.arcgraph-trial/index"
  ]
}
```

If the client expects absolute output paths, pass one explicitly. Otherwise
relative `output/arcgraph` resolves under `--repo-root`.

Each single-project server deliberately keeps `repo_id=default`; omit
`--repo-id` so Agent calls that omit the optional field work. For multiple
projects, create multiple named server entries with distinct paths and
processes rather than assigning custom repo ids inside one implicit-default
client flow.

### Cursor Or Generic MCP JSON Client

Use a client-specific JSON location, but keep the command local and stdio:

```json
{
  "arcgraph": {
    "command": "arcgraph",
    "args": [
      "mcp",
      "serve",
      "--repo-root",
      "/path/to/repo",
      "--output-dir",
      "output/arcgraph"
    ]
  }
}
```

This manual template does not write configuration. Explicit
`arcgraph setup --client cursor` writes the project's `.cursor/mcp.json`.
Validate the server command manually with:

```bash
arcgraph mcp serve --help
```

### Aider Or CLI Fallback

When direct MCP hosting is not applicable, use the CLI subprocess contract:

```bash
arcgraph help
arcgraph --repo-root /path/to/repo current
arcgraph --repo-root /path/to/repo context <target> --detail-level summary
arcgraph --repo-root /path/to/repo explain <target> --detail-level summary
arcgraph --repo-root /path/to/repo impact <target> --profile review_default
arcgraph --repo-root /path/to/repo tests <target>
```

See the Private-Alpha CLI Contract section above for exactly when stdout
carries JSON versus when it is empty; do not assume exit code zero is the
only case with parseable stdout. Treat nonzero exit codes, schema mismatches,
missing indexes, stale indexes, and warning-heavy payloads as conditions to
inspect before editing.

CLI does not have MCP's protocol-level `list_tools`. The operator must tell a
CLI Agent to run `arcgraph help`; this returns the same Agent-oriented selection
and recovery facts, while `arcgraph --help` remains argparse syntax and
`arcgraph docs` remains the complete reference. Do not claim CLI and MCP are
feature-equivalent.

CLI and MCP metrics are different contracts. The global CLI `--metrics-log`
records CLI latency/status events under an older, local-sensitive event
contract that may contain command failures; do not share a raw CLI log. The
MCP server's `--metrics-log` is the privacy-bounded, opt-in per-tool log. See
[mcp-usage.md](mcp-usage.md) for the MCP option reference and the
[External Trial Guide](external-trial-guide.md#6-optional-local-metrics-and-feedback)
for both, including how to review a shareable aggregate with
`arcgraph metrics PATH --trial-summary`. Feedback error codes and record limits
are in the
[External Trial Guide](external-trial-guide.md#feedback-machine-contract).
