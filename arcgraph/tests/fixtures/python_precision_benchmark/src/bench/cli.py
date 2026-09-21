import typer

from .models import Payload
from .service import WorkService

app = typer.Typer()


@app.command("sync")
def sync(limit: int = typer.Option(10, "--limit")) -> int:
    service = WorkService()
    service.create(Payload(content="sync"))
    return limit
