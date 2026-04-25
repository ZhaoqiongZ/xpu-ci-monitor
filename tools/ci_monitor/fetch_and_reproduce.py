#!/usr/bin/env python3
"""Fetch sub-issues and reproduce failures on dev machine.

Runs on the XPU dev machine (e.g. PVC4645). Polls GitHub for sub-issues
with 'needs-repro' label, parses the REPRO_START block, executes reproduce
commands, runs AI analysis, and posts results back as issue comments.

Usage:
    # Single issue
    python fetch_and_reproduce.py --issue 5

    # All open needs-repro issues
    python fetch_and_reproduce.py --all-open

    # Watch mode (poll every N seconds)
    python fetch_and_reproduce.py --watch --interval 300

    # Dry run (parse only, don't execute or post)
    python fetch_and_reproduce.py --all-open --dry-run
"""
import os
import sys
import json
import re
import time
import argparse
import subprocess
import socket
import requests
from datetime import datetime, timezone

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}
TRACKING_REPO = os.environ.get("TRACKING_REPO", "ZhaoqiongZ/xpu-ci-monitor")
API_BASE = f"https://api.github.com/repos/{TRACKING_REPO}"

# Dev machine pytorch source directory
PYTORCH_DIR = os.environ.get("PYTORCH_DIR", os.path.expanduser("~/pytorch"))


def get_issues_needing_repro():
    """Fetch all open issues with 'needs-repro' label."""
    url = f"{API_BASE}/issues"
    params = {"labels": "needs-repro", "state": "open", "per_page": 50}
    resp = requests.get(url, headers=HEADERS, params=params)
    if resp.status_code != 200:
        print(f"ERROR: Failed to fetch issues: {resp.status_code}")
        return []
    return resp.json()


def get_issue(issue_number):
    """Fetch a single issue by number."""
    url = f"{API_BASE}/issues/{issue_number}"
    resp = requests.get(url, headers=HEADERS)
    if resp.status_code != 200:
        print(f"ERROR: Failed to fetch issue #{issue_number}: {resp.status_code}")
        return None
    return resp.json()


def parse_repro_block(issue_body):
    """Extract REPRO_START/REPRO_END JSON from issue body.

    Returns parsed dict or None if not found.
    """
    pattern = r"<!-- REPRO_START -->\s*```json\s*\n(.*?)\n```\s*<!-- REPRO_END -->"
    match = re.search(pattern, issue_body, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as e:
        print(f"  WARNING: Failed to parse REPRO JSON: {e}")
        return None


def run_command(cmd, cwd=None, timeout=1800):
    """Run a shell command and capture output.

    Returns (return_code, stdout+stderr last 80 lines).
    """
    print(f"  $ {cmd}")
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd,
            capture_output=True, text=True, timeout=timeout,
            executable='/bin/bash'
        )
        output = result.stdout + result.stderr
        lines = output.strip().split("\n")
        tail = "\n".join(lines[-80:])
        return result.returncode, tail
    except subprocess.TimeoutExpired:
        return -1, f"TIMEOUT after {timeout}s"
    except Exception as e:
        return -1, str(e)


