"""Doctor's new visible check fails by assertion on the unchanged parent."""

import argparse
from arcgraph.interfaces.cli import handle_doctor


def test_doctor_reports_semantic_core_and_setup_remedy(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    (tmp_path / "demo.py").write_text("def f(): return 1\n", encoding="utf-8")
    payload = handle_doctor(
        argparse.Namespace(repo_root=str(tmp_path), output_dir="output")
    )
    semantic = [
        check for check in payload["checks"] if check["name"] == "semantic_core"
    ]
    assert len(semantic) == 1, "Doctor must expose the semantic toolchain check"
    assert semantic[0]["status"] == "warn"
    assert "arcgraph setup semantic" in semantic[0]["fix"]
