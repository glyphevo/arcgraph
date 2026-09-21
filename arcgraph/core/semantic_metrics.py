"""Semantic callsite metrics and unresolved diagnostic helpers."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from arcgraph.core.expression_kinds import call_expression_kind
from arcgraph.core.ids import (
    callsite_id,
    diagnostic_id,
    stable_callsite_subject,
    stable_callsite_subject_key,
)
from arcgraph.core.schemas import Edge, IndexMetadata, Node, SemanticDiagnostic

AST_CALL_EVIDENCE_KINDS = {
    "ast_attribute_call",
    "ast_call",
    "ast_class_body_call",
    "ast_config_call",
    "ast_constructor_call",
    "ast_declaration_call",
    "ast_definition_time_call",
    "ast_dynamic_call",
    "ast_log_call",
    "ast_module_call",
}
CALLSITE_RESOLUTION_EDGE_KINDS = {
    "calls",
    "constructs",
    "initializes",
    "logs",
    "configures",
    "declares",
    "uses",
    "dynamic_call",
}


@dataclass(frozen=True, slots=True)
class CallsiteRecord:
    callsite_id: str
    source_scope: str
    source_kind: str
    source_qualname: str | None
    path: str | None
    line: int | None
    column: int | None
    raw_expression: str
    context: str
    expression_kind: str
    candidate_count: int
    stable_subject: dict[str, str | None]
    stable_occurrence: int


def collect_callsite_records(nodes: list[Node]) -> list[CallsiteRecord]:
    target_candidates = _target_candidates(nodes)
    records: dict[str, CallsiteRecord] = {}
    for node in sorted(nodes, key=lambda item: item.id):
        callsites = node.properties.get("callsites", [])
        if not isinstance(callsites, list):
            continue
        occurrence_by_subject: dict[tuple[str, ...], int] = {}
        occurrence_by_callsite_id: dict[str, int] = {}
        for callsite in callsites:
            if not isinstance(callsite, dict):
                continue
            raw_expression = callsite.get("name")
            if not isinstance(raw_expression, str) or not raw_expression:
                continue
            line = _int_or_none(callsite.get("line"))
            column = _int_or_none(callsite.get("column"))
            context = _callsite_context(node, callsite)
            current_callsite_id = callsite_id(
                node.id,
                node.path,
                line,
                column,
                raw_expression,
            )
            stable_subject = stable_callsite_subject(
                node.id,
                raw_expression,
                context=callsite.get("context"),
                call_expression=callsite.get("call_expression"),
                receiver_expression=callsite.get("receiver"),
                attribute=callsite.get("attribute"),
            )
            stable_occurrence = occurrence_by_callsite_id.get(current_callsite_id)
            if stable_occurrence is None:
                subject_key = stable_callsite_subject_key(stable_subject)
                stable_occurrence = occurrence_by_subject.get(subject_key, 0) + 1
                occurrence_by_subject[subject_key] = stable_occurrence
                occurrence_by_callsite_id[current_callsite_id] = stable_occurrence
            records.setdefault(
                current_callsite_id,
                CallsiteRecord(
                    callsite_id=current_callsite_id,
                    source_scope=node.id,
                    source_kind=node.kind,
                    source_qualname=node.qualname,
                    path=node.path,
                    line=line,
                    column=column,
                    raw_expression=raw_expression,
                    context=context,
                    expression_kind=call_expression_kind(raw_expression),
                    candidate_count=_candidate_count(raw_expression, target_candidates),
                    stable_subject=stable_subject,
                    stable_occurrence=stable_occurrence,
                ),
            )
    return sorted(
        records.values(),
        key=lambda item: (
            item.path or "",
            item.line or 0,
            item.column or 0,
            item.source_scope,
            item.raw_expression,
        ),
    )


def collect_semantic_metrics(
    nodes: list[Node],
    edges: list[Edge],
) -> dict[str, Any]:
    return collect_semantic_metrics_from_resolved_callsites(
        nodes,
        resolved_callsite_ids(edges),
        edge_kind_counts=Counter(edge.kind for edge in edges),
        confidence_counts=Counter(edge.confidence for edge in edges),
    )


def collect_semantic_metrics_from_resolved_callsites(
    nodes: list[Node],
    resolved: set[str],
    *,
    edge_kind_counts: Mapping[str, int],
    confidence_counts: Mapping[str, int],
) -> dict[str, Any]:
    callsites = collect_callsite_records(nodes)
    unresolved_records = [
        record for record in callsites if record.callsite_id not in resolved
    ]

    context_counts = Counter(record.context for record in callsites)
    expression_counts = Counter(record.expression_kind for record in callsites)
    resolved_expression_counts = Counter(
        record.expression_kind for record in callsites if record.callsite_id in resolved
    )
    unresolved_expression_counts = Counter(
        record.expression_kind
        for record in callsites
        if record.callsite_id not in resolved
    )

    callsite_total = len(callsites)
    resolved_total = callsite_total - len(unresolved_records)
    resolution_by_expression_kind = {
        kind: (
            round(resolved_expression_counts.get(kind, 0) / total, 4) if total else 1.0
        )
        for kind, total in sorted(expression_counts.items())
    }
    return {
        "callsite_total": callsite_total,
        "direct_call_total": expression_counts.get("direct", 0),
        "attribute_call_total": expression_counts.get("attribute", 0),
        "chain_call_total": expression_counts.get("chain", 0),
        "decorator_call_total": context_counts.get("decorator", 0),
        "module_body_call_total": context_counts.get("module_body", 0),
        "class_body_call_total": context_counts.get("class_body", 0),
        "resolved_callsite_total": resolved_total,
        "unresolved_callsite_total": len(unresolved_records),
        "resolution_rate": (
            round(resolved_total / callsite_total, 4) if callsite_total else 1.0
        ),
        "by_context": dict(sorted(context_counts.items())),
        "by_expression_kind": dict(sorted(expression_counts.items())),
        "resolved_by_expression_kind": dict(sorted(resolved_expression_counts.items())),
        "unresolved_by_expression_kind": dict(
            sorted(unresolved_expression_counts.items())
        ),
        "resolution_rate_by_expression_kind": resolution_by_expression_kind,
        "by_edge_confidence": _sorted_count_mapping(confidence_counts),
        "by_edge_kind": _sorted_count_mapping(edge_kind_counts),
    }


def unresolved_callsite_diagnostics(
    metadata: IndexMetadata,
    nodes: list[Node],
    edges: list[Edge],
    *,
    repo_id: str,
    frontend_name: str,
) -> list[SemanticDiagnostic]:
    resolved = _resolved_callsite_ids(edges)
    unresolved_edges = _unresolved_callsite_edges(edges)
    source_nodes = {node.id: node for node in nodes}
    module_nodes = {
        node.path: node for node in nodes if node.kind == "module" and node.path
    }
    diagnostics: list[SemanticDiagnostic] = []
    seen_ids: set[str] = set()
    for record in collect_callsite_records(nodes):
        if record.callsite_id in resolved:
            continue
        unresolved_edge = unresolved_edges.get(record.callsite_id, {})
        callee_binding = _runtime_callee_binding(
            source_nodes.get(record.source_scope),
            module_nodes.get(record.path),
            record,
            scope_nodes=source_nodes,
        )
        failed_strategy = str(
            unresolved_edge.get("strategy") or _failed_strategy(record)
        )
        current_diagnostic_id = diagnostic_id(
            repo_id,
            "unresolved_callsite",
            record.source_scope,
            record.path,
            record.line,
            record.column,
            record.raw_expression,
        )
        if current_diagnostic_id in seen_ids:
            continue
        seen_ids.add(current_diagnostic_id)
        diagnostics.append(
            SemanticDiagnostic(
                diagnostic_id=current_diagnostic_id,
                repo_id=repo_id,
                index_version=metadata.index_version,
                diagnostic_kind="unresolved_callsite",
                message=(
                    f"Unresolved Python callsite {record.raw_expression!r} "
                    f"in {record.source_scope}"
                ),
                severity="info",
                path=record.path,
                start_line=record.line,
                frontend_name=frontend_name,
                first_seen_index=metadata.index_version,
                last_seen_index=metadata.index_version,
                properties={
                    "callsite_id": record.callsite_id,
                    "source_scope": record.source_scope,
                    "source_kind": record.source_kind,
                    "source_qualname": record.source_qualname,
                    "raw_expression": record.raw_expression,
                    "context": record.context,
                    "expression_kind": record.expression_kind,
                    "candidate_count": record.candidate_count,
                    "failed_strategy": failed_strategy,
                    "unresolved_edge_kind": unresolved_edge.get("edge_kind"),
                    **{
                        key: unresolved_edge[key]
                        for key in ("semantic_reason",)
                        if unresolved_edge.get(key) is not None
                    },
                    **callee_binding,
                    "stable_callsite_subject": record.stable_subject,
                    "stable_callsite_occurrence": record.stable_occurrence,
                    "path": record.path,
                    "line": record.line,
                    "column": record.column,
                },
            )
        )
    return diagnostics


def resolved_callsite_ids(edges: Iterable[Edge]) -> set[str]:
    resolved: set[str] = set()
    for edge in edges:
        if (
            edge.kind not in CALLSITE_RESOLUTION_EDGE_KINDS
            or edge.resolution.status != "resolved"
        ):
            continue
        if edge.resolution.callsite_id:
            resolved.add(edge.resolution.callsite_id)
            # Merged edges can carry evidence for additional callsites.
        for evidence in edge.evidence:
            if evidence.kind not in AST_CALL_EVIDENCE_KINDS:
                continue
            resolved.add(
                callsite_id(
                    edge.source,
                    evidence.path,
                    evidence.start_line,
                    evidence.column,
                    evidence.detail,
                )
            )
    return resolved


def resolved_callsite_ids_from_edge_rows(rows: Iterable[Any]) -> set[str]:
    resolved: set[str] = set()
    for row in rows:
        source = _row_value(row, "source")
        if not isinstance(source, str):
            continue
        resolution = _json_dict(_row_value(row, "resolution_json"))
        if resolution.get("status") != "resolved":
            continue
        resolution_callsite_id = resolution.get("callsite_id")
        if isinstance(resolution_callsite_id, str) and resolution_callsite_id:
            resolved.add(resolution_callsite_id)
        for evidence in _json_list(_row_value(row, "evidence_json")):
            if not isinstance(evidence, dict):
                continue
            if evidence.get("kind") not in AST_CALL_EVIDENCE_KINDS:
                continue
            detail = evidence.get("detail")
            if not isinstance(detail, str) or not detail:
                continue
            resolved.add(
                callsite_id(
                    source,
                    (
                        evidence.get("path")
                        if isinstance(evidence.get("path"), str)
                        else None
                    ),
                    _int_or_none(evidence.get("start_line")),
                    _int_or_none(evidence.get("column")),
                    detail,
                )
            )
    return resolved


def _resolved_callsite_ids(edges: list[Edge]) -> set[str]:
    return resolved_callsite_ids(edges)


def _unresolved_callsite_edges(edges: list[Edge]) -> dict[str, dict[str, Any]]:
    unresolved: dict[str, dict[str, Any]] = {}
    for edge in edges:
        if edge.resolution.status != "unresolved":
            continue
        callsite = edge.resolution.callsite_id
        if not callsite:
            continue
        unresolved[callsite] = {
            "strategy": edge.resolution.strategy,
            "edge_kind": edge.kind,
            "target": edge.target,
            "semantic_reason": _callsite_property(edge, "semantic_reason"),
        }
    return unresolved


def _callsite_property(edge: Edge, key: str) -> Any:
    callsite = edge.properties.get("callsite", {})
    return callsite.get(key) if isinstance(callsite, dict) else None


_RUNTIME_CALLEE_BINDING_KINDS = {
    "parameter",
    "assignment",
    "annotated_assignment",
    "for_target",
    "with_as",
    "except_as",
}

_LOCAL_CALLEE_BINDING_KINDS = _RUNTIME_CALLEE_BINDING_KINDS | {
    "global",
    "nonlocal",
    "import_alias",
    "type_checking_import_alias",
    "star_import",
    "function_definition",
    "method_definition",
    "class_definition",
    "deletion",
    "pattern_target",
}


def _runtime_callee_binding(
    source: Node | None,
    module: Node | None,
    record: CallsiteRecord,
    *,
    scope_nodes: dict[str, Node] | None = None,
) -> dict[str, str]:
    """Return bounded binding evidence for an already-unresolved direct call.

    This is deliberately a diagnostic projection, not a resolver.  A release
    classifier needs to distinguish ``callback(...)`` from a missing static
    symbol, but that need must not create or suppress graph edges.  Only a
    direct identifier plus a visible runtime binding textually established
    before the call are accepted. Module evidence remains a bounded structural
    signal, not proof of invocation order. Comprehension targets are excluded
    because they live in a nested Python 3 scope that the enclosing node's
    binding list cannot locate.
    """

    if source is None or record.expression_kind != "direct":
        return {}
    name = record.raw_expression
    if not name.isidentifier() or record.line is None:
        return {}
    source_bindings = _named_bindings(source, name)
    kinds = {
        str(binding.get("kind"))
        for binding in source_bindings
        if isinstance(binding.get("kind"), str) and not binding.get("static_only")
    }
    local_match = _latest_runtime_binding(
        source_bindings,
        record,
        allow_parameter=True,
        require_prior_line=True,
    )
    if local_match is not None:
        return _callee_binding_evidence(
            local_match,
            scope=(
                "module" if source.kind == "module" or "global" in kinds else "local"
            ),
        )

    # Python decides locality for the whole function at compile time.  A
    # later assignment therefore shadows a same-named module binding even
    # before that assignment executes (and can raise UnboundLocalError).  Do
    # not borrow module evidence whenever the current scope declares the name.
    # ``global`` is the explicit exception; ``nonlocal`` is itself bounded
    # evidence of runtime lookup.
    if "nonlocal" in kinds:
        return _callee_binding_evidence("nonlocal", scope="enclosing")
    if kinds & (_LOCAL_CALLEE_BINDING_KINDS - {"global"}):
        return {}

    # Indexed closures can expose the same runtime-binding evidence as a local
    # callback parameter or assignment. This affects diagnostics only: it does
    # not assert a callable target, and the nearest declaration masks ancestors.
    parent_id = (
        source.properties.get("lexical_parent") if "global" not in kinds else None
    )
    seen = {source.id}
    while parent_id and scope_nodes:
        parent = scope_nodes.get(str(parent_id))
        if parent is None or parent.id in seen or parent.path != source.path:
            break
        seen.add(parent.id)
        bindings = _named_bindings(parent, name)
        if bindings:
            match = _latest_runtime_binding(
                bindings, record, allow_parameter=True, require_prior_line=True
            )
            return _callee_binding_evidence(match, scope="enclosing") if match else {}
        parent_id = parent.properties.get("lexical_parent")

    if module is None or module.id == source.id:
        return {}
    module_match = _latest_runtime_binding(
        _named_bindings(module, name),
        record,
        allow_parameter=False,
        # Only a module binding textually established before the function's
        # call expression is bounded enough to lower release risk. A later
        # module assignment may execute before some invocations, but source
        # order alone cannot prove that and therefore remains fail-closed.
        require_prior_line=True,
    )
    if module_match is None:
        return {}
    return _callee_binding_evidence(module_match, scope="module")


def _named_bindings(node: Node, name: str) -> list[dict[str, Any]]:
    bindings = node.properties.get("bindings", [])
    if not isinstance(bindings, list):
        return []
    return [
        binding
        for binding in bindings
        if isinstance(binding, dict) and binding.get("name") == name
    ]


def _latest_runtime_binding(
    bindings: list[dict[str, Any]],
    record: CallsiteRecord,
    *,
    allow_parameter: bool,
    require_prior_line: bool,
) -> str | None:
    matches: list[tuple[int, int, str]] = []
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        kind = binding.get("kind")
        if kind not in _RUNTIME_CALLEE_BINDING_KINDS or binding.get("static_only"):
            continue
        if kind == "parameter":
            if not allow_parameter:
                continue
            matches.append((-1, -1, kind))
            continue
        line = _int_or_none(binding.get("line"))
        column = _int_or_none(binding.get("column"))
        # A same-line assignment/loop/context-manager target may not be bound
        # while its RHS/iterable/context expression is evaluated.  Refusing
        # that ambiguous case is fail-closed; ordinary established bindings
        # occur on an earlier line.
        if line is None or (require_prior_line and line >= record.line):
            continue
        matches.append((line, column if column is not None else -1, str(kind)))
    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def _callee_binding_evidence(binding_kind: str, *, scope: str) -> dict[str, str]:
    return {
        "callee_binding_kind": binding_kind,
        "callee_binding_scope": scope,
        "semantic_reason": (
            "callable_parameter"
            if binding_kind == "parameter"
            else "runtime_bound_callable"
        ),
    }


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
    else:
        loaded = value
    return loaded if isinstance(loaded, dict) else {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return []
    else:
        loaded = value
    return loaded if isinstance(loaded, list) else []


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _sorted_count_mapping(counts: Mapping[str, int]) -> dict[str, int]:
    return {
        str(key): int(value)
        for key, value in sorted(counts.items(), key=lambda item: str(item[0]))
    }


def _target_candidates(nodes: list[Node]) -> dict[str, dict[str, int]]:
    by_name: dict[str, int] = defaultdict(int)
    by_qualname: dict[str, int] = defaultdict(int)
    for node in nodes:
        if node.kind not in {"class", "function", "method"}:
            continue
        by_name[node.name] += 1
        if node.qualname:
            by_qualname[node.qualname] += 1
    return {"name": dict(by_name), "qualname": dict(by_qualname)}


def _candidate_count(
    raw_expression: str, target_candidates: dict[str, dict[str, int]]
) -> int:
    exact = target_candidates["qualname"].get(raw_expression, 0)
    if exact:
        return exact
    return target_candidates["name"].get(raw_expression.rsplit(".", 1)[-1], 0)


def _callsite_context(node: Node, callsite: dict[str, Any]) -> str:
    context = callsite.get("context")
    if isinstance(context, str) and context:
        return context
    if node.kind == "module":
        return "module_body"
    if node.kind == "class":
        return "class_body"
    return "function_body"


def _failed_strategy(record: CallsiteRecord) -> str:
    if record.expression_kind == "direct":
        return "ast_name_resolution"
    return "ast_attribute_resolution"


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None
