"""FastAPI dependency results are binding evidence, not callable names."""

from pathlib import Path

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer


def build(tmp_path, dependency, use, extra=""):
    package = tmp_path / "src/pkg"
    package.mkdir(parents=True)
    (package / "deps.py").write_text(dependency, encoding="utf-8")
    (package / "user.py").write_text(use, encoding="utf-8")
    (package / "other.py").write_text(extra, encoding="utf-8")
    out = tmp_path / "out"
    ArcGraphIndexer(
        tmp_path, out, [SourceRoot("src")], enable_v2_call_resolution=True
    ).build()
    store = GraphStoreReader.from_current(out)
    return list(store.read_nodes()), list(store.read_edges())


CLASS = """class Session:
    def execute(self): pass
"""


@pytest.mark.parametrize(
    "kind,annotation",
    [
        ("function", "Session"),
        ("async", "Session"),
        ("generator", "Generator[Session, None, None]"),
        ("generator", "Iterator[Session]"),
        ("async_generator", "AsyncGenerator[Session, None]"),
        ("async_generator", "AsyncIterator[Session]"),
    ],
)
def test_depends_propagates_result_in_defining_scope(tmp_path: Path, kind, annotation):
    prefix = "async " if kind in {"async", "async_generator"} else ""
    body = "yield Session()" if "generator" in kind else "return Session()"
    deps = (
        "from typing import Generator, Iterator, AsyncGenerator, AsyncIterator\n"
        + CLASS
        + f"{prefix}def provide() -> {annotation}:\n    {body}\n"
    )
    nodes, edges = build(
        tmp_path,
        deps,
        """from fastapi import Depends
from .deps import provide
def use(session=Depends(provide)):
    session.execute()
""",
    )
    fn = next(n for n in nodes if n.id == "fn:pkg.user.use")
    ref = next(
        r for r in fn.properties.get("type_refs", []) if r.get("name") == "session"
    )
    assert ref["type_id"] == "class:pkg.deps.Session"
    assert ref["strategy"] == "fastapi_dependency_result"
    assert any(
        e.source == fn.id and e.target == "method:pkg.deps.Session.execute"
        for e in edges
    )


def test_depends_class_field_and_nested_dependency(tmp_path: Path):
    nodes, edges = build(
        tmp_path,
        "from fastapi import Depends\n"
        + CLASS
        + """def inner() -> Session: return Session()
def outer(session=Depends(inner)) -> Session: return session
""",
        """from fastapi import Depends
from .deps import Session, outer
class Controller:
    session = Depends(outer)
    def use(self): self.session.execute()
def use(session=Depends(Session)):
    session.execute()
""",
    )
    targets = {(e.source, e.target) for e in edges}
    assert (
        "method:pkg.user.Controller.use",
        "method:pkg.deps.Session.execute",
    ) in targets
    assert ("fn:pkg.user.use", "method:pkg.deps.Session.execute") in targets
    outer = next(n for n in nodes if n.id == "fn:pkg.deps.outer")
    assert any(
        r.get("name") == "session" and r.get("type_id") == "class:pkg.deps.Session"
        for r in outer.properties.get("type_refs", [])
    )


def test_annotation_wins_over_depends_including_annotated(tmp_path: Path):
    _, edges = build(
        tmp_path,
        CLASS + "def provide() -> Session: return Session()\n",
        """from fastapi import Depends
from typing import Annotated
from .deps import provide
class Other:
    def execute(self): pass
def use(a: Other=Depends(provide), b: Annotated[Other, Depends(provide)]=None):
    a.execute()
    b.execute()
""",
    )
    targets = {e.target for e in edges if e.source == "fn:pkg.user.use"}
    assert "method:pkg.user.Other.execute" in targets
    assert "method:pkg.deps.Session.execute" not in targets


@pytest.mark.parametrize(
    "prefix,dependency",
    [
        ("", "def provide(): return Session()"),
        ("Depends = fake\n", "def provide() -> Session: return Session()"),
        ("", "def provide() -> Generator[Session, None, None]: return wrong"),
        ("", "@wrap\ndef provide() -> Session: return Session()"),
    ],
)
def test_unknown_or_shadowed_dependency_is_not_type_evidence(
    tmp_path: Path, prefix, dependency
):
    nodes, _ = build(
        tmp_path,
        "from typing import Generator\n" + CLASS + dependency + "\n",
        "from fastapi import Depends\nfrom .deps import provide\n"
        + prefix
        + "def use(session=Depends(provide)):\n    session.execute()\n",
    )
    fn = next(n for n in nodes if n.id == "fn:pkg.user.use")
    assert not any(
        r.get("name") == "session" and r.get("type_id") == "class:pkg.deps.Session"
        for r in fn.properties.get("type_refs", [])
    )


def test_dependency_parameter_reassignment_invalidates_injected_type(tmp_path: Path):
    nodes, edges = build(
        tmp_path,
        CLASS + "def provide() -> Session: return Session()\n",
        """from fastapi import Depends
from .deps import provide
def use(session=Depends(provide)):
    session = object()
    session.execute()
""",
    )
    fn = next(n for n in nodes if n.id == "fn:pkg.user.use")
    assert not any(
        e.source == fn.id and e.target == "method:pkg.deps.Session.execute"
        for e in edges
    )


def test_rebound_class_field_has_no_injected_type(tmp_path: Path):
    nodes, edges = build(
        tmp_path,
        CLASS + "def provide() -> Session: return Session()\n",
        """from fastapi import Depends
from .deps import provide
class Controller:
    session = Depends(provide)
    session = unrelated
    def use(self): self.session.execute()
""",
    )
    assert not any(
        e.source == "method:pkg.user.Controller.use"
        and e.target == "method:pkg.deps.Session.execute"
        for e in edges
    )


@pytest.mark.parametrize(
    "expression", ["inject(provide)", "inject(dependency=provide)"]
)
def test_depends_alias_and_keyword_keep_callable_identity(tmp_path: Path, expression):
    _, edges = build(
        tmp_path,
        CLASS + "def provide() -> Session: return Session()\n",
        f"""from fastapi import Depends as inject
from .deps import provide
def use(session={expression}):
    session.execute()
""",
    )
    assert any(
        e.source == "fn:pkg.user.use" and e.target == "method:pkg.deps.Session.execute"
        for e in edges
    )
