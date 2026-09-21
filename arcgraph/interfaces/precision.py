"""Precision input generation helpers for ArcGraph."""

from __future__ import annotations

import json
import queue
import re
import shutil
import shlex
import subprocess
import sys
import ast
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from arcgraph.analyzers.imports import ImportAnalyzer
from arcgraph.analyzers.symbols import SymbolAnalyzer
from arcgraph.core.ids import class_id, function_id, method_id, module_id
from arcgraph.core.metadata import current_commit
from arcgraph.core.scanner import FileScanner, SourceRoot, detect_source_roots
from arcgraph.core.schemas import FileRecord, Node

PYRIGHT_LSP_REQUEST_BATCH_SIZE = 16


def convert_scip_index_to_json(
    *,
    repo_root: Path,
    scip_index_path: str | Path,
    output_path: str | Path,
    scip_command: str = "scip",
    source_roots: Iterable[SourceRoot | str] | None = None,
    commit_sha: str | None = None,
) -> dict[str, Any]:
    """Convert a binary ``index.scip`` file to stable JSON for ArcGraph."""

    repo_root = repo_root.resolve()
    scip_index = _absolute_path(repo_root, scip_index_path)
    output = _absolute_path(repo_root, output_path)
    roots = _source_root_paths(source_roots or detect_source_roots(repo_root).roots)
    artifact_commit = (
        commit_sha if commit_sha is not None else current_commit(repo_root)
    )
    if not scip_index.exists():
        raise RuntimeError(f"SCIP index file does not exist: {scip_index}")

    payload = _run_scip_print_json(
        repo_root=repo_root,
        scip_index=scip_index,
        scip_command=scip_command,
    )
    payload = _with_arcgraph_metadata(
        payload,
        _artifact_metadata(
            commit_sha=artifact_commit,
            source_roots=roots,
            tool_name="scip",
            tool_version="unknown",
        ),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return {
        "status": "available",
        "action": "scip_json_converted",
        "scip_index": str(scip_index),
        "path": str(output),
        "documents": (
            len(payload.get("documents", [])) if isinstance(payload, dict) else 0
        ),
        "commit_sha": artifact_commit,
        "source_roots": roots,
    }


def generate_scip_python_json(
    *,
    repo_root: Path,
    output_path: str | Path,
    index_file: str | Path | None = None,
    project_name: str | None = None,
    project_version: str | None = None,
    target_only: list[str] | None = None,
    environment_path: str | Path | None = None,
    scip_python_command: str = "scip-python",
    scip_command: str = "scip",
    timeout_seconds: float = 600.0,
    source_roots: Iterable[SourceRoot | str] | None = None,
    commit_sha: str | None = None,
) -> dict[str, Any]:
    """Run scip-python, then convert its binary index to JSON."""

    repo_root = repo_root.resolve()
    output = _absolute_path(repo_root, output_path)
    roots = _source_root_paths(source_roots or detect_source_roots(repo_root).roots)
    artifact_commit = (
        commit_sha if commit_sha is not None else current_commit(repo_root)
    )
    scip_index = (
        _absolute_path(repo_root, index_file)
        if index_file is not None
        else output.with_suffix(".scip")
    )
    default_index = repo_root / "index.scip"
    targets = target_only or []
    if default_index.exists() and (
        len(targets) > 1 or default_index.resolve() != scip_index.resolve()
    ):
        raise RuntimeError(
            "Refusing to overwrite existing repo-root index.scip. "
            "Pass --index-file index.scip to reuse it, or move/delete it first."
        )

    if len(targets) > 1:
        payloads: list[Any] = []
        scip_indices: list[str] = []
        for target in targets:
            part_index = scip_index.with_name(
                f"{scip_index.stem}-{_safe_filename_fragment(target)}{scip_index.suffix}"
            )
            if part_index.exists():
                part_index.unlink()
            _run_scip_python_index(
                repo_root=repo_root,
                project_name=project_name or repo_root.name,
                project_version=project_version,
                target_only=[target],
                environment_path=environment_path,
                scip_python_command=scip_python_command,
                timeout_seconds=timeout_seconds,
            )
            if not default_index.exists():
                raise RuntimeError(
                    f"scip-python completed without producing index.scip for target {target!r}."
                )
            part_index.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(default_index), str(part_index))
            payloads.append(
                _run_scip_print_json(
                    repo_root=repo_root,
                    scip_index=part_index,
                    scip_command=scip_command,
                )
            )
            scip_indices.append(str(part_index))

        payload = _merge_scip_payloads(payloads)
        payload = _with_arcgraph_metadata(
            payload,
            _artifact_metadata(
                commit_sha=artifact_commit,
                source_roots=roots,
                tool_name="scip-python",
                tool_version="unknown",
            ),
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return {
            "status": "available",
            "action": "scip_python_json_generated",
            "path": str(output),
            "scip_indices": scip_indices,
            "documents": (
                len(payload.get("documents", [])) if isinstance(payload, dict) else 0
            ),
            "project_name": project_name or repo_root.name,
            "project_version": project_version,
            "target_only": targets,
            "commit_sha": artifact_commit,
            "source_roots": roots,
        }

    _run_scip_python_index(
        repo_root=repo_root,
        project_name=project_name or repo_root.name,
        project_version=project_version,
        target_only=targets,
        environment_path=environment_path,
        scip_python_command=scip_python_command,
        timeout_seconds=timeout_seconds,
    )
    if not default_index.exists():
        raise RuntimeError(
            "scip-python completed without producing index.scip in the repository root."
        )
    if default_index.resolve() != scip_index.resolve():
        scip_index.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(default_index), str(scip_index))

    result = convert_scip_index_to_json(
        repo_root=repo_root,
        scip_index_path=scip_index,
        output_path=output,
        scip_command=scip_command,
        source_roots=roots,
        commit_sha=artifact_commit,
    )
    result["action"] = "scip_python_json_generated"
    result["project_name"] = project_name or repo_root.name
    result["project_version"] = project_version
    result["target_only"] = targets
    return result


