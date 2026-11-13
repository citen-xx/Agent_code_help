"""Phase 5.5 stability confirmation and search-to-patch diagnosis."""

import hashlib
import json
import re
import shutil
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.agent.capabilities import AgentProfile, PROFILE_CAPABILITIES
from app.agent.graph import build_system_prompt
from app.code_intel.tools import create_code_intel_tools
from app.core.config import settings
from app.evaluation.models import BenchmarkReport
from app.evaluation.phase5 import (
    CI_TOOLS,
    DEFAULT_TASKS_FILE,
    PHASE5_DIR_NAME,
    _localization_metrics,
    _task_rows,
    load_external_tasks,
    make_host_maven_runner,
    phase5_task_evaluator,
    prepare_defects4j_workspace,
)
from app.evaluation.runner import BenchmarkRunner
from app.evaluation.tasks import DEFAULT_RESULTS_DIR, PROJECT_ROOT
from app.repair.tools import create_repair_plan_tools
from app.tools.build_tools import create_build_tools
from app.tools.git_tools import create_git_tools
from app.tools.patch_tools import create_patch_tools
from app.tools.registry import create_repo_tools
from app.workspace import git

PHASE55_DIR_NAME = "phase5_5_search_to_patch"
PROFILES = (AgentProfile.BASIC, AgentProfile.FULL)
EXPLORATION_TOOLS = {
    *CI_TOOLS,
    "read_file",
    "search_code",
    "list_files",
    "git_show",
    "git_log",
    "git_status",
    "git_diff",
}
_CHANGED_RANGE_CACHE: dict[tuple, dict[str, list[tuple[int, int]]]] = {}


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _write_json(path: Path, payload) -> None:
    _write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def _phase_dir(output_dir: str | Path) -> Path:
    root = Path(output_dir).expanduser()
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return root.resolve() / PHASE55_DIR_NAME


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _report_paths(directory: Path) -> list[Path]:
    return sorted(directory.glob("*/report.json"))


def _load_single_report(directory: Path) -> tuple[BenchmarkReport, Path]:
    paths = _report_paths(directory)
    if len(paths) != 1:
        raise RuntimeError(
            f"需要恰好一个完整 report，实际 {len(paths)}: {directory}"
        )
    report = BenchmarkReport.model_validate_json(
        paths[0].read_text(encoding="utf-8")
    )
    if len(report.results) != 15:
        raise RuntimeError(f"report 不完整: {paths[0]}")
    return report, paths[0]


def _tool_schema_hash(profile: AgentProfile) -> str:
    import tempfile

    workspace_id = "schema-snapshot"
    with tempfile.TemporaryDirectory(prefix="phase5-schema-") as temporary:
        repository = Path(temporary) / workspace_id / "repository"
        repository.mkdir(parents=True)
        tools = create_repo_tools(repository)
        tools += create_git_tools(workspace_id, repository)
        capabilities = PROFILE_CAPABILITIES[profile]
        if capabilities.code_intelligence:
            tools += create_code_intel_tools(workspace_id, repository)
        if capabilities.repair_planner:
            tools += create_repair_plan_tools(workspace_id, repository)
        tools += create_patch_tools(workspace_id, repository)
        tools += create_build_tools(workspace_id, repository)
        snapshot = []
        for tool in sorted(tools, key=lambda item: item.name):
            schema = getattr(tool, "args_schema", None)
            snapshot.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "schema": (
                        schema.model_json_schema()
                        if schema is not None
                        else None
                    ),
                }
            )
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_manifest(
    tasks_file: Path,
    tasks,
    *,
    phase5_manifest: dict,
    phase5_5_dir: Path,
) -> dict:
    if git.worktree_is_dirty(PROJECT_ROOT):
        raise RuntimeError("Phase 5.5 要求 clean working tree")
    tasks_hash = _sha256(tasks_file)
    if tasks_hash != phase5_manifest["tasks_sha256"]:
        raise RuntimeError("defects4j_v1 manifest 已被修改")
    prompt_hashes = {
        profile.value: hashlib.sha256(
            build_system_prompt(PROFILE_CAPABILITIES[profile]).encode(
                "utf-8"
            )
        ).hexdigest()
        for profile in PROFILES
    }
    if prompt_hashes != phase5_manifest["agent"]["prompt_hashes"]:
        raise RuntimeError("Agent prompt hash 与 Phase 5 不一致")
    benchmark_manifest = tasks_file.parent / "benchmark_manifest.json"
    return {
        "schema_version": 1,
        "phase": "5.5",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": "defects4j_v1",
        "benchmark_manifest": {
            "path": str(benchmark_manifest),
            "sha256": _sha256(benchmark_manifest),
        },
        "tasks": {
            "path": str(tasks_file),
            "sha256": tasks_hash,
            "count": len(tasks),
        },
        "defects4j_commit": phase5_manifest["defects4j_commit"],
        "jdk": phase5_manifest["agent"]["jdk"],
        "model": settings.LLM_MODEL,
        "temperature": 0,
        "max_steps_control": 20,
        "lifecycle_compaction": False,
        "agent": {
            "git_commit": git.head_commit(PROJECT_ROOT),
            "git_dirty": False,
            "prompt_hashes": prompt_hashes,
            "tool_schema_hashes": {
                profile.value: _tool_schema_hash(profile)
                for profile in PROFILES
            },
        },
        "phase5_source_manifest": str(
            phase5_5_dir.parent
            / PHASE5_DIR_NAME
            / "source_manifest.json"
        ),
    }


def _failure_category(
    task_id: str,
    report: BenchmarkReport,
) -> str | None:
    row = next(
        (item for item in _task_rows(report) if item["task_id"] == task_id),
        None,
    )
    return row.get("failure_category") if row else None


def _stability(
    run1: dict[AgentProfile, BenchmarkReport],
    run2: dict[AgentProfile, BenchmarkReport],
) -> dict:
    result = {}
    for profile in PROFILES:
        first = {item.task_id: item for item in run1[profile].results}
        second = {item.task_id: item for item in run2[profile].results}
        result[profile.value] = {}
        for task_id in sorted(first):
            one = first[task_id]
            two = second[task_id]
            if one.success and two.success:
                classification = "stable_pass"
            elif not one.success and not two.success:
                classification = "stable_fail"
            else:
                classification = "unstable"
            result[profile.value][task_id] = {
                "run_1_success": one.success,
                "run_2_success": two.success,
                "run_1_failure_category": _failure_category(
                    task_id,
                    run1[profile],
                ),
                "run_2_failure_category": _failure_category(
                    task_id,
                    run2[profile],
                ),
                "classification": classification,
            }
    return result


