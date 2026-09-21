"""Export current ArcGraph data for the force-directed visualization demo.

This script is a thin compatibility wrapper around the formal ArcGraph export
path. Prefer the equivalent CLI command for normal use:

    arcgraph visual force --output viz/graph_data.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _ensure_arcgraph_importable(repo_root: Path) -> None:
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export force-directed graph data from the current ArcGraph index."
    )
    parser.add_argument(
        "--repo-root",
        default=Path(__file__).resolve().parents[1],
        type=Path,
        help="Repository root. Defaults to the checkout containing this script.",
    )
    parser.add_argument(
        "--index-output-dir",
        default="output/arcgraph",
        help="ArcGraph index output directory, relative to --repo-root unless absolute.",
    )
    parser.add_argument(
        "--output-dir",
        default=Path(__file__).resolve().parent,
        type=Path,
        help="Directory where graph_data.json will be written.",
    )
    parser.add_argument("--max-symbol-nodes", type=int, default=400)
    parser.add_argument("--min-symbol-degree", type=int, default=5)
    parser.add_argument("--max-module-symbols", type=int, default=50)
    parser.add_argument("--min-module-edge-weight", type=int, default=1)
    parser.add_argument("--include-tests", action="store_true")
    parser.add_argument("--include-generated", action="store_true")
    parser.add_argument("--include-external", action="store_true")
    parser.add_argument("--include-structural-edges", action="store_true")
    parser.add_argument("--include-edge-kind", action="append", default=[])
    parser.add_argument("--exclude-edge-kind", action="append", default=[])
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    _ensure_arcgraph_importable(repo_root)

    from arcgraph.core.force_graph_export import (  # noqa: PLC0415
        ForceGraphExportOptions,
        build_force_graph_export,
    )
    from arcgraph.core.query_engine import QueryEngine  # noqa: PLC0415

    index_output_dir = Path(args.index_output_dir)
    if not index_output_dir.is_absolute():
        index_output_dir = repo_root / index_output_dir

    payload = build_force_graph_export(
        QueryEngine(index_output_dir),
        ForceGraphExportOptions(
            max_symbol_nodes=args.max_symbol_nodes,
            min_symbol_degree=args.min_symbol_degree,
            max_module_symbols=args.max_module_symbols,
            min_module_edge_weight=args.min_module_edge_weight,
            include_tests=args.include_tests,
            include_generated=args.include_generated,
            include_external=args.include_external,
            include_structural_edges=args.include_structural_edges,
            include_edge_kinds=tuple(args.include_edge_kind),
            exclude_edge_kinds=tuple(args.exclude_edge_kind),
        ),
    )

    output_dir = args.output_dir
    if not output_dir.is_absolute():
        output_dir = repo_root / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "graph_data.json"
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": payload["status"],
                "path": str(output_path.resolve()),
                "module_nodes": len(payload["module_graph"]["nodes"]),
                "module_edges": len(payload["module_graph"]["edges"]),
                "symbol_nodes": len(payload["symbol_graph"]["nodes"]),
                "symbol_edges": len(payload["symbol_graph"]["edges"]),
                "expandable_modules": len(payload["module_symbols"]),
                "excluded_node_counts": payload["meta"]["excluded_node_counts"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
