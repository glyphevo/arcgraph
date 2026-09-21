"""Optional coverage importer for ArcGraph test evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from defusedxml import ElementTree as ET

from arcgraph.core.schemas import BuildWarning, Edge, Evidence, Node


@dataclass
class CoverageAnalysis:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    warnings: list[BuildWarning] = field(default_factory=list)
    status: str = "unavailable"
    metrics: dict[str, Any] = field(default_factory=dict)


class CoverageCollector:
    """Import runtime coverage edges from JSON or Cobertura XML files."""

    DEFAULT_CANDIDATES = (
        "arcgraph_coverage.json",
        "coverage.json",
        "coverage.xml",
        "output/arcgraph/coverage.json",
        "output/arcgraph/coverage.xml",
    )
    AGGREGATE_NODE_ID = "coverage:aggregate"

    def __init__(
        self, repo_root: Path, coverage_path: str | Path | None = None
    ) -> None:
        self.repo_root = repo_root
        self.coverage_path = Path(coverage_path) if coverage_path else None

    def analyze(self, nodes: list[Node]) -> CoverageAnalysis:
        path = self._resolve_path()
        if path is None:
            return CoverageAnalysis(
                metrics={
                    "status": "unavailable",
                    "configured": False,
                    "reason": "not_configured",
                }
            )
        if not path.exists():
            return CoverageAnalysis(
                status="partial",
                warnings=[
                    BuildWarning(
                        kind="coverage_missing",
                        path=self._display_path(path),
                        message=f"Coverage file does not exist: {path}",
                    )
                ],
                metrics={
                    "status": "partial",
                    "configured": self.coverage_path is not None,
                    "path": self._display_path(path),
                    "reason": "missing",
                },
            )

        if path.suffix.lower() == ".xml":
            return self._finalize(path, "cobertura", self._analyze_xml(path, nodes))
        analysis = self._analyze_json(path, nodes)
        format_name = str(analysis.metrics.get("format") or "json")
        return self._finalize(path, format_name, analysis)

    def _resolve_path(self) -> Path | None:
        if self.coverage_path is not None:
            return self._absolute_path(self.coverage_path)
        for candidate in self.DEFAULT_CANDIDATES:
            path = self.repo_root / candidate
            if path.exists():
                return path
        return None

    def _analyze_json(self, path: Path, nodes: list[Node]) -> CoverageAnalysis:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return CoverageAnalysis(
                status="partial",
                warnings=[
                    BuildWarning(
                        kind="coverage_parse_error",
                        path=self._display_path(path),
                        message=str(exc),
                    )
                ],
                metrics={
                    "status": "partial",
                    "configured": self.coverage_path is not None,
                    "path": self._display_path(path),
                    "format": "json",
                    "reason": "parse_error",
                },
            )

        node_by_id = {node.id: node for node in nodes}
        node_by_qualname = {node.qualname: node for node in nodes if node.qualname}
        module_by_path = {
            node.path: node
            for node in nodes
            if node.kind == "module" and isinstance(node.path, str)
        }
        if isinstance(payload, list):
            return self._analyze_browser_json(path, payload, nodes, module_by_path)
        if isinstance(payload, dict) and self._looks_like_istanbul(payload):
            return self._analyze_istanbul_json(path, payload, nodes, module_by_path)
        if not isinstance(payload, dict):
            return CoverageAnalysis(
                status="partial",
                warnings=[
                    BuildWarning(
                        kind="coverage_parse_error",
                        path=self._display_path(path),
                        message="Coverage JSON must be an object or a browser coverage list.",
                    )
                ],
                metrics={
                    "status": "partial",
                    "configured": self.coverage_path is not None,
                    "path": self._display_path(path),
                    "format": "json",
                    "reason": "unsupported_json_shape",
                },
            )
        edges: list[Edge] = []
        for test_case in payload.get("tests", []):
            if not isinstance(test_case, dict):
                continue
            source = self._resolve_node(
                test_case.get("test")
                or test_case.get("test_id")
                or test_case.get("symbol"),
                node_by_id,
                node_by_qualname,
            )
            if source is None:
                continue
            for target, properties in self._json_targets(
                test_case.get("covers", []),
                node_by_id=node_by_id,
                node_by_qualname=node_by_qualname,
                module_by_path=module_by_path,
            ):
                if target.id == source.id:
                    continue
                edges.append(
                    Edge(
                        source=source.id,
                        target=target.id,
                        kind="covers",
                        confidence="runtime-only",
                        evidence=[
                            Evidence(
                                kind="coverage_json",
                                path=source.path or self._display_path(path),
                                detail=self._display_path(path),
                            )
                        ],
                        properties=properties,
                    )
                )

        return CoverageAnalysis(
            edges=sorted(edges, key=lambda edge: (edge.source, edge.target, edge.kind)),
            metrics={"format": "json"},
        )

    def _analyze_istanbul_json(
        self,
        path: Path,
        payload: dict[str, Any],
        nodes: list[Node],
        module_by_path: dict[str, Node],
    ) -> CoverageAnalysis:
        coverage_node = self._coverage_node(path, "istanbul")
        symbol_nodes = self._symbol_nodes(nodes)
        edges: list[Edge] = []
        warnings: list[BuildWarning] = []
        unmatched = 0
        entries = 0
        for raw_key, item in payload.items():
            if not isinstance(item, dict):
                continue
            entries += 1
            raw_path = (
                item.get("path") if isinstance(item.get("path"), str) else raw_key
            )
            file_path = self._match_file_path(
                self._normalized_path(raw_path), module_by_path
            )
            if file_path is None:
                unmatched += 1
                warnings.append(
                    BuildWarning(
                        kind="coverage_istanbul_unmatched_path",
                        path=self._display_path(path),
                        message=f"Istanbul coverage path did not match a repo file: {raw_path}",
                    )
                )
                continue
            covered_lines = self._istanbul_covered_lines(item)
            if not covered_lines:
                continue
            module = module_by_path[file_path]
            edges.append(
                self._coverage_edge(
                    coverage_node.id,
                    module.id,
                    path=file_path,
                    detail=self._display_path(path),
                    evidence_kind="coverage_istanbul",
                    properties={
                        "coverage_source": "istanbul",
                        "path": file_path,
                        "covered_lines": sorted(covered_lines),
                        "covered_line_count": len(covered_lines),
                        "level": "file",
                    },
                )
            )
            edges.extend(
                self._symbol_coverage_edges(
                    coverage_node.id,
                    file_path=file_path,
                    covered_lines=covered_lines,
                    symbols=symbol_nodes,
                    detail=self._display_path(path),
                    evidence_kind="coverage_istanbul",
                    coverage_source="istanbul",
                )
            )
        return CoverageAnalysis(
            nodes=[coverage_node] if edges else [],
            edges=self._unique_edges(edges),
            warnings=warnings,
            metrics={
                "format": "istanbul",
                "entries_total": entries,
                "entries_unmatched": unmatched,
            },
        )

    def _analyze_browser_json(
        self,
        path: Path,
        payload: list[Any],
        nodes: list[Node],
        module_by_path: dict[str, Node],
    ) -> CoverageAnalysis:
        coverage_node = self._coverage_node(path, "browser_coverage")
        symbol_nodes = self._symbol_nodes(nodes)
        edges: list[Edge] = []
        warnings: list[BuildWarning] = []
        unsupported = 0
        unmatched = 0
        malformed = 0
        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                malformed += 1
                warnings.append(
                    BuildWarning(
                        kind="coverage_browser_malformed_entry",
                        path=self._display_path(path),
                        message=f"Browser coverage entry {index} is not an object.",
                    )
                )
                continue
            matched_path = self._browser_entry_path(item, module_by_path)
            if matched_path is None:
                url = item.get("url")
                if not isinstance(url, str) or self._safe_url_path(url) is None:
                    unsupported += 1
                    warnings.append(
                        BuildWarning(
                            kind="coverage_browser_unsupported_url",
                            path=self._display_path(path),
                            message=f"Browser coverage entry {index} has no supported repo-local URL.",
                        )
                    )
                else:
                    unmatched += 1
                    warnings.append(
                        BuildWarning(
                            kind="coverage_browser_unmatched_url",
                            path=self._display_path(path),
                            message=(
                                "Browser coverage URL did not match a repo file: "
                                f"{self._safe_url_path(url)}"
                            ),
                        )
                    )
                continue
            module = module_by_path[matched_path]
            text = item.get("text")
            covered_lines = (
                self._browser_covered_lines(text, item.get("ranges"))
                if isinstance(text, str)
                else set()
            )
            properties: dict[str, Any] = {
                "coverage_source": "browser_coverage",
                "path": matched_path,
                "level": "file",
            }
            if covered_lines:
                properties["covered_lines"] = sorted(covered_lines)
                properties["covered_line_count"] = len(covered_lines)
            edges.append(
                self._coverage_edge(
                    coverage_node.id,
                    module.id,
                    path=matched_path,
                    detail=self._display_path(path),
                    evidence_kind="coverage_browser",
                    properties=properties,
                )
            )
            if covered_lines:
                edges.extend(
                    self._symbol_coverage_edges(
                        coverage_node.id,
                        file_path=matched_path,
                        covered_lines=covered_lines,
                        symbols=symbol_nodes,
                        detail=self._display_path(path),
                        evidence_kind="coverage_browser",
                        coverage_source="browser_coverage",
                    )
                )
        return CoverageAnalysis(
            nodes=[coverage_node] if edges else [],
            edges=self._unique_edges(edges),
            warnings=warnings,
            metrics={
                "format": "browser_coverage",
                "entries_total": len(payload),
                "entries_unmatched": unmatched,
                "entries_unsupported": unsupported,
                "entries_malformed": malformed,
            },
        )

    def _json_targets(
        self,
        covers: Any,
        *,
        node_by_id: dict[str, Node],
        node_by_qualname: dict[str, Node],
        module_by_path: dict[str, Node],
    ) -> list[tuple[Node, dict[str, Any]]]:
        targets: list[tuple[Node, dict[str, Any]]] = []
        if not isinstance(covers, list):
            return targets
        for item in covers:
            if isinstance(item, str):
                path = item.replace("\\", "/")
                module = module_by_path.get(path)
                if module:
                    targets.append((module, {"coverage_source": "json", "path": path}))
                continue
            if not isinstance(item, dict):
                continue
            path = self._normalized_path(item.get("path"))
            if path and (module := module_by_path.get(path)):
                targets.append(
                    (
                        module,
                        {
                            "coverage_source": "json",
                            "path": path,
                            "lines": item.get("lines", []),
                        },
                    )
                )
            for symbol in item.get("symbols", []):
                target = self._resolve_node(symbol, node_by_id, node_by_qualname)
                if target:
                    targets.append(
                        (
                            target,
                            {
                                "coverage_source": "json",
                                "path": path or target.path,
                                "lines": item.get("lines", []),
                            },
                        )
                    )
        return targets

    def _analyze_xml(self, path: Path, nodes: list[Node]) -> CoverageAnalysis:
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError) as exc:
            return CoverageAnalysis(
                status="partial",
                warnings=[
                    BuildWarning(
                        kind="coverage_parse_error",
                        path=self._display_path(path),
                        message=str(exc),
                    )
                ],
                metrics={
                    "status": "partial",
                    "configured": self.coverage_path is not None,
                    "path": self._display_path(path),
                    "format": "cobertura",
                    "reason": "parse_error",
                },
            )

        module_by_path = {
            node.path: node
            for node in nodes
            if node.kind == "module" and isinstance(node.path, str)
        }
        symbol_nodes = [
            node
            for node in nodes
            if node.kind in {"class", "function", "method"} and node.path
        ]
        coverage_node = Node(
            id=self.AGGREGATE_NODE_ID,
            kind="coverage_run",
            name="aggregate",
            qualname="aggregate",
            properties={"source": self._display_path(path), "format": "cobertura"},
        )
        edges: list[Edge] = []
        for class_el in root.findall(".//class"):
            filename = self._normalized_path(class_el.get("filename"))
            file_path = self._match_file_path(filename, module_by_path)
            if file_path is None:
                continue
            covered_lines = self._covered_lines(class_el)
            if not covered_lines:
                continue
            module = module_by_path[file_path]
            edges.append(
                self._coverage_edge(
                    coverage_node.id,
                    module.id,
                    path=file_path,
                    detail=self._display_path(path),
                    properties={
                        "coverage_source": "xml",
                        "path": file_path,
                        "covered_lines": sorted(covered_lines),
                        "level": "file",
                    },
                )
            )
            for symbol in symbol_nodes:
                if symbol.path != file_path or symbol.start_line is None:
                    continue
                end_line = symbol.end_line or symbol.start_line
                if any(symbol.start_line <= line <= end_line for line in covered_lines):
                    edges.append(
                        self._coverage_edge(
                            coverage_node.id,
                            symbol.id,
                            path=file_path,
                            detail=self._display_path(path),
                            properties={
                                "coverage_source": "xml",
                                "path": file_path,
                                "level": "symbol",
                            },
                        )
                    )

        return CoverageAnalysis(
            nodes=[coverage_node] if edges else [],
            edges=sorted(edges, key=lambda edge: (edge.source, edge.target, edge.kind)),
        )

    def _finalize(
        self, path: Path, format_name: str, analysis: CoverageAnalysis
    ) -> CoverageAnalysis:
        if analysis.metrics.get("reason") == "parse_error":
            return analysis

        stale_paths = self._stale_paths(path, analysis.edges)
        status = "available" if analysis.edges else "partial"
        if stale_paths:
            status = "partial"
            analysis.warnings.append(
                BuildWarning(
                    kind="coverage_stale",
                    path=self._display_path(path),
                    message=(
                        "Coverage file is older than covered source file(s): "
                        + ", ".join(stale_paths[:5])
                    ),
                )
            )

        covered_files = sorted(
            {
                edge.properties.get("path")
                for edge in analysis.edges
                if isinstance(edge.properties.get("path"), str)
            }
        )
        covered_targets = sorted({edge.target for edge in analysis.edges})
        extra_metrics = {
            key: value
            for key, value in analysis.metrics.items()
            if key
            not in {
                "configured",
                "covered_files",
                "covered_targets",
                "edges_imported",
                "format",
                "nodes_imported",
                "path",
                "reason",
                "stale",
                "stale_paths",
                "status",
                "warnings",
            }
        }
        analysis.status = status
        analysis.metrics = {
            **extra_metrics,
            "status": status,
            "configured": self.coverage_path is not None,
            "path": self._display_path(path),
            "format": format_name,
            "nodes_imported": len(analysis.nodes),
            "edges_imported": len(analysis.edges),
            "covered_files": len(covered_files),
            "covered_targets": len(covered_targets),
            "stale": bool(stale_paths),
            "stale_paths": stale_paths[:20],
            "warnings": len(analysis.warnings),
        }
        if not analysis.edges:
            analysis.metrics["reason"] = "no_matching_coverage_targets"
        return analysis

    def _stale_paths(self, coverage_path: Path, edges: list[Edge]) -> list[str]:
        try:
            coverage_mtime = coverage_path.stat().st_mtime
        except OSError:
            return []

        stale: set[str] = set()
        for edge in edges:
            raw_path = edge.properties.get("path")
            if not isinstance(raw_path, str) or not raw_path:
                continue
            source_path = self.repo_root / raw_path
            try:
                if source_path.stat().st_mtime > coverage_mtime:
                    stale.add(raw_path)
            except OSError:
                continue
        return sorted(stale)

    @staticmethod
    def _coverage_edge(
        source: str,
        target: str,
        *,
        path: str,
        detail: str,
        evidence_kind: str = "coverage_xml",
        properties: dict[str, Any],
    ) -> Edge:
        return Edge(
            source=source,
            target=target,
            kind="covers",
            confidence="runtime-only",
            evidence=[
                Evidence(kind=evidence_kind, path=path, detail=detail),
            ],
            properties=properties,
        )

    def _coverage_node(self, path: Path, format_name: str) -> Node:
        return Node(
            id=self.AGGREGATE_NODE_ID,
            kind="coverage_run",
            name="aggregate",
            qualname="aggregate",
            properties={"source": self._display_path(path), "format": format_name},
        )

    @staticmethod
    def _symbol_nodes(nodes: list[Node]) -> list[Node]:
        return [
            node
            for node in nodes
            if node.path
            and node.start_line is not None
            and node.kind
            in {
                "class",
                "component",
                "enum",
                "function",
                "interface",
                "method",
                "type_alias",
            }
        ]

    def _symbol_coverage_edges(
        self,
        source: str,
        *,
        file_path: str,
        covered_lines: set[int],
        symbols: list[Node],
        detail: str,
        evidence_kind: str,
        coverage_source: str,
    ) -> list[Edge]:
        edges: list[Edge] = []
        for symbol in symbols:
            if symbol.path != file_path or symbol.start_line is None:
                continue
            end_line = symbol.end_line or symbol.start_line
            if not any(symbol.start_line <= line <= end_line for line in covered_lines):
                continue
            edges.append(
                self._coverage_edge(
                    source,
                    symbol.id,
                    path=file_path,
                    detail=detail,
                    evidence_kind=evidence_kind,
                    properties={
                        "coverage_source": coverage_source,
                        "path": file_path,
                        "level": "symbol",
                    },
                )
            )
        return edges

    @staticmethod
    def _unique_edges(edges: list[Edge]) -> list[Edge]:
        unique: dict[tuple[str, str, str], Edge] = {}
        for edge in edges:
            unique.setdefault((edge.source, edge.target, edge.kind), edge)
        return sorted(
            unique.values(), key=lambda edge: (edge.source, edge.target, edge.kind)
        )

    @staticmethod
    def _looks_like_istanbul(payload: dict[str, Any]) -> bool:
        return any(
            isinstance(value, dict)
            and (
                "statementMap" in value
                or "fnMap" in value
                or "branchMap" in value
                or "s" in value
                or "f" in value
            )
            for value in payload.values()
        )

    @staticmethod
    def _istanbul_covered_lines(item: dict[str, Any]) -> set[int]:
        lines: set[int] = set()
        statements = (
            item.get("statementMap")
            if isinstance(item.get("statementMap"), dict)
            else {}
        )
        hits = item.get("s") if isinstance(item.get("s"), dict) else {}
        for key, loc in statements.items():
            if CoverageCollector._hit_count(hits.get(key)) <= 0:
                continue
            lines.update(CoverageCollector._lines_from_loc(loc))
        functions = item.get("fnMap") if isinstance(item.get("fnMap"), dict) else {}
        function_hits = item.get("f") if isinstance(item.get("f"), dict) else {}
        for key, fn in functions.items():
            if CoverageCollector._hit_count(function_hits.get(key)) <= 0:
                continue
            if isinstance(fn, dict):
                lines.update(CoverageCollector._lines_from_loc(fn.get("loc")))
        return lines

    @staticmethod
    def _hit_count(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _lines_from_loc(loc: Any) -> set[int]:
        if not isinstance(loc, dict):
            return set()
        start = loc.get("start")
        end = loc.get("end")
        if not isinstance(start, dict) or not isinstance(end, dict):
            return set()
        try:
            start_line = int(start.get("line"))
            end_line = int(end.get("line"))
        except (TypeError, ValueError):
            return set()
        if start_line <= 0 or end_line < start_line:
            return set()
        return set(range(start_line, end_line + 1))

    def _browser_entry_path(
        self, item: dict[str, Any], module_by_path: dict[str, Node]
    ) -> str | None:
        url = item.get("url")
        if not isinstance(url, str):
            return None
        safe_path = self._safe_url_path(url)
        if safe_path is None:
            return None
        return self._match_file_path(safe_path, module_by_path)

    @staticmethod
    def _safe_url_path(url: str) -> str | None:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            return None
        path = unquote(parsed.path or "").lstrip("/")
        return path or None

    @staticmethod
    def _browser_covered_lines(text: str, ranges: Any) -> set[int]:
        if not isinstance(ranges, list):
            return set()
        line_spans: list[tuple[int, int, int]] = []
        cursor = 0
        for line_number, line in enumerate(text.splitlines(keepends=True), start=1):
            line_start = cursor
            cursor += len(line)
            line_spans.append((line_number, line_start, cursor))
        if text and (not line_spans or line_spans[-1][2] < len(text)):
            line_spans.append(
                (len(line_spans) + 1, line_spans[-1][2] if line_spans else 0, len(text))
            )

        covered: set[int] = set()
        for range_item in ranges:
            if not isinstance(range_item, dict):
                continue
            try:
                start = int(range_item.get("start"))
                end = int(range_item.get("end"))
            except (TypeError, ValueError):
                continue
            if end <= start:
                continue
            for line_number, line_start, line_end in line_spans:
                if start < line_end and end > line_start:
                    covered.add(line_number)
        return covered

    @staticmethod
    def _covered_lines(class_el: ET.Element) -> set[int]:
        covered: set[int] = set()
        for line_el in class_el.findall(".//line"):
            hits = line_el.get("hits")
            number = line_el.get("number")
            if hits is None or number is None:
                continue
            try:
                if int(hits) > 0:
                    covered.add(int(number))
            except ValueError:
                continue
        return covered

    @staticmethod
    def _resolve_node(
        value: Any,
        node_by_id: dict[str, Node],
        node_by_qualname: dict[str, Node],
    ) -> Node | None:
        if not isinstance(value, str) or not value:
            return None
        return node_by_id.get(value) or node_by_qualname.get(value)

    @staticmethod
    def _normalized_path(value: Any) -> str | None:
        return value.replace("\\", "/") if isinstance(value, str) and value else None

    @staticmethod
    def _match_file_path(
        filename: str | None, module_by_path: dict[str, Node]
    ) -> str | None:
        if filename is None:
            return None
        if filename in module_by_path:
            return filename
        matches = [path for path in module_by_path if path.endswith(filename)]
        return matches[0] if len(matches) == 1 else None

    def _absolute_path(self, path: Path) -> Path:
        return path if path.is_absolute() else self.repo_root / path

    def _display_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.repo_root).as_posix()
        except ValueError:
            return str(path)
