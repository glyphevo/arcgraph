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
import sys

import pytest

from arcgraph.analyzers.stdlib_functions import CAPITALISED_STDLIB_FUNCTIONS
from arcgraph.analyzers.external_types import (
    EXTERNAL_METHODS_BY_TYPE,
    FUNCTION_RETURN_TYPES,
    LOWERCASE_STDLIB_CLASSES,
    METHOD_RETURN_TYPES,
)
from arcgraph.tests.test_path_receiver_types import _resolutions_by_function

SOURCE = """import datetime
import hashlib
import json
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path, PosixPath, PurePosixPath
from typing import Any, Optional

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


def pure_mkdir(pure: PurePosixPath) -> None:
    pure.mkdir()


def pure_parent_mkdir(pure: PurePosixPath) -> None:
    pure.parent.mkdir()


def str_mkdir(text: str) -> None:
    text.mkdir()


def str_named_conn(conn: str) -> None:
    conn.execute("SELECT 1").fetchall()


def untyped_elements(rules: list[str] | None) -> None:
    for rule in tuple(rules or ()):
        rule.endswith("/")


def constructed_set() -> None:
    set().add(1)
    set().mkdir()


class Accumulator:
    def add(self, value: int) -> None:
        pass


def stored_set(item: dict[str, Any]) -> None:
    item["targets"].add("x")


class Service:
    def get_view(self, plan_id: str) -> str:
        return plan_id


class Group:
    def _service(self) -> Service:
        return Service()

    def plan(self) -> str:
        return self._service().get_view("p")

    def plan_assigned(self) -> str:
        service = self._service()
        return service.get_view("p")


class ChildGroup(Group):
    def _service(self) -> Service:
        return Service()

    def parent_plan(self) -> str:
        return super()._service().get_view("p")


class Base:
    def explain(self) -> str:
        return ""


class Probe(Base):
    def explain(self) -> str:
        return super().explain()


class Failure(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)


class Reader:
    @classmethod
    def open(cls, path: str) -> "Reader":
        return cls()

    def diagnostics(self) -> list:
        return []


def read_back() -> list:
    return Reader.open("x").diagnostics()


class Resolution:
    def to_dict(self) -> dict:
        return {}


class Engine:
    def _scope(self, conn: Any) -> tuple[list[str], Optional[Resolution]]:
        return [], None

    def in_with(self) -> object:
        with open("x") as conn:
            ids, resolution = self._scope(conn)
            return resolution.to_dict() if resolution else None

    def in_branch(self, flag: bool) -> object:
        if flag:
            ids, resolution = self._scope(None)
            return resolution.to_dict()
        return None


def checked_text() -> None:
    subprocess.check_output(["echo"], text=True).decode()


def named_temp() -> None:
    tempfile.NamedTemporaryFile().read()


def sub_element(parent: ET.Element) -> None:
    ET.SubElement(parent, "child").set("a", "b")


def int_mkdir(count: int) -> None:
    count.mkdir()


def starred_paths(
    directories: tuple[Path, ...], files: tuple[Path, ...], root: Path
) -> None:
    for path in (*directories, *files):
        path.relative_to(root)


def mixed_elements(root: Path) -> None:
    for item in (root, "a"):
        item.exists()


def path_mkdir(path: Path) -> None:
    path.mkdir()


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


def test_a_known_type_without_the_method_links_nothing(resolutions):
    # A pure path has no mkdir and a str none either; no target, and no guess
    # from the method name, stands in for it.
    for name in ("pure_mkdir", "pure_parent_mkdir", "str_mkdir"):
        assert not any(
            target.endswith(".mkdir") for target in _targets(resolutions, name)
        ), name
    assert ("extsym:pathlib.Path.mkdir", "external_receiver_type") in (
        resolutions.get("path_mkdir", set())
    )
    # Nor does a name: a str called conn has no execute, so it is not taken
    # for a database connection, and its result is no cursor.
    assert not any(
        target.startswith("extsym:dbapi.")
        for target in _targets(resolutions, "str_named_conn")
    )


def test_external_methods_cover_every_public_method():
    for type_id, methods in EXTERNAL_METHODS_BY_TYPE.items():
        module_name, _, name = type_id.removeprefix("extsym:").rpartition(".")
        external_type = getattr(importlib.import_module(module_name), name)
        missing = sorted(
            method
            for method in dir(external_type)
            if not method.startswith("_")
            and callable(getattr(external_type, method))
            and method not in methods
        )
        assert missing == [], type_id


def test_an_element_of_unknown_type_is_not_its_container(resolutions):
    # tuple(...) has no element type, so rule is untyped and endswith is left
    # to the fallbacks an untyped receiver has; it was typed as the tuple
    # itself, which has no endswith, and lost the call.
    assert "extsym:builtins.str.endswith" in _targets(resolutions, "untyped_elements")


def test_a_builtin_constructor_gives_a_builtin_type(resolutions):
    constructed = resolutions.get("constructed_set", set())
    assert ("extsym:builtins.set.add", "builtin_receiver_type") in constructed
    # A set has no mkdir; as an external symbol it accepted any method name.
    assert not any(target.endswith(".mkdir") for target, _ in constructed)


def test_a_method_on_a_value_is_not_found_by_its_name_alone(resolutions):
    # item["targets"] is of no type; add is not the one add of the project.
    assert "method:lab.Accumulator.add" not in _targets(resolutions, "stored_set")


@pytest.mark.parametrize(
    ("name", "target", "strategy"),
    [
        ("plan", "method:lab.Service.get_view", "receiver_type"),
        ("plan_assigned", "method:lab.Service.get_view", "receiver_type"),
        ("explain", "method:lab.Base.explain", "super_receiver"),
        ("parent_plan", "method:lab.Group._service", "super_receiver"),
        ("parent_plan", "method:lab.Service.get_view", "receiver_type"),
        ("read_back", "method:lab.Reader.diagnostics", "receiver_type"),
    ],
)
def test_project_methods_resolve_through_their_class(
    resolutions, name, target, strategy
):
    assert (target, strategy) in resolutions.get(name, set()), sorted(
        resolutions.get(name, set())
    )


def test_super_of_an_external_base_names_no_builtins_super(resolutions):
    assert not any(
        target.startswith("extsym:builtins.super.")
        for target in _targets(resolutions, "__init__")
    )


def test_a_with_block_binds_unconditionally(resolutions):
    # A with block runs its body like the function around it, so a value bound
    # there is available to a later call in it; one bound under an if is not.
    assert ("method:lab.Resolution.to_dict", "receiver_type") in resolutions.get(
        "in_with", set()
    )
    assert "method:lab.Resolution.to_dict" not in _targets(resolutions, "in_branch")


def test_no_callee_types_a_value_it_does_not_return(resolutions):
    # check_output returns str with text=True and bytes without, so it has no
    # entry; NamedTemporaryFile and SubElement are functions capitalised like
    # classes, so their calls do not construct themselves.
    assert not any(
        target.startswith("extsym:builtins.bytes.")
        for target in _targets(resolutions, "checked_text")
    )
    for name, absent in (
        ("named_temp", "extsym:tempfile.NamedTemporaryFile.read"),
        ("sub_element", "extsym:xml.etree.ElementTree.SubElement.set"),
    ):
        assert absent not in _targets(resolutions, name), name
    # An int has no mkdir, and no guess stands in for it.
    assert not any(t.endswith(".mkdir") for t in _targets(resolutions, "int_mkdir"))


def test_capitalised_stdlib_functions():
    # Every name listed is a function on the interpreter that has it. Where the
    # list was generated, macOS, every capitalised function of a listed module
    # is listed too; os, ctypes and others differ by platform, so elsewhere a
    # platform's own capitalised functions (ctypes.WINFUNCTYPE on Windows) are
    # not checked, and modules not listed are not checked anywhere.
    modules: dict[str, set[str]] = {}
    for qualname in CAPITALISED_STDLIB_FUNCTIONS:
        module_name, _, name = qualname.rpartition(".")
        modules.setdefault(module_name, set()).add(name)
    for module_name, names in modules.items():
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        for name in names:
            value = getattr(module, name, None)
            if value is not None:
                assert callable(value) and not inspect.isclass(value), (
                    module_name,
                    name,
                )
        if sys.platform != "darwin":
            continue
        missing = sorted(
            name
            for name in dir(module)
            if name[:1].isupper()
            and callable(getattr(module, name))
            and not inspect.isclass(getattr(module, name))
            and getattr(getattr(module, name), "__module__", None) is not None
            and name not in names
        )
        assert missing == [], module_name


def test_a_display_iterates_the_type_its_elements_share(resolutions):
    assert ("extsym:pathlib.Path.relative_to", "external_receiver_type") in (
        resolutions.get("starred_paths", set())
    )
    # A path and a str share no type, so the element is untyped.
    assert ("extsym:pathlib.Path.exists", "external_receiver_type") not in (
        resolutions.get("mixed_elements", set())
    )
