# Language Support And Capability Tiers

ArcGraph reports language capability by tier so users and agents can distinguish
file discovery, protocol facts, semantic static frontends, and future
evidence-augmented high-availability profiles.

## Tier Model

| Tier | Meaning | Current use |
| --- | --- | --- |
| L0 | Workspace/file discovery | Source-root detection and scanner coverage. |
| L1 | Structural static facts | File, package, directory, and structural graph facts. |
| L2 | Protocol evidence facts | Explicit artifacts such as SCIP protocol JSON and OpenAPI. |
| L3 | Semantic static frontend | Native or validated semantic graph frontends. |
| L4 | Evidence-augmented high availability | Not claimed yet. Requires stronger evidence gates and maturity criteria. |

Confidence is separate from tier. A confirmed protocol fact is still L2 when it
comes from a protocol artifact. An inferred handler match is still not runtime
proof. Unsupported behavior should remain visible as warnings, diagnostics, or
unresolved records.

## Current Capability Summary

| Language or input | Current tier | How it is enabled | Public-safe claim |
| --- | --- | --- | --- |
| Python | L3 | Default native semantic static frontend. | Receiver-aware Python static graph with framework/resource/test semantics and visible gaps. |
| TypeScript | L3 | Native frontend when a TypeScript compiler API is resolvable at analysis runtime. | Static TS semantic graph with module/export/resource/framework facts and checker-backed receiver call resolution where deterministic. |
| JavaScript | L3 | Native frontend through the same resolvable TypeScript compiler API. | Static JS semantic graph through the same frontend, with conservative module and framework semantics. |
| Next.js | Framework layer over L3 TS/JS | Default TS/JS analysis over route and component files. | Deterministic filesystem route and component semantics where statically visible. |
| Vue | Framework layer over L3 TS/JS | Default TS/JS analysis over Vue files. | Component/import/render facts where statically visible; dynamic components remain warnings. |
| Go / C# / Java / Rust / C / C++ / Swift | L3 when explicit external payload is valid; otherwise L2 through SCIP reporting. | Validated external semantic extractor payload or explicit SCIP protocol input. | Payload-backed L3 only, not default live compiler extraction for those languages. |
| SCIP | L2 | `arcgraph build --scip-graph-index PATH`. | Confirmed protocol definitions and references only. |
| OpenAPI | L2 | `arcgraph build --openapi-spec PATH`. | Confirmed protocol route/schema facts and inferred operationId handler matches when unique. |

No language is claimed as L4. External L3 payload support is explicit and
payload-backed; it is not default live compiler extraction for those languages.
SCIP and OpenAPI facts remain protocol evidence and are not relabeled as L3
language semantics.

## Native L3 Frontends

Python and TypeScript/JavaScript are the native L3 frontends in the current
default registry.

Python analysis includes static modules, classes, functions, methods, imports,
calls, receiver/type propagation, framework adapters, resource mapping, test
signals, and unresolved triage. It remains conservative for dynamic dispatch,
reflection, metaprogramming, monkeypatching, and framework magic.

TypeScript/JavaScript analysis uses the TypeScript compiler API. It covers
committed static scenarios across `.ts`, `.tsx`, `.mts`, `.cts`, `.js`, `.jsx`,
`.mjs`, and `.cjs`: imports, exports, functions, classes, interfaces, type
aliases, enums, components, hooks, JSX/render edges, resource hints, route/API
calls, structural `similar_to` edges, and typechecker-backed receiver call
resolution where a local declaration can be mapped deterministically.
Standalone `.d.ts`, `.d.mts`, and `.d.cts` inputs preserve declaration-file
provenance. Coexisting same-stem sources in different lanes (`.ts`, `.tsx`,
`.js`, `.jsx`, and the explicit ESM/CommonJS lanes) are separate modules with
distinct identities rather than one collapsed module. A same-stem runtime file
counts as compiler output only with compiler provenance about that file -- a
sibling source map or an inline `sourceMappingURL` marker. A matching
declaration companion is deliberately not accepted, because under
`emitDeclarationOnly` it proves the TypeScript source was compiled rather than
the runtime file being emitted; a hand-written shim beside a TypeScript source
stays indexed, and a bare `tsc` emit carrying neither signal is indexed as
source. Skipped emit artifacts are disclosed in one build warning. Dynamic
imports, runtime plugin configuration, package-manager linking, and broad
bundler behavior remain limited.

