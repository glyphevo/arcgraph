from pathlib import Path
import re

import tomllib

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_ci_covers_supported_platforms_security_and_package_provenance() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    jobs_section = workflow.split("\njobs:\n", 1)[1]
    job_names = set(
        re.findall(
            r"^  ([a-z][a-z0-9-]+):$",
            jobs_section,
            flags=re.MULTILINE,
        )
    )

    assert job_names == {
        "quality",
        "test-matrix",
        "package-matrix",
        "typescript-acceptance",
        "security",
        "ci-gate",
    }
    for value in ("ubuntu-latest", "windows-latest", "macos-latest"):
        assert value in workflow
    for value in ("'3.11'", "'3.12'"):
        assert value in workflow
    action_references = re.findall(
        r"uses:\s+(actions/[a-z-]+)@([0-9a-f]{40})\s+#\s+(v\d+)",
        workflow,
    )
    assert {(action, version) for action, _sha, version in action_references} == {
        ("actions/checkout", "v7"),
        ("actions/setup-python", "v7"),
        ("actions/setup-node", "v7"),
        ("actions/upload-artifact", "v7"),
    }
    assert not re.search(r"uses:\s+actions/[a-z-]+@(?![0-9a-f]{40}\b)", workflow)
    for legacy_action in (
        "actions/checkout@v4",
        "actions/setup-python@v5",
        "actions/setup-node@v4",
        "actions/upload-artifact@v4",
    ):
        assert legacy_action not in workflow
    for command in (
        "arcgraph/tests scripts/tests",
        "arcgraph_release_gate.py",
        "pip install --upgrade pip setuptools wheel",
        'pip install -e ".[dev,mcp,security]"',
        "pip freeze --exclude-editable",
        "pip_audit --strict --disable-pip --no-deps --requirement",
        'test "$(npx --yes npm@11.12.1 --version)" = "11.12.1"',
        "npx --yes npm@11.12.1 audit --audit-level=high --json",
        "bandit -c pyproject.toml -r arcgraph scripts "
        "-x arcgraph/tests,scripts/tests",
        "cyclonedx_py environment",
        "name: CI Gate",
    ):
        assert command in workflow
    for required_job in (
        "quality",
        "test-matrix",
        "package-matrix",
        "typescript-acceptance",
        "security",
    ):
        assert f"      - {required_job}" in workflow


def test_typescript_acceptance_matrix_is_formal_and_fail_closed() -> None:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["typescript-acceptance"]

    assert job["strategy"]["matrix"]["include"] == [
        {"node": "20", "typescript": "5.4.5"},
        {"node": "20", "typescript": "5.9.3"},
        {"node": "22", "typescript": "5.9.3"},
        {"node": "24", "typescript": "5.9.3"},
    ]
    run_step = next(
        step
        for step in job["steps"]
        if step.get("name") == "Run formal TypeScript acceptance contract"
    )
    assert run_step["env"]["ARCGRAPH_REQUIRE_TS"] == "1"
    for suite in (
        "test_typescript_baseline.py",
        "test_typescript_esm_resolution.py",
        "test_typescript_package_exports.py",
        "test_typescript_conditional_exports.py",
        "test_typescript_express_routes.py",
        "test_typescript_similarity.py",
    ):
        assert suite in run_step["run"]
        source = (REPO_ROOT / "arcgraph" / "tests" / suite).read_text(encoding="utf-8")
        assert (
            "ARCGRAPH_REQUIRE_TS" in source or "typescript_acceptance_support" in source
        )
        if "typescript_acceptance_support" in source:
            assert "pytest.skip(" not in source


def test_package_matrix_covers_mcp_lower_and_latest_lines() -> None:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    matrix = workflow["jobs"]["package-matrix"]["strategy"]["matrix"]["include"]

    assert matrix == [
        {
            "os": "ubuntu-latest",
            "python": "3.11",
            "mcp_line": "lower",
            "mcp_spec": "mcp==2.0.0",
        },
        {
            "os": "ubuntu-latest",
            "python": "3.11",
            "mcp_line": "latest-2x",
            "mcp_spec": "mcp>=2.0.0,<3.0.0",
        },
        {
            "os": "windows-latest",
            "python": "3.12",
            "mcp_line": "latest-2x",
            "mcp_spec": "mcp>=2.0.0,<3.0.0",
        },
        {
            "os": "macos-latest",
            "python": "3.12",
            "mcp_line": "latest-2x",
            "mcp_spec": "mcp>=2.0.0,<3.0.0",
        },
    ]
    package_job = workflow["jobs"]["package-matrix"]
    run_step = next(
        step
        for step in package_job["steps"]
        if step.get("name") == "Build, install, and exercise package artifacts"
    )
    upload_step = next(
        step
        for step in package_job["steps"]
        if step.get("name") == "Upload package provenance"
    )
    assert '--mcp-version-spec "${{ matrix.mcp_spec }}"' in run_step["run"]
    assert "${{ matrix.mcp_line }}" in package_job["name"]
    assert "${{ matrix.mcp_line }}" in upload_step["with"]["name"]