def _overlaps(
    first_start: int | None,
    first_end: int | None,
    second_start: int | None,
    second_end: int | None,
) -> bool:
    if first_start is None or first_end is None:
        return second_start is None or second_end is None
    if second_start is None or second_end is None:
        return False
    left = max(first_start, second_start)
    right = min(first_end, second_end)
    overlap = max(0, right - left + 1)
    smaller = min(
        first_end - first_start + 1,
        second_end - second_start + 1,
    )
    return smaller > 0 and overlap / smaller >= 0.8


def _ground_truth_changed_ranges(task) -> dict[str, list[tuple[int, int]]]:
    files = tuple(task.ground_truth_files)
    key = (
        task.repository,
        task.base_commit,
        task.fixed_revision,
        files,
    )
    if key in _CHANGED_RANGE_CACHE:
        return _CHANGED_RANGE_CACHE[key]
    if not task.fixed_revision or not files:
        _CHANGED_RANGE_CACHE[key] = {}
        return {}
    try:
        output = git.run_git(
            [
                "diff",
                "--unified=0",
                "--no-color",
                "--no-ext-diff",
                task.base_commit,
                task.fixed_revision,
                "--",
                *files,
            ],
            cwd=Path(task.repository),
        )
    except (git.GitError, OSError):
        _CHANGED_RANGE_CACHE[key] = {}
        return {}
    ranges: dict[str, list[tuple[int, int]]] = defaultdict(list)
    current_file = None
    for line in output.splitlines():
        if line.startswith("+++ b/"):
            current_file = (
                line.removeprefix("+++ b/")
                .replace("\\", "/")
                .lstrip("./")
            )
            continue
        match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if not current_file or not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        if count == 0:
            ranges[current_file].append((start, start))
        else:
            ranges[current_file].append((start, start + count - 1))
    result = dict(ranges)
    _CHANGED_RANGE_CACHE[key] = result
    return result


def _read_is_near_change(
    event: dict,
    changed_ranges: dict[str, list[tuple[int, int]]],
) -> bool:
    path = (
        (event["target_file"] or "")
        .replace("\\", "/")
        .lstrip("./")
    )
    if event["read_start_line"] is None or event["read_end_line"] is None:
        return False
    for changed_start, changed_end in changed_ranges.get(path, []):
        if (
            event["read_start_line"] <= changed_end + 20
            and event["read_end_line"] >= changed_start - 20
        ):
            return True
    return False


def _timeline(
    task,
    report: BenchmarkReport,
    result,
) -> dict:
    ground_truth = {
        item.replace("\\", "/").lstrip("./")
        for item in task.ground_truth_files
    }

    def relevant_hit(paths: set[str]) -> bool:
        return any(
            any(
                path == expected or path.endswith("/" + expected)
                for expected in ground_truth
            )
            for path in paths
        )

    events = []
    for event in result.tool_events:
        paths = {
            value.strip().replace("\\", "/")
            for value in (event.target_file or "").split(",")
            if value.strip()
        }
        events.append(
            {
                "step": event.step,
                "llm_call_index": event.step,
                "tool": event.tool_name,
                "target_file": event.target_file,
                "query": event.query,
                "result_size": event.output_chars,
                "relevant_file_hit": relevant_hit(paths),
                "patch_attempted": event.tool_name == "apply_patch",
                "test_run": event.tool_name == "run_tests",
                "tool_call_index": event.tool_call_index,
                "read_start_line": event.read_start_line,
                "read_end_line": event.read_end_line,
                "elapsed_seconds": event.elapsed_seconds,
                "redundant_exploration": False,
            }
        )
    first_relevant = next(
        (event for event in events if event["relevant_file_hit"]),
        None,
    )
    first_patch = next(
        (event for event in events if event["patch_attempted"]),
        None,
    )
    changed_ranges = _ground_truth_changed_ranges(task)
    focused_relevant_reads = [
        event
        for event in events
        if event["tool"] == "read_file"
        and event["relevant_file_hit"]
        and _read_is_near_change(event, changed_ranges)
    ]
    first_focused_relevant_read = (
        focused_relevant_reads[0] if focused_relevant_reads else None
    )
    pre_patch = (
        events[: first_patch["tool_call_index"] - 1]
        if first_patch
        else events
    )
    post_relevant = (
        [
            event
            for event in pre_patch
            if first_relevant
            and event["tool_call_index"]
            > first_relevant["tool_call_index"]
        ]
        if first_relevant
        else []
    )
    exploration = [
        event for event in events if event["tool"] in EXPLORATION_TOOLS
    ]
    redundant_indexes = set()
    reads = defaultdict(list)
    search_groups = defaultdict(list)
    ci_groups = defaultdict(list)
    directory_groups = defaultdict(list)
    for event in exploration:
        index = event["tool_call_index"]
        if event["tool"] == "read_file" and event["target_file"]:
            for previous in reads[event["target_file"]]:
                if _overlaps(
                    previous["read_start_line"],
                    previous["read_end_line"],
                    event["read_start_line"],
                    event["read_end_line"],
                ):
                    redundant_indexes.add(index)
                    break
            reads[event["target_file"]].append(event)
        elif event["tool"] == "search_code" and event["query"]:
            key = " ".join(event["query"].lower().split())
            if search_groups[key]:
                redundant_indexes.add(index)
            search_groups[key].append(event)
        elif event["tool"] in CI_TOOLS and event["query"]:
            key = (event["tool"], event["query"].strip().lower())
            if ci_groups[key]:
                redundant_indexes.add(index)
            ci_groups[key].append(event)
        elif event["tool"] == "list_files" and event["target_file"]:
            key = event["target_file"].replace("\\", "/")
            if directory_groups[key]:
                redundant_indexes.add(index)
            directory_groups[key].append(event)
    for event in exploration:
        event["redundant_exploration"] = (
            event["tool_call_index"] in redundant_indexes
        )
    search_count = sum(
        event["tool"] == "search_code" for event in post_relevant
    )
    read_count = sum(
        event["tool"] == "read_file" for event in post_relevant
    )
    ci_count = sum(event["tool"] in CI_TOOLS for event in post_relevant)
    task_rows = _task_rows(report)
    metrics = next(
        item for item in task_rows if item["task_id"] == task.task_id
    )
    return {
        "task_id": task.task_id,
        "profile": report.variant,
        "success": result.success,
        "step_limit": "已达到最大步骤数" in result.agent_answer,
        "timeline": events,
        "first_relevant_file_step": (
            first_relevant["step"] if first_relevant else None
        ),
        "first_patch_step": first_patch["step"] if first_patch else None,
        "relevant_to_patch_gap": (
            first_patch["step"] - first_relevant["step"]
            if first_patch and first_relevant
            else None
        ),
        "llm_calls_before_first_patch": (
            max(first_patch["step"] - 1, 0) if first_patch else None
        ),
        "exploration_calls_before_first_patch": len(pre_patch),
        "post_relevant_search_calls": search_count,
        "post_relevant_read_calls": read_count,
        "post_relevant_ci_calls": ci_count,
        "patch_attempted": bool(first_patch),
        "exploration_without_commitment": bool(
            first_relevant and not first_patch
        ),
        "read_file_calls": sum(
            event["tool"] == "read_file" for event in events
        ),
        "read_near_ground_truth_change": bool(focused_relevant_reads),
        "first_read_near_ground_truth_change_step": (
            first_focused_relevant_read["step"]
            if first_focused_relevant_read
            else None
        ),
        "tokens_before_first_patch": metrics["tokens_before_first_patch"],
        "file_localization_recall": metrics["file_localization_recall"],
        "repeated_read_calls": sum(
            event["tool"] == "read_file"
            and event["redundant_exploration"]
            for event in exploration
        ),
        "repeated_search_calls": sum(
            event["tool_call_index"] in redundant_indexes
            and event["tool"] == "search_code"
            for event in exploration
        ),
        "repeated_ci_calls": sum(
            event["tool_call_index"] in redundant_indexes
            and event["tool"] in CI_TOOLS
            for event in exploration
        ),
        "repeated_directory_calls": sum(
            event["tool_call_index"] in redundant_indexes
            and event["tool"] == "list_files"
            for event in exploration
        ),
        "redundant_exploration_calls": len(redundant_indexes),
        "total_exploration_calls": len(exploration),
        "exploration_waste_ratio": (
            len(redundant_indexes) / len(exploration)
            if exploration
            else 0.0
        ),
        "last_five_steps": events[-5:],
        "query_telemetry_available": any(
            event["query"] is not None
            for event in exploration
            if event["tool"] == "search_code"
            or event["tool"] in CI_TOOLS
        ),
    }


