"""The coverage.py reader validates JSON and bounds redundant-section retention."""

from io import StringIO
import json

import pytest

from arcgraph.core.coverage_json import load_coverage_json


@pytest.mark.parametrize("chunk_size", [1, 3, 31, 65536])
@pytest.mark.parametrize("indent", [None, 2])
def test_stream_reader_discards_only_file_symbol_sections(chunk_size, indent):
    record = {
        "functions": {"run": {"contexts": {"2": ["tests/a.py::test[汉字|::1]|run"]}}},
        "classes": {"": {"executed_lines": [1, 2]}},
        "executed_lines": [1, 2],
        "contexts": {"2": ["tests/a.py::test[汉字|::1]|run"]},
        "summary": {"percent_covered": 1e-20},
    }
    payload = {"files": {"pkg/a.py": record}, "meta": {"format": 3}}
    result = load_coverage_json(
        StringIO(json.dumps(payload, indent=indent)), chunk_size=chunk_size
    )
    assert result == {
        "files": {
            "pkg/a.py": {
                key: value
                for key, value in record.items()
                if key not in {"functions", "classes"}
            }
        },
        "meta": {"format": 3},
    }


@pytest.mark.parametrize(
    "payload",
    [
        [{"url": "http://localhost/a.js", "functions": [{"ranges": [1, 2]}]}],
        {"tests": [{"functions": ["kept"], "classes": ["kept"]}]},
        {"src/a.js": {"fnMap": {"0": {"name": "run"}}, "f": {"0": 3}}},
        [None, True, False, 1, -2, 1.25, 1e100, 'escape: \\"'],
    ],
)
def test_stream_reader_preserves_other_formats_and_scalar_boundaries(payload):
    encoded = json.dumps(payload)
    for size in range(1, 12):
        assert load_coverage_json(StringIO(encoded), chunk_size=size) == payload


@pytest.mark.parametrize(
    "text",
    [
        '{"files":{"a":{"functions":{"f":{"lines":[1,]}}}}}',
        '{"files":{"a":{"classes":{"C": invalid}}}}',
        '{"files":{"a":{"functions":{"f": {"x": 1 2}}}}}',
        '{"files":{"a":{"classes":{"C": "unterminated}}}}',
        '{"files":{"a":{"functions":{}}',
        '{"files":{},}',
        '{"files":{}} trailing',
    ],
)
def test_stream_reader_rejects_invalid_discarded_sections_and_trailing_input(text):
    for size in [1, 7, 65536]:
        for retain_files in [True, False]:
            with pytest.raises(json.JSONDecodeError):
                load_coverage_json(
                    StringIO(text), chunk_size=size, retain_files=retain_files
                )


def test_stream_reader_does_not_buffer_the_whole_report():
    class BoundedReads(StringIO):
        def read(self, size=-1):
            assert 0 < size <= 1024
            return super().read(size)

    payload = {
        "meta": {"format": 3},
        "files": {
            f"file-{number}.py": {
                "executed_lines": [1],
                "contexts": {"1": ["test::run|run"]},
                "functions": {"a": {"contexts": {"1": ["test::run|run"]}}},
                "classes": {},
            }
            for number in range(1000)
        },
    }
    result = load_coverage_json(BoundedReads(json.dumps(payload)), chunk_size=128)
    assert len(result["files"]) == 1000
    assert all(
        "functions" not in record and "classes" not in record
        for record in result["files"].values()
    )


def test_metadata_reader_keeps_scope_metadata_without_retaining_file_data():
    payload = {
        "meta": {"format": 3},
        "files": {
            "pkg/a.py": {
                "executed_lines": [1],
                "contexts": {"1": ["test::run|run"]},
                "functions": {},
            }
        },
        "arcgraph_metadata": {"tool_version": "7.16.2", "source_roots": ["pkg"]},
    }
    assert load_coverage_json(
        StringIO(json.dumps(payload)), chunk_size=3, retain_files=False
    ) == {key: value for key, value in payload.items() if key != "files"}
