"""Qualified functools caches need module identity, not a matching suffix."""

import pytest

from arcgraph.tests.test_generator_and_dynamic_exports import _targets

BOXES = (
    "class Box:\n    def send(self, value): return value\n"
    "class Other:\n    def send(self, value): return value\n"
)


@pytest.mark.parametrize("assigned", [False, True], ids=["chained", "assigned"])
@pytest.mark.parametrize(
    "prefix,decorator",
    [
        ("import functools\n", "functools.cache"),
        ("import functools\n", "functools.lru_cache"),
        ("import functools\n", "functools.lru_cache()"),
        ("import functools\n", "functools.lru_cache(128)"),
        ("import functools\n", "functools.lru_cache(maxsize=None, typed=True)"),
        ("import functools as memo\n", "memo.cache"),
        ("import functools as memo\n", "memo.lru_cache(2)"),
    ],
)
def test_qualified_stdlib_cache_preserves_return(tmp_path, prefix, decorator, assigned):
    source = prefix + BOXES + f"@{decorator}\ndef made() -> Box: return Box()\n"
    source += "def use():\n" + (
        "    result = made()\n    return result.send(None)\n"
        if assigned
        else "    return made().send(None)\n"
    )
    assert "method:pkg.user.Box.send" in _targets(tmp_path, {"user": source}, v2=True)


@pytest.mark.parametrize("alias", ["functools", "memo"])
def test_qualified_cached_property_preserves_value(tmp_path, alias):
    source = (
        f"import functools as {alias}\n"
        + BOXES
        + f"class Holder:\n    @{alias}.cached_property\n"
        "    def value(self) -> Box: return Box()\n"
        "def use(): return Holder().value.send(None)\n"
    )
    assert "method:pkg.user.Box.send" in _targets(tmp_path, {"user": source}, v2=True)


@pytest.mark.parametrize(
    "prefix,decorator",
    [
        ("import functools\nfunctools = object()\n", "functools.cache"),
        (
            "import functools\ndef patch():\n    global functools\n    functools = replacement\npatch()\n",
            "functools.cache",
        ),
        ("from pkg.fake import functools\n", "functools.cache"),
        ("if flag:\n    import functools\n", "functools.cache"),
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import functools\n",
            "functools.cache",
        ),
        ("import functools\nfrom pkg.fake import *\n", "functools.cache"),
        (
            "import functools\nfunctools.cache = lambda fn: lambda: 42\n",
            "functools.cache",
        ),
        ("import functools\ndel functools.cache\n", "functools.cache"),
        ("import functools\npatch(functools)\n", "functools.cache"),
        (
            "import functools\nsetattr(functools, 'cache', replacement)\n",
            "functools.cache",
        ),
        ("import functools\nalias = functools\n", "functools.cache"),
        ("import functools.foo\n", "functools.cache"),
        ("import functools\n", "functools.lru_cache(another)"),
        ("import functools\n", "functools.lru_cache(misspelled=128)"),
        ("import functools\n", "functools.lru_cache(1, 2)"),
        ("import functools\n", "functools.lru_cache(**options)"),
        ("import functools\n", "functools.cache(another)"),
        ("import functools\n", "functools.cached_property(another)"),
        ("import functools\n", "functools.cached_property"),
        ("import pkg.fake as functools\n", "functools.cache"),
    ],
)
def test_qualified_cache_unknown_identity_or_configuration_is_conservative(
    tmp_path, prefix, decorator
):
    source = prefix + BOXES + f"@{decorator}\ndef made() -> Box: return Box()\n"
    source += "def use(): return made().send(None)\n"
    assert "method:pkg.user.Box.send" not in _targets(
        tmp_path,
        {"user": source, "fake": "functools = object()\n"},
        v2=True,
    )


