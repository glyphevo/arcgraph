"""Tests for Celery adapter — task extraction, queue nodes, enqueue edges."""

from __future__ import annotations

import ast

from arcgraph.adapters.celery_adapter import CeleryAdapter
from arcgraph.core.schemas import FileRecord


def _parse(source: str) -> ast.Module:
    return ast.parse(source)


def _file(module: str = "myapp.tasks", path: str = "myapp/tasks.py") -> FileRecord:
    return FileRecord(
        module=module,
        path=path,
        is_package=False,
        abs_path=path,
        source_root=".",
        file_hash="test",
        line_count=0,
    )


# ── detect ──────────────────────────────────────────────────


def test_detect_true_with_celery_import() -> None:
    adapter = CeleryAdapter()
    source = "from celery import shared_task\n"
    fr = _file()
    tree = _parse(source)
    assert adapter.detect([fr], {fr.path: tree}, [], set()) is True


def test_detect_false_without_celery() -> None:
    adapter = CeleryAdapter()
    source = "import os\n"
    fr = _file()
    tree = _parse(source)
    assert adapter.detect([fr], {fr.path: tree}, [], set()) is False


# ── @shared_task ────────────────────────────────────────────


def test_shared_task_decorator() -> None:
    adapter = CeleryAdapter()
    source = """\
from celery import shared_task

@shared_task
def send_email(to, subject, body):
    pass
"""
    fr = _file()
    tree = _parse(source)
    analysis = adapter.analyze([fr], {fr.path: tree}, [], set())

    workers = [n for n in analysis.nodes if n.kind == "worker_task"]
    assert len(workers) == 1
    assert workers[0].name == "send_email"
    assert workers[0].properties["celery_task_name"] == "myapp.tasks.send_email"
    assert workers[0].properties["queue_name"] == "celery"

    queues = [n for n in analysis.nodes if n.kind == "queue"]
    assert len(queues) == 1
    assert queues[0].name == "celery"

    invokes = [e for e in analysis.edges if e.kind == "invokes"]
    assert len(invokes) == 1
    assert invokes[0].target == "fn:myapp.tasks.send_email"

    consumes = [e for e in analysis.edges if e.kind == "consumes"]
    assert len(consumes) == 1


def test_shared_task_with_call() -> None:
    adapter = CeleryAdapter()
    source = """\
from celery import shared_task

@shared_task(name="custom.email_task", queue="emails", bind=True)
def send_email(self, to, body):
    pass
"""
    fr = _file()
    tree = _parse(source)
    analysis = adapter.analyze([fr], {fr.path: tree}, [], set())

    workers = [n for n in analysis.nodes if n.kind == "worker_task"]
    assert len(workers) == 1
    assert workers[0].properties["celery_task_name"] == "custom.email_task"
    assert workers[0].properties["queue_name"] == "emails"
    assert workers[0].properties["bind"] is True


# ── @app.task ───────────────────────────────────────────────


def test_app_task_decorator() -> None:
    adapter = CeleryAdapter()
    source = """\
from celery import Celery

app = Celery("myapp")

@app.task
def process_data(data):
    pass
"""
    fr = _file()
    tree = _parse(source)
    analysis = adapter.analyze([fr], {fr.path: tree}, [], set())

    workers = [n for n in analysis.nodes if n.kind == "worker_task"]
    assert len(workers) == 1
    assert workers[0].name == "process_data"


def test_app_task_with_kwargs() -> None:
    adapter = CeleryAdapter()
    source = """\
from celery import Celery

app = Celery("myapp")

@app.task(name="myapp.heavy_compute", queue="compute")
def heavy_compute(x, y):
    return x + y
"""
    fr = _file()
    tree = _parse(source)
    analysis = adapter.analyze([fr], {fr.path: tree}, [], set())

    workers = [n for n in analysis.nodes if n.kind == "worker_task"]
    assert len(workers) == 1
    assert workers[0].properties["celery_task_name"] == "myapp.heavy_compute"
    assert workers[0].properties["queue_name"] == "compute"


# ── enqueue edges ───────────────────────────────────────────


def test_delay_enqueue_edge() -> None:
    adapter = CeleryAdapter()
    source = """\
from celery import shared_task

@shared_task
def send_email(to, body):
    pass

def trigger_email(user):
    send_email.delay(user.email, "Hello!")
"""
    fr = _file()
    tree = _parse(source)
    analysis = adapter.analyze([fr], {fr.path: tree}, [], set())

    enqueues = [e for e in analysis.edges if e.kind == "enqueues"]
    assert len(enqueues) == 1
    assert enqueues[0].source == "fn:myapp.tasks.trigger_email"
    # target should be the worker task
    assert enqueues[0].target.startswith("worker:")


def test_apply_async_enqueue_edge() -> None:
    adapter = CeleryAdapter()
    source = """\
from celery import shared_task

@shared_task
def generate_report(report_id):
    pass

def schedule_report(rid):
    generate_report.apply_async(args=[rid], countdown=60)
"""
    fr = _file()
    tree = _parse(source)
    analysis = adapter.analyze([fr], {fr.path: tree}, [], set())

    enqueues = [e for e in analysis.edges if e.kind == "enqueues"]
    assert len(enqueues) == 1
    assert enqueues[0].source == "fn:myapp.tasks.schedule_report"


def test_send_task_enqueue_edge() -> None:
    adapter = CeleryAdapter()
    source = """\
from celery import Celery

app = Celery("myapp")

def dispatch_dynamic(task_name, data):
    app.send_task("myapp.tasks.process", args=[data])
"""
    fr = _file()
    tree = _parse(source)
    analysis = adapter.analyze([fr], {fr.path: tree}, [], set())

    enqueues = [e for e in analysis.edges if e.kind == "enqueues"]
    assert len(enqueues) == 1
    assert enqueues[0].target == "worker:celery:myapp.tasks.process"
    assert enqueues[0].confidence == "unresolved"
    assert enqueues[0].resolution.status == "unresolved"
    placeholder = next(
        node
        for node in analysis.nodes
        if node.id == "worker:celery:myapp.tasks.process"
    )
    assert placeholder.properties["external_reference"] is True


# ── multiple tasks ──────────────────────────────────────────


def test_multiple_tasks_same_file() -> None:
    adapter = CeleryAdapter()
    source = """\
from celery import shared_task

@shared_task
def task_a():
    pass

@shared_task(queue="high")
def task_b():
    pass

@shared_task
def task_c():
    pass
"""
    fr = _file()
    tree = _parse(source)
    analysis = adapter.analyze([fr], {fr.path: tree}, [], set())

    workers = [n for n in analysis.nodes if n.kind == "worker_task"]
    assert len(workers) == 3
    names = sorted(w.name for w in workers)
    assert names == ["task_a", "task_b", "task_c"]

    # task_b has its own queue "high", others share "celery"
    queues = [n for n in analysis.nodes if n.kind == "queue"]
    queue_names = sorted(q.name for q in queues)
    assert "celery" in queue_names
    assert "high" in queue_names
