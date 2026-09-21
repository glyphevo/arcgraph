from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from arcgraph.core.payload_policy import STALE_INDEX_WARNING_KIND
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.recovery import STALE_RECOVERY_COMMAND
from arcgraph.core.schemas import ContextRequest
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.cli import main
from arcgraph.interfaces.mcp_tools import ArcGraphMCPConfig, ArcGraphMCPToolGroup
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.providers.context_provider import ContextProvider

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"
TARGET = "pkg.service.MemoryService.create_memory"


def _stale_index(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = tmp_path / "sample_project"
    shutil.copytree(FIXTURE_ROOT, repo_root)
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    source = repo_root / "src" / "pkg" / "service.py"
    source.write_text(
        source.read_text(encoding="utf-8") + "\n# stale-index contract probe\n",
        encoding="utf-8",
    )
    return repo_root, output_dir


def _stale_warnings(payload: dict[str, object]) -> list[dict[str, object]]:
    warnings = payload.get("warnings")
    assert isinstance(warnings, list)
    return [
        warning
        for warning in warnings
        if isinstance(warning, dict) and warning.get("kind") == STALE_INDEX_WARNING_KIND
    ]


def test_provider_adds_one_path_free_warning_for_a_real_stale_index(
    tmp_path: Path,
) -> None:
    repo_root, output_dir = _stale_index(tmp_path)

    payload = ContextProvider(QueryEngine(output_dir)).get_context(
        ContextRequest(targets=[TARGET])
    )

    assert payload["freshness"]["status"] == "stale"
    assert _stale_warnings(payload) == [
        {
            "kind": "stale_index",
            "message": (
                f"Index is stale; run `{STALE_RECOVERY_COMMAND}` before relying "
                "on results."
            ),
            "stale_file_count": 1,
            "stale_module_count": 1,
            "freshness_sample_limit": 5,
            "freshness_details_omitted": 0,
            "detail": (
                "Freshness file/module lists are bounded on target-scoped "
                "responses; run `arcgraph current` for the complete lists."
            ),
        }
    ]
    assert str(repo_root) not in json.dumps(_stale_warnings(payload))
    assert "src/pkg/service.py" not in json.dumps(_stale_warnings(payload))
    assert payload["recovery_action"]["command"] == STALE_RECOVERY_COMMAND
    assert (
        f"`{payload['recovery_action']['command']}`"
        in _stale_warnings(payload)[0]["message"]
    )


def test_index_status_surfaces_report_the_shared_stale_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root, output_dir = _stale_index(tmp_path)

    current = QueryEngine(output_dir).current()
    assert current["schema_version"] == "1.0.0"
    assert current["freshness"]["status"] == "stale"
    assert len(_stale_warnings(current)) == 1

    base = [
        "--repo-root",
        str(repo_root),
        "--output-dir",
        str(output_dir),
    ]
    for command in ("current", "status"):
        assert main([*base, command]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["freshness"]["status"] == "stale"
        assert len(_stale_warnings(payload)) == 1

    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            repo_root,
            repo_id="sample",
            output_dir=output_dir,
            allowed_roots=[tmp_path],
        )
    )
    mcp_status = tools.arcgraph_index_status(repo_id="sample")
    assert mcp_status["schema_version"] == "1.0.0"
    assert mcp_status["freshness"]["status"] == "stale"
    assert len(_stale_warnings(mcp_status)) == 1
    serialized = json.dumps(_stale_warnings(mcp_status))
    assert str(repo_root) not in serialized
    assert "src/pkg/service.py" not in serialized


def test_cli_bounded_payload_warns_but_raw_bytes_match_query_engine(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root, output_dir = _stale_index(tmp_path)
    base = [
        "--repo-root",
        str(repo_root),
        "--output-dir",
        str(output_dir),
        "callers",
        TARGET,
    ]

    assert main(base) == 0
    bounded = json.loads(capsys.readouterr().out)
    assert _stale_warnings(bounded)

    expected = QueryEngine(output_dir).callers(TARGET)
    expected_stdout = (
        json.dumps(
            expected,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    )
    assert main([*base, "--raw"]) == 0

    assert capsys.readouterr().out == expected_stdout
    assert _stale_warnings(expected) == []


def test_mcp_bounded_payload_warns_for_a_real_stale_index(tmp_path: Path) -> None:
    repo_root, output_dir = _stale_index(tmp_path)
    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            repo_root,
            repo_id="sample",
            output_dir=output_dir,
            allowed_roots=[tmp_path],
        )
    )

    payload = tools.arcgraph_get_context(repo_id="sample", targets=[TARGET])

    assert payload["freshness"]["status"] == "stale"
    assert len(_stale_warnings(payload)) == 1
    assert _stale_warnings(payload)[0]["stale_file_count"] == 1
