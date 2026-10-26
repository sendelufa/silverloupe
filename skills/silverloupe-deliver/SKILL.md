---
name: silverloupe-deliver
description: >
  Use when a user asks to publish, reopen, or explain a Silverloupe review
  report, share culling recommendations, or asks whether selected ratings can
  be sent to Lightroom or another editor. Do NOT use for interactive review,
  unverified metadata writes, retouching, or deleting photographs.
---

# Silverloupe Deliver

## Overview

Publish the static report and, only when the user explicitly selected a supported metadata route, build/apply/roll back its delivery transaction. The generic XMP sidecar core is contract-tested; no Lightroom or Photoshop compatibility is inferred from that fact. LightCraft uses a separate native, host-mediated transport.

The runtime dependency is `silverloupe==0.1.2`.

## Preflight

1. Confirm `run-state.json` exists and matches the intended output directory.
2. Confirm `session.json`, assets, items, assessments, and decisions are readable.
3. Check whether the run status is completed or partial.
4. For metadata delivery, require an explicit target and a completed curation generation. Never choose XMP because a native capability is missing.

Why: planning is non-mutating, and an unavailable or unverified editor profile can remain report-only without improvising a writer.

Resolve and compare the requested directory with the `run-state output_root`. Do not
publish a copied subset: missing canonical state, a mismatched output root, or a folder
containing only HTML/previews is not a resumable Silverloupe output.

## Contract map

If you need report sections, artifact provenance, override behavior, or transaction contracts, read [runtime contracts](../../resources/contracts.md) completely before delivery.

When the requested target is Lightroom Classic, read [the Lightroom adapter](../../resources/adapters/lightroom.md). When it is Photoshop, Bridge, or Camera Raw, read [the Photoshop adapter](../../resources/adapters/photoshop.md). When it is LightCraft, read [the LightCraft adapter](../../resources/adapters/lightcraft.md). Read only the relevant adapter.

Do not load genre guides during report publication. The report displays the provenance already produced by curation.

## Publish the report

Execute:

```text
silverloupe report OUTPUT
```

Open `SOURCE/report.html`, where `SOURCE` is the source root stored in
`OUTPUT/session.json`. This is the only published HTML report. It works without a
network connection or JavaScript and loads previews from the hidden
`SOURCE/.silverloupe-report/previews.css`, which contains no importable JPEG files.

Keep `report.html` and `.silverloupe-report` together. After the review is final, the
user may delete `OUTPUT`; the report remains readable, but the session can no longer
be resumed, corrected, audited from JSON, or rebuilt.

The report generation marker remains `publishing` if publication is interrupted. Treat that marker as incomplete even when an older or partially replaced HTML file exists.

## Explain report status

For a completed run, describe the report as a reviewable recommendation set.

For a partial run, point to the missing visual evidence and `vision-request.json`. Do not describe empty selection sections as a completed negative judgment.

If `selection.json` or a complete curation generation is absent, do not create or
publish a replacement Top/Selects page. The only supported publication is the
canonical partial report generated from the same `OUTPUT`.

For failed or malformed state, report the validation or I/O error and stop.

## Report sections

| Section | Meaning |
|---|---|
| Top | ordered strongest recommendations |
| Selects | broader useful edit excluding items already shown in Top |
| Review | items that need human or stronger evidence |
| Reject | explicit reject-status records, not ordinary duplicate losers |
| Reserve | valid alternatives or redundant items outside the main edit |

Each item may show its preview, filename, item ID, status, tier, reason codes, human reason, authority, visual status, required assets, alternatives, and score provenance.

## Audit disclosures

Point out:

- coverage requested, satisfied, and missing;
- unmet K or repetition constraints;
- profile versions and uncalibrated score provenance;
- protected composite source assets;
- user-authority corrections;
- unavailable real-photo quality metrics.

Why: delivery is trustworthy when limitations travel with the recommendation.

## Manual correction handoff

If the user requests a tier change, create a revision-bound override file and execute:

```text
silverloupe apply-overrides OUTPUT OVERRIDES_JSON
```

The command rebuilds curation and the report. Excluding an item from top or selects changes its tier; it does not mark the photograph as a technical reject.

## Metadata transaction

Build and inspect a plan before every mutation:

```text
silverloupe delivery-plan OUTPUT --target TARGET
silverloupe delivery-apply OUTPUT
silverloupe delivery-rollback OUTPUT
```

`xmp-sidecar` is an explicit low-level route for proprietary RAW sidecars. It stages bytes under `OUTPUT`, backs up an existing sidecar, checks source/sidecar preimages, merges owned fields, reads them back, and journals each target. It never changes image bytes or `.lrcat`.

For a revised selection, rebuild with `--previous-receipt DELIVERY_RECEIPT`. Proposed rejects remain a suggested-reject keyword unless they have user/validated-policy authority or `--accept-reject-hash` matches the exact batch.

Lightroom/Photoshop targets remain report-only while their adapter says `not-tested`. No fallback writer is selected silently, and installed software is not compatibility proof.

For LightCraft, do not use the file-only CLI path because Python cannot import host MCP tools. Discover `catalog.query`, `photo.rate`, `photo.flag`, and `photo.label`; build an explicit transport from host results; call `plan_lightcraft(...)`; inspect its conflicts; then call `apply_delivery(plan, transport)`. Every mutation must contain explicit integer photo IDs. Read back `rating`, `flag`, and `label` by those IDs, persist the receipt, and use `rollback_delivery(receipt, transport)` for conflict-safe restoration.

Do not infer LightCraft IDs from the current selection or from a file name alone. Missing capability gives `report_only`; unknown flag/label values are preserved; labels require an explicit configured mapping. A prior receipt is required to update or remove Silverloupe-owned values after the canonical selection changes.

Provide the static report path as the supported deliverable.

## Completion checklist

- [ ] report generation marker is complete;
- [ ] `SOURCE/report.html` and `SOURCE/.silverloupe-report/previews.css` match the hashes in the generation marker;
- [ ] every preview link stays inside the output tree;
- [ ] all artifact-derived text is escaped;
- [ ] partial status and missing evidence are visible;
- [ ] all five review sections are present, including empty sections;
- [ ] provenance and alternatives are visible;
- [ ] target/backend selection is explicit;
- [ ] plan conflicts and report-only fields are visible;
- [ ] complete/partial receipt and backup paths are recorded;
- [ ] LightCraft operations, when selected, contain explicit IDs and verified readback;
- [ ] unavailable compatibility evidence remains explicit.

## Stop conditions

Stop when state is stale or malformed, report publication is interrupted, a path escapes its root, a preimage changed, a receipt postimage changed, or the requested editor/format row is unverified.

Do not start an interactive cards or swipe server. Do not represent the static report as an acceptance UI or delivery receipt.

## Common mistakes

- Opening an old HTML file without checking the generation marker.
- Moving `report.html` without its hidden preview stylesheet.
- Publishing copied HTML or previews after leaving session state in temporary storage.
- Treating partial as completed because the report renders.
- Omitting empty review/reject sections and hiding their meaning.
- Turning an override exclusion into reject.
- Accepting a proposed reject without an exact batch hash.
- Replacing a user rating or all keywords without prior field ownership.
- Treating contract-tested XMP merge as an Adobe application round trip.
- Promising editor synchronization because Lightroom is installed.
- Mapping LightCraft photos by duplicate-prone file names or the current selection.
- Calling a LightCraft metadata route without persisting field ownership and readback.
