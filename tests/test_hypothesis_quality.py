"""Phase 8 evidence quality and dependency gating tests."""

from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError

from app.agent.commitment import EvidenceCommitmentController
from app.agent.graph import run_agent
from app.agent.telemetry import ToolEvent
from app.repair.models import (
    CreateQualityEvidenceRepairPlanRequest,
    QualityEvidenceRepairPlan,
)
from tests.fakes import FakeChatModel, tool_call
from tests.test_evidence_commitment import (
    _evidence_plan_args,
    _patch,
    _sequence_runner,
    _test_result,
    _workspace,
)


def _quality_args(
    *,
    revision_reason: str | None = None,
    dependencies: list[dict] | None = None,
    alternatives: list[dict] | None = None,
) -> dict:
    value = _evidence_plan_args(revision_reason=revision_reason)
    value["hypothesis"].update(
        {
            "causal_mechanism": (
                "The stale step0 branch determines the result used by MainTest, "
                "so the observed failing assertion follows directly from it."
            ),
            "testable_prediction": (
                "Replacing the stale branch makes MainTest pass while unaffected "
                "behavior remains unchanged."
            ),
            "unresolved_dependencies": dependencies or [],
            "alternative_explanations": alternatives or [],
        }
    )
    return value


async def test_local_quality_plan_keeps_fast_patch_path(manager, origin_repo):
    info, _ = _workspace(manager, origin_repo)
    model = FakeChatModel(
        script=[
            tool_call("run_tests", {"tool": "maven"}, "initial_test"),
            tool_call("read_file", {"path": "Main.java"}, "read_main"),
            tool_call(
                "create_repair_plan",
                _quality_args(),
                "quality_plan",
            ),
            _patch(0),
        ]
    )
    result = await run_agent(
        "Fix Main",
        info.repository_path,
        model=model,
        workspace_id=info.workspace_id,
        workspace_manager=manager,
        hypothesis_quality=True,
        test_runner=_sequence_runner([_test_result(False), _test_result(True)]),
    )
    assert result.modified_files == ["Main.java"]
    assert isinstance(result.repair_plan, QualityEvidenceRepairPlan)
    assert result.commitment_telemetry.mode == "evidence_aware_commitment"
    assert result.commitment_telemetry.first_patch_step == 4


async def test_invalid_provenance_is_rejected(manager, origin_repo):
    info, _ = _workspace(manager, origin_repo)
    args = _quality_args()
    args["hypothesis"]["evidence"][1]["path"] = "OrderService.java"
    model = FakeChatModel(
        script=[
            tool_call("run_tests", {"tool": "maven"}, "initial_test"),
            tool_call("read_file", {"path": "Main.java"}, "read_main"),
            tool_call("create_repair_plan", args, "bad_plan"),
            AIMessage(content="Evidence is invalid."),
        ]
    )
    result = await run_agent(
        "Fix Main",
        info.repository_path,
        model=model,
        workspace_id=info.workspace_id,
        workspace_manager=manager,
        hypothesis_quality=True,
        test_runner=_sequence_runner([_test_result(False)]),
    )
    assert result.repair_plan is None
    assert not result.commitment_telemetry.evidence_provenance_valid
    feedback = "\n".join(
        message.content
        for history in model.history
        for message in history
        if isinstance(message, ToolMessage)
    )
    assert "was not read" in feedback


async def test_unresolved_dependency_allows_targeted_probe_then_blocks_patch(
    manager,
    origin_repo,
):
    info, _ = _workspace(manager, origin_repo)
    dependency = {
        "question": "Does another caller depend on the current Main behavior?",
        "dependency_type": "caller",
        "candidate_files": ["Main.java"],
        "candidate_symbols": ["Main"],
    }
    model = FakeChatModel(
        script=[
            tool_call("run_tests", {"tool": "maven"}, "initial_test"),
            tool_call("read_file", {"path": "Main.java"}, "read_main"),
            tool_call(
                "create_repair_plan",
                _quality_args(dependencies=[dependency]),
                "quality_plan",
            ),
            tool_call(
                "find_references",
                {"name": "Main"},
                "targeted_refs",
            ),
            _patch(0, call_id="blocked_patch"),
            AIMessage(content="The dependency must be revised first."),
        ]
    )
    result = await run_agent(
        "Fix Main",
        info.repository_path,
        model=model,
        workspace_id=info.workspace_id,
        workspace_manager=manager,
        hypothesis_quality=True,
        test_runner=_sequence_runner([_test_result(False)]),
    )
    probe = next(
        event for event in result.tool_events if event.call_id == "targeted_refs"
    )
    blocked_patch = next(
        event for event in result.tool_events if event.call_id == "blocked_patch"
    )
    assert probe.success is True
    assert blocked_patch.success is False
    assert result.commitment_telemetry.dependency_probe_count == 1
    assert "unresolved_dependency_blocks_patch" in (
        result.commitment_telemetry.validation_errors
    )


