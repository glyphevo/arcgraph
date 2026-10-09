"""The value returned by a callable must survive decoration and generator shape."""

import pytest

from arcgraph.core.graph_store import GraphStoreReader

from arcgraph.tests.test_generator_and_dynamic_exports import _targets


@pytest.mark.parametrize("assigned", [False, True], ids=["chained", "assigned"])
def test_non_generator_wrapper_invalidates_return_annotation(tmp_path, assigned):
    source = (
        "class Box:\n    def send(self, value): return value\n"
        "def wrap(fn):\n    def inner(): return 42\n    return inner\n"
        "@wrap\ndef wrapped() -> Box: return Box()\n"
        "def use():\n"
        + (
            "    value = wrapped()\n    return value.send(None)\n"
            if assigned
            else "    return wrapped().send(None)\n"
        )
    )
    assert "method:pkg.user.Box.send" not in _targets(
        tmp_path, {"user": source}, v2=True
    )
    wrapped = next(
        n
        for n in GraphStoreReader.from_current(tmp_path / "out").read_nodes()
        if n.id == "fn:pkg.user.wrapped"
    )
    declared = next(r for r in wrapped.properties["type_refs"] if r["name"] == "return")
    assert declared["subject_kind"] == "declared_return"
    assert declared["type_id"] == "class:pkg.user.Box"


@pytest.mark.parametrize(
    "annotation", ["Box", "Generator[int, None, None]", "Iterator[int]"]
)
@pytest.mark.parametrize("async_", [False, True], ids=["sync", "async"])
def test_generator_body_determines_runtime_object_before_annotation(
    tmp_path, annotation, async_
):
    prefix = "async " if async_ else ""
    member, args, owner = (
        ("aclose", "", "AsyncGeneratorType")
        if async_
        else ("send", "None", "GeneratorType")
    )
    source = (
        "from typing import Generator, Iterator\n"
        "class Box:\n    def send(self, value): return value\n"
        "    def aclose(self): pass\n"
        + f"{prefix}def made() -> {annotation}:\n    yield 1\n"
        + f"def use(): return made().{member}({args})\n"
    )
    targets = _targets(tmp_path, {"user": source}, v2=True)
    assert f"extsym:types.{owner}.{member}" in targets, targets
    assert f"method:pkg.user.Box.{member}" not in targets, targets


@pytest.mark.parametrize(
    "prefix,decorator,expected",
    [
        ("", "", True),
        ("from functools import cache\n", "@cache\n", True),
        ("from functools import lru_cache\n", "@lru_cache\n", True),
        (
            "from functools import lru_cache\n",
            "@lru_cache(maxsize=2, typed=True)\n",
            True,
        ),
        ("from functools import lru_cache as memo\n", "@memo(2)\n", True),
        (
            "from functools import cache\ndef cache(fn): return lambda: 42\n",
            "@cache\n",
            False,
        ),
        ("def lru_cache(fn): return lambda: 42\n", "@lru_cache\n", False),
        (
            "from functools import lru_cache\ndef another(fn): return 42\n",
            "@lru_cache(another)\n",
            False,
        ),
        ("import functools\n", "@functools.cache\n", True),
        (
            "from functools import cache\ndef wrap(fn): return lambda: 42\n",
            "@cache\n@wrap\n",
            False,
        ),
        (
            "class Router:\n    def get(self, path): return lambda fn: lambda: 42\nrouter = Router()\n",
            "@router.get('/')\n",
            False,
        ),
    ],
    ids=[
        "plain",
        "cache",
        "lru",
        "lru-keywords",
        "lru-alias",
        "shadowed-cache",
        "fake-lru",
        "callable-maxsize",
        "qualified-conservative",
        "stacked-unknown",
        "fake-router",
    ],
)
@pytest.mark.parametrize("assigned", [False, True], ids=["chained", "assigned"])
def test_annotation_requires_a_proven_return_preserving_decorator(
    tmp_path, prefix, decorator, expected, assigned
):
    source = (
        "class Box:\n    def send(self, value): return value\n"
        + prefix
        + decorator
        + "def made() -> Box: return Box()\n"
        + "def use():\n"
        + (
            "    value = made()\n    return value.send(None)\n"
            if assigned
            else "    return made().send(None)\n"
        )
    )
    targets = _targets(tmp_path, {"user": source}, v2=True)
    assert ("method:pkg.user.Box.send" in targets) is expected, targets


@pytest.mark.parametrize(
    "decorator,prefix",
    [
        ("property", ""),
        ("cached_property", "from functools import cached_property\n"),
    ],
)
def test_property_annotation_describes_the_accessed_value(tmp_path, decorator, prefix):
    source = (
        "class Box:\n    def send(self, value): return value\n"
        + prefix
        + "class Holder:\n"
        + f"    @{decorator}\n"
        + "    def value(self) -> Box: return Box()\n"
        + "def use(): return Holder().value.send(None)\n"
    )
    assert "method:pkg.user.Box.send" in _targets(tmp_path, {"user": source}, v2=True)


@pytest.mark.parametrize("decorator", ["staticmethod", "classmethod"])
def test_builtin_descriptor_keeps_ordinary_return_type(tmp_path, decorator):
    arg = "" if decorator == "staticmethod" else "cls"
    source = (
        "class Box:\n    def send(self, value): return value\n"
        + "class Holder:\n"
        + f"    @{decorator}\n"
        + f"    def made({arg}) -> Box: return Box()\n"
        + "def use(): return Holder.made().send(None)\n"
    )
    assert "method:pkg.user.Box.send" in _targets(tmp_path, {"user": source}, v2=True)


