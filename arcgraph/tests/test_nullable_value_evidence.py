"""Recover nullable value operations without discarding mixed/unknown evidence."""

from pathlib import Path

import pytest

from arcgraph.core.scanner import FileScanner, SourceRoot
from arcgraph.pipeline.contracts import FrontendGraphFragment
from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer


def analyze(tmp_path: Path, source: str) -> FrontendGraphFragment:
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    return PythonGraphAnalyzer().analyze(
        FileScanner(tmp_path, [SourceRoot(".")]).scan()
    )


@pytest.mark.parametrize(
    "source, target",
    [
        (
            "from typing import Iterable\ndef use(items: Iterable[str] | None):\n    if items is not None:\n        return [item.strip() for item in items]\n",
            "extsym:builtins.str.strip",
        ),
        (
            'from typing import Mapping\ndef use(items: Mapping[str, str] | None):\n    if items is not None:\n        return items.get("key")\n',
            "extsym:collections.abc.Mapping.get",
        ),
        (
            "def use(items: list[str] | None):\n    if items is not None:\n        return items[0].strip()\n",
            "extsym:builtins.str.strip",
        ),
        (
            "def use(value: tuple[int, str] | None):\n    if value is None: return\n    code, text = value\n    return text.strip()\n",
            "extsym:builtins.str.strip",
        ),
        (
            "def use(value: tuple[int, tuple[str, int]] | None):\n    if value is None: return\n    code, (text, other) = value\n    return text.strip()\n",
            "extsym:builtins.str.strip",
        ),
        (
            'def use(payload: dict[str, str] | None):\n    payload = payload or {}\n    return payload.get("key")\n',
            "extsym:builtins.dict.get",
        ),
        (
            "import tempfile\ndef use(flag):\n    temp: tempfile.TemporaryDirectory[str] | None = None\n    if flag:\n        temp = tempfile.TemporaryDirectory()\n    if temp is not None:\n        temp.cleanup()\n",
            "extsym:tempfile.TemporaryDirectory.cleanup",
        ),
        (
            "from pydantic import BaseModel\nclass Model(BaseModel): pass\ndef use(value: Model | None):\n    if value is not None:\n        return value.model_dump()\n",
            "extsym:pydantic.BaseModel.model_dump",
        ),
        (
            "from pydantic import BaseModel as B\nclass Parent(B): pass\nclass Model(Parent): pass\ndef use(value: Model | None):\n    if value is not None:\n        return value.model_dump_json()\n",
            "extsym:pydantic.BaseModel.model_dump_json",
        ),
        (
            "class Parent:\n    def fetch(self): ...\nclass Model(Parent): pass\ndef use(value: Model | None):\n    if value is not None:\n        return value.fetch()\n",
            "method:sample.Parent.fetch",
        ),
    ],
)
def test_nullable_operations_have_value_evidence(
    tmp_path: Path, source: str, target: str
) -> None:
    graph = analyze(tmp_path, source)
    assert any(
        e.source == "fn:sample.use"
        and e.target == target
        and e.resolution.status == "resolved"
        for e in graph.edges
    )


@pytest.mark.parametrize(
    "source",
    [
        'def use(payload: dict[str, str] | None):\n    payload = unknown()\n    return payload.get("key")\n',
        'def use(payload: dict[str, str] | str | None):\n    payload = payload or {}\n    return payload.get("key")\n',
        'def use(payload=None):\n    payload = payload or {}\n    return payload.get("key")\n',
        "from pydantic import BaseModel\nclass Model(BaseModel):\n    model_dump = None\ndef use(value: Model | None):\n    return value.model_dump()\n",
        "from external import BaseModel\nclass Model(BaseModel): pass\ndef use(value: Model | None):\n    return value.model_dump()\n",
        "from pydantic import BaseModel\nclass Other: pass\nclass Model(Other, BaseModel): pass\ndef use(value: Model | None):\n    return value.model_dump()\n",
        "from pydantic import BaseModel\nBaseModel = unknown()\nclass Model(BaseModel): pass\ndef use(value: Model | None):\n    return value.model_dump()\n",
        "def use(value: tuple[int, str] | None):\n    text, other = value\n    return text.strip()\n",
        "def use(value: tuple[str, ...] | None):\n    text, other = value\n    return text.strip()\n",
        "def use(value: tuple[str, str] | tuple[int, int]):\n    text, other = value\n    return text.strip()\n",
        'def use(payload: dict[str, str] | None = unknown()):\n    payload = payload or {}\n    return payload.get("key")\n',
        "def use(record: tuple[int, str] | None):\n    text.strip()\n    _, text = record\n",
        "def use(record: tuple[int, str] | None):\n    _, text = record\n    text = unknown()\n    alias = text\n    alias.strip()\n",
        "from pydantic import BaseModel\nclass Model(BaseModel):\n    @property\n    def model_dump(self): ...\ndef use(value: Model | None):\n    value.model_dump()\n",
        "from pydantic import BaseModel\nclass Model(BaseModel):\n    def model_dump(self): ...\n    model_dump = None\ndef use(value: Model | None):\n    value.model_dump()\n",
    ],
)
def test_incomplete_evidence_does_not_recover_target(
    tmp_path: Path, source: str
) -> None:
    graph = analyze(tmp_path, source)
    forbidden = {
        "extsym:pydantic.BaseModel.model_dump",
        "extsym:builtins.str.strip",
        "extsym:builtins.dict.get",
    }
    assert not [
        e for e in graph.edges if e.source == "fn:sample.use" and e.target in forbidden
    ]


