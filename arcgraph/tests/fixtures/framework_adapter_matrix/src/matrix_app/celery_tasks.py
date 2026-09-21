from celery import Celery, shared_task

app = Celery("matrix")


@shared_task(name="matrix.tasks.send_email", queue="emails")
def send_email(address: str) -> str:
    return address


@app.task(queue="reports")
def build_report(report_id: int) -> int:
    return report_id


def trigger_email(address: str) -> None:
    send_email.delay(address)


def schedule_report(report_id: int) -> None:
    build_report.apply_async(args=[report_id])


def dispatch_external(payload: str) -> None:
    app.send_task("matrix.tasks.external", args=[payload])


def dispatch_dynamic(task_name: str, payload: str) -> None:
    app.send_task(task_name, args=[payload])
