"""Real end-to-end acceptance for apply_patch + Maven repair loop.

Copies the Maven fixture into a temporary Git repository, injects a failing
Calculator implementation, then asks the configured LLM Agent to repair it
through the real /agent/analyze API. Requires Maven, network for the LLM, and
a fully configured .env.
"""

import json
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
    fixture = PROJECT_ROOT / "tests" / "fixtures" / "maven_basic"
    with tempfile.TemporaryDirectory(prefix="java-code-agent-patch-") as temp_dir:
        temp = Path(temp_dir)
        origin = temp / "origin"
        origin.mkdir()
        for source in fixture.rglob("*"):
            if source.is_file():
                target = origin / source.relative_to(fixture)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())

        calculator = origin / "src" / "main" / "java" / "example" / "Calculator.java"
        calculator.write_text(
            calculator.read_text(encoding="utf-8").replace(
                "return left + right;", "return left - right;"
            ),
            encoding="utf-8",
        )
        _git(["init", "-q", "--initial-branch=main"], origin)
        _git(["config", "user.email", "acceptance@example.com"], origin)
        _git(["config", "user.name", "Acceptance"], origin)
        _git(["add", "-A"], origin)
        _git(["commit", "-qm", "注入测试缺陷"], origin)

        manager = WorkspaceManager(
            temp / ".workspaces",
            clone_timeout_seconds=60,
            allow_file_urls=True,
        )
        manager_module.workspace_manager = manager
        workspace = manager.create(origin.as_uri())
        try:
            workspace_calculator = (
                Path(workspace.repository_path)
                / "src"
                / "main"
                / "java"
                / "example"
                / "Calculator.java"
            )
            with TestClient(create_app()) as client:
                client.timeout = httpx.Timeout(600.0)
                response = client.post(
                    "/agent/analyze",
                    json={
                        "task": (
                            "运行 Maven 测试，定位 Calculator.add 的缺陷并用 "
                            "apply_patch 修复，直到测试通过。不要修改测试代码。"
                        ),
                        "workspace_id": workspace.workspace_id,
                    },
                )
            print("POST /agent/analyze:", response.status_code)
            print(response.text)
            response.raise_for_status()
            body = response.json()
            repaired = workspace_calculator.read_text(encoding="utf-8")
            modified_files = body.get("modified_files", [])
            passed = (
                body.get("test_result") is not None
                and body["test_result"].get("success") is True
                and any(
                    path.endswith("src/main/java/example/Calculator.java")
                    for path in modified_files
                )
                and "return left + right;" in repaired
                and "diff --git" in body.get("diff", "")
            )
            print(
                json.dumps(
                    {
                        "workspace_id": workspace.workspace_id,
                        "modified_files": modified_files,
                        "repair_rounds": body.get("repair_rounds"),
                        "test_success": (
                            body.get("test_result") or {}
                        ).get("success"),
                        "calculator_repaired": "return left + right;" in repaired,
                        "acceptance_passed": passed,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if passed else 1
        finally:
            manager.delete(workspace.workspace_id)


if __name__ == "__main__":
    raise SystemExit(main())
