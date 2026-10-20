"""Manual phase-3 acceptance: real GitHub clone + real LLM analysis.

This script is intentionally not part of pytest: it needs network access,
a configured LLM, and consumes API quota. Run from the project root:

    uv run python scripts/acceptance_phase3.py
"""

import argparse
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.main import create_app


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repository-url",
        default="https://github.com/spring-projects/spring-petclinic.git",
    )
    parser.add_argument("--branch", default="main")
    parser.add_argument(
        "--task",
        default=(
            "请先查看仓库根目录，再定位并读取 OwnerController.java，"
            "简要说明它如何处理 /owners/find。回答中请引用具体类名和方法名。"
        ),
    )
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Keep the workspace after the run for manual inspection.",
    )
    args = parser.parse_args()

    with TestClient(create_app()) as client:
        client.timeout = httpx.Timeout(600.0)

        created = client.post(
            "/workspaces",
            json={
                "repository_url": args.repository_url,
                "branch": args.branch,
                "commit": None,
            },
        )
        print("POST /workspaces:", created.status_code)
        print(created.text)
        created.raise_for_status()
        workspace = created.json()
        workspace_id = workspace["workspace_id"]

        try:
            analyzed = client.post(
                "/agent/analyze",
                json={"task": args.task, "workspace_id": workspace_id},
            )
            print("POST /agent/analyze:", analyzed.status_code)
            print(analyzed.text)
            analyzed.raise_for_status()
            result = analyzed.json()

            repository = Path(workspace["repository_path"])
            owner_controller = next(repository.rglob("OwnerController.java"), None)
            print(
                json.dumps(
                    {
                        "workspace_id": workspace_id,
                        "repository_path": str(repository),
                        "branch": workspace["branch"],
                        "commit": workspace["commit"],
                        "owner_controller_found": owner_controller is not None,
                        "owner_controller_path": (
                            owner_controller.relative_to(repository).as_posix()
                            if owner_controller
                            else None
                        ),
                        "answer": result["answer"],
                        "steps": result["steps"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            if not args.keep_workspace:
                deleted = client.delete(f"/workspaces/{workspace_id}")
                print("DELETE /workspaces/{id}:", deleted.status_code)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
