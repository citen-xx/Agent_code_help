"""Manual Maven/Gradle acceptance against a real build tool.

The script copies a small local project into a temporary Git repository,
creates a workspace through WorkspaceManager, and runs the bound test tool.
It needs Maven and network access for the first dependency download.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.tools.build_tools import create_build_tools
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
    with tempfile.TemporaryDirectory(prefix="java-code-agent-build-") as temp_dir:
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
        _git(["commit", "-qm", "测试用 Maven 项目"], origin)

        manager = WorkspaceManager(
            temp / ".workspaces",
            clone_timeout_seconds=60,
            allow_file_urls=True,
        )
        workspace = manager.create(origin.as_uri())
        try:
            tools = {
                tool.name: tool
                for tool in create_build_tools(
                    workspace.workspace_id,
                    workspace.repository_path,
                    manager=manager,
                    timeout_seconds=180,
                )
            }
            detected = json.loads(tools["detect_build_tools"].invoke({}))
            result = json.loads(tools["run_tests"].invoke({"tool": "maven"}))
            print(json.dumps({"detected": detected, "result": result}, ensure_ascii=False, indent=2))
            return 0 if result["success"] else 1
        finally:
            manager.delete(workspace.workspace_id)


if __name__ == "__main__":
    raise SystemExit(main())
