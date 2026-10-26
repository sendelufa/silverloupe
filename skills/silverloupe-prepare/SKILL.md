---
name: silverloupe-prepare
description: >
  Use when a user asks to inventory a photo folder, make safe previews, resume
  an interrupted scan, identify RAW/JPEG companions, or protect HDR, panorama,
  and focus-stack sources before selection. Do NOT use for aesthetic ranking,
  retouching, deleting originals, or writing editor metadata.
---

# Silverloupe Prepare

## Overview

Create the source inventory, normalized previews, contact sheets, groups, and selection items that later stages consume. Preparation observes and organizes; it never removes source material.

The runtime dependency is `silverloupe==0.1.2`.

## Preflight

1. Run `silverloupe doctor`.
2. Resolve one source directory and one distinct output directory in a durable
   user-facing location. Do not prepare a normal run under `/tmp`, `/private/tmp`,
   `mktemp`, or another system-cleaned location.
3. Confirm the source is readable and the output is writable.
4. Record unsupported RAW decoding as unavailable rather than installing an unrequested backend.
5. Stop if an owned output path is a symlink or escapes the output root.

Why: preview generation is safe only when every write is confined to the owned output tree.

The output tree is the resumable product of preparation. Continue grouping,
assessment, curation, and report generation in this same directory; do not copy only
the previews or contact sheets into a new report folder.

## Contract map

If you need the asset, group, selection-item, path, or session fields, read [runtime contracts](../../resources/contracts.md) completely before interpreting artifacts.

Do not load genre references during preparation. File relationships do not depend on wedding, architecture, or landscape taste.

## Prepare inventory and previews

Execute:

```text
silverloupe prepare SOURCE OUTPUT
```

Use `--max-edge NUMBER` only when the user or runtime budget calls for a different preview size.

The command creates:

- `session.json` with source, brief snapshot, runtime mode, and revision;
- `assets.jsonl` with hashes, paths, metadata, decode state, and preview provenance;
- `previews/` with normalized JPEG overviews where decoding succeeds;
- `contact-sheets/` for bounded overview inspection;
- `state.sqlite3` for resumable checkpoints.

## Inspect status and resume

Execute:

```text
silverloupe status OUTPUT
```

If preparation stopped after some assets completed, execute:

```text
silverloupe resume OUTPUT
```

Resume from persisted hashes and cache keys. Re-render an asset when its content, rendering policy, or backend identity changed.

Why: a stale preview can support a false visual claim, so cache reuse must follow complete input identity.

## Group prepared assets

Execute:

```text
silverloupe group OUTPUT
```

Interpret relationships as follows:

| Relationship | Preparation rule |
|---|---|
| byte duplicate | keep one inventory identity per source and record the relation |
| RAW/JPEG companion | create one capture item and keep the companion linked |
| burst / near duplicate | create competing capture items for later comparison |
| HDR / panorama / focus stack | protect required sources as one composite item |
| uncertain | keep separate or route to review; do not force a merge |

If a human edited `groups.json`, use `silverloupe group OUTPUT --reuse-groups` to validate those groups. Use `--reset-manual-groups` only after the user explicitly asks to discard manual grouping.

## Evidence rules

Metadata narrows candidates; it does not prove visual similarity. Thumbnail similarity proposes relationships; it does not authorize deletion.

Multiple-camera clock offsets may be unknown. Preserve original EXIF and leave uncertain timing relations reviewable.

## Completion checklist

- [ ] every supported source appears in `assets.jsonl`;
- [ ] source hashes still match their pre-run values;
- [ ] every ready preview stays inside `OUTPUT/previews`;
- [ ] unsupported assets remain visible and unassessed;
- [ ] protected composite items include all required assets;
- [ ] selection items do not overlap on the same asset;
- [ ] grouping report names uncertain groups.

## Stop conditions

Stop when the source changes during rendering, the output layout is unsafe, an artifact is malformed, or a protected set cannot be resolved without user review.

Do not call preparation a completed selection. Do not claim sharpness, expression, aesthetic strength, or RAW recoverability from inventory data.

## Common mistakes

- Scanning the output as input: keep nested output excluded from inventory.
- Using a temporary output and saving only selected artifacts: keep the canonical state in its final durable location.
- Treating a JPEG companion as a competing photograph: keep it linked to the capture.
- Flattening overlapping relationship candidates: preserve uncertainty until composition is resolved.
- Deleting duplicate bytes: record them; source deletion is outside the workflow.
- Hiding decode errors: publish `unsupported` or `unassessed` with the reason.