def generate_pyright_json(
    *,
    repo_root: Path,
    output_path: str | Path,
    target_only: list[str] | None = None,
    source_roots: list[SourceRoot] | tuple[SourceRoot, ...] | None = None,
    python_version: str = "3.11",
    extra_path: list[str] | None = None,
    pyright_command: str | list[str] = "pyright-langserver",
    timeout_seconds: float = 300.0,
    commit_sha: str | None = None,
) -> dict[str, Any]:
    """Generate ArcGraph's Pyright precision JSON contract.

    Pyright remains an external tool. This helper verifies that a Pyright LSP
    endpoint can initialize, then emits ArcGraph-readable type evidence from
    statically located annotation probes. The importer treats the resulting
    records as Pyright evidence only when the LSP probe succeeds.
    """

    repo_root = repo_root.resolve()
    output = _absolute_path(repo_root, output_path)
    roots = (
        tuple(source_roots) if source_roots else detect_source_roots(repo_root).roots
    )
    root_paths = _source_root_paths(roots)
    artifact_commit = (
        commit_sha if commit_sha is not None else current_commit(repo_root)
    )
    targets = [target.replace("\\", "/").rstrip("/") for target in target_only or []]
    extra_paths = [str(_absolute_path(repo_root, path)) for path in extra_path or []]
    scanner = FileScanner(repo_root, roots)
    files = [
        file_record
        for file_record in scanner.scan()
        if _file_matches_targets(file_record, targets)
    ]
    nodes, trees = _symbol_context(files)
    type_info, lsp_probe = _pyright_type_info_records(
        repo_root=repo_root,
        files=files,
        trees=trees,
        nodes=nodes,
        command=pyright_command,
        python_version=python_version,
        extra_paths=extra_paths,
        timeout_seconds=timeout_seconds,
    )
    payload = {
        "status": "available",
        "generator": "arcgraph.precision.pyright",
        "generator_version": 1,
        "pyright_command": _display_command(pyright_command),
        "python_version": python_version,
        "extra_paths": extra_paths,
        "source_roots": root_paths,
        "target_only": targets,
        "lsp": lsp_probe,
        "references": [],
        "type_info": type_info,
        "diagnostics": [],
    }
    payload = _with_arcgraph_metadata(
        payload,
        _artifact_metadata(
            commit_sha=artifact_commit,
            source_roots=root_paths,
            tool_name="pyright-langserver",
            tool_version="unknown",
        ),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return {
        "status": "available",
        "action": "pyright_json_generated",
        "path": str(output),
        "files": len(files),
        "type_info": len(type_info),
        "pyright_lsp_probe_total": lsp_probe.get("probe_total", 0),
        "pyright_lsp_requestable_probe_total": lsp_probe.get(
            "requestable_probe_total", 0
        ),
        "pyright_lsp_skipped_unmappable_total": lsp_probe.get(
            "skipped_unmappable_total", 0
        ),
        "pyright_lsp_requests_total": lsp_probe.get("requests_total", 0),
        "pyright_lsp_type_info_total": lsp_probe.get("type_info_total", 0),
        "pyright_lsp_unresolved_total": lsp_probe.get("unresolved_total", 0),
        "source_roots": root_paths,
        "target_only": targets,
        "pyright_status": lsp_probe.get("status", "unavailable"),
        "commit_sha": artifact_commit,
    }


def _run_pyright_lsp(
    *,
    repo_root: Path,
    command: str | list[str],
    python_version: str,
    extra_paths: list[str],
    files: list[FileRecord],
    probes: list[dict[str, Any]],
    nodes: list[Node],
    timeout_seconds: float,
) -> dict[str, Any]:
    partitions = _partition_pyright_probes(probes)
    if len(partitions) <= 1:
        return _run_pyright_lsp_session(
            repo_root=repo_root,
            command=command,
            python_version=python_version,
            extra_paths=extra_paths,
            files=files,
            probes=probes,
            nodes=nodes,
            timeout_seconds=timeout_seconds,
        )

    merged: dict[str, Any] = {
        "status": "available",
        "command": " ".join(_command_args(command)),
        "returncode": 0,
        "response_count": 0,
        "requests_total": 0,
        "request_ids": {},
        "responses": [],
        "stderr": "",
        "probe_partitions": {
            partition: len(partition_probes)
            for partition, partition_probes in partitions
        },
    }
    response_id_offset = 0
    file_by_path = {
        Path(file_record.abs_path).resolve(): file_record for file_record in files
    }
    for _partition, partition_probes in partitions:
        partition_paths = {
            Path(probe["abs_path"]).resolve()
            for probe in partition_probes
            if isinstance(probe.get("abs_path"), str)
        }
        partition_files = [
            file_record
            for path, file_record in file_by_path.items()
            if path in partition_paths
        ]
        session = _run_pyright_lsp_session(
            repo_root=repo_root,
            command=command,
            python_version=python_version,
            extra_paths=extra_paths,
            files=partition_files,
            probes=partition_probes,
            nodes=nodes,
            timeout_seconds=timeout_seconds,
        )
        _merge_lsp_session(merged, session, response_id_offset)
        response_id_offset += _max_lsp_response_id(session) + 1000
    return merged


def _run_pyright_lsp_session(
    *,
    repo_root: Path,
    command: str | list[str],
    python_version: str,
    extra_paths: list[str],
    files: list[FileRecord],
    probes: list[dict[str, Any]],
    nodes: list[Node],
    timeout_seconds: float,
) -> dict[str, Any]:
    args = _command_args(command)
    if "--stdio" not in args:
        args.append("--stdio")
    request_ids: dict[str, tuple[int, str]] = {}
    responses: list[dict[str, Any]] = []
    request_groups: dict[int, dict[str, dict[str, Any]]] = {}
    nodes_by_path, class_nodes_by_name = _lsp_resolution_indexes(nodes)
    client: _PyrightLspClient | None = None
    try:
        client = _PyrightLspClient(
            args=args,
            repo_root=repo_root,
            timeout_seconds=timeout_seconds,
        )
        responses.append(
            client.request(
                "initialize",
                {
                    "processId": None,
                    "rootUri": repo_root.as_uri(),
                    "capabilities": {
                        "textDocument": {
                            "definition": {"dynamicRegistration": False},
                            "typeDefinition": {"dynamicRegistration": False},
                            "hover": {"dynamicRegistration": False},
                        }
                    },
                    "initializationOptions": {
                        "python": {
                            "analysis": {"extraPaths": extra_paths},
                            "pythonVersion": python_version,
                        }
                    },
                },
                _count_metric=False,
            )
        )
        client.notify("initialized", {})
        requestable_paths = {
            str(Path(probe["abs_path"]).resolve())
            for probe in probes
            if isinstance(probe.get("abs_path"), str)
        }
        for file_record in files:
            path = Path(file_record.abs_path)
            if str(path.resolve()) not in requestable_paths:
                continue
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError):
                continue
            client.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": path.resolve().as_uri(),
                        "languageId": "python",
                        "version": 1,
                        "text": text,
                    }
                },
            )
        pending_probes = list(probes)
        for method in (
            "textDocument/typeDefinition",
            "textDocument/definition",
            "textDocument/hover",
        ):
            if not pending_probes:
                break
            stage_responses = _run_pyright_lsp_stage(
                client=client,
                probes=pending_probes,
                method=method,
            )
            for probe, request_id, response in stage_responses:
                probe_id = int(probe["probe_id"])
                request_ids[str(request_id)] = (probe_id, method)
                request_groups.setdefault(probe_id, {})[method] = response
                responses.append(response)
            pending_probes = [
                probe
                for probe in pending_probes
                if not _probe_resolved_by_request_group(
                    probe=probe,
                    request_group=request_groups.get(int(probe["probe_id"]), {}),
                    nodes_by_path=nodes_by_path,
                    class_nodes_by_name=class_nodes_by_name,
                    repo_root=repo_root,
                )
            ]
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Required command is not available on PATH: {args[0]}"
        ) from exc
    except TimeoutError as exc:
        raise RuntimeError(
            f"Pyright LSP probe timed out after {timeout_seconds} seconds: "
            f"{' '.join(args)}"
        ) from exc
    finally:
        if client is not None:
            client.close()
    stderr = client.stderr if client is not None else ""
    response_count = len(responses)
    returncode = client.returncode if client is not None else None
    if returncode not in (0, None) and response_count == 0:
        detail = stderr or f"exit code {returncode}"
        raise RuntimeError(f"Pyright LSP probe failed ({' '.join(args)}): {detail}")
    return {
        "status": "available",
        "command": " ".join(args),
        "returncode": returncode,
        "response_count": response_count,
        "requests_total": len(request_ids),
        "request_ids": request_ids,
        "responses": responses,
        "stderr": stderr,
    }


