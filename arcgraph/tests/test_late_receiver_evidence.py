"""Local evidence remains usable after fixture admission and inside import blocks."""

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.tests.test_generator_and_dynamic_exports import _targets


def test_import_is_available_inside_its_own_conditional_block(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "provider": "def normalize(value): return value\n",
            "user": (
                "def use(flag):\n"
                "    def unrelated(): pass\n"
                "    if flag:\n"
                "        from .provider import normalize\n"
                "        return normalize('x')\n"
            ),
        },
        v2=True,
    )
    assert "fn:pkg.provider.normalize" in targets


@pytest.mark.parametrize(
    "body",
    [
        "normalize('x')\n    from .provider import normalize",
        "if flag:\n        from .provider import normalize\n    normalize('x')",
        "if flag:\n        from .provider import normalize\n    else:\n        normalize('x')",
        "if flag:\n        from .provider import normalize\n        normalize = flag\n        normalize('x')",
        "try:\n        from .provider import normalize\n    except ImportError:\n        normalize('x')",
    ],
)
def test_import_requires_order_scope_and_an_unchanged_binding(tmp_path, body):
    targets = _targets(
        tmp_path,
        {
            "provider": "def normalize(value): return value\n",
            "user": "def use(flag):\n    def unrelated(): pass\n    " + body + "\n",
        },
        v2=True,
    )
    assert "fn:pkg.provider.normalize" not in targets


def fixture_edges(tmp_path, body, extra="", parameters="tmp_path"):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_example.py").write_text(
        "import pytest\n" + extra + f"\ndef test_use({parameters}):\n" + body,
        encoding="utf-8",
    )
    output = tmp_path / "out"
    ArcGraphIndexer(
        repo_root=repo,
        output_dir=output,
        source_roots=[SourceRoot(".")],
        enable_v2_call_resolution=True,
    ).build()
    return {
        edge.target
        for edge in GraphStoreReader.from_current(output).read_edges()
        if edge.source.endswith(".test_use")
        and edge.resolution.status == "resolved"
        and edge.confidence != "heuristic"
    }


def test_fixture_evidence_propagates_through_local_path_expressions(tmp_path):
    targets = fixture_edges(
        tmp_path,
        "    folder = tmp_path / 'data'\n"
        "    child = folder / 'sample.txt'\n"
        "    folder.mkdir()\n"
        "    child.write_text('x')\n"
        "    copied = child\n"
        "    copied.unlink()\n",
    )
    assert {
        "extsym:pathlib.Path.mkdir",
        "extsym:pathlib.Path.write_text",
        "extsym:pathlib.Path.unlink",
    } <= targets


@pytest.mark.parametrize(
    "body,extra,parameters",
    [
        ("    folder.mkdir()\n    folder = tmp_path / 'data'\n", "", "tmp_path"),
        (
            "    folder = tmp_path / 'data'\n    folder = other\n    folder.mkdir()\n",
            "",
            "tmp_path, other",
        ),
        (
            "    if flag:\n        folder = tmp_path / 'data'\n    folder.mkdir()\n",
            "",
            "tmp_path, flag",
        ),
        (
            "    folder = tmp_path / 'data'\n    folder.mkdir()\n",
            "@pytest.fixture\ndef tmp_path(): return object()\n",
            "tmp_path",
        ),
        (
            "    folder = tmp_path / 'data'\n    folder.mkdir()\n",
            "@pytest.mark.parametrize('tmp_path', [object()])\n",
            "tmp_path",
        ),
        (
            "    folder = tmp_path / 'data'\n    def change():\n        nonlocal folder\n        folder = other\n    folder.mkdir()\n",
            "",
            "tmp_path, other",
        ),
        ("    left = right\n    right = left\n    left.mkdir()\n", "", "tmp_path"),
    ],
)
def test_local_inference_cannot_escape_missing_or_mutated_evidence(
    tmp_path, body, extra, parameters
):
    assert "extsym:pathlib.Path.mkdir" not in fixture_edges(
        tmp_path, body, extra, parameters
    )


def test_derived_local_values_keep_the_initializer_availability(tmp_path):
    targets = fixture_edges(
        tmp_path,
        "    text = tmp_path.name\n"
        "    pieces = text.split('.')\n"
        "    pieces[0].upper()\n"
        "    other = tmp_path / 'other'\n"
        "    other.parent.mkdir()\n",
    )
    assert "extsym:builtins.str.upper" in targets
    assert "extsym:pathlib.Path.mkdir" in targets


def test_fixture_provider_changes_refresh_local_expression_consumers(tmp_path):
    from arcgraph.pipeline.reindexer import ArcGraphReindexer

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_example.py").write_text(
        "def test_use(tmp_path):\n"
        "    folder = tmp_path / 'data'\n"
        "    folder.mkdir()\n",
        encoding="utf-8",
    )
    roots = [SourceRoot(".")]
    output = tmp_path / "out"
    ArcGraphIndexer(repo, output, roots, enable_v2_call_resolution=True).build()
    provider = repo / "conftest.py"
    for index, source in enumerate(
        ["import pytest\n@pytest.fixture\ndef tmp_path(): return object()\n", None]
    ):
        if source is None:
            provider.unlink()
        else:
            provider.write_text(source, encoding="utf-8")
        ArcGraphReindexer(repo, output, roots).reindex_changed()
        full = tmp_path / f"full-{index}"
        ArcGraphIndexer(repo, full, roots, enable_v2_call_resolution=True).build()

        def snapshot(path):
            return sorted(
                (e.target, e.confidence, e.resolution.status)
                for e in GraphStoreReader.from_current(path).read_edges()
                if e.source == "fn:test_example.test_use"
            )

        assert snapshot(output) == snapshot(full)
        resolved = {
            target
            for target, confidence, status in snapshot(output)
            if confidence != "heuristic" and status == "resolved"
        }
        assert ("extsym:pathlib.Path.mkdir" in resolved) is (source is None)


