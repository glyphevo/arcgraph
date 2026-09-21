"""Markdown report generation for ArcGraph query payloads."""

from __future__ import annotations

import html
import json
import posixpath
import re

from typing import Any

from arcgraph.interfaces.visual_slice import build_report_visual_slices

_METRICS_DASHBOARD_FIELDS = frozenset(
    {
        "commands",
        "duration_by_category_ms",
        "duration_by_command_ms",
        "duration_ms",
        "event_count",
        "performance_budget",
        "semantic_summary",
        "status",
        "statuses",
    }
)


def _safe_rel_path(path: str | None, repo_root: str | None = None) -> str:
    """Sanitize a path for display in reports.

    Returns a relative path if it falls under *repo_root*, otherwise the
    basename alone.  Absolute paths that sit outside the repo are replaced
    with ``<external>`` to avoid leaking filesystem layout.
    """
    if not path:
        return ""
    # Detect absolute paths: POSIX (/...), Windows drive-letter (C:\...), UNC (\\...).
    _is_absolute = (
        posixpath.isabs(path)
        or bool(re.match(r"^[A-Za-z]:[\\/]", path))
        or path.startswith("\\\\")
    )
    if not _is_absolute:
        return path
    if repo_root:
        norm_path = posixpath.normpath(path.replace("\\", "/"))
        norm_root = posixpath.normpath(repo_root.replace("\\", "/"))

        # Extract and compare drive letters (C:, D:, etc.)
        def _split_drive(p: str) -> tuple[str, str]:
            if len(p) >= 2 and p[1] == ":":
                return p[0].upper(), p[2:]
            return "", p

        drive_p, tail_p = _split_drive(norm_path)
        drive_r, tail_r = _split_drive(norm_root)
        # Only compare containment if drives match (or both are driveless)
        if drive_p == drive_r:
            if tail_p.startswith(tail_r + "/") or tail_p == tail_r:
                return posixpath.relpath(tail_p, tail_r)
    # Fall back to basename for absolute paths outside repo.
    base = posixpath.basename(path.replace("\\", "/"))
    return base if base else "<external>"


def render_impact_markdown(impact: dict[str, Any]) -> str:
    target = impact.get("query", "")
    lines = [
        f"# ArcGraph Impact Report: {target}",
        "",
        f"- Status: {impact.get('status', 'unknown')}",
        f"- Index version: {impact.get('index_version', 'unknown')}",
        f"- Freshness: {impact.get('freshness', {}).get('status', 'unknown')}",
        "",
        "## Resolved Targets",
    ]
    for target_id in impact.get("resolved_targets", []):
        lines.append(f"- `{target_id}`")
    if not impact.get("resolved_targets"):
        lines.append("- None")

    confidence_summary = impact.get("confidence_summary", {})
    if confidence_summary:
        lines.extend(
            [
                "",
                "## Confidence Impact",
                f"- Confirmed edges: {confidence_summary.get('confirmed_edges', 0)}",
                f"- Inferred edges: {confidence_summary.get('inferred_edges', 0)}",
                f"- Runtime-only edges: {confidence_summary.get('runtime_only_edges', 0)}",
                f"- Heuristic edges: {confidence_summary.get('heuristic_edges', 0)}",
                f"- Unresolved risks: {confidence_summary.get('unresolved_risks', 0)}",
            ]
        )

    unresolved_risks = impact.get("unresolved_risks", {})
    risk_items = unresolved_risks.get("items", [])
    lines.extend(["", "## Unresolved Risks"])
    for item in risk_items[:20]:
        props = item.get("properties", {})
        location = item.get("path") or props.get("path") or "unknown"
        line = item.get("start_line") or props.get("line")
        suffix = f":{line}" if line else ""
        lines.append(
            "- "
            f"`{location}{suffix}` "
            f"{props.get('raw_expression', '<unknown>')} "
            f"({props.get('failed_strategy', 'unknown')})"
        )
    if not risk_items:
        lines.append("- None")

    lines.extend(["", "## Affected Entrypoints"])
    for node in impact.get("entrypoint_impact", {}).get("entrypoints", []):
        lines.append(f"- `{node.get('id')}`")
    if not impact.get("entrypoint_impact", {}).get("entrypoints"):
        lines.append("- None")

    lines.extend(["", "## Resources"])
    for node in impact.get("resource_impact", {}).get("resources", []):
        lines.append(f"- `{node.get('id')}`")
    if not impact.get("resource_impact", {}).get("resources"):
        lines.append("- None")

    lines.extend(["", "## Test Candidates"])
    for candidate in impact.get("test_candidates", []):
        lines.append(
            f"- `{candidate.get('path')}` "
            f"({candidate.get('evidence', 'unknown')}: {candidate.get('reason', '')})"
        )
    if not impact.get("test_candidates"):
        lines.append("- None")

    lines.extend(["", "## Test Gaps"])
    for gap in impact.get("test_gaps", []):
        lines.append(
            f"- `{gap.get('target')}`: {gap.get('reason')} "
            f"[{gap.get('severity', 'unknown')}]"
        )
    if not impact.get("test_gaps"):
        lines.append("- None")

    coverage = impact.get("coverage", {})
    lines.extend(
        [
            "",
            "## Coverage",
            f"- Status: {coverage.get('status', 'unavailable')}",
            f"- Covered targets: {len(coverage.get('covered_targets', []))}",
        ]
    )
    return "\n".join(lines) + "\n"