def test_iterator_annotation_keeps_yielded_element_type(tmp_path):
    source = (
        "from typing import Iterator\n"
        "class Box:\n    def send(self, value): return value\n"
        "def made() -> Iterator[Box]: yield Box()\n"
        "def use():\n    for item in made(): item.send(None)\n"
    )
    assert "method:pkg.user.Box.send" in _targets(tmp_path, {"user": source}, v2=True)


@pytest.mark.parametrize("nested", [False, True], ids=["method", "nested-function"])
def test_wrapped_ordinary_returns_are_unknown_in_each_callable_scope(tmp_path, nested):
    prefix = (
        "class Box:\n    def send(self, value): return value\n"
        "def wrap(fn): return lambda *args: 42\n"
    )
    body = (
        "def use():\n    @wrap\n    def made() -> Box: return Box()\n"
        "    return made().send(None)\n"
        if nested
        else "class Holder:\n    @wrap\n    def made(self) -> Box: return Box()\n"
        "def use(): return Holder().made().send(None)\n"
    )
    assert "method:pkg.user.Box.send" not in _targets(
        tmp_path, {"user": prefix + body}, v2=True
    )


@pytest.mark.parametrize("nested", [False, True], ids=["method", "nested-function"])
def test_annotated_generators_in_each_callable_scope(tmp_path, nested):
    prefix = "class Box:\n    def send(self, value): return value\n"
    body = (
        "def use():\n    def made() -> Box: yield 1\n" "    return made().send(None)\n"
        if nested
        else "class Holder:\n    def made(self) -> Box: yield 1\n"
        "def use(): return Holder().made().send(None)\n"
    )
    targets = _targets(tmp_path, {"user": prefix + body}, v2=True)
    assert "extsym:types.GeneratorType.send" in targets, targets
    assert "method:pkg.user.Box.send" not in targets, targets


@pytest.mark.parametrize(
    "prefix,decorator",
    [
        ("from functools import lru_cache\n", "@lru_cache()\n"),
        ("from functools import lru_cache\n", "@lru_cache(maxsize=None)\n"),
        ("from functools import cache as memo\n", "@memo\n"),
    ],
)
def test_cache_literal_configuration_and_import_alias(tmp_path, prefix, decorator):
    source = (
        "class Box:\n    def send(self, value): return value\n"
        + prefix
        + decorator
        + "def made(cache=None) -> Box: return Box()\n"
        + "def use(): return made().send(None)\n"
    )
    assert "method:pkg.user.Box.send" in _targets(tmp_path, {"user": source}, v2=True)


@pytest.mark.parametrize(
    "prefix",
    [
        "from functools import cache\nfrom pkg.provider import *\n",
        "from pkg.provider import cache\n",
        "from functools import cache\ncache = replacement\n",
        "if flag:\n    from functools import cache\n",
    ],
)
def test_cache_requires_stable_runtime_standard_library_import(tmp_path, prefix):
    source = (
        "class Box:\n    def send(self, value): return value\n"
        "flag = True\ndef replacement(fn): return lambda: 42\n"
        + prefix
        + "@cache\ndef made() -> Box: return Box()\n"
        + "def use(): return made().send(None)\n"
    )
    provider = "__all__ = ['cache']\ndef cache(fn): return lambda: 42\n"
    assert "method:pkg.user.Box.send" not in _targets(
        tmp_path, {"user": source, "provider": provider}, v2=True
    )


@pytest.mark.parametrize("assigned", [False, True])
def test_imported_cached_factory_uses_provider_decorator_bindings(tmp_path, assigned):
    provider = (
        "from functools import lru_cache\n"
        "class Box:\n    def send(self, value): return value\n"
        "class Other:\n    def send(self, value): return value\n"
        "@lru_cache(maxsize=1)\ndef made() -> Box: return Box()\n"
    )
    user = "from pkg.provider import made\ndef use():\n" + (
        "    value = made()\n    return value.send(None)\n"
        if assigned
        else "    return made().send(None)\n"
    )
    assert "method:pkg.provider.Box.send" in _targets(
        tmp_path, {"provider": provider, "user": user}, v2=True
    )


@pytest.mark.parametrize("shadowed", [False, True])
def test_method_cache_decoration_reads_class_scope(tmp_path, shadowed):
    prefix = "from functools import cache\nclass Box:\n    def send(self, value): return value\ndef wrap(fn): return lambda *args: 42\n"
    source = (
        prefix
        + "class Holder:\n"
        + ("    cache = wrap\n" if shadowed else "")
        + "    @cache\n    def made(self) -> Box: return Box()\n"
        + "def use(): return Holder().made().send(None)\n"
    )
    assert (
        "method:pkg.user.Box.send" in _targets(tmp_path, {"user": source}, v2=True)
    ) is (not shadowed)


def test_cache_import_after_decoration_is_not_runtime_evidence(tmp_path):
    source = (
        "class Box:\n    def send(self, value): return value\n"
        "@cache\ndef made() -> Box: return Box()\n"
        "from functools import cache\ndef use(): return made().send(None)\n"
    )
    assert "method:pkg.user.Box.send" not in _targets(
        tmp_path, {"user": source}, v2=True
    )
