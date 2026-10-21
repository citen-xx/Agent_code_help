"""Generate human-readable V2 failure analysis from a benchmark report."""

from collections import Counter
from pathlib import Path

from app.evaluation.models import BenchmarkReport, FailureCategory


def _excerpt(text: str | None, limit: int = 1200) -> str:
    if not text:
        return "_not available_"
    value = text.strip()
    return value if len(value) <= limit else value[:limit] + "\n...[truncated]"


def generate_failure_analysis(
    report: BenchmarkReport,
    output_path: str | Path,
) -> Path:
    failures = [
        result
        for result in report.results
        if result.suite == "v2" and not result.success
    ]
    category_counts = Counter(
        result.failure_category.value
        if result.failure_category is not None
        else "unknown"
        for result in failures
    )

    def count_category(*categories: FailureCategory) -> int:
        return sum(
            category_counts.get(category.value, 0) for category in categories
        )

    call_related = [
        result
        for result in failures
        if {
            "call-graph",
            "multi-file",
            "cross-package",
            "service",
            "repository",
            "interface",
        }
        & set(result.tags)
    ]
    patch_like_failures = [
        result
        for result in failures
        if result.failure_category == FailureCategory.PATCH_FAILURE
        or (
            result.failure_category == FailureCategory.AGENT_FAILED
            and "已达到最大步骤数" in result.agent_answer
        )
    ]
    lines = [
        "# Benchmark V2 Failure Analysis",
        "",
        f"- Run: `{report.run_id}`",
        f"- V2 tasks: {sum(1 for item in report.results if item.suite == 'v2')}",
        f"- V2 failures: {len(failures)}",
        "",
        "## Failure Categories",
        "",
        "| Analysis bucket | Count |",
        "|---|---:|",
        f"| 定位失败 | {count_category(FailureCategory.LOCALIZATION_FAILURE)} |",
        f"| 调用关系理解失败（失败且涉及多文件/调用图/跨 package） | {len(call_related)} |",
        f"| Plan 错误 | {count_category(FailureCategory.PLANNING_FAILURE)} |",
        f"| Patch 错误 | {len(patch_like_failures)} |",
        f"| 测试反馈理解失败 | {count_category(FailureCategory.PUBLIC_TEST_FAILURE)} |",
        f"| 3 轮修复耗尽 | {count_category(FailureCategory.REPAIR_LIMIT_EXCEEDED)} |",
        f"| Hidden regression | {count_category(FailureCategory.HIDDEN_TEST_FAILURE)} |",
        "",
        "## Raw Categories",
        "",
    ]
    for category, count in sorted(category_counts.items()):
        lines.append(f"- `{category}`: {count}")

    lines.extend(["", "## Failed Tasks", ""])
    if not failures:
        lines.append("No V2 failures in this run.")
    for result in failures:
        plan_summary = (
            result.plan_revisions[-1].plan.problem_summary
            if result.plan_revisions
            else "_no plan revision recorded_"
        )
        category = (
            result.failure_category.value
            if result.failure_category is not None
            else "unknown"
        )
        lines.extend(
            [
                f"### {result.task_id} ({result.difficulty.value})",
                "",
                f"- Failure category: `{category}`",
                f"- Reason: {result.failure_reason or 'unknown'}",
                f"- Public tests passed: {result.public_tests_passed}",
                f"- Hidden tests passed: {result.hidden_tests_passed}",
                f"- Repair rounds: {result.repair_rounds}",
                f"- Plan revisions: {len(result.plan_revisions)}",
                f"- Changed files: {result.changed_file_count}",
                f"- Diff size: {result.diff_size}",
                f"- Latest plan: {plan_summary}",
                "",
                "Agent final answer:",
                "",
                "```text",
                _excerpt(result.agent_answer),
                "```",
                "",
                "Hidden test failure:",
                "",
                "```text",
                _excerpt(result.hidden_failure_summary),
                "```",
                "",
            ]
        )
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
