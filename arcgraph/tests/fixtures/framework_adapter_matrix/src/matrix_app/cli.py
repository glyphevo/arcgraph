import click
import typer

app = typer.Typer()
admin_app = typer.Typer()
app.add_typer(admin_app, name="admin")


@app.command("sync-users")
def sync_users(limit: int = typer.Option(10, "--limit")) -> int:
    return limit


@admin_app.command("reindex")
def reindex(force: bool = typer.Option(False, "--force")) -> bool:
    return force


@click.group("ops")
def ops():
    return None


@ops.command("repair")
@click.option("--dry-run")
def repair(dry_run: bool) -> bool:
    return dry_run
