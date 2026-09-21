"""Pydantic model, field, and settings extraction."""

from __future__ import annotations

import ast
from dataclasses import dataclass

from arcgraph.adapters.common import (
    AdapterAnalysis,
    ImportAliases,
    SemanticAdapter,
    annotation_name,
    call_name,
    import_aliases,
    keyword,
    literal_str,
    make_adapter_edge,
    resolve_name,
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
        del nodes
        analysis = AdapterAnalysis()
        model_count = 0
        settings_count = 0
        field_count = 0
        config_key_count = 0

        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            imports = import_aliases(file_record, tree, module_names)
            classes = [item for item in tree.body if isinstance(item, ast.ClassDef)]
            for stmt in classes:
                kind = self._pydantic_kind(stmt, file_record, imports)
                if kind is None:
                    continue
                qualname = f"{file_record.module}.{stmt.name}"
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

    def _pydantic_kind(
        self,
        stmt: ast.ClassDef,
        file_record: FileRecord,
        imports: ImportAliases,
    ) -> str | None:
        resolved_bases = {
            self._resolved_annotation(base, file_record, imports) for base in stmt.bases
        }
        if resolved_bases & self.SETTINGS_BASES:
            return "settings"
        if resolved_bases & self.MODEL_BASES:
            return "model"
        return None

    @staticmethod
    def _resolved_annotation(
        node: ast.AST, file_record: FileRecord, imports: ImportAliases
    ) -> str | None:
        name = annotation_name(node)
        if not name:
            return None
        resolved = resolve_name(name, file_record, imports)
        return resolved if "." in resolved else name.rsplit(".", 1)[-1]

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
