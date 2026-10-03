import base64
import json
import shutil
import subprocess
import zlib
from pathlib import Path

import pytest

from arcgraph.pipeline.frontends import (
    FrontendRegistry,
    LanguageFrontend,
    PythonSemanticFrontend,
    TypeScriptSemanticFrontend,
    default_frontend_registry,
)
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.contracts import (
    FrontendContractError,
    FrontendGraphFragment,
)
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.core.schemas import (
    BuildWarning,
    Edge,
    Evidence,
    FileRecord,
    FrontendAnalyzeRequest,
    FrontendAnalyzeResult,
    FrontendCapabilities,
    IndexMetadata,
    Node,
)
from arcgraph.pipeline import typescript_frontend
from arcgraph.pipeline.typescript_frontend import TypeScriptGraphAnalyzer


def _file_record(
    tmp_path: Path,
    path: str,
    *,
    module: str = "module",
    line_count: int = 1,
) -> FileRecord:
    return FileRecord(
        path=path,
        abs_path=str(tmp_path / path),
        source_root=path.split("/", 1)[0],
        module=module,
        file_hash="test",
        line_count=line_count,
    )


class _ContractFrontend(LanguageFrontend):
    name = "contract-test"
    language_ids = ("python",)
    version = "1.0"

    def __init__(self, fragment: object) -> None:
        self.fragment = fragment

    def capabilities(self) -> FrontendCapabilities:
        return FrontendCapabilities(
            name=self.name,
            version=self.version,
            language="python",
            capabilities={"mode": "contract-test"},
            file_extensions=list(self.file_extensions),
        )

    def detect(self, request: FrontendAnalyzeRequest) -> bool:
        return True

    def analyze(self, request: FrontendAnalyzeRequest) -> FrontendAnalyzeResult:
        return FrontendAnalyzeResult(frontend=self.capabilities())

    def analyze_to_graph(
        self,
        files: list[FileRecord],
        warnings: list[BuildWarning] | None = None,
        module_names: set[str] | None = None,
        call_context_nodes: list[Node] | None = None,
    ) -> FrontendGraphFragment:
        return self.fragment  # type: ignore[return-value]


def _analyze_with_contract_frontend(
    tmp_path: Path,
    fragment: object,
) -> FrontendGraphFragment:
    source_dir = tmp_path / "src"
    source_dir.mkdir(exist_ok=True)
    source_path = source_dir / "app.py"
    source_path.write_text("def app() -> None:\n    pass\n", encoding="utf-8")
    frontend = _ContractFrontend(fragment)
    indexer = ArcGraphIndexer(
        repo_root=tmp_path,
        output_dir=tmp_path / "arcgraph",
        source_roots=[SourceRoot("src")],
        frontend_registry=FrontendRegistry([frontend]),
    )
    file_record = _file_record(tmp_path, "src/app.py", module="app")
    indexer.analyze_files([file_record], frontends=[frontend])
    assert isinstance(fragment, FrontendGraphFragment)
    return fragment


def test_frontend_registry_detects_python_frontend(tmp_path: Path) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "pkg.py").write_text("def a() -> None:\n    pass\n", encoding="utf-8")
    frontend = PythonSemanticFrontend()
    registry = FrontendRegistry([frontend])
    request = FrontendAnalyzeRequest(
        repo_root=str(tmp_path),
        source_roots=["src"],
        index_version="test",
    )

    assert registry.frontends == (frontend,)
    assert registry.detect_frontends(request) == [frontend]
    assert registry.analyze(request)[0].status == "available"


def test_frontend_registry_rejects_duplicate_names() -> None:
    fragment = FrontendGraphFragment()
    frontend = _ContractFrontend(fragment)

    with pytest.raises(ValueError, match="Frontend already registered: contract-test"):
        FrontendRegistry([frontend, frontend])


