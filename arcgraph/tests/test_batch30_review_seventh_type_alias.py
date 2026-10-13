"""PEP 695 class writes must invalidate only methods they can overwrite."""

import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from arcgraph.semantic.contract import Snapshot
from arcgraph.semantic.external_targets import definition
from arcgraph.tests.test_batch30_review_sixth_bindings import lookup

# The provisioned project interpreter may have no ArcGraph dependencies. Run the
# actual definition functions there with ASCII span adapters, as in the review
# probe, so a 3.11 test host also exercises the real 3.12+ AST and runtime.
RUNNER = r"""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(
    "structure_facts", root / "arcgraph/semantic/structure_facts.py"
)
facts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(facts)
sys.modules["structure_facts"] = facts

def native_span(text, value, unit):
    assert text.isascii() and unit == "utf-16"
    return SimpleNamespace(
        start=(value["start"]["line"], value["start"]["character"]),
        end=(value["end"]["line"], value["end"]["character"]),
    )

namespace = dict(
    ast=ast, Path=Path, Span=SimpleNamespace, native_span=native_span,
    sha=lambda raw: hashlib.sha256(raw).hexdigest(),
)
source = (root / "arcgraph/semantic/external_targets.py").read_text(encoding="utf-8")
for node in ast.parse(source).body:
    if isinstance(node, ast.FunctionDef) and node.name in {
        "module_name", "target_condition", "overridden_stubs", "definition_index", "definition"
    }:
        code = ast.get_source_segment(source, node).replace(
            "from .structure_facts import", "from structure_facts import"
        )
        exec(code, namespace)

rows = {}
for label, text in json.load(sys.stdin).items():
    node = next(n for n in ast.walk(ast.parse(text))
                if isinstance(n, ast.FunctionDef) and n.name == "ping")
    line = node.lineno - 1
    column = text.splitlines()[line].index("ping")
    value = namespace["definition"](
        dict(origin="dependency", relative="example.py", module="example",
             text=text, text_digest=namespace["sha"](text.encode())),
        dict(name="ping", selectionRange=dict(
            start=dict(line=line, character=column),
            end=dict(line=line, character=column + 4))),
        "utf-16",
        SimpleNamespace(target_python=f"{sys.version_info.major}.{sys.version_info.minor}",
                        platform=sys.platform),
    )
    runtime = {}
    exec(text, runtime)
    client = runtime["C"]()
    try:
        call = client.ping()
    except TypeError:
        call = "TypeError"
    rows[label] = dict(
        proof=None if value is None else value["qualname"],
        runtime=type(runtime["C"].__dict__["ping"]).__name__, call=call,
    )
print(json.dumps(dict(python=sys.version, rows=rows)))
"""

METHOD = "    def ping(self): return 1\n"
CASES = {
    "type_after": "class C:\n" + METHOD + "    type ping = int\n",
    "type_before": "class C:\n    type ping = int\n" + METHOD,
    "type_in_taken_if": (
        "import sys\nclass C:\n"
        + METHOD
        + "    if sys.version_info >= (3, 12):\n        type ping = int\n"
    ),
    "type_in_false_if": (
        "import sys\nclass C:\n"
        + METHOD
        + "    if sys.version_info >= (3, 99):\n        type ping = int\n"
    ),
    "type_in_taken_platform_if": (
        "import sys\nclass C:\n"
        + METHOD
        + f"    if sys.platform == {sys.platform!r}:\n        type ping = int\n"
    ),
    "type_in_false_platform_if": (
        "import sys\nclass C:\n"
        + METHOD
        + "    if sys.platform == 'inactive-platform':\n        type ping = int\n"
    ),
    "type_other_lazy": "class C:\n" + METHOD + "    type Alias = missing()\n",
    "type_nested": (
        "class C:\n" + METHOD + "    class Inner:\n        type ping = int\n"
    ),
}
OVERWRITTEN = {"type_after", "type_in_taken_if", "type_in_taken_platform_if"}
CALIBRATION = {
    "assignment_after": "class C:\n" + METHOD + "    ping = None\n",
    "assignment_before": "class C:\n    ping = None\n" + METHOD,
    "inactive_assignment": (
        "import sys\nclass C:\n"
        + METHOD
        + "    if sys.version_info >= (3, 99):\n        ping = None\n"
    ),
}


@pytest.fixture(scope="module")
def type_alias_results():
    python = (
        sys.executable
        if hasattr(ast, "TypeAlias")
        else os.environ.get("ARCGRAPH_TEST_PROJECT_PYTHON")
    )
    if python is None:
        if os.environ.get("ARCGRAPH_REQUIRE_SEMANTIC") == "1":
            pytest.fail("A provisioned Python 3.12+ is required; never downloads")
        pytest.skip("Python 3.12+ is required for type statement syntax")
    probe = subprocess.run(
        [python, "-I", "-B", "-c", "import ast; print(hasattr(ast, 'TypeAlias'))"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    if probe.stdout.strip() != "True":
        pytest.skip("The provisioned Python does not support type statements")
    result = subprocess.run(
        [python, "-I", "-B", "-c", RUNNER, str(Path(__file__).resolve().parents[2])],
        input=json.dumps(CASES | CALIBRATION),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    return json.loads(result.stdout)["rows"]


@pytest.mark.parametrize("label", CASES)
def test_type_statement_overwrites_only_a_preceding_active_method(
    type_alias_results, label
):
    row = type_alias_results[label]
    if label in OVERWRITTEN:
        assert row["runtime"] == "TypeAliasType"
        assert row["call"] == "TypeError"
        assert row["proof"] is None
    else:
        assert row["runtime"] == "function"
        assert row["call"] == 1
        assert row["proof"] == "example.C.ping"
    if hasattr(ast, "TypeAlias"):
        # Native 3.12+ hosts additionally use the complete package and contract.
        text = CASES[label]
        node = next(
            n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.FunctionDef)
        )
        line = node.lineno - 1
        column = text.splitlines()[line].index("ping")
        from arcgraph.semantic.snapshot import sha

        value = definition(
            dict(
                origin="dependency",
                relative="example.py",
                module="example",
                text=text,
                text_digest=sha(text.encode()),
            ),
            dict(
                name="ping",
                selectionRange=dict(
                    start=dict(line=line, character=column),
                    end=dict(line=line, character=column + 4),
                ),
            ),
            "utf-16",
            Snapshot(
                files=(),
                target_python=f"{sys.version_info.major}.{sys.version_info.minor}",
                platform=sys.platform,
            ),
        )
        assert (value is None) == (label in OVERWRITTEN)


@pytest.mark.parametrize("label", CALIBRATION)
def test_provisioned_definition_matches_native_assignment_controls(
    type_alias_results, label
):
    value = lookup(CALIBRATION[label])
    assert type_alias_results[label]["proof"] == (
        None if value is None else "example.C.ping"
    )
