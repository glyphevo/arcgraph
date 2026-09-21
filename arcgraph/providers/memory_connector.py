"""External memory connector boundary for ArcGraph providers.

The connector keeps ArcGraph independent from the host application runtime.
Production integration can adapt memory services or MCP clients to this small
synchronous interface; tests can use the static connector below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from arcgraph.core.schemas import (
    MemoryCandidate,
    MemoryRef,
    WhyMemoryContext,
)


class MemoryConnectorError(RuntimeError):
    """Raised when an external memory connector fails to return usable context."""


@dataclass(slots=True)
class MemoryWhyQuery:
    repo_id: str
    target: str
    task: str | None = None
    resolved_targets: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    structural_why: list[dict[str, Any]] = field(default_factory=list)
    max_results: int = 10

    def text(self) -> str:
        return " ".join(
            item
            for item in [
                self.repo_id,
                self.target,
                self.task or "",
                *self.resolved_targets,
                *self.paths,
            ]
            if item
        )


class ExternalMemoryConnector:
    """Connector interface for retrieving external historical memory blocks."""

    def get_why_context(self, query: MemoryWhyQuery) -> WhyMemoryContext:
        raise NotImplementedError


class UnavailableExternalMemoryConnector(ExternalMemoryConnector):
    def __init__(
        self, reason: str = "External memory connector is not configured."
    ) -> None:
        self.reason = reason

    def get_why_context(self, query: MemoryWhyQuery) -> WhyMemoryContext:
        return WhyMemoryContext(
            status="unavailable",
            reason=self.reason,
            query=query.text(),
        )


class StaticExternalMemoryConnector(ExternalMemoryConnector):
    """Deterministic connector for tests and local fixtures."""

    def __init__(self, memories: Iterable[MemoryRef | dict[str, Any]]) -> None:
        self.memories = [_coerce_memory(memory) for memory in memories]

    def get_why_context(self, query: MemoryWhyQuery) -> WhyMemoryContext:
        query_text = query.text()
        ranked = sorted(
            (
                (score, memory)
                for memory in self.memories
                if (score := _score_memory(memory, query_text)) > 0
            ),
            key=lambda item: (-item[0], item[1].title),
        )
        selected = [
            memory.model_copy(update={"score": score})
            for score, memory in ranked[: query.max_results]
        ]
        return WhyMemoryContext(
            status="available",
            query=query_text,
            memories=selected,
            historical_decisions=[
                memory
                for memory in selected
                if memory.memory_type
                in {"architecture_design", "decision", "design_principle"}
            ],
            lessons_learned=[
                memory for memory in selected if memory.memory_type == "lesson_learned"
            ],
            best_practices=[
                memory for memory in selected if memory.memory_type == "best_practice"
            ],
        )


def build_memory_candidate(
    *,
    title: str,
    content: str,
    memory_type: str,
    repo_id: str,
    target: str,
    resolved_targets: list[str],
    paths: list[str],
    index_version: str | None,
    commit_sha: str | None,
) -> MemoryCandidate:
    redacted_title = _redact_sensitive_text(title)
    redacted_content = _redact_sensitive_text(content)
    tags = [
        "ArcGraph",
        f"repo:{repo_id}",
        f"type:{memory_type}",
    ]
    return MemoryCandidate(
        title=redacted_title,
        memory_type=memory_type,
        content=redacted_content,
        tags=tags,
        metadata={
            "repo_id": repo_id,
            "target": target,
            "resolved_targets": resolved_targets,
            "symbol_ids": resolved_targets,
            "paths": paths,
            "index_version": index_version,
            "arcgraph_index_version": index_version,
            "commit_sha": commit_sha,
            "source": "arcgraph_record_learning",
        },
    )


def _redact_sensitive_text(value: str) -> str:
    redacted = value
    key_value_patterns = [
        r"(?i)(password|passwd|token|api[_-]?key|secret)\s*[:=]\s*([^\s,;]+)",
        r"(?i)(authorization)\s*[:=]\s*([^\s,;]+)",
    ]
    for pattern in key_value_patterns:
        redacted = re.sub(
            pattern,
            lambda match: f"{match.group(1)}=[REDACTED]",
            redacted,
        )
    redacted = re.sub(
        r"(?i)(bearer)\s+([A-Za-z0-9._~+/\-=]+)",
        lambda match: f"{match.group(1)} [REDACTED]",
        redacted,
    )
    return redacted


def _coerce_memory(memory: MemoryRef | dict[str, Any]) -> MemoryRef:
    if isinstance(memory, MemoryRef):
        return memory
    payload = dict(memory)
    if "memory" in payload and isinstance(payload["memory"], dict):
        nested = dict(payload.pop("memory"))
        nested.setdefault("score", payload.get("score"))
        payload = nested
    if "memory_id" in payload and "id" not in payload:
        payload["id"] = str(payload.pop("memory_id"))
    payload.setdefault("title", _title_from_content(str(payload.get("content", ""))))
    payload.setdefault("content", "")
    payload.setdefault("memory_type", "document")
    if payload.get("id") is not None:
        payload["id"] = str(payload["id"])
    return MemoryRef.model_validate(payload)


def _title_from_content(content: str) -> str:
    text = " ".join(content.split())
    return text[:80] if text else "Untitled external memory"


def _score_memory(memory: MemoryRef, query_text: str) -> float:
    query_tokens = _tokens(query_text)
    if not query_tokens:
        return 0.0
    memory_text = " ".join(
        [
            memory.title,
            memory.content,
            memory.memory_type,
            " ".join(memory.tags),
            " ".join(str(value) for value in memory.metadata.values()),
        ]
    )
    memory_tokens = _tokens(memory_text)
    overlap = query_tokens & memory_tokens
    if not overlap:
        return 0.0
    return round(len(overlap) / max(len(query_tokens), 1), 4)


def _tokens(value: str) -> set[str]:
    tokens = {item.lower() for item in re.findall(r"[A-Za-z0-9_]+", value)}
    expanded: set[str] = set(tokens)
    for token in tokens:
        expanded.update(part for part in token.split("_") if part)
        expanded.update(part for part in token.split(".") if part)
    return expanded