def test_default_frontend_registry_inventory_is_stable_and_json_serializable(
    tmp_path: Path,
) -> None:
    registry = default_frontend_registry(repo_root=tmp_path)
    inventory = registry.inventory()

    assert [entry["name"] for entry in inventory] == [
        "python-v1-compat-shim",
        "typescript-static",
    ]
    assert "scip-protocol" not in {entry["name"] for entry in inventory}
    assert inventory[0]["language_ids"] == ["python"]
    assert inventory[0]["file_extensions"] == [".py"]
    assert inventory[1]["language_ids"] == ["typescript", "javascript"]
    assert inventory[1]["file_extensions"] == [
        ".ts",
        ".tsx",
        ".mts",
        ".cts",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".vue",
    ]
    assert inventory[0]["capabilities"]["capabilities"]["mode"] == "v1-compat"
    assert inventory[0]["capabilities"]["capabilities"]["language_tier"] == "L3"
    assert (
        inventory[0]["capabilities"]["capabilities"]["language_tier_languages"]
        == "python"
    )
    assert (
        inventory[1]["capabilities"]["capabilities"]["mode"]
        == "typescript-compiler-ast"
    )
    assert inventory[1]["capabilities"]["capabilities"]["language_tier"] == "L3"
    assert (
        inventory[1]["capabilities"]["capabilities"]["language_tier_languages"]
        == "typescript,javascript"
    )
    json.dumps(inventory)


def test_indexer_accepts_valid_frontend_contract_and_fills_frontend(
    tmp_path: Path,
) -> None:
    fragment = FrontendGraphFragment(
        nodes=[
            Node(
                id="fn:app.app",
                kind="function",
                name="app",
                path="src/app.py",
                start_line=1,
            )
        ],
        edges=[
            Edge(
                source="fn:app.app",
                target="extsym:builtins.print",
                kind="calls",
                evidence=[Evidence(kind="contract_test", path="src/app.py")],
            )
        ],
        warnings=[
            BuildWarning(
                kind="contract_test_warning",
                message="contract warning",
                path="src/app.py",
            )
        ],
        adapter_metrics={"contract": {"nodes": 1, "edges": 1}},
        phase_timings={"extract": 0.01},
    )

    validated = _analyze_with_contract_frontend(tmp_path, fragment)

    assert validated.frontend is not None
    assert validated.frontend.name == "contract-test"
    assert validated.frontend.version == "1.0"


@pytest.mark.parametrize(
    ("field_name", "bad_value", "message"),
    [
        ("nodes", ["not-node"], r"contract-test: nodes\[0\] must be Node"),
        ("edges", ["not-edge"], r"contract-test: edges\[0\] must be Edge"),
        (
            "warnings",
            ["not-warning"],
            r"contract-test: warnings\[0\] must be BuildWarning",
        ),
    ],
)
def test_indexer_rejects_invalid_frontend_fragment_items(
    tmp_path: Path,
    field_name: str,
    bad_value: list[str],
    message: str,
) -> None:
    fragment = FrontendGraphFragment()
    setattr(fragment, field_name, bad_value)

    with pytest.raises(FrontendContractError, match=message):
        _analyze_with_contract_frontend(tmp_path, fragment)


def test_indexer_rejects_non_fragment_return(tmp_path: Path) -> None:
    with pytest.raises(
        FrontendContractError,
        match=r"contract-test: analyze_to_graph\(\) must return FrontendGraphFragment",
    ):
        _analyze_with_contract_frontend(tmp_path, {"nodes": []})


@pytest.mark.parametrize(
    "frontend",
    [
        FrontendCapabilities(name="other", version="1.0"),
        FrontendCapabilities(name="contract-test", version="2.0"),
    ],
)
def test_indexer_rejects_frontend_identity_mismatch(
    tmp_path: Path,
    frontend: FrontendCapabilities,
) -> None:
    fragment = FrontendGraphFragment(
        frontend=frontend,
    )

    with pytest.raises(
        FrontendContractError,
        match="contract-test: fragment.frontend must match registered frontend",
    ):
        _analyze_with_contract_frontend(tmp_path, fragment)


def test_indexer_rejects_invalid_frontend_fragment_containers(tmp_path: Path) -> None:
    fragment = FrontendGraphFragment()
    fragment.nodes = "not-a-list"  # type: ignore[assignment]

    with pytest.raises(
        FrontendContractError,
        match="contract-test: nodes must be a list",
    ):
        _analyze_with_contract_frontend(tmp_path, fragment)


def test_indexer_rejects_non_json_frontend_metrics(tmp_path: Path) -> None:
    fragment = FrontendGraphFragment(adapter_metrics={"bad": {"not-json"}})

    with pytest.raises(
        FrontendContractError,
        match="contract-test: adapter_metrics must be JSON serializable",
    ):
        _analyze_with_contract_frontend(tmp_path, fragment)


def test_indexer_rejects_non_dict_frontend_metrics(tmp_path: Path) -> None:
    fragment = FrontendGraphFragment()
    fragment.adapter_metrics = ["not-a-dict"]  # type: ignore[assignment]

    with pytest.raises(
        FrontendContractError,
        match="contract-test: adapter_metrics must be a dict",
    ):
        _analyze_with_contract_frontend(tmp_path, fragment)


