"""Thin wrapper for running ArcGraph from a source checkout."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCGRAPH_ROOT = REPO_ROOT

arcgraph_root = str(ARCGRAPH_ROOT)
sys.path[:] = [entry for entry in sys.path if entry != arcgraph_root]
sys.path.insert(0, arcgraph_root)


def run() -> int:
    from arcgraph.interfaces.cli import main

    return main()


if __name__ == "__main__":
    raise SystemExit(run())