def _combined_metrics(reports: list[BenchmarkReport]) -> dict:
    results = [result for report in reports for result in report.results]
    tokens = [
        result.total_tokens
        for result in results
        if result.total_tokens is not None
    ]
    calls = [result.llm_calls for result in results]
    durations = [result.duration_seconds for result in results]
    tools = [result.agent_tool_calls for result in results]
    return {
        "task_runs": len(results),
        "passed": sum(1 for result in results if result.success),
        "success_rate": (
            sum(1 for result in results if result.success) / len(results)
            if results
            else 0.0
        ),
        "average_tokens": sum(tokens) / len(tokens) if tokens else None,
        "average_llm_calls": sum(calls) / len(calls) if calls else 0.0,
        "average_tool_calls": sum(tools) / len(tools) if tools else 0.0,
        "average_duration": (
            sum(durations) / len(durations) if durations else 0.0
        ),
    }


def _copy_run_1(phase5_dir: Path, phase55_dir: Path) -> dict:
    result = {}
    for profile in PROFILES:
        source = phase5_dir / f"{profile.value}_results"
        paths = _report_paths(source)
        if len(paths) != 1:
            raise RuntimeError(f"Phase 5 run_1 缺失: {source}")
        target_dir = phase55_dir / "run_1" / profile.value
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            paths[0].parent,
            target_dir,
            dirs_exist_ok=True,
        )
        result[profile.value] = target_dir / "report.json"
    return result


def _run_confirmation(
    tasks,
    phase55_dir: Path,
    *,
    run_ids: dict[AgentProfile, str] | None = None,
) -> dict[AgentProfile, BenchmarkReport]:
    host_test_runner = make_host_maven_runner(offline=True)
    result = {}
    for profile in PROFILES:
        output = phase55_dir / "confirmation_run_2"
        run_id = (
            run_ids[profile]
            if run_ids
            else f"phase5_5_{profile.value}_run2"
        )
        existing = output / run_id / "report.json"
        if existing.is_file():
            report = BenchmarkReport.model_validate_json(
                existing.read_text(encoding="utf-8")
            )
            if len(report.results) == 15:
                result[profile] = report
                continue
        runner = BenchmarkRunner(
            tasks,
            output_dir=output,
            run_id=run_id,
            profile=profile,
            max_steps=20,
            test_runner=host_test_runner,
            task_evaluator=phase5_task_evaluator,
            workspace_preparer=prepare_defects4j_workspace,
        )
        report, _ = asyncio.run(runner.run())
        result[profile] = report
    return result


def run_step_budget_diagnostic(
    tasks,
    phase55_dir: Path,
    stable_fail_ids: list[str],
) -> tuple[BenchmarkReport, list[dict]]:
    selected = [task for task in tasks if task.task_id in stable_fail_ids]
    if not selected:
        raise RuntimeError("没有 stable-fail Full tasks 可供 30-step 诊断")
    host_test_runner = make_host_maven_runner(offline=True)
    runner = BenchmarkRunner(
        selected,
        output_dir=phase55_dir / "step_budget_experiment",
        run_id="full_at_30_diagnostic",
        profile=AgentProfile.FULL,
        max_steps=30,
        test_runner=host_test_runner,
        task_evaluator=phase5_task_evaluator,
        workspace_preparer=prepare_defects4j_workspace,
    )
    report, _ = asyncio.run(runner.run())
    records = []
    for result in report.results:
        timeline = _timeline(
            next(
                task
                for task in selected
                if task.task_id == result.task_id
            ),
            report,
            result,
        )
        if result.success:
            category = "budget_limited"
        elif timeline["patch_attempted"]:
            category = "late_patch_failure"
        else:
            category = "strategy_limited"
        records.append(
            {
                "task_id": result.task_id,
                "success_at_30": result.success,
                "classification": category,
                "patch_at_30": timeline["patch_attempted"],
                "first_patch_step": timeline["first_patch_step"],
                "patch_only_after_step_20": bool(
                    timeline["first_patch_step"]
                    and timeline["first_patch_step"] > 20
                ),
                "llm_calls": result.llm_calls,
                "tool_calls": result.agent_tool_calls,
                "total_tokens": result.total_tokens,
                "duration_seconds": result.duration_seconds,
                "timeline": timeline,
            }
        )
    return report, records


def _summary(report: BenchmarkReport) -> dict:
    summary = report.summary
    return {
        "passed": summary.passed_tasks,
        "total": summary.total_tasks,
        "success_rate": summary.success_rate,
        "average_tokens": summary.average_tokens,
        "average_llm_calls": summary.average_llm_calls,
        "average_duration": summary.average_duration,
    }


def _profile_reports(
    paths: dict[str, Path],
) -> dict[AgentProfile, BenchmarkReport]:
    return {
        AgentProfile(profile): BenchmarkReport.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        for profile, path in paths.items()
    }


def _stability_summary(stability: dict) -> dict:
    result = {}
    for profile in PROFILES:
        counts = Counter(
            item["classification"]
            for item in stability[profile.value].values()
        )
        result[profile.value] = {
            name: counts.get(name, 0)
            for name in ("stable_pass", "stable_fail", "unstable")
        }
    return result


