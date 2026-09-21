"""Safe, preview-first setup planning for local ArcGraph trials."""

from __future__ import annotations

import os
import shutil
import stat
import sys
import sysconfig
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

# Windows Python exposes neither O_NOFOLLOW nor fchmod. The flag degrades to
# a no-op there and the explicit lstat refusal below carries the guarantee as
# far as the platform allows, matching how local_state.py handles the same
# pair. Bare use of either name raises AttributeError, which `except OSError`
# would not catch.
_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)

_PREREQUISITE_CHECKS = frozenset(
    {
        "python",
        "repo_root",
        "git_repo_root",
        "state_paths_usable",
        "arcgraph_executable",
    }
)

_BLOCKED_NEXT_STEP = (
    "Inspect the reported failure and rerun setup; do not register the MCP "
    "server from an incomplete state."
)
_NEXT_STEP_BY_ACTION = {
    "dry_run": "Rerun with --apply-local-files to create private local paths.",
    "not_applied": "Resolve failed prerequisite checks before applying local files.",
    "partially_applied": (
        "Local setup stopped part-way; resolve the reported error and rerun "
        "--apply-local-files before registering anything."
    ),
    "local_files_applied": (
        "Run the registration_command only after reviewing every path."
    ),
}


def trial_setup(
    *,
    repo_root: Path,
    client: str,
    apply_local_files: bool = False,
) -> dict[str, Any]:
    if client != "claude":
        raise ValueError(f"Unsupported trial client: {client}")
    repo_root = repo_root.resolve()
    state_root = repo_root / ".arcgraph-trial"
    output_dir = state_root / "index"
    metrics_dir = state_root / "metrics"
    feedback_dir = state_root / "feedback"
    metrics_log = metrics_dir / "mcp.jsonl"
    feedback_log = feedback_dir / "agent.jsonl"
    executable_selection = _arcgraph_executable_selection()
    arcgraph_executable = executable_selection.path
    exclude_path = repo_root / ".git" / "info" / "exclude"
    exclude_entry = ".arcgraph-trial/"

    checks = [
        {
            "name": "python",
            "status": "pass" if sys.version_info >= (3, 11) else "fail",
            "value": sys.version.split()[0],
        },
        {
            "name": "node",
            "status": "pass" if shutil.which("node") else "warn",
            "value": shutil.which("node"),
        },
        {
            "name": "repo_root",
            "status": "pass" if repo_root.is_dir() else "fail",
            "value": str(repo_root),
        },
        {
            "name": "git_repo_root",
            # The exclude write targets .git/info/exclude directly, so apply
            # mode requires a repository root holding a real .git directory;
            # a worktree's .git file, a symlinked .git, or a non-git directory
            # is fail-closed here instead of fabricating a phantom .git tree
            # or writing through the link.
            "status": (
                "pass"
                if not (repo_root / ".git").is_symlink()
                and (repo_root / ".git").is_dir()
                else "fail"
            ),
            "value": str(repo_root / ".git"),
        },
        _symlink_free_check(
            repo_root,
            (state_root, output_dir, metrics_dir, feedback_dir),
            (metrics_log, feedback_log, exclude_path),
        ),
        {
            "name": "arcgraph_executable",
            "status": (
                "fail"
                if not arcgraph_executable.is_file()
                or not os.access(arcgraph_executable, os.X_OK)
                else (
                    "pass"
                    if executable_selection.source
                    in {"invocation", "distribution_record"}
                    else "warn"
                )
            ),
            "value": str(arcgraph_executable),
            "selection_source": executable_selection.source,
            **(
                {
                    "message": (
                        "Executable exists but was not identified by the current "
                        "invocation or the installed distribution RECORD; verify "
                        "that it belongs to this ArcGraph installation."
                    )
                }
                if executable_selection.source
                not in {"invocation", "distribution_record"}
                and arcgraph_executable.is_file()
                and os.access(arcgraph_executable, os.X_OK)
                else {}
            ),
        },
        _permission_check(metrics_dir, expected=0o700, kind="directory"),
        _permission_check(feedback_dir, expected=0o700, kind="directory"),
        _permission_check(metrics_log, expected=0o600, kind="file"),
        _permission_check(feedback_log, expected=0o600, kind="file"),
        {
            "name": "git_exclude",
            "status": (
                "pass" if _exclude_contains(exclude_path, exclude_entry) else "planned"
            ),
            "value": str(exclude_path),
        },
    ]

    prerequisite_failures = [
        check
        for check in checks
        if check["name"] in _PREREQUISITE_CHECKS and check["status"] == "fail"
    ]
    if apply_local_files and prerequisite_failures:
        return _setup_payload(
            client=client,
            repo_root=repo_root,
            output_dir=output_dir,
            metrics_log=metrics_log,
            feedback_log=feedback_log,
            exclude_path=exclude_path,
            arcgraph_executable=arcgraph_executable,
            checks=checks,
            action="not_applied",
            status="blocked",
            exit_code=1,
        )

    if apply_local_files:
        try:
            for directory in (state_root, metrics_dir, feedback_dir):
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                directory.chmod(0o700)
            output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            output_dir.chmod(0o700)
            for path in (metrics_log, feedback_log):
                # O_NOFOLLOW and fchmod close the window between the
                # prerequisite check and the write: a link planted in between
                # cannot redirect either the creation or the mode change.
                descriptor = os.open(
                    path,
                    os.O_CREAT | os.O_APPEND | os.O_WRONLY | _NO_FOLLOW,
                    0o600,
                )
                try:
                    if _NO_FOLLOW == 0 and path.is_symlink():
                        # Without O_NOFOLLOW the open cannot refuse a link,
                        # so the refusal is explicit rather than assumed.
                        raise OSError(f"Refusing to write through a link: {path}")
                    if os.name == "posix":
                        os.fchmod(descriptor, 0o600)
                finally:
                    os.close(descriptor)
            _append_local_exclude(exclude_path, exclude_entry)
        except OSError as error:
            # The prerequisites rule out the states we can name, but the file
            # system can still refuse mid-apply (a race against the check, a
            # read-only mount, an exhausted disk). A setup command reports
            # that as a structured failure, never as a traceback.
            checks = [
                *checks,
                {
                    "name": "apply_local_files",
                    "status": "fail",
                    "value": f"{type(error).__name__}: {error}",
                },
            ]
            return _setup_payload(
                client=client,
                repo_root=repo_root,
                output_dir=output_dir,
                metrics_log=metrics_log,
                feedback_log=feedback_log,
                exclude_path=exclude_path,
                arcgraph_executable=arcgraph_executable,
                checks=checks,
                action="partially_applied",
                status="blocked",
                exit_code=1,
            )
        checks = [
            *[
                check
                for check in checks
                if check["name"]
                in {
                    "python",
                    "node",
                    "repo_root",
                    "git_repo_root",
                    "state_paths_usable",
                    "arcgraph_executable",
                }
            ],
            _permission_check(metrics_dir, expected=0o700, kind="directory"),
            _permission_check(feedback_dir, expected=0o700, kind="directory"),
            _permission_check(metrics_log, expected=0o600, kind="file"),
            _permission_check(feedback_log, expected=0o600, kind="file"),
            {
                "name": "git_exclude",
                "status": (
                    "pass" if _exclude_contains(exclude_path, exclude_entry) else "fail"
                ),
                "value": str(exclude_path),
            },
        ]

    return _setup_payload(
        client=client,
        repo_root=repo_root,
        output_dir=output_dir,
        metrics_log=metrics_log,
        feedback_log=feedback_log,
        exclude_path=exclude_path,
        arcgraph_executable=arcgraph_executable,
        checks=checks,
        action="local_files_applied" if apply_local_files else "dry_run",
        status=(
            "ready"
            if all(check["status"] not in {"fail", "warn"} for check in checks)
            else "review"
        ),
    )


