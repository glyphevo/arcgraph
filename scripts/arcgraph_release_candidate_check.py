"""Installed-wheel release candidate smoke for ArcGraph maintainers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import venv
import zipfile
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Sequence

DOC_TOPICS = (
    "quickstart",
    "security-model",
    "release-checklist",
    "schema-governance",
    "frontend-contract",
    "limitations",
    "evidence-cookbook",
)
REQUIRED_WORKBENCH_FILES = (
    "index.html",
    "graph_data.json",
    "status_data.json",
    "vendor/d3.v7.min.js",
    "vendor/LICENSE.d3.txt",
)
SCHEMA_VERSION = "1.2"
MAX_CAPTURE_CHARS = 4000
BUILD_PROVENANCE_MEMBER = "arcgraph/_build_provenance.json"
REBUILD_CHECK = "clean-rebuild-identical"
# A clean isolated build installs its backend from the package index, so it is
# allowed several times the per-command limit.
REBUILD_TIMEOUT_FACTOR = 5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run an installed-wheel v0.1 release candidate smoke in a temporary "
            "virtual environment."
        )
    )
    parser.add_argument(
        "--wheel",
        required=True,
        type=Path,
        help="Path to the ArcGraph wheel to install and smoke.",
    )
    parser.add_argument(
        "--sdist",
        required=True,
        type=Path,
        help=(
            "Path to the ArcGraph sdist that ships with the wheel. Both files "
            "are compared byte for byte with a clean rebuild of the repository "
            "head."
        ),
    )
    parser.add_argument(
        "--repo-root",
        default=Path("."),
        type=Path,
        help="Source repository root with a fresh current ArcGraph index.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON report output path. Defaults to stdout.",
    )
    parser.add_argument(
        "--timeout-seconds",
        default=60.0,
        type=float,
        help="Per-command timeout in seconds.",
    )
    parser.add_argument(
        "--allow-source-mismatch",
        action="store_true",
        help=(
            "Record a disclosed warning instead of failing when the wheel's "
            "embedded build provenance disagrees with the repository head: a "
            "different source commit or tree, a build over an unclean working "
            "tree, or provenance carrying no cleanliness claim. The report "
            "status then becomes 'warn' rather than 'pass'. The same flag "
            "downgrades a difference from the clean rebuild to a disclosed "
            "warning. Use only to re-validate a development or historical "
            "wheel on purpose."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    exit_code, payload = run_release_candidate_check(
        wheel_path=args.wheel,
        sdist_path=args.sdist,
        repo_root=args.repo_root,
        output_path=args.output,
        timeout_seconds=args.timeout_seconds,
        allow_source_mismatch=args.allow_source_mismatch,
    )
    if args.output is None:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(
            "ArcGraph RC smoke: "
            f"status={payload['status']} checks={len(payload['checks'])} "
            f"warnings={len(payload['warnings'])} "
            f"failures={len(payload['failures'])} "
            f"output={args.output}"
        )
    return exit_code


def run_release_candidate_check(
    *,
    wheel_path: Path,
    sdist_path: Path,
    repo_root: Path,
    output_path: Path | None = None,
    timeout_seconds: float = 60.0,
    allow_source_mismatch: bool = False,
) -> tuple[int, dict[str, Any]]:
    wheel = wheel_path.resolve()
    sdist = sdist_path.resolve()
    repo = repo_root.resolve()
    started = time.perf_counter()
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "pass",
        "started_at": _utc_now(),
        "finished_at": None,
        "duration_seconds": None,
        "wheel": str(wheel),
        "sdist": str(sdist),
        "repo_root": str(repo),
        "checks": [],
        "failures": [],
        "warnings": [],
        "artifacts": {},
        "environment": {
            "python": sys.version.split()[0],
            "python_executable": sys.executable,
            "platform": sys.platform,
        },
    }

    if timeout_seconds <= 0:
        _add_failure(payload, "validation", "--timeout-seconds must be greater than 0.")
        return _finish(payload, started, output_path)
    if not wheel.is_file():
        _add_failure(payload, "validation", f"Wheel does not exist: {wheel}")
        return _finish(payload, started, output_path)
    if not sdist.is_file():
        _add_failure(payload, "validation", f"Sdist does not exist: {sdist}")
        return _finish(payload, started, output_path)
    if not repo.is_dir():
        _add_failure(payload, "validation", f"Repo root does not exist: {repo}")
        return _finish(payload, started, output_path)

    if not _check_source_provenance(
        payload,
        wheel=wheel,
        repo=repo,
        allow_source_mismatch=allow_source_mismatch,
    ):
        return _finish(payload, started, output_path)

    if not _check_clean_rebuild(
        payload,
        wheel=wheel,
        sdist=sdist,
        repo=repo,
        timeout_seconds=timeout_seconds,
        allow_source_mismatch=allow_source_mismatch,
    ):
        return _finish(payload, started, output_path)

    with tempfile.TemporaryDirectory(prefix="arcgraph-rc-smoke-") as temp_dir:
        temp_root = Path(temp_dir)
        venv_dir = temp_root / "venv"
        try:
            venv_started = time.perf_counter()
            _create_virtualenv(venv_dir)
            payload["checks"].append(
                {
                    "name": "create-venv",
                    "status": "pass",
                    "duration_seconds": round(time.perf_counter() - venv_started, 3),
                }
            )
        except Exception as exc:  # pragma: no cover - defensive setup path.
            payload["checks"].append(
                {
                    "name": "create-venv",
                    "status": "fail",
                    "duration_seconds": round(time.perf_counter() - venv_started, 3),
                    "error": str(exc),
                }
            )
            _add_failure(payload, "create-venv", str(exc))
            return _finish(payload, started, output_path)

        venv_python = _venv_python(venv_dir)
        arcgraph_cli = _arcgraph_cli(venv_dir)
        payload["artifacts"]["venv_python"] = str(venv_python)
        if not _run_required(
            payload,
            name="install-wheel",
            command=[str(venv_python), "-m", "pip", "install", str(wheel)],
            cwd=repo,
            timeout_seconds=timeout_seconds,
        ):
            return _finish(payload, started, output_path)

        commands: list[tuple[str, list[str], Path]] = [
            ("arcgraph-help", [str(arcgraph_cli), "--help"], repo),
            *(
                (
                    f"docs-{topic}",
                    [str(arcgraph_cli), "docs", topic],
                    repo,
                )
                for topic in DOC_TOPICS
            ),
            ("repo-current", [str(arcgraph_cli), "current"], repo),
            (
                "benchmark-suite",
                [
                    str(arcgraph_cli),
                    "benchmark",
                    "suite",
                    "--iterations",
                    "1",
                    "--warmups",
                    "0",
                    "--output",
                    str(temp_root / "benchmark-suite.json"),
                ],
                repo,
            ),
        ]
        for name, command, cwd in commands:
            if not _run_required(
                payload,
                name=name,
                command=command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            ):
                return _finish(payload, started, output_path)

        try:
            fixture_project = _copy_fixture_project(repo, temp_root / "fixture-project")
        except Exception as exc:
            payload["checks"].append(
                {
                    "name": "copy-fixture-project",
                    "status": "fail",
                    "error": str(exc),
                }
            )
            _add_failure(payload, "copy-fixture-project", str(exc))
            return _finish(payload, started, output_path)
        payload["artifacts"]["fixture_project"] = str(fixture_project)
        fixture_commands = (
            ("fixture-build", [str(arcgraph_cli), "build"], fixture_project),
            ("fixture-current", [str(arcgraph_cli), "current"], fixture_project),
            (
                "fixture-workbench",
                [
                    str(arcgraph_cli),
                    "visual",
                    "workbench",
                    "--output-dir",
                    str(fixture_project / "workbench"),
                ],
                fixture_project,
            ),
        )
        for name, command, cwd in fixture_commands:
            if not _run_required(
                payload,
                name=name,
                command=command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            ):
                return _finish(payload, started, output_path)

        workbench_dir = fixture_project / "workbench"
        payload["artifacts"]["fixture_workbench"] = str(workbench_dir)
        _verify_workbench_assets(payload, workbench_dir)

    return _finish(payload, started, output_path)


def _check_source_provenance(
    payload: dict[str, Any],
    *,
    wheel: Path,
    repo: Path,
    allow_source_mismatch: bool,
) -> bool:
    """Bind the validated wheel bytes to a source commit.

    A product version is not a unique artifact identity: more than one wheel
    can carry the same version label. This check records the wheel digest and
    compares the wheel's embedded build provenance against the repository head
    so evidence can never silently describe different bytes than the checkout
    it is filed under.
    """
    wheel_sha256 = _sha256(wheel)
    payload["artifacts"]["wheel_sha256"] = wheel_sha256

    wheel_source = _wheel_build_provenance(wheel)
    head = _repo_head_provenance(repo)
    payload["artifacts"]["wheel_source"] = wheel_source
    payload["artifacts"]["repo_head"] = head

    wheel_facts: dict[str, Any] = wheel_source or {}
    repo_facts: dict[str, Any] = head or {}
    check: dict[str, Any] = {
        "name": "wheel-source-provenance",
        "wheel_sha256": wheel_sha256,
        "wheel_commit_sha": wheel_facts.get("commit_sha"),
        "wheel_tree_sha": wheel_facts.get("tree_sha"),
        "wheel_working_tree_clean": wheel_facts.get("working_tree_clean"),
        "repo_commit_sha": repo_facts.get("commit_sha"),
        "repo_tree_sha": repo_facts.get("tree_sha"),
    }

    if wheel_source is None or head is None:
        missing = "wheel" if wheel_source is None else "repository"
        check["status"] = "warn"
        check["detail"] = (
            f"No {missing} source provenance is available; the validated bytes "
            "are not bound to a source commit by this report."
        )
        payload["checks"].append(check)
        _add_warning(payload, "wheel-source-provenance", check["detail"])
        return True

    matches_head = wheel_source.get("commit_sha") == head.get(
        "commit_sha"
    ) and wheel_source.get("tree_sha") == head.get("tree_sha")
    built_clean = wheel_source.get("working_tree_clean")

    if matches_head and built_clean is True:
        check["status"] = "pass"
        payload["checks"].append(check)
        return True

    if not matches_head:
        detail = (
            "Wheel build provenance does not match the repository head: wheel "
            f"commit {wheel_source.get('commit_sha')!r} tree "
            f"{wheel_source.get('tree_sha')!r} versus repository commit "
            f"{head.get('commit_sha')!r} tree {head.get('tree_sha')!r}."
        )
    elif built_clean is False:
        detail = (
            "Wheel build provenance matches the repository head but records "
            "an unclean build working tree, so the wheel bytes are not the "
            f"bytes commit {wheel_source.get('commit_sha')!r} describes."
        )
    else:
        detail = (
            "Wheel build provenance records no working-tree cleanliness claim, "
            "so the wheel bytes cannot be bound to commit "
            f"{wheel_source.get('commit_sha')!r}."
        )
    check["detail"] = detail
    if allow_source_mismatch:
        check["status"] = "warn"
        check["allowed_by"] = "--allow-source-mismatch"
        payload["checks"].append(check)
        _add_warning(payload, "wheel-source-provenance", detail)
        return True

    check["status"] = "fail"
    payload["checks"].append(check)
    _add_failure(payload, "wheel-source-provenance", detail)
    return False


def _check_clean_rebuild(
    payload: dict[str, Any],
    *,
    wheel: Path,
    sdist: Path,
    repo: Path,
    timeout_seconds: float,
    allow_source_mismatch: bool,
) -> bool:
    """Require the exact wheel and sdist to be what the head commit builds.

    The content checks in package readiness enumerate what a package may
    contain, so they cannot vouch for a field nobody listed (a dependency
    declaration, an entry point) once an archive is edited and its RECORD
    regenerated. A clean, isolated rebuild of the repository head is the
    reference instead: the validated files must equal its output byte for byte.
    """
    head = payload["artifacts"]["repo_head"]
    wheel_sha256 = _sha256(wheel)
    sdist_sha256 = _sha256(sdist)
    payload["artifacts"]["sdist_sha256"] = sdist_sha256
    check: dict[str, Any] = {
        "name": REBUILD_CHECK,
        "wheel": wheel.name,
        "sdist": sdist.name,
        "wheel_sha256": wheel_sha256,
        "sdist_sha256": sdist_sha256,
    }
    if head is None:
        check["status"] = "warn"
        check["detail"] = (
            "No repository head is available to rebuild, so the validated "
            "bytes are not compared with a clean build."
        )
        payload["checks"].append(check)
        _add_warning(payload, REBUILD_CHECK, check["detail"])
        return True

    started = time.perf_counter()
    outcome = _rebuild_in_clean_clone(
        repo,
        commit_sha=head["commit_sha"],
        artifacts={"wheel": wheel, "sdist": sdist},
        timeout_seconds=timeout_seconds * REBUILD_TIMEOUT_FACTOR,
    )
    check.update(outcome["facts"])
    check["commit_sha"] = head["commit_sha"]
    check["tree_sha"] = head["tree_sha"]
    check["duration_seconds"] = round(time.perf_counter() - started, 3)
    problems = list(outcome["problems"])
    for role in ("wheel", "sdist"):
        if f"rebuilt_{role}_sha256" not in check:
            problems.append(f"No rebuilt {role} was hashed for comparison.")
    if not problems:
        check["status"] = "pass"
        payload["checks"].append(check)
        return True

    detail = (
        "The validated wheel and sdist are not what a clean isolated build of "
        f"commit {head['commit_sha']} produces: " + " ".join(problems)
    )
    check["detail"] = detail
    if allow_source_mismatch:
        check["status"] = "warn"
        check["allowed_by"] = "--allow-source-mismatch"
        payload["checks"].append(check)
        _add_warning(payload, REBUILD_CHECK, detail)
        return True

    check["status"] = "fail"
    payload["checks"].append(check)
    _add_failure(payload, REBUILD_CHECK, detail)
    return False


def _rebuild_in_clean_clone(
    repo: Path,
    *,
    commit_sha: str,
    artifacts: dict[str, Path],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Build the commit in a fresh local clone and compare it to ``artifacts``.

    Returns the recorded tool facts and a list of problems; no problem means
    every expected file exists in the rebuild with identical bytes.
    """
    facts: dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "build_frontend": _distribution_version("build"),
        "artifact_build_backend": _wheel_generator(artifacts["wheel"]),
    }
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="arcgraph-rc-rebuild-") as temp_dir:
        source = Path(temp_dir) / "source"
        dist = Path(temp_dir) / "dist"
        steps = (
            (
                "clone",
                ["git", "clone", "--quiet", "--no-local", "--single-branch"]
                + ["--no-checkout", str(repo), str(source)],
                repo,
            ),
            (
                "checkout",
                ["git", "checkout", "--quiet", "--detach", commit_sha],
                source,
            ),
            (
                "build",
                [sys.executable, "-m", "build", "--outdir", str(dist)],
                source,
            ),
        )
        for name, command, cwd in steps:
            step = _run_command(
                name=f"rebuild-{name}",
                command=command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            )
            if step["status"] != "pass":
                reason = step["error"] or step["stderr_tail"] or "no output"
                problems.append(f"The clean rebuild step {name!r} failed: {reason}")
                return {"facts": facts, "problems": problems}

        facts["build_requires"] = _build_requires(source / "pyproject.toml")
        produced = sorted(path.name for path in dist.iterdir())
        expected = sorted(path.name for path in artifacts.values())
        if produced != expected:
            problems.append(f"The rebuild produced {produced}, expected {expected}.")
        for role, original in artifacts.items():
            rebuilt = dist / original.name
            if not _is_regular_file(rebuilt):
                problems.append(
                    f"The rebuild has no regular file named {original.name} "
                    "(it is missing, a directory, a link or a special file)."
                )
                continue
            rebuilt_sha256 = _sha256(rebuilt)
            facts[f"rebuilt_{role}_sha256"] = rebuilt_sha256
            if role == "wheel":
                facts["build_backend"] = _wheel_generator(rebuilt)
            if rebuilt.read_bytes() != original.read_bytes():
                problems.append(
                    f"The {role} {original.name} differs from the rebuild "
                    f"(rebuilt sha256 {rebuilt_sha256})."
                )
    return {"facts": facts, "problems": problems}