def _conversion_metrics(
    reports: dict[AgentProfile, list[BenchmarkReport]],
    tasks,
) -> dict:
    task_map = {task.task_id: task for task in tasks}
    result = {}
    for profile in PROFILES:
        rows = [
            _timeline(
                task_map[task_result.task_id],
                report,
                task_result,
            )
            for report in reports[profile]
            for task_result in report.results
        ]
        located = [
            row
            for row in rows
            if row["first_relevant_file_step"] is not None
        ]
        patched = [
            row for row in located if row["patch_attempted"]
        ]
        result[profile.value] = {
            "task_runs": len(rows),
            "runs_located_relevant_file": len(located),
            "runs_with_any_patch": sum(
                row["patch_attempted"] for row in rows
            ),
            "runs_with_patch_after_localization": len(patched),
            "patch_conversion_rate": (
                len(patched) / len(located) if located else 0.0
            ),
            "successful_task_runs": sum(
                row["success"] for row in located
            ),
            "successful_patch_conversion": (
                sum(row["success"] for row in located) / len(located)
                if located
                else 0.0
            ),
            "average_first_patch_step": _mean_optional(
                [row["first_patch_step"] for row in patched]
            ),
            "average_relevant_to_patch_gap": _mean_optional(
                [row["relevant_to_patch_gap"] for row in patched]
            ),
        }
    return result


def _mean_optional(values) -> float | None:
    filtered = [value for value in values if value is not None]
    return sum(filtered) / len(filtered) if filtered else None


def _comparison_groups(timelines: list[dict], *, run: str) -> dict:
    result = {}
    for profile in PROFILES:
        groups = {}
        for name, predicate in (
            ("success", lambda item: item["success"]),
            ("failure", lambda item: not item["success"]),
        ):
            group = [
                item
                for item in timelines
                if item["profile"] == profile.value
                and item["run"] == run
                and predicate(item)
            ]
            groups[name] = {
                "task_runs": len(group),
                "patch_rate": (
                    sum(item["patch_attempted"] for item in group)
                    / len(group)
                    if group
                    else 0.0
                ),
                "first_relevant_step": _mean_optional(
                    [item["first_relevant_file_step"] for item in group]
                ),
                "first_patch_step": _mean_optional(
                    [item["first_patch_step"] for item in group]
                ),
                "relevant_to_patch_gap": _mean_optional(
                    [item["relevant_to_patch_gap"] for item in group]
                ),
                "exploration_calls": _mean_optional(
                    [
                        sum(
                            event["tool"] in EXPLORATION_TOOLS
                            for event in item["timeline"]
                        )
                        for item in group
                    ]
                ),
                "repeat_ratio": _mean_optional(
                    [item["exploration_waste_ratio"] for item in group]
                ),
            }
        result[profile.value] = groups
    return result


def _ci_planner_observation(
    report: BenchmarkReport,
    tasks,
) -> dict:
    task_map = {task.task_id: task for task in tasks}
    rows = _task_rows(report)
    timelines = [
        _timeline(task_map[result.task_id], report, result)
        for result in report.results
    ]
    ci = [
        item
        for item in timelines
        if any(event["tool"] in CI_TOOLS for event in item["timeline"])
    ]
    no_ci = [item for item in timelines if item not in ci]

    def summarize(group):
        return {
            "task_runs": len(group),
            "success_rate": (
                sum(item["success"] for item in group) / len(group)
                if group
                else 0.0
            ),
            "first_relevant_step": _mean_optional(
                [item["first_relevant_file_step"] for item in group]
            ),
            "first_patch_step": _mean_optional(
                [item["first_patch_step"] for item in group]
            ),
            "relevant_to_patch_gap": _mean_optional(
                [item["relevant_to_patch_gap"] for item in group]
            ),
        }

    planner_runs = []
    for result in report.results:
        plan = next(
            (
                event
                for event in result.tool_events
                if event.tool_name == "create_repair_plan"
            ),
            None,
        )
        patch = next(
            (
                event
                for event in result.tool_events
                if event.tool_name == "apply_patch"
            ),
            None,
        )
        if plan:
            plan_index = next(
                index
                for index, event in enumerate(result.tool_events)
                if event.tool_call_index == plan.tool_call_index
            )
            patch_index = next(
                (
                    index
                    for index, event in enumerate(result.tool_events)
                    if event.tool_call_index == patch.tool_call_index
                ),
                len(result.tool_events),
            ) if patch else len(result.tool_events)
            post_plan = result.tool_events[plan_index + 1 : patch_index]
            planner_runs.append(
                {
                    "task_id": result.task_id,
                    "plan_step": plan.step,
                    "first_patch_step": patch.step if patch else None,
                    "plan_to_patch_gap": (
                        patch.step - plan.step if patch else None
                    ),
                    "post_plan_search_calls": sum(
                        event.tool_name == "search_code"
                        for event in post_plan
                    ),
                    "post_plan_read_calls": sum(
                        event.tool_name == "read_file"
                        for event in post_plan
                    ),
                    "post_plan_ci_calls": sum(
                        event.tool_name in CI_TOOLS
                        for event in post_plan
                    ),
                    "success": result.success,
                }
            )
    return {
        "ci_used": summarize(ci),
        "ci_not_used": summarize(no_ci),
        "planner_runs": planner_runs,
        "average_plan_step": _mean_optional(
            [item["plan_step"] for item in planner_runs]
        ),
        "average_plan_to_patch_gap": _mean_optional(
            [item["plan_to_patch_gap"] for item in planner_runs]
        ),
        "average_post_plan_search_calls": _mean_optional(
            [item["post_plan_search_calls"] for item in planner_runs]
        ),
        "average_post_plan_read_calls": _mean_optional(
            [item["post_plan_read_calls"] for item in planner_runs]
        ),
        "average_post_plan_ci_calls": _mean_optional(
            [item["post_plan_ci_calls"] for item in planner_runs]
        ),
        "planner_runs_without_patch": sum(
            item["first_patch_step"] is None for item in planner_runs
        ),
    }


