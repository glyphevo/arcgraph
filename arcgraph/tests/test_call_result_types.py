"""A call's result is typed only by what is known about its callee.

A documented return, a constructed class or a boundary return types the value
a call gives. A function or method of undocumented return gives a value of no
type, and a callee guessed from its name types nothing; neither may name the
callee itself as the type, which produced targets that do not exist, such as
builtins.dict.get.get.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import subprocess
import sys

import pytest

from arcgraph.analyzers.stdlib_functions import CAPITALISED_STDLIB_FUNCTIONS
from arcgraph.analyzers.external_types import (
    EXTERNAL_METHODS_BY_TYPE,
    FUNCTION_RETURN_TYPES,
    LOWERCASE_STDLIB_CLASSES,
    METHOD_RETURN_TYPES,
    function_return_type,
)
from arcgraph.tests.test_path_receiver_types import _resolutions_by_function

SOURCE = """import array
import datetime
import hashlib
import json
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path, PosixPath, PurePosixPath
from subprocess import check_output
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


class Explicit(Group):
    def _service(self) -> Service:
        return Service()

    def explicit_plan(self) -> str:
        return super(Explicit, self)._service().get_view("p")

    @classmethod
    def explicit_cls(cls) -> Service:
        return super(Explicit, cls)._service()

    def explicit_other_class(self) -> Service:
        return super(Group, self)._service()

    def explicit_other_object(self, other: "Explicit") -> Service:
        return super(Explicit, other)._service()

    def shadowed_super(self, super: Any) -> Service:
        return super()._service()

    def unbound_super(self) -> Service:
        return super(Explicit)._service()

    def starred_super(self, *rest: Any) -> Service:
        return super(Explicit, *rest)._service()

    def dynamic_super(self) -> Service:
        return super(type(self), self)._service()

    @staticmethod
    def static_self(self: Group) -> Service:
        # In a static method self is an ordinary parameter, here a Group.
        return super(Explicit, self)._service()


class Deeper(Explicit):
    def _service(self) -> Service:
        return Service()

    def past_explicit(self) -> Service:
        # Past Explicit's own _service, to Group's; not past Deeper's.
        return super(Explicit, self)._service()


def explicit_outside(item: Explicit) -> Service:
    return super(Explicit, item)._service()


class Root:
    def ping(self) -> str:
        return "root"


class LeftPing(Root):
    def ping(self) -> str:
        return "left"


class RightPing(Root):
    def ping(self) -> str:
        return "right"


class BothPing(LeftPing, RightPing):
    pass


def past_left(item: BothPing) -> str:
    # BothPing is BothPing, LeftPing, RightPing, Root: past LeftPing is
    # RightPing's ping, not Root's, which LeftPing's own order gives.
    return super(LeftPing, item).ping()


def not_an_instance() -> str:
    return super(LeftPing, "text").ping()


def unrelated_super(item: BothPing) -> str:
    # Base is not in BothPing's order: super raises TypeError.
    return super(Base, item).ping()


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


def output_bytes() -> None:
    subprocess.check_output(["echo"]).decode()


def output_text(kw: dict) -> None:
    subprocess.check_output(["echo"], encoding="utf-8").casefold()
    out = subprocess.check_output(["echo"], universal_newlines=True)
    out.zfill(3)
    subprocess.check_output(["echo"], text=True, **kw).isdecimal()


def output_unknown(flag: bool, kw: dict) -> None:
    subprocess.check_output(["echo"], text=flag).upper()
    subprocess.check_output(["echo"], **kw).upper()


def output_raises() -> None:
    subprocess.check_output(["echo"], text=False, universal_newlines=True).upper()
    out = subprocess.check_output(["echo"], text=False, universal_newlines=True)
    out.upper()
    subprocess.check_output(["echo"], -1, None, None, None).decode()


def output_assigned() -> None:
    out = subprocess.check_output(["echo"])
    out.decode()


def hashed_assigned(data: bytes) -> None:
    digest = hashlib.sha256(data)
    digest.hexdigest()


def run_assigned() -> None:
    completed = subprocess.run(["echo"])
    completed.check_returncode()


def output_shadowed(subprocess) -> None:
    subprocess.check_output(["echo"]).upper()
    out = subprocess.check_output(["echo"])
    out.upper()


def output_rebound(make: Any) -> None:
    check_output = make()
    out = check_output(["echo"])
    out.upper()


def sha3_hash(data: bytes) -> None:
    hashlib.sha3_256(data).hexdigest().upper()


