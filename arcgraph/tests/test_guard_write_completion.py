"""Runtime guards remain valid through the right side of the next write."""

import pytest
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.tests.test_generator_and_dynamic_exports import _targets


def _edges(tmp_path, source):
    _targets(tmp_path, {"user": source}, v2=True)
    return [
        e
        for e in GraphStoreReader.from_current(tmp_path / "out").read_edges()
        if e.source == "fn:pkg.user.use" and e.resolution.status == "resolved"
    ]


@pytest.mark.parametrize(
    "body", ["value=value.lower()", "copy=value.lower(); value=copy"]
)
def test_guard_is_not_invalidated_before_the_assignment_finishes(tmp_path, body):
    edges = _edges(
        tmp_path,
        "from typing import Any\ndef use(value:Any):\n    if isinstance(value,str):\n        "
        + body
        + "\n",
    )
    assert any(
        e.target == "extsym:builtins.str.lower"
        and e.resolution.strategy == "builtin_receiver_type"
        for e in edges
    )


def test_a_runtime_check_proves_a_non_generic_class_after_an_unknown_branch(tmp_path):
    edges = _edges(
        tmp_path,
        "from typing import Any\ndef use(data:Any,flag,other):\n    if flag:data=other\n    if not isinstance(data,dict):return\n    data.get('key')\n",
    )
    assert any(
        e.target == "extsym:builtins.dict.get"
        and e.resolution.strategy == "builtin_receiver_type"
        for e in edges
    )


def test_unknown_conditional_write_keeps_the_existing_declared_parameter_model(
    tmp_path,
):
    edges = _edges(
        tmp_path,
        "def use(value:str,flag,other):\n    if flag:value=other\n    value.split()\n",
    )
    assert any(
        e.target == "extsym:builtins.str.split"
        and e.resolution.strategy == "builtin_receiver_type"
        for e in edges
    )


BOX = "class Box:\n    def __init__(self,value):self.value=value\n    @property\n    def path(self)->str:return 'path'\n"


def test_guarded_conversion_keeps_only_the_unconverted_false_branch(tmp_path):
    edges = _edges(
        tmp_path,
        BOX
        + "def use(value:str|Box):\n    if isinstance(value,str):value=Box(value)\n    value.path.split()\n",
    )
    assert any(
        e.target == "extsym:builtins.str.split"
        and e.resolution.strategy == "builtin_receiver_type"
        for e in edges
    )


def test_an_additional_condition_does_not_exclude_the_checked_entry_branch(tmp_path):
    edges = _edges(
        tmp_path,
        BOX
        + "def use(value:str|Box,flag):\n    if isinstance(value,str) and flag:value=Box(value)\n    value.path.split()\n",
    )
    assert not any(e.target == "extsym:builtins.str.split" for e in edges)


def test_a_known_different_write_is_not_replaced_with_a_declaration(tmp_path):
    edges = _edges(
        tmp_path,
        "from pathlib import Path\nclass Other:pass\ndef use(value:Path,flag):\n    if flag:value=Other()\n    value.exists()\n",
    )
    assert not any(e.target == "extsym:pathlib.Path.exists" for e in edges)


def test_a_wholly_unknown_conditional_value_keeps_only_the_legacy_guess(tmp_path):
    edges = _edges(
        tmp_path,
        "from typing import Any\ndef use(value:Any,flag,other):\n    if flag:value=other\n    value.get('key')\n",
    )
    assert any(
        e.target == "extsym:collections.abc.Mapping.get"
        and e.resolution.strategy == "common_boundary_method"
        and e.confidence == "heuristic"
        for e in edges
    )


def test_an_attribute_annotation_does_not_hide_an_unknown_conditional_write(tmp_path):
    _targets(
        tmp_path,
        {
            "user": "class Matcher:\n    def find(self):pass\nclass Owner:\n    def use(self,flag,other):\n        self.matcher:Matcher=Matcher()\n        if flag:self.matcher=other\n        self.matcher.find()\n"
        },
        v2=True,
    )
    assert not any(
        e.source == "method:pkg.user.Owner.use"
        and e.target == "method:pkg.user.Matcher.find"
        for e in GraphStoreReader.from_current(tmp_path / "out").read_edges()
    )


def test_an_unresolved_annotation_does_not_create_a_new_dispatch_join(tmp_path):
    _targets(
        tmp_path,
        {
            "provider": "from typing import Any, MutableMapping\nMessage=MutableMapping[str,Any]\n",
            "user": "from pkg.provider import Message\ndef use(value:Message,flag,other):\n    if flag:value=other\n    value.get('key')\n",
        },
        v2=True,
    )
    assert any(
        e.source == "fn:pkg.user.use"
        and e.target == "extsym:collections.abc.Mapping.get"
        and e.confidence == "heuristic"
        for e in GraphStoreReader.from_current(tmp_path / "out").read_edges()
    )
