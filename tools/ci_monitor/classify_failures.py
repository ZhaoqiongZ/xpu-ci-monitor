#!/usr/bin/env python3
"""Classify CI failures and create sub-issues.

Reads the CI results JSON (with new/existing classification),
queries PyTorch git history to classify each failure group,
then creates a sub-issue per group linked to the summary issue.

Classification categories (from xpu-nightly-ci-fix skill Step 3):
1. NEW_TEST - Test case was recently added; check if XPU support needed
2. UPSTREAM_REGRESSION - Community PR broke XPU path
3. XPU_BACKEND_BUG - Fix needed in torch/_inductor/ or torch-xpu-ops
4. TOLERANCE - Increase atol/rtol to match CUDA tolerances
5. SKIP_STALE - Remove stale @skipIfXpu or @expectedFailure
6. INFRA - Environment, import, or setup issue
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
TRACKING_REPO = os.environ.get("TRACKING_REPO", "ZhaoqiongZ/xpu-ci-monitor")


def get_recent_commits_for_file(file_path, days=7):
    """Get recent commits that touched a specific file in pytorch."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{API_BASE}/commits"
    params = {"path": file_path, "since": since, "per_page": 10}
    resp = requests.get(url, headers=HEADERS, params=params)
    if resp.status_code != 200:
        return []
    return resp.json()


def check_test_recently_added(test_file, test_name, days=14):
    """Check if a test was recently added by looking at commits to the test file.

    Returns (is_new, commit_info) where commit_info has PR details if found.
    """
    commits = get_recent_commits_for_file(test_file, days=days)
    for commit in commits:
        msg = commit.get("commit", {}).get("message", "")
        # Heuristic: if commit message mentions "add" or "enable" + test-related terms
        if any(kw in msg.lower() for kw in ["add test", "enable test", "new test", "add xpu"]):
            return True, {
                "sha": commit["sha"],
                "message": msg.split("\n")[0][:100],
                "author": commit.get("author", {}).get("login", "unknown"),
                "date": commit["commit"]["author"]["date"],
            }
    return False, None


def classify_failure_group(test_file, test_names, is_new_failure, days=7):
    """Classify a group of failures from the same test file.

    Args:
        test_file: e.g. "test/inductor/test_compiled_optimizers.py"
        test_names: list of full test IDs
        is_new_failure: True if these are NEW (not in previous run)
        days: look-back window for git history

    Returns dict with category, confidence, suspect_commits, reasoning.
    """
    result = {
        "test_file": test_file,
        "count": len(test_names),
        "is_new": is_new_failure,
        "category": "UNKNOWN",
        "confidence": "low",
        "suspect_commits": [],
        "reasoning": "",
    }

    # Get recent commits touching this test file
    commits = get_recent_commits_for_file(test_file, days=days)

    if not commits:
        # No recent changes to the test file itself
        # Check the source file (heuristic: test/inductor/test_foo.py -> torch/_inductor/foo.py)
        source_path = test_file_to_source(test_file)
        if source_path:
            source_commits = get_recent_commits_for_file(source_path, days=days)
            if source_commits:
                result["category"] = "UPSTREAM_REGRESSION"
                result["confidence"] = "medium"
                result["suspect_commits"] = format_commits(source_commits[:3])
                result["reasoning"] = (
                    f"No recent changes to {test_file}, but {len(source_commits)} "
                    f"recent commit(s) to {source_path}. Likely upstream regression."
                )
                return result

        # No changes to test or source
        if not is_new_failure:
            result["category"] = "XPU_BACKEND_BUG"
            result["confidence"] = "low"
            result["reasoning"] = "Existing failure with no recent code changes. Likely XPU backend issue."
        else:
            result["category"] = "UPSTREAM_REGRESSION"
            result["confidence"] = "low"
            result["reasoning"] = "New failure but no direct file changes found. May be indirect dependency."
        return result

    # There are recent commits to the test file
    latest_commit = commits[0]
    msg = latest_commit.get("commit", {}).get("message", "").lower()

    # Check if test was recently added
    is_added, add_info = check_test_recently_added(test_file, test_names[0] if test_names else "")
    if is_added and add_info:
        result["category"] = "NEW_TEST"
        result["confidence"] = "high"
        result["suspect_commits"] = [add_info]
        result["reasoning"] = f"Test appears to be recently added by {add_info['author']} ({add_info['sha'][:12]})"
        return result

    # Check for tolerance-related patterns in test names
    tolerance_patterns = ["atol", "rtol", "tolerance", "allclose", "precision"]
    if any(p in " ".join(test_names).lower() for p in tolerance_patterns):
        result["category"] = "TOLERANCE"
        result["confidence"] = "medium"
        result["suspect_commits"] = format_commits(commits[:2])
        result["reasoning"] = "Test names suggest tolerance/precision issue."
        return result

    # Default: if new failure with recent commits, likely upstream regression
    if is_new_failure:
        result["category"] = "UPSTREAM_REGRESSION"
        result["confidence"] = "high"
        result["suspect_commits"] = format_commits(commits[:3])
        result["reasoning"] = (
            f"{len(commits)} recent commit(s) to {test_file}. "
            f"Latest: {latest_commit['commit']['message'].split(chr(10))[0][:80]}"
        )
    else:
        result["category"] = "XPU_BACKEND_BUG"
        result["confidence"] = "medium"
        result["suspect_commits"] = format_commits(commits[:2])
        result["reasoning"] = "Existing failure; recent changes may or may not be related."

    return result


