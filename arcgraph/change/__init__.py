"""Surgical Change Safety domain package.

The package intentionally keeps planning, verification, persistence, and
interface concerns separate.  It never performs Git writes, remote requests,
or arbitrary command execution.
"""

from arcgraph.change.contracts import CHANGE_CONTRACT_VERSION

__all__ = ["CHANGE_CONTRACT_VERSION"]