def render_unresolved_classification_markdown(unresolved: dict[str, Any]) -> str:
    """Render a compact classification report for unresolved callsite diagnostics."""

    classification = classify_unresolved_records(unresolved)
    classifications = classification["records"]
    category_counts = classification["category_counts"]
    failed_strategy_counts = classification["failed_strategy_counts"]
    categories = classification.get("categories", {})

    lines = [
        "# ArcGraph Unresolved Classification",
        "",
        f"- Status: {unresolved.get('status', 'unknown')}",
        f"- Query: {unresolved.get('query') or 'all'}",
        f"- Index version: {unresolved.get('index_version', 'unknown')}",
        f"- Freshness: {unresolved.get('freshness', {}).get('status', 'unknown')}",
        f"- Total unresolved: {unresolved.get('summary', {}).get('total', 0)}",
        f"- Classified records: {len(classifications)}",
        f"- Release-blocking records: {classification.get('release_blocking_count', 0)}",
        f"- Limit: {unresolved.get('summary', {}).get('limit', len(classifications))}",
        "",
        "## Categories",
        "",
        "| Category | Count | Risk | Blocks python_full | Meaning |",
        "| --- | ---: | --- | --- | --- |",
    ]
    for category, meaning in _UNRESOLVED_CATEGORY_MEANINGS:
        category_meta = categories.get(category, {})
        lines.append(
            f"| `{category}` | {category_counts.get(category, 0)} | "
            f"{category_meta.get('risk_level', 'unknown')} | "
            f"{category_meta.get('release_blocking', False)} | {meaning} |"
        )

    recommended_actions = classification.get("recommended_actions", [])
    lines.extend(["", "## Recommended Actions", ""])
    if recommended_actions:
        for action in recommended_actions:
            lines.append(
                "- "
                f"`{action.get('category')}` ({action.get('count', 0)}): "
                f"{action.get('suggested_next_step', '')}"
            )
    else:
        lines.append("- None")

    lines.extend(["", "## Failed Strategies", ""])
    if failed_strategy_counts:
        for strategy, count in sorted(failed_strategy_counts.items()):
            lines.append(f"- `{strategy}`: {count}")
    else:
        lines.append("- None")

    lines.extend(["", "## Representative Diagnostics"])
    for category, _meaning in _UNRESOLVED_CATEGORY_MEANINGS:
        category_items = [
            item for item in classifications if item["category"] == category
        ][:10]
        lines.extend(["", f"### {category}", ""])
        if not category_items:
            lines.append("- None")
            continue
        for item in category_items:
            candidate_suffix = (
                f", candidates={item['candidate_count']}"
                if item["candidate_count"] is not None
                else ""
            )
            blocking_suffix = (
                ", blocks_python_full" if item.get("release_blocking") is True else ""
            )
            lines.append(
                "- "
                f"`{item['location']}` "
                f"`{item['raw_expression']}` "
                f"({item['failed_strategy']}{candidate_suffix}, "
                f"risk={item.get('risk_level', 'unknown')}{blocking_suffix}) - "
                f"{item.get('classification_reason', '')}"
            )

    return "\n".join(lines).rstrip() + "\n"


# Phase 1.1: classify logic moved to core/ — re-export for backward compat.
from arcgraph.core.unresolved_classification import (  # noqa: F401, E402
    classify_unresolved_records,
)


def render_ci_markdown(ci_result: dict[str, Any]) -> str:
    lines = [
        "# ArcGraph CI Summary",
        "",
        f"- Status: {ci_result.get('status', 'unknown')}",
        f"- Index version: {ci_result.get('index_version', 'unknown')}",
        "",
        "## Checks",
    ]
    for check in ci_result.get("checks", []):
        lines.append(
            f"- `{check.get('name')}`: {check.get('status')} - {check.get('message')}"
        )
    _append_precision_summary(lines, ci_result)
    _append_semantic_summary(lines, ci_result.get("semantic_summary", {}))
    _append_diagnostic_lifecycle(lines, ci_result.get("diagnostic_lifecycle", {}))
    summary = ci_result.get("pr_summary", {})
    lines.extend(["", "## PR Impact"])
    for label, key in [
        ("Changed symbols", "changed_symbols"),
        ("Changed files", "changed_files"),
        ("Affected entrypoints", "affected_entrypoints"),
        ("Affected resources", "affected_resources"),
        ("Related tests", "related_tests"),
        ("Risk factors", "risk_factors"),
    ]:
        values = [value for value in summary.get(key, []) if value]
        lines.append(f"- {label}: {', '.join(values) if values else 'None'}")
    high_risk_unresolved = _high_risk_unresolved_values(summary)
    lines.append(
        "- High-risk unresolved: "
        f"{', '.join(high_risk_unresolved) if high_risk_unresolved else 'None'}"
    )
    return "\n".join(lines) + "\n"


