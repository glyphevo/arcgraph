from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from arcgraph.interfaces.cli import main


def _sample_project(root: Path) -> None:
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "pkg" / "app.py").write_text("x = 1\n", encoding="utf-8")


@pytest.mark.parametrize(
    "version", ["3.10.16", "3.11.15", "3.12.13", "3.13.14", "3.14.6", "4.0.0"]
)
def test_doctor_enforces_declared_python_support(
    tmp_path: Path, capsys, monkeypatch, version: str
) -> None:
    from packaging.specifiers import SpecifierSet

    config = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    requires = config["project"]["requires-python"]
    supported = version in SpecifierSet(requires)
    _sample_project(tmp_path)
    monkeypatch.setattr("platform.python_version", lambda: version)

    # Doctor's exit code is unchanged: consumers must inspect its payload.
    assert main(["--repo-root", str(tmp_path), "doctor"]) == 0
    payload = json.loads(capsys.readouterr().out)
    check = next(c for c in payload["checks"] if c["name"] == "python_version")
    assert check["status"] == ("pass" if supported else "fail")
    if not supported:
        assert payload["status"] == "fail"
        assert requires in check["message"]
        assert "Recreate the tool environment" in check["fix"]


def test_doctor_no_index(tmp_path: Path, capsys) -> None:
    """Doctor should warn when there is no index."""
    _sample_project(tmp_path)
    exit_code = main(["--repo-root", str(tmp_path), "doctor"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] in ("warn", "fail")
    names = {c["name"] for c in payload["checks"]}
    assert "project_config" in names
    assert "source_roots" in names
    assert "index" in names
    assert "python_version" in names

    index_check = next(c for c in payload["checks"] if c["name"] == "index")
    assert index_check["status"] == "warn"
    assert "No index found" in index_check["message"]


def test_doctor_with_config_and_index(tmp_path: Path, capsys) -> None:
    """Doctor should pass project_config and index checks after init + build."""
    _sample_project(tmp_path)
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "sample"\nversion = "0.1.0"\n'
        "\n[tool.arcgraph]\n"
        'source_roots = ["src"]\n',
        encoding="utf-8",
    )

    # Build an index
    build_exit = main(
        [
            "--repo-root",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "output" / "arcgraph"),
            "build",
        ]
    )
    assert build_exit == 0
    capsys.readouterr()  # discard build output

    # Run doctor
    exit_code = main(
        [
            "--repo-root",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "output" / "arcgraph"),
            "doctor",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)

    config_check = next(c for c in payload["checks"] if c["name"] == "project_config")
    assert config_check["status"] == "pass"

    index_check = next(c for c in payload["checks"] if c["name"] == "index")
    assert index_check["status"] == "pass"
    assert "fresh" in index_check["message"]

    py_check = next(c for c in payload["checks"] if c["name"] == "python_version")
    assert py_check["status"] == "pass"


def test_doctor_human_output(tmp_path: Path, capsys) -> None:
    """Doctor human output should include status line and check names."""
    _sample_project(tmp_path)
    exit_code = main(["--repo-root", str(tmp_path), "--human", "doctor"])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "ArcGraph doctor:" in output
    assert "project_config" in output
    assert "source_roots" in output
    assert "python_version" in output


