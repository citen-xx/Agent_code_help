"""Phase 8 hypothesis quality and frozen-holdout evaluation."""

import asyncio
import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.agent.budget import StepBudgetPolicyName
from app.agent.capabilities import AgentProfile, PROFILE_CAPABILITIES
from app.agent.graph import build_system_prompt
from app.code_intel.tools import create_code_intel_tools
from app.evaluation.models import BenchmarkReport
from app.evaluation.phase5 import (
    DEFAULT_TASKS_FILE,
    load_external_tasks,
    make_host_maven_runner,
    phase5_task_evaluator,
    prepare_defects4j_workspace,
)
from app.evaluation.phase7 import (
    PHASE7_DIR_NAME,
    _control_reports as _phase7_control_reports,
    _policy_metrics,
    _task_comparison,
    _task_records,
)
from app.evaluation.runner import BenchmarkRunner
from app.evaluation.tasks import DEFAULT_RESULTS_DIR, PROJECT_ROOT
from app.repair.models import EvidenceRepairPlan
from app.repair.tools import create_repair_plan_tools
from app.tools.build_tools import create_build_tools
from app.tools.git_tools import create_git_tools
from app.tools.patch_tools import create_patch_tools
from app.tools.registry import create_repo_tools
from app.workspace import git

PHASE8_DIR_NAME = "phase8_hypothesis_quality"
HOLDOUT_TASKS_FILE = (
    PROJECT_ROOT
    / "evaluation"
    / "benchmarks"
    / "defects4j_v2_holdout"
    / "tasks.json"
)
V1_RUN_ORDER = (
    ("control_run1", False),
    ("treatment_run1", True),
    ("treatment_run2", True),
    ("control_run2", False),
)
HOLDOUT_RUN_ORDER = (
    ("control_holdout1", False),
    ("treatment_holdout1", True),
)
DESIGN = """# Evidence-aware Repair Commitment

Phase 8 keeps the Phase 7 evidence provenance checks and adds a separate
Hypothesis Readiness layer:

1. Every Hypothesis must state a concrete causal mechanism and testable
   prediction in addition to the Phase 7 observed failure and source evidence.
2. Known ambiguity is represented as structured unresolved dependencies or
   alternative explanations with candidate files/symbols.
3. A Hypothesis with no unresolved dependency takes the same fast path as
   Phase 7.
4. A Hypothesis with a dependency is not patch-ready. Only targeted reads and
   Code Intelligence calls matching the declared candidates are allowed, with
   four probes per Plan and deterministic duplicate blocking.
5. A revised Hypothesis needs a new runtime observation after the previous
   Plan; stale source evidence or an unfounded revision is rejected.
6. Patch targets must remain a subset of the revised Plan.

Provenance validity, readiness, and root-cause correctness remain separate.
Only test outcomes and offline analysis can assess correctness.
"""


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


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _phase_dir(output_dir: str | Path) -> Path:
    root = Path(output_dir).expanduser()
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return root.resolve() / PHASE8_DIR_NAME


