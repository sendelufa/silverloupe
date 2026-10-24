# Photoshop / Bridge / Camera Raw delivery adapter

Photoshop is not treated as a catalog with a universal pick/reject API. The intended selection handoff is through a separately verified Bridge/Camera Raw route, using only metadata fields proven for that exact version and format.

## Current support boundary

- `photoshop-camera-raw-xmp` remains report-only because Photoshop and Bridge are absent from the recorded probe environment and no real application readback exists.
- The generic `xmp-sidecar` backend is contract-tested for proprietary RAW sidecar safety, but that status is not Photoshop/Bridge compatibility evidence.
- JPEG, TIFF, DNG, and PSD are report/copy-only. Silverloupe does not inject metadata into original image bytes.
- No command launches Photoshop or claims selected files were opened successfully until the Bridge/Camera Raw handoff is verified end to end.

## Planned verified handoff

1. Create copies of representative RAW, JPEG, TIFF, DNG, and PSD fixtures and record their hashes.
2. Build and inspect an explicit plan; never fall back from an unavailable native route to XMP.
3. Apply only the verified fields to the copied fixture.
4. Read the values in Bridge and Camera Raw, pass the selected files to Photoshop, and read them back independently.
5. Confirm Camera Raw/develop settings and unknown XMP fields are unchanged.
6. Record application versions, evidence, label names, flag behavior, and limitations before marking a compatibility row verified.

Use `delivery-rollback` only with the receipt produced by the apply. If Bridge, Camera Raw, Photoshop, or the user changed the sidecar afterward, rollback reports a conflict and preserves the later edit.
