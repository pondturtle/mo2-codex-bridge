# Changelog

Each entry compares a published version with the previous published version.

## 0.1.3-local.9-candidate.3

Changes since `0.1.3-local.8-candidate.7`.

### Added

- Preview an optional plugin's provider, masters, and active dependents.
- Find an existing archive by exact Nexus file ID and verify it using MO2's local metadata and SHA-256.
- Preview how a staged mod replacement would affect plugin providers, masters, and activation.

### Changed

- Automatic Premium Nexus download starts and retries share a five-second minimum interval per MO2 instance. The bridge confirms the previous transfer started before timing the gap. Transfers can overlap. A valid `Retry-After` from the public Nexus catalog delays further submissions.

### Fixed

- The write guard clears after verified exits of MO2-launched command wrappers and direct tools. Unrelated processes that were already open no longer hold the guard. Related processes and unresolved launches still block writes.

## 0.1.3-local.8-candidate.7

Changes since `0.1.3-local.8-candidate.5`.

### Changed

- Setup installs the connector at a permanent per-user path. Upgrades preserve the existing registration and verify separate rollback copies for the connector and MO2 plugin.

### Fixed

- After verifying SKSE and Skyrim exits, the write guard checks surviving processes linked to the launch by Windows process ancestry. Unrelated processes no longer hold the guard open. Active helpers and unresolved launches still block writes.

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