def _is_regular_file(path: Path) -> bool:
    """Return whether ``path`` itself is a regular file, not a link to one."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _distribution_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _build_requires(pyproject: Path) -> list[str] | None:
    try:
        parsed = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if "build-system" not in parsed:
        return None
    requires = parsed["build-system"]["requires"]
    return [str(requirement) for requirement in requires]


def _wheel_generator(wheel: Path) -> str | None:
    """Return the build backend a wheel names in its ``WHEEL`` file."""
    dist_info = "-".join(wheel.name.split("-")[:2]) + ".dist-info"
    raw = _read_wheel_member(wheel, f"{dist_info}/WHEEL")
    if raw is None:
        return None
    return _generator_line(str(raw, "utf-8", "replace"))


def _read_wheel_member(wheel: Path, member: str) -> bytes | None:
    try:
        with zipfile.ZipFile(wheel) as archive:
            return archive.read(member)
    except (KeyError, OSError, zipfile.BadZipFile):
        return None


def _generator_line(text: str) -> str | None:
    prefix = "Generator:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None


def _wheel_build_provenance(wheel: Path) -> dict[str, Any] | None:
    raw = _read_wheel_member(wheel, BUILD_PROVENANCE_MEMBER)
    if raw is None:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    raw_source = payload.get("source") if isinstance(payload, dict) else None
    if not isinstance(raw_source, dict):
        return None
    source: dict[str, Any] = raw_source
    commit = source.get("commit_sha")
    tree = source.get("tree_sha")
    if not isinstance(commit, str) or not isinstance(tree, str):
        return None
    if not commit or not tree:
        return None
    clean = source.get("working_tree_clean")
    return {
        "commit_sha": commit,
        "tree_sha": tree,
        # None means the build recorded no cleanliness claim at all. A commit
        # alone cannot show that the built bytes are the ones that commit
        # describes, so an absent flag is reported as unknown, never as clean.
        "working_tree_clean": clean if isinstance(clean, bool) else None,
    }


def _repo_head_provenance(repo: Path) -> dict[str, Any] | None:
    commit = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    if commit is None or tree is None:
        return None
    return {"commit_sha": commit, "tree_sha": tree}


def _git(repo: Path, *arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=_subprocess_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = (completed.stdout or "").strip()
    return value or None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_required(
    payload: dict[str, Any],
    *,
    name: str,
    command: list[str],
    cwd: Path,
    timeout_seconds: float,
) -> bool:
    check = _run_command(
        name=name,
        command=command,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )
    payload["checks"].append(check)
    if check["status"] != "pass":
        _add_failure(payload, name, check.get("error") or "Command failed.")
        return False
    return True


def _run_command(
    *,
    name: str,
    command: list[str],
    cwd: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        completed = _run_process(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=_subprocess_env(),
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "name": name,
            "status": "fail",
            "command": command,
            "cwd": str(cwd),
            "exit_code": 124,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "error": f"Timed out after {timeout_seconds}s.",
            "stdout_tail": _tail(exc.stdout),
            "stderr_tail": _tail(exc.stderr),
        }

    stdout = str(getattr(completed, "stdout", "") or "")
    stderr = str(getattr(completed, "stderr", "") or "")
    exit_code = int(getattr(completed, "returncode", 1))
    return {
        "name": name,
        "status": "pass" if exit_code == 0 else "fail",
        "command": command,
        "cwd": str(cwd),
        "exit_code": exit_code,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
        "error": stderr.strip() if exit_code and stderr.strip() else None,
    }


def _run_process(
    command: list[str],
    *,
    cwd: Path,
    check: bool,
    capture_output: bool,
    text: bool,
    timeout: float,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=check,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        env=env,
    )


def _verify_workbench_assets(payload: dict[str, Any], workbench_dir: Path) -> None:
    missing = [
        required
        for required in REQUIRED_WORKBENCH_FILES
        if not (workbench_dir / required).is_file()
    ]
    check: dict[str, Any] = {
        "name": "fixture-workbench-assets",
        "status": "fail" if missing else "pass",
        "required_files": list(REQUIRED_WORKBENCH_FILES),
        "missing": missing,
    }
    payload["checks"].append(check)
    if missing:
        _add_failure(
            payload,
            "fixture-workbench-assets",
            f"Workbench output missing required files: {', '.join(missing)}",
        )


def _copy_fixture_project(repo_root: Path, destination: Path) -> Path:
    source = repo_root / "arcgraph" / "tests" / "fixtures" / "sample_project"
    if not source.exists():
        raise RuntimeError(f"Sample project fixture not found: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    for child_name in ("src", "tests"):
        shutil.copytree(source / child_name, destination / child_name)
    return destination


def _create_virtualenv(venv_dir: Path) -> None:
    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)


def _venv_python(venv_dir: Path) -> Path:
    scripts_dir = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return venv_dir / scripts_dir / executable


def _arcgraph_cli(venv_dir: Path) -> Path:
    scripts_dir = "Scripts" if os.name == "nt" else "bin"
    executable = "arcgraph.exe" if os.name == "nt" else "arcgraph"
    return venv_dir / scripts_dir / executable


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["NO_COLOR"] = "1"
    return env


def _tail(value: Any) -> str:
    if value is None:
        return ""
    text = (
        value.decode("utf-8", errors="replace")
        if isinstance(value, bytes)
        else str(value)
    )
    return text[-MAX_CAPTURE_CHARS:]


def _add_warning(payload: dict[str, Any], check: str, message: str) -> None:
    payload["warnings"].append({"check": check, "message": message})


def _add_failure(payload: dict[str, Any], check: str, message: str) -> None:
    payload["status"] = "fail"
    payload["failures"].append({"check": check, "message": message})


def _finish(
    payload: dict[str, Any],
    started: float,
    output_path: Path | None,
) -> tuple[int, dict[str, Any]]:
    payload["finished_at"] = _utc_now()
    payload["duration_seconds"] = round(time.perf_counter() - started, 3)
    if payload["failures"]:
        payload["status"] = "fail"
    elif payload["warnings"] and payload["status"] == "pass":
        # A disclosed bypass must never look identical to a clean run.
        payload["status"] = "warn"
    if output_path is not None:
        output = output_path.resolve()
        payload["output_path"] = str(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
    return (1 if payload["failures"] else 0), payload


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
