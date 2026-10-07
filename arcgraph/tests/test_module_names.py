"""A bare name is the definition its scope sees, in Python's order.

A function or method body sees its own locals, enclosing functions and the
module, never a class body or a function nested in another function: a name
there is the module's own top-level definition, so helper() is not a
helper method of a class in the module, however many share the name, and a
method is never reached by a bare name. A class body runs in order: a name
it has bound before the call is its own, one it has not is the module's.
"""

from __future__ import annotations

import pytest

from arcgraph.tests.test_path_receiver_types import _resolutions_by_function

SOURCE = """def helper(x):
    return x


def outer():
    def nested():
        return 1

    return nested()


def nested():
    return 2


class Box:
    @staticmethod
    def helper(x):
        return x

    def method(self):
        return helper(1)

    def nested(self):
        return 3


class Local:
    def helper(x):
        return x

    value = helper(1)


class Early:
    value = helper(1)

    def helper(x):
        return x


class Plain:
    value = helper(1)


class Assigned:
    helper = staticmethod(len)
    value = helper([])


class Only:
    @staticmethod
    def only(x):
        return x

    def method(self):
        return only(1)


def use():
    return helper(1)


def use_nested():
    return nested()

"""


@pytest.fixture(scope="module")
def resolutions(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, set[tuple[str, str]]]:
    return _resolutions_by_function(tmp_path_factory.mktemp("names"), SOURCE)


def test_source_parses() -> None:
    compile(SOURCE, "names.py", "exec")


@pytest.mark.parametrize(
    ("name", "target", "strategy"),
    [
        # Box.helper shares the name; the module's helper is what is called.
        ("use", "fn:lab.helper", "same_module_symbol"),
        # Nor a function nested in another function, nor another method.
        ("use_nested", "fn:lab.nested", "same_module_symbol"),
        # Where a name is defined locally, that definition is it.
        ("outer", "fn:lab.outer.nested", "local_definition"),
        # A class body that has defined helper by the call calls its own.
        ("Local", "method:lab.Local.helper", "same_module_symbol"),
        # One that defines it only after the call, or not at all, calls the
        # module's.
        ("Early", "fn:lab.helper", "same_module_symbol"),
        ("Plain", "fn:lab.helper", "same_module_symbol"),
    ],
)
def test_a_bare_name_is_the_definition_it_sees(resolutions, name, target, strategy):
    assert resolutions.get(name, set()) == {(target, strategy)}


def test_a_method_body_does_not_see_its_class_body(resolutions):
    # Box.method's helper(1) is the module's; Only.method's only(1) is a
    # NameError at run time, not the method of that name.
    targets = {t for t, _ in resolutions.get("method", set())}
    assert targets == {"fn:lab.helper"}


def test_a_class_body_name_bound_otherwise_is_not_the_module_s(resolutions):
    # helper is bound to staticmethod(len) by the call, which the index does
    # not follow; it is not the module's helper.
    assert "fn:lab.helper" not in {t for t, _ in resolutions.get("Assigned", set())}


def test_the_legacy_resolver_links_no_method_by_a_bare_name(tmp_path):
    from arcgraph.core.graph_store import GraphStoreReader
    from arcgraph.core.scanner import SourceRoot
    from arcgraph.pipeline.indexer import ArcGraphIndexer

    package = tmp_path / "repo" / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "only.py").write_text(
        "class Only:\n    @staticmethod\n    def only(x):\n        return x\n\n"
        "    def method(self):\n        return only(1)\n",
        encoding="utf-8",
    )
    output = tmp_path / "out"
    ArcGraphIndexer(
        repo_root=package.parents[1],
        output_dir=output,
        source_roots=[SourceRoot("src")],
        enable_v2_call_resolution=False,
    ).build()
    assert not [
        edge
        for edge in GraphStoreReader.from_current(output).read_edges()
        if edge.source == "method:pkg.only.Only.method"
        and edge.resolution.status == "resolved"
    ]


def test_the_legacy_resolver_still_matches_a_star_imported_name(tmp_path):
    # It matches an imported name to the one symbol of that name; a def that
    # a star import brings counts as imported, as the import itself does.
    from arcgraph.core.graph_store import GraphStoreReader
    from arcgraph.core.scanner import SourceRoot
    from arcgraph.pipeline.indexer import ArcGraphIndexer

    package = tmp_path / "repo" / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "provider.py").write_text(
        "def tool(x):\n    return x\n", encoding="utf-8"
    )
    (package / "user.py").write_text(
        "from pkg.provider import *\n\n\ndef use():\n    return tool(1)\n",
        encoding="utf-8",
    )
    output = tmp_path / "out"
    ArcGraphIndexer(
        repo_root=package.parents[1],
        output_dir=output,
        source_roots=[SourceRoot("src")],
        enable_v2_call_resolution=False,
    ).build()
    assert {
        edge.target
        for edge in GraphStoreReader.from_current(output).read_edges()
        if edge.source == "fn:pkg.user.use" and edge.resolution.status == "resolved"
    } == {"fn:pkg.provider.tool"}


def test_the_legacy_resolver_matches_no_name_a_star_import_leaves_out(tmp_path):
    from arcgraph.core.graph_store import GraphStoreReader
    from arcgraph.core.scanner import SourceRoot
    from arcgraph.pipeline.indexer import ArcGraphIndexer

    package = tmp_path / "repo" / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "provider.py").write_text(
        "def tool(x):\n    return x\n\n\n__all__ = []\n", encoding="utf-8"
    )
    (package / "user.py").write_text(
        "from pkg.provider import *\n\n\ndef use():\n    return tool(1)\n",
        encoding="utf-8",
    )
    output = tmp_path / "out"
    ArcGraphIndexer(
        repo_root=package.parents[1],
        output_dir=output,
        source_roots=[SourceRoot("src")],
        enable_v2_call_resolution=False,
    ).build()
    assert not [
        edge
        for edge in GraphStoreReader.from_current(output).read_edges()
        if edge.source == "fn:pkg.user.use" and edge.resolution.status == "resolved"
    ]
