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

Publish and explain the static review report. Version 0.1.0 is report-only: editor writes remain unavailable until verified metadata backends and round-trip checks are delivered by later work.

The runtime dependency is `silverloupe==0.1.0`.

## Preflight

1. Confirm `run-state.json` exists and matches the intended output directory.
2. Confirm `session.json`, assets, items, assessments, and decisions are readable.
3. Check whether the run status is completed or partial.
4. Stop before any requested Lightroom, Camera Raw, XMP, or other editor mutation.

Why: a static recommendation can be reviewed safely without claiming an integration that has not been verified.

## Contract map

If you need report sections, artifact provenance, override behavior, or the report-only boundary, read [runtime contracts](../../resources/contracts.md) completely before delivery.

Do not load genre guides during report publication. The report displays the provenance already produced by curation.

## Publish the report

Execute:

```text
silverloupe report OUTPUT
```

Open `OUTPUT/report/index.html`. The document works without a network connection or JavaScript.

The report generation marker remains `publishing` if publication is interrupted. Treat that marker as incomplete even when an older or partially replaced HTML file exists.

## Explain report status

For a completed run, describe the report as a reviewable recommendation set.

For a partial run, point to the missing visual evidence and `vision-request.json`. Do not describe empty selection sections as a completed negative judgment.

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

## Report-only boundary

Until verified delivery backends are available, respond to editor-write requests with a clear limitation:

- no Lightroom star or flag writes;
- no Camera Raw or XMP mutation;
- no fallback writer selected silently;
- no claim that an installed editor proves compatibility;
- no original image modification.

Provide the static report path as the supported deliverable.

## Completion checklist

- [ ] report generation marker is complete;
- [ ] every preview link stays inside the output tree;
- [ ] all artifact-derived text is escaped;
- [ ] partial status and missing evidence are visible;
- [ ] all five review sections are present, including empty sections;
- [ ] provenance and alternatives are visible;
- [ ] report-only and unavailable metrics are explicit.

## Stop conditions

Stop when state is stale or malformed, report publication is interrupted, a preview path escapes output, or the request requires an unverified editor mutation.

Do not start an interactive cards or swipe server. Do not represent the static report as an acceptance UI or delivery receipt.

## Common mistakes

- Opening an old HTML file without checking the generation marker.
- Treating partial as completed because the report renders.
- Omitting empty review/reject sections and hiding their meaning.
- Turning an override exclusion into reject.
- Promising editor synchronization because Lightroom is installed.
