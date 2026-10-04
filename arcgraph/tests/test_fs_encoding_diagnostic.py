from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

_CLI = (
    "import sys; from arcgraph.interfaces.cli import main; sys.exit(main(sys.argv[1:]))"
)
_FS_ENCODING_PROBE = "import sys; print(sys.getfilesystemencoding())"


def test_decodable_non_ascii_path_is_accepted() -> None:
    # Imported here so the end-to-end test below still runs against a scanner
    # without this function (the regression control).
    from arcgraph.core.scanner import require_decodable_path

    require_decodable_path(Path("项目") / "模块.py")


def test_undecodable_path_names_the_remedy() -> None:
    from arcgraph.core.scanner import require_decodable_path

    with pytest.raises(RuntimeError, match="PYTHONUTF8=1") as caught:
        require_decodable_path(Path("proj") / "\udce9\udca1.py")
    # The message itself must stay printable as UTF-8.
    str(caught.value).encode("utf-8")


def test_build_reports_a_non_ascii_project_under_an_ascii_file_system_encoding(
    tmp_path: Path,
) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONUTF8", "PYTHONIOENCODING"}
    }
    env.update(
        {"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
    )
    probe = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", _FS_ENCODING_PROBE],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=True,
    )
    if probe.stdout.strip().replace("-", "").lower() == "utf8":
        pytest.skip("this platform keeps a UTF-8 file-system encoding")
    project: Path = tmp_path / "项目"
    project.mkdir()
    (project / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", _CLI, "build"],
        cwd=project,
        capture_output=True,
        env=env,
        check=False,
    )

    stderr = result.stderr.decode("utf-8", "replace")
    assert result.returncode == 2, stderr
    assert "PYTHONUTF8=1" in stderr
    assert "Traceback" not in stderr
