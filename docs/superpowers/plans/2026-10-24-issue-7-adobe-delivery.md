# Issue #7 Adobe Metadata Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fail-closed, resumable Adobe XMP delivery path that transfers only confirmed Silverloupe metadata, preserves existing XMP/develop data, and can safely reapply or roll back its own field-level changes.

**Architecture:** `delivery/base.py` converts item-level decisions into explicit metadata patches and owns reject authorization, ownership, preimage, receipt, and rollback rules. `delivery/xmp.py` performs semantic field-level XMP merge into sidecars through staged writes, backups, a per-target journal, and readback; it never edits image bytes. `delivery/adobe.py` maps Lightroom Classic and Photoshop/Bridge/Camera Raw profiles to that backend only when XMP exchange was selected explicitly and the exact format/version combination is verified in the compatibility matrix; otherwise planning stays report/copy-only.

**Tech Stack:** Python 3.12, Pydantic 2 contracts, standard-library XML/hash/path/tempfile utilities, argparse, pytest, uv; ExifTool and Adobe applications are compatibility-probe dependencies, not hidden runtime fallbacks.

**Spec:** `docs/superpowers/specs/2026-10-22-silverloupe-design.md` sections 7 and 9, `docs/distribution.md` sections 4 and 4.1, GitHub Issue #7.

## Global Constraints

- All development and verification commands run through `uv run --locked`.
- No command writes `.lrcat`, image bytes, develop settings, or ACR sidecars; the first backend writes only adjacent `.xmp` sidecars for explicitly verified proprietary-RAW profiles.
- Missing native capability never selects XMP automatically; the target profile names the backend explicitly.
- Unknown or unverified editor/version/format combinations remain `not-tested` and produce report/copy-only conflicts.
- Planning and staging do not mutate source or sidecar files.
- Apply verifies source and metadata preimages immediately before replacement; conflicts are skipped and journaled per target.
- Existing unknown XML, namespaces, develop settings, user ratings, labels, and keywords survive unless the plan explicitly owns the exact field or keyword element.
- Proposed reject never becomes an Adobe reject value without user/validated-policy authority or a hash matching the exact displayed reject batch.
- Rollback restores only Silverloupe-owned postimages that still match; later user/editor changes win and become conflicts.
- JPEG, TIFF, DNG, PSD, and any format absent from verified evidence are never modified in place.

## Review Focus

- Two source files with the same stem must never share one sidecar silently; Task 1 pins the planning conflict.
- A source or sidecar changed after planning must be skipped before any write; Tasks 2–3 pin both preimage checks.
- XML merge must preserve unknown fields and develop/ACR payloads while owning keywords element-by-element; Task 2 pins semantic round trips.
- A partial multi-file write followed by the same apply must resume without rewriting completed targets or duplicating keywords; Task 3 pins journal recovery and idempotency.
- Rollback or selection replacement after an external edit must preserve that edit and surface a conflict; Task 3 pins postimage-safe ownership.

---

### Task 1: Delivery Contracts, Decision Mapping, and Capability Gate

**Files:**
- Modify: `src/silverloupe/contracts.py`
- Create: `src/silverloupe/delivery/__init__.py`
- Create: `src/silverloupe/delivery/base.py`
- Create: `src/silverloupe/delivery/adobe.py`
- Create: `tests/integration/test_xmp_roundtrip.py`

**Interfaces:**
- Consumes: `Manifest`, `SelectionItem`, `Decision`, `Selection`, an explicit Adobe target profile, optional prior `DeliveryReceipt`, and optional accepted reject-batch hash.
- Produces: `reject_batch_hash(decisions: list[Decision]) -> str`; `plan_delivery(manifest: Manifest, items: list[SelectionItem], decisions: list[Decision], target: str, *, source_root: Path | None = None, output_root: Path | None = None, previous_receipt: DeliveryReceipt | None = None, accepted_reject_hash: str | None = None) -> DeliveryPlan`; versioned plan/receipt fields sufficient for resume and rollback.

- [ ] **Step 1: Write failing planning tests**

Add tests for top → 5★, select → 4★, reserve → 3★ only when no user baseline conflicts, composite expansion to all required assets, primary-only capture mapping, proposed reject → `silverloupe:suggested-reject` without reject value, user-authority reject, exact reject-batch hash acceptance, stale/wrong batch hash refusal, same-stem collision, and unsupported/unverified format report-only behavior.

- [ ] **Step 2: Run Task 1 tests and verify RED**

Run: `uv run --locked pytest tests/integration/test_xmp_roundtrip.py -q`

Expected: FAIL because the delivery package and richer transaction contracts do not exist.

- [ ] **Step 3: Extend contracts without breaking existing minimal construction**

Add defaulted plan identity/backend/status/source/output/journal fields, mapping source/metadata identities, per-outcome operation/conflict details, and receipt status/plan identity while retaining existing schema version 1 and strict validation.

- [ ] **Step 4: Implement deterministic decision-to-patch planning**

