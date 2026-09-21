"""OpenAPI protocol graph ingestion frontend."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from arcgraph.core.ids import route_id
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

OPENAPI_PROTOCOL_FRONTEND_NAME = "openapi-protocol"
OPENAPI_PROTOCOL_FRONTEND_VERSION = "0.1.0"

_HTTP_METHODS = {
    "delete",
    "get",
    "head",
    "options",
    "patch",
    "post",
    "put",
    "trace",
}


class OpenApiProtocolFrontend(LanguageFrontend):
    """Build graph facts from an explicitly supplied OpenAPI 3.x artifact.

    The frontend is artifact-only and is never enabled by default scanner
    discovery. It emits confirmed protocol facts from the spec and only emits
    inferred handler edges when a unique indexed function or method matches an
    explicit ``operationId``.
    """

    name = OPENAPI_PROTOCOL_FRONTEND_NAME
    language_ids = ("openapi",)
    version = OPENAPI_PROTOCOL_FRONTEND_VERSION
    artifact_only = True

    def __init__(self, repo_root: Path, openapi_path: str | Path) -> None:
        self.repo_root = repo_root.resolve()
        self.openapi_path = self._absolute_path(openapi_path)

    @property
    def file_extensions(self) -> tuple[str, ...]:
        return ()

    def accepts(self, file: FileRecord) -> bool:
        del file
        return False

    def capabilities(self) -> FrontendCapabilities:
        return FrontendCapabilities(
            name=self.name,
            version=self.version,
            language="openapi",
            capabilities={
                "mode": "openapi-3-protocol",
                "fact_kinds": "node,edge,diagnostic",
                "confidence": "confirmed-and-inferred",
                "incremental": "artifact",
                **language_tier_capabilities(
                    tier="L2",
                    source="openapi",
                    scope=(
                        "OpenAPI protocol route/schema facts; handler matches "
                        "are inferred from unique operationId evidence."
                    ),
                    languages=("openapi",),
                ),
            },
            file_extensions=[],
        )

    def detect(self, request: object) -> bool:
        return True

    def analyze(self, request: object) -> object:
        raise RuntimeError(
            "OpenApiProtocolFrontend exposes graph fragments via analyze_to_graph()."
        )

    def analyze_to_graph(
        self,
        files: list[FileRecord],
        warnings: list[BuildWarning] | None = None,
        module_names: set[str] | None = None,
        call_context_nodes: list[Node] | None = None,
    ) -> FrontendGraphFragment:
        del module_names
        started = time.monotonic()
        warnings = list(warnings or [])
        metrics = _OpenApiMetrics()
        payload = self._load_payload(warnings, metrics)
        if payload is None:
            return FrontendGraphFragment(
                warnings=warnings,
                adapter_metrics={
                    **metrics.as_dict(),
                    "status": "unavailable",
                    "artifact_path": self._display_path(self.openapi_path),
                    "scanned_files": len(files),
                },
                phase_timings={"openapi_protocol": time.monotonic() - started},
            )

        builder = _OpenApiGraphBuilder(
            repo_root=self.repo_root,
            artifact_path=self._display_path(self.openapi_path),
            call_context_nodes=call_context_nodes or [],
            metrics=metrics,
        )
        fragment = builder.build(payload)
        warnings.extend(fragment.warnings)
        return FrontendGraphFragment(
            nodes=fragment.nodes,
            edges=fragment.edges,
            warnings=warnings,
            adapter_metrics={
                **builder.metrics.as_dict(),
                "status": "available",
                "artifact_path": self._display_path(self.openapi_path),
                "scanned_files": len(files),
            },
            phase_timings={"openapi_protocol": time.monotonic() - started},
        )

    def _absolute_path(self, raw_path: str | Path) -> Path:
        path = Path(raw_path)
        if not path.is_absolute():
            path = self.repo_root / path
        return path.resolve()

    def _load_payload(
        self, warnings: list[BuildWarning], metrics: "_OpenApiMetrics"
    ) -> dict[str, Any] | None:
        display_path = self._display_path(self.openapi_path)
        if not self._is_inside_repo(self.openapi_path):
            metrics.path_outside_repo += 1
            warnings.append(
                BuildWarning(
                    kind="openapi_protocol_path_outside_repo",
                    message=(
                        "OpenAPI protocol input is outside the repository and "
                        f"was not read: {display_path}"
                    ),
                    path=display_path,
                )
            )
            return None
        if not self.openapi_path.exists():
            warnings.append(
                BuildWarning(
                    kind="openapi_protocol_missing",
                    message=f"OpenAPI protocol input does not exist: {display_path}",
                    path=display_path,
                )
            )
            return None
        try:
            text = self.openapi_path.read_text(encoding="utf-8")
        except OSError as exc:
            warnings.append(
                BuildWarning(
                    kind="openapi_protocol_read_error",
                    message=f"OpenAPI protocol input could not be read: {exc}",
                    path=display_path,
                )
            )
            return None

        try:
            payload = self._parse_payload(text)
        except (ValueError, json.JSONDecodeError) as exc:
            warnings.append(
                BuildWarning(
                    kind="openapi_protocol_parse_error",
                    message=f"OpenAPI protocol input is not valid JSON/YAML: {exc}",
                    path=display_path,
                )
            )
            return None
        if not isinstance(payload, dict):
            warnings.append(
                BuildWarning(
                    kind="openapi_protocol_parse_error",
                    message="OpenAPI protocol input must be an object.",
                    path=display_path,
                )
            )
            return None
        return payload

    def _parse_payload(self, text: str) -> Any:
        suffix = self.openapi_path.suffix.lower()
        if suffix in {".yaml", ".yml"}:
            return _load_yaml(text)
        return json.loads(text)

    def _is_inside_repo(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.repo_root)
        except (OSError, ValueError):
            return False
        return True

    def _display_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.repo_root).as_posix()
        except ValueError:
            return str(path)


class _OpenApiGraphBuilder:
    def __init__(
        self,
        *,
        repo_root: Path,
        artifact_path: str,
        call_context_nodes: list[Node],
        metrics: "_OpenApiMetrics",
    ) -> None:
        self.repo_root = repo_root
        self.artifact_path = artifact_path
        self.call_context_nodes = call_context_nodes
        self.metrics = metrics
        self.nodes: list[Node] = []
        self.edges: list[Edge] = []
        self.warnings: list[BuildWarning] = []

    def build(self, payload: dict[str, Any]) -> FrontendGraphFragment:
        if not self._valid_spec(payload):
            self.metrics.warnings = len(self.warnings)
            return FrontendGraphFragment(warnings=self.warnings)

        schemas = self._schema_names(payload)
        for schema_name in schemas:
            self.nodes.append(self._schema_node(schema_name))

        operations = self._operations(payload)
        self._warn_duplicate_operation_ids(operations)
        for operation in operations:
            self._add_operation(operation, schemas)

        nodes = EvidenceMergeEngine.dedupe_nodes(self.nodes)
        edges = EvidenceMergeEngine.dedupe_edges(self.edges)
        self.metrics.nodes_emitted = len(nodes)
        self.metrics.edges_emitted = len(edges)
        self.metrics.warnings = len(self.warnings)
        return FrontendGraphFragment(nodes=nodes, edges=edges, warnings=self.warnings)

    def _valid_spec(self, payload: dict[str, Any]) -> bool:
        openapi_version = payload.get("openapi")
        if not isinstance(openapi_version, str) or not openapi_version.startswith("3."):
            self.metrics.invalid_specs += 1
            self.warnings.append(
                BuildWarning(
                    kind="openapi_protocol_invalid_spec",
                    message="OpenAPI protocol input must declare an OpenAPI 3.x version.",
                    path=self.artifact_path,
                )
            )
            return False
        if not isinstance(payload.get("paths"), dict):
            self.metrics.invalid_specs += 1
            self.warnings.append(
                BuildWarning(
                    kind="openapi_protocol_invalid_spec",
                    message="OpenAPI protocol input field 'paths' must be an object.",
                    path=self.artifact_path,
                )
            )
            return False
        return True

    def _schema_names(self, payload: dict[str, Any]) -> set[str]:
        components = payload.get("components", {})
        if not isinstance(components, dict):
            return set()
        schemas = components.get("schemas", {})
        if not isinstance(schemas, dict):
            return set()
        names = {
            str(name) for name, value in schemas.items() if isinstance(value, dict)
        }
        self.metrics.schemas_total = len(names)
        return names

    def _operations(self, payload: dict[str, Any]) -> list["_OpenApiOperation"]:
        paths = payload.get("paths", {})
        operations: list[_OpenApiOperation] = []
        for raw_path, path_item in sorted(paths.items()):
            if not isinstance(raw_path, str) or not raw_path.startswith("/"):
                self.metrics.invalid_specs += 1
                self.warnings.append(
                    BuildWarning(
                        kind="openapi_protocol_invalid_path",
                        message=f"OpenAPI path key must start with '/': {raw_path!r}",
                        path=self.artifact_path,
                    )
                )
                continue
            if not isinstance(path_item, dict):
                self.metrics.invalid_specs += 1
                self.warnings.append(
                    BuildWarning(
                        kind="openapi_protocol_invalid_path",
                        message=f"OpenAPI path item must be an object: {raw_path}",
                        path=self.artifact_path,
                    )
                )
                continue
            for method, operation_payload in sorted(path_item.items()):
                method_lower = str(method).lower()
                if method_lower not in _HTTP_METHODS:
                    continue
                if not isinstance(operation_payload, dict):
                    self.metrics.invalid_specs += 1
                    self.warnings.append(
                        BuildWarning(
                            kind="openapi_protocol_invalid_operation",
                            message=(
                                "OpenAPI operation must be an object: "
                                f"{method_upper(method_lower)} {raw_path}"
                            ),
                            path=self.artifact_path,
                        )
                    )
                    continue
                operation_id = operation_payload.get("operationId")
                operations.append(
                    _OpenApiOperation(
                        method=method_upper(method_lower),
                        path=raw_path,
                        operation_id=(
                            operation_id if isinstance(operation_id, str) else None
                        ),
                        payload=operation_payload,
                    )
                )
        self.metrics.operations_total = len(operations)
        return operations

    def _warn_duplicate_operation_ids(
        self, operations: list["_OpenApiOperation"]
    ) -> None:
        seen: set[str] = set()
        duplicated: set[str] = set()
        for operation in operations:
            if not operation.operation_id:
                continue
            if operation.operation_id in seen:
                duplicated.add(operation.operation_id)
            seen.add(operation.operation_id)
        for operation_id in sorted(duplicated):
            self.metrics.duplicate_operation_ids += 1
            self.warnings.append(
                BuildWarning(
                    kind="openapi_protocol_duplicate_operation_id",
                    message=f"OpenAPI operationId is duplicated: {operation_id}",
                    path=self.artifact_path,
                )
            )

    def _add_operation(
        self, operation: "_OpenApiOperation", schema_names: set[str]
    ) -> None:
        operation_node_id = _operation_node_id(operation.method, operation.path)
        route_node_id = route_id(operation.method, operation.path)
        self.metrics.routes_total += 1
        self.nodes.append(self._operation_node(operation, operation_node_id))
        self.nodes.append(self._route_node(operation, route_node_id))
        self.edges.append(
            Edge(
                source=operation_node_id,
                target=route_node_id,
                kind="defines",
                confidence="confirmed",
                semantic_role="openapi_route",
                resolution=FactResolution(strategy="openapi_protocol_operation"),
                confidence_sources={"confirmed": ["openapi_protocol"]},
                evidence=[self._operation_evidence(operation)],
                properties=_frontend_properties(operation),
            )
        )
        for schema_name in sorted(_schema_refs(operation.payload) & schema_names):
            self.edges.append(
                Edge(
                    source=operation_node_id,
                    target=_schema_node_id(schema_name),
                    kind="references",
                    confidence="confirmed",
                    semantic_role="openapi_schema",
                    resolution=FactResolution(
                        strategy="openapi_protocol_schema_reference"
                    ),
                    confidence_sources={"confirmed": ["openapi_protocol"]},
                    evidence=[self._operation_evidence(operation, detail=schema_name)],
                    properties=_frontend_properties(operation),
                )
            )

        self._add_handler_match(operation, route_node_id)

    def _operation_node(
        self, operation: "_OpenApiOperation", operation_node_id: str
    ) -> Node:
        return Node(
            id=operation_node_id,
            kind="operation",
            name=operation.operation_id or f"{operation.method} {operation.path}",
            qualname=f"{operation.method} {operation.path}",
            path=self.artifact_path,
            properties={
                **_frontend_properties(operation),
                "operation_id": operation.operation_id,
            },
        )

    def _route_node(self, operation: "_OpenApiOperation", route_node_id: str) -> Node:
        return Node(
            id=route_node_id,
            kind="route",
            name=f"{operation.method} {operation.path}",
            qualname=f"{operation.method} {operation.path}",
            path=self.artifact_path,
            properties={
                **_frontend_properties(operation),
                "method": operation.method,
                "path": operation.path,
                "operation_id": operation.operation_id,
                "protocol": "openapi",
            },
        )

    def _schema_node(self, schema_name: str) -> Node:
        return Node(
            id=_schema_node_id(schema_name),
            kind="schema",
            name=schema_name,
            qualname=f"openapi.schema.{schema_name}",
            path=self.artifact_path,
            properties={
                "schema_name": schema_name,
                "schema_kind": "openapi_component_schema",
                "frontend_name": OPENAPI_PROTOCOL_FRONTEND_NAME,
                "frontend_version": OPENAPI_PROTOCOL_FRONTEND_VERSION,
                "language": "openapi",
                "protocol": "openapi",
            },
        )

    def _add_handler_match(
        self, operation: "_OpenApiOperation", route_node_id: str
    ) -> None:
        if not operation.operation_id:
            self._warn_unmatched(operation, "operation has no operationId")
            return
        matches = self._handler_matches(operation.operation_id)
        if len(matches) == 1:
            target = matches[0]
            self.metrics.matched_handlers += 1
            self.edges.append(
                Edge(
                    source=route_node_id,
                    target=target.id,
                    kind="invokes",
                    confidence="inferred",
                    semantic_role="openapi_operation_id",
                    resolution=FactResolution(
                        strategy="openapi_operation_id_handler_match",
                        candidate_count=1,
                    ),
                    confidence_sources={"inferred": ["openapi_operation_id"]},
                    evidence=[
                        self._operation_evidence(
                            operation,
                            detail=(
                                f"{operation.operation_id} -> "
                                f"{target.qualname or target.id}"
                            ),
                        )
                    ],
                    properties=_frontend_properties(operation),
                )
            )
            return
        if len(matches) > 1:
            self.metrics.ambiguous_operation_matches += 1
            self.warnings.append(
                BuildWarning(
                    kind="openapi_protocol_operation_handler_ambiguous",
                    message=(
                        "OpenAPI operationId matched multiple indexed handlers: "
                        f"{operation.operation_id}"
                    ),
                    path=self.artifact_path,
                )
            )
            return
        self._warn_unmatched(operation, "no indexed handler matched operationId")

    def _handler_matches(self, operation_id: str) -> list[Node]:
        matches: dict[str, Node] = {}
        for node in self.call_context_nodes:
            if node.kind not in {"function", "method"}:
                continue
            qualname = node.qualname or ""
            if (
                node.id == operation_id
                or node.name == operation_id
                or qualname == operation_id
                or qualname.endswith(f".{operation_id}")
            ):
                matches[node.id] = node
        return [matches[key] for key in sorted(matches)]

    def _warn_unmatched(self, operation: "_OpenApiOperation", reason: str) -> None:
        self.metrics.unmatched_operations += 1
        self.warnings.append(
            BuildWarning(
                kind="openapi_protocol_operation_unmatched",
                message=(
                    "OpenAPI operation has no deterministic indexed handler match "
                    f"({reason}): {operation.method} {operation.path}"
                ),
                path=self.artifact_path,
            )
        )

    def _operation_evidence(
        self, operation: "_OpenApiOperation", *, detail: str | None = None
    ) -> Evidence:
        return Evidence(
            kind="openapi_protocol_operation",
            path=self.artifact_path,
            detail=detail or f"{operation.method} {operation.path}",
        )


class _OpenApiMetrics:
    def __init__(self) -> None:
        self.operations_total = 0
        self.routes_total = 0
        self.schemas_total = 0
        self.matched_handlers = 0
        self.unmatched_operations = 0
        self.ambiguous_operation_matches = 0
        self.duplicate_operation_ids = 0
        self.invalid_specs = 0
        self.path_outside_repo = 0
        self.nodes_emitted = 0
        self.edges_emitted = 0
        self.warnings = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "operations_total": self.operations_total,
            "routes_total": self.routes_total,
            "schemas_total": self.schemas_total,
            "matched_handlers": self.matched_handlers,
            "unmatched_operations": self.unmatched_operations,
            "ambiguous_operation_matches": self.ambiguous_operation_matches,
            "duplicate_operation_ids": self.duplicate_operation_ids,
            "invalid_specs": self.invalid_specs,
            "path_outside_repo": self.path_outside_repo,
            "nodes_emitted": self.nodes_emitted,
            "edges_emitted": self.edges_emitted,
            "warnings": self.warnings,
        }


class _OpenApiOperation:
    def __init__(
        self,
        *,
        method: str,
        path: str,
        operation_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        self.method = method
        self.path = path
        self.operation_id = operation_id
        self.payload = payload


def _operation_node_id(method: str, path: str) -> str:
    return f"openapi_operation:{method.upper()}:{quote(path, safe='/:{}._-')}"


def _schema_node_id(name: str) -> str:
    return f"schema:openapi:{quote(name, safe=':._/-')}"


def _frontend_properties(operation: _OpenApiOperation | None = None) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "frontend_name": OPENAPI_PROTOCOL_FRONTEND_NAME,
        "frontend_version": OPENAPI_PROTOCOL_FRONTEND_VERSION,
        "language": "openapi",
        "protocol": "openapi",
    }
    if operation is not None:
        properties.update(
            {
                "method": operation.method,
                "path": operation.path,
                "operation_id": operation.operation_id,
            }
        )
    return properties


def _schema_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            refs.add(ref.rsplit("/", 1)[-1])
        for child in value.values():
            refs.update(_schema_refs(child))
    elif isinstance(value, list):
        for item in value:
            refs.update(_schema_refs(item))
    return refs


def _load_yaml(text: str) -> Any:
    try:
        import yaml  # type: ignore[import-untyped]
    except ModuleNotFoundError:
        return _load_yaml_subset(text)
    return yaml.safe_load(text)


def _load_yaml_subset(text: str) -> dict[str, Any]:
    """Parse a minimal YAML mapping subset used by simple OpenAPI artifacts.

    PyYAML is the supported parser for OpenAPI YAML artifacts. This constrained
    fallback keeps local diagnostics usable in environments where the dependency
    is unavailable, but complex YAML should be parsed with PyYAML.
    """

    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw_line in text.splitlines():
        line = raw_line.split(" #", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if stripped.startswith("- "):
            raise ValueError("YAML list items require PyYAML support.")
        if ":" not in stripped:
            raise ValueError(f"Unsupported YAML line: {stripped!r}")
        key, raw_value = stripped.split(":", 1)
        key = _yaml_scalar(key)
        if not isinstance(key, str):
            raise ValueError(f"YAML mapping key must be a string: {stripped!r}")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        value_text = raw_value.strip()
        if value_text:
            parent[key] = _yaml_scalar(value_text)
            continue
        child: dict[str, Any] = {}
        parent[key] = child
        stack.append((indent, child))
    return root


def _yaml_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"{}", "{ }"}:
        return {}
    if value in {"[]", "[ ]"}:
        return []
    if value in {"null", "Null", "NULL", "~"}:
        return None
    if value in {"true", "True", "TRUE"}:
        return True
    if value in {"false", "False", "FALSE"}:
        return False
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def method_upper(method: str) -> str:
    return method.upper()
