"""Target-conditioned external definitions must match the frozen snapshot."""

import ast
import json
from pathlib import Path
import subprocess
import shutil
import sys
from types import SimpleNamespace

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.semantic.contract import Snapshot, digest
from arcgraph.semantic.external_targets import Registry
from arcgraph.semantic.snapshot import sha
from arcgraph.tests.test_batch30_build import configure, generation_directory
from arcgraph.tests.test_semantic_isolation import prepared as prepared


def lookup(text, line, version="3.11", platform="linux", name="ping", module="example"):
    row = dict(
        origin="typeshed",
        relative=f"stdlib/{module}.pyi",
        module=module,
        text=text,
        text_digest=sha(text.encode()),
    )
    uri = "file:///frozen/stdlib/" + module + ".pyi"
    registry = Registry({uri: row}, digest({uri: row}))
    # Also runs against the delivered parent: its Registry ignores this context.
    snapshot = Snapshot(files=(), target_python=version, platform=platform)
    registry.snapshot = snapshot
    column = text.splitlines()[line].index(name)
    item = dict(
        uri=uri,
        name=name,
        selectionRange=dict(
            start=dict(line=line, character=column),
            end=dict(line=line, character=column + len(name)),
        ),
    )
    records = SimpleNamespace(
        generation_id="generation",
        producer_id="producer",
        snapshot_id=snapshot.id,
        view=SimpleNamespace(defs={}),
    )
    return registry.lookup(item, "utf-16", records)


@pytest.mark.parametrize(
    "condition,version,platform,active",
    [
        ("sys.version_info >= (3, 13)", "3.11", "linux", False),
        ("sys.version_info >= (3, 13)", "3.13", "linux", True),
        ("sys.version_info < (3, 13)", "3.14", "darwin", False),
        ("(3, 13) <= sys.version_info", "3.14", "darwin", True),
        ("sys.version_info[0] == 3", "3.11", "linux", True),
        ("sys.version_info[1] < 13", "3.14", "linux", False),
        ("sys.version_info[:2] == (3, 11)", "3.11", "linux", True),
        ("sys.version_info[0:2] != (3, 11)", "3.11", "linux", False),
        ("sys.platform == 'win32'", "3.11", "win32", True),
        ("'darwin' != sys.platform", "3.11", "darwin", False),
        ("sys.platform.startswith('linux')", "3.11", "linux", True),
        ("sys.platform.startswith('win')", "3.11", "darwin", False),
        (
            "sys.platform == 'win32' and sys.version_info < (3, 12)",
            "3.11",
            "win32",
            True,
        ),
        ("sys.platform == 'win32' or sys.platform == 'linux'", "3.11", "darwin", False),
        ("not sys.platform.startswith('linux')", "3.11", "darwin", True),
    ],
)
def test_only_the_target_branch_can_be_mapped(condition, version, platform, active):
    text = (
        f"import sys\nclass Client:\n    if {condition}:\n"
        "        def ping(self): return 1\n"
        "    else:\n        def ping(self): return 2\n"
    )
    selected, inactive = (3, 5) if active else (5, 3)
    result = lookup(text, selected, version, platform)
    assert result is not None, (condition, version, platform)
    assert result.span.start[0] == selected
    assert lookup(text, inactive, version, platform) is None


@pytest.mark.parametrize(
    "condition",
    [
        "FLAG",
        "check()",
        "sys.platform.endswith('win')",
        "sys.version_info[compute()] >= 3",
        "FLAG and sys.platform == 'linux'",
    ],
)
def test_other_conditions_keep_the_existing_ambiguity(condition):
    text = (
        f"import sys\nclass Client:\n    if {condition}:\n"
        "        def ping(self): return 1\n"
        "    else:\n        def ping(self): return 2\n"
    )
    assert lookup(text, 3) is None
    assert lookup(text, 5) is None


@pytest.mark.parametrize("version", ["3.11", "3.14"])
def test_real_build_selects_the_correct_str_replace_overloads(
    prepared, tmp_path, monkeypatch, version
):
    project, environment, _, _ = prepared
    if version == "3.11":
        assert sys.version_info[:2] == (3, 11)
        shutil.rmtree(environment)
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-m",
                "venv",
                "--without-pip",
                str(environment),
            ],
            check=True,
            timeout=30,
        )  # nosec B603
    (project / "pyproject.toml").write_text(
        '[project]\nname="replace-probe"\nversion="0.0.0"\n'
        f'requires-python=">={version},<3.{int(version.split(".")[1]) + 1}"\n'
        '[tool.arcgraph]\nsource_roots=["."]\n',
        encoding="utf-8",
    )
    (project / "demo.py").write_text(
        "def replace(s: str): return s.replace('a', 'b')\n"
        "def upper(s: str): return s.upper()\n"
        "def get(d: dict[str, int]): return d.get('k')\n",
        encoding="utf-8",
    )
    configure(project)
    output = tmp_path / "out"
    proved = []
    original = Registry.lookup

    def capture(registry, *args):
        value = original(registry, *args)
        if value is not None and value.id == "extsym:builtins.str.replace":
            proved.append(value)
        return value

    monkeypatch.setattr(Registry, "lookup", capture)
    metadata, directory = ArcGraphIndexer(project, output).build()
    assert (
        metadata.toolchain_status["semantic_core"]["status"] == "active"
    ), metadata.toolchain_status["semantic_core"]
    generation = generation_directory(directory)
    primary = json.loads((generation / "pyright.json").read_text(encoding="utf-8"))
    assert primary["snapshot"]["target_python"] == version
    edges = list(GraphStoreReader.from_current(output).read_edges())
    for name, target in (
        ("replace", "builtins.str.replace"),
        ("upper", "builtins.str.upper"),
        ("get", "builtins.dict.get"),
    ):
        matches = [
            e
            for e in edges
            if e.source == "fn:demo." + name and e.target == "extsym:" + target
        ]
        assert len(matches) == 1
        assert {"ast_attribute_call", "type_inference"} <= {
            v.kind for v in matches[0].evidence
        }
    assert proved
    definition = proved[0]
    captures = json.loads(
        (generation / "external-targets.json").read_text(encoding="utf-8")
    )
    text = next(
        r["text"] for r in captures.values() if r["relative"] == "stdlib/builtins.pyi"
    )
    tree = ast.parse(text)
    branch = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.If)
        and ast.unparse(n.test) == "sys.version_info >= (3, 13)"
        and any(isinstance(d, ast.FunctionDef) and d.name == "replace" for d in n.body)
    )
    active = branch.orelse if version == "3.11" else branch.body
    assert definition.span.start[0] in {n.lineno - 1 for n in active}
    selected = next(n for n in active if n.lineno - 1 == definition.span.start[0])
    assert ("count" in {a.arg for a in selected.args.posonlyargs}) == (
        version == "3.11"
    )
    assert Path(generation / "bridge.json").is_file()