@pytest.mark.parametrize("bad_timing", ["fast", True])
def test_indexer_rejects_non_numeric_frontend_phase_timings(
    tmp_path: Path,
    bad_timing: object,
) -> None:
    fragment = FrontendGraphFragment(phase_timings={"extract": bad_timing})  # type: ignore[dict-item]

    with pytest.raises(
        FrontendContractError,
        match=r"contract-test: phase_timings\['extract'\] must be numeric seconds",
    ):
        _analyze_with_contract_frontend(tmp_path, fragment)


def test_indexer_rejects_non_dict_frontend_phase_timings(tmp_path: Path) -> None:
    fragment = FrontendGraphFragment()
    fragment.phase_timings = ["not-a-dict"]  # type: ignore[assignment]

    with pytest.raises(
        FrontendContractError,
        match="contract-test: phase_timings must be a dict",
    ):
        _analyze_with_contract_frontend(tmp_path, fragment)


def test_python_semantic_frontend_converts_graph_to_facts(tmp_path: Path) -> None:
    metadata = IndexMetadata(
        index_version="test",
        repo_root=str(tmp_path),
        source_roots=["src"],
    )
    node = Node(
        id="fn:pkg.a",
        kind="function",
        name="a",
        path="src/pkg.py",
        start_line=1,
    )
    edge = Edge(
        source="fn:pkg.a",
        target="fn:pkg.b",
        kind="calls",
        evidence=[Evidence(kind="ast_call", path="src/pkg.py", start_line=2)],
    )

    result = PythonSemanticFrontend().analyze_graph(metadata, [node], [edge], [])

    assert result.status == "available"
    assert result.frontend.name == "python-v1-compat-shim"
    assert [fact.fact_kind for fact in result.facts] == ["node", "edge"]
    assert result.metrics == {"node_facts": 1, "edge_facts": 1, "diagnostics": 0}


def test_indexer_uses_frontend_accepts_for_file_grouping(
    tmp_path: Path,
    monkeypatch,
) -> None:
    keep = _file_record(tmp_path, "src/keep.py", module="keep")
    skip = _file_record(tmp_path, "src/skip.py", module="skip")
    frontend = PythonSemanticFrontend()
    monkeypatch.setattr(frontend, "accepts", lambda file: file.path == keep.path)

    assert ArcGraphIndexer._files_for_frontend([keep, skip], frontend) == [keep]


def test_indexer_build_plan_uses_registration_order_without_discovery(
    tmp_path: Path, monkeypatch
) -> None:
    frontend = PythonSemanticFrontend()
    monkeypatch.setattr(
        frontend,
        "detect",
        lambda request: (_ for _ in ()).throw(AssertionError("detect called")),
    )
    indexer = ArcGraphIndexer(
        tmp_path,
        tmp_path / "output" / "arcgraph",
        [SourceRoot("src")],
        frontend_registry=FrontendRegistry([frontend]),
    )

    assert indexer._build_plan() == [frontend]


def test_frontend_registry_build_plan_detects_typescript(tmp_path: Path) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "App.tsx").write_text(
        "export function App() { return <main />; }\n",
        encoding="utf-8",
    )
    frontend = TypeScriptSemanticFrontend(repo_root=tmp_path)
    registry = FrontendRegistry([frontend])
    request = FrontendAnalyzeRequest(
        repo_root=str(tmp_path),
        source_roots=["src"],
        index_version="test",
    )

    assert registry.build_plan(request) == [frontend]
    assert registry.analyze(request)[0].frontend.name == "typescript-static"


def test_typescript_frontend_accepts_declaration_files(tmp_path: Path) -> None:
    frontend = TypeScriptSemanticFrontend(repo_root=tmp_path)
    declaration = _file_record(tmp_path, "src/global.d.ts", module="global")
    source = _file_record(tmp_path, "src/app.ts", module="app")

    assert frontend.accepts(source)
    assert frontend.accepts(declaration)


