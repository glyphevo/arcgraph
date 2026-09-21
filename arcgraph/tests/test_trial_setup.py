from __future__ import annotations

import os

import pytest

import json
import stat
from pathlib import Path

from arcgraph.interfaces.cli import main
from arcgraph.interfaces.trial_setup import trial_setup

# CI runs this file on windows-latest, where permission bits and FIFOs do not
# exist. The scenarios that assert them are POSIX-only; the platform-neutral
# apply-mode contract is asserted separately so the Windows lane still covers
# the code path the no-follow guards live in.
posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX permission bits and FIFOs do not exist on Windows.",
)


def _git_repo(root: Path) -> None:
    (root / ".git" / "info").mkdir(parents=True)
    (root / ".git" / "info" / "exclude").write_text("# local only\n", encoding="utf-8")


def test_trial_setup_is_preview_only_by_default(tmp_path: Path) -> None:
    _git_repo(tmp_path)

    payload = trial_setup(repo_root=tmp_path, client="claude")

    assert payload["action"] == "dry_run"
    assert payload["claude_config_modified"] is False
    assert not (tmp_path / ".arcgraph-trial").exists()
    assert payload["registration_command"][:6] == [
        "claude",
        "mcp",
        "add",
        "--scope",
        "local",
        "arcgraph",
    ]
    assert all("$" not in value for value in payload["registration_command"])