def render_pr_markdown(ci_result: dict[str, Any]) -> str:
    summary = ci_result.get("pr_summary", {})
    lines = [
        "# ArcGraph PR Impact Summary",
        "",
        f"- Status: {ci_result.get('status', 'unknown')}",
        f"- Index version: {ci_result.get('index_version', 'unknown')}",
        f"- Freshness: {ci_result.get('freshness', {}).get('status', 'unknown')}",
        "",
    ]
    sections = [
        ("Changed files", summary.get("changed_files", [])),
        ("Changed symbols", summary.get("changed_symbols", [])),
        ("Affected entrypoints", summary.get("affected_entrypoints", [])),
        ("Affected data resources", summary.get("affected_resources", [])),
        ("Related tests", summary.get("related_tests", [])),
        ("Risk factors", summary.get("risk_factors", [])),
        ("High-risk unresolved", _high_risk_unresolved_values(summary)),
    ]
    for title, values in sections:
        lines.extend([f"## {title}", ""])
        _append_values(lines, values)
        lines.append("")

    _append_semantic_summary(
        lines,
        ci_result.get("semantic_summary") or summary.get("semantic_summary", {}),
    )
    _append_precision_summary(lines, ci_result)
    _append_diagnostic_lifecycle(lines, ci_result.get("diagnostic_lifecycle", {}))
    lines.append("")

    lines.extend(["## Architecture", ""])
    architecture_checks = {
        check.get("name"): check
        for check in ci_result.get("checks", [])
        if check.get("name") in {"import_cycles", "layer_violations"}
    }
    for name in ["import_cycles", "layer_violations"]:
        check = architecture_checks.get(name)
        if not check:
            continue
        lines.append(f"- `{name}`: {check.get('status')} - {check.get('message')}")
    if not architecture_checks:
        lines.append("- None")
    lines.append("")

    lines.extend(["## Similarity", ""])
    similar = summary.get("similarity", [])
    if similar:
        for item in similar:
            reasons = ", ".join(str(reason) for reason in item.get("reasons", []))
            lines.append(
                "- "
                f"`{item.get('target')}` -> `{item.get('similar')}` "
                f"(score={item.get('score')}, {reasons})"
            )
    else:
        lines.append("- None")
    lines.append("")

    warnings = ci_result.get("warnings", [])
    if warnings:
        lines.extend(["## Warnings", ""])
        _append_values(lines, warnings)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_static_html_report(
    *,
    target: str,
    impact: dict[str, Any],
    similar: dict[str, Any],
    architecture: dict[str, Any],
    entrypoint_flow: dict[str, Any] | None = None,
    ci_result: dict[str, Any] | None = None,
) -> str:
    data = {
        "target": target,
        "impact": impact,
        "similar": similar,
        "architecture": architecture,
        "entrypoint_flow": entrypoint_flow,
        "ci": ci_result,
    }
    graph_data = build_report_visual_slices(
        impact=impact,
        similar=similar,
        architecture=architecture,
        entrypoint_flow=entrypoint_flow,
    )
    title = f"ArcGraph Report: {target}"
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{html.escape(title)}</title>",
            "<style>",
            _REPORT_CSS,
            "</style>",
            "</head>",
            "<body>",
            "<main>",
            f"<h1>{html.escape(title)}</h1>",
            _summary_section(impact, similar, architecture, ci_result),
            _impact_semantic_section(impact),
            _semantic_section(ci_result),
            _ci_checks_section(ci_result),
            _graph_ui_section(),
            _nodes_section(
                "Impact Radius",
                [
                    *impact.get("call_impact", {}).get("affected_symbols", []),
                    *impact.get("import_impact", {}).get("affected_modules", []),
                ],
            ),
            _entrypoint_section(entrypoint_flow, impact),
            _nodes_section(
                "Resources",
                impact.get("resource_impact", {}).get("resources", []),
            ),
            _tests_section(impact),
            _similarity_section(similar),
            _architecture_section(architecture),
            "<h2>Embedded JSON</h2>",
            "<pre>",
            html.escape(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)),
            "</pre>",
            "</main>",
            '<script id="ArcGraph-slice-data" type="application/json">',
            _json_script(graph_data),
            "</script>",
            "<script>",
            _REPORT_JS,
            "</script>",
            "</body>",
            "</html>",
            "",
        ]
    )


def render_metrics_dashboard_html(metrics: dict[str, Any]) -> str:
    title = "ArcGraph Metrics Dashboard"
    dashboard_metrics = {
        key: value for key, value in metrics.items() if key in _METRICS_DASHBOARD_FIELDS
    }
    warnings = metrics.get("warnings")
    dashboard_metrics["warning_count"] = (
        len(warnings) if isinstance(warnings, list) else 0
    )
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{title}</title>",
            "<style>",
            _REPORT_CSS,
            "</style>",
            "</head>",
            "<body>",
            "<main>",
            f"<h1>{title}</h1>",
            _metrics_summary_section(dashboard_metrics),
            _metrics_semantic_section(dashboard_metrics),
            _metrics_performance_budget_section(dashboard_metrics),
            _key_value_section("Commands", dashboard_metrics.get("commands", {})),
            _key_value_section("Statuses", dashboard_metrics.get("statuses", {})),
            _latency_section(
                "Build Latency",
                dashboard_metrics.get("duration_by_category_ms", {}).get("build", {}),
            ),
            _latency_section(
                "Query Latency",
                dashboard_metrics.get("duration_by_category_ms", {}).get("query", {}),
            ),
            _latency_table(
                "Latency By Command",
                dashboard_metrics.get("duration_by_command_ms", {}),
            ),
            "<h2>Embedded JSON</h2>",
            "<pre>",
            html.escape(
                json.dumps(
                    dashboard_metrics,
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                )
            ),
            "</pre>",
            "</main>",
            "</body>",
            "</html>",
            "",
        ]
    )


