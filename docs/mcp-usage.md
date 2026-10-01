# MCP And Tooling Usage

ArcGraph includes a local stdio MCP server. Its default analysis, Change Safety,
and Agent-help surface is read-only. An operator can separately enable one
privacy-bounded local feedback append tool. The v0.1.0rc7 external trial
validates the server from an installed wheel; source-checkout use remains
available for maintainers. Starting the MCP server does not configure clients,
publish a package, upload feedback, or enable network transport. For explicit
client configuration use [setup](client-setup.md).

## Server Command

Start the server from the analyzed repository after installing the external
trial wheel with its `mcp` extra:

```bash
arcgraph mcp serve --repo-root . --output-dir output/arcgraph
```

To opt in to local per-tool metrics, add an operator-controlled path:

```bash
arcgraph mcp serve --repo-root . --output-dir output/arcgraph \
  --metrics-log /private/local/path/arcgraph-mcp.jsonl
```

The equivalent module entrypoint is:

```bash
python -m arcgraph.interfaces.mcp_server --repo-root . --output-dir output/arcgraph
```

The server uses stdio transport only. In a source checkout, install the
optional runtime only when serving MCP:

```bash
python -m pip install -e '.[mcp]'
```

ArcGraph supports MCP Python SDK `>=2.0.0,<3.0.0` and uses its public
`MCPServer` API. Startup diagnostics distinguish a missing MCP distribution,
an installed version outside that range, and a supported-version installation
whose public runtime API is damaged or incomplete.

`arcgraph mcp serve --help` and `arcgraph docs mcp-server` work without the MCP
runtime dependency installed. If the runtime dependency is missing when serving,
ArcGraph returns an actionable install message instead of a confusing import
crash.

The MCP SDK installs the OpenTelemetry API, but ArcGraph does not install or
configure an exporter, collector, or outbound telemetry destination. An
operator or host process may configure a global OpenTelemetry provider; spans
produced by that host-owned provider are host behavior, and ArcGraph does not
use private SDK hooks to suppress or reconfigure it.

## Scope And Defaults

| Option | Default | Behavior |
| --- | --- | --- |
| `--repo-root` | `.` | Repository exposed to read-only MCP tools. |
| `--output-dir` | `output/arcgraph` | Existing ArcGraph index directory; relative paths resolve under `repo_root`. |
| `--repo-id` | `default` | Repository id passed by MCP clients. |
| `--allowed-root` | resolved `repo_root` | May be repeated; requests outside allowed roots are rejected or sanitized. |
| `--transport` | `stdio` | Only stdio is supported in the rc7 external trial. |
| `--expose-source-snippets` | off | Source snippets remain disabled unless this flag is explicitly used and the request asks for source. |
| `--metrics-log` | off | Explicit local JSONL metrics, one event per completed tool call. |
| `--feedback-log` | off | Absolute private local JSONL path; enables the optional Agent feedback tool. |

Server startup reads configuration and registers tools. It does not run
`arcgraph build`, mutate the target repository, write user agent configuration,
publish packages, create tags, create releases, or change GitHub settings.

Protocol `list_tools` is the authoritative inventory for one running process.
The server always registers `arcgraph_help`, whose bounded topics explain
workflow, selection, result interpretation, recovery, CLI-only fallbacks, and
feedback. Supplying `--feedback-log ABSOLUTE_PATH` additionally registers
`arcgraph_record_trial_feedback`; omitting it creates no feedback file and
keeps the registered surface read-only. A particular Agent client may filter
tool metadata before presenting it to its model, so validate discovery in the
actual configured client.
Help can still describe that known optional feedback capability while it is
disabled, but its `tool_registered` and `mcp_enabled` fields remain false; do
not mistake discoverable documentation for protocol availability.

Local MCP metrics are disabled unless `--metrics-log` is supplied. Each event
contains a timestamp, tool name, success/error status, duration, serialized
payload byte size, estimated token count, truncation flag, and enum-only
freshness status. Events never contain arguments or targets, repository ids,
filesystem paths, source or snippets, returned payload text, raw exception
text, client identity, or prompts. Writes are concurrency-safe and
best-effort; a write failure does not change the tool result and emits at most
one bounded diagnostic to stderr.

Use `arcgraph metrics PATH --trial-summary` for a path- and timestamp-free
aggregate of counts, latency, payload size, estimated tokens, truncation, and
freshness. Do not share the raw metrics JSONL.

## One Installation, Multiple Projects

