"""End-to-end Agent patch + Maven feedback loop tests with a fake LLM."""

from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

from app.main import create_app
from app.workspace.manager import WorkspaceManager
from tests.build_helpers import patch_popen, patch_popen_sequence
from tests.fakes import FakeChatModel


def _setup(workspace_root, origin_repo, monkeypatch):
    manager = WorkspaceManager(workspace_root, allow_file_urls=True)
    monkeypatch.setattr("app.workspace.manager.workspace_manager", manager)
    info = manager.create(origin_repo.as_uri())
    repo = Path(info.repository_path)
    (repo / "pom.xml").write_text("<project/>", encoding="utf-8")
    (repo / "Main.java").write_text(
        "public class Main { // step0 }\n", encoding="utf-8"
    )
    return manager, info, repo


def _patch_change(step: int) -> dict:
    return {
        "files": [
            {
                "path": "Main.java",
                "replacements": [
                    {
                        "old_text": f"// step{step}",
                        "new_text": f"// step{step + 1}",
                    }
                ],
            }
        ]
    }


def _patch_only(step: int) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "apply_patch",
                "args": _patch_change(step),
                "id": f"auto_patch_{step}",
            }
        ],
    )


def _tool_calls_named(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id}],
    )


def _plan_call(reason: str | None = None, call_id: str = "plan") -> AIMessage:
    return _tool_calls_named(
        "create_repair_plan",
        {
            "problem_summary": "修复 Main 中的测试失败",
            "target_symbols": ["Main"],
            "impacted_files": ["Main.java"],
            "impacted_tests": [],
            "planned_changes": [
                {
                    "file_path": "Main.java",
                    "target_symbol": "Main",
                    "change_description": "更新 Main 实现",
                    "reason": "使 Maven 测试通过",
                }
            ],
            "risk_notes": ["改动范围限于 Main.java"],
            "validation_plan": {
                "run_maven_test": True,
                "focus": ["Main 相关测试与调用方"],
                "notes": [],
            },
            "revision_reason": reason,
        },
        call_id,
    )


