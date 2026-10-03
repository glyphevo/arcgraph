from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from arcgraph.interfaces import cli, mcp_server, stdio_encoding

_CLI = (
    "import sys; from arcgraph.interfaces.cli import main; sys.exit(main(sys.argv[1:]))"
)
_LOCALE_PROBE = "import locale; print(locale.getpreferredencoding(False))"


def _non_utf8_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONUTF8", "PYTHONIOENCODING"}
    }
    env.update(
        {"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
    )
    return env


def test_cli_json_survives_a_pipe_with_a_non_utf8_locale(tmp_path: Path) -> None:
    # Agents read CLI JSON through a pipe; with a non-UTF-8 code page, a name
    # the code page cannot represent used to raise UnicodeEncodeError.
    # Paths stay ASCII: under this locale Linux decodes file names as ASCII,
    # a separate limitation from the one tested here.
    env = _non_utf8_env()
    probe = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", _LOCALE_PROBE],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=True,
    )
    if probe.stdout.strip().replace("-", "").lower() == "utf8":
        pytest.skip("this platform kept a UTF-8 locale encoding")
    project: Path = tmp_path / "project"
    project.mkdir()
    (project / "mod.py").write_text(
        "def 帮助():\n    return 1\n\n\ndef main():\n    return 帮助()\n",
        encoding="utf-8",
    )

    outputs = []
    for command in (["build"], ["current"], ["explain", "mod.main"]):
        result = subprocess.run(
            [sys.executable, "-X", "utf8=0", "-c", _CLI, *command],
            cwd=project,
            capture_output=True,
            env=env,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        outputs.append(json.loads(result.stdout.decode("utf-8")))

    text: str = json.dumps(outputs, ensure_ascii=False)
    assert "mod.帮助" in text


def test_utf8_stdio_reencodes_text_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    out_buffer = io.BytesIO()
    err_buffer = io.BytesIO()
    stdout = io.TextIOWrapper(out_buffer, encoding="ascii", errors="strict")
    stderr = io.TextIOWrapper(err_buffer, encoding="ascii", errors="backslashreplace")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    stdio_encoding.use_utf8_stdio()
    stdout.write("项目")
    stdout.flush()
    stderr.write("模块")
    stderr.flush()

    assert out_buffer.getvalue() == "项目".encode("utf-8")
    assert err_buffer.getvalue() == "模块".encode("utf-8")
    assert stdout.errors == "strict"
    assert stderr.errors == "backslashreplace"


def test_utf8_stdio_leaves_streams_without_reconfigure_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    stdio_encoding.use_utf8_stdio()


@pytest.mark.parametrize("module", [cli, mcp_server])
def test_both_entry_points_switch_stdio_to_utf8(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(module, "use_utf8_stdio", lambda: calls.append("called"))

    with pytest.raises(SystemExit):
        module.main(["--definitely-not-an-option"])  # type: ignore[attr-defined]

    assert calls == ["called"]
