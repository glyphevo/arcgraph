"""Evidence-bounded protected-surface detection capability matrix."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from arcgraph.change.contracts import ProtectedSurface, canonical_digest
from arcgraph.change.errors import RepositoryPathError
from arcgraph.change.paths import NormalizedRepositoryPath, normalize_repository_path
from arcgraph.core.schemas import Node
from arcgraph.pipeline.openapi_frontend import _load_yaml

_KIND_TO_SURFACE: dict[str, str] = {
    "route": "route",
    "endpoint": "route",
    "api_route": "route",
    "openapi_operation": "openapi",
    "openapi_route": "openapi",
    "table": "table",
    "database_table": "table",
    "queue": "queue",
    "worker": "queue",
    "cli_command": "cli",
    "command": "cli",
    "mcp_tool": "mcp",
    "typescript_export": "typescript_export",
    "export": "typescript_export",
    "configuration": "configuration",
    "config": "configuration",
    "framework_registration": "framework_registration",
    "database_migration": "database_migration",
    "migration": "database_migration",
    "security_boundary": "security_boundary",
    "auth": "security_boundary",
    "event_contract": "event_contract",
    "event": "event_contract",
    "external_integration": "external_integration",
    "external_service": "external_integration",
}

_VALID_SURFACE_KINDS = frozenset(
    {
        "route",
        "openapi",
        "table",
        "queue",
        "cli",
        "mcp",
        "typescript_export",
        "python_public_api",
        "configuration",
        "framework_registration",
        "database_migration",
        "security_boundary",
        "event_contract",
        "external_integration",
    }
)


class ProtectedSurfaceDetector:
    """Detect only surfaces supported by an explicit graph or user declaration.

    A missing capability never turns into a confirmed surface.  Dynamic
    registration is represented as ``unknown`` so a planner/verifier can keep
    a human review boundary instead of inventing a static guarantee.
    """

    def __init__(self, repo_root: Path, *, repo_id: str) -> None:
        self.repo_root = repo_root.resolve()
        self.repo_id = repo_id

    def detect(
        self,
        nodes: Iterable[Node],
        *,
        user_declarations: Iterable[str] = (),
        openapi_input: str | Path | None = None,
    ) -> list[ProtectedSurface]:
        surfaces: list[ProtectedSurface] = []
        for node in nodes:
            for surface_kind in self._surface_kinds_for_node(node):
                surfaces.append(self._from_node(surface_kind, node))
        surfaces.extend(self._user_declared_surfaces(user_declarations))
        if openapi_input is not None:
            surfaces.append(self._openapi_input_surface(openapi_input))
        return _dedupe_surfaces(surfaces)

    def capability_matrix(
        self, surfaces: Iterable[ProtectedSurface]
    ) -> dict[str, dict[str, list[str]]]:
        """Return the full, evidence-bounded matrix without collapsing Unknown."""

        matrix = {
            surface_kind: {
                "capabilities": ["unavailable"],
                "detection_statuses": ["unknown"],
                "surface_ids": [],
            }
            for surface_kind in _VALID_SURFACE_KINDS
        }
        for surface in surfaces:
            entry = matrix[surface.surface_kind]
            if entry["surface_ids"] == []:
                entry["capabilities"] = []
                entry["detection_statuses"] = []
            entry["capabilities"].append(surface.capability)
            entry["detection_statuses"].append(surface.detection_status)
            entry["surface_ids"].append(surface.surface_id)
        for entry in matrix.values():
            entry["capabilities"] = sorted(set(entry["capabilities"]))
            entry["detection_statuses"] = sorted(set(entry["detection_statuses"]))
            entry["surface_ids"] = sorted(set(entry["surface_ids"]))
        return dict(sorted(matrix.items()))

    def _surface_kinds_for_node(self, node: Node) -> set[str]:
        values: set[str] = set()
        kind = node.kind.casefold()
        if kind in _KIND_TO_SURFACE:
            values.add(_KIND_TO_SURFACE[kind])
        # Arbitrary node properties are payload data, not provenance.  Only a
        # typed graph kind or an explicit user/protocol input may establish a
        # protected-surface capability.
        # Python public APIs require explicit export/public metadata.  A public
        # looking function name alone is intentionally insufficient evidence.
        if node.kind in {"function", "class"} and (
            node.properties.get("public_api") is True
            or node.properties.get("exported") is True
        ):
            values.add("python_public_api")
        return values

    def _from_node(
        self,
        surface_kind: str,
        node: Node,
    ) -> ProtectedSurface:
        normalized_path = (
            normalize_repository_path(node.path, self.repo_root) if node.path else None
        )
        evidence = {
            "source": f"graph:{node.kind}",
            "node_id": node.id,
            "kind": node.kind,
        }
        surface_id = _surface_id(
            surface_kind,
            node.id,
            normalized_path.display_path if normalized_path else None,
        )
        capability, confidence, detection_status, detection_source = (
            _node_detection_profile(surface_kind, node)
        )
        return ProtectedSurface(
            repo_id=self.repo_id,
            surface_id=surface_id,
            surface_kind=surface_kind,  # type: ignore[arg-type]
            path=normalized_path.display_path if normalized_path else None,
            path_comparison_key=(
                normalized_path.comparison_key if normalized_path else None
            ),
            symbol_id=node.id,
            detection_source=detection_source or f"graph:{node.kind}",
            capability=capability,  # type: ignore[arg-type]
            confidence=confidence,  # type: ignore[arg-type]
            detection_status=detection_status,  # type: ignore[arg-type]
            evidence=[evidence],
        )

    def _user_declared_surfaces(
        self,
        declarations: Iterable[str],
    ) -> list[ProtectedSurface]:
        result: list[ProtectedSurface] = []
        for declaration in sorted(
            {item.strip() for item in declarations if item.strip()}
        ):
            path = None
            path_key = None
            if "/" in declaration or "\\" in declaration or declaration.endswith(".py"):
                try:
                    normalized = normalize_repository_path(declaration, self.repo_root)
                except RepositoryPathError:
                    normalized = None
                if normalized is not None:
                    path = normalized.display_path
                    path_key = normalized.comparison_key
            surface_kind = _declared_surface_kind(declaration)
            result.append(
                ProtectedSurface(
                    repo_id=self.repo_id,
                    surface_id=_surface_id(surface_kind, declaration, path),
                    surface_kind=surface_kind,  # type: ignore[arg-type]
                    path=path,
                    path_comparison_key=path_key,
                    detection_source="user_declaration",
                    capability="user_declaration_required",
                    confidence="confirmed",
                    detection_status="declared",
                    evidence=[{"source": "user_declaration", "value": declaration}],
                )
            )
        return result

    def _openapi_input_surface(
        self,
        openapi_input: str | Path,
    ) -> ProtectedSurface:
        normalized, valid = _explicit_openapi_input(self.repo_root, openapi_input)
        return ProtectedSurface(
            repo_id=self.repo_id,
            surface_id=_surface_id(
                "openapi",
                normalized.display_path if normalized else str(openapi_input),
                None,
            ),
            surface_kind="openapi",
            path=normalized.display_path if normalized else None,
            path_comparison_key=normalized.comparison_key if normalized else None,
            detection_source="explicit_openapi_input",
            capability="explicit_protocol_input" if valid else "unavailable",
            confidence="confirmed" if valid else "unresolved",
            detection_status="confirmed" if valid else "unknown",
            evidence=[
                {
                    "source": "explicit_openapi_input",
                    "valid_openapi_3": valid,
                }
            ],
        )


def _surface_id(surface_kind: str, subject: str, path: str | None) -> str:
    return f"surface-{surface_kind}-{canonical_digest({'subject': subject, 'path': path})[:16]}"


def _node_detection_profile(
    surface_kind: str,
    node: Node,
) -> tuple[str, str, str, str]:
    """Return only the capability warranted by the observed static fact."""

    if node.properties.get("dynamic") is True:
        return (
            "partial_or_heuristic",
            "unresolved",
            "unknown",
            f"graph:dynamic_{node.kind}",
        )
    if surface_kind == "typescript_export":
        return (
            "reliable_graph_fact",
            "confirmed",
            "confirmed",
            "graph:typescript_static_export",
        )
    if surface_kind == "python_public_api":
        return (
            "user_declaration_required",
            "inferred",
            "candidate",
            "graph:python_export_hint",
        )
    if surface_kind == "database_migration":
        return (
            "unavailable",
            "unresolved",
            "unknown",
            "graph:non_application_migration",
        )
    return (
        "partial_or_heuristic",
        "inferred",
        "candidate",
        f"graph:{node.kind}",
    )


def _declared_surface_kind(declaration: str) -> str:
    if declaration in _VALID_SURFACE_KINDS:
        return declaration
    prefix, separator, _value = declaration.partition(":")
    if separator and prefix in _VALID_SURFACE_KINDS:
        return prefix
    return "security_boundary"


def _explicit_openapi_input(
    repo_root: Path,
    openapi_input: str | Path,
) -> tuple[NormalizedRepositoryPath | None, bool]:
    try:
        normalized = normalize_repository_path(openapi_input, repo_root)
    except RepositoryPathError:
        return None, False
    path = repo_root / Path(*normalized.display_path.split("/"))
    if path.is_symlink() or not path.is_file():
        return normalized, False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return normalized, False
    try:
        payload = (
            json.loads(text) if path.suffix.casefold() == ".json" else _load_yaml(text)
        )
    except Exception:
        # Parser implementations expose different exception hierarchies
        # (for example PyYAML's YAMLError is not a ValueError).  This boundary
        # consumes only an explicit protocol input, so any parser failure must
        # degrade to Unknown rather than abort planning.
        return normalized, False
    if not isinstance(payload, dict):
        return normalized, False
    version = payload.get("openapi")
    return (
        normalized,
        isinstance(version, str)
        and version.startswith("3.")
        and isinstance(payload.get("paths"), dict),
    )


def _dedupe_surfaces(surfaces: Iterable[ProtectedSurface]) -> list[ProtectedSurface]:
    unique = {surface.surface_id: surface for surface in surfaces}
    return [unique[key] for key in sorted(unique)]
