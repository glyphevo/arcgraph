"""Strict local release gate for ArcGraph package publishing."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
STALE_WARNING_MARKERS = (
    "target_stale",
    "stale edge",
    "Current index is stale",
)
ARCGRAPH_PACKAGE_DIR = REPO_ROOT
WHEEL_TEST_PREFIX = "arcgraph/tests/"
WORKBENCH_WHEEL_FILES = (
    "arcgraph/assets/workbench/index.html",
    "arcgraph/assets/workbench/vendor/LICENSE.d3.txt",
    "arcgraph/assets/workbench/vendor/d3.v7.min.js",
)
TYPESCRIPT_EXTRACTOR_ENTRY = "arcgraph/pipeline/typescript_extractor.mjs"
TYPESCRIPT_EXTRACTOR_HELPER_DIR = (
    REPO_ROOT / "arcgraph" / "pipeline" / "typescript_extractor"
)
GENERATED_WHEEL_FILES = frozenset({"arcgraph/_build_provenance.json"})


def _relative_package_file(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def typescript_extractor_helper_wheel_files() -> tuple[str, ...]:
    if not TYPESCRIPT_EXTRACTOR_HELPER_DIR.exists():
        return ()
    return tuple(
        sorted(
            _relative_package_file(path)
            for path in TYPESCRIPT_EXTRACTOR_HELPER_DIR.glob("*.mjs")
            if path.is_file()
        )
    )


def required_wheel_files() -> tuple[str, ...]:
    return (
        *sorted(GENERATED_WHEEL_FILES),
        *WORKBENCH_WHEEL_FILES,
        TYPESCRIPT_EXTRACTOR_ENTRY,
        *typescript_extractor_helper_wheel_files(),
    )


# Backward-compatible module constant for scripts/tests that import it directly.
REQUIRED_WHEEL_FILES = required_wheel_files()


def arcgraph_command() -> list[str]:
    return [sys.executable, str(REPO_ROOT / "scripts" / "arcgraph.py")]


def capture_source_provenance(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """Bind package output to one clean Git tree without mutating the checkout."""

    def git(args: list[str]) -> bytes:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
        return completed.stdout

    status = git(["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    tracked_entries = _parse_git_index(git(["ls-files", "-s", "-z"]))
    tracked_symlinks = sorted(
        path for mode, path in tracked_entries if mode == "120000"
    )
    tracked_package_files = sorted(
        path
        for mode, path in tracked_entries
        if mode != "120000"
        and path.startswith("arcgraph/")
        and not path.startswith("arcgraph/tests/")
    )
    return {
        "head_sha": git(["rev-parse", "HEAD"]).decode("ascii").strip(),
        "tree_sha": git(["rev-parse", "HEAD^{tree}"]).decode("ascii").strip(),
        "working_tree_clean": not status,
        "working_tree_status_sha256": hashlib.sha256(status).hexdigest(),
        "tracked_symlinks": tracked_symlinks,
        "tracked_package_files": tracked_package_files,
    }


def _parse_git_index(payload: bytes) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for raw_record in payload.split(b"\0"):
        if not raw_record:
            continue
        header, separator, raw_path = raw_record.partition(b"\t")
        if not separator:
            raise RuntimeError("git ls-files returned an invalid index record")
        mode = header.split(b" ", 1)[0].decode("ascii", errors="strict")
        path = raw_path.decode("utf-8", errors="surrogateescape")
        entries.append((mode, path))
    return entries


def validate_source_provenance(provenance: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if provenance.get("working_tree_clean") is not True:
        errors.append(
            "release candidates require a clean working tree; commit or remove "
            "staged, unstaged, and untracked changes first"
        )
    for field in ("head_sha", "tree_sha", "working_tree_status_sha256"):
        value = provenance.get(field)
        if not isinstance(value, str) or not value:
            errors.append(f"release source provenance is missing {field}")
    tracked_symlinks = provenance.get("tracked_symlinks", [])
    if not isinstance(tracked_symlinks, list):
        errors.append("release source provenance has invalid tracked_symlinks")
    elif tracked_symlinks:
        errors.append(
            "release candidates must not contain tracked symlinks: "
            + ", ".join(str(path) for path in tracked_symlinks[:10])
        )
    return errors


def run_arcgraph_json(args: list[str]) -> dict[str, Any]:
    command = [*arcgraph_command(), *args]
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        if completed.stdout:
            print(completed.stdout, file=sys.stderr)
        if completed.stderr:
            print(completed.stderr, file=sys.stderr)
        raise SystemExit(completed.returncode)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        print(
            f"ArcGraph command did not return JSON: {' '.join(command)}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    if not isinstance(payload, dict):
        print(
            f"ArcGraph command returned non-object JSON: {' '.join(command)}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return payload


def iter_warnings(payload: Any) -> list[str]:
    warnings: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "warnings" and isinstance(value, list):
                warnings.extend(str(item) for item in value)
            else:
                warnings.extend(iter_warnings(value))
    elif isinstance(payload, list):
        for item in payload:
            warnings.extend(iter_warnings(item))
    return warnings


def has_stale_target_warning(payload: dict[str, Any]) -> bool:
    for warning in iter_warnings(payload):
        lowered = warning.lower()
        if any(marker.lower() in lowered for marker in STALE_WARNING_MARKERS):
            return True
    return False


def find_ci_check(ci: dict[str, Any], name: str) -> dict[str, Any] | None:
    checks = ci.get("checks")
    if not isinstance(checks, list):
        return None
    for check in checks:
        if isinstance(check, dict) and check.get("name") == name:
            return check
    return None


def validate_evidence_commit_consistency(ci: dict[str, Any]) -> list[str]:
    check = find_ci_check(ci, "evidence_commit_consistency")
    if check is None:
        return ["arcgraph ci did not report evidence_commit_consistency"]

    details = check.get("details")
    if not isinstance(details, dict):
        return ["arcgraph ci evidence_commit_consistency details were missing"]

    errors: list[str] = []
    head_commit_sha = details.get("head_commit_sha")
    if details.get("checked") is not True:
        errors.append("arcgraph ci could not verify evidence commit consistency")
    if details.get("index_matches_head") is False:
        errors.append(
            "ArcGraph index was generated for a different HEAD: "
            f"head={head_commit_sha}, index={details.get('index_commit_sha')}"
        )
    if (
        details.get("runtime_trace_commit_sha")
        and details.get("runtime_trace_matches_head") is False
    ):
        errors.append(
            "ArcGraph runtime trace was generated for a different HEAD: "
            f"head={head_commit_sha}, "
            f"runtime_trace={details.get('runtime_trace_commit_sha')}"
        )
    if details.get("consistent") is False and not errors:
        errors.append(
            "ArcGraph evidence commit consistency failed without a classified mismatch"
        )
    return errors


def validate_gate(
    current: dict[str, Any],
    ci: dict[str, Any],
    *,
    source_provenance: dict[str, Any] | None = None,
) -> list[str]:
    errors = (
        validate_source_provenance(source_provenance)
        if source_provenance is not None
        else []
    )
    freshness = current.get("freshness")
    if not isinstance(freshness, dict):
        errors.append("arcgraph current did not include freshness metadata")
    elif freshness.get("status") != "fresh" or freshness.get("stale") is True:
        errors.append(f"ArcGraph index is not fresh: {freshness}")

    if has_stale_target_warning(current):
        errors.append("arcgraph current reported stale target edges")

    summary = ci.get("summary") if isinstance(ci.get("summary"), dict) else {}
    fail_count = int(summary.get("fail") or 0)
    warn_count = int(summary.get("warn") or 0)
    if ci.get("status") == "fail" or fail_count:
        errors.append(f"arcgraph ci failed: {fail_count} failing check(s)")
    ci_has_stale_warning = has_stale_target_warning(ci)
    if (ci.get("status") == "warn" or warn_count) and not ci_has_stale_warning:
        errors.append(f"arcgraph ci reported {warn_count} warning check(s)")

    ci_freshness = ci.get("freshness")
    if isinstance(ci_freshness, dict) and (
        ci_freshness.get("status") != "fresh" or ci_freshness.get("stale") is True
    ):
        errors.append(f"arcgraph ci used a stale index: {ci_freshness}")

    if ci_has_stale_warning:
        errors.append("arcgraph ci reported stale target edges")

    errors.extend(validate_evidence_commit_consistency(ci))

    return errors


def declared_project_version() -> str:
    """Return the version this repository declares it is releasing."""

    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(config["project"]["version"])


def build_arcgraph_wheel(dist_dir: Path) -> Path:
    command = [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        str(ARCGRAPH_PACKAGE_DIR),
        "-w",
        str(dist_dir),
        "--no-deps",
    ]
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        if completed.stdout:
            print(completed.stdout, file=sys.stderr)
        if completed.stderr:
            print(completed.stderr, file=sys.stderr)
        raise SystemExit(completed.returncode)

    version = declared_project_version()
    # Select by the version under release, never by name order: sorted() is
    # lexicographic, so with an rc10 present it returns rc6, and the gate would
    # then validate the contents of an older artifact under the current label.
    wheels = sorted(dist_dir.glob(f"arcgraph-{version}-*.whl"))
    if not wheels:
        others = sorted(path.name for path in dist_dir.glob("arcgraph-*.whl"))
        print(
            f"ArcGraph wheel build did not produce a wheel for version {version}"
            + (f"; {dist_dir} holds {others}" if others else ""),
            file=sys.stderr,
        )
        raise SystemExit(1)
    if len(wheels) > 1:
        print(
            f"More than one wheel matches version {version}: "
            f"{[path.name for path in wheels]}. Refusing to guess which one the "
            "gate should validate.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return wheels[0]


def validate_wheel_contents(
    wheel_path: Path,
    *,
    tracked_package_files: list[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    with zipfile.ZipFile(wheel_path) as wheel:
        names = wheel.namelist()
    packaged = set(names)
    packaged_tests = [
        name
        for name in names
        if name.startswith(WHEEL_TEST_PREFIX) or name == "arcgraph/tests"
    ]
    if packaged_tests:
        errors.append(
            f"ArcGraph wheel includes test files under {WHEEL_TEST_PREFIX.rstrip('/')}"
        )
    for required_file in required_wheel_files():
        if required_file not in packaged:
            errors.append(
                f"ArcGraph wheel is missing required package file {required_file}"
            )
    if tracked_package_files is not None:
        expected = set(tracked_package_files)
        packaged_runtime = {
            name
            for name in names
            if name.startswith("arcgraph/")
            and not name.endswith("/")
            and not name.startswith(WHEEL_TEST_PREFIX)
        }
        missing_tracked = sorted(expected - packaged_runtime)
        unexpected_runtime = sorted(packaged_runtime - expected - GENERATED_WHEEL_FILES)
        if missing_tracked:
            errors.append(
                "ArcGraph wheel is missing tracked package files: "
                + ", ".join(missing_tracked[:10])
            )
        if unexpected_runtime:
            errors.append(
                "ArcGraph wheel contains package files absent from Git provenance: "
                + ", ".join(unexpected_runtime[:10])
            )
    return errors


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    try:
        source_provenance = capture_source_provenance()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    current = run_arcgraph_json(["current"])
    ci = run_arcgraph_json(["ci"])
    errors = validate_gate(
        current,
        ci,
        source_provenance=source_provenance,
    )
    wheel_name = "skipped"
    wheel_sha256 = "skipped"
    wheel_size: int | str = "skipped"

    if not errors:
        with tempfile.TemporaryDirectory(prefix="arcgraph-wheel-") as temp_dir:
            wheel_path = build_arcgraph_wheel(Path(temp_dir))
            wheel_name = wheel_path.name
            wheel_sha256 = sha256_file(wheel_path)
            wheel_size = wheel_path.stat().st_size
            errors.extend(
                validate_wheel_contents(
                    wheel_path,
                    tracked_package_files=source_provenance["tracked_package_files"],
                )
            )
            if capture_source_provenance() != source_provenance:
                errors.append("working tree changed while the release gate was running")

    summary = ci.get("summary") if isinstance(ci.get("summary"), dict) else {}
    print(
        "ArcGraph release gate: "
        f"freshness={current.get('freshness', {}).get('status') if isinstance(current.get('freshness'), dict) else 'unknown'}, "
        f"ci_status={ci.get('status')}, "
        f"fail={summary.get('fail', 0)}, "
        f"warn={summary.get('warn', 0)}, "
        f"source={'clean' if source_provenance['working_tree_clean'] else 'dirty'}, "
        f"head={source_provenance['head_sha'][:12]}, "
        f"wheel={wheel_name}, "
        f"wheel_sha256={wheel_sha256}, "
        f"wheel_size={wheel_size}"
    )

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
