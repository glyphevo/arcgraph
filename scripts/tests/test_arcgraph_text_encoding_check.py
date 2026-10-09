from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


def load_check_module() -> Any:
    script_path = (
        Path(__file__).resolve().parents[1] / "arcgraph_text_encoding_check.py"
    )
    spec = importlib.util.spec_from_file_location(
        "arcgraph_text_encoding_check", script_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves the module's string annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHECK = load_check_module()


def rules(source: str) -> list[str]:
    return [finding.rule for finding in CHECK.scan_source("sample.py", source)]


def test_repository_text_io_names_an_encoding() -> None:
    result = CHECK.check(CHECK.REPO_ROOT)

    assert result == {"findings": [], "stale_allowlist": []}


@pytest.mark.parametrize(
    "source,rule",
    [
        ("open(path)", "open"),
        ("open(path, 'w')", "open"),
        ("open(path, mode=chosen)", "open"),
        ("open(path, encoding=None)", "open"),
        ("open(path, **options)", "open"),
        ("import io\nio.open(path, 'r')", "open"),
        ("from io import open as io_open\nio_open(path)", "open"),
        ("path.open()", "method-open"),
        ("path.open('a')", "method-open"),
        ("import os\nos.fdopen(fd, 'w')", "os.fdopen"),
        ("import io\nio.TextIOWrapper(buffer)", "io.TextIOWrapper"),
        ("import tempfile\ntempfile.TemporaryFile('w+')", "tempfile"),
        ("import tempfile\ntempfile.NamedTemporaryFile(mode='w')", "tempfile"),
        ("import tempfile\ntempfile.SpooledTemporaryFile(10, 'w+')", "tempfile"),
        ("path.read_text()", "read_text"),
        ("path.read_text(errors='replace')", "read_text"),
        ("path.write_text(data)", "write_text"),
        ("import subprocess\nsubprocess.run(cmd, text=True)", "subprocess-text"),
        (
            "import subprocess\nsubprocess.check_output(cmd, universal_newlines=True)",
            "subprocess-text",
        ),
        ("import subprocess as sp\nsp.Popen(cmd, text=flag)", "subprocess-text"),
        ("from subprocess import run\nrun(cmd, text=True)", "subprocess-text"),
        ("import subprocess\nsubprocess.getoutput('git status')", "subprocess-locale"),
    ],
)
def test_text_io_in_the_locale_encoding_is_reported(source: str, rule: str) -> None:
    assert rules(source) == [rule]


@pytest.mark.parametrize(
    "source",
    [
        "open(path, 'rb')",
        "open(path, mode='wb')",
        "open(path, encoding='utf-8')",
        "open(path, 'r', -1, 'utf-8')",
        "path.open('rb')",
        "path.open('w', encoding='utf-8')",
        "path.read_text('utf-8')",
        "path.read_text(encoding='utf-8')",
        "path.write_text(data, 'utf-8')",
        "path.write_text(data, encoding='utf-8')",
        "path.read_bytes()",
        "import os\nos.open(path, os.O_RDONLY)",
        "import os\nos.fdopen(fd, 'wb')",
        "import os\nos.fdopen(fd, 'w', encoding='utf-8')",
        "import tarfile\ntarfile.open(path, 'r:gz')",
        "import webbrowser\nwebbrowser.open(url)",
        "import io\nio.TextIOWrapper(buffer, 'utf-8')",
        "import tempfile\ntempfile.TemporaryFile()",
        "import tempfile\ntempfile.NamedTemporaryFile('w', encoding='utf-8')",
        "import subprocess\nsubprocess.run(cmd)",
        "import subprocess\nsubprocess.run(cmd, text=False)",
        "import subprocess\nsubprocess.run(cmd, text=True, encoding='utf-8')",
    ],
)
def test_binary_or_explicitly_encoded_io_passes(source: str) -> None:
    assert rules(source) == []


def test_finding_names_the_call_without_line_breaks() -> None:
    [finding] = CHECK.scan_source(
        "pkg/sample.py", "x = 1\npath.write_text(\n    data,\n)\n"
    )

    assert (finding.path, finding.line, finding.call) == (
        "pkg/sample.py",
        2,
        "path.write_text( data, )",
    )


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, source in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    return tmp_path


def test_allowlisted_call_passes_and_unused_entry_is_stale(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "pkg/archive.py": "archive.open(member)\n",
            "pkg/other.py": "path.read_text()\n",
        },
    )
    allowed = {
        ("pkg/archive.py", "archive.open(member)"): "ZipFile.open returns bytes",
        ("pkg/gone.py", "path.open()"): "no longer present",
    }

    result = CHECK.check(root, allowed)

    assert [f["path"] for f in result["findings"]] == ["pkg/other.py"]
    assert result["stale_allowlist"] == [
        {"path": "pkg/gone.py", "call": "path.open()", "reason": "no longer present"}
    ]


def test_fixtures_and_untracked_files_are_not_scanned(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"arcgraph/tests/fixtures/sample/app.py": "open(path)\n"})
    (root / "untracked.py").write_text("open(path)\n", encoding="utf-8")

    assert CHECK.check(root, {}) == {"findings": [], "stale_allowlist": []}


def test_executed_non_py_fixture_is_checked(tmp_path: Path) -> None:
    path = "arcgraph/tests/fixtures/semantic_prototype/replay-stdio.txt"
    root = _repo(
        tmp_path,
        {path: "from pathlib import Path\nPath('recorded-lsp.json').read_text()\n"},
    )

    [finding] = CHECK.check(root, {})["findings"]

    assert (finding["path"], finding["line"], finding["rule"]) == (path, 2, "read_text")
    (root / path).write_text(
        "from pathlib import Path\nPath('recorded-lsp.json').read_text(encoding='utf-8')\n",
        encoding="utf-8",
    )
    assert path in CHECK.tracked_python_files(root)
    assert CHECK.check(root, {}) == {"findings": [], "stale_allowlist": []}


def test_main_exits_nonzero_on_findings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, {"tool.py": "path.write_text(data)\n"})

    assert CHECK.main(["--root", str(root)]) == 1
    assert '"rule": "write_text"' in capsys.readouterr().out