def _summary_section(
    impact: dict[str, Any],
    similar: dict[str, Any],
    architecture: dict[str, Any],
    ci_result: dict[str, Any] | None,
) -> str:
    ci_status = ci_result.get("status") if ci_result else "not-run"
    items = [
        ("Status", impact.get("status", "unknown")),
        ("Freshness", impact.get("freshness", {}).get("status", "unknown")),
        ("Resolved targets", len(impact.get("resolved_targets", []))),
        (
            "Affected entrypoints",
            len(impact.get("entrypoint_impact", {}).get("entrypoints", [])),
        ),
        ("Similar implementations", len(similar.get("similar", []))),
        ("Import cycles", len(architecture.get("import_cycles", []))),
        ("CI", ci_status),
    ]
    return (
        "<section><h2>Summary</h2><dl>"
        + "".join(
            f"<dt>{html.escape(str(label))}</dt><dd>{html.escape(str(value))}</dd>"
            for label, value in items
        )
        + "</dl></section>"
    )


def _high_risk_unresolved_values(summary: dict[str, Any]) -> list[str]:
    high_risk = summary.get("high_risk_unresolved", {})
    values: list[str] = []
    for item in high_risk.get("targets", []):
        target = item.get("target")
        count = item.get("unresolved_risk_count", 0)
        if target:
            values.append(f"{target}: {count}")
    return values


def _semantic_section(ci_result: dict[str, Any] | None) -> str:
    if not ci_result:
        return ""
    summary = ci_result.get("semantic_summary", {})
    if not summary:
        return ""
    items = [
        ("Callsites", summary.get("callsite_total", 0)),
        ("Resolved", summary.get("resolved_callsite_total", 0)),
        ("Unresolved", summary.get("unresolved_callsite_total", 0)),
        ("Resolution rate", summary.get("resolution_rate", 1.0)),
    ]
    binding_summary = summary.get("binding_summary", {})
    if binding_summary:
        items.extend(
            [
                ("Bindings", binding_summary.get("binding_total", 0)),
                (
                    "Binding diagnostics",
                    binding_summary.get("binding_diagnostic_total", 0),
                ),
            ]
        )
    type_summary = summary.get("type_summary", {})
    if type_summary:
        items.extend(
            [
                ("TypeRefs", type_summary.get("type_ref_total", 0)),
                ("Resolved TypeRefs", type_summary.get("resolved_type_ref_total", 0)),
                (
                    "Type diagnostics",
                    type_summary.get("type_diagnostic_total", 0),
                ),
            ]
        )
    return (
        "<section><h2>Semantic Summary</h2><dl>"
        + "".join(
            f"<dt>{html.escape(str(label))}</dt><dd>{html.escape(str(value))}</dd>"
            for label, value in items
        )
        + "</dl></section>"
    )


def _impact_semantic_section(impact: dict[str, Any]) -> str:
    summary = impact.get("confidence_summary", {})
    unresolved = impact.get("unresolved_risks", {})
    if not summary and not unresolved:
        return ""
    items = [
        ("Confidence profile", impact.get("confidence_profile", "unknown")),
        ("Confirmed edges", summary.get("confirmed_edges", 0)),
        ("Inferred edges", summary.get("inferred_edges", 0)),
        ("Runtime-only edges", summary.get("runtime_only_edges", 0)),
        ("Heuristic edges", summary.get("heuristic_edges", 0)),
        ("Unresolved risks", summary.get("unresolved_risks", 0)),
    ]
    rows = []
    for item in unresolved.get("items", [])[:20]:
        props = item.get("properties", {})
        location = item.get("path") or props.get("path") or ""
        line = item.get("start_line") or props.get("line")
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(location))}{':' + html.escape(str(line)) if line else ''}</code></td>"
            f"<td>{html.escape(str(props.get('raw_expression', '')))}</td>"
            f"<td>{html.escape(str(props.get('failed_strategy', 'unknown')))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="3">None</td></tr>')
    return (
        "<section><h2>Semantic Impact</h2><dl>"
        + "".join(
            f"<dt>{html.escape(str(label))}</dt><dd>{html.escape(str(value))}</dd>"
            for label, value in items
        )
        + "</dl><h3>Unresolved Risks</h3>"
        "<table><thead><tr><th>Location</th><th>Expression</th><th>Strategy</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></section>"
    )


