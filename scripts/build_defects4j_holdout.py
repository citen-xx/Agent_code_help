"""Build the frozen Phase 8 Defects4J V2 holdout candidate manifest."""

import argparse
import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

D4J_COMMIT = "8c16da8230843cdc918eaf4ddb449637f02b83c6"
PROBLEM = (
    "Repository contains a failing Java project.\n\n"
    "Fix the bug so the failing tests pass without introducing regressions. "
    "Inspect the repository and determine the root cause."
)
PROJECTS = {
    "Cli": "commons-cli.git",
    "Codec": "commons-codec.git",
    "Gson": "gson.git",
    "JacksonXml": "jackson-dataformat-xml.git",
    "Jsoup": "jsoup.git",
}
CANDIDATES = {
    "Cli": [1, 5, 7, 8, 10],
    "Codec": [1, 2, 3, 4, 10, 13, 14, 15, 16, 17, 18],
    "Gson": [1, 2, 11, 12],
    "JacksonXml": [1, 2, 3, 4, 5],
    "Jsoup": [
        10,
        20,
        30,
        40,
        50,
        60,
        70,
        75,
        80,
        84,
        85,
        88,
        89,
        90,
        91,
        92,
        93,
    ],
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _trigger(path: Path) -> list[str]:
    values = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("--- ") and "::" in stripped:
            cls, method = stripped[4:].split("::", 1)
            values.append(f"{cls}#{method}")
    return list(dict.fromkeys(values))


def _difficulty(files: int, hunks: int, relevant: int) -> str:
    if files <= 1 and hunks <= 1 and relevant < 20:
        return "local"
    if files <= 3 and hunks <= 3 and relevant < 60:
        return "cross_file"
    return "structural"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata-root", required=True)
    parser.add_argument("--repos-root", required=True)
    parser.add_argument(
        "--output-dir",
        default="evaluation/benchmarks/defects4j_v2_holdout",
    )
    args = parser.parse_args()
    metadata_root = Path(args.metadata_root).resolve()
    repos_root = Path(args.repos_root).resolve()
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    patches_dir = output / "test_patches"
    patches_dir.mkdir(parents=True, exist_ok=True)
    tasks = []
    candidates = []
    for project, ids in CANDIDATES.items():
        project_root = metadata_root / project
        active = {
            int(row["bug.id"]): row
            for row in csv.DictReader(
                (project_root / "active-bugs.csv").open(
                    encoding="utf-8",
                    newline="",
                )
            )
        }
        for bug_id in ids:
            row = active.get(bug_id)
            if row is None:
                candidates.append(
                    {
                        "project": project,
                        "bug_id": bug_id,
                        "status": "excluded",
                        "reason": "not_active_in_pinned_defects4j",
                    }
                )
                continue
            trigger = _trigger(
                project_root / "trigger_tests" / str(bug_id)
            )
            relevant_text = (
                project_root / "relevant_tests" / str(bug_id)
            ).read_text(encoding="utf-8")
            relevant = [
                value.strip()
                for value in relevant_text.splitlines()
                if value.strip()
            ]
            source_patch = (
                project_root / "patches" / f"{bug_id}.src.patch"
            ).read_text(encoding="utf-8")
            test_patch = (
                project_root / "patches" / f"{bug_id}.test.patch"
            ).read_text(encoding="utf-8")
            files = list(
                dict.fromkeys(
                    re.findall(
                        r"^diff --git a/(.+?) b/",
                        source_patch,
                        re.M,
                    )
                )
            )
            hunks = len(re.findall(r"^@@ ", source_patch, re.M))
            difficulty = _difficulty(
                len(files),
                hunks,
                len(relevant),
            )
            task_id = f"d4j-v2-{project.lower()}-{bug_id}"
            patch_path = patches_dir / f"{task_id}.test.patch"
            patch_path.write_text(test_patch, encoding="utf-8")
            tasks.append(
                {
                    "task_id": task_id,
                    "title": f"Defects4J V2 Holdout {project} bug {bug_id}",
                    "description": f"Defects4J V2 holdout {project} bug {bug_id}",
                    "repository": f"${{DEFECTS4J_REPOS_ROOT}}/{PROJECTS[project]}",
                    "base_commit": row["revision.id.buggy"],
                    "problem_statement": PROBLEM,
                    "expected_test_command": "mvn test",
                    "timeout": 600,
                    "tags": [
                        project,
                        f"bug-{bug_id}",
                        difficulty,
                        "holdout",
                    ],
                    "suite": "defects4j_v2_holdout",
                    "difficulty": (
                        "easy"
                        if difficulty == "local"
                        else "medium"
                        if difficulty == "cross_file"
                        else "hard"
                    ),
                    "hidden_tests": None,
                    "source": "defects4j",
                    "external_difficulty": difficulty,
                    "project": project,
                    "bug_id": bug_id,
                    "buggy_revision": row["revision.id.buggy"],
                    "fixed_revision": row["revision.id.fixed"],
                    "triggering_tests": trigger,
                    "relevant_tests": relevant,
                    "ground_truth_files": files,
                    "jdk": "17",
                    "test_patch": str(patch_path.relative_to(output)),
                }
            )
            candidates.append(
                {
                    "task_id": task_id,
                    "project": project,
                    "bug_id": bug_id,
                    "status": "candidate",
                    "difficulty": difficulty,
                    "source_files": files,
                    "patch_hunk_count": hunks,
                    "relevant_test_count": len(relevant),
                    "buggy_revision": row["revision.id.buggy"],
                    "fixed_revision": row["revision.id.fixed"],
                    "selection_basis": (
                        "official metadata only; no Agent outcome used"
                    ),
                }
            )
    tasks_path = output / "candidates.tasks.json"
    tasks_path.write_text(
        json.dumps(tasks, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    selection = {
        "schema_version": 1,
        "benchmark": "defects4j_v2_holdout",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "defects4j_commit": D4J_COMMIT,
        "candidate_count": len(tasks),
        "selection_note": (
            "Candidates were selected from official metadata by project "
            "diversity, non-overlap with V1 bug IDs, and source-patch complexity. "
            "Agent outcomes were not consulted."
        ),
        "candidates": candidates,
    }
    (output / "candidate_selection.initial.json").write_text(
        json.dumps(selection, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(tasks_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
