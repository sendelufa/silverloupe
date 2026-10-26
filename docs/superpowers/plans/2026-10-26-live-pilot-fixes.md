# Live Pilot Reliability Fixes Implementation Plan

**Goal:** Make the real RAW+JPEG pilot complete through the canonical Silverloupe pipeline and prevent a partial temporary run from being presented as a finished deliverable.

**Architecture:** Keep asset-level evidence honest while evaluating a RAW+JPEG companion as one capture item. Group only unique same-directory, same-stem pairs when metadata does not contradict the relationship; prefer the member with a safe preview as the capture representative; derive workflow completion from item-level evidence. Tighten the production skills so the canonical output directory is durable and a missing canonical selection can never be replaced by an ad-hoc “top”.

**Tech Stack:** Python 3.12, Pydantic contracts, pytest, Markdown production skills.

## Constraints

- Never merge ambiguous stems or pairs with conflicting camera/timestamp metadata.
- Never claim that the RAW pixels were inspected when only the companion JPEG was observed.
- Keep unsupported RAW assets in the manifest and linked to the capture item.
- Preserve canonical artifacts in the user-facing output directory; do not use a system temporary directory for the normal workflow.
- A partial run or missing `selection.json` must remain visibly partial.

### Task 1: Capture identity and representative evidence

**Files:**
- Modify: `tests/unit/test_grouping.py`
- Modify: `tests/unit/test_triage.py`
- Modify: `src/silverloupe/grouping.py`
- Modify: `src/silverloupe/triage.py`

- [x] Add failing tests for exact unique RAW+JPEG fallback grouping, contradictory metadata, preview-backed primary selection, and item-level visual completion.
- [x] Implement conservative grouping and representative evidence rules.
- [x] Run focused unit tests.

### Task 2: End-to-end workflow regression

**Files:**
- Modify: `tests/integration/test_workflow.py`
- Modify: `src/silverloupe/triage.py`
- Modify: `src/silverloupe/workflow.py` only if the regression requires clearer state disclosure.

- [x] Add a live-shaped fixture with a valid JPEG and unsupported same-stem RAW.
- [x] Prove vision import completes the capture and the second run creates canonical `selection.json`.
- [x] Preserve honest asset-level unassessed counts for the RAW member.

### Task 3: Production skill guardrails

**Files:**
- Modify: `skills/silverloupe/SKILL.md`
- Modify: `skills/silverloupe-prepare/SKILL.md`
- Modify: `skills/silverloupe-curate/SKILL.md`
- Modify: `skills/silverloupe-deliver/SKILL.md`
- Modify: `tests/contracts/test_skill_bundles.py`

- [x] Require the canonical pipeline to run directly in a durable output directory.
- [x] Define the complete output tree, not copied HTML, as the deliverable.
- [x] Forbid ad-hoc top/select artifacts when canonical curation is absent or partial.
- [x] Validate the rebuilt production skill collection.

### Task 4: Verification

- [x] Run focused grouping, triage, workflow, and skill-bundle tests.
- [x] Run the full test suite.
- [x] Build a production release and verify the packaged skills contain the guardrails.
- [x] Review the final diff and working-tree state before handoff.
