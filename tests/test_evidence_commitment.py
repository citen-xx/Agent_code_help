"""Evidence-driven commitment integration tests with a fake LLM."""

import inspect
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.commitment import EvidenceCommitmentController
from app.agent.graph import run_agent
from app.repair.models import (
    CreateEvidenceRepairPlanRequest,
    PlanExecutionStatus,
)
from app.repair.tools import create_repair_plan_tools
from app.workspace.models import BuildTool, TestRunResult as MavenTestResult
from tests.fakes import FakeChatModel, tool_call


def _write(repo: Path, relative: str, content: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _workspace(manager, origin_repo):
    info = manager.create(origin_repo.as_uri())
    repo = Path(info.repository_path)
    _write(repo, "pom.xml", "<project/>")
    _write(repo, "Main.java", "public class Main { // step0 }\n")
    _write(repo, "OrderService.java", "class OrderService {}\n")
    return info, repo


def _evidence_plan_args(
    *,
    revision_reason: str | None = None,
    unresolved: list[str] | None = None,
) -> dict:
    return {
        "problem_summary": "Main behavior is inconsistent with MainTest",
        "target_symbols": ["Main"],
        "impacted_files": ["Main.java"],
        "impacted_tests": ["MainTest"],
        "planned_changes": [
            {
                "file_path": "Main.java",
                "target_symbol": "Main",
                "change_description": "Correct the observed branch behavior",
                "reason": "The observed source contradicts the failing test",
            }
        ],
        "risk_notes": ["Keep the change local to Main"],
        "validation_plan": {
            "run_maven_test": True,
            "focus": ["MainTest"],
            "notes": [],
        },
        "hypothesis": {
            "failure_observation": "MainTest reports incorrect Main behavior",
            "evidence": [
                {
                    "evidence_type": "test_failure",
                    "description": "Maven test failed for MainTest",
                },
                {
                    "evidence_type": "source_read",
                    "description": "Main implementation contains step0 behavior",
                    "path": "Main.java",
                    "start_line": 1,
                    "end_line": 1,
                },
            ],
            "suspected_files": ["Main.java"],
            "suspected_symbols": ["Main"],
            "root_cause_hypothesis": "The stale step0 branch causes the failure",
            "proposed_change": "Replace the stale branch",
            "expected_behavior": "MainTest passes",
            "verification_strategy": ["Run Maven test and inspect MainTest"],
            "unresolved_questions": unresolved or [],
            "confidence": 0.8,
        },
        "revision_reason": revision_reason,
    }


def _patch(step: int, path: str = "Main.java", call_id: str | None = None):
    return tool_call(
        "apply_patch",
        {
            "files": [
                {
                    "path": path,
                    "replacements": [
                        {
                            "old_text": f"// step{step}",
                            "new_text": f"// step{step + 1}",
                        }
                    ],
                }
            ]
        },
        call_id or f"patch_{step}",
    )


def _test_result(success: bool) -> MavenTestResult:
    return MavenTestResult(
        tool=BuildTool.MAVEN,
        command=["mvn", "test"],
        exit_code=0 if success else 1,
        success=success,
        stdout="Tests passed" if success else "Tests failed",
        stderr="" if success else "MainTest failure",
        duration_seconds=0.1,
    )


def _sequence_runner(results):
    values = iter(results)

    def run(repository_path, tool, timeout_seconds):
        return next(values)

    return run


async def test_insufficient_evidence_rejects_hypothesis(manager, origin_repo):
    info, _ = _workspace(manager, origin_repo)
    model = FakeChatModel(
        script=[
            tool_call(
                "create_repair_plan",
                _evidence_plan_args(),
                "plan_without_observation",
            ),
            AIMessage(content="Evidence is insufficient."),
        ]
    )
    result = await run_agent(
        "Fix Main",
        info.repository_path,
        model=model,
        workspace_id=info.workspace_id,
        workspace_manager=manager,
        evidence_commitment=True,
    )
    assert result.repair_plan is None
    assert result.commitment_telemetry is not None
    assert result.commitment_telemetry.validation_errors
    feedback = "\n".join(
        message.content
        for message in model.history[1]
        if isinstance(message, ToolMessage)
    )
    assert "commitment required" in feedback
    assert "not been observed" in feedback


async def test_sufficient_evidence_prompt_and_plan_execution_tracking(
    manager,
    origin_repo,
):
    info, _ = _workspace(manager, origin_repo)
    model = FakeChatModel(
        script=[
            tool_call("run_tests", {"tool": "maven"}, "initial_test"),
            tool_call("read_file", {"path": "Main.java"}, "read_main"),
            tool_call(
                "create_repair_plan",
                _evidence_plan_args(),
                "evidence_plan",
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
        evidence_commitment=True,
        test_runner=_sequence_runner([_test_result(False), _test_result(True)]),
    )
    assert result.modified_files == ["Main.java"]
    assert result.repair_plan is not None
    telemetry = result.commitment_telemetry
    assert telemetry is not None
    assert telemetry.mode == "evidence_commitment"
    assert telemetry.evidence_sufficient_step is not None
    assert telemetry.first_hypothesis_step == telemetry.first_plan_step
    assert telemetry.plan_execution_success is True
    statuses = [item.status for item in telemetry.plan_status_history]
    assert PlanExecutionStatus.COMMITTED in statuses
    assert PlanExecutionStatus.PATCH_APPLIED in statuses
    assert PlanExecutionStatus.VERIFYING in statuses
    assert PlanExecutionStatus.COMPLETED in statuses
    assert any(
        isinstance(message, HumanMessage)
        and "[evidence commitment]" in message.content
        for history in model.history
        for message in history
    )
    assert result.budget_telemetry.policy == "adaptive_20_30"


async def test_patch_must_match_plan(manager, origin_repo):
    info, repo = _workspace(manager, origin_repo)
    model = FakeChatModel(
        script=[
            tool_call("run_tests", {"tool": "maven"}, "initial_test"),
            tool_call("read_file", {"path": "Main.java"}, "read_main"),
            tool_call(
                "create_repair_plan",
                _evidence_plan_args(),
                "evidence_plan",
            ),
            _patch(0, path="OrderService.java", call_id="wrong_target"),
            AIMessage(content="Patch target does not match the plan."),
        ]
    )
    result = await run_agent(
        "Fix Main",
        info.repository_path,
        model=model,
        workspace_id=info.workspace_id,
        workspace_manager=manager,
        evidence_commitment=True,
        test_runner=_sequence_runner([_test_result(False)]),
    )
    assert result.modified_files == []
    assert "// step0" in (repo / "Main.java").read_text(encoding="utf-8")
    feedback = "\n".join(
        message.content
        for history in model.history
        for message in history
        if isinstance(message, ToolMessage)
    )
    assert "目标与当前 Repair Plan 不一致" in feedback


async def test_committed_plan_blocks_broad_exploration(manager, origin_repo):
    info, _ = _workspace(manager, origin_repo)
    model = FakeChatModel(
        script=[
            tool_call("run_tests", {"tool": "maven"}, "initial_test"),
            tool_call("read_file", {"path": "Main.java"}, "read_main"),
            tool_call(
                "create_repair_plan",
                _evidence_plan_args(),
                "evidence_plan",
            ),
            tool_call(
                "search_code",
                {"query": "Main", "file_pattern": "*.java"},
                "broad_search",
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
        evidence_commitment=True,
        test_runner=_sequence_runner([_test_result(False), _test_result(True)]),
    )
    blocked = [
        event
        for event in result.tool_events
        if event.call_id == "broad_search"
    ][0]
    assert blocked.success is False
    assert blocked.invoked is False
    assert result.modified_files == ["Main.java"]


async def test_unresolved_question_allows_directed_exploration(manager, origin_repo):
    info, _ = _workspace(manager, origin_repo)
    model = FakeChatModel(
        script=[
            tool_call("run_tests", {"tool": "maven"}, "initial_test"),
            tool_call("read_file", {"path": "Main.java"}, "read_main"),
            tool_call(
                "create_repair_plan",
                _evidence_plan_args(
                    unresolved=["Check whether Main has another caller"]
                ),
                "evidence_plan",
            ),
            tool_call(
                "search_code",
                {"query": "Main", "file_pattern": "*.java"},
                "targeted_search",
            ),
            AIMessage(content="Need to resolve the named caller question."),
        ]
    )
    result = await run_agent(
        "Fix Main",
        info.repository_path,
        model=model,
        workspace_id=info.workspace_id,
        workspace_manager=manager,
        evidence_commitment=True,
        test_runner=_sequence_runner([_test_result(False)]),
    )
    search = next(
        event
        for event in result.tool_events
        if event.call_id == "targeted_search"
    )
    assert search.success is True


async def test_repair_failure_requires_revised_hypothesis(manager, origin_repo):
    info, _ = _workspace(manager, origin_repo)
    model = FakeChatModel(
        script=[
            tool_call("run_tests", {"tool": "maven"}, "initial_test"),
            tool_call("read_file", {"path": "Main.java"}, "read_main"),
            tool_call(
                "create_repair_plan",
                _evidence_plan_args(),
                "plan_1",
            ),
            _patch(0, call_id="patch_1"),
            tool_call(
                "create_repair_plan",
                _evidence_plan_args(
                    revision_reason="First hypothesis was incomplete"
                ),
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
        evidence_commitment=True,
        test_runner=_sequence_runner(
            [
                _test_result(False),
                _test_result(False),
                _test_result(True),
            ]
        ),
    )
    telemetry = result.commitment_telemetry
    assert telemetry is not None
    assert telemetry.plan_revision_count == 2
    assert telemetry.hypothesis_revision_count == 1
    assert telemetry.plan_execution_success is True
    statuses = [item.status for item in telemetry.plan_status_history]
    assert PlanExecutionStatus.REVISED in statuses
    assert statuses.count(PlanExecutionStatus.COMMITTED) == 2
    assert PlanExecutionStatus.REJECTED in statuses


def test_evidence_models_serialize_without_ground_truth_fields():
    request = CreateEvidenceRepairPlanRequest.model_validate(
        _evidence_plan_args()
    )
    restored = CreateEvidenceRepairPlanRequest.model_validate_json(
        request.model_dump_json()
    )
    assert restored.hypothesis.root_cause_hypothesis
    forbidden = {
        "ground_truth",
        "hidden_tests",
        "relevant_files",
        "developer_patch",
    }
    assert forbidden.isdisjoint(
        CreateEvidenceRepairPlanRequest.model_fields
    )
    parameters = inspect.signature(
        EvidenceCommitmentController.readiness
    ).parameters
    assert forbidden.isdisjoint(parameters)


def test_control_and_treatment_plan_schemas_are_isolated(manager, origin_repo):
    info, _ = _workspace(manager, origin_repo)
    control = create_repair_plan_tools(
        info.workspace_id,
        info.repository_path,
        manager=manager,
        require_evidence=False,
    )[0]
    treatment = create_repair_plan_tools(
        info.workspace_id,
        info.repository_path,
        manager=manager,
        require_evidence=True,
    )[0]
    control_args = _evidence_plan_args()
    control_args.pop("hypothesis")
    assert control.invoke(control_args)
    try:
        treatment.invoke(control_args)
    except Exception as exc:
        assert "hypothesis" in str(exc).lower()
    else:
        raise AssertionError("Treatment accepted a plan without evidence")
