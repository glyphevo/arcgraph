import typer

app = typer.Typer()


@app.command("sync")
def sync(limit: int = typer.Option(10, "--limit")) -> int:
    return limit
