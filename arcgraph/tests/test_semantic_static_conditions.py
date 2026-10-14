"""Static branches and repeated imports select pyright's declaration identity."""

import pytest

from arcgraph.semantic.implementation import ImplementationProof
from arcgraph.semantic.snapshot import capture
from arcgraph.tests.test_semantic_implementation_conflicts import mark


@pytest.mark.parametrize("workspace", [False, True], ids=["dependency", "workspace"])
@pytest.mark.parametrize(
    "imports, condition",
    [
        ("from typing import TYPE_CHECKING", "TYPE_CHECKING"),
        ("import typing", "typing.TYPE_CHECKING"),
        ("from typing_extensions import TYPE_CHECKING", "TYPE_CHECKING"),
        ("import typing_extensions", "typing_extensions.TYPE_CHECKING"),
        ("from typing import TYPE_CHECKING as TC", "TC"),
        ("import typing as t", "t.TYPE_CHECKING"),
        ("from typing_extensions import TYPE_CHECKING as TC", "TC"),
        ("import typing_extensions as t", "t.TYPE_CHECKING"),
        ("from bridge import TC as check", "check"),
        ("import bridge as b", "b.TC"),
        ("from bridge import t as types", "types.TYPE_CHECKING"),
        ("from typing import *", "TYPE_CHECKING"),
    ],
)
def test_type_checking_imports_choose_static_branch(
    tmp_path, imports, condition, workspace
):
    before, after = mark(
        tmp_path,
        {
            "impl.py": "def run(): pass\n",
            "other.py": "def run(): pass\n",
            "bridge.py": "from typing import TYPE_CHECKING as TC\nimport typing as t\n",
            "typing.py": "TYPE_CHECKING = False\n__all__ = ['TYPE_CHECKING']\n",
            "pkg.py": imports
            + "\nif "
            + condition
            + ":\n from impl import run\nelse:\n from other import run\n",
        },
        "extsym:pkg.run",
        ["extsym:impl.run"],
        workspace=workspace,
    )
    # Pyright 1.1.414 retains both branches for a renamed sentinel or a
    # cross-module re-export, even when definition resolves to typing.pyi.
    if imports in (
        "from typing import TYPE_CHECKING as TC",
        "from typing_extensions import TYPE_CHECKING as TC",
        "from bridge import TC as check",
        "import bridge as b",
        "from bridge import t as types",
    ):
        assert after["resolution"]["status"] == "conflict"
        assert "conditional_binding" in after["resolution"]["detail"]
    else:
        assert after == before


@pytest.mark.parametrize(
    "condition, selected",
    [
        ("TYPE_CHECKING or unknown()", "impl"),
        ("not TYPE_CHECKING and unknown()", "other"),
        ("not TYPE_CHECKING", "other"),
        ("TYPE_CHECKING and unknown()", None),
        ("not TYPE_CHECKING or unknown()", None),
        ("(not TYPE_CHECKING and unknown()) or TYPE_CHECKING", "impl"),
        ("TYPE_CHECKING and sys.version_info >= (3, 12)", "impl"),
        ("TYPE_CHECKING and sys.platform == 'win32'", "other"),
        ("sys.version_info < (3, 0) and unknown()", "other"),
        ("sys.platform == 'linux' or unknown()", "impl"),
        ("True or unknown()", "impl"),
        ("False and unknown()", "other"),
    ],
)
def test_short_circuit_combines_type_checking_and_target(tmp_path, condition, selected):
    before, after = mark(
        tmp_path,
        {
            "impl.py": "def run(): pass\n",
            "other.py": "def run(): pass\n",
            "pkg.py": "import sys\nfrom typing import TYPE_CHECKING\nif "
            + condition
            + ":\n from impl import run\nelse:\n from other import run\n",
        },
        "extsym:pkg.run",
        ["extsym:" + (selected or "impl") + ".run"],
    )
    if selected is None:
        assert after["resolution"]["status"] == "conflict"
        assert "conditional_binding" in after["resolution"]["detail"]
    else:
        assert after == before


@pytest.mark.parametrize(
    "imports, condition",
    [
        ("TYPE_CHECKING = unknown", "TYPE_CHECKING"),
        ("from typing import TYPE_CHECKING\nTYPE_CHECKING = unknown", "TYPE_CHECKING"),
        ("import typing\ntyping = unknown", "typing.TYPE_CHECKING"),
        ("from other import TYPE_CHECKING", "TYPE_CHECKING"),
        ("if unknown():\n from typing import TYPE_CHECKING", "TYPE_CHECKING"),
        ("from cycle import TYPE_CHECKING", "TYPE_CHECKING"),
        ("import sys\nsys = unknown", "sys.platform == 'linux'"),
    ],
)
def test_unresolved_or_shadowed_condition_stays_conditional(
    tmp_path, imports, condition
):
    _, after = mark(
        tmp_path,
        {
            "impl.py": "def run(): pass\n",
            "other.py": "TYPE_CHECKING = True\n",
            "cycle.py": "from cycle2 import TYPE_CHECKING\n",
            "cycle2.py": "from cycle import TYPE_CHECKING\n",
            "pkg.py": imports + "\nif " + condition + ":\n from impl import run\n",
        },
        "extsym:pkg.run",
        ["extsym:impl.run"],
    )
    assert after["resolution"]["status"] == "conflict"
    assert "conditional_binding" in after["resolution"]["detail"]


