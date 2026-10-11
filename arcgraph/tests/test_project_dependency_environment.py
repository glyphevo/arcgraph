"""Dependency source belongs to the analyzed project, never the tool host."""

import importlib.metadata
from types import SimpleNamespace

import pytest

from arcgraph.core.freshness import compute_freshness
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.reindexer import ArcGraphReindexer
from arcgraph.tests.test_generator_and_dynamic_exports import _targets

SOURCE = "from starlette.testclient import TestClient\ndef use(app):\n    client=TestClient(app)\n    client.get('/')\n"


def _environment(tmp_path, *, base="httpx2", version="1.0"):
    env = tmp_path / "repo/.venv"
    env.mkdir(parents=True, exist_ok=True)
    (env / "pyvenv.cfg").write_text("version = 3.14.6\n", encoding="utf-8")
    site = env / "lib/python3.14/site-packages"
    for name in ("starlette", base):
        (site / name).mkdir(parents=True, exist_ok=True)
        dist = site / (name + "-fixture.dist-info")
        dist.mkdir(exist_ok=True)
        (dist / "METADATA").write_text(
            f"Name: {name}\nVersion: {version}\n", encoding="utf-8"
        )
    (site / "starlette/testclient.py").write_text(
        f"import {base}\nclass TestClient({base}.Client): pass\n",
        encoding="utf-8",
    )
    (site / base / "_client.py").write_text(
        "class Client:\n    def get(self, *args): pass\n", encoding="utf-8"
    )
    return site


def _host(tmp_path, monkeypatch):
    root = tmp_path / "host"
    (root / "starlette").mkdir(parents=True)
    (root / "starlette/testclient.py").write_text(
        "import httpx\nclass TestClient(httpx.Client):\n    def get(self,*args):pass\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        importlib.metadata,
        "distribution",
        lambda name: SimpleNamespace(
            version="host", locate_file=lambda path: root / path
        ),
    )


def test_project_dependency_source_overrides_the_host_without_execution(
    tmp_path, monkeypatch
):
    _host(tmp_path, monkeypatch)
    site = _environment(tmp_path)
    (site / "starlette/__init__.py").write_text(
        "raise RuntimeError('dependency must not execute')\n", encoding="utf-8"
    )
    targets = _targets(tmp_path, {"user": SOURCE}, v2=True)
    assert "extsym:httpx2.Client.get" in targets
    assert "extsym:starlette.testclient.TestClient.get" not in targets
    metadata = GraphStoreReader.from_current(tmp_path / "out").metadata
    environment = metadata["extractor_metadata"]["python-v1-compat-shim"][
        "dependency_environment"
    ]
    assert environment["status"] == "available"
    assert environment["distributions"]["starlette"]["version"] == "1.0"
    assert environment["distributions"]["starlette"]["sources"]["testclient.py"]


def test_missing_project_environment_does_not_fall_back_to_host(tmp_path, monkeypatch):
    _host(tmp_path, monkeypatch)
    targets = _targets(tmp_path, {"user": SOURCE}, v2=True)
    assert not any(t.endswith(".get") for t in targets)
    nodes = GraphStoreReader.from_current(tmp_path / "out").read_nodes()
    assert any(
        n.kind == "diagnostic"
        and n.properties.get("dependency_reason") == "project_environment_missing"
        for n in nodes
    )


