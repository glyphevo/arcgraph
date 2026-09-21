"""Human-readable CLI output formatters for ArcGraph."""

from __future__ import annotations

import sys
from typing import Any

# ---------------------------------------------------------------------------
#  Encoding-safe symbols (ASCII fallback for non-UTF-8 terminals)
# ---------------------------------------------------------------------------


def _can_encode_utf8() -> bool:
    enc = getattr(sys.stdout, "encoding", None) or ""
    return enc.lower().replace("-", "") in ("utf8", "utf_8", "utf8sig")


_UTF8 = _can_encode_utf8()
SYM_OK = "✓" if _UTF8 else "OK"
SYM_WARN = "⚠" if _UTF8 else "!"
SYM_FAIL = "✗" if _UTF8 else "FAIL"
SYM_BULLET = "•" if _UTF8 else "-"


# ---------------------------------------------------------------------------
#  Shared helpers
# ---------------------------------------------------------------------------


def _kv(label: str, value: str, indent: int = 2) -> str:
    """Render a key-value line with fixed-width label column."""
    return f"{' ' * indent}{label:<20} {value}"


def _capability_pills(caps: dict[str, str]) -> str:
    """Format capabilities as a compact two-column summary.

    Only a few keys are highlighted, and a payload normally carries many more.
    The count of the rest is disclosed rather than dropped: a reader who sees
    four lines must be able to tell that the others were omitted here, not
    absent from the index.
    """
    highlight_keys = [
        "precision",
        "coverage",
        "runtime_trace",
        "receiver_resolution",
    ]
    pills: list[str] = []
    for key in highlight_keys:
        val = caps.get(key)
        if val is not None:
            pills.append(f"{key}={val}")
    # Two per line
    lines: list[str] = []
    for i in range(0, len(pills), 2):
        pair = pills[i : i + 2]
        lines.append("  ".join(f"{p:<36}" for p in pair).rstrip())
    omitted = len(set(caps) - set(highlight_keys))
    if omitted:
        lines.append(f"... and {omitted} more, listed in the JSON output")
    return "\n".join(f"{'':>22}{line}" for line in lines) if lines else ""


def _fmt_count(n: int) -> str:
    """Format an integer with thousand separators."""
    return f"{n:,}"


# ---------------------------------------------------------------------------
#  Build output
# ---------------------------------------------------------------------------