def shake_hash(data: bytes) -> None:
    hashlib.shake_128(data).hexdigest(8).upper()


def blake_hash(data: bytes) -> None:
    hashlib.blake2b(data).digest().hex()
    digest = hashlib.blake2s(data)
    digest.hexdigest().casefold()


def blake_lacks(data: bytes) -> None:
    hashlib.blake2b(data).mkdir()


def typed_array() -> None:
    array.array("i").tobytes()


def array_lacks() -> None:
    array.array("i").mkdir()
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
        ("unannotated_mapping", "extsym:collections.abc.Mapping.get.get"),
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
        ("output_bytes", "extsym:builtins.bytes.decode"),
        ("output_text", "extsym:builtins.str.casefold"),
        ("output_text", "extsym:builtins.str.zfill"),
        ("output_text", "extsym:builtins.str.isdecimal"),
        # The type analyzer reads the same table for an assigned call.
        ("output_assigned", "extsym:builtins.bytes.decode"),
        ("hashed_assigned", "extsym:_hashlib.HASH.hexdigest"),
        ("run_assigned", "extsym:subprocess.CompletedProcess.check_returncode"),
        ("sha3_hash", "extsym:_hashlib.HASH.hexdigest"),
        ("sha3_hash", "extsym:builtins.str.upper"),
        ("shake_hash", "extsym:_hashlib.HASHXOF.hexdigest"),
        ("shake_hash", "extsym:builtins.str.upper"),
        ("blake_hash", "extsym:_blake2.blake2b.digest"),
        ("blake_hash", "extsym:builtins.bytes.hex"),
        ("blake_hash", "extsym:_blake2.blake2s.hexdigest"),
        ("blake_hash", "extsym:builtins.str.casefold"),
        ("typed_array", "extsym:array.array.tobytes"),
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
    assert "protocol:pep249.Cursor.execute" in _targets(resolutions, "any_connection")


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
        target.startswith("protocol:pep249.")
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
        # super(C, obj) reaches the method after C's own, wherever it is
        # written and whatever obj is.
        ("explicit_plan", "method:lab.Group._service", "super_receiver"),
        ("explicit_plan", "method:lab.Service.get_view", "receiver_type"),
        ("explicit_cls", "method:lab.Group._service", "super_receiver"),
        ("explicit_other_object", "method:lab.Group._service", "super_receiver"),
        ("past_explicit", "method:lab.Group._service", "super_receiver"),
        ("explicit_outside", "method:lab.Group._service", "super_receiver"),
        ("past_left", "method:lab.RightPing.ping", "super_receiver"),
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
    # check_output returns str with text=True, which has no decode;
    # NamedTemporaryFile and SubElement are functions capitalised like
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


# Capitalised standard library names whose kind differs between the supported
# versions: a function ("function"), a class ("class"), a value that cannot be
# called ("value"), or not there ("absent"), on 3.11, 3.12, 3.13 and 3.14. They
# keep the reading they had on 3.11 and 3.12, listed or not, so the checks
# below pass over them; test_version_dependent_stdlib_names checks each kind on
# the running interpreter instead.
VERSION_DEPENDENT_STDLIB_NAMES = {
    "importlib.metadata._meta.Union": ("function", "function", "function", "class"),
    "importlib.resources.Anchor": ("absent", "function", "function", "value"),
    "importlib.resources.Package": ("function", "function", "function", "value"),
    "importlib.resources._common.Anchor": ("absent", "function", "function", "value"),
    "importlib.resources._common.Package": (
        "function",
        "function",
        "function",
        "value",
    ),
    "importlib.resources._common.Union": ("function", "function", "function", "class"),
    "importlib.resources.abc.StrPath": ("function", "function", "function", "value"),
    "importlib.resources.abc.Union": ("function", "function", "function", "class"),
    "multiprocessing.dummy.Lock": ("function", "function", "class", "class"),
    "threading.Lock": ("function", "function", "class", "class"),
    "typing.Annotated": ("class", "class", "function", "function"),
    "typing.Union": ("function", "function", "function", "class"),
}
SUPPORTED_MINOR_VERSIONS = ((3, 11), (3, 12), (3, 13), (3, 14))


def _stdlib_kind(qualname: str) -> str:
    module_name, _, name = qualname.rpartition(".")
    value = getattr(importlib.import_module(module_name), name, None)
    if value is None:
        return "absent"
    if inspect.isclass(value):
        return "class"
    return "function" if callable(value) else "value"


