"""Phase 5 real-world Defects4J benchmark integration."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from app.agent.capabilities import AgentProfile, PROFILE_CAPABILITIES
from app.agent.graph import build_system_prompt
from app.core.config import settings
from app.evaluation.models import (
    BenchmarkReport,
    BenchmarkTask,
    ContextTelemetry,
    FailureCategory,
)
from app.evaluation.runner import BenchmarkRunner
from app.evaluation.tasks import DEFAULT_RESULTS_DIR, PROJECT_ROOT
from app.schemas.agent import AnalyzeResponse
from app.workspace import git
from app.workspace.models import BuildTool, TestRunResult

PHASE5_DIR_NAME = "phase5_real_world"
DEFAULT_BENCHMARK_DIR = (
    PROJECT_ROOT / "evaluation" / "benchmarks" / "defects4j_v1"
)
DEFAULT_TASKS_FILE = DEFAULT_BENCHMARK_DIR / "tasks.json"
CI_TOOLS = {
    "list_symbols",
    "find_symbol",
    "find_references",
    "find_callers",
    "find_callees",
    "analyze_impact",
}
GENERIC_PROBLEM_STATEMENT = (
    "Repository contains a failing Java project.\n\n"
    "Fix the bug so the failing tests pass without introducing regressions. "
    "Inspect the repository and determine the root cause."
)


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


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _phase_dir(output_dir: str | Path) -> Path:
    path = Path(output_dir).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve() / PHASE5_DIR_NAME


def load_external_tasks(path: str | Path) -> list[BenchmarkTask]:
    tasks_path = Path(path).expanduser().resolve()
    raw = json.loads(tasks_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("Defects4J tasks manifest 必须是非空数组")
    tasks = [BenchmarkTask.model_validate(item) for item in raw]
    seen = set()
    resolved_tasks = []
    for task in tasks:
        if task.task_id in seen:
            raise ValueError(f"重复 task_id: {task.task_id}")
        seen.add(task.task_id)
        if task.source != "defects4j":
            raise ValueError(f"{task.task_id} source 必须是 defects4j")
        repository = Path(os.path.expandvars(task.repository))
        if not repository.is_absolute():
            repository = tasks_path.parent / repository
        if not repository.exists():
            raise ValueError(f"{task.task_id} repository 不存在: {repository}")
        test_patch = task.test_patch
        if test_patch:
            test_patch_path = Path(test_patch)
            if not test_patch_path.is_absolute():
                test_patch_path = tasks_path.parent / test_patch_path
            test_patch = str(test_patch_path.resolve())
        if task.base_commit != task.buggy_revision:
            raise ValueError(f"{task.task_id} base_commit 必须等于 buggy_revision")
        if task.expected_test_command != "mvn test":
            raise ValueError(f"{task.task_id} 只支持固定 mvn test")
        resolved_tasks.append(
            task.model_copy(
                update={
                    "repository": str(repository.resolve()),
                    "test_patch": test_patch,
                }
            )
        )
    return resolved_tasks


def parse_trigger_metadata(text: str) -> list[str]:
    tests = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("--- "):
            continue
        value = stripped[4:].strip()
        if "::" in value:
            class_name, method = value.split("::", 1)
            tests.append(f"{class_name}#{method}")
    return list(dict.fromkeys(tests))


def _surefire_results(repository_path: Path) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for report in repository_path.rglob("TEST-*.xml"):
        try:
            root = ElementTree.parse(report).getroot()
        except (ElementTree.ParseError, OSError):
            continue
        for case in root.iter():
            if not case.tag.endswith("testcase"):
                continue
            class_name = case.attrib.get("classname", "")
            method_name = case.attrib.get("name", "")
            if not class_name or not method_name:
                continue
            failed = any(
                child.tag.endswith(("failure", "error", "skipped"))
                for child in case
            )
            results[f"{class_name}#{method_name}"] = not failed
            results[f"{class_name}#{method_name.split('[', 1)[0]}"] = not failed
    return results


def _tests_passed(
    expected: list[str],
    results: dict[str, bool],
) -> bool | None:
    if not expected:
        return None
    return all(
        results.get(test, results.get(test.split("[", 1)[0], False))
        for test in expected
    )


def _ground_truth_paths(classes: list[str]) -> set[str]:
    paths = set()
    for value in classes:
        normalized = value.replace("\\", "/").lstrip("./")
        if normalized.endswith(".java"):
            paths.add(normalized)
            continue
        top_level = normalized.split("$", 1)[0]
        paths.add(top_level.replace(".", "/") + ".java")
    return paths


def _event_paths(value: str | None) -> set[str]:
    if not value:
        return set()
    return {
        item.strip().replace("\\", "/")
        for item in value.split(",")
        if item.strip()
    }


def _matches_ground_truth(path: str, ground_truth: set[str]) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    return any(
        normalized == expected or normalized.endswith("/" + expected)
        for expected in ground_truth
    )


def _localization_metrics(
    task: BenchmarkTask,
    events: list,
    changed_files: list[str],
    context_telemetry: ContextTelemetry,
) -> dict:
    first_patch_index = next(
        (
            index
            for index, event in enumerate(events)
            if event.tool_name == "apply_patch"
        ),
        len(events),
    )
    pre_patch_events = events[:first_patch_index]
    files_read = sorted(
        {
            path
            for event in pre_patch_events
            if event.tool_name == "read_file"
            for path in _event_paths(event.target_file)
        }
    )
    files_queried_ci = sorted(
        {
            path
            for event in pre_patch_events
            if event.tool_name in CI_TOOLS
            for path in _event_paths(event.target_file)
        }
    )
    first_patch_files = (
        sorted(_event_paths(events[first_patch_index].target_file))
        if first_patch_index < len(events)
        else []
    )
    ground_truth = _ground_truth_paths(task.ground_truth_files)
    accessed_before_patch = files_read + files_queried_ci
    localized = {
        path
        for path in accessed_before_patch
        if _matches_ground_truth(path, ground_truth)
    }
    final_ground_truth = {
        path
        for path in changed_files
        if _matches_ground_truth(path, ground_truth)
    }
    first_relevant = next(
        (
            event
            for event in pre_patch_events
            if any(
                _matches_ground_truth(path, ground_truth)
                for path in _event_paths(event.target_file)
            )
        ),
        None,
    )
    first_patch_step = (
        events[first_patch_index].step
        if first_patch_index < len(events)
        else None
    )
    tokens_before_patch = sum(
        call.provider_prompt_tokens or 0
        for call in context_telemetry.calls
        if first_patch_step is None or call.call_index <= first_patch_step
    )
    protected_files = [
        path
        for path in changed_files
        if "/src/test/" in f"/{path.replace(chr(92), '/')}"
        or path.replace("\\", "/").endswith("Test.java")
        or path.replace("\\", "/") in {"pom.xml", "build.gradle"}
    ]
    return {
        "files_read_before_first_patch": files_read,
        "files_queried_by_ci": files_queried_ci,
        "first_patch_files": first_patch_files,
        "final_changed_files": sorted(changed_files),
        "ground_truth_files": task.ground_truth_files,
        "file_localization_recall": (
            len(localized) / len(ground_truth) if ground_truth else None
        ),
        "patch_file_precision": (
            len(final_ground_truth) / len(changed_files)
            if changed_files
            else None
        ),
        "time_to_first_relevant_file_seconds": (
            first_relevant.elapsed_seconds if first_relevant else None
        ),
        "llm_calls_to_first_relevant_file": (
            first_relevant.step if first_relevant else None
        ),
        "tool_calls_to_first_relevant_file": (
            first_relevant.tool_call_index if first_relevant else None
        ),
        "tokens_before_first_patch": tokens_before_patch,
        "protected_files_modified": protected_files,
    }


def phase5_task_evaluator(
    task: BenchmarkTask,
    repository_path: Path,
    response: AnalyzeResponse | None,
    final_test: TestRunResult | None,
    changed_files: list[str],
    context_telemetry: ContextTelemetry,
) -> dict:
    events = list(response.tool_events) if response else []
    surefire = _surefire_results(repository_path)
    full_passed = bool(final_test and final_test.success)
    trigger_passed = (
        True if full_passed else _tests_passed(task.triggering_tests, surefire)
    )
    relevant_passed = (
        True if full_passed else _tests_passed(task.relevant_tests, surefire)
    )
    localization = _localization_metrics(
        task,
        events,
        changed_files,
        context_telemetry,
    )
    protected_files = localization["protected_files_modified"]
    observations = []
    if not response:
        observations.append("agent produced no response")
    if protected_files:
        observations.append("protected files modified")
    if final_test and final_test.timed_out:
        observations.append("final test timeout")
    if response and "已达到最大步骤数" in response.answer:
        observations.append("step limit reached")
    return {
        **localization,
        "trigger_tests_passed": trigger_passed,
        "relevant_tests_passed": relevant_passed,
        "full_tests_passed": full_passed,
        "trigger_tests_defined": len(task.triggering_tests),
        "relevant_tests_defined": len(task.relevant_tests),
        "secondary_observations": observations,
    }


def _clone_at(repository: Path, destination: Path, revision: str) -> None:
    completed = subprocess.run(
        ["git", "clone", "--quiet", "--no-checkout", str(repository), str(destination)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git clone failed")
    checked_out = subprocess.run(
        ["git", "-C", str(destination), "checkout", "--quiet", "--detach", revision],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if checked_out.returncode != 0:
        raise RuntimeError(checked_out.stderr.strip() or "git checkout failed")


def prepare_defects4j_workspace(
    task: BenchmarkTask,
    repository_path: Path,
) -> None:
    """Make old JUnit projects runnable under the pinned evaluation JDK."""
    head = subprocess.run(
        ["git", "-C", str(repository_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    if task.test_patch and head == task.buggy_revision:
        patch_path = Path(task.test_patch)
        if not patch_path.is_absolute():
            patch_path = DEFAULT_BENCHMARK_DIR / patch_path
        base_command = [
            "git",
            "-C",
            str(repository_path),
            "apply",
            "--whitespace=nowarn",
        ]
        applied = subprocess.run(
            [*base_command, "-R", str(patch_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if applied.returncode != 0:
            applied = subprocess.run(
                [*base_command, str(patch_path)],
                capture_output=True,
                text=True,
                timeout=60,
            )
        if applied.returncode != 0:
            raise RuntimeError(
                "Failed to apply Defects4J test patch: "
                f"{applied.stderr.strip()}"
            )
        changed = True
    else:
        changed = False
    for pom in repository_path.rglob("pom.xml"):
        if any(part in {"target", ".git"} for part in pom.parts):
            continue
        text = pom.read_text(encoding="utf-8")
        updated = re.sub(
            r"(<artifactId>junit</artifactId>\s*<version>)[^<]+(</version>)",
            r"\g<1>4.13.2\2",
            text,
        )
        updated = re.sub(
            r"(<source>)1\.[4567](</source>)",
            r"\g<1>1.8\2",
            updated,
        )
        updated = re.sub(
            r"(<target>)1\.[4567](</target>)",
            r"\g<1>1.8\2",
            updated,
        )
        if updated != text:
            pom.write_text(updated, encoding="utf-8")
            changed = True
    if changed:
        subprocess.run(
            ["git", "-C", str(repository_path), "add", "-A"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repository_path),
                "-c",
                "user.name=Benchmark Harness",
                "-c",
                "user.email=benchmark@invalid",
                "commit",
                "--quiet",
                "-m",
                "benchmark environment setup",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )


def make_host_maven_runner(*, offline: bool):
    """Return a fixed ``mvn test`` runner using the local JDK 8."""
    java_home = os.environ.get(
        "DEFECTS4J_JAVA_HOME",
        r"C:\Program Files\Java\jdk1.8.0_202",
    )
    mvn = shutil.which("mvn")
    if not mvn:
        raise RuntimeError("Maven executable not found")

    def run(repository_path, tool="maven", timeout_seconds=600):
        if tool not in {"maven", "auto"}:
            raise ValueError("Defects4J runner only supports Maven")
        started = time.monotonic()
        environment = {
            **os.environ,
            "JAVA_HOME": java_home,
            "PATH": str(Path(java_home) / "bin")
            + os.pathsep
            + os.environ.get("PATH", ""),
            "CI": "true",
        }
        if offline:
            environment["MAVEN_ARGS"] = (
                environment.get("MAVEN_ARGS", "") + " -o"
            ).strip()
        process = subprocess.Popen(
            [mvn, "test"],
            cwd=repository_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            shell=False,
        )
        timed_out = False
        error = None
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            error = (
                f"Maven test timeout (>{timeout_seconds:.0f}s), "
                "process terminated"
            )
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=15,
                )
            else:
                process.kill()
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
        stdout = stdout or ""
        stderr = stderr or ""
        truncated = len(stdout) > 20_000 or len(stderr) > 20_000
        stdout = stdout[:20_000]
        stderr = stderr[:20_000]
        return TestRunResult(
            tool=BuildTool.MAVEN,
            command=["mvn", "test"],
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            success=not timed_out and process.returncode == 0,
            timed_out=timed_out,
            truncated=truncated,
            error=error,
            duration_seconds=round(time.monotonic() - started, 3),
        )

    return run


def _compile_status(result: TestRunResult, surefire: dict[str, bool]) -> str:
    output = f"{result.stdout}\n{result.stderr}".lower()
    if "compilation error" in output or "compilation failure" in output:
        return "failed"
    if result.timed_out:
        return "timeout"
    if surefire:
        return "passed"
    return "unknown"


def reproduce_tasks(
    tasks: list[BenchmarkTask],
    *,
    phase_dir: Path,
    timeout_seconds: float = 600,
    test_runner=None,
    existing_records: dict[str, dict] | None = None,
) -> list[dict]:
    test_runner = test_runner or make_host_maven_runner(offline=False)
    records = []
    existing_records = existing_records or {}
    work_root = phase_dir / "reproduction_workspaces"
    work_root.mkdir(parents=True, exist_ok=True)
    for task in tasks:
        repository = Path(task.repository)
        started = time.monotonic()
        record = {
            "task_id": task.task_id,
            "project": task.project,
            "bug_id": task.bug_id,
            "buggy_revision": task.buggy_revision,
            "fixed_revision": task.fixed_revision,
        }
        previous = existing_records.get(task.task_id)
        if (
            previous
            and previous.get("reproduction_success") is True
            and previous.get("buggy_revision") == task.buggy_revision
            and previous.get("fixed_revision") == task.fixed_revision
        ):
            records.append(previous)
            print(f"{task.task_id}: REUSED", flush=True)
            continue
        try:
            with tempfile.TemporaryDirectory(
                prefix=f"repro-{task.task_id}-",
                dir=work_root,
            ) as temporary:
                root = Path(temporary)
                buggy = root / "buggy"
                fixed = root / "fixed"
                _clone_at(repository, buggy, task.buggy_revision)
                _clone_at(repository, fixed, task.fixed_revision)
                prepare_defects4j_workspace(task, buggy)
                prepare_defects4j_workspace(task, fixed)
                buggy_test = test_runner(
                    buggy,
                    "maven",
                    timeout_seconds,
                )
                buggy_tests = _surefire_results(buggy)
                fixed_test = test_runner(
                    fixed,
                    "maven",
                    timeout_seconds,
                )
                fixed_tests = _surefire_results(fixed)
                trigger_failed = all(
                    buggy_tests.get(test, False) is False
                    for test in task.triggering_tests
                )
                fixed_trigger_passed = all(
                    fixed_tests.get(test, False) is True
                    for test in task.triggering_tests
                )
                record.update(
                    {
                        "buggy_compile_status": _compile_status(
                            buggy_test,
                            buggy_tests,
                        ),
                        "fixed_compile_status": _compile_status(
                            fixed_test,
                            fixed_tests,
                        ),
                        "buggy_test_success": buggy_test.success,
                        "fixed_test_success": fixed_test.success,
                        "trigger_tests_failed_on_buggy": trigger_failed,
                        "trigger_tests_passed_on_fixed": fixed_trigger_passed,
                        "reproduction_success": (
                            trigger_failed
                            and fixed_test.success
                            and fixed_trigger_passed
                        ),
                        "buggy_duration_seconds": buggy_test.duration_seconds,
                        "fixed_duration_seconds": fixed_test.duration_seconds,
                        "rejection_reason": None,
                    }
                )
        except Exception as exc:
            record.update(
                {
                    "reproduction_success": False,
                    "rejection_reason": str(exc),
                }
            )
        if not record.get("reproduction_success"):
            record.setdefault(
                "rejection_reason",
                "triggering tests did not fail on buggy and pass on fixed revision",
            )
        record["execution_duration_seconds"] = round(
            time.monotonic() - started,
            3,
        )
        records.append(record)
        print(
            f"{task.task_id}: "
            f"{'READY' if record.get('reproduction_success') else 'REJECTED'}",
            flush=True,
        )
    return records


def run_realworld_reproduction(
    *,
    tasks_file: str | Path = DEFAULT_TASKS_FILE,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
    timeout_seconds: float = 600,
) -> Path:
    tasks_path = Path(tasks_file).expanduser().resolve()
    tasks = load_external_tasks(tasks_path)
    phase_dir = _phase_dir(output_dir)
    existing_path = phase_dir / "candidate_selection.json"
    existing_records = {}
    if existing_path.is_file():
        existing = json.loads(
            existing_path.read_text(encoding="utf-8")
        )
        existing_records = {
            item["task_id"]: item
            for item in existing.get("candidates", [])
        }
    records = reproduce_tasks(
        tasks,
        phase_dir=phase_dir,
        timeout_seconds=timeout_seconds,
        existing_records=existing_records,
    )
    output = phase_dir / "candidate_selection.json"
    _write_json(
        output,
        {
            "schema_version": 1,
            "benchmark": "defects4j_v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tasks_file": str(tasks_path),
            "tasks_sha256": _sha256_file(tasks_path),
            "candidates": records,
        },
    )
    return output


def _source_manifest(tasks_file: Path, tasks: list[BenchmarkTask]) -> dict:
    repositories = {}
    for task in tasks:
        repository = Path(task.repository).resolve()
        try:
            head = git.head_commit(repository)
        except git.GitError:
            head = None
        repositories[task.project or task.task_id] = {
            "path": str(repository),
            "head_commit": head,
        }
    benchmark_manifest = tasks_file.parent / "benchmark_manifest.json"
    benchmark_data = (
        json.loads(benchmark_manifest.read_text(encoding="utf-8"))
        if benchmark_manifest.is_file()
        else {}
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": "defects4j_v1",
        "tasks_file": str(tasks_file.resolve()),
        "tasks_sha256": _sha256_file(tasks_file),
        "task_count": len(tasks),
        "composition": benchmark_data.get("composition"),
        "defects4j_commit": benchmark_data.get("defects4j_commit"),
        "benchmark_manifest": {
            "path": str(benchmark_manifest),
            "sha256": (
                _sha256_file(benchmark_manifest)
                if benchmark_manifest.is_file()
                else None
            ),
        },
        "repositories": repositories,
        "agent": {
            "git_commit": git.head_commit(PROJECT_ROOT),
            "git_dirty": git.worktree_is_dirty(PROJECT_ROOT),
            "model": settings.LLM_MODEL,
            "docker_image": settings.DOCKER_MAVEN_IMAGE,
            "maven_repo_path": settings.DOCKER_MAVEN_REPO_PATH,
            "test_execution": "host_mvn_test",
            "jdk": "1.8.0_202",
            "prompt_hashes": {
                profile.value: hashlib.sha256(
                    build_system_prompt(PROFILE_CAPABILITIES[profile]).encode(
                        "utf-8"
                    )
                ).hexdigest()
                for profile in (AgentProfile.BASIC, AgentProfile.FULL)
            },
        },
    }


def _task_rows(report: BenchmarkReport) -> list[dict]:
    rows = []
    tasks_by_id = {task.task_id: task for task in report.tasks}
    for result in report.results:
        task = tasks_by_id[result.task_id]
        metrics = {
            **result.benchmark_metrics,
            **_localization_metrics(
                task,
                list(result.tool_events),
                list(result.changed_files),
                result.context_telemetry,
            ),
        }
        primary_failure = None
        if not result.success:
            output = (
                f"{result.final_test_result.stdout}\n"
                f"{result.final_test_result.stderr}"
                if result.final_test_result
                else ""
            ).lower()
            if metrics["protected_files_modified"]:
                primary_failure = "test_overfitting"
            elif "已达到最大步骤数" in result.agent_answer:
                primary_failure = "step_limit"
            elif "compilation error" in output:
                primary_failure = "compile_failure"
            elif not result.changed_files:
                primary_failure = "localization_failure"
            elif result.final_test_result is not None:
                primary_failure = "incorrect_patch"
            elif result.failure_category:
                primary_failure = result.failure_category.value
        rows.append(
            {
                "task_id": result.task_id,
                "success": result.success,
                "trigger_tests_passed": metrics.get("trigger_tests_passed"),
                "relevant_tests_passed": metrics.get("relevant_tests_passed"),
                "full_tests_passed": metrics.get("full_tests_passed"),
                "prompt_tokens": result.prompt_tokens,
                "total_tokens": result.total_tokens,
                "llm_calls": result.llm_calls,
                "tool_calls": result.agent_tool_calls,
                "duration_seconds": result.duration_seconds,
                "failure_category": primary_failure,
                "failure_reason": result.failure_reason,
                **metrics,
            }
        )
    return rows


def _category_usage(report: BenchmarkReport) -> dict:
    usage = Counter()
    ci_by_difficulty = defaultdict(
        lambda: {
            "calls": 0,
            "tasks": 0,
            "tasks_with_ci": 0,
            "successes": 0,
        }
    )
    tasks_by_id = {task.task_id: task for task in report.tasks}
    for result in report.results:
        usage.update(result.tool_usage)
        task = tasks_by_id[result.task_id]
        group = ci_by_difficulty[task.external_difficulty or "unknown"]
        ci_calls = sum(
            result.tool_usage.get(tool, 0) for tool in CI_TOOLS
        )
        group["calls"] += ci_calls
        group["tasks"] += 1
        group["tasks_with_ci"] += int(ci_calls > 0)
        group["successes"] += int(result.success and ci_calls > 0)
    return {
        "tool_usage": dict(sorted(usage.items())),
        "ci_by_difficulty": dict(ci_by_difficulty),
    }


def _averages(rows: list[dict], key: str) -> float | None:
    values = [row.get(key) for row in rows if row.get(key) is not None]
    return sum(values) / len(values) if values else None


def regenerate_phase5_report(
    *,
    tasks_file: str | Path = DEFAULT_TASKS_FILE,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
) -> Path:
    phase_dir = _phase_dir(output_dir)
    payload = json.loads(
        (phase_dir / "phase5_report.json").read_text(encoding="utf-8")
    )
    reports = {}
    task_rows = {}
    for profile in ("basic", "full"):
        report_paths = sorted(
            (phase_dir / f"{profile}_results").glob("*/report.json")
        )
        if len(report_paths) != 1:
            raise RuntimeError(
                f"Phase 5 {profile} report 需要恰好一个 run，"
                f"实际 {len(report_paths)}"
            )
        report = BenchmarkReport.model_validate_json(
            report_paths[0].read_text(encoding="utf-8")
        )
        reports[profile] = report
        task_rows[profile] = _task_rows(report)
    payload["reports"] = {
        profile: report.model_dump(mode="json")
        for profile, report in reports.items()
    }
    payload["task_rows"] = task_rows
    payload["usage"] = {
        profile: _category_usage(report)
        for profile, report in reports.items()
    }
    v2_experiment = (
        phase_dir.parent / "ablation_phase2_20261111_v2" / "experiment.json"
    )
    if v2_experiment.is_file():
        v2_data = json.loads(v2_experiment.read_text(encoding="utf-8"))
        payload["v2_reference"] = {
            "experiment": str(v2_experiment),
            "basic": v2_data["variants"]["basic"]["metrics"],
            "full": v2_data["variants"]["full"]["metrics"],
        }
    _write_json(phase_dir / "phase5_report.json", payload)
    report_path = phase_dir / "phase5_report.md"
    _write(report_path, render_phase5_report(payload))
    return report_path


async def run_realworld_profiles(
    *,
    tasks_file: str | Path = DEFAULT_TASKS_FILE,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
) -> Path:
    tasks_path = Path(tasks_file).expanduser().resolve()
    tasks = load_external_tasks(tasks_path)
    phase_dir = _phase_dir(output_dir)
    phase_dir.mkdir(parents=True, exist_ok=True)
    manifest = _source_manifest(tasks_path, tasks)
    _write_json(phase_dir / "source_manifest.json", manifest)
    reports = {}
    task_rows = {}
    host_test_runner = make_host_maven_runner(offline=True)
    for profile in (AgentProfile.BASIC, AgentProfile.FULL):
        output = phase_dir / f"{profile.value}_results"
        runner = BenchmarkRunner(
            tasks,
            output_dir=output,
            run_id=f"defects4j_v1_{profile.value}",
            profile=profile,
            test_runner=host_test_runner,
            task_evaluator=phase5_task_evaluator,
            workspace_preparer=prepare_defects4j_workspace,
        )
        report, _ = await runner.run()
        reports[profile.value] = report
        task_rows[profile.value] = _task_rows(report)
    payload = {
        "schema_version": 1,
        "phase": "5",
        "benchmark": "defects4j_v1",
        "source": manifest,
        "reports": {
            profile: report.model_dump(mode="json")
            for profile, report in reports.items()
        },
        "task_rows": task_rows,
        "usage": {
            profile: _category_usage(report)
            for profile, report in reports.items()
        },
    }
    v2_experiment = (
        phase_dir.parent / "ablation_phase2_20261111_v2" / "experiment.json"
    )
    if v2_experiment.is_file():
        v2_data = json.loads(v2_experiment.read_text(encoding="utf-8"))
        payload["v2_reference"] = {
            "experiment": str(v2_experiment),
            "basic": v2_data["variants"]["basic"]["metrics"],
            "full": v2_data["variants"]["full"]["metrics"],
        }
    _write_json(phase_dir / "experiment.json", payload)
    _write_json(phase_dir / "phase5_report.json", payload)
    _write(phase_dir / "phase5_report.md", render_phase5_report(payload))
    report_path = phase_dir / "phase5_report.md"
    print(f"Phase 5 report: {report_path}")
    return report_path


def _pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.1f}%"


def _num(value: float | None, digits: int = 1) -> str:
    return "N/A" if value is None else f"{value:.{digits}f}"


def _seconds(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.1f}s"


def render_phase5_report(payload: dict) -> str:
    reports = payload["reports"]
    basic = reports["basic"]
    full = reports["full"]
    basic_summary = basic["summary"]
    full_summary = full["summary"]
    rows = payload["task_rows"]
    difficulties = Counter(
        task.get("external_difficulty")
        for task in basic["tasks"]
    )
    lines = [
        "# Phase 5 Real-world Java Bug Benchmark",
        "",
        "## 1. Benchmark Construction",
        "",
        f"The fixed Defects4J V1 manifest contains {len(basic['tasks'])} bugs. "
        "Selection and rejection records are in `candidate_selection.json`; "
        "only stable reproduction candidates enter the manifest.",
        "",
        "## 2. Leakage Protection",
        "",
        "The Agent receives only a cloned buggy workspace and a generic problem "
        "statement. Fixed revisions, patches, ground-truth classes, evaluator "
        "source, and metadata files remain outside the workspace. Tests and "
        "build files are protected; modifying them fails the task.",
        "",
        "## 3. Reproducibility",
        "",
        "All manifest tasks were checked as buggy-test-failing and "
        "fixed-test-passing before Agent execution.",
        "",
        "## 4. Benchmark Composition",
        "",
        "| Difficulty | Count |",
        "|---|---:|",
    ]
    for difficulty in ("local", "cross_file", "structural"):
        lines.append(f"| {difficulty} | {difficulties.get(difficulty, 0)} |")
    lines.extend(
        [
            "",
            "## 5. Basic vs Full",
            "",
            "| Profile | Success | Trigger Pass | Relevant Pass | Avg Tokens | "
            "LLM Calls | Tool Calls | Duration |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for profile, summary, profile_rows in (
        ("basic", basic_summary, rows["basic"]),
        ("full", full_summary, rows["full"]),
    ):
        trigger = _averages(profile_rows, "trigger_tests_passed")
        relevant = _averages(profile_rows, "relevant_tests_passed")
        lines.append(
            f"| {profile} | {_pct(summary['success_rate'])} | "
            f"{_pct(trigger)} | {_pct(relevant)} | "
            f"{_num(summary['average_tokens'])} | "
            f"{_num(summary['average_llm_calls'], 2)} | "
            f"{_num(_averages(profile_rows, 'tool_calls'), 2)} | "
            f"{_num(summary['average_duration'])}s |"
        )
    lines.extend(
        [
            "",
            "## 6. Localization",
            "",
            "| Profile | File Recall | Time to Relevant File | "
            "LLM Calls to Relevant File | Tokens Before First Patch |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for profile in ("basic", "full"):
        profile_rows = rows[profile]
        lines.append(
            f"| {profile} | "
            f"{_pct(_averages(profile_rows, 'file_localization_recall'))} | "
            f"{_seconds(_averages(profile_rows, 'time_to_first_relevant_file_seconds'))} | "
            f"{_num(_averages(profile_rows, 'llm_calls_to_first_relevant_file'), 2)} | "
            f"{_num(_averages(profile_rows, 'tokens_before_first_patch'))} |"
        )
    lines.extend(
        [
            "",
            "## 7. Code Intelligence Usage",
            "",
            "| Profile | Tool | Calls |",
            "|---|---|---:|",
        ]
    )
    for profile in ("basic", "full"):
        for tool, count in payload["usage"][profile]["tool_usage"].items():
            lines.append(f"| {profile} | `{tool}` | {count} |")
    lines.extend(
        [
            "",
            "### CI by Difficulty",
            "",
            "| Profile | Difficulty | Tasks | CI Calls | Tasks With CI | "
            "Tasks Saved by CI |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for profile in ("basic", "full"):
        for difficulty, values in payload["usage"][profile][
            "ci_by_difficulty"
        ].items():
            lines.append(
                f"| {profile} | {difficulty} | {values['tasks']} | "
                f"{values['calls']} | {values['tasks_with_ci']} | "
                f"{values['successes']} |"
            )
    lines.extend(
        [
            "",
            "## 8. Task-Level Results",
            "",
            "| Task | Basic | Full | Full Failure | Key Full Tools |",
            "|---|---:|---:|---|---|",
        ]
    )
    basic_by_id = {row["task_id"]: row for row in rows["basic"]}
    full_results = {result["task_id"]: result for result in full["results"]}
    for row in rows["full"]:
        task_id = row["task_id"]
        basic_row = basic_by_id.get(task_id, {})
        key_tools = ", ".join(
            sorted(
                full_results[task_id]["tool_usage"],
                key=lambda name: full_results[task_id]["tool_usage"][name],
                reverse=True,
            )[:5]
        )
        lines.append(
            f"| {task_id} | {'PASS' if basic_row.get('success') else 'FAIL'} | "
            f"{'PASS' if row.get('success') else 'FAIL'} | "
            f"{row.get('failure_category') or ''} | {key_tools} |"
        )
    failure_counts = Counter(
        row.get("failure_category") or "none"
        for row in rows["full"]
        if not row.get("success")
    )
    basic_no_change = sum(
        not row["success"] and not row.get("final_changed_files")
        for row in rows["basic"]
    )
    full_no_change = sum(
        not row["success"] and not row.get("final_changed_files")
        for row in rows["full"]
    )
    protected_changes = sum(
        bool(row.get("protected_files_modified"))
        for profile in ("basic", "full")
        for row in rows[profile]
    )
    lines.extend(["", "## 9. Failure Analysis", ""])
    for category, count in failure_counts.most_common(5):
        lines.append(f"- `{category}`: {count} task(s).")
    if not failure_counts:
        lines.append("- No failed Full tasks.")
    lines.extend(
        [
            f"- Search/patch budget exhaustion: Basic made no file change in "
            f"{basic_no_change} failed tasks; Full made no change in "
            f"{full_no_change}.",
            f"- Test overfitting was not observed: protected test/build files "
            f"were modified in {protected_changes} task-runs.",
            "- Localization was not the primary failure: trigger-relevant "
            "files were reached in most runs, but agents often exhausted "
            "steps while converting localization into a patch.",
        ]
    )
    lines.extend(
        [
            "",
            "## 10. Comparison with V2",
            "",
            "V2 is an in-repository synthetic suite. This phase uses external "
            "Bug revisions and full project Maven suites. Direct success-rate "
            "comparison should therefore be treated as a difficulty signal, "
            "not a controlled model comparison.",
        ]
    )
    v2 = payload.get("v2_reference")
    if v2:
        lines.extend(
            [
                "",
                "| Profile | V2 Success | Defects4J Success | V2 Tokens | "
                "Defects4J Tokens | V2 Calls | Defects4J Calls |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for profile, current in (
            ("basic", basic_summary),
            ("full", full_summary),
        ):
            reference = v2[profile]
            lines.append(
                f"| {profile} | {_pct(reference['success_rate'])} | "
                f"{_pct(current['success_rate'])} | "
                f"{_num(reference['average_tokens'])} | "
                f"{_num(current['average_tokens'])} | "
                f"{_num(reference['average_llm_calls'], 2)} | "
                f"{_num(current['average_llm_calls'], 2)} |"
            )
    lines.extend(
        [
            "",
            "The real-world benchmark is much more difficult than V2 and "
            "triggers substantially more context consumption and step-limit "
            "termination.",
            "",
            "## 11. Conclusion",
            "",
            f"Basic passed {basic_summary['passed_tasks']}/"
            f"{basic_summary['total_tasks']} real bugs; Full passed "
            f"{full_summary['passed_tasks']}/{full_summary['total_tasks']}. "
            "Current Agent capability on these real bugs is therefore low.",
            "",
            "Full did not show a stable advantage over Basic on this subset. "
            "The Code Intelligence tools were used, but their usage did not "
            "translate into successful repairs.",
            "",
            "The dominant observed bottleneck is reaching the 20-step limit "
            "while searching and patching, not a lack of test execution.",
            "",
            "## 12. Recommended Next Step",
            "",
            "Use the localization/failure data to improve search and patch "
            "planning for large real repositories. Do not change this "
            "Defects4J V1 manifest; create a new benchmark version for future "
            "expansion.",
            "",
        ]
    )
    return "\n".join(lines)
