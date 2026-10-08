"""Experimental projection with traceable suppressions; no graph writes."""

from collections import Counter

from .contract import Generation, admit

POLICY = "shadow-logical-targets/0.1"


def project(primary: Generation, fallback: Generation) -> dict:
    for g in (primary, fallback):
        allowed, reason = admit(g, primary.snapshot, g.producer)
        if not allowed:
            raise ValueError(reason)
    if {s.id for s in primary.callsites} != {s.id for s in fallback.callsites}:
        raise ValueError("structure_subject_mismatch")
    sites = {s.id: s for s in primary.callsites}
    answers = {a.subject: a for a in primary.answers}
    supports = {}
    suppressed = []
    per_source = {}
    for generation in (primary, fallback):
        values = set()
        for answer in generation.answers:
            site = sites[answer.subject]
            for candidate in answer.candidates:
                reason = None
                if (
                    generation is fallback
                    and answers[answer.subject].outcome == "candidates"
                    and candidate.method == "name_guess"
                ):
                    reason = "primary_positive_blocks_name_guess"
                elif candidate.semantics in ("declaration", "descriptor_value"):
                    reason = candidate.semantics + "_not_execution_target"
                elif (
                    candidate.method == "name_guess"
                    and candidate.receiver_relation == "unverified"
                ):
                    reason = "receiver_relation_unverified"
                key = (answer.subject, candidate.target, candidate.dispatch, site.phase)
                if reason:
                    suppressed.append(
                        {
                            "key": key,
                            "producer": generation.producer.name,
                            "record": answer.envelope.record_id,
                            "reason": reason,
                        }
                    )
                    continue
                values.add(key)
                supports.setdefault(key, set()).add(
                    (generation.producer.id, answer.envelope.record_id)
                )
        per_source[generation.producer.name] = values
    # Baseline is the wrapped existing graph BEFORE the experimental policy.
    baseline = {
        (a.subject, c.target, c.dispatch, sites[a.subject].phase)
        for a in fallback.answers
        for c in a.candidates
    }
    projected = set(supports)

    def pairs(keys):
        return {(sites[s].owner, t, d, p) for s, t, d, p in keys}

    disagreements = []
    for sid in sorted(sites):
        left = {(t, d) for s, t, d, _ in per_source[primary.producer.name] if s == sid}
        right = {
            (t, d) for s, t, d, _ in per_source[fallback.producer.name] if s == sid
        }
        if left and right and left != right:
            disagreements.append(
                {
                    "site": sid,
                    "primary": sorted(left),
                    "fallback": sorted(right),
                    "classification": "open_set_disagreement",
                }
            )
    counts = Counter(r.first_reason for g in (primary, fallback) for r in g.mapping)
    return {
        "policy": POLICY,
        "admission": [
            admit(g, primary.snapshot, g.producer)[1] for g in (primary, fallback)
        ],
        "edges": [
            {"key": key, "supports": sorted(value)}
            for key, value in sorted(supports.items())
        ],
        "added_calls": sorted(projected - baseline),
        "removed_calls": sorted(baseline - projected),
        "added_pairs": sorted(pairs(projected) - pairs(baseline)),
        "removed_pairs": sorted(pairs(baseline) - pairs(projected)),
        "suppressed": suppressed,
        "disagreements": disagreements,
        "raw_units": sum(len(g.mapping) for g in (primary, fallback)),
        "first_reasons": dict(counts),
    }
