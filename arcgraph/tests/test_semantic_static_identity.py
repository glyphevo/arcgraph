"""Identity follows static declarations, including their explicit write limits."""

import textwrap

import pytest

from arcgraph.semantic.implementation import ImplementationProof
from arcgraph.semantic.snapshot import capture
from arcgraph.tests.test_semantic_implementation_conflicts import mark


@pytest.mark.parametrize(
    "write",
    [
        "run = replacement",
        "run += replacement",
        "def read(value=(run := replacement)): pass",
        "class Other(object, flag=(run := replacement)): pass",
        "del run",
        "from other import run",  # Rebind the selected name to a different declaration.
        "for run in values: pass",
        "with context() as run: pass",
        "try:\n pass\nexcept Exception as run:\n pass",
        "if (run := replacement): pass",
        "match value:\n case run: pass",
        "def change():\n global run\n run = replacement",
    ],
    ids=[
        "assign",
        "augment",
        "default-walrus",
        "class-keyword-walrus",
        "delete",
        "import",
        "for",
        "with",
        "except",
        "walrus",
        "match",
        "global",
    ],
)
def test_static_namespace_rebinding_prevents_proof(tmp_path, write):
    _, after = mark(
        tmp_path,
        {
            "impl.py": "def run(): pass\n",
            "other.py": "def run(): pass\n",
            "pkg.py": "from impl import run\n" + write + "\n",
        },
        "extsym:pkg.run",
        ["extsym:impl.run"],
    )
    assert after["resolution"]["status"] == "conflict"


def test_static_nonlocal_binding_prevents_enclosing_namespace_proof(tmp_path):
    source = tmp_path / "live"
    source.mkdir()
    text = "def outer():\n from impl import run\n def change():\n  nonlocal run\n  run = replacement\n"
    (source / "pkg.py").write_text(text, encoding="utf-8")
    root = tmp_path / "frozen"
    snapshot = capture(source, ["pkg.py"], root, target_python="3.12", platform="linux")
    snapshot = snapshot.model_copy(update={"module_names": (("pkg.py", "pkg"),)})
    proof = ImplementationProof(root, snapshot)
    result = proof.binding("pkg", proof.module("pkg")[0].body[0].body, "run", ())
    assert result[0] == "unknown"
    assert "nested_namespace_write" in result[1]


@pytest.mark.parametrize("receiver", ["Child", "alias"], ids=["direct", "alias-chain"])
@pytest.mark.parametrize(
    "write",
    [
        "{r}.get = replacement",
        "del {r}.get",
        "setattr({r}, 'get', replacement)",
        "delattr({r}, 'get')",
        "builtins.setattr({r}, 'get', replacement)",
        "set_member({r}, 'get', replacement)",
    ],
    ids=[
        "assign",
        "delete",
        "setattr",
        "delattr",
        "qualified-builtin",
        "builtin-alias",
    ],
)
def test_static_class_member_write_prevents_proof(tmp_path, receiver, write):
    _, after = mark(
        tmp_path,
        {
            "base.py": "class Base:\n def get(self): pass\n",
            "pkg.py": "from base import Base\nimport builtins\nclass Child(Base): pass\n"
            "first = Child\nalias = first\nset_member = builtins.setattr\n"
            + write.format(r=receiver)
            + "\n",
        },
        "extsym:pkg.Child.get",
        ["extsym:base.Base.get"],
    )
    assert after["resolution"]["status"] == "conflict"


@pytest.mark.parametrize(
    "write",
    [
        "alias.run = replacement",
        "del alias.run",
        "setattr(alias, 'run', replacement)",
        "delattr(alias, 'run')",
    ],
)
def test_static_module_member_write_in_importer_prevents_proof(tmp_path, write):
    _, after = mark(
        tmp_path,
        {
            "impl.py": "def run(): pass\n",
            "pkg.py": "from impl import run\nimport impl\nfirst = impl\nalias = first\n"
            + write
            + "\n",
        },
        "extsym:pkg.run",
        ["extsym:impl.run"],
    )
    assert after["resolution"]["status"] == "conflict"