def test_typescript_frontend_indexes_components_and_api_route_calls(
    tmp_path: Path,
) -> None:
    api_dir = tmp_path / "api"
    src_dir = tmp_path / "src"
    (src_dir / "components").mkdir(parents=True)
    (src_dir / "services").mkdir(parents=True)
    api_dir.mkdir()
    (api_dir / "routes.py").write_text(
        "\n".join(
            [
                "from fastapi import APIRouter",
                "router = APIRouter(prefix='/api/v1/arcgraph')",
                "",
                "@router.get('/status')",
                "def status() -> dict[str, str]:",
                "    return {'status': 'ok'}",
            ]
        ),
        encoding="utf-8",
    )
    (src_dir / "components" / "Button.tsx").write_text(
        "export function Button() { return <button />; }\n",
        encoding="utf-8",
    )
    (src_dir / "services" / "apiClient.ts").write_text(
        "export const apiClient = { get: async (path: string) => path };\n",
        encoding="utf-8",
    )
    (src_dir / "services" / "arcgraph.ts").write_text(
        "\n".join(
            [
                "import { apiClient } from './apiClient';",
                "",
                "export class ArcGraphService {",
                "  static async getStatus() {",
                "    return apiClient.get('/arcgraph/status');",
                "  }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    (src_dir / "App.tsx").write_text(
        "\n".join(
            [
                "import { Button } from './components/Button';",
                "import { ArcGraphService } from './services/arcgraph';",
                "",
                "export function App() {",
                "  ArcGraphService.getStatus();",
                "  return <Button />;",
                "}",
            ]
        ),
        encoding="utf-8",
    )

    ArcGraphIndexer(
        repo_root=tmp_path,
        output_dir=tmp_path / "arcgraph",
        source_roots=[SourceRoot("api"), SourceRoot("src")],
    ).build()
    engine = QueryEngine(tmp_path / "arcgraph")

    app = engine.symbol("App.App")
    button_callers = engine.callers("components.Button.Button")
    route_callers = engine.callers("route:GET:/api/v1/arcgraph/status")
    service_callers = engine.callers("services.arcgraph.ArcGraphService.getStatus")

    assert app["matches"][0]["id"] == "component:App.App"
    assert any(edge["kind"] == "renders" for edge in button_callers["edges"])
    assert "method:services.arcgraph.ArcGraphService.getStatus" in {
        node["id"] for node in route_callers["callers"]
    }
    assert "component:App.App" in {node["id"] for node in service_callers["callers"]}


def test_typescript_frontend_indexes_types_hooks_and_conservative_http_matching(
    tmp_path: Path,
) -> None:
    api_dir = tmp_path / "api"
    src_dir = tmp_path / "src"
    api_dir.mkdir()
    src_dir.mkdir()
    (api_dir / "routes.py").write_text(
        "\n".join(
            [
                "from fastapi import APIRouter",
                "router = APIRouter(prefix='/api/v1')",
                "",
                "@router.get('/users/status')",
                "def user_status() -> dict[str, str]:",
                "    return {'status': 'ok'}",
            ]
        ),
        encoding="utf-8",
    )
    (src_dir / "apiClient.ts").write_text(
        "export const apiClient = { get: async (path: string) => path };\n",
        encoding="utf-8",
    )
    (src_dir / "types.ts").write_text(
        "\n".join(
            [
                "export interface ButtonProps { label: string; }",
                "export interface FancyProps extends ButtonProps { tone: string; }",
                "export type ButtonKind = 'primary' | 'secondary';",
                "export enum ViewMode { List, Grid }",
                "export class BaseWidget {}",
                "export class Widget extends BaseWidget implements FancyProps {",
                "  label = '';",
                "  tone = '';",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    (src_dir / "Widget.tsx").write_text(
        "\n".join(
            [
                "import { useState } from 'react';",
                "import { Widget } from './types';",
                "",
                "export function WidgetView() {",
                "  useState(0);",
                "  return <Widget />;",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    (src_dir / "status.ts").write_text(
        "\n".join(
            [
                "import { apiClient } from './apiClient';",
                "",
                "export class StatusService {",
                "  static async getGenericStatus() {",
                "    return apiClient.get('/status');",
                "  }",
                "  static async getDynamicStatus(name: string) {",
                "    return apiClient.get(`/arcgraph/${name}`);",
                "  }",
                "}",
            ]
        ),
        encoding="utf-8",
    )

    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=tmp_path,
        output_dir=output_dir,
        source_roots=[SourceRoot("api"), SourceRoot("src")],
    ).build()
    store: GraphStoreReader = GraphStoreReader.from_current(output_dir)
    nodes = store.read_nodes()
    edges = store.read_edges()
    node_ids = {node.id for node in nodes}
    edge_triples = {(edge.source, edge.kind, edge.target) for edge in edges}

    assert "interface:types.ButtonProps" in node_ids
    assert "interface:types.FancyProps" in node_ids
    assert "type_alias:types.ButtonKind" in node_ids
    assert "enum:types.ViewMode" in node_ids
    assert (
        "interface:types.FancyProps",
        "extends",
        "interface:types.ButtonProps",
    ) in edge_triples
    assert (
        "class:types.Widget",
        "extends",
        "class:types.BaseWidget",
    ) in edge_triples
    assert (
        "class:types.Widget",
        "implements",
        "interface:types.FancyProps",
    ) in edge_triples
    assert (
        "component:Widget.WidgetView",
        "uses_hook",
        "extsym:react.useState",
    ) in edge_triples
    assert (
        "method:status.StatusService.getGenericStatus",
        "calls",
        "route:GET:/api/v1/users/status",
    ) not in edge_triples
    assert not any(
        source == "method:status.StatusService.getDynamicStatus"
        and kind == "calls"
        and target.startswith("route:")
        for source, kind, target in edge_triples
    )


def test_typescript_frontend_warns_when_node_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    app_path = source_dir / "App.tsx"
    app_path.write_text(
        "export function App() { return <main />; }\n", encoding="utf-8"
    )
    file_record = FileRecord(
        path="src/App.tsx",
        abs_path=str(app_path),
        source_root="src",
        module="App",
        file_hash="test",
        line_count=1,
    )
    monkeypatch.setattr(
        "arcgraph.pipeline.typescript_frontend.shutil.which", lambda _: None
    )

    fragment = TypeScriptGraphAnalyzer(tmp_path).analyze([file_record])

    assert fragment.nodes == []
    assert fragment.edges == []
    assert any(
        warning.kind == "typescript_frontend_unavailable"
        and "Node.js is required" in warning.message
        for warning in fragment.warnings
    )


def test_typescript_frontend_indexes_declaration_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: list[FileRecord] = []

    def fake_run_extractor(
        self,
        files: list[FileRecord],
        call_context_nodes: list[Node],
        *,
        similarity_profiles: list[dict] | None = None,
        defer_similarity_scoring: bool = False,
    ):
        captured.extend(files)
        return {
            "nodes": [],
            "edges": [],
            "warnings": [],
            "external_packages": [],
            "external_symbols": [],
        }

    monkeypatch.setattr(TypeScriptGraphAnalyzer, "_run_extractor", fake_run_extractor)
    declaration = _file_record(tmp_path, "src/global.d.ts", module="global")
    source = _file_record(tmp_path, "src/app.ts", module="app")

    fragment = TypeScriptGraphAnalyzer(tmp_path).analyze([declaration, source])

    assert [file.path for file in captured] == ["src/global.d.ts", "src/app.ts"]
    assert {node.id for node in fragment.nodes} == {"mod:global", "mod:app"}


def test_typescript_frontend_materializes_unresolved_reference_targets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run_extractor(
        self,
        files: list[FileRecord],
        call_context_nodes: list[Node],
        *,
        similarity_profiles: list[dict] | None = None,
        defer_similarity_scoring: bool = False,
    ):
        return {
            "nodes": [
                {
                    "id": "fn:app.load",
                    "kind": "function",
                    "name": "load",
                    "qualname": "app.load",
                    "path": "src/app.ts",
                }
            ],
            "edges": [
                {
                    "source": "fn:app.load",
                    "target": "unresolved:@missing/widget",
                    "kind": "imports",
                    "confidence": "unresolved",
                }
            ],
            "warnings": [],
            "external_packages": [],
            "external_symbols": [],
        }

    monkeypatch.setattr(TypeScriptGraphAnalyzer, "_run_extractor", fake_run_extractor)
    source = _file_record(tmp_path, "src/app.ts", module="app")

    fragment = TypeScriptGraphAnalyzer(tmp_path).analyze([source])

    unresolved = next(
        node for node in fragment.nodes if node.id == "unresolved:@missing/widget"
    )
    edge = next(edge for edge in fragment.edges if edge.target == unresolved.id)
    assert unresolved.properties["identity_profile"] == (
        "typescript_unresolved_reference_v1"
    )
    assert edge.resolution.status == "unresolved"


def test_typescript_frontend_reports_parse_warning_for_unreadable_file(
    tmp_path: Path,
) -> None:
    missing = _file_record(tmp_path, "src/missing.ts", module="missing")

    fragment = TypeScriptGraphAnalyzer(tmp_path).analyze([missing])

    assert any(
        warning.kind == "typescript_parse_error" and warning.path == "src/missing.ts"
        for warning in fragment.warnings
    )


def test_typescript_frontend_uses_configured_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, float] = {}

    class Result:
        returncode = 0
        stdout = (
            '{"nodes":[],"edges":[],"warnings":[],'
            '"external_packages":[],"external_symbols":[]}'
        )
        stderr = ""

    def fake_run(*args, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        return Result()

    monkeypatch.setattr(
        "arcgraph.pipeline.typescript_frontend.shutil.which", lambda _: "node"
    )
    monkeypatch.setattr(
        "arcgraph.pipeline.typescript_frontend.subprocess.run", fake_run
    )
    source = _file_record(tmp_path, "src/app.ts", module="app")

    TypeScriptGraphAnalyzer(tmp_path, timeout_seconds=7.5).analyze([source])

    assert captured["timeout"] == 7.5


def test_typescript_incremental_context_is_transferred_as_a_compressed_bundle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class Result:
        returncode = 0
        stdout = '{"nodes":[],"edges":[],"warnings":[]}'
        stderr = ""

    def fake_run(*_args, **kwargs):
        captured.update(json.loads(kwargs["input"]))
        return Result()

    monkeypatch.setattr(
        "arcgraph.pipeline.typescript_frontend.shutil.which", lambda _: "node"
    )
    monkeypatch.setattr(
        "arcgraph.pipeline.typescript_frontend.subprocess.run", fake_run
    )
    source = _file_record(tmp_path, "src/app.ts", module="app")
    context_module = Node(
        id="mod:support",
        kind="module",
        name="support",
        qualname="support",
        path="src/support.ts",
        properties={
            "frontend_name": "typescript-static",
            "line_count": 1,
            "source_root": "src",
            "is_package": False,
        },
    )

    TypeScriptGraphAnalyzer(tmp_path)._run_extractor(
        [source],
        [context_module],
        similarity_profiles=[],
        defer_similarity_scoring=True,
    )

    assert captured["contextEncoding"] == "zlib-base64-json-v1"
    assert "moduleContext" not in captured
    decoded = json.loads(
        zlib.decompress(base64.b64decode(str(captured["contextBundle"]))).decode(
            "utf-8"
        )
    )
    assert decoded["moduleContext"][0]["module"] == "support"


def test_typescript_extractor_emits_profile_rows_only_for_deferred_scoring(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for the TypeScript extractor.")
    source = tmp_path / "src" / "handlers.ts"
    source.parent.mkdir()
    source.write_text(
        "export function firstHandler(value: number): number {\n"
        "  const step = value + 1;\n"
        "  return step * 2 - Math.floor(step / 3);\n"
        "}\n\n"
        "export function secondHandler(value: number): number {\n"
        "  const step = value + 2;\n"
        "  return step * 3 - Math.floor(step / 4);\n"
        "}\n",
        encoding="utf-8",
    )
    script = Path(typescript_frontend.__file__).with_name("typescript_extractor.mjs")
    file_entry = {
        "path": "src/handlers.ts",
        "absPath": str(source),
        "module": "handlers",
        "lineCount": 9,
        "sourceRoot": "src",
    }

    def run_extractor(**overrides: object) -> dict[str, object]:
        request = {
            "repoRoot": str(tmp_path),
            "files": [file_entry],
            "similarityProfiles": [],
            **overrides,
        }
        result = subprocess.run(
            ["node", str(script)],
            input=json.dumps(request),
            text=True,
            capture_output=True,
            cwd=str(tmp_path),
            timeout=60,
            check=False,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    full = run_extractor(deferSimilarityScoring=False)
    assert "similarity_profiles" not in full
    assert any(
        isinstance(node.get("properties"), dict)
        and isinstance(node["properties"].get("similarity"), dict)
        for node in full["nodes"]
    )

    deferred = run_extractor(deferSimilarityScoring=True)
    assert len(deferred["similarity_profiles"]) == 2


def test_typescript_frontend_reports_timeout_with_explicit_provenance(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["node", "extractor.mjs"], 4.25)

    monkeypatch.setattr(TypeScriptGraphAnalyzer, "_run_extractor", timeout)
    source = _file_record(tmp_path, "src/app.ts", module="app")

    fragment = TypeScriptGraphAnalyzer(
        tmp_path,
        timeout_seconds=4.25,
    ).analyze([source])

    assert fragment.nodes == []
    assert fragment.edges == []
    assert fragment.warnings == [
        BuildWarning(
            kind="typescript_frontend_unavailable",
            message="TypeScript extractor timed out after 4.25s.",
            frontend_name="typescript-static",
        )
    ]
