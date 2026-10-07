"""Where a value may be None, what proves it is not before it is assigned.

A name assigned a value that may be None takes the type without None where
the assignment runs only with the value present: after a statement that
leaves when it is absent or asserts it is present, or a match whose first
case None leaves; in the branch of an if taken only when it is present; in
the else of a try whose body proved it, or after a try whose handlers all
leave. Anything that may bind the name on the way, including a test, a with
item, a case pattern or an except clause, or a part of a try that may have
run before a handler or finally, leaves None in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcgraph.tests.test_path_receiver_types import _index

SOURCE = """class Box:
    def build(self):
        return 1


def make_box() -> Box | None:
    return None


def other() -> Box | None:
    return None


def assert_is_not_none():
    shared: Box | None = None
    source = make_box()
    assert source is not None
    shared = source
    shared.build()


def assert_truthy():
    shared: Box | None = None
    source = make_box()
    assert source
    shared = source
    shared.build()


def assert_and():
    shared: Box | None = None
    source = make_box()
    assert source is not None and source.build()
    shared = source
    shared.build()


def assert_or():
    shared: Box | None = None
    source = make_box()
    assert source or other()
    shared = source
    shared.build()


def assert_is_none():
    shared: Box | None = None
    source = make_box()
    assert source is None
    shared = source
    shared.build()


def positive_branch():
    shared: Box | None = None
    source = make_box()
    if source is not None:
        shared = source
        shared.build()


def positive_not_equals():
    shared: Box | None = None
    source = make_box()
    if None != source:
        shared = source
        shared.build()


def positive_truthy():
    shared: Box | None = None
    source = make_box()
    if source:
        shared = source
        shared.build()


def else_of_absent():
    shared: Box | None = None
    source = make_box()
    if source is None:
        print("none")
    else:
        shared = source
        shared.build()


def elif_of_absent(flag):
    shared: Box | None = None
    source = make_box()
    if source is None:
        print("none")
    elif flag:
        shared = source
        shared.build()


def body_of_absent():
    shared: Box | None = None
    source = make_box()
    if source is None:
        shared = source
        shared.build()


def else_of_present():
    shared: Box | None = None
    source = make_box()
    if source is not None:
        print("box")
    else:
        shared = source
        shared.build()


def positive_or(flag):
    shared: Box | None = None
    source = make_box()
    if source is not None or flag:
        shared = source
        shared.build()


def positive_and(flag):
    shared: Box | None = None
    source = make_box()
    if flag and source is not None:
        shared = source
        shared.build()


def positive_rebound():
    shared: Box | None = None
    source = make_box()
    if source is not None:
        source = other()
        shared = source
        shared.build()


def positive_walrus():
    shared: Box | None = None
    source = make_box()
    if source is not None and (source := other()):
        shared = source
        shared.build()


def match_none_return():
    shared: Box | None = None
    source = make_box()
    match source:
        case None:
            return
    shared = source
    shared.build()


def match_none_not_exiting():
    shared: Box | None = None
    source = make_box()
    match source:
        case None:
            print("none")
    shared = source
    shared.build()


def match_capture_rebinds(items):
    shared: Box | None = None
    source = make_box()
    if source is None:
        return
    match items:
        case [source]:
            pass
    shared = source
    shared.build()


def match_wildcard_first():
    shared: Box | None = None
    source = make_box()
    match source:
        case object():
            pass
        case None:
            return
    shared = source
    shared.build()


def match_other_case_rebinds():
    shared: Box | None = None
    source = make_box()
    match source:
        case None:
            return
        case Box():
            source = other()
    shared = source
    shared.build()


def try_after_handler_goes_on():
    shared: Box | None = None
    source = make_box()
    try:
        if source is None:
            return
    except ValueError:
        pass
    shared = source
    shared.build()


def match_guarded_none(flag):
    shared: Box | None = None
    source = make_box()
    match source:
        case None if flag:
            return
    shared = source
    shared.build()


