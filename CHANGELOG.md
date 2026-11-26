# Changelog

Each entry compares a published version with the previous published version.

## 0.1.3-local.8-candidate.5

Changes since `0.1.3-local.4-candidate.1`.

### Added

- Select an exact Nexus file by version and category. Queue eligible Premium downloads through the account signed into MO2, or track a browser-initiated **Mod Manager Download**. Request IDs survive restarts, and verified downloads return the archive path and SHA-256.
- Retry a canceled or missing download with explicit approval. Repeating the retry returns the same successor request. Existing archives, partial files, and uncertain transfers block the retry.
- Preview relative mod placement and native MO2 renames. Placement checks plugin providers. Renaming verifies the shared mod folder and affected saved profiles.

### Fixed

- After a verified Skyrim exit, unrelated desktop processes and verified Steam helpers no longer keep the SKSE write guard active. Skyrim, related tools, and unresolved launches still block writes.
- Canceled or removed downloads now report `no_archive` once MO2's queue is idle and the exact download is absent.
- A download refusal before reservation now reports that no changes occurred. Failures after a reservation attempt retain the recovery warning.
