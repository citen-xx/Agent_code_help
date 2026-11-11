"""Real Java workspace acceptance for Tree-sitter symbol indexing.

Creates a temporary Git workspace, builds the symbol index, queries classes,
methods, and references, then patches a method and verifies incremental
refresh. This script does not invoke an LLM or Maven.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.code_intel.registry import clear_all_indexes
from app.code_intel.tools import create_code_intel_tools
from app.tools.patch_tools import create_patch_tools
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
    clear_all_indexes()
    with tempfile.TemporaryDirectory(prefix="java-code-agent-intel-") as temp_dir:
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
        _git(["commit", "-qm", "Java code intelligence fixture"], origin)

        manager = WorkspaceManager(
            temp / ".workspaces",
            clone_timeout_seconds=60,
            allow_file_urls=True,
        )
        workspace = manager.create(origin.as_uri())
        try:
            tools = {
                tool.name: tool
                for tool in create_code_intel_tools(
                    workspace.workspace_id,
                    workspace.repository_path,
                    manager=manager,
                )
            }
            patch_tool = {
                tool.name: tool
                for tool in create_patch_tools(
                    workspace.workspace_id,
                    workspace.repository_path,
                    manager=manager,
                )
            }["apply_patch"]

            listed = json.loads(tools["list_symbols"].invoke({}))
            calculator = json.loads(
                tools["find_symbol"].invoke(
                    {"name": "Calculator", "symbol_type": "class"}
                )
            )
            add = json.loads(
                tools["find_symbol"].invoke(
                    {"name": "add", "symbol_type": "method"}
                )
            )
            references = json.loads(
                tools["find_references"].invoke({"name": "Calculator"})
            )

            source_path = "src/main/java/example/Calculator.java"
            patch_tool.invoke(
                {
                    "files": [
                        {
                            "path": source_path,
                            "replacements": [
                                {
                                    "old_text": "public static int add(int left, int right) {",
                                    "new_text": "public static int add(int left, int right, int offset) {",
                                }
                            ],
                        }
                    ]
                }
            )
            refreshed = json.loads(
                tools["find_symbol"].invoke(
                    {"name": "add", "symbol_type": "method"}
                )
            )

            signature = (
                refreshed["symbols"][0]["signature"] if refreshed["symbols"] else ""
            )
            passed = (
                calculator["total"] == 1
                and add["total"] == 1
                and references["total"] >= 1
                and "int offset" in signature
            )
            print(
                json.dumps(
                    {
                        "workspace_id": workspace.workspace_id,
                        "indexed_symbols": listed["total"],
                        "calculator": calculator["symbols"],
                        "add_before_patch": add["symbols"],
                        "calculator_references": references["references"],
                        "add_after_patch": refreshed["symbols"],
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
