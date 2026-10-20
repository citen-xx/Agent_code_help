"""Load built-in offline benchmark tasks."""

import json
from pathlib import Path

from app.evaluation.models import BenchmarkTask

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TASKS_FILE = PROJECT_ROOT / "evaluation" / "tasks.json"
DEFAULT_RESULTS_DIR = PROJECT_ROOT / "evaluation" / "results"


class BenchmarkTaskLoadError(RuntimeError):
    """Raised when benchmark metadata is missing or invalid."""


def load_tasks(
    task_id: str | None = None,
    tasks_file: str | Path = DEFAULT_TASKS_FILE,
) -> list[BenchmarkTask]:
    path = Path(tasks_file)
    if not path.is_file():
        raise BenchmarkTaskLoadError(f"Benchmark tasks file 不存在: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BenchmarkTaskLoadError(f"Benchmark tasks JSON 非法: {exc}") from exc
    if not isinstance(raw, list):
        raise BenchmarkTaskLoadError("Benchmark tasks JSON 必须是数组")
    tasks = [BenchmarkTask.model_validate(item) for item in raw]
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
    if task_id:
        tasks = [task for task in tasks if task.task_id == task_id]
        if not tasks:
            raise BenchmarkTaskLoadError(f"未找到 benchmark task: {task_id}")
    return tasks