def _ci_checks_section(ci_result: dict[str, Any] | None) -> str:
    if not ci_result:
        return ""
    checks = ci_result.get("checks", [])
    if not checks:
        return ""
    rows = [
        "<tr>"
        f"<td><code>{html.escape(str(check.get('name', '')))}</code></td>"
        f"<td>{html.escape(str(check.get('status', '')))}</td>"
        f"<td>{html.escape(str(check.get('message', '')))}</td>"
        "</tr>"
        for check in checks
    ]
    return (
        "<section><h2>CI Checks</h2>"
        "<table><thead><tr><th>Name</th><th>Status</th><th>Message</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></section>"
    )


def _metrics_summary_section(metrics: dict[str, Any]) -> str:
    items = [
        ("Status", metrics.get("status", "unknown")),
        ("Events", metrics.get("event_count", 0)),
        ("Warnings", metrics.get("warning_count", 0)),
        ("Privacy", "Input path and event timestamps omitted"),
    ]
    return (
        "<section><h2>Summary</h2><dl>"
        + "".join(
            f"<dt>{html.escape(str(label))}</dt><dd>{html.escape(str(value))}</dd>"
            for label, value in items
        )
        + "</dl></section>"
    )


def _metrics_semantic_section(metrics: dict[str, Any]) -> str:
    summary = metrics.get("semantic_summary", {})
    if not summary:
        return ""
    items = [
        ("Callsites", summary.get("callsite_total", 0)),
        ("Resolved", summary.get("resolved_callsite_total", 0)),
        ("Unresolved", summary.get("unresolved_callsite_total", 0)),
        ("Resolution rate", summary.get("resolution_rate", 1.0)),
    ]
    binding_summary = summary.get("binding_summary", {})
    if binding_summary:
        items.append(("Bindings", binding_summary.get("binding_total", 0)))
    type_summary = summary.get("type_summary", {})
    if type_summary:
        items.append(("TypeRefs", type_summary.get("type_ref_total", 0)))
    return (
        "<section><h2>Semantic Metrics</h2><dl>"
        + "".join(
            f"<dt>{html.escape(str(label))}</dt><dd>{html.escape(str(value))}</dd>"
            for label, value in items
        )
        + "</dl></section>"
    )


def _metrics_performance_budget_section(metrics: dict[str, Any]) -> str:
    budget = metrics.get("performance_budget", {})
    if not isinstance(budget, dict) or not budget:
        return ""
    items = [
        ("Status", budget.get("status", "unknown")),
        ("Observed budgets", budget.get("observed_budget_count", 0)),
        ("Violations", budget.get("violation_count", 0)),
    ]
    rows = []
    for item in budget.get("violations", []):
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(item.get('command', '')))}</code></td>"
            f"<td>{html.escape(str(item.get('metric', '')))}</td>"
            f"<td>{html.escape(str(item.get('observed_ms', '')))}</td>"
            f"<td>{html.escape(str(item.get('budget_ms', '')))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="4">None</td></tr>')
    return (
        "<section><h2>Performance Budget</h2><dl>"
        + "".join(
            f"<dt>{html.escape(str(label))}</dt><dd>{html.escape(str(value))}</dd>"
            for label, value in items
        )
        + "</dl><table><thead><tr><th>Command</th><th>Metric</th>"
        "<th>Observed ms</th><th>Budget ms</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></section>"
    )


def _key_value_section(title: str, values: dict[str, Any]) -> str:
    rows = [
        "<tr>"
        f"<td><code>{html.escape(str(key))}</code></td>"
        f"<td>{html.escape(str(value))}</td>"
        "</tr>"
        for key, value in values.items()
    ]
    if not rows:
        rows.append('<tr><td colspan="2">None</td></tr>')
    return (
        f"<section><h2>{html.escape(title)}</h2>"
        "<table><thead><tr><th>Name</th><th>Value</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></section>"
    )


def _latency_section(title: str, values: dict[str, Any]) -> str:
    return _key_value_section(title, values)


def _latency_table(title: str, values: dict[str, Any]) -> str:
    rows = [
        "<tr>"
        f"<td><code>{html.escape(str(command))}</code></td>"
        f"<td>{html.escape(str(stats.get('count', '')))}</td>"
        f"<td>{html.escape(str(stats.get('avg', '')))}</td>"
        f"<td>{html.escape(str(stats.get('p95', '')))}</td>"
        f"<td>{html.escape(str(stats.get('max', '')))}</td>"
        "</tr>"
        for command, stats in values.items()
    ]
    if not rows:
        rows.append('<tr><td colspan="5">None</td></tr>')
    return (
        f"<section><h2>{html.escape(title)}</h2>"
        "<table><thead><tr><th>Command</th><th>Count</th><th>Avg ms</th><th>P95 ms</th><th>Max ms</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></section>"
    )


def _nodes_section(title: str, nodes: list[dict[str, Any]]) -> str:
    rows = [
        "<tr>"
        f"<td><code>{html.escape(str(node.get('id', '')))}</code></td>"
        f"<td>{html.escape(str(node.get('kind', '')))}</td>"
        f"<td>{_file_link(node)}</td>"
        "</tr>"
        for node in nodes
    ]
    if not rows:
        rows.append('<tr><td colspan="3">None</td></tr>')
    return (
        f"<section><h2>{html.escape(title)}</h2>"
        "<table><thead><tr><th>ID</th><th>Kind</th><th>Path</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></section>"
    )