def test_qualified_cache_global_rebinding_is_unknown(tmp_path):
    source = (
        "import functools\n"
        + BOXES
        + "def outer():\n    global functools\n    functools = replacement\n"
        "    @functools.cache\n    def made() -> Box: return Box()\n"
        "    return made\n"
        "def use(): return outer()().send(None)\n"
    )
    assert "method:pkg.user.Box.send" not in _targets(
        tmp_path, {"user": source}, v2=True
    )


@pytest.mark.parametrize("member", ["cache", "lru_cache", "cached_property"])
def test_qualified_cache_member_writes_invalidate_identity(tmp_path, member):
    source = (
        "import functools as memo\n"
        f"memo.{member} = lambda fn: lambda *a: 42\n"
        + BOXES
        + f"class Holder:\n    @memo.{member}\n"
        "    def value(self) -> Box: return Box()\n"
        + (
            "def use(): return Holder().value.send(None)\n"
            if member == "cached_property"
            else "def use(): return Holder().value().send(None)\n"
        )
    )
    assert "method:pkg.user.Box.send" not in _targets(
        tmp_path, {"user": source}, v2=True
    )


def test_qualified_cache_cannot_claim_an_indexed_functools_module(tmp_path):
    from arcgraph.core.graph_store import GraphStoreReader
    from arcgraph.pipeline.indexer import ArcGraphIndexer
    from arcgraph.core.scanner import SourceRoot

    root = tmp_path / "repo"
    root.mkdir()
    (root / "functools.py").write_text(
        "def cache(fn): return lambda: 42\n", encoding="utf-8"
    )
    (root / "user.py").write_text(
        "import functools\n"
        + BOXES
        + "@functools.cache\ndef made() -> Box: return Box()\n"
        "def use(): return made().send(None)\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    ArcGraphIndexer(
        repo_root=root, output_dir=out, source_roots=[SourceRoot(".")]
    ).build()
    assert not any(
        e.source == "fn:user.use" and e.target == "method:user.Box.send"
        for e in GraphStoreReader.from_current(out).read_edges()
    )


def test_qualified_cache_keeps_provider_import_resolution_separate(tmp_path):
    assert "method:pkg.models.Box.send" in _targets(
        tmp_path,
        {
            "a_consumer": "import pkg.provider\n",
            "models": BOXES,
            "provider": "import functools\nfrom pkg.models import Box as Alias\n"
            "@functools.cache\ndef made() -> Alias: return Alias()\n",
            "user": "from pkg.provider import made\ndef use(): return made().send(None)\n",
        },
        v2=True,
    )


@pytest.mark.parametrize("kind", ["method", "nested"])
def test_qualified_cache_method_and_nested_definition(tmp_path, kind):
    source = "import functools as memo\n" + BOXES
    if kind == "method":
        source += (
            "class Holder:\n    @memo.lru_cache(2)\n"
            "    def value(self) -> Box: return Box()\n"
            "def use(): return Holder().value().send(None)\n"
        )
    else:
        source += (
            "def use():\n    @memo.cache\n    def made() -> Box: return Box()\n"
            "    return made().send(None)\n"
        )
    assert "method:pkg.user.Box.send" in _targets(tmp_path, {"user": source}, v2=True)


def test_qualified_cache_unrelated_member_write_preserves_identity(tmp_path):
    source = (
        "import functools as memo\nmemo.lru_cache = replacement\n"
        + BOXES
        + "@memo.cache\ndef made() -> Box: return Box()\n"
        "def use(): return made().send(None)\n"
    )
    assert "method:pkg.user.Box.send" in _targets(tmp_path, {"user": source}, v2=True)


def test_cache_write_collection_does_not_change_builtin_descriptor_proof(tmp_path):
    source = (
        "import builtins\nbuiltins.cache = replacement\n"
        "class Holder:\n    @builtins.staticmethod\n    def made(): yield 1\n"
        "def use(): return Holder.made().send(None)\n"
    )
    assert "extsym:types.GeneratorType.send" in _targets(
        tmp_path, {"user": source}, v2=True
    )
