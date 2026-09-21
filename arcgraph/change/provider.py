"""Read-only application provider for Change Safety plans and previews."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from arcgraph.change.contracts import BaselineReference, ChangeIntent
from arcgraph.change.planner import ChangePlanner
from arcgraph.change.service import ChangeSafetyService
from arcgraph.core.schemas import Node


class ChangeSafetyProvider:
    """Expose read/compute operations while keeping persistence in the service."""

    def __init__(self, service: ChangeSafetyService, planner: ChangePlanner) -> None:
        self.service = service
        self.planner = planner

    def get_plan(self, plan_id: str) -> dict:
        return self.service.get_view(plan_id).model_dump(mode="json")

    def list_plans(self) -> list[dict]:
        return [view.model_dump(mode="json") for view in self.service.list_views()]

    def preview_plan(
        self,
        intent: ChangeIntent,
        baseline: BaselineReference,
        nodes: Iterable[Node],
        *,
        plan_id: str | None = None,
        revision: int = 1,
        openapi_input: str | Path | None = None,
    ) -> dict:
        """Compute a deterministic plan without persisting any state."""

        return self.planner.plan(
            intent,
            baseline,
            nodes,
            plan_id=plan_id,
            revision=revision,
            openapi_input=openapi_input,
        ).model_dump(mode="json")
