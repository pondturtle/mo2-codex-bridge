# Changelog

Changes in the independent fork. Versions are listed newest first.

## 0.1.3-local.8-candidate.5

- A Nexus retry refused before reservation now reports that no changes occurred. Failures after a reservation attempt retain the recovery warning.
- Verified live on MO2 2.5.2 with an existing archive. There was no duplicate download and the original archive checksum matched.

## 0.1.3-local.8-candidate.4

- Reports `no_archive` after cancellation or removal when MO2's queue is idle and the exact archive is absent.
- An explicitly approved retry creates one successor request. Repeating it returns the same successor. Existing archives, partial files and uncertain transfers block retries.

## 0.1.3-local.8-candidate.3

- Lists Nexus files by version and category. Downloads one selected file through MO2's signed-in Premium account, or tracks a browser-initiated **Mod Manager Download**.
- Retains request IDs across restarts. Verifies the Nexus game, mod and file IDs before returning the completed archive path and SHA-256.
- Uses MO2's native authorization for Premium eligibility and recognizes MO2 2.5.2's Premium account label.
- Premium downloads and pause, restart and resume passed live checks. The free-account flow awaits a live check.

## 0.1.3-local.6 and 0.1.3-local.7

- Scoped SKSE launch tracking to related process identities. Unrelated desktop processes no longer hold up writes after a verified Skyrim exit.
- Clears the guard after cold and warm SKSE launches when only the verified Steam client and its helpers remain.

## 0.1.3-local.5

- Added previews for relative mod placement and native renaming. Placement checks plugin providers. Renaming verifies the shared folder and affected saved profiles.

## 0.1.3-local.4

- Retained verified SKSE exit evidence and durable recovery records for interrupted operations.
- Improved configuration and load-order restoration, plugin ordering during installs and updates, and saved-state verification after removal.
- Added read-only native backup inventory and cross-profile mod usage.
- Made bridge upgrades recognize localized Windows process lists and verify the rollback copy.
