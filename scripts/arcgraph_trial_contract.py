"""Independent black-box expectations for ArcGraph external-trial gates."""

from __future__ import annotations

EXPECTED_DEFAULT_TOOL_NAMES = (
    "arcgraph_index_status",
    "arcgraph_get_context",
    "arcgraph_explain",
    "arcgraph_get_risk",
    "arcgraph_entrypoint_flow",
    "arcgraph_get_why",
    "arcgraph_find_similar",
    "arcgraph_record_learning",
    "arcgraph_preview_change_plan",
    "arcgraph_get_change_plan",
    "arcgraph_list_change_plans",
    "arcgraph_get_graph_delta",
    "arcgraph_verify_change",
    "arcgraph_help",
)

EXPECTED_FEEDBACK_TOOL_NAMES = (
    *EXPECTED_DEFAULT_TOOL_NAMES,
    "arcgraph_record_trial_feedback",
)
