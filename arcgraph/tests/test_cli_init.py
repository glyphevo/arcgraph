from __future__ import annotations

import json
from pathlib import Path

from arcgraph.interfaces.cli import main


def _sample_project(repo_root: Path) -> None:
    (repo_root / "src" / "pkg").mkdir(parents=True)
    (repo_root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")


def test_cli_init_writes_arcgraph_config(tmp_path: Path, capsys) -> None:
    _sample_project(tmp_path)
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "sample"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )

    exit_code = main(["--repo-root", str(tmp_path), "init"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "written"
    assert payload["wrote"] is True
    text = pyproject.read_text(encoding="utf-8")
    assert "[tool.arcgraph]" in text
    assert 'source_roots = ["src"]' in text

    second_exit = main(["--repo-root", str(tmp_path), "init"])

    assert second_exit == 0
    second_payload = json.loads(capsys.readouterr().out)
    assert second_payload["action"] == "already_configured"
    assert pyproject.read_text(encoding="utf-8").count("[tool.arcgraph]") == 1


def test_cli_init_dry_run_ignores_ci_only_arcgraph_table(
    tmp_path: Path,
    capsys,
) -> None:
    _sample_project(tmp_path)
    pyproject = tmp_path / "pyproject.toml"
    original_text = (
        '[project]\nname = "sample"\nversion = "0.1.0"\n'
        "\n[tool.arcgraph.ci.semantic_quality_targets]\n"
        "overall_callsite_resolution_rate = 0.95\n"
    )
    pyproject.write_text(original_text, encoding="utf-8")

    exit_code = main(["--repo-root", str(tmp_path), "init", "--dry-run"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "dry_run"
    assert payload["wrote"] is False
    assert "[tool.arcgraph]" in payload["config_snippet"]
    assert pyproject.read_text(encoding="utf-8") == original_text


def test_cli_init_treats_source_roots_as_configured(
    tmp_path: Path,
    capsys,
) -> None:
    _sample_project(tmp_path)
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "sample"\nversion = "0.1.0"\n'
        "\n[tool.arcgraph]\n"
        'source_roots = ["src"]\n'
        "\n[tool.arcgraph.ci.semantic_quality_targets]\n"
        "overall_callsite_resolution_rate = 0.95\n",
        encoding="utf-8",
    )

    exit_code = main(["--repo-root", str(tmp_path), "init", "--dry-run"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "already_configured"
    assert payload["wrote"] is False


def test_cli_init_reports_manual_config_without_pyproject(
    tmp_path: Path,
    capsys,
) -> None:
    _sample_project(tmp_path)

    exit_code = main(["--repo-root", str(tmp_path), "init"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "manual_config"
    assert payload["pyproject_exists"] is False
    assert payload["wrote"] is False
    assert not (tmp_path / "pyproject.toml").exists()
    assert "[tool.arcgraph]" in payload["config_snippet"]


def test_cli_init_human_dry_run_does_not_modify_pyproject(
    tmp_path: Path,
    capsys,
) -> None:
    _sample_project(tmp_path)
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "sample"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )

    exit_code = main(["--repo-root", str(tmp_path), "--human", "init", "--dry-run"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "ArcGraph init: dry run" in output
    assert "Config snippet" in output
    assert "[tool.arcgraph]" not in pyproject.read_text(encoding="utf-8")
