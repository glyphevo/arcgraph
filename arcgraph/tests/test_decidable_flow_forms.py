"""Small decidable control-flow forms keep imports and non-None evidence."""

import pytest

from arcgraph.core.scanner import FileScanner, SourceRoot
from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer
from arcgraph.tests.test_generator_and_dynamic_exports import _targets


@pytest.mark.parametrize(
    "prefix",
    [
        "while False:\n    break\nelse:\n    from os.path import join\n",
        "match 1:\n    case _ if True:\n        from os.path import join\n",
        "if flag:\n    from pkg.one import *\nelse:\n    from pkg.two import *\n",
    ],
)
def test_decidable_import_paths_keep_common_binding(tmp_path, prefix):
    targets = _targets(
        tmp_path,
        {
            "one": "from os.path import join\n",
            "two": "from os.path import join\n",
            "user": "flag=True\n" + prefix + "def use(): return join('a','b')\n",
        },
        v2=True,
    )
    assert "extsym:os.path.join" in targets


@pytest.mark.parametrize(
    "two,earlier,expected",
    [
        ("__all__=[]\n", "", False),
        ("__all__=[]\n", "from os.path import join\n", True),
        ("__all__=[]\n", "def join(*args): pass\n", False),
        ("from os.path import split as join\n", "", False),
    ],
)
def test_star_branch_missing_or_different_export_retains_prior_uncertainty(
    tmp_path, two, earlier, expected
):
    targets = _targets(
        tmp_path,
        {
            "one": "from os.path import join\n",
            "two": two,
            "user": earlier
            + "if flag:\n    from pkg.one import *\nelse:\n    from pkg.two import *\ndef use(): return join('a','b')\n",
        },
        v2=True,
    )
    assert ("extsym:os.path.join" in targets) is expected


@pytest.mark.parametrize(
    "guard",
    [
        "not source is None",
        "(source is not None and flag) or (source is not None and other)",
        "not (source is None or (flag and other))",
    ],
)
def test_nested_none_tests_keep_shared_value_type(tmp_path, guard):
    source = (
        "class Box:\n    def build(self): pass\ndef make() -> Box | None: return Box()\ndef use(flag, other):\n    shared: Box | None = None\n    source=make()\n    if "
        + guard
        + ":\n        shared=source\n        shared.build()\n"
    )
    targets = _targets(tmp_path, {"user": source}, v2=True)
    assert "method:pkg.user.Box.build" in targets


@pytest.mark.parametrize(
    "guard",
    [
        "(source is not None and flag) or other",
        "not (source is None and flag)",
        "source is not None and (source := unknown())",
    ],
)
def test_nested_none_tests_do_not_ignore_unknown_paths_or_writes(tmp_path, guard):
    source = (
        "class Box:\n    def build(self): pass\ndef make() -> Box | None: return Box()\ndef use(flag, other):\n    shared: Box | None = None\n    source=make()\n    if "
        + guard
        + ":\n        shared=source\n        shared.build()\n"
    )
    targets = _targets(tmp_path, {"user": source}, v2=True)
    assert "method:pkg.user.Box.build" not in targets


@pytest.mark.parametrize(
    "source,expected",
    [
        ("unused=1; from os.path import join; result=join('a','b')\n", True),
        ("result=join('a','b'); from os.path import join\n", False),
    ],
)
def test_same_line_module_import_is_available_only_after_its_statement(
    tmp_path, source, expected
):
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    graph = PythonGraphAnalyzer().analyze(
        FileScanner(tmp_path, [SourceRoot(".")]).scan()
    )
    assert (
        any(
            e.target == "extsym:os.path.join" and e.resolution.status == "resolved"
            for e in graph.edges
        )
        is expected
    )


@pytest.mark.parametrize(
    "expression",
    [
        "source is not None and (source := unknown())",
        "source is not None or other()",
        "not (source is None and other())",
    ],
)
def test_none_proof_keeps_writes_and_unknown_calls_unknown(expression):
    import ast
    from arcgraph.analyzers.types import _truth_when_none

    assert _truth_when_none(ast.parse(expression, mode="eval").body, "source") is None
