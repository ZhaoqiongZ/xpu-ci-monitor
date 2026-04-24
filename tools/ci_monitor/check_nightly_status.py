#!/usr/bin/env python3
"""Check PyTorch nightly CI XPU test results.

Queries GitHub Actions API for the latest XPU CI workflow runs.
Outputs failure list if any tests failed, or ALL_PASS if clean.
"""
import os
import sys
import json
import argparse
import requests
from datetime import datetime, timedelta, timezone

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}
PYTORCH_REPO = "pytorch/pytorch"
API_BASE = f"https://api.github.com/repos/{PYTORCH_REPO}"

# XPU CI workflow ID in pytorch/pytorch
# Found via: GET /repos/pytorch/pytorch/actions/workflows -> name="xpu", path=".github/workflows/xpu.yml"
XPU_WORKFLOW_ID = 79954307


def get_latest_xpu_runs(days=1):
    """Get recent XPU CI workflow runs from the dedicated xpu workflow."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{API_BASE}/actions/workflows/{XPU_WORKFLOW_ID}/runs"
    params = {"per_page": 10, "status": "completed", "created": f">={since}"}
    resp = requests.get(url, headers=HEADERS, params=params)
    resp.raise_for_status()
    return resp.json().get("workflow_runs", [])


def get_failed_jobs(run_id):
    """Get failed jobs from a workflow run."""
    url = f"{API_BASE}/actions/runs/{run_id}/jobs"
    params = {"per_page": 100, "filter": "latest"}
    resp = requests.get(url, headers=HEADERS, params=params)
    resp.raise_for_status()
    return [j for j in resp.json().get("jobs", []) if j["conclusion"] == "failure"]


def parse_failure_info(job):
    """Extract failure info from a failed job."""
    return {
        "job_name": job["name"],
        "job_url": job["html_url"],
        "run_id": job["run_id"],
        "started_at": job["started_at"],
        "completed_at": job["completed_at"],
    }


def main():
    parser = argparse.ArgumentParser(description="Check PyTorch XPU nightly CI status")
    parser.add_argument("--days", type=int, default=1, help="Look back N days")
    parser.add_argument("--output", type=str, default=None, help="Output JSON file")
    args = parser.parse_args()

    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    print(f"Checking XPU CI runs from last {args.days} day(s)...")
    runs = get_latest_xpu_runs(args.days)
    print(f"Found {len(runs)} XPU CI runs")

    all_failures = []
    for run in runs:
        commit_sha = run["head_sha"]
        print(f"\nRun: {run['name']} (commit: {commit_sha[:12]})")
        failed_jobs = get_failed_jobs(run["id"])
        if not failed_jobs:
            print("  All jobs passed")
            continue
        for job in failed_jobs:
            info = parse_failure_info(job)
            info["commit_sha"] = commit_sha
            info["workflow_name"] = run["name"]
            all_failures.append(info)
            print(f"  FAIL: {info['job_name']}")
            print(f"    URL: {info['job_url']}")

    if not all_failures:
        print("\nALL_PASS")
        result = {"status": "ALL_PASS", "failures": []}
    else:
        print(f"\nFOUND {len(all_failures)} FAILURE(S)")
        result = {"status": "HAS_FAILURES", "failures": all_failures}

    if args.output:
        with open(args.output, "w") as f:
            json.dump(result, f, indent=2)

    # Set GitHub Actions output
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"has_failures={'true' if all_failures else 'false'}\n")
            if all_failures:
                f.write(f"failure_count={len(all_failures)}\n")

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