Reuse one installed ArcGraph executable, but start one stdio server process per
project. Give every process a distinct human-readable server name and separate
repository root, index output, metrics log, and feedback log. Each
single-repository server keeps `repo_id=default`; do not invent a project id
unless the Agent is also configured to pass that exact id on every tool call.

```bash
arcgraph mcp serve \
  --name "ArcGraph Project A" \
  --repo-root /absolute/projects/project-a \
  --output-dir /absolute/projects/project-a/.arcgraph-trial/index \
  --metrics-log /absolute/projects/project-a/.arcgraph-trial/metrics/mcp.jsonl \
  --feedback-log /absolute/projects/project-a/.arcgraph-trial/feedback/agent.jsonl

arcgraph mcp serve \
  --name "ArcGraph Project B" \
  --repo-root /absolute/projects/project-b \
  --output-dir /absolute/projects/project-b/.arcgraph-trial/index \
  --metrics-log /absolute/projects/project-b/.arcgraph-trial/metrics/mcp.jsonl \
  --feedback-log /absolute/projects/project-b/.arcgraph-trial/feedback/agent.jsonl
```

These are two independent local processes, not a multi-tenant server. ArcGraph
does not edit either Agent client's configuration. On POSIX, newly created
metrics and feedback files use private modes and newly created state
directories use private modes. Metrics and feedback paths must be distinct;
the server rejects a shared file before registering tools. On Windows, ArcGraph
prevents unsafe file following and serializes writers but does not claim that
POSIX mode bits describe Windows ACL guarantees. Existing unsafe local-state
files are rejected and are never silently `chmod`-repaired.

## Tool Surface

The registered tool names are:

| Tool | Use |
| --- | --- |
| `arcgraph_index_status` | Return freshness, counts, build identity, disk use, protected pins, and a read-only prune preview. |
| `arcgraph_get_context` | Return compact structural context for editing or review agents. |
| `arcgraph_explain` | Explain target resolution, direct edge evidence, fallbacks, and confidence sources. |
| `arcgraph_get_risk` | Default Change Preflight: resolution, impact, tests, similar siblings, entrypoints, unknowns, assurance, next reads, and optional exact references. |
| `arcgraph_entrypoint_flow` | Trace a route, MCP tool, worker, or queue entrypoint through indexed flow edges. |
| `arcgraph_get_why` | Explain structural reasons plus optional external historical memory when configured. |
| `arcgraph_find_similar` | Find implementations with similar normalized AST structure and supporting evidence. |
| `arcgraph_record_learning` | Create a read-only external memory candidate; it does not persist memory by default. |
| `arcgraph_preview_change_plan` | Compute a Surgical Change Safety plan preview without activating a plan or creating a pin. |
| `arcgraph_get_change_plan` | Read the current view of one activated change plan. |
| `arcgraph_list_change_plans` | List current change plan views without mutating state. |
| `arcgraph_get_graph_delta` | Compute the current graph delta for one exact plan revision. |
| `arcgraph_verify_change` | Compute a verification result without persisting a report or lifecycle transition. |
| `arcgraph_help` | Return bounded Agent-oriented workflow, tool-selection, result, recovery, and feedback guidance. |
| `arcgraph_record_trial_feedback` | Conditionally registered local append; record one strict privacy-bounded feedback event when `--feedback-log` is enabled. |

`arcgraph_get_risk` defaults to `max_results=3` for a compact preflight. Set a
larger value when the response must carry more target reports, blast-radius
members, tests, or exact-reference rows; omissions are reported in
`truncation` and `assurance`. Each `unknowns` item classifies itself as an
`evidence_gap`, `analysis_limit`, or `response_limit` and lists the bounded
`resolvable_by` actions. Only `response_limit` can recommend a higher
`max_results`; traversal or analysis-data truncation instead recommends a
narrower target or direct follow-up queries. Absence of exact-reference or
dynamic-runtime evidence therefore remains explicit, and names what would
establish it — `verify_references` for the first — without masquerading as a
newly discovered defect.

This is an intentional read-schema `1.3.0` migration: `truncated_scope` now
means response-presentation truncation only, and traversal or analysis-data
truncation uses `analysis_truncated_scope`. Consumers that switch on
`unknowns.kind` must recognize both values; do not silently treat an unknown
analysis scope as a complete result.

`arcgraph_record_learning` is proposal-only and is intentionally documented
because the name sounds write-like. In the default server it returns a proposal
payload through the read-only facade and does not write external memory.

