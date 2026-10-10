"""Runtime guards keep generic branches; nullable defaults keep one value type."""

import pytest

from arcgraph.analyzers.type_unions import guarded_value_type, type_identity
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.tests.test_flow_attribute_chains import _calls


@pytest.mark.parametrize(
    "annotation,guard,expression,target",
    [
        ("Path | list[str]", "list", "x[0].upper()", "extsym:builtins.str.upper"),
        ("str | list[Path]", "list", "x[0].exists()", "extsym:pathlib.Path.exists"),
        (
            "tuple[Path, ...] | str",
            "tuple",
            "x[0].exists()",
            "extsym:pathlib.Path.exists",
        ),
        (
            "dict[str, Path] | str",
            "dict",
            "x['p'].exists()",
            "extsym:pathlib.Path.exists",
        ),
        ("list[Path] | None", "list", "x[0].exists()", "extsym:pathlib.Path.exists"),
        ("list[Path]", "list", "x[0].exists()", "extsym:pathlib.Path.exists"),
    ],
)
def test_isinstance_keeps_selected_generic_value(
    tmp_path, annotation, guard, expression, target
):
    calls = _calls(
        tmp_path,
        f"from pathlib import Path\ndef use(x: {annotation}):\n"
        f"    if isinstance(x, {guard}):\n        {expression}\n",
    )
    assert calls.get(expression) == {target}
    edges = GraphStoreReader.from_current(tmp_path / "out").read_edges()
    assert not any(
        any(
            site.get("call_expression") == expression
            and site.get("resolution_strategy") == "common_boundary_method"
            for site in edge.properties.get("callsites")
            or [edge.properties.get("callsite", {})]
        )
        for edge in edges
    )


@pytest.mark.parametrize(
    "annotation,guard",
    [
        ("Path | list[str]", "list"),
        ("Path | str", "list"),
        ("list[Path] | list[str]", "list"),
        ("list[Path | str] | str", "list"),
        ("tuple[Path, str] | str", "tuple"),
        ("object", "list"),
        ("list[Path] | object", "list"),
    ],
)
@pytest.mark.parametrize("assigned", [False, True])
def test_narrowing_cannot_guess_an_unknown_or_incompatible_element(
    tmp_path, annotation, guard, assigned
):
    expression = "result.exists()" if assigned else "x[0].exists()"
    body = "result=x[0]\n        result.exists()" if assigned else expression
    calls = _calls(
        tmp_path,
        f"from pathlib import Path\ndef use(x: {annotation}):\n"
        f"    if isinstance(x, {guard}):\n        {body}\n",
    )
    assert expression not in calls


@pytest.mark.parametrize("assigned", [False, True])
def test_narrowed_container_unknown_method_result_is_not_guessed(tmp_path, assigned):
    body = "result=x.pop()\n        result.exists()" if assigned else "x.pop().exists()"
    calls = _calls(
        tmp_path,
        "def use(x: object):\n    if isinstance(x,list):\n        " + body + "\n",
    )
    assert ("result.exists()" if assigned else "x.pop().exists()") not in calls


def test_isinstance_base_does_not_select_one_subclass_union_member(tmp_path):
    calls = _calls(
        tmp_path,
        "class Base:\n    def work(self): pass\n"
        "class Left(Base): pass\nclass Right(Base): pass\n"
        "def use(x: Left | Right):\n"
        "    if isinstance(x, Base):\n        x.work()\n",
    )
    assert "x.work()" not in calls


@pytest.mark.parametrize("annotation", ["Base | Child", "Child | Base"])
def test_exact_base_member_is_not_unique_when_a_subclass_also_matches(
    tmp_path, annotation
):
    calls = _calls(
        tmp_path,
        "class Base:\n    def work(self): pass\nclass Child(Base): pass\n"
        + f"def use(x: {annotation}):\n    if isinstance(x, Base):\n        x.work()\n",
    )
    assert "x.work()" not in calls


@pytest.mark.parametrize("annotation,target", [("int | bool", "int")])
def test_builtin_subclass_alternatives_are_not_disjoint(tmp_path, annotation, target):
    calls = _calls(
        tmp_path,
        f"def use(x: {annotation}):\n    if isinstance(x, {target}):\n        x.bit_length()\n",
    )
    assert "x.bit_length()" not in calls


def test_stdlib_subtype_guard_has_one_concrete_non_generic_result(tmp_path):
    calls = _calls(
        tmp_path,
        "from datetime import date,datetime\ndef use(x: date | datetime | str):\n"
        "    if isinstance(x,date):\n        if isinstance(x,datetime):\n            x.date()\n",
    )
    assert calls.get("x.date()") == {"extsym:datetime.datetime.date"}


