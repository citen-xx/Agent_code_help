"""Phase 8 frozen holdout integrity tests."""

import hashlib
import json
from pathlib import Path


BENCHMARK = Path("evaluation/benchmarks/defects4j_v2_holdout")


def test_holdout_manifest_is_frozen_and_complete():
    tasks_path = BENCHMARK / "tasks.json"
    manifest = json.loads(
        (BENCHMARK / "benchmark_manifest.json").read_text(encoding="utf-8")
    )
    tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
    assert 12 <= len(tasks) <= 15
    assert manifest["task_count"] == len(tasks)
    assert manifest["tasks_sha256"] == hashlib.sha256(
        tasks_path.read_bytes()
    ).hexdigest()
    assert len({task["task_id"] for task in tasks}) == len(tasks)
    assert len({(task["project"], task["bug_id"]) for task in tasks}) == len(
        tasks
    )


def test_holdout_does_not_reuse_v1_bug_ids():
    v1 = json.loads(
        Path("evaluation/benchmarks/defects4j_v1/tasks.json").read_text(
            encoding="utf-8"
        )
    )
    holdout = json.loads(
        (BENCHMARK / "tasks.json").read_text(encoding="utf-8")
    )
    v1_ids = {(task["project"], task["bug_id"]) for task in v1}
    holdout_ids = {(task["project"], task["bug_id"]) for task in holdout}
    assert v1_ids.isdisjoint(holdout_ids)


def test_holdout_problem_statement_and_workspace_metadata_are_sanitized():
    tasks = json.loads(
        (BENCHMARK / "tasks.json").read_text(encoding="utf-8")
    )
    for task in tasks:
        problem = task["problem_statement"].lower()
        assert "ground" not in problem
        assert "fixed" not in problem
        assert task["fixed_revision"]
        assert task["ground_truth_files"]
        assert task["test_patch"]