def try_else():
    shared: Box | None = None
    source = make_box()
    try:
        if source is None:
            return
    except ValueError:
        pass
    else:
        shared = source
        shared.build()


def try_except_after_guard():
    shared: Box | None = None
    source = make_box()
    try:
        if source is None:
            return
    except ValueError:
        shared = source
        shared.build()


def try_finally_after_guard():
    shared: Box | None = None
    source = make_box()
    try:
        if source is None:
            return
    finally:
        shared = source
        shared.build()


def try_after_guard():
    shared: Box | None = None
    source = make_box()
    try:
        if source is None:
            return
    except ValueError:
        return
    shared = source
    shared.build()


def try_else_rebound():
    shared: Box | None = None
    source = make_box()
    try:
        if source is None:
            return
        source = other()
    except ValueError:
        pass
    else:
        shared = source
        shared.build()


def loop_rebinds(items):
    shared: Box | None = None
    source = make_box()
    if source is None:
        return
    for item in items:
        shared = source
        shared.build()
        source = other()


async def async_loop_rebinds(items):
    shared: Box | None = None
    source = make_box()
    if source is None:
        return
    async for source in items:
        shared = source
        shared.build()


def loop_target_rebinds(items):
    shared: Box | None = None
    source = make_box()
    if source is None:
        return
    for source in items:
        shared = source
        shared.build()


def except_star_rebinds():
    shared: Box | None = None
    source = make_box()
    if source is None:
        return
    try:
        pass
    except* ValueError as source:
        shared = source
        shared.build()


def case_star_rebinds(items):
    shared: Box | None = None
    source = make_box()
    if source is None:
        return
    match items:
        case [*source]:
            shared = source
            shared.build()


def case_rest_rebinds(items):
    shared: Box | None = None
    source = make_box()
    if source is None:
        return
    match items:
        case {**source}:
            shared = source
            shared.build()


def while_none_return():
    shared: Box | None = None
    source = make_box()
    while source is None:
        return
    shared = source
    shared.build()


def while_polls():
    shared: Box | None = None
    source = make_box()
    while not source:
        source = make_box()
    shared = source
    shared.build()


def while_inner_break(items):
    shared: Box | None = None
    source = make_box()
    while source is None:
        for item in items:
            break
        source = make_box()
    shared = source
    shared.build()


def while_breaks(flag):
    shared: Box | None = None
    source = make_box()
    while source is None:
        if flag:
            break
        source = make_box()
    shared = source
    shared.build()


def while_inner_else_breaks(items):
    shared: Box | None = None
    source = make_box()
    while source is None:
        for item in items:
            pass
        else:
            break
    shared = source
    shared.build()


def while_else_prints():
    shared: Box | None = None
    source = make_box()
    while source is None:
        source = make_box()
    else:
        print("found")
    shared = source
    shared.build()


def while_else_rebinds():
    shared: Box | None = None
    source = make_box()
    while source is None:
        source = make_box()
    else:
        source = other()
    shared = source
    shared.build()


def match_none_or_return():
    shared: Box | None = None
    source = make_box()
    match source:
        case None | int():
            return
    shared = source
    shared.build()


def match_none_true_guard():
    shared: Box | None = None
    source = make_box()
    match source:
        case None if True:
            return
    shared = source
    shared.build()


def match_none_false_guard():
    shared: Box | None = None
    source = make_box()
    match source:
        case None if 0:
            return
    shared = source
    shared.build()


def match_or_without_none():
    shared: Box | None = None
    source = make_box()
    match source:
        case int() | str():
            return
    shared = source
    shared.build()


def try_except_rebound():
    shared: Box | None = None
    source = make_box()
    if source is None:
        return
    try:
        source = other()
    except ValueError:
        shared = source
        shared.build()


def try_finally_rebound():
    shared: Box | None = None
    source = make_box()
    if source is None:
        return
    try:
        source = other()
    finally:
        shared = source
        shared.build()


