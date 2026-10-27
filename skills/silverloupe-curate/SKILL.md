---
name: silverloupe-curate
description: >
  Use when a user asks for a genre-aware final edit, a diverse top K, broader
  selects, story or view coverage, repetition limits, or a coherent wedding,
  architecture, or landscape set. Do NOT use for inventory, retouching,
  routing evaluation, or direct editor writes.
---

# Silverloupe Curate

## Overview

Build the broad selects and ordered top from complete evidence, comparison alternatives, genre priorities, and the user's coverage constraints. Keep unmet constraints visible.

The runtime dependency is `silverloupe==0.1.3`.

## Preflight

1. Confirm `assessment-generation.json` is complete.
2. Confirm every selection item has current technical and visual evidence or remains explicitly unassessed.
3. Read the brief's genres, `top_k`, coverage checklist, repetition limits, and preselected items.
4. Stop when visual evidence is partial. When a requested genre has no bundled profile,
   continue with generic uncalibrated scoring and disclose that no genre-specific profile
   contributed weights.

Why: editorial ranking cannot honestly complete when its evidence generation is incomplete.

Do not curate outside the canonical artifacts. If the workflow is partial, required
generation markers are incomplete, or `silverloupe curate OUTPUT` does not publish
`selection.json`, do not hand-rank files into a substitute top, write a parallel
selection JSON, or build custom ranking HTML.

## Contract map

If you need selection fields, decision authority, override semantics, or stopping rules, read [runtime contracts](../../resources/contracts.md) completely before curation.

Load only the requested genre guide:

- If the brief includes wedding, read [wedding editorial guide](../../resources/genres/wedding.md) completely.
- If the brief includes architecture, read [architecture editorial guide](../../resources/genres/architecture.md) completely.
- If the brief includes landscape, read [landscape editorial guide](../../resources/genres/landscape.md) completely.

Do not load an unrequested genre guide. Why: irrelevant priorities can distort evidence and violate disclosure expectations.

If none of the requested genres has a bundled guide, load no genre guide. The runtime's
generic criteria remain available, but the result must not claim genre-specific calibration.

## Validate the brief

Use only explicit coverage labels. Keep absent people, events, locations, and views absent.

Interpret `top_k` as a target number of selection items, not asset files. A composite item occupies one top position while retaining every required source asset.

If coverage cannot fit K, preserve the best admissible set and report `coverage_exceeds_top_k` or the specific missing labels.

## Run curation

Execute:

```text
silverloupe curate OUTPUT
```

The runtime applies comparative scores, diversity penalties, coverage, repetition limits, protected sets, and prior user overrides.

## Selection tiers

| Tier | Meaning |
|---|---|
| top | strongest ordered recommendations for the requested use |
| select | useful broader edit and alternatives |
| reserve | valid but redundant or currently outside the edit |
| none | review, unassessed, or otherwise not tiered |

Require every top item to appear in selects. Keep ranking scores explicitly uncalibrated.

## Authority and rejection

Generated decisions use proposal authority. User corrections use user authority and survive reruns.

Ordinary duplicate losers remain `keep/reserve`. Unsupported or insufficiently observed items remain review or unassessed. Version 0.1.3 has no validated automatic reject policy.

Why: a false reject is more costly than a conservative reserve recommendation.

## Review editorial output

Inspect:

- `selection.json` for top, selects, ranked order, coverage, constraints, and protected sets;
- `decisions.jsonl` for reason codes, human reasons, alternatives, and provenance;
- `curation-report.json` for counts, profile versions, and unavailable photo metrics;
- `curation-generation.json` for a complete publication marker.

## Completion checklist

- [ ] `top_ids` is a subset of `selects_ids`;
- [ ] requested K counts items, not member assets;
- [ ] required composite assets stay protected;
- [ ] user-authority decisions remain unchanged;
- [ ] repetition limits are applied or disclosed as unmet;
- [ ] requested coverage is satisfied or explicitly missing;
- [ ] reasons retain winners and acceptable alternatives;
- [ ] unprofiled genres use the disclosed generic fallback rather than a fabricated profile;
- [ ] photo-eval metrics remain unavailable unless separately measured.

## Stop conditions

Stop when assessment publication is incomplete, IDs are unknown or duplicated, selection items overlap, genre profiles are invalid, or visual evidence cannot support the requested edit.

Do not claim the top is objectively best, statistically calibrated, accepted by a photographer, or safe to write into an editor.

## Common mistakes

- Loading all genre guides: load only the brief's requested genres.
- Filling K with unassessed items: disclose `insufficient_qualified_items`.
- Hiding coverage conflict: publish the missing labels.
- Counting RAW and JPEG companions as two selections: use the capture item.
- Replacing a user override with a newer proposal: user authority has priority.
- Publishing an improvised top after curation stops: report the canonical partial state instead.
