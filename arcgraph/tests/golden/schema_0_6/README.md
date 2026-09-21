# ArcGraph schema 0.6 golden fixtures

Schema `0.6.0` golden outputs add platform staging artifacts on top of the
legacy node and edge JSONL files:

- `semantic_facts.jsonl`
- `nodes.jsonl`
- `edges.jsonl`
- `diagnostics.jsonl`
- `merge_metrics.json`

Fixture updates must be explicit and reviewer-approved. Normal test runs must
not overwrite these files.
