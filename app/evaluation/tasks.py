"""Load built-in offline benchmark tasks."""

import json
from pathlib import Path

from app.evaluation.models import BenchmarkTask

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TASKS_FILE = PROJECT_ROOT / "evaluation" / "tasks.json"
DEFAULT_V2_TASKS_FILE = PROJECT_ROOT / "evaluation" / "tasks_v2.json"
DEFAULT_RESULTS_DIR = PROJECT_ROOT / "evaluation" / "results"


class BenchmarkTaskLoadError(RuntimeError):
    """Raised when benchmark metadata is missing or invalid."""


def load_tasks(
    task_id: str | None = None,
    tasks_file: str | Path | None = None,
    suite: str = "v1",
) -> list[BenchmarkTask]:
    if suite not in {"v1", "v2", "all"}:
        raise BenchmarkTaskLoadError(f"未知 benchmark suite: {suite}")
    if tasks_file is not None:
        files = [(Path(tasks_file), suite if suite != "all" else "v1")]
    elif suite == "v1":
        files = [(DEFAULT_TASKS_FILE, "v1")]
    elif suite == "v2":
        files = [(DEFAULT_V2_TASKS_FILE, "v2")]
    else:
        files = [
            (DEFAULT_TASKS_FILE, "v1"),
            (DEFAULT_V2_TASKS_FILE, "v2"),
        ]
    tasks: list[BenchmarkTask] = []
    for path, file_suite in files:
        if not path.is_file():
            raise BenchmarkTaskLoadError(f"Benchmark tasks file 不存在: {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BenchmarkTaskLoadError(f"Benchmark tasks JSON 非法: {exc}") from exc
        if not isinstance(raw, list):
            raise BenchmarkTaskLoadError("Benchmark tasks JSON 必须是数组")
        for item in raw:
            item = dict(item)
            item.setdefault("suite", file_suite)
            item.setdefault(
                "difficulty",
                "easy" if file_suite == "v1" else "medium",
            )
            tasks.append(BenchmarkTask.model_validate(item))
    seen: set[str] = set()
    for task in tasks:
        if task.task_id in seen:
            raise BenchmarkTaskLoadError(f"重复 task_id: {task.task_id}")
        seen.add(task.task_id)
        repository = Path(task.repository)
        resolved = repository if repository.is_absolute() else PROJECT_ROOT / repository
        if not resolved.exists():
            raise BenchmarkTaskLoadError(
                f"任务 {task.task_id} repository 不存在: {resolved}"
            )
        if task.expected_test_command != "mvn test":
            raise BenchmarkTaskLoadError(
                f"任务 {task.task_id} 只允许固定 expected_test_command='mvn test'"
            )
        if task.hidden_tests:
            hidden = Path(task.hidden_tests)
            hidden = hidden if hidden.is_absolute() else PROJECT_ROOT / hidden
            if not hidden.is_dir():
                raise BenchmarkTaskLoadError(
                    f"任务 {task.task_id} hidden_tests 不存在: {hidden}"
                )
    if task_id:
        tasks = [task for task in tasks if task.task_id == task_id]
        if not tasks:
            raise BenchmarkTaskLoadError(f"未找到 benchmark task: {task_id}")
    return tasks
