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
        # A parameter that hides nothing keeps the boundary its name gives.
        ("unhidden_param", "extsym:dbapi.Connection.fetchone"),
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