def test_file_to_source(test_file):
    """Heuristic: map test file path to likely source file path."""
    # test/inductor/test_foo.py -> torch/_inductor/
    m = re.match(r"test/inductor/test_(\w+)\.py", test_file)
    if m:
        return f"torch/_inductor/"
    m = re.match(r"test/test_(\w+)\.py", test_file)
    if m:
        return f"torch/{m.group(1)}.py"
    return None


def format_commits(commits):
    """Format commit list for output."""
    result = []
    for c in commits:
        result.append({
            "sha": c["sha"],
            "message": c.get("commit", {}).get("message", "").split("\n")[0][:100],
            "author": c.get("author", {}).get("login", "unknown") if c.get("author") else "unknown",
            "date": c.get("commit", {}).get("author", {}).get("date", ""),
        })
    return result


def create_sub_issue(summary_issue_num, group_num, classification, test_names,
                     commit_sha="unknown"):
    """Create a sub-issue for a failure group.

    Embeds a machine-readable REPRO_START/REPRO_END JSON block in the issue body
    so that fetch_and_reproduce.py can parse and execute reproduce commands.
    """
    cat = classification["category"]
    test_file = classification["test_file"]
    short_file = test_file.split("/")[-1] if "/" in test_file else test_file
    count = classification["count"]
    is_new = classification["is_new"]

    tag = "[NEW]" if is_new else "[EXISTING]"
    title = f"[CI Fix] {tag} {short_file} ({count} failures) - {cat} - Ref #{summary_issue_num}"

    body_lines = [
        f"## Failure Group #{group_num}",
        "",
        f"**Summary Issue:** #{summary_issue_num}",
        f"**Test File:** `{test_file}`",
        f"**Failed Tests:** {count}",
        f"**Status:** {'NEW' if is_new else 'EXISTING'}",
        f"**Category:** `{cat}`",
        f"**Confidence:** {classification['confidence']}",
        "",
        "---",
        "",
        "### Analysis",
        "",
        classification["reasoning"],
        "",
    ]

    if classification["suspect_commits"]:
        body_lines.append("### Suspect Commits")
        body_lines.append("")
        body_lines.append("| Commit | Author | Message |")
        body_lines.append("|--------|--------|---------|")
        for sc in classification["suspect_commits"]:
            sha = sc["sha"][:12]
            body_lines.append(
                f"| [`{sha}`](https://github.com/{PYTORCH_REPO}/commit/{sc['sha']}) "
                f"| @{sc['author']} | {sc['message']} |"
            )
        body_lines.append("")

    # Failed test list
    body_lines.append("### Failed Tests")
    body_lines.append("")
    if count > 20:
        body_lines.append("<details>")
        body_lines.append(f"<summary>Show all {count} tests</summary>")
        body_lines.append("")
    for t in test_names:
        name = t.split("::")[-1]
        body_lines.append(f"- `{name}`")
    if count > 20:
        body_lines.append("")
        body_lines.append("</details>")
    body_lines.append("")

    # Action items based on category
    body_lines.append("### Action Items")
    body_lines.append("")
    if cat == "NEW_TEST":
        body_lines.extend([
            "- [ ] Check if XPU support is required for this test",
            "- [ ] If yes, enable XPU support",
            "- [ ] If no, add proper skip decorator with reason",
        ])
    elif cat == "UPSTREAM_REGRESSION":
        body_lines.extend([
            "- [ ] Reproduce on dev machine",
            "- [ ] Identify the guilty commit via bisect",
            "- [ ] Apply XPU-specific workaround or fix",
            "- [ ] Ping original PR author if needed",
        ])
    elif cat == "TOLERANCE":
        body_lines.extend([
            "- [ ] Compare XPU tolerance with CUDA tolerance",
            "- [ ] Adjust atol/rtol to match CUDA if appropriate",
        ])
    elif cat == "XPU_BACKEND_BUG":
        body_lines.extend([
            "- [ ] Reproduce on dev machine",
            "- [ ] Analyze error logs",
            "- [ ] Fix in torch/_inductor/ or torch-xpu-ops",
        ])
    else:
        body_lines.extend([
            "- [ ] Reproduce on dev machine",
            "- [ ] Classify and fix",
        ])

    # Embed machine-readable reproduce instructions for fetch_and_reproduce.py
    # Extract test method names for -k filter
    test_method_names = []
    for t in test_names:
        parts = t.split("::")
        test_method_names.append(parts[-1] if len(parts) > 1 else t)

    repro_data = {
        "commit_sha": commit_sha,
        "test_file": test_file,
        "test_names": test_method_names[:20],  # cap to avoid huge JSON
        "category": cat,
        "suspect_commits": [sc["sha"] for sc in classification.get("suspect_commits", [])],
        "repro_commands": [
            f"git fetch origin && git checkout {commit_sha}",
            "source .env && python setup.py clean && pip install -e . -v --no-build-isolation",
        ] + [
            f"source .env && python {test_file} -k {name} 2>&1 | tail -80"
            for name in test_method_names[:5]  # first 5 tests
        ],
    }
    body_lines.append("")
    body_lines.append("---")
    body_lines.append("")
    body_lines.append("<!-- REPRO_START -->")
    body_lines.append("```json")
    body_lines.append(json.dumps(repro_data, indent=2))
    body_lines.append("```")
    body_lines.append("<!-- REPRO_END -->")

    body = "\n".join(body_lines)

    # Determine labels
    labels = ["ci-fix", f"category:{cat.lower()}", "needs-repro"]
    if is_new:
        labels.append("new-failure")

    url = f"https://api.github.com/repos/{TRACKING_REPO}/issues"
    payload = {"title": title, "body": body, "labels": labels}
    resp = requests.post(url, headers=HEADERS, json=payload)
    if resp.status_code == 201:
        issue = resp.json()
        print(f"  Sub-issue created: {issue['html_url']}")
        return issue
    else:
        print(f"  Failed to create sub-issue: {resp.status_code} {resp.text[:200]}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Classify failures and create sub-issues")
    parser.add_argument("--input", type=str, required=True, help="CI results JSON")
    parser.add_argument("--summary-issue", type=int, required=True,
                        help="Summary issue number to reference")
    parser.add_argument("--dry-run", action="store_true", help="Print classification without creating issues")
    parser.add_argument("--days", type=int, default=7, help="Look-back days for git history")
    args = parser.parse_args()

    if not GITHUB_TOKEN and not args.dry_run:
        print("ERROR: GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    with open(args.input) as f:
        data = json.load(f)

    commit_sha = data.get("commit_sha", "unknown")
    new_tests = data.get("new_failed_tests", [])
    existing_tests = data.get("existing_failed_tests", [])

    # Group by test file
    from collections import OrderedDict

    def group_by_file(tests):
        groups = OrderedDict()
        for t in tests:
            f = t.split("::")[0] if "::" in t else t
            if f not in groups:
                groups[f] = []
            groups[f].append(t)
        return groups

    new_groups = group_by_file(new_tests)
    existing_groups = group_by_file(existing_tests)

    print(f"=== Classifying {len(new_groups)} NEW + {len(existing_groups)} EXISTING failure groups ===")
    print(f"    Summary issue: #{args.summary_issue}")
    print()

    all_classifications = []
    group_num = 0

    # Process NEW failures first (higher priority)
    for test_file, tests in new_groups.items():
        group_num += 1
        short = test_file.split("/")[-1]
        print(f"[{group_num}] NEW: {short} ({len(tests)} tests)")
        classification = classify_failure_group(test_file, tests, is_new_failure=True, days=args.days)
        print(f"    Category: {classification['category']} (confidence: {classification['confidence']})")
        print(f"    Reasoning: {classification['reasoning']}")
        if classification["suspect_commits"]:
            for sc in classification["suspect_commits"]:
                print(f"    Suspect: {sc['sha'][:12]} by @{sc['author']} - {sc['message'][:60]}")
        all_classifications.append(classification)

        if not args.dry_run:
            create_sub_issue(args.summary_issue, group_num, classification, tests,
                             commit_sha=commit_sha)
        print()

    # Process EXISTING failures
    for test_file, tests in existing_groups.items():
        group_num += 1
        short = test_file.split("/")[-1]
        print(f"[{group_num}] EXISTING: {short} ({len(tests)} tests)")
        classification = classify_failure_group(test_file, tests, is_new_failure=False, days=args.days)
        print(f"    Category: {classification['category']} (confidence: {classification['confidence']})")
        print(f"    Reasoning: {classification['reasoning']}")
        all_classifications.append(classification)

        if not args.dry_run:
            create_sub_issue(args.summary_issue, group_num, classification, tests,
                             commit_sha=commit_sha)
        print()

    # Summary
    print("=== Classification Summary ===")
    cats = {}
    for c in all_classifications:
        cat = c["category"]
        cats[cat] = cats.get(cat, 0) + c["count"]
    for cat, count in sorted(cats.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {count} test(s)")


if __name__ == "__main__":
    main()
