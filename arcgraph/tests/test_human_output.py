"""Cover the rendering path a human actually sees.

JSON is the default output; `--human` (cli.py:397) is the opt-in that reaches
these formatters. So this is not the most-executed path — an earlier note in
this repository had that backwards — but it is the only path whose sole
consumer is a person, which is what makes it worth covering: every payload
field is read through `.get`, so the risk is not a crash on an unexpected
shape but a verdict that renders without the thing that made it a verdict. A
failing check that is counted and never named, a stale index whose stale files
are dropped, a truncated list that misreports its own total — for each of
those, the reader who asked for human output is the only detector.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from arcgraph.interfaces import human_output
from arcgraph.interfaces.human_output import (
    format_build_human,
    format_ci_human,
    format_current_human,
    format_doctor_human,
    format_generic_human,
    format_init_human,
)


@pytest.mark.parametrize(
    "render",
    [
        lambda: format_build_human({}, 0.0),
        lambda: format_current_human({}),
        lambda: format_ci_human({}),
        lambda: format_init_human({}),
        lambda: format_doctor_human({}),
        lambda: format_generic_human({}, None),
    ],
    ids=["build", "current", "ci", "init", "doctor", "generic"],
)
def test_every_formatter_renders_an_empty_payload(render: Any) -> None:
    """The default output path must survive a payload it has never seen."""

    rendered = render()
    assert isinstance(rendered, str)


def test_ci_names_every_failing_and_warning_check() -> None:
    """A count alone tells a reader something broke but not what."""

    payload = {
        "status": "fail",
        "checks": [
            {"name": "quiet_pass", "status": "pass", "message": "fine"},
            {"name": "loud_fail", "status": "fail", "message": "budget exceeded"},
            {"name": "loud_warn", "status": "warn", "message": "baseline drifted"},
            {"name": "skipped", "status": "skip", "message": "not applicable"},
        ],
    }
    rendered = format_ci_human(payload)

    assert "loud_fail" in rendered
    assert "budget exceeded" in rendered
    assert "loud_warn" in rendered
    assert "baseline drifted" in rendered
    assert "1 pass, 1 warn, 1 fail, 1 skip" in rendered
    # A passing check is summarised, not listed, so the failures stay readable.
    assert "quiet_pass" not in rendered


def test_doctor_shows_the_fix_only_where_something_is_wrong() -> None:
    payload = {
        "status": "warn",
        "summary": {"pass": 1, "warn": 1, "fail": 0},
        "checks": [
            {
                "name": "node_toolchain",
                "status": "warn",
                "message": "node not found",
                "fix": "install Node 20+",
            },
            {
                "name": "index_present",
                "status": "pass",
                "message": "index found",
                "fix": "should not be offered",
            },
        ],
    }
    rendered = format_doctor_human(payload)

    assert "install Node 20+" in rendered
    assert "should not be offered" not in rendered
    assert "1 pass, 1 warn, 0 fail" in rendered


def test_current_reports_staleness_and_keeps_the_true_stale_total() -> None:
    """A truncated list must still say how many it stands for."""

    payload = {
        "freshness": {
            "status": "stale",
            "stale_files": ["a.py", "b.py", "c.py", "d.py", "e.py"],
        },
        "schema_version": "1.0",
        "index_version": "7",
        "commit_sha": "0123456789abcdef0123",
        "file_count": 1234,
        "node_count": 5678,
        "edge_count": 91011,
    }
    rendered = format_current_human(payload)

    assert "Index is stale" in rendered
    assert "5 changed since index" in rendered
    assert "a.py" in rendered and "c.py" in rendered
    assert "... and 2 more" in rendered
    # Counts are grouped for a reader, and the commit is abbreviated, not lost.
    assert "1,234" in rendered
    assert "91,011" in rendered
    assert "0123456789ab" in rendered


def test_build_truncates_roots_without_misreporting_how_many() -> None:
    payload = {
        "source_root_detection": {
            "strategy": "pyproject",
            "roots": [f"src/pkg{i}" for i in range(9)],
        },
        "file_count": 10,
        "node_count": 20,
        "edge_count": 30,
        "warning_count": 4,
        "capabilities": {"precision": "high", "coverage": "partial"},
        "schema_version": "1.0",
    }
    rendered = format_build_human(payload, 0.25)

    assert "pyproject (9 roots)" in rendered
    assert "src/pkg4" in rendered
    assert "src/pkg5" not in rendered
    assert "... and 4 more" in rendered
    assert "precision=high" in rendered
    # A non-zero warning count carries the warning symbol, not just a number.
    assert f"4 {human_output.SYM_WARN}" in rendered
    assert "250ms" in rendered


def test_build_duration_is_readable_at_every_scale() -> None:
    assert "250ms" in format_build_human({}, 0.25)
    assert "5.0s" in format_build_human({}, 5.0)
    assert "2m 5.0s" in format_build_human({}, 125.0)


def test_generic_fallback_summarises_unknown_payloads_and_hides_internals() -> None:
    rendered = format_generic_human(
        {"kind": "route", "hits": [1, 2, 3], "meta": {"a": 1}, "_exit_code": 3},
        "some-unrouted-command",
    )

    assert "kind" in rendered and "route" in rendered
    assert "[3 items]" in rendered
    assert "{1 keys}" in rendered
    assert "_exit_code" not in rendered


def test_generic_dispatches_to_the_command_specific_formatter() -> None:
    payload = {"status": "pass", "checks": []}
    assert format_generic_human(payload, "ci") == format_ci_human(payload)


def test_symbols_degrade_when_the_terminal_cannot_encode_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ASCII terminal gets words, not mojibake, in the default output."""

    class _AsciiStdout:
        encoding = "ascii"

    monkeypatch.setattr(human_output.sys, "stdout", _AsciiStdout())
    reloaded = importlib.reload(human_output)
    try:
        assert reloaded.SYM_OK == "OK"
        assert reloaded.SYM_FAIL == "FAIL"
        rendered = reloaded.format_ci_human({"status": "fail", "checks": []})
        assert "FAIL" in rendered
        assert "✗" not in rendered
    finally:
        monkeypatch.undo()
        importlib.reload(human_output)


def test_capabilities_disclose_the_keys_they_do_not_render() -> None:
    """Four pills out of twenty-five must not read as four capabilities.

    `_capability_pills` highlights a fixed short list. Without a count of the
    rest, a reader cannot distinguish a capability that was omitted from the
    rendering from one the index does not have — and the real payload carries
    twenty-five keys, of which four are highlighted.
    """

    caps = {
        "precision": "full",
        "coverage": "partial",
        "architecture": "available",
        "similarity": "available",
        "types": "available",
    }
    rendered = format_build_human({"capabilities": caps}, 0.0)

    assert "precision=full" in rendered
    assert "coverage=partial" in rendered
    assert "... and 3 more" in rendered
    # The omitted ones are counted, not named: naming twenty-one would bury the
    # four that were chosen for being worth reading first.
    assert "architecture" not in rendered


def test_capabilities_say_nothing_extra_when_nothing_is_omitted() -> None:
    caps = {"precision": "full", "coverage": "partial"}
    rendered = format_build_human({"capabilities": caps}, 0.0)

    assert "precision=full" in rendered
    assert "more" not in rendered


def test_capabilities_disclose_omissions_even_with_no_highlighted_key() -> None:
    """A payload of only unhighlighted keys must not render as no capabilities."""

    rendered = format_build_human(
        {"capabilities": {"architecture": "available", "types": "available"}}, 0.0
    )

    assert "... and 2 more" in rendered
