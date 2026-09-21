from __future__ import annotations

import ast

from arcgraph.analyzers.bindings import BindingAnalyzer
from arcgraph.analyzers.symbols import SymbolAnalyzer
from arcgraph.analyzers.types import TypeRefAnalyzer
from arcgraph.core.ids import class_id, method_id, module_id
from arcgraph.core.schemas import FileRecord, Node


def test_type_ref_analyzer_collects_annotation_constructor_and_provider_refs() -> None:
    source = "\n".join(
        [
            "from typing import ClassVar, Final, Literal, Optional, Protocol, cast",
            "from .repository import MemoryRepository as Repo",
            "",
            "class SupportsCreate(Protocol):",
            "    pass",
            "",
            "class MemoryService:",
            "    default_repo: Optional[Repo] = None",
            "    class_repo: ClassVar[Repo]",
            "    final_repo: Final[Repo]",
            "    mode: Literal['read'] = 'read'",
            "",
            "    def __init__(self, repo: Repo) -> None:",
            "        self.repo = repo",
            "        created = MemoryService(repo)",
            "        factory_repo = make_repo()",
            "        provided = ServiceContainer().get_memory_service()",
            "        maybe: Optional[MemoryService] = None",
            "        items: list[MemoryService] = []",
            "        scope_refs = {}",
            "        values = []",
            "        flags = set()",
            "        label = 'ready'",
            "        cast_repo = cast(Repo, object())",
            "        repos: list[dict[str, object]] = []",
            "        for repo_entry in repos:",
            "            repo_entry",
            "        selected = [entry for entry in repos]",
            "        fallback = scope_refs.get('missing', {})",
            "        ensured = scope_refs.setdefault('items', [])",
            "",
            "class ServiceContainer:",
            "    def get_memory_service(self) -> MemoryService:",
            "        return MemoryService(make_repo())",
            "",
            "def make_repo() -> Repo:",
            "    return Repo()",
        ]
    )
    tree = ast.parse(source)
    file_record = _file_record()
    nodes = [
        _node(module_id("pkg.service"), "module"),
        _node(class_id("pkg.repository.MemoryRepository"), "class", "MemoryRepository"),
        *SymbolAnalyzer().analyze(file_record, tree).nodes,
    ]
    module_names = {"pkg.service", "pkg.repository"}

    BindingAnalyzer().analyze(file_record, tree, module_names).attach_to_nodes(nodes)
    TypeRefAnalyzer().analyze(file_record, tree, nodes, module_names).attach_to_nodes(
        nodes
    )

    protocol = _node_by_id(nodes, class_id("pkg.service.SupportsCreate"))
    service_class = _node_by_id(nodes, class_id("pkg.service.MemoryService"))
    class_refs = _type_refs_by_name(service_class.properties["type_refs"])
    init_method = _node_by_id(nodes, method_id("pkg.service.MemoryService.__init__"))
    method_refs = _type_refs_by_name(init_method.properties["type_refs"])
    bindings = {item["name"]: item for item in init_method.properties["bindings"]}

    assert protocol.properties["type_traits"] == {"protocol": True}
    assert class_refs["class_repo"][0]["origin"] == "ClassVar"
    assert class_refs["class_repo"][0]["type_id"] == (
        "class:pkg.repository.MemoryRepository"
    )
    assert class_refs["final_repo"][0]["origin"] == "Final"
    assert class_refs["final_repo"][0]["type_id"] == (
        "class:pkg.repository.MemoryRepository"
    )
    assert class_refs["mode"][0]["origin"] == "Literal"
    assert class_refs["mode"][0]["type_id"] == "typing:Literal"
    assert method_refs["repo"][0]["type_id"] == "class:pkg.repository.MemoryRepository"
    assert method_refs["self.repo"][0]["type_id"] == (
        "class:pkg.repository.MemoryRepository"
    )
    assert method_refs["self.repo"][0]["strategy"] == ("instance_attribute_propagation")
    assert method_refs["created"][0]["strategy"] == "constructor"
    assert method_refs["created"][0]["type_id"] == "class:pkg.service.MemoryService"
    assert method_refs["factory_repo"][0]["strategy"] == "factory_return_annotation"
    assert method_refs["factory_repo"][0]["type_id"] == (
        "class:pkg.repository.MemoryRepository"
    )
    assert method_refs["provided"][0]["strategy"] == "provider_return_annotation"
    assert method_refs["provided"][0]["type_id"] == "class:pkg.service.MemoryService"
    assert method_refs["maybe"][0]["origin"] == "Optional"
    assert method_refs["maybe"][0]["type_id"] == "class:pkg.service.MemoryService"
    assert method_refs["items"][0]["origin"] == "list"
    assert method_refs["items"][0]["type_id"] == "builtin:list"
    assert method_refs["items"][0]["type_args"][0]["type_id"] == (
        "class:pkg.service.MemoryService"
    )
    assert method_refs["scope_refs"][0]["strategy"] == "literal"
    assert method_refs["scope_refs"][0]["type_id"] == "builtin:dict"
    assert method_refs["values"][0]["strategy"] == "literal"
    assert method_refs["values"][0]["type_id"] == "builtin:list"
    assert method_refs["flags"][0]["strategy"] == "constructor"
    assert method_refs["flags"][0]["type_id"] == "builtin:set"
    assert method_refs["label"][0]["strategy"] == "literal"
    assert method_refs["label"][0]["type_id"] == "builtin:str"
    assert method_refs["cast_repo"][0]["strategy"] == "cast"
    assert method_refs["cast_repo"][0]["type_id"] == (
        "class:pkg.repository.MemoryRepository"
    )
    assert method_refs["repo_entry"][0]["strategy"] == "iteration_element"
    assert method_refs["repo_entry"][0]["type_id"] == "builtin:dict"
    assert method_refs["entry"][0]["strategy"] == "iteration_element"
    assert method_refs["entry"][0]["type_id"] == "builtin:dict"
    assert method_refs["fallback"][0]["strategy"] == "builtin_method_return"
    assert method_refs["fallback"][0]["type_id"] == "builtin:dict"
    assert method_refs["ensured"][0]["strategy"] == "builtin_method_return"
    assert method_refs["ensured"][0]["type_id"] == "builtin:list"
    assert bindings["repo"]["type_ref"] == "class:pkg.repository.MemoryRepository"
    assert bindings["self.repo"]["type_ref_strategy"] == (
        "instance_attribute_propagation"
    )


