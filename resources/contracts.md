# Silverloupe runtime contracts

This reference defines the stable runtime interface used by the production skills. The core dependency is `silverloupe==0.1.0`.

## Commands

```text
silverloupe run SOURCE OUTPUT --brief BRIEF_JSON
silverloupe report OUTPUT
silverloupe apply-overrides OUTPUT OVERRIDES_JSON
silverloupe delivery-plan OUTPUT --target TARGET
silverloupe delivery-apply OUTPUT
silverloupe delivery-rollback OUTPUT
silverloupe prepare SOURCE OUTPUT
silverloupe group OUTPUT
silverloupe assess OUTPUT
silverloupe vision-request OUTPUT
silverloupe vision-import OUTPUT RESPONSE_JSON
silverloupe curate OUTPUT
silverloupe doctor
```

`run` returns exit code `0` for a complete workflow, `3` for an honest partial workflow, `2` for invalid input, and `4` for an I/O failure. A partial result is useful but is not a final selection.

## Brief

The brief is a strict JSON object with `schema_version: 1`. Supported fields are:

- `genres`: requested genre names.
- `purpose`: the intended use.
- `top_k`: requested top size.
- `coverage_checklist`: labels supplied by the user.
- `repetition_limits`: maximum selected items for named groups.
- `allowed_artistic_techniques`: techniques explicitly allowed by the brief.
- `preselected_item_ids`: user-preselected item IDs.

Unknown fields are invalid. Do not infer people, relationships, events, locations, or commercial requirements that are absent from the brief.

## Run state

`run-state.json` contains the session ID and revision, `completed|partial|failed` status, output root, output-relative artifact paths, and disclosure errors. Treat `partial` as an explicit stopping condition.

The core artifacts are:

- `session.json`: immutable run context plus current revision.
- `assets.jsonl`: source inventory, hashes, metadata, decode state, and preview provenance.
- `groups.json`: duplicate, companion, burst, composite, and uncertain relationships.
- `selection-items.json`: item-level units used by selection.
- `assessments.jsonl`: technical and visual evidence.
- `decisions.jsonl`: recommendation or user-authority decisions.
- `selection.json`: selects, top, ranked order, coverage, and unmet constraints.
- `vision-request.json`: auditable host-vision request for a partial run.
- `report/index.html`: static review report.

All exchange schemas use `schema_version: 1`. Paths inside exchange artifacts remain relative to their declared root.

## Safety invariants

- Keep originals byte-for-byte unchanged.
- Treat automatic technical results as recommendations.
- Keep ordinary duplicate losers as `keep/reserve`, not technical reject.
- Keep unsupported, undecodable, or insufficiently observed assets in review or unassessed state.
- Keep required HDR, panorama, and focus-stack source assets together.
- Require `top_ids` to be a subset of `selects_ids`.
- Preserve user-authority decisions on rerun.
- Report unavailable vision, metrics, and integrations as unavailable.

## Vision handoff

When `run` exits `3`, inspect `vision-request.json`. A host response must echo the request ID, use only requested asset and artifact IDs, cover exactly the requested criteria for completed observations, and bind every region finding to an observed artifact. Import it with `silverloupe vision-import OUTPUT RESPONSE_JSON`, then rerun `silverloupe run` with the same source, output, and brief.

Never turn metadata, thumbnail-scale evidence, or an unavailable host trace into a visual claim. `insufficient_evidence` is a valid result.

## Manual overrides

The override file is a JSON array. Each object contains:

```json
{
  "schema_version": 1,
  "item_id": "item-id",
  "revision": 2,
  "selection_scope": "top",
  "action": "exclude",
  "reason": "Client prefers the alternate expression"
}
```

`selection_scope` is `top` or `selects`; `action` is `include` or `exclude`. Every override in one request must target the active session revision and a unique known item. Excluding from a set changes tier; it does not produce technical reject.

## Report and delivery transaction

The report is static HTML. It shows top, selects, review, reject, reserve, reasons, provenance, comparisons, coverage, constraints, protected source sets, and unavailable quality metrics. It does not mutate state.

`delivery-plan` requires completed curation and writes `delivery-plan.json` without changing source metadata. The target is explicit: a missing native capability never selects XMP. A report-only plan is a safe result for an unsupported editor/version/format.

`delivery-apply` consumes the exact persisted plan and writes `delivery-receipt.json`. The contract-tested `xmp-sidecar` backend is limited to adjacent sidecars for proprietary RAW files. It checks source and metadata preimages, preserves unknown XMP/develop properties, owns keywords element-by-element, stages and backs up bytes under the output root, atomically replaces one sidecar at a time, reads owned fields back, and journals partial progress. It never changes `.lrcat`, image bytes, or ACR sidecars.

`delivery-rollback` writes `delivery-rollback-receipt.json`. Rollback is conflict-safe: it restores an exact backup or removes a newly created sidecar only while the current postimage still matches the apply receipt. A later editor/user change is preserved and reported as a conflict.

Proposed reject does not become an Adobe reject value during ordinary delivery. It requires user or validated-policy authority, or an acceptance hash matching the exact displayed proposed-reject batch. A prior receipt is required to update or clear Silverloupe-owned fields after a selection change; user values without ownership remain conflicts.

The generic XMP transaction tests do not prove Lightroom or Photoshop compatibility. Any application profile marked `not-tested` remains report-only until its exact round trip, develop preservation, fields, flags, color labels, versions, platform, and fixture hashes are recorded.