Node.js and the TypeScript compiler API are runtime dependencies for this
frontend, not merely test dependencies. The ArcGraph Python wheel contains its
`.mjs` extractor but does not bundle `node_modules/typescript`. When no compiler
API is resolvable from the analyzed project or another documented local runtime
location, the build succeeds with `typescript_frontend_unavailable` recorded in
both `summary.json` and `diagnostics.jsonl`.

The workflow matrix is defined to validate Node.js 20 with TypeScript 5.4.5 (the
supported floor) and Node.js 20/22/24 with TypeScript 5.9.3 (the current tested
line). GitHub Actions runs this matrix; a passing run is evidence only for the
commit it ran on. The analyzed project must supply the TypeScript compiler API
either way, since ArcGraph never bundles it.

The v0.1.0rc7 external-trial acceptance scope is Python analysis through the
installed CLI plus local stdio MCP. The default MCP analysis/change/help
surface is read-only; optional local feedback is a separate disclosed append.
TS/JS may work when its runtime is available, but it is not part of that trial
guarantee.

## Framework Semantics Over TypeScript/JavaScript

Next.js and Vue are not independent languages in ArcGraph. They are framework
semantic layers over the TypeScript/JavaScript frontend.

Next.js support covers deterministic route and component facts from app/pages
file conventions and exported HTTP handlers. Dynamic middleware and rewrites are
reported as unsupported warnings instead of invented edges.

Express support emits static mounted routes as `route` nodes and links handlers
when imports, routers, mount paths, and registrations are statically
resolvable. Root middleware chains are recognized; unresolved or excessively
fan-out mount graphs remain bounded and disclosed as build warnings rather than
invented as complete topology.

Vue support covers statically visible components, imports, and render edges.
Dynamic component resolution remains warning-based.

## Explicit External L3 Payloads

Go, C#, Java, Rust, C, C++, and Swift L3 support is backed by validated external
semantic extractor payloads. ArcGraph validates schema version, extractor
identity, declared tier, supported node/edge kinds, path containment, semantic
edge evidence, and resolution strategy before merging a payload into the graph.

The current repository provides wrappers and fixture-backed validation for these
external L3 frontends. It does not ship default live compiler-backed extractor
commands for those languages. Missing or invalid toolchains should remain
visible as unavailable/degraded diagnostics rather than becoming false support.

See [docs/examples/external-l3-payload.md](examples/external-l3-payload.md) for
a worked example and `arcgraph docs frontend-contract` for the full
`LanguageFrontend` implementation contract and extension boundary.

## Explicit L2 Protocol Evidence

SCIP protocol input is explicit:

```bash
arcgraph build --scip-graph-index output/arcgraph/scip-protocol.json
```

SCIP protocol facts are confirmed definitions and references from an existing
`scip print --json` style payload. ArcGraph does not infer calls, imports, or
language-specific semantics from SCIP references.

OpenAPI protocol input is explicit:

```bash
arcgraph build --openapi-spec PATH_TO_YOUR_OPENAPI_SPEC
```

OpenAPI route and schema facts are confirmed protocol facts. Handler matches are
inferred only when `operationId` uniquely matches an indexed local function or
method. Ambiguous, unmatched, outside-repo, malformed, or invalid artifacts stay
visible as warnings. There is no `docs/openapi.json` shipped in this repository
to point at; the repository's own OpenAPI fixture used by its tests lives at
`arcgraph/tests/fixtures/openapi_protocol_baseline/openapi.json` if you want a
working example to inspect.

## Not Yet Claimed

ArcGraph does not currently claim L4 for any language. L4 would require stronger
evidence profiles, stability criteria, documented limits, and release gates than
the current public surface.
