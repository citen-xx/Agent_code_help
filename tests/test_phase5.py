"""Tests for Phase 5 real-world benchmark integration."""

import json
from pathlib import Path

from app.agent.telemetry import ToolEvent
from app.evaluation.models import (
    BenchmarkTask,
    ContextCallTelemetry,
    ContextTelemetry,
)
from app.evaluation.phase5 import (
    parse_trigger_metadata,
    phase5_task_evaluator,
)
from app.schemas.agent import AnalyzeResponse
from app.workspace.models import BuildTool, TestRunResult as MavenTestResult


def _event(
    index: int,
    name: str,
    target: str,
    *,
    step: int,
    elapsed: float,
) -> ToolEvent:
    return ToolEvent(
        tool_call_index=index,
        step=step,
        repair_round=0,
        tool_name=name,
        target_file=target,
        success=True,
        invoked=True,
        argument_fingerprint=str(index),
        elapsed_seconds=elapsed,
    )


def test_parse_trigger_metadata():
    text = """--- org.example.FooTest::testOne
java.lang.AssertionError
\tat org.example.FooTest.testOne(FooTest.java:10)
--- org.example.BarTest::testTwo
"""
    assert parse_trigger_metadata(text) == [
        "org.example.FooTest#testOne",
        "org.example.BarTest#testTwo",
    ]


def test_phase5_evaluator_records_localization_and_before_patch_cost(tmp_path):
    reports = tmp_path / "target" / "surefire-reports"
    reports.mkdir(parents=True)
    (reports / "TEST-example.FooTest.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<testsuite>
  <testcase classname="example.FooTest" name="trigger"/>
</testsuite>
""",
        encoding="utf-8",
    )
    task = BenchmarkTask(
        task_id="d4j-example-1",
        title="example",
        description="example",
        repository=str(tmp_path / "repo.git"),
        base_commit="abc",
        problem_statement="fix it",
        suite="defects4j_v1",
        source="defects4j",
        project="Example",
        bug_id=1,
        buggy_revision="abc",
        fixed_revision="def",
        triggering_tests=["example.FooTest#trigger"],
        relevant_tests=["example.FooTest#trigger"],
        ground_truth_files=["example.Foo"],
    )
    events = [
        _event(
            1,
            "find_symbol",
            "src/main/java/example/Foo.java",
            step=2,
            elapsed=4.5,
        ),
        _event(
            2,
            "read_file",
            "src/main/java/example/Foo.java",
            step=3,
            elapsed=6.0,
        ),
        _event(
            3,
            "apply_patch",
            "src/main/java/example/Foo.java",
            step=4,
            elapsed=7.0,
        ),
    ]
    response = AnalyzeResponse(answer="done", steps=4, tool_events=events)
    context = ContextTelemetry(
        calls=[
            ContextCallTelemetry(
                call_index=index,
                task_id=task.task_id,
                profile="full",
                provider_prompt_tokens=100 * index,
                total_history_chars=10,
            )
            for index in range(1, 5)
        ]
    )
    final_test = MavenTestResult(
        tool=BuildTool.MAVEN,
        command=["mvn", "test"],
        exit_code=0,
        success=True,
        duration_seconds=1,
    )
    metrics = phase5_task_evaluator(
        task,
        tmp_path,
        response,
        final_test,
        ["src/main/java/example/Foo.java"],
        context,
    )
    assert metrics["trigger_tests_passed"] is True
    assert metrics["relevant_tests_passed"] is True
    assert metrics["file_localization_recall"] == 1.0
    assert metrics["patch_file_precision"] == 1.0
    assert metrics["time_to_first_relevant_file_seconds"] == 4.5
    assert metrics["llm_calls_to_first_relevant_file"] == 2
    assert metrics["tool_calls_to_first_relevant_file"] == 1
    assert metrics["tokens_before_first_patch"] == 1000


def test_external_task_schema_hides_ground_truth_from_problem_statement():
    raw = json.loads(Path("evaluation/tasks_v2.json").read_text(encoding="utf-8"))
    assert raw
    assert "ground_truth_files" not in raw[0]


def test_defects4j_manifest_protects_evaluator_metadata():
    manifest_path = Path("evaluation/benchmarks/defects4j_v1/tasks.json")
    tasks = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(tasks) == 15
    assert {
        difficulty: sum(
            task["external_difficulty"] == difficulty for task in tasks
        )
        for difficulty in ("local", "cross_file", "structural")
    } == {"local": 5, "cross_file": 5, "structural": 5}
    for task in tasks:
        assert task["source"] == "defects4j"
        assert task["fixed_revision"] not in task["problem_statement"]
        for path in task["ground_truth_files"]:
            assert Path(path).name not in task["problem_statement"]
        patch = Path(task["test_patch"])
        assert not patch.is_absolute()
        assert patch.as_posix().startswith("test_patches/")
