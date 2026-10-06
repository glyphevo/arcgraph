"""Similarity analyzer for function and method implementations."""

from __future__ import annotations

import ast
import copy
import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from arcgraph.core.ids import function_id, method_id
from arcgraph.core.schemas import BuildWarning, Edge, Evidence, Node
from arcgraph.core.semantic import COMPAT_FRONTEND_NAME

PROFILE_ALGORITHM = "python_ast_token_ngrams_v1"


@dataclass(slots=True)
class SimilarityAnalysis:
    edges: list[Edge] = field(default_factory=list)
    warnings: list[BuildWarning] = field(default_factory=list)


@dataclass(slots=True)
class _Profile:
    node: Node
    bucket: str
    structure_hash: str
    ngrams: set[str]
    call_targets: set[str]
    resource_targets: set[str]
    profile: dict[str, Any]


class SimilarityAnalyzer:
    """Generate bounded `similar_to` edges from normalized AST profiles."""

    CALLABLE_KINDS = {"function", "method"}
    RESOURCE_KINDS = {"reads", "writes", "enqueues", "publishes", "consumes"}
    MAX_BUCKET_SIZE = 200
    MAX_NGRAMS = 512
    MIN_SCORE = 0.82

    def analyze(
        self,
        parsed_files: dict[str, ast.Module],
        nodes: list[Node],
        edges: list[Edge],
        *,
        source_node_ids: set[str] | None = None,
        refresh_buckets: set[str] | None = None,
    ) -> SimilarityAnalysis:
        ast_by_id = self._function_asts(parsed_files)
        call_targets, resource_targets = self._edge_features(edges)
        valid_target_ids = {node.id for node in nodes}
        profiles: list[_Profile] = []
        warnings: list[BuildWarning] = []

        for node in nodes:
            if node.kind not in self.CALLABLE_KINDS:
                continue
            profile = self._profile_for_node(
                node,
                ast_by_id.get(node.id),
                call_targets.get(node.id, set()),
                resource_targets.get(node.id, set()),
                valid_target_ids,
            )
            if profile is not None:
                profiles.append(profile)

        by_bucket: dict[str, list[_Profile]] = defaultdict(list)
        for profile in profiles:
            by_bucket[profile.bucket].append(profile)

        # New/changed callables reveal their buckets only after profiling. Union
        # those with the caller-provided refresh set so an add into an existing
        # bucket fully rescored rather than dropping unchanged-unchanged edges.
        effective_refresh_buckets: set[str] | None = None
        if refresh_buckets is not None:
            effective_refresh_buckets = set(refresh_buckets)
            if source_node_ids is not None:
                effective_refresh_buckets.update(
                    profile.bucket
                    for profile in profiles
                    if profile.node.id in source_node_ids
                )

        emitted: dict[tuple[str, str], Edge] = {}
        for bucket, bucket_profiles in by_bucket.items():
            # Affected buckets are fully rescored so skip thresholds and
            # unchanged-unchanged pairs match a full build. Other buckets keep
            # the incremental filter that only emits pairs involving a changed
            # callable.
            refresh_bucket = (
                effective_refresh_buckets is not None
                and bucket in effective_refresh_buckets
            )
            for comparison_group in self._comparison_groups(
                bucket,
                bucket_profiles,
                warnings,
            ):
                for index, source in enumerate(comparison_group):
                    for target in comparison_group[index + 1 :]:
                        if (
                            source_node_ids is not None
                            and not refresh_bucket
                            and not (
                                source.node.id in source_node_ids
                                or target.node.id in source_node_ids
                            )
                        ):
                            continue
                        score, reasons = self._score(source, target)
                        if score < self.MIN_SCORE:
                            continue
                        source_id, target_id = sorted([source.node.id, target.node.id])
                        emitted[(source_id, target_id)] = self._edge(
                            source if source.node.id == source_id else target,
                            target if target.node.id == target_id else source,
                            score=score,
                            reasons=reasons,
                            bucket=bucket,
                        )

        return SimilarityAnalysis(
            edges=sorted(
                emitted.values(), key=lambda edge: (edge.source, edge.target, edge.kind)
            ),
            warnings=warnings,
        )

    def _comparison_groups(
        self,
        bucket: str,
        bucket_profiles: list[_Profile],
        warnings: list[BuildWarning],
    ) -> list[list[_Profile]]:
        if len(bucket_profiles) <= self.MAX_BUCKET_SIZE:
            return [bucket_profiles]

        by_structure: dict[str, list[_Profile]] = defaultdict(list)
        for profile in bucket_profiles:
            by_structure[profile.structure_hash].append(profile)

        groups: list[list[_Profile]] = []
        for structure_hash, profiles in sorted(by_structure.items()):
            if len(profiles) < 2:
                continue
            if len(profiles) > self.MAX_BUCKET_SIZE:
                warnings.append(
                    BuildWarning(
                        kind="similarity_bucket_skipped",
                        message=(
                            f"Skipped similarity bucket {bucket!r} structure "
                            f"{structure_hash!r} with {len(profiles)} candidates."
                        ),
                        frontend_name=COMPAT_FRONTEND_NAME,
                    )
                )
                continue
            groups.append(profiles)

        return groups

    @staticmethod
    def _surviving_targets(
        values: Any,
        valid_target_ids: set[str],
    ) -> set[str]:
        """Drop restored edge features whose target no longer exists.

        A restored profile carries the feature ids captured when it was last
        written. Scoring them verbatim credits a match against a deleted node
        and inflates the result above what a full build would produce, so keep
        only surviving ids plus externals, which are not carried as nodes here.
        """

        return {
            value
            for value in _strings(values)
            if value in valid_target_ids
            or value.startswith(("ext:", "extsym:", "protocol:"))
        }

    def _profile_for_node(
        self,
        node: Node,
        function_ast: ast.AST | None,
        call_targets: set[str],
        resource_targets: set[str],
        valid_target_ids: set[str],
    ) -> _Profile | None:
        if node.name.startswith("__") and node.name.endswith("__"):
            return None
        existing = node.properties.get("similarity")
        # A profile written by a different algorithm generation is not
        # comparable with freshly computed ones; ignore it and recompute or
        # skip. It must not be popped: the reindexer hands this analyzer the
        # live unchanged nodes it republishes, including callables owned by
        # another frontend, so mutating here would strip that frontend's
        # persisted profile from the index.
        if (
            isinstance(existing, dict)
            and existing.get("algorithm") != PROFILE_ALGORITHM
        ):
            existing = None
        if function_ast is None and isinstance(existing, dict):
            ngrams = set(_strings(existing.get("ngrams")))
            if ngrams:
                surviving_calls = self._surviving_targets(
                    existing.get("call_targets"), valid_target_ids
                )
                surviving_resources = self._surviving_targets(
                    existing.get("resource_targets"), valid_target_ids
                )
                # Persist the pruned features rather than only scoring with
                # them. This node is republished by the reindexer, so leaving
                # the stale lists in ``properties`` keeps ids of deleted nodes
                # in the index, diverges from a full build, and lets the old
                # features revive if the same id is created again later.
                restored = {
                    **existing,
                    "call_targets": sorted(surviving_calls),
                    "resource_targets": sorted(surviving_resources),
                }
                node.properties["similarity"] = restored
                return _Profile(
                    node=node,
                    bucket=str(existing.get("bucket", "")),
                    structure_hash=str(existing.get("structure_hash", "")),
                    ngrams=ngrams,
                    call_targets=surviving_calls,
                    resource_targets=surviving_resources,
                    profile=restored,
                )
            return None
        if function_ast is None:
            return None

        normalized = _NormalizedAst().visit(copy.deepcopy(function_ast))
        ast.fix_missing_locations(normalized)
        normalized_dump = ast.dump(normalized, include_attributes=False)
        structure_hash = hashlib.sha256(normalized_dump.encode("utf-8")).hexdigest()[
            :16
        ]
        ngrams = self._ngrams(normalized_dump)
        if len(ngrams) < 3:
            return None
        properties = node.properties
        profile = {
            "algorithm": PROFILE_ALGORITHM,
            "bucket": self._bucket(node),
            "structure_hash": structure_hash,
            "ngrams": sorted(ngrams)[: self.MAX_NGRAMS],
            "call_targets": sorted(call_targets),
            "resource_targets": sorted(resource_targets),
            "param_count": (
                len(properties.get("params", []))
                if isinstance(properties.get("params"), list)
                else 0
            ),
            "returns": properties.get("returns"),
            "async": bool(properties.get("async")),
        }
        node.properties["similarity"] = profile
        return _Profile(
            node=node,
            bucket=str(profile["bucket"]),
            structure_hash=structure_hash,
            ngrams=set(profile["ngrams"]),
            call_targets=call_targets,
            resource_targets=resource_targets,
            profile=profile,
        )

    @staticmethod
    def _function_asts(parsed_files: dict[str, ast.Module]) -> dict[str, ast.AST]:
        functions: dict[str, ast.AST] = {}
        for tree in parsed_files.values():
            module = getattr(tree, "_arcgraph_module", None)
            if not isinstance(module, str):
                continue
            for stmt in tree.body:
                if isinstance(stmt, ast.ClassDef):
                    class_qualname = f"{module}.{stmt.name}"
                    for child in stmt.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            functions[method_id(f"{class_qualname}.{child.name}")] = (
                                child
                            )
                elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions[function_id(f"{module}.{stmt.name}")] = stmt
        return functions

    @staticmethod
    def _edge_features(
        edges: list[Edge],
    ) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        call_targets: dict[str, set[str]] = defaultdict(set)
        resource_targets: dict[str, set[str]] = defaultdict(set)
        for edge in edges:
            if edge.kind == "calls":
                call_targets[edge.source].add(edge.target)
            elif edge.kind in SimilarityAnalyzer.RESOURCE_KINDS:
                resource_targets[edge.source].add(edge.target)
        return call_targets, resource_targets

    @staticmethod
    def _bucket(node: Node) -> str:
        path_package = ""
        if node.path:
            path_package = node.path.rsplit("/", 1)[0] if "/" in node.path else ""
        params = node.properties.get("params")
        param_count = len(params) if isinstance(params, list) else 0
        returns = node.properties.get("returns") or ""
        async_flag = "async" if node.properties.get("async") else "sync"
        return "|".join(
            [path_package, node.kind, str(param_count), str(returns), async_flag]
        )

    @classmethod
    def _ngrams(cls, normalized_dump: str) -> set[str]:
        tokens = re.findall(r"[A-Za-z_]+", normalized_dump.lower())
        if len(tokens) < 3:
            return set(tokens)
        ngrams = {
            " ".join(tokens[index : index + 3]) for index in range(len(tokens) - 2)
        }
        return set(sorted(ngrams)[: cls.MAX_NGRAMS])

    @staticmethod
    def _score(source: _Profile, target: _Profile) -> tuple[float, list[str]]:
        token_score = _jaccard(source.ngrams, target.ngrams)
        call_score = _jaccard(source.call_targets, target.call_targets)
        resource_score = _jaccard(source.resource_targets, target.resource_targets)
        score = token_score
        reasons = [f"token_jaccard={token_score:.2f}"]

        if source.structure_hash == target.structure_hash:
            score = max(score, 0.95)
            reasons.append("same_structure")
        if source.call_targets or target.call_targets:
            score += (1 - score) * 0.15 * call_score
            reasons.append(f"call_overlap={call_score:.2f}")
        if source.resource_targets or target.resource_targets:
            score += (1 - score) * 0.15 * resource_score
            reasons.append(f"resource_overlap={resource_score:.2f}")
        return round(score, 4), reasons

    @staticmethod
    def _edge(
        source: _Profile,
        target: _Profile,
        *,
        score: float,
        reasons: list[str],
        bucket: str,
    ) -> Edge:
        return Edge(
            source=source.node.id,
            target=target.node.id,
            kind="similar_to",
            confidence="inferred",
            evidence=[
                Evidence(
                    kind="ast_similarity",
                    path=source.node.path,
                    start_line=source.node.start_line,
                    end_line=source.node.end_line,
                    detail=f"score={score}; " + "; ".join(reasons),
                )
            ],
            properties={
                "score": score,
                "reasons": reasons,
                "bucket": bucket,
                "algorithm": PROFILE_ALGORITHM,
                "source_structure_hash": source.structure_hash,
                "target_structure_hash": target.structure_hash,
            },
        )


class _NormalizedAst(ast.NodeTransformer):
    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        node.name = "_function"
        node.decorator_list = []
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        node.name = "_function"
        node.decorator_list = []
        self.generic_visit(node)
        return node

    def visit_arg(self, node: ast.arg) -> ast.AST:
        node.arg = "_arg"
        self.generic_visit(node)
        return node

    def visit_Name(self, node: ast.Name) -> ast.AST:
        node.id = "_name"
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        self.generic_visit(node)
        node.attr = "_attr"
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        node.value = f"_{type(node.value).__name__}"
        return node


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
