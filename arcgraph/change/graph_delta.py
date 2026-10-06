"""Deterministic, streaming graph comparison projections for Change Safety."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass
import heapq
import json
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, TypeVar

from arcgraph.change.contracts import (
    COMPARISON_PROJECTION_VERSION,
    BuildIdentity,
    ChangedPath,
    ChangedRegion,
    CodeIdentity,
    GraphDelta,
    canonical_json,
    stable_projection,
)
from arcgraph.change.errors import GraphDeltaIncomplete, StableIdentityCollision
from arcgraph.change.identities import (
    assert_current_build_matches_code,
    diagnostic_lifecycle_projection,
    diagnostic_semantic_projection,
    stable_edge_identity,
    stable_diagnostic_identity,
    stable_node_identity,
)
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.schemas import Edge, Evidence, Node, SemanticDiagnostic

ValueT = TypeVar("ValueT")


@dataclass(slots=True)
class _DeltaAccumulator:
    max_findings: int | None
    identity_changes: list[dict[str, Any]]
    semantic_changes: list[dict[str, Any]]
    implementation_changes: list[dict[str, Any]]
    location_changes: list[dict[str, Any]]
    evidence_changes: list[dict[str, Any]]
    evidence_location_changes: list[dict[str, Any]]
    lifecycle_metadata_changes: list[dict[str, Any]]
    unknowns: list[dict[str, Any]]
    truncated: bool = False

    def add(self, collection: list[dict[str, Any]], value: dict[str, Any]) -> None:
        if self.max_findings is not None and self.count >= self.max_findings:
            self.truncated = True
            return
        collection.append(value)

    @property
    def count(self) -> int:
        return sum(
            len(collection)
            for collection in (
                self.identity_changes,
                self.semantic_changes,
                self.implementation_changes,
                self.location_changes,
                self.evidence_changes,
                self.evidence_location_changes,
                self.lifecycle_metadata_changes,
                self.unknowns,
            )
        )


class _NodeIdentityIndex:
    """Disk-backed, snapshot-specific lookup for validated Node identities.

    Edge streams are ordered by source/target while Node streams are ordered by
    node id, so a streaming merge cannot safely resolve arbitrary target
    endpoints with an in-memory full graph.  This temporary SQLite index keeps
    the binding explicit without allowing an unbounded Python object graph.
    """

    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path)
        self._connection.execute("PRAGMA journal_mode=OFF")
        self._connection.execute("PRAGMA synchronous=OFF")
        self._connection.execute("PRAGMA temp_store=FILE")
        self._connection.execute("PRAGMA cache_size=-2048")
        self._connection.execute("""
            CREATE TABLE node_identities (
                snapshot TEXT NOT NULL,
                node_id TEXT NOT NULL,
                stable_identity TEXT NOT NULL,
                stability_profile TEXT NOT NULL,
                node_payload TEXT NOT NULL,
                PRIMARY KEY (snapshot, node_id),
                UNIQUE (snapshot, stable_identity)
            ) WITHOUT ROWID
            """)
        self._connection.execute("""
            CREATE TABLE edge_identities (
                snapshot TEXT NOT NULL,
                source_identity TEXT NOT NULL,
                target_identity TEXT NOT NULL,
                edge_kind TEXT NOT NULL,
                semantic_role TEXT NOT NULL,
                edge_payload TEXT NOT NULL,
                PRIMARY KEY (
                    snapshot,
                    source_identity,
                    target_identity,
                    edge_kind,
                    semantic_role
                )
            ) WITHOUT ROWID
            """)
        self._connection.execute("""
            CREATE TABLE embedded_identities (
                snapshot TEXT NOT NULL,
                collection TEXT NOT NULL,
                raw_id TEXT NOT NULL,
                semantic_subject TEXT NOT NULL,
                PRIMARY KEY (snapshot, collection, raw_id)
            ) WITHOUT ROWID
            """)

    def add_snapshot(
        self,
        snapshot: str,
        repo_id: str,
        nodes: Iterable[Node],
    ) -> list[dict[str, Any]]:
        unknowns: list[dict[str, Any]] = []
        with self._connection:
            for node in nodes:
                try:
                    identity = stable_node_identity(repo_id, node)
                except StableIdentityCollision as exc:
                    unknowns.append(
                        {
                            "code": "NODE_IDENTITY_UNRESOLVED",
                            "reason": (
                                f"{snapshot} graph node lacks a validated stable "
                                f"identity: {exc}"
                            ),
                            "snapshot": snapshot,
                            "node_id": node.id,
                            "blocking": True,
                        }
                    )
                    continue
                stable_identity = str(identity["stable_identity"])
                profile = canonical_json(identity["stability_profile"])
                payload = canonical_json(
                    stable_projection(node.model_dump(mode="json"))
                )
                try:
                    self._connection.execute(
                        """
                        INSERT INTO node_identities (
                            snapshot, node_id, stable_identity, stability_profile,
                            node_payload
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (snapshot, node.id, stable_identity, profile, payload),
                    )
                    self._add_embedded_identities(snapshot, node)
                except sqlite3.IntegrityError as exc:
                    existing_by_id = self._connection.execute(
                        """
                        SELECT stable_identity, stability_profile
                        FROM node_identities
                        WHERE snapshot = ? AND node_id = ?
                        """,
                        (snapshot, node.id),
                    ).fetchone()
                    existing_by_identity = self._connection.execute(
                        """
                        SELECT node_id, stability_profile
                        FROM node_identities
                        WHERE snapshot = ? AND stable_identity = ?
                        """,
                        (snapshot, stable_identity),
                    ).fetchone()
                    existing_profile = (
                        existing_by_id[1]
                        if existing_by_id is not None
                        else (
                            existing_by_identity[1]
                            if existing_by_identity is not None
                            else None
                        )
                    )
                    if existing_profile is not None and existing_profile != profile:
                        raise GraphDeltaIncomplete(
                            "graph contains a stable identity with conflicting "
                            "witnesses"
                        ) from exc
                    raise GraphDeltaIncomplete(
                        "graph contains multiple records with the same "
                        "comparison identity"
                    ) from exc
        return unknowns

    def _add_embedded_identities(self, snapshot: str, node: Node) -> None:
        for collection, id_field in _EMBEDDED_ID_FIELD_BY_COLLECTION.items():
            records = node.properties.get(collection, [])
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                raw_id = record.get(id_field)
                if not isinstance(raw_id, str) or not raw_id:
                    continue
                subject = canonical_json(
                    _embedded_record_semantic_subject(collection, record)
                )
                existing = self._connection.execute(
                    """
                    SELECT semantic_subject
                    FROM embedded_identities
                    WHERE snapshot = ? AND collection = ? AND raw_id = ?
                    """,
                    (snapshot, collection, raw_id),
                ).fetchone()
                if existing is not None:
                    if existing[0] != subject:
                        raise GraphDeltaIncomplete(
                            "embedded analyzer identity has conflicting semantic "
                            "subjects"
                        )
                    continue
                self._connection.execute(
                    """
                    INSERT INTO embedded_identities (
                        snapshot, collection, raw_id, semantic_subject
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (snapshot, collection, raw_id, subject),
                )

    def resolve_embedded(
        self,
        snapshot: str,
        collection: str,
        raw_id: str,
    ) -> Any:
        row = self._connection.execute(
            """
            SELECT semantic_subject
            FROM embedded_identities
            WHERE snapshot = ? AND collection = ? AND raw_id = ?
            """,
            (snapshot, collection, raw_id),
        ).fetchone()
        if row is None:
            raise GraphDeltaIncomplete(
                "embedded analyzer relationship references an identity without "
                "a semantic subject"
            )
        try:
            return json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as exc:
            raise GraphDeltaIncomplete(
                "temporary embedded identity index is corrupt"
            ) from exc

    def iter_nodes(self, snapshot: str) -> Iterator[Node]:
        rows = self._connection.execute(
            """
            SELECT node_payload
            FROM node_identities
            WHERE snapshot = ?
            ORDER BY stable_identity
            """,
            (snapshot,),
        )
        for row in rows:
            try:
                yield Node.model_validate(json.loads(row[0]))
            except Exception as exc:
                raise GraphDeltaIncomplete(
                    "temporary node identity index is corrupt"
                ) from exc

    def resolve(self, snapshot: str, node_id: str) -> str:
        row = self._connection.execute(
            """
            SELECT stable_identity
            FROM node_identities
            WHERE snapshot = ? AND node_id = ?
            """,
            (snapshot, node_id),
        ).fetchone()
        if row is None:
            raise GraphDeltaIncomplete(
                "edge identity references a node without a validated stable identity"
            )
        return str(row[0])

    def add_edges(
        self,
        snapshot: str,
        repo_id: str,
        edges: Iterable[Edge],
    ) -> list[dict[str, Any]]:
        unknowns: list[dict[str, Any]] = []
        with self._connection:
            for edge in edges:
                try:
                    source_identity = self.resolve(snapshot, edge.source)
                    target_identity = self.resolve(snapshot, edge.target)
                    identity = stable_edge_identity(
                        repo_id,
                        edge,
                        source_identity=source_identity,
                        target_identity=target_identity,
                    )
                except (GraphDeltaIncomplete, StableIdentityCollision) as exc:
                    unknowns.append(
                        {
                            "code": "EDGE_IDENTITY_UNRESOLVED",
                            "reason": (
                                f"{snapshot} graph edge lacks validated endpoint "
                                f"identities: {exc}"
                            ),
                            "snapshot": snapshot,
                            "source": edge.source,
                            "target": edge.target,
                            "blocking": True,
                        }
                    )
                    continue
                payload = canonical_json(
                    stable_projection(edge.model_dump(mode="json"))
                )
                try:
                    self._connection.execute(
                        """
                        INSERT INTO edge_identities (
                            snapshot, source_identity, target_identity, edge_kind,
                            semantic_role, edge_payload
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            snapshot,
                            str(identity["source"]),
                            str(identity["target"]),
                            edge.kind,
                            edge.semantic_role or "",
                            payload,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise GraphDeltaIncomplete(
                        "graph contains multiple records with the same edge "
                        "comparison identity"
                    ) from exc
        return unknowns

    def iter_edges(self, snapshot: str) -> Iterator[Edge]:
        rows = self._connection.execute(
            """
            SELECT edge_payload
            FROM edge_identities
            WHERE snapshot = ?
            ORDER BY source_identity, target_identity, edge_kind, semantic_role
            """,
            (snapshot,),
        )
        for row in rows:
            try:
                yield Edge.model_validate(json.loads(row[0]))
            except Exception as exc:
                raise GraphDeltaIncomplete(
                    "temporary edge identity index is corrupt"
                ) from exc

    def close(self) -> None:
        self._connection.close()


class GraphDeltaEngine:
    """Compare published builds through fixed, versioned field projections."""

    def __init__(
        self,
        *,
        comparison_projection_version: str = COMPARISON_PROJECTION_VERSION,
        max_findings: int | None = None,
        diagnostic_sort_chunk_size: int = 2048,
    ) -> None:
        if comparison_projection_version != COMPARISON_PROJECTION_VERSION:
            raise GraphDeltaIncomplete(
                "comparison projection version is unsupported; explicit migration is required"
            )
        if diagnostic_sort_chunk_size < 1:
            raise GraphDeltaIncomplete(
                "diagnostic sort chunk size must be a positive bounded value"
            )
        self.comparison_projection_version = comparison_projection_version
        self.max_findings = max_findings
        self.diagnostic_sort_chunk_size = diagnostic_sort_chunk_size

    def compare(
        self,
        *,
        repo_id: str,
        baseline_reader: GraphStoreReader,
        current_reader: GraphStoreReader,
        baseline_build_identity: BuildIdentity,
        baseline_source_identity: dict[str, Any],
        current_build_identity: BuildIdentity,
        current_code_identity: CodeIdentity,
        changed_paths: list[ChangedPath] | None = None,
        changed_regions: list[ChangedRegion] | None = None,
        protected_surface_changes: list[dict[str, Any]] | None = None,
    ) -> GraphDelta:
        """Build a complete delta or explicitly mark truncation as unsafe."""

        _validate_repo_ids(
            repo_id,
            baseline_build_identity,
            current_build_identity,
            current_code_identity,
            *(changed_paths or []),
            *(changed_regions or []),
        )
        if baseline_source_identity.get("repo_id") != repo_id:
            raise GraphDeltaIncomplete(
                "baseline source identity does not belong to the requested repository"
            )
        assert_current_build_matches_code(current_build_identity, current_code_identity)
        accumulator = _DeltaAccumulator(
            max_findings=self.max_findings,
            identity_changes=[],
            semantic_changes=[],
            implementation_changes=[],
            location_changes=[],
            evidence_changes=[],
            evidence_location_changes=[],
            lifecycle_metadata_changes=[],
            unknowns=[],
        )
        # Edge identities must derive from the same validated Node identities
        # used by the node comparison.  The temporary index makes that binding
        # available to both endpoint streams without materializing the graph.
        with tempfile.TemporaryDirectory(prefix="arcgraph-change-nodes-") as raw_dir:
            node_identities = _NodeIdentityIndex(
                Path(raw_dir) / "node-identities.sqlite"
            )
            try:
                for unknown in node_identities.add_snapshot(
                    "baseline", repo_id, baseline_reader.iter_nodes()
                ):
                    accumulator.add(accumulator.unknowns, unknown)
                for unknown in node_identities.add_snapshot(
                    "current", repo_id, current_reader.iter_nodes()
                ):
                    accumulator.add(accumulator.unknowns, unknown)
                for unknown in node_identities.add_edges(
                    "baseline", repo_id, baseline_reader.iter_edges()
                ):
                    accumulator.add(accumulator.unknowns, unknown)
                for unknown in node_identities.add_edges(
                    "current", repo_id, current_reader.iter_edges()
                ):
                    accumulator.add(accumulator.unknowns, unknown)
                self._compare_nodes(
                    repo_id,
                    _unique_by_key(
                        node_identities.iter_nodes("baseline"),
                        lambda node: _node_key(repo_id, node),
                        profile_fn=lambda node: stable_node_identity(repo_id, node)[
                            "stability_profile"
                        ],
                    ),
                    _unique_by_key(
                        node_identities.iter_nodes("current"),
                        lambda node: _node_key(repo_id, node),
                        profile_fn=lambda node: stable_node_identity(repo_id, node)[
                            "stability_profile"
                        ],
                    ),
                    accumulator,
                    node_identities,
                )
                self._compare_edges(
                    repo_id,
                    _unique_by_key(
                        node_identities.iter_edges("baseline"),
                        lambda edge: _edge_key(
                            repo_id, edge, node_identities, "baseline"
                        ),
                    ),
                    _unique_by_key(
                        node_identities.iter_edges("current"),
                        lambda edge: _edge_key(
                            repo_id, edge, node_identities, "current"
                        ),
                    ),
                    accumulator,
                    node_identities,
                )
            finally:
                node_identities.close()
        self._compare_diagnostics(
            repo_id,
            _unique_by_key(
                _externally_sorted_diagnostics(
                    repo_id,
                    baseline_reader.iter_diagnostics(),
                    chunk_size=self.diagnostic_sort_chunk_size,
                ),
                lambda diagnostic: _diagnostic_sort_key(repo_id, diagnostic),
                profile_fn=lambda diagnostic: stable_diagnostic_identity(
                    repo_id, diagnostic
                )["stability_profile"],
            ),
            _unique_by_key(
                _externally_sorted_diagnostics(
                    repo_id,
                    current_reader.iter_diagnostics(),
                    chunk_size=self.diagnostic_sort_chunk_size,
                ),
                lambda diagnostic: _diagnostic_sort_key(repo_id, diagnostic),
                profile_fn=lambda diagnostic: stable_diagnostic_identity(
                    repo_id, diagnostic
                )["stability_profile"],
            ),
            accumulator,
        )
        self._implementation_changes(changed_regions or [], accumulator)
        self._mapped_node_identity_moves(changed_regions or [], accumulator)
        if accumulator.truncated:
            accumulator.unknowns.append(
                {
                    "code": "GRAPH_DELTA_INCOMPLETE",
                    "reason": "configured delta finding limit was reached",
                    "blocking": True,
                }
            )
        return GraphDelta(
            repo_id=repo_id,
            baseline_build_identity=baseline_build_identity,
            baseline_source_identity=stable_projection(baseline_source_identity),
            current_build_identity=current_build_identity,
            current_code_identity=current_code_identity,
            comparison_projection_version=self.comparison_projection_version,
            changed_paths=changed_paths or [],
            changed_regions=changed_regions or [],
            identity_changes=_sorted_records(accumulator.identity_changes),
            semantic_changes=_sorted_records(accumulator.semantic_changes),
            implementation_changes=_sorted_records(accumulator.implementation_changes),
            location_changes=_sorted_records(accumulator.location_changes),
            evidence_changes=_sorted_records(accumulator.evidence_changes),
            evidence_location_changes=_sorted_records(
                accumulator.evidence_location_changes
            ),
            lifecycle_metadata_changes=_sorted_records(
                accumulator.lifecycle_metadata_changes
            ),
            protected_surface_changes=_sorted_records(protected_surface_changes or []),
            unknowns=_sorted_records(accumulator.unknowns),
            truncation={
                "truncated": accumulator.truncated,
                "max_findings": self.max_findings,
                "complete": not accumulator.truncated,
            },
            warnings=(["GRAPH_DELTA_INCOMPLETE"] if accumulator.truncated else []),
        )

    def _compare_nodes(
        self,
        repo_id: str,
        baseline: Iterable[Node],
        current: Iterable[Node],
        accumulator: _DeltaAccumulator,
        node_identities: _NodeIdentityIndex,
    ) -> None:
        for relation, old, new in _merge_by_key(
            baseline, current, lambda node: _node_key(repo_id, node)
        ):
            if relation == "removed":
                accumulator.add(
                    accumulator.identity_changes,
                    _identity_change(repo_id, "node", "removed", old),
                )
                continue
            if relation == "added":
                accumulator.add(
                    accumulator.identity_changes,
                    _identity_change(repo_id, "node", "added", new),
                )
                continue
            if old is None or new is None:
                raise GraphDeltaIncomplete("node comparison merge invariant failed")
            old_semantic = _node_semantic_projection(
                old,
                relationship_resolver=lambda collection, raw_id: (
                    node_identities.resolve_embedded("baseline", collection, raw_id)
                ),
            )
            new_semantic = _node_semantic_projection(
                new,
                relationship_resolver=lambda collection, raw_id: (
                    node_identities.resolve_embedded("current", collection, raw_id)
                ),
            )
            if old_semantic != new_semantic:
                accumulator.add(
                    accumulator.semantic_changes,
                    {
                        "entity_type": "node",
                        "stable_id": _node_key(repo_id, old),
                        "before": old_semantic,
                        "after": new_semantic,
                    },
                )
            old_location = _node_location_projection(old)
            new_location = _node_location_projection(new)
            if old_location != new_location:
                accumulator.add(
                    accumulator.location_changes,
                    {
                        "entity_type": "node",
                        "stable_id": _node_key(repo_id, old),
                        "before": old_location,
                        "after": new_location,
                    },
                )

    def _compare_edges(
        self,
        repo_id: str,
        baseline: Iterable[Edge],
        current: Iterable[Edge],
        accumulator: _DeltaAccumulator,
        node_identities: _NodeIdentityIndex,
    ) -> None:
        for relation, old, new in _merge_by_key(
            baseline,
            current,
            lambda edge: _edge_key(repo_id, edge, node_identities, "baseline"),
            lambda edge: _edge_key(repo_id, edge, node_identities, "current"),
        ):
            if relation == "removed":
                accumulator.add(
                    accumulator.identity_changes,
                    {
                        "entity_type": "edge",
                        "stable_id": _edge_stable_id(
                            repo_id, old, node_identities, "baseline"
                        ),
                        "change": "removed",
                        "projection": _edge_identity_projection(
                            repo_id, old, node_identities, "baseline"
                        ),
                    },
                )
                continue
            if relation == "added":
                accumulator.add(
                    accumulator.identity_changes,
                    {
                        "entity_type": "edge",
                        "stable_id": _edge_stable_id(
                            repo_id, new, node_identities, "current"
                        ),
                        "change": "added",
                        "projection": _edge_identity_projection(
                            repo_id, new, node_identities, "current"
                        ),
                    },
                )
                continue
            if old is None or new is None:
                raise GraphDeltaIncomplete("edge comparison merge invariant failed")
            old_semantic = _edge_semantic_projection(old)
            new_semantic = _edge_semantic_projection(new)
            if old_semantic != new_semantic:
                accumulator.add(
                    accumulator.semantic_changes,
                    {
                        "entity_type": "edge",
                        "stable_id": _edge_stable_id(
                            repo_id, old, node_identities, "baseline"
                        ),
                        "before": old_semantic,
                        "after": new_semantic,
                    },
                )
            old_evidence_semantic = _evidence_semantic_projection(old.evidence)
            new_evidence_semantic = _evidence_semantic_projection(new.evidence)
            if old_evidence_semantic != new_evidence_semantic:
                accumulator.add(
                    accumulator.evidence_changes,
                    {
                        "entity_type": "edge",
                        "stable_id": _edge_stable_id(
                            repo_id, old, node_identities, "baseline"
                        ),
                        "before": old_evidence_semantic,
                        "after": new_evidence_semantic,
                    },
                )
            old_evidence_location = _evidence_location_projection(old.evidence)
            new_evidence_location = _evidence_location_projection(new.evidence)
            if old_evidence_location != new_evidence_location:
                accumulator.add(
                    accumulator.evidence_location_changes,
                    {
                        "entity_type": "edge",
                        "stable_id": _edge_stable_id(
                            repo_id, old, node_identities, "baseline"
                        ),
                        "before": old_evidence_location,
                        "after": new_evidence_location,
                    },
                )

    def _compare_diagnostics(
        self,
        repo_id: str,
        baseline: Iterable[SemanticDiagnostic],
        current: Iterable[SemanticDiagnostic],
        accumulator: _DeltaAccumulator,
    ) -> None:
        for relation, old, new in _merge_by_key(
            baseline,
            current,
            lambda diagnostic: _diagnostic_sort_key(repo_id, diagnostic),
        ):
            if relation == "removed":
                identity = stable_diagnostic_identity(repo_id, old)
                accumulator.add(
                    accumulator.identity_changes,
                    {
                        "entity_type": "diagnostic",
                        "stable_id": identity["stable_diagnostic_id"],
                        "change": "removed",
                        "projection": identity["stability_profile"],
                    },
                )
                continue
            if relation == "added":
                identity = stable_diagnostic_identity(repo_id, new)
                accumulator.add(
                    accumulator.identity_changes,
                    {
                        "entity_type": "diagnostic",
                        "stable_id": identity["stable_diagnostic_id"],
                        "change": "added",
                        "projection": identity["stability_profile"],
                    },
                )
                continue
            if old is None or new is None:
                raise GraphDeltaIncomplete(
                    "diagnostic comparison merge invariant failed"
                )
            old_semantic = diagnostic_semantic_projection(old)
            new_semantic = diagnostic_semantic_projection(new)
            if old_semantic != new_semantic:
                identity = stable_diagnostic_identity(repo_id, old)
                accumulator.add(
                    accumulator.semantic_changes,
                    {
                        "entity_type": "diagnostic",
                        "stable_id": identity["stable_diagnostic_id"],
                        "before": old_semantic,
                        "after": new_semantic,
                    },
                )
            old_lifecycle = _diagnostic_lifecycle_projection(old)
            new_lifecycle = _diagnostic_lifecycle_projection(new)
            if old_lifecycle != new_lifecycle:
                identity = stable_diagnostic_identity(repo_id, old)
                accumulator.add(
                    accumulator.lifecycle_metadata_changes,
                    {
                        "entity_type": "diagnostic",
                        "stable_id": identity["stable_diagnostic_id"],
                        "before": old_lifecycle,
                        "after": new_lifecycle,
                    },
                )

    def _implementation_changes(
        self,
        regions: list[ChangedRegion],
        accumulator: _DeltaAccumulator,
    ) -> None:
        records: list[dict[str, Any]] = []
        for region in regions:
            baseline_digest = region.baseline_normalized_implementation_digest
            current_digest = region.current_normalized_implementation_digest
            if baseline_digest is not None and current_digest is not None:
                if baseline_digest != current_digest:
                    records.append(
                        {
                            "path": region.path,
                            "path_comparison_key": region.path_comparison_key,
                            "old_path": region.old_path,
                            "old_path_comparison_key": (region.old_path_comparison_key),
                            "new_path": region.new_path,
                            "new_path_comparison_key": (region.new_path_comparison_key),
                            "baseline_symbol_id": region.baseline_symbol_id,
                            "current_symbol_id": region.current_symbol_id,
                            "classification": region.classification,
                            "before": baseline_digest,
                            "after": current_digest,
                            "source_normalization_projection_version": (
                                region.source_normalization_projection_version
                            ),
                        },
                    )
                continue
            if (
                region.classification in {"added", "deleted"}
                and region.baseline_raw_source_digest
                != region.current_raw_source_digest
            ):
                records.append(
                    {
                        "path": region.path,
                        "path_comparison_key": region.path_comparison_key,
                        "old_path": region.old_path,
                        "old_path_comparison_key": region.old_path_comparison_key,
                        "new_path": region.new_path,
                        "new_path_comparison_key": region.new_path_comparison_key,
                        "baseline_symbol_id": region.baseline_symbol_id,
                        "current_symbol_id": region.current_symbol_id,
                        "classification": region.classification,
                        "before": region.baseline_raw_source_digest,
                        "after": region.current_raw_source_digest,
                        "source_normalization_projection_version": (
                            region.source_normalization_projection_version
                        ),
                    },
                )
                continue
            raw_changed = (
                region.baseline_raw_source_digest != region.current_raw_source_digest
            )
            if raw_changed and region.classification not in {"added", "deleted"}:
                accumulator.add(
                    accumulator.unknowns,
                    {
                        "code": "SOURCE_NORMALIZATION_UNKNOWN",
                        "path": region.path,
                        "classification": region.classification,
                        "blocking": True,
                    },
                )
        for record in _coalesce_implementation_changes(records):
            accumulator.add(accumulator.implementation_changes, record)

    def _mapped_node_identity_moves(
        self,
        changed_regions: list[ChangedRegion],
        accumulator: _DeltaAccumulator,
    ) -> None:
        """Retain a mapped raw-id replacement as a blocking manual review.

        A frontend can change a raw symbol identifier when code moves between
        modules.  Without a separately proven paired identity, emitting only
        unrelated Added/Removed records would hide that uncertainty.
        """

        for region in changed_regions:
            if (
                region.mapping_status != "mapped"
                or not region.baseline_symbol_id
                or not region.current_symbol_id
                or region.baseline_symbol_id == region.current_symbol_id
            ):
                continue
            accumulator.add(
                accumulator.unknowns,
                {
                    "code": "NODE_IDENTITY_MOVE_REQUIRES_REVIEW",
                    "reason": (
                        "mapped changed region has different baseline and current "
                        "symbol identities"
                    ),
                    "baseline_symbol_id": region.baseline_symbol_id,
                    "current_symbol_id": region.current_symbol_id,
                    "path": region.path,
                    "blocking": True,
                },
            )


def _merge_by_key(
    baseline: Iterable[ValueT],
    current: Iterable[ValueT],
    key_fn: Callable[[ValueT], Any],
    current_key_fn: Callable[[ValueT], Any] | None = None,
) -> Iterator[tuple[str, ValueT | None, ValueT | None]]:
    """Perform a deterministic merge join over already sorted graph iterators."""

    current_key_fn = current_key_fn or key_fn
    old_iter = iter(baseline)
    new_iter = iter(current)
    old = next(old_iter, None)
    new = next(new_iter, None)
    while old is not None or new is not None:
        if old is None:
            yield "added", None, new
            new = next(new_iter, None)
        elif new is None:
            yield "removed", old, None
            old = next(old_iter, None)
        elif key_fn(old) == current_key_fn(new):
            yield "matched", old, new
            old = next(old_iter, None)
            new = next(new_iter, None)
        elif key_fn(old) < current_key_fn(new):
            yield "removed", old, None
            old = next(old_iter, None)
        else:
            yield "added", None, new
            new = next(new_iter, None)


def _unique_by_key(
    values: Iterable[ValueT],
    key_fn: Callable[[ValueT], Any],
    *,
    profile_fn: Callable[[ValueT], Any] | None = None,
) -> Iterator[ValueT]:
    """Reject a stable-identity collision before merge comparison can hide it."""

    previous_key: Any | None = None
    previous_profile: Any | None = None
    has_previous = False
    for value in values:
        current_key = key_fn(value)
        if has_previous and current_key == previous_key:
            if profile_fn is not None and profile_fn(value) != previous_profile:
                raise GraphDeltaIncomplete(
                    "graph contains a stable identity with conflicting witnesses"
                )
            raise GraphDeltaIncomplete(
                "graph contains multiple records with the same comparison identity"
            )
        previous_key = current_key
        previous_profile = profile_fn(value) if profile_fn is not None else None
        has_previous = True
        yield value


def _node_semantic_projection(
    node: Node,
    *,
    relationship_resolver: Callable[[str, str], Any] | None = None,
) -> dict[str, Any]:
    properties = _node_properties_semantic_projection(
        node.properties,
        relationship_resolver=relationship_resolver,
    )
    if "stable_callsite_subject" in properties:
        properties = {
            key: value for key, value in properties.items() if key != "callsite_id"
        }
    return {
        "kind": node.kind,
        "name": node.name,
        "qualname": node.qualname,
        "properties": stable_projection(properties),
    }


_POSITIONAL_NODE_RECORD_FIELDS = {
    "path",
    "line",
    "column",
    "start_line",
    "end_line",
}

_POSITION_DERIVED_FIELDS_BY_COLLECTION = {
    "bindings": {
        "binding_id",
        "shadows_binding_id",
        "type_ref_id",
    },
    "binding_diagnostics": {
        "binding_id",
        "shadowed_binding_id",
    },
    "type_refs": {
        "type_ref_id",
        "binding_id",
        "inferred_from_type_ref_id",
    },
    "type_diagnostics": {"binding_id"},
}

_EMBEDDED_ID_FIELD_BY_COLLECTION = {
    "bindings": "binding_id",
    "type_refs": "type_ref_id",
}

_EMBEDDED_RELATION_FIELDS_BY_COLLECTION = {
    "bindings": {
        "shadows_binding_id": ("bindings", "shadows_binding"),
        "type_ref_id": ("type_refs", "type_ref"),
    },
    "binding_diagnostics": {
        "binding_id": ("bindings", "binding"),
        "shadowed_binding_id": ("bindings", "shadowed_binding"),
    },
    "type_refs": {
        "binding_id": ("bindings", "binding"),
        "inferred_from_type_ref_id": (
            "type_refs",
            "inferred_from_type_ref",
        ),
    },
    "type_diagnostics": {
        "binding_id": ("bindings", "binding"),
    },
}


def _node_properties_semantic_projection(
    properties: dict[str, Any],
    *,
    relationship_resolver: Callable[[str, str], Any] | None = None,
) -> dict[str, Any]:
    """Remove location-derived identities from embedded analyzer records.

    Bindings and TypeRefs live inside ``Node.properties`` rather than as graph
    entities with their own location projection.  Their content-hash IDs
    include line/column, so comparing the raw records turns a pure line move
    into a semantic edit.  Normalize only these enumerated analyzer surfaces;
    graph-level identities and ordinary semantic IDs remain untouched.
    """

    projected = dict(properties)
    for collection in _POSITION_DERIVED_FIELDS_BY_COLLECTION:
        value = projected.get(collection)
        if not isinstance(value, list):
            continue
        records: list[Any] = []
        for item in value:
            if not isinstance(item, dict):
                records.append(item)
                continue
            record = _embedded_record_semantic_subject(collection, item)
            for raw_field, (
                target_collection,
                semantic_field,
            ) in _EMBEDDED_RELATION_FIELDS_BY_COLLECTION.get(collection, {}).items():
                raw_id = item.get(raw_field)
                if not isinstance(raw_id, str) or not raw_id:
                    continue
                if relationship_resolver is None:
                    raise GraphDeltaIncomplete(
                        "embedded analyzer relationship needs a semantic resolver"
                    )
                record[semantic_field] = relationship_resolver(
                    target_collection,
                    raw_id,
                )
            records.append(record)
        projected[collection] = sorted(
            (stable_projection(record) for record in records),
            key=canonical_json,
        )
    return projected


def _embedded_record_semantic_subject(
    collection: str,
    record: dict[str, Any],
) -> dict[str, Any]:
    derived_fields = _POSITION_DERIVED_FIELDS_BY_COLLECTION.get(collection, set())
    projected = {
        key: nested
        for key, nested in record.items()
        if key not in _POSITIONAL_NODE_RECORD_FIELDS and key not in derived_fields
    }
    evidence = record.get("evidence")
    if isinstance(evidence, dict):
        projected["evidence"] = {
            key: nested
            for key, nested in evidence.items()
            if key not in _POSITIONAL_NODE_RECORD_FIELDS
        }
    return stable_projection(projected)


def _node_location_projection(node: Node) -> dict[str, Any]:
    return {
        "path": node.path,
        "start_line": node.start_line,
        "end_line": node.end_line,
    }


def _identity_change(
    repo_id: str,
    entity_type: str,
    change: str,
    node: Node,
) -> dict[str, Any]:
    identity = stable_node_identity(repo_id, node)
    return {
        "entity_type": entity_type,
        "stable_id": identity["stable_identity"],
        "change": change,
        "projection": identity["stability_profile"],
    }


def _node_key(repo_id: str, node: Node) -> str:
    return str(stable_node_identity(repo_id, node)["stable_identity"])


def _edge_key(
    repo_id: str,
    edge: Edge,
    node_identities: _NodeIdentityIndex,
    snapshot: str,
) -> tuple[str, str, str, str]:
    identity = stable_edge_identity(
        repo_id,
        edge,
        source_identity=node_identities.resolve(snapshot, edge.source),
        target_identity=node_identities.resolve(snapshot, edge.target),
    )
    return (
        str(identity["source"]),
        str(identity["target"]),
        edge.kind,
        edge.semantic_role or "",
    )


def _edge_stable_id(
    repo_id: str,
    edge: Edge,
    node_identities: _NodeIdentityIndex,
    snapshot: str,
) -> str:
    return str(
        stable_edge_identity(
            repo_id,
            edge,
            source_identity=node_identities.resolve(snapshot, edge.source),
            target_identity=node_identities.resolve(snapshot, edge.target),
        )["stable_edge_id"]
    )


def _edge_identity_projection(
    repo_id: str,
    edge: Edge,
    node_identities: _NodeIdentityIndex,
    snapshot: str,
) -> dict[str, Any]:
    identity = stable_edge_identity(
        repo_id,
        edge,
        source_identity=node_identities.resolve(snapshot, edge.source),
        target_identity=node_identities.resolve(snapshot, edge.target),
    )
    return {
        "repo_id": repo_id,
        "source": identity["source"],
        "target": identity["target"],
        "kind": edge.kind,
        "semantic_role": edge.semantic_role,
    }


# Fields of a call's fact derived from where the call is, which a line shift
# changes and the call's meaning does not. A callsite_id is dropped only where
# a stable subject still says which call the fact is; a TypeScript fact has
# none, and its expression and arguments do.
_CALLSITE_LINE_KEYS = frozenset({"column", "line"})
_CALLSITE_POSITION_KEYS = _CALLSITE_LINE_KEYS | {"callsite_id"}


def _stable_callsite_fact(fact: Any) -> Any:
    if not isinstance(fact, dict):
        return fact
    dropped = (
        _CALLSITE_POSITION_KEYS
        if "stable_callsite_subject" in fact
        else _CALLSITE_LINE_KEYS
    )
    return {key: value for key, value in fact.items() if key not in dropped}


def _edge_semantic_projection(edge: Edge) -> dict[str, Any]:
    resolution = edge.resolution.model_dump(mode="json")
    properties = edge.properties
    callsite = properties.get("callsite")
    if isinstance(callsite, dict) and "stable_callsite_subject" in callsite:
        resolution = {
            key: value for key, value in resolution.items() if key != "callsite_id"
        }
    if isinstance(callsite, dict):
        properties = {**properties, "callsite": _stable_callsite_fact(callsite)}
    facts = properties.get("callsites")
    if isinstance(facts, list):
        # Every call a merged edge stands for, as its first call is above.
        properties = {
            **properties,
            "callsites": [_stable_callsite_fact(fact) for fact in facts],
        }
    return {
        "confidence": edge.confidence,
        "resolution": resolution,
        "confidence_sources": stable_projection(edge.confidence_sources),
        "properties": stable_projection(properties),
    }


def _evidence_semantic_projection(evidence: list[Evidence]) -> list[dict[str, Any]]:
    return _sorted_records(
        [
            {
                "kind": item.kind,
                "detail": item.detail,
                "snippet": item.snippet,
            }
            for item in evidence
        ]
    )


def _evidence_location_projection(evidence: list[Evidence]) -> list[dict[str, Any]]:
    return _sorted_records(
        [
            {
                "path": item.path,
                "start_line": item.start_line,
                "end_line": item.end_line,
                "column": item.column,
            }
            for item in evidence
        ]
    )


def _diagnostic_sort_key(repo_id: str, diagnostic: SemanticDiagnostic) -> str:
    return str(stable_diagnostic_identity(repo_id, diagnostic)["stable_diagnostic_id"])


def _externally_sorted_diagnostics(
    repo_id: str,
    diagnostics: Iterable[SemanticDiagnostic],
    *,
    chunk_size: int,
) -> Iterator[SemanticDiagnostic]:
    """Sort diagnostics by stable identity without unbounded graph loading.

    GraphStore's native diagnostic order is intentionally lifecycle-oriented,
    not Change-Safety identity order.  A bounded external merge sort retains
    the streaming comparison contract even when a repository has many
    diagnostics.
    """

    with tempfile.TemporaryDirectory(prefix="arcgraph-change-diagnostics-") as raw_dir:
        directory = Path(raw_dir)
        paths: list[Path] = []
        chunk: list[tuple[str, str, dict[str, Any]]] = []
        for diagnostic in diagnostics:
            identity = stable_diagnostic_identity(repo_id, diagnostic)
            payload = diagnostic.model_dump(mode="json")
            chunk.append(
                (
                    str(identity["stable_diagnostic_id"]),
                    canonical_json(stable_projection(payload)),
                    payload,
                )
            )
            if len(chunk) >= chunk_size:
                paths.append(_write_diagnostic_sort_chunk(directory, len(paths), chunk))
                chunk = []
        if chunk:
            paths.append(_write_diagnostic_sort_chunk(directory, len(paths), chunk))
        if not paths:
            return

        with ExitStack() as stack:
            handles = [
                stack.enter_context(path.open("r", encoding="utf-8")) for path in paths
            ]
            heap: list[tuple[str, str, int, int, SemanticDiagnostic]] = []
            sequence = 0
            for handle_index, handle in enumerate(handles):
                record = _next_diagnostic_sort_record(handle)
                if record is None:
                    continue
                key, tie_breaker, diagnostic = record
                heapq.heappush(
                    heap,
                    (key, tie_breaker, sequence, handle_index, diagnostic),
                )
                sequence += 1
            while heap:
                _key, _tie_breaker, _sequence, handle_index, diagnostic = heapq.heappop(
                    heap
                )
                yield diagnostic
                record = _next_diagnostic_sort_record(handles[handle_index])
                if record is None:
                    continue
                key, tie_breaker, next_diagnostic = record
                heapq.heappush(
                    heap,
                    (key, tie_breaker, sequence, handle_index, next_diagnostic),
                )
                sequence += 1


def _write_diagnostic_sort_chunk(
    directory: Path,
    sequence: int,
    chunk: list[tuple[str, str, dict[str, Any]]],
) -> Path:
    path = directory / f"{sequence:08d}.jsonl"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for key, tie_breaker, payload in sorted(chunk):
            handle.write(
                json.dumps(
                    {"key": key, "tie_breaker": tie_breaker, "payload": payload},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            handle.write("\n")
    return path


def _next_diagnostic_sort_record(
    handle: Any,
) -> tuple[str, str, SemanticDiagnostic] | None:
    raw = handle.readline()
    if not raw:
        return None
    try:
        record = json.loads(raw)
        key = record["key"]
        tie_breaker = record["tie_breaker"]
        payload = record["payload"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise GraphDeltaIncomplete("diagnostic sort spill record is corrupt") from exc
    if not isinstance(key, str) or not isinstance(tie_breaker, str):
        raise GraphDeltaIncomplete("diagnostic sort spill record has an invalid key")
    try:
        diagnostic = SemanticDiagnostic.model_validate(payload)
    except Exception as exc:
        raise GraphDeltaIncomplete("diagnostic sort spill record is invalid") from exc
    return key, tie_breaker, diagnostic


def _diagnostic_lifecycle_projection(diagnostic: SemanticDiagnostic) -> dict[str, Any]:
    return diagnostic_lifecycle_projection(diagnostic)


def _sorted_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {
        canonical_json(stable_projection(record)): stable_projection(record)
        for record in records
    }
    return [unique[key] for key in sorted(unique)]


def _coalesce_implementation_changes(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Emit one full-symbol implementation change across split Git hunks."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        baseline_symbol = record.get("baseline_symbol_id")
        current_symbol = record.get("current_symbol_id")
        if isinstance(baseline_symbol, str) and isinstance(current_symbol, str):
            key = canonical_json(
                {
                    "baseline_symbol_id": baseline_symbol,
                    "current_symbol_id": current_symbol,
                    "before": record.get("before"),
                    "after": record.get("after"),
                    "source_normalization_projection_version": record.get(
                        "source_normalization_projection_version"
                    ),
                }
            )
        else:
            key = canonical_json(stable_projection(record))
        grouped.setdefault(key, []).append(record)

    result: list[dict[str, Any]] = []
    for key in sorted(grouped):
        group = sorted(grouped[key], key=canonical_json)
        merged = dict(stable_projection(group[0]))
        classifications = {str(item.get("classification")) for item in group}
        if {"added", "deleted"} <= classifications:
            merged["classification"] = "moved_and_modified"
        elif "renamed" in classifications:
            merged["classification"] = "renamed"
        elif len(classifications) > 1:
            merged["classification"] = "modified"

        for field in (
            "old_path",
            "old_path_comparison_key",
            "new_path",
            "new_path_comparison_key",
        ):
            values = sorted(
                {
                    str(item[field])
                    for item in group
                    if isinstance(item.get(field), str) and item[field]
                }
            )
            if values:
                merged[field] = values[0]

        if isinstance(merged.get("new_path"), str):
            merged["path"] = merged["new_path"]
            if isinstance(merged.get("new_path_comparison_key"), str):
                merged["path_comparison_key"] = merged["new_path_comparison_key"]
        result.append(merged)
    return result


def _validate_repo_ids(repo_id: str, *records: Any) -> None:
    for record in records:
        if getattr(record, "repo_id", repo_id) != repo_id:
            raise GraphDeltaIncomplete("graph delta inputs cross repository ownership")