Build changes by item/asset identity, never filename alone. Use the prior receipt only for fields/keyword elements it owns; otherwise an existing non-null user value is a conflict. Hash the sorted exact proposed-reject decision payload and require equality before batch authorization.

- [ ] **Step 5: Implement explicit Adobe capability routing**

Recognize only named native or XMP modes. A missing native capability returns a conflict and never falls back; an XMP target admits only matrix rows whose editor, version family, platform, format, fields, and round-trip status are verified.

- [ ] **Step 6: Run Task 1 tests and verify GREEN**

Run: `uv run --locked pytest tests/integration/test_xmp_roundtrip.py -q`

Expected: all planning tests PASS.

### Task 2: Field-Level XMP Merge, Staging, Backup, and Readback

**Files:**
- Create: `src/silverloupe/delivery/xmp.py`
- Modify: `tests/integration/test_xmp_roundtrip.py`

**Interfaces:**
- Consumes: one ready XMP `DeliveryPlan` whose paths are relative to validated roots and whose expected preimages include source/sidecar hashes and values.
- Produces: `read_xmp(path: Path) -> dict[str, Any]`; `merge_xmp(original: bytes | None, changes: list[ProposedChange], ownership: dict[str, Any]) -> tuple[bytes, dict[str, Any]]`; staged sidecar bytes and exact semantic readback.

- [ ] **Step 1: Write failing semantic XMP tests**

Cover a missing sidecar, attributes and element-form XMP, unknown namespaces/elements, nested develop settings, arbitrary user keywords, Unicode, rating replacement under ownership, and removal/restoration of only Silverloupe-owned keywords.

- [ ] **Step 2: Verify RED**

Run: `uv run --locked pytest tests/integration/test_xmp_roundtrip.py -q`

Expected: new merge tests FAIL because `xmp.py` is absent.

- [ ] **Step 3: Implement namespace-safe merge and readback**

Parse without executing entities, preserve the existing RDF graph, update only `xmp:Rating`, `xmp:Label`, and `dc:subject/rdf:Bag` values declared by the plan, and create the minimal packet only when no sidecar exists. Invalid XML is a conflict, never replaced from scratch.

- [ ] **Step 4: Implement staging and backups**

Write staged bytes under the plan output root, fsync them, copy an existing preimage to a content-addressed backup, recheck source/sidecar hashes, then atomically replace only the adjacent sidecar. Read the target back and compare every owned postimage before recording success.

- [ ] **Step 5: Run Task 2 tests and verify GREEN**

Run: `uv run --locked pytest tests/integration/test_xmp_roundtrip.py -q`

Expected: all merge/staging/readback tests PASS.

### Task 3: Apply Resume, Ownership Replacement, and Conflict-Safe Rollback

**Files:**
- Modify: `src/silverloupe/delivery/base.py`
- Modify: `src/silverloupe/delivery/xmp.py`
- Create: `tests/integration/test_delivery_resume.py`

**Interfaces:**
- Produces: `apply_delivery(plan: DeliveryPlan) -> DeliveryReceipt`; `rollback_delivery(receipt: DeliveryReceipt) -> DeliveryReceipt`.
- Persists: append-safe journal records and a receipt containing per-field baseline, last owned postimage, backup identity, and operation IDs.

- [ ] **Step 1: Write failing transaction tests**

Add changed-source preimage, changed-sidecar preimage, injected second-file failure, resume after partial write, repeated apply, top 5★ → select 4★ from prior receipt, removal restoring the original user value, keyword element ownership, missing backup, rollback after external edit, and successful rollback of both pre-existing and newly created sidecars.

- [ ] **Step 2: Verify RED**

Run: `uv run --locked pytest tests/integration/test_delivery_resume.py -q`

Expected: FAIL because apply/resume/rollback do not exist.

- [ ] **Step 3: Implement per-target journal and idempotent resume**

Derive stable operation IDs from plan/asset/target/desired patch. Completed journal entries whose readback still matches become idempotent skips; failed or absent entries are retried. Return a partial receipt instead of pretending batch atomicity.

- [ ] **Step 4: Implement ownership-aware replacement**

When the current value equals the prior receipt postimage, update it or restore its baseline. For keywords, compare/remove only the owned elements. Any mismatch becomes a conflict and preserves current metadata.

- [ ] **Step 5: Implement conflict-safe rollback**

Require current target hash/owned values to equal the receipt postimage. Restore the exact backup when the sidecar existed, remove only a sidecar created by this operation, and report skipped/conflict on divergence or missing proof.

- [ ] **Step 6: Run Task 3 tests and verify GREEN**

Run: `uv run --locked pytest tests/integration/test_xmp_roundtrip.py tests/integration/test_delivery_resume.py -q`

Expected: all delivery transaction tests PASS.

### Task 4: CLI, Persisted Session Handoff, and Safe Output Paths

