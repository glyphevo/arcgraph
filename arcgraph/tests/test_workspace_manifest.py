from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.cli import main
from arcgraph.interfaces.workspace import (
    load_workspace_manifest,
    workspace_context,
    workspace_resolve,
    workspace_status,
)
from arcgraph.pipeline.indexer import ArcGraphIndexer

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"


def test_workspace_status_summarizes_repositories(tmp_path: Path) -> None:
    repo_a = _copy_fixture_repo(tmp_path, "repo-a")
    repo_b = _copy_fixture_repo(tmp_path, "repo-b")
    _build_repo(repo_a, repo_a / "output" / "arcgraph")
    _build_repo(repo_b, tmp_path / "indexes" / "repo-b")
    (tmp_path / "missing-index").mkdir()
    manifest = tmp_path / "arcgraph.workspace.toml"
    manifest.write_text(
        "\n".join(
            [
                'schema_version = "1.0"',
                'name = "sample-workspace"',
                "",
                "[[repositories]]",
                'id = "repo-a"',
                'path = "repo-a"',
                'role = "service"',
                "",
                "[[repositories]]",
                'id = "repo-b"',
                'path = "repo-b"',
                'output_dir = "indexes/repo-b"',
                'role = "library"',
                "",
                "[[repositories]]",
                'id = "missing-index"',
                'path = "missing-index"',
            ]
        ),
        encoding="utf-8",
    )

    payload = workspace_status(manifest)

    assert payload["schema"] == "ArcGraphWorkspaceStatus"
    assert payload["status"] == "partial"
    assert payload["summary"]["repositories"] == 3
    assert payload["summary"]["available"] == 2
    assert payload["summary"]["missing_index"] == 1
    by_id = {repo["id"]: repo for repo in payload["repositories"]}
    assert by_id["repo-a"]["status"] == "available"
    assert by_id["repo-a"]["current_index"] is True
    assert by_id["repo-a"]["counts"]["nodes"] > 0
    assert by_id["repo-a"]["ci"]["summary"]["fail"] == 0
    assert by_id["repo-b"]["output_dir"] == str(
        (tmp_path / "indexes" / "repo-b").resolve()
    )
    assert by_id["missing-index"]["status"] == "missing_index"
    assert by_id["missing-index"]["current_index"] is False
    assert payload["capability_summary"]["symbols"]["available"] == 2
    assert payload["catalog"]["summary"]["repositories"] == 2
    assert "dependency_hints" in payload


def test_workspace_status_keeps_missing_path_per_repo(tmp_path: Path) -> None:
    repo = _copy_fixture_repo(tmp_path, "repo-a")
    _build_repo(repo, repo / "output" / "arcgraph")
    manifest = tmp_path / "arcgraph.workspace.toml"
    manifest.write_text(
        "\n".join(
            [
                'schema_version = "1.0"',
                'name = "sample-workspace"',
                "",
                "[[repositories]]",
                'id = "repo-a"',
                'path = "repo-a"',
                "",
                "[[repositories]]",
                'id = "missing-path"',
                'path = "does-not-exist"',
            ]
        ),
        encoding="utf-8",
    )

    payload = workspace_status(manifest)

    by_id = {repo["id"]: repo for repo in payload["repositories"]}
    assert payload["status"] == "partial"
    assert by_id["missing-path"]["status"] == "unavailable"
    assert by_id["missing-path"]["reason"] == "path_missing"
    assert "missing-path:" in payload["warnings"][0]