def try_handler_rebound():
    shared: Box | None = None
    source = make_box()
    if source is None:
        return
    try:
        pass
    except ValueError:
        source = other()
    finally:
        shared = source
        shared.build()


def entered_test_walrus():
    shared: Box | None = None
    source = make_box()
    if source is None:
        return
    if (source := other()) or True:
        shared = source
        shared.build()


def entered_with_as(ctx):
    shared: Box | None = None
    source = make_box()
    if source is None:
        return
    with ctx as source:
        shared = source
        shared.build()


def entered_case_capture(items):
    shared: Box | None = None
    source = make_box()
    if source is None:
        return
    match items:
        case [source]:
            shared = source
            shared.build()


def entered_handler_name():
    shared: Box | None = None
    source = make_box()
    if source is None:
        return
    try:
        pass
    except ValueError as source:
        shared = source
        shared.build()
"""


@pytest.fixture(scope="module")
def linked(tmp_path_factory: pytest.TempPathFactory) -> set[str]:
    """The functions whose shared.build() is linked to Box.build."""

    _, edges = _index(Path(tmp_path_factory.mktemp("guards")), SOURCE)
    functions: set[str] = set()
    for edge in edges:
        if edge.resolution.status != "resolved":
            continue
        if edge.target != "method:lab.Box.build":
            continue
        facts = edge.properties.get("callsites") or [edge.properties.get("callsite")]
        if any(
            isinstance(fact, dict) and fact.get("receiver_expression") == "shared"
            for fact in facts
        ):
            functions.add(edge.source.rsplit(".", 1)[-1])
    return functions


def test_source_parses() -> None:
    compile(SOURCE, "guards.py", "exec")


@pytest.mark.parametrize(
    "name",
    [
        "assert_is_not_none",
        "assert_truthy",
        "assert_and",
        "positive_branch",
        "positive_not_equals",
        "positive_truthy",
        "positive_and",
        "else_of_absent",
        "elif_of_absent",
        "match_none_return",
        "try_else",
        "try_after_guard",
        "while_none_return",
        "while_polls",
        "while_inner_break",
        "while_else_prints",
        "match_none_or_return",
        "match_none_true_guard",
    ],
)
def test_a_value_proven_present_is_not_none(linked, name):
    assert name in linked


@pytest.mark.parametrize(
    "name",
    [
        # Only one operand of an or need hold.
        "assert_or",
        "positive_or",
        # The test proves the value is None.
        "assert_is_none",
        "body_of_absent",
        "else_of_present",
        # Rebound after the test, or in it.
        "positive_rebound",
        "positive_walrus",
        # The case does not leave, has a guard, or another case binds.
        "match_none_not_exiting",
        "match_guarded_none",
        "match_capture_rebinds",
        "match_wildcard_first",
        "match_other_case_rebinds",
        # A handler or finally may run before the guard in the body has.
        "try_except_after_guard",
        "try_finally_after_guard",
        "try_else_rebound",
        "try_after_handler_goes_on",
        # A handler or finally may run after the body, or a handler, rebound
        # the name.
        "try_except_rebound",
        "try_finally_rebound",
        "try_handler_rebound",
        # What enters the block binds the name.
        "entered_test_walrus",
        "entered_with_as",
        "entered_case_capture",
        "entered_handler_name",
        # A loop, or what it binds, rebinds the name; so does except*, a star
        # capture or a mapping rest.
        "loop_rebinds",
        "async_loop_rebinds",
        "loop_target_rebinds",
        "except_star_rebinds",
        "case_star_rebinds",
        "case_rest_rebinds",
        # A break ends the loop with the test still true; an else rebinds.
        "while_breaks",
        "while_inner_else_breaks",
        "while_else_rebinds",
        # The guard is false, or the pattern does not take None.
        "match_none_false_guard",
        "match_or_without_none",
    ],
)
def test_a_value_not_proven_present_keeps_none(linked, name):
    assert name not in linked