**Files:**
- Modify: `src/silverloupe/cli.py`
- Modify: `src/silverloupe/previews.py`
- Modify: `src/silverloupe/workflow.py`
- Modify: `tests/integration/test_delivery_resume.py`

**Interfaces:**
- Produces: CLI `delivery-plan OUTPUT --target TARGET [--previous-receipt PATH] [--accept-reject-hash HASH]`, `delivery-apply OUTPUT`, and `delivery-rollback OUTPUT`.
- Persists: `delivery-plan.json`, `delivery-receipt.json`, `delivery-rollback-receipt.json`, and delivery-owned staging/backup/journal directories under `OUTPUT`.

- [ ] **Step 1: Write failing CLI and path-safety tests**

Exercise paths with spaces/Unicode, stale/incomplete curation, missing explicit backend, malformed plan/receipt, symlinked delivery-owned paths, report-only plans, partial apply exit status, repeated apply, and rollback conflict summaries.

- [ ] **Step 2: Verify RED**

Run: `uv run --locked pytest tests/integration/test_delivery_resume.py -q`

Expected: CLI tests FAIL because commands and owned paths are absent.

- [ ] **Step 3: Implement thin CLI orchestration and atomic artifacts**

Load and validate session artifacts before planning, pass the session's source/output roots explicitly, publish plan/receipts atomically, and print stable JSON summaries. Use exit `0` for complete/safe report-only planning, `3` for partial/conflicted apply or rollback, `2` for invalid state, and `4` for I/O failures.

- [ ] **Step 4: Run Task 4 tests and verify GREEN**

Run: `uv run --locked pytest tests/integration/test_delivery_resume.py -q`

Expected: all CLI and path-safety tests PASS.

### Task 5: Adapter Guidance, Compatibility Evidence, and User Documentation

**Files:**
- Create: `resources/adapters/lightroom.md`
- Create: `resources/adapters/photoshop.md`
- Create: `evals/compatibility-matrix.json`
- Modify: `skills/silverloupe-deliver/SKILL.md`
- Modify: `resources/contracts.md`
- Modify: `README.md`
- Modify: `tests/contracts/test_skill_bundles.py`

**Interfaces:**
- Consumes: Tasks 1–4 and the actual local probe environment.
- Produces: closed runtime references, factual matrix rows, and documentation that distinguishes contract-tested core behavior from editor-verified compatibility.

- [ ] **Step 1: Add failing adapter/bundle/matrix contract tests**

Require schema/version/evidence/status for RAW, JPEG, TIFF, DNG, and PSD rows; allow only `verified`, `failed`, or `not-tested`; prohibit a verified row without editor/version/platform, read/write/readback evidence, develop-preservation result, fields, and fixture hashes. Require production skill references to resolve inside built bundles.

- [ ] **Step 2: Verify RED**

Run: `uv run --locked pytest tests/contracts/test_skill_bundles.py -q`

Expected: FAIL because Adobe adapters and the matrix do not exist.

- [ ] **Step 3: Document exact supported and unsupported routes**

Explain explicit XMP selection, plan/apply/rollback, conflict handling, reject authorization, backups, Lightroom refresh/import caveats, and the Photoshop Bridge/Camera Raw handoff. Keep Lightroom cloud and unverified embedded-metadata formats report-only.

- [ ] **Step 4: Record factual local compatibility results**

Record detected Lightroom Classic 15.0.1 on macOS 26.6.2 arm64, absent ExifTool, and absent Photoshop/Bridge as environment evidence. Mark every combination `not-tested` until a copied fixture completes editor write/readback and develop-preservation checks; do not infer compatibility from installation alone.

- [ ] **Step 5: Run targeted and full verification**

Run: `uv run --locked pytest tests/integration/test_xmp_roundtrip.py tests/integration/test_delivery_resume.py tests/contracts/test_skill_bundles.py -q`

Expected: PASS.

Run: `uv run --locked pytest -q`

Expected: all tests PASS.

- [ ] **Step 6: Run diagnostics and record remaining real-probe blockers**

Run: `uv run --locked silverloupe doctor`

Expected: Lightroom detection may be reported, while ExifTool and Photoshop/Bridge remain honestly unavailable; no matrix row becomes verified without a real round trip.

## Self-Review

- Spec coverage: explicit backend choice, compatibility gating, same-stem detection, reject authority/hash, field merge, staging, backup, journal, readback, partial resume, ownership replacement, rollback, CLI, adapters, and matrix each map to a task.
- Step scan: every production mutation follows a named failing test and every implementation step fixes exact interfaces or safety behavior rather than prescribing ordinary code bodies.
- Type consistency: Tasks 2–4 consume the exact enriched `DeliveryPlan`/`DeliveryReceipt` contracts and stable operation identities created in Task 1.
- Review focus: stem collisions, changed preimages, unknown-field preservation, partial resume/idempotency, and postimage-safe rollback all have assigned regressions.
- Proportion: the plan fixes safety semantics and boundaries while leaving XML traversal, serialization, and CLI plumbing details to implementation.
