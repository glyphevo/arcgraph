# Change Preflight

ArcGraph's default coding-agent promise is intentionally bounded:

> Given a symbol, file, or route that is about to change, return the known
> impact surface, related tests, similar implementations, entry paths, evidence
> boundary, and concrete follow-up reads.

## Default call

Use the read-only MCP tool `arcgraph_get_risk` with one or more targets. A
normal summary defaults to `max_results=3` so the response stays compact. Raise
that explicit limit when a broader evidence set is required; CI and native
providers honor their requested limit and do not apply a second hidden display
cap. The summary returns:

- deterministic target resolution and ambiguity candidates;
- affected symbols/modules, entrypoints, resources, and related tests;
- structurally similar siblings and pattern-family signals;
- unknowns, recommended next reads, and a bounded token estimate. Every unknown
  includes a stable `class` and `resolvable_by` list, so missing evidence,
  analysis traversal/data limits, and response-presentation limits remain
  distinct. An analysis limit recommends a narrower target or direct follow-up
  queries; only a response limit recommends raising `max_results`;
- `assurance`, including index scope/freshness, confidence buckets, unresolved
  or dynamic gaps, response/analysis truncation, completeness, and non-claims.

Read schema `1.3.0` assigns distinct stable kinds to the two truncation classes:
`truncated_scope` is a response-presentation limit, while
`analysis_truncated_scope` means traversal or analysis data was incomplete.
Clients matching only the older `truncated_scope` value must add the analysis
kind; treating an analysis limit as a larger-response request is unsafe.

For a high-risk TypeScript/JavaScript target, set `verify_references=true`.
ArcGraph first uses imported SCIP reference edges when present, otherwise it
invokes the analyzed project's TypeScript Language Service. Exact verification
is target-scoped and read-only; it is not a claim about reflection, runtime
plugins, or cross-repository callers.

## Safe target resolution

An ordinary symbol or path target (the common case for `context`, `explain`,
`impact`, `tests`, and similar queries) uses this precedence:

1. stable ArcGraph id;
2. exact fully-qualified name;
3. a normalized-id retry: if the query has no `:` in it, ArcGraph tries it as
   a bare qualname missing its stable-id kind prefix (`method:`, `fn:`,
   `component:`, `class:`, `interface:`, `type_alias:`, `enum:`, `mod:`)
   and checks each kind for an exact match;
4. exact repository-relative or absolute path;
5. unique bare symbol name or unique qualified-name suffix.

An ambiguous name produces ordered candidates and no resolved symbol. A path
may intentionally resolve to multiple definitions; a symbol query may not.

This precedence is not universal. `arcgraph_entrypoint_flow` and `worker`-flow
selectors use their own dedicated entrypoint/worker matching instead of this
precedence entirely; they never reach the steps below. For every other query,
ArcGraph first tries a route-shape match (`route:METHOD:/path` or
`METHOD /path`) before this chain; a query that matches a route is resolved
there and never enters the chain above.

## Follow-up CLI queries

```bash
arcgraph impact TARGET --profile review_default
arcgraph tests TARGET
arcgraph references TARGET
arcgraph similar TARGET
arcgraph route GET /path
```

`references` reports line-level exact reference facts. `similar` marks
low-information functions and highlights a family where only one member has
changed since the index. `route` reports static mount context, ordered
pre-handler middleware and terminal handler roles, naming-only authorization
hints, and relevant dynamic-registration gaps.

## Freshness lifecycle

Queries and MCP readers never write an index. Use:

```bash
arcgraph sync --if-stale
arcgraph watch --poll-interval 0.25 --debounce 0.5
```

`sync` publishes an incremental build atomically. If it fails, the old
`current.json` remains usable. Stale query payloads expose a machine-readable
`recovery_action`; callers decide whether and when to execute it.

`arcgraph_index_status` also reports exact runtime/build identity and a
read-only storage summary. The storage section lists current size, build count,
pin-protected versions, reclaimable candidates, and a dry-run prune command.
Status never deletes files; applying prune requires an explicit `--apply`.

See [Agent Reading Guide](agent-reading-guide.md) for interpretation limits.
