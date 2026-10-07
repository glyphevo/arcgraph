"""Pydantic model, field, and settings extraction."""

from __future__ import annotations

import ast
from dataclasses import dataclass

from arcgraph.adapters.common import (
    AdapterAnalysis,
    SemanticAdapter,
    annotation_name,
    call_name,
    keyword,
    literal_str,
    make_adapter_edge,
)
from arcgraph.core.ids import (
    class_id,
    config_key_id,
    model_field_id,
    pydantic_model_id,
)
from arcgraph.core.schemas import Edge, FileRecord, Node


@dataclass(frozen=True)
class PydanticField:
    name: str
    annotation: str | None
    node: ast.AST
    value: ast.AST | None


class PydanticAdapter(SemanticAdapter):
    name = "pydantic"
    capabilities = ("resource:pydantic_model", "resource:config", "edge:declares")
    required_facts = ("class",)

    MODEL_BASES = {"BaseModel", "pydantic.BaseModel"}
    SETTINGS_BASES = {
        "BaseSettings",
        "pydantic.BaseSettings",
        "pydantic_settings.BaseSettings",
    }

    def analyze(
        self,
        files: list[FileRecord],
        parsed_files: dict[str, ast.Module],
        nodes: list[Node],
        module_names: set[str],
    ) -> AdapterAnalysis:
        del module_names
        analysis = AdapterAnalysis()
        model_count = 0
        settings_count = 0
        field_count = 0
        config_key_count = 0

        kinds = self._model_kinds(files, nodes)
        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            classes = [item for item in tree.body if isinstance(item, ast.ClassDef)]
            for stmt in classes:
                qualname = f"{file_record.module}.{stmt.name}"
                kind = kinds.get(qualname)
                if kind is None:
                    continue
                class_node_id = class_id(qualname)
                schema_node = Node(
                    id=pydantic_model_id(qualname),
                    kind="pydantic_model",
                    name=stmt.name,
                    qualname=qualname,
                    path=file_record.path,
                    start_line=stmt.lineno,
                    end_line=getattr(stmt, "end_lineno", stmt.lineno),
                    properties={"model_kind": kind},
                )
                analysis.nodes.append(schema_node)
                analysis.edges.append(
                    self._edge(
                        class_node_id,
                        schema_node.id,
                        "declares",
                        "pydantic_model",
                        file_record,
                        stmt,
                        f"{stmt.name} declares {kind}",
                    )
                )
                model_count += 1
                settings_count += 1 if kind == "settings" else 0

                for field in self._fields(stmt):
                    field_node = self._field_node(qualname, field, file_record, kind)
                    analysis.nodes.append(field_node)
                    analysis.edges.append(
                        self._edge(
                            class_node_id,
                            field_node.id,
                            "declares",
                            "pydantic_field",
                            file_record,
                            field.node,
                            f"{stmt.name}.{field.name}",
                        )
                    )
                    field_count += 1
                    if kind == "settings":
                        config_node, env_key = self._config_node(
                            stmt, field, file_record
                        )
                        analysis.nodes.append(config_node)
                        analysis.edges.append(
                            self._edge(
                                class_node_id,
                                config_node.id,
                                "configures",
                                "pydantic_settings",
                                file_record,
                                field.node,
                                f"{stmt.name}.{field.name} -> {env_key}",
                            )
                        )
                        analysis.edges.append(
                            self._edge(
                                field_node.id,
                                config_node.id,
                                "maps_to",
                                "pydantic_settings",
                                file_record,
                                field.node,
                                f"{field.name} maps to {env_key}",
                            )
                        )
                        config_key_count += 1

        analysis.nodes.sort(key=lambda node: node.id)
        analysis.edges.sort(key=lambda edge: (edge.source, edge.target, edge.kind))
        analysis.metrics = {
            "pydantic_models": model_count,
            "settings_models": settings_count,
            "model_fields": field_count,
            "config_keys": config_key_count,
            "discovered_registrations": model_count,
            "resolved_handlers": model_count,
            "unresolved_registrations": 0,
        }
        return analysis

    def _model_kinds(
        self, files: list[FileRecord], nodes: list[Node]
    ) -> dict[str, str]:
        """The kind of every module-level class that is a model or settings.

        A class is one when a base is BaseModel or BaseSettings, or a project
        class that is one, as mealie's schemas derive from its MealieModel(
        BaseModel); settings wins over model. A base imported through a
        package that re-exports it is followed to where it is defined.

        Read from the graph's nodes rather than the files being analysed, so
        an incremental reindex, which analyses only changed files, sees the
        bases and re-exports of the others too. A class whose base is defined
        in another file is marked inherited_type_input, so the reindexer
        analyses it again whenever a Python file changes.
        """

        module_by_path: dict[str, str] = {}
        aliases: dict[str, dict[str, str]] = {}
        for node in nodes:
            if node.kind != "module" or not node.qualname or not node.path:
                continue
            module_by_path[node.path] = node.qualname
            names = aliases.setdefault(node.qualname, {})
            for binding in node.properties.get("bindings") or []:
                if (
                    isinstance(binding, dict)
                    and binding.get("kind") == "import_alias"
                    and binding.get("scope_kind") == "module"
                    and isinstance(binding.get("name"), str)
                    and isinstance(binding.get("value"), str)
                ):
                    names.setdefault(binding["name"], binding["value"])

        classes: dict[str, Node] = {}
        bases: dict[str, list[str]] = {}
        for node in nodes:
            module = module_by_path.get(node.path or "")
            if node.kind != "class" or not module or not node.qualname:
                continue
            if node.qualname != f"{module}.{node.name}":
                continue
            classes[node.qualname] = node
            bases[node.qualname] = [
                resolved
                for base in node.properties.get("bases") or []
                if isinstance(base, str)
                and (resolved := self._resolved_base(base, module, aliases))
            ]

        def defined(name: str) -> str:
            for _ in range(32):
                if name in classes:
                    return name
                module, _, attribute = name.rpartition(".")
                target = aliases.get(module, {}).get(attribute)
                if target is None or target == name:
                    return name
                name = target
            return name

        kinds: dict[str, str | None] = {}

        def kind_of(qualname: str) -> str | None:
            if qualname in kinds:
                return kinds[qualname]
            kinds[qualname] = None  # a cycle of bases makes no model
            found: set[str] = set()
            for base in bases[qualname]:
                origin = defined(base)
                if {base, origin} & self.SETTINGS_BASES:
                    found.add("settings")
                elif {base, origin} & self.MODEL_BASES:
                    found.add("model")
                elif origin in classes:
                    found.add(kind_of(origin) or "")
            kind = (
                "settings"
                if "settings" in found
                else "model" if "model" in found else None
            )
            kinds[qualname] = kind
            return kind

        analysed = {file.path for file in files}
        for qualname, node in classes.items():
            if node.path in analysed and any(
                (origin := defined(base)) in classes
                and classes[origin].path != node.path
                for base in bases[qualname]
            ):
                node.properties["inherited_type_input"] = True
        return {
            qualname: kind
            for qualname in sorted(classes)
            if (kind := kind_of(qualname)) is not None
        }

    @staticmethod
    def _resolved_base(
        base: str, module: str, aliases: dict[str, dict[str, str]]
    ) -> str | None:
        name = base.split("[", 1)[0].strip()
        if not name or not name.replace(".", "").replace("_", "").isalnum():
            return None  # a call or other expression, as in declarative_base()
        first, _, rest = name.partition(".")
        target = aliases.get(module, {}).get(first)
        if target is not None:
            return f"{target}.{rest}" if rest else target
        return name if rest else f"{module}.{name}"

    def _fields(self, stmt: ast.ClassDef) -> list[PydanticField]:
        fields: list[PydanticField] = []
        for item in stmt.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                fields.append(
                    PydanticField(
                        name=item.target.id,
                        annotation=annotation_name(item.annotation),
                        node=item,
                        value=item.value,
                    )
                )
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and self._is_field_call(item.value):
                        fields.append(
                            PydanticField(
                                name=target.id,
                                annotation=None,
                                node=item,
                                value=item.value,
                            )
                        )
        return fields

    @staticmethod
    def _is_field_call(node: ast.AST | None) -> bool:
        if not isinstance(node, ast.Call):
            return False
        name = call_name(node.func)
        return bool(name and name.rsplit(".", 1)[-1] in {"Field", "PrivateAttr"})

    @staticmethod
    def _field_node(
        model_qualname: str,
        field: PydanticField,
        file_record: FileRecord,
        model_kind: str,
    ) -> Node:
        qualname = f"{model_qualname}.{field.name}"
        return Node(
            id=model_field_id(qualname),
            kind="model_field",
            name=field.name,
            qualname=qualname,
            path=file_record.path,
            start_line=getattr(field.node, "lineno", None),
            end_line=getattr(
                field.node, "end_lineno", getattr(field.node, "lineno", None)
            ),
            properties={"annotation": field.annotation, "model_kind": model_kind},
        )

    def _config_node(
        self,
        stmt: ast.ClassDef,
        field: PydanticField,
        file_record: FileRecord,
    ) -> tuple[Node, str]:
        env_key = self._field_env_key(field) or field.name.upper()
        node_id = config_key_id(f"env:{env_key}")
        return (
            Node(
                id=node_id,
                kind="config",
                name=env_key,
                qualname=env_key,
                path=file_record.path,
                start_line=getattr(field.node, "lineno", stmt.lineno),
                properties={
                    "source": "pydantic_settings",
                    "settings_class": stmt.name,
                    "field": field.name,
                    "key": env_key,
                },
            ),
            env_key,
        )

    @staticmethod
    def _field_env_key(field: PydanticField) -> str | None:
        if not isinstance(field.value, ast.Call):
            return None
        for key in ("env", "validation_alias", "alias"):
            value = literal_str(keyword(field.value, key))
            if value:
                return value
        return None

    @staticmethod
    def _edge(
        source: str,
        target: str,
        kind: str,
        evidence_kind: str,
        file_record: FileRecord,
        node: ast.AST,
        detail: str,
    ) -> Edge:
        return make_adapter_edge(
            source=source,
            target=target,
            kind=kind,
            confidence="heuristic",
            evidence_kind=evidence_kind,
            file_record=file_record,
            node=node,
            detail=detail,
        )
