"""A name takes the type of the value it was last assigned before a call.

The type analyzer records a type for an f-string, a slice, a boolean
expression of operands of one type and a conditional of arms of one type; the
call analyzer gives an f-string, a literal or a display called on directly the
type it would have if assigned first. A call through a name whose binding
there has no recorded type has no type, rather than that of another, typed
binding of the name: x: Box; x = flag; x.build() is not Box.build.
"""

from __future__ import annotations

import pytest

from arcgraph.tests.test_path_receiver_types import _resolutions_by_function

SOURCE = """from pathlib import Path


class Box:
    def build(self):
        return 1


def fstring(name: str):
    combined = f"/{name}"
    combined.casefold()


def fstring_direct(name: str):
    f"/{name}".casefold()


def str_literal_direct(items):
    ", ".join(items).strip()


def bytes_literal_direct():
    b"x".decode()


def number_literal_direct():
    (255).bit_length()
    (1.5).is_integer()


def generator_direct(xs):
    (y for y in xs).send(None)


def generator_assigned(xs):
    made = (y for y in xs)
    made.close()


def display_direct(x, xs):
    [].append(x)
    {}.get(x)
    {1}.add(x)
    (1, 2).count(x)
    [y for y in xs].sort()


def text_slice(text: str):
    part = text[2:]
    part.casefold()


def list_slice(items: list[str]):
    rest = items[1:]
    rest.append("x")


def inline_list_slice(items: list[str]):
    items[1:].append("x")


def list_element(items: list[str]):
    first = items[0]
    first.casefold()


def tuple_slice(pair: tuple[int, str]):
    rest = pair[1:]
    rest.count(1)


def tuple_slice_element(pair: tuple[int, str]):
    rest = pair[1:]
    # Its members are of two types, so no one element type follows.
    rest[0].bit_length()


def either(path: str):
    chosen = path or "/"
    chosen.casefold()


def mixed_or(path: str):
    chosen = path or 1
    chosen.casefold()


def rebound_or(path: str, other):
    path = other
    chosen = path or "/"
    chosen.casefold()


def defaulted_or(path: str = make()):
    chosen = path or "/"
    chosen.casefold()


def mixed_conditional(flag: bool, name: str):
    value = name if flag else 1
    value.casefold()


def untyped_overwrite(x: Box, flag):
    x = flag
    x.build()


def typed_parameter(x: Box):
    x.build()
"""


@pytest.fixture(scope="module")
def resolutions(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, set[tuple[str, str]]]:
    return _resolutions_by_function(tmp_path_factory.mktemp("assigned"), SOURCE)


def test_source_parses() -> None:
    compile(SOURCE, "assigned.py", "exec")


@pytest.mark.parametrize(
    ("name", "target", "strategy"),
    [
        ("fstring", "extsym:builtins.str.casefold", "builtin_receiver_type"),
        ("fstring_direct", "extsym:builtins.str.casefold", "builtin_receiver_type"),
        # The literal's join, not a guess by the method's name, and so the
        # str it returns.
        ("str_literal_direct", "extsym:builtins.str.join", "builtin_receiver_type"),
        ("str_literal_direct", "extsym:builtins.str.strip", "builtin_receiver_type"),
        (
            "bytes_literal_direct",
            "extsym:builtins.bytes.decode",
            "builtin_receiver_type",
        ),
        (
            "number_literal_direct",
            "extsym:builtins.int.bit_length",
            "builtin_receiver_type",
        ),
        (
            "number_literal_direct",
            "extsym:builtins.float.is_integer",
            "builtin_receiver_type",
        ),
        ("display_direct", "extsym:builtins.list.append", "builtin_receiver_type"),
        ("display_direct", "extsym:builtins.dict.get", "builtin_receiver_type"),
        ("display_direct", "extsym:builtins.set.add", "builtin_receiver_type"),
        ("display_direct", "extsym:builtins.tuple.count", "builtin_receiver_type"),
        ("display_direct", "extsym:builtins.list.sort", "builtin_receiver_type"),
        (
            "generator_direct",
            "extsym:types.GeneratorType.send",
            "external_receiver_type",
        ),
        (
            "generator_assigned",
            "extsym:types.GeneratorType.close",
            "external_receiver_type",
        ),
        ("text_slice", "extsym:builtins.str.casefold", "builtin_receiver_type"),
        # A slice of a list is a list, not an element.
        ("list_slice", "extsym:builtins.list.append", "builtin_receiver_type"),
        ("inline_list_slice", "extsym:builtins.list.append", "builtin_receiver_type"),
        ("list_element", "extsym:builtins.str.casefold", "builtin_receiver_type"),
        ("tuple_slice", "extsym:builtins.tuple.count", "builtin_receiver_type"),
        ("either", "extsym:builtins.str.casefold", "builtin_receiver_type"),
        ("typed_parameter", "method:lab.Box.build", "receiver_type"),
    ],
)
def test_assigned_values_are_typed(resolutions, name, target, strategy):
    assert (target, strategy) in resolutions.get(name, set()), sorted(
        resolutions.get(name, set())
    )