def _next_step(action: str, checks: list[dict[str, Any]]) -> str:
    if action == "dry_run" and any(
        check["name"] in _PREREQUISITE_CHECKS and check["status"] == "fail"
        for check in checks
    ):
        return _NEXT_STEP_BY_ACTION["not_applied"]
    return _NEXT_STEP_BY_ACTION.get(action, _BLOCKED_NEXT_STEP)


def _setup_payload(
    *,
    client: str,
    repo_root: Path,
    output_dir: Path,
    metrics_log: Path,
    feedback_log: Path,
    exclude_path: Path,
    arcgraph_executable: Path,
    checks: list[dict[str, Any]],
    action: str,
    status: str,
    exit_code: int = 0,
) -> dict[str, Any]:
    serve_command = [
        str(arcgraph_executable),
        "mcp",
        "serve",
        "--repo-root",
        str(repo_root),
        "--output-dir",
        str(output_dir),
        "--metrics-log",
        str(metrics_log),
        "--feedback-log",
        str(feedback_log),
    ]
    registration_command = [
        "claude",
        "mcp",
        "add",
        "--scope",
        "local",
        "arcgraph",
        "--",
        *serve_command,
    ]
    payload = {
        "schema_version": "1.0.0",
        "status": status,
        "action": action,
        "client": client,
        "repo_root": str(repo_root),
        "checks": checks,
        "paths": {
            "state_root": str(output_dir.parent),
            "output_dir": str(output_dir),
            "metrics_log": str(metrics_log),
            "feedback_log": str(feedback_log),
            "git_exclude": str(exclude_path),
        },
        "serve_command": serve_command,
        "registration_command": registration_command,
        "claude_config_modified": False,
        # Keyed by action rather than chained, so a new action can never fall
        # through to the registration advice: only a completed apply has
        # earned that, and a half-written state has not. A dry run whose
        # prerequisites already failed has not earned the apply advice
        # either, so the checks decide there — the action alone does not
        # know whether applying would work.
        "next_steps": [_next_step(action, checks)],
    }
    if exit_code:
        payload["_exit_code"] = exit_code
    return payload