def test_local_constructor_follows_a_static_star_reexport(tmp_path):
    _targets(
        tmp_path,
        {
            "provider": "class Worker:\n    def run(self): pass\n",
            "facade": "from .provider import *\n",
            "user": "from .facade import Worker\ndef use():\n    job = Worker()\n    job.run()\n",
        },
        v2=True,
    )
    assert any(
        e.target == "method:pkg.provider.Worker.run"
        and e.source == "fn:pkg.user.use"
        and e.confidence != "heuristic"
        for e in GraphStoreReader.from_current(tmp_path / "out").read_edges()
    )


@pytest.mark.parametrize(
    "facade",
    [
        "from .provider import *\nWorker = unknown\n",
        "from .provider import *\ndef change():\n    global Worker\n    Worker = unknown\n",
        "from .provider import *\nfrom .missing import *\n",
    ],
)
def test_a_rebound_star_export_cannot_prove_a_project_constructor(tmp_path, facade):
    _targets(
        tmp_path,
        {
            "provider": "class Worker:\n    def run(self): pass\n",
            "facade": facade,
            "user": "from .facade import Worker\ndef use():\n    job = Worker()\n    job.run()\n",
        },
        v2=True,
    )
    assert not any(
        e.source == "fn:pkg.user.use"
        and e.target.endswith("Worker.run")
        and e.confidence != "heuristic"
        and e.resolution.status == "resolved"
        for e in GraphStoreReader.from_current(tmp_path / "out").read_edges()
    )


def test_star_reexport_changes_refresh_local_constructor_consumers(tmp_path):
    from arcgraph.pipeline.reindexer import ArcGraphReindexer

    _targets(
        tmp_path,
        {
            "provider": "class Worker:\n    def run(self): pass\n",
            "alternate": "class Worker:\n    def run(self): pass\n",
            "facade": "from .provider import *\n",
            "user": "from .facade import Worker\ndef use():\n    job = Worker()\n    job.run()\n",
        },
        v2=True,
    )
    repo = tmp_path / "repo"
    (repo / "src/pkg/facade.py").write_text(
        "from .alternate import *\n", encoding="utf-8"
    )
    output = tmp_path / "out"
    roots = [SourceRoot("src")]
    ArcGraphReindexer(repo, output, roots).reindex_changed()
    full = tmp_path / "full"
    ArcGraphIndexer(repo, full, roots, enable_v2_call_resolution=True).build()

    def snapshot(path):
        return sorted(
            (e.target, e.confidence, e.resolution.status)
            for e in GraphStoreReader.from_current(path).read_edges()
            if e.source == "fn:pkg.user.use"
        )

    assert snapshot(output) == snapshot(full)
    assert ("method:pkg.alternate.Worker.run", "inferred", "resolved") in snapshot(
        output
    )


@pytest.mark.parametrize(
    "module,owner,method",
    [
        ("collections", "Counter", "clear"),
        ("collections", "defaultdict", "clear"),
        ("collections", "deque", "clear"),
        ("io", "BytesIO", "close"),
        ("io", "TextIOWrapper", "flush"),
        ("io", "StringIO", "getvalue"),
        ("threading", "Timer", "cancel"),
        ("threading", "Event", "set"),
        ("contextlib", "ExitStack", "close"),
        ("subprocess", "Popen", "poll"),
    ],
)
def test_standard_library_value_contract_does_not_invent_members(
    tmp_path, module, owner, method
):
    _targets(
        tmp_path,
        {
            "user": f"from {module} import {owner}\n"
            f"def use(item: {owner}):\n"
            f"    item.{method}()\n"
            "    item.this_member_does_not_exist()\n",
        },
        v2=True,
    )
    edges = [
        e
        for e in GraphStoreReader.from_current(tmp_path / "out").read_edges()
        if e.source == "fn:pkg.user.use" and e.resolution.status == "resolved"
    ]
    assert any(e.target == f"extsym:{module}.{owner}.{method}" for e in edges)
    assert not any(e.target.endswith(".this_member_does_not_exist") for e in edges)


def test_an_opaque_capitalized_import_cannot_prove_a_local_constructor(tmp_path):
    _targets(
        tmp_path,
        {
            "user": "from opaque_dependency import Factory\n"
            "def use():\n"
            "    item = Factory()\n"
            "    item.perform()\n",
        },
        v2=True,
    )
    assert not any(
        e.source == "fn:pkg.user.use"
        and e.target == "extsym:opaque_dependency.Factory.perform"
        and e.confidence != "heuristic"
        for e in GraphStoreReader.from_current(tmp_path / "out").read_edges()
    )
