"""Fixture API boundary."""

from .service import change_status


def get_change_status(change_id: str) -> dict[str, str]:
    return change_status(change_id)