def _tool_schema_hash(
    profile: AgentProfile,
    *,
    hypothesis_quality: bool,
) -> str:
    workspace_id = "schema-snapshot"
    with tempfile.TemporaryDirectory(prefix="phase8-schema-") as temporary:
        repo = Path(temporary) / workspace_id / "repository"
        repo.mkdir(parents=True)
        tools = create_repo_tools(repo)
        tools += create_git_tools(workspace_id, repo)
        caps = PROFILE_CAPABILITIES[profile]
        if caps.code_intelligence:
            tools += create_code_intel_tools(workspace_id, repo)
        if caps.repair_planner:
            tools += create_repair_plan_tools(
                workspace_id,
                repo,
                require_evidence=not hypothesis_quality,
                hypothesis_quality=hypothesis_quality,
            )
        tools += create_patch_tools(workspace_id, repo)
        tools += create_build_tools(workspace_id, repo)
        snapshot = [
            {
                "name": tool.name,
                "description": tool.description,
                "schema": (
                    tool.args_schema.model_json_schema()
                    if getattr(tool, "args_schema", None)
                    else None
                ),
            }
            for tool in sorted(tools, key=lambda value: value.name)
        ]
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _source_manifest(tasks_file: Path, tasks) -> dict:
    if git.worktree_is_dirty(PROJECT_ROOT):
        raise RuntimeError("Phase 8 要求 clean working tree")
    phase7_manifest = json.loads(
        (
            PROJECT_ROOT
            / "evaluation"
            / "results"
            / PHASE7_DIR_NAME
            / "source_manifest.json"
        ).read_text(encoding="utf-8")
    )
    prompt_hashes = {
        profile.value: hashlib.sha256(
            build_system_prompt(PROFILE_CAPABILITIES[profile]).encode()
        ).hexdigest()
        for profile in (AgentProfile.BASIC, AgentProfile.FULL)
    }
    if prompt_hashes != phase7_manifest["agent"]["prompt_hashes"]:
        raise RuntimeError("Prompt hash 与 Phase 7 不一致")
    control_tools = {
        profile.value: _tool_schema_hash(
            profile,
            hypothesis_quality=False,
        )
        for profile in (AgentProfile.BASIC, AgentProfile.FULL)
    }
    if control_tools != phase7_manifest["agent"]["treatment_tool_schema_hashes"]:
        raise RuntimeError("Phase 7 Control tool schema 不一致")
    phase65_manifest = json.loads(
        (
            PROJECT_ROOT
            / "evaluation"
            / "results"
            / "phase6_5_budget_refinement"
            / "source_manifest.json"
        ).read_text(encoding="utf-8")
    )
    if _sha(PROJECT_ROOT / "app/agent/budget.py") != (
        phase65_manifest["budget_policy_source"]["sha256"]
    ):
        raise RuntimeError("Budget source changed")
    holdout_manifest = HOLDOUT_TASKS_FILE.parent / "benchmark_manifest.json"
    return {
        "schema_version": 1,
        "phase": "8",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "development_benchmark": {
            "name": "defects4j_v1",
            "path": str(tasks_file),
            "sha256": _sha(tasks_file),
            "count": len(tasks),
            "role": "development_diagnostic_only",
        },
        "holdout_benchmark": {
            "name": "defects4j_v2_holdout",
            "path": str(HOLDOUT_TASKS_FILE),
            "sha256": _sha(HOLDOUT_TASKS_FILE),
            "manifest_path": str(holdout_manifest),
            "manifest_sha256": _sha(holdout_manifest),
        },
        "benchmark_commit": phase7_manifest["defects4j_commit"],
        "jdk": phase7_manifest["jdk"],
        "model": phase7_manifest["model"],
        "temperature": phase7_manifest["temperature"],
        "budget_policy": StepBudgetPolicyName.ADAPTIVE_20_30.value,
        "agent": {
            "git_commit": git.head_commit(PROJECT_ROOT),
            "git_dirty": False,
            "prompt_hashes": prompt_hashes,
            "control_tool_schema_hashes": control_tools,
            "treatment_tool_schema_hashes": {
                profile.value: _tool_schema_hash(
                    profile,
                    hypothesis_quality=True,
                )
                for profile in (AgentProfile.BASIC, AgentProfile.FULL)
            },
        },
        "quality_sources": {
            name: {
                "path": str(PROJECT_ROOT / "app" / "agent" / name),
                "sha256": _sha(PROJECT_ROOT / "app" / "agent" / name),
            }
            for name in ("commitment.py", "graph.py")
        },
    }


def _load_report(path: Path) -> BenchmarkReport:
    if not path.is_file():
        raise RuntimeError(f"Missing report: {path}")
    report = BenchmarkReport.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    return report


