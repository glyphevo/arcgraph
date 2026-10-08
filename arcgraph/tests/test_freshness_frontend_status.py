"""Freshness respects persisted, explicitly unavailable frontend declarations.

Native defaults always advertise available, even without Node. A caller-supplied
registry can override that declaration; build_language_tiers persists it. Keep
checking indexed files, but do not recover unobserved suffixes from an unavailable
tier. The existing empty-index Python fallback is a separate compatibility rule.
"""

from pathlib import Path

import pytest

from arcgraph.core.freshness import compute_freshness
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.core.schemas import FrontendCapabilities
from arcgraph.pipeline.frontends import (
    FrontendRegistry,
    PythonSemanticFrontend,
    TypeScriptSemanticFrontend,
)
from arcgraph.pipeline.indexer import ArcGraphIndexer


class _UnavailablePythonFrontend(PythonSemanticFrontend):
    def capabilities(self) -> FrontendCapabilities:
        capabilities = super().capabilities()
        capabilities.capabilities["language_tier_status"] = "unavailable"
        return capabilities


class _UnavailableTypeScriptFrontend(TypeScriptSemanticFrontend):
    def capabilities(self) -> FrontendCapabilities:
        capabilities = super().capabilities()
        capabilities.capabilities["language_tier_status"] = "unavailable"
        return capabilities


def test_native_tier_availability_does_not_depend_on_node_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    for frontend in (PythonSemanticFrontend(), TypeScriptSemanticFrontend()):
        assert (
            frontend.capabilities().capabilities["language_tier_status"] == "available"
        )


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_unavailable_tier_does_not_expand_freshness_scope(
    tmp_path: Path, language: str
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    hidden_suffix = ".py" if language == "python" else ".ts"
    visible_suffix = ".ts" if language == "python" else ".py"
    sources = {".py": "value = 1\n", ".ts": "export const value = 1;\n"}
    hidden = "src/hidden" + hidden_suffix
    visible = "src/visible" + visible_suffix
    (tmp_path / hidden).write_text(sources[hidden_suffix], encoding="utf-8")
    (tmp_path / visible).write_text(sources[visible_suffix], encoding="utf-8")
    config = tmp_path / "pyproject.toml"
    config.write_text(
        '[tool.arcgraph]\nexclude = ["' + hidden + '"]\n', encoding="utf-8"
    )
    python = (
        _UnavailablePythonFrontend()
        if language == "python"
        else PythonSemanticFrontend()
    )
    typescript = (
        _UnavailableTypeScriptFrontend(tmp_path)
        if language == "typescript"
        else TypeScriptSemanticFrontend(tmp_path)
    )
    output = tmp_path / "output"
    ArcGraphIndexer(
        tmp_path,
        output,
        [SourceRoot("src")],
        frontend_registry=FrontendRegistry([python, typescript]),
    ).build()
    store = GraphStoreReader.from_current(output)
    assert store.metadata["language_tiers"][language]["status"] == "unavailable"
    assert [file.path for file in store.read_files()] == [visible]
    assert compute_freshness(store).status == "fresh"

    config.write_text("[tool.arcgraph]\n", encoding="utf-8")
    assert compute_freshness(store).status == "fresh"
    # The other, already indexed language lane is still checked.
    (tmp_path / visible).write_text(sources[visible_suffix] + "\n", encoding="utf-8")
    freshness = compute_freshness(store)
    assert freshness.status == "stale"
    assert freshness.stale_files == [visible]

    # A supplied frontend can still emit files despite its tier declaration.
    # Once indexed, those files must remain tracked even for an unavailable tier.
    ArcGraphIndexer(
        tmp_path,
        output,
        [SourceRoot("src")],
        frontend_registry=FrontendRegistry([python, typescript]),
    ).build()
    store = GraphStoreReader.from_current(output)
    assert store.metadata["language_tiers"][language]["status"] == "unavailable"
    assert compute_freshness(store).status == "fresh"
    (tmp_path / hidden).write_text(sources[hidden_suffix] + "\n", encoding="utf-8")
    freshness = compute_freshness(store)
    assert freshness.status == "stale"
    assert freshness.stale_files == [hidden]