The five change tools are read/preview/compute only. MCP does not expose plan
approval, reject, abandon, archive, evidence persistence or purge, audit export,
test execution, code editing, or Git/remote writes. Those remain CLI-only. See
`arcgraph docs change-safety` and
[change-safety.md](change-safety.md).

The feedback tool is the sole optional write in this MCP surface. It never
changes source, Git state, graph indexes, plans, evidence, pins, or Agent
configuration. Its strict input contains no free text, paths, targets,
repository ids, source, prompts, arbitrary metadata, or raw errors. A repeated
valid `client_event_id` is idempotent. Review a path-free aggregate with
`arcgraph feedback summarize ABSOLUTE_PATH`; do not share the raw JSONL.

MCP intentionally does not expose every CLI query or operational command.
`symbol`, `callers`, `callees`, `tests`, and `impact` remain CLI-only, as do
build/reindex/prune and Change Safety mutations. For CLI-based Agents,
`arcgraph help` exposes the shared Agent guidance, but CLI has no automatic
protocol inventory.

## Index State Behavior

The MCP server uses the index already present at `--output-dir`. If the index is
missing, stale, unavailable, or schema-incompatible, tools return status and
warning payloads. ArcGraph does not hide that state by auto-building during MCP
startup.

Recommended recovery:

```bash
arcgraph doctor
arcgraph build
# or, after compatible source edits:
arcgraph sync --if-stale
# or keep an explicit debounced synchronizer running:
arcgraph watch
```

Every stale target response includes a machine-readable `recovery_action`.
Read-only MCP calls never execute it and never update `current.json`. A failed
explicit sync preserves the previously published current build.

Agent clients should inspect `schema_version`, `index_schema_version`, `status`,
`freshness`, `warnings`, `truncation`, and `source_snippets` before trusting
returned payloads. `arcgraph_index_status` reports index schema `1.0.0`; bounded
target-scoped tools report read schema `1.4.0` and keep the underlying storage
version in `index_schema_version`. Target lists are de-duplicated and bounded to
100 unique values by default; a host may configure a lower ceiling.

Since read schema `1.1.0`, `warnings` is a heterogeneous JSON array. Each element
may be a string or a structured object, so clients must branch on the element
type instead of using string-only operations such as `"\n".join(warnings)`.
Structured warnings expose `message`, `kind`, and an optional `path`.

## CLI Equivalents

When a runtime does not host MCP, agents can call the CLI surfaces:

| Agent need | CLI command |
| --- | --- |
| Index status | `arcgraph current` |
| Counts and graph shape | `arcgraph stats` |
| Task context | `arcgraph context TARGET --detail-level summary` |
| Target explanation | `arcgraph explain TARGET --detail-level summary` |
| Impact analysis | `arcgraph impact TARGET --profile review_default` |
| Test recommendations | `arcgraph tests TARGET` |
| Exact reference verification | `arcgraph references TARGET` |
| Similar pattern family | `arcgraph similar TARGET` |
| Route middleware and mount chain | `arcgraph route GET /path` |
| Architecture | `arcgraph architecture` |
| Evidence status | `arcgraph evidence status` |
| Evidence next steps | `arcgraph evidence plan --profile python_full` |
| CI health | `arcgraph ci` |
| Change plan view | `arcgraph change --repo-id REPO show --plan-id ID` |
| List change plans | `arcgraph change --repo-id REPO list` |
| Graph delta for a revision | `arcgraph change --repo-id REPO diff --plan-id ID --revision N --plan-content-digest DIGEST` |
| Visual audit | `arcgraph visual workbench` or `arcgraph visual serve --host 127.0.0.1` |

`arcgraph_preview_change_plan` has no read-only CLI equivalent. The closest
command is the `plan` subcommand of `arcgraph change`, and it is not a preview:
it creates and activates a plan and writes a durable pin. Use the MCP tool when a
caller needs plan output without changing state.

See the Client Integration Examples section of
[agent-reading-guide.md](agent-reading-guide.md) for manual client examples
and CLI fallback guidance.

## Lower-Level Host Example

ArcGraph still includes a minimal example script at
`docs/examples/mcp_readonly_host.py`. It reuses the same server helpers and is
useful when embedding ArcGraph's read-only facade inside another compatible MCP
runtime:

```bash
python docs/examples/mcp_readonly_host.py --repo-root . --output-dir output/arcgraph
```

For normal alpha use, prefer `arcgraph mcp serve`.
