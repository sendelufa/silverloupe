# Issue #6 Workflow, Skills, and Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the first reviewable Silverloupe user workflow: one orchestration command, a safe static report, validated manual corrections, six production skills, and separately built development eval tooling.

**Architecture:** `workflow.py` orchestrates the existing Tasks 1–5 functions and persists a `RunState`; it stops honestly at `partial` when host visual evidence is unavailable. `report.py` renders escaped, static HTML from persisted artifacts, while validated revision-bound overrides are converted to user-authority decisions and passed back through curation. Skills remain concise source instructions; `build_collection.py` copies and rewrites their conditional references into closed runtime bundles and emits the eval skill only in a separate dev toolkit.

**Tech Stack:** Python 3.12, argparse, Pydantic 2, standard-library HTML/JSON/path utilities, existing Pillow/NumPy runtime, pytest, uv.

**Spec:** `docs/superpowers/specs/2026-10-22-silverloupe-design.md` sections 3–4 and 7–10, `docs/evals.md` sections 5–7, and GitHub Issue #6.

## Global Constraints

- All development and verification commands run through `uv run --locked`.
- The runtime imports neither `evals` nor `tests`; eval tooling remains development-only.
- Automatic reject remains disabled; excluding an item from `top` or `selects` is not a technical reject.
- No original photograph or editor metadata is modified by `run`, `report`, or `apply-overrides`.
- Host vision absence, budget exhaustion, unsupported assets, and incomplete evidence produce `partial` plus disclosure, never invented completion.
- Production contains exactly six skills; `silverloupe-eval`, routing cases, baselines, tests, and internal plans remain outside the runtime collection.
- Installed skill links must be closed inside their bundle and may not point to the development checkout.
- Static report HTML contains no interactive review server or state-changing JavaScript; that remains Issue #11.
- Real-photo quality and compatibility claims remain `unavailable` until their separate release evals exist.

## Review Focus

- A first run with unavailable vision must produce a useful partial report and auditable vision request without `selection.json`; Task 2 pins this workflow.
- Unknown, duplicate, or stale revision override items must fail before decisions, selection, session revision, or report change; Task 2 pins the no-mutation boundary.
- Filenames, EXIF, reasons, and provenance containing HTML/script text must be escaped in the report; Task 3 includes an injection regression.
- Interrupted override/report publication must remain visibly incomplete rather than pairing a new report with stale state; Tasks 2–3 use generation markers and failure injection.
- Broken reference paths, traversal, source-checkout links, or eval/dev leakage must fail bundle construction; Task 4 validates both source and built trees.

---

### Task 1: B2 Baseline and Routing Corpus

**Files:**
- Create: `evals/cases/routing.jsonl`
- Create: `evals/baselines/issue-6-b2.jsonl`
- Create: `tests/contracts/test_skill_bundles.py`

**Interfaces:**
- Consumes: current `silverloupe` CLI without collection instructions and the B2 definition in `docs/evals.md`.
- Produces: 60 versioned routing cases and a factual B2 record containing prompt, host, model/CLI identity when available, observable actions, final response, and categorized failures; no private reasoning.

- [ ] **Step 1: Run the no-skill B2 control before creating any `skills/*/SKILL.md`**

Use the available Codex host on six fixed prompts: two positive selection requests, two negative unrelated requests, and two boundary requests. Save actual final text/tool actions and explicit `unavailable` fields when the host exposes no trace. Do not fabricate Claude results if its CLI/authentication is absent.

- [ ] **Step 2: Write failing corpus-contract tests**

Add `test_routing_corpus_has_20_positive_20_negative_20_boundary_cases`, `test_routing_corpus_is_bilingual_and_has_unique_ids`, and `test_b2_baseline_records_observed_failures_without_private_reasoning`. Assert exact category counts, both `ru` and `en`, schema version 1, and at least one concrete B2 failure category.

- [ ] **Step 3: Run the corpus tests and verify RED**

Run: `uv run --locked pytest tests/contracts/test_skill_bundles.py -q`

Expected: FAIL because routing and baseline files do not exist.

- [ ] **Step 4: Add the deterministic routing corpus and factual B2 record**

Every routing case contains `schema_version`, `case_id`, `language`, `category`, `prompt`, `expected_skill`, and `required_disclosures`. Positive cases route among the six production skills; negative cases use `expected_skill: null`; boundary cases distinguish ordinary culling from evals, delivery, retouching, unsupported editor writes, and requests lacking a source.

