"""Fixture type evidence must follow pytest provider selection, not names."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcgraph.adapters.registry import default_adapter_registry
from arcgraph.core.scanner import FileScanner, SourceRoot
from arcgraph.pipeline.contracts import FrontendGraphFragment
from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer

HEADER = "import pytest\nclass Client:\n    def status(self): return 'ok'\n"
OTHER = "class Other:\n    def status(self): return 'other'\n"


def analyze(tmp_path: Path, sources: dict[str, str]) -> FrontendGraphFragment:
    for path, text in sources.items():
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
    files = FileScanner(tmp_path, [SourceRoot(".")]).scan()
    return PythonGraphAnalyzer(adapter_registry=default_adapter_registry()).analyze(
        files
    )


def calls(fragment: FrontendGraphFragment, source: str) -> set[str]:
    return {
        e.target for e in fragment.edges if e.source == source and e.kind == "calls"
    }


@pytest.mark.parametrize(
    "body",
    [
        "    return Client()\n",
        "    value = Client()\n    return value\n",
        "    yield Client()\n",
        "    value = Client()\n    yield value\n    value.close()\n",
        "    def inner():\n        return unknown()\n    return Client()\n",
    ],
)
def test_fixture_result_is_transferred_with_provider_evidence(tmp_path: Path, body):
    result = analyze(
        tmp_path,
        {
            "test_api.py": HEADER
            + "@pytest.fixture\ndef eyes():\n"
            + body
            + "def test_status(eyes):\n    return eyes.status()\n"
        },
    )
    assert "method:test_api.Client.status" in calls(result, "fn:test_api.test_status")
    node = next(n for n in result.nodes if n.id == "fn:test_api.test_status")
    ref = next(
        r
        for r in node.properties["type_refs"]
        if r["strategy"] == "pytest_fixture_result"
    )
    assert ref["fixture_id"] == "fixture:test_api.eyes"
    assert ref["provider_evidence"]["path"] == "test_api.py"
    assert ref["confidence"] == "inferred"


@pytest.mark.parametrize(
    "definition",
    [
        "@pytest.fixture\ndef client():\n    return unknown()\n",
        "@pytest.fixture\ndef client():\n    if flag: return Client()\n",
        "@pytest.fixture\ndef client():\n    def inner(): return Client()\n    return inner\n",
        "@pytest.fixture\ndef client():\n    value = Client()\n    value = unknown()\n    return value\n",
        "@pytest.fixture\ndef client(Client):\n    return Client()\n",
        "@pytest.fixture\ndef client():\n    Client = unknown()\n    return Client()\n",
        "@pytest.fixture\ndef client():\n    yield from unknown()\n",
        "@pytest.fixture\ndef client():\n    yield Client()\n    yield Client()\n",
        "@pytest.fixture\nasync def client():\n    return Client()\n",
        "@wrap\n@pytest.fixture\ndef client():\n    return Client()\n",
        "",
    ],
)
def test_unknown_fixture_cannot_use_class_name_or_unique_method(
    tmp_path: Path, definition
):
    result = analyze(
        tmp_path,
        {
            "test_api.py": HEADER
            + definition
            + "def test_status(client):\n    return client.status()\n"
        },
    )
    assert "method:test_api.Client.status" not in calls(
        result, "fn:test_api.test_status"
    )
    assert any(
        e.source == "fn:test_api.test_status" and e.confidence == "unresolved"
        for e in result.edges
    )


def test_alias_and_imported_constructor(tmp_path: Path):
    result = analyze(
        tmp_path,
        {
            "model.py": HEADER,
            "conftest.py": 'from pytest import fixture as fx\nfrom model import Client as C\n@fx(name="eyes")\ndef provider():\n    return C()\n',
            "test_api.py": "def test_status(eyes):\n    eyes.status()\n",
        },
    )
    assert "method:model.Client.status" in calls(result, "fn:test_api.test_status")
    assert any(
        e.source == "test:test_api.test_status"
        and e.target == "fixture:conftest.provider"
        and e.kind == "injects"
        for e in result.edges
    )


def test_nearest_conftest_module_and_class_precedence(tmp_path: Path):
    result = analyze(
        tmp_path,
        {
            "model.py": HEADER + OTHER,
            "conftest.py": "import pytest\nfrom model import Client\n@pytest.fixture\ndef eyes(): return Client()\n",
            "suite/conftest.py": "import pytest\nfrom model import Other\n@pytest.fixture\ndef eyes(): return Other()\n",
            "test_root.py": "def test_status(eyes): eyes.status()\n",
            "suite/test_near.py": "def test_status(eyes): eyes.status()\n",
            "suite/test_local.py": "import pytest\nfrom model import Client, Other\n@pytest.fixture\ndef eyes(): return Client()\ndef test_status(eyes): eyes.status()\nclass TestLocal:\n    @pytest.fixture\n    def eyes(self): return Other()\n    def test_status(self, eyes): eyes.status()\nclass TestSibling:\n    def test_status(self, eyes): eyes.status()\n",
        },
    )
    for name in [
        "fn:test_root.test_status",
        "fn:suite.test_local.test_status",
        "method:suite.test_local.TestSibling.test_status",
    ]:
        assert "method:model.Client.status" in calls(result, name)
        assert "method:model.Other.status" not in calls(result, name)
    for name in [
        "fn:suite.test_near.test_status",
        "method:suite.test_local.TestLocal.test_status",
    ]:
        assert "method:model.Other.status" in calls(result, name)
        assert "method:model.Client.status" not in calls(result, name)


@pytest.mark.parametrize("path", ["test_api.py", "suite/conftest.py"])
def test_ambiguous_winning_scope_does_not_fall_back(tmp_path: Path, path):
    local = 'import pytest\nfrom model import Client\n@pytest.fixture(name="eyes")\ndef a(): return Client()\n@pytest.fixture(name="eyes")\ndef b(): return Client()\n'
    testpath = "test_api.py" if path == "test_api.py" else "suite/test_api.py"
    sources = {
        "model.py": HEADER,
        "conftest.py": "import pytest\nfrom model import Client\n@pytest.fixture\ndef eyes(): return Client()\n",
        path: local,
    }
    sources[testpath] = (
        sources.get(testpath, "") + "def test_status(eyes): eyes.status()\n"
    )
    result = analyze(tmp_path, sources)
    source = "fn:" + testpath[:-3].replace("/", ".") + ".test_status"
    assert not calls(result, source)
    assert not any(
        e.source == source.replace("fn:", "test:") and e.kind == "injects"
        for e in result.edges
    )


@pytest.mark.parametrize(
    "mark,expected",
    [
        ('@pytest.mark.parametrize("eyes", [None])', False),
        ('@pytest.mark.parametrize("eyes", [None], indirect=True)', True),
        ('@pytest.mark.parametrize("eyes", [None], indirect=["eyes"])', True),
        ('@pytest.mark.parametrize("eyes", [None], indirect=dynamic)', False),
    ],
)
def test_parametrization_uses_fixture_only_when_indirect(
    tmp_path: Path, mark, expected
):
    result = analyze(
        tmp_path,
        {
            "test_api.py": HEADER
            + "@pytest.fixture\ndef eyes(): return Client()\n"
            + mark
            + "\ndef test_status(eyes): eyes.status()\n"
        },
    )
    assert (
        "method:test_api.Client.status" in calls(result, "fn:test_api.test_status")
    ) == expected
    assert (
        any(
            e.source == "test:test_api.test_status" and e.kind == "injects"
            for e in result.edges
        )
        == expected
    )


@pytest.mark.parametrize(
    "mark",
    [
        'pytestmark = pytest.mark.parametrize("eyes", [None])\n',
        "pytestmark = dynamic\n",
    ],
)
def test_module_parameter_marks_block_fixture_type(tmp_path: Path, mark):
    result = analyze(
        tmp_path,
        {
            "test_api.py": HEADER
            + "@pytest.fixture\ndef eyes(): return Client()\n"
            + mark
            + "def test_status(eyes): eyes.status()\n"
        },
    )
    assert not calls(result, "fn:test_api.test_status")


def test_annotation_and_asyncio_fixture(tmp_path: Path):
    result = analyze(
        tmp_path,
        {
            "test_api.py": HEADER
            + OTHER
            + "import pytest_asyncio as pa\n@pa.fixture\nasync def eyes(): yield Client()\nasync def test_status(eyes): eyes.status()\ndef test_typed(eyes: Other): eyes.status()\n"
        },
    )
    assert "method:test_api.Client.status" in calls(result, "fn:test_api.test_status")
    assert "method:test_api.Other.status" in calls(result, "fn:test_api.test_typed")
    assert "method:test_api.Client.status" not in calls(
        result, "fn:test_api.test_typed"
    )


def test_test_parameter_reassignment_masks_fixture_type(tmp_path: Path):
    result = analyze(
        tmp_path,
        {
            "test_api.py": HEADER
            + "@pytest.fixture\ndef eyes(): return Client()\ndef test_status(eyes):\n    eyes = unknown()\n    eyes.status()\n"
        },
    )
    assert not calls(result, "fn:test_api.test_status")


def test_incremental_provider_change_addition_and_deletion_match_full_build(
    tmp_path: Path,
):
    from arcgraph.core.graph_store import GraphStoreReader
    from arcgraph.pipeline.indexer import ArcGraphIndexer
    from arcgraph.pipeline.reindexer import ArcGraphReindexer

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_api.py").write_text(
        "def test_status(eyes): eyes.status()\n", encoding="utf-8"
    )
    (repo / "model.py").write_text(HEADER + OTHER, encoding="utf-8")
    roots = [SourceRoot(".")]
    index = tmp_path / "incremental"
    ArcGraphIndexer(repo, index, roots).build()
    provider = repo / "conftest.py"
    for return_type in ["Client", "Other", None]:
        if return_type:
            provider.write_text(
                f"import pytest\nfrom model import {return_type}\n@pytest.fixture\ndef eyes(): return {return_type}()\n",
                encoding="utf-8",
            )
        else:
            provider.unlink()
        ArcGraphReindexer(repo, index, roots).reindex_changed()
        full = tmp_path / f"full-{return_type}"
        ArcGraphIndexer(repo, full, roots).build()

        def evidence(path):
            store = GraphStoreReader.from_current(path)
            return (
                sorted((e.source, e.target, e.kind) for e in store.read_edges()),
                {n.id: n.properties.get("type_refs") for n in store.read_nodes()},
            )

        assert evidence(index) == evidence(full)


@pytest.mark.parametrize(
    "tail",
    [
        "eyes = unknown()\n",
        "def eyes(): return unknown()\n",
        "pytest = unknown()\n",
    ],
)
def test_replaced_fixture_or_decorator_does_not_supply_type(tmp_path: Path, tail):
    result = analyze(
        tmp_path,
        {
            "test_api.py": HEADER
            + "@pytest.fixture\ndef eyes(): return Client()\n"
            + tail
            + "def test_status(eyes): eyes.status()\n"
        },
    )
    assert not calls(result, "fn:test_api.test_status")


def test_inherited_and_imported_fixture_do_not_fall_back_to_conftest(tmp_path: Path):
    result = analyze(
        tmp_path,
        {
            "conftest.py": HEADER + "@pytest.fixture\ndef eyes(): return Client()\n",
            "test_inherited.py": "class TestChild(ExternalBase):\n    def test_status(self, eyes): eyes.status()\n",
            "test_imported.py": "from elsewhere import eyes\ndef test_status(eyes): eyes.status()\n",
        },
    )
    assert not calls(result, "method:test_inherited.TestChild.test_status")
    assert not calls(result, "fn:test_imported.test_status")


def test_class_parametrize_default_and_execution_only_fixtures(tmp_path: Path):
    result = analyze(
        tmp_path,
        {
            "test_api.py": HEADER
            + '@pytest.fixture(autouse=True)\ndef eyes(): return Client()\n@pytest.mark.parametrize("eyes", [None])\nclass TestGroup:\n    def test_status(self, eyes): eyes.status()\n@pytest.mark.usefixtures("eyes")\ndef test_no_arg(): pass\ndef test_default(eyes=None): pass\n'
        },
    )
    assert not calls(result, "method:test_api.TestGroup.test_status")
    for name in ["test_no_arg", "test_default"]:
        node = next(n for n in result.nodes if n.id == "fn:test_api." + name)
        assert not any(
            r.get("strategy") == "pytest_fixture_result"
            for r in node.properties.get("type_refs", [])
        )


@pytest.mark.parametrize(
    "body",
    [
        "    return\n    yield Client()\n",
        "    if flag:\n        yield Client()\n",
        "    try:\n        yield Client()\n    finally:\n        cleanup()\n",
    ],
)
def test_unsupported_yield_control_flow_stays_unknown(tmp_path: Path, body):
    result = analyze(
        tmp_path,
        {
            "test_api.py": HEADER
            + "@pytest.fixture\ndef eyes():\n"
            + body
            + "def test_status(eyes): eyes.status()\n"
        },
    )
    assert not calls(result, "fn:test_api.test_status")


def test_fixture_parameter_captured_by_nested_test_helper(tmp_path: Path):
    result = analyze(
        tmp_path,
        {
            "test_api.py": HEADER
            + "@pytest.fixture\ndef eyes(): return Client()\ndef test_status(eyes):\n    def inner(): return eyes.status()\n    inner()\n"
        },
    )
    assert "method:test_api.Client.status" in calls(
        result, "fn:test_api.test_status.inner"
    )
    assert "method:test_api.Client.status" not in calls(
        result, "fn:test_api.test_status"
    )


def test_fixture_return_annotation(tmp_path: Path):
    result = analyze(
        tmp_path,
        {
            "test_api.py": HEADER
            + "@pytest.fixture\ndef eyes() -> Client: return external_factory()\ndef test_status(eyes): eyes.status()\n"
        },
    )
    assert "method:test_api.Client.status" in calls(result, "fn:test_api.test_status")


@pytest.mark.parametrize(
    "definition",
    [
        "@pytest.fixture(name=dynamic_name)\ndef provider(): return unknown()\n",
        "if flag:\n    @pytest.fixture\n    def eyes(): return unknown()\n",
    ],
)
def test_dynamic_provider_cannot_fall_back_to_ancestor(tmp_path: Path, definition):
    result = analyze(
        tmp_path,
        {
            "conftest.py": HEADER + "@pytest.fixture\ndef eyes(): return Client()\n",
            "test_api.py": "import pytest\n"
            + definition
            + "def test_status(eyes): eyes.status()\n",
        },
    )
    assert not calls(result, "fn:test_api.test_status")


def test_class_pytestmark_prevents_fixture_inference(tmp_path: Path):
    result = analyze(
        tmp_path,
        {
            "test_api.py": HEADER
            + '@pytest.fixture\ndef eyes(): return Client()\nclass TestGroup:\n    pytestmark = pytest.mark.parametrize("eyes", [None])\n    def test_status(self, eyes): eyes.status()\n'
        },
    )
    assert not calls(result, "method:test_api.TestGroup.test_status")


def test_unknown_fixture_alias_does_not_escape_receiver_guard(tmp_path: Path):
    result = analyze(
        tmp_path,
        {
            "test_api.py": HEADER
            + "@pytest.fixture\ndef eyes(): return unknown()\ndef test_status(eyes):\n    client = eyes\n    other = client\n    other.status()\n"
        },
    )
    assert not calls(result, "fn:test_api.test_status")


def test_incremental_new_fixture_consumer_sees_unchanged_provider_class(tmp_path: Path):
    from arcgraph.core.query_engine import QueryEngine
    from arcgraph.pipeline.indexer import ArcGraphIndexer
    from arcgraph.pipeline.reindexer import ArcGraphReindexer

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "model.py").write_text(HEADER, encoding="utf-8")
    consumer = repo / "test_api.py"
    consumer.write_text("def test_empty(): pass\n", encoding="utf-8")
    roots = [SourceRoot(".")]
    index = tmp_path / "index"
    ArcGraphIndexer(repo, index, roots).build()
    consumer.write_text(
        "import pytest\nfrom model import Client\n@pytest.fixture\ndef eyes(): return Client()\ndef test_status(eyes): eyes.status()\n",
        encoding="utf-8",
    )
    ArcGraphReindexer(repo, index, roots).reindex_changed()
    assert "fn:test_api.test_status" in {
        n["id"] for n in QueryEngine(index).callers("model.Client.status")["callers"]
    }


def test_unannotated_builtin_fixtures_have_provider_type_evidence(tmp_path):
    result = analyze(
        tmp_path,
        {
            "test_api.py": 'def test_run(monkeypatch, tmp_path, capsys):\n    monkeypatch.setenv("K", "v")\n    tmp_path.joinpath("x")\n    capsys.readouterr()\n'
        },
    )
    edges = {
        e.target
        for e in result.edges
        if e.source == "fn:test_api.test_run" and e.resolution.status == "resolved"
    }
    assert {
        "extsym:pytest.MonkeyPatch.setenv",
        "extsym:pathlib.Path.joinpath",
        "extsym:pytest.CaptureFixture.readouterr",
    } <= edges
    node = next(n for n in result.nodes if n.id == "fn:test_api.test_run")
    assert all(
        ref["strategy"] == "pytest_builtin_fixture"
        for ref in node.properties["type_refs"]
    )


@pytest.mark.parametrize(
    "prefix,provider",
    [
        ("", "@pytest.fixture\ndef monkeypatch(): return unknown()\n"),
        ('@pytest.mark.parametrize("monkeypatch", [None])\n', ""),
        ("", "@pytest.fixture(name=dynamic)\ndef custom(): return unknown()\n"),
        (
            "",
            "if flag:\n    @pytest.fixture\n    def monkeypatch(): return unknown()\n",
        ),
        ("", "from plugin import monkeypatch\n"),
        ("", 'pytest_plugins = ["some_plugin"]\n'),
    ],
)
def test_builtin_provider_is_suppressed_by_project_overrides(
    tmp_path, prefix, provider
):
    result = analyze(
        tmp_path,
        {
            "conftest.py": "import pytest\n" + provider,
            "test_api.py": "import pytest\n"
            + prefix
            + 'def test_run(monkeypatch):\n    monkeypatch.setenv("K", "v")\n',
        },
    )
    assert not any(e.target == "extsym:pytest.MonkeyPatch.setenv" for e in result.edges)


def test_project_fixture_takes_precedence_over_builtin(tmp_path):
    result = analyze(
        tmp_path,
        {
            "conftest.py": "import pytest\nclass Custom:\n    def setenv(self, *args): pass\n@pytest.fixture\ndef monkeypatch(): return Custom()\n",
            "test_api.py": 'def test_run(monkeypatch):\n    monkeypatch.setenv("K", "v")\n',
        },
    )
    assert "method:conftest.Custom.setenv" in calls(result, "fn:test_api.test_run")
    assert not any(e.target == "extsym:pytest.MonkeyPatch.setenv" for e in result.edges)


def test_business_test_prefix_is_not_pytest(tmp_path):
    result = analyze(
        tmp_path,
        {"network.py": "def test_connection(monkeypatch):\n    return monkeypatch\n"},
    )
    assert not any(
        b.get("pytest_parameter")
        for n in result.nodes
        for b in n.properties.get("bindings", [])
    )
    assert not any(n.kind == "test_case" for n in result.nodes)


@pytest.mark.parametrize(
    "name,config",
    [
        (
            "pytest.ini",
            "[pytest]\npython_files = check_*.py\npython_functions = check_\npython_classes = Check\n",
        ),
        (
            "pyproject.toml",
            '[tool.pytest.ini_options]\npython_files = ["check_*.py"]\npython_functions = ["check_"]\npython_classes = ["Check"]\n',
        ),
        (
            "setup.cfg",
            "[tool:pytest]\npython_files = check_*.py\npython_functions = check_\npython_classes = Check\n",
        ),
    ],
)
def test_custom_pytest_collection_patterns(tmp_path, name, config):
    result = analyze(
        tmp_path,
        {
            name: config,
            "check_api.py": 'class CheckAPI:\n    def check_run(self, monkeypatch):\n        monkeypatch.setenv("K", "v")\n',
            "test_old.py": "def test_run(monkeypatch): pass\n",
        },
    )
    assert any(e.target == "extsym:pytest.MonkeyPatch.setenv" for e in result.edges)
    assert {n.id for n in result.nodes if n.kind == "test_case"} == {
        "test:check_api.CheckAPI.check_run"
    }


@pytest.mark.parametrize(
    "business,changed",
    [
        ("VALUE = 1\n", 'fixture_dir = "data"\n'),
        ("def test_connection(host): return host\n", "VALUE = 2\n"),
    ],
)
def test_non_pytest_incremental_edits_only_analyze_changed_file(
    tmp_path, monkeypatch, business, changed
):
    from arcgraph.pipeline.indexer import ArcGraphIndexer
    from arcgraph.pipeline.reindexer import ArcGraphReindexer

    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(30):
        (repo / f"m{i}.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "network.py").write_text(business, encoding="utf-8")
    roots = [SourceRoot(".")]
    out = tmp_path / "index"
    ArcGraphIndexer(repo, out, roots).build()
    (repo / "m0.py").write_text(changed, encoding="utf-8")
    batches = []
    original = ArcGraphIndexer.analyze_files

    def observe(self, files, *args, **kwargs):
        batches.append([f.path for f in files])
        return original(self, files, *args, **kwargs)

    monkeypatch.setattr(ArcGraphIndexer, "analyze_files", observe)
    ArcGraphReindexer(repo, out, roots).reindex_changed()
    assert batches == [["m0.py"]]


def test_incremental_builtin_override_delete_restore_matches_full(tmp_path):
    from arcgraph.pipeline.indexer import ArcGraphIndexer
    from arcgraph.pipeline.reindexer import ArcGraphReindexer
    from arcgraph.core.graph_store import GraphStoreReader

    repo = tmp_path / "repo"
    repo.mkdir()
    roots = [SourceRoot(".")]
    (repo / "test_api.py").write_text(
        'def test_run(monkeypatch):\n    monkeypatch.setenv("K", "v")\n',
        encoding="utf-8",
    )
    (repo / "model.py").write_text(
        "class Custom:\n    def setenv(self, *args): pass\n", encoding="utf-8"
    )
    provider = repo / "conftest.py"
    index = tmp_path / "index"
    ArcGraphIndexer(repo, index, roots).build()
    known = "import pytest\nfrom model import Custom\n@pytest.fixture\ndef monkeypatch(): return Custom()\n"
    for i, (source, target) in enumerate(
        [
            (known, "method:model.Custom.setenv"),
            (
                "import pytest\n@pytest.fixture\ndef monkeypatch(): return unknown()\n",
                None,
            ),
            (None, "extsym:pytest.MonkeyPatch.setenv"),
            (known, "method:model.Custom.setenv"),
        ]
    ):
        if source is None:
            provider.unlink()
        else:
            provider.write_text(source, encoding="utf-8")
        ArcGraphReindexer(repo, index, roots).reindex_changed()
        full = tmp_path / f"full-{i}"
        ArcGraphIndexer(repo, full, roots).build()

        def snapshot(out):
            reader = GraphStoreReader.from_current(out)
            return sorted(
                (e.target, e.resolution.status, e.resolution.strategy)
                for e in reader.read_edges()
                if e.source == "fn:test_api.test_run"
                and e.kind in {"calls", "uses", "dynamic_call"}
            )

        assert snapshot(index) == snapshot(full)
        assert {t for t, s, _ in snapshot(index) if s == "resolved"} == (
            {target} if target else set()
        )


def test_invalid_collection_configuration_is_conservative(tmp_path):
    from arcgraph.adapters.pytest_collection import PytestCollection

    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest]\nini_options = "invalid"\n', encoding="utf-8"
    )
    assert not PytestCollection.from_root(tmp_path).file_matches("test_app.py")


def test_empty_pytest_ini_takes_precedence_over_pyproject(tmp_path):
    from arcgraph.adapters.pytest_collection import PytestCollection

    (tmp_path / "pytest.ini").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npython_files = ["check_*.py"]\n', encoding="utf-8"
    )
    assert PytestCollection.from_root(tmp_path).file_matches("test_app.py")
    assert not PytestCollection.from_root(tmp_path).file_matches("check_app.py")


@pytest.mark.parametrize(
    "declaration",
    [
        "from plugins.overrides import *\n",
        'pytest_plugins: list[str] = ["plugins.overrides"]\n',
        "if flag:\n    from plugins.overrides import *\n",
    ],
)
@pytest.mark.parametrize(
    "fixture,method", [("monkeypatch", "setattr"), ("tmp_path", "joinpath")]
)
def test_unknown_fixture_overrides_block_builtin_types(
    tmp_path, declaration, fixture, method
):
    result = analyze(
        tmp_path,
        {
            "conftest.py": declaration,
            "test_api.py": f'def test_run({fixture}):\n    {fixture}.{method}("x")\n',
        },
    )
    assert not any(
        e.source == "fn:test_api.test_run" and e.resolution.status == "resolved"
        for e in result.edges
    )


def test_sibling_conftest_unknown_providers_do_not_block_builtin(tmp_path):
    result = analyze(
        tmp_path,
        {
            "sibling/conftest.py": 'from plugins.overrides import *\npytest_plugins: list[str] = ["plugins.overrides"]\n',
            "tests/test_api.py": 'def test_run(monkeypatch):\n    monkeypatch.setenv("K", "v")\n',
        },
    )
    assert any(e.target == "extsym:pytest.MonkeyPatch.setenv" for e in result.edges)


_NON_UTF8_LOCALE_PROBE = """
import json, locale, sys
from pathlib import Path
from arcgraph.adapters.pytest_collection import PytestCollection

encoding = locale.getpreferredencoding(False)
collection = PytestCollection.from_root(Path(sys.argv[1]))
print(json.dumps({"encoding": encoding, "python_files": collection.python_files}))
"""


@pytest.mark.parametrize(
    "name,config",
    [
        (
            "pyproject.toml",
            '[project]\nname = "含空格"\n\n'
            '[tool.pytest.ini_options]\npython_files = ["check_*.py"]\n',
        ),
        ("setup.cfg", "# 含空格\n[tool:pytest]\npython_files = check_*.py\n"),
    ],
)
def test_collection_config_is_read_as_utf8_under_a_non_utf8_locale(
    tmp_path, name, config
):
    import json
    import os
    import subprocess
    import sys

    (tmp_path / name).write_text(config, encoding="utf-8")
    env = {
        **os.environ,
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
    }
    result = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", _NON_UTF8_LOCALE_PROBE, str(tmp_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=True,
    )
    report = json.loads(result.stdout)
    if report["encoding"].replace("-", "").lower() == "utf8":
        pytest.skip("this platform kept a UTF-8 locale encoding")
    assert report["python_files"] == ["check_*.py"]