def test_workspace_manifest_rejects_duplicate_repo_ids(tmp_path: Path) -> None:
    manifest = tmp_path / "arcgraph.workspace.toml"
    manifest.write_text(
        "\n".join(
            [
                'schema_version = "1.0"',
                'name = "bad-workspace"',
                "",
                "[[repositories]]",
                'id = "repo"',
                'path = "."',
                "",
                "[[repositories]]",
                'id = "repo"',
                'path = "."',
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Duplicate workspace repository id"):
        load_workspace_manifest(manifest)


def test_workspace_status_cli_outputs_json(tmp_path: Path, capsys) -> None:
    repo = _copy_fixture_repo(tmp_path, "repo-a")
    _build_repo(repo, repo / "output" / "arcgraph")
    manifest = tmp_path / "arcgraph.workspace.toml"
    manifest.write_text(
        "\n".join(
            [
                'schema_version = "1.0"',
                'name = "sample-workspace"',
                "",
                "[[repositories]]",
                'id = "repo-a"',
                'path = "repo-a"',
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(["workspace", "status", "--config", str(manifest)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "sample-workspace"
    assert payload["summary"]["available"] == 1
    assert payload["repositories"][0]["id"] == "repo-a"
    assert "catalog" in payload
    assert "dependency_hints" in payload


def test_workspace_status_catalog_and_dependency_hints(tmp_path: Path) -> None:
    repo_a = _copy_fixture_repo(tmp_path, "repo-a")
    repo_b = _copy_fixture_repo(tmp_path, "repo-b")
    repo_c = _copy_fixture_repo(tmp_path, "repo-c")
    _write_pyproject(repo_a, name="service-a", dependencies=["repo-b>=1"])
    _write_pyproject(repo_b, name="repo-b", dependencies=[])
    _write_pyproject(repo_c, name="repo-c", dependencies=[])
    _write_python(repo_a / "src" / "pkg" / "cross_repo.py", "import libb\n")
    _write_python(repo_b / "src" / "libb" / "__init__.py", "VALUE = 1\n")
    _write_python(repo_c / "src" / "libb" / "__init__.py", "VALUE = 2\n")
    _build_repo(repo_a, repo_a / "output" / "arcgraph")
    _build_repo(repo_b, repo_b / "output" / "arcgraph")
    _build_repo(repo_c, repo_c / "output" / "arcgraph")
    manifest = tmp_path / "arcgraph.workspace.toml"
    manifest.write_text(
        "\n".join(
            [
                'schema_version = "1.0"',
                'name = "sample-workspace"',
                "",
                "[[repositories]]",
                'id = "repo-a"',
                'path = "repo-a"',
                "",
                "[[repositories]]",
                'id = "repo-b"',
                'path = "repo-b"',
                "",
                "[[repositories]]",
                'id = "repo-c"',
                'path = "repo-c"',
            ]
        ),
        encoding="utf-8",
    )

    payload = workspace_status(manifest)

    catalogs = {repo["repo_id"]: repo for repo in payload["catalog"]["repositories"]}
    assert catalogs["repo-a"]["project_name"] == "service-a"
    assert catalogs["repo-a"]["declared_dependencies"] == [
        {
            "name": "repo-b",
            "normalized_name": "repo-b",
            "requirement": "repo-b>=1",
            "source": "project.dependencies",
        }
    ]
    assert "libb" in catalogs["repo-b"]["top_level_modules"]
    assert "libb" in catalogs["repo-c"]["top_level_modules"]

    declared = [
        hint
        for hint in payload["dependency_hints"]
        if hint["kind"] == "declared_dependency"
    ]
    assert declared == [
        {
            "source_repo_id": "repo-a",
            "target_repo_id": "repo-b",
            "kind": "declared_dependency",
            "evidence": {
                "type": "declared_dependency",
                "dependency": "repo-b",
                "source": "project.dependencies",
            },
            "confidence": "confirmed",
            "reason": "Declared dependency 'repo-b' matches a workspace repository.",
        }
    ]
    ambiguous = [
        hint
        for hint in payload["dependency_hints"]
        if hint["kind"] == "ambiguous"
        and hint["evidence"]["type"] == "indexed_import"
        and hint["evidence"]["module"] == "libb"
    ]
    assert ambiguous == [
        {
            "source_repo_id": "repo-a",
            "candidates": ["repo-b", "repo-c"],
            "kind": "ambiguous",
            "evidence": {
                "type": "indexed_import",
                "module": "libb",
                "targets": ["ext:libb"],
            },
            "confidence": "heuristic",
            "reason": "Indexed import 'libb' matches a workspace repository. Multiple workspace repositories match.",
        }
    ]
    assert any("ambiguous" in warning for warning in payload["warnings"])


def test_workspace_status_can_omit_dependency_hints(tmp_path: Path, capsys) -> None:
    repo = _copy_fixture_repo(tmp_path, "repo-a")
    _write_pyproject(repo, name="repo-a", dependencies=[])
    _build_repo(repo, repo / "output" / "arcgraph")
    manifest = tmp_path / "arcgraph.workspace.toml"
    manifest.write_text(
        "\n".join(
            [
                'schema_version = "1.0"',
                'name = "sample-workspace"',
                "",
                "[[repositories]]",
                'id = "repo-a"',
                'path = "repo-a"',
            ]
        ),
        encoding="utf-8",
    )

    payload = workspace_status(manifest, include_dependency_hints=False)
    assert "catalog" in payload
    assert "dependency_hints" not in payload

    exit_code = main(
        ["workspace", "status", "--config", str(manifest), "--no-dependency-hints"]
    )

    assert exit_code == 0
    cli_payload = json.loads(capsys.readouterr().out)
    assert "catalog" in cli_payload
    assert "dependency_hints" not in cli_payload


def test_workspace_resolve_routes_repo_project_dependency_and_path(
    tmp_path: Path, capsys
) -> None:
    repo_a, repo_b, repo_c, manifest = _build_cross_repo_workspace(tmp_path)

    repo_match = workspace_resolve(manifest, "repo-b", kind="repo")
    assert repo_match["status"] == "matched"
    assert repo_match["matches"][0]["repo_id"] == "repo-b"
    assert repo_match["matches"][0]["confidence"] == "confirmed"
    assert repo_match["matches"][0]["matched_fields"] == ["repo_id"]

    project_match = workspace_resolve(manifest, "repo.b", kind="project")
    assert project_match["status"] == "matched"
    assert project_match["matches"][0]["repo_id"] == "repo-b"
    assert project_match["matches"][0]["confidence"] == "confirmed"
    assert project_match["matches"][0]["matched_fields"] == ["project_name"]

    dependency_match = workspace_resolve(manifest, "repo-b", kind="dependency")
    assert dependency_match["status"] == "matched"
    assert dependency_match["matches"][0]["repo_id"] == "repo-b"
    assert dependency_match["matches"][0]["matched_fields"] == ["declared_dependencies"]
    assert any(
        hint["kind"] == "declared_dependency"
        for hint in dependency_match["dependency_hints"]
    )

    path_match = workspace_resolve(
        manifest, str(repo_a / "src" / "pkg" / "service.py"), kind="path"
    )
    assert path_match["status"] == "matched"
    assert path_match["matches"][0]["repo_id"] == "repo-a"
    assert path_match["matches"][0]["matched_fields"] == ["path"]

    outside = workspace_resolve(manifest, str(tmp_path / "outside.py"), kind="path")
    assert outside["status"] == "not_found"
    assert any(
        "did not match any repository path" in warning
        for warning in outside["warnings"]
    )

    exit_code = main(["workspace", "resolve", "repo-b", "--config", str(manifest)])
    assert exit_code == 0
    cli_payload = json.loads(capsys.readouterr().out)
    assert cli_payload["status"] == "matched"
    assert any(
        command["repo_id"] == "repo-b"
        and "arcgraph context" in " ".join(command["commands"])
        for command in cli_payload["suggested_commands"]
    )


def test_workspace_resolve_reports_ambiguous_module_and_can_omit_hints(
    tmp_path: Path,
) -> None:
    _repo_a, _repo_b, _repo_c, manifest = _build_cross_repo_workspace(tmp_path)

    module_match = workspace_resolve(manifest, "libb", kind="module")
    assert module_match["status"] == "ambiguous"
    assert module_match["matches"] == [
        {
            "repo_id": None,
            "role": None,
            "path": None,
            "project_name": None,
            "matched_fields": ["top_level_modules"],
            "confidence": "heuristic",
            "resolution_status": "ambiguous",
            "candidates": [
                {
                    "repo_id": "repo-b",
                    "role": "repository",
                    "path": str((tmp_path / "repo-b").resolve()),
                    "project_name": "repo-b",
                    "commit_sha": None,
                    "index_version": module_match["matches"][0]["candidates"][0][
                        "index_version"
                    ],
                },
                {
                    "repo_id": "repo-c",
                    "role": "repository",
                    "path": str((tmp_path / "repo-c").resolve()),
                    "project_name": "repo-c",
                    "commit_sha": None,
                    "index_version": module_match["matches"][0]["candidates"][1][
                        "index_version"
                    ],
                },
            ],
            "reason": "'libb' matches repository top-level modules. Multiple workspace repositories match.",
            "evidence": [
                {
                    "type": "top_level_modules",
                    "value": "libb",
                    "matched_values": ["libb"],
                }
            ],
            "query": "libb",
        }
    ]

    no_hints = workspace_resolve(
        manifest, "repo-b", kind="dependency", include_dependency_hints=False
    )
    assert no_hints["status"] == "matched"
    assert "dependency_hints" not in no_hints


def test_workspace_context_returns_per_repo_context_for_dependency(
    tmp_path: Path,
) -> None:
    repo_a, _repo_b, _repo_c, manifest = _build_cross_repo_workspace(tmp_path)

    payload = workspace_context(manifest, "repo-b", kind="dependency")

    assert payload["schema"] == "ArcGraphWorkspaceContext"
    assert payload["status"] == "available"
    assert payload["query"]["detail_level"] == "summary"
    assert payload["resolve"]["status"] == "matched"
    assert payload["repo_contexts"][0]["repo_id"] == "repo-b"
    assert payload["repo_contexts"][0]["status"] == "available"
    assert payload["repo_contexts"][0]["match_status"] == "matched"
    assert payload["repo_contexts"][0]["context"]["schema_version"]
    assert payload["repo_contexts"][0]["context"]["detail_level"] == "summary"
    _assert_no_raw_payload_keys(payload)

    path_payload = workspace_context(
        manifest, str(repo_a / "src" / "pkg" / "service.py"), kind="path"
    )
    assert path_payload["status"] == "available"
    assert path_payload["repo_contexts"][0]["repo_id"] == "repo-a"
    assert path_payload["repo_contexts"][0]["context"]["status"] == "available"
    assert path_payload["repo_contexts"][0]["context"]["targets"] == [
        str(repo_a / "src" / "pkg" / "service.py")
    ]


def test_workspace_context_expands_ambiguous_candidates_and_applies_limit(
    tmp_path: Path,
) -> None:
    _repo_a, _repo_b, _repo_c, manifest = _build_cross_repo_workspace(tmp_path)

    payload = workspace_context(manifest, "libb", kind="module", limit=2)

    assert payload["status"] == "available"
    assert payload["resolve"]["status"] == "ambiguous"
    assert [item["repo_id"] for item in payload["repo_contexts"]] == [
        "repo-b",
        "repo-c",
    ]
    assert {item["match_status"] for item in payload["repo_contexts"]} == {
        "ambiguous_candidate"
    }
    assert payload["truncation"]["truncated"] is False

    limited = workspace_context(manifest, "libb", kind="module", limit=1)
    assert [item["repo_id"] for item in limited["repo_contexts"]] == ["repo-b"]
    assert limited["truncation"]["truncated"] is True


def test_workspace_context_keeps_repo_errors_per_context(tmp_path: Path) -> None:
    repo = _copy_fixture_repo(tmp_path, "repo-a")
    _build_repo(repo, repo / "output" / "arcgraph")
    (tmp_path / "missing-index").mkdir()
    manifest = tmp_path / "arcgraph.workspace.toml"
    manifest.write_text(
        "\n".join(
            [
                'schema_version = "1.0"',
                'name = "sample-workspace"',
                "",
                "[[repositories]]",
                'id = "repo-a"',
                'path = "repo-a"',
                "",
                "[[repositories]]",
                'id = "missing-index"',
                'path = "missing-index"',
            ]
        ),
        encoding="utf-8",
    )

    payload = workspace_context(manifest, "missing-index", kind="repo")

    assert payload["status"] == "partial"
    assert len(payload["repo_contexts"]) == 1
    context = payload["repo_contexts"][0]
    assert context["repo_id"] == "missing-index"
    assert context["repo_path"] == str((tmp_path / "missing-index").resolve())
    assert context["match_status"] == "matched"
    assert context["match"]["matched_fields"] == ["repo_id"]
    assert context["status"] == "unavailable"
    assert "No ArcGraph current index" in context["error"]
    assert any("missing-index:" in warning for warning in payload["warnings"])


def test_workspace_context_cli_and_detail_validation(tmp_path: Path, capsys) -> None:
    _repo_a, _repo_b, _repo_c, manifest = _build_cross_repo_workspace(tmp_path)

    exit_code = main(
        [
            "workspace",
            "context",
            "repo-b",
            "--config",
            str(manifest),
            "--kind",
            "dependency",
            "--detail-level",
            "standard",
            "--no-dependency-hints",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "ArcGraphWorkspaceContext"
    assert payload["query"]["detail_level"] == "standard"
    assert payload["repo_contexts"][0]["context"]["detail_level"] == "standard"
    assert "dependency_hints" not in payload["resolve"]
    _assert_no_raw_payload_keys(payload)

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "workspace",
                "context",
                "repo-b",
                "--config",
                str(manifest),
                "--detail-level",
                "detailed",
            ]
        )
    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "invalid choice" in captured.err


def test_workspace_status_cli_reports_missing_manifest(capsys) -> None:
    exit_code = main(["workspace", "status", "--config", "missing.workspace.toml"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "workspace manifest not found" in captured.err
    assert "Traceback" not in captured.err


def _build_cross_repo_workspace(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path]:
    repo_a = _copy_fixture_repo(tmp_path, "repo-a")
    repo_b = _copy_fixture_repo(tmp_path, "repo-b")
    repo_c = _copy_fixture_repo(tmp_path, "repo-c")
    _write_pyproject(repo_a, name="service-a", dependencies=["repo-b>=1"])
    _write_pyproject(repo_b, name="repo-b", dependencies=[])
    _write_pyproject(repo_c, name="repo-c", dependencies=[])
    _write_python(repo_a / "src" / "pkg" / "cross_repo.py", "import libb\n")
    _write_python(repo_b / "src" / "libb" / "__init__.py", "VALUE = 1\n")
    _write_python(repo_c / "src" / "libb" / "__init__.py", "VALUE = 2\n")
    _build_repo(repo_a, repo_a / "output" / "arcgraph")
    _build_repo(repo_b, repo_b / "output" / "arcgraph")
    _build_repo(repo_c, repo_c / "output" / "arcgraph")
    manifest = tmp_path / "arcgraph.workspace.toml"
    manifest.write_text(
        "\n".join(
            [
                'schema_version = "1.0"',
                'name = "sample-workspace"',
                "",
                "[[repositories]]",
                'id = "repo-a"',
                'path = "repo-a"',
                "",
                "[[repositories]]",
                'id = "repo-b"',
                'path = "repo-b"',
                "",
                "[[repositories]]",
                'id = "repo-c"',
                'path = "repo-c"',
            ]
        ),
        encoding="utf-8",
    )
    return repo_a, repo_b, repo_c, manifest


def _copy_fixture_repo(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURE_ROOT, target)
    return target


def _write_pyproject(repo: Path, *, name: str, dependencies: list[str]) -> None:
    dependency_lines = ", ".join(json.dumps(item) for item in dependencies)
    repo.joinpath("pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                f"name = {json.dumps(name)}",
                'version = "1.0.0"',
                f"dependencies = [{dependency_lines}]",
            ]
        ),
        encoding="utf-8",
    )


def _write_python(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _build_repo(repo_root: Path, output_dir: Path) -> None:
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()


def _assert_no_raw_payload_keys(value: object) -> None:
    if isinstance(value, dict):
        assert "properties" not in value
        assert "snippet" not in value
        assert "source_snippet" not in value
        for item in value.values():
            _assert_no_raw_payload_keys(item)
        return
    if isinstance(value, list):
        for item in value:
            _assert_no_raw_payload_keys(item)
