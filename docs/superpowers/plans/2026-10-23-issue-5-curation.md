# Issue #5 Curation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic genre-aware curation that produces explainable `selects` and an ordered `top`, preserves manual decisions and protected composite inputs, and exposes the stage through `silverloupe curate`.

**Architecture:** `curation.py` owns profile loading, input validation, item-level comparative scoring, constrained greedy selection, decision synthesis, and atomic publication for prepared sessions. Genre profiles are versioned JSON data loaded only for recognized genres; narrative Markdown guides document the corresponding editorial policy. The CLI remains a thin adapter, while development-only cases record deterministic expectations and keep real-photo quality claims unavailable until expert labels exist.

**Tech Stack:** Python 3.12, Pydantic contracts already in `silverloupe.contracts`, standard-library JSON/path utilities, pytest, uv.

**Spec:** `docs/superpowers/specs/2026-10-22-silverloupe-design.md` sections 5.4, 5.6, 6, and 7; GitHub Issue #5.

## Global Constraints

- All development commands run through `uv run --locked`.
- K is counted in `SelectionItem` IDs; a composite occupies one slot and retains every `required_asset_id`.
- `Selection.top_ids` is always an ordered subset of `Selection.selects_ids`.
- Ordinary comparison losers remain `keep/reserve`; curation does not create automatic technical rejects.
- User-authority decisions and `user_override=true` take priority over generated proposals.
- Unknown genres use only genre-neutral scoring and must not inherit wedding/portrait rules.
- Missing or impossible coverage and K constraints are reported honestly in `Selection.unmet_constraints`.
- Runtime code must not import the development-only `evals` package.
- Real-photo burst-choice, nDCG, and coverage stay `unavailable` without expert labels.

## Review Focus

- Duplicate or unknown item/asset references must fail before any curation artifact is published; Task 1 validation tests pin this boundary.
- Non-finite or malformed scoring evidence must be ignored rather than destabilize ordering; Task 1 score-normalization tests cover it.
- Conflicting user overrides (duplicate item IDs or forced top beyond K) must remain deterministic and be disclosed; Task 2 override-conflict tests cover it.
- `top_k=None`, an empty shoot, and all-unassessed candidates must produce valid contracts without invented quality claims; Tasks 2 and 3 cover these inputs.
- A publication failure must not leave a new `selection.json` paired with stale decisions as if it were complete; Task 3 generation-marker tests cover interruption.

---

### Task 1: Genre Profiles and Comparative Item Scoring

**Files:**
- Create: `src/silverloupe/curation.py`
- Create: `resources/profiles/wedding.json`
- Create: `resources/profiles/architecture.json`
- Create: `resources/profiles/landscape.json`
- Create: `resources/genres/wedding.md`
- Create: `resources/genres/architecture.md`
- Create: `resources/genres/landscape.md`
- Create: `tests/unit/test_curation.py`

**Interfaces:**
- Consumes: `Manifest`, `Group`, `SelectionItem`, `Assessment`, and `Brief` from `silverloupe.contracts`.
- Produces: deterministic internal item scores and comparison provenance consumed by `curate(...)` in Task 2; profile files with `schema_version`, `profile_version`, `genre`, `criteria_weights`, `diversity_penalty`, `artistic_exceptions`, and `negative_examples`.

- [ ] **Step 1: Write failing tests for input validation and scoring**

  Add tests named `test_curation_rejects_duplicate_or_unknown_references`, `test_numeric_and_categorical_evidence_produce_stable_scores`, `test_nonfinite_or_malformed_score_evidence_is_ignored`, `test_ties_are_stable_and_record_all_alternatives`, and `test_unknown_genre_does_not_apply_wedding_portrait_rules`. Assert that known numeric criteria and categorical `acceptable/concern` evidence affect ranking, equal scores use `item_id` as the stable final key and expose alternative IDs, and an unknown genre neither loads nor emits wedding-specific reason codes.

- [ ] **Step 2: Run the new tests and verify RED**

  Run: `uv run --locked pytest tests/unit/test_curation.py -q`

  Expected: FAIL during collection because `silverloupe.curation` does not exist.

- [ ] **Step 3: Add strict profile loading and item-level comparative scoring**

  Implement resource resolution relative to the installed package/repository rather than the process CWD; validate every selected profile before use. Score each item from finite numeric criteria plus categorical visual evidence using recognized profile weights, aggregate member evidence without counting a composite as multiple top slots, and add a deterministic technical-readiness contribution. Keep scores comparative and uncalibrated; store their components and tie alternatives in provenance rather than presenting them as probabilities.

