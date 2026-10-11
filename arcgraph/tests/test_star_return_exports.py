"""A return annotation follows runtime export identity, never a short name."""

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer


def graph(tmp_path, exports, *, annotation="Directories"):
    package = tmp_path / "pkg"
    package.mkdir()
    sources = {
        "__init__.py": "",
        "settings/__init__.py": "from .directories import *\nfrom .other import *\n",
        "settings/directories.py": 'from pathlib import Path\nclass Directories:\n    TEMP_DIR: Path = Path("a")\n'
        + exports,
        "settings/other.py": "class Unrelated: pass\n",
        "unrelated.py": 'class Directories:\n    TEMP_DIR: bytes = b"wrong"\n',
        "provider.py": "from pkg.settings import Directories\ndef get_dirs() -> "
        + annotation
        + ":\n    return Directories()\n",
        "user.py": "from pkg.provider import get_dirs\ndef use():\n    get_dirs().TEMP_DIR.mkdir()\n",
    }
    for path, content in sources.items():
        file = package / path
        file.parent.mkdir(exist_ok=True)
        file.write_text(content, encoding="utf-8")
    ArcGraphIndexer(tmp_path, tmp_path / "out", [SourceRoot(".")]).build()
    store = GraphStoreReader.from_current(tmp_path / "out")
    return {
        (e.target, e.confidence)
        for e in store.read_edges()
        if e.source == "fn:pkg.user.use"
    }


@pytest.mark.parametrize("exports", ["", '__all__=["Directories"]\n'])
def test_star_reexports_preserve_the_defining_return_class(tmp_path, exports):
    targets = graph(tmp_path, exports)
    assert ("extsym:pathlib.Path.mkdir", "inferred") in targets
    assert not any(t == "class:pkg.unrelated.Directories" for t, _ in targets)


@pytest.mark.parametrize(
    "exports",
    [
        "__all__=[]\n",
        "Directories=object()\n",
        '__all__=["Directories"]\ndef clear():\n    global __all__\n    __all__=[]\nclear()\n',
    ],
)
def test_star_export_masks_and_rebindings_do_not_invent_a_class(tmp_path, exports):
    targets = graph(tmp_path, exports)
    assert ("extsym:pathlib.Path.mkdir", "inferred") not in targets


def test_imported_return_class_preserves_generic_field_elements(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    for name, content in {
        "__init__": "",
        "models": "class Item:\n    properties: dict[str, object]\nclass Graph:\n    nodes: list[Item]\n",
        "provider": "from pkg.models import Graph\ndef load() -> Graph:\n    return Graph()\n",
        "user": "from pkg.provider import load\ndef use():\n    return [n.properties.get('a') for n in load().nodes]\n",
    }.items():
        (package / (name + ".py")).write_text(content, encoding="utf-8")
    ArcGraphIndexer(tmp_path, tmp_path / "out", [SourceRoot(".")]).build()
    targets = {
        (e.target, e.confidence)
        for e in GraphStoreReader.from_current(tmp_path / "out").read_edges()
        if e.source == "fn:pkg.user.use"
    }
    assert any(t == "extsym:builtins.dict.get" and c != "heuristic" for t, c in targets)
