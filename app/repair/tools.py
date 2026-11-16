"""Workspace-bound structured repair-plan tool."""

from pathlib import Path
from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool, StructuredTool

from app.repair.models import (
    CreateQualityEvidenceRepairPlanRequest,
    CreateEvidenceRepairPlanRequest,
    CreateRepairPlanRequest,
)
from app.tools.repo import ToolError

if TYPE_CHECKING:
    from app.workspace.manager import WorkspaceManager


def create_repair_plan_tools(
    workspace_id: str,
    repository_path: str | Path,
    manager: "WorkspaceManager | None" = None,
    *,
    require_evidence: bool = False,
    hypothesis_quality: bool = False,
) -> list[BaseTool]:
    if manager is not None:
        repo = manager.assert_repository_binding(workspace_id, repository_path)
    else:
        repo = Path(repository_path).expanduser().resolve()
        if repo.name != "repository" or repo.parent.name != workspace_id:
            raise ToolError(
                f"Repair Planner 只能绑定 workspace {workspace_id} 的 repository 目录"
            )
    if not repo.is_dir():
        raise ToolError(f"workspace repository 不存在: {repo}")

    def _validate_relative_file(raw_path: str) -> None:
        path = Path(raw_path)
        if path.is_absolute() or path.drive:
            raise ToolError(f"计划文件必须是 repository 相对路径: {raw_path!r}")
        if any(part in {"", ".", ".."} for part in path.parts):
            raise ToolError(f"计划文件路径非法: {raw_path!r}")
        candidate = (repo / path).resolve()
        try:
            candidate.relative_to(repo)
        except ValueError:
            raise ToolError(f"计划文件越出 repository: {raw_path!r}") from None
        if not candidate.is_file():
            raise ToolError(f"计划目标文件不存在: {raw_path!r}")

    def _create_plan(**kwargs) -> str:
        request_class = (
            CreateQualityEvidenceRepairPlanRequest
            if hypothesis_quality
            else CreateEvidenceRepairPlanRequest
            if require_evidence
            else CreateRepairPlanRequest
        )
        request = request_class.model_validate(kwargs)
        if not request.validation_plan.run_maven_test:
            raise ToolError("Repair Plan 必须要求运行 Maven test")
        if not request.validation_plan.focus:
            raise ToolError("Repair Plan 必须说明需要重点关注的测试或调用方")
        for path in request.impacted_files:
            _validate_relative_file(path)
        for change in request.planned_changes:
            _validate_relative_file(change.file_path)
        if isinstance(
            request,
            (
                CreateEvidenceRepairPlanRequest,
                CreateQualityEvidenceRepairPlanRequest,
            ),
        ):
            for evidence in request.hypothesis.evidence:
                if evidence.path:
                    _validate_relative_file(evidence.path)
                if (
                    evidence.start_line is not None
                    and evidence.end_line is not None
                    and evidence.end_line < evidence.start_line
                ):
                    raise ToolError("Evidence line range 非法")
            for path in request.hypothesis.suspected_files:
                _validate_relative_file(path)
            if isinstance(request, CreateQualityEvidenceRepairPlanRequest):
                for dependency in request.hypothesis.unresolved_dependencies:
                    for path in dependency.candidate_files:
                        _validate_relative_file(path)
                for alternative in request.hypothesis.alternative_explanations:
                    for path in alternative.candidate_files:
                        _validate_relative_file(path)
        return request.model_dump_json(indent=2)

    description = (
        "Create or revise the structured repair plan before apply_patch. "
        "This is planning-only and never edits or executes code. Include "
        "target symbols, impacted files/tests, planned changes, risks, and "
        "a validation plan that requires Maven test. When revising after a "
        "failed test, provide revision_reason."
    )
    if hypothesis_quality:
        description += (
            " Phase 8 quality mode requires causal mechanism and testable "
            "prediction fields, plus explicit unresolved dependencies and "
            "alternative explanations when runtime evidence is ambiguous."
        )
    elif require_evidence:
        description += (
            " Evidence-driven mode requires a RepairHypothesis whose evidence "
            "references only already-observed runtime information."
        )
    return [
        StructuredTool.from_function(
            name="create_repair_plan",
            description=description,
            func=_create_plan,
            args_schema=(
                CreateQualityEvidenceRepairPlanRequest
                if hypothesis_quality
                else CreateEvidenceRepairPlanRequest
                if require_evidence
                else CreateRepairPlanRequest
            ),
        )
    ]
