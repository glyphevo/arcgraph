"""Body shape and a literal export list do not survive unknown runtime changes."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer


def _targets(tmp_path: Path, modules: dict[str, str], *, v2: bool) -> set[str]:
    package = tmp_path / "repo" / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name, source in modules.items():
        compile(source, name, "exec")
        (package / f"{name}.py").write_text(source, encoding="utf-8")
    output = tmp_path / "out"
    ArcGraphIndexer(
        repo_root=package.parents[1],
        output_dir=output,
        source_roots=[SourceRoot("src")],
        enable_v2_call_resolution=v2,
    ).build()
    return {
        edge.target
        for edge in GraphStoreReader.from_current(output).read_edges()
        if edge.source == "fn:pkg.user.use"
        and edge.resolution.status == "resolved"
        and (edge.properties.get("callsite") or edge.properties.get("callsites"))
    }


GENERATOR_CASES = [
    ("plain", "", "", False, False, True),
    (
        "contextmanager",
        "from contextlib import contextmanager\n",
        "@contextmanager\n",
        False,
        False,
        False,
    ),
    (
        "annotated_contextmanager",
        "from contextlib import contextmanager\n",
        "@contextmanager\n",
        False,
        True,
        False,
    ),
    (
        "asynccontextmanager",
        "from contextlib import asynccontextmanager\n",
        "@asynccontextmanager\n",
        True,
        False,
        False,
    ),
    ("custom", "def wrap(fn):\n    return lambda: 1\n", "@wrap\n", False, False, False),
    ("identity", "def wrap(fn):\n    return fn\n", "@wrap\n", False, False, False),
    (
        "wraps",
        "from functools import wraps\ndef wrap(fn):\n    @wraps(fn)\n    def replaced():\n        return 1\n    return replaced\n",
        "@wrap\n",
        False,
        False,
        False,
    ),
    ("plain_async", "", "", True, False, True),
]


@pytest.mark.parametrize("v2", [False, True], ids=["legacy", "v2"])
@pytest.mark.parametrize(
    "case,prefix,decorator,async_,annotated,expected",
    GENERATOR_CASES,
    ids=[row[0] for row in GENERATOR_CASES],
)
def test_generator_call_respects_decorators(
    tmp_path, v2, case, prefix, decorator, async_, annotated, expected
):
    keyword = "async " if async_ else ""
    annotation = " -> Generator[int, None, None]" if annotated else ""
    source = (
        "from typing import Generator\n"
        + prefix
        + decorator
        + f"{keyword}def made(){annotation}:\n    yield 1\n"
    )
    method = "aclose" if async_ else "send"
    args = "" if async_ else "None"
    targets = _targets(
        tmp_path,
        {"user": source + f"def use():\n    return made().{method}({args})\n"},
        v2=v2,
    )
    owner = "AsyncGeneratorType" if async_ else "GeneratorType"
    assert (f"extsym:types.{owner}.{method}" in targets) is (expected and v2), targets


@pytest.mark.parametrize("assigned", [False, True])
def test_wrapped_generator_does_not_keep_the_original_return_annotation(
    tmp_path, assigned
):
    source = "class Box:\n    def send(self, value):\n        return value\ndef wrap(fn):\n    return lambda: 1\n@wrap\ndef made() -> Box:\n    yield 1\n"
    body = (
        "    value = made()\n    return value.send(None)\n"
        if assigned
        else "    return made().send(None)\n"
    )
    targets = _targets(tmp_path, {"user": source + "def use():\n" + body}, v2=True)
    assert "method:pkg.user.Box.send" not in targets, targets


@pytest.mark.parametrize("v2", [False, True], ids=["legacy", "v2"])
@pytest.mark.parametrize(
    "decorator,prefix,expected",
    [
        ("staticmethod", "", True),
        ("classmethod", "", True),
        ("staticmethod", "def staticmethod(fn):\n    return lambda: 1\n", False),
        ("classmethod", "def classmethod(fn):\n    return lambda: 1\n", False),
        ("contextmanager", "from contextlib import contextmanager\n", False),
        ("staticmethod", "from contextlib import contextmanager\n", False),
    ],
    ids=[
        "static",
        "class",
        "shadow_static",
        "shadow_class",
        "wrapped_method",
        "stacked",
    ],
)
def test_generator_methods_preserve_only_known_descriptors(
    tmp_path, v2, decorator, prefix, expected
):
    extra = (
        "    @contextmanager\n"
        if decorator == "staticmethod" and prefix.startswith("from")
        else ""
    )
    parameter = "cls" if decorator == "classmethod" else ""
    source = (
        prefix
        + f"class Box:\n    @{decorator}\n"
        + extra
        + f"    def made({parameter}):\n        yield 1\ndef use():\n    return Box.made().send(None)\n"
    )
    targets = _targets(tmp_path, {"user": source}, v2=v2)
    assert ("extsym:types.GeneratorType.send" in targets) is (expected and v2), targets


@pytest.mark.parametrize("decorated", [False, True])
def test_nested_generator_decorators(tmp_path, decorated):
    decorator = "    @contextmanager\n" if decorated else ""
    source = (
        "from contextlib import contextmanager\ndef use():\n"
        + decorator
        + "    def made():\n        yield 1\n    return made().send(None)\n"
    )
    targets = _targets(tmp_path, {"user": source}, v2=True)
    assert ("extsym:types.GeneratorType.send" in targets) is (not decorated), targets


@pytest.mark.parametrize(
    "setup,expected",
    [
        ("def unrelated(staticmethod):\n    pass\n", True),
        (
            "def change():\n    global staticmethod\n    staticmethod = lambda fn: lambda: 1\nchange()\n",
            False,
        ),
        ("", True),
    ],
    ids=["unrelated_local", "global_shadow", "own_parameter"],
)
def test_generator_descriptor_uses_definition_scope(tmp_path, setup, expected):
    source = (
        setup
        + "class Box:\n    @staticmethod\n    def made(staticmethod=None):\n        yield 1\ndef use():\n    return Box.made().send(None)\n"
    )
    targets = _targets(tmp_path, {"user": source}, v2=True)
    assert ("extsym:types.GeneratorType.send" in targets) is expected, targets


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("decorated", [False, True])
def test_instance_generator_method(tmp_path, async_, decorated):
    keyword = "async " if async_ else ""
    owner = "AsyncGeneratorType" if async_ else "GeneratorType"
    method = "aclose" if async_ else "send"
    argument = "" if async_ else "None"
    decoration = "    @wrap\n" if decorated else ""
    source = (
        "def wrap(fn):\n    return lambda self: 1\nclass Box:\n"
        + decoration
        + f"    {keyword}def made(self):\n        yield 1\ndef use():\n    return Box().made().{method}({argument})\n"
    )
    targets = _targets(tmp_path, {"user": source}, v2=True)
    assert (f"extsym:types.{owner}.{method}" in targets) is (not decorated), targets


EXPORT_BASE = "def helper():\n    return 1\ndef other():\n    return 2\n"
EXPORT_CASES = [
    ("literal", "__all__ = ['helper', 'other']\n", True),
    ("tuple", "__all__ = ('helper', 'other')\n", True),
    ("augmented_only", "__all__ += ['helper']\n", False),
    ("empty", "__all__ = []\n", False),
    (
        "global_write",
        "__all__ = ['helper', 'other']\ndef clear():\n    global __all__\n    __all__ = ['other']\nclear()\n",
        False,
    ),
    (
        "global_create",
        "def clear():\n    global __all__\n    __all__ = ['other']\nclear()\n",
        False,
    ),
    (
        "global_delete",
        "__all__ = ['other']\ndef clear():\n    global __all__\n    del __all__\nclear()\n",
        False,
    ),
    (
        "global_augmented",
        "__all__ = ['other']\ndef clear():\n    global __all__\n    __all__ += ['helper']\nclear()\n",
        False,
    ),
    (
        "function_remove",
        "__all__ = ['helper', 'other']\ndef clear():\n    __all__.remove('helper')\nclear()\n",
        False,
    ),
    (
        "nested_remove",
        "__all__ = ['helper', 'other']\ndef clear():\n    def inner():\n        __all__.remove('helper')\n    inner()\nclear()\n",
        False,
    ),
    (
        "method_remove",
        "__all__ = ['helper', 'other']\nclass Box:\n    def clear(self):\n        __all__.remove('helper')\nBox().clear()\n",
        False,
    ),
    (
        "local_list",
        "__all__ = ['helper', 'other']\ndef clear():\n    __all__ = ['helper']\n    __all__.remove('helper')\nclear()\n",
        True,
    ),
    (
        "nested_local_list",
        "__all__ = ['helper', 'other']\ndef clear():\n    __all__ = ['helper']\n    def inner():\n        __all__.remove('helper')\n    inner()\nclear()\n",
        True,
    ),
    (
        "local_alias",
        "__all__ = ['helper', 'other']\ndef clear():\n    __all__ = ['helper']\n    alias = __all__\n    alias.remove('helper')\nclear()\n",
        True,
    ),
    ("module_append", "__all__ = ['other']\n__all__.append('helper')\n", False),
    ("module_extend", "__all__ = ['other']\n__all__.extend(['helper'])\n", False),
    (
        "module_remove",
        "__all__ = ['helper', 'other']\n__all__.remove('helper')\n",
        False,
    ),
    ("module_augmented", "__all__ = ['other']\n__all__ += ['helper']\n", False),
    ("module_delete", "__all__ = ['other']\ndel __all__\n", False),
    ("subscript_delete", "__all__ = ['helper', 'other']\ndel __all__[0]\n", False),
    (
        "function_subscript",
        "__all__ = ['helper', 'other']\ndef clear():\n    __all__[0] = 'other'\nclear()\n",
        False,
    ),
    (
        "alias",
        "__all__ = ['helper', 'other']\nalias = __all__\nalias.remove('helper')\n",
        False,
    ),
    (
        "function_alias",
        "__all__ = ['helper', 'other']\ndef clear():\n    alias = __all__\n    alias.remove('helper')\nclear()\n",
        False,
    ),
    (
        "escape",
        "__all__ = ['helper', 'other']\ndef change(value):\n    value.remove('helper')\nchange(__all__)\n",
        False,
    ),
    (
        "conditional",
        "__all__ = ['helper', 'other']\nif FLAG:\n    __all__ = ['other']\n",
        False,
    ),
    ("computed", "from pkg.values import exports\n__all__ = exports\n", False),
]


@pytest.mark.parametrize("v2", [False, True], ids=["legacy", "v2"])
@pytest.mark.parametrize(
    "case,changes,expected", EXPORT_CASES, ids=[row[0] for row in EXPORT_CASES]
)
def test_star_import_requires_stable_runtime_exports(
    tmp_path, v2, case, changes, expected
):
    targets = _targets(
        tmp_path,
        {
            "provider": EXPORT_BASE + changes,
            "user": "from pkg.provider import *\ndef use():\n    return helper()\n",
            "values": "exports = ['other']\n",
        },
        v2=v2,
    )
    assert ("fn:pkg.provider.helper" in targets) is expected, targets


def test_unknown_exports_do_not_select_the_importers_old_binding(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "provider": EXPORT_BASE
            + dict((row[0], row[1]) for row in EXPORT_CASES)["global_write"],
            "user": "def helper():\n    return 3\nfrom pkg.provider import *\ndef use():\n    return helper()\n",
        },
        v2=True,
    )
    assert not targets, targets
