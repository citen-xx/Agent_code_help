"""Agent HTTP endpoints."""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.agent.graph import run_agent
from app.llm.client import LLMConfigError, build_chat_model
from app.schemas.agent import AnalyzeRequest, AnalyzeResponse
from app.workspace import manager as workspace_manager_module
from app.workspace.manager import WorkspaceNotFoundError
from app.workspace.models import WorkspaceStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    """Run the code agent on a local repository.

    Target is either a workspace_id (recommended) or a legacy
    repository_path. When a workspace_id is given, the repository path is
    resolved by the workspace manager; atomic patching, Git and build tests
    are then bound to that workspace. Legacy mode remains inspection-only.
    """
    if request.workspace_id:
        try:
            info = workspace_manager_module.workspace_manager.get(request.workspace_id)
        except WorkspaceNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if info.status != WorkspaceStatus.READY:
            raise HTTPException(
                status_code=409,
                detail=f"workspace 尚未就绪（status={info.status.value}）: {request.workspace_id}",
            )
        repository_path = info.repository_path
    else:
        repository_path = request.repository_path or ""
        if not Path(repository_path).is_dir():
            raise HTTPException(status_code=400, detail=f"repository_path 不是有效目录: {repository_path}")

    try:
        model = build_chat_model()
    except LLMConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return await run_agent(
        request.task,
        repository_path,
        model=model,
        workspace_id=request.workspace_id,
        workspace_manager=workspace_manager_module.workspace_manager,
        profile=request.profile,
        evidence_commitment=request.evidence_commitment,
    )
