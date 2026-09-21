"""Analysis pass dispatcher scaffold for semantic graph overlays."""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from arcgraph.core.ids import diagnostic_id
from arcgraph.core.schemas import (
    BuildWarning,
    Edge,
    FrontendAnalyzeResult,
    FrontendCapabilities,
    IndexMetadata,
    Node,
    SemanticDiagnostic,
    SemanticFact,
)


@dataclass(slots=True)
class AnalysisPassContext:
    metadata: IndexMetadata
    repo_root: Path
    repo_id: str
    nodes: list[Node]
    edges: list[Edge]
    warnings: list[BuildWarning]
    changed_files: tuple[str, ...] = ()
    full_rebuild: bool = True


@dataclass(slots=True)
class AnalysisPassResult:
    name: str
    result: FrontendAnalyzeResult


class AnalysisPass(Protocol):
    name: str

    def run(self, context: AnalysisPassContext) -> AnalysisPassResult:
        """Run the pass against the current overlay context."""


@dataclass(slots=True)
class PassDispatcherResult:
    results: list[AnalysisPassResult] = field(default_factory=list)

    @property
    def facts(self) -> list[SemanticFact]:
        return [
            fact for pass_result in self.results for fact in pass_result.result.facts
        ]

    @property
    def diagnostics(self) -> list[SemanticDiagnostic]:
        return [
            diagnostic
            for pass_result in self.results
            for diagnostic in pass_result.result.diagnostics
        ]

    @property
    def metrics(self) -> dict[str, Any]:
        return {
            pass_result.name: pass_result.result.metrics for pass_result in self.results
        }


class PassDispatcher:
    """Run registered semantic overlay passes in deterministic order."""

    def __init__(self, passes: list[AnalysisPass] | None = None) -> None:
        self._passes: list[AnalysisPass] = []
        for analysis_pass in passes or []:
            self.register(analysis_pass)

    def register(self, analysis_pass: AnalysisPass) -> None:
        self._passes.append(analysis_pass)

    def run(self, context: AnalysisPassContext) -> PassDispatcherResult:
        return PassDispatcherResult(
            results=[
                self._run_pass(analysis_pass, context) for analysis_pass in self._passes
            ]
        )

    @property
    def passes(self) -> tuple[AnalysisPass, ...]:
        return tuple(self._passes)

    @staticmethod
    def _run_pass(
        analysis_pass: AnalysisPass, context: AnalysisPassContext
    ) -> AnalysisPassResult:
        try:
            return analysis_pass.run(context)
        except Exception as exc:
            traceback_lines = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__, limit=5)
            ).splitlines()
            traceback_excerpt = traceback_lines[:5]
            exception_line = traceback.format_exception_only(type(exc), exc)[-1].strip()
            if exception_line not in traceback_excerpt:
                traceback_excerpt.append(exception_line)
            return AnalysisPassResult(
                name=analysis_pass.name,
                result=FrontendAnalyzeResult(
                    frontend=FrontendCapabilities(
                        name=analysis_pass.name,
                        version="unknown",
                    ),
                    diagnostics=[
                        SemanticDiagnostic(
                            diagnostic_id=diagnostic_id(
                                context.repo_id,
                                "analysis_pass_failed",
                                analysis_pass.name,
                                context.metadata.index_version,
                            ),
                            repo_id=context.repo_id,
                            index_version=context.metadata.index_version,
                            diagnostic_kind="analysis_pass_failed",
                            message=(
                                f"Analysis pass {analysis_pass.name!r} failed: {exc}"
                            ),
                            severity="warning",
                            frontend_name=analysis_pass.name,
                            first_seen_index=context.metadata.index_version,
                            last_seen_index=context.metadata.index_version,
                            properties={
                                "exception_type": exc.__class__.__name__,
                                "traceback": traceback_excerpt,
                            },
                        )
                    ],
                    status="partial",
                ),
            )
