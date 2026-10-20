"""Workspace HTTP endpoints."""

import logging

from fastapi import APIRouter, HTTPException

from app.schemas.workspace import WorkspaceCreateRequest
from app.workspace import git, manager as manager_module
from app.workspace.manager import WorkspaceNotFoundError, WorkspaceValidationError
from app.workspace.models import WorkspaceInfo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.post("", response_model=WorkspaceInfo, status_code=201)
def create_workspace(request: WorkspaceCreateRequest) -> WorkspaceInfo:
    """Clone a Git repository into a new isolated workspace."""
    try:
        return manager_module.workspace_manager.create(
            request.repository_url, request.branch, request.commit
        )
    except WorkspaceValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except git.GitError as exc:
        raise HTTPException(status_code=502, detail=f"Git 操作失败：{exc}") from exc


@router.get("", response_model=list[WorkspaceInfo])
def list_workspaces() -> list[WorkspaceInfo]:
    """List all isolated workspaces."""
    return manager_module.workspace_manager.list()


@router.get("/{workspace_id}", response_model=WorkspaceInfo)
def get_workspace(workspace_id: str) -> WorkspaceInfo:
    """Return the basic status of a workspace."""
    try:
        return manager_module.workspace_manager.get(workspace_id)
    except WorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{workspace_id}", status_code=204)
def delete_workspace(workspace_id: str) -> None:
    """Delete a workspace (explicit cleanup; only its own directory)."""
    try:
        manager_module.workspace_manager.delete(workspace_id)
    except WorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