def test_type_ref_analyzer_records_unresolved_annotation_diagnostics() -> None:
    source = "\n".join(
        [
            "def build(missing: MissingType) -> MissingReturn:",
            "    value: AlsoMissing = None",
            "    return missing",
        ]
    )
    tree = ast.parse(source)
    file_record = _file_record()
    nodes = [
        _node(module_id("pkg.service"), "module"),
        *SymbolAnalyzer().analyze(file_record, tree).nodes,
    ]
    module_names = {"pkg.service"}

    BindingAnalyzer().analyze(file_record, tree, module_names).attach_to_nodes(nodes)
    TypeRefAnalyzer().analyze(file_record, tree, nodes, module_names).attach_to_nodes(
        nodes
    )

    function = _node_by_id(nodes, "fn:pkg.service.build")
    diagnostics = function.properties["type_diagnostics"]

    assert [item["kind"] for item in diagnostics] == [
        "type_resolution_unresolved",
        "type_resolution_unresolved",
        "type_resolution_unresolved",
    ]
    assert {item["name"] for item in diagnostics} == {"return", "missing", "value"}
    assert all(item["path"] == "src/pkg/service.py" for item in diagnostics)


def test_type_ref_analyzer_propagates_context_manager_enter_type() -> None:
    source = "\n".join(
        [
            "class Repository:",
            "    def save(self, payload: str) -> str:",
            "        return payload",
            "",
            "class RepoManager:",
            "    def __enter__(self) -> Repository:",
            "        return Repository()",
            "",
            "    def __exit__(self, exc_type, exc, tb) -> None:",
            "        return None",
            "",
            "class UntypedRepoManager:",
            "    def __enter__(self):",
            "        return Repository()",
            "",
            "    def __exit__(self, exc_type, exc, tb) -> None:",
            "        return None",
            "",
            "class AsyncRepoManager:",
            "    async def __aenter__(self) -> Repository:",
            "        return Repository()",
            "",
            "    async def __aexit__(self, exc_type, exc, tb) -> None:",
            "        return None",
            "",
            "class UntypedAsyncRepoManager:",
            "    async def __aenter__(self):",
            "        return Repository()",
            "",
            "    async def __aexit__(self, exc_type, exc, tb) -> None:",
            "        return None",
            "",
            "async def handle(",
            "    sync_manager: RepoManager,",
            "    untyped_sync_manager: UntypedRepoManager,",
            "    manager: AsyncRepoManager,",
            "    untyped_manager: UntypedAsyncRepoManager,",
            ") -> None:",
            "    with sync_manager as sync_repo:",
            "        sync_repo",
            "    with untyped_sync_manager as untyped_sync_repo:",
            "        untyped_sync_repo",
            "    async with manager as repo:",
            "        repo",
            "    async with untyped_manager as untyped_repo:",
            "        untyped_repo",
        ]
    )
    tree = ast.parse(source)
    file_record = _file_record()
    nodes = [
        _node(module_id("pkg.service"), "module"),
        *SymbolAnalyzer().analyze(file_record, tree).nodes,
    ]
    module_names = {"pkg.service"}

    BindingAnalyzer().analyze(file_record, tree, module_names).attach_to_nodes(nodes)
    TypeRefAnalyzer().analyze(file_record, tree, nodes, module_names).attach_to_nodes(
        nodes
    )

    handle = _node_by_id(nodes, "fn:pkg.service.handle")
    refs = _type_refs_by_name(handle.properties["type_refs"])

    assert refs["sync_repo"][0]["strategy"] == "context_manager_enter"
    assert refs["sync_repo"][0]["confidence"] == "inferred"
    assert refs["sync_repo"][0]["type_id"] == "class:pkg.service.Repository"
    assert refs["repo"][0]["strategy"] == "async_context_manager_enter"
    assert refs["repo"][0]["confidence"] == "inferred"
    assert refs["repo"][0]["type_id"] == "class:pkg.service.Repository"
    assert "untyped_sync_repo" not in refs
    assert "untyped_repo" not in refs