def _graph_ui_section() -> str:
    return (
        '<section class="graph-ui">'
        "<h2>Graph Slice</h2>"
        '<div class="toolbar" aria-label="Graph view controls">'
        '<button type="button" data-view="entrypoint">Entrypoint</button>'
        '<button type="button" data-view="impact">Impact</button>'
        '<button type="button" data-view="similarity">Similarity</button>'
        '<button type="button" data-view="architecture">Architecture</button>'
        '<button type="button" data-view="semantic">Semantic</button>'
        '<input id="graph-filter" type="search" placeholder="Filter nodes and edges">'
        '<button type="button" id="export-json">Export JSON</button>'
        '<button type="button" id="export-mermaid">Export Mermaid</button>'
        "</div>"
        '<div id="graph-status" class="muted"></div>'
        '<div class="graph-grid">'
        '<div><h3>Nodes</h3><table><thead><tr><th>ID</th><th>Kind</th><th>Path</th></tr></thead><tbody id="graph-nodes"></tbody></table></div>'
        '<div><h3>Edges</h3><table><thead><tr><th>Source</th><th>Kind</th><th>Target</th></tr></thead><tbody id="graph-edges"></tbody></table></div>'
        "</div>"
        '<textarea id="graph-export" readonly aria-label="Graph export"></textarea>'
        "</section>"
    )


def _entrypoint_section(
    entrypoint_flow: dict[str, Any] | None, impact: dict[str, Any]
) -> str:
    if not entrypoint_flow or entrypoint_flow.get("status") != "available":
        return _nodes_section(
            "Affected Entrypoints",
            impact.get("entrypoint_impact", {}).get("entrypoints", []),
        )

    nodes_by_id: dict[str, dict[str, Any]] = {}
    for node in [
        *entrypoint_flow.get("entrypoints", []),
        *entrypoint_flow.get("nodes", []),
    ]:
        nodes_by_id.setdefault(node.get("id", ""), node)

    node_rows = [
        "<tr>"
        f"<td><code>{html.escape(str(node.get('id', '')))}</code></td>"
        f"<td>{html.escape(str(node.get('kind', '')))}</td>"
        f"<td>{_file_link(node)}</td>"
        "</tr>"
        for node in nodes_by_id.values()
    ]
    if not node_rows:
        node_rows.append('<tr><td colspan="3">None</td></tr>')

    edge_rows = [
        "<tr>"
        f"<td><code>{html.escape(str(edge.get('source', '')))}</code></td>"
        f"<td>{html.escape(str(edge.get('kind', '')))}</td>"
        f"<td><code>{html.escape(str(edge.get('target', '')))}</code></td>"
        "</tr>"
        for edge in entrypoint_flow.get("edges", [])
    ]
    if not edge_rows:
        edge_rows.append('<tr><td colspan="3">None</td></tr>')

    return (
        "<section><h2>Entrypoint Flow</h2>"
        "<table><thead><tr><th>ID</th><th>Kind</th><th>Path</th></tr></thead>"
        f"<tbody>{''.join(node_rows)}</tbody></table>"
        "<h3>Edges</h3>"
        "<table><thead><tr><th>Source</th><th>Kind</th><th>Target</th></tr></thead>"
        f"<tbody>{''.join(edge_rows)}</tbody></table></section>"
    )


def _tests_section(impact: dict[str, Any]) -> str:
    rows = [
        "<tr>"
        f"<td><code>{html.escape(str(item.get('path', '')))}</code></td>"
        f"<td>{html.escape(str(item.get('evidence', '')))}</td>"
        f"<td>{html.escape(str(item.get('reason', '')))}</td>"
        "</tr>"
        for item in impact.get("test_candidates", [])
    ]
    if not rows:
        rows.append('<tr><td colspan="3">None</td></tr>')
    gaps = impact.get("test_gaps", [])
    gap_items = "".join(
        f"<li><code>{html.escape(str(gap.get('target')))}</code>: "
        f"{html.escape(str(gap.get('reason')))}</li>"
        for gap in gaps
    )
    if not gap_items:
        gap_items = "<li>None</li>"
    return (
        "<section><h2>Tests</h2>"
        "<table><thead><tr><th>Path</th><th>Evidence</th><th>Reason</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        f"<h3>Coverage Gaps</h3><ul>{gap_items}</ul></section>"
    )


def _similarity_section(similar: dict[str, Any]) -> str:
    rows = [
        "<tr>"
        f"<td><code>{html.escape(str(item.get('node', {}).get('id', '')))}</code></td>"
        f"<td>{html.escape(str(item.get('score', '')))}</td>"
        f"<td>{html.escape(', '.join(str(reason) for reason in item.get('reasons', [])))}</td>"
        "</tr>"
        for item in similar.get("similar", [])
    ]
    if not rows:
        rows.append('<tr><td colspan="3">None</td></tr>')
    return (
        "<section><h2>Similarity Map</h2>"
        "<table><thead><tr><th>Similar node</th><th>Score</th><th>Reasons</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></section>"
    )


