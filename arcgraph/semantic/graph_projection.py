"""Bridge admitted execution decisions to the existing graph schema in shadow."""

from collections import defaultdict

from arcgraph.core.ids import callsite_id, stable_callsite_subject
from arcgraph.core.merge import EvidenceMergeEngine
from arcgraph.core.schemas import CONFIDENCE_RANK, Edge, Evidence, FactResolution
from .contract import digest


def edge_key(edge):
    return edge["source"], edge["target"], edge["kind"], edge.get("semantic_role")


def call_facts(edge):
    properties = edge.get("properties", {})
    return list(
        {
            digest(
                (
                    f.get("callsite_id"),
                    f.get("line"),
                    f.get("column"),
                    f.get("call_expression"),
                )
            ): f
            for f in [properties.get("callsite", {}), *properties.get("callsites", [])]
            if f
        }.values()
    )


def public_call_identity(edge):
    """Normalize only calls; preserve the producer's role as provenance."""
    if edge.kind != "calls" or edge.semantic_role is None:
        return edge
    normalized = edge.model_copy(deep=True)
    provenance = dict(normalized.properties.get("provenance", {}))
    provenance["semantic_roles"] = sorted(
        set(provenance.get("semantic_roles", [])) | {edge.semantic_role}
    )
    normalized.properties["provenance"] = provenance
    normalized.semantic_role = None
    return normalized


def public_edges(edges):
    """Combine supports after normalization, including their original roles."""
    normalized = [public_call_identity(edge) for edge in edges]
    roles = defaultdict(set)
    for edge in normalized:
        roles[edge_key(edge.model_dump(mode="json"))].update(
            edge.properties.get("provenance", {}).get("semantic_roles", [])
        )
    result = EvidenceMergeEngine.dedupe_edges(normalized)
    for edge in result:
        original_roles = roles[edge_key(edge.model_dump(mode="json"))]
        if original_roles:
            edge.properties["provenance"] = {
                **edge.properties.get("provenance", {}),
                "semantic_roles": sorted(original_roles),
            }
    return result