def test_not_type_checking_skips_second_definition(tmp_path):
    source = tmp_path / "live"
    source.mkdir()
    text = "from typing import TYPE_CHECKING\ndef run(): pass\nif not TYPE_CHECKING:\n def run(): pass\n"
    (source / "pkg.py").write_text(text, encoding="utf-8")
    root = tmp_path / "frozen"
    snapshot = capture(source, ["pkg.py"], root, target_python="3.12", platform="linux")
    snapshot = snapshot.model_copy(update={"module_names": (("pkg.py", "pkg"),)})
    proof = ImplementationProof(root, snapshot)
    identity, reason = proof.identity("extsym:pkg.run")
    assert reason is None
    assert identity[2] == (2, 0, 2, 15)


@pytest.mark.parametrize("workspace", [False, True], ids=["dependency", "workspace"])
@pytest.mark.parametrize("same", [True, False], ids=["same-class", "different-classes"])
def test_two_star_imports_compare_class_declarations(tmp_path, workspace, same):
    before, after = mark(
        tmp_path,
        {
            "pkg/__init__.py": "from .definition import *\nfrom .alias import *\n",
            "pkg/definition.py": "class Item:\n def run(self): pass\n",
            "pkg/alias.py": (
                "from .definition import Item\n"
                if same
                else "class Item:\n def run(self): pass\n"
            ),
        },
        "extsym:pkg.Item.run",
        ["extsym:pkg.definition.Item.run"],
        workspace=workspace,
    )
    if same:
        assert after == before
    else:
        assert after["resolution"]["status"] == "conflict"
        assert "multiple_bindings" in after["resolution"]["detail"]


def test_unknown_write_before_import_still_prevents_proof(tmp_path):
    _, after = mark(
        tmp_path,
        {
            "impl.py": "def run(): pass\n",
            "pkg.py": "run = unknown\nfrom impl import run\n",
        },
        "extsym:pkg.run",
        ["extsym:impl.run"],
    )
    assert after["resolution"]["status"] == "conflict"
    assert "multiple_bindings" in after["resolution"]["detail"]


def test_condition_evaluation_stops_before_unreachable_operand(tmp_path):
    # A cyclic import on the skipped side must consume neither proof work nor IO.
    before, after = mark(
        tmp_path,
        {
            "impl.py": "def run(): pass\n",
            "cycle.py": "from cycle import TC\n",
            "pkg.py": "from typing import TYPE_CHECKING\nfrom cycle import TC\n"
            "if TYPE_CHECKING or TC:\n from impl import run\n",
        },
        "extsym:pkg.run",
        ["extsym:impl.run"],
    )
    assert after == before


@pytest.mark.parametrize("second", ["impl", "alias", "other", "missing"])
def test_repeated_named_imports_compare_declarations(tmp_path, second):
    before, after = mark(
        tmp_path,
        {
            "impl.py": "def run(): pass\n",
            "alias.py": "from impl import run\n",
            "other.py": "def run(): pass\n",
            "pkg.py": "from impl import run\nfrom " + second + " import run\n",
        },
        "extsym:pkg.run",
        ["extsym:impl.run"],
    )
    if second in ("impl", "alias"):
        assert after == before
    else:
        assert after["resolution"]["status"] == "conflict"
        assert "multiple_bindings" in after["resolution"]["detail"]


def test_unavailable_star_before_explicit_import_keeps_known_binding(tmp_path):
    # An unavailable wildcard is not a statically established write to run.
    before, after = mark(
        tmp_path,
        {
            "impl.py": "def run(): pass\n",
            "pkg.py": "from missing import *\nfrom impl import run\n",
        },
        "extsym:pkg.run",
        ["extsym:impl.run"],
    )
    assert after == before


@pytest.mark.parametrize(
    "condition, conflict",
    [
        ("TYPE_CHECKING or (run := replacement)", True),
        ("not TYPE_CHECKING and (run := replacement)", True),
        ("(run := replacement) or TYPE_CHECKING", True),
    ],
)
def test_short_circuit_only_counts_evaluated_name_writes(tmp_path, condition, conflict):
    # Despite runtime short-circuiting, pyright's definition list includes the
    # skipped walrus binding (review-g definitions-g and alias-extra-g).
    before, after = mark(
        tmp_path,
        {
            "impl.py": "def run(): pass\n",
            "pkg.py": "from typing import TYPE_CHECKING\nfrom impl import run\nif "
            + condition
            + ":\n pass\n",
        },
        "extsym:pkg.run",
        ["extsym:impl.run"],
    )
    assert (after["resolution"]["status"] == "conflict") is conflict
    if not conflict:
        assert after == before
