from __future__ import annotations

import ast

from arcgraph.analyzers.bindings import BindingAnalyzer
from arcgraph.core.ids import class_id, function_id, method_id, module_id
from arcgraph.core.schemas import FileRecord, Node


def test_binding_analyzer_collects_scope_binding_summaries() -> None:
    source = "\n".join(
        [
            "from typing import TYPE_CHECKING",
            "from .repository import MemoryRepository as Repo",
            "if TYPE_CHECKING:",
            "    from .model import Memory",
            "__all__ = ['MemoryService']",
            "",
            "class MemoryService:",
            "    default_repo: Repo | None = None",
            "",
            "    def __init__(self, repo: Repo) -> None:",
            "        self.repo = repo",
            "        items = [item for item in []]",
        ]
    )
    analysis = BindingAnalyzer().analyze(
        _file_record(),
        ast.parse(source),
        {"pkg.service", "pkg.repository", "pkg.model"},
    )
    nodes = [
        _node(module_id("pkg.service"), "module"),
        _node(class_id("pkg.service.MemoryService"), "class"),
        _node(method_id("pkg.service.MemoryService.__init__"), "method"),
    ]

    analysis.attach_to_nodes(nodes)

    module_bindings = _bindings_by_name(nodes[0].properties["bindings"])
    class_bindings = _bindings_by_name(nodes[1].properties["bindings"])
    method_bindings = _bindings_by_name(nodes[2].properties["bindings"])

    assert module_bindings["Repo"][0]["kind"] == "import_alias"
    assert module_bindings["Repo"][0]["target_module"] == "mod:pkg.repository"
    assert module_bindings["Memory"][0]["kind"] == "type_checking_import_alias"
    assert module_bindings["Memory"][0]["static_only"] is True
    memory_service_class = _binding_with_kind(
        module_bindings["MemoryService"], "class_definition"
    )
    assert memory_service_class["target"] == ("class:pkg.service.MemoryService")
    assert any(
        item["kind"] == "re_export" and item["name"] == "MemoryService"
        for item in nodes[0].properties["bindings"]
    )

    assert class_bindings["default_repo"][0]["kind"] == "annotated_assignment"
    assert class_bindings["default_repo"][0]["annotation"] == "Repo | None"
    assert class_bindings["__init__"][0]["kind"] == "method_definition"
    assert method_bindings["repo"][0]["kind"] == "parameter"
    assert method_bindings["repo"][0]["annotation"] == "Repo"
    assert method_bindings["self.repo"][0]["kind"] == "instance_attribute"
    assert method_bindings["self.repo"][0]["owner"] == "self"
    assert method_bindings["item"][0]["kind"] == "comprehension_target"


def test_binding_analyzer_records_import_shadowing_diagnostic() -> None:
    source = "\n".join(
        [
            "from .repository import MemoryRepository as Repo",
            "Repo = object()",
        ]
    )
    analysis = BindingAnalyzer().analyze(
        _file_record(),
        ast.parse(source),
        {"pkg.service", "pkg.repository"},
    )
    nodes = [_node(module_id("pkg.service"), "module")]

    analysis.attach_to_nodes(nodes)

    bindings = nodes[0].properties["bindings"]
    diagnostics = nodes[0].properties["binding_diagnostics"]
    shadowing = [item for item in bindings if item["name"] == "Repo"][-1]

    assert shadowing["kind"] == "assignment"
    assert shadowing["shadows_binding_id"] == bindings[0]["binding_id"]
    assert diagnostics == [
        {
            "kind": "binding_shadowing",
            "scope_id": "mod:pkg.service",
            "name": "Repo",
            "binding_id": shadowing["binding_id"],
            "shadowed_binding_id": bindings[0]["binding_id"],
            "path": "src/pkg/service.py",
            "line": 2,
            "column": 0,
            "message": "Local binding 'Repo' shadows imported binding in mod:pkg.service.",
        }
    ]


def test_binding_analyzer_collects_control_flow_binding_targets() -> None:
    source = "\n".join(
        [
            "from .repository import *",
            "",
            "async def handle(items, manager):",
            "    global cache",
            "    cache = 1",
            "    total = 0",
            "    total += 1",
            "    for item in items:",
            "        pass",
            "    async for chunk in items:",
            "        pass",
            "    with manager as resource:",
            "        pass",
            "    async with manager as async_resource:",
            "        pass",
            "    try:",
            "        raise ValueError()",
            "    except ValueError as exc:",
            "        pass",
        ]
    )
    analysis = BindingAnalyzer().analyze(
        _file_record(),
        ast.parse(source),
        {"pkg.service", "pkg.repository"},
    )
    nodes = [
        _node(module_id("pkg.service"), "module"),
        _node(function_id("pkg.service.handle"), "function"),
    ]

    analysis.attach_to_nodes(nodes)

    module_bindings = _bindings_by_name(nodes[0].properties["bindings"])
    function_bindings = _bindings_by_name(nodes[1].properties["bindings"])

    assert module_bindings["*"][0]["kind"] == "star_import"
    assert module_bindings["*"][0]["target_module"] == "mod:pkg.repository"
    assert function_bindings["cache"][0]["kind"] == "global"
    assert function_bindings["cache"][1]["kind"] == "assignment"
    assert [item["kind"] for item in function_bindings["total"]] == [
        "assignment",
        "assignment",
    ]
    assert function_bindings["item"][0]["kind"] == "for_target"
    assert function_bindings["chunk"][0]["kind"] == "for_target"
    assert function_bindings["resource"][0]["kind"] == "with_as"
    assert function_bindings["async_resource"][0]["kind"] == "with_as"
    assert function_bindings["exc"][0]["kind"] == "except_as"


def _file_record() -> FileRecord:
    return FileRecord(
        path="src/pkg/service.py",
        abs_path="/repo/src/pkg/service.py",
        source_root="src",
        module="pkg.service",
        file_hash="hash",
        line_count=1,
    )


def _node(node_id: str, kind: str) -> Node:
    return Node(id=node_id, kind=kind, name=node_id, path="src/pkg/service.py")


def _bindings_by_name(
    bindings: list[dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for item in bindings:
        grouped.setdefault(str(item["name"]), []).append(item)
    return grouped


def _binding_with_kind(
    bindings: list[dict[str, object]], kind: str
) -> dict[str, object]:
    return next(item for item in bindings if item["kind"] == kind)
