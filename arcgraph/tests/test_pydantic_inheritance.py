"""A class is a Pydantic model when it derives from one through project bases.

The adapter took only a class whose own base was BaseModel or BaseSettings for
a model, so mealie, whose schemas derive from its MealieModel(BaseModel), had
282 of its 305 models indexed as plain classes, and nothing said so.
"""

from __future__ import annotations

from pathlib import Path

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.reindexer import ArcGraphReindexer

FILES = {
    "pkg/__init__.py": "from pkg.base import AppModel\n",
    "pkg/base.py": (
        "from pydantic import BaseModel\n\n\nclass AppModel(BaseModel):\n    pass\n"
    ),
    "pkg/schemas.py": (
        "from typing import Generic, TypeVar\n\n"
        "from pkg import AppModel\n\n"
        'T = TypeVar("T")\n\n\n'
        "class User(AppModel):\n    name: str\n\n\n"
        "class Admin(User):\n    level: int\n\n\n"
        "class Page(AppModel, Generic[T]):\n    items: list\n\n\n"
        "class Plain:\n    pass\n\n\n"
        "class Loop(Plain):\n    pass\n"
    ),
    "pkg/settings.py": (
        "from pydantic_settings import BaseSettings\n\n"
        "from pkg.base import AppModel\n\n\n"
        "class BaseConfig(BaseSettings):\n    pass\n\n\n"
        "class AppSettings(BaseConfig, AppModel):\n    port: int = 8000\n"
    ),
    "pkg/cycle.py": "class First(Second):\n    pass\n\n\nclass Second(First):\n    pass\n",
}


def _write(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = root / "src" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _models(output: Path) -> dict[str, str]:
    return {
        node.qualname: node.properties.get("model_kind")
        for node in GraphStoreReader.from_current(output).read_nodes()
        if node.kind == "pydantic_model"
    }


def test_models_through_project_bases(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo, FILES)
    output = tmp_path / "out"
    ArcGraphIndexer(repo, output, [SourceRoot("src")]).build()
    assert _models(output) == {
        "pkg.base.AppModel": "model",
        # Through AppModel, re-exported by the package.
        "pkg.schemas.User": "model",
        "pkg.schemas.Admin": "model",
        "pkg.schemas.Page": "model",
        "pkg.settings.BaseConfig": "settings",
        # Settings wins over model.
        "pkg.settings.AppSettings": "settings",
    }
    nodes = {
        node.qualname: node
        for node in GraphStoreReader.from_current(output).read_nodes()
        if node.kind == "class"
    }
    # A base in another file marks the class, so a change there reaches it.
    assert nodes["pkg.schemas.User"].properties.get("inherited_type_input")
    assert nodes["pkg.settings.AppSettings"].properties.get("inherited_type_input")
    assert not nodes["pkg.schemas.Admin"].properties.get("inherited_type_input")
    assert not nodes["pkg.base.AppModel"].properties.get("inherited_type_input")


def test_a_change_to_a_project_base_reaches_its_subclasses(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo, FILES)
    roots = [SourceRoot("src")]
    incremental = tmp_path / "incremental"
    ArcGraphIndexer(repo, incremental, roots).build()

    # AppModel stops being a model; only base.py changes.
    _write(repo, {"pkg/base.py": "class AppModel:\n    pass\n"})
    ArcGraphReindexer(repo, incremental, roots).reindex_changed()
    full = tmp_path / "full"
    ArcGraphIndexer(repo, full, roots).build()

    assert (
        _models(incremental)
        == _models(full)
        == {
            "pkg.settings.BaseConfig": "settings",
            "pkg.settings.AppSettings": "settings",
        }
    )