def _run_variant(
    tasks,
    phase_dir: Path,
    *,
    run_name: str,
    quality: bool,
    benchmark_name: str,
) -> BenchmarkReport:
    run_id = f"phase8_{run_name}"
    path = phase_dir / "runs" / run_id / "report.json"
    if path.is_file():
        report = _load_report(path)
        if (
            len(report.results) == len(tasks)
            and report.capabilities.get("hypothesis_quality", False) is quality
            and not any(
                result.failure_category
                and result.failure_category.value == "infrastructure_error"
                for result in report.results
            )
        ):
            return report
    runner = BenchmarkRunner(
        tasks,
        output_dir=phase_dir / "runs",
        run_id=run_id,
        profile=AgentProfile.FULL,
        max_steps=20,
        step_budget_policy=StepBudgetPolicyName.ADAPTIVE_20_30,
        evidence_commitment=True,
        hypothesis_quality=quality,
        test_runner=make_host_maven_runner(offline=True),
        task_evaluator=phase5_task_evaluator,
        workspace_preparer=prepare_defects4j_workspace,
    )
    report, _ = asyncio.run(runner.run())
    print(
        f"{benchmark_name} {run_name} ({'treatment' if quality else 'control'}): "
        f"{report.summary.passed_tasks}/{report.summary.total_tasks}",
        flush=True,
    )
    return report


def _audit_phase7(tasks) -> str:
    reports = _phase7_control_reports()
    task_map = {task.task_id: task for task in tasks}
    lines = [
        "# Phase 7 Structural / Cross-file Hypothesis Audit",
        "",
        "This audit uses Phase 7 Treatment traces and evaluator metadata only "
        "after runtime completion. It was not used by the Agent.",
        "",
        "| Run | Task | Category | Ground Truth | Planned Files | Patch Files | "
        "Evidence Errors | Revisions | Final |",
        "|---|---|---|---|---|---|---:|---:|---|",
    ]
    for report in reports:
        for result in report.results:
            task = task_map[result.task_id]
            if task.external_difficulty == "local":
                continue
            if result.success:
                continue
            plan = result.plan_revisions[-1].plan if result.plan_revisions else None
            planned = [
                change.file_path for change in plan.planned_changes
            ] if plan else []
            patches = sorted(
                {
                    path.strip()
                    for event in result.tool_events
                    if event.tool_name == "apply_patch" and event.success
                    for path in (event.target_file or "").split(",")
                    if path.strip()
                }
            )
            errors = (
                result.commitment_telemetry.validation_errors
                if result.commitment_telemetry
                else []
            )
            lines.append(
                f"| {report.run_id} | {result.task_id} | "
                f"{task.external_difficulty} | "
                f"{', '.join(task.ground_truth_files)} | "
                f"{', '.join(planned) or '-'} | "
                f"{', '.join(patches) or '-'} | "
                f"{'; '.join(errors) or '-'} | "
                f"{len(result.plan_revisions)} | "
                f"{result.failure_category or 'failed'} |"
            )
    lines.extend(
        [
            "",
            "## Findings",
            "",
            "- Provenance was generally valid, but Phase 7 did not prevent "
            "incomplete causal coverage. Multi-file expectations were often "
            "reduced to one planned file.",
            "- The commitment gate sometimes blocked another broad search "
            "without ensuring that known cross-file or inheritance dependencies "
            "had been checked.",
            "- Test-failure revisions frequently corrected patch syntax or "
            "anchor text rather than revisiting the root-cause explanation.",
            "- Structured schema completeness therefore overestimated readiness. "
            "Phase 8 keeps provenance validity separate from readiness and "
            "correctness.",
        ]
    )
    return "\n".join(lines) + "\n"


def _experiment_metrics(
    control_reports: list[BenchmarkReport],
    treatment_reports: list[BenchmarkReport],
    tasks,
) -> dict:
    control_records = _task_records(
        control_reports,
        tasks,
        variant="control",
    )
    treatment_records = _task_records(
        treatment_reports,
        tasks,
        variant="treatment",
    )
    return {
        "control": {
            "metrics": _policy_metrics(control_records, control_reports),
            "records": control_records,
        },
        "treatment": {
            "metrics": _policy_metrics(treatment_records, treatment_reports),
            "records": treatment_records,
        },
        "tasks": _task_comparison(control_records, treatment_records),
    }