@pytest.mark.parametrize("change", ["version", "source", "removed"])
def test_dependency_change_invalidates_freshness_and_sync_matches_build(
    tmp_path, monkeypatch, change
):
    _host(tmp_path, monkeypatch)
    site = _environment(tmp_path)
    _targets(tmp_path, {"user": SOURCE}, v2=True)
    assert not compute_freshness(GraphStoreReader.from_current(tmp_path / "out")).stale
    if change == "version":
        (site / "starlette-fixture.dist-info/METADATA").write_text(
            "Name: starlette\nVersion: 2.0\n", encoding="utf-8"
        )
    elif change == "source":
        (site / "starlette/testclient.py").write_text(
            "import httpx2\nclass TestClient(httpx2.Client):\n    def get(self,*args):pass\n",
            encoding="utf-8",
        )
    else:
        (site / "starlette/testclient.py").unlink()
    freshness = compute_freshness(GraphStoreReader.from_current(tmp_path / "out"))
    assert freshness.stale
    assert "dependency_environment_changed" in freshness.reason
    result = ArcGraphReindexer(tmp_path / "repo", tmp_path / "out").reindex_changed()
    assert result["status"] == "reindexed"
    sync = GraphStoreReader.from_current(tmp_path / "out")
    assert not compute_freshness(sync).stale
    ArcGraphIndexer(tmp_path / "repo", tmp_path / "full", [SourceRoot("src")]).build()
    full = GraphStoreReader.from_current(tmp_path / "full")
    assert [n.model_dump() for n in sync.read_nodes()] == [
        n.model_dump() for n in full.read_nodes()
    ]
    assert [e.model_dump() for e in sync.read_edges()] == [
        e.model_dump() for e in full.read_edges()
    ]
    assert sync.metadata["extractor_metadata"] == full.metadata["extractor_metadata"]


def test_project_environment_with_only_httpx_keeps_its_definition(
    tmp_path, monkeypatch
):
    _host(tmp_path, monkeypatch)
    _environment(tmp_path, base="httpx")
    targets = _targets(tmp_path, {"user": SOURCE}, v2=True)
    assert "extsym:httpx.Client.get" in targets
    assert "extsym:starlette.testclient.TestClient.get" not in targets


def test_doctor_and_index_share_the_project_environment(tmp_path, monkeypatch):
    from arcgraph.interfaces.cli_support import python_checks

    _host(tmp_path, monkeypatch)
    _environment(tmp_path)
    checks = python_checks(tmp_path / "repo")
    check = next(
        (c for c in checks if c["name"] == "project_dependency_environment"), None
    )
    assert check is not None
    assert check["status"] == "pass"
    _targets(tmp_path, {"user": SOURCE}, v2=True)
    metadata = GraphStoreReader.from_current(tmp_path / "out").metadata
    recorded = metadata["extractor_metadata"]["python-v1-compat-shim"][
        "dependency_environment"
    ]
    assert recorded["path"] in check["message"]


def test_configured_environment_is_read_and_path_change_is_stale(tmp_path, monkeypatch):
    _host(tmp_path, monkeypatch)
    _environment(tmp_path)
    env = tmp_path / "repo/.venv"
    destination = tmp_path / "project-env"
    env.rename(destination)
    config = tmp_path / "repo/pyproject.toml"
    config.write_text(
        '[tool.arcgraph]\npython_environment="../project-env"\n', encoding="utf-8"
    )
    targets = _targets(tmp_path, {"user": SOURCE}, v2=True)
    assert "extsym:httpx2.Client.get" in targets
    config.write_text(
        '[tool.arcgraph]\npython_environment="../missing-env"\n', encoding="utf-8"
    )
    assert compute_freshness(GraphStoreReader.from_current(tmp_path / "out")).stale


def test_dependency_pth_files_do_not_execute_or_expand_source_roots(
    tmp_path, monkeypatch
):
    _host(tmp_path, monkeypatch)
    site = _environment(tmp_path)
    (site / "starlette/testclient.py").unlink()
    (site / "foreign.pth").write_text(
        str(tmp_path / "host") + "\nimport missing\n", encoding="utf-8"
    )
    targets = _targets(tmp_path, {"user": SOURCE}, v2=True)
    assert not any(t.endswith(".get") for t in targets)


def test_external_method_requires_a_definition_in_the_project_dependency(
    tmp_path, monkeypatch
):
    _host(tmp_path, monkeypatch)
    site = _environment(tmp_path)
    (site / "httpx2/_client.py").write_text("class Client:pass\n", encoding="utf-8")
    targets = _targets(tmp_path, {"user": SOURCE}, v2=True)
    assert not any(t.endswith(".get") for t in targets)
