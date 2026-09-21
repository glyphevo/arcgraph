"""Git-hunk changed-path and symbol-region mapping for Change Safety."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import Iterable

from arcgraph.change.contracts import ChangedPath, ChangedRegion
from arcgraph.change.errors import BaselineSourceUnavailable
from arcgraph.change.paths import (
    NormalizedRepositoryPath,
    normalize_repository_path,
    repository_path_comparison_key,
)
from arcgraph.change.source_provider import (
    BaselineSourceProvider,
    CurrentSourceProvider,
    SourceDocument,
    SourceNormalization,
    normalize_implementation_region,
)
from arcgraph.core.schemas import Node

_HUNK = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


@dataclass(frozen=True, slots=True)
class DiffHunk:
    old_path: str | None
    new_path: str | None
    old_start: int
    old_count: int
    new_start: int
    new_count: int


class ChangedRegionMapper:
    """Map Git changes to baseline/current symbols without filename-only approval."""

    def __init__(
        self,
        repo_root: Path,
        *,
        repo_id: str,
        baseline_source: BaselineSourceProvider,
        current_source: CurrentSourceProvider,
        baseline_nodes: Iterable[Node],
        current_nodes: Iterable[Node],
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.repo_id = repo_id
        self.baseline_source = baseline_source
        self.current_source = current_source
        self.baseline_nodes = list(baseline_nodes)
        self.current_nodes = list(current_nodes)

    def changed_paths(self) -> list[ChangedPath]:
        """Return tracked and untracked Git paths with original status preserved."""

        records = _git_bytes(
            self.repo_root,
            [
                "diff",
                "--no-ext-diff",
                "--name-status",
                "-z",
                "--find-renames",
                "--find-copies",
                self.baseline_source.commit_sha,
                "--",
            ],
        )
        changed = _parse_name_status(records, self.repo_root, self.repo_id)
        known_new_paths = {
            item.new_path_comparison_key
            for item in changed
            if item.new_path_comparison_key is not None
        }
        status = _git_bytes(
            self.repo_root,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        )
        for raw_path in _untracked_paths(status):
            normalized = normalize_repository_path(raw_path, self.repo_root)
            if normalized.comparison_key in known_new_paths:
                continue
            changed.append(
                ChangedPath(
                    repo_id=self.repo_id,
                    change_kind="untracked",
                    new_path=normalized.display_path,
                    new_path_comparison_key=normalized.comparison_key,
                    status="observed",
                    evidence=[{"source": "git_status", "status": "??"}],
                )
            )
        return sorted(
            changed,
            key=lambda item: (
                item.new_path_comparison_key or "",
                item.old_path_comparison_key or "",
                item.change_kind,
            ),
        )

    def changed_regions(
        self, changed_paths: list[ChangedPath] | None = None
    ) -> list[ChangedRegion]:
        paths = changed_paths if changed_paths is not None else self.changed_paths()
        hunks = _parse_hunks(
            _git_bytes(
                self.repo_root,
                [
                    "diff",
                    "--no-ext-diff",
                    "--unified=0",
                    "--find-renames",
                    "--find-copies",
                    self.baseline_source.commit_sha,
                    "--",
                ],
            )
        )
        regions: list[ChangedRegion] = []
        hunk_keys: set[tuple[str | None, str | None]] = set()
        for hunk in hunks:
            changed = _find_changed_path(paths, hunk.old_path, hunk.new_path)
            if changed is None:
                continue
            hunk_keys.add((changed.old_path, changed.new_path))
            regions.append(self._region_for_hunk(changed, hunk))
        for changed in paths:
            key = (changed.old_path, changed.new_path)
            if key in hunk_keys:
                continue
            regions.extend(self._regions_without_hunk(changed))
        return sorted(
            regions,
            key=lambda item: (
                item.path_comparison_key,
                item.new_start_line or 0,
                item.old_start_line or 0,
                item.classification,
            ),
        )

    def _region_for_hunk(self, changed: ChangedPath, hunk: DiffHunk) -> ChangedRegion:
        old_document = self._read_old(changed)
        new_document = self._read_new(changed)
        old_start, old_end = _line_range(hunk.old_start, hunk.old_count)
        new_start, new_end = _line_range(hunk.new_start, hunk.new_count)
        old_symbol_start, old_symbol_end = _trim_blank_boundaries(
            old_document,
            old_start,
            old_end,
        )
        new_symbol_start, new_symbol_end = _trim_blank_boundaries(
            new_document,
            new_start,
            new_end,
        )
        old_symbol, old_state = _symbol_for_range(
            self.baseline_nodes,
            changed.old_path_comparison_key,
            old_symbol_start,
            old_symbol_end,
            self.repo_root,
        )
        new_symbol, new_state = _symbol_for_range(
            self.current_nodes,
            changed.new_path_comparison_key,
            new_symbol_start,
            new_symbol_end,
            self.repo_root,
        )
        if old_start is None and old_state == "not_applicable" and new_symbol:
            counterpart_symbol, counterpart_state = _stable_counterpart_symbol(
                self.baseline_nodes,
                new_symbol,
                changed.old_path_comparison_key,
                self.repo_root,
            )
            if counterpart_state != "unmapped":
                old_symbol, old_state = counterpart_symbol, counterpart_state
        if new_start is None and new_state == "not_applicable" and old_symbol:
            counterpart_symbol, counterpart_state = _stable_counterpart_symbol(
                self.current_nodes,
                old_symbol,
                changed.new_path_comparison_key,
                self.repo_root,
            )
            if counterpart_state != "unmapped":
                new_symbol, new_state = counterpart_symbol, counterpart_state
        mapping_status = _mapping_status(
            changed.change_kind,
            old_symbol,
            old_state,
            new_symbol,
            new_state,
        )
        old_normalization = _symbol_normalization(
            old_document,
            self.baseline_nodes,
            old_symbol,
            self.repo_root,
        )
        new_normalization = _symbol_normalization(
            new_document,
            self.current_nodes,
            new_symbol,
            self.repo_root,
        )
        classification = _hunk_classification(changed.change_kind, old_start, new_start)
        if (
            changed.change_kind not in {"renamed", "copied"}
            and old_document is not None
            and new_document is not None
            and old_normalization is not None
            and new_normalization is not None
            and old_normalization.raw_source_digest
            != new_normalization.raw_source_digest
            and old_normalization.normalized_implementation_digest
            == new_normalization.normalized_implementation_digest
            and old_normalization.normalized_implementation_digest is not None
        ):
            classification = "non_implementation"
            mapping_status = "not_applicable"
        document = new_document or old_document
        if document is None:  # pragma: no cover - defensive provider boundary
            raise BaselineSourceUnavailable("diff hunk has no readable source side")
        normalization = new_normalization or old_normalization or document.normalization
        return ChangedRegion(
            repo_id=self.repo_id,
            path=document.path.display_path,
            path_comparison_key=document.path.comparison_key,
            old_path=changed.old_path,
            old_path_comparison_key=changed.old_path_comparison_key,
            new_path=changed.new_path,
            new_path_comparison_key=changed.new_path_comparison_key,
            old_start_line=old_start,
            old_end_line=old_end,
            new_start_line=new_start,
            new_end_line=new_end,
            baseline_symbol_id=old_symbol,
            current_symbol_id=new_symbol,
            classification=classification,
            mapping_status=mapping_status,
            baseline_raw_source_digest=(
                old_normalization.raw_source_digest if old_normalization else None
            ),
            current_raw_source_digest=(
                new_normalization.raw_source_digest if new_normalization else None
            ),
            baseline_normalized_implementation_digest=(
                old_normalization.normalized_implementation_digest
                if old_normalization
                else None
            ),
            current_normalized_implementation_digest=(
                new_normalization.normalized_implementation_digest
                if new_normalization
                else None
            ),
            source_normalization_projection_version=normalization.projection_version,
            baseline_source_identity=(
                old_document.source_identity if old_document else None
            ),
            current_source_identity=(
                new_document.source_identity if new_document else None
            ),
            evidence=[
                {
                    "source": "git_diff_hunk",
                    "old": [old_start, old_end],
                    "new": [new_start, new_end],
                    "old_symbol_range": [old_symbol_start, old_symbol_end],
                    "new_symbol_range": [new_symbol_start, new_symbol_end],
                    "old_mapping": old_state,
                    "new_mapping": new_state,
                }
            ],
        )

    def _regions_without_hunk(self, changed: ChangedPath) -> list[ChangedRegion]:
        old_document = self._read_old(changed)
        new_document = self._read_new(changed)
        document = new_document or old_document
        if document is None:  # pragma: no cover - defensive provider boundary
            return []
        old_start, old_end = _document_line_range(old_document)
        new_start, new_end = _document_line_range(new_document)
        old_symbol, old_state = _symbol_for_range(
            self.baseline_nodes,
            changed.old_path_comparison_key,
            old_start,
            old_end,
            self.repo_root,
        )
        new_symbol, new_state = _symbol_for_range(
            self.current_nodes,
            changed.new_path_comparison_key,
            new_start,
            new_end,
            self.repo_root,
        )
        mapping_status = _mapping_status(
            changed.change_kind,
            old_symbol,
            old_state,
            new_symbol,
            new_state,
        )
        old_normalization = _symbol_normalization(
            old_document,
            self.baseline_nodes,
            old_symbol,
            self.repo_root,
        )
        new_normalization = _symbol_normalization(
            new_document,
            self.current_nodes,
            new_symbol,
            self.repo_root,
        )
        normalization = new_normalization or old_normalization or document.normalization
        classification = changed.change_kind
        return [
            ChangedRegion(
                repo_id=self.repo_id,
                path=document.path.display_path,
                path_comparison_key=document.path.comparison_key,
                old_path=changed.old_path,
                old_path_comparison_key=changed.old_path_comparison_key,
                new_path=changed.new_path,
                new_path_comparison_key=changed.new_path_comparison_key,
                old_start_line=old_start,
                old_end_line=old_end,
                new_start_line=new_start,
                new_end_line=new_end,
                baseline_symbol_id=old_symbol,
                current_symbol_id=new_symbol,
                classification=classification,
                mapping_status=mapping_status,
                baseline_raw_source_digest=(
                    old_normalization.raw_source_digest if old_normalization else None
                ),
                current_raw_source_digest=(
                    new_normalization.raw_source_digest if new_normalization else None
                ),
                baseline_normalized_implementation_digest=(
                    old_normalization.normalized_implementation_digest
                    if old_normalization
                    else None
                ),
                current_normalized_implementation_digest=(
                    new_normalization.normalized_implementation_digest
                    if new_normalization
                    else None
                ),
                source_normalization_projection_version=normalization.projection_version,
                baseline_source_identity=(
                    old_document.source_identity if old_document else None
                ),
                current_source_identity=(
                    new_document.source_identity if new_document else None
                ),
                evidence=[{"source": "git_name_status", "status": changed.change_kind}],
            )
        ]

    def _read_old(self, changed: ChangedPath) -> SourceDocument | None:
        return self.baseline_source.read(changed.old_path) if changed.old_path else None

    def _read_new(self, changed: ChangedPath) -> SourceDocument | None:
        return self.current_source.read(changed.new_path) if changed.new_path else None


def _parse_name_status(raw: bytes, repo_root: Path, repo_id: str) -> list[ChangedPath]:
    tokens = [
        token.decode("utf-8", errors="surrogateescape")
        for token in raw.split(b"\0")
        if token
    ]
    position = 0
    result: list[ChangedPath] = []
    while position < len(tokens):
        status = tokens[position]
        position += 1
        if not status:
            continue
        kind = status[0]
        if kind in {"R", "C"}:
            if position + 1 >= len(tokens):
                raise BaselineSourceUnavailable("Git rename/copy status is incomplete")
            old = normalize_repository_path(tokens[position], repo_root)
            new = normalize_repository_path(tokens[position + 1], repo_root)
            position += 2
            result.append(
                _changed_path(
                    repo_id,
                    "renamed" if kind == "R" else "copied",
                    old,
                    new,
                    status,
                )
            )
            continue
        if position >= len(tokens):
            raise BaselineSourceUnavailable("Git name-status record has no path")
        path = normalize_repository_path(tokens[position], repo_root)
        position += 1
        if kind == "M":
            result.append(_changed_path(repo_id, "modified", path, path, status))
        elif kind == "A":
            result.append(_changed_path(repo_id, "added", None, path, status))
        elif kind == "D":
            result.append(_changed_path(repo_id, "deleted", path, None, status))
        else:
            result.append(
                ChangedPath(
                    repo_id=repo_id,
                    change_kind="modified",
                    old_path=path.display_path,
                    old_path_comparison_key=path.comparison_key,
                    new_path=path.display_path,
                    new_path_comparison_key=path.comparison_key,
                    status="unknown",
                    evidence=[{"source": "git_name_status", "status": status}],
                )
            )
    return result


def _changed_path(
    repo_id: str,
    kind: str,
    old: NormalizedRepositoryPath | None,
    new: NormalizedRepositoryPath | None,
    status: str,
) -> ChangedPath:
    return ChangedPath(
        repo_id=repo_id,
        change_kind=kind,  # type: ignore[arg-type]
        old_path=old.display_path if old else None,
        old_path_comparison_key=old.comparison_key if old else None,
        new_path=new.display_path if new else None,
        new_path_comparison_key=new.comparison_key if new else None,
        status="observed",
        evidence=[{"source": "git_name_status", "status": status}],
    )


def _untracked_paths(raw: bytes) -> list[str]:
    result: list[str] = []
    tokens = [
        token.decode("utf-8", errors="surrogateescape")
        for token in raw.split(b"\0")
        if token
    ]
    position = 0
    while position < len(tokens):
        item = tokens[position]
        position += 1
        if len(item) < 4 or item[2] != " ":
            continue
        status = item[:2]
        if "R" in status or "C" in status:
            position += 1
        if status == "??":
            result.append(item[3:])
    return result


def _parse_hunks(raw: bytes) -> list[DiffHunk]:
    old_path: str | None = None
    new_path: str | None = None
    result: list[DiffHunk] = []
    for line in raw.decode("utf-8", errors="surrogateescape").splitlines():
        if line.startswith("--- "):
            old_path = _patch_path(line[4:])
            continue
        if line.startswith("+++ "):
            new_path = _patch_path(line[4:])
            continue
        match = _HUNK.match(line)
        if not match:
            continue
        result.append(
            DiffHunk(
                old_path=old_path,
                new_path=new_path,
                old_start=int(match.group("old_start")),
                old_count=int(match.group("old_count") or "1"),
                new_start=int(match.group("new_start")),
                new_count=int(match.group("new_count") or "1"),
            )
        )
    return result


def _patch_path(value: str) -> str | None:
    if value == "/dev/null":
        return None
    path = value.split("\t", 1)[0]
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _find_changed_path(
    paths: list[ChangedPath],
    old_path: str | None,
    new_path: str | None,
) -> ChangedPath | None:
    for item in paths:
        if item.old_path == old_path and item.new_path == new_path:
            return item
    for item in paths:
        if new_path and item.new_path == new_path:
            return item
    for item in paths:
        if old_path and item.old_path == old_path:
            return item
    return None


def _line_range(start: int, count: int) -> tuple[int | None, int | None]:
    if count == 0:
        return None, None
    return start, start + count - 1


def _document_line_range(
    document: SourceDocument | None,
) -> tuple[int | None, int | None]:
    if document is None:
        return None, None
    # ``splitlines`` does not invent a third empty source line for a normal
    # two-line file ending in a newline.  That keeps whole-file rename/copy
    # regions mappable to a symbol whose recorded end line is the final
    # physical source line.
    line_count = max(1, len(document.content.splitlines()))
    return 1, line_count


def _trim_blank_boundaries(
    document: SourceDocument | None,
    start: int | None,
    end: int | None,
) -> tuple[int | None, int | None]:
    """Exclude only blank boundary lines when mapping a Git hunk to a symbol.

    A zero-context hunk can include separator lines immediately before or after
    an added symbol.  Those lines do not belong to the symbol and must not
    prevent a precise mapping.  Comments and any nonblank line are retained:
    if they are outside a symbol, the hunk remains safely unmapped.
    """

    if document is None or start is None or end is None:
        return start, end
    lines = document.content.splitlines()
    if start < 1 or end < start or end > len(lines):
        return start, end
    while start <= end and not lines[start - 1].strip():
        start += 1
    while end >= start and not lines[end - 1].strip():
        end -= 1
    if start > end:
        return None, None
    return start, end


def _symbol_for_range(
    nodes: list[Node],
    comparison_key: str | None,
    start: int | None,
    end: int | None,
    repo_root: Path,
) -> tuple[str | None, str]:
    if comparison_key is None or start is None or end is None:
        return None, "not_applicable"
    matches = [
        node
        for node in nodes
        if node.path is not None
        and repository_path_comparison_key(node.path, repo_root) == comparison_key
        and node.start_line is not None
        and node.end_line is not None
        and node.start_line <= start
        and node.end_line >= end
    ]
    if not matches:
        return None, "unmapped"
    matches.sort(
        key=lambda node: ((node.end_line or 0) - (node.start_line or 0), node.id)
    )
    if len(matches) > 1 and (
        (matches[0].end_line or 0) - (matches[0].start_line or 0)
        == (matches[1].end_line or 0) - (matches[1].start_line or 0)
    ):
        return None, "ambiguous"
    return matches[0].id, "mapped"


def _symbol_normalization(
    document: SourceDocument | None,
    nodes: list[Node],
    symbol_id: str | None,
    repo_root: Path,
) -> SourceNormalization | None:
    if document is None:
        return None
    if symbol_id is None:
        return document.normalization
    matches = [
        node
        for node in nodes
        if node.id == symbol_id
        and node.path is not None
        and repository_path_comparison_key(node.path, repo_root)
        == document.path.comparison_key
        and node.start_line is not None
        and node.end_line is not None
    ]
    if len(matches) != 1:
        # The hunk mapper already treats ambiguous symbol ownership as unsafe.
        # Keep the document-level projection only for the resulting Unknown
        # evidence; it is never used to authorize a mapped Symbol.
        return document.normalization
    node = matches[0]
    return normalize_implementation_region(
        document.path.display_path,
        document.content,
        start_line=node.start_line,
        end_line=node.end_line,
    )


def _stable_counterpart_symbol(
    nodes: list[Node],
    symbol_id: str,
    comparison_key: str | None,
    repo_root: Path,
) -> tuple[str | None, str]:
    """Recover the no-line side of a pure insertion/deletion by stable identity.

    A zero-context hunk has no source range on one side, so line mapping cannot
    determine its counterpart.  Reusing an exact graph Symbol ID in the
    corresponding old/new path is deterministic evidence, not a line-number
    guess.  Multiple candidates remain ambiguous and fail closed.
    """

    if comparison_key is None:
        return None, "not_applicable"
    matches = [
        node
        for node in nodes
        if node.id == symbol_id
        and node.path is not None
        and repository_path_comparison_key(node.path, repo_root) == comparison_key
    ]
    if len(matches) == 1:
        return matches[0].id, "mapped_by_stable_identity"
    if len(matches) > 1:
        return None, "ambiguous"
    return None, "unmapped"


def _hunk_classification(
    path_change_kind: str,
    old_start: int | None,
    new_start: int | None,
) -> str:
    """Retain Git's file status while classifying zero-context hunk direction."""

    if path_change_kind != "modified":
        return path_change_kind
    if old_start is None and new_start is not None:
        return "added"
    if new_start is None and old_start is not None:
        return "deleted"
    return "modified"


def _mapping_status(
    change_kind: str,
    old_symbol: str | None,
    old_state: str,
    new_symbol: str | None,
    new_state: str,
) -> str:
    if old_state == "ambiguous" or new_state == "ambiguous":
        return "ambiguous"
    if change_kind in {"added", "untracked"}:
        return "mapped" if new_state == "mapped" else "unmapped"
    if change_kind == "deleted":
        return "mapped" if old_state == "mapped" else "unmapped"
    # Git identifies a file as modified even when a specific zero-context hunk
    # is a pure insertion or deletion.  Such a hunk has no corresponding
    # source-side line range and must be evaluated against the side it actually
    # changes, rather than being falsely marked unmapped.
    if old_state == "not_applicable":
        return "mapped" if new_state == "mapped" else "unmapped"
    if new_state == "not_applicable":
        return "mapped" if old_state == "mapped" else "unmapped"
    if old_symbol and new_symbol:
        return "mapped"
    return "unmapped"


def _git_bytes(repo_root: Path, args: list[str]) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise BaselineSourceUnavailable(detail or "Git diff is unavailable")
    return result.stdout