def _comparison(experiment: dict) -> dict:
    control = experiment["control"]["metrics"]
    treatment = experiment["treatment"]["metrics"]
    return {
        "success_delta": treatment["success_rate"] - control["success_rate"],
        "local_delta": (
            treatment["category_success"]["local"]["success_rate"]
            - control["category_success"]["local"]["success_rate"]
        ),
        "cross_file_delta": (
            treatment["category_success"]["cross_file"]["success_rate"]
            - control["category_success"]["cross_file"]["success_rate"]
        ),
        "structural_delta": (
            treatment["category_success"]["structural"]["success_rate"]
            - control["category_success"]["structural"]["success_rate"]
        ),
        "no_patch_delta": (
            treatment["no_patch_failures"]
            - control["no_patch_failures"]
        ),
        "token_delta": (
            treatment["average_task_tokens"]
            - control["average_task_tokens"]
        ),
        "provenance_validity": {
            "control": control["evidence_provenance_valid_runs"],
            "treatment": treatment["evidence_provenance_valid_runs"],
        },
        "targeted_exploration": {
            "control": control["targeted_exploration_calls"],
            "treatment": treatment["targeted_exploration_calls"],
        },
        "broad_exploration": {
            "control": control["broad_exploration_calls"],
            "treatment": treatment["broad_exploration_calls"],
        },
    }


def _build_report(
    *,
    manifest: dict,
    v1: dict,
    holdout: dict,
    audit: str,
) -> dict:
    v1_comparison = _comparison(v1)
    holdout_comparison = _comparison(holdout)
    v1_metrics = v1["treatment"]["metrics"]
    h_metrics = holdout["treatment"]["metrics"]
    v1_ok = (
        v1_comparison["success_delta"] >= 0
        and v1_comparison["local_delta"] >= -0.1
        and (
            v1_comparison["structural_delta"] > 0
            or v1_comparison["cross_file_delta"] > 0
        )
        and v1_comparison["no_patch_delta"] <= 2
    )
    holdout_ok = (
        holdout_comparison["success_delta"] >= 0
        and holdout_comparison["local_delta"] >= -0.1
        and holdout_comparison["no_patch_delta"] <= 2
    )
    return {
        "schema_version": 1,
        "phase": "8",
        "source": manifest,
        "design": DESIGN,
        "hypothesis_quality_audit": audit,
        "defects4j_v1": {
            **v1,
            "comparison": v1_comparison,
        },
        "holdout": {
            **holdout,
            "comparison": holdout_comparison,
        },
        "acceptance": {
            "development_pass": v1_ok,
            "holdout_pass": holdout_ok,
            "default_capability_recommended": v1_ok and holdout_ok,
        },
    }