def test_future_tuple_write_does_not_change_prior_call(tmp_path: Path) -> None:
    graph = analyze(
        tmp_path,
        "from pydantic import BaseModel\nclass Model(BaseModel): pass\ndef use(values: list[Model], record: tuple[str, Model] | None):\n    for value in values:\n        value.model_dump()\n    key, value = record\n",
    )
    assert any(
        e.source == "fn:sample.use"
        and e.target == "extsym:pydantic.BaseModel.model_dump"
        for e in graph.edges
    )


def test_override_keeps_its_actual_owner(tmp_path: Path) -> None:
    graph = analyze(
        tmp_path,
        "from pydantic import BaseModel\nclass Model(BaseModel):\n    def model_dump(self): ...\ndef use(value: Model | None):\n    return value.model_dump()\n",
    )
    calls = [e for e in graph.edges if e.source == "fn:sample.use"]
    assert len(calls) == 1 and calls[0].target == "method:sample.Model.model_dump"


def test_call_uses_prior_binding_not_future_tuple_member(tmp_path: Path) -> None:
    graph = analyze(
        tmp_path,
        "class First:\n    def fetch(self): ...\nclass Later:\n    def fetch(self): ...\ndef use(values: list[First], record: tuple[str, Later] | None):\n    for value in values:\n        value.fetch()\n    key, value = record\n    value.fetch()\n",
    )
    calls = [
        e for e in graph.edges if e.source == "fn:sample.use" and e.kind == "calls"
    ]
    assert {e.target for e in calls} == {
        "method:sample.First.fetch",
        "method:sample.Later.fetch",
    }
    assert (
        next(e for e in calls if e.target.endswith("First.fetch"))
        .evidence[0]
        .start_line
        == 7
    )


@pytest.mark.parametrize(
    "body",
    [
        "    _, text = record\n    text = unknown()\n    text.strip()\n",
        "    _, text = record\n    del text\n    text.strip()\n",
        "    _, text = record\n    if flag:\n        text = unknown()\n    text.strip()\n",
    ],
)
def test_unpacked_value_cannot_survive_unknown_write(tmp_path: Path, body: str) -> None:
    graph = analyze(tmp_path, "def use(record: tuple[int, str] | None, flag):\n" + body)
    assert not any(
        e.source == "fn:sample.use" and e.target == "extsym:builtins.str.strip"
        for e in graph.edges
    )


def test_nullable_proof_does_not_license_unrelated_or_expression(
    tmp_path: Path,
) -> None:
    graph = analyze(
        tmp_path,
        'def use(payload: dict[str, str] | None):\n    payload = payload or {}\n    alias = payload or unknown()\n    return alias.get("key")\n',
    )
    assert not any(
        e.source == "fn:sample.use" and e.target == "extsym:builtins.dict.get"
        for e in graph.edges
    )