def test_agent_patch_then_passing_test_returns_summary_diff(
    workspace_root, origin_repo, monkeypatch
):
    _, info, repo = _setup(workspace_root, origin_repo, monkeypatch)
    captured = patch_popen_sequence(
        monkeypatch,
        [{"stdout": "Tests run: 1, Failures: 0", "returncode": 0}],
    )
    model = FakeChatModel(
        script=[
            _plan_call(),
            _patch_only(0),
            AIMessage(content="已修复并通过测试。"),
        ]
    )
    monkeypatch.setattr("app.api.agent.build_chat_model", lambda: model)

    with TestClient(create_app()) as client:
        response = client.post(
            "/agent/analyze",
            json={"task": "修复代码并运行 Maven 测试", "workspace_id": info.workspace_id},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["modified_files"] == ["Main.java"]
    assert body["repair_rounds"] == 0
    assert body["test_result"]["success"] is True
    assert body["test_result"]["exit_code"] == 0
    assert body["repair_plan"]["problem_summary"] == "修复 Main 中的测试失败"
    assert body["repair_plan"]["impacted_files"] == ["Main.java"]
    assert body["repair_plan"]["impacted_tests"] == []
    assert len(body["plan_revisions"]) == 1
    assert "diff --git" in body["diff"]
    assert "### 修改摘要" in body["answer"]
    assert "### 测试结果" in body["answer"]
    assert "### 最终 Diff" in body["answer"]
    assert "// step1" in (repo / "Main.java").read_text(encoding="utf-8")
    # The LLM never requested run_tests: Patch triggered Maven automatically.
    assert len(captured) == 1
    docker_command = captured[0]["command"]
    assert docker_command[:2] == ["docker", "run"]
    assert docker_command[docker_command.index("--network") + 1] == "none"
    assert docker_command.count("--mount") == 1
    assert docker_command[-2:] == ["mvn", "test"]
    assert model.calls == 2  # plan + patch; passing test ends immediately


def test_agent_recovers_from_structured_multi_replacement_failure(
    workspace_root,
    origin_repo,
    monkeypatch,
):
    _, info, repo = _setup(workspace_root, origin_repo, monkeypatch)
    patch_popen_sequence(
        monkeypatch,
        [{"stdout": "Tests run: 1, Failures: 0", "returncode": 0}],
    )
    invalid = _tool_calls_named(
        "apply_patch",
        {
            "files": [
                {
                    "path": "Main.java",
                    "replacements": [
                        {
                            "old_text": "// step0",
                            "new_text": "// step1",
                        },
                        {
                            "old_text": "missing marker",
                            "new_text": "unused",
                        },
                    ],
                }
            ]
        },
        "invalid_multi_patch",
    )
    valid = _patch_only(0)
    model = FakeChatModel(
        script=[
            _plan_call(),
            invalid,
            valid,
            AIMessage(content="fixed"),
        ]
    )
    monkeypatch.setattr("app.api.agent.build_chat_model", lambda: model)

    with TestClient(create_app()) as client:
        response = client.post(
            "/agent/analyze",
            json={"task": "修复代码", "workspace_id": info.workspace_id},
        )

    body = response.json()
    assert body["apply_patch_invocations"] == 2
    assert body["apply_patch_failures"] == 1
    assert body["patch_files_requested"] == 2
    assert body["patch_replacements_requested"] == 3
    assert body["successful_patch_replacements"] == 1
    assert body["failed_patch_replacement_indices"] == [2]
    assert body["recovered_patch_failures"] == 1
    events = body["tool_events"]
    patch_events = [event for event in events if event["tool_name"] == "apply_patch"]
    assert patch_events[0]["success"] is False
    assert patch_events[0]["error_category"] == "old_text_not_found"
    assert patch_events[0]["failed_replacement_index"] == 2
    assert patch_events[0]["patch_applied"] is False
    assert patch_events[1]["success"] is True
    assert "// step1" in (repo / "Main.java").read_text(encoding="utf-8")
    assert model.calls == 3


def test_agent_repairs_after_failure_and_receives_feedback(
    workspace_root, origin_repo, monkeypatch
):
    _, info, repo = _setup(workspace_root, origin_repo, monkeypatch)
    patch_popen_sequence(
        monkeypatch,
        [
            {
                "stdout": "Tests run: 1, Failures: 1\nMainTest.fails",
                "stderr": "expected true",
                "returncode": 1,
            },
            {
                "stdout": "Tests run: 1, Failures: 0",
                "stderr": "",
                "returncode": 0,
            },
        ],
    )
    model = FakeChatModel(
        script=[
            _plan_call(call_id="plan_1"),
            _patch_only(0),
            _tool_calls_named("read_file", {"path": "Main.java"}, "read_after_fail"),
            _plan_call("首次测试失败，调整 Main 实现", call_id="plan_2"),
            _patch_only(1),
            AIMessage(content="第一次测试失败，第二次修复后通过。"),
        ]
    )
    monkeypatch.setattr("app.api.agent.build_chat_model", lambda: model)

    with TestClient(create_app()) as client:
        response = client.post(
            "/agent/analyze",
            json={"task": "测试失败后继续修复", "workspace_id": info.workspace_id},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["repair_rounds"] == 1
    assert body["test_result"]["success"] is True
    assert len(body["plan_revisions"]) == 2
    assert body["plan_revisions"][1]["failure_summary"]
    assert body["plan_revisions"][1]["modified_files"] == ["Main.java"]
    assert "首次测试失败" in body["plan_revisions"][1]["reason"]
    assert "diff --git" in body["plan_revisions"][1]["diff"]
    assert "// step2" in (repo / "Main.java").read_text(encoding="utf-8")
    feedback = "\n".join(
        message.content
        for call_history in model.history
        for message in call_history
        if isinstance(message, ToolMessage)
    )
    assert "MainTest.fails" in feedback
    assert "expected true" in feedback
    assert "public class Main" in feedback  # LLM could read code after failure
    assert model.calls == 5


def test_agent_stops_after_three_repair_rounds(
    workspace_root, origin_repo, monkeypatch
):
    _, info, repo = _setup(workspace_root, origin_repo, monkeypatch)
    patch_popen(
        monkeypatch,
        stdout="Tests run: 1, Failures: 1",
        stderr="still failing",
        returncode=1,
    )
    model = FakeChatModel(
        script=[
            _plan_call(call_id="plan_0"),
            _patch_only(0),
            _plan_call("第一轮失败", call_id="plan_1"),
            _patch_only(1),
            _plan_call("第二轮失败", call_id="plan_2"),
            _patch_only(2),
            _plan_call("第三轮失败", call_id="plan_3"),
            _patch_only(3),
            AIMessage(content="已完成三轮自动修复，测试仍未通过。"),
        ]
    )
    monkeypatch.setattr("app.api.agent.build_chat_model", lambda: model)

    with TestClient(create_app()) as client:
        response = client.post(
            "/agent/analyze",
            json={"task": "持续修复直到测试通过", "workspace_id": info.workspace_id},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["repair_rounds"] == 3
    assert body["test_result"]["success"] is False
    assert len(body["plan_revisions"]) == 4
    assert [item["revision"] for item in body["plan_revisions"]] == [1, 2, 3, 4]
    content = (repo / "Main.java").read_text(encoding="utf-8")
    assert "// step4" in content
    assert "// step5" not in content
    feedback = "\n".join(
        message.content
        for call_history in model.history
        for message in call_history
        if isinstance(message, ToolMessage)
    )
    assert "Tests run: 1, Failures: 1" in feedback
    assert "已达到最多 3 轮自动修复" in body["answer"]
    assert model.calls == 8


def test_agent_survives_docker_unavailable(
    workspace_root, origin_repo, monkeypatch
):
    _, info, repo = _setup(workspace_root, origin_repo, monkeypatch)
    monkeypatch.setattr(
        "app.workspace.docker_sandbox._docker_preflight",
        lambda: "Docker 守护进程不可用: test",
    )
    model = FakeChatModel(
        script=[
            _plan_call(),
            _patch_only(0),
            AIMessage(content="Docker 不可用，保留修改并报告错误。"),
        ]
    )
    monkeypatch.setattr("app.api.agent.build_chat_model", lambda: model)

    with TestClient(create_app()) as client:
        response = client.post(
            "/agent/analyze",
            json={"task": "修复并测试", "workspace_id": info.workspace_id},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["modified_files"] == ["Main.java"]
    assert body["test_result"]["success"] is False
    assert body["test_result"]["exit_code"] is None
    assert "Docker 守护进程不可用" in body["test_result"]["error"]
    assert "// step1" in (repo / "Main.java").read_text(encoding="utf-8")
    assert model.calls == 3
