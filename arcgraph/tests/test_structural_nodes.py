"""Tests for structural hierarchy generation (Phase 5)."""

from __future__ import annotations

from arcgraph.core.ids import package_id, source_root_id
from arcgraph.core.scanner import SourceRoot
from arcgraph.core.schemas import Node
from arcgraph.core.structural import generate_structural_hierarchy


def test_source_root_nodes_created() -> None:
    """Each source root produces a source_root node."""
    roots = [SourceRoot("backend/src"), SourceRoot("scripts", "scripts")]
    nodes, edges = generate_structural_hierarchy(roots)
    node_ids = {n.id for n in nodes}
    assert source_root_id("backend/src") in node_ids
    assert source_root_id("scripts") in node_ids


def test_source_root_has_module_prefix_in_properties() -> None:
    """source_root nodes carry module_prefix in properties."""
    roots = [SourceRoot("scripts", "scripts")]
    nodes, _ = generate_structural_hierarchy(roots)
    sr_node = next(n for n in nodes if n.kind == "source_root")
    assert sr_node.properties["module_prefix"] == "scripts"


def test_package_nodes_from_init_files() -> None:
    """__init__.py files produce package nodes with contains edges."""
    roots = [SourceRoot("src")]
    files = [
        {
            "path": "src/pkg/__init__.py",
            "source_root": "src",
            "module": "pkg",
            "is_package": True,
        },
        {
            "path": "src/pkg/mod.py",
            "source_root": "src",
            "module": "pkg.mod",
            "is_package": False,
        },
    ]
    nodes, edges = generate_structural_hierarchy(roots, files=files)

    pkg_nodes = [n for n in nodes if n.kind == "package"]
    assert len(pkg_nodes) == 1
    assert pkg_nodes[0].name == "pkg"
    assert pkg_nodes[0].id == package_id("pkg")

    # Should have source_root → package contains edge
    contains_edges = [e for e in edges if e.kind == "contains"]
    assert any(
        e.source == source_root_id("src") and e.target == package_id("pkg")
        for e in contains_edges
    )


def test_package_with_module_prefix() -> None:
    """Non-package roots add module_prefix to package qualname."""
    roots = [SourceRoot("scripts", "scripts")]
    files = [
        {
            "path": "scripts/utils/__init__.py",
            "source_root": "scripts",
            "module": "scripts.utils",
            "is_package": True,
        },
    ]
    nodes, edges = generate_structural_hierarchy(roots, files=files)
    pkg_nodes = [n for n in nodes if n.kind == "package"]
    assert len(pkg_nodes) == 1
    assert pkg_nodes[0].name == "scripts.utils"


def test_module_linked_to_package_when_package_exists() -> None:
    """When a package node exists, modules link to the package, not source_root."""
    roots = [SourceRoot("src")]
    files = [
        {
            "path": "src/pkg/__init__.py",
            "source_root": "src",
            "module": "pkg",
            "is_package": True,
        },
        {
            "path": "src/pkg/foo.py",
            "source_root": "src",
            "module": "pkg.foo",
            "is_package": False,
        },
    ]
    existing_nodes = [
        Node(
            id="mod:pkg.foo",
            kind="module",
            name="pkg.foo",
            qualname="pkg.foo",
            path="src/pkg/foo.py",
        ),
    ]
    nodes, edges = generate_structural_hierarchy(
        roots, files=files, existing_nodes=existing_nodes
    )

    module_edges = [
        e for e in edges if e.kind == "contains" and e.target == "mod:pkg.foo"
    ]
    assert len(module_edges) == 1
    # Module should link to its package, NOT directly to source_root
    assert module_edges[0].source == package_id("pkg")


def test_module_linked_to_source_root_when_no_package() -> None:
    """Modules without a package ancestor link directly to source_root."""
    roots = [SourceRoot("scripts", "scripts")]
    existing_nodes = [
        Node(
            id="mod:scripts.run",
            kind="module",
            name="scripts.run",
            qualname="scripts.run",
            path="scripts/run.py",
        ),
    ]
    nodes, edges = generate_structural_hierarchy(roots, existing_nodes=existing_nodes)

    module_edges = [
        e for e in edges if e.kind == "contains" and e.target == "mod:scripts.run"
    ]
    assert len(module_edges) == 1
    # No package exists → link to source_root
    assert module_edges[0].source == source_root_id("scripts")


