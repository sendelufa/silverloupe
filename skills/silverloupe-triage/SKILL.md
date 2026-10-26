---
name: silverloupe-triage
description: >
  Use when a user asks to assess focus, motion blur, exposure, occlusion,
  decode failures, or other technical risks in prepared photos, especially
  when uncertain frames must remain reviewable. Do NOT use for final genre
  ranking, destructive rejection, retouching, or editor metadata writes.
---

# Silverloupe Triage

## Overview

Measure technical signals and request visual evidence without turning a proxy into a verdict. Triage produces recommendation-only decisions and keeps unique or uncertain photographs reviewable.

The runtime dependency is `silverloupe==0.1.2`.

## Preflight

1. Confirm `assets.jsonl`, `groups.json`, and `selection-items.json` exist in the output.
2. Run `silverloupe doctor` and record whether host vision is ready, degraded, or unavailable.
3. Confirm the brief identifies where eyes, motion, or geometry are contextually relevant.
4. Stop if prepared artifacts are malformed or incomplete.

Why: a technical signal needs a known asset, selection item, and context before it can influence a recommendation.

## Contract map

If you need assessment fields, decision authority, vision exchange rules, or failure semantics, read [runtime contracts](../../resources/contracts.md) completely before proceeding.

If numeric and visual evidence conflict, or a defect may be an intentional technique, read [artistic intent safeguards](../../resources/artistic-intent.md) completely before assigning review reasons. Otherwise do not load that reference.

## Run local assessment

Execute:

```text
silverloupe assess OUTPUT
```

Interpret signals as observations:

| Signal | Allowed conclusion |
|---|---|
| decode error | unassessed or review |
| low edge energy | possible softness; visual check required |
| high edge energy | possible detail, noise, or sharpening |
| dark or bright overview | preview exposure concern, not RAW recoverability |
| closed-eye candidate | relevant only when open eyes matter to the moment |
| possible occlusion | inspect the significant subject region |

The command must report `auto_reject_enabled: false`.

## Host vision request

If assessment is partial, execute:

```text
silverloupe vision-request OUTPUT
```

Inspect only artifacts listed in `vision-request.json`. For each asset:

1. Use exactly the requested criteria.
2. Name every observed artifact ID.
3. Bind region findings to normalized coordinates inside an observed artifact.
4. Use `insufficient_evidence` when the artifact scale cannot support the claim.
5. Keep intent as a hypothesis unless the visible result establishes it.

## Import observations

Write a strict response with the active request ID and execute:

```text
silverloupe vision-import OUTPUT RESPONSE_JSON
```

A completed response covers every requested asset and criterion. A partial response leaves missing assets insufficiently observed. A transient error follows the bounded retry contract.

Why: exact coverage makes absent evidence distinguishable from a favorable judgment.

## Decision rules

- Keep an assessed, usable item as a proposal.
- Route visual concerns to `review` with reason codes and observed artifacts.
- Keep unsupported assets `unassessed` rather than rejected.
- Keep a unique important moment in review even when technically imperfect.
- Preserve prior user-authority decisions when assessment reruns.
- Preserve protected composite sources even when one member looks weak alone.

## Completion checklist

- [ ] assessment generation marker is complete;
- [ ] every assessment references a known asset;
- [ ] evaluated claims cite observed artifacts;
- [ ] region evidence cites an observed artifact;
- [ ] no unassessed asset claims proven visual criteria;
- [ ] automatic reject count remains zero;
- [ ] unavailable real-photo validation is disclosed.

## Stop conditions

Stop with partial status when vision is unavailable, retries are exhausted, required crops are missing, or evidence remains insufficient.

Do not state that a frame is in focus from metadata or an overview that cannot show the relevant detail. Do not convert a possible artistic technique into automatic success.

## Common mistakes

- Treating Laplacian variance as truth: it is a queueing signal.
- Applying eye rules to every face: the brief and moment determine relevance.
- Calling preview clipping irrecoverable RAW loss: preview rendering cannot prove that.
- Dropping unsupported files: keep them visible for review.
- Overwriting user decisions on rerun: user authority has priority.