@dataclass(frozen=True, slots=True)
class _ArcgraphExecutableSelection:
    path: Path
    source: str


def _arcgraph_executable_selection() -> _ArcgraphExecutableSelection:
    """Locate the console script belonging to the imported distribution.

    A venv happens to place scripts beside ``sys.executable``; base and user
    installs need not.  PATH is not an identity signal either, because it may
    select another ArcGraph installation.  Prefer the exact invocation, then
    the active distribution's RECORD entry, and finally interpreter/user
    scheme paths.  If none exists, return the primary expected path so the
    prerequisite check fails closed with a useful value.
    """

    script_names = {"arcgraph", "arcgraph.exe"}
    invoked = Path(sys.argv[0]).expanduser()
    candidates: list[tuple[str, Path]] = []
    if invoked.name.lower() in script_names:
        candidates.append(("invocation", invoked))

    try:
        distribution = importlib_metadata.distribution("arcgraph")
    except importlib_metadata.PackageNotFoundError:
        distribution = None
    if distribution is not None:
        for entry in distribution.files or ():
            if Path(str(entry)).name.lower() in script_names:
                candidates.append(
                    ("distribution_record", Path(distribution.locate_file(entry)))
                )

    suffix = ".exe" if os.name == "nt" else ""
    expected_name = f"arcgraph{suffix}"
    candidates.append(
        ("interpreter_scheme", Path(sysconfig.get_path("scripts")) / expected_name)
    )
    try:
        user_scheme = sysconfig.get_preferred_scheme("user")
        candidates.append(
            (
                "user_scheme",
                Path(sysconfig.get_path("scripts", scheme=user_scheme)) / expected_name,
            )
        )
    except (KeyError, AttributeError):
        pass
    candidates.append(
        ("interpreter_sibling", Path(sys.executable).parent / expected_name)
    )

    seen: set[Path] = set()
    for source, candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return _ArcgraphExecutableSelection(resolved, source)
    source, candidate = candidates[0]
    return _ArcgraphExecutableSelection(candidate.resolve(), f"missing_{source}")


def _arcgraph_executable() -> Path:
    """Compatibility wrapper for callers that only need the selected path."""

    return _arcgraph_executable_selection().path


