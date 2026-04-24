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


import re


def get_failed_test_cases(job_id):
    """Parse job log to extract specific failed test case names.

    Looks for lines like:
      FAILED CONSISTENTLY: test/inductor/test_deterministic.py::DeterministicTest::test_mm_padding_deterministic_True
    """
    url = f"{API_BASE}/actions/jobs/{job_id}/logs"
    resp = requests.get(url, headers=HEADERS, allow_redirects=True)
    if resp.status_code != 200:
        return []
    failed_tests = []
    for line in resp.text.split("\n"):
        m = re.search(r"FAILED CONSISTENTLY:\s+(.+)", line)
        if m:
            test_id = m.group(1).strip()
            if test_id not in failed_tests:
                failed_tests.append(test_id)
    return failed_tests


def parse_failure_info(job):
    """Extract failure info from a failed job."""
    return {
        "job_name": job["name"],
        "job_id": job["id"],
        "job_url": job["html_url"],
        "run_id": job["run_id"],
        "started_at": job["started_at"],
        "completed_at": job["completed_at"],
    }


def main():
    parser = argparse.ArgumentParser(description="Check PyTorch XPU nightly CI status")
    parser.add_argument("--days", type=int, default=1, help="Look back N days")
    parser.add_argument("--output", type=str, default=None, help="Output JSON file")
    parser.add_argument("--latest-only", action="store_true", default=True,
                        help="Only check the latest non-cancelled run (default: True)")
    parser.add_argument("--parse-logs", action="store_true", default=False,
                        help="Parse job logs to extract specific test case names")
    args = parser.parse_args()

    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    print(f"Checking XPU CI runs from last {args.days} day(s)...")
    runs = get_latest_xpu_runs(args.days)
    print(f"Found {len(runs)} XPU CI runs")

    if args.latest_only:
        # Filter to only the latest non-cancelled run
        valid_runs = [r for r in runs if r["conclusion"] != "cancelled"]
        if valid_runs:
            runs = [valid_runs[0]]
            print(f"Using latest non-cancelled run only: {runs[0]['head_sha'][:12]}")

    all_failures = []
    for run in runs:
        commit_sha = run["head_sha"]
        print(f"\nRun: {run['name']} (commit: {commit_sha[:12]}, conclusion: {run['conclusion']})")
        failed_jobs = get_failed_jobs(run["id"])
        if not failed_jobs:
            print("  All jobs passed")
            continue
        for job in failed_jobs:
            info = parse_failure_info(job)
            info["commit_sha"] = commit_sha
            info["workflow_name"] = run["name"]

            # Parse job log for specific test cases
            if args.parse_logs:
                print(f"  Parsing log for: {info['job_name']}...")
                info["failed_tests"] = get_failed_test_cases(job["id"])
                for t in info["failed_tests"]:
                    print(f"    ❌ {t}")
            else:
                info["failed_tests"] = []

            all_failures.append(info)
            print(f"  FAIL: {info['job_name']}")
            print(f"    URL: {info['job_url']}")

    # Deduplicate test cases across jobs
    unique_tests = set()
    for f in all_failures:
        for t in f.get("failed_tests", []):
            unique_tests.add(t)

    if not all_failures:
        print("\nALL_PASS")
        result = {"status": "ALL_PASS", "failures": [], "unique_failed_tests": []}
    else:
        print(f"\nFOUND {len(all_failures)} failed job(s)")
        if unique_tests:
            print(f"UNIQUE FAILED TESTS: {len(unique_tests)}")
            for t in sorted(unique_tests):
                print(f"  ❌ {t}")
        result = {
            "status": "HAS_FAILURES",
            "commit_sha": all_failures[0]["commit_sha"],
            "failures": all_failures,
            "unique_failed_tests": sorted(unique_tests),
        }

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
