"""Standalone pytest recorder; only stdlib and project-environment pytest.

Executed as a file by trace.py, so ArcGraph need not be installed in the project.
The same recorder is also imported by the in-process compatibility path.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any


class _RuntimeTraceRecorder:
    def __init__(
        self,
        *,
        repo_root: Path,
        source_roots: list[Any],
        max_events: int,
        max_seconds: float,
    ) -> None:
        self.repo_root = repo_root
        self.source_roots = source_roots
        self.max_events = max(1, max_events)
        self.max_seconds = max_seconds
        self.events: list[dict[str, Any]] = []
        self.diagnostics: list[dict[str, Any]] = []
        self.truncated_events = 0
        self._current_test_id: str | None = None
        self._current_test_symbol: str | None = None
        self._started = 0.0

    def trace_test_call(self, item: Any) -> Any:
        self._current_test_id = str(item.nodeid)
        self._current_test_symbol = self._test_symbol(item)
        self._started = time.monotonic()
        previous_sys_profile = sys.getprofile()
        previous_thread_profile = threading.getprofile()
        sys.setprofile(self._profile)
        threading.setprofile(self._profile)
        try:
            yield
        finally:
            sys.setprofile(previous_sys_profile)
            threading.setprofile(previous_thread_profile)
            self._current_test_id = None
            self._current_test_symbol = None

    def _profile(self, frame: Any, event: str, arg: Any) -> None:
        if event != "call":
            return
        if len(self.events) >= self.max_events:
            self.truncated_events += 1
            return
        if time.monotonic() - self._started > self.max_seconds:
            self.diagnostics.append(
                {
                    "kind": "wall_time_limit_exceeded",
                    "message": f"Runtime trace exceeded max_seconds={self.max_seconds}.",
                    "path": self._frame_path(frame),
                }
            )
            sys.setprofile(None)
            return
        source = self._frame_symbol(frame.f_back) if frame.f_back is not None else None
        target = self._frame_symbol(frame)
        if target == self._current_test_symbol:
            return
        if source is None and target is not None:
            source = self._current_test_symbol
        if source is None or target is None or source == target:
            return
        path = self._frame_path(frame)
        if path is None:
            return
        self.events.append(
            {
                "kind": "call",
                "source": source,
                "target": target,
                "path": path,
                "line": frame.f_lineno,
                "test_id": self._current_test_id,
            }
        )

    def _frame_symbol(self, frame: Any) -> str | None:
        path = self._frame_path(frame)
        if path is None:
            return None
        module = self._module_for_path(path)
        if module is None:
            return None
        function_name = frame.f_code.co_name
        qualname = getattr(frame.f_code, "co_qualname", function_name)
        if function_name == "<module>":
            return f"mod:{module}"
        if function_name.startswith("<") or "<locals>" in qualname:
            return None
        if "." in qualname:
            return f"method:{module}.{qualname}"
        return f"fn:{module}.{function_name}"

    def _frame_path(self, frame: Any) -> str | None:
        filename = frame.f_code.co_filename
        try:
            resolved = Path(filename).resolve()
            relative = resolved.relative_to(self.repo_root)
        except (OSError, ValueError):
            return None
        if self._is_ignored_runtime_path(relative):
            return None
        return relative.as_posix()

    def _test_symbol(self, item: Any) -> str | None:
        test_obj = getattr(item, "obj", None)
        item_path = getattr(item, "path", None) or getattr(item, "fspath", None)
        if item_path is None:
            return None
        try:
            relative_path = Path(item_path).resolve().relative_to(self.repo_root)
        except (OSError, ValueError):
            return None
        module = self._module_for_path(relative_path.as_posix())
        qualname = getattr(test_obj, "__qualname__", None)
        if not isinstance(module, str) or not module:
            return None
        if not isinstance(qualname, str) or not qualname:
            return None
        if "<locals>" in qualname or qualname.startswith("<"):
            return None
        return (
            f"method:{module}.{qualname}"
            if "." in qualname
            else f"fn:{module}.{qualname}"
        )

    def _module_for_path(self, path: str) -> str | None:
        for source_root in self.source_roots:
            root = Path(source_root.path)
            try:
                relative = Path(path).relative_to(root)
            except ValueError:
                continue
            if relative.suffix != ".py":
                return None
            parts = list(relative.parts)
            parts[-1] = relative.stem
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if source_root.module_prefix:
                parts.insert(0, source_root.module_prefix)
            return ".".join(parts)
        return None

    @staticmethod
    def _is_ignored_runtime_path(path: Path) -> bool:
        ignored_parts = {
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".venv",
            "__pycache__",
            "node_modules",
            "site-packages",
            "venv",
        }
        return any(part in ignored_parts for part in path.parts)


def run_project_pytest(config):
    import pytest

    class Recorder(_RuntimeTraceRecorder):
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_call(self, item):
            yield from self.trace_test_call(item)

    root = Path(config["repo_root"])
    roots = [SimpleNamespace(**entry) for entry in config["source_roots"]]
    for entry in reversed(roots):
        sys.path.insert(0, str(root / entry.path))
    recorder = Recorder(
        repo_root=root,
        source_roots=roots,
        max_events=config["max_events"],
        max_seconds=config["max_seconds"],
    )
    start = time.monotonic()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    code = pytest.main(config["pytest_args"], plugins=[recorder])
    duration = time.monotonic() - start
    payload = {
        "trace_run": {
            "trace_run_id": f"pytest-{int(time.time() * 1000)}",
            "source": "pytest",
            "commit_sha": config["commit_sha"],
            "started_at": started_at,
            "duration_seconds": round(duration, 4),
            "frontend_version": "ArcGraph-runtime-trace-v1",
            "python_executable": sys.executable,
            "python_prefix": sys.prefix,
            "python_version": sys.version.split()[0],
            "interpreter_source": config["interpreter_source"],
        },
        "events": recorder.events,
        "diagnostics": recorder.diagnostics,
    }
    Path(config["output"]).write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    result = {
        "status": "pass" if code == 0 else "fail",
        "pytest_exit_code": int(code),
        "events": len(recorder.events),
        "truncated_events": recorder.truncated_events,
        "duration_seconds": round(duration, 4),
    }
    Path(config["result"]).write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    run_project_pytest(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
