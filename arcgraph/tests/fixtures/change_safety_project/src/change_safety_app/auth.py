"""Fixture authentication boundary."""


def can_view_change(actor: str) -> bool:
    return bool(actor)
