"""A call's result is typed only by what is known about its callee.

A documented return, a constructed class or a boundary return types the value
a call gives. A function or method of undocumented return gives a value of no
type, and a callee guessed from its name types nothing; neither may name the
callee itself as the type, which produced targets that do not exist, such as
builtins.dict.get.get.
"""

from __future__ import annotations

import importlib
import inspect

import pytest

from arcgraph.analyzers.external_types import (
    FUNCTION_RETURN_TYPES,
    LOWERCASE_STDLIB_CLASSES,
    METHOD_RETURN_TYPES,
)
from arcgraph.tests.test_path_receiver_types import _resolutions_by_function

SOURCE = """import datetime
import hashlib
import json
from pathlib import Path, PosixPath, PurePosixPath
from typing import Any

from sqlalchemy import select


def undocumented_function(text: str) -> None:
    json.loads(text).get("a")


def unannotated_mapping(data) -> None:
    data.get("a").get("b")


def str_chain(text: str) -> None:
    text.strip().lower()


def split_element(text: str) -> None:
    text.split(",")[0].strip()


def read_lines(path: Path) -> None:
    path.read_text().splitlines()


def bytes_chain(data: bytes) -> None:
    data.decode().strip()


def pure_flavour(path: PurePosixPath) -> None:
    path.with_suffix(".txt").as_posix().upper()


def concrete_flavour(path: PosixPath) -> None:
    path.resolve().as_uri()


def guessed_receiver(text) -> None:
    text.strip().lower()


def set_by_default(index: dict[str, set[str]], name: str) -> None:
    index.setdefault(name, set()).add(name)


def get_with_default(names: dict[str, str]) -> None:
    names.get("a", "").strip()


def constructed_path() -> None:
    Path("a").resolve().as_posix()


def constructed_datetime() -> None:
    datetime.datetime(2020, 1, 1).isoformat()


def hashed(data: bytes) -> None:
    hashlib.sha256(data).hexdigest().upper()


def dumped(value: dict) -> None:
    json.dumps(value).encode()


def stamped() -> None:
    datetime.datetime.now().isoformat().replace("T", " ")


def any_connection(conn: Any) -> None:
    conn.execute("SELECT 1").fetchone()


def any_value(value: Any) -> None:
    value.get("a").strip()


def select_statement(model: type) -> None:
    select(model).where(True)


def assigned_chain(text: str) -> None:
    stripped = text.strip()
    stripped.lower()
    parts = text.split(",")
    parts.append("x")
"""


@pytest.fixture(scope="module")
def resolutions(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, set[tuple[str, str]]]:
    return _resolutions_by_function(tmp_path_factory.mktemp("results"), SOURCE)


def _targets(resolutions: dict[str, set[tuple[str, str]]], name: str) -> set[str]:
    return {target for target, _ in resolutions.get(name, set())}


@pytest.mark.parametrize(
    ("name", "absent"),
    [
        ("undocumented_function", "extsym:json.loads.get"),
        ("unannotated_mapping", "extsym:builtins.mapping.get.get"),
        ("guessed_receiver", "extsym:builtins.str.strip.lower"),
    ],
)
def test_undocumented_or_guessed_callees_name_no_type(resolutions, name, absent):
    assert absent not in _targets(resolutions, name)


def test_a_guessed_callee_types_nothing(resolutions):
    # text has no type, so strip is guessed from its name and its result is not
    # typed: lower is not linked by type.
    assert not any(
        strategy in {"builtin_receiver_type", "external_receiver_type"}
        for _, strategy in resolutions.get("guessed_receiver", set())
    )


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("str_chain", "extsym:builtins.str.lower"),
        ("split_element", "extsym:builtins.str.strip"),
        ("read_lines", "extsym:builtins.str.splitlines"),
        ("bytes_chain", "extsym:builtins.str.strip"),
        ("pure_flavour", "extsym:builtins.str.upper"),
        ("pure_flavour", "extsym:pathlib.PurePosixPath.as_posix"),
        ("concrete_flavour", "extsym:pathlib.PosixPath.as_uri"),
        ("set_by_default", "extsym:builtins.set.add"),
        ("get_with_default", "extsym:builtins.str.strip"),
        ("constructed_path", "extsym:pathlib.Path.as_posix"),
        ("constructed_datetime", "extsym:datetime.datetime.isoformat"),
        ("select_statement", "extsym:sqlalchemy.sql.Select.where"),
        ("hashed", "extsym:_hashlib.HASH.hexdigest"),
        ("hashed", "extsym:builtins.str.upper"),
        ("dumped", "extsym:builtins.str.encode"),
        ("stamped", "extsym:datetime.datetime.isoformat"),
        ("stamped", "extsym:builtins.str.replace"),
        ("assigned_chain", "extsym:builtins.str.lower"),
        ("assigned_chain", "extsym:builtins.list.append"),
    ],
)
def test_documented_returns_type_the_next_call(resolutions, name, target):
    # Linked by the receiver's type, not guessed from the method name.
    typed = {"builtin_receiver_type", "external_receiver_type", "receiver_type"}
    assert any(
        found == target and strategy in typed
        for found, strategy in resolutions.get(name, set())
    ), (name, sorted(resolutions.get(name, set())))


def test_documented_return_owners_name_real_methods():
    for (owner, method), _ in METHOD_RETURN_TYPES.items():
        module_name, _, class_name = (
            owner.removeprefix("extsym:").rpartition(".")
            if owner.startswith("extsym:")
            else ("builtins", "", owner.removeprefix("builtin:"))
        )
        owner_class = getattr(importlib.import_module(module_name), class_name)
        assert callable(getattr(owner_class, method, None)), (owner, method)


def test_documented_functions_exist():
    for qualname in FUNCTION_RETURN_TYPES:
        module_name, _, name = qualname.rpartition(".")
        owner = (
            getattr(importlib.import_module("datetime"), "datetime")
            if module_name == "datetime.datetime"
            else importlib.import_module(module_name)
        )
        assert callable(getattr(owner, name, None)), qualname


def test_no_target_holds_source_text(resolutions):
    # A call on a module function's value is not a symbol of that module.
    for name in ("hashed", "dumped", "stamped", "constructed_datetime"):
        assert not any("(" in target for target in _targets(resolutions, name)), name


def test_lowercase_stdlib_classes_are_classes():
    for qualname in LOWERCASE_STDLIB_CLASSES:
        module_name, _, name = qualname.rpartition(".")
        assert inspect.isclass(getattr(importlib.import_module(module_name), name))


def test_source_parses() -> None:
    # A source that does not parse would drop every case from the index.
    compile(SOURCE, "results.py", "exec")


def test_any_names_no_type(resolutions):
    for name in ("any_connection", "any_value"):
        assert not any(
            target.startswith("extsym:typing.Any")
            for target in _targets(resolutions, name)
        ), name
    # With Any read as no type, conn keeps the database boundary its name gives.
    assert "extsym:dbapi.Cursor.fetchone" in _targets(resolutions, "any_connection")
