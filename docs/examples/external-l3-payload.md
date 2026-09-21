# Example: External L3 Payloads

External L3 frontends are for semantic payloads produced by trusted language
tooling outside ArcGraph. They are validated before graph merge.

Current external L3 wrappers cover Go, C#, Java, Rust, C, C++, and Swift. These
wrappers do not mean ArcGraph runs live compiler-backed extractors by default.
They mean ArcGraph can accept a valid payload for that language and report L3
only when the payload passes validation.

## Payload Expectations

An external payload must include:

- `schema_version`
- extractor identity and version matching the registered frontend
- language
- declared tier
- toolchain status
- supported node and edge kinds
- nodes
- edges
- warnings and metrics when applicable

Semantic edges must include evidence and a resolution strategy. Paths must stay
inside the repository. Unsupported node/edge kinds, missing evidence, missing
strategy, oversized output, invalid JSON, and repo-outside paths keep the
frontend unavailable or degraded instead of upgrading language support.

## Conceptual Flow

```bash
# Future or internal extractor command produces a protocol payload.
external-language-extractor --repo . --output output/arcgraph/go-l3-payload.json

# ArcGraph build consumes the validated payload through an explicitly configured
# external frontend or fixture-backed registry path.
arcgraph build
arcgraph current
```

The public CLI does not yet expose a generic `--external-l3-payload` flag. Use
this document as the capability boundary for external extractor integrations,
not as a copy-paste command for current default builds.

## What This Does Not Claim

- It does not claim live Go, C#, Java, Rust, C, C++, or Swift compiler extraction
  is automatic.
- It does not relabel SCIP references as L3.
- It does not treat parser-only facts as semantic L3.
- It does not claim L4.
