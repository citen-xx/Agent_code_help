"""HTTP API tests for /workspaces — local git repos only, no network."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.workspace.manager import WorkspaceManager


@pytest.fixture
def api_client(workspace_root, monkeypatch):
    monkeypatch.setattr(
        "app.workspace.manager.workspace_manager",
        WorkspaceManager(workspace_root, allow_file_urls=True),
    )
    with TestClient(create_app()) as client:
        yield client


def test_create_workspace_endpoint(api_client, origin_repo):
    response = api_client.post("/workspaces", json={"repository_url": origin_repo.as_uri()})
    assert response.status_code == 201
    body = response.json()
    assert re.fullmatch(r"[a-f0-9]{12}", body["workspace_id"])
    assert Path(body["repository_path"]).is_dir()
    assert body["branch"] == "main"
    assert body["commit"]
    assert body["status"] == "ready"


def test_create_workspace_with_branch(api_client, origin_repo):
    response = api_client.post(
        "/workspaces", json={"repository_url": origin_repo.as_uri(), "branch": "develop"}
    )
    assert response.status_code == 201
    assert response.json()["branch"] == "develop"


def test_create_workspace_invalid_url(api_client):
    response = api_client.post("/workspaces", json={"repository_url": "ssh://git@github.com/x/y.git"})
    assert response.status_code == 400


def test_create_workspace_clone_failure(api_client):
    response = api_client.post("/workspaces", json={"repository_url": "https://127.0.0.1:9/nope.git"})
    assert response.status_code == 502


def test_get_workspace_endpoint(api_client, origin_repo):
    created = api_client.post("/workspaces", json={"repository_url": origin_repo.as_uri()}).json()
    response = api_client.get(f"/workspaces/{created['workspace_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["workspace_id"] == created["workspace_id"]
    assert body["repository_path"] == created["repository_path"]
    assert body["branch"] == "main"
    assert body["status"] == "ready"


def test_list_workspaces_endpoint(api_client, origin_repo):
    first = api_client.post(
        "/workspaces", json={"repository_url": origin_repo.as_uri()}
    ).json()
    second = api_client.post(
        "/workspaces",
        json={"repository_url": origin_repo.as_uri(), "branch": "develop"},
    ).json()

    response = api_client.get("/workspaces")

    assert response.status_code == 200
    body = response.json()
    assert {item["workspace_id"] for item in body} == {
        first["workspace_id"],
        second["workspace_id"],
    }
    assert all(item["status"] == "ready" for item in body)
    assert {item["branch"] for item in body} == {"main", "develop"}


def test_list_workspaces_empty(api_client):
    response = api_client.get("/workspaces")
    assert response.status_code == 200
    assert response.json() == []


def test_get_workspace_path_traversal(api_client):
    assert api_client.get("/workspaces/abc").status_code == 404
    assert api_client.get("/workspaces/..%2F..").status_code == 404
    assert api_client.get("/workspaces/%2E%2E").status_code == 404
    assert api_client.get("/workspaces/abcdef012345").status_code == 404  # valid format, unknown id


def test_delete_workspace_endpoint(api_client, origin_repo):
    created = api_client.post("/workspaces", json={"repository_url": origin_repo.as_uri()}).json()
    ws_id = created["workspace_id"]
    repo_path = Path(created["repository_path"])
    assert repo_path.is_dir()
    response = api_client.delete(f"/workspaces/{ws_id}")
    assert response.status_code == 204
    assert not repo_path.parent.exists()
    assert api_client.get(f"/workspaces/{ws_id}").status_code == 404
    assert api_client.delete(f"/workspaces/{ws_id}").status_code == 404
    assert api_client.get("/workspaces").json() == []
