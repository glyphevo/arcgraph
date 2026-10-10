"""Conditional attributes keep path alternatives and resolve only a common type."""

import pytest

from arcgraph.core.scanner import FileScanner, SourceRoot
from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer
from arcgraph.tests.test_attribute_binding_flow import _targets


@pytest.mark.parametrize(
    "block",
    [
        "        if flag: self.matcher=Matcher()\n",
        "        if flag: pass\n        else: self.matcher=Matcher()\n",
        "        if flag==1: pass\n        elif flag==2: self.matcher=Matcher()\n",
        "        for item in flag: self.matcher=Matcher()\n",
        "        while flag: self.matcher=Matcher(); break\n",
        "        try: self.matcher=Matcher()\n        except ValueError: pass\n",
        "        try: flag()\n        except ValueError: self.matcher=Matcher()\n",
    ],
)
def test_same_type_conditional_attribute_preserves_entry_receiver(tmp_path, block):
    assert _targets(tmp_path, block + "        self.matcher.find(row)\n") == {
        "method:pkg.user.Matcher.find"
    }


def test_exhaustive_attribute_branches_replace_entry_type(tmp_path):
    assert _targets(
        tmp_path,
        "        if flag: self.matcher=Other()\n        else: self.matcher=Other()\n        self.matcher.find(row)\n",
    ) == {"method:pkg.user.Other.find"}


def test_distinct_attribute_candidates_match_local_union_policy(tmp_path):
    source = "class Matcher:\n    def find(self): pass\nclass Other:\n    def find(self): pass\nclass User:\n    def __init__(self): self.matcher=Matcher()\n    def use(self, flag):\n        if flag: self.matcher=Other()\n        self.matcher.find()\ndef local(matcher: Matcher | Other): matcher.find()\n"
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    graph = PythonGraphAnalyzer().analyze(
        FileScanner(tmp_path, [SourceRoot(".")]).scan()
    )
    for owner in ["method:sample.User.use", "fn:sample.local"]:
        edge = next(
            e
            for e in graph.edges
            if e.source == owner
            and (e.properties.get("callsite") or {}).get("attribute") == "find"
        )
        assert edge.resolution.status == "unresolved"
        diagnostic = next(n for n in graph.nodes if n.id == edge.target)
        assert diagnostic.properties["reason"] == "ambiguous_union_receiver"
        assert {
            r.get("type_id") for r in diagnostic.properties["receiver_type_options"]
        } == {"class:sample.Matcher", "class:sample.Other"}


def test_unknown_attribute_branch_keeps_unknown_candidate(tmp_path):
    assert (
        _targets(
            tmp_path,
            "        if flag: self.matcher=flag()\n        self.matcher.find(row)\n",
        )
        == set()
    )
