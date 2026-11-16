"""Phase 6.5 candidate acceptance and report tests."""

from app.evaluation.phase6_5 import _acceptance


def _metrics(**overrides):
    value = {
        "average_run_tokens": 1_000_000.0,
        "average_passed_per_run": 6.0,
        "failure_categories": {"no_patch": 4, "step_limit": 6},
    }
    value.update(overrides)
    return value


def test_candidate_acceptance_requires_cost_reduction_and_success_preservation():
    result = _acceptance(
        _metrics(),
        _metrics(average_run_tokens=800_000.0),
    )
    assert result["status"] == "accepted"
    assert result["token_reduction_rate"] == 0.2


def test_candidate_rejected_below_ten_percent_cost_reduction():
    result = _acceptance(
        _metrics(),
        _metrics(average_run_tokens=950_000.0),
    )
    assert result["status"] == "rejected"
    assert result["reason"] == "token_reduction_below_10_percent"


def test_candidate_rejected_when_success_regresses():
    result = _acceptance(
        _metrics(),
        _metrics(
            average_run_tokens=700_000.0,
            average_passed_per_run=5.0,
        ),
    )
    assert result["status"] == "rejected"
    assert result["reason"] == "success_regression"


def test_candidate_inconclusive_when_no_patch_failures_increase():
    result = _acceptance(
        _metrics(),
        _metrics(
            average_run_tokens=700_000.0,
            failure_categories={"no_patch": 7, "step_limit": 6},
        ),
    )
    assert result["status"] == "inconclusive"
    assert result["no_patch_failure_delta"] == 3