@pytest.mark.parametrize(
    "write",
    [
        "def patch(target):\n setattr(target, 'get', replacement)",
        "setattr(Child, name, replacement)",
        "def patch(target):\n delattr(target, 'get')",
        "delattr(Child, name)",
        "class Other:\n def mutate(self):\n  self.get = replacement",
        "Child.__dict__['get'] = replacement",
        "vars(Child)['get'] = replacement",
        "globals()['Child'] = Base",
        "locals()['Child'] = Base",
        "exec('Child.get = replacement')",
        "eval(\"exec('Child.get = replacement')\")",
        "import importlib, base\nimportlib.reload(base)",
        "Child.__bases__ = (Other,)",
        "Child.__class__ = Meta",
        "type.__setattr__(Child, 'get', replacement)",
        "type.__delattr__(Child, 'get')",
        "class Other:\n def get(self): pass\nOther.get = replacement",
        "def patch(Child):\n Child.get = replacement",  # Parameter shadows class.
        "def setattr(*args): pass\nsetattr(Child, 'get', replacement)",
    ],
    ids=[
        "unknown-setattr-receiver",
        "unknown-setattr-name",
        "unknown-delattr-receiver",
        "unknown-delattr-name",
        "instance",
        "dict",
        "vars",
        "globals",
        "locals",
        "exec",
        "eval",
        "reload",
        "bases",
        "class",
        "type-setattr",
        "type-delattr",
        "sibling-class",
        "shadowed-receiver",
        "shadowed-builtin",
    ],
)
def test_static_model_ignores_runtime_or_unresolved_member_writes(tmp_path, write):
    # This deliberately proves static declarations, not runtime object identity.
    before, after = mark(
        tmp_path,
        {
            "base.py": "class Base:\n def get(self): pass\n",
            "pkg.py": "from base import Base\nclass Child(Base): pass\n"
            + textwrap.dedent(write)
            + "\n",
        },
        "extsym:pkg.Child.get",
        ["extsym:base.Base.get"],
    )
    assert after == before


@pytest.mark.parametrize("suffix", ["metaclass=Meta", "decorator"])
def test_static_model_ignores_metaclass_and_decorator_effects(tmp_path, suffix):
    declaration = (
        "@deco\nclass Child(Base): pass\n"
        if suffix == "decorator"
        else "class Child(Base, metaclass=Meta): pass\n"
    )
    before, after = mark(
        tmp_path,
        {
            "base.py": "class Base:\n def get(self): pass\n",
            "pkg.py": "from base import Base\n" + declaration,
        },
        "extsym:pkg.Child.get",
        ["extsym:base.Base.get"],
    )
    assert after == before


def test_global_declaration_without_binding_keeps_static_identity(tmp_path):
    before, after = mark(
        tmp_path,
        {
            "impl.py": "def run(): pass\n",
            "pkg.py": "from impl import run\ndef read():\n global run\n return run\n",
        },
        "extsym:pkg.run",
        ["extsym:impl.run"],
    )
    assert after == before


@pytest.mark.parametrize(
    "expression",
    ["[run for run in values]", "lambda run: run", "lambda: (run := replacement)"],
)
def test_nested_local_bindings_keep_static_module_identity(tmp_path, expression):
    before, after = mark(
        tmp_path,
        {
            "impl.py": "def run(): pass\n",
            "pkg.py": "from impl import run\nother = " + expression + "\n",
        },
        "extsym:pkg.run",
        ["extsym:impl.run"],
    )
    assert after == before


def test_required_keyword_only_parameter_does_not_break_static_scope_scan(tmp_path):
    before, after = mark(
        tmp_path,
        {"impl.py": "def run(*, required): pass\n", "pkg.py": "from impl import run\n"},
        "extsym:pkg.run",
        ["extsym:impl.run"],
    )
    assert after == before


