"""Known attribute values remain typed behind the attribute-chain barrier."""

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.tests.test_generator_and_dynamic_exports import _targets


def _assert_binding_type(tmp_path, name, type_id):
    node = next(
        n
        for n in GraphStoreReader.from_current(tmp_path / "out").read_nodes()
        if n.id == "fn:pkg.user.use"
    )
    assert any(
        r.get("name") == name and r.get("type_id") == type_id
        for r in node.properties.get("type_refs", [])
    )


@pytest.mark.parametrize("assigned", [False, True])
def test_path_suffix_is_text_in_inline_and_assigned_chains(tmp_path, assigned):
    body = (
        "value=p.suffix\n    value.lstrip('.').lower()"
        if assigned
        else "p.suffix.lstrip('.').lower()"
    )
    targets = _targets(
        tmp_path,
        {"user": "from pathlib import Path\ndef use(p: Path):\n    " + body + "\n"},
        v2=True,
    )
    assert {"extsym:builtins.str.lstrip", "extsym:builtins.str.lower"} <= targets
    if assigned:
        _assert_binding_type(tmp_path, "value", "builtin:str")


@pytest.mark.parametrize("assigned", [False, True])
def test_enum_name_is_text_without_guessing_its_value(tmp_path, assigned):
    body = (
        "name=k.name\n    name.replace('_',' ')"
        if assigned
        else "k.name.replace('_',' ')"
    )
    targets = _targets(
        tmp_path,
        {
            "user": "from enum import Enum\nclass Kind(Enum): A_B=1\ndef use(k: Kind):\n    "
            + body
            + "\n"
        },
        v2=True,
    )
    assert "extsym:builtins.str.replace" in targets
    if assigned:
        _assert_binding_type(tmp_path, "name", "builtin:str")


@pytest.mark.parametrize(
    "source",
    [
        "from fastapi import Request\ndef use(request: Request): request.headers.get('embedded', '').lower()\n",
        "from starlette.requests import Request\ndef use(request: Request): request.headers.get('embedded', '').lower()\n",
        "import httpx\ndef use(response: httpx.Response): response.headers.get('embedded', '').lower()\n",
        "import httpx\nclass Client(httpx.Client): pass\ndef use(client: Client): client.headers.get('embedded', '').lower()\n",
    ],
)
def test_documented_header_values_are_string_mappings(tmp_path, source):
    targets = _targets(tmp_path, {"user": source}, v2=True)
    assert {
        "extsym:collections.abc.Mapping.get",
        "extsym:builtins.str.lower",
    } <= targets


@pytest.mark.parametrize("module", ["pydantic", "pydantic_core.core_schema"])
def test_validation_data_is_a_dictionary(tmp_path, module):
    targets = _targets(
        tmp_path,
        {
            "user": f"from {module} import ValidationInfo\ndef use(info: ValidationInfo): info.data.get('field')\n"
        },
        v2=True,
    )
    assert "extsym:builtins.dict.get" in targets


@pytest.mark.parametrize("assigned", [False, True])
def test_simple_getter_exposes_its_known_field(tmp_path, assigned):
    body = (
        "value=dirs.TEMP_DIR\n    value.mkdir()"
        if assigned
        else "dirs.TEMP_DIR.mkdir()"
    )
    targets = _targets(
        tmp_path,
        {
            "user": "from pathlib import Path\nclass Dirs:\n    def __init__(self,p: Path): self._temp=p\n    @property\n    def TEMP_DIR(self): return self._temp\ndef use(dirs: Dirs):\n    "
            + body
            + "\n"
        },
        v2=True,
    )
    assert "extsym:pathlib.Path.mkdir" in targets
    if assigned:
        _assert_binding_type(tmp_path, "value", "extsym:pathlib.Path")


@pytest.mark.parametrize("assigned", [False, True])
def test_inherited_fields_are_not_missing_attributes(tmp_path, assigned):
    body = (
        "value=child.directories\n    value.BACKUP_DIR.mkdir()"
        if assigned
        else "child.directories.BACKUP_DIR.mkdir()"
    )
    targets = _targets(
        tmp_path,
        {
            "user": "from pathlib import Path\nclass Dirs:\n    BACKUP_DIR: Path\nclass Parent:\n    directories: Dirs\nclass Child(Parent): pass\ndef use(child: Child):\n    "
            + body
            + "\n"
        },
        v2=True,
    )
    assert "extsym:pathlib.Path.mkdir" in targets
    if assigned:
        _assert_binding_type(tmp_path, "value", "class:pkg.user.Dirs")


def test_annotated_class_factory_result_survives_name_guard(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "user": "class BaseMigrator:\n    def migrate(self): pass\nclass Child(BaseMigrator): pass\ndef use(key):\n    table: dict[str, type[BaseMigrator]]={'child': Child}\n    constructor=table.get(key)\n    if constructor is None: raise ValueError()\n    migrator=constructor()\n    migrator.migrate()\n"
        },
        v2=True,
    )
    assert "method:pkg.user.BaseMigrator.migrate" in targets


