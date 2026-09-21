"""TypeScript graph analyzer backed by the TypeScript compiler API."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import time
import zlib
from pathlib import Path
from typing import Any

from arcgraph.core.ids import external_package_id, module_id
from arcgraph.core.merge import EvidenceMergeEngine
from arcgraph.core.scanner import logical_module_name
from arcgraph.core.schemas import (
    BuildWarning,
    Edge,
    Evidence,
    FactResolution,
    FileRecord,
    Node,
)
from arcgraph.pipeline.contracts import FrontendGraphFragment
from arcgraph.pipeline.typescript_frameworks import TypeScriptFrameworkAnalyzer

TYPESCRIPT_FRONTEND_NAME = "typescript-static"
TYPESCRIPT_FRONTEND_VERSION = "0.1.0"
TYPESCRIPT_SIMILARITY_PROFILE_ALGORITHM = "typescript_syntax_kind_hashes_v6"
TYPESCRIPT_SOURCE_EXTENSIONS = (
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
)
TYPESCRIPT_FILE_EXTENSIONS = (*TYPESCRIPT_SOURCE_EXTENSIONS, ".vue")


def typescript_similarity_profile_count(nodes: list[Node]) -> int:
    """Count TypeScript callables carrying a similarity profile in *nodes*."""

    return sum(
        1
        for node in nodes
        if node.kind in {"function", "method"}
        and node.properties.get("frontend_name") == TYPESCRIPT_FRONTEND_NAME
        and isinstance(node.properties.get("similarity"), dict)
    )


class TypeScriptGraphAnalyzer:
    """Extract a best-effort TypeScript/React graph from TS compiler AST."""

    def __init__(self, repo_root: Path, *, timeout_seconds: float = 60) -> None:
        self.repo_root = repo_root.resolve()
        self.timeout_seconds = timeout_seconds

    def analyze(
        self,
        files: list[FileRecord],
        warnings: list[BuildWarning] | None = None,
        module_names: set[str] | None = None,
        call_context_nodes: list[Node] | None = None,
        incremental_similarity_buckets: set[str] | None = None,
    ) -> FrontendGraphFragment:
        warnings = list(warnings or [])
        frontend_files = [
            file for file in files if file.path.endswith(TYPESCRIPT_FILE_EXTENSIONS)
        ]
        ts_files = [
            file
            for file in frontend_files
            if file.path.endswith(TYPESCRIPT_SOURCE_EXTENSIONS)
        ]
        context_nodes = call_context_nodes or []
        similarity_profiles = self._similarity_profiles(context_nodes)
        incremental_similarity = incremental_similarity_buckets is not None
        # A deletion-only incremental refresh has no current frontend file but
        # still needs the lightweight scorer for the previous affected buckets.
        if (
            not frontend_files
            and not similarity_profiles
            and not incremental_similarity_buckets
        ):
            return FrontendGraphFragment(warnings=warnings)

        phase_timings: dict[str, float] = {}
        started = time.monotonic()
        payload: dict[str, Any] = {}
        if ts_files or (not incremental_similarity and similarity_profiles):
            try:
                if incremental_similarity:
                    payload = self._run_extractor(
                        ts_files,
                        context_nodes,
                        similarity_profiles=[],
                        defer_similarity_scoring=True,
                    )
                else:
                    payload = self._run_extractor(
                        ts_files,
                        context_nodes,
                        similarity_profiles=similarity_profiles,
                    )
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                return self._unavailable_fragment(
                    warnings, started, exc, tool_name="TypeScript extractor"
                )

        if incremental_similarity:
            try:
                self._apply_incremental_similarity(
                    payload,
                    similarity_profiles=similarity_profiles,
                    incremental_similarity_buckets=incremental_similarity_buckets,
                    context_nodes=context_nodes,
                    ts_files=ts_files,
                )
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                return self._unavailable_fragment(
                    warnings,
                    started,
                    exc,
                    tool_name="TypeScript similarity scorer",
                )

        phase_timings["typescript_extract"] = time.monotonic() - started
        nodes = self._module_nodes(ts_files)
        nodes.extend(self._nodes_from_payload(payload.get("nodes", [])))
        edges = self._edges_from_payload(payload.get("edges", []))
        nodes.extend(self._unresolved_reference_nodes(edges))
        warnings.extend(self._warnings_from_payload(payload.get("warnings", [])))
        nodes.extend(self._external_package_nodes(payload.get("external_packages", [])))
        nodes.extend(self._external_symbol_nodes(payload.get("external_symbols", [])))

        framework_result = TypeScriptFrameworkAnalyzer(str(self.repo_root)).analyze(
            frontend_files,
            nodes,
        )
        nodes.extend(framework_result.nodes)
        edges.extend(framework_result.edges)
        warnings.extend(framework_result.warnings)

        nodes = EvidenceMergeEngine.dedupe_nodes(nodes)
        edges = EvidenceMergeEngine.dedupe_edges(edges)
        phase_timings["typescript_total"] = time.monotonic() - started
        return FrontendGraphFragment(
            nodes=nodes,
            edges=edges,
            warnings=warnings,
            adapter_metrics=framework_result.metrics,
            external_packages=set(payload.get("external_packages", [])),
            extractor_metadata=(
                {
                    "similarity_profile_algorithm": (
                        TYPESCRIPT_SIMILARITY_PROFILE_ALGORITHM
                    ),
                    # Callables below the n-gram floor legitimately carry no
                    # profile, so "this index has none" is only evidence of
                    # damage when the build that wrote it produced some. This
                    # counts the fragment this frontend produced; an
                    # incremental caller must recount over the merged graph,
                    # since a batch of changed files is not the whole index.
                    "similarity_profile_count": typescript_similarity_profile_count(
                        nodes
                    ),
                }
                if ts_files or similarity_profiles
                else {}
            ),
            phase_timings=phase_timings,
        )

    def _unavailable_fragment(
        self,
        warnings: list[BuildWarning],
        started: float,
        exc: BaseException,
        *,
        tool_name: str,
    ) -> FrontendGraphFragment:
        message = (
            f"{tool_name} timed out after {self.timeout_seconds}s."
            if isinstance(exc, subprocess.TimeoutExpired)
            else str(exc)
        )
        warnings.append(
            BuildWarning(
                kind="typescript_frontend_unavailable",
                message=message,
                frontend_name=TYPESCRIPT_FRONTEND_NAME,
            )
        )
        return FrontendGraphFragment(
            warnings=warnings,
            phase_timings={"typescript": time.monotonic() - started},
        )

    def _apply_incremental_similarity(
        self,
        payload: dict[str, Any],
        *,
        similarity_profiles: list[dict[str, Any]],
        incremental_similarity_buckets: set[str],
        context_nodes: list[Node],
        ts_files: list[FileRecord],
    ) -> None:
        """Rescore affected buckets and fold the results into *payload*."""

        current_profiles = payload.pop("similarity_profiles", [])
        refreshed_buckets = {
            *incremental_similarity_buckets,
            *(
                str(profile.get("profile", {}).get("bucket"))
                for profile in current_profiles
                if isinstance(profile, dict)
                and isinstance(profile.get("profile"), dict)
                and profile["profile"].get("bucket") is not None
            ),
        }
        restored_profiles = [
            row
            for row in similarity_profiles
            if row["profile"].get("bucket") in refreshed_buckets
        ]
        if not refreshed_buckets:
            return
        valid_target_ids = {
            node.id for node in context_nodes if isinstance(node.id, str)
        }
        valid_target_ids.update(
            str(row.get("id"))
            for row in payload.get("nodes", [])
            if isinstance(row, dict) and row.get("id")
        )
        typescript_module_names = {file.module for file in ts_files if file.module}
        typescript_module_names.update(
            node.qualname
            for node in context_nodes
            if node.kind == "module"
            and node.properties.get("frontend_name") == TYPESCRIPT_FRONTEND_NAME
            and node.path is not None
            and node.path.endswith(TYPESCRIPT_SOURCE_EXTENSIONS)
            and node.qualname is not None
        )
        scored = self._run_similarity_scorer(
            [*restored_profiles, *current_profiles],
            valid_target_ids=valid_target_ids,
            module_names=typescript_module_names,
        )
        profile_updates = {
            str(row.get("id")): row.get("profile")
            for row in scored.get("profiles", [])
            if isinstance(row, dict)
            and row.get("id")
            and isinstance(row.get("profile"), dict)
        }
        for node in context_nodes:
            updated_profile = profile_updates.get(node.id)
            if updated_profile is not None:
                node.properties["similarity"] = updated_profile
        payload.setdefault("edges", []).extend(scored.get("edges", []))
        payload.setdefault("warnings", []).extend(scored.get("warnings", []))

    def _run_extractor(
        self,
        files: list[FileRecord],
        call_context_nodes: list[Node],
        *,
        similarity_profiles: list[dict[str, Any]] | None = None,
        defer_similarity_scoring: bool = False,
    ) -> dict[str, Any]:
        script_path = Path(__file__).with_name("typescript_extractor.mjs")
        request: dict[str, Any] = {
            "repoRoot": str(self.repo_root),
            "files": [
                {
                    "path": file.path,
                    "absPath": file.abs_path,
                    "module": file.module,
                    "lineCount": file.line_count,
                    "sourceRoot": file.source_root,
                }
                for file in files
            ],
            "similarityProfiles": (
                self._similarity_profiles(call_context_nodes)
                if similarity_profiles is None
                else similarity_profiles
            ),
            "deferSimilarityScoring": defer_similarity_scoring,
        }
        context = {
            "moduleContext": self._module_context(call_context_nodes),
            "routes": self._route_context(call_context_nodes),
            "declarationContext": self._declaration_context(call_context_nodes),
        }
        if defer_similarity_scoring and any(context.values()):
            encoded_context = json.dumps(
                context,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            request.update(
                {
                    "contextEncoding": "zlib-base64-json-v1",
                    "contextBundle": base64.b64encode(
                        zlib.compress(encoded_context, level=1)
                    ).decode("ascii"),
                }
            )
        else:
            request.update(context)
        return self._run_node_script(
            script_path, request, tool_name="TypeScript extractor"
        )

    def _run_similarity_scorer(
        self,
        profiles: list[dict[str, Any]],
        *,
        valid_target_ids: set[str],
        module_names: set[str],
    ) -> dict[str, Any]:
        script_path = (
            Path(__file__).with_name("typescript_extractor") / "similarity_runner.mjs"
        )
        return self._run_node_script(
            script_path,
            {
                "profiles": profiles,
                "validTargetIds": sorted(valid_target_ids),
                "moduleNames": sorted(module_names),
            },
            tool_name="TypeScript similarity scorer",
        )

    def _run_node_script(
        self,
        script_path: Path,
        request: dict[str, Any],
        *,
        tool_name: str,
    ) -> dict[str, Any]:
        node = shutil.which("node")
        if node is None:
            raise RuntimeError("Node.js is required for TypeScript analysis.")
        if not script_path.exists():
            raise RuntimeError(f"{tool_name} not found: {script_path}")
        result = subprocess.run(
            [node, str(script_path)],
            input=json.dumps(request),
            text=True,
            capture_output=True,
            cwd=str(self.repo_root),
            timeout=self.timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                f"{tool_name} failed with exit code {result.returncode}: {detail}"
            )
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{tool_name} returned invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(f"{tool_name} returned a non-object payload.")
        return parsed

    @staticmethod
    def _similarity_profiles(nodes: list[Node]) -> list[dict[str, Any]]:
        profiles: list[dict[str, Any]] = []
        for node in nodes:
            if node.kind not in {"function", "method"}:
                continue
            if node.properties.get("frontend_name") != TYPESCRIPT_FRONTEND_NAME:
                continue
            profile = node.properties.get("similarity")
            if not isinstance(profile, dict):
                continue
            profiles.append(
                {
                    "id": node.id,
                    "path": node.path,
                    "start_line": node.start_line,
                    "end_line": node.end_line,
                    "profile": profile,
                }
            )
        return profiles

    @staticmethod
    def _route_context(nodes: list[Node]) -> list[dict[str, str]]:
        routes: list[dict[str, str]] = []
        for node in nodes:
            if node.kind != "route" or not node.id.startswith("route:"):
                continue
            _, method, path = node.id.split(":", 2)
            routes.append({"id": node.id, "method": method, "path": path})
        return routes

    def _module_context(self, nodes: list[Node]) -> list[dict[str, Any]]:
        return [
            {
                "path": node.path,
                "absPath": str((self.repo_root / node.path).resolve()),
                "module": node.qualname,
                "lineCount": node.properties.get("line_count", 0),
                "sourceRoot": node.properties.get("source_root", "."),
                "isPackage": node.properties.get("is_package", False),
            }
            for node in nodes
            if node.kind == "module"
            and node.properties.get("frontend_name") == TYPESCRIPT_FRONTEND_NAME
            and node.path is not None
            and node.path.endswith(TYPESCRIPT_SOURCE_EXTENSIONS)
            and node.qualname is not None
        ]

    @staticmethod
    def _declaration_context(nodes: list[Node]) -> list[dict[str, Any]]:
        declaration_kinds = {
            "function",
            "component",
            "method",
            "class",
            "interface",
            "type_alias",
            "enum",
        }
        modules_by_path = {
            node.path: node.qualname
            for node in nodes
            if node.kind == "module"
            and node.properties.get("frontend_name") == TYPESCRIPT_FRONTEND_NAME
            and node.path is not None
            and node.path.endswith(TYPESCRIPT_SOURCE_EXTENSIONS)
            and node.qualname is not None
        }
        return [
            {
                "id": node.id,
                "name": node.name,
                # Methods belong to their class scope. Keep them in the
                # declaration-location context for checker-backed resolution,
                # but never flatten their names into the module symbol table.
                "local_names": (
                    []
                    if node.kind == "method"
                    else [
                        node.name,
                        *(
                            ["default"]
                            if node.properties.get("default_export") is True
                            else []
                        ),
                    ]
                ),
                "module": modules_by_path.get(node.path),
                "path": node.path,
                "start_line": node.start_line,
                "end_line": node.end_line,
            }
            for node in nodes
            if node.kind in declaration_kinds
            and node.properties.get("frontend_name") == TYPESCRIPT_FRONTEND_NAME
            and node.path is not None
            and node.path in modules_by_path
            and node.start_line is not None
            and node.end_line is not None
        ]

    @staticmethod
    def _module_nodes(files: list[FileRecord]) -> list[Node]:
        nodes: list[Node] = []
        for file_record in files:
            nodes.append(
                Node(
                    id=module_id(file_record.module),
                    kind="module",
                    name=(
                        logical_module_name(file_record.module).rsplit(".", 1)[-1]
                        or logical_module_name(file_record.module)
                    ),
                    qualname=file_record.module,
                    path=file_record.path,
                    properties={
                        "line_count": file_record.line_count,
                        "display_module": logical_module_name(file_record.module),
                        "source_root": file_record.source_root,
                        "is_package": file_record.is_package,
                        "language": "typescript",
                        "frontend_name": TYPESCRIPT_FRONTEND_NAME,
                        "frontend_version": TYPESCRIPT_FRONTEND_VERSION,
                    },
                )
            )
        return nodes

    @staticmethod
    def _nodes_from_payload(rows: list[Any]) -> list[Node]:
        nodes: list[Node] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            properties = dict(row.get("properties") or {})
            properties.setdefault("language", "typescript")
            properties.setdefault("frontend_name", TYPESCRIPT_FRONTEND_NAME)
            properties.setdefault("frontend_version", TYPESCRIPT_FRONTEND_VERSION)
            nodes.append(
                Node(
                    id=str(row["id"]),
                    kind=str(row["kind"]),
                    name=str(row.get("name") or row["id"]),
                    qualname=row.get("qualname"),
                    path=row.get("path"),
                    start_line=row.get("start_line"),
                    end_line=row.get("end_line"),
                    properties=properties,
                )
            )
        return nodes

    @staticmethod
    def _edges_from_payload(rows: list[Any]) -> list[Edge]:
        edges: list[Edge] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            evidence = [
                Evidence(**item)
                for item in row.get("evidence", [])
                if isinstance(item, dict)
            ]
            properties = dict(row.get("properties") or {})
            properties.setdefault("language", "typescript")
            properties.setdefault("frontend_name", TYPESCRIPT_FRONTEND_NAME)
            properties.setdefault("frontend_version", TYPESCRIPT_FRONTEND_VERSION)
            resolution = row.get("resolution")
            confidence = row.get("confidence", "heuristic")
            edges.append(
                Edge(
                    source=str(row["source"]),
                    target=str(row["target"]),
                    kind=str(row["kind"]),
                    confidence=confidence,
                    semantic_role=row.get("semantic_role"),
                    resolution=(
                        FactResolution.model_validate(resolution)
                        if isinstance(resolution, dict)
                        else (
                            FactResolution(
                                status="unresolved",
                                strategy="typescript_module_resolution",
                                candidate_count=0,
                                detail="TypeScript extractor did not resolve the target",
                            )
                            if confidence == "unresolved"
                            else FactResolution()
                        )
                    ),
                    evidence=evidence,
                    properties=properties,
                )
            )
        return edges

    @staticmethod
    def _unresolved_reference_nodes(edges: list[Edge]) -> list[Node]:
        specifiers = {
            edge.target.removeprefix("unresolved:")
            for edge in edges
            if edge.target.startswith("unresolved:")
        }
        return [
            Node(
                id=f"unresolved:{specifier}",
                kind="diagnostic",
                name=specifier,
                qualname=specifier,
                properties={
                    "identity_profile": "typescript_unresolved_reference_v1",
                    "specifier": specifier,
                    "language": "typescript",
                    "frontend_name": TYPESCRIPT_FRONTEND_NAME,
                    "frontend_version": TYPESCRIPT_FRONTEND_VERSION,
                    "resolution_status": "unresolved",
                },
            )
            for specifier in sorted(specifiers)
        ]

    @staticmethod
    def _warnings_from_payload(rows: list[Any]) -> list[BuildWarning]:
        warnings: list[BuildWarning] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            warnings.append(
                BuildWarning(
                    kind=str(row.get("kind") or "typescript_warning"),
                    message=str(row.get("message") or ""),
                    path=row.get("path"),
                    frontend_name=TYPESCRIPT_FRONTEND_NAME,
                )
            )
        return warnings

    @staticmethod
    def _external_package_nodes(packages: list[Any]) -> list[Node]:
        return [
            Node(
                id=external_package_id(str(package)),
                kind="external_package",
                name=str(package),
                qualname=str(package),
                properties={
                    "package": str(package),
                    "language": "typescript",
                    "frontend_name": TYPESCRIPT_FRONTEND_NAME,
                    "frontend_version": TYPESCRIPT_FRONTEND_VERSION,
                },
            )
            for package in sorted({str(package) for package in packages if package})
        ]

    @staticmethod
    def _external_symbol_nodes(symbols: list[Any]) -> list[Node]:
        nodes: list[Node] = []
        for symbol in sorted({str(symbol) for symbol in symbols if symbol}):
            if not symbol.startswith("extsym:"):
                continue
            name = symbol.removeprefix("extsym:")
            nodes.append(
                Node(
                    id=symbol,
                    kind="external_symbol",
                    name=name.rsplit(".", 1)[-1],
                    qualname=name,
                    properties={
                        "language": "typescript",
                        "frontend_name": TYPESCRIPT_FRONTEND_NAME,
                        "frontend_version": TYPESCRIPT_FRONTEND_VERSION,
                    },
                )
            )
        return nodes