def test_doctor_treats_node_only_project_as_a_first_class_project(
    tmp_path: Path, capsys
) -> None:
    (tmp_path / "package.json").write_text(
        '{"name":"node-only","devDependencies":{"typescript":"5.9.3"}}',
        encoding="utf-8",
    )
    source = tmp_path / "src" / "index.ts"
    source.parent.mkdir()
    source.write_text("export const value = 1;\n", encoding="utf-8")

    assert main(["--repo-root", str(tmp_path), "doctor"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["project_languages"] == ["node"]
    config = next(
        item for item in payload["checks"] if item["name"] == "project_config"
    )
    assert config["status"] == "pass"
    assert "pyproject.toml is not required" in config["message"]
    roots = next(item for item in payload["checks"] if item["name"] == "source_roots")
    assert roots["status"] == "pass"
    assert "typescript_javascript=1" in roots["message"]
    assert any(item["name"] == "node_runtime" for item in payload["checks"])


def test_doctor_reports_node_probe_timeouts_instead_of_crashing(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    (tmp_path / "package.json").write_text(
        '{"name":"node-timeout","devDependencies":{"typescript":"5.9.3"}}',
        encoding="utf-8",
    )
    source = tmp_path / "src" / "index.ts"
    source.parent.mkdir()
    source.write_text("export const value = 1;\n", encoding="utf-8")
    real_run = subprocess.run

    def timeout_node(*args, **kwargs):
        command = args[0]
        if "--version" in command or "require.resolve('typescript')" in command:
            raise subprocess.TimeoutExpired(command, 10)
        return real_run(*args, **kwargs)

    monkeypatch.setattr("arcgraph.interfaces.cli.subprocess.run", timeout_node)

    assert main(["--repo-root", str(tmp_path), "doctor"]) == 0
    payload = json.loads(capsys.readouterr().out)

    node = next(item for item in payload["checks"] if item["name"] == "node_runtime")
    compiler = next(
        item for item in payload["checks"] if item["name"] == "typescript_compiler"
    )
    assert node["status"] == "fail"
    assert "timed out" in node["message"]
    assert compiler["status"] == "warn"
    assert "timed out" in compiler["message"]


def test_language_probe_prunes_ignored_directories(tmp_path: Path) -> None:
    """The probe must not descend into .git or node_modules: a post-hoc
    filter still enumerates every entry underneath them."""

    from arcgraph.interfaces.project_probe import walk_project_files

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    for ignored in (".git", "node_modules", ".venv", "dist"):
        nested = tmp_path / ignored / "deep" / "deeper"
        nested.mkdir(parents=True)
        (nested / "trap.py").write_text("x = 1\n", encoding="utf-8")

    walked = list(walk_project_files(tmp_path))

    assert (tmp_path / "src" / "main.py") in walked
    assert all(
        part not in {".git", "node_modules", ".venv", "dist"}
        for path in walked
        for part in path.relative_to(tmp_path).parts
    )


def test_doctor_supported_python_is_the_declared_one() -> None:
    from arcgraph.interfaces.cli_support import SUPPORTED_PYTHON

    config = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    low, high = SUPPORTED_PYTHON
    assert config["project"]["requires-python"] == (
        f">={low[0]}.{low[1]},<{high[0]}.{high[1]}"
    )


@pytest.mark.parametrize(
    ("requires", "running", "status", "fix"),
    [
        # mealie declares >=3.14,<3.15; on 3.12 nine of its files did not parse.
        (">=3.14,<3.15", "3.12.13", "warn", "with Python 3.14 or later"),
        (">=3.14,<3.15", "3.14.6", "pass", None),
        ("~=3.13", "3.12.13", "warn", "with Python 3.13 or later"),
        ("==3.13.*", "3.13.1", "pass", None),
        (">3.12", "3.12.13", "pass", None),
        (">=3.9", "3.11.15", "pass", None),
        (">=3.16", "3.14.6", "warn", "supports Python up to 3.14"),
    ],
)
def test_doctor_compares_the_project_python_with_its_own(
    tmp_path: Path, capsys, monkeypatch, requires, running, status, fix
) -> None:
    _sample_project(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "sample"\nrequires-python = "{requires}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("platform.python_version", lambda: running)
    assert main(["--repo-root", str(tmp_path), "doctor"]) == 0
    payload = json.loads(capsys.readouterr().out)
    check = next(c for c in payload["checks"] if c["name"] == "project_python")
    assert check["status"] == status
    assert requires in check["message"]
    if fix is None:
        assert "fix" not in check
    else:
        assert fix in check["fix"]


def test_doctor_has_no_project_python_check_without_a_declaration(
    tmp_path: Path, capsys
) -> None:
    _sample_project(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "sample"\n', encoding="utf-8"
    )
    assert main(["--repo-root", str(tmp_path), "doctor"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "project_python" not in {c["name"] for c in payload["checks"]}
