"""Machine-readable recovery actions for read-only query surfaces."""

from __future__ import annotations

from typing import Any

STALE_RECOVERY_COMMAND = "arcgraph sync --if-stale"


def stale_recovery_action(freshness: dict[str, Any]) -> dict[str, Any] | None:
    if freshness.get("stale") is not True and freshness.get("status") != "stale":
        return None
    return {
        "kind": "refresh_index",
        "command": STALE_RECOVERY_COMMAND,
        "automatic": False,
        "reader_remains_read_only": True,
    }
