"""Class route registration and served prefix chains without executing a project."""

from pathlib import Path

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer


def graph(tmp_path, files):
    root = tmp_path / "src"
    for name, text in files.items():
        path = root / "pkg" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    out = tmp_path / "out"
    ArcGraphIndexer(tmp_path, out, [SourceRoot("src")]).build()
    store = GraphStoreReader.from_current(out)
    return list(store.read_nodes()), list(store.read_edges())


def test_class_routes_use_method_identity_and_full_mount_chain(tmp_path: Path):
    nodes, edges = graph(
        tmp_path,
        {
            "api.py": """from fastapi import APIRouter
base = '/leaf'
router = APIRouter(prefix=f'{base}/v1')
class A:
    @router.get('/item')
    def get(self, value: int): return value
class B:
    @router.post('/item')
    def get(self): return None
""",
            "app.py": """from fastapi import FastAPI, APIRouter
from . import api
router = APIRouter(prefix='/outer')
router.include_router(api.router, prefix='/middle')
app = FastAPI()
app.include_router(router, prefix='/root')
""",
        },
    )
    routes = {n.id: n for n in nodes if n.kind == "route"}
    assert "route:GET:/root/outer/middle/leaf/v1/item" in routes
    route = routes["route:GET:/root/outer/middle/leaf/v1/item"]
    assert route.properties["handler"] == "method:pkg.api.A.get"
    assert route.properties["parameters"] == ["value"]
    assert route.properties["self_injection"] == "not_injected"
    assert (
        "route:POST:/root/outer/middle/leaf/v1/item",
        "method:pkg.api.B.get",
        "invokes",
    ) in {(e.source, e.target, e.kind) for e in edges}
    assert len(routes) == 4
    assert (
        routes["route:GET:/leaf/v1/item"].properties["path_scope"]
        == "local_registration"
    )
    assert route.properties["path_scope"] == "mounted_registration"


def test_unknown_prefix_keeps_local_path_and_reports_uncertainty(tmp_path: Path):
    nodes, _ = graph(
        tmp_path,
        {"api.py": """from fastapi import APIRouter
router = APIRouter(prefix=runtime())
class A:
    @router.get('/item')
    def get(self): pass
"""},
    )
    route = next((n for n in nodes if n.kind == "route"), None)
    assert route is not None, "The class method must register a local route"
    assert route.properties["path"] == "/item"
    assert route.properties["path_status"] == "unknown_prefix"


def test_router_subclass_forwards_prefix_without_project_special_case(tmp_path: Path):
    nodes, _ = graph(
        tmp_path,
        {"api.py": """from fastapi import APIRouter
class SecureRouter(APIRouter):
    def __init__(self, prefix='', **kwargs):
        super().__init__(prefix=prefix, **kwargs)
router = SecureRouter(prefix='/secure')
class A:
    @router.get('/item')
    def get(self): pass
"""},
    )
    assert any(n.id == "route:GET:/secure/item" for n in nodes)


def test_method_receivers_must_be_unshadowed_module_routers(tmp_path: Path):
    nodes, _ = graph(
        tmp_path,
        {"api.py": """from fastapi import APIRouter
router = APIRouter()
class A:
    router = unrelated()
    @router.get('/shadow')
    def a(self): pass
class B:
    @self.router.get('/self')
    def b(self): pass
    @missing.get('/missing')
    def c(self): pass
def local():
    router = APIRouter()
    @router.get('/local')
    def f(): pass
"""},
    )
    assert not [n for n in nodes if n.kind == "route"]


def test_mutated_prefix_does_not_claim_served_path(tmp_path: Path):
    nodes, _ = graph(
        tmp_path,
        {"api.py": """from fastapi import APIRouter
router = APIRouter(prefix='/before')
router.prefix = runtime()
class A:
    @router.get('/item')
    def get(self): pass
"""},
    )
    route = next((n for n in nodes if n.kind == "route"), None)
    assert route is not None, "Unknown prefixes must retain the local registration"
    assert route.properties["path_status"] == "unknown_prefix"
    assert route.properties["path"] == "/item"


def test_uncalled_mount_function_does_not_invent_served_path(tmp_path: Path):
    nodes, _ = graph(
        tmp_path,
        {"api.py": """from fastapi import APIRouter, FastAPI
router = APIRouter(prefix='/leaf')
@router.get('/item')
def get(): pass
app = FastAPI()
def unused():
    app.include_router(router, prefix='/not_mounted')
"""},
    )
    assert {n.id for n in nodes if n.kind == "route"} == {"route:GET:/leaf/item"}


def test_rebound_factory_is_not_a_fastapi_router(tmp_path: Path):
    nodes, _ = graph(
        tmp_path,
        {"api.py": """from fastapi import APIRouter
APIRouter = fake
router = APIRouter()
@router.get('/item')
def get(): pass
"""},
    )
    assert not [n for n in nodes if n.kind == "route"]


def test_imported_module_router_is_a_proven_binding(tmp_path: Path):
    nodes, _ = graph(
        tmp_path,
        {
            "routing.py": """from fastapi import APIRouter
router = APIRouter(prefix='/api')
""",
            "controller.py": """from . import routing
class A:
    @routing.router.get('/item')
    def get(self): pass
""",
            "app.py": """from fastapi import FastAPI
from .routing import router
app = FastAPI()
app.include_router(router, prefix='/v1')
""",
        },
    )
    routes = {n.id: n for n in nodes if n.kind == "route"}
    assert "route:GET:/v1/api/item" in routes
    assert (
        routes["route:GET:/v1/api/item"].properties["handler"]
        == "method:pkg.controller.A.get"
    )
    assert "route:GET:/api/item" in routes


def test_unknown_mount_prefix_is_reported_on_retained_local_route(tmp_path: Path):
    nodes, _ = graph(
        tmp_path,
        {"api.py": """from fastapi import APIRouter, FastAPI
router = APIRouter(prefix='/leaf')
@router.get('/item')
def get(): pass
app = FastAPI()
app.include_router(router, prefix=runtime())
"""},
    )
    routes = [n for n in nodes if n.kind == "route"]
    assert len(routes) == 1
    assert routes[0].properties["path"] == "/leaf/item"
    assert "path_status" in routes[0].properties
    assert routes[0].properties["path_status"] == "unknown_prefix"


def test_missing_dependency_does_not_guess_same_named_provider(tmp_path: Path):
    _, edges = graph(
        tmp_path,
        {
            "api.py": """from fastapi import APIRouter, Depends
router = APIRouter()
@router.get('/item')
def get(service=Depends(provide)): pass
""",
            "other.py": """def provide(): return None
""",
        },
    )
    assert not any(
        e.kind == "injects" and e.target == "fn:pkg.other.provide" for e in edges
    )
