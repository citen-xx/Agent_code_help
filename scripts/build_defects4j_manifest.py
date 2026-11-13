"""Build the pinned Defects4J V1 manifest from official metadata."""

import argparse
import csv
import hashlib
import io
import json
import re
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

D4J_COMMIT = "8c16da8230843cdc918eaf4ddb449637f02b83c6"
BASE_URL = (
    "https://cdn.jsdelivr.net/gh/rjust/defects4j"
    f"@{D4J_COMMIT}/framework/projects"
)
PROBLEM_STATEMENT = (
    "Repository contains a failing Java project.\n\n"
    "Fix the bug so the failing tests pass without introducing regressions. "
    "Inspect the repository and determine the root cause."
)
SELECTED = {
    "local": [
        ("Codec", 5),
        ("Codec", 7),
        ("Codec", 9),
        ("Gson", 5),
        ("Gson", 6),
    ],
    "cross_file": [
        ("Codec", 6),
        ("Codec", 8),
        ("Codec", 12),
        ("Gson", 3),
        ("Gson", 7),
    ],
    "structural": [
        ("Codec", 11),
        ("Gson", 8),
        ("Gson", 10),
        ("Gson", 4),
        ("Gson", 9),
    ],
}
REPO_NAMES = {
    "Codec": "commons-codec.git",
    "Gson": "gson.git",
    "JacksonXml": "jackson-dataformat-xml.git",
    "Jsoup": "jsoup.git",
}


def _fetch(project: str, kind: str, bug_id: int, suffix: str = "") -> str:
    url = f"{BASE_URL}/{project}/{kind}/{bug_id}{suffix}"
    return _fetch_url(url)


def _fetch_url(url: str) -> str:
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=90) as response:
                return response.read().decode("utf-8")
        except Exception as exc:
            last_error = exc
            time.sleep(1 + attempt)
    raise RuntimeError(f"无法读取 {url}: {last_error}")


def _active_bugs(project: str) -> dict[int, dict]:
    text = _fetch_url(f"{BASE_URL}/{project}/active-bugs.csv")
    return {
        int(row["bug.id"]): row
        for row in csv.DictReader(io.StringIO(text))
    }


def _parse_trigger_tests(text: str) -> list[str]:
    tests = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("--- "):
            continue
        value = stripped[4:]
        if "::" in value:
            class_name, method = value.split("::", 1)
            tests.append(f"{class_name}#{method}")
    return list(dict.fromkeys(tests))