def test_version_dependent_stdlib_names():
    version = sys.version_info[:2]
    if version not in SUPPORTED_MINOR_VERSIONS:
        pytest.skip(f"no kinds recorded for Python {version[0]}.{version[1]}")
    index = SUPPORTED_MINOR_VERSIONS.index(version)
    actual = {
        qualname: _stdlib_kind(qualname) for qualname in VERSION_DEPENDENT_STDLIB_NAMES
    }
    expected = {
        qualname: kinds[index]
        for qualname, kinds in VERSION_DEPENDENT_STDLIB_NAMES.items()
    }
    assert actual == expected
    for qualname, kinds in VERSION_DEPENDENT_STDLIB_NAMES.items():
        # Only a name whose kind does differ needs the exemption.
        assert len(set(kinds) - {"absent"}) > 1, qualname


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
            if f"{module_name}.{name}" in VERSION_DEPENDENT_STDLIB_NAMES:
                continue
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
            and f"{module_name}.{name}" not in VERSION_DEPENDENT_STDLIB_NAMES
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


def test_check_output_types_by_text_mode_or_not_at_all(resolutions):
    # Text mode is unknown when a keyword is not a constant or a ** argument
    # may set it; text and universal_newlines that differ raise.
    typed = {"builtin_receiver_type", "external_receiver_type", "receiver_type"}
    for name in ("output_unknown", "output_shadowed", "output_rebound"):
        assert not any(
            strategy in typed for _, strategy in resolutions.get(name, set())
        ), (name, sorted(resolutions.get(name, set())))
    # A call that always raises gives no value: the next call is neither typed
    # nor guessed from its name.
    assert {t for t, _ in resolutions.get("output_raises", set())} == {
        "extsym:subprocess.check_output"
    }


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ('f(["a"])', "builtin:bytes"),
        ('f(args=["a"])', "builtin:bytes"),
        ('f(["a"], text=True)', "builtin:str"),
        ('f(["a"], universal_newlines=1)', "builtin:str"),
        ('f(["a"], encoding="utf-8")', "builtin:str"),
        ('f(["a"], errors="strict")', "builtin:str"),
        ('f(["a"], text=False, encoding=None)', "builtin:bytes"),
        ('f(["a"], encoding="")', "builtin:bytes"),
        ('f(["a"], input=b"x", timeout=1)', "builtin:bytes"),
        ('f(["a"], text=flag)', None),
        ('f(["a"], **kw)', None),
        ('f(["a"], text=True, **kw)', "builtin:str"),
        # bufsize, executable and stdin leave text mode as it is.
        ('f(["a"], -1)', "builtin:bytes"),
        ('f(["a"], -1, text=True)', "builtin:str"),
        ("f(*args)", "builtin:bytes"),
        ("f(*args, text=True)", "builtin:str"),
        # A fifth positional argument is stdout, which check_output sets.
        ('f(["a"], -1, None, None, None)', "typing:NoReturn"),
        # text and universal_newlines that differ raise SubprocessError.
        ('f(["a"], text=False, universal_newlines=True)', "typing:NoReturn"),
        ('f(["a"], text=1, universal_newlines="")', "typing:NoReturn"),
        ('f(["a"], text=True, universal_newlines=1)', "builtin:str"),
        ('f(["a"], text=None, universal_newlines=True)', "builtin:str"),
        ('f(["a"], text=flag, encoding="utf-8")', "builtin:str"),
    ],
)
def test_check_output_return_follows_its_text_keywords(call, expected):
    returned = function_return_type(
        "subprocess.check_output", ast.parse(call, mode="eval").body
    )
    assert (returned or {}).get("type_id") == expected


@pytest.mark.parametrize(
    "keywords",
    [
        {},
        {"text": True},
        {"universal_newlines": 1},
        {"encoding": "utf-8"},
        {"errors": "strict"},
        {"text": False, "encoding": None},
        {"encoding": ""},
        {"text": True, "universal_newlines": 1},
        {"text": None, "universal_newlines": True},
    ],
)
def test_check_output_rule_matches_the_runtime(keywords):
    call = "f(['a'], " + ", ".join(f"{k}={v!r}" for k, v in keywords.items()) + ")"
    returned = subprocess.check_output([sys.executable, "-c", ""], **keywords)
    expected = function_return_type(
        "subprocess.check_output", ast.parse(call, mode="eval").body
    )
    assert expected == {
        "type_id": "builtin:" + type(returned).__name__,
        "type_expression": type(returned).__name__,
    }


