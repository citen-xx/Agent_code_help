"""Freeze the reproducible Phase 8 V2 Holdout from candidate metadata."""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-dir", required=True)
    parser.add_argument("--reproduction-dir", required=True)
    parser.add_argument("--selection-output", required=True)
    args = parser.parse_args()
    benchmark_dir = Path(args.benchmark_dir).resolve()
    reproduction_dir = Path(args.reproduction_dir).resolve()
    selection_output = Path(args.selection_output).resolve()
    candidates = json.loads(
        (benchmark_dir / "candidates.tasks.json").read_text(encoding="utf-8")
    )
    records = {}
    for path in reproduction_dir.glob("*reproduction.json"):
        for record in json.loads(path.read_text(encoding="utf-8")):
            records[record["task_id"]] = record
    selected = [
        task
        for task in candidates
        if records.get(task["task_id"], {}).get("reproduction_success") is True
    ]
    if not 12 <= len(selected) <= 15:
        raise RuntimeError(
            f"Holdout requires 12-15 reproducible tasks, got {len(selected)}"
        )
    tasks_path = benchmark_dir / "tasks.json"
    tasks_path.write_text(
        json.dumps(selected, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    by_id = {task["task_id"]: task for task in candidates}
    rows = []
    for task_id, task in by_id.items():
        record = records.get(task_id)
        if task_id in {item["task_id"] for item in selected}:
            status = "selected"
            reason = "buggy-fails_and_fixed-passes_reproduction"
        elif record:
            status = "excluded"
            reason = (
                record.get("rejection_reason")
                or "buggy/fixed full Maven reproduction did not satisfy criteria"
            )
        else:
            status = "not_evaluated"
            reason = "target_holdout_size_reached"
        rows.append(
            {
                "task_id": task_id,
                "project": task["project"],
                "bug_id": task["bug_id"],
                "difficulty": task["external_difficulty"],
                "status": status,
                "reason": reason,
                "reproduction": record,
                "agent_outcome_used": False,
            }
        )
    composition = {
        name: sum(
            task["external_difficulty"] == name for task in selected
        )
        for name in ("local", "cross_file", "structural")
    }
    manifest = {
        "schema_version": 1,
        "benchmark": "defects4j_v2_holdout",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "defects4j_commit": "8c16da8230843cdc918eaf4ddb449637f02b83c6",
        "source": "local pinned Defects4J metadata and project repositories",
        "task_count": len(selected),
        "tasks_sha256": _sha(tasks_path),
        "composition": composition,
        "projects": sorted({task["project"] for task in selected}),
        "selection_rule": (
            "Official metadata and reproducible buggy-fail/fixed-pass only; "
            "Agent outcomes were never consulted."
        ),
        "limitations": (
            "Cli, JacksonXml, and Jsoup candidates were attempted but did not "
            "satisfy full-suite reproduction under the pinned host Maven/JDK "
            "environment. The frozen holdout therefore has lower project "
            "diversity than preferred."
        ),
    }
    (benchmark_dir / "benchmark_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    selection_output.parent.mkdir(parents=True, exist_ok=True)
    selection_output.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "benchmark": "defects4j_v2_holdout",
                "manifest_sha256": _sha(tasks_path),
                "candidates": rows,
                "selected": len(selected),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(tasks_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