def _symlink_free_check(
    repo_root: Path,
    directories: tuple[Path, ...],
    files: tuple[Path, ...],
) -> dict[str, Any]:
    """Fail closed when a path apply mode writes is not what it must be.

    Two conditions, both prerequisites because the contract is refuse
    implies create nothing — a guard that runs mid-apply has already
    chmod'ed the state root by the time it objects:

    * No component below the root may be a symbolic link. repo_root is
      resolved and every path is literal with no parent references, so that
      is the closed form of "every write lands inside the repository".
    * Every path that already exists must be the type apply mode will use
      it as. A plain file where a directory belongs (or a FIFO, socket, or
      device anywhere) is not a link, so the link rule alone lets it reach
      mkdir and raise.
    """

    offenders: list[str] = []
    for path in (*directories, *files):
        try:
            relative = path.relative_to(repo_root)
        except ValueError:
            offenders.append(str(path))
            continue
        current = repo_root
        crossed_link = False
        for part in relative.parts:
            current = current / part
            # lstat-based, so a dangling link is an offender too.
            if current.is_symlink():
                offenders.append(str(current))
                crossed_link = True
                break
        if crossed_link:
            continue
        if not path.exists():
            continue
        expected_is_directory = path in directories
        actual_is_directory = path.is_dir()
        actual_is_file = path.is_file()
        if expected_is_directory and not actual_is_directory:
            offenders.append(str(path))
        elif not expected_is_directory and not actual_is_file:
            offenders.append(str(path))
    unique_offenders = list(dict.fromkeys(offenders))
    return {
        "name": "state_paths_usable",
        "status": "fail" if unique_offenders else "pass",
        "value": unique_offenders or None,
    }


def _permission_check(path: Path, *, expected: int, kind: str) -> dict[str, Any]:
    if os.name != "posix":
        # The platform decides this, not the path: Windows chmod carries only
        # the read-only flag, so these bits can never be what was asked for.
        # "fail" would blame the wrong thing — setup did not fail to set the
        # mode, the mode cannot express the guarantee there; it needs ACLs,
        # which this command does not set. Reported before the existence
        # branch so a first dry run, when nothing is created yet, states the
        # real boundary instead of previewing a guarantee it will not deliver.
        return {
            "name": f"{kind}_permissions",
            "path": str(path),
            "status": "warn",
            "expected": f"{expected:#o}",
            **(
                {"actual": f"{stat.S_IMODE(path.stat().st_mode):#o}"}
                if path.exists()
                else {"actual": None}
            ),
            "detail": (
                "Mode-based private state is POSIX-only; on this platform the "
                "0700/0600 guarantee is not enforced by setup."
            ),
        }
    if not path.exists():
        return {
            "name": f"{kind}_permissions",
            "path": str(path),
            "status": "planned",
            "expected": f"{expected:#o}",
        }
    actual = stat.S_IMODE(path.stat().st_mode)
    return {
        "name": f"{kind}_permissions",
        "path": str(path),
        "status": "pass" if actual == expected else "fail",
        "expected": f"{expected:#o}",
        "actual": f"{actual:#o}",
    }


def _exclude_contains(path: Path, entry: str) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return entry in {line.strip() for line in lines}


def _append_local_exclude(path: Path, entry: str) -> None:
    # parents=False: only info/ may be created inside an existing .git
    # directory; a missing .git must never be fabricated by this write.
    path.parent.mkdir(parents=False, exist_ok=True)
    # O_NOFOLLOW, matching the log writes: the prerequisite check rejects a
    # symlink here, but a link swapped in after that check would otherwise
    # redirect this write outside the repository. Read and append through
    # the one descriptor so the containment test cannot race the write
    # either. (A parent directory swapped mid-apply is not covered by the
    # final-component flag; that is the same residual window the log writes
    # carry, and it is what the prerequisite check exists to narrow.)
    if _NO_FOLLOW == 0 and path.is_symlink():
        raise OSError(f"Refusing to write through a link: {path}")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | _NO_FOLLOW, 0o644)
    with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
        prior = handle.read()
        if entry in {line.strip() for line in prior.splitlines()}:
            return
        separator = "" if not prior or prior.endswith("\n") else "\n"
        handle.write(f"{separator}{entry}\n")
