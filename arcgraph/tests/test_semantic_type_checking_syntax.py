"""Pyright's branch pruning and definition bindings are syntax-sensitive."""

import pytest

from arcgraph.tests.test_semantic_implementation_conflicts import mark


@pytest.mark.parametrize("workspace", [False, True], ids=["dependency", "workspace"])
@pytest.mark.parametrize(
    "imports, condition, pruned",
    [
        ("from bridge import TYPE_CHECKING", "TYPE_CHECKING", False),
        ("import bridge as b", "b.TYPE_CHECKING", False),
        ("from bridge import typing as types", "types.TYPE_CHECKING", False),
        ("from typing import TYPE_CHECKING as TC", "not TC", False),
        ("import typing as t", "t.TYPE_CHECKING", True),
        ("import typing_extensions as te", "te.TYPE_CHECKING", True),
        ("from typing import TYPE_CHECKING", "TYPE_CHECKING", True),
    ],
)
def test_only_direct_typing_syntax_prunes_branches(
    tmp_path, workspace, imports, condition, pruned
):
    before, after = mark(
        tmp_path,
        {
            "impl.py": "def run(): pass\n",
            "other.py": "def run(): pass\n",
            "bridge.py": "from typing import TYPE_CHECKING\nimport typing\n",
            "pkg.py": imports
            + "\nif "
            + condition
            + ":\n from impl import run\nelse:\n from other import run\n",
        },
        "extsym:pkg.run",
        ["extsym:impl.run"],
        workspace=workspace,
    )
    if pruned:
        assert after == before
    else:
        assert after["resolution"]["status"] == "conflict"
        assert "conditional_binding" in after["resolution"]["detail"]


@pytest.mark.parametrize("workspace", [False, True], ids=["dependency", "workspace"])
@pytest.mark.parametrize(
    "condition",
    [
        "TYPE_CHECKING or (run := replacement)",
        "not TYPE_CHECKING and (run := replacement)",
        "True or (run := replacement)",
        "False and (run := replacement)",
    ],
)
@pytest.mark.parametrize("reimport", [False, True], ids=["keep-import", "reimport"])
def test_skipped_walrus_is_a_possible_static_binding(
    tmp_path, workspace, condition, reimport
):
    _, after = mark(
        tmp_path,
        {
            "impl.py": "def run(): pass\n",
            "pkg.py": "from typing import TYPE_CHECKING\nfrom impl import run\nif "
            + condition
            + ":\n pass\n"
            + ("from impl import run\n" if reimport else ""),
        },
        "extsym:pkg.run",
        ["extsym:impl.run"],
        workspace=workspace,
    )
    assert after["resolution"]["status"] == "conflict"
    assert ("multiple_bindings" if reimport else "assignment_binding") in after[
        "resolution"
    ]["detail"]
