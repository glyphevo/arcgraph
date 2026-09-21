"""Framework semantics layered over the TypeScript/JavaScript frontend."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from arcgraph.core.ids import component_id, module_id, route_id
from arcgraph.core.scanner import logical_module_name
from arcgraph.core.schemas import BuildWarning, Edge, Evidence, FactResolution, Node

TYPESCRIPT_FRONTEND_NAME = "typescript-static"
TYPESCRIPT_FRONTEND_VERSION = "0.1.0"
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
NEXT_PAGE_FILENAMES = {"page.tsx", "page.jsx", "page.ts", "page.js"}
NEXT_LAYOUT_FILENAMES = {"layout.tsx", "layout.jsx", "layout.ts", "layout.js"}
NEXT_LOADING_FILENAMES = {"loading.tsx", "loading.jsx", "loading.ts", "loading.js"}
NEXT_ERROR_FILENAMES = {"error.tsx", "error.jsx", "error.ts", "error.js"}
NEXT_ROUTE_FILENAMES = {"route.ts", "route.js"}
VUE_COMPONENT_TAG_RE = re.compile(r"<\s*([A-Za-z][A-Za-z0-9.-]*)\b")
VUE_DYNAMIC_COMPONENT_RE = re.compile(r"<\s*component\b[^>]*\s(:is|v-bind:is)\s*=")
VUE_IMPORT_RE = re.compile(
    r"import\s+([A-Za-z_$][\w$]*)\s+from\s+['\"]([^'\"]+\.vue)['\"]"
)
VUE_SCRIPT_RE = re.compile(
    r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.IGNORECASE | re.DOTALL
)
NEXT_LINK_RE = re.compile(
    r"(?:<Link\b[^>]*\bhref|<a\b[^>]*\bhref)\s*=\s*['\"](?P<href>/[^'\"#?]*)['\"]",
    re.IGNORECASE,
)


@dataclass
class TypeScriptFrameworkResult:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    warnings: list[BuildWarning] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


class TypeScriptFrameworkAnalyzer:
    """Add deterministic framework facts without changing language tiering."""

    def __init__(self, repo_root: str | None = None) -> None:
        self.repo_root = repo_root

    def analyze(
        self, files: list[Any], existing_nodes: list[Node]
    ) -> TypeScriptFrameworkResult:
        by_path: dict[str, list[Node]] = defaultdict(list)
        for node in existing_nodes:
            if node.path:
                by_path[node.path].append(node)

        next_nodes, next_edges, next_warnings = self._analyze_next(files, by_path)
        vue_nodes, vue_edges, vue_warnings = self._analyze_vue(files)

        next_route_count = sum(
            1 for node in next_nodes if node.properties.get("framework") == "nextjs"
        )
        vue_component_count = sum(
            1
            for node in vue_nodes
            if node.kind == "component" and node.properties.get("framework") == "vue"
        )
        metrics = {
            "frameworks": {
                "nextjs": {
                    "status": "available" if next_nodes else "not_detected",
                    "routes": next_route_count,
                    "edges": len(next_edges),
                    "warnings": len(next_warnings),
                },
                "vue": {
                    "status": "available" if vue_component_count else "not_detected",
                    "components": vue_component_count,
                    "edges": len(vue_edges),
                    "warnings": len(vue_warnings),
                },
            }
        }
        return TypeScriptFrameworkResult(
            nodes=[*next_nodes, *vue_nodes],
            edges=[*next_edges, *vue_edges],
            warnings=[*next_warnings, *vue_warnings],
            metrics=metrics,
        )

    def _analyze_next(
        self,
        files: list[Any],
        by_path: dict[str, list[Node]],
    ) -> tuple[list[Node], list[Edge], list[BuildWarning]]:
        nodes: list[Node] = []
        edges: list[Edge] = []
        warnings: list[BuildWarning] = []
        route_nodes: dict[str, Node] = {}
        page_components: dict[str, str] = {}

        for file_record in files:
            path = PurePosixPath(file_record.path)
            filename = path.name
            text = self._read_text(file_record)

            if self._is_next_middleware(path):
                warnings.append(
                    BuildWarning(
                        kind="nextjs_middleware_unsupported",
                        path=file_record.path,
                        message=(
                            "Next.js middleware semantics are runtime-mounted "
                            "and are not modeled by the static framework layer."
                        ),
                    )
                )
            if self._is_next_config_with_rewrites(path, text):
                warnings.append(
                    BuildWarning(
                        kind="nextjs_rewrites_unsupported",
                        path=file_record.path,
                        message=(
                            "Next.js rewrites are dynamic routing config and are "
                            "not resolved into route facts statically."
                        ),
                    )
                )

            app_route = self._next_app_route(file_record, path)
            if app_route and filename in NEXT_PAGE_FILENAMES:
                route_node = self._route_node(
                    "nextjs",
                    "app",
                    "page",
                    "GET",
                    app_route.path,
                    file_record,
                    dynamic_segments=app_route.dynamic_segments,
                )
                nodes.append(route_node)
                route_nodes[route_node.id] = route_node
                component = self._component_for_path(
                    file_record,
                    by_path,
                    default_name="Page",
                    framework="nextjs",
                    role="page",
                )
                if component.id not in {node.id for node in by_path[file_record.path]}:
                    nodes.append(component)
                page_components[file_record.path] = component.id
                edges.append(
                    self._edge(
                        route_node.id,
                        component.id,
                        "renders",
                        "inferred",
                        "next_app_page",
                        file_record,
                        "Next.js app router page",
                        strategy="nextjs_filesystem_route",
                        properties={"framework": "nextjs", "router": "app"},
                    )
                )
                continue

            if app_route and filename in NEXT_ROUTE_FILENAMES:
                for method in self._exported_http_methods(text):
                    route_node = self._route_node(
                        "nextjs",
                        "app",
                        "route_handler",
                        method,
                        app_route.path,
                        file_record,
                        dynamic_segments=app_route.dynamic_segments,
                    )
                    nodes.append(route_node)
                    route_nodes[route_node.id] = route_node
                    target = self._target_for_name(by_path[file_record.path], method)
                    if target:
                        edges.append(
                            self._edge(
                                route_node.id,
                                target,
                                "invokes",
                                "inferred",
                                "next_route_handler",
                                file_record,
                                f"Next.js route handler {method}",
                                strategy="nextjs_exported_http_method",
                                properties={
                                    "framework": "nextjs",
                                    "router": "app",
                                    "http_method": method,
                                },
                            )
                        )
                    else:
                        warnings.append(
                            BuildWarning(
                                kind="nextjs_route_handler_unresolved",
                                path=file_record.path,
                                message=(
                                    f"Next.js route handler {method} was found "
                                    "but no indexed local function target matched it."
                                ),
                            )
                        )
                continue

            if app_route and filename in (
                NEXT_LAYOUT_FILENAMES | NEXT_LOADING_FILENAMES | NEXT_ERROR_FILENAMES
            ):
                role = filename.split(".", 1)[0]
                component = self._component_for_path(
                    file_record,
                    by_path,
                    default_name=role.title(),
                    framework="nextjs",
                    role=role,
                )
                if component.id not in {node.id for node in by_path[file_record.path]}:
                    nodes.append(component)
                continue

            pages_route = self._next_pages_route(file_record, path)
            if pages_route and pages_route.kind == "page":
                route_node = self._route_node(
                    "nextjs",
                    "pages",
                    "page",
                    "GET",
                    pages_route.path,
                    file_record,
                    dynamic_segments=pages_route.dynamic_segments,
                )
                nodes.append(route_node)
                route_nodes[route_node.id] = route_node
                component = self._component_for_path(
                    file_record,
                    by_path,
                    default_name="Page",
                    framework="nextjs",
                    role="page",
                )
                if component.id not in {node.id for node in by_path[file_record.path]}:
                    nodes.append(component)
                page_components[file_record.path] = component.id
                edges.append(
                    self._edge(
                        route_node.id,
                        component.id,
                        "renders",
                        "inferred",
                        "next_pages_page",
                        file_record,
                        "Next.js pages router page",
                        strategy="nextjs_filesystem_route",
                        properties={"framework": "nextjs", "router": "pages"},
                    )
                )
                continue

            if pages_route and pages_route.kind == "api":
                route_node = self._route_node(
                    "nextjs",
                    "pages",
                    "api_route",
                    "ANY",
                    pages_route.path,
                    file_record,
                    dynamic_segments=pages_route.dynamic_segments,
                )
                nodes.append(route_node)
                route_nodes[route_node.id] = route_node
                target = self._target_for_name(
                    by_path[file_record.path], "default"
                ) or self._target_for_name(by_path[file_record.path], "handler")
                if target:
                    edges.append(
                        self._edge(
                            route_node.id,
                            target,
                            "invokes",
                            "inferred",
                            "next_pages_api",
                            file_record,
                            "Next.js pages API route",
                            strategy="nextjs_pages_api",
                            properties={"framework": "nextjs", "router": "pages"},
                        )
                    )
                else:
                    warnings.append(
                        BuildWarning(
                            kind="nextjs_pages_api_handler_unresolved",
                            path=file_record.path,
                            message=(
                                "Next.js pages API route was found but no indexed "
                                "default or handler function matched it."
                            ),
                        )
                    )

        edges.extend(self._next_static_link_edges(files, page_components, route_nodes))
        return nodes, edges, warnings

    def _analyze_vue(
        self,
        files: list[Any],
    ) -> tuple[list[Node], list[Edge], list[BuildWarning]]:
        vue_files = [file for file in files if file.path.endswith(".vue")]
        vue_by_path = {file.path: file for file in vue_files}
        component_by_path = {
            file.path: self._vue_component_node(file, self._read_text(file))
            for file in vue_files
        }
        component_by_name = {node.name: node.id for node in component_by_path.values()}

        nodes: list[Node] = list(component_by_path.values())
        edges: list[Edge] = []
        warnings: list[BuildWarning] = []

        for file_record in vue_files:
            text = self._read_text(file_record)
            source_component = component_by_path[file_record.path]
            module_node = self._module_node(file_record, language="vue")
            nodes.append(module_node)
            edges.append(
                self._edge(
                    module_node.id,
                    source_component.id,
                    "defines",
                    "confirmed",
                    "vue_sfc_define",
                    file_record,
                    "Vue single-file component",
                    properties={"framework": "vue"},
                )
            )

            for prop in self._vue_props(text):
                prop_node = self._vue_decl_node(source_component, prop, "prop")
                nodes.append(prop_node)
                edges.append(
                    self._edge(
                        source_component.id,
                        prop_node.id,
                        "declares",
                        "confirmed",
                        "vue_define_props",
                        file_record,
                        prop,
                        properties={"framework": "vue", "declaration_kind": "prop"},
                    )
                )
            for event in self._vue_emits(text):
                event_node = self._vue_decl_node(source_component, event, "emit")
                nodes.append(event_node)
                edges.append(
                    self._edge(
                        source_component.id,
                        event_node.id,
                        "declares",
                        "confirmed",
                        "vue_define_emits",
                        file_record,
                        event,
                        properties={"framework": "vue", "declaration_kind": "emit"},
                    )
                )

            imports = self._vue_imports(file_record, text, vue_by_path)
            for local_name, target in imports.items():
                edges.append(
                    self._edge(
                        source_component.id,
                        target.id,
                        "imports",
                        "confirmed",
                        "vue_component_import",
                        file_record,
                        local_name,
                        properties={"framework": "vue"},
                    )
                )

            for tag in self._template_component_tags(text):
                target = imports.get(tag) or imports.get(_pascal_case(tag))
                if target is None and tag in component_by_name:
                    target_id = component_by_name[tag]
                else:
                    target_id = target.id if target else None
                if target_id:
                    edges.append(
                        self._edge(
                            source_component.id,
                            target_id,
                            "renders",
                            "heuristic",
                            "vue_template_component",
                            file_record,
                            tag,
                            properties={"framework": "vue"},
                        )
                    )

            if VUE_DYNAMIC_COMPONENT_RE.search(text):
                warnings.append(
                    BuildWarning(
                        kind="vue_dynamic_component_unresolved",
                        path=file_record.path,
                        message=(
                            "Vue dynamic component usage cannot be resolved "
                            "statically."
                        ),
                    )
                )

        route_nodes, route_edges, route_warnings = self._vue_router_edges(
            files, component_by_path
        )
        nodes.extend(route_nodes)
        edges.extend(route_edges)
        warnings.extend(route_warnings)
        return nodes, edges, warnings

    def _vue_router_edges(
        self,
        files: list[Any],
        component_by_path: dict[str, Node],
    ) -> tuple[list[Node], list[Edge], list[BuildWarning]]:
        nodes: list[Node] = []
        edges: list[Edge] = []
        warnings: list[BuildWarning] = []
        for file_record in files:
            if not file_record.path.endswith((".ts", ".tsx", ".js", ".jsx")):
                continue
            text = self._read_text(file_record)
            if "createRouter" not in text and "routes" not in text:
                continue
            imports = self._vue_imports(file_record, text, component_by_path)
            for match in re.finditer(
                r"path\s*:\s*['\"](?P<path>/[^'\"]*)['\"][^{}]*?"
                r"component\s*:\s*(?P<component>[A-Za-z_$][\w$]*)",
                text,
                flags=re.DOTALL,
            ):
                route_path = match.group("path") or "/"
                component_name = match.group("component")
                target = imports.get(component_name)
                if not target:
                    warnings.append(
                        BuildWarning(
                            kind="vue_router_target_unresolved",
                            path=file_record.path,
                            message=(
                                f"Vue Router path {route_path} references "
                                f"{component_name}, but no indexed .vue import "
                                "matched it."
                            ),
                        )
                    )
                    continue
                route_node = self._route_node(
                    "vue",
                    "vue-router",
                    "client_route",
                    "VIEW",
                    route_path,
                    file_record,
                    dynamic_segments=self._dynamic_segments(route_path),
                )
                nodes.append(route_node)
                edges.append(
                    self._edge(
                        route_node.id,
                        target.id,
                        "renders",
                        "inferred",
                        "vue_router_static_route",
                        file_record,
                        f"Vue Router route {route_path}",
                        strategy="vue_router_static_route",
                        properties={"framework": "vue", "router": "vue-router"},
                    )
                )
        return nodes, edges, warnings

    def _next_static_link_edges(
        self,
        files: list[Any],
        page_components: dict[str, str],
        route_nodes: dict[str, Node],
    ) -> list[Edge]:
        by_path = {
            node.properties.get("route_path"): node
            for node in route_nodes.values()
            if node.properties.get("route_path")
        }
        edges: list[Edge] = []
        for file_record in files:
            source = page_components.get(file_record.path)
            if not source:
                continue
            text = self._read_text(file_record)
            for match in NEXT_LINK_RE.finditer(text):
                href = match.group("href") or "/"
                target = by_path.get(href)
                if not target:
                    continue
                edges.append(
                    self._edge(
                        source,
                        target.id,
                        "links_to",
                        "heuristic",
                        "next_static_link",
                        file_record,
                        href,
                        properties={"framework": "nextjs"},
                    )
                )
        return edges

    @staticmethod
    def _is_next_middleware(path: PurePosixPath) -> bool:
        return path.name in {"middleware.ts", "middleware.js"}

    @staticmethod
    def _is_next_config_with_rewrites(path: PurePosixPath, text: str) -> bool:
        return path.name.startswith("next.config.") and "rewrites" in text

    @staticmethod
    def _next_app_route(file_record: Any, path: PurePosixPath) -> "_RoutePath | None":
        parts = path.parts
        if "app" not in parts:
            return None
        app_index = parts.index("app")
        segments = _route_segments(parts[app_index + 1 : -1])
        return _RoutePath(
            path=_path_from_segments(segments),
            dynamic_segments=tuple(
                segment
                for segment in segments
                if segment.startswith("[") and segment.endswith("]")
            ),
            kind="app",
        )

    @staticmethod
    def _next_pages_route(file_record: Any, path: PurePosixPath) -> "_RoutePath | None":
        parts = path.parts
        if "pages" not in parts:
            return None
        pages_index = parts.index("pages")
        route_parts = list(parts[pages_index + 1 :])
        if not route_parts:
            return None
        filename = route_parts[-1]
        stem = filename.rsplit(".", 1)[0]
        if filename.endswith(".d.ts"):
            return None
        route_parts[-1] = stem
        kind = "api" if route_parts and route_parts[0] == "api" else "page"
        if route_parts[-1] == "index":
            route_parts = route_parts[:-1]
        segments = _route_segments(tuple(route_parts))
        return _RoutePath(
            path=_path_from_segments(segments),
            dynamic_segments=tuple(
                segment
                for segment in segments
                if segment.startswith("[") and segment.endswith("]")
            ),
            kind=kind,
        )

    @staticmethod
    def _exported_http_methods(text: str) -> list[str]:
        methods: list[str] = []
        for method in HTTP_METHODS:
            if re.search(
                rf"\bexport\s+(?:async\s+)?function\s+{method}\b", text
            ) or re.search(rf"\bexport\s+const\s+{method}\b", text):
                methods.append(method)
        return methods

    @staticmethod
    def _target_for_name(nodes: list[Node], name: str) -> str | None:
        for node in nodes:
            if node.name == name:
                return node.id
            if name == "default" and node.properties.get("default_export") is True:
                return node.id
        return None

    def _component_for_path(
        self,
        file_record: Any,
        by_path: dict[str, list[Node]],
        *,
        default_name: str,
        framework: str,
        role: str,
    ) -> Node:
        for node in by_path[file_record.path]:
            if node.kind == "component":
                node.properties.setdefault("framework", framework)
                node.properties.setdefault("framework_role", role)
                return node
        display_module = logical_module_name(file_record.module)
        name = _pascal_case(display_module.rsplit(".", 1)[-1]) or default_name
        if name.lower() in {"page", "layout", "loading", "error", "route"}:
            name = default_name
        qualname = f"{file_record.module}.{name}"
        return Node(
            id=component_id(qualname),
            kind="component",
            name=name,
            qualname=qualname,
            path=file_record.path,
            properties={
                "language": "typescript",
                "framework": framework,
                "framework_role": role,
                "frontend_name": TYPESCRIPT_FRONTEND_NAME,
                "frontend_version": TYPESCRIPT_FRONTEND_VERSION,
            },
        )

    @staticmethod
    def _route_node(
        framework: str,
        router: str,
        route_kind: str,
        method: str,
        path: str,
        file_record: Any,
        *,
        dynamic_segments: tuple[str, ...] = (),
    ) -> Node:
        node_id = route_id(method, path)
        return Node(
            id=node_id,
            kind="route",
            name=f"{method.upper()} {path}",
            qualname=node_id,
            path=file_record.path,
            properties={
                "language": "typescript",
                "framework": framework,
                "router": router,
                "route_kind": route_kind,
                "route_path": path,
                "http_method": method.upper(),
                "dynamic_segments": list(dynamic_segments),
                "frontend_name": TYPESCRIPT_FRONTEND_NAME,
                "frontend_version": TYPESCRIPT_FRONTEND_VERSION,
            },
        )

    @staticmethod
    def _module_node(file_record: Any, *, language: str) -> Node:
        display_module = logical_module_name(file_record.module)
        return Node(
            id=module_id(file_record.module),
            kind="module",
            name=display_module.rsplit(".", 1)[-1] or display_module,
            qualname=file_record.module,
            path=file_record.path,
            properties={
                "line_count": file_record.line_count,
                "display_module": display_module,
                "source_root": file_record.source_root,
                "is_package": file_record.is_package,
                "language": language,
                "frontend_name": TYPESCRIPT_FRONTEND_NAME,
                "frontend_version": TYPESCRIPT_FRONTEND_VERSION,
            },
        )

    @staticmethod
    def _vue_component_node(file_record: Any, text: str) -> Node:
        name = TypeScriptFrameworkAnalyzer._vue_component_name(file_record, text)
        qualname = f"{file_record.module}.{name}"
        script = TypeScriptFrameworkAnalyzer._vue_script_metadata(text)
        return Node(
            id=component_id(qualname),
            kind="component",
            name=name,
            qualname=qualname,
            path=file_record.path,
            properties={
                "language": "vue",
                "framework": "vue",
                "sfc": True,
                "script_setup": script["script_setup"],
                "script_lang": script["script_lang"],
                "frontend_name": TYPESCRIPT_FRONTEND_NAME,
                "frontend_version": TYPESCRIPT_FRONTEND_VERSION,
            },
        )

    @staticmethod
    def _vue_component_name(file_record: Any, text: str) -> str:
        match = re.search(r"\bname\s*:\s*['\"]([A-Za-z][\w]*)['\"]", text)
        if match:
            return match.group(1)
        return _pascal_case(PurePosixPath(file_record.path).stem)

    @staticmethod
    def _vue_script_metadata(text: str) -> dict[str, Any]:
        script_setup = False
        script_lang = None
        for match in VUE_SCRIPT_RE.finditer(text):
            attrs = match.group("attrs") or ""
            if "setup" in attrs:
                script_setup = True
            lang_match = re.search(r"lang\s*=\s*['\"]([^'\"]+)['\"]", attrs)
            if lang_match:
                script_lang = lang_match.group(1)
        return {"script_setup": script_setup, "script_lang": script_lang}

    @staticmethod
    def _vue_decl_node(component: Node, name: str, kind: str) -> Node:
        node_id = f"vue_{kind}:{component.qualname}:{name}"
        return Node(
            id=node_id,
            kind=f"vue_{kind}",
            name=name,
            qualname=f"{component.qualname}.{name}",
            path=component.path,
            properties={
                "language": "vue",
                "framework": "vue",
                "frontend_name": TYPESCRIPT_FRONTEND_NAME,
                "frontend_version": TYPESCRIPT_FRONTEND_VERSION,
            },
        )

    @staticmethod
    def _vue_props(text: str) -> list[str]:
        props: set[str] = set()
        for body in _generic_macro_bodies(text, "defineProps"):
            props.update(_type_literal_keys(body))
        for body in _object_macro_bodies(text, "defineProps"):
            props.update(_object_keys(body))
        for body in _object_property_bodies(text, "props"):
            props.update(_object_keys(body))
        return sorted(props)

    @staticmethod
    def _vue_emits(text: str) -> list[str]:
        emits: set[str] = set()
        for body in _array_macro_bodies(text, "defineEmits"):
            emits.update(_array_strings(body))
        for body in _generic_macro_bodies(text, "defineEmits"):
            emits.update(re.findall(r"['\"]([A-Za-z][\w:-]*)['\"]", body))
        for body in _array_property_bodies(text, "emits"):
            emits.update(_array_strings(body))
        return sorted(emits)

    @staticmethod
    def _vue_imports(
        file_record: Any,
        text: str,
        vue_by_path: dict[str, Any | Node],
    ) -> dict[str, Node]:
        imports: dict[str, Node] = {}
        base = PurePosixPath(file_record.path).parent
        for match in VUE_IMPORT_RE.finditer(text):
            local_name = match.group(1)
            specifier = match.group(2)
            target_path = _resolve_relative_vue_path(base, specifier)
            target = vue_by_path.get(target_path)
            if target is None:
                continue
            if isinstance(target, Node):
                imports[local_name] = target
            else:
                imports[local_name] = TypeScriptFrameworkAnalyzer._vue_component_node(
                    target,
                    TypeScriptFrameworkAnalyzer._read_text(target),
                )
        return imports

    @staticmethod
    def _template_component_tags(text: str) -> list[str]:
        tags: set[str] = set()
        for match in VUE_COMPONENT_TAG_RE.finditer(text):
            tag = match.group(1)
            if tag.lower() in {
                "template",
                "script",
                "style",
                "component",
                "slot",
                "div",
                "span",
                "button",
                "input",
                "main",
                "section",
                "p",
            }:
                continue
            if tag[0].isupper() or "-" in tag:
                tags.add(tag if tag[0].isupper() else _pascal_case(tag))
        return sorted(tags)

    @staticmethod
    def _dynamic_segments(path: str) -> tuple[str, ...]:
        return tuple(segment for segment in path.split("/") if segment.startswith(":"))

    @staticmethod
    def _edge(
        source: str,
        target: str,
        kind: str,
        confidence: str,
        evidence_kind: str,
        file_record: Any,
        detail: str,
        *,
        strategy: str | None = None,
        properties: dict[str, Any] | None = None,
    ) -> Edge:
        return Edge(
            source=source,
            target=target,
            kind=kind,
            confidence=confidence,  # type: ignore[arg-type]
            resolution=(
                FactResolution(strategy=strategy) if strategy else FactResolution()
            ),
            evidence=[
                Evidence(
                    kind=evidence_kind,
                    path=file_record.path,
                    start_line=1,
                    end_line=1,
                    column=1,
                    detail=detail,
                )
            ],
            properties={
                **(properties or {}),
                "language": "typescript",
                "frontend_name": TYPESCRIPT_FRONTEND_NAME,
                "frontend_version": TYPESCRIPT_FRONTEND_VERSION,
            },
        )

    @staticmethod
    def _read_text(file_record: Any) -> str:
        try:
            return Path(file_record.abs_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""


@dataclass(frozen=True)
class _RoutePath:
    path: str
    dynamic_segments: tuple[str, ...]
    kind: str


def _route_segments(parts: tuple[str, ...]) -> tuple[str, ...]:
    segments: list[str] = []
    for part in parts:
        if part.startswith("(") and part.endswith(")"):
            continue
        if part.startswith("@"):
            continue
        segments.append(part)
    return tuple(segments)


def _path_from_segments(segments: tuple[str, ...]) -> str:
    return "/" + "/".join(segment for segment in segments if segment and segment != ".")


def _pascal_case(value: str) -> str:
    pieces = [piece for piece in re.split(r"[^A-Za-z0-9]+", value) if piece]
    return "".join(piece[:1].upper() + piece[1:] for piece in pieces) or "Component"


def _generic_macro_bodies(text: str, macro: str) -> list[str]:
    return re.findall(rf"\b{macro}\s*<(?P<body>.*?)>\s*\(", text, flags=re.DOTALL)


def _object_macro_bodies(text: str, macro: str) -> list[str]:
    return re.findall(
        rf"\b{macro}\s*\(\s*\{{(?P<body>.*?)\}}\s*\)", text, flags=re.DOTALL
    )


def _array_macro_bodies(text: str, macro: str) -> list[str]:
    return re.findall(rf"\b{macro}\s*\(\s*\[(?P<body>.*?)\]\s*\)", text, re.DOTALL)


def _object_property_bodies(text: str, name: str) -> list[str]:
    return re.findall(rf"\b{name}\s*:\s*\{{(?P<body>.*?)\}}", text, flags=re.DOTALL)


def _array_property_bodies(text: str, name: str) -> list[str]:
    return re.findall(rf"\b{name}\s*:\s*\[(?P<body>.*?)\]", text, flags=re.DOTALL)


def _type_literal_keys(body: str) -> set[str]:
    keys: set[str] = set()
    for match in re.finditer(r"([A-Za-z_$][\w$]*)\??\s*:", body):
        keys.add(match.group(1))
    return keys


def _object_keys(body: str) -> set[str]:
    keys: set[str] = set()
    for match in re.finditer(r"([A-Za-z_$][\w$]*)\s*:", body):
        keys.add(match.group(1))
    return keys


def _array_strings(body: str) -> set[str]:
    return set(re.findall(r"['\"]([A-Za-z][\w:-]*)['\"]", body))


def _resolve_relative_vue_path(base: PurePosixPath, specifier: str) -> str:
    if not specifier.startswith("."):
        return specifier
    stack = list(base.parts)
    for part in PurePosixPath(specifier).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if stack:
                stack.pop()
            continue
        stack.append(part)
    return PurePosixPath(*stack).as_posix()