def _architecture_section(architecture: dict[str, Any]) -> str:
    node_kinds = architecture.get("node_kinds", {})
    edge_kinds = architecture.get("edge_kinds", {})
    cycles = architecture.get("import_cycles", [])
    return (
        "<section><h2>Architecture Map</h2>"
        f"<p>Node kinds: {html.escape(json.dumps(node_kinds, sort_keys=True))}</p>"
        f"<p>Edge kinds: {html.escape(json.dumps(edge_kinds, sort_keys=True))}</p>"
        f"<p>Import cycles: {html.escape(str(len(cycles)))}</p>"
        "</section>"
    )


def _file_link(node: dict[str, Any]) -> str:
    path = node.get("path")
    if not path:
        return ""
    line = node.get("start_line")
    label = f"{path}:{line}" if line else path
    return f"<code>{html.escape(label)}</code>"


def _append_values(lines: list[str], values: list[Any]) -> None:
    filtered = [value for value in values if value]
    if not filtered:
        lines.append("- None")
        return
    for value in filtered:
        lines.append(f"- `{value}`")


def _append_precision_summary(lines: list[str], ci_result: dict[str, Any]) -> None:
    precision_check = _check_by_name(ci_result, "precision_inputs")
    if not precision_check:
        return
    details = precision_check.get("details", {})
    if not isinstance(details, dict):
        return
    metrics = details.get("metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}
    lines.extend(["", "## Precision Evidence"])
    lines.append(f"- Status: {details.get('status', 'unknown')}")
    lines.append(
        "- Type precision complete: "
        f"{str(details.get('type_precision_complete', False)).lower()}"
    )
    lines.append(f"- Pyright status: {details.get('pyright_status', 'unavailable')}")
    lines.append(
        "- Pyright type info: "
        f"{details.get('pyright_type_info_total', metrics.get('pyright_type_info_total', 0))}"
    )
    lines.append(
        "- Pyright LSP type info: "
        f"{details.get('pyright_lsp_type_info_total', metrics.get('pyright_lsp_type_info_total', 0))}"
    )
    lines.append(
        "- Pyright LSP requestable probes: "
        f"{details.get('pyright_lsp_requestable_probe_total', metrics.get('pyright_lsp_requestable_probe_total', 0))}"
    )
    lines.append(
        "- Pyright LSP skipped probes: "
        f"{details.get('pyright_lsp_skipped_unmappable_total', metrics.get('pyright_lsp_skipped_unmappable_total', 0))}"
    )
    lines.append(
        "- Pyright LSP requests: "
        f"{details.get('pyright_lsp_requests_total', metrics.get('pyright_lsp_requests_total', 0))}"
    )
    lines.append(
        "- SCIP type occurrences: "
        f"{details.get('scip_type_occurrences', metrics.get('scip_type_occurrences', 0))}"
    )
    lines.append(
        "- Type occurrences: "
        f"{details.get('type_occurrences', metrics.get('type_occurrences', 0))}"
    )
    lines.append(
        "- Precise references: " f"{details.get('precise_references', 'ast-fallback')}"
    )


def _append_semantic_summary(lines: list[str], summary: dict[str, Any]) -> None:
    if not summary:
        return
    lines.extend(["", "## Semantic Summary"])
    lines.append(f"- Callsites: {summary.get('callsite_total', 0)}")
    lines.append(f"- Resolved: {summary.get('resolved_callsite_total', 0)}")
    lines.append(f"- Unresolved: {summary.get('unresolved_callsite_total', 0)}")
    lines.append(f"- Resolution rate: {summary.get('resolution_rate', 1.0)}")
    binding_summary = summary.get("binding_summary", {})
    if binding_summary:
        lines.append(f"- Bindings: {binding_summary.get('binding_total', 0)}")
        lines.append(
            "- Binding diagnostics: "
            f"{binding_summary.get('binding_diagnostic_total', 0)}"
        )
    type_summary = summary.get("type_summary", {})
    if type_summary:
        lines.append(f"- TypeRefs: {type_summary.get('type_ref_total', 0)}")
        lines.append(
            "- Resolved TypeRefs: " f"{type_summary.get('resolved_type_ref_total', 0)}"
        )
        lines.append(
            "- Type diagnostics: " f"{type_summary.get('type_diagnostic_total', 0)}"
        )


def _check_by_name(ci_result: dict[str, Any], name: str) -> dict[str, Any] | None:
    for check in ci_result.get("checks", []):
        if check.get("name") == name:
            return check
    return None


# Phase 1.1: classification helpers moved to core/unresolved_classification.py.
# Only _UNRESOLVED_CATEGORY_MEANINGS is re-imported here because
# render_unresolved_classification_markdown() references it directly.
from arcgraph.core.unresolved_classification import (  # noqa: F401, E402
    _UNRESOLVED_CATEGORY_MEANINGS,
)


def _append_diagnostic_lifecycle(lines: list[str], lifecycle: dict[str, Any]) -> None:
    if not lifecycle:
        return
    lines.extend(["", "## Diagnostics Lifecycle"])
    lines.append(f"- New diagnostics: {lifecycle.get('new_diagnostics_count', 0)}")
    lines.append(
        "- Existing diagnostics: " f"{lifecycle.get('existing_diagnostics_count', 0)}"
    )
    lines.append(f"- Total diagnostics: {lifecycle.get('diagnostics_total', 0)}")


def _json_script(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False).replace(
        "<", "\\u003C"
    )


_REPORT_CSS = """
:root { color-scheme: light; font-family: Inter, Segoe UI, Arial, sans-serif; }
body { margin: 0; background: #f6f7f9; color: #1f2933; }
main { max-width: 1180px; margin: 0 auto; padding: 32px 20px 56px; }
h1 { font-size: 28px; margin: 0 0 24px; }
h2 { font-size: 18px; margin: 0 0 12px; }
h3 { font-size: 15px; margin: 16px 0 8px; }
section { background: #fff; border: 1px solid #d9dee7; border-radius: 8px; padding: 18px; margin: 14px 0; }
dl { display: grid; grid-template-columns: minmax(160px, 240px) 1fr; gap: 8px 16px; margin: 0; }
dt { color: #52606d; }
dd { margin: 0; font-weight: 600; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; }
th, td { border-top: 1px solid #e4e7ec; padding: 8px 10px; text-align: left; vertical-align: top; overflow-wrap: anywhere; }
th { color: #52606d; font-weight: 600; }
.toolbar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }
button { border: 1px solid #cbd2dd; background: #fff; color: #1f2933; border-radius: 6px; padding: 6px 10px; cursor: pointer; }
button.active { background: #1f2933; color: #fff; border-color: #1f2933; }
input[type="search"] { min-width: 240px; flex: 1; border: 1px solid #cbd2dd; border-radius: 6px; padding: 7px 10px; }
.graph-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; }
.muted { color: #52606d; margin: 6px 0 12px; }
textarea { width: 100%; min-height: 120px; border: 1px solid #cbd2dd; border-radius: 8px; margin-top: 12px; padding: 10px; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
@media (max-width: 860px) { .graph-grid { grid-template-columns: 1fr; } }
code, pre { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
pre { background: #111827; color: #f9fafb; border-radius: 8px; padding: 16px; overflow: auto; }
""".strip()

_REPORT_JS = r"""
(() => {
  const script = document.getElementById("ArcGraph-slice-data");
  if (!script) return;
  const data = JSON.parse(script.textContent || "{}");
  const views = data.views || {};
  const nodesBody = document.getElementById("graph-nodes");
  const edgesBody = document.getElementById("graph-edges");
  const filterInput = document.getElementById("graph-filter");
  const status = document.getElementById("graph-status");
  const exportBox = document.getElementById("graph-export");
  let currentView = "entrypoint";

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));

  const currentSlice = () => views[currentView] || {nodes: [], edges: []};
  const matches = (item, query) => JSON.stringify(item).toLowerCase().includes(query);

  function render() {
    const query = (filterInput?.value || "").toLowerCase();
    const slice = currentSlice();
    const nodes = (slice.nodes || []).filter((node) => matches(node, query));
    const edges = (slice.edges || []).filter((edge) => matches(edge, query));
    nodesBody.innerHTML = nodes.length ? nodes.map((node) => (
      `<tr><td><code>${escapeHtml(node.id)}</code></td><td>${escapeHtml(node.kind)}</td><td><code>${escapeHtml(node.path || "")}${node.start_line ? ":" + escapeHtml(node.start_line) : ""}</code></td></tr>`
    )).join("") : '<tr><td colspan="3">None</td></tr>';
    edgesBody.innerHTML = edges.length ? edges.map((edge) => (
      `<tr><td><code>${escapeHtml(edge.source)}</code></td><td>${escapeHtml(edge.kind)}</td><td><code>${escapeHtml(edge.target)}</code></td></tr>`
    )).join("") : '<tr><td colspan="3">None</td></tr>';
    status.textContent = `${currentView}: ${nodes.length} nodes, ${edges.length} edges`;
    document.querySelectorAll("[data-view]").forEach((button) => {
      button.classList.toggle("active", button.dataset.view === currentView);
    });
  }

  function mermaid(slice) {
    const ids = new Map();
    const safeId = (value) => {
      if (!ids.has(value)) ids.set(value, `n${ids.size + 1}`);
      return ids.get(value);
    };
    const lines = ["flowchart LR"];
    (slice.nodes || []).forEach((node) => {
      lines.push(`  ${safeId(node.id)}["${String(node.id || "").replace(/"/g, "'")}"]`);
    });
    (slice.edges || []).forEach((edge) => {
      lines.push(`  ${safeId(edge.source)} -->|${String(edge.kind || "").replace(/"/g, "'")}| ${safeId(edge.target)}`);
    });
    return lines.join("\n");
  }

  document.querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", () => {
      currentView = button.dataset.view || currentView;
      render();
    });
  });
  filterInput?.addEventListener("input", render);
  document.getElementById("export-json")?.addEventListener("click", () => {
    exportBox.value = JSON.stringify(currentSlice(), null, 2);
  });
  document.getElementById("export-mermaid")?.addEventListener("click", () => {
    exportBox.value = mermaid(currentSlice());
  });
  render();
})();
""".strip()
