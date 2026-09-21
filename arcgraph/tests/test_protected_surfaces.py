from __future__ import annotations

from pathlib import Path

import pytest

from arcgraph.change.protected_surfaces import ProtectedSurfaceDetector
from arcgraph.core.schemas import Node


def test_python_public_api_requires_explicit_graph_evidence(tmp_path: Path) -> None:
    detector = ProtectedSurfaceDetector(tmp_path, repo_id="repo")
    ordinary = Node(
        id="fn:app.public_name",
        kind="function",
        name="public_name",
        path="src/app.py",
    )
    exported = ordinary.model_copy(
        update={"id": "fn:app.exported", "properties": {"public_api": True}}
    )

    ordinary_surfaces = detector.detect([ordinary])
    exported_surfaces = detector.detect([exported])

    assert not any(
        surface.surface_kind == "python_public_api" for surface in ordinary_surfaces
    )
    assert any(
        surface.surface_kind == "python_public_api"
        and surface.detection_status == "candidate"
        for surface in exported_surfaces
    )


def test_dynamic_framework_registration_is_unknown_not_confirmed(
    tmp_path: Path,
) -> None:
    detector = ProtectedSurfaceDetector(tmp_path, repo_id="repo")
    node = Node(
        id="registration:dynamic",
        kind="framework_registration",
        name="dynamic",
        path="src/app.py",
        properties={"dynamic": True},
    )

    surfaces = detector.detect([node])

    assert any(
        surface.surface_kind == "framework_registration"
        and surface.detection_status == "unknown"
        for surface in surfaces
    )


def test_explicit_missing_openapi_input_is_unknown(tmp_path: Path) -> None:
    detector = ProtectedSurfaceDetector(tmp_path, repo_id="repo")

    surfaces = detector.detect([], openapi_input="api/openapi.yaml")

    assert surfaces[0].surface_kind == "openapi"
    assert surfaces[0].detection_status == "unknown"


def test_explicit_valid_openapi_input_is_confirmed_protocol_evidence(
    tmp_path: Path,
) -> None:
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    (api_dir / "openapi.yaml").write_text(
        "openapi: 3.0.3\ninfo:\n  title: Example\npaths: {}\n",
        encoding="utf-8",
    )
    detector = ProtectedSurfaceDetector(tmp_path, repo_id="repo")

    surfaces = detector.detect([], openapi_input="api/openapi.yaml")
    surface = surfaces[0]

    assert surface.capability == "explicit_protocol_input"
    assert surface.detection_status == "confirmed"
    matrix = detector.capability_matrix(surfaces)
    assert matrix["openapi"]["capabilities"] == ["explicit_protocol_input"]
    assert matrix["openapi"]["detection_statuses"] == ["confirmed"]


def test_user_declaration_is_declared_not_graph_confirmed(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text("# boundary\n", encoding="utf-8")
    detector = ProtectedSurfaceDetector(tmp_path, repo_id="repo")

    surfaces = detector.detect([], user_declarations=["src/auth.py"])

    assert surfaces[0].surface_kind == "security_boundary"
    assert surfaces[0].capability == "user_declaration_required"
    assert surfaces[0].detection_status == "declared"


def test_invalid_user_declared_path_remains_a_global_declared_surface(
    tmp_path: Path,
) -> None:
    detector = ProtectedSurfaceDetector(tmp_path, repo_id="repo")

    surfaces = detector.detect([], user_declarations=["../outside.py"])

    assert surfaces[0].path is None
    assert surfaces[0].path_comparison_key is None
    assert surfaces[0].detection_status == "declared"


@pytest.mark.parametrize("payload", ["openapi: [", "- item\n"])
def test_invalid_openapi_payload_is_unknown(tmp_path: Path, payload: str) -> None:
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    (api_dir / "openapi.yaml").write_text(payload, encoding="utf-8")
    detector = ProtectedSurfaceDetector(tmp_path, repo_id="repo")

    surface = detector.detect([], openapi_input="api/openapi.yaml")[0]

    assert surface.capability == "unavailable"
    assert surface.detection_status == "unknown"


def test_openapi_path_escape_is_unknown_and_pathless(tmp_path: Path) -> None:
    detector = ProtectedSurfaceDetector(tmp_path, repo_id="repo")

    surface = detector.detect([], openapi_input="../openapi.yaml")[0]

    assert surface.path is None
    assert surface.detection_status == "unknown"


@pytest.mark.parametrize(
    ("property_name", "property_value"),
    [
        ("surface_kind", "typescript_export"),
        ("surface_type", "typescript_export"),
        ("typescript_export", True),
    ],
)
def test_arbitrary_node_properties_cannot_self_claim_a_confirmed_surface(
    tmp_path: Path,
    property_name: str,
    property_value: object,
) -> None:
    detector = ProtectedSurfaceDetector(tmp_path, repo_id="repo")
    node = Node(
        id="fn:app.helper",
        kind="function",
        name="helper",
        path="src/app.py",
        properties={property_name: property_value},
    )

    surfaces = detector.detect([node])

    assert not any(surface.surface_kind == "typescript_export" for surface in surfaces)


def test_typed_typescript_export_is_a_confirmed_graph_surface(tmp_path: Path) -> None:
    detector = ProtectedSurfaceDetector(tmp_path, repo_id="repo")
    node = Node(
        id="export:app.item",
        kind="typescript_export",
        name="item",
        path="src/app.ts",
    )

    surface = detector.detect([node])[0]

    assert surface.surface_kind == "typescript_export"
    assert surface.capability == "reliable_graph_fact"
    assert surface.detection_status == "confirmed"


def test_openapi_read_error_degrades_to_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    document = api_dir / "openapi.yaml"
    document.write_text(
        "openapi: 3.0.3\ninfo:\n  title: Example\npaths: {}\n",
        encoding="utf-8",
    )
    original = Path.read_text

    def fail_read(path: Path, *args: object, **kwargs: object) -> str:
        if path == document:
            raise OSError("simulated read failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_read)

    surface = ProtectedSurfaceDetector(tmp_path, repo_id="repo").detect(
        [],
        openapi_input="api/openapi.yaml",
    )[0]

    assert surface.capability == "unavailable"
    assert surface.detection_status == "unknown"
