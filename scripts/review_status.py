#!/usr/bin/env python3
"""Operator status snapshot for a deployed review agent.

Read-only and side-effect-free: it only queries GitHub (via `gh`) and the public
health endpoint. No secrets, no writes, nothing that can affect the live reviewer.

  python3 scripts/review_status.py \
    --repo Sundsvallskommun/example-repository \
    --health-url https://review.example.org/health

Shows: gateway health, recent /review workflow outcomes, and where to drill in
(per-run logs + the findings registry).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import urllib.request

DEFAULT_WORKFLOW_FILE = "ai-review-request.yml"
GITHUB_QUERY_TIMEOUT_SECONDS = 15


def _health(health_url: str) -> str:
    try:
        with urllib.request.urlopen(health_url, timeout=10) as response:
            body = response.read(200).decode("utf-8", "replace").strip()
            return f"OK ({response.status})  {body}"
    except Exception as exc:  # noqa: BLE001 - operator tool, report any failure
        return f"UNREACHABLE: {exc}"


def _recent_runs(repository: str, workflow_file: str):
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repository}/actions/workflows/{workflow_file}/runs?per_page=15",
            ],
            capture_output=True,
            text=True,
            timeout=GITHUB_QUERY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return (
            None,
            f"GitHub query timed out after {GITHUB_QUERY_TIMEOUT_SECONDS} seconds",
        )
    if result.returncode != 0:
        return None, result.stderr.strip()
    return json.loads(result.stdout or "{}").get("workflow_runs", []), None


_MARK = {
    "success": "WORKFLOW SUCCEEDED",
    "skipped": "WORKFLOW SKIPPED",
    "failure": "WORKFLOW FAILED",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        required=True,
        help="GitHub repository as owner/name",
    )
    parser.add_argument(
        "--health-url",
        required=True,
        help="Public gateway health URL",
    )
    parser.add_argument("--workflow-file", default=DEFAULT_WORKFLOW_FILE)
    args = parser.parse_args(argv)

    print("=== Review agent — status ===\n")
    print(f"Gateway: {_health(args.health_url)}")

    runs, error = _recent_runs(args.repo, args.workflow_file)
    if error is not None:
        print(f"\n/review triggers: could not query GitHub Actions ({error})")
        return 1

    runs = runs or []
    succeeded = sum(1 for run in runs if run.get("conclusion") == "success")
    skipped = sum(1 for run in runs if run.get("conclusion") == "skipped")
    failed = sum(1 for run in runs if run.get("conclusion") == "failure")
    print(
        f"\n/review triggers (last {len(runs)}): "
        f"{succeeded} succeeded, {skipped} skipped, {failed} failed\n"
    )
    for run in runs[:12]:
        when = str(run.get("created_at", ""))[:16].replace("T", " ")
        who = (run.get("triggering_actor") or {}).get("login", "?")
        outcome = run.get("conclusion") or run.get("status") or "?"
        mark = _MARK.get(outcome, outcome)
        title = str(run.get("display_title", ""))[:42]
        print(f"  {when}  {who:18.18}  {mark:42.42}  {title}")

    print("\nDrill in:")
    print(f"  inspect a workflow run:  gh run view <id> --repo {args.repo}")
    print(
        "  live gateway activity:   "
        "Dokploy -> review platform -> hermes-review -> Logs"
    )
    print(
        "  findings totals:         "
        f"review-agent-memory stats --repo {args.repo}   (run in the container)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
