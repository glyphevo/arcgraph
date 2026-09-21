from __future__ import annotations

import os

import pytest


def skip_or_fail(message: str) -> None:
    """Fail formal TS acceptance lanes; permit local optional-tool skips."""

    required = os.environ.get("ARCGRAPH_REQUIRE_TS", "").strip().lower()
    if required in {"1", "true", "yes", "on"}:
        pytest.fail(message)
    pytest.skip(message)
