"""Fixture service boundary."""

from .repository import load_change


def change_status(change_id: str) -> dict[str, str]:
    record = load_change(change_id)
    return {"change_id": record["change_id"], "status": "planned"}