@pytest.mark.parametrize(
    "write", ["alias.get = replacement", "set_member(alias, 'get', replacement)"]
)
def test_static_aliases_can_cross_frozen_imports(tmp_path, write):
    _, after = mark(
        tmp_path,
        {
            "base.py": "class Base:\n def get(self): pass\n",
            "pkg.py": "from base import Base\nclass Child(Base): pass\n",
            "aliases.py": "from pkg import Child\nfrom builtins import setattr\nalias = Child\nset_member = setattr\n",
            "facade.py": "from aliases import alias, set_member\nfrom pkg import Child\n"
            + write
            + "\n",
        },
        "extsym:facade.Child.get",
        ["extsym:base.Base.get"],
    )
    assert after["resolution"]["status"] == "conflict"


def test_static_dotted_module_receiver_prevents_proof(tmp_path):
    _, after = mark(
        tmp_path,
        {
            "impl/__init__.py": "",
            "impl/sub.py": "def run(): pass\n",
            "pkg.py": "from impl.sub import run\nimport impl.sub\nimpl.sub.run = replacement\n",
        },
        "extsym:pkg.run",
        ["extsym:impl.sub.run"],
    )
    assert after["resolution"]["status"] == "conflict"


@pytest.mark.parametrize(
    "expression,conflict",
    [
        ("[Child for Child in values]\nChild.get = replacement", True),
        ("[setattr(Child, 'get', replacement) for Child in instances]", False),
        ("[setattr(Child, 'get', replacement) for item in values]", True),
        ("lambda Child: setattr(Child, 'get', replacement)", False),
    ],
)
def test_static_comprehension_and_lambda_receivers_respect_local_scope(
    tmp_path, expression, conflict
):
    before, after = mark(
        tmp_path,
        {
            "base.py": "class Base:\n def get(self): pass\n",
            "pkg.py": "from base import Base\nclass Child(Base): pass\nother = "
            + expression
            + "\n",
        },
        "extsym:pkg.Child.get",
        ["extsym:base.Base.get"],
    )
    assert (after["resolution"]["status"] == "conflict") is conflict
    if not conflict:
        assert after == before


@pytest.mark.parametrize(
    "declaration",
    [
        "def read(value=setattr(Child, 'get', replacement)): pass",
        "def read(*, value=delattr(Child, 'get')): pass",
        "class Other(object, flag=setattr(Child, 'get', replacement)): pass",
        "@decorate(setattr(Child, 'get', replacement))\ndef read(): pass",
    ],
    ids=["default", "keyword-default", "class-keyword", "decorator-expression"],
)
def test_explicit_static_member_write_in_declaration_expression_prevents_proof(
    tmp_path, declaration
):
    # Ignore implicit decorator/metaclass effects, but inspect explicit writes
    # in their source expressions in the enclosing lexical namespace.
    _, after = mark(
        tmp_path,
        {
            "base.py": "class Base:\n def get(self): pass\n",
            "pkg.py": "from base import Base\nclass Child(Base): pass\n"
            + declaration
            + "\n",
        },
        "extsym:pkg.Child.get",
        ["extsym:base.Base.get"],
    )
    assert after["resolution"]["status"] == "conflict"


@pytest.mark.parametrize(
    "write",
    ["Parent.get = replacement", "alias = Parent\nsetattr(alias, 'get', replacement)"],
)
def test_static_target_condition_keeps_selected_write_receiver(tmp_path, write):
    _, after = mark(
        tmp_path,
        {
            "base.py": "class Base:\n def get(self): pass\n",
            "pkg.py": "import sys\nif sys.version_info >= (3, 10):\n"
            " from base import Base as Parent\nelse:\n from missing import Other as Parent\n"
            "class Child(Parent): pass\n" + write + "\n",
        },
        "extsym:pkg.Child.get",
        ["extsym:base.Base.get"],
    )
    assert after["resolution"]["status"] == "conflict"
    assert "static_member_write:" in after["resolution"]["detail"]
