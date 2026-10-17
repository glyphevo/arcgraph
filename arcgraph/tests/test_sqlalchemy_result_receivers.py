"""SQLAlchemy result adapters have distinct implementations and result shapes."""

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.tests.test_generator_and_dynamic_exports import _targets


def result_targets(tmp_path, adapter, assigned, final):
    expression = f"session.execute(select(1)).{adapter}()"
    body = (
        f"    rows = {expression}\n    rows.{final}\n"
        if assigned
        else f"    {expression}.{final}\n"
    )
    _targets(
        tmp_path,
        {
            "user": "from sqlalchemy import select\nfrom sqlalchemy.orm import Session\n"
            "def use(session: Session):\n" + body,
        },
        v2=True,
    )
    return {
        edge.target
        for edge in GraphStoreReader.from_current(tmp_path / "out").read_edges()
        if edge.source == "fn:pkg.user.use"
        and edge.resolution.status == "resolved"
        and edge.confidence != "heuristic"
    }


@pytest.mark.parametrize("assigned", [False, True])
@pytest.mark.parametrize("final", ["all()", "unique().all()"])
@pytest.mark.parametrize(
    "adapter,owner", [("scalars", "ScalarResult"), ("mappings", "MappingResult")]
)
def test_result_adapter_methods_use_the_adapted_result_type(
    tmp_path, assigned, adapter, owner, final
):
    targets = result_targets(tmp_path, adapter, assigned, final)
    assert f"extsym:sqlalchemy.engine.{owner}.all" in targets
    assert "extsym:sqlalchemy.engine.Result.all" not in targets


@pytest.mark.parametrize("adapter", ["scalars", "mappings"])
def test_adapter_all_returns_a_sequence_not_a_mutable_list(tmp_path, adapter):
    targets = result_targets(tmp_path, adapter, False, "all().append(None)")
    # SQLAlchemy declares Sequence[_R]/Sequence[RowMapping], not list.
    assert "extsym:builtins.list.append" not in targets
    assert not any(target.endswith("Result.append") for target in targets)


@pytest.mark.parametrize(
    "terminal", ["first", "one", "one_or_none", "scalar", "scalar_one"]
)
def test_row_or_scalar_terminals_cannot_fabricate_another_result(tmp_path, terminal):
    targets = result_targets(tmp_path, terminal, False, "all()")
    assert "extsym:sqlalchemy.engine.Result.all" not in targets
