"""Real Maven Java workspace acceptance for call graph and impact analysis.

Builds a temporary Git workspace with OrderService -> PriceService ->
Calculator, verifies callers/callees/impact, atomically renames the Calculator
method, verifies incremental refresh, then runs Maven twice in Docker.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.code_intel.registry import clear_all_indexes
from app.code_intel.tools import create_code_intel_tools
from app.tools.build_tools import create_build_tools
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
    fixture = PROJECT_ROOT / "tests" / "fixtures" / "maven_call_graph"
    clear_all_indexes()
    with tempfile.TemporaryDirectory(prefix="java-code-agent-calls-") as temp_dir:
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
        _git(["commit", "-qm", "Java call graph fixture"], origin)

        manager = WorkspaceManager(
            temp / ".workspaces",
            clone_timeout_seconds=60,
            allow_file_urls=True,
        )
        workspace = manager.create(origin.as_uri())
        try:
            intel = {
                tool.name: tool
                for tool in create_code_intel_tools(
                    workspace.workspace_id,
                    workspace.repository_path,
                    manager=manager,
                )
            }
            patch = {
                tool.name: tool
                for tool in create_patch_tools(
                    workspace.workspace_id,
                    workspace.repository_path,
                    manager=manager,
                )
            }["apply_patch"]
            maven = {
                tool.name: tool
                for tool in create_build_tools(
                    workspace.workspace_id,
                    workspace.repository_path,
                    manager=manager,
                    timeout_seconds=180,
                )
            }["run_tests"]

            baseline_test = json.loads(maven.invoke({"tool": "maven"}))
            callers = json.loads(
                intel["find_callers"].invoke(
                    {"method_name": "multiply", "class_name": "Calculator"}
                )
            )
            callees = json.loads(
                intel["find_callees"].invoke(
                    {"method_name": "create", "class_name": "OrderService"}
                )
            )
            impact = json.loads(
                intel["analyze_impact"].invoke(
                    {"name": "multiply", "depth": 2}
                )
            )

            patch.invoke(
                {
                    "files": [
                        {
                            "path": "src/main/java/example/Calculator.java",
                            "replacements": [
                                {
                                    "old_text": "public int multiply(int left, int right) {",
                                    "new_text": "public int multiplyBy(int left, int right) {",
                                }
                            ],
                        },
                        {
                            "path": "src/main/java/example/PriceService.java",
                            "replacements": [
                                {
                                    "old_text": "calculator.multiply(price, 2)",
                                    "new_text": "calculator.multiplyBy(price, 2)",
                                }
                            ],
                        },
                        {
                            "path": "src/test/java/example/CalculatorTest.java",
                            "replacements": [
                                {
                                    "old_text": "calculator.multiply(2, 3)",
                                    "new_text": "calculator.multiplyBy(2, 3)",
                                }
                            ],
                        },
                    ]
                }
            )
            refreshed_callees = json.loads(
                intel["find_callees"].invoke(
                    {"method_name": "calculate", "class_name": "PriceService"}
                )
            )
            refreshed_callers = json.loads(
                intel["find_callers"].invoke(
                    {"method_name": "multiplyBy", "class_name": "Calculator"}
                )
            )
            patched_test = json.loads(maven.invoke({"tool": "maven"}))

            impacted = {
                (
                    item["depth"],
                    item["call"]["containing_class"],
                    item["call"]["containing_method"],
                )
                for item in impact["impacted_callers"]
            }
            tests = {
                reference["file_path"] for reference in impact["test_references"]
            }
            passed = (
                baseline_test["success"] is True
                and any(
                    call["containing_class"] == "PriceService"
                    for call in callers["calls"]
                )
                and any(
                    call["callee_name"] == "calculate"
                    for call in callees["calls"]
                )
                and (1, "PriceService", "calculate") in impacted
                and (2, "OrderService", "create") in impacted
                and tests
                and any(
                    call["callee_name"] == "multiplyBy"
                    for call in refreshed_callees["calls"]
                )
                and any(
                    call["containing_class"] == "PriceService"
                    for call in refreshed_callers["calls"]
                )
                and patched_test["success"] is True
            )
            print(
                json.dumps(
                    {
                        "workspace_id": workspace.workspace_id,
                        "docker_image": os.getenv("DOCKER_MAVEN_IMAGE"),
                        "baseline_maven_success": baseline_test["success"],
                        "patched_maven_success": patched_test["success"],
                        "calculator_callers": callers["calls"],
                        "order_service_callees": callees["calls"],
                        "impacted_callers": impact["impacted_callers"],
                        "impact_test_files": sorted(tests),
                        "refreshed_callees": refreshed_callees["calls"],
                        "refreshed_callers": refreshed_callers["calls"],
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
