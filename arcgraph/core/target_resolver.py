"""Deterministic, ambiguity-safe target resolution for every read surface."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

from arcgraph.core.ids import mcp_tool_id, normalize_symbol_query, route_id
from arcgraph.core.visual_contract import ENTRYPOINT_KINDS_SET

# Every suffix the scanner can index as a source file. A target ending in
# one of these is a path even without a separator, so a repository-root
# file resolves the same way a nested one does.
SOURCE_PATH_SUFFIXES = (
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
)

_CONCRETE_ROUTE_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
)
_WILDCARD_ROUTE_METHODS = ("ANY", "ALL")
_ROUTE_METHODS = _CONCRETE_ROUTE_METHODS | frozenset(_WILDCARD_ROUTE_METHODS)


class ResolutionPolicy(str, Enum):
    """Resolution cardinality and target-shape contract."""

    SYMBOL_ONE = "symbol_one"
    ANALYSIS_TARGET = "analysis_target"
    ENTRYPOINT_SET = "entrypoint_set"
    WORKER_SET = "worker_set"


@dataclass(frozen=True)
class TargetResolution:
    query: str
    status: str
    strategy: str | None
    resolved_ids: tuple[str, ...] = ()
    candidates: tuple[dict[str, Any], ...] = ()
    candidates_truncated: bool = False
    suggestions: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "status": self.status,
            "strategy": self.strategy,
            "resolved_ids": list(self.resolved_ids),
            "candidates": [dict(item) for item in self.candidates],
            "candidates_truncated": self.candidates_truncated,
            "suggestions": [dict(item) for item in self.suggestions],
        }


def parse_route_target(target: str) -> tuple[str, str] | None:
    """Parse the shared whitespace ``METHOD /path`` route form."""

    parts = target.split(maxsplit=1)
    if (
        len(parts) == 2
        and parts[0].upper() in _ROUTE_METHODS
        and parts[1].startswith("/")
    ):
        return parts[0].upper(), parts[1]
    return None


def is_entrypoint_target(target: str) -> bool:
    if target.startswith(("route:", "mcp_tool:", "mcp:", "worker:", "queue:")):
        return True
    return parse_route_target(target) is not None


def route_query_aliases(query: str) -> list[str]:
    """Return exact and wildcard route ids in stable precedence order."""

    method: str | None = None
    path: str | None = None
    if query.startswith("route:"):
        parts = query.split(":", 2)
        if len(parts) == 3 and parts[1].upper() in _ROUTE_METHODS and parts[2]:
            method = parts[1].upper()
            path = parts[2]
    else:
        parsed = parse_route_target(query)
        if parsed is not None:
            method, path = parsed
    if method is None or path is None:
        return []

    aliases = [route_id(method, path)]
    if method in _CONCRETE_ROUTE_METHODS:
        aliases.extend(route_id(wildcard, path) for wildcard in _WILDCARD_ROUTE_METHODS)
    else:
        aliases.extend(
            route_id(wildcard, path)
            for wildcard in _WILDCARD_ROUTE_METHODS
            if wildcard != method
        )
    return aliases


def entrypoint_query_aliases(query: str) -> list[str]:
    """Return stable-id aliases used by the entrypoint-set policy."""

    aliases = [query]
    if query.startswith("mcp:"):
        aliases.append(mcp_tool_id(query.split(":", 1)[1]))
    if not query.startswith(
        ("route:", "mcp_tool:", "mcp:", "worker:", "queue:", "cli:", "test:")
    ):
        aliases.append(mcp_tool_id(query))
    aliases.extend(route_query_aliases(query))
    return list(dict.fromkeys(aliases))


class TargetResolver:
    """Resolve targets without ever choosing one candidate from an ambiguous set."""

    def __init__(
        self, repo_root: str | Path | None, *, candidate_limit: int = 8
    ) -> None:
        self.repo_root = str(repo_root or "").replace("\\", "/").rstrip("/")
        self.candidate_limit = max(1, candidate_limit)
        self._indexed_suffixes: frozenset[str] | None = None

    def resolve(
        self,
        conn: Any,
        query: str,
        *,
        policy: ResolutionPolicy = ResolutionPolicy.ANALYSIS_TARGET,
    ) -> TargetResolution:
        normalized = query.strip()
        if not normalized:
            return self._unresolved(conn, query)

        if policy is ResolutionPolicy.ENTRYPOINT_SET:
            return self._resolve_entrypoint(conn, normalized)
        if policy is ResolutionPolicy.WORKER_SET:
            return self._resolve_worker(conn, normalized)

        route_resolution = self._resolve_route(conn, normalized)
        if route_resolution is not None:
            return route_resolution

        rows = self._rows(conn, "id = ?", (normalized,))
        if rows:
            return self._unique_or_ambiguous(normalized, "node_id", rows)

        path_like = self._looks_like_path(conn, normalized)
        if (
            policy is ResolutionPolicy.SYMBOL_ONE
            and path_like
            # Refuse a path-shaped target only when the index actually holds
            # that path. A qualname can end in an indexed suffix — a method
            # named `go` in a repository that indexes Go, or `py` in this
            # one — and that is a symbol, not a file.
            and self._rows(
                conn, "path = ?", (self._normalized_path(normalized),), limit=1
            )
        ):
            return self._unresolved(conn, normalized, suggestions=False)

        exact_qualname = self._rows(
            conn,
            "kind NOT IN ('external_symbol', 'protocol_symbol') AND qualname = ?",
            (normalized,),
            limit=self.candidate_limit + 1,
        )
        if exact_qualname:
            return self._unique_or_ambiguous(
                normalized, "exact_qualname", exact_qualname
            )

        normalized_ids = normalize_symbol_query(normalized)
        id_placeholders = ",".join("?" for _ in normalized_ids)
        normalized_rows = self._rows(
            conn,
            f"id IN ({id_placeholders})",
            tuple(normalized_ids),
            limit=self.candidate_limit + 1,
        )
        if normalized_rows:
            return self._unique_or_ambiguous(
                normalized, "normalized_node_id", normalized_rows
            )

        if path_like and policy is ResolutionPolicy.ANALYSIS_TARGET:
            path = self._normalized_path(normalized)
            path_rows = self._rows(conn, "path = ?", (path,))
            if path_rows:
                return self._resolved(normalized, "exact_path", path_rows)

        exact_name = self._rows(
            conn,
            "kind NOT IN ('external_symbol', 'protocol_symbol') AND name = ?",
            (normalized,),
            limit=self.candidate_limit + 1,
        )
        if exact_name:
            return self._unique_or_ambiguous(normalized, "exact_name", exact_name)

        suffix_rows = self._rows(
            conn,
            "kind NOT IN ('external_symbol', 'protocol_symbol') "
            "AND qualname IS NOT NULL "
            "AND substr(qualname, -(length(?) + 1)) = '.' || ?",
            (normalized, normalized),
            limit=self.candidate_limit + 1,
        )
        if suffix_rows:
            return self._unique_or_ambiguous(normalized, "qualname_suffix", suffix_rows)

        return self._unresolved(conn, normalized)

    def _resolve_entrypoint(self, conn: Any, query: str) -> TargetResolution:
        aliases = entrypoint_query_aliases(query)
        kinds = tuple(sorted(ENTRYPOINT_KINDS_SET))
        kind_placeholders = ",".join("?" for _ in kinds)
        alias_placeholders = ",".join("?" for _ in aliases)
        exact_rows = self._rows(
            conn,
            f"kind IN ({kind_placeholders}) AND id IN ({alias_placeholders})",
            (*kinds, *aliases),
        )
        by_id = {row["id"]: row for row in exact_rows if self._is_routable(row)}
        ordered = [by_id[alias] for alias in aliases if alias in by_id]
        if ordered:
            return self._resolved(query, "entrypoint_alias", ordered)

        rows = self._rows(
            conn,
            f"kind IN ({kind_placeholders}) AND (id = ? OR name = ? OR qualname = ?)",
            (*kinds, query, query, query),
        )
        rows = [row for row in rows if self._is_routable(row)]
        if rows:
            return self._resolved(query, "entrypoint_exact", rows)
        # Do not silently reinterpret a function as an entrypoint. Offer its
        # registered entrypoints instead, preserving multi-registration choice.
        handlers = self._rows(
            conn,
            "kind IN ('function', 'method') AND (id = ? OR qualname = ?)",
            (query, query),
        )
        related = []
        for handler in handlers:
            related.extend(
                self._rows(
                    conn,
                    f"kind IN ({kind_placeholders}) AND id IN (SELECT source FROM edges WHERE target = ? AND kind = 'invokes')",
                    (*kinds, handler["id"]),
                )
            )
        related = [row for row in related if self._is_routable(row)]
        if related:
            unique = {row["id"]: row for row in related}
            return TargetResolution(
                query=query,
                status="unresolved",
                strategy=None,
                suggestions=tuple(
                    self._candidate(unique[key])
                    for key in sorted(unique)[: self.candidate_limit]
                ),
            )
        return self._unresolved(conn, query)

    def _resolve_worker(self, conn: Any, query: str) -> TargetResolution:
        rows = self._rows(
            conn,
            "kind IN ('worker_task', 'queue') AND (id = ? OR name = ? OR qualname = ?)",
            (query, query, query),
        )
        matches = {row["id"]: row for row in rows}
        queue_match = any(
            row["kind"] == "queue" and query in {row["name"], row["qualname"]}
            for row in rows
        )
        if not query.startswith(("worker:", "queue:")) and (
            not matches or query.startswith("fn:") or queue_match
        ):
            for row in self._rows(conn, "kind = 'worker_task'", ()):
                try:
                    properties = json.loads(row.get("properties_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    properties = {}
                if query in {properties.get("queue_name"), properties.get("handler")}:
                    matches[row["id"]] = row
        if matches:
            return self._resolved(
                query,
                "worker_alias",
                [matches[node_id] for node_id in sorted(matches)],
            )
        return self._unresolved(conn, query)

    def _resolve_route(self, conn: Any, query: str) -> TargetResolution | None:
        aliases = route_query_aliases(query)
        if not aliases:
            return None
        placeholders = ",".join("?" for _ in aliases)
        rows = self._rows(conn, f"id IN ({placeholders})", tuple(aliases))
        by_id = {row["id"]: row for row in rows}
        routable = [
            by_id[node_id]
            for node_id in aliases
            if node_id in by_id and self._is_routable(by_id[node_id])
        ]
        if routable:
            return self._resolved(query, "route_alias", routable)
        # A known router-local route is deliberately not handed to generic id
        # resolution, because it is not an addressable URL entrypoint.
        return self._unresolved(conn, query)

    def _unique_or_ambiguous(
        self, query: str, strategy: str, rows: list[dict[str, Any]]
    ) -> TargetResolution:
        if len(rows) == 1:
            return self._resolved(query, strategy, rows)
        bounded = rows[: self.candidate_limit]
        return TargetResolution(
            query=query,
            status="ambiguous",
            strategy=strategy,
            candidates=tuple(self._candidate(row) for row in bounded),
            candidates_truncated=len(rows) > self.candidate_limit,
        )

    def _resolved(
        self, query: str, strategy: str, rows: list[dict[str, Any]]
    ) -> TargetResolution:
        return TargetResolution(
            query=query,
            status="resolved",
            strategy=strategy,
            resolved_ids=tuple(row["id"] for row in rows),
        )

    def _unresolved(
        self, conn: Any, query: str, *, suggestions: bool = True
    ) -> TargetResolution:
        matches = self._suggestions(conn, query) if suggestions and query else []
        return TargetResolution(
            query=query,
            status="unresolved",
            strategy=None,
            suggestions=tuple(self._candidate(row) for row in matches),
        )

    def _suggestions(self, conn: Any, query: str) -> list[dict[str, Any]]:
        parsed_route = parse_route_target(query)
        if parsed_route is None and query.startswith("route:"):
            parts = query.split(":", 2)
            if len(parts) == 3 and parts[1].upper() in _ROUTE_METHODS:
                parsed_route = (parts[1].upper(), parts[2])
        if parsed_route:
            _, path = parsed_route
            # Use exact addressable IDs rather than display names.
            aliases = [route_id(method, path) for method in sorted(_ROUTE_METHODS)]
            placeholders = ",".join("?" for _ in aliases)
            candidates = self._rows(conn, f"id IN ({placeholders})", tuple(aliases))
            matches = {row["id"]: row for row in candidates if self._is_routable(row)}
            if matches:
                return [matches[key] for key in sorted(matches)[: self.candidate_limit]]
        escaped = self._escape_like(query)
        pattern = f"%{escaped}%"
        return self._rows(
            conn,
            "kind NOT IN ('external_symbol', 'protocol_symbol') AND "
            "(name LIKE ? ESCAPE '\\' OR qualname LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\')",
            (pattern, pattern, pattern),
            limit=self.candidate_limit,
        )

    @staticmethod
    def _rows(
        conn: Any,
        predicate: str,
        params: tuple[Any, ...],
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        limit_clause = "" if limit is None else f" LIMIT {int(limit)}"
        rows = conn.execute(
            "SELECT id, kind, name, qualname, path, start_line, end_line, "
            f"properties_json FROM nodes WHERE {predicate} "
            f"ORDER BY kind, path, start_line, id{limit_clause}",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _candidate(row: dict[str, Any]) -> dict[str, Any]:
        return {
            key: row.get(key)
            for key in (
                "id",
                "kind",
                "name",
                "qualname",
                "path",
                "start_line",
                "end_line",
            )
            if row.get(key) is not None
        }

    @staticmethod
    def _is_routable(row: dict[str, Any]) -> bool:
        try:
            properties = json.loads(row.get("properties_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            properties = {}
        return properties.get("route_mounted") is not False

    def _normalized_path(self, target: str) -> str:
        """Return the repository-relative form the index stores.

        Indexed paths are repository-relative with no leading marker, so an
        absolute path under the root and a `./`-prefixed path must both
        reduce to the same key. `../` is deliberately not resolved: a path
        that leaves the repository has no indexed form.
        """

        normalized = target.replace("\\", "/")
        if self.repo_root and normalized.startswith(f"{self.repo_root}/"):
            normalized = normalized[len(self.repo_root) + 1 :]
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized

    def _looks_like_path(self, conn: Any, target: str) -> bool:
        if any(separator in target for separator in ("/", "\\")):
            return True
        # Suffixes are cached lowercased, so the target is compared the same
        # way: Widget.VUE is as path-shaped as Widget.vue. Only this shape
        # test is case-insensitive; the path lookup itself stays exact,
        # because the index stores paths as the file system spells them.
        return target.lower().endswith(tuple(self._indexed_path_suffixes(conn)))

    def _indexed_path_suffixes(self, conn: Any) -> frozenset[str]:
        """Return every file suffix this index actually holds.

        A static table cannot stay in sync with what the scanner accepts:
        frontends contribute their own extensions and SCIP ingestion carries
        whatever the payload names, so the set is open-ended. Reading it from
        the index is correct by construction. SOURCE_PATH_SUFFIXES stays as a
        floor so an empty or partial index never makes a plainly path-shaped
        target stop looking like one.
        """

        if self._indexed_suffixes is None:
            suffixes = set(SOURCE_PATH_SUFFIXES)
            try:
                rows = conn.execute(
                    "SELECT DISTINCT path FROM files WHERE path IS NOT NULL "
                    "UNION SELECT DISTINCT path FROM nodes WHERE path IS NOT NULL"
                ).fetchall()
            except Exception:  # pragma: no cover - defensive on older indexes
                rows = []
            for row in rows:
                value = row[0] if not isinstance(row, dict) else row.get("path")
                suffix = PurePosixPath(str(value or "")).suffix.lower()
                if suffix:
                    suffixes.add(suffix)
            self._indexed_suffixes = frozenset(suffixes)
        return self._indexed_suffixes

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