def _render(payload: dict) -> str:
    v1 = payload["defects4j_v1"]
    holdout = payload["holdout"]
    c1 = v1["control"]["metrics"]
    t1 = v1["treatment"]["metrics"]
    ch = holdout["control"]["metrics"]
    th = holdout["treatment"]["metrics"]
    lines = [
        "# Phase 8 Evidence-grounded Hypothesis Quality",
        "",
        "## 1. Structural Audit",
        "",
        "Detailed historical failures are in `hypothesis_quality_audit.md`.",
        "",
        "## 2. Quality Constraints",
        "",
        "Treatment adds causal mechanism, testable prediction, structured "
        "unresolved dependencies, and alternative explanations. Provenance, "
        "readiness, and correctness remain separate.",
        "",
        "## 3. V1 Development Benchmark",
        "",
        "| Metric | Control | Treatment |",
        "|---|---:|---:|",
    ]
    for label, key, percent in (
        ("Success", "success_rate", True),
        ("Local", None, False),
        ("Cross-file", None, False),
        ("Structural", None, False),
        ("No-Patch", "no_patch_failures", False),
        ("Avg Tokens", "average_task_tokens", False),
        ("First Plan", "average_first_plan_step", False),
        ("First Patch", "average_first_patch_step", False),
        ("Plan→Patch", "average_plan_to_patch_gap", False),
        ("Relevant→Patch", "average_relevant_to_patch_gap", False),
    ):
        if key is None:
            category = label.lower().replace("-", "_")
            cv = c1["category_success"][category]
            tv = t1["category_success"][category]
            left = f"{cv['passed']}/{cv['total']}"
            right = f"{tv['passed']}/{tv['total']}"
        else:
            left = _fmt(c1.get(key), percent)
            right = _fmt(t1.get(key), percent)
        lines.append(f"| {label} | {left} | {right} |")
    lines.extend(
        [
            "",
            f"Provenance-valid runs: {c1['evidence_provenance_valid_runs']} → "
            f"{t1['evidence_provenance_valid_runs']}.",
            f"Targeted exploration calls: {c1['targeted_exploration_calls']} → "
            f"{t1['targeted_exploration_calls']}; broad calls: "
            f"{c1['broad_exploration_calls']} → "
            f"{t1['broad_exploration_calls']}.",
            "",
            "## 4. Frozen V2 Holdout",
            "",
            "| Metric | Control | Treatment |",
            "|---|---:|---:|",
            f"| Success | {ch['passed']}/{ch['task_runs']} | "
            f"{th['passed']}/{th['task_runs']} |",
            f"| Local | {ch['category_success']['local']['passed']}/"
            f"{ch['category_success']['local']['total']} | "
            f"{th['category_success']['local']['passed']}/"
            f"{th['category_success']['local']['total']} |",
            f"| Cross-file | {ch['category_success']['cross_file']['passed']}/"
            f"{ch['category_success']['cross_file']['total']} | "
            f"{th['category_success']['cross_file']['passed']}/"
            f"{th['category_success']['cross_file']['total']} |",
            f"| Structural | {ch['category_success']['structural']['passed']}/"
            f"{ch['category_success']['structural']['total']} | "
            f"{th['category_success']['structural']['passed']}/"
            f"{th['category_success']['structural']['total']} |",
            f"| No-Patch | {ch['no_patch_failures']} | "
            f"{th['no_patch_failures']} |",
            f"| Avg Tokens | {ch['average_task_tokens']:,.0f} | "
            f"{th['average_task_tokens']:,.0f} |",
            "",
            "## 5. Generalization",
            "",
            payload["conclusion"],
            "",
            "## 6. Default Capability",
            "",
            (
                "Evidence-aware commitment is recommended as default."
                if payload["acceptance"]["default_capability_recommended"]
                else "Evidence-aware commitment remains opt-in."
            ),
            "",
            "## Recommended Next Step",
            "",
            "Phase 9: Production Readiness & Portfolio Delivery.",
            "This phase does not start Phase 9.",
            "",
        ]
    )
    return "\n".join(lines)


def _fmt(value, percent=False):
    if value is None:
        return "N/A"
    return f"{value * 100:.1f}%" if percent else f"{value:.1f}"


def _write_artifacts(phase_dir: Path, payload: dict) -> Path:
    _write_json(phase_dir / "defects4j_v1_experiment.json", payload["defects4j_v1"])
    _write_json(phase_dir / "holdout_experiment.json", payload["holdout"])
    _write_json(
        phase_dir / "task_level_analysis.json",
        {
            "defects4j_v1": payload["defects4j_v1"]["tasks"],
            "holdout": payload["holdout"]["tasks"],
        },
    )
    _write_json(phase_dir / "phase8_report.json", payload)
    _write(phase_dir / "hypothesis_quality_design.md", DESIGN)
    path = phase_dir / "phase8_report.md"
    _write(path, _render(payload))
    return path


