---
name: silverloupe
description: >
  Use when a user asks to cull a photo shoot, choose the best images, build a
  ranked top and broader selects, reduce near-duplicates, or review a shoot as
  a coherent story — even if they do not explicitly say "Silverloupe". Do NOT
  use for retouching, image generation, camera advice, or routing benchmarks.
---

# Silverloupe

## Overview

Run the safe end-to-end photo-culling workflow. Keep the result recommendation-only, preserve originals, and expose missing evidence instead of inventing completion.

The runtime dependency is `silverloupe==0.1.3`.

## Preflight

1. Run `silverloupe doctor`.
2. Confirm the source directory exists and contains the intended shoot.
3. Resolve one durable output directory that is separate from irreplaceable source
   content. When the user did not name one, use a clearly named sibling such as
   `SOURCE-parent/SOURCE-name-silverloupe` and disclose it; ask only if that path
   already contains unrelated data.
4. Ask for the purpose, requested `top_k`, genres, and required coverage when they affect selection.
5. Stop when the source is missing, the brief is materially ambiguous, or the output path is unsafe.

Why: the workflow can preserve files only when source and output ownership are explicit.

Never use a system temporary directory (`/tmp`, `/private/tmp`, `mktemp`, or an
equivalent) as `OUTPUT` for a normal user run. Run the canonical pipeline directly
in the durable user-facing directory; do not plan to copy selected artifacts later.

The launcher-level doctor cannot prove whether the current Codex or Claude host can
inspect local images. Treat `degraded — host vision unavailable` as an unverified
host capability, not as proof that vision is impossible. When this active host can
inspect the artifacts listed by `vision-request.json`, perform the handoff. Stop only
when the active host actually cannot inspect them.

## Contract map

If you need artifact fields, exit codes, the vision handoff, or the override schema, read [runtime contracts](../../resources/contracts.md) completely before running commands.

Do not load genre guides from this entry skill. Genre-specific evidence belongs to the curation stage.

## Build the brief

Create a strict JSON document with `schema_version: 1` and only fields supported by the runtime contract.

Use user-supplied coverage labels. Keep unknown events and identities absent.

Example:

```json
{
  "schema_version": 1,
  "genres": ["wedding"],
  "purpose": "client story",
  "top_k": 40,
  "coverage_checklist": ["ceremony", "portraits", "reception"]
}
```

## Run

Execute:

```text
silverloupe run SOURCE OUTPUT --brief BRIEF_JSON
```

Keep the same exact `SOURCE`, `OUTPUT`, and brief for every resume, vision import,
and rerun. `OUTPUT` is the resumable session, not scratch space.

Interpret the exit status:

| Exit | Meaning | Action |
|---|---|---|
| `0` | completed | Open the report and present recommendations. |
| `3` | partial | Open the report and complete the requested vision handoff. |
| `2` | invalid input | Correct the input; do not retry unchanged. |
| `4` | I/O failure | Report the filesystem error and stop. |

## Partial vision handoff

If the run is partial, open `vision-request.json` and inspect only the listed artifacts using the available host vision capability.

If `vision-request.json` is absent, do not invent a replacement inspection flow.
Explain which assets have no safe visual representation and leave the run partial.

Return observations for exactly the requested criteria. Bind every claim to an observed artifact. Mark an unclear criterion `insufficient_evidence`.

Import the response:

```text
silverloupe vision-import OUTPUT RESPONSE_JSON
silverloupe run SOURCE OUTPUT --brief BRIEF_JSON
```

Why: the second run preserves validated evidence and completes curation without pretending the first pass saw what it could not see.

## Review the result

Open `SOURCE/report.html`. This is the only published HTML report. Its preview data
lives in `SOURCE/.silverloupe-report/previews.css` as embedded data URIs; the hidden
directory contains no importable JPEG files.

Check at the point of review:

- [ ] status is `Completed` before calling the selection final;
- [ ] `run-state.json` has `status: completed` and its `output_root` resolves to this exact `OUTPUT`;
- [ ] `selection.json`, `curation-generation.json`, and `report-generation.json` exist and their generation markers are complete;
- [ ] top is a subset of selects;
- [ ] unmet coverage and K constraints are visible;
- [ ] protected composite source assets remain intact;
- [ ] review and unassessed items remain visible;
- [ ] real-photo quality metrics remain marked unavailable unless measured elsewhere.

The user-facing deliverable is `SOURCE/report.html` plus
`SOURCE/.silverloupe-report/previews.css`. Keep the canonical `OUTPUT` directory
while review, reruns, corrections, or audit access may still be needed. The final
report remains readable after `OUTPUT` is removed, but deleting `OUTPUT` also removes
resumability, detailed JSON evidence, and the ability to rebuild the report.

When the run is partial, `selection.json` is absent, or curation generation is not
complete: Do not create a substitute top, selects page, ranking JSON, or custom HTML.
Present the canonical partial report and continue the documented handoff or stop with
the limitation.

## Manual corrections

To let the user compare alternatives and replace a selected photo in a completed
session, run `silverloupe review OUTPUT` and keep it running while they use the
local browser page. Their choice updates the actual selection and report. The
standalone HTML is view-only; editing requires the saved OUTPUT directory.

Use item IDs and the current session revision. Write a JSON array that follows the runtime contract, then execute:

```text
silverloupe apply-overrides OUTPUT OVERRIDES_JSON
```

An exclusion from `top` moves the item to selects. An exclusion from `selects` moves it to reserve. Neither operation means technical reject.

## Stop conditions

Stop and disclose the limitation when vision is unavailable, evidence is insufficient, a requested editor write lacks a verified backend, or the user asks to delete originals.

Do not claim measured aesthetic quality, calibrated confidence, Lightroom compatibility, or safe automatic rejection. Do not alter photographs or editor metadata.

## Common mistakes

- Treating exit `3` as success: present it as partial and continue the vision handoff.
- Running in temporary storage and copying a presentation-only subset: use durable `OUTPUT` from the first command.
- Deleting `OUTPUT` before review is final: preserve it until no rerun or correction is needed.
- Moving `report.html` without its hidden `.silverloupe-report` sibling: keep both together.
- Naming hand-picked images “Top” before canonical curation completes: keep them as observations, not a Silverloupe selection.
- Inventing a wedding checklist: use only the user's coverage labels.
- Converting duplicate losers to reject: keep them as reserve recommendations.
- Hiding unsupported files: keep them visible as review or unassessed.
- Writing editor metadata from the ordinary workflow: version 0.1.3 is report-only.
