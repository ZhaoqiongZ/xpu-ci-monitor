#!/usr/bin/env python3
"""Check PyTorch nightly CI XPU test results.

Queries GitHub Actions API for the latest XPU CI workflow runs.
Outputs failure list if any tests failed, or ALL_PASS if clean.

Supports filtering by trigger event type (schedule, push, pull_request, etc.)
and saves all collected runs for downstream bisect usage.
"""
import os
import sys
import json
import re
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


def get_latest_xpu_runs(days=1, event=None):
    """Get recent XPU CI workflow runs from the dedicated xpu workflow.

    Args:
        days: Look back N days.
        event: If set, pass as GitHub API 'event' filter (e.g. 'schedule', 'push').
               GitHub API only supports a single event value per request.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{API_BASE}/actions/workflows/{XPU_WORKFLOW_ID}/runs"
    params = {"per_page": 30, "status": "completed", "created": f">={since}"}
    if event:
        params["event"] = event
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


def summarize_run(run):
    """Extract a compact summary from a workflow run object for saving."""
    return {
        "run_id": run["id"],
        "event": run["event"],
        "head_branch": run["head_branch"],
        "head_sha": run["head_sha"],
        "conclusion": run["conclusion"],
        "created_at": run["created_at"],
        "updated_at": run["updated_at"],
        "html_url": run["html_url"],
    }


def process_runs(runs, parse_logs=False, label=""):
    """Process a list of runs: collect failures and run summaries.

    Returns (all_failures, run_summaries).
    """
    all_failures = []
    run_summaries = []

    for run in runs:
        run_summaries.append(summarize_run(run))
        commit_sha = run["head_sha"]
        event = run["event"]
        branch = run["head_branch"]
        print(f"\n  [{label}] Run: {run['name']} (commit: {commit_sha[:12]}, "
              f"event: {event}, branch: {branch}, conclusion: {run['conclusion']})")

        failed_jobs = get_failed_jobs(run["id"])
        if not failed_jobs:
            print("    All jobs passed [PASS]")
            continue

        for job in failed_jobs:
            info = parse_failure_info(job)
            info["commit_sha"] = commit_sha
            info["workflow_name"] = run["name"]
            info["event"] = event
            info["head_branch"] = branch

            if parse_logs:
                print(f"    Parsing log for: {info['job_name']}...")
                info["failed_tests"] = get_failed_test_cases(job["id"])
                for t in info["failed_tests"]:
                    print(f"      [FAIL] {t}")
            else:
                info["failed_tests"] = []

            all_failures.append(info)
            print(f"    FAIL: {info['job_name']}")
            print(f"      URL: {info['job_url']}")

    return all_failures, run_summaries


def main():
    parser = argparse.ArgumentParser(description="Check PyTorch XPU nightly CI status")
    parser.add_argument("--days", type=int, default=1, help="Look back N days")
    parser.add_argument("--output", type=str, default=None, help="Output JSON file")
    parser.add_argument("--event", type=str, default="schedule",
                        help="Primary event type to check (default: schedule). "
                             "Push runs are always collected as bisect reference.")
    parser.add_argument("--num-runs", type=int, default=2,
                        help="Number of recent non-cancelled schedule runs to compare (default: 2). "
                             "Compares latest vs previous to identify NEW failures.")
    parser.add_argument("--parse-logs", action="store_true", default=False,
                        help="Parse job logs to extract specific test case names")
    parser.add_argument("--save-all-runs", type=str, default=None,
                        help="Save all collected run summaries (schedule + push) to this JSON file "
                             "for downstream bisect usage")
    args = parser.parse_args()

    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    # --- Fetch primary event runs (default: schedule) ---
    print(f"=== Checking XPU CI [{args.event}] runs from last {args.days} day(s) ===")
    all_primary_runs = get_latest_xpu_runs(args.days, event=args.event)
    print(f"Found {len(all_primary_runs)} [{args.event}] runs")

    # Pick the N most recent non-cancelled runs
    valid_runs = [r for r in all_primary_runs if r["conclusion"] != "cancelled"]
    selected_runs = valid_runs[:args.num_runs]
    if selected_runs:
        print(f"Selected {len(selected_runs)} non-cancelled run(s):")
        for r in selected_runs:
            print(f"  {r['created_at']}  {r['head_sha'][:12]}  {r['conclusion']}")

    # Process latest run (full log parsing)
    latest_run = selected_runs[:1]
    primary_failures, primary_summaries = process_runs(
        latest_run, parse_logs=args.parse_logs, label=f"{args.event}-latest")

    # Process previous run(s) for comparison (parse logs to get test names)
    prev_runs = selected_runs[1:]
    prev_failures = []
    prev_summaries = []
    if prev_runs:
        print(f"\n=== Previous [{args.event}] run(s) for comparison ===")
        prev_failures, prev_summaries = process_runs(
            prev_runs, parse_logs=args.parse_logs, label=f"{args.event}-prev")
        primary_summaries.extend(prev_summaries)

    # --- Fetch push runs as bisect reference ---
    push_summaries = []
    if args.event != "push":
        print(f"\n=== Collecting [push] runs as bisect reference ===")
        push_runs = get_latest_xpu_runs(args.days, event="push")
        print(f"Found {len(push_runs)} [push] runs")
        # Don't parse logs for push runs (just collect summaries + basic failure info)
        _, push_summaries = process_runs(push_runs, parse_logs=False, label="push")

    # --- Deduplicate test cases ---
    latest_tests = set()
    for f in primary_failures:
        for t in f.get("failed_tests", []):
            latest_tests.add(t)

    prev_tests = set()
    for f in prev_failures:
        for t in f.get("failed_tests", []):
            prev_tests.add(t)

    new_tests = sorted(latest_tests - prev_tests)
    existing_tests = sorted(latest_tests & prev_tests)
    fixed_tests = sorted(prev_tests - latest_tests)

    # --- Build result ---
    if not primary_failures:
        print(f"\nALL_PASS (for [{args.event}] runs)")
        result = {
            "status": "ALL_PASS",
            "event": args.event,
            "failures": [],
            "unique_failed_tests": [],
            "new_failed_tests": [],
            "existing_failed_tests": [],
            "fixed_tests": [],
        }
    else:
        print(f"\n{'='*60}")
        print(f"RESULTS for [{args.event}] runs")
        print(f"{'='*60}")
        print(f"Total failed tests: {len(latest_tests)}")
        if prev_tests:
            print(f"  [NEW]      {len(new_tests)} (not in previous run)")
            print(f"  [EXISTING] {len(existing_tests)} (also failed in previous run)")
            print(f"  [FIXED]    {len(fixed_tests)} (failed before, now passing)")
            if new_tests:
                print(f"\n--- NEW Failures ---")
                for t in new_tests:
                    print(f"  [NEW]  {t}")
            if existing_tests:
                print(f"\n--- Existing Failures ---")
                for t in existing_tests:
                    print(f"  [OLD]  {t}")
            if fixed_tests:
                print(f"\n--- Fixed (was failing, now passing) ---")
                for t in fixed_tests:
                    print(f"  [FIX]  {t}")
        else:
            print("  (no previous run to compare)")
            for t in sorted(latest_tests):
                print(f"  [FAIL] {t}")

        prev_commit = prev_failures[0]["commit_sha"] if prev_failures else None
        result = {
            "status": "HAS_FAILURES",
            "event": args.event,
            "commit_sha": primary_failures[0]["commit_sha"],
            "prev_commit_sha": prev_commit,
            "failures": primary_failures,
            "unique_failed_tests": sorted(latest_tests),
            "new_failed_tests": new_tests,
            "existing_failed_tests": existing_tests,
            "fixed_tests": fixed_tests,
        }

    if args.output:
        with open(args.output, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nPrimary results saved to {args.output}")

    # --- Save all run summaries for bisect ---
    if args.save_all_runs:
        all_runs_data = {
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "days": args.days,
            "schedule_runs": primary_summaries if args.event == "schedule" else [],
            "push_runs": push_summaries if args.event != "push" else primary_summaries,
        }
        with open(args.save_all_runs, "w") as f:
            json.dump(all_runs_data, f, indent=2)
        print(f"All run summaries saved to {args.save_all_runs}")

    # Set GitHub Actions output
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"has_failures={'true' if primary_failures else 'false'}\n")
            if primary_failures:
                f.write(f"failure_count={len(primary_failures)}\n")

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
