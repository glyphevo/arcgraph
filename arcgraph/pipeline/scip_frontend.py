"""SCIP protocol graph ingestion frontend."""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote

from arcgraph.core.ids import module_id
from arcgraph.core.language_tiers import language_tier_capabilities
from arcgraph.core.merge import EvidenceMergeEngine
from arcgraph.core.schemas import (
    BuildWarning,
    Edge,
    Evidence,
    FactResolution,
    FileRecord,
    FrontendCapabilities,
    Node,
)
from arcgraph.pipeline.contracts import FrontendGraphFragment
from arcgraph.pipeline.frontends import LanguageFrontend

SCIP_PROTOCOL_FRONTEND_NAME = "scip-protocol"
SCIP_PROTOCOL_FRONTEND_VERSION = "0.1.0"

_COMMON_SCIP_SOURCE_EXTENSIONS = (
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".m",
    ".mm",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".swift",
    ".ts",
    ".tsx",
)

_LANGUAGE_BY_EXTENSION = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".kt": "kotlin",
    ".m": "objective-c",
    ".mm": "objective-cpp",
    ".php": "php",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".scala": "scala",
    ".swift": "swift",
    ".ts": "typescript",
    ".tsx": "typescript",
}


class ScipProtocolFrontend(LanguageFrontend):
    """Build a compact graph fragment from a `scip print --json` payload.

    This frontend is explicit opt-in through ``--scip-graph-index``. It treats
    SCIP definitions and references as confirmed protocol facts, but does not
    infer calls, imports, or language-specific semantics.
    """

    name = SCIP_PROTOCOL_FRONTEND_NAME
    language_ids = ("scip",)
    version = SCIP_PROTOCOL_FRONTEND_VERSION

    def __init__(self, repo_root: Path, scip_graph_index_path: str | Path) -> None:
        self.repo_root = repo_root.resolve()
        self.scip_graph_index_path = self._absolute_path(scip_graph_index_path)
        self._file_extensions = self._discover_document_extensions()

    @property
    def file_extensions(self) -> tuple[str, ...]:
        return self._file_extensions

    def accepts(self, file: FileRecord) -> bool:
        return Path(file.path).suffix in self.file_extensions

    def capabilities(self) -> FrontendCapabilities:
        return FrontendCapabilities(
            name=self.name,
            version=self.version,
            language="scip",
            capabilities={
                "mode": "scip-json-protocol",
                "fact_kinds": "node,edge,diagnostic",
                "confidence": "confirmed",
                "incremental": "artifact",
                **language_tier_capabilities(
                    tier="L2",
                    source="scip",
                    scope=(
                        "SCIP protocol definitions and references only; calls, "
                        "imports, and language semantics are not inferred."
                    ),
                    languages="artifact-languages",
                ),
            },
            file_extensions=list(self.file_extensions),
        )

    def detect(self, request: object) -> bool:
        return True

    def analyze(self, request: object) -> object:
        raise RuntimeError(
            "ScipProtocolFrontend exposes graph fragments via analyze_to_graph()."
        )

    def analyze_to_graph(
        self,
        files: list[FileRecord],
        warnings: list[BuildWarning] | None = None,
        module_names: set[str] | None = None,
        call_context_nodes: list[Node] | None = None,
    ) -> FrontendGraphFragment:
        started = time.monotonic()
        warnings = list(warnings or [])
        payload = self._load_payload(warnings)
        if payload is None:
            return FrontendGraphFragment(
                warnings=warnings,
                adapter_metrics={
                    "status": "unavailable",
                    "artifact_path": self._display_path(self.scip_graph_index_path),
                },
                phase_timings={"scip_protocol": time.monotonic() - started},
            )

        documents = self._documents(payload, warnings)
        builder = _ScipGraphBuilder(self.repo_root)
        for document in documents:
            builder.collect_document(document)
        fragment = builder.build()
        warnings.extend(fragment.warnings)
        metrics = {
            **builder.metrics,
            "status": "available",
            "artifact_path": self._display_path(self.scip_graph_index_path),
            "input_documents": len(documents),
            "scanned_files": len(files),
        }
        return FrontendGraphFragment(
            nodes=fragment.nodes,
            edges=fragment.edges,
            warnings=warnings,
            adapter_metrics=metrics,
            phase_timings={"scip_protocol": time.monotonic() - started},
        )

    def _absolute_path(self, raw_path: str | Path) -> Path:
        path = Path(raw_path)
        if not path.is_absolute():
            path = self.repo_root / path
        return path.resolve()

    def _discover_document_extensions(self) -> tuple[str, ...]:
        try:
            payload = json.loads(self.scip_graph_index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return _COMMON_SCIP_SOURCE_EXTENSIONS
        if not isinstance(payload, dict):
            return _COMMON_SCIP_SOURCE_EXTENSIONS
        extensions: list[str] = []
        for document in payload.get("documents", []):
            if not isinstance(document, dict):
                continue
            relative_path = _string_value(document, "relativePath", "relative_path")
            suffix = Path(relative_path).suffix if relative_path else ""
            if suffix and suffix not in extensions:
                extensions.append(suffix)
        return tuple(extensions or _COMMON_SCIP_SOURCE_EXTENSIONS)

    def _load_payload(self, warnings: list[BuildWarning]) -> dict[str, Any] | None:
        if not self.scip_graph_index_path.exists():
            warnings.append(
                BuildWarning(
                    kind="scip_protocol_missing",
                    message=(
                        "SCIP protocol graph input does not exist: "
                        f"{self._display_path(self.scip_graph_index_path)}"
                    ),
                    path=self._display_path(self.scip_graph_index_path),
                )
            )
            return None
        try:
            payload = json.loads(self.scip_graph_index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            warnings.append(
                BuildWarning(
                    kind="scip_protocol_parse_error",
                    message=f"SCIP protocol graph input is not valid JSON: {exc}",
                    path=self._display_path(self.scip_graph_index_path),
                )
            )
            return None
        if not isinstance(payload, dict):
            warnings.append(
                BuildWarning(
                    kind="scip_protocol_parse_error",
                    message="SCIP protocol graph input must be a JSON object.",
                    path=self._display_path(self.scip_graph_index_path),
                )
            )
            return None
        return payload

    def _documents(
        self, payload: dict[str, Any], warnings: list[BuildWarning]
    ) -> list[dict[str, Any]]:
        documents = payload.get("documents", [])
        if not isinstance(documents, list):
            warnings.append(
                BuildWarning(
                    kind="scip_protocol_parse_error",
                    message="SCIP protocol graph input field 'documents' must be a list.",
                    path=self._display_path(self.scip_graph_index_path),
                )
            )
            return []
        return [document for document in documents if isinstance(document, dict)]

    def _display_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.repo_root).as_posix()
        except ValueError:
            return str(path)


class _ScipGraphBuilder:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.nodes: list[Node] = []
        self.edges: list[Edge] = []
        self.warnings: list[BuildWarning] = []
        self.definitions: dict[str, _Definition] = {}
        self.documents: list[_Document] = []
        self.languages: Counter[str] = Counter()
        self.metrics: dict[str, Any] = {
            "documents_total": 0,
            "definitions_total": 0,
            "references_total": 0,
            "external_symbols": 0,
            "duplicate_definitions": 0,
            "path_outside_repo": 0,
            "unmapped_local_references": 0,
        }

    def collect_document(self, document: dict[str, Any]) -> None:
        path = _string_value(document, "relativePath", "relative_path")
        if not path:
            self.warnings.append(
                BuildWarning(
                    kind="scip_protocol_invalid_document",
                    message="SCIP document is missing relativePath.",
                )
            )
            return
        self._warn_if_path_outside_repo(path)
        self.languages[_language_for_path(path)] += 1
        occurrences = document.get("occurrences", [])
        if not isinstance(occurrences, list):
            self.warnings.append(
                BuildWarning(
                    kind="scip_protocol_invalid_document",
                    message=f"SCIP document occurrences must be a list: {path}",
                    path=path,
                )
            )
            occurrences = []
        parsed = _Document(path=path, occurrences=[])
        self.documents.append(parsed)
        self.metrics["documents_total"] += 1
        self._add_document_nodes(path)
        for row in occurrences:
            if not isinstance(row, dict):
                continue
            occurrence = _Occurrence.from_row(path, row)
            if occurrence is None:
                continue
            parsed.occurrences.append(occurrence)
            if occurrence.is_definition:
                self._collect_definition(occurrence)

    def _warn_if_path_outside_repo(self, path: str) -> None:
        raw_path = Path(path)
        candidate = raw_path if raw_path.is_absolute() else self.repo_root / raw_path
        try:
            candidate.resolve().relative_to(self.repo_root)
        except (OSError, ValueError):
            self.metrics["path_outside_repo"] += 1
            self.warnings.append(
                BuildWarning(
                    kind="scip_protocol_path_outside_repo",
                    message=f"SCIP document path is outside the repository: {path}",
                    path=path,
                )
            )

    def build(self) -> FrontendGraphFragment:
        for document in self.documents:
            document_definitions = [
                occurrence
                for occurrence in document.occurrences
                if occurrence.is_definition
                and occurrence.symbol in self.definitions
                and self.definitions[occurrence.symbol].occurrence is occurrence
            ]
            for occurrence in document.occurrences:
                if occurrence.is_definition:
                    self._add_definition_edge(occurrence)
                    continue
                self._add_reference_edge(occurrence, document_definitions)

        nodes = EvidenceMergeEngine.dedupe_nodes(self.nodes)
        edges = EvidenceMergeEngine.dedupe_edges(self.edges)
        self.metrics["nodes_emitted"] = len(nodes)
        self.metrics["edges_emitted"] = len(edges)
        self.metrics["languages"] = sorted(self.languages)
        self.metrics["documents_by_language"] = dict(sorted(self.languages.items()))
        self.metrics["warnings"] = len(self.warnings)
        return FrontendGraphFragment(nodes=nodes, edges=edges, warnings=self.warnings)

    def _add_document_nodes(self, path: str) -> None:
        doc_id = _document_id(path)
        mod_id = _module_id_for_path(path)
        language = _language_for_path(path)
        module_name = _module_name_for_path(path)
        common_properties = _frontend_properties(language, path)
        self.nodes.append(
            Node(
                id=doc_id,
                kind="document",
                name=Path(path).name,
                qualname=path,
                path=path,
                properties={
                    **common_properties,
                    "scip_document": True,
                },
            )
        )
        self.nodes.append(
            Node(
                id=mod_id,
                kind="module",
                name=module_name.rsplit(".", 1)[-1],
                qualname=module_name,
                path=path,
                properties={
                    **common_properties,
                    "scip_document": True,
                },
            )
        )
        self.edges.append(
            Edge(
                source=doc_id,
                target=mod_id,
                kind="contains",
                confidence="confirmed",
                semantic_role="document_module",
                resolution=FactResolution(strategy="scip_protocol_document"),
                confidence_sources={"confirmed": ["scip_protocol"]},
                evidence=[Evidence(kind="scip_protocol_document", path=path)],
                properties=_frontend_properties(language, path),
            )
        )

    def _collect_definition(self, occurrence: "_Occurrence") -> None:
        if occurrence.symbol in self.definitions:
            previous = self.definitions[occurrence.symbol].occurrence
            self.metrics["duplicate_definitions"] += 1
            self.warnings.append(
                BuildWarning(
                    kind="scip_protocol_duplicate_definition",
                    message=(
                        "SCIP symbol has multiple definitions; keeping first "
                        f"definition for {occurrence.symbol}"
                    ),
                    path=occurrence.path,
                )
            )
            if previous.path == occurrence.path:
                return
            return
        symbol_node = self._symbol_node(occurrence)
        self.nodes.append(symbol_node)
        self.definitions[occurrence.symbol] = _Definition(
            node_id=symbol_node.id,
            occurrence=occurrence,
        )
        self.metrics["definitions_total"] += 1

    def _add_definition_edge(self, occurrence: "_Occurrence") -> None:
        definition = self.definitions.get(occurrence.symbol)
        if definition is None or definition.occurrence is not occurrence:
            return
        self.edges.append(
            Edge(
                source=_module_id_for_path(occurrence.path),
                target=definition.node_id,
                kind="defines",
                confidence="confirmed",
                semantic_role="definition",
                resolution=FactResolution(strategy="scip_protocol_definition"),
                confidence_sources={"confirmed": ["scip_protocol"]},
                evidence=[
                    Evidence(
                        kind="scip_protocol_definition",
                        path=occurrence.path,
                        start_line=occurrence.start_line,
                        end_line=occurrence.end_line,
                        column=occurrence.start_column,
                        detail=occurrence.symbol,
                    )
                ],
                properties=_frontend_properties(
                    _language_for_path(occurrence.path), occurrence.path
                ),
            )
        )

    def _add_reference_edge(
        self,
        occurrence: "_Occurrence",
        document_definitions: list["_Occurrence"],
    ) -> None:
        if not occurrence.symbol:
            return
        source = self._reference_source(occurrence, document_definitions)
        target = self.definitions.get(occurrence.symbol)
        target_id = (
            target.node_id
            if target is not None
            else _external_symbol_id(occurrence.symbol)
        )
        if target is None:
            self._add_external_symbol(occurrence)
            if _looks_local_symbol(occurrence.symbol):
                self.metrics["unmapped_local_references"] += 1
                self.warnings.append(
                    BuildWarning(
                        kind="scip_protocol_unmapped_symbol",
                        message=(
                            "SCIP local reference has no matching definition: "
                            f"{occurrence.symbol}"
                        ),
                        path=occurrence.path,
                    )
                )
        self.metrics["references_total"] += 1
        self.edges.append(
            Edge(
                source=source,
                target=target_id,
                kind="references",
                confidence="confirmed",
                semantic_role="reference",
                resolution=FactResolution(strategy="scip_protocol_reference"),
                confidence_sources={"confirmed": ["scip_protocol"]},
                evidence=[
                    Evidence(
                        kind="scip_protocol_reference",
                        path=occurrence.path,
                        start_line=occurrence.start_line,
                        end_line=occurrence.end_line,
                        column=occurrence.start_column,
                        detail=occurrence.symbol,
                    )
                ],
                properties=_frontend_properties(
                    _language_for_path(occurrence.path), occurrence.path
                ),
            )
        )

    def _reference_source(
        self,
        occurrence: "_Occurrence",
        document_definitions: list["_Occurrence"],
    ) -> str:
        for candidate in sorted(
            document_definitions,
            key=lambda item: item.range_width,
        ):
            if candidate.contains(occurrence):
                definition = self.definitions.get(candidate.symbol)
                if definition is not None:
                    return definition.node_id
        return _module_id_for_path(occurrence.path)

    def _symbol_node(self, occurrence: "_Occurrence") -> Node:
        display = _symbol_display_name(occurrence.symbol)
        return Node(
            id=_symbol_id(occurrence.symbol),
            kind=_symbol_kind(occurrence.symbol),
            name=display.rsplit(".", 1)[-1],
            qualname=display,
            path=occurrence.path,
            start_line=occurrence.start_line,
            end_line=occurrence.end_line,
            properties={
                **_frontend_properties(
                    _language_for_path(occurrence.path), occurrence.path
                ),
                "scip_symbol": occurrence.symbol,
            },
        )

    def _add_external_symbol(self, occurrence: "_Occurrence") -> None:
        self.metrics["external_symbols"] += 1
        display = _symbol_display_name(occurrence.symbol)
        self.nodes.append(
            Node(
                id=_external_symbol_id(occurrence.symbol),
                kind="external_symbol",
                name=display.rsplit(".", 1)[-1],
                qualname=display,
                properties={
                    **_frontend_properties(
                        _language_for_path(occurrence.path), occurrence.path
                    ),
                    "scip_symbol": occurrence.symbol,
                },
            )
        )


class _Document:
    def __init__(self, *, path: str, occurrences: list["_Occurrence"]) -> None:
        self.path = path
        self.occurrences = occurrences


class _Definition:
    def __init__(self, *, node_id: str, occurrence: "_Occurrence") -> None:
        self.node_id = node_id
        self.occurrence = occurrence


class _Occurrence:
    def __init__(
        self,
        *,
        path: str,
        symbol: str,
        is_definition: bool,
        start_line: int | None,
        end_line: int | None,
        start_column: int | None,
        range_start: tuple[int, int] | None,
        range_end: tuple[int, int] | None,
    ) -> None:
        self.path = path
        self.symbol = symbol
        self.is_definition = is_definition
        self.start_line = start_line
        self.end_line = end_line
        self.start_column = start_column
        self.range_start = range_start
        self.range_end = range_end

    @property
    def range_width(self) -> int:
        if self.range_start is None or self.range_end is None:
            return 0
        return (self.range_end[0] - self.range_start[0]) * 10000 + (
            self.range_end[1] - self.range_start[1]
        )

    def contains(self, other: "_Occurrence") -> bool:
        if (
            self.range_start is None
            or self.range_end is None
            or other.range_start is None
            or other.range_end is None
        ):
            return False
        return (
            self.range_start <= other.range_start and self.range_end >= other.range_end
        )

    @classmethod
    def from_row(cls, path: str, row: dict[str, Any]) -> "_Occurrence | None":
        symbol = _string_value(row, "symbol")
        if not symbol:
            return None
        raw_range = row.get("range")
        start_line = end_line = start_column = None
        range_start = range_end = None
        if isinstance(raw_range, list) and len(raw_range) >= 3:
            try:
                start_line_zero = int(raw_range[0])
                start_column_zero = int(raw_range[1])
                if len(raw_range) == 3:
                    end_line_zero = start_line_zero
                    end_column_zero = int(raw_range[2])
                else:
                    end_line_zero = int(raw_range[2])
                    end_column_zero = int(raw_range[3])
                start_line = start_line_zero + 1
                end_line = end_line_zero + 1
                start_column = start_column_zero + 1
                range_start = (start_line_zero, start_column_zero)
                range_end = (end_line_zero, end_column_zero)
            except (TypeError, ValueError):
                pass
        return cls(
            path=path,
            symbol=symbol,
            is_definition=_is_definition(row),
            start_line=start_line,
            end_line=end_line,
            start_column=start_column,
            range_start=range_start,
            range_end=range_end,
        )


def _is_definition(row: dict[str, Any]) -> bool:
    roles = row.get("symbolRoles", row.get("symbol_roles", 0))
    if isinstance(roles, int):
        return bool(roles & 1)
    if isinstance(roles, str):
        return roles.lower() == "definition"
    if isinstance(roles, list):
        return any(str(role).lower() == "definition" for role in roles)
    return False


def _string_value(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str):
            return value
    return ""


def _module_name_for_path(path: str) -> str:
    return ".".join(Path(path).with_suffix("").parts)


def _module_id_for_path(path: str) -> str:
    return module_id(_module_name_for_path(path))


def _document_id(path: str) -> str:
    return f"doc:scip:{quote(path, safe=':._/-')}"


def _symbol_id(symbol: str) -> str:
    return f"scip:symbol:{_digest(symbol)}"


def _external_symbol_id(symbol: str) -> str:
    return f"extsym:scip:{_digest(symbol)}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _symbol_display_name(symbol: str) -> str:
    tokens = symbol.strip().split()
    descriptor = tokens[-1] if tokens else symbol
    cleaned = (
        descriptor.replace("#", ".")
        .replace("/", ".")
        .replace("().", "")
        .replace("()", "")
        .rstrip(".")
    )
    return cleaned or symbol


def _symbol_kind(symbol: str) -> str:
    descriptor = symbol.strip().split()[-1] if symbol.strip().split() else symbol
    if "()." in descriptor or descriptor.endswith("()"):
        return "method" if "#" in descriptor else "function"
    if descriptor.endswith("#") or "#" in descriptor:
        return "class"
    return "symbol"


def _language_for_path(path: str) -> str:
    return _LANGUAGE_BY_EXTENSION.get(Path(path).suffix, "scip")


def _frontend_properties(language: str, path: str) -> dict[str, str]:
    return {
        "language": language,
        "frontend_name": SCIP_PROTOCOL_FRONTEND_NAME,
        "frontend_version": SCIP_PROTOCOL_FRONTEND_VERSION,
        "scip_path": path,
    }


def _looks_local_symbol(symbol: str) -> bool:
    return symbol.startswith("local ") or symbol.startswith("local:")