@pytest.mark.parametrize(
    "source",
    [
        "class Matcher: pass\nclass User:\n    def __init__(self): self.matcher=Matcher()\ndef use(user: User): user.matcher.inner.find('x')\n",
        "from enum import Enum\nclass Kind(Enum):\n    name=1\ndef use(k: Kind): k.name.replace('_',' ')\n",
        "from pathlib import Path\nclass Child(Path):\n    suffix=1\ndef use(p: Child): p.suffix.lstrip('.')\n",
        "from pathlib import Path\nclass Dirs:\n    @property\n    def TEMP_DIR(self):\n        if flag: return Path('.')\n        return 1\ndef use(d: Dirs): d.TEMP_DIR.mkdir()\n",
        "class Box:\n    def migrate(self): pass\ndef use(factory):\n    migrator=factory()\n    migrator.migrate()\n",
    ],
)
def test_unproved_or_shadowed_attribute_values_stay_unknown(tmp_path, source):
    targets = _targets(tmp_path, {"user": source}, v2=True)
    assert not targets & {
        "extsym:builtins.bytes.find",
        "extsym:builtins.str.replace",
        "extsym:builtins.str.lstrip",
        "extsym:pathlib.Path.mkdir",
        "method:pkg.user.Box.migrate",
    }


@pytest.mark.parametrize(
    "prefix",
    [
        "if flag: import httpx\nelse: import httpx",
        "try: import httpx\nexcept ImportError: import httpx",
    ],
)
def test_finite_base_with_one_owner_targets_definition(tmp_path, prefix):
    targets = _targets(
        tmp_path,
        {
            "user": prefix
            + "\nclass Client(httpx.Client): pass\ndef use(client: Client): client.get('/')\n"
        },
        v2=True,
    )
    assert "extsym:httpx.Client.get" in targets
    assert "extsym:pkg.user.Client.get" not in targets


@pytest.mark.parametrize("name", ["client", "test_client"])
def test_different_external_definition_owners_remain_unknown(tmp_path, name):
    targets = _targets(
        tmp_path,
        {
            "user": "try: import httpx2 as httpx\nexcept ImportError: import httpx\nclass Client(httpx.Client): pass\n"
            + f"def use({name}: Client): {name}.get('/')\n"
        },
        v2=True,
    )
    assert not any(t.endswith(".get") for t in targets)
    nodes = GraphStoreReader.from_current(tmp_path / "out").read_nodes()
    assert any(
        n.kind == "diagnostic"
        and n.properties.get("reason") == "ambiguous_external_definition"
        and set(n.properties.get("definition_candidates", []))
        == {"extsym:httpx.Client.get", "extsym:httpx2.Client.get"}
        for n in nodes
    )


def test_reassigned_client_headers_use_current_binding(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "user": "try: import httpx2 as httpx\nexcept ImportError: import httpx\nclass Client(httpx.Client): pass\ndef use():\n    client=Client()\n    client.headers.get('first')\n    client=Client()\n    client.headers.get('second')\n"
        },
        v2=True,
    )
    assert "extsym:collections.abc.Mapping.get" in targets
    edges = GraphStoreReader.from_current(tmp_path / "out").read_edges()
    edge = next(
        e
        for e in edges
        if e.source == "fn:pkg.user.use"
        and e.target == "extsym:collections.abc.Mapping.get"
    )
    assert len(edge.properties.get("callsites", [])) == 2


def test_factory_class_reexport_with_explicit_none_default(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "base": "class BaseMigrator:\n    def migrate(self): pass\n",
            "first": "from .base import BaseMigrator\n",
            "second": "from .base import BaseMigrator\n",
            "export": "from .first import *\nfrom .second import *\n",
            "user": "from .export import BaseMigrator\ndef use(key):\n    table: dict[str, type[BaseMigrator]]={}\n    constructor=table.get(key, None)\n    if constructor is None: raise ValueError()\n    migrator=constructor()\n    migrator.migrate()\n",
        },
        v2=True,
    )
    assert "method:pkg.base.BaseMigrator.migrate" in targets


@pytest.mark.parametrize(
    "provider",
    [
        "from .base import BaseMigrator\n__all__=[]\n",
        "from .base import BaseMigrator\n__all__=['BaseMigrator']\ndef clear():\n    global __all__\n    __all__=[]\nclear()\n",
        "from .base import BaseMigrator\nBaseMigrator=object\n",
    ],
)
def test_factory_reexport_requires_runtime_class_binding(tmp_path, provider):
    targets = _targets(
        tmp_path,
        {
            "base": "class BaseMigrator:\n    def migrate(self): pass\n",
            "first": provider,
            "export": "from .first import *\n",
            "user": "from .export import BaseMigrator\ndef use(key):\n    table: dict[str,type[BaseMigrator]]={}\n    constructor=table.get(key,None)\n    migrator=constructor()\n    migrator.migrate()\n",
        },
        v2=True,
    )
    assert "method:pkg.base.BaseMigrator.migrate" not in targets


