#!/usr/bin/env python3
"""Create a tracking issue in the monitoring repo with CI failure results.

Reads the output JSON from check_nightly_status.py and creates a GitHub issue
with structured failure information.
"""
import os
import sys
import json
import argparse
import requests
from datetime import datetime, timezone

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}
# Where to create tracking issues (your own repo for now)
TRACKING_REPO = os.environ.get("TRACKING_REPO", "ZhaoqiongZ/xpu-ci-monitor")


def format_issue_body(data):
    """Format CI failure data into a GitHub issue body.

    Groups failures by test file with collapsible sections to stay
    within GitHub's 65536 character limit.
    """
    from collections import OrderedDict

    commit = data.get("commit_sha", "unknown")[:12]
    full_commit = data.get("commit_sha", "")
    failures = data.get("failures", [])
    unique_tests = data.get("unique_failed_tests", [])
    new_tests = data.get("new_failed_tests", [])
    existing_tests = data.get("existing_failed_tests", [])
    fixed_tests = data.get("fixed_tests", [])
    prev_commit = data.get("prev_commit_sha", "")

    n_new = len(new_tests)
    n_existing = len(existing_tests)
    n_fixed = len(fixed_tests)

    # Header
    lines = [
        "## XPU CI Nightly Status Report",
        "",
        f"**Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"**PyTorch Commit:** [`{commit}`](https://github.com/pytorch/pytorch/commit/{full_commit})",
    ]
    if prev_commit:
        prev_short = prev_commit[:12]
        lines.append(f"**Previous Commit:** [`{prev_short}`](https://github.com/pytorch/pytorch/commit/{prev_commit})")
    lines.extend([
        f"**Status:** {'ALL PASS' if data['status'] == 'ALL_PASS' else 'HAS FAILURES'}",
        f"**Failed Jobs:** {len(failures)}",
        f"**Total Failed Tests:** {len(unique_tests)}",
    ])
    if n_new > 0 or n_existing > 0:
        lines.append(f"**New Failures:** {n_new} | **Existing:** {n_existing} | **Fixed:** {n_fixed}")
    lines.extend(["", "---", ""])

    if not unique_tests:
        lines.append("All XPU tests passed! No action needed.")
        return "\n".join(lines)

    # Helper: group tests by file
    def group_by_file(test_list):
        groups = OrderedDict()
        for t in test_list:
            parts = t.split("::")
            f = parts[0] if parts else t
            if f not in groups:
                groups[f] = []
            groups[f].append(t)
        return groups

    # Helper: find job info for a test
    def find_job(test_id):
        for f in failures:
            if test_id in f.get("failed_tests", []):
                jn = f["job_name"]
                shard = "?"
                if "test (default," in jn:
                    shard = jn.split("test (default,")[1].split(",")[0].strip()
                return f["job_url"], jn, shard
        return "", "", "?"

    # --- NEW Failures Section ---
    if new_tests:
        new_groups = group_by_file(new_tests)
        lines.append(f"### NEW Failures ({n_new} tests in {len(new_groups)} file(s))")
        lines.append("")
        lines.append("| # | Test File | Count | Shard | Job |")
        lines.append("|---|-----------|:-----:|-------|-----|")
        for i, (test_file, tests) in enumerate(new_groups.items(), 1):
            short = test_file.split("/")[-1]
            url, jn, shard = find_job(tests[0])
            job_link = f"[shard {shard}]({url})" if url else f"shard {shard}"
            lines.append(f"| {i} | `{short}` | {len(tests)} | {job_link} | `{jn}` |")
        lines.append("")

        # Collapsible details per file group
        for i, (test_file, tests) in enumerate(new_groups.items(), 1):
            short = test_file.split("/")[-1]
            lines.append(f"<details>")
            lines.append(f"<summary><b>{i}. {short}</b> ({len(tests)} failures)</summary>")
            lines.append("")
            for t in tests:
                name = t.split("::")[-1]
                lines.append(f"- `{name}`")
            lines.append("")
            lines.append(f"</details>")
            lines.append("")

    # --- EXISTING Failures Section ---
    if existing_tests:
        existing_groups = group_by_file(existing_tests)
        lines.append(f"### Existing Failures ({n_existing} tests, also failed in previous run)")
        lines.append("")
        lines.append("| # | Test File | Count | Shard |")
        lines.append("|---|-----------|:-----:|-------|")
        for i, (test_file, tests) in enumerate(existing_groups.items(), 1):
            short = test_file.split("/")[-1]
            _, _, shard = find_job(tests[0])
            lines.append(f"| {i} | `{short}` | {len(tests)} | {shard} |")
        lines.append("")

        lines.append("<details>")
        lines.append("<summary>Show existing failures</summary>")
        lines.append("")
        for t in existing_tests:
            name = t.split("::")[-1]
            lines.append(f"- `{name}`")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    # --- FIXED Section ---
    if fixed_tests:
        lines.append(f"### Fixed ({n_fixed} tests, was failing, now passing)")
        lines.append("")
        lines.append("<details>")
        lines.append("<summary>Show fixed tests</summary>")
        lines.append("")
        for t in fixed_tests:
            name = t.split("::")[-1]
            lines.append(f"- `{name}`")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    # --- Fallback: if no new/existing classification available ---
    if not new_tests and not existing_tests:
        all_groups = group_by_file(unique_tests)
        lines.append(f"### All Failures ({len(unique_tests)} tests in {len(all_groups)} file(s))")
        lines.append("")
        for i, (test_file, tests) in enumerate(all_groups.items(), 1):
            short = test_file.split("/")[-1]
            lines.append(f"<details>")
            lines.append(f"<summary><b>{i}. {short}</b> ({len(tests)} failures)</summary>")
            lines.append("")
            for t in tests:
                name = t.split("::")[-1]
                lines.append(f"- `{name}`")
            lines.append("")
            lines.append(f"</details>")
            lines.append("")

    # Commands section
    lines.extend([
        "---",
        "",
        "### Commands",
        "",
        "| Command | Action |",
        "|---------|--------|",
        "| `/approve` | Submit all fixes as PR(s) to pytorch/pytorch |",
        "| `/approve N` | Submit fix for group #N only |",
        "| `/skip N` | Mark group #N for manual handling |",
        "| `/status` | Show current status |",
        "",
        "---",
        f"*Auto-generated by [xpu-ci-monitor](https://github.com/{TRACKING_REPO})*",
    ])

    return "\n".join(lines)


def check_existing_issue(commit_short):
    """Check if an issue for this commit already exists."""
    url = f"https://api.github.com/repos/{TRACKING_REPO}/issues"
    params = {"state": "open", "labels": "ci-nightly", "per_page": 20}
    resp = requests.get(url, headers=HEADERS, params=params)
    if resp.status_code == 200:
        for issue in resp.json():
            if commit_short in issue["title"]:
                return issue["number"]
    return None


def create_issue(title, body, labels=None):
    """Create a GitHub issue."""
    url = f"https://api.github.com/repos/{TRACKING_REPO}/issues"
    payload = {"title": title, "body": body, "labels": labels or []}
    resp = requests.post(url, headers=HEADERS, json=payload)
    if resp.status_code == 201:
        issue = resp.json()
        print(f"Issue created: {issue['html_url']}")
        return issue
    else:
        print(f"Failed to create issue: {resp.status_code} {resp.text[:200]}")
        return None


def update_issue(issue_number, body):
    """Update an existing GitHub issue body."""
    url = f"https://api.github.com/repos/{TRACKING_REPO}/issues/{issue_number}"
    payload = {"body": body}
    resp = requests.patch(url, headers=HEADERS, json=payload)
    if resp.status_code == 200:
        print(f"Issue #{issue_number} updated")
        return resp.json()
    else:
        print(f"Failed to update issue: {resp.status_code}")
        return None


def format_push_runs_timeline(all_runs_data):
    """Format push runs as a timeline for bisect reference."""
    push_runs = all_runs_data.get("push_runs", [])
    if not push_runs:
        return ""

    lines = [
        "### Push Runs Timeline (Bisect Reference)",
        "",
        "| Time (UTC) | Commit | Status | Link |",
        "|------------|--------|--------|------|",
    ]
    for run in push_runs:
        sha = run["head_sha"][:12]
        conclusion = run["conclusion"]
        status = "PASS" if conclusion == "success" else "FAIL" if conclusion == "failure" else conclusion.upper()
        tag = "[PASS]" if conclusion == "success" else "[FAIL]" if conclusion == "failure" else "[?]"
        created = run.get("created_at", "?")
        url = run.get("html_url", "")
        lines.append(f"| {created} | `{sha}` | {tag} {status} | [link]({url}) |")

    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Create tracking issue from CI results")
    parser.add_argument("--input", type=str, required=True, help="Input JSON from check_nightly_status.py")
    parser.add_argument("--all-runs", type=str, default=None,
                        help="all_runs.json for push runs timeline")
    parser.add_argument("--dry-run", action="store_true", help="Print issue body without creating")
    args = parser.parse_args()

    if not GITHUB_TOKEN and not args.dry_run:
        print("ERROR: GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    with open(args.input) as f:
        data = json.load(f)

    # Load push runs if available
    all_runs_data = {}
    if args.all_runs:
        with open(args.all_runs) as f:
            all_runs_data = json.load(f)

    body = format_issue_body(data)
    timeline = format_push_runs_timeline(all_runs_data)
    if timeline:
        body = body + "\n\n" + timeline
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    commit_short = data.get("commit_sha", "unknown")[:12]
    n_failures = len(data.get("unique_failed_tests", []))
    n_new = len(data.get("new_failed_tests", []))
    n_existing = len(data.get("existing_failed_tests", []))

    if data["status"] == "ALL_PASS":
        title = f"[XPU CI] {date_str} - ALL PASS ({commit_short})"
    else:
        if n_new > 0 or n_existing > 0:
            title = f"[XPU CI] {date_str} - {n_new} new, {n_existing} existing failure(s) ({commit_short})"
        else:
            title = f"[XPU CI] {date_str} - {n_failures} failure(s) ({commit_short})"

    if args.dry_run:
        print(f"=== TITLE ===\n{title}\n")
        print(f"=== BODY ===\n{body}")
        return

    # Check if an issue for the same commit already exists (update it)
    # Different commit = new issue
    issue_number = None
    existing = check_existing_issue(commit_short)
    if existing:
        print(f"Updating existing issue #{existing} (same commit {commit_short})")
        update_issue(existing, body)
        issue_number = existing
    else:
        labels = ["ci-nightly"]
        if data["status"] != "ALL_PASS":
            labels.append("has-failures")
        if n_new > 0:
            labels.append("new-failures")
        issue = create_issue(title, body, labels)
        if issue:
            issue_number = issue["number"]

    # Set GitHub Actions output for downstream steps
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output and issue_number:
        with open(gh_output, "a") as f:
            f.write(f"issue_number={issue_number}\n")


if __name__ == "__main__":
    main()
