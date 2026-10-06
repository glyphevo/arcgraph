"""A local value hides the import, definition or builtin of its name.

In Python a parameter, an assignment, a del or any other binding of a value
makes a name local in the whole function body, so a call through that name
does not reach the module's import, function or class, nor the builtin. This
held in nested functions only; in a module-level function or a method the
call was linked to what the module binds to the name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcgraph.tests.test_path_receiver_types import _resolutions_by_function

SOURCE = """import os
from os.path import join
from pathlib import Path


class Config:
    @classmethod
    def load(cls) -> "Config":
        return cls()

    def value(self) -> int:
        return 1


def helper() -> Config:
    return Config()


def module_param(os) -> None:
    os.getcwd()


def assigned_import() -> None:
    join = str
    join("a")


class Box:
    def method_param(self, os) -> None:
        os.getcwd()

    def super_param(self, super) -> None:
        super().value()


def builtin_param(format) -> None:
    format(1)


def builtin_assigned() -> None:
    len = str
    len("a")


def assigned_after() -> None:
    os.getcwd()
    os = None


def deleted() -> None:
    os.getcwd()
    del os


def annotated_only() -> None:
    os: Path
    os.getcwd()


def helper_param(helper) -> None:
    made = helper()
    made.value()


def class_param(Config) -> None:
    loaded = Config.load()
    loaded.value()


def typed_shadow(os: Path) -> None:
    os.exists()


def typed_chain(os: Path) -> None:
    os.resolve().exists()


def typed_project_chain(helper: Config) -> None:
    helper.load().value()


class Aliases:
    def target(self) -> int:
        return 1

    def via_alias(self) -> None:
        # helper is also a module function; the local is this method.
        helper = self.target
        helper()

    @classmethod
    def built_by_cls(cls) -> "Aliases":
        return cls()


def declared_global() -> None:
    global os
    os.getcwd()


def local_import() -> None:
    import os

    os.getcwd()


def comprehension(items: list) -> None:
    names = [os for os in items]
    os.getcwd()


def unhidden_param(conn) -> None:
    conn.fetchone()


def unshadowed() -> None:
    os.getcwd()
    join("a", "b")
    format(1)
    loaded = Config.load()
    loaded.value()
    made = helper()
    made.value()
"""


@pytest.fixture(scope="module")
def resolutions(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, set[tuple[str, str]]]:
    return _resolutions_by_function(tmp_path_factory.mktemp("shadowed"), SOURCE)


def test_source_parses() -> None:
    compile(SOURCE, "shadowed.py", "exec")


@pytest.mark.parametrize(
    "name",
    [
        "module_param",
        "assigned_import",
        "method_param",
        "super_param",
        "builtin_param",
        "builtin_assigned",
        # Bound later in the body, the name is local at the call too, which
        # raises UnboundLocalError rather than calling the import.
        "assigned_after",
        "deleted",
        "annotated_only",
        "helper_param",
        "class_param",
    ],
)
def test_a_call_through_a_hiding_local_links_nothing(resolutions, name):
    assert resolutions.get(name, set()) == set()


def test_a_typed_local_resolves_by_its_own_type(resolutions):
    assert resolutions.get("typed_shadow") == {
        ("extsym:pathlib.Path.exists", "external_receiver_type")
    }
    # So does the inner call of a chain through it.
    assert resolutions.get("typed_chain") == {
        ("extsym:pathlib.Path.resolve", "external_receiver_type"),
        ("extsym:pathlib.Path.exists", "external_receiver_type"),
    }
    assert ("method:lab.Config.value", "receiver_type") in resolutions.get(
        "typed_project_chain", set()
    )


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("declared_global", "extsym:os.getcwd"),
        ("local_import", "extsym:os.getcwd"),
        # A comprehension's variable is local to the comprehension only.
        ("comprehension", "extsym:os.getcwd"),
        ("unshadowed", "extsym:os.getcwd"),
        ("unshadowed", "extsym:os.path.join"),
        ("unshadowed", "extsym:builtins.format"),
        ("unshadowed", "method:lab.Config.load"),
        ("unshadowed", "fn:lab.helper"),
        ("unshadowed", "method:lab.Config.value"),
        # A local's own definition, alias or class still resolves it.
        ("via_alias", "method:lab.Aliases.target"),
        ("built_by_cls", "class:lab.Aliases"),
        # A parameter that hides nothing keeps the boundary its name gives.
        ("unhidden_param", "protocol:pep249.Cursor.fetchone"),
    ],
)
def test_a_name_no_local_hides_still_resolves(resolutions, name, target):
    assert target in {t for t, _ in resolutions.get(name, set())}


def test_the_index_records_no_call_through_a_hiding_local(tmp_path: Path) -> None:
    # The same through the indexer's own edges, for one case of each kind.
    found = _resolutions_by_function(
        tmp_path,
        "import json\n\n\ndef dump(json):\n    return json.dumps(1)\n\n\n"
        "def size(len):\n    return len([])\n\n\n"
        "text = json.dumps(1)\njson = None\n",
    )
    assert found.get("dump", set()) == set()
    assert found.get("size", set()) == set()
    # At module level the module's own bindings are the scope: before the
    # assignment, json is still the import.
    assert ("extsym:json.dumps", "imported_module_attribute") in found["mod:lab"]


ELSEWHERE = {
    "other.py": (
        "class Made:\n    def value(self):\n        return 1\n\n\n"
        "def helper() -> Made:\n    return Made()\n"
    ),
    "use.py": (
        "class Config:\n    @classmethod\n    def load(cls) -> 'Config':\n"
        "        return cls()\n\n    def value(self) -> int:\n        return 1\n\n\n"
        "def param_called(helper):\n    made = helper()\n    made.value()\n\n\n"
        "class Box:\n    def method_called(self, helper):\n        return helper()\n\n\n"
        "def assigned_called(make):\n    helper = make\n    helper()\n\n\n"
        "def param_constructed(Made):\n    Made().value()\n\n\n"
        "def inline_hidden(Config):\n    Config.load().value()\n\n\n"
        "def imported_called():\n    from pkg.other import helper\n\n"
        "    helper().value()\n    Config.load().value()\n"
    ),
}


@pytest.fixture(scope="module")
def elsewhere(tmp_path_factory: pytest.TempPathFactory) -> dict[str, set[str]]:
    from arcgraph.core.graph_store import GraphStoreReader
    from arcgraph.core.scanner import SourceRoot
    from arcgraph.pipeline.indexer import ArcGraphIndexer

    root = tmp_path_factory.mktemp("elsewhere")
    package = root / "repo" / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name, text in ELSEWHERE.items():
        (package / name).write_text(text, encoding="utf-8")
    output = root / "out"
    ArcGraphIndexer(
        repo_root=package.parents[1],
        output_dir=output,
        source_roots=[SourceRoot("src")],
    ).build()
    targets: dict[str, set[str]] = {}
    for edge in GraphStoreReader.from_current(output).read_edges():
        if edge.resolution.status == "resolved" and edge.kind != "similar_to":
            targets.setdefault(edge.source.rsplit(".", 1)[-1], set()).add(edge.target)
    return targets


@pytest.mark.parametrize(
    "name",
    [
        # A local value called is that value, not the one function or class of
        # its name elsewhere in the project, and its result has no type.
        "param_called",
        "method_called",
        "assigned_called",
        "param_constructed",
        # An inner call of a chain through a hiding local links nothing either.
        "inline_hidden",
    ],
)
def test_a_called_local_value_is_not_a_function_elsewhere(elsewhere, name):
    assert elsewhere.get(name, set()) == set()


def test_an_imported_function_still_resolves(elsewhere):
    assert {
        "fn:pkg.other.helper",
        "method:pkg.other.Made.value",
        "method:pkg.use.Config.load",
        "method:pkg.use.Config.value",
    } <= elsewhere["imported_called"]


RECEIVERS = """class Other:
    def build(self):
        return 1


