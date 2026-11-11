"""Phase 3 patch contract and A/B reporting tests."""

from app.evaluation.phase3 import (
    _metric_delta,
    render_patch_contract_design,
)
from app.workspace.models import ApplyPatchRequest


def test_new_patch_schema_groups_replacements_by_file():
    request = ApplyPatchRequest.model_validate(
        {
            "files": [
                {
                    "path": "A.java",
                    "replacements": [
                        {"old_text": "one", "new_text": "two"},
                        {"old_text": "three", "new_text": "four"},
                    ],
                }
            ]
        }
    )
    assert request.files[0].path == "A.java"
    assert len(request.files[0].replacements) == 2
    schema = ApplyPatchRequest.model_json_schema()
    assert "files" in schema["properties"]
    assert "changes" not in schema["properties"]


def test_patch_contract_design_documents_atomicity_and_exact_match():
    text = render_patch_contract_design()
    assert "files" in text
    assert "replacements" in text
    assert "all-or-nothing" in text
    assert "exact-match" in text
    assert "replacement_index" in text


def test_metric_delta_uses_treatment_minus_control():
    control = {
        "metrics": {
            "success_rate": 0.8,
            "medium_success_rate": 1.0,
            "hard_success_rate": 0.7,
            "hidden_test_pass_rate": 0.8,
            "average_tokens": 100,
            "average_llm_calls": 5,
            "average_agent_tool_calls": 10,
            "apply_patch_invocations": 8,
            "apply_patch_failures": 2,
            "apply_patch_failure_rate": 0.25,
            "recovered_patch_failures": 1,
            "average_duration": 40,
        }
    }
    treatment = {
        "metrics": {
            **control["metrics"],
            "success_rate": 0.9,
            "apply_patch_failures": 1,
            "apply_patch_failure_rate": 0.125,
        }
    }
    delta = _metric_delta(control, treatment)
    assert delta["success_rate"]["delta"] == 0.9 - 0.8
    assert delta["apply_patch_failures"]["delta"] == -1
    assert delta["apply_patch_failure_rate"]["delta"] == -0.125
