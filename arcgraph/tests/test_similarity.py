from __future__ import annotations

import ast

import pytest

from arcgraph.analyzers.similarity import PROFILE_ALGORITHM, SimilarityAnalyzer
from arcgraph.core.schemas import Edge, Node


@pytest.mark.parametrize("prefix", ["", "async "])
def test_profile_does_not_mutate_the_function_ast(prefix):
    function = ast.parse(
        '@decorate("keep")\n'
        + prefix
        + "def sample(value: int = 3, *, flag=True) -> str:\n"
        "    def nested(item): return item.attribute\n"
        "    result = [item.name for item in value if item is not None]\n"
        '    return nested(result) if flag else "fallback"\n'
    ).body[0]
    before = ast.dump(function, include_attributes=True)
    original_decorator = function.decorator_list[0]
    node = Node(id="fn:sample.sample", kind="function", name="sample")
    profile = SimilarityAnalyzer()._profile_for_node(
        node, function, set(), set(), {node.id}
    )
    assert profile is not None
    assert ast.dump(function, include_attributes=True) == before
    assert function.decorator_list[0] is original_decorator


def test_profile_matches_the_baseline_fields_without_deepcopy(monkeypatch):
    from arcgraph.analyzers import similarity

    function = ast.parse(
        "def sample(value):\n    return service.send(value, 7)\n"
    ).body[0]
    node = Node(
        id="fn:sample.sample",
        kind="function",
        name="sample",
        qualname="sample.sample",
        path="src/sample.py",
        properties={"params": ["value"], "returns": None, "async": False},
    )

    def forbidden(*args, **kwargs):
        pytest.fail("profiling deep-copied the input AST")

    monkeypatch.setattr(similarity.copy, "deepcopy", forbidden)
    profile = SimilarityAnalyzer()._profile_for_node(
        node, function, {"fn:service.send"}, {"table:sample"}, {node.id}
    )
    # Captured from 3661e0c, including the ordered, truncated ngram payload.
    assert profile.profile == {
        "algorithm": "python_ast_token_ngrams_v2",
        "bucket": "src|function|1||sync",
        "structure_hash": "069fed139a823e5e",
        "ngrams": [
            "_arg body return",
            "_attr ctx load",
            "_function args arguments",
            "_name ctx load",
            "arg _arg body",
            "arg arg _arg",
            "args arg arg",
            "args arguments args",
            "args name id",
            "arguments args arg",
            "attr _attr ctx",
            "attribute value name",
            "body return value",
            "call func attribute",
            "constant value _int",
            "ctx load args",
            "ctx load attr",
            "ctx load constant",
            "func attribute value",
            "functiondef name _function",
            "id _name ctx",
            "load args name",
            "load attr _attr",
            "load constant value",
            "name _function args",
            "name id _name",
            "return value call",
            "value call func",
            "value name id",
        ],
        "call_targets": ["fn:service.send"],
        "resource_targets": ["table:sample"],
        "param_count": 1,
        "returns": None,
        "async": False,
    }


def test_same_structure_with_disjoint_features_remains_similar() -> None:
    tree = ast.parse("""
def left(value):
    return first(value)

def right(value):
    return second(value)
""")
    tree._arcgraph_module = "sample"  # type: ignore[attr-defined]
    nodes = [
        Node(
            id=f"fn:sample.{name}",
            kind="function",
            name=name,
            qualname=f"sample.{name}",
            path="src/sample.py",
            properties={"params": ["value"]},
        )
        for name in ("left", "right")
    ]
    edges = [
        Edge(source="fn:sample.left", target="fn:deps.first", kind="calls"),
        Edge(source="fn:sample.right", target="fn:deps.second", kind="calls"),
        Edge(source="fn:sample.left", target="table:first", kind="reads"),
        Edge(source="fn:sample.right", target="table:second", kind="reads"),
    ]

    analysis = SimilarityAnalyzer().analyze(
        {"src/sample.py": tree},
        nodes,
        edges,
    )

    assert len(analysis.edges) == 1
    edge = analysis.edges[0]
    assert edge.properties["score"] == 1.0
    assert edge.properties["algorithm"] == PROFILE_ALGORITHM
    assert edge.properties["reasons"] == [
        "token_jaccard=1.00",
        "same_structure",
        "call_overlap=0.00",
        "resource_overlap=0.00",
    ]
    assert all(
        node.properties["similarity"]["algorithm"] == PROFILE_ALGORITHM
        for node in nodes
    )


def test_python_similarity_does_not_restore_an_incompatible_profile() -> None:
    node = Node(
        id="fn:sample.old",
        kind="function",
        name="old",
        qualname="sample.old",
        path="src/sample.py",
        properties={
            "similarity": {
                "algorithm": "python_ast_token_ngrams_v0",
                "bucket": "src|function|0|sync",
                "structure_hash": "old",
                "ngrams": ["one", "two", "three"],
                "call_targets": [],
                "resource_targets": [],
            }
        },
    )

    profile = SimilarityAnalyzer()._profile_for_node(node, None, set(), set(), set())

    assert profile is None
    # The incompatible profile must not be restored, and must also not be
    # stripped: the reindexer passes the live unchanged nodes it republishes,
    # so popping here would delete the owning frontend's persisted profile.
    assert node.properties["similarity"]["algorithm"] == "python_ast_token_ngrams_v0"


SHAPES = """async def shaped(a, /, b: int = 1, *rest, c, **more) -> int:
    class Inner[T]:
        pass

    try:
        return [x async for x in a if x]
    except (ValueError, KeyError) as error:
        raise RuntimeError from error
    finally:
        del b
"""


def _normalized_function(source: str) -> ast.AST:
    import copy

    from arcgraph.analyzers.similarity import _NormalizedAst

    function = ast.parse(source).body[0]
    normalized = _NormalizedAst().visit(copy.deepcopy(function))
    ast.fix_missing_locations(normalized)
    return normalized


def test_the_structure_dump_is_the_same_on_every_python_version() -> None:
    from arcgraph.analyzers.similarity import _structure_dump

    # ast.dump kept None and empty lists up to 3.12 and leaves them out from
    # 3.13 on, and 3.12 added type_params; the profile's dump must not follow.
    dumped = _structure_dump(_normalized_function("def f(a):\n    return a\n"))
    assert dumped == (
        "FunctionDef(name='_function', args=arguments(args=[arg(arg='_arg')]), "
        "body=[Return(value=Name(id='_name', ctx=Load()))])"
    )


def test_the_structure_dump_is_the_dump_of_python_3_13() -> None:
    import sys

    import pytest

    from arcgraph.analyzers.similarity import _structure_dump

    if sys.version_info < (3, 13):
        pytest.skip("ast.dump leaves empty fields out from Python 3.13 on")
    normalized = _normalized_function(SHAPES)
    assert _structure_dump(normalized) == ast.dump(normalized, include_attributes=False)
