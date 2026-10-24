# Lightroom Classic delivery adapter

This adapter describes the explicit Adobe XMP exchange route. It does not imply a native Lightroom API and never edits `.lrcat`.

## Current support boundary

- `xmp-sidecar` is a contract-tested low-level backend for adjacent sidecars of proprietary RAW files. It proves Silverloupe merge, preimage, journal, readback, ownership, and rollback behavior; it does not by itself prove that a particular Lightroom version consumed the result.
- `lightroom-classic-xmp` remains report-only until the exact Lightroom/OS/format row has a verified editor round trip.
- `lightroom-classic-native` remains report-only while no checked native write capability exists. Missing native capability never selects XMP automatically.
- JPEG, TIFF, DNG, and PSD remain report/copy-only until separate embedded-metadata or safe-copy probes pass. Lightroom cloud is a separate, unsupported target.

## Safe workflow

1. Work on copies in a test catalog before changing a compatibility row.
2. Save any Lightroom catalog changes to metadata before planning, so the sidecar preimage is current.
3. Build `delivery-plan.json` with the explicitly chosen backend and inspect every conflict.
4. Apply the plan. Each sidecar is staged under the Silverloupe output, its prior bytes are backed up, the source and sidecar hashes are rechecked, and owned fields are read back after the atomic replacement.
5. In Lightroom Classic, use its documented metadata refresh/read command for the copied fixture. Reopening a folder or merely having Lightroom installed is not readback evidence.
6. Roll back with the receipt when required. Rollback stops if the editor or user changed the postimage after apply.

```text
silverloupe delivery-plan OUTPUT --target xmp-sidecar
silverloupe delivery-apply OUTPUT
silverloupe delivery-rollback OUTPUT
```

For a revised selection, pass the prior receipt when rebuilding the plan. Silverloupe updates a rating only when it still equals the last owned value. Keywords are owned element-by-element; unrelated user keywords remain.

## Reject authorization

A proposed reject becomes only `silverloupe:suggested-reject`. A reject value requires user/validated-policy authority or `--accept-reject-hash` matching the exact displayed proposed-reject batch. Ordinary delivery does not accept proposed rejects implicitly.

## Compatibility probe

For every RAW/JPEG/TIFF/DNG/PSD row record editor version, macOS version and architecture, fixture SHA-256, actual write route, editor read/readback, preserved develop/ACR settings, flags, keywords, rating, and custom color-label behavior. Unknown means `not-tested`; installation detection is not verification.
