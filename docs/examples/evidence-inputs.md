# Example: Evidence Inputs

ArcGraph works without optional evidence, but explicit artifacts can improve
context and make uncertainty visible.

## Coverage

```bash
python -m pytest arcgraph/tests -q --cov=arcgraph --cov-report=xml:output/arcgraph/coverage.xml --cov-report=term
arcgraph build --coverage output/arcgraph/coverage.xml
arcgraph evidence status
```

## Python Precision Evidence

```bash
arcgraph precision scip-python --output output/arcgraph/scip-index.json --index-file output/arcgraph/index.scip --project-name ArcGraph --project-version local --target-only arcgraph
arcgraph precision pyright --output output/arcgraph/pyright-export.json --python-version 3.11 --timeout-seconds 300 --target-only arcgraph
arcgraph build --scip-index output/arcgraph/scip-index.json --pyright-export output/arcgraph/pyright-export.json
```

If a SCIP binary index already exists, convert it first:

```bash
arcgraph precision scip-json --input index.scip --output output/arcgraph/scip-index.json
```

## Runtime Trace

```bash
arcgraph trace run --output output/arcgraph/runtime-trace.json --root arcgraph --max-events 5000 --max-seconds 30 -- arcgraph/tests/test_imports.py::test_import_analyzer_marks_function_local_import_edges -q
arcgraph build --runtime-trace output/arcgraph/runtime-trace.json
```

Runtime trace evidence is advisory and labeled `runtime-only`.

## SCIP Protocol Graph

SCIP protocol graph input is explicit and separate from Python precision SCIP:

```bash
arcgraph build --scip-graph-index output/arcgraph/scip-protocol.json
```

ArcGraph consumes existing SCIP JSON protocol facts. It does not run a language
indexer for this input.

## OpenAPI Protocol Graph

OpenAPI input is explicit:

```bash
arcgraph build --openapi-spec PATH_TO_YOUR_OPENAPI_SPEC
```

OpenAPI route/schema facts are confirmed protocol evidence. Handler matches are
inferred only when `operationId` uniquely maps to an indexed local function or
method.