def _patch_files(patch: str) -> list[str]:
    return list(
        dict.fromkeys(
            re.findall(r"^diff --git a/(.+?) b/", patch, re.M)
        )
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _commit_exists(git_dir: Path, revision: str) -> bool:
    result = subprocess.run(
        ["git", f"--git-dir={git_dir}", "cat-file", "-e", f"{revision}^{{commit}}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repos-root", required=True)
    parser.add_argument(
        "--output-dir",
        default="evaluation/benchmarks/defects4j_v1",
    )
    args = parser.parse_args()
    repos_root = Path(args.repos_root).expanduser().resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    active = {
        project: _active_bugs(project)
        for project in REPO_NAMES
    }
    selected = [
        (difficulty, project, bug_id)
        for difficulty, items in SELECTED.items()
        for project, bug_id in items
    ]

    def fetch_metadata(item):
        difficulty, project, bug_id = item
        return (
            difficulty,
            project,
            bug_id,
            _fetch(project, "trigger_tests", bug_id),
            _fetch(project, "relevant_tests", bug_id),
            _fetch(project, "patches", bug_id, ".src.patch"),
            _fetch(project, "patches", bug_id, ".test.patch"),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        metadata = list(executor.map(fetch_metadata, selected))

    tasks = []
    task_records = []
    patches_dir = output_dir / "test_patches"
    patches_dir.mkdir(parents=True, exist_ok=True)
    for (
        difficulty,
        project,
        bug_id,
        trigger,
        relevant,
        patch,
        test_patch,
    ) in metadata:
        row = active[project][bug_id]
        trigger_tests = _parse_trigger_tests(trigger)
        relevant_tests = [
            line.strip()
            for line in relevant.splitlines()
            if line.strip()
        ]
        ground_truth_files = _patch_files(patch)
        if not trigger_tests or not ground_truth_files:
            raise RuntimeError(
                f"{project}-{bug_id} metadata incomplete"
            )
        repository = repos_root / REPO_NAMES[project]
        buggy = row["revision.id.buggy"]
        fixed = row["revision.id.fixed"]
        if not _commit_exists(repository, buggy):
            raise RuntimeError(f"{project}-{bug_id} buggy commit missing")
        if not _commit_exists(repository, fixed):
            raise RuntimeError(f"{project}-{bug_id} fixed commit missing")
        task_id = f"d4j-{project.lower()}-{bug_id}"
        test_patch_path = patches_dir / f"{task_id}.test.patch"
        test_patch_path.write_text(test_patch, encoding="utf-8")
        task = {
            "task_id": task_id,
            "title": f"Defects4J {project} bug {bug_id}",
            "description": f"Defects4J {project} bug {bug_id}",
            "repository": (
                "${DEFECTS4J_REPOS_ROOT}/"
                f"{REPO_NAMES[project]}"
            ),
            "base_commit": buggy,
            "problem_statement": PROBLEM_STATEMENT,
            "expected_test_command": "mvn test",
            "timeout": 600,
            "tags": [project, f"bug-{bug_id}", difficulty],
            "suite": "defects4j_v1",
            "difficulty": {
                "local": "easy",
                "cross_file": "medium",
                "structural": "hard",
            }[difficulty],
            "hidden_tests": None,
            "source": "defects4j",
            "external_difficulty": difficulty,
            "project": project,
            "bug_id": bug_id,
            "buggy_revision": buggy,
            "fixed_revision": fixed,
            "triggering_tests": trigger_tests,
            "relevant_tests": relevant_tests,
            "ground_truth_files": ground_truth_files,
            "jdk": "17",
            "test_patch": str(test_patch_path.relative_to(output_dir)),
        }
        tasks.append(task)
        task_records.append(
            {
                "task_id": task["task_id"],
                "project": project,
                "bug_id": bug_id,
                "difficulty": difficulty,
                "buggy_revision": buggy,
                "fixed_revision": fixed,
                "trigger_tests_sha256": _sha256(trigger),
                "relevant_tests_sha256": _sha256(relevant),
                "patch_sha256": _sha256(patch),
                "test_patch_sha256": _sha256(test_patch),
                "ground_truth_file_count": len(ground_truth_files),
                "patch_hunk_count": len(re.findall(r"^@@ ", patch, re.M)),
            }
        )

    tasks_path = output_dir / "tasks.json"
    tasks_path.write_text(
        json.dumps(tasks, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "benchmark": "defects4j_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "defects4j_commit": D4J_COMMIT,
        "source": "https://github.com/rjust/defects4j",
        "task_count": len(tasks),
        "tasks_sha256": hashlib.sha256(tasks_path.read_bytes()).hexdigest(),
        "composition": {
            difficulty: sum(
                task["external_difficulty"] == difficulty
                for task in tasks
            )
            for difficulty in ("local", "cross_file", "structural")
        },
        "difficulty_rules": {
            "local": (
                "One changed source file and one patch hunk with a direct "
                "triggering failure."
            ),
            "cross_file": (
                "Two to three changed source files or two to three patch "
                "hunks requiring coordinated edits."
            ),
            "structural": (
                "At least four changed files, at least four hunks, or a "
                "broad relevant-test closure (>=60 tests)."
            ),
        },
        "tasks": task_records,
    }
    (output_dir / "benchmark_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(tasks_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