@pytest.mark.parametrize("annotation", ["type[A | B]", "type[Any]"])
def test_class_factory_does_not_choose_an_unknown_or_mixed_result(tmp_path, annotation):
    targets = _targets(
        tmp_path,
        {
            "user": "from typing import Any\nclass A:\n    def migrate(self): pass\nclass B:\n    def migrate(self): pass\n"
            + f"def use(factory: {annotation}):\n    item=factory()\n    item.migrate()\n"
        },
        v2=True,
    )
    assert not targets & {"method:pkg.user.A.migrate", "method:pkg.user.B.migrate"}


def test_rebound_type_name_is_not_a_constructor_contract(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "user": "class BaseMigrator:\n    def migrate(self): pass\ntype=unknown\ndef use(factory: type[BaseMigrator]):\n    item=factory()\n    item.migrate()\n"
        },
        v2=True,
    )
    assert "method:pkg.user.BaseMigrator.migrate" not in targets


def test_finite_external_base_does_not_override_a_local_header_field(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "user": "try: import httpx2 as httpx\nexcept ImportError: import httpx\nclass Client(httpx.Client):\n    headers=unknown\ndef use(client: Client): client.headers.get('field')\n"
        },
        v2=True,
    )
    assert "extsym:collections.abc.Mapping.get" not in targets


def test_custom_wrapper_does_not_preserve_a_simple_getter(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "user": "from pathlib import Path\ndef wrap(fn): return lambda self: 1\nclass Dirs:\n    def __init__(self, p: Path): self._temp=p\n    @property\n    @wrap\n    def TEMP_DIR(self): return self._temp\ndef use(dirs: Dirs): dirs.TEMP_DIR.mkdir()\n"
        },
        v2=True,
    )
    assert "extsym:pathlib.Path.mkdir" not in targets


def test_header_mapping_does_not_promise_arbitrary_members_or_default_types(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "user": "from starlette.requests import Request\ndef use(request: Request):\n    request.headers.made_up()\n    request.headers.get('field', 42).lower()\n"
        },
        v2=True,
    )
    assert not targets & {
        "extsym:collections.abc.Mapping.made_up",
        "extsym:builtins.str.lower",
    }


@pytest.mark.parametrize(
    "binding",
    [
        "BaseMigrator=object\n",
        "def change():\n    global BaseMigrator\n    BaseMigrator=object\n",
    ],
)
def test_factory_argument_must_still_name_its_declared_class(tmp_path, binding):
    targets = _targets(
        tmp_path,
        {
            "user": "class BaseMigrator:\n    def migrate(self): pass\n"
            + binding
            + "def use(factory: type[BaseMigrator]):\n    item=factory()\n    item.migrate()\n"
        },
        v2=True,
    )
    assert "method:pkg.user.BaseMigrator.migrate" not in targets


@pytest.mark.parametrize("concrete", [False, True])
def test_awaited_field_is_result_not_awaitable(tmp_path, concrete):
    argument = "Box" if concrete else "T"
    targets = _targets(
        tmp_path,
        {
            "user": "from collections.abc import Awaitable\nfrom typing import TypeVar\nT=TypeVar('T')\nclass Box:\n    def close(self): pass\nclass Wrapper:\n    def __init__(self, aw: Awaitable["
            + argument
            + "]): self.aw=aw\n    async def enter(self): self.entered=await self.aw\ndef use(w: Wrapper): w.entered.close()\n"
        },
        v2=True,
    )
    assert "extsym:collections.abc.Awaitable.close" not in targets
    assert ("method:pkg.user.Box.close" in targets) == concrete


@pytest.mark.parametrize(
    ("annotation", "expression", "target"),
    [
        ("ast.ClassDef", "node.name.lower()", "extsym:builtins.str.lower"),
        (
            "ast.ImportFrom",
            "node.module.startswith('pkg')",
            "extsym:builtins.str.startswith",
        ),
    ],
)
def test_known_ast_text_fields_are_not_unrelated_method_guesses(
    tmp_path, annotation, expression, target
):
    targets = _targets(
        tmp_path,
        {
            "user": f"import ast\ndef use(node: {annotation}):\n    return {expression}\n"
        },
        v2=True,
    )
    assert target in targets


@pytest.mark.parametrize("assigned", [False, True])
def test_path_name_is_text_without_method_name_fallback(tmp_path, assigned):
    body = (
        "    value=p.name\n    return value.lower()"
        if assigned
        else "    return p.name.lower()"
    )
    targets = _targets(
        tmp_path,
        {"user": "from pathlib import Path\ndef use(p: Path):\n" + body + "\n"},
        v2=True,
    )
    assert "extsym:builtins.str.lower" in targets
    if assigned:
        _assert_binding_type(tmp_path, "value", "builtin:str")