@posix_only
def test_trial_setup_applies_only_private_local_paths_and_git_exclude(
    tmp_path: Path,
) -> None:
    _git_repo(tmp_path)

    payload = trial_setup(
        repo_root=tmp_path,
        client="claude",
        apply_local_files=True,
    )

    assert payload["action"] == "local_files_applied"
    for relative in ("metrics", "feedback"):
        mode = stat.S_IMODE((tmp_path / ".arcgraph-trial" / relative).stat().st_mode)
        assert mode == 0o700
    for relative in ("metrics/mcp.jsonl", "feedback/agent.jsonl"):
        mode = stat.S_IMODE((tmp_path / ".arcgraph-trial" / relative).stat().st_mode)
        assert mode == 0o600
    exclude = (tmp_path / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert exclude.count(".arcgraph-trial/") == 1


def test_trial_setup_cli_emits_copyable_absolute_command(
    tmp_path: Path, capsys
) -> None:
    _git_repo(tmp_path)

    assert (
        main(
            [
                "--repo-root",
                str(tmp_path),
                "trial",
                "setup",
                "--client",
                "claude",
                "--dry-run",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    serve = payload["serve_command"]
    assert Path(serve[0]).is_absolute()
    assert str(tmp_path.resolve()) in serve
    assert payload["claude_config_modified"] is False


def test_trial_setup_does_not_apply_when_repo_root_is_missing(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    payload = trial_setup(
        repo_root=missing,
        client="claude",
        apply_local_files=True,
    )

    assert payload["status"] == "blocked"
    assert payload["action"] == "not_applied"
    assert payload["_exit_code"] == 1
    assert not missing.exists()
    repo_check = next(
        check for check in payload["checks"] if check["name"] == "repo_root"
    )
    assert repo_check["status"] == "fail"


def test_apply_refuses_when_repo_root_is_not_a_git_repository(tmp_path: Path) -> None:
    """Apply mode must never fabricate a .git directory: a non-repository
    root fails closed, writes nothing, and does not report ready."""

    from arcgraph.interfaces.trial_setup import trial_setup

    payload = trial_setup(
        repo_root=tmp_path,
        client="claude",
        apply_local_files=True,
    )

    assert payload["status"] == "blocked"
    assert payload["action"] == "not_applied"
    assert not (tmp_path / ".git").exists()
    assert not (tmp_path / ".arcgraph-trial").exists()
    git_check = next(
        check for check in payload["checks"] if check["name"] == "git_repo_root"
    )
    assert git_check["status"] == "fail"


@posix_only
def test_apply_refuses_from_a_subdirectory_of_a_git_repository(tmp_path: Path) -> None:
    from arcgraph.interfaces.trial_setup import trial_setup

    (tmp_path / ".git" / "info").mkdir(parents=True)
    (tmp_path / ".git" / "info" / "exclude").write_text("", encoding="utf-8")
    subdir = tmp_path / "app"
    subdir.mkdir()

    payload = trial_setup(
        repo_root=subdir,
        client="claude",
        apply_local_files=True,
    )

    assert payload["status"] == "blocked"
    assert not (subdir / ".git").exists()


def _outside_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / ".git" / "info").mkdir(parents=True)
    (repo / ".git" / "info" / "exclude").write_text("# local\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    return repo, outside


@posix_only
def test_apply_refuses_every_symlinked_state_path(tmp_path: Path) -> None:
    """Apply mode must never write or chmod through a link: refusing means
    creating nothing, so the check is a prerequisite, not a per-path guard."""

    import stat as stat_module

    from arcgraph.interfaces.trial_setup import trial_setup

    # (a) the state root itself is a link out of the repository
    repo, outside = _outside_repo(tmp_path / "a")
    (repo / ".arcgraph-trial").symlink_to(outside)
    payload = trial_setup(repo_root=repo, client="claude", apply_local_files=True)
    assert payload["status"] == "blocked"
    assert payload["action"] == "not_applied"
    assert stat_module.S_IMODE(outside.lstat().st_mode) == 0o755
    assert not list(outside.iterdir())

    # (b) an inner directory is a link out of the repository
    repo, outside = _outside_repo(tmp_path / "b")
    (repo / ".arcgraph-trial").mkdir()
    (repo / ".arcgraph-trial" / "metrics").symlink_to(outside)
    payload = trial_setup(repo_root=repo, client="claude", apply_local_files=True)
    assert payload["status"] == "blocked"
    assert not list(outside.iterdir())

    # (c) a log file is a link onto an existing outside file
    repo, outside = _outside_repo(tmp_path / "c")
    (repo / ".arcgraph-trial" / "metrics").mkdir(parents=True)
    secret = outside / "secret.txt"
    secret.write_text("PREEXISTING\n", encoding="utf-8")
    secret.chmod(0o644)
    (repo / ".arcgraph-trial" / "metrics" / "mcp.jsonl").symlink_to(secret)
    payload = trial_setup(repo_root=repo, client="claude", apply_local_files=True)
    assert payload["status"] == "blocked"
    assert stat_module.S_IMODE(secret.lstat().st_mode) == 0o644
    assert secret.read_text(encoding="utf-8") == "PREEXISTING\n"

    # (d) .git itself is a link out of the repository
    repo = tmp_path / "d" / "repo"
    repo.mkdir(parents=True)
    outside = tmp_path / "d" / "outside"
    (outside / "info").mkdir(parents=True)
    (outside / "info" / "exclude").write_text("# outside\n", encoding="utf-8")
    (repo / ".git").symlink_to(outside)
    payload = trial_setup(repo_root=repo, client="claude", apply_local_files=True)
    assert payload["status"] == "blocked"
    assert (outside / "info" / "exclude").read_text(encoding="utf-8") == "# outside\n"


@posix_only
def test_apply_blocks_dangling_state_link_without_raising(tmp_path: Path) -> None:
    from arcgraph.interfaces.trial_setup import trial_setup

    repo, _outside = _outside_repo(tmp_path)
    (repo / ".arcgraph-trial").symlink_to(tmp_path / "missing")

    payload = trial_setup(repo_root=repo, client="claude", apply_local_files=True)

    assert payload["status"] == "blocked"


@posix_only
def test_apply_refuses_existing_paths_of_the_wrong_type(tmp_path: Path) -> None:
    """A non-link object of the wrong type is not a symlink, so the link rule
    alone let it reach mkdir and raise after the state root was chmod'ed."""

    import stat as stat_module

    from arcgraph.interfaces.trial_setup import trial_setup

    # A plain file where a directory belongs.
    repo, _outside = _outside_repo(tmp_path / "file_for_dir")
    (repo / ".arcgraph-trial").mkdir()
    (repo / ".arcgraph-trial" / "metrics").write_text("not a dir\n", encoding="utf-8")
    mode_before = stat_module.S_IMODE((repo / ".arcgraph-trial").stat().st_mode)

    payload = trial_setup(repo_root=repo, client="claude", apply_local_files=True)

    assert payload["status"] == "blocked"
    assert payload["action"] == "not_applied"
    # Refusing means creating and changing nothing.
    assert stat_module.S_IMODE((repo / ".arcgraph-trial").stat().st_mode) == mode_before

    # A directory where a log file belongs.
    repo, _outside = _outside_repo(tmp_path / "dir_for_file")
    (repo / ".arcgraph-trial" / "metrics" / "mcp.jsonl").mkdir(parents=True)
    payload = trial_setup(repo_root=repo, client="claude", apply_local_files=True)
    assert payload["status"] == "blocked"

    # A FIFO is neither, and opening one can block forever.
    repo, _outside = _outside_repo(tmp_path / "fifo_for_file")
    (repo / ".arcgraph-trial" / "metrics").mkdir(parents=True)
    os.mkfifo(repo / ".arcgraph-trial" / "metrics" / "mcp.jsonl")
    payload = trial_setup(repo_root=repo, client="claude", apply_local_files=True)
    assert payload["status"] == "blocked"


def test_apply_reports_filesystem_failure_as_structured_result(
    tmp_path: Path, monkeypatch
) -> None:
    """The prerequisites rule out the states we can name; anything the file
    system still refuses mid-apply is a blocked payload, not a traceback."""

    from arcgraph.interfaces import trial_setup as trial_setup_module

    repo, _outside = _outside_repo(tmp_path)

    real_mkdir = Path.mkdir

    def failing_mkdir(self, *args, **kwargs):
        if self.name == "feedback":
            raise PermissionError(13, "Permission denied")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", failing_mkdir)

    payload = trial_setup_module.trial_setup(
        repo_root=repo, client="claude", apply_local_files=True
    )

    assert payload["status"] == "blocked"
    assert payload["action"] == "partially_applied"
    failure = next(
        check for check in payload["checks"] if check["name"] == "apply_local_files"
    )
    assert "PermissionError" in failure["value"]


def test_next_steps_never_suggest_registering_from_a_blocked_state(
    tmp_path: Path, monkeypatch
) -> None:
    """A half-written local state has not earned the registration advice, and
    a new action must not fall through to it either."""

    from arcgraph.interfaces import trial_setup as trial_setup_module

    repo, _outside = _outside_repo(tmp_path)
    real_mkdir = Path.mkdir

    def failing_mkdir(self, *args, **kwargs):
        if self.name == "feedback":
            raise PermissionError(13, "Permission denied")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", failing_mkdir)
    payload = trial_setup_module.trial_setup(
        repo_root=repo, client="claude", apply_local_files=True
    )

    assert payload["action"] == "partially_applied"
    assert "registration_command" not in payload["next_steps"][0]
    assert "rerun" in payload["next_steps"][0].lower()

    # An unmapped action falls back to the blocked advice, not to registering.
    unknown = trial_setup_module._NEXT_STEP_BY_ACTION.get(
        "some_future_action", trial_setup_module._BLOCKED_NEXT_STEP
    )
    assert "do not register" in unknown


@posix_only
def test_exclude_write_refuses_a_symlink_swapped_after_the_check(
    tmp_path: Path,
) -> None:
    """The prerequisite rejects a symlinked exclude file, but a link swapped
    in afterwards would otherwise redirect the write outside the repository —
    the same window the log writes already close with O_NOFOLLOW."""

    from arcgraph.interfaces.trial_setup import _append_local_exclude

    repo = tmp_path / "repo"
    (repo / ".git" / "info").mkdir(parents=True)
    outside = tmp_path / "outside.conf"
    outside.write_text("IMPORTANT\n", encoding="utf-8")
    (repo / ".git" / "info" / "exclude").symlink_to(outside)

    with pytest.raises(OSError):
        _append_local_exclude(repo / ".git" / "info" / "exclude", ".arcgraph-trial/")

    assert outside.read_text(encoding="utf-8") == "IMPORTANT\n"


def test_no_follow_writes_do_not_depend_on_posix_only_names() -> None:
    """Windows Python exposes neither O_NOFOLLOW nor fchmod, and CI runs
    windows-latest. A bare reference raises AttributeError, which the
    apply-mode `except OSError` would not catch."""

    from arcgraph.interfaces import trial_setup as trial_setup_module

    source = Path(trial_setup_module.__file__).read_text(encoding="utf-8")

    assert "os.O_NOFOLLOW" not in source
    assert 'getattr(os, "O_NOFOLLOW", 0)' in source
    # fchmod is only reached on posix.
    for line_number, line in enumerate(source.splitlines()):
        if "os.fchmod" in line:
            window = source.splitlines()[max(0, line_number - 3) : line_number]
            assert any('os.name == "posix"' in item for item in window)


def test_arcgraph_executable_uses_active_distribution_record_not_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arcgraph.interfaces import trial_setup as trial_setup_module

    suffix = ".exe" if os.name == "nt" else ""
    expected = tmp_path / "user-scripts" / f"arcgraph{suffix}"
    expected.parent.mkdir()
    expected.write_text("launcher", encoding="utf-8")
    expected.chmod(0o755)
    wrong = tmp_path / "wrong-path" / f"arcgraph{suffix}"
    wrong.parent.mkdir()
    wrong.write_text("wrong launcher", encoding="utf-8")
    wrong.chmod(0o755)

    class FakeDistribution:
        files = [Path("../../../user-scripts") / expected.name]

        @staticmethod
        def locate_file(entry: Path) -> Path:
            del entry
            return expected

    monkeypatch.setattr(
        trial_setup_module.importlib_metadata,
        "distribution",
        lambda name: FakeDistribution(),
    )
    monkeypatch.setattr(trial_setup_module.sys, "argv", ["pytest"])
    monkeypatch.setenv("PATH", str(wrong.parent))

    assert trial_setup_module._arcgraph_executable() == expected.resolve()


def test_trial_setup_discloses_non_record_executable_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arcgraph.interfaces import trial_setup as trial_setup_module

    repo = tmp_path / "repo"
    _git_repo(repo)
    suffix = ".exe" if os.name == "nt" else ""
    scripts_dir = tmp_path / "scheme-scripts"
    scripts_dir.mkdir()
    fallback = scripts_dir / f"arcgraph{suffix}"
    fallback.write_text("launcher", encoding="utf-8")
    fallback.chmod(0o755)

    monkeypatch.setattr(trial_setup_module.sys, "argv", ["pytest"])
    monkeypatch.setattr(
        trial_setup_module.importlib_metadata,
        "distribution",
        lambda name: (_ for _ in ()).throw(
            trial_setup_module.importlib_metadata.PackageNotFoundError(name)
        ),
    )
    monkeypatch.setattr(
        trial_setup_module.sysconfig,
        "get_path",
        lambda name, scheme=None: str(scripts_dir),
    )

    payload = trial_setup_module.trial_setup(repo_root=repo, client="claude")
    executable_check = next(
        check for check in payload["checks"] if check["name"] == "arcgraph_executable"
    )

    assert executable_check["status"] == "warn"
    assert executable_check["selection_source"] == "interpreter_scheme"
    assert executable_check["value"] == str(fallback.resolve())
    assert "not identified" in executable_check["message"]
    assert payload["status"] == "review"


def test_dry_run_advice_follows_the_checks_not_only_the_action(
    tmp_path: Path,
) -> None:
    """A dry run whose prerequisites already failed has not earned the apply
    advice: the action alone does not know whether applying would work."""

    from arcgraph.interfaces.trial_setup import trial_setup

    repo, _outside = _outside_repo(tmp_path)
    healthy = trial_setup(repo_root=repo, client="claude")
    assert healthy["status"] == ("ready" if os.name == "posix" else "review")
    assert "--apply-local-files" in healthy["next_steps"][0]

    broken = trial_setup(repo_root=tmp_path / "missing", client="claude")
    assert broken["status"] == "review"
    assert [check["name"] for check in broken["checks"] if check["status"] == "fail"]
    assert "Resolve failed prerequisite checks" in broken["next_steps"][0]


def test_apply_mode_succeeds_on_every_platform(tmp_path: Path) -> None:
    """Platform-independent apply-mode contract. The permission assertions
    live in POSIX-only tests, so this one still runs on the Windows lane the
    no-follow fix was written for."""

    from arcgraph.interfaces.trial_setup import trial_setup

    repo, _outside = _outside_repo(tmp_path)

    payload = trial_setup(repo_root=repo, client="claude", apply_local_files=True)

    assert payload["action"] == "local_files_applied"
    if os.name == "posix":
        assert payload["status"] == "ready"
    else:
        # chmod cannot express 0700/0600 on Windows, so setup does not
        # enforce the private-mode guarantee there and says so rather than
        # reporting ready.
        assert payload["status"] == "review"
        permission_checks = [
            check
            for check in payload["checks"]
            if check["name"].endswith("_permissions")
        ]
        assert permission_checks
        assert all(check["status"] == "warn" for check in permission_checks)
    assert (repo / ".arcgraph-trial" / "index").is_dir()
    assert (repo / ".arcgraph-trial" / "metrics" / "mcp.jsonl").is_file()
    assert (repo / ".arcgraph-trial" / "feedback" / "agent.jsonl").is_file()
    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".arcgraph-trial/" in exclude
    # Rerunning is idempotent: the entry is not appended twice.
    trial_setup(repo_root=repo, client="claude", apply_local_files=True)
    reread = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert reread.count(".arcgraph-trial/") == 1


def test_permission_check_states_the_platform_boundary_before_anything_exists(
    tmp_path: Path,
) -> None:
    """The platform decides whether mode bits can carry the guarantee, so a
    first dry run — when no state path exists yet — must already say so
    rather than previewing a guarantee it will not deliver."""

    from arcgraph.interfaces.trial_setup import _permission_check

    missing = tmp_path / "not-created-yet"
    check = _permission_check(missing, expected=0o700, kind="directory")

    if os.name == "posix":
        assert check["status"] == "planned"
    else:
        assert check["status"] == "warn"
        assert check["actual"] is None
        assert "POSIX-only" in check["detail"]


@posix_only
def test_posix_dry_run_still_previews_planned_permissions(tmp_path: Path) -> None:
    _git_repo(tmp_path)

    payload = trial_setup(repo_root=tmp_path, client="claude")

    permission_checks = [
        check for check in payload["checks"] if check["name"].endswith("_permissions")
    ]
    assert permission_checks
    assert all(check["status"] == "planned" for check in permission_checks)
    assert payload["status"] == "ready"
