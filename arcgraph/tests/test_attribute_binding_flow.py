"""Attribute writes take effect after their statement and within proven paths."""

from pathlib import Path

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer


def _targets(tmp_path: Path, body: str) -> set[str]:
    source = (
        "class Matcher:\n    def find(self, row): return row\n"
        "class Other:\n    def find(self, row): return row\n"
        "class User:\n"
        "    def __init__(self): self.matcher = Matcher()\n"
        "    def run(self, flag, row):\n" + body
    )
    package = tmp_path / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "user.py").write_text(source, encoding="utf-8")
    output = tmp_path / "out"
    ArcGraphIndexer(
        repo_root=tmp_path,
        output_dir=output,
        source_roots=[SourceRoot("src")],
    ).build()
    return {
        edge.target
        for edge in GraphStoreReader.from_current(output).read_edges()
        if edge.source == "method:pkg.user.User.run"
        and edge.target
        and edge.target.endswith(".find")
        and edge.resolution.status == "resolved"
    }


@pytest.mark.parametrize(
    "block",
    [
        "        if flag: self.matcher = Other()\n",
        "        if flag: pass\n        else: self.matcher = Other()\n",
        "        if flag == 1: pass\n        elif flag == 2: self.matcher = Other()\n",
        "        for item in flag: self.matcher = Other()\n",
        "        while flag: self.matcher = Other(); break\n",
        "        try:\n            flag()\n            self.matcher = Other()\n"
        "        except ValueError: pass\n",
        "        try: flag()\n        except ValueError: self.matcher = Other()\n",
        "        try: flag()\n        except ValueError: pass\n"
        "        else: self.matcher = Other()\n",
        "        with flag: self.matcher = Other()\n",
        "        match flag:\n            case 1: self.matcher = Other()\n",
    ],
    ids=[
        "if",
        "else",
        "elif",
        "for",
        "while",
        "try",
        "except",
        "try-else",
        "with",
        "match",
    ],
)
def test_optional_attribute_write_does_not_become_a_definite_receiver(tmp_path, block):
    targets = _targets(tmp_path, block + "        return self.matcher.find(row)\n")
    assert targets == set(), targets


def test_attribute_call_before_same_line_write_keeps_entry_receiver(tmp_path):
    targets = _targets(
        tmp_path,
        "        result = self.matcher.find(row); self.matcher = Other()\n"
        "        return result\n",
    )
    assert targets == {"method:pkg.user.Matcher.find"}, targets


@pytest.mark.parametrize(
    "body,expected",
    [
        ("        self.matcher = Other(); return self.matcher.find(row)\n", "Other"),
        ("        self.matcher = self.matcher.find(row)\n", "Matcher"),
        (
            "        if flag:\n            self.matcher = Other()\n"
            "            return self.matcher.find(row)\n",
            "Other",
        ),
        (
            "        try: flag()\n        finally: self.matcher = Other()\n"
            "        return self.matcher.find(row)\n",
            "Other",
        ),
        (
            "        if flag: self.matcher = Other()\n"
            "        self.matcher = Matcher()\n        return self.matcher.find(row)\n",
            "Matcher",
        ),
    ],
    ids=[
        "after-same-line",
        "assignment-rhs",
        "inside-branch",
        "finally",
        "unconditional-reset",
    ],
)
def test_proven_attribute_write_and_assignment_rhs(tmp_path, body, expected):
    assert _targets(tmp_path, body) == {f"method:pkg.user.{expected}.find"}


@pytest.mark.parametrize(
    "body,expected",
    [
        (
            "        matcher = Matcher()\n        result = matcher.find(row); matcher = Other()\n        return result\n",
            "Matcher",
        ),
        (
            "        matcher = Matcher()\n        matcher = Other(); return matcher.find(row)\n",
            "Other",
        ),
        (
            "        matcher = Matcher()\n        matcher = matcher.find(row)\n",
            "Matcher",
        ),
    ],
)
def test_method_local_statement_order_is_unchanged(tmp_path, body, expected):
    assert _targets(tmp_path, body) == {f"method:pkg.user.{expected}.find"}