def project_graph(view, raw, fallback, decisions, *, primary_producer):
    """Retain out-of-scope defaults; alter only exactly mapped callsite supports.

    Non-execution declaration/property relations remain in the complete decision
    artifact. This bridge does not invent public graph relation kinds or symbols.
    """
    nodes = {n["id"]: n for n in raw["nodes"]}
    graph_symbols = defaultdict(list)
    for node in nodes.values():
        graph_symbols[
            (node.get("path"), node.get("qualname"), node.get("start_line"))
        ].append(node["id"])
    symbols = {}
    structural_definitions = {d["id"]: d for d in view.data["definitions"]}
    for definition in view.definitions:
        info = structural_definitions[definition.id]
        choices = set(
            graph_symbols[(definition.path, definition.qualname, info["line"])]
            + graph_symbols[(definition.path, definition.qualname, info["first_line"])]
        )
        if len(choices) == 1:
            symbols[definition.id] = choices.pop()
    for node in nodes.values():
        if node["kind"] == "module":
            symbols["module:" + node["path"]] = node["id"]
    accepted = set(tuple(key) for key in decisions["projected_calls"])
    mapped = {m.raw_id: m for m in fallback.mapping if m.stage == "mapped"}
    retained, removed, losses = [], [], []
    for index, edge in enumerate(raw["edges"]):
        facts = call_facts(edge)
        rows = [mapped.get(f"edge:{index}:{i}") for i in range(max(1, len(facts)))]
        # Keep raw graph facts the wrapper could not verify, including external
        # targets. Unknown evidence never becomes a closed negative.
        kept = []
        for fact, mapping in zip(facts or [{}], rows, strict=True):
            if mapping is None:
                kept.append(fact)
                continue
            dispatch = (
                "construct"
                if view.defs[mapping.target].symbol_kind == "class"
                else "invoke"
            )
            key = (
                mapping.subject,
                mapping.target,
                dispatch,
                view.sites[mapping.subject].phase,
            )
            if key in accepted:
                kept.append(fact)
            else:
                removed.append({"edge": edge_key(edge), "fact": fact, "key": key})
        if kept:
            copy = dict(edge)
            if kept != (facts or [{}]):
                copy["properties"] = {
                    **copy.get("properties", {}),
                    "callsite": kept[0],
                    "callsites": kept,
                }
            retained.append(copy)
    by_key = {
        edge_key(edge.model_dump(mode="json")): edge
        for edge in public_edges([Edge.model_validate(edge) for edge in retained])
    }
    node_facts = {}
    occurrences = {}
    for node in nodes.values():
        counter = defaultdict(int)
        for fact in node.get("properties", {}).get("callsites", []):
            subject = stable_callsite_subject(
                node["id"],
                str(fact.get("name")),
                context=fact.get("context"),
                call_expression=fact.get("call_expression"),
                receiver_expression=fact.get("receiver"),
                attribute=fact.get("attribute"),
            )
            counter[digest(subject)] += 1
            key = (
                node["id"],
                fact.get("line"),
                fact.get("column"),
                fact.get("call_expression"),
            )
            node_facts[key] = fact
            occurrences[key] = counter[digest(subject)]
    for edge in raw["edges"]:
        for fact in call_facts(edge):
            key = (
                edge["source"],
                fact.get("line"),
                fact.get("column"),
                fact.get("call_expression"),
            )
            node_facts.setdefault(key, {"name": fact.get("raw_expression"), **fact})
            occurrences.setdefault(key, fact.get("stable_callsite_occurrence", 1))
    counts = defaultdict(set)
    for subject, target, dispatch, phase in accepted:
        counts[subject].add((target, dispatch))
    for row in decisions["edges"]:
        if row["relation"] != "execution":
            continue
        if not any(s["producer"] == primary_producer for s in row["supports"]):
            continue
        subject, target, dispatch, phase = row["key"]
        site = view.sites[subject]
        source_id, target_id = symbols.get(site.owner), symbols.get(target)
        if source_id is None or target_id is None:
            losses.append(
                {
                    "key": row["key"],
                    "reason": (
                        "source_symbol_unmapped"
                        if source_id is None
                        else "target_symbol_unmapped"
                    ),
                }
            )
            continue
        key = (source_id, site.span.start[0] + 1, site.span.start[1], site.expression)
        original = node_facts.get(key)
        if original is None:
            losses.append(
                {"key": row["key"], "reason": "default_callsite_identity_unmapped"}
            )
            continue
        name = str(original.get("raw_expression") or original.get("name"))
        identity = original.get("callsite_id") or callsite_id(
            source_id, site.path, key[1], key[2], name
        )
        confidence = max(
            (
                (
                    "inferred"
                    if s["method"] == "type_inference"
                    else "heuristic" if s["method"] == "name_guess" else s["confidence"]
                )
                for s in row["supports"]
            ),
            key=lambda c: CONFIDENCE_RANK.get(c, 0),
        )
        if confidence not in CONFIDENCE_RANK:
            confidence = "inferred"
        kind = "constructs" if dispatch == "construct" else "calls"
        fact = {
            "line": key[1],
            "column": key[2],
            "raw_expression": name,
            "call_expression": site.expression,
            "context": original.get("context", phase),
            "callsite_id": identity,
            "confidence": confidence,
            "resolution_strategy": (
                "pyright_call_hierarchy"
                if any(s["method"] == "type_inference" for s in row["supports"])
                else original.get("resolution_strategy")
            ),
            "resolution_status": "resolved",
            "candidate_count": len(counts[subject]),
            "edge_kind": kind,
            "stable_callsite_subject": original.get("stable_callsite_subject")
            or stable_callsite_subject(
                source_id,
                name,
                context=original.get("context", phase),
                call_expression=site.expression,
                receiver_expression=original.get("receiver"),
                attribute=original.get("attribute"),
            ),
            "stable_callsite_occurrence": occurrences[key],
        }
        graph_key = (source_id, target_id, kind, None)
        evidence = Evidence(
            kind="type_inference",
            path=site.path,
            start_line=key[1],
            column=key[2],
            detail="pyright callHierarchy; " + decisions["policy"],
        )
        existing = by_key.get(graph_key)
        if existing is None:
            by_key[graph_key] = Edge(
                source=source_id,
                target=target_id,
                kind=kind,
                confidence=confidence,
                resolution=FactResolution(
                    status="resolved",
                    strategy=fact["resolution_strategy"],
                    candidate_count=fact["candidate_count"],
                    callsite_id=identity,
                ),
                evidence=[evidence],
                confidence_sources={confidence: ["pyright_call_hierarchy"]},
                properties={"callsite": fact, "callsites": [fact]},
            )
        else:
            facts = {
                f.get("callsite_id") or digest(f): f
                for f in call_facts(existing.model_dump(mode="json"))
            }
            old = facts.get(identity)
            if old is None or CONFIDENCE_RANK[confidence] > CONFIDENCE_RANK.get(
                old.get("confidence"), 0
            ):
                facts[identity] = fact
            values = list(facts.values())
            existing.properties.update(callsite=values[0], callsites=values)
            if CONFIDENCE_RANK[confidence] > CONFIDENCE_RANK[existing.confidence]:
                existing.confidence = confidence
                existing.resolution = FactResolution(
                    status="resolved",
                    strategy=fact["resolution_strategy"],
                    candidate_count=fact["candidate_count"],
                    callsite_id=identity,
                )
            if evidence not in existing.evidence:
                existing.evidence.append(evidence)
            existing.confidence_sources.setdefault(confidence, []).append(
                "pyright_call_hierarchy"
            )
    return [
        edge.model_dump(mode="json")
        for _, edge in sorted(by_key.items(), key=lambda pair: str(pair[0]))
    ], {"removed_facts": removed, "graph_losses": losses}
