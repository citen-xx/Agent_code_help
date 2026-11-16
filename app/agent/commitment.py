"""Runtime evidence readiness, validation, and commitment telemetry."""

from pathlib import PurePosixPath

from pydantic import BaseModel, Field

from app.agent.telemetry import ToolEvent
from app.repair.models import (
    EvidenceRepairPlan,
    EvidenceType,
    PlanExecutionStatus,
)


class EvidenceReadiness(BaseModel):
    has_failure_observation: bool
    source_reads: list[str] = Field(default_factory=list)
    first_failure_observation_step: int | None = Field(default=None, ge=1)
    first_source_read_step: int | None = Field(default=None, ge=1)
    sufficient: bool
    reason: str


class PlanStatusEvent(BaseModel):
    step: int = Field(ge=1)
    status: PlanExecutionStatus
    reason: str


class CommitmentTelemetry(BaseModel):
    mode: str
    first_failure_observation_step: int | None = Field(default=None, ge=1)
    first_source_read_step: int | None = Field(default=None, ge=1)
    evidence_sufficient_step: int | None = Field(default=None, ge=1)
    first_hypothesis_step: int | None = Field(default=None, ge=1)
    first_plan_step: int | None = Field(default=None, ge=1)
    first_patch_step: int | None = Field(default=None, ge=1)
    first_test_step: int | None = Field(default=None, ge=1)
    plan_to_patch_gap: int | None = None
    hypothesis_to_patch_gap: int | None = None
    plan_revision_count: int = Field(default=0, ge=0)
    hypothesis_revision_count: int = Field(default=0, ge=0)
    plan_execution_success: bool = False
    patch_attempts: int = Field(default=0, ge=0)
    patch_successes: int = Field(default=0, ge=0)
    post_plan_exploration_calls: int = Field(default=0, ge=0)
    no_patch_failure: bool = False
    plan_status_history: list[PlanStatusEvent] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    evidence_references: list[dict] = Field(default_factory=list)


EXPLORATION_TOOLS = {
    "read_file",
    "search_code",
    "list_files",
    "find_symbol",
    "find_references",
    "find_callers",
    "find_callees",
    "analyze_impact",
    "list_symbols",
    "git_show",
    "git_log",
    "git_status",
    "git_diff",
}
BROAD_EXPLORATION_TOOLS = EXPLORATION_TOOLS - {"read_file"}
CI_TOOLS = {
    "list_symbols",
    "find_symbol",
    "find_references",
    "find_callers",
    "find_callees",
    "analyze_impact",
}


def _normalize_path(path: str) -> str:
    return str(PurePosixPath(path.replace("\\", "/").lstrip("./")))