def format_build_human(payload: dict[str, Any], duration_s: float) -> str:
    """Format a build result payload as a human-readable summary."""
    lines: list[str] = []

    lines.append("")
    lines.append(f"  {SYM_OK} ArcGraph build completed")
    lines.append("")

    # Source root detection
    detection = payload.get("source_root_detection", {})
    strategy = detection.get("strategy", "unknown")
    roots = detection.get("roots", payload.get("source_roots", []))
    root_count = len(roots)
    lines.append(
        _kv(
            "Source roots",
            f"{strategy} ({root_count} root{'s' if root_count != 1 else ''})",
        )
    )
    for root in roots[:5]:
        lines.append(f"{'':>22}{SYM_BULLET} {root}")
    if root_count > 5:
        lines.append(f"{'':>22}  ... and {root_count - 5} more")
    lines.append("")

    # Stats
    lines.append(_kv("Files", f"{_fmt_count(payload.get('file_count', 0))} scanned"))
    lines.append(
        _kv("Nodes", f"{_fmt_count(payload.get('node_count', 0))} symbols indexed")
    )
    lines.append(
        _kv("Edges", f"{_fmt_count(payload.get('edge_count', 0))} relationships mapped")
    )
    warn_count = payload.get("warning_count", 0)
    warn_suffix = "" if warn_count == 0 else f" {SYM_WARN}"
    lines.append(_kv("Warnings", f"{_fmt_count(warn_count)}{warn_suffix}"))
    lines.append("")

    # Capabilities
    caps = payload.get("capabilities", {})
    if caps:
        pill_block = _capability_pills(caps)
        if pill_block:
            lines.append(_kv("Capabilities", ""))
            lines.append(pill_block)
            lines.append("")

    # Schema + output
    lines.append(_kv("Schema", payload.get("schema_version", "?")))
    build_dir = payload.get("build_dir", "")
    if build_dir:
        # Try to show a shorter path
        import os

        try:
            build_dir = os.path.relpath(build_dir)
        except ValueError:
            pass  # cross-drive on Windows
        lines.append(_kv("Output", build_dir))
    lines.append("")

    # Duration
    if duration_s < 1:
        dur_str = f"{duration_s * 1000:.0f}ms"
    elif duration_s < 60:
        dur_str = f"{duration_s:.1f}s"
    else:
        mins = int(duration_s // 60)
        secs = duration_s % 60
        dur_str = f"{mins}m {secs:.1f}s"
    lines.append(_kv("Duration", dur_str))
    lines.append("")

    # Cleanup info
    cleanup = payload.get("cleanup")
    if cleanup and cleanup.get("pruned_count", 0) > 0:
        lines.append(_kv("Cleanup", f"pruned {cleanup['pruned_count']} old build(s)"))
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  Current / status output
# ---------------------------------------------------------------------------


def format_current_human(payload: dict[str, Any]) -> str:
    """Format a current/status result as a human-readable summary."""
    lines: list[str] = []

    lines.append("")

    # Freshness
    freshness = payload.get("freshness", {})
    fresh_status = freshness.get("status", "unknown")
    fresh_icon = (
        SYM_OK
        if fresh_status == "fresh"
        else SYM_WARN if fresh_status == "stale" else SYM_FAIL
    )
    lines.append(f"  {fresh_icon} Index is {fresh_status}")
    lines.append("")

    # Basic info
    lines.append(_kv("Schema", payload.get("schema_version", "?")))
    lines.append(_kv("Index version", payload.get("index_version", "?")))
    commit = payload.get("commit_sha")
    if commit:
        lines.append(_kv("Commit", commit[:12]))
    lines.append("")

    # Counts
    lines.append(_kv("Files", _fmt_count(payload.get("file_count", 0))))
    lines.append(_kv("Nodes", _fmt_count(payload.get("node_count", 0))))
    lines.append(_kv("Edges", _fmt_count(payload.get("edge_count", 0))))
    lines.append("")

    # Capabilities
    caps = payload.get("capabilities", {})
    if caps:
        pill_block = _capability_pills(caps)
        if pill_block:
            lines.append(_kv("Capabilities", ""))
            lines.append(pill_block)
            lines.append("")

    # Source roots
    source_roots = payload.get("source_roots", [])
    if source_roots:
        lines.append(_kv("Source roots", f"{len(source_roots)} configured"))
        for root in source_roots[:5]:
            lines.append(f"{'':>22}{SYM_BULLET} {root}")
        if len(source_roots) > 5:
            lines.append(f"{'':>22}  ... and {len(source_roots) - 5} more")
        lines.append("")

    # Stale files
    stale_files = freshness.get("stale_files", [])
    if stale_files:
        lines.append(_kv("Stale files", f"{len(stale_files)} changed since index"))
        for sf in stale_files[:3]:
            lines.append(f"{'':>22}{SYM_BULLET} {sf}")
        if len(stale_files) > 3:
            lines.append(f"{'':>22}  ... and {len(stale_files) - 3} more")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  CI output
# ---------------------------------------------------------------------------


def format_ci_human(payload: dict[str, Any]) -> str:
    """Format CI check results as a human-readable summary."""
    lines: list[str] = []

    status = payload.get("status", "unknown")
    icon = SYM_OK if status == "pass" else SYM_WARN if status == "warn" else SYM_FAIL
    lines.append("")
    lines.append(f"  {icon} ArcGraph CI: {status.upper()}")
    lines.append("")

    checks = payload.get("checks", [])
    fail_count = sum(1 for c in checks if c.get("status") == "fail")
    warn_count = sum(1 for c in checks if c.get("status") == "warn")
    pass_count = sum(1 for c in checks if c.get("status") == "pass")
    skip_count = sum(1 for c in checks if c.get("status") == "skip")

    lines.append(
        _kv(
            "Checks",
            f"{pass_count} pass, {warn_count} warn, {fail_count} fail, {skip_count} skip",
        )
    )
    lines.append("")

    # Show failed and warned checks
    for c in checks:
        c_status = c.get("status", "?")
        if c_status in ("fail", "warn"):
            c_icon = SYM_FAIL if c_status == "fail" else SYM_WARN
            c_name = c.get("name", "?")
            c_msg = c.get("message", "")
            lines.append(f"  {c_icon} {c_name}: {c_msg}")

    if fail_count > 0 or warn_count > 0:
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  Init output
# ---------------------------------------------------------------------------


def format_init_human(payload: dict[str, Any]) -> str:
    """Format an init result as a human-readable summary."""
    action = payload.get("action", "unknown")
    wrote = bool(payload.get("wrote"))
    icon = SYM_OK if wrote or action in {"already_configured", "dry_run"} else SYM_WARN

    lines: list[str] = []
    lines.append("")
    lines.append(f"  {icon} ArcGraph init: {action.replace('_', ' ')}")
    lines.append("")

    message = payload.get("message")
    if message:
        lines.append(_kv("Status", str(message)))

    detection = payload.get("source_root_detection", {})
    strategy = detection.get("strategy", "unknown")
    roots = payload.get("source_roots", [])
    lines.append(
        _kv(
            "Detection",
            f"{strategy} ({len(roots)} root{'s' if len(roots) != 1 else ''})",
        )
    )
    for root in roots[:8]:
        if isinstance(root, dict):
            path = root.get("path", "")
            prefix = root.get("module_prefix", "")
            suffix = f" (module_prefix={prefix})" if prefix else ""
            lines.append(f"{'':>22}{SYM_BULLET} {path}{suffix}")
        else:
            lines.append(f"{'':>22}{SYM_BULLET} {root}")
    if len(roots) > 8:
        lines.append(f"{'':>22}  ... and {len(roots) - 8} more")
    lines.append("")

    pyproject = payload.get("pyproject_path")
    if pyproject:
        lines.append(_kv("pyproject", str(pyproject)))
    lines.append(_kv("Wrote config", "yes" if wrote else "no"))
    lines.append("")

    snippet = payload.get("config_snippet")
    if snippet and action in {"manual_config", "dry_run"}:
        lines.append("  Config snippet")
        lines.append("")
        for line in str(snippet).splitlines():
            lines.append(f"    {line}")
        lines.append("")

    next_steps = payload.get("next_steps", [])
    if next_steps:
        lines.append("  Next steps")
        for step in next_steps:
            lines.append(f"    {SYM_BULLET} {step}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  Doctor output
# ---------------------------------------------------------------------------


def format_doctor_human(payload: dict[str, Any]) -> str:
    """Format doctor check results as a human-readable summary."""
    status = payload.get("status", "unknown")
    icon = SYM_OK if status == "pass" else SYM_WARN if status == "warn" else SYM_FAIL
    summary = payload.get("summary", {})

    lines: list[str] = []
    lines.append("")
    lines.append(f"  {icon} ArcGraph doctor: {status.upper()}")
    lines.append("")
    lines.append(
        _kv(
            "Checks",
            f"{summary.get('pass', 0)} pass, "
            f"{summary.get('warn', 0)} warn, "
            f"{summary.get('fail', 0)} fail",
        )
    )
    lines.append("")

    for check in payload.get("checks", []):
        c_status = check.get("status", "?")
        c_icon = (
            SYM_OK
            if c_status == "pass"
            else SYM_WARN if c_status == "warn" else SYM_FAIL
        )
        c_name = check.get("name", "?")
        c_msg = check.get("message", "")
        lines.append(f"  {c_icon} {c_name}: {c_msg}")
        fix = check.get("fix")
        if fix and c_status != "pass":
            lines.append(f"{'':>6}{SYM_BULLET} {fix}")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  Generic fallback
# ---------------------------------------------------------------------------


def format_generic_human(payload: dict[str, Any], command: str | None = None) -> str:
    """Best-effort human formatting for any command payload."""
    # Dispatch to specific formatters when possible
    if command == "build":
        return format_build_human(payload, 0)
    if command in ("current", "status"):
        return format_current_human(payload)
    if command == "ci":
        return format_ci_human(payload)
    if command == "init":
        return format_init_human(payload)
    if command == "doctor":
        return format_doctor_human(payload)

    # Fallback: show top-level keys as a simple table
    lines = [""]
    for key, value in payload.items():
        if key.startswith("_"):
            continue
        if isinstance(value, (dict, list)):
            if isinstance(value, list):
                lines.append(_kv(key, f"[{len(value)} items]"))
            else:
                lines.append(_kv(key, f"{{{len(value)} keys}}"))
        else:
            lines.append(_kv(key, str(value)))
    lines.append("")
    return "\n".join(lines)
