from __future__ import annotations

import ast

from arcgraph.analyzers.bindings import BindingAnalyzer
from arcgraph.analyzers.calls import CallAnalyzer
from arcgraph.analyzers.symbols import SymbolAnalyzer
from arcgraph.analyzers.types import TypeRefAnalyzer
from arcgraph.core.ids import class_id, function_id, method_id, module_id
from arcgraph.core.schemas import FileRecord, IndexMetadata, Node
from arcgraph.core.graph_store import GraphStoreWriter
from arcgraph.core.semantic_metrics import unresolved_callsite_diagnostics
from arcgraph.core.unresolved_classification import classify_unresolved_records


def test_v2_call_analyzer_resolves_receiver_and_provider_chain_calls() -> None:
    source = "\n".join(
        [
            "class Repository:",
            "    def save(self, content: str) -> object:",
            "        return object()",
            "",
            "class Service:",
            "    def __init__(self, repo: Repository) -> None:",
            "        self.repo = repo",
            "",
            "    def create(self, content: str) -> object:",
            "        return self.repo.save(content)",
            "",
            "def endpoint(service: Service) -> object:",
            "    return service.create('x')",
            "",
            "def make_service() -> Service:",
            "    return Service(Repository())",
            "",
            "def run_chain() -> object:",
            "    return make_service().create('x')",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    by_pair = {(edge.source, edge.target): edge for edge in edges}

    repo_save = by_pair[
        (
            method_id("pkg.calls.Service.create"),
            method_id("pkg.calls.Repository.save"),
        )
    ]
    endpoint_create = by_pair[
        (
            function_id("pkg.calls.endpoint"),
            method_id("pkg.calls.Service.create"),
        )
    ]
    chain_create = by_pair[
        (
            function_id("pkg.calls.run_chain"),
            method_id("pkg.calls.Service.create"),
        )
    ]

    assert repo_save.kind == "calls"
    assert repo_save.resolution.strategy == "receiver_type"
    assert repo_save.properties["callsite"]["receiver_expression"] == "self.repo"
    assert repo_save.properties["callsite"]["receiver_type"] == (
        "class:pkg.calls.Repository"
    )
    assert endpoint_create.resolution.strategy == "receiver_type"
    assert endpoint_create.properties["callsite"]["receiver_expression"] == "service"
    assert chain_create.resolution.strategy == "receiver_type"
    assert (
        chain_create.properties["callsite"]["receiver_expression"] == "make_service()"
    )
    assert (
        by_pair[
            (function_id("pkg.calls.make_service"), class_id("pkg.calls.Service"))
        ].kind
        == "constructs"
    )
    assert (
        by_pair[
            (function_id("pkg.calls.make_service"), class_id("pkg.calls.Repository"))
        ].kind
        == "constructs"
    )


def test_v2_call_analyzer_propagates_classmethod_return_to_instance_attribute() -> None:
    source = "\n".join(
        [
            "class StoreReader:",
            "    @classmethod",
            "    def from_current(cls, path: str) -> 'StoreReader':",
            "        return cls(path)",
            "",
            "    def __init__(self, path: str) -> None:",
            "        self.path = path",
            "",
            "    def connect(self) -> object:",
            "        return object()",
            "",
            "class Query:",
            "    def __init__(self, path: str) -> None:",
            "        self.store = StoreReader.from_current(path)",
            "",
            "    def current(self) -> object:",
            "        return self.store.connect()",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    store_edge = next(
        edge
        for edge in edges
        if edge.source == method_id("pkg.calls.Query.current")
        and edge.target == method_id("pkg.calls.StoreReader.connect")
    )

    assert store_edge.resolution.strategy == "receiver_type"
    assert store_edge.properties["callsite"]["receiver_expression"] == "self.store"
    assert store_edge.properties["callsite"]["receiver_type"] == (
        "class:pkg.calls.StoreReader"
    )


def test_v2_call_analyzer_resolves_property_and_local_alias_receiver_types() -> None:
    source = "\n".join(
        [
            "from functools import cached_property",
            "",
            "class Repository:",
            "    def save(self, payload: str) -> str:",
            "        return payload",
            "",
            "def provide_repository() -> Repository:",
            "    return Repository()",
            "",
            "class Service:",
            "    repo: Repository",
            "",
            "    def __init__(self) -> None:",
            "        self.repo = provide_repository()",
            "",
            "    @property",
            "    def property_repo(self) -> Repository:",
            "        return self.repo",
            "",
            "    @cached_property",
            "    def cached_repo(self) -> Repository:",
            "        return provide_repository()",
            "",
            "    def from_class_annotation(self, payload: str) -> str:",
            "        return self.repo.save(payload)",
            "",
            "    def from_property(self, payload: str) -> str:",
            "        return self.property_repo.save(payload)",
            "",
            "    def from_cached_property(self, payload: str) -> str:",
            "        return self.cached_repo.save(payload)",
            "",
            "    def from_local_alias(self, payload: str) -> str:",
            "        repo = self.repo",
            "        return repo.save(payload)",
            "",
            "def dynamic_negative(cache, bag, plugin) -> None:",
            "    cache.get('x')",
            "    bag.add('y')",
            "    plugin.run()",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    by_pair = {(edge.source, edge.target): edge for edge in edges}
    expected_sources = {
        method_id("pkg.calls.Service.from_class_annotation"): "self.repo",
        method_id("pkg.calls.Service.from_property"): "self.property_repo",
        method_id("pkg.calls.Service.from_cached_property"): "self.cached_repo",
        method_id("pkg.calls.Service.from_local_alias"): "repo",
    }

    for source_id, receiver_expression in expected_sources.items():
        edge = by_pair[(source_id, method_id("pkg.calls.Repository.save"))]
        assert edge.resolution.strategy == "receiver_type"
        assert edge.properties["callsite"]["receiver_expression"] == receiver_expression
        assert edge.properties["callsite"]["receiver_type"] == (
            "class:pkg.calls.Repository"
        )

    dynamic_edges = [
        edge
        for edge in edges
        if edge.source == function_id("pkg.calls.dynamic_negative")
        and edge.kind == "dynamic_call"
    ]
    assert {
        edge.properties["callsite"]["raw_expression"] for edge in dynamic_edges
    } == {
        "cache.get",
        "bag.add",
        "plugin.run",
    }


def test_v2_call_analyzer_names_db_execute_by_protocol_without_a_cursor() -> None:
    source = "\n".join(
        [
            "def load_count(conn) -> object:",
            "    return conn.execute('select 1').fetchone()",
            "",
            "def ambiguous_fetchone(fetchone) -> object:",
            "    return fetchone()",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    by_pair = {(edge.source, edge.target): edge for edge in edges}
    load_count_id = function_id("pkg.calls.load_count")

    # PEP 249 has no module: execute is its Cursor's, a protocol target.
    execute_edge = by_pair[(load_count_id, "protocol:pep249.Cursor.execute")]

    assert execute_edge.kind == "uses"
    assert execute_edge.resolution.strategy == "receiver_name_boundary_method"
    assert execute_edge.properties["callsite"]["receiver_expression"] == "conn"
    assert execute_edge.properties["callsite"]["target_symbol"] == (
        "pep249.Cursor.execute"
    )
    # PEP 249 does not say what execute returns, so the next call is not
    # typed as a cursor's.
    assert not any(
        edge.source == load_count_id and edge.target.endswith(".fetchone")
        for edge in edges
    )
    assert not any(
        edge.source == function_id("pkg.calls.ambiguous_fetchone")
        and edge.target.endswith(".fetchone")
        for edge in edges
    )


def test_unresolved_diagnostics_classify_runtime_bindings_without_changing_edges() -> (
    None
):
    source = "\n".join(
        [
            "module_callback = lambda payload: payload",
            "module_callback({})",
            "plugin = lambda payload: payload",
            "send_callback = lambda payload: payload",
            "shadowed_callback = lambda payload: payload",
            "",
            "def apply(payload, mutation):",
            "    mutation(payload)",
            "    plugin(payload)",
            "    send_callback(payload)",
            "",
            "def local_shadow(payload):",
            "    shadowed_callback(payload)",
            "    shadowed_callback = lambda value: value",
            "",
            "def before_module_binding(payload):",
            "    late_callback(payload)",
            "",
            "late_callback = lambda payload: payload",
            "",
            "def apply_all(payloads, operations):",
            "    for operation in operations:",
            "        operation(payloads)",
            "",
            "def comprehension_does_not_escape(values):",
            "    [item for item in values]",
            "    item()",
        ]
    )
    nodes = _analyzed_nodes(source)
    analysis = CallAnalyzer(enable_v2=True).analyze(nodes)

    # Classification evidence must not manufacture dynamic graph edges.
    assert not any(
        edge.kind == "dynamic_call"
        and edge.properties.get("callsite", {}).get("raw_expression")
        in {
            "module_callback",
            "mutation",
            "plugin",
            "send_callback",
            "shadowed_callback",
            "late_callback",
            "operation",
            "item",
        }
        for edge in analysis.edges
    )

    diagnostics = unresolved_callsite_diagnostics(
        IndexMetadata(
            index_version="runtime-binding-diagnostic",
            repo_root="/repo",
            source_roots=["pkg"],
        ),
        [*nodes, *analysis.nodes],
        analysis.edges,
        repo_id="test",
        frontend_name="python-v1-compat-shim",
    )
    records = [item.model_dump(mode="json") for item in diagnostics]
    diagnostic_properties = {
        item["properties"]["raw_expression"]: item["properties"] for item in records
    }
    classified = classify_unresolved_records({"unresolved": records})
    by_expression = {item["raw_expression"]: item for item in classified["records"]}

    assert by_expression["module_callback"]["category"] == "true_dynamic_call"
    assert diagnostic_properties["module_callback"]["callee_binding_kind"] == (
        "assignment"
    )
    assert diagnostic_properties["module_callback"]["callee_binding_scope"] == (
        "module"
    )
    assert by_expression["mutation"]["category"] == "true_dynamic_call"
    assert diagnostic_properties["mutation"]["callee_binding_kind"] == ("parameter")
    for expression in ("plugin", "send_callback"):
        assert by_expression[expression]["category"] == "true_dynamic_call"
        assert by_expression[expression]["release_blocking"] is False
        assert diagnostic_properties[expression]["callee_binding_kind"] == (
            "assignment"
        )
        assert diagnostic_properties[expression]["callee_binding_scope"] == "module"
    assert by_expression["shadowed_callback"]["category"] == "static_candidate"
    assert by_expression["shadowed_callback"]["release_blocking"] is True
    assert "callee_binding_kind" not in diagnostic_properties["shadowed_callback"]
    assert by_expression["late_callback"]["category"] == "static_candidate"
    assert by_expression["late_callback"]["release_blocking"] is True
    assert "callee_binding_kind" not in diagnostic_properties["late_callback"]
    assert by_expression["operation"]["category"] == "true_dynamic_call"
    assert diagnostic_properties["operation"]["callee_binding_kind"] == ("for_target")
    assert by_expression["item"]["category"] == "static_candidate"
    assert "callee_binding_kind" not in diagnostic_properties["item"]


def test_v2_call_analyzer_classifies_logs_and_definition_time_initializers() -> None:
    source = "\n".join(
        [
            "class Logger:",
            "    def info(self, message: str) -> None:",
            "        return None",
            "",
            "def build_logger() -> Logger:",
            "    return Logger()",
            "",
            "logger = build_logger()",
            "",
            "class Registry:",
            "    value = build_logger()",
            "",
            "def run(logger: Logger) -> None:",
            "    logger.info('ready')",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    by_pair = {(edge.source, edge.target): edge for edge in edges}

    log_edge = by_pair[
        (function_id("pkg.calls.run"), method_id("pkg.calls.Logger.info"))
    ]
    module_init = by_pair[
        (module_id("pkg.calls"), function_id("pkg.calls.build_logger"))
    ]
    class_init = by_pair[
        (class_id("pkg.calls.Registry"), function_id("pkg.calls.build_logger"))
    ]

    assert log_edge.kind == "logs"
    assert log_edge.evidence[0].kind == "ast_log_call"
    assert module_init.kind == "initializes"
    assert class_init.kind == "initializes"


def test_v2_call_analyzer_classifies_dynamic_config_declares_and_uses() -> None:
    source = "\n".join(
        [
            "import os",
            "from httpx import Client",
            "",
            "def get_settings() -> object:",
            "    return object()",
            "",
            "def Field(default: str) -> object:",
            "    return object()",
            "",
            "def post(payload: str) -> None:",
            "    return None",
            "",
            "class Model:",
            "    name = Field(default='x')",
            "",
            "def configure() -> None:",
            "    get_settings()",
            "    os.environ.get('API_KEY')",
            "",
            "def dispatch(plugin: object, handler_name: str) -> None:",
            "    getattr(plugin, handler_name)('payload')",
            "",
            "def send(client: Client) -> None:",
            "    client.post('https://example.test')",
            "",
            "def ambiguous(client) -> None:",
            "    client.post('payload')",
        ]
    )
    nodes = _analyzed_nodes(source)
    analysis = CallAnalyzer(enable_v2=True).analyze(nodes)
    edges = analysis.edges
    by_pair = {(edge.source, edge.target): edge for edge in edges}

    settings_edge = by_pair[
        (function_id("pkg.calls.configure"), function_id("pkg.calls.get_settings"))
    ]
    env_edge = by_pair[(function_id("pkg.calls.configure"), "config:env:API_KEY")]
    field_edge = by_pair[(class_id("pkg.calls.Model"), function_id("pkg.calls.Field"))]
    uses_edge = by_pair[(function_id("pkg.calls.send"), "extsym:httpx.Client.post")]
    dynamic_edges = [
        edge
        for edge in edges
        if edge.source == function_id("pkg.calls.dispatch")
        and edge.kind == "dynamic_call"
    ]
    ambiguous_boundary_edges = [
        edge
        for edge in edges
        if edge.source == function_id("pkg.calls.ambiguous")
        and edge.target == "extsym:httpx.Client.post"
    ]
    ambiguous_dynamic_edges = [
        edge
        for edge in edges
        if edge.source == function_id("pkg.calls.ambiguous")
        and edge.kind == "dynamic_call"
    ]

    assert settings_edge.kind == "configures"
    assert env_edge.kind == "configures"
    assert env_edge.resolution.status == "resolved"
    assert field_edge.kind == "declares"
    assert uses_edge.kind == "uses"
    assert uses_edge.target in {node.id for node in analysis.nodes}
    assert dynamic_edges
    assert all(edge.confidence == "unresolved" for edge in dynamic_edges)
    assert all(edge.resolution.status == "unresolved" for edge in dynamic_edges)
    assert not ambiguous_boundary_edges
    assert ambiguous_dynamic_edges
    assert (
        function_id("pkg.calls.ambiguous"),
        function_id("pkg.calls.post"),
    ) not in by_pair
    assert all(edge.confidence == "unresolved" for edge in ambiguous_dynamic_edges)
    assert all(
        edge.resolution.status == "unresolved" for edge in ambiguous_dynamic_edges
    )
    assert any(
        edge.properties["callsite"]["call_expression"]
        == "getattr(plugin, handler_name)('payload')"
        for edge in dynamic_edges
    )


def test_v2_call_analyzer_resolves_builtins_import_aliases_and_common_methods() -> None:
    source = "\n".join(
        [
            "import ast",
            "import threading",
            "from dataclasses import dataclass",
            "from pathlib import Path",
            "from typing import Any",
            "",
            "@dataclass()",
            "class Payload:",
            "    name: str",
            "",
            "class Context:",
            "    shared: dict[str, object] = {}",
            "",
            "    def __init__(self) -> None:",
            "        self.index: dict[str, object] = {}",
            "        self.values: list[str] = []",
            "",
            "    def use_shared(self) -> None:",
            "        self.shared.get('name')",
            "",
            "def inspect(payload: dict[str, object], values: list[str]) -> None:",
            "    isinstance(payload, dict)",
            "    payload.get('name')",
            "    values.append('x')",
            "    ast.walk(Path('x'))",
            "    Path('x').exists()",
            "    Path('x').joinpath('y')",
            "",
            "def iterated_mapping(repos: list[dict[str, Any]]) -> None:",
            "    for repo in repos:",
            "        repo.get('status')",
            "",
            "def stdlib_boundaries(handler) -> None:",
            "    thread = threading.Thread(target=lambda: None)",
            "    thread.start()",
            "    handler.wfile.write(b'ok')",
            "",
            "def literal_containers() -> None:",
            "    scope_refs = {}",
            "    scope_refs.get('name')",
            "    scope_refs.items()",
            "    values = []",
            "    values.append('x')",
            "",
            "def typed_receiver_chain(context: Context) -> None:",
            "    context.index.get('name')",
            "    context.values.append('x')",
            "",
            "def boundary_methods(redis, mock_redis) -> None:",
            "    dict.fromkeys(['name'])",
            "    redis.get('name')",
            "    mock_redis.setex('name', 60, 'value')",
            "",
            "def ambiguous_common(cache, bag) -> None:",
            "    cache.get('name')",
            "    bag.add('x')",
            "    bag.update(['x'])",
            "",
            "def container_like(entry, recommendations, conditions) -> None:",
            "    entry.get('name')",
            "    recommendations.append('x')",
            "    conditions.append('ready')",
            "",
            "def relation_maps(callers, callees, tests, failed_strategies) -> None:",
            "    callers.get('incoming')",
            "    callees.get('outgoing')",
            "    tests.get('candidates')",
            "    failed_strategies.items()",
            "",
            "def claims_like(token) -> None:",
            "    token.claims.get('sub')",
            "",
            "def text_like(entry, raw_payload, char, tail) -> None:",
            "    entry.message.encode('utf-8')",
            "    raw_payload.decode('utf-8')",
            "    char.isupper()",
            "    tail.isupper()",
        ]
    )
    nodes = _analyzed_nodes(source)
    analysis = CallAnalyzer(enable_v2=True).analyze(nodes)
    by_pair = {(edge.source, edge.target): edge for edge in analysis.edges}
    inspect_id = function_id("pkg.calls.inspect")

    assert by_pair[(inspect_id, "extsym:builtins.isinstance")].kind == "uses"
    assert by_pair[(inspect_id, "extsym:builtins.dict.get")].resolution.strategy == (
        "builtin_receiver_type"
    )
    assert by_pair[(inspect_id, "extsym:builtins.list.append")].resolution.strategy == (
        "builtin_receiver_type"
    )
    assert by_pair[(inspect_id, "extsym:ast.walk")].resolution.strategy == (
        "imported_module_attribute"
    )
    assert by_pair[(inspect_id, "extsym:pathlib.Path")].resolution.strategy == (
        "import_alias"
    )
    assert by_pair[(inspect_id, "extsym:pathlib.Path.exists")].resolution.strategy == (
        "external_receiver_type"
    )
    assert (
        by_pair[(inspect_id, "extsym:pathlib.Path.joinpath")].resolution.strategy
        == "external_receiver_type"
    )
    assert (
        by_pair[(class_id("pkg.calls.Payload"), "extsym:dataclasses.dataclass")].kind
        == "initializes"
    )

    iterated_id = function_id("pkg.calls.iterated_mapping")
    assert (
        by_pair[(iterated_id, "extsym:builtins.dict.get")].resolution.strategy
        == "builtin_receiver_type"
    )

    stdlib_id = function_id("pkg.calls.stdlib_boundaries")
    assert (
        by_pair[(stdlib_id, "extsym:threading.Thread.start")].resolution.strategy
        == "external_receiver_type"
    )
    assert (
        by_pair[(stdlib_id, "extsym:typing.IO.write")].resolution.strategy
        == "receiver_name_boundary_method"
    )

    literal_id = function_id("pkg.calls.literal_containers")
    assert (
        by_pair[(literal_id, "extsym:builtins.dict.get")].resolution.strategy
        == "builtin_receiver_type"
    )
    assert (
        by_pair[(literal_id, "extsym:builtins.dict.items")].resolution.strategy
        == "builtin_receiver_type"
    )
    assert (
        by_pair[(literal_id, "extsym:builtins.list.append")].resolution.strategy
        == "builtin_receiver_type"
    )

    typed_chain_id = function_id("pkg.calls.typed_receiver_chain")
    assert (
        by_pair[(typed_chain_id, "extsym:builtins.dict.get")].resolution.strategy
        == "builtin_receiver_type"
    )
    assert (
        by_pair[(typed_chain_id, "extsym:builtins.list.append")].resolution.strategy
        == "builtin_receiver_type"
    )
    assert (
        by_pair[
            (method_id("pkg.calls.Context.use_shared"), "extsym:builtins.dict.get")
        ].resolution.strategy
        == "builtin_receiver_type"
    )

    boundary_id = function_id("pkg.calls.boundary_methods")
    assert (
        by_pair[(boundary_id, "extsym:builtins.dict.fromkeys")].resolution.strategy
        == "builtin_type_attribute"
    )
    assert (
        by_pair[(boundary_id, "extsym:redis.asyncio.Redis.get")].resolution.strategy
        == "receiver_name_boundary_method"
    )
    assert (
        by_pair[(boundary_id, "extsym:redis.asyncio.Redis.setex")].resolution.strategy
        == "receiver_name_boundary_method"
    )

    ambiguous_id = function_id("pkg.calls.ambiguous_common")
    assert (ambiguous_id, "extsym:collections.abc.Mapping.get") not in by_pair
    assert (ambiguous_id, "extsym:builtins.set.add") not in by_pair
    assert (ambiguous_id, "extsym:builtins.set.update") not in by_pair
    assert {
        edge.properties["callsite"]["raw_expression"]
        for edge in analysis.edges
        if edge.source == ambiguous_id and edge.kind == "dynamic_call"
    } == {"cache.get", "bag.add", "bag.update"}

    container_id = function_id("pkg.calls.container_like")
    assert (
        by_pair[
            (container_id, "extsym:collections.abc.Mapping.get")
        ].resolution.strategy
        == "common_boundary_method"
    )
    assert (
        by_pair[
            (container_id, "extsym:collections.abc.MutableSequence.append")
        ].resolution.strategy
        == "common_boundary_method"
    )

    relation_maps_id = function_id("pkg.calls.relation_maps")
    assert (
        by_pair[
            (relation_maps_id, "extsym:collections.abc.Mapping.get")
        ].resolution.strategy
        == "common_boundary_method"
    )
    assert (
        by_pair[
            (relation_maps_id, "extsym:collections.abc.Mapping.items")
        ].resolution.strategy
        == "common_boundary_method"
    )

    claims_id = function_id("pkg.calls.claims_like")
    assert (
        by_pair[(claims_id, "extsym:collections.abc.Mapping.get")].resolution.strategy
        == "common_boundary_method"
    )
    assert (
        by_pair[(claims_id, "extsym:collections.abc.Mapping.get")].properties[
            "callsite"
        ]["receiver_expression"]
        == "token.claims"
    )

    text_id = function_id("pkg.calls.text_like")
    assert (
        by_pair[(text_id, "extsym:builtins.str.encode")].resolution.strategy
        == "common_boundary_method"
    )
    assert (
        by_pair[(text_id, "extsym:builtins.bytes.decode")].resolution.strategy
        == "common_boundary_method"
    )
    assert (
        by_pair[(text_id, "extsym:builtins.str.isupper")].resolution.strategy
        == "common_boundary_method"
    )


def test_v2_call_analyzer_resolves_common_framework_boundaries() -> None:
    source = "\n".join(
        [
            "router = object()",
            "_REQUESTS_TOTAL = object()",
            "",
            "@router.post('/items')",
            "def endpoint() -> None:",
            "    return None",
            "",
            "def run(logger, session, stmt, result, memory) -> None:",
            "    logger.warning('slow')",
            "    session.execute(stmt)",
            "    stmt.where(True).order_by('id').limit(10)",
            "    result.scalars().all()",
            "    _REQUESTS_TOTAL.labels(kind='item').inc()",
            "    _REDIS_MEMORY_FRAGMENTATION_RATIO.set(1)",
            "    memory.created_at.isoformat()",
            "    ValueError('bad')",
            "",
            "def config_call(CONFIG) -> None:",
            "    CONFIG.labels()",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    by_pair = {(edge.source, edge.target): edge for edge in edges}
    run_id = function_id("pkg.calls.run")

    assert by_pair[(module_id("pkg.calls"), "extsym:fastapi.APIRouter.post")].kind == (
        "initializes"
    )
    assert by_pair[(run_id, "extsym:logging.Logger.warning")].kind == "logs"
    assert by_pair[(run_id, "extsym:sqlalchemy.orm.Session.execute")].kind == "uses"
    assert by_pair[(run_id, "extsym:sqlalchemy.sql.Select.where")].kind == "uses"
    assert by_pair[(run_id, "extsym:sqlalchemy.sql.Select.order_by")].kind == "uses"
    assert by_pair[(run_id, "extsym:sqlalchemy.sql.Select.limit")].kind == "uses"
    assert by_pair[(run_id, "extsym:sqlalchemy.engine.Result.scalars")].kind == "uses"
    assert by_pair[(run_id, "extsym:sqlalchemy.engine.Result.all")].kind == "uses"
    assert (
        by_pair[
            (run_id, "extsym:prometheus_client.metrics.MetricWrapperBase.labels")
        ].kind
        == "uses"
    )
    assert by_pair[(run_id, "extsym:prometheus_client.Counter.inc")].kind == "uses"
    assert by_pair[(run_id, "extsym:prometheus_client.Gauge.set")].kind == "uses"
    assert by_pair[(run_id, "extsym:datetime.datetime.isoformat")].kind == "uses"
    assert by_pair[(run_id, "extsym:builtins.ValueError")].kind == "uses"
    assert by_pair[(run_id, "extsym:logging.Logger.warning")].resolution.strategy == (
        "receiver_name_boundary_method"
    )
    config_id = function_id("pkg.calls.config_call")
    assert (
        config_id,
        "extsym:prometheus_client.metrics.MetricWrapperBase.labels",
    ) not in by_pair
    assert any(
        edge.source == config_id
        and edge.kind == "dynamic_call"
        and edge.properties["callsite"]["raw_expression"] == "CONFIG.labels"
        for edge in edges
    )


def test_v2_call_analyzer_resolves_service_container_singleton_methods() -> None:
    source = "\n".join(
        [
            "class ServiceContainer:",
            "    def get_db_session(self) -> object:",
            "        return object()",
            "",
            "def run(container) -> object:",
            "    return container.get_db_session()",
        ]
    )
    nodes = _analyzed_nodes(
        source,
        module="shared.service_container",
        path="backend/src/shared/service_container.py",
    )
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    by_pair = {(edge.source, edge.target): edge for edge in edges}

    edge = by_pair[
        (
            function_id("shared.service_container.run"),
            method_id("shared.service_container.ServiceContainer.get_db_session"),
        )
    ]
    assert edge.kind == "calls"
    assert edge.resolution.strategy == "receiver_name_boundary_method"


def test_v2_call_analyzer_resolves_classmethod_cls_and_local_helpers() -> None:
    source = "\n".join(
        [
            "class Builder:",
            "    @staticmethod",
            "    def normalize(value: str) -> str:",
            "        return value.strip()",
            "",
            "    @classmethod",
            "    def make(cls) -> 'Builder':",
            "        return cls()",
            "",
            "    def walk(self, count: int) -> None:",
            "        if count:",
            "            self.walk(count - 1)",
            "",
            "class Request:",
            "    class State:",
            "        pass",
            "    state = State()",
            "",
            "def settings() -> object:",
            "    return object()",
            "",
            "class Config:",
            "    @staticmethod",
            "    def settings() -> object:",
            "        return object()",
            "",
            "def alias_call() -> object:",
            "    real_settings = Config.settings",
            "    return real_settings()",
            "",
            "def run(value: str) -> str:",
            "    def clean(raw: str) -> str:",
            "        return raw.strip()",
            "    return Builder.normalize(clean(value))",
        ]
    )
    nodes = _analyzed_nodes(source)
    analysis = CallAnalyzer(enable_v2=True).analyze(nodes)
    by_pair = {(edge.source, edge.target): edge for edge in analysis.edges}

    cls_edge = by_pair[
        (method_id("pkg.calls.Builder.make"), class_id("pkg.calls.Builder"))
    ]
    static_edge = by_pair[
        (function_id("pkg.calls.run"), method_id("pkg.calls.Builder.normalize"))
    ]
    recursive_edge = by_pair[
        (method_id("pkg.calls.Builder.walk"), method_id("pkg.calls.Builder.walk"))
    ]
    class_local_edges = [
        edge
        for edge in analysis.edges
        if edge.source == class_id("pkg.calls.Request")
        and edge.target.startswith("local:")
    ]
    alias_edge = by_pair[
        (function_id("pkg.calls.alias_call"), method_id("pkg.calls.Config.settings"))
    ]
    local_edges = [
        edge
        for edge in analysis.edges
        if edge.source == function_id("pkg.calls.run")
        and edge.target == function_id("pkg.calls.run.clean")
    ]

    assert cls_edge.kind == "constructs"
    assert cls_edge.resolution.strategy == "class_receiver_constructor"
    assert static_edge.resolution.strategy == "class_attribute_method"
    assert recursive_edge.resolution.strategy == "same_class_receiver"
    assert class_local_edges
    assert class_local_edges[0].resolution.strategy == "local_definition"
    assert alias_edge.resolution.strategy == "local_callable_alias"
    assert local_edges
    assert local_edges[0].resolution.strategy == "local_definition"
    assert local_edges[0].kind == "calls"


def test_v2_call_analyzer_resolves_module_local_helper_inside_main_guard() -> None:
    source = "\n".join(
        [
            "import asyncio",
            "",
            "if __name__ == '__main__':",
            "    async def _main() -> None:",
            "        pass",
            "",
            "    asyncio.run(_main())",
        ]
    )
    nodes = _analyzed_nodes(
        source,
        module="evals.eval_maintenance",
        path="backend/evals/eval_maintenance.py",
    )
    analysis = CallAnalyzer(enable_v2=True).analyze(nodes)
    local_edges = [
        edge
        for edge in analysis.edges
        if edge.source == module_id("evals.eval_maintenance")
        and edge.target.startswith("local:")
        and edge.evidence
        and edge.evidence[0].detail == "_main"
    ]
    unresolved_main_edges = [
        edge
        for edge in analysis.edges
        if edge.source == module_id("evals.eval_maintenance")
        and edge.target.startswith("unresolved:")
        and edge.evidence
        and edge.evidence[0].detail == "_main"
    ]

    assert local_edges
    assert local_edges[0].resolution.strategy == "local_definition"
    assert not unresolved_main_edges


def test_v2_call_analyzer_resolves_mock_assert_in_test_files() -> None:
    """Positive: mock assert methods resolve when source is a test file."""
    source = "\n".join(
        [
            "def test_example(mock_session) -> None:",
            "    mock_session.execute.assert_awaited_once()",
            "    mock_session.commit.assert_called_once_with()",
        ]
    )
    nodes = _analyzed_nodes(
        source,
        module="tests.test_example",
        path="backend/tests/unit/test_example.py",
    )
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    mock_edges = [
        edge for edge in edges if edge.target.startswith("extsym:unittest.mock.")
    ]

    assert len(mock_edges) == 2
    assert all(edge.resolution.strategy == "mock_assert_method" for edge in mock_edges)
    assert {edge.target for edge in mock_edges} == {
        "extsym:unittest.mock.AsyncMock.assert_awaited_once",
        "extsym:unittest.mock.Mock.assert_called_once_with",
    }


def test_v2_call_analyzer_rejects_mock_assert_in_non_test_non_mock_context() -> None:
    """Negative: assert_called_once on a non-mock receiver in production code
    must NOT be resolved as unittest.mock."""
    source = "\n".join(
        [
            "def verify(probe) -> None:",
            "    probe.assert_called_once()",
        ]
    )
    nodes = _analyzed_nodes(
        source,
        module="pkg.verifier",
        path="src/pkg/verifier.py",
    )
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    mock_edges = [
        edge for edge in edges if edge.target.startswith("extsym:unittest.mock.")
    ]
    dynamic_edges = [
        edge
        for edge in edges
        if edge.source == function_id("pkg.verifier.verify")
        and edge.kind == "dynamic_call"
    ]

    # Should NOT resolve as mock — receiver is "probe", not mock-prefixed.
    assert not mock_edges
    # Should fall through to unresolved / dynamic_call instead.
    assert dynamic_edges


def test_v2_call_analyzer_rejects_mock_assert_in_contest_path() -> None:
    """Negative: paths containing 'test' as a substring (contest/, latest/,
    attestation/) must NOT be treated as test files."""
    source = "\n".join(
        [
            "def check(probe) -> None:",
            "    probe.assert_called_once()",
        ]
    )
    # "contest" contains "test" as a substring — must not trigger.
    for path, module in [
        ("src/pkg/contest/verifier.py", "pkg.contest.verifier"),
        ("src/attestation/handler.py", "attestation.handler"),
    ]:
        nodes = _analyzed_nodes(source, module=module, path=path)
        edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
        mock_edges = [
            edge for edge in edges if edge.target.startswith("extsym:unittest.mock.")
        ]
        assert not mock_edges, f"False positive on production path: {path}"


def test_v2_call_analyzer_rejects_argparse_for_non_parser_receiver() -> None:
    """Negative: form.add_argument() must NOT resolve to argparse."""
    source = "\n".join(
        [
            "def build_form(form) -> None:",
            "    form.add_argument('--name')",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    argparse_edges = [
        edge for edge in edges if edge.target.startswith("extsym:argparse.")
    ]
    assert not argparse_edges, "form.add_argument incorrectly resolved to argparse"


def test_v2_call_analyzer_rejects_test_client_for_generic_client() -> None:
    """Negative: github_client.get() must NOT resolve to TestClient."""
    source = "\n".join(
        [
            "def fetch(github_client) -> None:",
            "    github_client.get('/repos')",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    test_client_edges = [
        edge
        for edge in edges
        if edge.target.startswith("extsym:starlette.testclient.TestClient.")
    ]
    assert not test_client_edges, "github_client.get incorrectly resolved to TestClient"


def test_v2_call_analyzer_rejects_unique_fallback_for_import_alias() -> None:
    """Negative: external.checkout() must NOT resolve via unique_method_fallback
    when 'external' is an import alias."""
    source = "\n".join(
        [
            "import external",
            "",
            "class InternalService:",
            "    def checkout(self) -> None: ...",
            "",
            "def use(obj) -> None:",
            "    external.checkout()",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    fn_id = function_id("pkg.calls.use")
    fallback_edges = [
        edge
        for edge in edges
        if edge.source == fn_id
        and edge.resolution is not None
        and edge.resolution.strategy == "unique_method_fallback"
    ]
    assert not fallback_edges, (
        "import-aliased external.checkout() incorrectly resolved via "
        "unique_method_fallback"
    )


def test_v2_call_analyzer_rejects_unique_fallback_for_unrelated_param() -> None:
    """Negative: external.checkout() must NOT resolve via unique_method_fallback
    when 'external' is an untyped parameter with no lexical affinity to the
    candidate class."""
    source = "\n".join(
        [
            "class InternalService:",
            "    def checkout(self) -> None: ...",
            "",
            "def run(external) -> None:",
            "    external.checkout()",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    fn_id = function_id("pkg.calls.run")
    fallback_edges = [
        edge
        for edge in edges
        if edge.source == fn_id
        and edge.resolution is not None
        and edge.resolution.strategy == "unique_method_fallback"
    ]
    assert not fallback_edges, (
        "unrelated-param external.checkout() incorrectly resolved via "
        "unique_method_fallback"
    )


def test_v2_call_analyzer_accepts_unique_fallback_for_matching_receiver() -> None:
    """Positive: service.checkout() should resolve via unique_method_fallback
    when 'service' has lexical affinity to 'InternalService'."""
    source = "\n".join(
        [
            "class InternalService:",
            "    def checkout(self) -> None: ...",
            "",
            "def run(service) -> None:",
            "    service.checkout()",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    fn_id = function_id("pkg.calls.run")
    fallback_edges = [
        edge
        for edge in edges
        if edge.source == fn_id
        and edge.resolution is not None
        and edge.resolution.strategy == "unique_method_fallback"
    ]
    assert len(fallback_edges) == 1, (
        "matching-receiver service.checkout() should resolve via "
        "unique_method_fallback"
    )


def test_extends_subscript_base_resolves_to_root_symbol() -> None:
    """P1 regression: ``BaseRepo[Memory]`` must resolve to ``BaseRepo``, not
    ``extsym:BaseRepo[Memory]``."""
    source = "\n".join(
        [
            "class BaseRepo:",
            "    def save(self): ...",
            "",
            "class MemoryRepo(BaseRepo[Memory]):",
            "    pass",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    extends_edges = [e for e in edges if e.kind == "extends"]
    assert len(extends_edges) == 1
    edge = extends_edges[0]
    assert edge.source == class_id("pkg.calls.MemoryRepo")
    assert edge.target == class_id("pkg.calls.BaseRepo")
    assert edge.confidence == "confirmed"
    # Evidence detail preserves original expression
    assert edge.evidence[0].detail == "BaseRepo[Memory]"


def test_extends_unresolved_bare_name_skips_edge() -> None:
    """P2 regression: unknown bare base without import evidence must NOT produce
    a confirmed extsym edge."""
    source = "\n".join(
        [
            "class Child(UnknownBase):",
            "    pass",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    extends_edges = [e for e in edges if e.kind == "extends"]
    assert (
        len(extends_edges) == 0
    ), "Unresolved bare base should not generate an extends edge"


def test_extends_confidence_levels() -> None:
    """Extends edges must reflect evidence quality: same-module → confirmed,
    global unique → heuristic."""
    # --- same-module: confirmed ---
    source = "\n".join(
        [
            "class Base:",
            "    pass",
            "",
            "class Child(Base):",
            "    pass",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    extends_edges = [e for e in edges if e.kind == "extends"]
    assert len(extends_edges) == 1
    assert extends_edges[0].confidence == "confirmed"

    # --- cross-module global unique: heuristic ---
    # Simulate: module A defines UniqueBase, module B's Child(UniqueBase)
    # resolves via global name uniqueness → heuristic.
    source_a = "\n".join(
        [
            "class UniqueBase:",
            "    pass",
        ]
    )
    source_b = "\n".join(
        [
            "class Consumer(UniqueBase):",
            "    pass",
        ]
    )
    nodes_a = _analyzed_nodes(
        source_a, module="pkg.base_mod", path="src/pkg/base_mod.py"
    )
    nodes_b = _analyzed_nodes(
        source_b, module="pkg.consumer", path="src/pkg/consumer.py"
    )
    # Combine into a single node pool for cross-module resolution
    all_nodes = [*nodes_a, *nodes_b]
    edges = CallAnalyzer(enable_v2=True).analyze(all_nodes).edges
    extends_edges = [e for e in edges if e.kind == "extends"]
    assert len(extends_edges) == 1
    assert extends_edges[0].source == class_id("pkg.consumer.Consumer")
    assert extends_edges[0].target == class_id("pkg.base_mod.UniqueBase")
    assert extends_edges[0].confidence == "heuristic"


def test_extends_import_alias_with_subscript() -> None:
    """Import alias + subscript combo: ``from pkg.base import BaseRepo`` +
    ``class Child(BaseRepo[Memory])`` must resolve via import alias."""
    source = "\n".join(
        [
            "from pkg.base import BaseRepo",
            "",
            "class Child(BaseRepo[Memory]):",
            "    pass",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    extends_edges = [e for e in edges if e.kind == "extends"]
    assert len(extends_edges) == 1
    edge = extends_edges[0]
    assert edge.source == class_id("pkg.calls.Child")
    # Import resolves to external symbol (pkg.base.BaseRepo not in node pool)
    assert "BaseRepo" in edge.target
    assert edge.confidence == "confirmed"
    # Evidence preserves original subscripted expression
    assert edge.evidence[0].detail == "BaseRepo[Memory]"


def _analyzed_nodes(
    source: str,
    *,
    module: str = "pkg.calls",
    path: str = "src/pkg/calls.py",
) -> list[Node]:
    tree = ast.parse(source)
    file_record = FileRecord(
        path=path,
        abs_path=f"/repo/{path}",
        source_root="src",
        module=module,
        file_hash="hash",
        line_count=1,
    )
    symbol_analysis = SymbolAnalyzer().analyze(file_record, tree)
    module_name = module.rsplit(".", 1)[-1]
    nodes = [
        Node(
            id=module_id(module),
            kind="module",
            name=module_name,
            qualname=module,
            path=path,
        ),
        *symbol_analysis.nodes,
    ]
    nodes[0].properties["callsites"] = symbol_analysis.module_callsites
    module_names = {module}
    BindingAnalyzer().analyze(file_record, tree, module_names).attach_to_nodes(nodes)
    TypeRefAnalyzer().analyze(file_record, tree, nodes, module_names).attach_to_nodes(
        nodes
    )
    return nodes


# ---------------------------------------------------------------------------
# implements / overrides tests
# ---------------------------------------------------------------------------


def test_implements_overlay_for_abc_subclass() -> None:
    """Concrete class inheriting a user-defined ABC generates both
    ``extends`` and ``implements`` edges."""
    source = "\n".join(
        [
            "from abc import ABC, abstractmethod",
            "",
            "class BaseRepo(ABC):",
            "    @abstractmethod",
            "    def save(self): ...",
            "",
            "class MemoryRepo(BaseRepo):",
            "    def save(self): return True",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    extends_edges = [e for e in edges if e.kind == "extends"]
    implements_edges = [e for e in edges if e.kind == "implements"]

    # BaseRepo(ABC) → extends only (declaring, not implementing)
    base_extends = [
        e for e in extends_edges if e.source == class_id("pkg.calls.BaseRepo")
    ]
    assert len(base_extends) >= 1  # extends abc.ABC (extsym)

    # MemoryRepo(BaseRepo) → extends + implements
    child_extends = [
        e for e in extends_edges if e.source == class_id("pkg.calls.MemoryRepo")
    ]
    assert len(child_extends) == 1
    assert child_extends[0].target == class_id("pkg.calls.BaseRepo")

    child_implements = [
        e for e in implements_edges if e.source == class_id("pkg.calls.MemoryRepo")
    ]
    assert len(child_implements) == 1
    assert child_implements[0].target == class_id("pkg.calls.BaseRepo")
    assert child_implements[0].evidence[0].kind == "ast_abc_impl"


def test_implements_overlay_for_protocol() -> None:
    """Concrete class implementing a user-defined Protocol generates both
    ``extends`` and ``implements`` edges."""
    source = "\n".join(
        [
            "from typing import Protocol",
            "",
            "class Notifier(Protocol):",
            "    def notify(self, msg: str) -> None: ...",
            "",
            "class EmailNotifier(Notifier):",
            "    def notify(self, msg: str) -> None: print(msg)",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    implements_edges = [e for e in edges if e.kind == "implements"]

    # EmailNotifier → implements Notifier
    child_impl = [
        e for e in implements_edges if e.source == class_id("pkg.calls.EmailNotifier")
    ]
    assert len(child_impl) == 1
    assert child_impl[0].target == class_id("pkg.calls.Notifier")
    assert child_impl[0].evidence[0].kind == "ast_protocol_impl"


def test_no_implements_for_protocol_declaration() -> None:
    """Direct subclass of ``typing.Protocol`` is *declaring* a protocol,
    not implementing one — no ``implements`` edge."""
    source = "\n".join(
        [
            "from typing import Protocol",
            "",
            "class Notifier(Protocol):",
            "    def notify(self, msg: str) -> None: ...",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    implements_edges = [e for e in edges if e.kind == "implements"]
    assert (
        len(implements_edges) == 0
    ), "Protocol declaration should NOT generate implements edge"


def test_no_implements_for_abc_declaration() -> None:
    """Direct subclass of ``abc.ABC`` is *declaring* an abstract class,
    not implementing one — no ``implements`` edge."""
    source = "\n".join(
        [
            "from abc import ABC, abstractmethod",
            "",
            "class BaseService(ABC):",
            "    @abstractmethod",
            "    def execute(self): ...",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    implements_edges = [e for e in edges if e.kind == "implements"]
    assert (
        len(implements_edges) == 0
    ), "ABC declaration should NOT generate implements edge"


def test_extends_only_for_concrete_class() -> None:
    """Inheriting a concrete (non-abstract) class generates only ``extends``,
    never ``implements``."""
    source = "\n".join(
        [
            "class Animal:",
            "    def speak(self): return 'generic'",
            "",
            "class Dog(Animal):",
            "    def speak(self): return 'woof'",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    implements_edges = [e for e in edges if e.kind == "implements"]
    extends_edges = [
        e
        for e in edges
        if e.kind == "extends" and e.source == class_id("pkg.calls.Dog")
    ]
    assert len(extends_edges) == 1
    assert len(implements_edges) == 0


def test_overrides_edge_for_method() -> None:
    """Child class overriding a parent method generates ``overrides`` edge
    when both parent and child methods exist in the graph."""
    source = "\n".join(
        [
            "class Base:",
            "    def process(self): return 1",
            "    def helper(self): return 2",
            "",
            "class Child(Base):",
            "    def process(self): return 3",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    override_edges = [e for e in edges if e.kind == "overrides"]
    assert len(override_edges) == 1
    assert override_edges[0].source == method_id("pkg.calls.Child.process")
    assert override_edges[0].target == method_id("pkg.calls.Base.process")
    assert override_edges[0].confidence == "confirmed"
    assert override_edges[0].evidence[0].kind == "ast_method_override"


def test_no_overrides_for_external_parent() -> None:
    """External parent class (resolved via import to extsym) must NOT
    generate ``overrides`` edges without precise evidence."""
    source = "\n".join(
        [
            "from some_lib import ExternalBase",
            "",
            "class MyImpl(ExternalBase):",
            "    def run(self): return True",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    override_edges = [e for e in edges if e.kind == "overrides"]
    assert (
        len(override_edges) == 0
    ), "External parents without precise evidence should not generate overrides"


def test_overrides_abstract_method() -> None:
    """Implementing an abstract method generates ``overrides`` edge."""
    source = "\n".join(
        [
            "from abc import ABC, abstractmethod",
            "",
            "class BaseHandler(ABC):",
            "    @abstractmethod",
            "    def handle(self): ...",
            "",
            "class ConcreteHandler(BaseHandler):",
            "    def handle(self): return True",
        ]
    )
    nodes = _analyzed_nodes(source)
    edges = CallAnalyzer(enable_v2=True).analyze(nodes).edges
    override_edges = [e for e in edges if e.kind == "overrides"]
    assert len(override_edges) == 1
    assert override_edges[0].source == method_id("pkg.calls.ConcreteHandler.handle")
    assert override_edges[0].target == method_id("pkg.calls.BaseHandler.handle")
    assert override_edges[0].confidence == "confirmed"
    # Should also have implements edge
    impl_edges = [e for e in edges if e.kind == "implements"]
    assert len(impl_edges) == 1


def _unresolved_diagnostics_for(source: str) -> list[dict[str, object]]:
    """Return unresolved-callsite diagnostics for one synthetic module."""

    nodes = _analyzed_nodes(source)
    analysis = CallAnalyzer(enable_v2=True).analyze(nodes)
    diagnostics = unresolved_callsite_diagnostics(
        IndexMetadata(
            index_version="identity-probe",
            repo_root="/repo",
            source_roots=["pkg"],
        ),
        [*nodes, *analysis.nodes],
        analysis.edges,
        repo_id="test",
        frontend_name="python-v1-compat-shim",
    )
    return [item.model_dump(mode="json") for item in diagnostics]


def _lifecycle_keys_for(source: str, expression: str = "obj.run") -> list[tuple]:
    """Return the lifecycle keys the store would use for one probe expression."""

    nodes = _analyzed_nodes(source)
    analysis = CallAnalyzer(enable_v2=True).analyze(nodes)
    diagnostics = unresolved_callsite_diagnostics(
        IndexMetadata(
            index_version="identity-probe",
            repo_root="/repo",
            source_roots=["pkg"],
        ),
        [*nodes, *analysis.nodes],
        analysis.edges,
        repo_id="test",
        frontend_name="python-v1-compat-shim",
    )
    return [
        GraphStoreWriter._diagnostic_lifecycle_key(item)
        for item in diagnostics
        if item.properties.get("raw_expression") == expression
    ]


_UNRESOLVED_PROBE_SOURCE = "\n".join(
    [
        "def handler(obj):",
        "    return obj.run()",
        "",
    ]
)


def test_unresolved_callsite_identity_survives_a_line_shift() -> None:
    """Moving a callsite must not present it as a newly discovered one.

    Without a fact id the diagnostic lifecycle key degrades to path plus start
    line, so inserting an unrelated line above an unresolved call reports it as
    new. Any gate that blocks on newly introduced unresolved calls would then
    reject ordinary edits.
    """

    original = _unresolved_diagnostics_for(_UNRESOLVED_PROBE_SOURCE)
    shifted = _unresolved_diagnostics_for(
        "# inserted\n# inserted\n" + _UNRESOLVED_PROBE_SOURCE
    )

    assert original and shifted
    assert [item["start_line"] for item in original] != [
        item["start_line"] for item in shifted
    ], "the probe must actually move, or the test proves nothing"
    # The stable subject is used instead of fact_id, which Change Safety reads
    # as the diagnostic's identity subject and which must keep meaning a row in
    # semantic_facts.
    assert all(item["fact_id"] is None for item in original)
    assert _lifecycle_keys_for(_UNRESOLVED_PROBE_SOURCE) == _lifecycle_keys_for(
        "# inserted\n# inserted\n" + _UNRESOLVED_PROBE_SOURCE
    )


def test_repeated_identical_callsites_keep_distinct_identities() -> None:
    """Byte-identical calls in one scope must not collapse into one identity.

    The occurrence counter is assigned per stable subject, so subject plus
    occurrence stays unique within an index by construction. A collision here
    would silently merge separate unresolved calls into a single lifecycle
    record and hide disclosure.
    """

    source = "\n".join(
        [
            "def handler(obj):",
            "    first = obj.run()",
            "    second = obj.run()",
            "    third = obj.run()",
            "    return first, second, third",
            "",
        ]
    )

    records = _unresolved_diagnostics_for(source)
    probes = [
        item for item in records if item["properties"]["raw_expression"] == "obj.run"
    ]

    assert len(probes) == 3
    assert len(set(_lifecycle_keys_for(source))) == 3
    assert sorted(
        int(item["properties"]["stable_callsite_occurrence"]) for item in probes
    ) == [1, 2, 3]


def test_removing_one_repeated_callsite_renumbers_the_survivors() -> None:
    """The accepted limit of an ordinal occurrence, measured rather than assumed.

    Occurrence is a position-ordered counter within a stable subject, so
    deleting the first of several byte-identical calls renumbers the survivors
    into the freed slots. The surviving identities are therefore a subset of
    the originals: nothing looks new, and two calls differing only by position
    carry no other distinguishing evidence, so this loss is accepted rather
    than solved.
    """

    three = "\n".join(
        [
            "def handler(obj):",
            "    first = obj.run()",
            "    second = obj.run()",
            "    third = obj.run()",
            "    return first, second, third",
            "",
        ]
    )
    two = "\n".join(
        [
            "def handler(obj):",
            "    second = obj.run()",
            "    third = obj.run()",
            "    return second, third",
            "",
        ]
    )

    before = set(_lifecycle_keys_for(three))
    after = set(_lifecycle_keys_for(two))

    assert len(before) == 3
    assert len(after) == 2
    assert after.issubset(before)
    assert not after - before, "a pure removal must not look like new debt"


def test_replacing_one_repeated_callsite_is_invisible_to_identity_alone() -> None:
    """A removal plus an addition inside one repeated group cancels out.

    Deleting the first of three byte-identical calls and adding a fourth
    reproduces exactly the original occurrence slots, so comparing identity
    sets between two indexes reports no change at all. The added call is real
    new unresolved debt and is invisible here. This is a false negative, not a
    false positive: a gate that blocks on newly introduced unresolved calls
    cannot be built from identity-set difference alone, and must also compare
    per-subject counts.
    """

    three = "\n".join(
        [
            "def handler(obj):",
            "    first = obj.run()",
            "    second = obj.run()",
            "    third = obj.run()",
            "    return first, second, third",
            "",
        ]
    )
    replaced = "\n".join(
        [
            "def handler(obj):",
            "    second = obj.run()",
            "    third = obj.run()",
            "    fourth = obj.run()",
            "    return second, third, fourth",
            "",
        ]
    )

    before = set(_lifecycle_keys_for(three))
    after = set(_lifecycle_keys_for(replaced))

    assert len(before) == len(after) == 3
    assert after == before, "identity difference alone cannot see this change"
    # The count per stable subject is what does stay comparable.
    assert len(_lifecycle_keys_for(three)) == len(_lifecycle_keys_for(replaced))
