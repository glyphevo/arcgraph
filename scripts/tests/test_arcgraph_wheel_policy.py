"""Both artifact gates must enforce the same reviewed wheel boundary."""

from __future__ import annotations

import io
from pathlib import Path
import tarfile
import zipfile

import pytest

from scripts.tests.test_arcgraph_package_readiness_smoke import (
    DIST_INFO,
    _record_rows,
    _wheel_files,
    load_package_smoke_module,
)
from scripts.tests.test_arcgraph_release_gate import load_gate_module

ROOT = Path(__file__).resolve().parents[2]
PRODUCT = "arcgraph/core/nonrequired_product.py"
PROTOTYPE = "arcgraph/semantic_prototype/nested/record.py"
NEIGHBOR = "arcgraph/semantic_prototype_product.py"


@pytest.mark.parametrize("checker", ["release", "smoke"])
@pytest.mark.parametrize(
    "case",
    [
        "intentional-omission",
        "missing-product",
        "included-prototype",
        "untracked-prototype",
        "prototype-directory-entry",
        "broad-exclude",
        "appended-exclude",
        "global-exclude",
        "missing-exclude",
        "missing-neighbor",
        "untracked-product",
    ],
)
def test_both_artifact_gates_enforce_reviewed_wheel_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checker: str, case: str
) -> None:
    gate = load_gate_module()
    smoke = load_package_smoke_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if case == "broad-exclude":
        config = config.replace('"/arcgraph/semantic_prototype/**"', '"/arcgraph/**"')
    elif case == "appended-exclude":
        config = config.replace(
            '    "/arcgraph/semantic_prototype/**",',
            '    "/arcgraph/semantic_prototype/**",\n    "/arcgraph/core/**",',
        )
    elif case == "global-exclude":
        config = config.replace(
            "[tool.hatch.build.hooks.custom]",
            '[tool.hatch.build]\nexclude = ["/arcgraph/**"]\n\n'
            "[tool.hatch.build.hooks.custom]",
        )
    elif case == "missing-exclude":
        config = config.replace('    "/arcgraph/semantic_prototype/**",\n', "")
    (repo / "pyproject.toml").write_text(config, encoding="utf-8")
    monkeypatch.setattr(gate, "REPO_ROOT", repo)
    monkeypatch.setattr(
        gate,
        "TYPESCRIPT_EXTRACTOR_HELPER_DIR",
        repo / "arcgraph/pipeline/typescript_extractor",
    )

    files = _wheel_files(smoke)
    files.update({name: b"" for name in gate.required_wheel_files()})
    files.update({PRODUCT: b"product", NEIGHBOR: b"neighbor"})
    tracked_package = sorted(
        {name for name in files if name.startswith("arcgraph/")}
        - gate.GENERATED_WHEEL_FILES
        | {PROTOTYPE}
    )
    if case == "missing-product":
        del files[PRODUCT]
    elif case == "missing-neighbor":
        del files[NEIGHBOR]
    elif case in ("included-prototype", "untracked-prototype"):
        files[PROTOTYPE] = b"source only"
        if case == "untracked-prototype":
            tracked_package.remove(PROTOTYPE)
    elif case == "prototype-directory-entry":
        files["arcgraph/semantic_prototype/"] = b""
    elif case == "untracked-product":
        files["arcgraph/core/untracked_product.py"] = b"untracked"
    files[f"{DIST_INFO}/RECORD"] = (
        "\n".join(_record_rows(smoke, files)) + "\n"
    ).encode("utf-8")
    wheel = tmp_path / "arcgraph-0.1.0rc7-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = (0o040755 if name.endswith("/") else 0o100644) << 16
            archive.writestr(info, data)
    # The source-only prototype remains tracked and present in the sdist.
    tracked = [*tracked_package, PROTOTYPE, "README.md", "pyproject.toml"]
    sdist = tmp_path / "arcgraph-0.1.0rc7.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name in sorted(set(tracked) | {"PKG-INFO"}):
            info = tarfile.TarInfo("arcgraph-0.1.0rc7/" + name)
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))

    if checker == "release":
        errors = gate.validate_wheel_contents(
            wheel, tracked_package_files=tracked_package
        )
    else:
        result = smoke._validate_package_contents(
            wheel=wheel,
            sdist=sdist,
            tracked_package_files=tracked_package,
            tracked_files=tracked,
            repo_root=repo,
        )
        assert result["sdist_files_absent_from_git"] == []
        assert result["sdist_non_regular_members"] == []
        errors = result["errors"]
    if case == "intentional-omission":
        assert errors == []
    elif case in ("missing-product", "missing-neighbor", "untracked-product"):
        expected = {
            "missing-product": PRODUCT,
            "missing-neighbor": NEIGHBOR,
            "untracked-product": "arcgraph/core/untracked_product.py",
        }[case]
        assert any(expected in error for error in errors), errors
    elif case in (
        "broad-exclude",
        "appended-exclude",
        "global-exclude",
        "missing-exclude",
    ):
        assert any("reviewed source-only policy" in error for error in errors), errors
    else:
        assert any(
            "source" in error and "semantic_prototype" in error for error in errors
        ), errors
