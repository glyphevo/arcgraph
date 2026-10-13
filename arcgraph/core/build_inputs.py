"""Private constructor inputs carried between graph generations."""

import json

from arcgraph.core.utils import symlink_below

ARTIFACT_FIELDS = (
    "scip_index_path",
    "scip_graph_index_path",
    "openapi_path",
    "pyright_export_path",
    "coverage_path",
    "runtime_trace_path",
)


def persist(indexer, build_dir):
    values = {
        field: str(value)
        for field in ARTIFACT_FIELDS
        if (value := getattr(indexer, field)) is not None
    }
    if indexer.ignore_rules is not None:
        values["ignore_rules"] = indexer.ignore_rules
    (build_dir / "build-inputs.json").write_text(
        json.dumps(values, sort_keys=True) + "\n", encoding="utf-8"
    )


def recover(store, root, output):
    path = store.sqlite_path.parent / "build-inputs.json"
    if symlink_below(output, path):
        raise RuntimeError("Unsafe build inputs; rebuild required")
    values = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    inputs = {
        key: root / value
        for key, value in values.items()
        if key in ARTIFACT_FIELDS and isinstance(value, str)
    }
    if isinstance(values.get("ignore_rules"), list) and all(
        isinstance(rule, str) for rule in values["ignore_rules"]
    ):
        inputs["ignore_rules"] = values["ignore_rules"]
    for field, (metrics, key) in {
        "scip_index_path": ("precision_metrics", "scip_index_path"),
        "pyright_export_path": ("precision_metrics", "pyright_export_path"),
        "coverage_path": ("coverage_metrics", "path"),
        "runtime_trace_path": ("runtime_metrics", "trace_path"),
    }.items():
        value = (store.metadata.get(metrics) or {}).get(key)
        if field not in inputs and isinstance(value, str) and value:
            inputs[field] = root / value
    for field, frontend in (
        ("openapi_path", "openapi-protocol"),
        ("scip_graph_index_path", "scip-protocol"),
    ):
        value = (store.metadata.get("adapter_metrics", {}).get(frontend) or {}).get(
            "artifact_path"
        )
        if field not in inputs and isinstance(value, str) and value:
            inputs[field] = root / value
    return inputs


def publish(indexer, semantic, build_dir):
    """Publish constructor inputs together with any adopted semantic generation."""
    persist(indexer, build_dir)
    if semantic is not None:
        semantic.publish(build_dir)