def test_containment_forms_single_parent_tree() -> None:
    """Invariant: contains is a single-parent tree, not flat grouping.

    source_root:src → package:pkg → package:pkg.sub → mod:pkg.sub.mod
    """
    roots = [SourceRoot("src")]
    files = [
        {
            "path": "src/pkg/__init__.py",
            "source_root": "src",
            "module": "pkg",
            "is_package": True,
        },
        {
            "path": "src/pkg/sub/__init__.py",
            "source_root": "src",
            "module": "pkg.sub",
            "is_package": True,
        },
        {
            "path": "src/pkg/sub/mod.py",
            "source_root": "src",
            "module": "pkg.sub.mod",
            "is_package": False,
        },
    ]
    existing_nodes = [
        Node(
            id="mod:pkg.sub.mod",
            kind="module",
            name="pkg.sub.mod",
            qualname="pkg.sub.mod",
            path="src/pkg/sub/mod.py",
        ),
    ]
    nodes, edges = generate_structural_hierarchy(
        roots, files=files, existing_nodes=existing_nodes
    )

    contains_edges = [e for e in edges if e.kind == "contains"]
    edge_map = {(e.source, e.target) for e in contains_edges}

    # source_root → package:pkg (top-level package)
    assert (source_root_id("src"), package_id("pkg")) in edge_map
    # package:pkg → package:pkg.sub (sub-package hangs off parent package)
    assert (package_id("pkg"), package_id("pkg.sub")) in edge_map
    # package:pkg.sub → mod:pkg.sub.mod (module hangs off nearest package)
    assert (package_id("pkg.sub"), "mod:pkg.sub.mod") in edge_map

    # MUST NOT exist: flat linkage that bypasses hierarchy
    assert (source_root_id("src"), package_id("pkg.sub")) not in edge_map
    assert (source_root_id("src"), "mod:pkg.sub.mod") not in edge_map

    # Single-parent invariant: every target has at most one contains in-edge
    targets = [e.target for e in contains_edges]
    assert len(targets) == len(set(targets)), f"Duplicate contains targets: {targets}"


def test_contains_edge_not_in_call_kinds() -> None:
    """contains is structural-only and must not appear in CALL_QUERY_EDGE_KINDS."""
    from arcgraph.core.query_engine import CALL_QUERY_EDGE_KINDS

    assert "contains" not in CALL_QUERY_EDGE_KINDS


def test_contains_edge_in_structural_kinds() -> None:
    """contains must appear in STRUCTURAL_EDGE_KINDS."""
    from arcgraph.core.visual_contract import (
        STRUCTURAL_EDGE_KINDS,
        CONTAINMENT_EDGE_KINDS,
    )

    assert "contains" in STRUCTURAL_EDGE_KINDS
    assert "contains" in CONTAINMENT_EDGE_KINDS


def test_container_node_kinds_defined() -> None:
    """CONTAINER_NODE_KINDS must list source_root and package."""
    from arcgraph.core.visual_contract import CONTAINER_NODE_KINDS

    assert "source_root" in CONTAINER_NODE_KINDS
    assert "package" in CONTAINER_NODE_KINDS


def test_dot_root_produces_valid_source_root_node() -> None:
    """'.' root (repo root) produces a valid source_root node."""
    roots = [SourceRoot(".", "")]
    nodes, edges = generate_structural_hierarchy(roots)
    sr_nodes = [n for n in nodes if n.kind == "source_root"]
    assert len(sr_nodes) == 1
    assert sr_nodes[0].name == "."


def test_no_duplicate_nodes() -> None:
    """Repeated calls with same roots don't produce duplicate nodes."""
    roots = [SourceRoot("src")]
    files = [
        {
            "path": "src/pkg/__init__.py",
            "source_root": "src",
            "module": "pkg",
            "is_package": True,
        },
    ]
    nodes, edges = generate_structural_hierarchy(roots, files=files)
    node_ids = [n.id for n in nodes]
    assert len(node_ids) == len(set(node_ids)), "Duplicate node IDs found"


def test_evidence_has_structural_kind() -> None:
    """Contains edges have evidence with kind='structural'."""
    roots = [SourceRoot("src")]
    files = [
        {
            "path": "src/pkg/__init__.py",
            "source_root": "src",
            "module": "pkg",
            "is_package": True,
        },
    ]
    _, edges = generate_structural_hierarchy(roots, files=files)
    for edge in edges:
        if edge.kind == "contains":
            assert any(ev.kind == "structural" for ev in edge.evidence)


def test_package_hierarchy_order_insensitive() -> None:
    """Invariant: generate_structural_hierarchy must be input-order insensitive.

    Even when files list sub-package __init__.py BEFORE parent __init__.py,
    the result must be package:pkg -> package:pkg.sub, not source_root -> pkg.sub.
    """
    roots = [SourceRoot("src")]
    # Deliberately reversed: sub-package comes before parent
    files = [
        {
            "path": "src/pkg/sub/__init__.py",
            "source_root": "src",
            "module": "pkg.sub",
            "is_package": True,
        },
        {
            "path": "src/pkg/__init__.py",
            "source_root": "src",
            "module": "pkg",
            "is_package": True,
        },
    ]
    existing_nodes = [
        Node(
            id="mod:pkg.sub.foo",
            kind="module",
            name="pkg.sub.foo",
            qualname="pkg.sub.foo",
            path="src/pkg/sub/foo.py",
        ),
    ]
    nodes, edges = generate_structural_hierarchy(
        roots, files=files, existing_nodes=existing_nodes
    )

    contains_edges = [e for e in edges if e.kind == "contains"]
    edge_map = {(e.source, e.target) for e in contains_edges}

    # Must produce tree: source_root -> pkg -> pkg.sub -> mod
    assert (source_root_id("src"), package_id("pkg")) in edge_map
    assert (package_id("pkg"), package_id("pkg.sub")) in edge_map
    assert (package_id("pkg.sub"), "mod:pkg.sub.foo") in edge_map

    # Must NOT produce flat linkage
    assert (source_root_id("src"), package_id("pkg.sub")) not in edge_map
    assert (source_root_id("src"), "mod:pkg.sub.foo") not in edge_map