def _partition_pyright_probes(
    probes: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    partitions: dict[str, list[dict[str, Any]]] = {}
    for probe in probes:
        path = str(probe.get("path") or "").replace("\\", "/")
        partition = _pyright_probe_partition(path)
        partitions.setdefault(partition, []).append(probe)
    return sorted(partitions.items(), key=lambda item: item[0])


def _pyright_probe_partition(path: str) -> str:
    if path.startswith("src/"):
        return "src"
    if path.startswith("tests/"):
        return "tests"
    if path.startswith("arcgraph/"):
        return "arcgraph"
    if path.startswith("scripts/"):
        return "scripts"
    return path.split("/", 1)[0] if path else "<unknown>"


def _merge_lsp_session(
    merged: dict[str, Any],
    session: dict[str, Any],
    response_id_offset: int,
) -> None:
    merged["response_count"] += int(session.get("response_count") or 0)
    merged["requests_total"] += int(session.get("requests_total") or 0)
    if session.get("returncode") not in (0, None):
        merged["returncode"] = session.get("returncode")
    stderr = str(session.get("stderr") or "").strip()
    if stderr:
        merged["stderr"] = "\n".join(
            part for part in [str(merged.get("stderr") or ""), stderr] if part
        )
    request_ids = session.get("request_ids", {})
    if isinstance(request_ids, dict):
        for request_id, value in request_ids.items():
            merged["request_ids"][str(int(request_id) + response_id_offset)] = value
    responses = session.get("responses", [])
    if isinstance(responses, list):
        for response in responses:
            if not isinstance(response, dict):
                continue
            copied = dict(response)
            response_id = copied.get("id")
            if isinstance(response_id, int):
                copied["id"] = response_id + response_id_offset
            merged["responses"].append(copied)


def _max_lsp_response_id(session: dict[str, Any]) -> int:
    max_id = 0
    request_ids = session.get("request_ids", {})
    if isinstance(request_ids, dict):
        for request_id in request_ids:
            try:
                max_id = max(max_id, int(request_id))
            except (TypeError, ValueError):
                continue
    responses = session.get("responses", [])
    if isinstance(responses, list):
        for response in responses:
            if isinstance(response, dict) and isinstance(response.get("id"), int):
                max_id = max(max_id, int(response["id"]))
    return max_id


def _run_pyright_lsp_stage(
    *,
    client: "_PyrightLspClient",
    probes: list[dict[str, Any]],
    method: str,
) -> list[tuple[dict[str, Any], int, dict[str, Any]]]:
    responses: list[tuple[dict[str, Any], int, dict[str, Any]]] = []
    for chunk in _chunks(probes, PYRIGHT_LSP_REQUEST_BATCH_SIZE):
        requests: list[tuple[dict[str, Any], int]] = []
        for probe in chunk:
            request_id = client.send_request(method, _lsp_request_params(probe))
            requests.append((probe, request_id))
        responses.extend(
            (probe, request_id, client.wait_for_response(request_id))
            for probe, request_id in requests
        )
    return responses


def _chunks(items: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _lsp_request_params(probe: dict[str, Any]) -> dict[str, Any]:
    return {
        "textDocument": {"uri": Path(probe["abs_path"]).resolve().as_uri()},
        "position": {
            "line": max(int(probe["line"] or 1) - 1, 0),
            "character": max(int(probe["column"] or 0), 0),
        },
    }


class _PyrightLspClient:
    def __init__(
        self,
        *,
        args: list[str],
        repo_root: Path,
        timeout_seconds: float,
    ) -> None:
        self.args = args
        self.timeout_seconds = timeout_seconds
        self.deadline = time.monotonic() + timeout_seconds
        self.next_id = 1
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.pending: dict[str, dict[str, Any]] = {}
        self.stderr_parts: list[str] = []
        self.process = subprocess.Popen(
            args,
            cwd=repo_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()
        self.stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self.stderr_reader.start()

    @property
    def returncode(self) -> int | None:
        return self.process.poll()

    @property
    def stderr(self) -> str:
        return "".join(self.stderr_parts).strip()

    def notify(self, method: str, params: Any) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def request(
        self, method: str, params: Any, *, _count_metric: bool = True
    ) -> dict[str, Any]:
        _request_id, response = self.request_with_id(
            method, params, _count_metric=_count_metric
        )
        return response

    def request_with_id(
        self, method: str, params: Any, *, _count_metric: bool = True
    ) -> tuple[int, dict[str, Any]]:
        request_id = self.send_request(method, params)
        return request_id, self.wait_for_response(request_id)

    def send_request(self, method: str, params: Any) -> int:
        request_id = self.next_id
        self.next_id += 1
        self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        return request_id

    def wait_for_response(self, request_id: int) -> dict[str, Any]:
        return self._wait_for_response(request_id)

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.request("shutdown", None, _count_metric=False)
                self.notify("exit", None)
            except (BrokenPipeError, RuntimeError, TimeoutError):
                pass
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def _write(self, message: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise RuntimeError("Pyright LSP stdin is not available.")
        self.process.stdin.write(_lsp_message(message))
        self.process.stdin.flush()

    def _wait_for_response(self, request_id: int) -> dict[str, Any]:
        key = str(request_id)
        if key in self.pending:
            return self.pending.pop(key)
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for LSP response {request_id}.")
            try:
                message = self.messages.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if self.process.poll() is not None:
                    raise RuntimeError(
                        "Pyright LSP exited before returning response "
                        f"{request_id}: {self.stderr or self.process.returncode}"
                    )
                continue
            message_id = message.get("id")
            if message_id is None:
                continue
            message_key = str(message_id)
            if message_key == key:
                return message
            self.pending[message_key] = message

    def _read_stdout(self) -> None:
        stream = self.process.stdout
        if stream is None:
            return
        while True:
            message = _read_lsp_message_from_stream(stream)
            if message is None:
                return
            self.messages.put(message)

    def _read_stderr(self) -> None:
        stream = self.process.stderr
        if stream is None:
            return
        while True:
            chunk = stream.readline()
            if not chunk:
                return
            self.stderr_parts.append(chunk.decode("utf-8", errors="replace"))


def _read_lsp_message_from_stream(stream: Any) -> dict[str, Any] | None:
    headers: list[str] = []
    while True:
        line = stream.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        headers.append(line.decode("ascii", errors="ignore"))
    length: int | None = None
    for line in headers:
        if line.lower().startswith("content-length:"):
            try:
                length = int(line.split(":", 1)[1].strip())
            except ValueError:
                length = None
            break
    if length is None:
        return None
    body = stream.read(length)
    if len(body) != length:
        return None
    try:
        message = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return message if isinstance(message, dict) else None


def _lsp_message(message: dict[str, Any]) -> bytes:
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


def _command_args(command: str | list[str]) -> list[str]:
    if isinstance(command, list):
        args = [str(part) for part in command if str(part)]
    else:
        args = [
            part.strip("\"'")
            for part in shlex.split(command, posix=(sys.platform != "win32"))
            if part
        ]
    if args:
        resolved = shutil.which(args[0])
        if resolved:
            args[0] = resolved
    return args


def _display_command(command: str | list[str]) -> str:
    if isinstance(command, list):
        return " ".join(command)
    return command


def _artifact_metadata(
    *,
    commit_sha: str | None,
    source_roots: list[str],
    tool_name: str,
    tool_version: str | None,
) -> dict[str, Any]:
    return {
        "commit_sha": commit_sha,
        "source_roots": source_roots,
        "tool_name": tool_name,
        "tool_version": tool_version or "unknown",
        "generated_at": _utc_now(),
        "scope": "repository",
    }


def _with_arcgraph_metadata(payload: Any, metadata: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, dict):
        updated = dict(payload)
    elif isinstance(payload, list):
        updated = {"documents": payload}
    else:
        updated = {"documents": []}
    updated["arcgraph_metadata"] = metadata
    for key in (
        "commit_sha",
        "source_roots",
        "tool_name",
        "tool_version",
        "generated_at",
    ):
        if metadata.get(key) is not None:
            updated[key] = metadata[key]
    return updated


def _source_root_paths(source_roots: Iterable[SourceRoot | str]) -> list[str]:
    paths: list[str] = []
    for root in source_roots:
        if isinstance(root, SourceRoot):
            value = root.path
        else:
            value = str(root)
        if value:
            paths.append(value)
    return paths


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _file_matches_targets(file_record: FileRecord, targets: list[str]) -> bool:
    if not targets:
        return True
    path = file_record.path.replace("\\", "/")
    source_root = file_record.source_root.replace("\\", "/")
    module = file_record.module.replace(".", "/")
    return any(
        path == target
        or path.startswith(f"{target}/")
        or source_root == target
        or source_root.startswith(f"{target}/")
        or module == target.replace(".", "/")
        or module.startswith(f"{target.replace('.', '/')}/")
        for target in targets
    )


def _symbol_context(
    files: list[FileRecord],
) -> tuple[list[Node], dict[str, ast.Module]]:
    symbol_analyzer = SymbolAnalyzer()
    nodes: list[Node] = []
    trees: dict[str, ast.Module] = {}
    for file_record in files:
        try:
            source = Path(file_record.abs_path).read_text(encoding="utf-8-sig")
            tree = ast.parse(source, filename=file_record.path)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        trees[file_record.path] = tree
        module_name = file_record.module.rsplit(".", 1)[-1]
        nodes.append(
            Node(
                id=module_id(file_record.module),
                kind="module",
                name=module_name,
                qualname=file_record.module,
                path=file_record.path,
            )
        )
        nodes.extend(symbol_analyzer.analyze(file_record, tree).nodes)
    return nodes, trees


def _pyright_type_info_records(
    *,
    repo_root: Path,
    files: list[FileRecord],
    trees: dict[str, ast.Module],
    nodes: list[Node],
    command: str | list[str],
    python_version: str,
    extra_paths: list[str],
    timeout_seconds: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    class_targets = {
        node.qualname: node
        for node in nodes
        if node.kind == "class" and isinstance(node.qualname, str)
    }
    simple_names: dict[str, list[Node]] = {}
    for node in class_targets.values():
        simple_names.setdefault(node.name, []).append(node)

    probes: list[dict[str, Any]] = []
    for file_record in files:
        tree = trees.get(file_record.path)
        if tree is None:
            continue
        aliases = _import_aliases(tree, file_record)
        visitor = _PyrightTypeProbeVisitor(
            file_record=file_record,
            aliases=aliases,
            class_targets=class_targets,
            simple_names=simple_names,
        )
        visitor.visit(tree)
        probes.extend(visitor.probes)
    for index, probe in enumerate(probes):
        probe["probe_id"] = index
    requestable_probes = [
        probe for probe in probes if _is_pyright_requestable_probe(probe)
    ]
    lsp = _run_pyright_lsp(
        repo_root=repo_root,
        command=command,
        python_version=python_version,
        extra_paths=extra_paths,
        files=files,
        probes=requestable_probes,
        nodes=nodes,
        timeout_seconds=timeout_seconds,
    )
    records, unresolved = _records_from_lsp_responses(
        probes=requestable_probes,
        responses=lsp["responses"],
        request_ids=lsp["request_ids"],
        nodes=nodes,
        repo_root=repo_root,
    )
    records.sort(
        key=lambda item: (
            str(item.get("path") or ""),
            int(item.get("line") or 0),
            int(item.get("column") or 0),
            str(item.get("subject") or ""),
            str(item.get("name") or ""),
        )
    )
    lsp_summary = {
        key: value
        for key, value in lsp.items()
        if key not in {"responses", "request_ids"}
    }
    lsp_summary.update(
        {
            "probe_total": len(probes),
            "requestable_probe_total": len(requestable_probes),
            "skipped_unmappable_total": len(probes) - len(requestable_probes),
            "type_info_total": len(records),
            "unresolved_total": len(unresolved),
            "unresolved_probes": unresolved[:100],
        }
    )
    return records, lsp_summary


def _import_aliases(tree: ast.Module, file_record: FileRecord) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                aliases[bound] = alias.name
        elif isinstance(stmt, ast.ImportFrom):
            base = ImportAnalyzer._resolve_import_from_base(file_record, stmt)
            if not base:
                continue
            for alias in stmt.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name
                aliases[bound] = f"{base}.{alias.name}"
    return aliases


def _is_pyright_requestable_probe(probe: dict[str, Any]) -> bool:
    static_target_ids = probe.get("static_target_ids")
    return isinstance(static_target_ids, list) and any(
        isinstance(target_id, str) and target_id.startswith("class:")
        for target_id in static_target_ids
    )


def _records_from_lsp_responses(
    *,
    probes: list[dict[str, Any]],
    responses: list[dict[str, Any]],
    request_ids: dict[str, tuple[int, str]],
    nodes: list[Node],
    repo_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    responses_by_id = {
        str(message.get("id")): message
        for message in responses
        if message.get("id") is not None
    }
    requests_by_probe: dict[int, dict[str, dict[str, Any]]] = {}
    for request_id, value in request_ids.items():
        probe_id, method = value
        response = responses_by_id.get(str(request_id))
        if response is not None:
            requests_by_probe.setdefault(probe_id, {})[method] = response

    nodes_by_path, class_nodes_by_name = _lsp_resolution_indexes(nodes)

    records: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for probe in probes:
        probe_id = int(probe["probe_id"])
        request_group = requests_by_probe.get(probe_id, {})
        resolved = _resolve_probe_from_lsp(
            probe=probe,
            request_group=request_group,
            nodes_by_path=nodes_by_path,
            class_nodes_by_name=class_nodes_by_name,
            repo_root=repo_root,
        )
        if resolved:
            records.extend(resolved)
        else:
            unresolved.append(
                {
                    "path": probe.get("path"),
                    "line": probe.get("line"),
                    "column": probe.get("column"),
                    "name": probe.get("name"),
                    "expression": probe.get("expression"),
                    "subject": probe.get("subject"),
                }
            )
    return records, unresolved


def _lsp_resolution_indexes(
    nodes: list[Node],
) -> tuple[dict[str, list[Node]], dict[str, list[Node]]]:
    nodes_by_path: dict[str, list[Node]] = {}
    class_nodes_by_name: dict[str, list[Node]] = {}
    for node in nodes:
        if node.path:
            nodes_by_path.setdefault(node.path.replace("\\", "/"), []).append(node)
        if node.kind == "class" and node.name:
            class_nodes_by_name.setdefault(node.name, []).append(node)
    return nodes_by_path, class_nodes_by_name


def _probe_resolved_by_request_group(
    *,
    probe: dict[str, Any],
    request_group: dict[str, dict[str, Any]],
    nodes_by_path: dict[str, list[Node]],
    class_nodes_by_name: dict[str, list[Node]],
    repo_root: Path,
) -> bool:
    return bool(
        _resolve_probe_from_lsp(
            probe=probe,
            request_group=request_group,
            nodes_by_path=nodes_by_path,
            class_nodes_by_name=class_nodes_by_name,
            repo_root=repo_root,
        )
    )


def _resolve_probe_from_lsp(
    *,
    probe: dict[str, Any],
    request_group: dict[str, dict[str, Any]],
    nodes_by_path: dict[str, list[Node]],
    class_nodes_by_name: dict[str, list[Node]],
    repo_root: Path,
) -> list[dict[str, Any]]:
    for method in ("textDocument/typeDefinition", "textDocument/definition"):
        response = request_group.get(method, {})
        for location in _lsp_locations(response.get("result")):
            target = _node_from_lsp_location(
                location,
                nodes_by_path=nodes_by_path,
                repo_root=repo_root,
            )
            if target is not None:
                return [
                    _type_info_record(
                        probe,
                        target,
                        lsp_method=method,
                        location=location,
                        hover_text=None,
                    )
                ]

    hover_response = request_group.get("textDocument/hover", {})
    hover_text = _hover_text(hover_response.get("result"))
    hover_target = _node_from_hover_text(hover_text, class_nodes_by_name)
    if hover_target is not None:
        return [
            _type_info_record(
                probe,
                hover_target,
                lsp_method="textDocument/hover",
                location=None,
                hover_text=hover_text,
            )
        ]
    return []


def _type_info_record(
    probe: dict[str, Any],
    target: Node,
    *,
    lsp_method: str,
    location: dict[str, Any] | None,
    hover_text: str | None,
) -> dict[str, Any]:
    record = {
        "subject": probe["subject"],
        "target": target.id,
        "type_id": target.id,
        "symbol_id": target.id,
        "name": probe["name"],
        "subject_kind": probe["subject_kind"],
        "type": probe["type"],
        "type_expression": probe["type_expression"],
        "expression": probe["expression"],
        "path": probe["path"],
        "line": probe["line"],
        "column": probe["column"],
        "kind": "type_info",
        "source": "pyright_lsp",
        "evidence_kind": "pyright_lsp_type_info",
        "lsp_method": lsp_method,
    }
    if location is not None:
        uri = location.get("targetUri") or location.get("uri")
        range_value = location.get("targetRange") or location.get("range")
        record["lsp_target_uri"] = uri
        record["lsp_target_range"] = range_value
    if hover_text:
        record["hover_text"] = hover_text
    return record


def _lsp_locations(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, dict):
        if "uri" in result or "targetUri" in result:
            return [result]
        return []
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    return []


def _node_from_lsp_location(
    location: dict[str, Any],
    *,
    nodes_by_path: dict[str, list[Node]],
    repo_root: Path,
) -> Node | None:
    uri = location.get("targetUri") or location.get("uri")
    if not isinstance(uri, str):
        return None
    relative_path = _relative_path_from_uri(uri, repo_root)
    if relative_path is None:
        return None
    range_value = location.get("targetRange") or location.get("range") or {}
    start = range_value.get("start") if isinstance(range_value, dict) else {}
    line = start.get("line") if isinstance(start, dict) else None
    line_number = int(line) + 1 if isinstance(line, int) else None
    candidates = nodes_by_path.get(relative_path, [])
    if line_number is not None:
        containing = [
            node
            for node in candidates
            if node.start_line
            and node.end_line
            and node.start_line <= line_number <= node.end_line
        ]
        if containing:
            return sorted(
                containing,
                key=lambda node: (
                    (node.end_line or node.start_line or 0) - (node.start_line or 0),
                    node.id,
                ),
            )[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _relative_path_from_uri(uri: str, repo_root: Path) -> str | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    raw_path = unquote(parsed.path)
    if sys.platform == "win32" and re.match(r"^/[A-Za-z]:", raw_path):
        raw_path = raw_path[1:]
    try:
        return Path(raw_path).resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return None


def _hover_text(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    contents = result.get("contents")
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        value = contents.get("value")
        return value if isinstance(value, str) else None
    if isinstance(contents, list):
        parts: list[str] = []
        for item in contents:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("value"), str):
                parts.append(item["value"])
        return "\n".join(parts) if parts else None
    return None


def _node_from_hover_text(
    hover_text: str | None, class_nodes_by_name: dict[str, list[Node]]
) -> Node | None:
    if not hover_text:
        return None
    matches_by_id: dict[str, Node] = {}
    for token in set(re.findall(r"(?<![\w.])([A-Za-z_]\w*)(?![\w.])", hover_text)):
        for node in class_nodes_by_name.get(token, []):
            matches_by_id[node.id] = node
    matches = list(matches_by_id.values())
    return matches[0] if len(matches) == 1 else None


class _PyrightTypeProbeVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        file_record: FileRecord,
        aliases: dict[str, str],
        class_targets: dict[str, Node],
        simple_names: dict[str, list[Node]],
    ) -> None:
        self.file_record = file_record
        self.aliases = aliases
        self.class_targets = class_targets
        self.simple_names = simple_names
        self.probes: list[dict[str, Any]] = []
        self.class_stack: list[str] = []
        self.scope_stack: list[str] = [module_id(file_record.module)]
        self._module_classes = {
            qualname.rsplit(".", 1)[-1]: qualname
            for qualname in class_targets
            if qualname.rsplit(".", 1)[0] == file_record.module
        }

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualname = f"{self.file_record.module}.{node.name}"
        scope_id = class_id(qualname)
        self._record_annotations(
            node.decorator_list,
            subject=scope_id,
            name="decorator",
            subject_kind="decorator",
        )
        self._record_annotations(
            node.bases,
            subject=scope_id,
            name="base",
            subject_kind="base_class",
        )
        self.class_stack.append(qualname)
        self.scope_stack.append(scope_id)
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.visit(stmt)
            elif isinstance(stmt, ast.AnnAssign):
                self._record_annotation(
                    stmt.annotation,
                    subject=scope_id,
                    name=self._target_name(stmt.target) or "<class-var>",
                    subject_kind="binding",
                    line=getattr(stmt, "lineno", None),
                    column=getattr(stmt, "col_offset", None),
                )
            elif not isinstance(stmt, ast.ClassDef):
                self.visit(stmt)
        self.scope_stack.pop()
        self.class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if self.class_stack:
            qualname = f"{self.class_stack[-1]}.{node.name}"
            scope_id = method_id(qualname)
        else:
            qualname = f"{self.file_record.module}.{node.name}"
            scope_id = function_id(qualname)
        self.scope_stack.append(scope_id)
        for arg in self._all_arguments(node.args):
            if arg.annotation is not None:
                self._record_annotation(
                    arg.annotation,
                    subject=scope_id,
                    name=arg.arg,
                    subject_kind="parameter",
                    line=getattr(
                        arg.annotation, "lineno", getattr(node, "lineno", None)
                    ),
                    column=getattr(arg.annotation, "col_offset", None),
                )
        if node.returns is not None:
            self._record_annotation(
                node.returns,
                subject=scope_id,
                name="return",
                subject_kind="return",
                line=getattr(node.returns, "lineno", getattr(node, "lineno", None)),
                column=getattr(node.returns, "col_offset", None),
            )
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            self.visit(stmt)
        self.scope_stack.pop()

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._record_annotation(
            node.annotation,
            subject=self.scope_stack[-1],
            name=self._target_name(node.target) or "<local>",
            subject_kind="binding",
            line=getattr(node, "lineno", None),
            column=getattr(node, "col_offset", None),
        )
        if node.value is not None:
            self.visit(node.value)

    def _record_annotations(
        self,
        annotations: Iterable[ast.AST],
        *,
        subject: str,
        name: str,
        subject_kind: str,
    ) -> None:
        for annotation in annotations:
            self._record_annotation(
                annotation,
                subject=subject,
                name=name,
                subject_kind=subject_kind,
                line=getattr(annotation, "lineno", None),
                column=getattr(annotation, "col_offset", None),
            )

    def _record_annotation(
        self,
        annotation: ast.AST,
        *,
        subject: str,
        name: str,
        subject_kind: str,
        line: int | None,
        column: int | None,
    ) -> None:
        expression = self._unparse(annotation)
        self.probes.append(
            {
                "subject": subject,
                "name": name,
                "subject_kind": subject_kind,
                "type": expression,
                "type_expression": expression,
                "expression": expression,
                "path": self.file_record.path,
                "abs_path": self.file_record.abs_path,
                "line": line,
                "column": column,
                "kind": "type_info_probe",
                "source": "pyright_lsp_probe",
                "static_target_ids": [
                    target.id for target in self._annotation_targets(annotation)
                ],
            }
        )

    def _annotation_targets(self, node: ast.AST) -> list[Node]:
        targets: list[Node] = []

        def add(target: Node | None) -> None:
            if target is not None and all(
                existing.id != target.id for existing in targets
            ):
                targets.append(target)

        if isinstance(node, ast.Name):
            add(self._resolve_type_name(node.id))
        elif isinstance(node, ast.Attribute):
            add(self._resolve_qualified_name(self._unparse(node)))
        elif isinstance(node, ast.Subscript):
            for target in self._annotation_targets(node.value):
                add(target)
            for target in self._annotation_targets(node.slice):
                add(target)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            for target in self._annotation_targets(node.left):
                add(target)
            for target in self._annotation_targets(node.right):
                add(target)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for element in node.elts:
                for target in self._annotation_targets(element):
                    add(target)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            parsed = _parse_type_expression(node.value)
            if parsed is not None:
                for target in self._annotation_targets(parsed):
                    add(target)
            else:
                add(self._resolve_type_name(node.value))
        return targets

    def _resolve_type_name(self, name: str) -> Node | None:
        if name in {"None", "NoneType"}:
            return None
        alias = self.aliases.get(name)
        if alias:
            target = self._resolve_qualified_name(alias)
            if target is not None:
                return target
        module_qualname = self._module_classes.get(name)
        if module_qualname:
            return self.class_targets.get(module_qualname)
        candidates = self.simple_names.get(name, [])
        return candidates[0] if len(candidates) == 1 else None

    def _resolve_qualified_name(self, qualname: str) -> Node | None:
        parts = qualname.split(".")
        if parts and parts[0] in self.aliases:
            qualname = ".".join([self.aliases[parts[0]], *parts[1:]])
        return self.class_targets.get(qualname)

    @staticmethod
    def _all_arguments(args: ast.arguments) -> Iterable[ast.arg]:
        yield from args.posonlyargs
        yield from args.args
        if args.vararg is not None:
            yield args.vararg
        yield from args.kwonlyargs
        if args.kwarg is not None:
            yield args.kwarg

    @staticmethod
    def _target_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            try:
                return ast.unparse(node)
            except Exception:
                return node.attr
        return None

    @staticmethod
    def _unparse(node: ast.AST) -> str:
        try:
            return ast.unparse(node)
        except Exception:
            return node.__class__.__name__


def _parse_type_expression(value: str) -> ast.AST | None:
    try:
        return ast.parse(value, mode="eval").body
    except SyntaxError:
        return None


def _run_scip_python_index(
    *,
    repo_root: Path,
    project_name: str,
    project_version: str | None,
    target_only: list[str],
    environment_path: str | Path | None,
    scip_python_command: str,
    timeout_seconds: float,
) -> None:
    command = [
        scip_python_command,
        "index",
        ".",
        "--project-name",
        project_name,
    ]
    if project_version:
        command.extend(["--project-version", project_version])
    for target in target_only:
        command.append(f"--target-only={target}")
    if environment_path is not None:
        command.extend(
            ["--environment", str(_absolute_path(repo_root, environment_path))]
        )
    _run_checked(command, cwd=repo_root, timeout_seconds=timeout_seconds)


def _run_scip_print_json(
    *,
    repo_root: Path,
    scip_index: Path,
    scip_command: str,
) -> Any:
    attempts = (
        [scip_command, "print", "--json", str(scip_index)],
        [scip_command, "print", str(scip_index), "--json"],
    )
    last_error: RuntimeError | None = None
    for command in attempts:
        try:
            completed = _run_checked(command, cwd=repo_root, timeout_seconds=300.0)
        except RuntimeError as exc:
            last_error = exc
            continue
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            last_error = RuntimeError(f"scip print --json produced invalid JSON: {exc}")
    if last_error is not None:
        raise last_error
    raise RuntimeError("Unable to convert SCIP index to JSON.")


def _merge_scip_payloads(payloads: list[Any]) -> dict[str, Any]:
    metadata: Any | None = None
    documents_by_path: dict[str, dict[str, Any]] = {}
    external_symbols_by_id: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        if metadata is None and isinstance(payload.get("metadata"), dict):
            metadata = payload["metadata"]
        for document in payload.get("documents", []):
            if not isinstance(document, dict):
                continue
            path = (
                document.get("relative_path")
                or document.get("relativePath")
                or document.get("path")
                or f"document:{len(documents_by_path)}"
            )
            documents_by_path[str(path)] = document
        for symbol in payload.get("external_symbols", []):
            if not isinstance(symbol, dict):
                continue
            symbol_id = str(
                symbol.get("symbol") or f"external:{len(external_symbols_by_id)}"
            )
            external_symbols_by_id[symbol_id] = symbol
    return {
        "metadata": metadata or {},
        "documents": list(documents_by_path.values()),
        "external_symbols": list(external_symbols_by_id.values()),
    }


def _safe_filename_fragment(value: str) -> str:
    fragment = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip().replace("\\", "/"))
    return fragment.strip("-._") or "target"


def _run_checked(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(_missing_precision_tool_message(command[0])) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Command timed out after {timeout_seconds} seconds: {' '.join(command)}"
        ) from exc
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        detail = stderr or stdout or f"exit code {completed.returncode}"
        raise RuntimeError(f"Command failed ({' '.join(command)}): {detail}")
    return completed


def _missing_precision_tool_message(command: str) -> str:
    name = Path(command).name.lower()
    if name.endswith((".exe", ".cmd", ".bat", ".ps1")):
        name = name.rsplit(".", 1)[0]
    base = f"Required command is not available on PATH: {command}"
    if "scip" not in name:
        return base
    return (
        f"{base}. Install ArcGraph precision tools with "
        "powershell -ExecutionPolicy Bypass -File "
        "scripts\\install-arcgraph-precision-tools.ps1. "
        "On Windows, scip.exe v0.7.1 is built with "
        "go install github.com/scip-code/scip/cmd/scip@v0.7.1; "
        "install Go first with winget install --id GoLang.Go -e if needed."
    )


def _absolute_path(repo_root: Path, path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else (repo_root / candidate).resolve()