class EvidenceCommitmentController:
    """Deterministic validation over Agent-observed runtime telemetry."""

    @staticmethod
    def readiness(
        tool_events: list[ToolEvent],
        *,
        has_seen_test_failure: bool,
        current_step: int,
    ) -> EvidenceReadiness:
        tests = [
            event
            for event in tool_events
            if event.tool_name == "run_tests" and event.invoked
        ]
        source_reads = [
            event
            for event in tool_events
            if event.tool_name == "read_file"
            and event.success
            and event.target_file
            and event.target_file.endswith(".java")
            and not (
                "/src/test/" in f"/{_normalize_path(event.target_file)}"
                or _normalize_path(event.target_file).endswith("Test.java")
            )
        ]
        first_failure = tests[0].step if tests and has_seen_test_failure else None
        first_read = source_reads[0].step if source_reads else None
        sufficient = bool(
            has_seen_test_failure
            and first_failure is not None
            and source_reads
        )
        return EvidenceReadiness(
            has_failure_observation=bool(
                has_seen_test_failure and first_failure is not None
            ),
            source_reads=list(
                dict.fromkeys(
                    _normalize_path(event.target_file)
                    for event in source_reads
                    if event.target_file
                )
            ),
            first_failure_observation_step=first_failure,
            first_source_read_step=first_read,
            sufficient=sufficient,
            reason=(
                "test_failure_and_source_observed"
                if sufficient
                else "missing_test_failure"
                if not has_seen_test_failure
                else "missing_source_read"
            ),
        )

    @staticmethod
    def evidence_prompt(readiness: EvidenceReadiness) -> str:
        files = ", ".join(readiness.source_reads[:5])
        return (
            "[evidence commitment] Runtime evidence is sufficient: a failing "
            f"test and source implementation have been observed ({files}). "
            "Form a RepairHypothesis using only these observations. Call "
            "create_repair_plan with structured evidence before further broad "
            "exploration. If a critical unresolved question remains, name it "
            "explicitly; otherwise proceed to the first actionable Patch."
        )

    @staticmethod
    def validate_evidence_plan(
        plan: EvidenceRepairPlan,
        tool_events: list[ToolEvent],
        *,
        has_seen_test_failure: bool,
    ) -> list[str]:
        errors: list[str] = []
        observed_reads = [
            event
            for event in tool_events
            if event.tool_name == "read_file"
            and event.success
            and event.target_file
        ]
        observed_ci = [
            event
            for event in tool_events
            if event.tool_name in CI_TOOLS and event.success
        ]
        observed_git = [
            event
            for event in tool_events
            if event.tool_name in {
                "git_show",
                "git_log",
                "git_status",
                "git_diff",
            }
            and event.success
        ]
        for index, evidence in enumerate(plan.hypothesis.evidence):
            prefix = f"evidence[{index}]"
            if evidence.evidence_type is EvidenceType.TEST_FAILURE:
                if not has_seen_test_failure:
                    errors.append(
                        f"{prefix}: test failure has not been observed"
                    )
            elif evidence.evidence_type is EvidenceType.SOURCE_READ:
                if not evidence.path:
                    errors.append(f"{prefix}: source evidence requires path")
                    continue
                matches = [
                    event
                    for event in observed_reads
                    if _normalize_path(event.target_file or "")
                    == _normalize_path(evidence.path)
                ]
                if not matches:
                    errors.append(
                        f"{prefix}: source file was not read: {evidence.path}"
                    )
                elif (
                    evidence.start_line is not None
                    and evidence.end_line is not None
                    and not any(
                        event.read_start_line is None
                        or event.read_end_line is None
                        or (
                            evidence.start_line <= event.read_end_line
                            and evidence.end_line >= event.read_start_line
                        )
                        for event in matches
                    )
                ):
                    errors.append(
                        f"{prefix}: line range was not observed"
                    )
            elif evidence.evidence_type is EvidenceType.SYMBOL_QUERY:
                if not any(
                    event.query
                    and (
                        not evidence.symbol
                        or event.query == evidence.symbol
                    )
                    for event in observed_ci
                ):
                    errors.append(
                        f"{prefix}: symbol query was not observed"
                    )
            elif evidence.evidence_type is EvidenceType.CALL_GRAPH:
                if not observed_ci:
                    errors.append(f"{prefix}: call graph was not observed")
            elif evidence.evidence_type is EvidenceType.GIT_CONTEXT:
                if not observed_git:
                    errors.append(f"{prefix}: git context was not observed")

        read_paths = {
            _normalize_path(event.target_file or "")
            for event in observed_reads
        }
        source_evidence_paths = {
            _normalize_path(evidence.path)
            for evidence in plan.hypothesis.evidence
            if evidence.evidence_type is EvidenceType.SOURCE_READ
            and evidence.path
        }
        for change in plan.planned_changes:
            path = _normalize_path(change.file_path)
            if path not in read_paths or path not in source_evidence_paths:
                errors.append(
                    f"planned file lacks observed source evidence: {path}"
                )
        for path in plan.hypothesis.suspected_files:
            if _normalize_path(path) not in read_paths:
                errors.append(f"suspected file was not read: {path}")
        if not source_evidence_paths:
            errors.append("hypothesis has no source-read evidence")
        return errors

    @staticmethod
    def patch_matches_plan(args: dict, plan) -> bool:
        planned = {
            _normalize_path(change.file_path)
            for change in plan.planned_changes
        }
        requested = {
            _normalize_path(str(entry.get("path")))
            for entry in args.get("files", [])
            if isinstance(entry, dict) and entry.get("path")
        }
        return bool(requested) and requested.issubset(planned)

    @staticmethod
    def exploration_is_directed(
        tool_name: str,
        args: dict,
        plan: EvidenceRepairPlan,
    ) -> bool:
        if tool_name in BROAD_EXPLORATION_TOOLS:
            return False
        if tool_name != "read_file":
            return True
        path = args.get("path")
        if not path:
            return False
        planned = {
            _normalize_path(change.file_path)
            for change in plan.planned_changes
        }
        suspected = {
            _normalize_path(value)
            for value in plan.hypothesis.suspected_files
        }
        return _normalize_path(str(path)) in planned | suspected

    @staticmethod
    def build_telemetry(
        *,
        evidence_commitment: bool,
        tool_events: list[ToolEvent],
        plan_revisions: list,
        plan_status_history: list[PlanStatusEvent],
        evidence_sufficient_step: int | None,
        final_test_passed: bool,
        has_seen_test_failure: bool,
        validation_errors: list[str],
    ) -> CommitmentTelemetry:
        tests = [
            event
            for event in tool_events
            if event.tool_name == "run_tests" and event.invoked
        ]
        reads = [
            event
            for event in tool_events
            if event.tool_name == "read_file" and event.success
        ]
        plans = [
            event
            for event in tool_events
            if event.tool_name == "create_repair_plan" and event.success
        ]
        patches = [
            event
            for event in tool_events
            if event.tool_name == "apply_patch"
        ]
        successful_patches = [
            event for event in patches if event.success
        ]
        evidence_plans = [
            revision.plan
            for revision in plan_revisions
            if isinstance(revision.plan, EvidenceRepairPlan)
        ]
        first_failure = (
            tests[0].step
            if tests and has_seen_test_failure
            else None
        )
        first_read = reads[0].step if reads else None
        first_plan = plans[0].step if plans else None
        first_patch = patches[0].step if patches else None
        first_test = tests[0].step if tests else None
        first_hypothesis = (
            first_plan if evidence_plans else None
        )
        post_plan_exploration = 0
        if first_plan is not None:
            end = first_patch or 10**9
            post_plan_exploration = sum(
                first_plan < event.step < end
                and event.tool_name in EXPLORATION_TOOLS
                for event in tool_events
            )
        references = []
        if evidence_plans:
            references = [
                evidence.model_dump(mode="json")
                for evidence in evidence_plans[-1].hypothesis.evidence
            ]
        return CommitmentTelemetry(
            mode=(
                "evidence_commitment"
                if evidence_commitment
                else "planner_control"
            ),
            first_failure_observation_step=first_failure,
            first_source_read_step=first_read,
            evidence_sufficient_step=evidence_sufficient_step,
            first_hypothesis_step=first_hypothesis,
            first_plan_step=first_plan,
            first_patch_step=first_patch,
            first_test_step=first_test,
            plan_to_patch_gap=(
                first_patch - first_plan
                if first_plan is not None and first_patch is not None
                else None
            ),
            hypothesis_to_patch_gap=(
                first_patch - first_hypothesis
                if first_hypothesis is not None
                and first_patch is not None
                else None
            ),
            plan_revision_count=len(plan_revisions),
            hypothesis_revision_count=max(len(evidence_plans) - 1, 0),
            plan_execution_success=bool(
                final_test_passed and plan_revisions
            ),
            patch_attempts=len(patches),
            patch_successes=len(successful_patches),
            post_plan_exploration_calls=post_plan_exploration,
            no_patch_failure=not successful_patches,
            plan_status_history=plan_status_history,
            validation_errors=validation_errors,
            evidence_references=references,
        )
