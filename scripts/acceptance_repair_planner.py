"""Real LLM + Docker Maven acceptance for the Repair Planner workflow.

The fixture contains Controller -> Service -> Calculator with a Calculator
multiply bug. The Agent must create a Repair Plan before patching, fix the
source, run Docker Maven, and return the final plan/revisions/diff.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.workspace.manager as manager_module
from app.code_intel.registry import clear_all_indexes
from app.main import create_app
from app.workspace.manager import WorkspaceManager


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def main() -> int:
    fixture = PROJECT_ROOT / "tests" / "fixtures" / "maven_repair_planner"
    clear_all_indexes()
    with tempfile.TemporaryDirectory(prefix="java-code-agent-planner-") as temp_dir:
        temp = Path(temp_dir)
        origin = temp / "origin"
        origin.mkdir()
        for source in fixture.rglob("*"):
            if source.is_file():
                target = origin / source.relative_to(fixture)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())

        _git(["init", "-q", "--initial-branch=main"], origin)
        _git(["config", "user.email", "acceptance@example.com"], origin)
        _git(["config", "user.name", "Acceptance"], origin)
        _git(["add", "-A"], origin)
        _git(["commit", "-qm", "Repair planner fixture"], origin)

        manager = WorkspaceManager(
            temp / ".workspaces",
            clone_timeout_seconds=60,
            allow_file_urls=True,
        )
        manager_module.workspace_manager = manager
        workspace = manager.create(origin.as_uri())
        try:
            with TestClient(create_app()) as client:
                client.timeout = httpx.Timeout(600.0)
                response = client.post(
                    "/agent/analyze",
                    json={
                        "task": (
                            "Controller 创建订单的价格不正确。请先用符号工具和 "
                            "analyze_impact 分析影响，再创建结构化修复计划，修复 "
                            "Calculator 或 Service 层的缺陷，不要修改测试代码，"
                            "最后运行 Maven test 直到通过。"
                        ),
                        "workspace_id": workspace.workspace_id,
                    },
                )
            print("POST /agent/analyze:", response.status_code)
            print(response.text)
            response.raise_for_status()
            body = response.json()
            calculator = (
                Path(workspace.repository_path)
                / "src"
                / "main"
                / "java"
                / "example"
                / "Calculator.java"
            )
            fixed = "return left * right;" in calculator.read_text(encoding="utf-8")
            plan = body.get("repair_plan") or {}
            revisions = body.get("plan_revisions") or []
            test_result = body.get("test_result") or {}
            passed = (
                fixed
                and plan.get("impacted_files")
                and plan.get("planned_changes")
                and plan.get("validation_plan", {}).get("run_maven_test") is True
                and len(revisions) >= 1
                and test_result.get("success") is True
                and any(
                    path.endswith("src/main/java/example/Calculator.java")
                    for path in body.get("modified_files", [])
                )
                and "diff --git" in body.get("diff", "")
            )
            print(
                json.dumps(
                    {
                        "workspace_id": workspace.workspace_id,
                        "docker_image": os.getenv("DOCKER_MAVEN_IMAGE"),
                        "repair_plan": body.get("repair_plan"),
                        "plan_revisions": body.get("plan_revisions"),
                        "modified_files": body.get("modified_files"),
                        "repair_rounds": body.get("repair_rounds"),
                        "test_success": test_result.get("success"),
                        "calculator_fixed": fixed,
                        "acceptance_passed": passed,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if passed else 1
        finally:
            manager.delete(workspace.workspace_id)
            clear_all_indexes()


if __name__ == "__main__":
    raise SystemExit(main())
