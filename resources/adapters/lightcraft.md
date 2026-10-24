# LightCraft native delivery adapter

Silverloupe transfers canonical culling decisions to LightCraft through a host-mediated MCP exchange. The Python runtime never assumes that agent tools are importable Python functions: the host supplies an explicit transport, and every mutation names concrete integer photo IDs.

## Verified support boundary

- Contract coverage: capability absence, explicit IDs, stale/colliding mappings, identical file names, existing user values, unknown flag/label values, partial failure, resume, repeated apply, revised selection, readback, and conflict-safe rollback.
- Codex host probe on 2026-10-24: the available headless LightCraft backend accepted one explicit-ID `command_batch` containing `photo.rate`, `photo.flag`, and `photo.label`; `catalog.query` returned the expected `5 / pick / red`; the full develop document was unchanged before apply, after apply, and after metadata restoration.
- The probe used a disposable 2×2 JPEG fixture, restored `0 / none / null`, and moved its library record to Recently Deleted. It did not touch a user photograph.
- The LightCraft backend did not expose an application version in the available command discovery, so the evidence is scoped to this Codex host/backend rather than a named desktop release.
- Claude Code has not been probed for this adapter. Treat that host as `contract-tested only` until the same explicit-ID round trip succeeds there.

## Transport contract

`plan_lightcraft(manifest, items, decisions, transport, previous_receipt=None)` accepts an object with four synchronous methods:

| Method | Host responsibility |
|---|---|
| `discover()` | Return `commands` containing `catalog.query`, `photo.rate`, `photo.flag`, and `photo.label`; optionally return a `label_mapping` such as `{"top": "red"}`. |
| `resolve(assets)` | Return one explicit `{asset_id, photo_id, content_sha256}` mapping per matched asset. Mapping by file name alone is insufficient. |
| `read(ids)` | Return LightCraft photo records for exactly those IDs, including `id`, `rating`, `flag`, and `label`. |
| `batch(operations)` | Execute normalized operations and return one result per `operation_id` with `applied_ids` and `failed_ids`. The host normally translates this to LightCraft `command_batch`. |

The exchange object is deliberately small. A host may call MCP tools and normalize their results, while tests use an in-memory fake implementing the same four methods.

## Planning rules

1. Discover all required commands first. Missing MCP or any required command produces an `unavailable/report_only` plan; it never falls back to XMP.
2. Resolve every manifest asset to an explicit photo ID and bind the mapping to the asset content SHA-256. A missing ID, changed hash, missing photo, or two assets mapped to one ID is a conflict.
3. Read current `rating`, `flag`, and `label` without changing selection.
4. Map `top → 5`, `select → 4`, and `reserve → 3`. A reject flag is emitted only for `user` or `validated_policy` authority; a proposed reject is not applied.
5. Emit a color label only when `discover()` supplies an explicit mapping. Silverloupe does not assume universal color meaning.
6. Preserve nonempty user metadata unless a previous receipt proves Silverloupe ownership and the current value still equals the recorded `last` value.

Planning does not invoke `batch()` and therefore does not mutate LightCraft.

## Apply, resume, and readback

Call `apply_delivery(plan, transport)` for a ready native plan. The adapter:

- re-reads the planned IDs and rejects a changed preimage;
- sends only `photo.rate`, `photo.flag`, and `photo.label`, each with a nonempty explicit `ids` array;
- never selects photos and never calls a develop command;
- reads every target back and records `DeliveryOutcome`, baseline, postimage, operation IDs, and field ownership;
- reports a partial receipt when any operation or readback fails.

Reapplying the same plan is idempotent: fields already equal to the desired value are skipped, while missing operations are retried. For a revised selection, pass the prior receipt to `plan_lightcraft`; only fields whose current value still matches the prior receipt may be changed or restored.

## Rollback

Call `rollback_delivery(receipt, transport)`. Rollback restores only fields whose current value still matches the receipt postimage. Any later LightCraft or user edit produces `postimage_changed` and is preserved.

## Stop conditions

Stop and show conflicts when capability discovery fails, a mapping lacks a verified hash or explicit ID, IDs collide, a photo disappears, a preimage changes, a batch result is incomplete, readback differs, a flag/label value is unknown, or a rollback postimage changed.

Do not infer mapping from the current selection or from duplicate file names. Do not issue `set_develop`, crop, preset, export, delete, or selection commands as part of metadata delivery.