- [ ] **Step 4: Write and validate all three profiles and guides**

  Each JSON profile must include at least one artistic exception and one negative example. Each Markdown guide must cover criteria, counterexamples, escalation, coverage, and a comparison with multiple acceptable alternatives. Wedding may weight moment/expression; architecture geometry/space/material; landscape light/atmosphere/depth. No profile may turn an artistic exception into unconditional quality.

- [ ] **Step 5: Run Task 1 tests and verify GREEN**

  Run: `uv run --locked pytest tests/unit/test_curation.py -q`

  Expected: all Task 1 tests PASS.

- [ ] **Step 6: Commit Task 1**

  ```bash
  git add src/silverloupe/curation.py resources/genres resources/profiles tests/unit/test_curation.py
  git commit -m "feat: add genre-aware comparative scoring"
  ```

### Task 2: Constrained `selects` / `top` and Manual Override Priority

**Files:**
- Modify: `src/silverloupe/curation.py`
- Modify: `tests/unit/test_curation.py`

**Interfaces:**
- Consumes: scores from Task 1 plus `overrides: list[Decision]`.
- Produces: `curate(manifest: Manifest, groups: list[Group], items: list[SelectionItem], assessments: list[Assessment], brief: Brief, overrides: list[Decision]) -> tuple[list[Decision], Selection]`.

- [ ] **Step 1: Write failing tests for the selection contract**

  Add focused tests for `top_k` values 0, 1, larger than the qualified set, and `None`; `top_ids` ordering and subset membership; K counted by item IDs for a multi-asset composite; all-unassessed and empty inputs; and honest `insufficient_qualified_items` reporting.

- [ ] **Step 2: Run the focused tests and verify RED**

  Run: `uv run --locked pytest tests/unit/test_curation.py -q`

  Expected: FAIL because `curate` and constrained selection are not implemented.

- [ ] **Step 3: Implement baseline selection and decisions**

  Build `selects_ids` from reviewable/qualified items and rank them by comparative score. Treat `top_k=None` as no requested top cap and emit an empty `top_ids`; otherwise fill at most K item IDs. Assign generated decisions as `top` (rating 5), `select` (4), or `reserve` (3), never automatic reject, with score components, alternatives, genre profile versions, and required composite asset IDs in provenance.

- [ ] **Step 4: Write failing tests for diversity and mandatory coverage**

  Cover burst/near-duplicate redundancy penalties, distinct high-value items, coverage labels carried by assessment evidence, feasible mandatory coverage, missing coverage, and a coverage-vs-K conflict. Assert stable best-effort output plus explicit `coverage:<label>` and `coverage_exceeds_top_k` constraint codes.

- [ ] **Step 5: Implement constrained greedy top selection**

  Select uncovered mandatory coverage first by marginal score, then fill remaining K slots by score minus profile diversity penalties for shared burst/near-duplicate groups. Populate `Selection.coverage` with requested, satisfied, missing, and per-label item IDs. Put selected protected group IDs in `Selection.protected_sets` and keep every selected composite's `required_asset_ids` in its decision provenance.

- [ ] **Step 6: Write failing tests for manual overrides and conflict disclosure**

  Cover forced top/select/reserve/none, user reject, preservation of status/tags/reasons/rating, duplicate override IDs, unknown override IDs, and more forced-top items than K. Assert user-authority or `user_override=true` decisions win, forced top remains in selects, and K conflicts are disclosed rather than silently dropping a user choice.

- [ ] **Step 7: Implement override priority**

  Validate unique known override IDs. Apply only decisions with `decision_authority=user` or `user_override=true` as manual constraints; merge untouched generated decisions around them. Preserve manual fields verbatim, repair only the `top ⊆ selects` aggregate invariant, and report `manual_top_exceeds_top_k` when forced top membership exceeds K.

- [ ] **Step 8: Run Task 2 tests and verify GREEN**

  Run: `uv run --locked pytest tests/unit/test_curation.py -q`

  Expected: all Task 1–2 tests PASS.

- [ ] **Step 9: Commit Task 2**

  ```bash
  git add src/silverloupe/curation.py tests/unit/test_curation.py
  git commit -m "feat: build constrained curation selections"
  ```

