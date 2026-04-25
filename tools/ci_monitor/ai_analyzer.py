#!/usr/bin/env python3
"""AI analysis module for CI failure diagnosis.

Provides an abstraction layer for AI-powered analysis of CI failures.
Supports GitHub Models API (default) and OpenAI API as fallback.
"""
import os
import json
import requests


SKILL_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "skills",
                          "xpu-nightly-ci-fix", "SKILL.md")


def load_skill_context():
    """Load the xpu-nightly-ci-fix skill for prompt context."""
    paths = [
        SKILL_PATH,
        os.path.join(os.path.dirname(__file__), "SKILL.md"),
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p) as f:
                return f.read()
    return "(SKILL.md not found - using default analysis rules)"


def build_analysis_prompt(category, test_file, test_names, error_log,
                          suspect_commits=None):
    """Build the analysis prompt based on failure category and SKILL rules."""
    skill = load_skill_context()

    suspect_info = ""
    if suspect_commits:
        suspect_info = "\nSuspect commits:\n" + "\n".join(
            f"  - {c}" for c in suspect_commits[:5]
        )

    return f"""You are an XPU CI failure analysis expert. Analyze the following test failure
and provide actionable diagnosis based on the SKILL rules below.

## Failure Info
- Category (heuristic): {category}
- Test file: {test_file}
- Failed tests: {', '.join(test_names[:10])}
{suspect_info}

## Error Log (last 80 lines)
```
{error_log[-4000:] if error_log else '(no log captured)'}
```

## SKILL Rules (Step 3 - Analyze and categorize)
{skill[skill.find('### Step 3'):skill.find('### Step 4')] if '### Step 3' in skill else skill[:2000]}

## Instructions
Based on the error log and SKILL rules:
1. Confirm or correct the heuristic category
2. Identify the root cause (be specific)
3. Suggest a concrete fix direction (which file to modify, what change)
4. If this is a tolerance issue, suggest specific atol/rtol values
5. If this is a new test, state whether XPU support is needed

Respond in this JSON format:
{{
  "confirmed_category": "...",
  "root_cause": "...",
  "fix_direction": "...",
  "files_to_modify": ["..."],
  "confidence": "high/medium/low",
  "notes": "..."
}}"""


class GitHubModelsAnalyzer:
    """Analyze failures using GitHub Models API (models.github.ai)."""

    API_URL = "https://models.github.ai/inference/chat/completions"

    def __init__(self, token=None, model="openai/gpt-4o-mini"):
        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        self.model = model

    def analyze(self, category, test_file, test_names, error_log,
                suspect_commits=None):
        """Send analysis request to GitHub Models API.

        Returns dict with analysis results or error info.
        """
        prompt = build_analysis_prompt(
            category, test_file, test_names, error_log, suspect_commits
        )

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are an XPU CI failure analysis expert. Always respond with valid JSON."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 1000,
        }

        try:
            resp = requests.post(self.API_URL, headers=headers, json=payload,
                                 timeout=60)
            if resp.status_code != 200:
                return {
                    "error": f"API returned {resp.status_code}: {resp.text[:200]}",
                    "confirmed_category": category,
                    "root_cause": "AI analysis unavailable",
                    "fix_direction": "Manual analysis required",
                    "files_to_modify": [],
                    "confidence": "low",
                }

            data = resp.json()
            content = data["choices"][0]["message"]["content"]

            # Try to parse JSON from response
            # Handle markdown code blocks
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]

            return json.loads(content.strip())

        except json.JSONDecodeError:
            return {
                "confirmed_category": category,
                "root_cause": content[:500] if 'content' in dir() else "Parse error",
                "fix_direction": "See raw AI response",
                "files_to_modify": [],
                "confidence": "low",
                "raw_response": content[:1000] if 'content' in dir() else "",
            }
        except Exception as e:
            return {
                "error": str(e),
                "confirmed_category": category,
                "root_cause": "AI analysis failed",
                "fix_direction": "Manual analysis required",
                "files_to_modify": [],
                "confidence": "low",
            }


class OpenAIAnalyzer:
    """Fallback: analyze using OpenAI API directly."""

    API_URL = "https://api.openai.com/v1/chat/completions"

    def __init__(self, api_key=None, model="gpt-4o-mini"):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model

    def analyze(self, category, test_file, test_names, error_log,
                suspect_commits=None):
        prompt = build_analysis_prompt(
            category, test_file, test_names, error_log, suspect_commits
        )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are an XPU CI failure analysis expert. Always respond with valid JSON."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 1000,
        }
        try:
            resp = requests.post(self.API_URL, headers=headers, json=payload,
                                 timeout=60)
            if resp.status_code != 200:
                return {"error": f"OpenAI API returned {resp.status_code}"}
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            return json.loads(content.strip())
        except Exception as e:
            return {"error": str(e), "confirmed_category": category}


def get_analyzer(backend="github-models", **kwargs):
    """Factory: get an analyzer instance.

    Args:
        backend: "github-models" (default) or "openai"
    """
    if backend == "openai":
        return OpenAIAnalyzer(**kwargs)
    return GitHubModelsAnalyzer(**kwargs)
