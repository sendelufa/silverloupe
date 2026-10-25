# Issue #10 Release Evals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reproducible developer-only release evaluation harness, honest release qualification/reporting, and a root-level installer entry point without claiming unavailable live-photo evidence.

**Architecture:** Pure metric functions live separately from host execution. Host adapters invoke Codex or Claude Code in isolated trial directories using their verified non-interactive CLI flags, while the runner records immutable inputs, outputs, versions, timing, and status in `TrialResult`. Release qualification inspects the exact production archive and combines deterministic checks, replay/live trial summaries, compatibility evidence, and dataset availability into a fail-closed decision. The repository-root `install.sh` is the canonical bootstrap source copied into release assets.

**Tech Stack:** Python 3.12, standard library, Pydantic 2, pytest, POSIX shell, existing deterministic release builder.

**Spec:** `docs/evals.md`, `docs/requirements.md`, `docs/distribution.md`, `docs/superpowers/specs/2026-10-22-silverloupe-design.md`, GitHub Issue #10.

## Global Constraints

- Evals, gold, tests, traces, and `silverloupe-eval` remain dev-only and absent from the production archive.
- The release remains recommendation-only while the false-reject gate lacks enough independent real shoots; auto-reject stays disabled.
- Missing hosts, credentials, trace visibility, real datasets, editor evidence, or metric denominators produce `unavailable`/`partial`, never synthetic success.
- Live trials run only against an exact versioned production artifact in clean per-trial state.
- The supported end-user platform remains macOS on Apple Silicon with Python 3.12 managed by `uv`.
- `install.sh` at repository root is the canonical reviewed bootstrap; generated release assets copy it byte-for-byte.
- Publishing tags, GitHub Releases, or remote assets is outside this plan.

## Review Focus

- Empty metric denominators return unavailable rather than inflated percentages; pin in Task 1.
- A host command that exits zero without a valid structured result is partial/failed, not passed; pin in Task 2.
- Trial output directories are unique and never overwrite previous evidence; pin in Task 2.
- Qualification rejects a mismatched archive hash/version or an archive containing dev/eval material; pin in Task 3.
- Root bootstrap and generated release bootstrap cannot drift; pin in Task 4.

---

### Task 1: Deterministic release metrics

**Files:**
- Create: `evals/metrics.py`
- Create: `tests/unit/test_metrics.py`

**Interfaces:**
- Consumes: literal gold/prediction collections and `TrialResult` values.
- Produces: `safe_ratio(numerator: int, denominator: int) -> float | None`; `ranking_metrics(...) -> dict`; `rejection_metrics(...) -> dict`; `routing_metrics(...) -> dict`; `summarize_trials(results: list[TrialResult]) -> dict`.

- [ ] **Step 1: Write failing tests for exact small examples and empty denominators**

  Cover retention/false-reject, reject precision/recall, nDCG@K, routing precision/recall, status counts, p50/p95 latency, and the zero-error one-sided 95% upper bound.

- [ ] **Step 2: Run `uv run --locked pytest tests/unit/test_metrics.py -q` and verify RED**

- [ ] **Step 3: Implement the minimal pure metric functions**

  Use hand-checkable counts and `None` for undefined ratios. The zero-error false-reject bound is `1 - 0.05 ** (1 / n)`; it does not authorize auto-reject with the current dataset.

- [ ] **Step 4: Run the metric tests and the existing grader tests; verify GREEN**

- [ ] **Step 5: Commit the metric implementation**

### Task 2: Isolated Codex and Claude Code trial harness

**Files:**
- Create: `evals/host_adapters.py`
- Modify: `evals/runner.py`
- Modify: `evals/contracts.py`
- Create: `tests/unit/test_eval_runner.py`

**Interfaces:**
- Consumes: case dictionaries with `case_id`, `prompt`, optional `artifact`, expected output contract, a host (`codex` or `claude`), variant, and an unused output root.
- Produces: `run_trial(case: dict, host: str, variant: str, output_dir: Path) -> TrialResult`; explicit host command builders using Codex `exec --ephemeral --ignore-user-config --json` and Claude `--print --no-session-persistence --output-format json`; persisted `case.json`, transcript, stderr/tool trace, and `trial-result.json`.

