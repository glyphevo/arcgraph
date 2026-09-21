# Example: Impact Analysis

Use impact analysis before changing shared symbols, routes, resources, or
frontends.

```bash
arcgraph build
arcgraph impact arcgraph.pipeline.indexer.ArcGraphIndexer --profile review_default
arcgraph callers arcgraph.pipeline.indexer.ArcGraphIndexer --detail-level summary
arcgraph callees arcgraph.pipeline.indexer.ArcGraphIndexer --detail-level summary
arcgraph tests arcgraph.pipeline.indexer.ArcGraphIndexer
```

For route-oriented review:

```bash
arcgraph route GET /api/example
arcgraph impact route:GET:/api/example
```

For a human-readable report:

```bash
arcgraph report pr --output output/arcgraph/reports/pr-impact.md
arcgraph report html "arcgraph.pipeline.indexer.ArcGraphIndexer" --output output/arcgraph/reports/indexer-report.html
```

`impact` is not proof that every runtime path was observed. It is a compact
blast-radius view over indexed static, protocol, resource, and imported evidence
with confidence metadata.