def test_dependabot_covers_each_repository_package_ecosystem() -> None:
    config = (REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    ecosystems = set(
        re.findall(
            r"package-ecosystem: ([a-z-]+)",
            config,
        )
    )

    assert ecosystems == {"pip", "npm", "github-actions"}


def test_dependabot_defers_unreviewed_breaking_toolchain_updates() -> None:
    config = yaml.safe_load(
        (REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    )
    updates = {entry["package-ecosystem"]: entry for entry in config["updates"]}
    pip_ignores = updates["pip"]["ignore"]
    npm_ignores = updates["npm"]["ignore"]

    assert {
        "dependency-name": "ruff",
        "versions": [">=0.16.0"],
    } in pip_ignores
    assert {
        "dependency-name": "typescript",
        "update-types": ["version-update:semver-major"],
    } in npm_ignores


def test_security_tool_versions_are_bounded() -> None:
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    requirements = pyproject["project"]["optional-dependencies"]["security"]
    runtime_requirements = pyproject["project"]["dependencies"]

    assert any(
        value.startswith("bandit>=") and "<2.0.0" in value for value in requirements
    )
    assert any(
        value.startswith("cyclonedx-bom>=") and "<8.0.0" in value
        for value in requirements
    )
    assert any(
        value.startswith("pip-audit>=") and "<3.0.0" in value for value in requirements
    )
    assert "defusedxml>=0.7.1,<0.8.0" in runtime_requirements
    assert set(pyproject["tool"]["bandit"]["skips"]) == {
        "B110",
        "B404",
        "B603",
        "B607",
        "B608",
    }


def test_the_coverage_floor_is_declared_once_and_enforced_where_it_is_measured() -> (
    None
):
    """A floor in one file and a flag in another drift apart silently.

    The number lives in pyproject; the Quality lane is the only lane that runs
    `--cov`, and the only one that skips nothing, so it is the only place the
    figure is comparable. Removing the flag, or moving either number without
    the other, fails here.
    """

    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    floor = config["tool"]["arcgraph"]["ci"]["coverage"]["floor_percent"]
    assert isinstance(floor, int) and 0 < floor <= 100

    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    coverage_steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "--cov=arcgraph" in str(step.get("run", ""))
    ]
    assert len(coverage_steps) == 1, (
        "expected exactly one lane to measure coverage; a second lane would "
        "enforce the floor against a different set of skipped tests"
    )
    command = str(coverage_steps[0]["run"])
    assert (
        f"--cov-fail-under={floor}" in command
    ), f"the coverage lane must enforce the declared floor of {floor}%"


CURRENT_LABEL_SURFACES = (
    # Ships inside the wheel: an installed rc7 answering `arcgraph docs` with
    # "rc6" tells the operator they are running something they are not.
    "arcgraph/interfaces/docs.py",
    # The operator contract and everything it points at.
    "docs/external-trial-guide.md",
    "docs/mcp-usage.md",
    "docs/language-support.md",
    "docs/agent-reading-guide.md",
    "docs/package-readiness.md",
    "README.md",
    # Refuses to assemble unless the tree matches its own expectation.
    "scripts/arcgraph_external_trial_bundle.py",
)
CANDIDATE_LABEL = re.compile(r"0\.1\.0rc(\d+)|v0\.1\.0-rc(\d+)")
FINAL_VERSION = re.compile(r"\d+\.\d+\.\d+")
# Once a final release is declared, a surface may still name an earlier
# pre-release on purpose, for instance the version an install was tested with.
# Each such phrase is listed here, so any other mention of a candidate still
# fails until someone decides it is history rather than a stale label.
EARLIER_PRERELEASE_MENTIONS = (
    "pip installs of 0.1.0rc10 from PyPI were tested",
    "The earlier 0.1.0rc7–0.1.0rc10 pre-releases remain on PyPI",
    "Installs from PyPI were tested with 0.1.0rc10",
)
# The surfaces above that name the version being run, not just its features.
VERSION_NAMING_SURFACES = (
    "arcgraph/interfaces/docs.py",
    "docs/external-trial-guide.md",
    "docs/package-readiness.md",
    "README.md",
    "scripts/arcgraph_external_trial_bundle.py",
)


def test_sdist_excludes_local_tool_directories() -> None:
    """A file ignored only by a developer's global gitignore is still packed.

    hatch chooses sdist files from the repository .gitignore. A local tool file
    such as .claude/settings.local.json, ignored only by a global gitignore, was
    therefore packed into an sdist built in that checkout but not into one built
    from a clean clone. Naming the directory keeps both builds identical.
    """

    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    excluded = config["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]

    for pattern in ("/.claude", "/.claude/**"):
        assert pattern in excluded, f"sdist exclude is missing {pattern}"


def test_the_build_backend_is_pinned_to_an_exact_version() -> None:
    """The clean-rebuild comparison needs the same backend on both builds.

    An unpinned backend lets a release between two builds change the bytes and
    fail the comparison for a reason that is not tampering. The exact pin makes
    the drift a reviewed change to pyproject.toml instead.
    """

    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requires = config["build-system"]["requires"]

    assert len(requires) == 1, "build-system requires must list only the backend"
    name, _, version = requires[0].partition("==")
    assert name == "hatchling", "the pin is for the declared build backend"
    assert version.count(".") == 2 and version.replace(".", "").isdigit()


def _declared_version() -> str:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(config["project"]["version"])


def test_every_current_label_surface_names_the_declared_version() -> None:
    """Bumping the candidate must fail until every surface follows it.

    Eighteen files mention the candidate label. Some are historical records
    that must keep naming the release they describe; these are the ones that
    state what a reader is running right now, so a stale label here is a
    document that lies to the operator. Rather than a checklist item to
    remember at each cut, the bump itself turns this red.
    """

    declared = _declared_version()
    expected = CANDIDATE_LABEL.search(declared)
    final = expected is None
    assert not final or FINAL_VERSION.fullmatch(declared), (
        f"version {declared!r} is neither a 0.1.0rcN candidate nor a final "
        "release; update this test along with whatever numbering replaced it"
    )
    expected_number = None if final else expected.group(1) or expected.group(2)

    stale: list[tuple[str, str]] = []
    unnamed: list[str] = []
    for relative in CURRENT_LABEL_SURFACES:
        path = REPO_ROOT / relative
        assert path.exists(), f"{relative} is listed as a label surface but is gone"
        text = " ".join(path.read_text(encoding="utf-8").split())
        if final:
            for phrase in EARLIER_PRERELEASE_MENTIONS:
                text = text.replace(phrase, "")
            current = re.compile(rf"(?<![\d.]){re.escape(declared)}(?!rc|\d)")
            if relative in VERSION_NAMING_SURFACES and not current.search(text):
                unnamed.append(relative)
        for match in CANDIDATE_LABEL.finditer(text):
            number = match.group(1) or match.group(2)
            if number != expected_number:
                stale.append((relative, match.group(0)))
    assert stale == [], (
        f"pyproject declares {declared} but these surfaces still name another "
        f"candidate: {sorted(set(stale))}"
    )
    assert unnamed == [], f"these surfaces do not name {declared}: {unnamed}"


def test_the_bundle_assembler_agrees_with_the_declared_version() -> None:
    """A hardcoded expectation that drifts stops the assembler, silently late."""

    source = (REPO_ROOT / "scripts" / "arcgraph_external_trial_bundle.py").read_text(
        encoding="utf-8"
    )
    declared = _declared_version()
    assert f'EXPECTED_VERSION = "{declared}"' in source

    notes = re.search(r'RELEASE_NOTES = Path\("([^"]+)"\)', source)
    assert notes is not None
    assert (REPO_ROOT / notes.group(1)).exists(), (
        f"the assembler points at {notes.group(1)}, which does not exist; a "
        "version bump needs its release-notes file created in the same change"
    )
