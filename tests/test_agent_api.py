"""HTTP API tests for POST /agent/analyze — LLM is faked via monkeypatch."""

from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

from app.llm.client import LLMConfigError
from app.main import create_app
from app.workspace.manager import WorkspaceManager
from tests.build_helpers import patch_popen
from tests.fakes import FakeChatModel, tool_call


# ---- legacy repository_path mode (phase 2 behavior, kept intact) ----

def test_analyze_basic_flow(tmp_path, monkeypatch):
    (tmp_path / "Order.java").write_text("public class Order {}\n", encoding="utf-8")
    model = FakeChatModel(script=[
        tool_call("list_files", {"path": "."}, "call_1"),
        AIMessage(content="完成：创建订单的代码在 Order.java。"),
    ])
    monkeypatch.setattr("app.api.agent.build_chat_model", lambda: model)

    with TestClient(create_app()) as client:
        response = client.post(
            "/agent/analyze",
            json={"task": "找到创建订单的代码", "repository_path": str(tmp_path)},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "完成：创建订单的代码在 Order.java。"
    assert body["steps"] == 2


def test_analyze_rejects_missing_repository(monkeypatch):
    monkeypatch.setattr("app.api.agent.build_chat_model", lambda: FakeChatModel())
    with TestClient(create_app()) as client:
        response = client.post(
            "/agent/analyze",
            json={"task": "分析", "repository_path": "Z:/no/such/repo"},
        )
    assert response.status_code == 400


def test_analyze_returns_503_when_llm_not_configured(tmp_path, monkeypatch):
    def _unconfigured():
        raise LLMConfigError("LLM_API_KEY 未配置")

    monkeypatch.setattr("app.api.agent.build_chat_model", _unconfigured)
    with TestClient(create_app()) as client:
        response = client.post(
            "/agent/analyze",
            json={"task": "分析", "repository_path": str(tmp_path)},
        )
    assert response.status_code == 503


# ---- workspace_id mode (phase 3) ----

def test_analyze_requires_exactly_one_target():
    with TestClient(create_app()) as client:
        response = client.post("/agent/analyze", json={"task": "分析"})
    assert response.status_code == 422
    with TestClient(create_app()) as client:
        response = client.post(
            "/agent/analyze",
            json={"task": "分析", "repository_path": "C:/", "workspace_id": "abcdef012345"},
        )
    assert response.status_code == 422


def test_analyze_with_workspace_id(workspace_root, origin_repo, monkeypatch):
    manager = WorkspaceManager(workspace_root, allow_file_urls=True)
    monkeypatch.setattr("app.workspace.manager.workspace_manager", manager)
    info = manager.create(origin_repo.as_uri())

    model = FakeChatModel(script=[
        AIMessage(content="", tool_calls=[
            {"name": "git_log", "args": {"limit": 2}, "id": "call_1"},
            {"name": "read_file", "args": {"path": "Main.java"}, "id": "call_2"},
        ]),
        AIMessage(content="分析完成：已确认提交历史并读取 Main.java。"),
    ])
    monkeypatch.setattr("app.api.agent.build_chat_model", lambda: model)

    with TestClient(create_app()) as client:
        response = client.post(
            "/agent/analyze",
            json={"task": "分析仓库", "workspace_id": info.workspace_id},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "分析完成：已确认提交历史并读取 Main.java。"
    assert body["steps"] == 2
    tool_contents = "\n".join(m.content for m in model.history[1] if isinstance(m, ToolMessage))
    assert "add order service" in tool_contents  # git_log ran on the workspace repo
    assert "public class Main" in tool_contents  # read_file ran on the workspace repo


def test_analyze_can_run_tests_and_receive_failure_feedback(
    workspace_root, origin_repo, monkeypatch
):
    manager = WorkspaceManager(workspace_root, allow_file_urls=True)
    monkeypatch.setattr("app.workspace.manager.workspace_manager", manager)
    info = manager.create(origin_repo.as_uri())
    repository = Path(info.repository_path)
    (repository / "pom.xml").write_text("<project/>", encoding="utf-8")
    captured = patch_popen(
        monkeypatch,
        stdout="Tests run: 1, Failures: 1\nOrderServiceTest.shouldCreateOrder",
        stderr="expected true but was false",
        returncode=1,
    )

    model = FakeChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "detect_build_tools",
                        "args": {},
                        "id": "call_detect",
                    },
                    {
                        "name": "run_tests",
                        "args": {"tool": "maven"},
                        "id": "call_test",
                    },
                ],
            ),
            AIMessage(
                content="测试失败发生在 OrderServiceTest.shouldCreateOrder。"
            ),
        ]
    )
    monkeypatch.setattr("app.api.agent.build_chat_model", lambda: model)

    with TestClient(create_app()) as client:
        response = client.post(
            "/agent/analyze",
            json={
                "task": "运行测试并分析失败原因",
                "workspace_id": info.workspace_id,
            },
        )

    assert response.status_code == 200
    tool_contents = "\n".join(
        message.content
        for message in model.history[1]
        if isinstance(message, ToolMessage)
    )
    assert '"tool": "maven"' in tool_contents
    assert '"exit_code": 1' in tool_contents
    assert "OrderServiceTest.shouldCreateOrder" in tool_contents
    assert "expected true but was false" in tool_contents
    mount = captured["command"][captured["command"].index("--mount") + 1]
    assert f"source={repository}" in mount
    assert "target=/workspace" in mount
    assert captured["kwargs"]["shell"] is False


def test_analyze_unknown_workspace_returns_404(workspace_root, monkeypatch):
    monkeypatch.setattr(
        "app.workspace.manager.workspace_manager",
        WorkspaceManager(workspace_root, allow_file_urls=True),
    )
    monkeypatch.setattr("app.api.agent.build_chat_model", lambda: FakeChatModel())
    with TestClient(create_app()) as client:
        response = client.post("/agent/analyze", json={"task": "分析", "workspace_id": "abcdef012345"})
    assert response.status_code == 404


def test_analyze_rejects_not_ready_workspace(
    workspace_root, origin_repo, monkeypatch
):
    manager = WorkspaceManager(workspace_root, allow_file_urls=True)
    monkeypatch.setattr("app.workspace.manager.workspace_manager", manager)
    monkeypatch.setattr("app.api.agent.build_chat_model", lambda: FakeChatModel())
    info = manager.create(origin_repo.as_uri())
    repository = Path(info.repository_path)
    repository.rename(repository.with_name("repository.broken"))

    with TestClient(create_app()) as client:
        response = client.post(
            "/agent/analyze",
            json={"task": "分析", "workspace_id": info.workspace_id},
        )

    assert response.status_code == 409
    assert "error" in response.json()["detail"]


def test_analyze_workspace_id_path_traversal_blocked(workspace_root, monkeypatch):
    monkeypatch.setattr(
        "app.workspace.manager.workspace_manager",
        WorkspaceManager(workspace_root, allow_file_urls=True),
    )
    monkeypatch.setattr("app.api.agent.build_chat_model", lambda: FakeChatModel())
    with TestClient(create_app()) as client:
        response = client.post("/agent/analyze", json={"task": "分析", "workspace_id": "../../etc"})
    assert response.status_code == 404