class Box:
    def __init__(self) -> None:
        self.other = Other()

    def build(self):
        return 2

    def make(self) -> Other:
        return Other()

    def instance_named_cls(cls):
        # An instance method's first parameter is the instance, whatever its
        # name; calling it is not constructing Box.
        return cls()

    @staticmethod
    def static_made(self):
        made = self.make()
        made.build()

    @staticmethod
    def static_attribute(self):
        other = self.other
        other.build()

    def cls_param(self, cls):
        cls.build()

    def cls_rebound(self):
        cls = Other
        cls.build()

    def cls_called_param(self, cls):
        return cls()

    def cls_called_rebound(self):
        cls = str
        return cls()

    @classmethod
    def class_rebound(cls):
        cls = str
        return cls()

    @staticmethod
    def static_self(self):
        self.build()

    def self_rebound(self):
        self = Other()
        self.build()

    @classmethod
    def class_own(cls):
        cls.build()
        return cls()

    def self_own(self):
        self.build()
"""


@pytest.fixture(scope="module")
def receivers(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, set[tuple[str, str]]]:
    return _resolutions_by_function(tmp_path_factory.mktemp("receivers"), RECEIVERS)


@pytest.mark.parametrize(
    "name",
    [
        "cls_param",
        "cls_rebound",
        "cls_called_param",
        "cls_called_rebound",
        "class_rebound",
        "static_self",
        "instance_named_cls",
    ],
)
def test_self_or_cls_that_is_not_the_receiver_is_not_the_class(receivers, name):
    # Only the first parameter of a method that is not static, never rebound,
    # is the receiver; elsewhere self and cls are ordinary names.
    assert not any(
        target in {"class:lab.Box", "method:lab.Box.build"}
        for target, _ in receivers.get(name, set())
    ), sorted(receivers.get(name, set()))


@pytest.mark.parametrize(
    ("name", "target", "strategy"),
    [
        ("class_own", "method:lab.Box.build", "same_class_receiver"),
        ("class_own", "class:lab.Box", "class_receiver_constructor"),
        ("self_own", "method:lab.Box.build", "same_class_receiver"),
        # A rebound self is read by its new value.
        ("self_rebound", "method:lab.Other.build", "receiver_type"),
    ],
)
def test_the_own_receiver_is_the_class(receivers, name, target, strategy):
    assert (target, strategy) in receivers.get(name, set())


@pytest.mark.parametrize("name", ["static_made", "static_attribute"])
def test_a_value_through_a_static_self_has_no_class_type(receivers, name):
    # The type analyzer reads self as the class only where it is the receiver.
    assert "method:lab.Other.build" not in {t for t, _ in receivers.get(name, set())}
