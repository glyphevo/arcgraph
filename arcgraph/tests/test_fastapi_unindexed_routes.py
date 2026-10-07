"""A FastAPI route decorator the adapter passes over is reported, not dropped.

Only a module-level function becomes a route, so mealie's class-based
controllers, whose methods carry @router.get and the like, gave 252 of its 277
handlers no route and no warning.
"""

from __future__ import annotations

from pathlib import Path

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.reindexer import ArcGraphReindexer

CONTROLLERS = """from fastapi import APIRouter

router = APIRouter(prefix="/items")


def controller(router):
    def wrap(cls):
        return cls

    return wrap


@router.get("/health")
def health():
    return "ok"


@controller(router)
class ItemController:
    @router.get("")
    def get_all(self):
        return []

    @router.post("")
    def create(self):
        return {}

    def helper(self):
        return None


def register():
    @router.delete("/{item_id}")
    def delete(item_id: int):
        return None

    return delete
"""

PLAIN = """from fastapi import APIRouter

router = APIRouter()


@router.get("/ping")
def ping():
    return "pong"
"""


def _build(tmp_path: Path, files: dict[str, str]) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    for name, text in files.items():
        path = repo / "src" / "app" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    output = tmp_path / "out"
    ArcGraphIndexer(repo, output, [SourceRoot("src")]).build()
    return repo, output


def _skipped(output: Path) -> list:
    return [
        warning
        for warning in GraphStoreReader.from_current(output).read_warnings()
        if warning.kind == "adapter_fastapi_routes_not_indexed"
    ]


def test_route_decorators_on_methods_and_nested_functions_are_reported(
    tmp_path: Path,
) -> None:
    _, output = _build(
        tmp_path,
        {"__init__.py": "", "controllers.py": CONTROLLERS, "plain.py": PLAIN},
    )
    warnings = _skipped(output)
    assert [warning.path for warning in warnings] == ["src/app/controllers.py"]
    message = warnings[0].message
    assert message.startswith("3 FastAPI route decorator(s)")
    for name in ("ItemController.get_all", "ItemController.create", "register.delete"):
        assert name in message
    assert "helper" not in message
    store = GraphStoreReader.from_current(output)
    routes = {node.id for node in store.read_nodes() if node.kind == "route"}
    # The module-level functions are routes as before.
    assert any(route.endswith("/items/health") for route in routes), routes
    assert any(route.endswith("/ping") for route in routes), routes
    assert store.metadata["capabilities"]["framework_adapters"] == "partial"


def test_a_project_of_module_level_routes_has_no_such_warning(tmp_path: Path) -> None:
    _, output = _build(tmp_path, {"__init__.py": "", "plain.py": PLAIN})
    assert _skipped(output) == []
    store = GraphStoreReader.from_current(output)
    assert store.metadata["capabilities"]["framework_adapters"] != "partial"


def test_the_warning_follows_its_file_through_a_reindex(tmp_path: Path) -> None:
    repo, output = _build(tmp_path, {"__init__.py": "", "controllers.py": CONTROLLERS})
    assert len(_skipped(output)) == 1
    (repo / "src" / "app" / "controllers.py").write_text(PLAIN, encoding="utf-8")
    ArcGraphReindexer(repo, output, [SourceRoot("src")]).reindex_changed()
    assert _skipped(output) == []
