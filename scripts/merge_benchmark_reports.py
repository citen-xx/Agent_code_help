"""Merge benchmark reports by task_id and recompute aggregate metrics."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.evaluation.models import BenchmarkReport
from app.evaluation.runner import BenchmarkRunner


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="append", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    tasks = {}
    results = {}
    started_at = None
    finished_at = None
    for raw_path in args.report:
        report = BenchmarkReport.model_validate_json(
            Path(raw_path).read_text(encoding="utf-8")
        )
        for task in report.tasks:
            tasks[task.task_id] = task
        for result in report.results:
            results[result.task_id] = result
        started_at = min(
            filter(None, [started_at, report.started_at])
        )
        finished_at = max(
            filter(None, [finished_at, report.finished_at])
        )

    ordered_results = [results[key] for key in sorted(results)]
    merged = BenchmarkReport(
        run_id=f"merged-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        started_at=started_at or datetime.now(timezone.utc).isoformat(),
        finished_at=finished_at or datetime.now(timezone.utc).isoformat(),
        tasks=[tasks[key] for key in sorted(tasks)],
        results=ordered_results,
        summary=BenchmarkRunner._summary(ordered_results),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(merged.model_dump_json(indent=2), encoding="utf-8")
    BenchmarkRunner.print_summary(merged)
    print(f"Report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