def _step_budget_records(
    report_30: BenchmarkReport,
    run_2: BenchmarkReport,
) -> list[dict]:
    rows_20 = {row["task_id"]: row for row in _task_rows(run_2)}
    tasks_by_id = {task.task_id: task for task in report_30.tasks}
    records = []
    for result in report_30.results:
        row_30 = _timeline(
            tasks_by_id[result.task_id],
            report_30,
            result,
        )
        row_20 = rows_20[result.task_id]
        after_step_20 = [
            event
            for event in row_30["timeline"]
            if event["step"] > 20
            and event["tool"] in EXPLORATION_TOOLS
        ]
        if result.success:
            classification = "budget_limited"
        elif row_30["patch_attempted"]:
            classification = "late_patch_failure"
        else:
            classification = "strategy_limited"
        records.append(
            {
                "task_id": result.task_id,
                "success_at_20": False,
                "success_at_30": result.success,
                "classification": classification,
                "first_relevant_file_step_at_30": row_30[
                    "first_relevant_file_step"
                ],
                "first_patch_step_at_30": row_30["first_patch_step"],
                "relevant_to_patch_gap_at_30": row_30[
                    "relevant_to_patch_gap"
                ],
                "patch_only_after_step_20": bool(
                    row_30["first_patch_step"]
                    and row_30["first_patch_step"] > 20
                ),
                "exploration_calls_after_step_20": len(after_step_20),
                "repeated_exploration_calls_after_step_20": sum(
                    event["redundant_exploration"]
                    for event in after_step_20
                ),
                "exploration_waste_ratio": row_30[
                    "exploration_waste_ratio"
                ],
                "extra_llm_calls": result.llm_calls - row_20["llm_calls"],
                "extra_tool_calls": (
                    result.agent_tool_calls - (row_20.get("tool_calls") or 0)
                ),
                "extra_tokens": (
                    (result.total_tokens or 0)
                    - (row_20.get("total_tokens") or 0)
                ),
                "extra_duration": (
                    result.duration_seconds
                    - row_20.get("duration_seconds", 0)
                ),
                "timeline": row_30,
            }
        )
    return records


def _build_phase5_5_report(
    *,
    phase_dir: Path,
    source_manifest: dict,
    run1: dict[AgentProfile, BenchmarkReport],
    run2: dict[AgentProfile, BenchmarkReport],
    stability: dict,
    stability_summary: dict,
    timelines: list[dict],
    conversion: dict,
    comparison_groups: dict,
    ci_planner: dict,
    step_records: list[dict],
    tasks,
) -> dict:
    by_profile = {
        profile.value: {
            "run_1": _summary(run1[profile]),
            "run_2": _summary(run2[profile]),
            "combined": _combined_metrics([run1[profile], run2[profile]]),
            "stability": stability_summary[profile.value],
        }
        for profile in PROFILES
    }
    step_summary = {
        "stable_fail_count": len(step_records),
        "recovered_at_30": sum(
            item["success_at_30"] for item in step_records
        ),
        "still_failed": sum(
            not item["success_at_30"] for item in step_records
        ),
        "budget_limited": sum(
            item["classification"] == "budget_limited"
            for item in step_records
        ),
        "strategy_limited": sum(
            item["classification"] == "strategy_limited"
            for item in step_records
        ),
        "late_patch_failure": sum(
            item["classification"] == "late_patch_failure"
            for item in step_records
        ),
        "extra_tokens": sum(item["extra_tokens"] for item in step_records),
        "extra_llm_calls": sum(
            item["extra_llm_calls"] for item in step_records
        ),
        "extra_tool_calls": sum(
            item["extra_tool_calls"] for item in step_records
        ),
        "extra_duration": sum(
            item["extra_duration"] for item in step_records
        ),
        "repeated_exploration_calls_after_step_20": sum(
            item["repeated_exploration_calls_after_step_20"]
            for item in step_records
        ),
    }
    payload = {
        "schema_version": 1,
        "phase": "5.5",
        "benchmark": "defects4j_v1",
        "source": source_manifest,
        "profiles": by_profile,
        "stability": stability,
        "timelines": timelines,
        "conversion": conversion,
        "success_vs_failure": comparison_groups,
        "ci_planner": ci_planner,
        "step_budget": {
            "summary": step_summary,
            "records": step_records,
        },
    }
    _write_json(phase_dir / "stability_report.json", {
        "profiles": by_profile,
        "tasks": stability,
    })
    _write_json(phase_dir / "search_to_patch_trace.json", timelines)
    _write_json(
        phase_dir / "step_budget_experiment.json",
        payload["step_budget"],
    )
    _write_json(phase_dir / "experiment.json", payload)
    _write_json(phase_dir / "phase5_5_report.json", payload)
    _write(
        phase_dir / "phase5_5_report.md",
        render_phase5_5_report(payload, tasks),
    )
    return payload


