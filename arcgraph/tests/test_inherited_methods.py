"""A class inherits methods in the order of its method resolution order.

The order is the C3 linearization of the class's bases, as Python computes
it. A base outside the project, such as object or Exception, has methods
unknown to the index, so the search stops there with no target; a base that
may be a project class not settled here leaves the whole order unknown.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcgraph.tests.test_path_receiver_types import _resolutions_by_function

SOURCE = """from typing import Generic, TypeVar

T = TypeVar("T")


class Left:
    def left_only(self) -> int:
        return 1


class Right:
    def right_only(self) -> int:
        return 2

    def shared(self) -> int:
        return 3


class Both(Left, Right):
    pass


class Top:
    def ping(self) -> int:
        return 0


class Mid1(Top):
    pass


class Mid2(Top):
    def ping(self) -> int:
        return 1


class Diamond(Mid1, Mid2):
    def call_super(self) -> int:
        return super().ping()


class ErrorBase(Exception):
    pass


class Mixed(ErrorBase, Right):
    pass


class Sub(Right[int]):
    pass


class Typed(Generic[T], Right):
    pass


class Plain(object):
    def plain(self) -> int:
        return 1


class PlainChild(Plain):
    pass


class X(Left, Right):
    pass


class Y(Right, Left):
    def shared(self) -> int:
        return 4


class Bad(X, Y):
    pass


Assigned = Mid2


class ThroughAssigned(Mid1, Assigned):
    pass


class Twice(Top):
    def ping(self) -> int:
        return 5


class Twice(Top):
    def ping(self) -> int:
        return 6


class ThroughTwice(Mid1, Twice):
    pass


class Loop1(Loop2):
    pass


class Loop2(Loop1):
    pass


def multi(item: Both) -> None:
    item.right_only()
    item.left_only()


def diamond(item: Diamond) -> None:
    item.ping()


def mixed(item: Mixed) -> None:
    item.right_only()


def sub(item: Sub) -> None:
    item.shared()


def typed(item: Typed) -> None:
    item.right_only()


def plain_child(item: PlainChild) -> None:
    item.plain()


def bad(item: Bad) -> None:
    item.shared()


def through_assigned(item: ThroughAssigned) -> None:
    item.ping()


def through_twice(item: ThroughTwice) -> None:
    item.ping()


def loop(item: Loop1) -> None:
    item.ping()
"""


@pytest.fixture(scope="module")
def resolutions(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, set[tuple[str, str]]]:
    return _resolutions_by_function(tmp_path_factory.mktemp("mro"), SOURCE)


def _targets(resolutions: dict[str, set[tuple[str, str]]], name: str) -> set[str]:
    return {target for target, _ in resolutions.get(name, set())}


def test_source_parses() -> None:
    compile(SOURCE, "mro.py", "exec")


@pytest.mark.parametrize(
    ("name", "target", "strategy"),
    [
        ("multi", "method:lab.Right.right_only", "inherited_receiver_type"),
        ("multi", "method:lab.Left.left_only", "inherited_receiver_type"),
        # Diamond is Diamond, Mid1, Mid2, Top: Mid2's ping comes before Top's,
        # which a depth-first search of the first base would reach.
        ("diamond", "method:lab.Mid2.ping", "inherited_receiver_type"),
        ("call_super", "method:lab.Mid2.ping", "super_receiver"),
        ("sub", "method:lab.Right.shared", "inherited_receiver_type"),
        # object is a builtin and comes after Plain, where plain is found.
        ("plain_child", "method:lab.Plain.plain", "inherited_receiver_type"),
    ],
)
def test_methods_resolve_in_method_resolution_order(
    resolutions, name, target, strategy
):
    assert (target, strategy) in resolutions.get(name, set()), sorted(
        resolutions.get(name, set())
    )


@pytest.mark.parametrize(
    "name",
    [
        # Exception, whose methods are unknown here, comes before Right.
        "mixed",
        # So does typing.Generic, which has no right_only; a known limit.
        "typed",
        # Bad has no order: X puts Left before Right and Y the reverse.
        "bad",
        # Two bases that may be project classes not settled here: placing
        # them alone would put Top before them and link Top.ping.
        "through_assigned",
        "through_twice",
        "loop",
    ],
)
def test_no_method_past_an_unknown_base_or_order(resolutions, name):
    assert not any(t.startswith("method:lab.") for t in _targets(resolutions, name))


def test_a_star_imported_base_is_not_taken_for_a_builtin(tmp_path: Path) -> None:
    from arcgraph.core.graph_store import GraphStoreReader
    from arcgraph.core.scanner import SourceRoot
    from arcgraph.pipeline.indexer import ArcGraphIndexer

    package = tmp_path / "repo" / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "base.py").write_text(
        "class Top:\n    def ping(self):\n        return 0\n\n\n"
        "class Mid1(Top):\n    pass\n\n\n"
        "class Over(Top):\n    def ping(self):\n        return 1\n",
        encoding="utf-8",
    )
    (package / "star.py").write_text(
        "from pkg.base import *\nfrom pkg.base import Mid1\n\n\n"
        "class Joined(Mid1, Over):\n    pass\n\n\n"
        "def use(item: Joined):\n    return item.ping()\n",
        encoding="utf-8",
    )
    output = tmp_path / "out"
    ArcGraphIndexer(
        repo_root=package.parents[1],
        output_dir=output,
        source_roots=[SourceRoot("src")],
    ).build()
    targets = {
        edge.target
        for edge in GraphStoreReader.from_current(output).read_edges()
        if edge.source == "fn:pkg.star.use" and edge.resolution.status == "resolved"
    }
    # Over comes by the star import, so it is not a builtin standing alone;
    # Joined is Joined, Mid1, Over, Top, and Top.ping is not what is called.
    assert "method:pkg.base.Top.ping" not in targets
