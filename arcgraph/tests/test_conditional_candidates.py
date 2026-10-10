"""Conditional dispatch reports all proven targets, not one textual winner."""

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.tests.test_generator_and_dynamic_exports import _targets

PREFIX = "class Matcher:\n    def find(self,row): return row\nclass Other:\n    def find(self,row): return row\n"


@pytest.mark.parametrize("attribute", [False, True])
def test_conditional_receiver_reports_each_type_and_candidate_count(
    tmp_path, attribute
):
    if attribute:
        source = (
            PREFIX
            + "class User:\n    def __init__(self): self.matcher=Matcher()\n    def use(self,flag):\n        if flag: self.matcher=Other()\n        self.matcher.find(1)\n"
        )
        owner = "method:pkg.user.User.use"
    else:
        source = (
            PREFIX
            + "def use(flag):\n    matcher=Matcher()\n    if flag: matcher=Other()\n    matcher.find(1)\n"
        )
        owner = "fn:pkg.user.use"
    _targets(tmp_path, {"user": source}, v2=True)
    edges = [
        e
        for e in GraphStoreReader.from_current(tmp_path / "out").read_edges()
        if e.source == owner
        and e.target.endswith(".find")
        and e.resolution.status == "resolved"
    ]
    assert {e.target for e in edges} == {
        "method:pkg.user.Matcher.find",
        "method:pkg.user.Other.find",
    }
    assert all(e.resolution.candidate_count == 2 for e in edges)
    assert all(
        e.resolution.strategy == "conditional_receiver_candidates" for e in edges
    )


def test_unknown_conditional_write_does_not_turn_entry_value_into_a_proof(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "user": PREFIX
            + "def use(flag,other):\n    matcher=Matcher()\n    if flag: matcher=other\n    matcher.find(1)\n"
        },
        v2=True,
    )
    assert not {"method:pkg.user.Matcher.find", "method:pkg.user.Other.find"} & targets


@pytest.mark.parametrize(
    "branch",
    [
        "if flag: matcher=Other()",
        "for _ in range(flag): matcher=Other()",
        "try:\n        matcher=Other()\n    except Exception: pass",
        "if flag: matcher=Other()\n    else: matcher=Matcher()",
    ],
)
def test_conditional_local_control_flow_keeps_all_known_targets(tmp_path, branch):
    _targets(
        tmp_path,
        {
            "user": PREFIX
            + "def use(flag):\n    matcher=Matcher()\n    "
            + branch
            + "\n    matcher.find(1)\n"
        },
        v2=True,
    )
    edges = [
        e
        for e in GraphStoreReader.from_current(tmp_path / "out").read_edges()
        if e.source == "fn:pkg.user.use" and e.target.endswith(".find")
    ]
    assert {e.target for e in edges} == {
        "method:pkg.user.Matcher.find",
        "method:pkg.user.Other.find",
    }


def test_compact_flow_and_relationships_preserve_candidate_count():
    from arcgraph.providers.flow_payload import compact_flow
    from arcgraph.providers.relationship_payload import compact_relations

    edge = {
        "source": "fn:a",
        "target": "fn:b",
        "kind": "calls",
        "confidence": "inferred",
        "resolution": {
            "status": "resolved",
            "strategy": "conditional_receiver_candidates",
            "candidate_count": 2,
        },
        "evidence": [],
    }
    assert (
        compact_flow({"edges": [edge]})["edges"][0]["resolution"].get("candidate_count")
        == 2
    )
    assert (
        compact_relations({"scope": "calls", "incoming": [], "outgoing": [edge]}, 10)[
            "outgoing"
        ][0].get("candidate_count")
        == 2
    )


def test_conditional_unrelated_or_missing_attribute_never_guesses_a_target(tmp_path):
    targets = _targets(
        tmp_path,
        {
            "user": PREFIX
            + "class User:\n    def __init__(self): self.matcher=Matcher()\n    def use(self,flag):\n        if flag: self.matcher=Other()\n        self.matcher.inner.find(1)\n"
        },
        v2=True,
    )
    assert not any(t.endswith(".find") for t in targets)


def test_a_definite_later_write_replaces_conditional_candidates(tmp_path):
    _targets(
        tmp_path,
        {
            "user": PREFIX
            + "def use(flag):\n    matcher=Matcher()\n    if flag: matcher=Other()\n    matcher=Matcher()\n    matcher.find(1)\n"
        },
        v2=True,
    )
    edges = [
        e
        for e in GraphStoreReader.from_current(tmp_path / "out").read_edges()
        if e.source == "fn:pkg.user.use" and e.target.endswith(".find")
    ]
    assert {e.target for e in edges} == {"method:pkg.user.Matcher.find"}