- [ ] **Step 5: Run the corpus tests and verify GREEN**

Run: `uv run --locked pytest tests/contracts/test_skill_bundles.py -q`

Expected: all Task 1 tests PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add evals/cases/routing.jsonl evals/baselines/issue-6-b2.jsonl tests/contracts/test_skill_bundles.py
git commit -m "test: capture routing baseline for skill collection"
```

### Task 2: Workflow State and Revision-Bound Manual Corrections

**Files:**
- Modify: `src/silverloupe/contracts.py`
- Modify: `src/silverloupe/previews.py`
- Modify: `src/silverloupe/state.py`
- Create: `src/silverloupe/workflow.py`
- Create: `tests/integration/test_workflow.py`

**Interfaces:**
- Consumes: `prepare(...)`, `group_prepared(...)`, `assess_prepared(...)`, `create_vision_request_prepared(...)`, `curate_prepared(...)`, `SessionConfig`, and existing curation override priority.
- Produces: `run_session(config: RunConfig) -> RunState`; `load_run_state(output: Path) -> RunState`; `SelectionOverride`; `apply_overrides(output: Path, overrides: list[SelectionOverride]) -> RunState`.

- [ ] **Step 1: Write failing first-run workflow tests**

Add `test_run_session_without_visual_evidence_returns_partial_and_preserves_sources`, `test_second_run_after_valid_visual_import_completes_curation`, and `test_run_state_paths_are_output_owned`. Assert the first run writes inventory/grouping/assessment/vision-request/run-state artifacts but no false completed selection, while a rerun with preserved evaluated assessments writes a complete curation snapshot.

- [ ] **Step 2: Write failing override validation and round-trip tests**

Define expectations for a strict `SelectionOverride` with `item_id`, session `revision`, `selection_scope: top|selects`, `action: include|exclude`, and non-empty `reason`. Add tests for include/exclude transitions, `top ⊆ selects`, user authority, session revision increment, preservation after rerun, and rejection of unknown/duplicate/stale overrides without byte changes to owned state.

- [ ] **Step 3: Run Task 2 tests and verify RED**

Run: `uv run --locked pytest tests/integration/test_workflow.py -q`

Expected: FAIL because `silverloupe.workflow` and `SelectionOverride` do not exist.

- [ ] **Step 4: Implement persisted orchestration**

Implement `run_session(config: RunConfig) -> RunState` for exactly one source root. Persist `run-state.json` atomically with output-owned relative artifact paths. A host-vision first run creates `vision-request.json` and returns `partial` with `visual_evidence_required`; only complete assessment evidence proceeds to `curate_prepared`. Expected partial states are returned, while invalid config/input still raises a typed validation or I/O error.

- [ ] **Step 5: Implement validated manual corrections**

Add `SelectionOverride` to contracts with cross-field validation. Convert corrections to `DecisionAuthority.USER` decisions without creating reject status: include/top → `top`, exclude/top → `select`, include/selects → `select`, exclude/selects → `reserve`. Validate all inputs before publication, use `override-generation.json` (`publishing` → `complete`), write `manual-overrides.jsonl`, rerun curation, and atomically increment `SessionConfig.revision` only for a completed generation.

- [ ] **Step 6: Add interruption regressions and owned-path protection**

Failure-inject every override publication boundary and assert the marker never becomes `complete`; add `run-state.json`, `manual-overrides.jsonl`, and `override-generation.json` to the existing symlink/path safety list.

- [ ] **Step 7: Run Task 2 tests and verify GREEN**

Run: `uv run --locked pytest tests/integration/test_workflow.py -q`

Expected: all workflow and override tests PASS.

- [ ] **Step 8: Commit Task 2**

```bash
git add src/silverloupe/contracts.py src/silverloupe/previews.py src/silverloupe/state.py src/silverloupe/workflow.py tests/integration/test_workflow.py
git commit -m "feat: orchestrate culling workflow and manual corrections"
```

### Task 3: Static Review Report and User CLI

**Files:**
- Create: `src/silverloupe/report.py`
- Create: `src/silverloupe/resources/templates/report/index.html`
- Modify: `pyproject.toml`
- Modify: `src/silverloupe/previews.py`
- Modify: `src/silverloupe/cli.py`
- Modify: `tests/integration/test_workflow.py`

**Interfaces:**
- Consumes: persisted `RunState`, `session.json`, assets/items/assessments/decisions, optional `selection.json`, and `apply_overrides(...)` from Task 2.
- Produces: `build_report(state: RunState) -> Path`; CLI `run SOURCE OUTPUT --brief BRIEF_JSON`, `report OUTPUT`, and `apply-overrides OUTPUT OVERRIDES_JSON`.

- [ ] **Step 1: Write failing report tests**

Add `test_partial_report_discloses_missing_visual_evidence`, `test_complete_report_shows_top_selects_review_reject_reserve_and_provenance`, `test_report_escapes_untrusted_text_and_uses_only_output_relative_previews`, and parameterized interruption tests. Assert a fully static UTF-8 document, no state-changing forms/scripts, visible photo-eval limitations, reasons/comparison alternatives, and marker `report-generation.json` left `publishing` on failure.

- [ ] **Step 2: Write failing CLI tests from a different CWD**

Exercise `run`, `report`, and `apply-overrides` with paths containing spaces/Unicode. Assert stable JSON summaries and exit codes `0` complete, `3` partial, `2` invalid data, and `4` I/O failure.

- [ ] **Step 3: Run the focused tests and verify RED**

Run: `uv run --locked pytest tests/integration/test_workflow.py -q`

Expected: FAIL because report rendering and CLI branches are absent.

- [ ] **Step 4: Implement escaped static report publication**

Load the packaged template with `importlib.resources`; construct section rows in Python and escape every artifact-derived value with `html.escape`. Publish `report/index.html` behind `report-generation.json`, and include status/disclosure, top/selects/review/reject/reserve, preview link, reason codes, score/profile provenance, alternatives, coverage, constraints, composite required assets, and unavailable real-photo metrics.

- [ ] **Step 5: Implement the three CLI branches**

Parse a strict `Brief` document for `run`; do not silently repair invalid JSON. `report` loads `run-state.json`; `apply-overrides` parses a JSON array of `SelectionOverride`, applies it, rebuilds the report, and prints the new revision/report path. Keep CLI orchestration thin and reuse Task 2 functions.

- [ ] **Step 6: Run Task 3 tests and verify GREEN**

Run: `uv run --locked pytest tests/integration/test_workflow.py -q`

Expected: all workflow/report/CLI tests PASS.

- [ ] **Step 7: Commit Task 3**

```bash
git add src/silverloupe/report.py src/silverloupe/resources/templates/report/index.html pyproject.toml src/silverloupe/previews.py src/silverloupe/cli.py tests/integration/test_workflow.py
git commit -m "feat: publish static culling report and workflow CLI"
```

### Task 4: Production Skills and Closed Bundle Builder

**Files:**
- Create: `skills/silverloupe/SKILL.md`
- Create: `skills/silverloupe-prepare/SKILL.md`
- Create: `skills/silverloupe-triage/SKILL.md`
- Create: `skills/silverloupe-compare/SKILL.md`
- Create: `skills/silverloupe-curate/SKILL.md`
- Create: `skills/silverloupe-deliver/SKILL.md`
- Create: `dev/skills/silverloupe-eval/SKILL.md`
- Create: `resources/contracts.md`
- Create: `tools/build_collection.py`
- Modify: `tests/contracts/test_skill_bundles.py`

**Interfaces:**
- Consumes: canonical runtime contracts/resources and the CLI commands from Task 3.
- Produces: `build_collection(source_root: Path, destination: Path, *, kind: Literal["runtime", "dev-toolkit"]) -> dict[str, Any]`; six source production skills and one dev-only eval skill.

- [ ] **Step 1: Extend failing source-skill contract tests**

Assert exact production names, valid `name`/`description` frontmatter, independent trigger wording, `silverloupe==0.1.0` runtime dependency, 80–250 line working instructions, conditional resource links, no ordinary-selection trigger in the eval skill, and no production references to `evals`, `tests`, internal plans, absolute developer paths, or absent files.

- [ ] **Step 2: Write failing bundle-builder tests**

Build runtime and dev-toolkit trees under `tmp_path`. Assert runtime contains exactly six skills and copied referenced resources, all Markdown links resolve inside the artifact, canonical bytes match, traversal/symlink inputs are rejected, repeated builds are deterministic, and `silverloupe-eval` plus routing/baseline files appear only in dev-toolkit.

- [ ] **Step 3: Run Task 4 tests and verify RED**

Run: `uv run --locked pytest tests/contracts/test_skill_bundles.py -q`

Expected: FAIL because skills and builder do not exist.

- [ ] **Step 4: Write concise progressive-disclosure instructions**

Keep `silverloupe` as the ordinary entry point; stage skills may be invoked independently. Each skill states input/output, exact safe CLI commands, stopping conditions, recommendation-only/reject rules, and what it must not claim. `silverloupe-curate` reads only requested genre guides; `silverloupe-triage` reads artistic exceptions only for ambiguous evidence; `silverloupe-deliver` explicitly remains report-only until Issues #7–8 provide a verified backend.

- [ ] **Step 5: Implement deterministic bundle construction**

Copy only allowlisted files, replace source-relative resource links with bundle-local `references/...` links, hash all artifact files in a sorted manifest, and refuse an existing non-empty destination. Runtime output excludes every dev/eval path physically; dev-toolkit includes only its eval skill and routing/baseline inputs.

- [ ] **Step 6: Run Task 4 tests and verify GREEN**

Run: `uv run --locked pytest tests/contracts/test_skill_bundles.py -q`

Expected: all corpus/source/bundle tests PASS.

- [ ] **Step 7: Commit Task 4**

```bash
git add skills dev/skills resources/contracts.md tools/build_collection.py tests/contracts/test_skill_bundles.py
git commit -m "feat: add production skill collection and bundle builder"
```

### Task 5: End-to-End Evidence and Documentation

**Files:**
- Modify: `README.md`
- Create: `evals/baselines/issue-6-host-smoke.jsonl`
- Modify: `tests/integration/test_workflow.py`
- Modify: `tests/contracts/test_skill_bundles.py`

**Interfaces:**
- Consumes: Tasks 1–4 and a freshly built runtime collection.
- Produces: documented first-user workflow plus factual Codex/Claude host-smoke evidence and reference-read traces when the host exposes them.

- [ ] **Step 1: Add the full deterministic end-to-end regression**

From another CWD, run prepare/group/partial report, import a valid host-vision response, resume to complete curation/report, apply one manual correction, rerun, and assert the correction/revision survive. Also run the built runtime artifact with no access to `evals`, `tests`, or the source checkout.

- [ ] **Step 2: Update user documentation**

Document `run`, `report`, `apply-overrides`, brief/override JSON examples, partial vision handoff, report sections, static/non-interactive limitation, report-only delivery until Issues #7–8, and separation of runtime collection from dev toolkit.

- [ ] **Step 3: Run one Codex and one Claude Code smoke from clean state**

Use the same small synthetic fixture and request with the built collection. Record host/CLI versions, status, observable commands/files read, final artifact paths, and `trace_visibility: unavailable` where necessary. If a host executable/authentication is unavailable, record that exact blocker and do not label that host PASS.

- [ ] **Step 4: Visually inspect partial and complete HTML**

Open both reports in a browser, verify layout/readability and that partial disclosure, section counts, reasons, preview links, and constraints are visible without network or JavaScript. Record no screenshots or generated user run output in git.

- [ ] **Step 5: Run targeted verification**

Run: `uv run --locked pytest tests/contracts/test_skill_bundles.py tests/integration/test_workflow.py -q`

Expected: PASS.

- [ ] **Step 6: Run the complete suite and diagnostics**

Run: `uv run --locked pytest -q`

Expected: all tests PASS.

Run: `uv run --locked silverloupe doctor`

Expected: core/schema/python `ready`; optional ExifTool/raw/vision may honestly be `unavailable`.

- [ ] **Step 7: Commit Task 5**

```bash
git add README.md evals/baselines/issue-6-host-smoke.jsonl tests/integration/test_workflow.py tests/contracts/test_skill_bundles.py
git commit -m "docs: verify first user workflow and disclosures"
```

## Self-Review

- Spec coverage: six production skills, dev-only eval skill, closed references, workflow/report/overrides CLI, partial vision behavior, runtime/dev separation, B2, routing cases, and two-host evidence each map to a task.
- Step scan: every implementation step has exact signatures or fixed behavior; every mutation follows a failing test and focused verification.
- Type consistency: Task 3 consumes the exact `RunState`, `SelectionOverride`, `apply_overrides`, and `load_run_state` produced by Task 2; Task 4 uses the fixed CLI surface produced by Task 3.
- Review focus: partial vision, stale override/no mutation, HTML injection, interrupted generations, and bundle escape/leakage are each assigned concrete regressions.
- Proportion: the plan fixes cross-task interfaces and safety semantics but leaves ordinary implementation details to the executor.
