"""Freeze explicit and discoverable evidence once, before any consumer reads it."""

from pathlib import Path
import hashlib
import json
import re

from arcgraph.analyzers.coverage import CoverageCollector
from arcgraph.core.evidence_manifest import evidence_sidecar_path
from .contract import digest
from .journal import dump
from arcgraph.core.utils import symlink_below


def freshness_reason(build_dir, root, expected):
    path = build_dir / "artifact-inputs.json"
    if symlink_below(build_dir.parent.parent, path):
        return "semantic_artifact_input_unsafe"
    payload = json.loads(path.read_text(encoding="utf-8"))
    inputs = payload["inputs"]
    if digest(inputs) != expected or payload.get("checksum") != expected:
        return "semantic_artifact_input_binding_changed"
    for name, checksum in inputs.items():
        relative = Path(name)
        if ".." in relative.parts:
            return "semantic_artifact_input_unsafe"
        live = relative if relative.is_absolute() else root / relative
        if checksum is not None:
            if not re.fullmatch(r"[0-9a-f]{64}", checksum):
                return "semantic_artifact_input_unsafe"
            captured = build_dir / ("input-" + checksum + ".bin")
            if symlink_below(build_dir.parent.parent, captured):
                return "semantic_artifact_input_unsafe"
            if hashlib.sha256(captured.read_bytes()).hexdigest() != checksum:
                return "semantic_artifact_input_capture_changed"
        actual = (
            hashlib.sha256(live.read_bytes()).hexdigest() if live.is_file() else None
        )
        if actual != checksum:
            return "semantic_artifact_input_changed"
    return None


class FrozenInputs:
    def __init__(self, indexer):
        self.root = indexer.repo_root
        paths = [self.root / name for name in CoverageCollector.DEFAULT_CANDIDATES]
        paths.extend(
            indexer.output_dir / name
            for name in (
                "coverage.json",
                "coverage.xml",
                "runtime-trace.json",
                "scip-index.json",
                "pyright-export.json",
            )
        )
        paths.extend(
            self.root / name for name in (".scip/index.json", "scip-index.json")
        )
        for field in (
            "scip_index_path",
            "scip_graph_index_path",
            "openapi_path",
            "pyright_export_path",
            "coverage_path",
            "runtime_trace_path",
        ):
            raw = getattr(indexer, field)
            if raw is not None:
                path = Path(raw)
                path = path if path.is_absolute() else self.root / path
                if field == "openapi_path" and not path.resolve().is_relative_to(
                    self.root
                ):
                    continue
                paths.append(path)
        paths.extend(evidence_sidecar_path(p) for p in list(paths))
        self.values = {}
        self.logical = {}
        for path in paths:
            logical = path.absolute()
            path = logical.resolve()
            self.logical[logical] = path
            if path not in self.values:
                if path.is_file():
                    raw = path.read_bytes()
                    info = path.stat()
                    if info.st_size != len(raw):
                        raise ValueError(
                            "input_changed_during_run: artifact capture changed"
                        )
                    self.values[path] = (raw, info)
                else:
                    self.values[path] = (None, None)
        self.manifest = {
            self.display(path): (
                hashlib.sha256(raw).hexdigest() if raw is not None else None
            )
            for path, (raw, _) in self.values.items()
        }
        self.checksum = digest(self.manifest)

    def display(self, path):
        return (
            path.relative_to(self.root).as_posix()
            if path.is_relative_to(self.root)
            else str(path)
        )

    def value(self, path):
        key = Path(path).resolve()
        if key not in self.values:
            raise ValueError("input_changed_during_run: uncaptured artifact input")
        return self.values[key]

    def exists(self, path):
        return self.value(path)[0] is not None

    def read_bytes(self, path):
        raw = self.value(path)[0]
        if raw is None:
            raise FileNotFoundError(path)
        return raw

    def stat(self, path):
        info = self.value(path)[1]
        if info is None:
            raise FileNotFoundError(path)
        return info

    def verify(self):
        for logical, resolved in self.logical.items():
            if logical.resolve() != resolved:
                raise ValueError("input_changed_during_run: artifact link changed")
        for path, (raw, _) in self.values.items():
            live = path.read_bytes() if path.is_file() else None
            if live != raw:
                raise ValueError(
                    "input_changed_during_run: artifact input changed: "
                    + self.display(path)
                )

    def persist(self, destination):
        dump(
            {"inputs": self.manifest, "checksum": self.checksum},
            destination / "artifact-inputs.json",
        )
        for path, (raw, _) in self.values.items():
            if raw is not None:
                (
                    destination
                    / ("input-" + self.manifest[self.display(path)] + ".bin")
                ).write_bytes(raw)