def test_none_exclusion_keeps_existing_untyped_mapping_iteration(tmp_path):
    calls = _calls(
        tmp_path,
        "from typing import Mapping\ndef use(headers: Mapping[str,str] | None):\n"
        "    if headers is not None:\n        result=[k.lower() for k,v in headers.items()]\n",
    )
    assert calls.get("k.lower()") == {"extsym:builtins.str.lower"}


def test_matching_branch_keeps_nested_type_parameters_and_metadata():
    chosen = {
        "type_id": "builtin:list",
        "origin": "list",
        "type_expression": "list[dict[str, Path]]",
        "type_args": [
            {
                "type_id": "builtin:dict",
                "type_args": [
                    {"type_id": "builtin:str"},
                    {"type_id": "extsym:pathlib.Path"},
                ],
            }
        ],
    }
    original = {
        "origin": "Union",
        "type_id": "extsym:pathlib.Path",
        "type_args": [{"type_id": "extsym:pathlib.Path"}, chosen],
        "binding_id": "binding:source",
    }
    result = guarded_value_type(
        {
            "receiver_narrowings": [
                {
                    "name": "x",
                    "type_id": "builtin:list",
                    "check": [2, 0],
                    "range": [3, 0, 4, 99],
                }
            ]
        },
        "x",
        (3, 4),
        original,
        [],
    )
    assert type_identity(result) == type_identity(chosen)
    assert result["origin"] == chosen["origin"]
    assert result["type_expression"] == chosen["type_expression"]
    assert result["binding_id"] == "binding:source"
    assert result["narrowed_receiver"]
    assert original["type_args"][1] == chosen


HEADER_CLASSES = (
    "class Headers:\n    def items(self): return []\n    def touch(self): pass\n"
    "class Other:\n    def items(self): return []\n    def touch(self): pass\n"
)


@pytest.mark.parametrize("fallback", ["Headers()", "other"])
@pytest.mark.parametrize("assigned", [False, True])
def test_optional_same_class_default_propagates(tmp_path, fallback, assigned):
    body = (
        f"result = headers or {fallback}\n    result.items()"
        if assigned
        else f"(headers or {fallback}).items()"
    )
    calls = _calls(
        tmp_path,
        HEADER_CLASSES
        + "def use(headers: Headers | None, other: Headers):\n    "
        + body
        + "\n",
    )
    assert calls.get(
        "result.items()" if assigned else f"(headers or {fallback}).items()"
    ) == {"method:pkg.user.Headers.items"}


def test_constructor_nullable_default_is_a_concrete_field(tmp_path):
    calls = _calls(
        tmp_path,
        HEADER_CLASSES + "class Upload:\n"
        "    def __init__(self, headers: Headers | None = None):\n"
        "        self.headers = headers or Headers()\n"
        "def use(value: object):\n"
        "    if isinstance(value, Upload):\n        value.headers.items()\n",
    )
    assert calls.get("value.headers.items()") == {"method:pkg.user.Headers.items"}


@pytest.mark.parametrize(
    "annotation,body",
    [
        ("Headers | None", "result = headers or Other()\n    result.touch()"),
        ("Headers | None", "result = headers or unknown()\n    result.touch()"),
        ("Headers | Other | None", "result = headers or Headers()\n    result.touch()"),
        (
            "Headers | None",
            "headers = other\n    result = headers or Headers()\n    result.touch()",
        ),
        ("object", "result = headers or Headers()\n    result.touch()"),
        ("Headers", "result = headers or Other()\n    result.touch()"),
    ],
)
def test_nullable_default_does_not_hide_unknown_or_distinct_types(
    tmp_path, annotation, body
):
    calls = _calls(
        tmp_path,
        HEADER_CLASSES + f"def use(headers: {annotation}, other):\n    " + body + "\n",
    )
    assert "result.touch()" not in calls


def test_nonnullable_same_type_uses_existing_operand_rule(tmp_path):
    calls = _calls(
        tmp_path,
        HEADER_CLASSES
        + "def use(headers: Headers):\n    result=headers or Headers()\n    result.items()\n",
    )
    assert calls.get("result.items()") == {"method:pkg.user.Headers.items"}


def test_optional_default_does_not_confuse_container_argument_types(tmp_path):
    calls = _calls(
        tmp_path,
        "from pathlib import Path\ndef use(x: list[Path] | None, other: list[str]):\n"
        "    result=x or other\n    result[0].exists()\n",
    )
    assert "result[0].exists()" not in calls
