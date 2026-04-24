---
name: xpu-nightly-ci-fix
description: Analyze CI nightly test failures and fix XPU test cases. Use when the user provides CI failure reports, nightly status emails, or lists of failing test cases. Covers triaging failures, reproducing locally, identifying root causes, and applying fixes.
---
# PRINCIPLE

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---
# CI Failure Analysis

Analyze CI nightly test failure reports and fix failing XPU test cases on PyTorch.

## Quick Start

User provides a CI failure report (email content, test case list, or log snippets). Follow the workflow below to triage, reproduce, and fix.


**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

## Workflow

### Step 1: Parse the failure report
- Parse the Pytorch commit_id, report_date.
- Extract the list of failing test cases (test file, class, method name)
- Group failures by test file / module

### Step 2: Reproduce locally
- fetch latest pytorch origin main if commit_id is provided in previous step, checkout to it else use origin main.
- Create a new branch for the fix, branch name: fix-<report_date>
- build pytorch:
  ```bash
  source .env
  python setup.py clean
  pip install -e . -v --no-build-isolation
  ```


- Run each failing test on this machine:
  ```bash
  source .env && python <test_file> -k <test_name> 2>&1 | tail -80
  ```
- Confirm the failure reproduces.


### Step 3: Analyze and categorize

For each failure, determine the root cause category:
- First, use git to check when the test case was added. If it was added recently, identify which commit or PR introduced it. If it's newly added, check the commit or PR for relevant changes, to see if xpu support is required. then go to fix step. otherwise proceed to categorize the failure.
1. **XPU backend bug** — Fix in `torch/_inductor/` or `third_party/torch-xpu-ops/`
2. **Tolerance too tight** — Increase atol/rtol to match CUDA tolerances
3. **Skip decorator stale** — Remove `@skipIfXpu` or `@expectedFailure` if the test now passes
4. **Community Upstream regression** — New upstream code broke XPU; needs XPU-specific workaround
5. **Test infrastructure** — Environment, import, or setup issue

### Step 4: Fix

- For newly added test case: try to enable it for XPU, if not possible, leave it skipped with a proper reason.
- For a regression test, first try to find the guilty commit by reviewing the recent commit history and identifying which change introduced the failure. Apply an XPU-specific fix if necessary.
- If can not identify the guilty commit, start analysis, compare with cuda/rocm backend, and try to find the root cause.


### Step 5: Verify

- Run the fixed test and confirm it passes
- Run the full test file to check for regressions
- Linter check:
```bash
spin fixlint
```
analysis the output log and fix the linting issues
- Stage and commit changes, commit message need to include title: [fix][xpu] xxxxx , ## Motivation, ## Solution, ## Test plan.


## Environment

- Source `.env` before running any test
- Build: `pip install -e . -v --no-build-isolation`
- Scratch files go in `agent_space/` (git-ignored)

## Best Practices

- Always reproduce before fixing
- Match upstream CUDA tolerances when adjusting XPU tolerances
- Remove unused imports when removing skip decorators
- Keep commits focused: one fix per commit
- Editable installs resolve Python from source but C++ headers from the installed location (`torch/include/`). After editing a C++ header, **manually copy** it to the installed include path.
- Delete the PCH cache (`/tmp/torchinductor_<user_name>/precompiled_headers/`) after modifying any header under `torch/csrc/inductor/cpp_wrapper/` — stale precompiled headers mask the fix.
- For C++ compile errors in AOT Inductor generated code (`CppCompileError`), read the generated `.wrapper.cpp` error message carefully — the root cause is usually in the **codegen ordering** in `cpp_wrapper_cpu.py` (e.g. a function used before its definition is emitted). Check `write_wrapper_decl()` and `generate_input_output_runtime_checks()` ordering.


## Requirements

- PyTorch built from source with XPU support
- `.env` file configured for oneAPI environment