- [ ] **Step 1: Write failing tests with fake host executables**

  Assert command isolation, version capture, structured-output validation, nonzero exit handling, missing executable handling, and refusal to reuse a trial directory.

- [ ] **Step 2: Run `uv run --locked pytest tests/unit/test_eval_runner.py -q` and verify RED**

- [ ] **Step 3: Implement adapters and `run_trial`**

  Never use bypass-permission flags. Record exact commands without secrets, wall time, versions, paths, and parsing errors. A transcript alone is not proof of the expected behavior.

- [ ] **Step 4: Run runner/metric/grader tests and verify GREEN**

- [ ] **Step 5: Commit the trial harness**

### Task 3: Exact-artifact qualification and release decision

**Files:**
- Create: `evals/release.py`
- Create: `tests/unit/test_release_qualification.py`
- Modify: `tools/build_collection.py`

**Interfaces:**
- Consumes: versioned production archive, optional trial results/metric inputs, `evals/dataset-manifest.json`, and `evals/compatibility-matrix.json`.
- Produces: `qualify_release(artifact: Path, *, source_root: Path, evidence_root: Path | None = None) -> dict`; JSON release evidence with artifact SHA-256, manifest versions, allowlist status, deterministic test inputs, dataset/editor blockers, and `decision` (`recommendation-only`, `blocked`, or `eligible`).

- [ ] **Step 1: Write failing tests for archive provenance, forbidden content, missing datasets, and fail-closed decisions**

- [ ] **Step 2: Run `uv run --locked pytest tests/unit/test_release_qualification.py -q` and verify RED**

- [ ] **Step 3: Implement exact-artifact inspection and qualification**

  Inspect tar members safely without extracting untrusted paths. Current repository evidence must resolve to recommendation-only or blocked, never fully eligible.

- [ ] **Step 4: Run qualification, production-package, and installer tests; verify GREEN**

- [ ] **Step 5: Commit release qualification**

### Task 4: Canonical root installer

**Files:**
- Create: `install.sh`
- Delete: `packaging/install.sh`
- Modify: `tools/build_collection.py`
- Modify: `tests/contracts/test_production_package.py`
- Modify: `tests/integration/test_installer_shell.py`
- Modify: `docs/user-installation.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: repository-root `install.sh`.
- Produces: the same root script for source review/raw invocation and a byte-identical executable `install.sh` release asset.

- [ ] **Step 1: Change tests to require the root script and run them to verify RED**

- [ ] **Step 2: Move the bootstrap to the repository root and update the builder/documentation**

- [ ] **Step 3: Run shell syntax, bootstrap integration, and release-asset tests; verify GREEN**

- [ ] **Step 4: Commit the root installer move**

### Task 5: Release command, compatibility report, and reproducible local evidence

**Files:**
- Modify: `evals/runner.py`
- Create: `docs/compatibility.md`
- Create: `docs/release-report.md`
- Modify: `README.md`
- Modify: `docs/user-installation.md`
- Test: `tests/unit/test_eval_runner.py`
- Test: `tests/unit/test_release_qualification.py`

**Interfaces:**
- Consumes: suite name, hosts, variants, trial count, cases path, artifact path, and output root.
- Produces: `python -m evals.runner --suite ...` CLI, machine-readable `summary.json`, and factual reports that distinguish deterministic evidence, replay evidence, live-host evidence, real-photo evidence, editor evidence, and unavailable gates.

- [ ] **Step 1: Write failing CLI tests for validation and deterministic summary output**

- [ ] **Step 2: Run the CLI tests and verify RED**

- [ ] **Step 3: Implement the CLI and factual documentation**

  The documented full release command remains `uv run --locked python -m evals.runner --suite release --hosts codex,claude --trials 5 --artifact <archive> --output runs/release`. Do not claim live results unless produced in this repository state.

- [ ] **Step 4: Build a fresh local release and generate qualification evidence**

- [ ] **Step 5: Run the full locked test suite, shell syntax checks, release build/inspection, and `git diff --check`**

- [ ] **Step 6: Commit docs and final integration**
