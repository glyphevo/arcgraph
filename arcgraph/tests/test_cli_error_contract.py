"""A failing command must leave something a caller can parse.

JSON is this CLI's default output mode and `--human` is the opt-in, but until
this contract every non-`change` failure wrote one prose line to stderr and
left stdout empty. An agent parsing stdout received nothing at all and had to
recover the reason from stderr text plus an exit code whose two values invert
the usual reading: 2 is an expected application error, 1 is a crash.

These tests pin the envelope, and equally pin what did not move: the stderr
line, the exit codes, human output, and `--raw`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcgraph import SCHEMA_VERSION
from arcgraph.core.query_engine import SchemaVersionError
from arcgraph.interfaces.cli import main
from arcgraph.interfaces.cli_support import _cli_error_code


def _run(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_a_failing_command_puts_an_error_envelope_on_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(
        ["--repo-root", str(tmp_path), "--output-dir", "arcgraph", "stats"], capsys
    )

    assert code == 2
    # Unchanged: the operator still gets the one-line reason on stderr.
    assert err.startswith("ArcGraph: ")
    payload = json.loads(out)
    assert payload["status"] == "error"
    assert payload["command"] == "stats"
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["error_code"] == "ARCGRAPH_INPUT_NOT_FOUND"
    assert payload["error"]["code"] == payload["error_code"]
    assert payload["error"]["message"] in err


def test_the_envelope_cannot_be_mistaken_for_a_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A consumer that reads stdout must be able to tell failure from data."""

    _, out, _ = _run(
        ["--repo-root", str(tmp_path), "--output-dir", "arcgraph", "stats"], capsys
    )
    payload = json.loads(out)

    assert payload["status"] == "error"
    assert "error_code" in payload and "error" in payload


def test_human_output_is_unchanged_and_stays_off_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--human` asked for prose; it must not receive JSON instead."""

    code, out, err = _run(
        ["--repo-root", str(tmp_path), "--output-dir", "arcgraph", "--human", "stats"],
        capsys,
    )

    assert code == 2
    assert out == ""
    assert err.startswith("ArcGraph: ")


def test_an_internal_defect_keeps_its_traceback_and_still_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swallowing a TypeError would hide a bug; leaving stdout empty hid it too."""

    def boom(**_kwargs: object) -> dict[str, object]:
        raise TypeError("receiver was not a node")

    monkeypatch.setattr("arcgraph.interfaces.cli.version_info", boom)
    code, out, err = _run(["--repo-root", str(tmp_path), "version"], capsys)

    assert code == 1
    # The diagnostic a developer needs survives.
    assert "Traceback" in err
    assert "TypeError" in err
    payload = json.loads(out)
    assert payload["status"] == "error"
    assert payload["error_code"] == "ARCGRAPH_INTERNAL_ERROR"
    assert payload["error"]["message"] == "receiver was not a node"


def test_error_codes_distinguish_a_subclass_from_its_base() -> None:
    """SchemaVersionError subclasses RuntimeError and must not collapse into it."""

    assert _cli_error_code(SchemaVersionError("x")) == (
        "ARCGRAPH_SCHEMA_VERSION_UNSUPPORTED"
    )
    assert _cli_error_code(RuntimeError("x")) == "ARCGRAPH_RUNTIME_ERROR"
    assert _cli_error_code(FileNotFoundError("x")) == "ARCGRAPH_INPUT_NOT_FOUND"
    assert _cli_error_code(TypeError("x")) == "ARCGRAPH_INTERNAL_ERROR"
