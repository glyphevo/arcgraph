from __future__ import annotations

import copy

import pytest

from arcgraph.core.payload_policy import (
    MAX_FRESHNESS_SAMPLES_PER_KIND,
    STALE_INDEX_WARNING_KIND,
    STALE_INDEX_WARNING_MESSAGE,
    apply_index_status_contract,
    apply_target_payload_contract,
)


def _payload(
    *,
    status: str,
    stale: bool,
    stale_files: list[str] | None = None,
    warnings: list[object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "status": "partial" if status == "stale" else "available",
        "freshness": {
            "status": status,
            "stale": stale,
            "stale_files": stale_files or [],
            "stale_modules": [],
        },
        "capabilities": {},
        "warnings": list(warnings or []),
    }


def _stale_warnings(payload: dict[str, object]) -> list[dict[str, object]]:
    warnings = payload.get("warnings")
    assert isinstance(warnings, list)
    return [
        warning
        for warning in warnings
        if isinstance(warning, dict) and warning.get("kind") == STALE_INDEX_WARNING_KIND
    ]


def test_stale_warning_is_path_free_idempotent_and_does_not_mutate_input() -> None:
    original = _payload(
        status="stale",
        stale=True,
        stale_files=["src/secret.py", "src/other.py", "tests/test_secret.py"],
        warnings=["existing warning"],
    )
    snapshot = copy.deepcopy(original)

    once = apply_target_payload_contract(original)
    twice = apply_target_payload_contract(once)

    assert original == snapshot
    assert once == twice
    assert _stale_warnings(once) == [
        {
            "kind": STALE_INDEX_WARNING_KIND,
            "message": STALE_INDEX_WARNING_MESSAGE,
            "stale_file_count": 3,
        }
    ]
    stale_warning_text = repr(_stale_warnings(once))
    assert "src/secret.py" not in stale_warning_text
    assert "src/other.py" not in stale_warning_text


def test_target_payload_bounds_repeated_freshness_lists_but_current_keeps_them() -> (
    None
):
    stale_files = [f"src/pkg/file_{index}.py" for index in range(82)]
    stale_modules = [f"pkg.file_{index}" for index in range(82)]
    original = _payload(status="stale", stale=True, stale_files=stale_files)
    original["freshness"]["stale_modules"] = stale_modules

    target_payload = apply_target_payload_contract(original)
    current_payload = apply_index_status_contract(original)

    assert (
        target_payload["freshness"]["stale_files"]
        == stale_files[:MAX_FRESHNESS_SAMPLES_PER_KIND]
    )
    assert (
        target_payload["freshness"]["stale_modules"]
        == stale_modules[:MAX_FRESHNESS_SAMPLES_PER_KIND]
    )
    assert current_payload["freshness"]["stale_files"] == stale_files
    assert current_payload["freshness"]["stale_modules"] == stale_modules
    warning = _stale_warnings(target_payload)[0]
    assert warning["stale_file_count"] == 82
    assert warning["stale_module_count"] == 82
    assert warning["freshness_details_omitted"] == 154
    assert warning["freshness_sample_limit"] == MAX_FRESHNESS_SAMPLES_PER_KIND
    assert "arcgraph current" in warning["detail"]
    assert apply_target_payload_contract(target_payload) == target_payload


def test_target_payload_bounds_nested_lists_including_existing_warning() -> None:
    stale_files = [f"src/pkg/file_{index}.py" for index in range(12)]
    stale_modules = [f"pkg.file_{index}" for index in range(11)]
    existing_warning = {
        "kind": STALE_INDEX_WARNING_KIND,
        "message": STALE_INDEX_WARNING_MESSAGE,
        "stale_file_count": len(stale_files),
        "stale_module_count": len(stale_modules),
        "evidence": {"stale_modules": stale_modules},
    }
    original = _payload(
        status="stale",
        stale=True,
        stale_files=stale_files,
        warnings=[existing_warning],
    )
    original["freshness"]["stale_modules"] = stale_modules
    original["risk_factors"] = [
        {"details": {"stale_files": stale_files, "stale_modules": stale_modules}}
    ]
    snapshot = copy.deepcopy(original)

    bounded = apply_target_payload_contract(original)

    assert original == snapshot
    assert bounded["freshness"]["stale_files"] == stale_files[:5]
    assert bounded["freshness"]["stale_modules"] == stale_modules[:5]
    assert bounded["risk_factors"][0]["details"]["stale_files"] == []
    assert bounded["risk_factors"][0]["details"]["stale_modules"] == []
    warning = _stale_warnings(bounded)[0]
    assert warning["evidence"]["stale_modules"] == []
    assert warning["stale_file_count"] == len(stale_files)
    assert warning["stale_module_count"] == len(stale_modules)
    assert apply_target_payload_contract(bounded) == bounded


def test_nested_freshness_lists_contribute_to_truthful_omitted_count() -> None:
    stale_files = [f"src/pkg/file_{index}.py" for index in range(40)]
    stale_modules = [f"pkg.file_{index}" for index in range(12)]
    original = {
        "schema_version": "1.0.0",
        "status": "partial",
        "freshness": {
            "status": "stale",
            "stale": True,
            "stale_file_count": len(stale_files),
            "stale_module_count": len(stale_modules),
        },
        "index_status": {
            "stale_files": stale_files,
            "stale_modules": stale_modules,
        },
        "capabilities": {},
        "warnings": [
            {
                "kind": STALE_INDEX_WARNING_KIND,
                "message": STALE_INDEX_WARNING_MESSAGE,
                "stale_file_count": len(stale_files),
                "stale_module_count": len(stale_modules),
            }
        ],
    }

    bounded = apply_target_payload_contract(original)

    assert bounded["index_status"]["stale_files"] == stale_files[:5]
    assert bounded["index_status"]["stale_modules"] == stale_modules[:5]
    warning = _stale_warnings(bounded)[0]
    assert warning["freshness_details_omitted"] == 42


def test_freshness_sample_is_global_across_distinct_provider_copies() -> None:
    canonical = [f"src/canonical_{index}.py" for index in range(10)]
    provider_only = [f"src/provider_{index}.py" for index in range(5)]
    original = _payload(
        status="stale",
        stale=True,
        stale_files=canonical,
        warnings=[
            {
                "kind": STALE_INDEX_WARNING_KIND,
                "message": STALE_INDEX_WARNING_MESSAGE,
                "stale_file_count": len(canonical),
                "stale_module_count": 0,
            }
        ],
    )
    original["risk_factors"] = [{"stale_files": provider_only}]

    bounded = apply_target_payload_contract(original)

    assert bounded["freshness"]["stale_files"] == canonical[:5]
    assert bounded["risk_factors"][0]["stale_files"] == []
    warning = _stale_warnings(bounded)[0]
    assert warning["stale_file_count"] == 15
    assert warning["freshness_details_omitted"] == 10


def test_canonical_freshness_sample_is_independent_of_payload_key_order() -> None:
    stale_files = [f"src/file_{index}.py" for index in range(10)]
    canonical_first = _payload(
        status="stale",
        stale=True,
        stale_files=list(stale_files),
    )
    canonical_first["risk_factors"] = [{"stale_files": list(stale_files)}]
    secondary_first = {
        "risk_factors": [{"stale_files": list(stale_files)}],
        **{
            key: value
            for key, value in canonical_first.items()
            if key != "risk_factors"
        },
    }

    bounded = apply_target_payload_contract(secondary_first)

    assert bounded == apply_target_payload_contract(canonical_first)
    assert bounded["freshness"]["stale_files"] == stale_files[:5]
    assert bounded["risk_factors"][0]["stale_files"] == []


def test_freshness_sample_is_global_across_structural_why_copy() -> None:
    stale_files = [f"src/file_{index}.py" for index in range(10)]
    original = _payload(status="stale", stale=True, stale_files=stale_files)
    original["structural_why"] = [
        {"kind": "freshness", "evidence": {"stale_files": list(stale_files)}}
    ]

    bounded = apply_target_payload_contract(original)

    assert bounded["freshness"]["stale_files"] == stale_files[:5]
    assert bounded["structural_why"][0]["evidence"]["stale_files"] == []
    warning = _stale_warnings(bounded)[0]
    assert warning["stale_file_count"] == 10
    assert warning["freshness_details_omitted"] == 5


def test_freshness_sample_is_global_across_agent_task_context_wrappers() -> None:
    stale_files = [f"src/file_{index}.py" for index in range(10)]

    def provider_payload() -> dict[str, object]:
        return _payload(
            status="stale",
            stale=True,
            stale_files=list(stale_files),
        )

    original = _payload(
        status="stale",
        stale=True,
        stale_files=list(stale_files),
    )
    original["index_status"] = provider_payload()
    original["structural_context"] = provider_payload()
    original["risk"] = provider_payload()
    original["why"] = provider_payload()
    original["symbols"] = {"stale_files": ["must-not-be-traversed.py"]}

    bounded = apply_target_payload_contract(original)

    assert bounded["freshness"]["stale_files"] == stale_files[:5]
    for key in ("index_status", "structural_context", "risk", "why"):
        assert bounded[key]["freshness"]["stale_files"] == []
    assert bounded["symbols"]["stale_files"] == ["must-not-be-traversed.py"]
    warning = _stale_warnings(bounded)[0]
    assert warning["stale_file_count"] == len(stale_files)
    assert warning["freshness_details_omitted"] == 5


def test_freshness_sample_also_bounds_assurance_indexed_scope() -> None:
    stale_files = [f"src/file_{index}.py" for index in range(10)]
    original = _payload(status="stale", stale=True, stale_files=list(stale_files))
    original["assurance"] = {
        "indexed_scope": {
            "freshness": {
                "status": "stale",
                "stale": True,
                "stale_files": list(stale_files),
                "stale_modules": [],
            }
        }
    }

    bounded = apply_target_payload_contract(original)

    assert bounded["freshness"]["stale_files"] == stale_files[:5]
    assert bounded["assurance"]["indexed_scope"]["freshness"]["stale_files"] == []
    warning = _stale_warnings(bounded)[0]
    assert warning["stale_file_count"] == 10
    assert warning["freshness_details_omitted"] == 5


def test_freshness_sample_accepts_structured_provider_details() -> None:
    structured = [
        {"path": f"src/file_{index}.py", "reason": "modified"} for index in range(6)
    ]
    original = _payload(status="stale", stale=True)
    original["risk_factors"] = [{"stale_files": structured}]

    bounded = apply_target_payload_contract(original)

    assert bounded["risk_factors"][0]["stale_files"] == structured[:5]
    warning = _stale_warnings(bounded)[0]
    assert warning["stale_file_count"] == 6
    assert warning["freshness_details_omitted"] == 1


def test_freshness_deduplication_membership_work_is_linear() -> None:
    class ComparisonCountingString(str):
        comparisons = 0

        def __eq__(self, other: object) -> bool:
            type(self).comparisons += 1
            return super().__eq__(other)

        __hash__ = str.__hash__

    stale_files = [
        ComparisonCountingString(f"src/file_{index}.py") for index in range(400)
    ]
    original = _payload(status="stale", stale=True, stale_files=stale_files)

    bounded = apply_target_payload_contract(original)

    assert bounded["freshness"]["stale_files"] == stale_files[:5]
    assert ComparisonCountingString.comparisons < len(stale_files) * 4


def test_small_top_level_freshness_lists_skip_unrelated_payload_walk() -> None:
    class ExplodingDict(dict[str, object]):
        def items(self):  # type: ignore[override]
            raise AssertionError("unrelated payload branch was traversed")

    original = _payload(
        status="stale",
        stale=True,
        stale_files=["src/changed.py"],
    )
    original["symbols"] = ExplodingDict({"large": "payload"})

    bounded = apply_target_payload_contract(original)

    assert bounded["freshness"]["stale_files"] == ["src/changed.py"]
    assert isinstance(bounded["symbols"], ExplodingDict)


def test_index_status_contract_only_adds_the_idempotent_stale_warning() -> None:
    original = _payload(
        status="stale",
        stale=True,
        stale_files=["src/changed.py"],
        warnings=["existing warning"],
    )
    original["capabilities"] = {"calls": "available"}
    snapshot = copy.deepcopy(original)

    once = apply_index_status_contract(original)
    twice = apply_index_status_contract(once)

    assert original == snapshot
    assert once == twice
    assert once["schema_version"] == "1.0.0"
    assert once["capabilities"] == {"calls": "available"}
    assert len(_stale_warnings(once)) == 1


@pytest.mark.parametrize(
    ("status", "stale"),
    [("fresh", False), ("unknown", True)],
)
def test_stale_warning_requires_confirmed_stale_status(
    status: str,
    stale: bool,
) -> None:
    payload = apply_target_payload_contract(
        _payload(status=status, stale=stale, stale_files=["src/changed.py"])
    )

    assert _stale_warnings(payload) == []


def test_existing_stale_warning_is_not_duplicated() -> None:
    existing = {
        "kind": STALE_INDEX_WARNING_KIND,
        "message": "Existing producer-specific stale warning.",
        "stale_file_count": 7,
    }

    payload = apply_target_payload_contract(
        _payload(
            status="stale",
            stale=True,
            stale_files=["src/changed.py"],
            warnings=[existing],
        )
    )

    assert _stale_warnings(payload) == [existing]


def test_contract_does_not_mutate_its_input_when_bounding_resolutions() -> None:
    """The contract shallow-copies the payload, so anything it writes into a
    nested dict would land in the caller's object. Bounding the resolution
    touches `truncation` and its counts, which are exactly such dicts."""

    import copy

    payload = {
        "schema_version": "1.0.0",
        "max_results": 2,
        "target_resolution": {
            "query": "src/wide.py",
            "status": "resolved",
            "resolved_ids": [f"fn:s{index}" for index in range(10)],
        },
        "resolved_targets": [f"fn:s{index}" for index in range(10)],
        "truncation": {"truncated": False, "reason": None, "truncated_counts": {}},
        "freshness": {
            "status": "stale",
            "stale": True,
            "stale_files": [f"src/f{index}.py" for index in range(12)],
        },
        "warnings": [],
    }
    original = copy.deepcopy(payload)

    result = apply_target_payload_contract(payload)

    assert payload == original
    # The returned payload still carries the bound and its disclosure.
    assert len(result["target_resolution"]["resolved_ids"]) == 2
    assert len(result["resolved_targets"]) == 2
    assert result["truncation"]["truncated"] is True
    assert set(result["truncation"]["truncated_counts"]) == {
        "target_resolution.resolved_ids",
        "resolved_targets",
    }


def test_contract_bounds_report_siblings_without_provider_preslicing() -> None:
    """A report carries the ids twice, and the contract is the one place all
    surfaces cross. A new surface that forgets to pre-slice must still not
    emit the full list beside a bounded one."""

    payload = {
        "schema_version": "1.0.0",
        "max_results": 5,
        "reports": [
            {
                "target": "src/wide.py",
                "target_resolution": {
                    "query": "src/wide.py",
                    "status": "resolved",
                    "resolved_ids": [f"fn:s{index}" for index in range(40)],
                },
                "resolved_targets": [f"fn:s{index}" for index in range(40)],
            }
        ],
        "truncation": {"truncated": False, "reason": None, "truncated_counts": {}},
    }

    result = apply_target_payload_contract(payload)
    report = result["reports"][0]

    assert len(report["target_resolution"]["resolved_ids"]) == 5
    assert len(report["resolved_targets"]) == 5
    counts = result["truncation"]["truncated_counts"]
    assert counts["reports.target_resolution.resolved_ids"] == 35
    assert counts["reports.resolved_targets"] == 35


def test_late_truncation_downgrades_the_assurance_it_invalidates() -> None:
    """Assurance reads response truncation, so a bound applied after it is
    built would leave the payload asserting the opposite of itself."""

    payload = {
        "schema_version": "1.0.0",
        "max_results": 5,
        "target_resolution": {
            "query": "src/wide.py",
            "status": "resolved",
            "resolved_ids": [f"fn:s{index}" for index in range(40)],
        },
        "truncation": {"truncated": False, "reason": None, "truncated_counts": {}},
        "assurance": {
            "posture": "bounded_support",
            "limits": {"response_truncated": False, "response_truncation_count": 0},
            "non_claims": [],
            "verification_required": [],
            "target_resolutions": [],
        },
    }

    result = apply_target_payload_contract(payload)
    assurance = result["assurance"]

    assert result["truncation"]["truncated"] is True
    assert assurance["limits"]["response_truncated"] is True
    assert assurance["posture"] == "limited"
    assert assurance["non_claims"]
    assert assurance["verification_required"]
    # A payload that was not truncated keeps its posture untouched.
    untouched = apply_target_payload_contract(
        {
            "schema_version": "1.0.0",
            "max_results": 50,
            "target_resolution": {
                "query": "one",
                "status": "resolved",
                "resolved_ids": ["fn:one"],
            },
            "truncation": {"truncated": False, "reason": None, "truncated_counts": {}},
            "assurance": {
                "posture": "bounded_support",
                "limits": {"response_truncated": False},
                "non_claims": [],
                "verification_required": [],
                "target_resolutions": [],
            },
        }
    )
    assert untouched["assurance"]["posture"] == "bounded_support"


def test_already_truncated_assurance_absorbs_later_counts() -> None:
    """An assurance already marked truncated still has to take the counts
    added after it was built, or the payload's own total disagrees with the
    number printed beside it."""

    from arcgraph.core.assurance import apply_response_truncation

    assurance = {
        "posture": "limited",
        "limits": {"response_truncated": True, "response_truncation_count": 2},
        "non_claims": ["The response is a presentation subset of the analyzed result."],
        "verification_required": [
            "Request a larger result limit or inspect the raw result."
        ],
    }
    truncation = {"truncated": True, "truncated_counts": {"a": 2, "b": 2}}

    updated = apply_response_truncation(assurance, truncation)

    assert updated["limits"]["response_truncation_count"] == 4
    # Monotone: nothing is added twice and the posture does not strengthen.
    assert updated["posture"] == "limited"
    assert len(updated["non_claims"]) == 1
    assert len(updated["verification_required"]) == 1
