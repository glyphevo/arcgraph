"""A bare name in a function is the module's own definition of it.

A function or method body sees its own locals, enclosing functions and the
module, not a class body or a function nested elsewhere. Same-module
candidates for a bare name counted every symbol of the module with that
name, so helper() was left unresolved when a class of the module also had a
helper method; the module's own definition now wins among them.
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
        # A method body does not see its class body's names.
        ("method", "fn:lab.helper", "same_module_symbol"),
        # Nor a function nested in another function, nor another method.
        ("use_nested", "fn:lab.nested", "same_module_symbol"),
        # Where a name is defined locally, that definition is it.
        ("outer", "fn:lab.outer.nested", "local_definition"),
    ],
)
def test_a_bare_name_is_the_definition_it_sees(resolutions, name, target, strategy):
    assert resolutions.get(name, set()) == {(target, strategy)}


def test_a_class_body_name_is_not_taken_for_the_module_s(resolutions):
    # The class body sees its own helper, which the index does not tell from
    # the module's here, so neither is linked.
    assert "fn:lab.helper" not in {t for t, _ in resolutions.get("Local", set())}
