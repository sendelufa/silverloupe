---
name: silverloupe-compare
description: >
  Use when a user asks to compare bursts, near-duplicates, expressions,
  compositions, or lighting variants and retain a winner plus acceptable
  alternatives. Do NOT use for whole-shoot coverage, technical screening
  alone, retouching, or automatic reject decisions.
---

# Silverloupe Compare

## Overview

Compare only genuine competitors using fixed dimensions and explicit evidence. Allow ties and useful alternatives when trade-offs prevent a single defensible winner.

The runtime dependency is `silverloupe==0.1.2`.

## Preflight

1. Confirm preparation produced `groups.json` and `selection-items.json`.
2. Confirm assessment evidence is complete enough for the requested comparison dimensions.
3. Separate protected composite members from competing burst or near-duplicate items.
4. Stop when a group is uncertain or a relevant detail is not visible at the available scale.

Why: comparing unrelated items or protected source members creates false losers.

## Contract map

If you need group types, item identity, decision provenance, or selection invariants, read [runtime contracts](../../resources/contracts.md) completely before comparing.

Do not load genre guides here. Genre coverage and overall story strength belong to curation; comparison stays local to plausible alternatives.

## Prepare evidence

Run missing stages with exact commands:

```text
silverloupe group OUTPUT
silverloupe assess OUTPUT
silverloupe vision-request OUTPUT
silverloupe vision-import OUTPUT RESPONSE_JSON
```

Use the vision commands only when assessment is partial. Do not fabricate a completed comparison when the request remains pending.

## Comparison dimensions

Apply the same dimensions to every candidate in one group:

| Dimension | Question |
|---|---|
| moment / expression | Which frame best carries the intended instant? |
| main subject | Is the significant subject readable and appropriately emphasized? |
| composition | Which arrangement best serves the stated purpose? |
| light | Which light supports form, mood, and significant detail? |
| artistic intent | Does the visible result support an intentional technique? |
| technical usability | Is the relevant subject usable at the inspected scale? |
| repair cost | Is a correctable issue material to the intended delivery? |

Use categorical evidence rather than invented precision. Keep `uncertain` when evidence cannot separate candidates.

## Select winner and alternatives

For each competitive group:

1. Name the strongest item as `winner_id` only when evidence supports it.
2. List acceptable runner-ups in `alternative_ids`.
3. Record reason codes and a human-readable trade-off.
4. Preserve multiple useful variants when they serve different moments or purposes.
5. Keep ordinary losers as reserve recommendations, not technical rejects.

Why: alternatives preserve user choice and prevent an uncalibrated score from becoming false certainty.

## Produce comparison provenance

The runtime generates comparison provenance during curation. Execute:

```text
silverloupe curate OUTPUT
```

Inspect `decisions.jsonl` for `winner_id`, `alternative_ids`, score components, profile versions, and `score_calibrated: false`.

There is no standalone state-mutating compare command in version 0.1.2. Use prepared evidence and the curation artifact rather than inventing an ad-hoc file format.

## Completion checklist

- [ ] every winner and alternative is a known selection item;
- [ ] each comparison stays within a genuine competitive relationship;
- [ ] protected composite members are not treated as disposable alternatives;
- [ ] dimensions are consistent across candidates;
- [ ] ties remain ties when evidence does not decide;
- [ ] provenance names the observed evidence and uncalibrated score;
- [ ] duplicate losers remain reserve, not reject.

## Stop conditions

Stop and request review when relevant focus, expression, or subject detail is unavailable; when an uncertain group may merge different captures; or when the brief does not define the trade-off.

Do not rank protected HDR, panorama, or focus-stack members against one another as finished photographs. Do not infer a person's identity or relationship from appearance.

## Common mistakes

- Choosing the numerically sharpest frame regardless of moment.
- Forcing one winner when two variants serve different uses.
- Comparing capture items across unrelated events because timestamps are close.
- Hiding the runner-up that exposes an important trade-off.
- Reporting an uncalibrated comparative score as probability.
