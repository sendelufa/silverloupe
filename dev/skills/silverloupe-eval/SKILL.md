---
name: silverloupe-eval
description: >
  Use when a developer asks to benchmark Silverloupe routing, compare B2 and
  B3 behavior, calculate trigger precision or recall, inspect disclosure
  traces, or validate a skill collection release. Do NOT use for ordinary
  photo selection, culling a user's shoot, report delivery, or editor writes.
---

# Silverloupe Eval

## Overview

Evaluate skill routing and observable runtime behavior from clean host sessions. Keep this skill in the development toolkit; never ship it in the photographer runtime collection.

The runtime under evaluation is `silverloupe==0.1.0`.

## Preflight

1. Record the host CLI version and model identity when the host exposes it.
2. Create a clean state for every trial.
3. Confirm the candidate collection is the exact artifact under evaluation.
4. Confirm routing gold labels remain outside the candidate's readable workspace.
5. Stop when host authentication or the required executable is unavailable.

Why: routing metrics are meaningful only when candidate, host, and hidden expectations are separated.

## Development inputs

If you are running routing checks, read [routing cases](../../../evals/cases/routing.jsonl) as the versioned case set.

If you are comparing behavior without the collection, read [B2 baseline](../../../evals/baselines/issue-6-b2.jsonl) for observable actions and categorized failures.

Do not use these development files as production instructions or disclose gold labels to the executor.

## Variants

| Variant | Meaning |
|---|---|
| B2 | same host and CLI, user request without the collection |
| B3 | same host and CLI, user request with the complete runtime collection |

Keep model, host, tools, prompt, network mode, and input fixture fixed between paired variants.

## Routing trials

Use the 60 versioned prompts:

- 20 positive requests that should route to a production stage;
- 20 negative requests that should avoid the collection;
- 20 boundary requests covering evals, delivery, retouching, unsupported writes, missing source, and insufficient vision.

Run three trials per host for release routing metrics. A one-trial smoke can detect gross packaging failures but cannot establish stochastic trigger quality.

## Observable evidence

Capture only:

- prompt and case ID;
- host, CLI, model identity when available, and collection version;
- tool and file actions visible in the trace;
- final response;
- produced artifact paths and exit status;
- wall time, tokens, and cost when exposed;
- explicit `unavailable` for hidden traces or metrics.

Do not request, infer, or store private reasoning.

## Routing scores

Calculate trigger precision and recall overall, by category, by skill, by language, and by host.

Count a route as correct only when the selected skill matches `expected_skill`. A negative case is correct when no Silverloupe skill triggers. Keep boundary disclosures as separate invariant checks.

Report raw counts alongside percentages. Do not label a missing trace as proof that a reference was not read.

## Runtime invariants

For applicable workflow trials, verify:

- sources remain unchanged;
- partial vision runs do not publish `selection.json`;
- completed top remains a subset of selects;
- unsupported assets remain visible;
- ordinary duplicate losers do not become reject;
- runtime contains no development cases or baselines;
- report-only delivery makes no editor mutation.

## Compare B2 and B3

Pair cases by host, prompt, and clean-state fixture. Classify improvements and regressions from observable outputs rather than response style.

Treat broad filesystem search, missing partial artifacts, failure to route, hidden limitations, unnecessary genre references, and false completion as concrete failures.

## Completion checklist

- [ ] case-set schema version is recorded;
- [ ] clean state is recreated for every trial;
- [ ] B2 and B3 share the same controlled inputs;
- [ ] unavailable hosts are not labeled pass;
- [ ] trace visibility is explicit;
- [ ] private reasoning is absent;
- [ ] raw counts accompany precision and recall;
- [ ] runtime/dev separation is verified physically.

## Stop conditions

Stop when the host cannot authenticate, a clean state cannot be established, model identity changes mid-comparison, the candidate can read gold labels, or result artifacts are incomplete.

Do not use replay evidence to claim current model quality. Do not use routing success to claim photographic quality, preservation, or editor compatibility.

## Common mistakes

- Letting the B3 executor read expected routes.
- Comparing different model versions as a skill-only change.
- Calling a single smoke run a release metric.
- Treating self-reported reference reads as trace evidence.
- Shipping this development skill with the six production skills.
