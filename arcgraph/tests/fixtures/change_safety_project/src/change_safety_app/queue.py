"""Fixture queue boundary."""


def enqueue_change(change_id: str) -> str:
    return f"queued:{change_id}"