def run_phase8(
    *,
    tasks_file: str | Path = DEFAULT_TASKS_FILE,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
) -> Path:
    tasks_path = Path(tasks_file).expanduser().resolve()
    v1_tasks = load_external_tasks(tasks_path)
    holdout_tasks = load_external_tasks(HOLDOUT_TASKS_FILE)
    phase_dir = _phase_dir(output_dir)
    manifest = _source_manifest(tasks_path, v1_tasks)
    _write_json(phase_dir / "source_manifest.json", manifest)
    audit = _audit_phase7(v1_tasks)
    _write(phase_dir / "hypothesis_quality_audit.md", audit)
    holdout_selection = (
        PROJECT_ROOT
        / "evaluation"
        / "results"
        / PHASE8_DIR_NAME
        / "holdout_candidate_selection.json"
    )
    holdout_manifest = HOLDOUT_TASKS_FILE.parent / "benchmark_manifest.json"
    if not holdout_selection.is_file():
        raise RuntimeError("Missing frozen holdout selection audit")
    selection_target = phase_dir / "holdout_candidate_selection.json"
    if holdout_selection.resolve() != selection_target.resolve():
        shutil.copyfile(holdout_selection, selection_target)
    shutil.copyfile(holdout_manifest, phase_dir / "holdout_manifest.json")

    v1_reports = {False: [], True: []}
    for run_name, quality in V1_RUN_ORDER:
        v1_reports[quality].append(
            _run_variant(
                v1_tasks,
                phase_dir,
                run_name=run_name,
                quality=quality,
                benchmark_name="V1",
            )
        )
    holdout_reports = {False: [], True: []}
    for run_name, quality in HOLDOUT_RUN_ORDER:
        holdout_reports[quality].append(
            _run_variant(
                holdout_tasks,
                phase_dir,
                run_name=run_name,
                quality=quality,
                benchmark_name="V2 holdout",
            )
        )
    v1 = _experiment_metrics(
        v1_reports[False],
        v1_reports[True],
        v1_tasks,
    )
    holdout = _experiment_metrics(
        holdout_reports[False],
        holdout_reports[True],
        holdout_tasks,
    )
    payload = _build_report(
        manifest=manifest,
        v1=v1,
        holdout=holdout,
        audit=audit,
    )
    payload["conclusion"] = _conclusion(payload)
    return _write_artifacts(phase_dir, payload)


def _conclusion(payload: dict) -> str:
    v1 = payload["defects4j_v1"]["comparison"]
    holdout = payload["holdout"]["comparison"]
    return (
        "V1 success delta: "
        f"{v1['success_delta'] * 100:+.1f} pp; structural delta "
        f"{v1['structural_delta'] * 100:+.1f} pp; cross-file delta "
        f"{v1['cross_file_delta'] * 100:+.1f} pp; no-patch delta "
        f"{v1['no_patch_delta']:+d}. Holdout success delta: "
        f"{holdout['success_delta'] * 100:+.1f} pp. These are exploratory "
        "two-run comparisons and do not establish stable causal effects."
    )


def regenerate_phase8_report(
    *,
    tasks_file: str | Path = DEFAULT_TASKS_FILE,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
) -> Path:
    tasks_path = Path(tasks_file).expanduser().resolve()
    v1_tasks = load_external_tasks(tasks_path)
    holdout_tasks = load_external_tasks(HOLDOUT_TASKS_FILE)
    phase_dir = _phase_dir(output_dir)
    manifest = json.loads(
        (phase_dir / "source_manifest.json").read_text(encoding="utf-8")
    )
    v1_reports = {False: [], True: []}
    for run_name, quality in V1_RUN_ORDER:
        v1_reports[quality].append(
            _load_report(
                phase_dir / "runs" / f"phase8_{run_name}" / "report.json"
            )
        )
    holdout_reports = {False: [], True: []}
    for run_name, quality in HOLDOUT_RUN_ORDER:
        holdout_reports[quality].append(
            _load_report(
                phase_dir / "runs" / f"phase8_{run_name}" / "report.json"
            )
        )
    payload = _build_report(
        manifest=manifest,
        v1=_experiment_metrics(
            v1_reports[False],
            v1_reports[True],
            v1_tasks,
        ),
        holdout=_experiment_metrics(
            holdout_reports[False],
            holdout_reports[True],
            holdout_tasks,
        ),
        audit=_audit_phase7(v1_tasks),
    )
    payload["conclusion"] = _conclusion(payload)
    return _write_artifacts(phase_dir, payload)


