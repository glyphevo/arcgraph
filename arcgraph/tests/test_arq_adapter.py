from __future__ import annotations

import ast

from arcgraph.adapters.arq_adapter import ARQAdapter
from arcgraph.core.schemas import FileRecord, Node


def _file() -> FileRecord:
    return FileRecord(
        module="app.worker",
        path="app/worker.py",
        abs_path="app/worker.py",
        source_root=".",
        file_hash="test",
        line_count=20,
    )


def test_missing_worker_handler_is_materialized_as_unresolved() -> None:
    source = """\
class WorkerSettings:
    queue_name = "arq:jobs"
    functions = [external_handler]
"""
    file_record = _file()

    analysis = ARQAdapter().analyze(
        [file_record],
        {file_record.path: ast.parse(source)},
        [],
        set(),
    )

    edge = next(edge for edge in analysis.edges if edge.kind == "invokes")
    placeholder = next(node for node in analysis.nodes if node.id == edge.target)
    assert edge.confidence == "unresolved"
    assert edge.resolution.status == "unresolved"
    assert placeholder.properties["external_reference"] is True


def test_repeated_missing_worker_handler_stays_unresolved() -> None:
    source = """\
class WorkerSettings:
    queue_name = "arq:jobs"
    functions = [external_handler, external_handler]
"""
    file_record = _file()

    analysis = ARQAdapter().analyze(
        [file_record],
        {file_record.path: ast.parse(source)},
        [],
        set(),
    )

    invokes = [edge for edge in analysis.edges if edge.kind == "invokes"]
    placeholders = [
        node
        for node in analysis.nodes
        if node.properties.get("external_reference") is True
    ]
    assert len(invokes) == 1
    assert invokes[0].confidence == "unresolved"
    assert len(placeholders) == 1


def test_external_enqueue_target_is_materialized_as_unresolved() -> None:
    source = """\
async def producer(ctx):
    await ctx["redis"].enqueue_job("external_task", _queue_name="arq:external")
"""
    file_record = _file()
    producer = Node(
        id="fn:app.worker.producer",
        kind="function",
        name="producer",
        qualname="app.worker.producer",
        path=file_record.path,
        start_line=1,
        end_line=2,
    )

    analysis = ARQAdapter().analyze(
        [file_record],
        {file_record.path: ast.parse(source)},
        [producer],
        set(),
    )

    edge = next(edge for edge in analysis.edges if edge.kind == "enqueues")
    placeholder = next(node for node in analysis.nodes if node.id == edge.target)
    assert edge.source == producer.id
    assert edge.confidence == "unresolved"
    assert edge.resolution.status == "unresolved"
    assert placeholder.properties["resolution_status"] == "unresolved"
