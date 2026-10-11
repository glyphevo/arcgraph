"""Store derivable semantic properties as graph references, preserving overrides."""

from arcgraph.core.storage_codec import encode_json


def fact_properties(facts, nodes, edges, encode=encode_json):
    node_map = {n.id: n for n in nodes}
    edge_map = {
        (e.source, e.target, e.kind, e.semantic_role): (i, e)
        for i, e in enumerate(edges, 1)
    }
    for fact in facts:
        reference = None
        derived = None
        node = node_map.get(fact.node_id) if fact.fact_kind == "node" else None
        if node is not None:
            derived = {
                "kind": node.kind,
                "name": node.name,
                "qualname": node.qualname,
                "canonical_identity": node.canonical_identity,
                "node_properties": node.properties,
            }
        elif fact.fact_kind == "edge":
            entry = edge_map.get(
                (fact.source, fact.target, fact.edge_kind, fact.semantic_role)
            )
            if entry is not None:
                reference, edge = entry
                derived = {
                    **edge.properties,
                    "confidence_sources": edge.confidence_sources,
                }
        if (
            derived is not None
            and derived == fact.properties
            and (
                reference is None
                or (
                    fact.resolution == edge.resolution
                    and len(fact.evidence) == len(edge.evidence)
                    and all(
                        a.__dict__ == b.__dict__
                        for a, b in zip(fact.evidence, edge.evidence)
                    )
                )
            )
        ):
            yield None, reference
        else:
            yield encode(fact.properties), None