async def test_repeated_dependency_probe_is_blocked(manager, origin_repo):
    info, _ = _workspace(manager, origin_repo)
    dependency = {
        "question": "Which interface implementation controls Main behavior?",
        "dependency_type": "interface",
        "candidate_files": ["Main.java"],
        "candidate_symbols": ["Main"],
    }
    model = FakeChatModel(
        script=[
            tool_call("run_tests", {"tool": "maven"}, "initial_test"),
            tool_call("read_file", {"path": "Main.java"}, "read_main"),
            tool_call(
                "create_repair_plan",
                _quality_args(dependencies=[dependency]),
                "quality_plan",
            ),
            tool_call("find_references", {"name": "Main"}, "probe_1"),
            tool_call("find_references", {"name": "Main"}, "probe_2"),
            AIMessage(content="Repeated probe blocked."),
        ]
    )
    result = await run_agent(
        "Fix Main",
        info.repository_path,
        model=model,
        workspace_id=info.workspace_id,
        workspace_manager=manager,
        hypothesis_quality=True,
        test_runner=_sequence_runner([_test_result(False)]),
    )
    repeated = next(
        event for event in result.tool_events if event.call_id == "probe_2"
    )
    assert repeated.success is False
    assert "dependency_probe_blocked" in result.commitment_telemetry.validation_errors


async def test_quality_revision_after_failed_test_requires_fresh_read(
    manager,
    origin_repo,
):
    info, _ = _workspace(manager, origin_repo)
    model = FakeChatModel(
        script=[
            tool_call("run_tests", {"tool": "maven"}, "initial_test"),
            tool_call("read_file", {"path": "Main.java"}, "read_main_1"),
            tool_call("create_repair_plan", _quality_args(), "plan_1"),
            _patch(0, call_id="patch_1"),
            tool_call("read_file", {"path": "Main.java"}, "read_main_2"),
            tool_call(
                "create_repair_plan",
                _quality_args(revision_reason="Test failed after first patch"),
                "plan_2",
            ),
            _patch(1, call_id="patch_2"),
        ]
    )
    result = await run_agent(
        "Fix Main",
        info.repository_path,
        model=model,
        workspace_id=info.workspace_id,
        workspace_manager=manager,
        hypothesis_quality=True,
        test_runner=_sequence_runner(
            [
                _test_result(False),
                _test_result(False),
                _test_result(True),
            ]
        ),
    )
    assert result.commitment_telemetry.plan_revision_count == 2
    assert result.commitment_telemetry.hypothesis_revision_count == 1
    assert result.commitment_telemetry.plan_execution_success is True


def test_quality_hypothesis_serialization():
    request = CreateQualityEvidenceRepairPlanRequest.model_validate(
        _quality_args()
    )
    restored = CreateQualityEvidenceRepairPlanRequest.model_validate_json(
        request.model_dump_json()
    )
    assert restored.hypothesis.causal_mechanism
    assert restored.hypothesis.testable_prediction
    assert isinstance(restored, QualityEvidenceRepairPlan)


def test_missing_causal_mechanism_and_prediction_are_rejected():
    missing_cause = _quality_args()
    missing_cause["hypothesis"].pop("causal_mechanism")
    try:
        CreateQualityEvidenceRepairPlanRequest.model_validate(missing_cause)
    except ValidationError as exc:
        assert "causal_mechanism" in str(exc)
    else:
        raise AssertionError("Missing causal mechanism was accepted")

    missing_prediction = _quality_args()
    missing_prediction["hypothesis"].pop("testable_prediction")
    try:
        CreateQualityEvidenceRepairPlanRequest.model_validate(
            missing_prediction
        )
    except ValidationError as exc:
        assert "testable_prediction" in str(exc)
    else:
        raise AssertionError("Missing testable prediction was accepted")


def _event(index: int, step: int, name: str, target: str | None = None):
    return ToolEvent(
        tool_call_index=index,
        step=step,
        repair_round=0,
        tool_name=name,
        target_file=target,
        success=True,
        invoked=True,
        argument_fingerprint=str(index),
        read_start_line=1 if name == "read_file" else None,
        read_end_line=20 if name == "read_file" else None,
    )


def test_source_changed_after_observation_is_invalid_evidence():
    args = _quality_args()
    args.pop("revision_reason")
    plan = QualityEvidenceRepairPlan.model_validate(args)
    events = [
        _event(1, 1, "run_tests"),
        _event(2, 2, "read_file", "Main.java"),
        _event(3, 3, "apply_patch", "Main.java"),
    ]
    errors = EvidenceCommitmentController.validate_quality_plan(
        plan,
        events,
        has_seen_test_failure=True,
    )
    assert any("source changed after observation" in error for error in errors)