@pytest.mark.parametrize(
    ("name", "absent"),
    [
        # Operands or arms of different types give no single type.
        ("mixed_or", "extsym:builtins.str.casefold"),
        # A rewritten name, or a parameter whose default may not be a str,
        # does not hold the type of its annotation here.
        ("rebound_or", "extsym:builtins.str.casefold"),
        ("defaulted_or", "extsym:builtins.str.casefold"),
        ("mixed_conditional", "extsym:builtins.str.casefold"),
        # A slice is not the list's element.
        ("list_slice", "extsym:builtins.str.append"),
        ("tuple_slice_element", "extsym:builtins.int.bit_length"),
        # The value assigned last before the call is of no known type.
        ("untyped_overwrite", "method:lab.Box.build"),
    ],
)
def test_values_of_no_single_type_are_not_typed(resolutions, name, absent):
    assert not any(
        target == absent and strategy.endswith("receiver_type")
        for target, strategy in resolutions.get(name, set())
    ), sorted(resolutions.get(name, set()))


GUARDS = """class Box:
    def build(self):
        return 1


def make_box() -> Box | None:
    return None


def not_return(items):
    shared: Box | None = None
    for item in items:
        source = make_box()
        if not source:
            return
        if shared is None:
            shared = source
        elif shared.build():
            return


def is_none_raise():
    shared: Box | None = None
    source = make_box()
    if source is None:
        raise ValueError
    shared = source
    shared.build()


def or_continue(items):
    shared: Box | None = None
    for item in items:
        source = make_box()
        if item or not source:
            continue
        shared = source
        shared.build()


def none_is_raise():
    shared: Box | None = None
    source = make_box()
    if None is source:
        raise ValueError
    shared = source
    shared.build()


def equals_none_return():
    shared: Box | None = None
    source = make_box()
    if source == None:
        return
    shared = source
    shared.build()


def none_equals_return():
    shared: Box | None = None
    source = make_box()
    if None == source:
        return
    shared = source
    shared.build()


def is_not_none_return():
    shared: Box | None = None
    source = make_box()
    # Leaves when the value is present, so what follows may be None.
    if source is not None:
        return
    shared = source
    shared.build()


def not_equals_none_return():
    shared: Box | None = None
    source = make_box()
    if None != source:
        return
    shared = source
    shared.build()


def unguarded():
    shared: Box | None = None
    source = make_box()
    shared = source
    shared.build()


def guard_with_else():
    shared: Box | None = None
    source = make_box()
    if not source:
        return
    else:
        pass
    shared = source
    shared.build()


def guard_then_rebind():
    shared: Box | None = None
    source = make_box()
    if source is None:
        return
    source = make_box()
    shared = source
    shared.build()


def guard_outside_rebinding_loop(items):
    shared: Box | None = None
    source = make_box()
    if source is None:
        return
    for item in items:
        shared = source
        shared.build()
        source = make_box()


def guard_not_exiting():
    shared: Box | None = None
    source = make_box()
    if not source:
        print("missing")
    shared = source
    shared.build()


def walrus_guard():
    shared: Box | None = None
    source = make_box()
    # source is checked, then rebound inside the test to what may be None.
    if not source or print(source := make_box()):
        return
    shared = source
    shared.build()
"""


@pytest.fixture(scope="module")
def guards(tmp_path_factory: pytest.TempPathFactory) -> dict[str, set[tuple[str, str]]]:
    return _resolutions_by_function(tmp_path_factory.mktemp("guards"), GUARDS)


@pytest.mark.parametrize(
    "name",
    [
        "not_return",
        "is_none_raise",
        "or_continue",
        "none_is_raise",
        "equals_none_return",
        "none_equals_return",
    ],
)
def test_a_value_assigned_after_a_none_exit_is_not_none(guards, name):
    # A guard that leaves when the value is absent excludes None from what is
    # assigned after it, so the annotated union of the target is narrowed.
    assert ("method:lab.Box.build", "receiver_type") in guards.get(name, set())


@pytest.mark.parametrize(
    "name",
    [
        "unguarded",
        "guard_with_else",
        "guard_then_rebind",
        "guard_outside_rebinding_loop",
        "guard_not_exiting",
        "walrus_guard",
        "is_not_none_return",
        "not_equals_none_return",
    ],
)
def test_a_value_not_proven_present_keeps_none(guards, name):
    assert "method:lab.Box.build" not in {t for t, _ in guards.get(name, set())}
