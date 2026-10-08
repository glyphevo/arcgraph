"""Pin the source-only prototype boundary without pretending to build a wheel."""

from pathlib import Path, PurePosixPath
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROTOTYPE = PurePosixPath("arcgraph/semantic_prototype")


@pytest.mark.parametrize(
    "pattern", ["/arcgraph/semantic_prototype", "/arcgraph/semantic_prototype/**"]
)
def test_wheel_excludes_semantic_prototype(pattern):
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    build = config["tool"]["hatch"]["build"]
    wheel = build["targets"]["wheel"]
    assert pattern in wheel["exclude"]
    # Hatch force-include can override exclusion. Check both source and destination,
    # including a forced ancestor directory, rather than just this exact spelling.
    for table in (build, wheel):
        for source, destination in table.get("force-include", {}).items():
            for value in (source, destination):
                path = PurePosixPath(value.lstrip("/"))
                assert not (
                    path == PROTOTYPE
                    or PROTOTYPE in path.parents
                    or path in PROTOTYPE.parents
                )


def test_sdist_keeps_semantic_prototype_sources():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    excluded = config["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    assert not any(PROTOTYPE.match(pattern.lstrip("/")) for pattern in excluded)
    for source in (ROOT / PROTOTYPE).glob("*.py"):
        relative = PurePosixPath(source.relative_to(ROOT).as_posix())
        assert not any(relative.match(pattern.lstrip("/")) for pattern in excluded)
