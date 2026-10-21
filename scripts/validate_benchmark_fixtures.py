"""Validate that every offline benchmark fixture fails on its initial commit."""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.evaluation.tasks import load_tasks
from app.workspace.build import run_workspace_tests
from app.workspace.manager import WorkspaceManager


def _create_workspace(manager, bundle: Path, base_commit: str):
    with tempfile.TemporaryDirectory(prefix="benchmark-validate-") as temp_dir:
        origin = Path(temp_dir) / "origin"
        subprocess.run(
            ["git", "clone", "--quiet", str(bundle), str(origin)],
            check=True,
            capture_output=True,
            text=True,
        )
        return manager.create(origin.as_uri(), commit=base_commit)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=["v1", "v2", "all"], default="v1")
    parser.add_argument("--task")
    args = parser.parse_args()
    tasks = load_tasks(suite=args.suite, task_id=args.task)
    failures = []
    with tempfile.TemporaryDirectory(prefix="benchmark-fixtures-") as temp_dir:
        manager = WorkspaceManager(
            Path(temp_dir) / ".workspaces",
            clone_timeout_seconds=60,
            allow_file_urls=True,
        )
        for task in tasks:
            bundle = PROJECT_ROOT / task.repository
            workspace = _create_workspace(manager, bundle, task.base_commit)
            try:
                result = run_workspace_tests(
                    workspace.repository_path,
                    "maven",
                    timeout_seconds=task.timeout,
                )
                expected = not result.success and result.exit_code == 1
                print(
                    f"{task.task_id}: "
                    f"exit={result.exit_code} success={result.success} "
                    f"expected_initial_failure={expected}"
                )
                if not expected:
                    failures.append(task.task_id)
            finally:
                manager.delete(workspace.workspace_id)
    if failures:
        print("Invalid fixtures: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
