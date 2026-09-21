# Example: Agent Context

Use `context` for a compact task package and `explain` for a target-specific
provenance view.

```bash
arcgraph build
arcgraph context arcgraph.pipeline.indexer.ArcGraphIndexer --task "review indexing behavior" --detail-level summary
arcgraph explain arcgraph.pipeline.indexer.ArcGraphIndexer --detail-level detailed
```

For a path-oriented task:

```bash
arcgraph context arcgraph/pipeline/indexer.py --task "summarize indexing responsibilities" --detail-level standard
```

Add source snippets only when the workflow allows source exposure:

```bash
arcgraph context arcgraph.pipeline.indexer.ArcGraphIndexer --include-source --detail-level detailed
```

Agents should prefer `summary` for planning, then request `standard` or
`detailed` only when the task needs more edge or evidence detail.