@pytest.mark.parametrize("initial_override", [False, True])
def test_inherited_provider_changes_match_full_build(
    tmp_path: Path, initial_override: bool
) -> None:
    from arcgraph.core.graph_store import GraphStoreReader
    from arcgraph.pipeline.indexer import ArcGraphIndexer
    from arcgraph.pipeline.reindexer import ArcGraphReindexer

    repo = tmp_path / "repo"
    repo.mkdir()
    provider = repo / "model.py"
    parent = "class Parent:\n    def fetch(self): ...\n"
    child = (
        "class Child(Parent):\n    def fetch(self): ...\n"
        if initial_override
        else "class Child(Parent): pass\n"
    )
    provider.write_text(parent + child, encoding="utf-8")
    (repo / "consumer.py").write_text(
        "from model import Child\ndef use(value: Child):\n    return value.fetch()\n",
        encoding="utf-8",
    )
    roots = [SourceRoot(".")]
    index = tmp_path / "index"
    ArcGraphIndexer(repo, index, roots).build()

    def state(output: Path) -> list[tuple[str, str]]:
        return sorted(
            (e.target, e.resolution.status)
            for e in GraphStoreReader.from_current(output).read_edges()
            if e.source == "fn:consumer.use"
        )

    assert state(index) == [
        (
            (
                "method:model.Child.fetch"
                if initial_override
                else "method:model.Parent.fetch"
            ),
            "resolved",
        )
    ]
    for step, source in enumerate(
        [
            parent + "class Child(Parent): pass\n",
            "class Parent: pass\nclass Child(Parent): pass\n",
            parent + "class Child(Parent):\n    fetch = None\n",
            parent + "class Child(Parent): pass\n",
        ]
    ):
        provider.write_text(source, encoding="utf-8")
        ArcGraphReindexer(repo, index, roots).reindex_changed()
        full = tmp_path / f"full-{step}"
        ArcGraphIndexer(repo, full, roots).build()
        assert state(index) == state(full)
        if step in {0, 3}:
            assert state(index) == [("method:model.Parent.fetch", "resolved")]
        else:
            assert all(status == "unresolved" for _, status in state(index))


@pytest.mark.parametrize(
    "body",
    [
        "def use(items: tuple[str, int] | None):\n    return [item.strip() for item in items]\n",
        "def use(items: tuple[str, int] | None):\n    return items[1].strip()\n",
        "def use(items: list[int] | None):\n    return items[0].strip()\n",
    ],
)
def test_nullable_elements_do_not_guess_string_methods(
    tmp_path: Path, body: str
) -> None:
    graph = analyze(tmp_path, body)
    assert not any(
        e.source == "fn:sample.use" and e.target == "extsym:builtins.str.strip"
        for e in graph.edges
    )


@pytest.mark.parametrize(
    "body",
    [
        'from typing import Mapping, Any\ndef use(items: Mapping[str, Any] | None):\n    return items.get("key").fetch()\n',
        'def use(payload: dict[str, object] | None):\n    payload = payload or {}\n    return payload.get("key").fetch()\n',
    ],
)
def test_nullable_method_result_is_not_the_method_symbol(
    tmp_path: Path, body: str
) -> None:
    graph = analyze(tmp_path, body)
    calls = [
        e
        for e in graph.edges
        if e.source == "fn:sample.use"
        and e.properties["callsite"].get("attribute") == "fetch"
    ]
    assert calls and all(e.resolution.status == "unresolved" for e in calls)


def test_inherited_model_dump_has_its_documented_return_type(tmp_path: Path) -> None:
    graph = analyze(
        tmp_path,
        'from pydantic import BaseModel\nclass Model(BaseModel): pass\ndef use(value: Model | None):\n    return value.model_dump().get("key")\n',
    )
    assert any(
        e.source == "fn:sample.use" and e.target == "extsym:builtins.dict.get"
        for e in graph.edges
    )
    assert not any("model_dump.get" in e.target for e in graph.edges)


def test_nullable_write_requires_constructor_not_class_factory(tmp_path: Path) -> None:
    graph = analyze(
        tmp_path,
        "class Model:\n    def fetch(self): ...\n    @staticmethod\n    def unknown(): ...\ndef use(value: Model | None):\n    value = Model.unknown()\n    return value.fetch()\n",
    )
    assert not any(
        e.source == "fn:sample.use" and e.target == "method:sample.Model.fetch"
        for e in graph.edges
    )


def test_nested_base_does_not_resolve_to_its_container(tmp_path: Path) -> None:
    graph = analyze(
        tmp_path,
        "class Container:\n    def fetch(self): ...\n    class Parent: pass\nclass Child(Container.Parent): pass\ndef use(value: Child | None):\n    return value.fetch()\n",
    )
    assert not any(
        e.source == "fn:sample.use" and e.target == "method:sample.Container.fetch"
        for e in graph.edges
    )