def reproduce_issue(repro_data, dry_run=False):
    """Execute reproduce commands from repro_data.

    Returns dict with reproduce results per test.
    """
    commit_sha = repro_data["commit_sha"]
    test_file = repro_data["test_file"]
    test_names = repro_data.get("test_names", [])
    commands = repro_data.get("repro_commands", [])

    results = {
        "commit_sha": commit_sha,
        "test_file": test_file,
        "machine": socket.gethostname(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "build_status": "skipped",
        "test_results": [],
    }

    if dry_run:
        print(f"  [DRY RUN] Would execute {len(commands)} commands")
        for cmd in commands:
            print(f"    $ {cmd}")
        results["build_status"] = "dry_run"
        return results

    # Step 1: checkout + submodule sync
    print(f"  Checking out {commit_sha[:12]}...")
    rc, out = run_command(
        f"git fetch origin && git checkout {commit_sha} && git submodule sync && git submodule update --init --recursive",
        cwd=PYTORCH_DIR, timeout=300
    )
    if rc != 0:
        print(f"  ERROR: checkout failed (rc={rc})")
        results["build_status"] = "checkout_failed"
        results["build_log"] = out
        return results

    # Step 2: build (BKC: bdist_wheel + pip install)
    print("  Building PyTorch (this may take a while)...")
    rc, out = run_command(
        "source .env && python setup.py clean && rm -rf dist && python setup.py bdist_wheel && pip install --force-reinstall dist/torch-*.whl",
        cwd=PYTORCH_DIR, timeout=7200
    )
    if rc != 0:
        print(f"  ERROR: build failed (rc={rc})")
        results["build_status"] = "build_failed"
        results["build_log"] = out[-2000:]
        return results

    results["build_status"] = "success"

    # Step 3: run each test
    for name in test_names:
        print(f"  Running test: {name}...")
        cmd = f"source .env && python {test_file} -k {name} 2>&1 | tail -80"
        rc, out = run_command(cmd, cwd=PYTORCH_DIR, timeout=600)
        status = "PASS" if rc == 0 else "FAIL"
        print(f"    Result: {status}")
        results["test_results"].append({
            "test_name": name,
            "status": status,
            "return_code": rc,
            "output": out,
        })

    return results


def format_result_comment(repro_data, results, ai_analysis=None):
    """Format reproduce results as a GitHub issue comment."""
    lines = [
        "## Reproduce Result",
        "",
        f"**Status:** {'REPRODUCED' if any(t['status'] == 'FAIL' for t in results['test_results']) else 'CANNOT_REPRODUCE' if results['test_results'] else results['build_status'].upper()}",
        f"**Machine:** {results['machine']}",
        f"**Commit:** `{results['commit_sha'][:12]}`",
        f"**Date:** {results['timestamp'][:10]}",
        f"**Build:** {results['build_status']}",
        "",
    ]

    if results.get("build_log") and results["build_status"] != "success":
        lines.extend([
            "### Build Log (last lines)",
            "<details>",
            "<summary>Click to expand</summary>",
            "",
            "```",
            results["build_log"][-2000:],
            "```",
            "</details>",
            "",
        ])

    if results["test_results"]:
        lines.append("### Test Results")
        lines.append("")
        for tr in results["test_results"]:
            tag = "[PASS]" if tr["status"] == "PASS" else "[FAIL]"
            lines.append(f"<details>")
            lines.append(f"<summary>{tag} {tr['test_name']}</summary>")
            lines.append("")
            lines.append("```")
            lines.append(tr["output"][-2000:])
            lines.append("```")
            lines.append("</details>")
            lines.append("")

    if ai_analysis:
        lines.extend([
            "### AI Analysis",
            "",
            f"**Category:** `{ai_analysis.get('confirmed_category', 'N/A')}`",
            f"**Confidence:** {ai_analysis.get('confidence', 'N/A')}",
            f"**Root Cause:** {ai_analysis.get('root_cause', 'N/A')}",
            f"**Fix Direction:** {ai_analysis.get('fix_direction', 'N/A')}",
        ])
        files = ai_analysis.get("files_to_modify", [])
        if files:
            lines.append(f"**Files to Modify:** {', '.join(f'`{f}`' for f in files)}")
        notes = ai_analysis.get("notes", "")
        if notes:
            lines.append(f"**Notes:** {notes}")
        if ai_analysis.get("error"):
            lines.append(f"**AI Error:** {ai_analysis['error']}")
        lines.append("")

    return "\n".join(lines)


def post_comment(issue_number, body):
    """Post a comment to an issue."""
    url = f"{API_BASE}/issues/{issue_number}/comments"
    resp = requests.post(url, headers=HEADERS, json={"body": body})
    if resp.status_code == 201:
        print(f"  Comment posted to #{issue_number}")
        return True
    else:
        print(f"  ERROR: Failed to post comment: {resp.status_code} {resp.text[:200]}")
        return False


def update_labels(issue_number, add=None, remove=None):
    """Add/remove labels on an issue."""
    if add:
        for label in add:
            url = f"{API_BASE}/issues/{issue_number}/labels"
            requests.post(url, headers=HEADERS, json={"labels": [label]})
    if remove:
        for label in remove:
            url = f"{API_BASE}/issues/{issue_number}/labels/{label}"
            requests.delete(url, headers=HEADERS)


def process_issue(issue, dry_run=False, skip_ai=False):
    """Process a single issue: parse repro block, reproduce, analyze, post."""
    issue_number = issue["number"]
    title = issue["title"]
    print(f"\n=== Processing #{issue_number}: {title} ===")

    repro_data = parse_repro_block(issue.get("body", ""))
    if not repro_data:
        print("  No REPRO_START block found, skipping")
        return False

    print(f"  Commit: {repro_data['commit_sha'][:12]}")
    print(f"  Test file: {repro_data['test_file']}")
    print(f"  Tests: {len(repro_data.get('test_names', []))}")
    print(f"  Category: {repro_data.get('category', 'unknown')}")

    # Reproduce
    results = reproduce_issue(repro_data, dry_run=dry_run)

    # AI analysis (if we have test output)
    ai_analysis = None
    if not skip_ai and not dry_run and results["test_results"]:
        print("  Running AI analysis...")
        try:
            from ai_analyzer import get_analyzer
            analyzer = get_analyzer()
            error_log = "\n".join(
                tr["output"] for tr in results["test_results"]
                if tr["status"] == "FAIL"
            )
            ai_analysis = analyzer.analyze(
                category=repro_data.get("category", "UNKNOWN"),
                test_file=repro_data["test_file"],
                test_names=repro_data.get("test_names", []),
                error_log=error_log,
                suspect_commits=repro_data.get("suspect_commits", []),
            )
            print(f"  AI result: {ai_analysis.get('confirmed_category', 'N/A')} "
                  f"({ai_analysis.get('confidence', 'N/A')})")
        except Exception as e:
            print(f"  WARNING: AI analysis failed: {e}")

    # Format and post comment
    comment = format_result_comment(repro_data, results, ai_analysis)

    if dry_run:
        print("\n  [DRY RUN] Would post comment:")
        print("  " + comment[:500].replace("\n", "\n  ") + "...")
        return True

    post_comment(issue_number, comment)

    # Update labels
    has_failures = any(t["status"] == "FAIL" for t in results["test_results"])
    if has_failures:
        update_labels(issue_number, add=["reproduced"], remove=["needs-repro"])
    elif results["build_status"] == "success":
        update_labels(issue_number, add=["cannot-reproduce"], remove=["needs-repro"])
    else:
        update_labels(issue_number, add=["build-failed"], remove=["needs-repro"])

    return True


def watch_loop(interval, dry_run=False, skip_ai=False):
    """Poll for needs-repro issues and process them."""
    print(f"=== Watch mode: polling every {interval}s ===")
    print(f"  Machine: {socket.gethostname()}")
    print(f"  PyTorch dir: {PYTORCH_DIR}")
    print(f"  Repo: {TRACKING_REPO}")
    print()

    while True:
        try:
            issues = get_issues_needing_repro()
            if issues:
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] "
                      f"Found {len(issues)} issue(s) needing repro")
                for issue in issues:
                    process_issue(issue, dry_run=dry_run, skip_ai=skip_ai)
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] No issues needing repro")
        except Exception as e:
            print(f"ERROR in watch loop: {e}")

        print(f"  Sleeping {interval}s...")
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(
        description="Fetch sub-issues and reproduce failures on dev machine"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--issue", type=int, help="Process a single issue by number")
    group.add_argument("--all-open", action="store_true",
                       help="Process all open needs-repro issues")
    group.add_argument("--watch", action="store_true",
                       help="Watch mode: poll and process continuously")

    parser.add_argument("--interval", type=int, default=300,
                        help="Poll interval in seconds for watch mode (default: 300)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and print without executing or posting")
    parser.add_argument("--skip-ai", action="store_true",
                        help="Skip AI analysis step")
    parser.add_argument("--pytorch-dir", type=str, default=None,
                        help="Override PYTORCH_DIR")
    args = parser.parse_args()

    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    global PYTORCH_DIR
    if args.pytorch_dir:
        PYTORCH_DIR = args.pytorch_dir

    if args.issue:
        issue = get_issue(args.issue)
        if issue:
            process_issue(issue, dry_run=args.dry_run, skip_ai=args.skip_ai)
    elif args.all_open:
        issues = get_issues_needing_repro()
        print(f"Found {len(issues)} issue(s) with needs-repro label")
        for issue in issues:
            process_issue(issue, dry_run=args.dry_run, skip_ai=args.skip_ai)
    elif args.watch:
        watch_loop(args.interval, dry_run=args.dry_run, skip_ai=args.skip_ai)


if __name__ == "__main__":
    main()
