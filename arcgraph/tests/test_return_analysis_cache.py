"""Decorator evidence is shared only within a complete binding generation."""

from collections import Counter

from arcgraph.analyzers.types import TypeRefAnalyzer, _TypeContext
from arcgraph.pipeline import python_frontend
from arcgraph.tests.test_generator_and_dynamic_exports import _targets


def test_decorator_proof_computed_once_per_function_per_build(tmp_path, monkeypatch):
    analyzer = TypeRefAnalyzer()
    monkeypatch.setattr(python_frontend, "TypeRefAnalyzer", lambda: analyzer)
    counts = Counter()
    original = _TypeContext._annotation_decorators_preserve_return

    def counted(self, node):
        if node.properties.get("decorators"):
            counts[node.qualname] += 1
        return original(self, node)

    monkeypatch.setattr(_TypeContext, "_annotation_decorators_preserve_return", counted)
    provider = (
        "from functools import cache\n"
        "class Box:\n    def send(self, value): return value\n"
        "class Other:\n    def send(self, value): return value\n"
        "@cache\ndef made() -> Box: return Box()\n"
    )
    modules = {
        "provider": provider,
        "conftest": "import pytest\n@pytest.fixture\ndef item() -> int: return 1\n",
    }
    modules.update(
        {f"consumer{i}": "from pkg.provider import made\n" for i in range(5)}
    )
    modules["user"] = (
        "from pkg.provider import made\ndef use(): return made().send(None)\n"
    )
    assert "method:pkg.provider.Box.send" in _targets(
        tmp_path / "first", modules, v2=True
    )
    assert counts == {"pkg.provider.made": 1, "pkg.conftest.item": 1}, counts
    modules["provider"] = provider.replace(
        "from functools import cache", "def cache(fn): return lambda: 42"
    )
    assert "method:pkg.provider.Box.send" not in _targets(
        tmp_path / "second", modules, v2=True
    )
    assert counts == {"pkg.provider.made": 2, "pkg.conftest.item": 2}, counts


def test_return_maps_skip_functions_without_return_evidence(tmp_path, monkeypatch):
    counts = Counter()
    original = _TypeContext.callable_return_type

    def counted(self, node):
        counts[node.qualname] += 1
        return original(self, node)

    monkeypatch.setattr(_TypeContext, "callable_return_type", counted)
    _targets(
        tmp_path,
        {
            "provider": "def plain(): return 42\n",
            "other": "value = 1\n",
            "user": "from pkg.provider import plain\ndef use(): return plain()\n",
        },
        v2=True,
    )
    # Its own return record is examined once; every consumer must skip it when
    # rebuilding the project-wide annotated factory map.
    assert counts["pkg.provider.plain"] == 1, counts