def test_type_ref_analyzer_propagates_pydantic_classmethod_result_types() -> None:
    source = "\n".join(
        [
            "from pydantic import BaseModel",
            "from pydantic_settings import BaseSettings",
            "",
            "class Payload(BaseModel):",
            "    value: str",
            "",
            "class Settings(BaseSettings):",
            "    mode: str",
            "",
            "class Plain:",
            "    @classmethod",
            "    def model_validate(cls, raw):",
            "        return cls()",
            "",
            "def handle(raw: dict[str, object], external_model, dynamic_model) -> None:",
            "    payload = Payload.model_validate(raw)",
            "    constructed = Payload.model_construct(value='ready')",
            "    parsed = Payload.parse_obj(raw)",
            "    settings = Settings.model_validate(raw)",
            "    settings_constructed = Settings.model_construct(mode='dev')",
            "    plain = Plain.model_validate(raw)",
            "    external_payload = external_model.model_validate(raw)",
            "    external_constructed = external_model.model_construct(raw)",
            "    dynamic_payload = dynamic_model.model_validate(raw)",
            "    dynamic_parsed = dynamic_model.parse_obj(raw)",
        ]
    )
    tree = ast.parse(source)
    file_record = _file_record()
    nodes = [
        _node(module_id("pkg.service"), "module"),
        *SymbolAnalyzer().analyze(file_record, tree).nodes,
    ]
    module_names = {"pkg.service"}

    BindingAnalyzer().analyze(file_record, tree, module_names).attach_to_nodes(nodes)
    TypeRefAnalyzer().analyze(file_record, tree, nodes, module_names).attach_to_nodes(
        nodes
    )

    handle = _node_by_id(nodes, "fn:pkg.service.handle")
    refs = _type_refs_by_name(handle.properties["type_refs"])

    assert refs["payload"][0]["strategy"] == "pydantic_model_validate"
    assert refs["payload"][0]["confidence"] == "inferred"
    assert refs["payload"][0]["type_id"] == "class:pkg.service.Payload"
    assert refs["constructed"][0]["strategy"] == "pydantic_model_construct"
    assert refs["constructed"][0]["confidence"] == "inferred"
    assert refs["constructed"][0]["type_id"] == "class:pkg.service.Payload"
    assert refs["parsed"][0]["strategy"] == "pydantic_parse_obj"
    assert refs["parsed"][0]["confidence"] == "inferred"
    assert refs["parsed"][0]["type_id"] == "class:pkg.service.Payload"
    assert refs["settings"][0]["strategy"] == "pydantic_model_validate"
    assert refs["settings"][0]["confidence"] == "inferred"
    assert refs["settings"][0]["type_id"] == "class:pkg.service.Settings"
    assert refs["settings_constructed"][0]["strategy"] == "pydantic_model_construct"
    assert refs["settings_constructed"][0]["confidence"] == "inferred"
    assert refs["settings_constructed"][0]["type_id"] == "class:pkg.service.Settings"
    assert "plain" not in refs
    assert "external_payload" not in refs
    assert "external_constructed" not in refs
    assert "dynamic_payload" not in refs
    assert "dynamic_parsed" not in refs


