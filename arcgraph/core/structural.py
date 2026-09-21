"""Shared structural hierarchy generation for ArcGraph.

This module provides ``generate_structural_hierarchy()`` which creates
source_root and package nodes and 'contains' edges.  It is called by both
``ArcGraphIndexer.build()`` and ``ArcGraphReindexer.reindex_changed()`` to
ensure structural consistency between full builds and incremental reindexes.

Architecture contract:
    - ``contains`` is containment-only; it MUST NOT enter impact/call traversal.
    - Full build and reindex MUST produce identical structural nodes/edges
      for the same file set and source_root_specs.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from arcgraph.core.ids import package_id, source_root_id
from arcgraph.core.scanner import SourceRoot
from arcgraph.core.schemas import Edge, Evidence, Node


def generate_structural_hierarchy(
    source_roots: tuple[SourceRoot, ...] | list[SourceRoot],
    files: list[dict[str, Any]] | None = None,
    *,
    existing_nodes: list[Node] | None = None,
) -> tuple[list[Node], list[Edge]]:
    """Generate source_root and package nodes and 'contains' edges.

    Parameters
    ----------
    source_roots:
        The resolved source roots (path + module_prefix).
    files:
        If provided, list of dicts with at least ``path``, ``source_root``,
        ``module`` keys. Used to discover packages and create per-file
        contains edges.
    existing_nodes:
        If provided, module nodes from the current graph. Used to link
        modules to their parent package via contains edges.

    Returns
    -------
    tuple[list[Node], list[Edge]]
        New structural nodes and edges to merge into the graph.
    """
    nodes: list[Node] = []
    edges: list[Edge] = []
    seen_node_ids: set[str] = set()

    # 1. Create source_root nodes
    for root in source_roots:
        sr_id = source_root_id(root.path)
        if sr_id not in seen_node_ids:
            nodes.append(
                Node(
                    id=sr_id,
                    kind="source_root",
                    name=root.path or ".",
                    path=root.path,
                    properties={
                        "module_prefix": root.module_prefix,
                        "structural": True,
                    },
                )
            )
            seen_node_ids.add(sr_id)

    # 2. Discover packages from file paths and create package nodes + edges
    if files:
        _add_packages_from_files(
            source_roots,
            files,
            nodes,
            edges,
            seen_node_ids,
        )

    # 3. Link existing module nodes to their source_root via contains
    if existing_nodes:
        _link_modules_to_roots(
            source_roots,
            existing_nodes,
            edges,
            seen_node_ids,
        )

    return nodes, edges


def _add_packages_from_files(
    source_roots: tuple[SourceRoot, ...] | list[SourceRoot],
    files: list[dict[str, Any]],
    nodes: list[Node],
    edges: list[Edge],
    seen_node_ids: set[str],
) -> None:
    """Create package nodes from __init__.py files and link them.

    Two-pass algorithm to guarantee input-order insensitivity:
    - Pass 1: Collect all package qualnames and create nodes.
    - Pass 2: Create contains edges using the complete package ID set.
    """
    root_by_path = {root.path: root for root in source_roots}

    # Pass 1: Collect all packages — qualname → (pkg_id, source_root_path, dir_path)
    package_info: dict[str, tuple[str, str, str]] = {}  # qualname → (pkg_id, sr, dir)

    for file_info in files:
        file_path = file_info.get("path", "")
        file_source_root = file_info.get("source_root", "")
        is_package = file_info.get("is_package", False)

        if not is_package:
            continue

        root = root_by_path.get(file_source_root)
        if root is None:
            continue

        posix_path = PurePosixPath(file_path)
        init_parent = posix_path.parent
        sr_posix = (
            PurePosixPath(file_source_root)
            if file_source_root != "."
            else PurePosixPath()
        )

        try:
            pkg_rel = (
                init_parent.relative_to(sr_posix) if str(sr_posix) else init_parent
            )
        except ValueError:
            continue

        parts = pkg_rel.parts
        if not parts:
            continue

        if root.module_prefix:
            qualname = f"{root.module_prefix}.{'.'.join(parts)}"
        else:
            qualname = ".".join(parts)

        if qualname not in package_info:
            pkg_id = package_id(qualname)
            package_info[qualname] = (pkg_id, file_source_root, str(init_parent))

    # Create all package nodes first (complete set for parent lookup)
    all_pkg_ids: set[str] = set()
    for qualname, (pkg_id, sr, dir_path) in package_info.items():
        all_pkg_ids.add(pkg_id)
        if pkg_id not in seen_node_ids:
            nodes.append(
                Node(
                    id=pkg_id,
                    kind="package",
                    name=qualname,
                    path=dir_path,
                    properties={
                        "source_root": sr,
                        "structural": True,
                    },
                )
            )
            seen_node_ids.add(pkg_id)

    # Pass 2: Create contains edges using the complete package set
    for qualname, (pkg_id, sr, _) in package_info.items():
        sr_id = source_root_id(sr)
        parent_id = sr_id  # default: top-level package → source_root

        # Walk qualname up to find nearest parent package.
        remainder = qualname.rsplit(".", 1)
        while len(remainder) == 2:
            candidate = package_id(remainder[0])
            if candidate in all_pkg_ids or candidate in seen_node_ids:
                parent_id = candidate
                break
            remainder = remainder[0].rsplit(".", 1)

        edges.append(
            Edge(
                source=parent_id,
                target=pkg_id,
                kind="contains",
                evidence=[Evidence(kind="structural")],
            )
        )


def _link_modules_to_roots(
    source_roots: tuple[SourceRoot, ...] | list[SourceRoot],
    existing_nodes: list[Node],
    edges: list[Edge],
    seen_node_ids: set[str],
) -> None:
    """Link module nodes to their nearest structural parent via contains edges.

    Builds a single-parent tree:
        source_root → package → module   (when a package exists)
        source_root → module             (no package in path)
    """
    # Collect all package node IDs that were created so far
    package_node_ids = {
        node.id for node in existing_nodes if node.kind == "package"
    } | {nid for nid in seen_node_ids if nid.startswith("package:")}

    seen_edges: set[tuple[str, str]] = set()

    for node in existing_nodes:
        if node.kind != "module":
            continue
        if node.path is None:
            continue

        # Find which source root this module belongs to — use longest
        # (most specific) match so nested roots work correctly.
        matched_root: SourceRoot | None = None
        for root in sorted(source_roots, key=lambda r: len(r.path), reverse=True):
            root_prefix = root.path.rstrip("/") + "/" if root.path != "." else ""
            if root.path == "." or node.path.startswith(root_prefix):
                matched_root = root
                break

        if matched_root is None:
            continue

        sr_id = source_root_id(matched_root.path)

        # Try to find the nearest parent package.
        # Module qualname is e.g. "pkg.sub.foo" → try "pkg.sub", then "pkg"
        qualname = node.qualname or node.name or ""
        parent_id: str | None = None
        parts = qualname.rsplit(".", 1)
        while len(parts) == 2:
            candidate = package_id(parts[0])
            if candidate in package_node_ids:
                parent_id = candidate
                break
            # Try one level up
            parts = parts[0].rsplit(".", 1)

        # Use source_root as parent only if no package was found
        if parent_id is None:
            parent_id = sr_id

        edge_key = (parent_id, node.id)
        if edge_key not in seen_edges:
            edges.append(
                Edge(
                    source=parent_id,
                    target=node.id,
                    kind="contains",
                    evidence=[Evidence(kind="structural")],
                )
            )
            seen_edges.add(edge_key)