def test_documented_hash_types_are_the_runtime_types():
    # The analyzers name these types statically; a build whose hashlib takes a
    # hash from elsewhere fails here rather than silently naming other types.
    hashes = {
        qualname: type_id
        for qualname, (type_id, _) in FUNCTION_RETURN_TYPES.items()
        if qualname.startswith("hashlib.")
    }
    assert len(hashes) == 14
    for qualname, type_id in hashes.items():
        runtime = type(getattr(hashlib, qualname.removeprefix("hashlib."))())
        assert type_id == f"extsym:{runtime.__module__}.{runtime.__qualname__}"


def test_hash_and_array_types_link_no_method_they_lack(resolutions):
    for name in ("blake_lacks", "array_lacks"):
        assert not any(t.endswith(".mkdir") for t in _targets(resolutions, name)), name


def test_super_past_a_class_without_the_method_is_not_resolved(resolutions):
    # super(Group, self) looks past Group, whose bases have no _service; a
    # parameter named super is not the builtin; super(C) is unbound; a starred
    # or computed argument names no known class.
    for name in (
        "explicit_other_class",
        "shadowed_super",
        "unbound_super",
        "starred_super",
        "dynamic_super",
        "not_an_instance",
        "unrelated_super",
        "static_self",
    ):
        found = resolutions.get(name, set())
        assert not any(t.startswith("method:lab.") for t, _ in found), (name, found)
        assert not any(s == "super_receiver" for _, s in found), (name, found)


def test_super_names_its_class_by_import_and_only_by_a_stable_binding(tmp_path):
    from pathlib import Path

    from arcgraph.core.graph_store import GraphStoreReader
    from arcgraph.core.scanner import SourceRoot
    from arcgraph.pipeline.indexer import ArcGraphIndexer

    package = Path(tmp_path) / "repo" / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "base.py").write_text(
        "class Group:\n    def service(self):\n        return 1\n\n\n"
        "class Child(Group):\n    def service(self):\n        return 2\n",
        encoding="utf-8",
    )
    (package / "use.py").write_text(
        "from pkg.base import Child, Group\n\n\n"
        "class Twice(Group):\n    pass\n\n\n"
        "class Twice(Child):\n    pass\n\n\n"
        "def imported(item: Child):\n    return super(Child, item).service()\n\n\n"
        "def twice(item: Twice):\n    return super(Twice, item).service()\n\n\n"
        "def untyped(item):\n    return super(Child, item).service()\n",
        encoding="utf-8",
    )
    output = Path(tmp_path) / "out"
    ArcGraphIndexer(
        repo_root=package.parents[1],
        output_dir=output,
        source_roots=[SourceRoot("src")],
    ).build()
    targets = {
        (edge.source, edge.target)
        for edge in GraphStoreReader.from_current(output).read_edges()
        if edge.resolution.status == "resolved"
    }
    # Child is imported, and super(Child, item) reaches Group's service.
    assert ("fn:pkg.use.imported", "method:pkg.base.Group.service") in targets
    # Of an object of no known class, the order is not known.
    assert not any(
        source == "fn:pkg.use.untyped" and target.startswith("method:")
        for source, target in targets
    )
    # Twice is defined twice on different bases, so which class it names, and
    # so which service it reaches, is not settled.
    assert not any(
        source == "fn:pkg.use.twice" and target.startswith("method:")
        for source, target in targets
    )


@pytest.mark.parametrize(
    ("positional", "keywords", "raised"),
    [
        ((-1,), {}, None),
        ((-1, None, None, None), {}, TypeError),
        ((), {"text": False, "universal_newlines": True}, subprocess.SubprocessError),
    ],
)
def test_check_output_positionals_and_conflicts_match_the_runtime(
    positional, keywords, raised
):
    command = [sys.executable, "-c", ""]
    call = ast.parse(
        "f(c"
        + "".join(f", {value!r}" for value in positional)
        + "".join(f", {k}={v!r}" for k, v in keywords.items())
        + ")",
        mode="eval",
    ).body
    returned = function_return_type("subprocess.check_output", call)
    if raised is not None:
        with pytest.raises(raised):
            subprocess.check_output(command, *positional, **keywords)
        assert returned is not None
        assert returned["type_id"] == "typing:NoReturn"
    else:
        value = subprocess.check_output(command, *positional, **keywords)
        assert returned is not None
        assert returned["type_id"] == "builtin:" + type(value).__name__
