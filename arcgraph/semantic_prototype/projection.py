"""Shadow policy 0.2: open execution targets and distinct dependency relations."""

from collections import Counter
from .anchors import View, BackendRun, admit

POLICY = "shadow-open-targets/0.2"


def relation(view, subject, candidate):
    fact = view.records[subject]
    if candidate.semantics == "declaration":
        return "declaration_dependency"
    if candidate.semantics == "descriptor_value":
        return "property_access"
    if fact.kind != "callsite" or fact.execution_owner is None:
        return None
    return "execution"


def gradual(annotation):
    return annotation is None or annotation.strip("'\" ") in (
        "Any",
        "typing.Any",
        "object",
        "builtins.object",
    )


def project(view: View, primary: BackendRun, fallback: BackendRun, expected_producers):
    snapshot = view.bundle.snapshot
    admission = []
    active = []
    for run, expected in zip((primary, fallback), expected_producers, strict=True):
        allowed, reason = admit(run, view, snapshot, expected)
        admission.append(
            {"producer": expected.name, "allowed": allowed, "reason": reason}
        )
        if not allowed and reason != "backend_unavailable":
            raise ValueError(reason)
        if allowed:
            active.append(run)
    primary_answers = (
        {a.subject: a for a in primary.answers} if primary in active else {}
    )
    supports, decisions = {}, []
    for run in active:
        for answer in run.answers:
            site = view.sites[answer.subject]
            for candidate in answer.candidates:
                kind = relation(view, answer.subject, candidate)
                reason = "retain_open_candidate"
                accepted = kind is not None
                main = primary_answers.get(answer.subject)
                positive = main is not None and any(
                    relation(view, answer.subject, c) == "execution"
                    for c in main.candidates
                )
                if not accepted:
                    reason = "non_execution_syntax_or_access"
                elif kind != "execution":
                    reason = "retain_" + kind
                elif run is fallback and candidate.method == "name_guess" and positive:
                    accepted, reason = False, "primary_execution_blocks_name_guess"
                elif (
                    candidate.method == "name_guess"
                    and candidate.receiver_relation == "unverified"
                ):
                    if gradual(site.receiver_annotation):
                        reason = "retain_gradual_receiver_unverified"
                    else:
                        accepted, reason = False, "nominal_receiver_relation_unverified"
                key = (answer.subject, candidate.target, candidate.dispatch, site.phase)
                evidence = {
                    "producer": run.producer.id,
                    "generation": run.id,
                    "answer": answer.envelope.record_id,
                    "method": candidate.method,
                    "confidence": candidate.confidence,
                    "receiver_relation": candidate.receiver_relation,
                    "evidence": candidate.evidence,
                    "decision": reason,
                    "policy": POLICY,
                }
                decisions.append(
                    {"key": key, "relation": kind, "accepted": accepted, **evidence}
                )
                if accepted:
                    supports.setdefault((kind, key), []).append(evidence)
    projected = {key for kind, key in supports if kind == "execution"}
    baseline = (
        {
            (a.subject, c.target, c.dispatch, view.sites[a.subject].phase)
            for a in fallback.answers
            for c in a.candidates
            if view.records[a.subject].kind == "callsite"
        }
        if fallback in active
        else set()
    )

    def pairs(keys):
        return {(view.sites[s].owner, t, d, p) for s, t, d, p in keys}

    return {
        "policy": POLICY,
        "admission": admission,
        "edges": [
            {"relation": kind, "key": key, "supports": sources}
            for (kind, key), sources in sorted(supports.items())
        ],
        "baseline_calls": sorted(baseline),
        "projected_calls": sorted(projected),
        "added_calls": sorted(projected - baseline),
        "removed_calls": sorted(baseline - projected),
        "baseline_pairs": sorted(pairs(baseline)),
        "projected_pairs": sorted(pairs(projected)),
        "added_pairs": sorted(pairs(projected) - pairs(baseline)),
        "removed_pairs": sorted(pairs(baseline) - pairs(projected)),
        "decisions": decisions,
        "mapping": {
            run.producer.name: {
                "raw": run.raw_count,
                "first_reasons": dict(Counter(m.first_reason for m in run.mapping)),
                "stages": dict(Counter(m.stage for m in run.mapping)),
            }
            for run in (primary, fallback)
        },
    }