def render_phase5_5_report(payload: dict, tasks) -> str:
    profiles = payload["profiles"]
    lines = [
        "# Phase 5.5 Stability & Search-to-Patch Report",
        "",
        "## 1. Stability",
        "",
        "| Profile | Run | Success | Stable Pass | Stable Fail | Unstable |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for profile in PROFILES:
        value = profiles[profile.value]
        for run in ("run_1", "run_2"):
            summary = value[run]
            lines.append(
                f"| {profile.value} | {run} | "
                f"{summary['passed']}/{summary['total']} "
                f"({summary['success_rate'] * 100:.1f}%) | "
                f"- | - | - |"
            )
        combined = value["combined"]
        stability = value["stability"]
        lines.append(
            f"| {profile.value} | combined | "
            f"{combined['passed']}/{combined['task_runs']} "
            f"({combined['success_rate'] * 100:.1f}%) | "
            f"{stability['stable_pass']} | {stability['stable_fail']} | "
            f"{stability['unstable']} |"
        )
    lines.extend(
        [
            "",
            "| Task | Basic R1 | Basic R2 | Full R1 | Full R2 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    basic = payload["stability"]["basic"]
    full = payload["stability"]["full"]
    for task in tasks:
        b = basic[task.task_id]
        f = full[task.task_id]
        mark = lambda value: "PASS" if value else "FAIL"
        lines.append(
            f"| {task.task_id} | {mark(b['run_1_success'])} | "
            f"{mark(b['run_2_success'])} | {mark(f['run_1_success'])} | "
            f"{mark(f['run_2_success'])} |"
        )
    full_step_stable = [
        task_id
        for task_id, item in full.items()
        if item["classification"] == "stable_fail"
        and item["run_1_failure_category"] == "step_limit"
        and item["run_2_failure_category"] == "step_limit"
    ]
    full_unstable = [
        task_id
        for task_id, item in full.items()
        if item["classification"] == "unstable"
    ]
    full_run_2_failures = Counter(
        item["run_2_failure_category"]
        for item in full.values()
        if item["run_2_failure_category"] is not None
    )
    lines.extend(
        [
            "",
            "## 2. Failure Stability",
            "",
            f"Full stable step-limit failures: {len(full_step_stable)}. "
            "The detailed failure categories are in `stability_report.json`.",
            (
                "Run 2 Full failure categories: "
                + ", ".join(
                    f"{name}={count}"
                    for name, count in sorted(full_run_2_failures.items())
                )
                + "."
            ),
            (
                "Full unstable tasks: "
                + (
                    ", ".join(f"`{task_id}`" for task_id in full_unstable)
                    if full_unstable
                    else "none"
                )
                + "."
            ),
            "",
            "## 3. Search-to-Patch Timeline",
            "",
            "The complete per-step timeline is in `search_to_patch_trace.json`. "
            "A representative Run 2 Full no-patch failure:",
            "",
        ]
    )
    full_run_2 = [
        item
        for item in payload["timelines"]
        if item["run"] == "run_2" and item["profile"] == "full"
    ]
    no_patch_failures = [
        item
        for item in full_run_2
        if not item["success"] and not item["patch_attempted"]
    ]
    example = next(
        (
            item
            for item in no_patch_failures
            if item["exploration_without_commitment"]
        ),
        None,
    )
    if example:
        lines.append(f"`{example['task_id']}`")
        lines.append("")
        for event in example["timeline"]:
            target = event["target_file"] or ""
            lines.append(
                f"- Step {event['step']}: `{event['tool']}` {target} "
                f"({event['result_size']} chars)"
            )
        lines.append(
            f"- END: step_limit; first relevant file step "
            f"{example['first_relevant_file_step']}, first focused read near "
            f"the evaluator change step "
            f"{example['first_read_near_ground_truth_change_step'] or 'N/A'}, "
            "no patch."
        )
    lines.extend(
        [
            "",
            "| Task | Located Step | Focused Change Read | Post Search | "
            "Post Read | Post CI | Repeated Exploration | Last Five Tools |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for item in no_patch_failures:
        last_tools = " → ".join(
            f"S{event['step']}:{event['tool']}"
            for event in item["last_five_steps"]
        )
        lines.append(
            f"| {item['task_id']} | {item['first_relevant_file_step']} | "
            f"{item['first_read_near_ground_truth_change_step'] or 'N/A'} | "
            f"{item['post_relevant_search_calls']} | "
            f"{item['post_relevant_read_calls']} | "
            f"{item['post_relevant_ci_calls']} | "
            f"{item['redundant_exploration_calls']}/"
            f"{item['total_exploration_calls']} | {last_tools} |"
        )
    lines.extend(
        [
            "",
            "A focused change read means a ranged read within 20 lines of an "
            "evaluator-only changed hunk. Whole-file reads are not counted as "
            "focused reads. Ground-truth ranges are never passed to the Agent.",
        ]
    )
    lines.extend(["", "## 4. Conversion Metrics", "", (
        "| Profile | Located File | Any Patch | Patch After Localization | Patch Conversion | "
        "Success Conversion | Avg First Patch | Avg Relevant→Patch Gap |"
    ), "|---|---:|---:|---:|---:|---:|---:|---:|"])
    for profile in PROFILES:
        value = payload["conversion"][profile.value]
        lines.append(
            f"| {profile.value} | {value['runs_located_relevant_file']}/"
            f"{value['task_runs']} | "
            f"{value['runs_with_any_patch']} | "
            f"{value['runs_with_patch_after_localization']} | "
            f"{value['patch_conversion_rate'] * 100:.1f}% | "
            f"{value['successful_patch_conversion'] * 100:.1f}% | "
            f"{value['average_first_patch_step'] or 0:.1f} | "
            f"{value['average_relevant_to_patch_gap'] or 0:.1f} |"
        )
    lines.extend(
        [
            "",
            "These are project diagnostic metrics, not standard academic metrics.",
            "",
            "## 5. Repeated Exploration",
            "",
            "| Profile | Run | Repeated Reads | Repeated Searches | Repeated CI | "
            "Repeated Directories | Waste Ratio |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for profile in PROFILES:
        for run in ("run_1", "run_2"):
            items = [
                item
                for item in payload["timelines"]
                if item["profile"] == profile.value and item["run"] == run
            ]
            query_items = [
                item
                for item in items
                if item.get("query_telemetry_available")
            ]
            lines.append(
                f"| {profile.value} | {run} | "
                f"{sum(item['repeated_read_calls'] for item in items)} | "
                f"{sum(item['repeated_search_calls'] for item in query_items)} | "
                f"{sum(item['repeated_ci_calls'] for item in query_items)} | "
                f"{sum(item['repeated_directory_calls'] for item in items)} | "
                f"{_mean_percent([item['exploration_waste_ratio'] for item in query_items])} |"
            )
    lines.extend(
        [
            "",
            "Run 1 did not persist query strings, so repeated search/CI counts "
            "and waste ratio are unavailable there. Read and directory "
            "repetition is valid in both runs.",
            "",
            "## 6. Success vs Failure",
            "",
            "Run 2 comparison:",
            "",
            "| Profile | Group | Relevant Step | First Patch | Gap | "
            "Exploration Calls | Repeat Ratio | Patch Rate |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for profile in PROFILES:
        for group in ("success", "failure"):
            value = payload["success_vs_failure"][profile.value][group]
            lines.append(
                f"| {profile.value} | {group} | "
                f"{_fmt(value['first_relevant_step'])} | "
                f"{_fmt(value['first_patch_step'])} | "
                f"{_fmt(value['relevant_to_patch_gap'])} | "
                f"{_fmt(value['exploration_calls'])} | "
                f"{_fmt(value['repeat_ratio'], percent=True)} | "
                f"{_fmt(value['patch_rate'], percent=True)} |"
            )
    full_success = payload["success_vs_failure"]["full"]["success"]
    full_failure = payload["success_vs_failure"]["full"]["failure"]
    lines.extend(
        [
            "",
            "Run 2 Full successes did not localize earlier "
            f"({_fmt(full_success['first_relevant_step'])} vs "
            f"{_fmt(full_failure['first_relevant_step'])}), but they converted "
            "localization to a first patch with a shorter gap "
            f"({_fmt(full_success['relevant_to_patch_gap'])} vs "
            f"{_fmt(full_failure['relevant_to_patch_gap'])} steps), used fewer "
            f"exploration calls ({_fmt(full_success['exploration_calls'])} vs "
            f"{_fmt(full_failure['exploration_calls'])}), and had patch rate "
            f"{_fmt(full_success['patch_rate'], percent=True)} vs "
            f"{_fmt(full_failure['patch_rate'], percent=True)}.",
        ]
    )
    budget = payload["step_budget"]["summary"]
    lines.extend(
        [
            "",
            "## 7. Step Budget Sensitivity",
            "",
            f"- Stable Full step-limit failures at 20: "
            f"{budget['stable_fail_count']}",
            f"- Recovered at 30: {budget['recovered_at_30']}",
            f"- Still failed at 30: {budget['still_failed']}",
            f"- Aggregate token delta: {budget['extra_tokens']}",
            f"- Aggregate LLM-call delta: {budget['extra_llm_calls']}",
            f"- Aggregate tool-call delta: {budget['extra_tool_calls']}",
            f"- Aggregate duration delta: {budget['extra_duration']:.1f}s",
            f"- Repeated exploration after step 20: "
            f"{budget['repeated_exploration_calls_after_step_20']}",
            "",
            "| Task | 20 | 30 | Classification | Relevant @30 | Patch @30 | "
            "Patch After 20 | Repeated After 20 | Δ Tokens | Δ LLM | Δ Tool | "
            "Δ Duration |",
            "|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in payload["step_budget"]["records"]:
        lines.append(
            f"| {item['task_id']} | FAIL | "
            f"{'PASS' if item['success_at_30'] else 'FAIL'} | "
            f"{item['classification']} | "
            f"{item['first_relevant_file_step_at_30'] or 'N/A'} | "
            f"{item['first_patch_step_at_30'] or 'N/A'} | "
            f"{'YES' if item['patch_only_after_step_20'] else 'NO'} | "
            f"{item['repeated_exploration_calls_after_step_20']} | "
            f"{item['extra_tokens']} | {item['extra_llm_calls']} | "
            f"{item['extra_tool_calls']} | {item['extra_duration']:.1f}s |"
        )
    recovered_late_patch = sum(
        item["success_at_30"]
        and (item["first_patch_step_at_30"] or 0) > 20
        for item in payload["step_budget"]["records"]
    )
    late_first_patch_tasks = [
        item["task_id"]
        for item in payload["step_budget"]["records"]
        if (item["first_patch_step_at_30"] or 0) > 20
    ]
    lines.extend(
        [
            "",
            "Full@30 is an independent rerun, not a continuation of the 20-step "
            "context. Per-run deltas can therefore be negative.",
            f"Recovered runs whose first patch occurred after step 20: "
            f"{recovered_late_patch}/{budget['recovered_at_30']}. "
            "Thus the observed 30-step recovery is budget sensitivity across "
            "whole trajectories, not direct evidence that every recovered patch "
            "was cut off at step 20.",
            "Tasks whose first patch appeared only after step 20: "
            + (
                ", ".join(f"`{task_id}`" for task_id in late_first_patch_tasks)
                if late_first_patch_tasks
                else "none"
            )
            + ".",
        ]
    )
    strategy_limited = [
        item
        for item in payload["step_budget"]["records"]
        if item["classification"] == "strategy_limited"
    ]
    late_patch = [
        item
        for item in payload["step_budget"]["records"]
        if item["classification"] == "late_patch_failure"
    ]
    budget_limited = [
        item
        for item in payload["step_budget"]["records"]
        if item["classification"] == "budget_limited"
    ]
    stable_fail_ids = {
        task_id
        for task_id, item in full.items()
        if item["classification"] == "stable_fail"
    }
    stable_fail_run_2 = [
        item
        for item in full_run_2
        if item["task_id"] in stable_fail_ids
    ]
    stable_no_patch_at_20 = [
        item for item in stable_fail_run_2 if not item["patch_attempted"]
    ]
    stable_located_at_20 = sum(
        item["first_relevant_file_step"] is not None
        for item in stable_fail_run_2
    )
    unresolved = len(strategy_limited) + len(late_patch)
    if len(budget_limited) >= max(len(strategy_limited), len(late_patch)):
        root_cause = (
            "This is not a single-cause failure. The benchmark is sensitive to "
            "the 20-step setting: "
            f"{len(budget_limited)} of "
            f"{payload['step_budget']['summary']['stable_fail_count']} stable "
            "step-limit failures passed at 30 steps. However, "
            f"{unresolved} still failed, and at 20 steps "
            f"{stable_located_at_20}/{len(stable_fail_run_2)} stable failures "
            "had located a relevant file while "
            f"{len(stable_no_patch_at_20)} had produced no patch. The largest "
            "single classification is budget sensitivity, but late commitment "
            "and residual patch reasoning account for the other half. Because "
            "Full@30 is an independent rerun, this does not establish that "
            "uniformly raising the cap fixes the underlying convergence issue."
        )
        recommendation = (
            "Step Budget Adjustment is best supported by the 5/10 observed "
            "recoveries at 30 steps. This phase does not apply the change."
        )
    elif len(late_patch) >= len(strategy_limited):
        root_cause = (
            "Localization is not the bottleneck. With 30 steps, the dominant "
            f"residual failure is patch reasoning: {len(late_patch)} tasks "
            "eventually patch but still fail."
        )
        recommendation = (
            "Patch Reasoning is the best-supported next intervention because "
            "late-patch failures dominate the remaining stable failures."
        )
    else:
        root_cause = (
            "The dominant residual failure is exploration without commitment: "
            f"{len(strategy_limited)} stable step-limit tasks still produced "
            "no patch at 30 steps."
        )
        recommendation = (
            "Search Budget / Commitment Mechanism is the best-supported next "
            "intervention because extra steps did not convert the dominant "
            "no-patch failures."
        )
    lines.extend(
        [
            "",
            "## 8. Budget vs Strategy",
            "",
            f"- `budget_limited`: {budget['budget_limited']}",
            f"- `strategy_limited`: {budget['strategy_limited']}",
            f"- `late_patch_failure`: {budget['late_patch_failure']}",
            "",
            "## 9. CI / Planner Observations",
            "",
        ]
    )
    ci = payload["ci_planner"]
    lines.extend(
        [
            f"- Full with CI: {ci['ci_used']['task_runs']} task-runs, "
            f"success {ci['ci_used']['success_rate'] * 100:.1f}%, "
            f"avg relevant→patch gap {_fmt(ci['ci_used']['relevant_to_patch_gap'])}.",
            f"- Full without CI: {ci['ci_not_used']['task_runs']} task-runs, "
            f"success {ci['ci_not_used']['success_rate'] * 100:.1f}%, "
            f"avg relevant→patch gap {_fmt(ci['ci_not_used']['relevant_to_patch_gap'])}.",
            f"- Planner runs: {len(ci['planner_runs'])}; avg plan step "
            f"{_fmt(ci['average_plan_step'])}; avg plan→patch gap "
            f"{_fmt(ci['average_plan_to_patch_gap'])}; "
            f"planner runs without a patch: "
            f"{ci['planner_runs_without_patch']}.",
            f"- After the first plan, average additional search/read/CI calls: "
            f"{_fmt(ci['average_post_plan_search_calls'])}/"
            f"{_fmt(ci['average_post_plan_read_calls'])}/"
            f"{_fmt(ci['average_post_plan_ci_calls'])}.",
            "",
            (
                "When invoked, the planner usually acted as a commitment "
                "point: "
                f"{sum((item['plan_to_patch_gap'] == 1) for item in ci['planner_runs'])}/"
                f"{len(ci['planner_runs'])} plans were followed by a patch on "
                "the next step, but the first plan arrived late on average "
                f"(step {_fmt(ci['average_plan_step'])})."
            ),
            "- These comparisons are descriptive only; the sample is too small "
            "to claim causality.",
            "",
            "## 10. Root Cause",
            "",
            root_cause,
            "",
            "## 11. Recommended Next Step",
            "",
            recommendation,
            "This phase remains diagnostic; no Agent behavior has been changed.",
            "",
        ]
    )
    return "\n".join(lines)


def _fmt(value: float | None, *, percent: bool = False) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:.1f}%" if percent else f"{value:.1f}"


def _mean_percent(values: list[float]) -> str:
    return "N/A" if not values else f"{(sum(values) / len(values)) * 100:.1f}%"


def run_phase5_5(
    *,
    tasks_file: str | Path = DEFAULT_TASKS_FILE,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
) -> Path:
    tasks_path = Path(tasks_file).expanduser().resolve()
    phase55_dir = _phase_dir(output_dir)
    phase55_dir.mkdir(parents=True, exist_ok=True)
    tasks = load_external_tasks(tasks_path)
    phase5_dir = phase55_dir.parent / PHASE5_DIR_NAME
    phase5_manifest_path = phase5_dir / "source_manifest.json"
    phase5_manifest = json.loads(
        phase5_manifest_path.read_text(encoding="utf-8")
    )
    manifest = _source_manifest(
        tasks_path,
        tasks,
        phase5_manifest=phase5_manifest,
        phase5_5_dir=phase55_dir,
    )
    _write_json(phase55_dir / "source_manifest.json", manifest)
    run1_paths = _copy_run_1(phase5_dir, phase55_dir)
    run1 = _profile_reports(run1_paths)
    run2 = _run_confirmation(tasks, phase55_dir)
    stability = _stability(run1, run2)
    stability_summary = _stability_summary(stability)

    task_map = {task.task_id: task for task in tasks}
    timelines = []
    for run_name, reports in (("run_1", run1), ("run_2", run2)):
        for profile in PROFILES:
            report = reports[profile]
            for result in report.results:
                timeline = _timeline(
                    task_map[result.task_id],
                    report,
                    result,
                )
                timeline["run"] = run_name
                timelines.append(timeline)

    stable_fail_full = [
        task_id
        for task_id, item in stability["full"].items()
        if item["classification"] == "stable_fail"
        and item["run_1_failure_category"] == "step_limit"
        and item["run_2_failure_category"] == "step_limit"
    ]
    step_dir = phase55_dir / "step_budget_experiment"
    step_paths = _report_paths(step_dir)
    if len(step_paths) == 1:
        report_30 = BenchmarkReport.model_validate_json(
            step_paths[0].read_text(encoding="utf-8")
        )
        if len(report_30.results) != len(stable_fail_full):
            report_30, _ = run_step_budget_diagnostic(
                tasks,
                phase55_dir,
                stable_fail_full,
            )
    else:
        report_30, _ = run_step_budget_diagnostic(
            tasks,
            phase55_dir,
            stable_fail_full,
        )
    step_records = _step_budget_records(report_30, run2[AgentProfile.FULL])
    conversion = _conversion_metrics(
        {
            profile: [run1[profile], run2[profile]]
            for profile in PROFILES
        },
        tasks,
    )
    comparison_groups = _comparison_groups(timelines, run="run_2")
    ci_planner = _ci_planner_observation(
        run2[AgentProfile.FULL],
        tasks,
    )
    payload = _build_phase5_5_report(
        phase_dir=phase55_dir,
        source_manifest=manifest,
        run1=run1,
        run2=run2,
        stability=stability,
        stability_summary=stability_summary,
        timelines=timelines,
        conversion=conversion,
        comparison_groups=comparison_groups,
        ci_planner=ci_planner,
        step_records=step_records,
        tasks=tasks,
    )
    print(f"Phase 5.5 report: {phase55_dir / 'phase5_5_report.md'}")
    return phase55_dir / "phase5_5_report.md"


def regenerate_phase5_5_report(
    *,
    tasks_file: str | Path = DEFAULT_TASKS_FILE,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
) -> Path:
    """Rebuild report artifacts from completed runs without invoking the Agent."""
    tasks_path = Path(tasks_file).expanduser().resolve()
    phase55_dir = _phase_dir(output_dir)
    tasks = load_external_tasks(tasks_path)
    source_manifest_path = phase55_dir / "source_manifest.json"
    if not source_manifest_path.is_file():
        raise RuntimeError("缺少 source_manifest.json")
    source_manifest = json.loads(
        source_manifest_path.read_text(encoding="utf-8")
    )
    run1_paths = {
        profile.value: phase55_dir / "run_1" / profile.value / "report.json"
        for profile in PROFILES
    }
    run1 = _profile_reports(run1_paths)
    run2 = {}
    for profile in PROFILES:
        report_path = (
            phase55_dir
            / "confirmation_run_2"
            / f"phase5_5_{profile.value}_run2"
            / "report.json"
        )
        if not report_path.is_file():
            raise RuntimeError(f"缺少 Run 2 report: {report_path}")
        report = BenchmarkReport.model_validate_json(
            report_path.read_text(encoding="utf-8")
        )
        if len(report.results) != 15:
            raise RuntimeError(f"Run 2 report 不完整: {report_path}")
        run2[profile] = report
    step_paths = _report_paths(phase55_dir / "step_budget_experiment")
    if len(step_paths) != 1:
        raise RuntimeError(
            f"需要恰好一个 30-step report，实际 {len(step_paths)}"
        )
    report_30 = BenchmarkReport.model_validate_json(
        step_paths[0].read_text(encoding="utf-8")
    )
    stability = _stability(run1, run2)
    task_map = {task.task_id: task for task in tasks}
    timelines = []
    for run_name, reports in (("run_1", run1), ("run_2", run2)):
        for profile in PROFILES:
            report = reports[profile]
            for result in report.results:
                timeline = _timeline(
                    task_map[result.task_id],
                    report,
                    result,
                )
                timeline["run"] = run_name
                timelines.append(timeline)
    step_records = _step_budget_records(report_30, run2[AgentProfile.FULL])
    conversion = _conversion_metrics(
        {
            profile: [run1[profile], run2[profile]]
            for profile in PROFILES
        },
        tasks,
    )
    _build_phase5_5_report(
        phase_dir=phase55_dir,
        source_manifest=source_manifest,
        run1=run1,
        run2=run2,
        stability=stability,
        stability_summary=_stability_summary(stability),
        timelines=timelines,
        conversion=conversion,
        comparison_groups=_comparison_groups(timelines, run="run_2"),
        ci_planner=_ci_planner_observation(run2[AgentProfile.FULL], tasks),
        step_records=step_records,
        tasks=tasks,
    )
    print(f"Phase 5.5 report: {phase55_dir / 'phase5_5_report.md'}")
    return phase55_dir / "phase5_5_report.md"