def _file_record() -> FileRecord:
    return FileRecord(
        path="src/pkg/service.py",
        abs_path="/repo/src/pkg/service.py",
        source_root="src",
        module="pkg.service",
        file_hash="hash",
        line_count=1,
    )


def _node(node_id: str, kind: str, name: str | None = None) -> Node:
    return Node(
        id=node_id,
        kind=kind,
        name=name or node_id.rsplit(":", 1)[-1].rsplit(".", 1)[-1],
        qualname=node_id.split(":", 1)[1] if ":" in node_id else None,
        path="src/pkg/service.py",
    )


def _node_by_id(nodes: list[Node], node_id: str) -> Node:
    return next(node for node in nodes if node.id == node_id)


def _type_refs_by_name(
    type_refs: list[dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for item in type_refs:
        grouped.setdefault(str(item["name"]), []).append(item)
    return grouped


def test_type_ref_analyzer_follows_documented_external_factory_methods() -> None:
    """An argparse parser tree is typed through its factory methods.

    Subcommand parsers are ordinary locals named after the command, so no
    receiver-name heuristic reaches them. The factory return contract is a
    closed structural signal: add_subparsers returns the subparsers action and
    add_parser returns a parser, which types every later add_argument on it.
    The chain is also followed through a local subclass of the external type.
    """

    source = "\n".join(
        [
            "import argparse",
            "",
            "class LocalParser(argparse.ArgumentParser):",
            "    pass",
            "",
            "def build():",
            "    parser = argparse.ArgumentParser()",
            "    subparsers = parser.add_subparsers()",
            "    build_command = subparsers.add_parser('build')",
            "    group = parser.add_argument_group('g')",
            "    subclassed = LocalParser()",
            "    subclassed_subparsers = subclassed.add_subparsers()",
            "    subclassed_command = subclassed_subparsers.add_parser('x')",
            "    untyped = object()",
            "    untyped_command = untyped.add_parser('y')",
            "    return build_command, group, subclassed_command, untyped_command",
        ]
    )
    tree = ast.parse(source)
    file_record = _file_record()
    nodes = [
        _node(module_id("pkg.service"), "module"),
        *SymbolAnalyzer().analyze(file_record, tree).nodes,
    ]
    module_names = {"pkg.service"}

    BindingAnalyzer().analyze(file_record, tree, module_names).attach_to_nodes(nodes)
    TypeRefAnalyzer().analyze(file_record, tree, nodes, module_names).attach_to_nodes(
        nodes
    )

    build = _node_by_id(nodes, "fn:pkg.service.build")
    refs = _type_refs_by_name(build.properties["type_refs"])

    assert refs["parser"][0]["type_id"] == "extsym:argparse.ArgumentParser"
    assert refs["subparsers"][0]["type_id"] == "extsym:argparse._SubParsersAction"
    assert refs["subparsers"][0]["strategy"] == "external_method_return"
    assert refs["build_command"][0]["type_id"] == "extsym:argparse.ArgumentParser"
    assert refs["build_command"][0]["strategy"] == "external_method_return"
    assert refs["group"][0]["type_id"] == "extsym:argparse._ArgumentGroup"
    # The same chain resolves through a local subclass of the external type.
    assert (
        refs["subclassed_subparsers"][0]["type_id"]
        == "extsym:argparse._SubParsersAction"
    )
    assert refs["subclassed_command"][0]["type_id"] == "extsym:argparse.ArgumentParser"
    # A receiver of unknown type must stay unresolved rather than inherit the
    # registry entry from a same-named method elsewhere.
    assert "untyped_command" not in refs