### Task 3: Prepared-Session Publication and CLI `curate`

**Files:**
- Modify: `src/silverloupe/curation.py`
- Modify: `src/silverloupe/cli.py`
- Modify: `tests/unit/test_curation.py`

**Interfaces:**
- Consumes: a prepared output directory with complete assessment generation, `session.json`, `assets.jsonl`, `groups.json`, `selection-items.json`, `assessments.jsonl`, and existing `decisions.jsonl`.
- Produces: `curate_prepared(output: Path) -> dict[str, Any]`; atomic `decisions.jsonl`, `selection.json`, `curation-report.json`, and `curation-generation.json`; CLI `silverloupe curate OUTPUT`.

- [ ] **Step 1: Write failing CLI and publication tests**

  Test invocation from a different CWD, preservation of a prior user override, summary JSON, invalid/incomplete assessment generation, malformed inputs, and `curation-report.json` fields including `photo_eval_status: "unavailable"`.

- [ ] **Step 2: Run the CLI tests and verify RED**

  Run: `uv run --locked pytest tests/unit/test_curation.py -q`

  Expected: FAIL because the command and publisher do not exist.

- [ ] **Step 3: Implement prepared-session orchestration and atomic publication**

  Read and validate all inputs before writing. Publish a generation marker as `publishing`, atomically write the three artifacts, then mark it `complete`; derive a deterministic generation ID from artifact contents. Report counts, unmet constraints, loaded profile versions, and unavailable real-photo metrics without importing `evals`.

- [ ] **Step 4: Add interruption regression tests**

  Parameterize failures during each artifact write and final marker update. Assert the marker remains `publishing`, so mixed-generation artifacts cannot be treated as a complete curation snapshot.

- [ ] **Step 5: Add the `curate` parser and dispatcher branch**

  Parse one output path, call `curate_prepared`, print a stable JSON summary, return 0 for a complete artifact publication (including honest selection shortfall) and existing error codes 2/4 for invalid data or I/O failures.

- [ ] **Step 6: Run Task 3 tests and verify GREEN**

  Run: `uv run --locked pytest tests/unit/test_curation.py -q`

  Expected: all curation tests PASS.

- [ ] **Step 7: Commit Task 3**

  ```bash
  git add src/silverloupe/curation.py src/silverloupe/cli.py tests/unit/test_curation.py
  git commit -m "feat: publish curation results from CLI"
  ```

### Task 4: Development Cases, Documentation, and Full Verification

**Files:**
- Create: `evals/cases/curation.jsonl`
- Modify: `README.md`
- Modify: `tests/unit/test_eval_graders.py` only if a deterministic curation grader gap is demonstrated by the new cases

**Interfaces:**
- Consumes: curation artifacts from Task 3.
- Produces: replayable synthetic/adversarial curation cases and accurate public documentation; no runtime dependency on eval code.

- [ ] **Step 1: Add deterministic curation cases**

  Include ties/multiple acceptable winners, composite K accounting, feasible/impossible coverage, diversity, and manual override cases. Mark burst-choice, nDCG, and photographic coverage `unavailable` wherever expert labels are absent.

- [ ] **Step 2: Update README state and usage**

  Document `silverloupe curate OUTPUT`, the new artifacts, item-level K, `top ⊆ selects`, protected composite behavior, profile loading, manual priority, constraint disclosure, and the limit that genre quality is not validated by synthetic fixtures.

- [ ] **Step 3: Run targeted verification**

  Run: `uv run --locked pytest tests/unit/test_curation.py -q`

  Expected: PASS.

- [ ] **Step 4: Run the complete test suite**

  Run: `uv run --locked pytest -q`

  Expected: all tests PASS with no warnings or errors.

- [ ] **Step 5: Run the installed CLI diagnostic**

  Run: `uv run --locked silverloupe doctor`

  Expected: valid JSON with core and schema status `ready`; optional capabilities may honestly be `unavailable`.

- [ ] **Step 6: Review the diff against Issue #5**

  Check every issue checkbox against code, tests, resources, and reported eval availability. Confirm no real photos, generated run output, or runtime imports from `evals` were added.

- [ ] **Step 7: Commit Task 4**

  ```bash
  git add evals/cases/curation.jsonl README.md tests/unit/test_eval_graders.py
  git commit -m "docs: document curation stage and eval limits"
  ```