def write_phase8_infrastructure_status(
    *,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
    provider_error: str = "402 Insufficient Balance",
) -> Path:
    """Write an honest blocked status without counting partial runs."""
    phase_dir = _phase_dir(output_dir)
    source = json.loads(
        (phase_dir / "source_manifest.json").read_text(encoding="utf-8")
    )
    control_path = (
        phase_dir / "runs" / "phase8_control_run1" / "report.json"
    )
    control = _load_report(control_path)
    if len(control.results) != 15:
        raise RuntimeError("Control R1 is incomplete")
    aborted_path = (
        phase_dir
        / "aborted_runs"
        / "phase8_treatment_run1_402"
        / "report.json"
    )
    aborted = _load_report(aborted_path)
    holdout_manifest = json.loads(
        (phase_dir / "holdout_manifest.json").read_text(encoding="utf-8")
    )
    task_level = {
        "status": "infrastructure_blocked",
        "official_ab_comparison_available": False,
        "control_run1": {
            "complete": True,
            "summary": control.summary.model_dump(mode="json"),
            "results": [
                result.model_dump(mode="json")
                for result in control.results
            ],
        },
        "treatment_run1_aborted": {
            "complete": False,
            "results_completed": len(aborted.results),
            "results": [
                result.model_dump(mode="json")
                for result in aborted.results
            ],
            "exclude_from_official_statistics": True,
        },
    }
    v1 = {
        "status": "infrastructure_blocked",
        "provider_error": provider_error,
        "official_ab_comparison_available": False,
        "reason": (
            "Treatment reached 402 Insufficient Balance before the minimum "
            "two complete runs per variant. Partial results must not be "
            "counted as Agent failures."
        ),
        "completed_runs": 1,
        "required_runs_per_variant": 2,
        "task_level": task_level,
    }
    holdout = {
        "status": "not_run_after_infrastructure_block",
        "official_result_available": False,
        "manifest": holdout_manifest,
        "reason": (
            "Holdout execution was intentionally not started after the "
            "provider balance failure."
        ),
    }
    payload = {
        "schema_version": 1,
        "phase": "8",
        "status": "infrastructure_blocked",
        "source": source,
        "design": DESIGN,
        "hypothesis_quality_audit": (
            phase_dir / "hypothesis_quality_audit.md"
        ).read_text(encoding="utf-8"),
        "defects4j_v1": v1,
        "holdout": holdout,
        "acceptance": {
            "development_pass": False,
            "holdout_pass": False,
            "default_capability_recommended": False,
        },
        "conclusion": (
            "Phase 8 capability implementation, audit, tests, and frozen "
            "holdout are complete, but controlled validation is blocked by "
            "provider balance. No default-capability decision can be made."
        ),
    }
    _write_json(phase_dir / "defects4j_v1_experiment.json", v1)
    _write_json(phase_dir / "holdout_experiment.json", holdout)
    _write_json(phase_dir / "task_level_analysis.json", task_level)
    _write_json(phase_dir / "phase8_report.json", payload)
    _write(phase_dir / "hypothesis_quality_design.md", DESIGN)
    report = "\n".join(
        [
            "# Phase 8 Evidence-grounded Hypothesis Quality",
            "",
            "## Status",
            "",
            f"`infrastructure_blocked`: {provider_error}.",
            "",
            "This is not an Agent failure and partial runs are excluded from "
            "official statistics.",
            "",
            "## Completed Work",
            "",
            "- Evidence-aware Hypothesis implementation and tests completed.",
            "- Phase 7 structural/cross-file audit completed.",
            "- V2 Holdout candidate reproduction and manifest freeze completed.",
            f"- V1 Control R1 completed: {control.summary.passed_tasks}/"
            f"{control.summary.total_tasks}.",
            f"- Treatment R1 aborted after {len(aborted.results)} tasks.",
            "",
            "## Holdout",
            "",
            f"Frozen tasks: {holdout_manifest['task_count']}; hash: "
            f"`{holdout_manifest['tasks_sha256']}`. Holdout execution did not "
            "start after the infrastructure failure.",
            "",
            "## Decision",
            "",
            "No default-capability decision is possible. The current production "
            "default remains unchanged.",
            "",
            "## Required Resume",
            "",
            "After provider balance is restored, rerun `phase8`; completed "
            "Control R1 can be reused while the partial Treatment run is "
            "ignored.",
            "",
            "## Recommended Next Step",
            "",
            "Phase 9: Production Readiness & Portfolio Delivery after the "
            "blocked Phase 8 validation is completed.",
            "",
        ]
    )
    path = phase_dir / "phase8_report.md"
    _write(path, report)
    return path
