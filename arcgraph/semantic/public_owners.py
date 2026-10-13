"""Project anonymous execution owners from admitted structural records."""

from collections import defaultdict
from copy import deepcopy

from arcgraph.core.expression_kinds import call_expression_kind
from arcgraph.core.ids import callsite_id, stable_callsite_subject
from arcgraph.core.schemas import Node
from .contract import digest
from .graph_projection import call_facts, retained_edge


def owner_name(view, record):
    if record.kind == "definition":
        return record.payload["name"], record.payload["qualname"]
    name = "<" + record.payload["scope_kind"] + ">"
    parent = view.records.get(record.payload["parent"])
    prefix = (
        owner_name(view, parent)[1]
        if parent is not None
        else dict(view.bundle.snapshot.module_names).get(record.path, record.path)
    )
    return name, prefix + "." + name


def anonymous_id(view, record):
    return (
        "fn:"
        + owner_name(view, record)[1]
        + "#"
        + digest((record.path, record.span.model_dump(mode="json")))
    )


def materialize_owners(view, raw):
    """Move syntactic occurrences and their supports to the real execution owner.

    Python 3.12+ inlined comprehensions have no separate execution owner. Their
    calls stay with the surrounding owner, including the first iterable.
    """
    result = deepcopy(raw)
    owned = defaultdict(list)
    for site in view.callsites:
        record = view.records[site.id]
        owner = view.records.get(record.execution_owner)
        if record.kind != "callsite" or owner is None:
            continue
        if (
            owner.kind == "definition"
            and owner.payload["symbol_kind"] in ("lambda", "generator")
            or owner.kind == "scope"
            and owner.payload["scope_kind"] in ("ListComp", "SetComp", "DictComp")
        ):
            owned[owner.id].append(site)
    symbols = {}
    replacements = {}
    occurrences = {}
    original_facts = {
        (n.get("path"), f.get("line"), f.get("column"), f.get("call_expression")): f
        for n in raw["nodes"]
        for f in n.get("properties", {}).get("callsites", [])
    }
    for owner, sites in sorted(owned.items()):
        record = view.records[owner]
        name, qualname = owner_name(view, record)
        identity = anonymous_id(view, record)
        symbols[identity] = owner
        facts = []
        counter = defaultdict(int)
        for site in sorted(sites, key=lambda s: (s.path, s.span.start, s.span.end)):
            callee = view.records[site.id].payload["callee"]
            existing = original_facts.get(
                (site.path, site.span.start[0] + 1, site.span.start[1], site.expression)
            )
            expression = existing["name"] if existing else callee["source"]
            kind = call_expression_kind(expression)
            receiver, _, attribute = expression.rpartition(".")
            fact = {
                "name": expression,
                "line": site.span.start[0] + 1,
                "column": site.span.start[1],
                "call_expression": site.expression,
                "context": site.phase,
                "expression_kind": kind,
                "receiver": receiver if kind == "attribute" else None,
                "attribute": attribute if kind == "attribute" else None,
            }
            facts.append(fact)
            replacements[(site.path, fact["line"], fact["column"], site.expression)] = (
                identity,
                fact,
            )
            subject = stable_callsite_subject(
                identity,
                expression,
                context=site.phase,
                call_expression=site.expression,
                receiver_expression=fact["receiver"],
                attribute=fact["attribute"],
            )
            counter[digest(subject)] += 1
            occurrences[(site.path, fact["line"], fact["column"], site.expression)] = (
                counter[digest(subject)]
            )
        properties = {"callsites": facts}
        if record.kind == "definition":
            properties.update(
                params=[p["name"] for p in record.payload["parameters"]],
                **{"async": record.payload["is_async"]},
            )
        if name == "<genexpr>":
            properties["generator"] = True
        result["nodes"].append(
            Node(
                id=identity,
                kind="function",
                name=name,
                qualname=qualname,
                path=record.path,
                start_line=record.span.start[0] + 1,
                end_line=record.span.end[0] + 1,
                properties=properties,
            ).model_dump(mode="json")
        )
    paths = {n["id"]: n.get("path") for n in result["nodes"]}
    kinds = {n["id"]: n["kind"] for n in result["nodes"]}
    for node in result["nodes"]:
        if node["id"] in symbols:
            continue
        props = node.get("properties", {})
        if "callsites" in props:
            props["callsites"] = [
                f
                for f in props["callsites"]
                if (
                    node.get("path"),
                    f.get("line"),
                    f.get("column"),
                    f.get("call_expression"),
                )
                not in replacements
            ]
    edges = []
    for edge in result["edges"]:
        groups = defaultdict(list)
        for fact in call_facts(edge):
            replacement = replacements.get(
                (
                    paths.get(edge["source"]),
                    fact.get("line"),
                    fact.get("column"),
                    fact.get("call_expression"),
                )
            )
            if replacement is None:
                groups[edge["source"]].append(fact)
                continue
            source, syntax = replacement
            name = syntax["name"]
            groups[source].append(
                {
                    **fact,
                    "context": syntax["context"],
                    "raw_expression": name,
                    "stable_callsite_occurrence": occurrences[
                        (
                            paths[source],
                            syntax["line"],
                            syntax["column"],
                            syntax["call_expression"],
                        )
                    ],
                    "callsite_id": callsite_id(
                        source, paths[source], syntax["line"], syntax["column"], name
                    ),
                    "stable_callsite_subject": stable_callsite_subject(
                        source,
                        name,
                        context=syntax["context"],
                        call_expression=syntax["call_expression"],
                        receiver_expression=syntax["receiver"],
                        attribute=syntax["attribute"],
                    ),
                }
            )
        if not groups:
            edges.append(edge)
        else:
            for source, facts in groups.items():
                projected = (
                    deepcopy(edge)
                    if source == edge["source"] and facts == call_facts(edge)
                    else retained_edge(edge, facts)
                )
                projected["source"] = source
                if source in symbols and edge["kind"] == "initializes":
                    projected["kind"] = (
                        "constructs"
                        if kinds.get(edge["target"]) == "class"
                        else "uses" if edge["target"].startswith("extsym:") else "calls"
                    )
                    projected["semantic_role"] = None
                    for fact in projected["properties"]["callsites"]:
                        fact["edge_kind"] = projected["kind"]
                edges.append(projected)
    result["edges"] = edges
    result["owner_symbols"] = symbols
    return result
