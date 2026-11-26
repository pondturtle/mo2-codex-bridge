# MO2 Codex Bridge

Independent fork of [shadowpendant69/mo2-codex-bridge](https://github.com/shadowpendant69/mo2-codex-bridge). This branch carries `0.1.3-local.8-candidate.5`. The upstream project and fork changes were developed with AI assistance. [Changelog](CHANGELOG.md) · [MIT license](LICENSE)

## Why this fork exists

This fork adds verified Nexus downloads through MO2, mod placement and renaming, and read-only backup and profile inventories. It also fixes bugs in launch tracking, mod operations, and recovery.

- **SKSE exit tracking.** Keeps writes blocked while Skyrim or a related tool may still be running. Clears the guard after a verified exit even if Steam stays open.
- **Download recovery.** Reports when a canceled download leaves no archive, allows one explicitly approved retry, and rejects a retry if the original archive is still present.
- **Interrupted requests.** Keeps job records across restarts. If an install or write times out, you can look up the original job without running the operation again.
- **Configuration and load-order recovery.** Checks the target profile, backup integrity, and dependency order before restoring. Preserves configuration file encoding and saves a recovery copy before applying a restore.
- **Mod installs, updates, and removals.** Preserves the relative order of existing plugins in supported installs and keeps optional plugins disabled during supported updates. Checks that MO2 saved the expected lists after a removal.
- **Bridge upgrades.** Detects running MO2 on localized Windows and keeps a verified rollback copy of the bridge component when upgrading.

## Install

Use Windows, MO2 2.5.2 with Python plugin support, and an external Python. The client was tested on Python 3.13 and 3.15. Keep the checkout in a stable location. Close MO2 and run:

```powershell
python setup.py --mo2 'C:\Tools\MO2' --register-codex
```

Use your own MO2 install path. Setup generates local connector settings. Use `--component-only` to keep an existing registration and point that client at the matching source. Reopen MO2, start a fresh client connection, and check `status` and `snapshot`.

## What it does

- Inspect profiles, mod and plugin state, dependencies, loose-file winners, downloads and recovery records.
- List MO2-native mod backups and likely originals with `native_mod_backups`. Report a mod's saved cross-profile usage and current plugin relationships with `mod_profile_usage`.
- Find Nexus file versions, start or track one exact MO2-owned download, and verify the finished archive.
- Preview and place a mod relative to another mod, or preview a rename and verify it across affected profiles.
- Install already-downloaded archives, adjust priority and activation, and edit supported configuration with recovery records.

Native backup creation and restore, archive-content and plugin-record conflict analysis, and physical hiding of ESP/ESM/ESL files remain outside the supported operations.

## Preview, place, and rename

Use preview_mod_change with action place_mod, the exact mod and anchor names, and position before or after. The preview shows the left-pane change in the active profile. Moves that could switch a plugin provider are refused. Loose-file winners can change as priorities change.

For rename_mod, preview the exact mod and new_name. On MO2 2.5.2, renaming uses its native name editor and affects the shared mod folder and saved profiles. The bridge verifies the folder contents, plugin state, and saved lists. It flags keys in the resolved PGLite 2.4 mod rules when available. Review external tool settings that use the old name. The bridge does not edit them. Case-only renames, links, unclear profile state, and mod trees beyond the verification limits (512 MiB or 12,000 entries) are refused.

Apply the same arguments with the preview's plan_token, expected_profile, and expected_revision. Rename also requires acknowledge_shared_folder set to true. Previews expire after ten minutes or when the bridge restarts. On a partial outcome, inspect the existing job and recovery record before applying another change.

## Nexus downloads

1. Call `nexus_files` with a public Nexus mod or file URL. A mod URL lists public files with versions, categories, exact IDs, and links. An exact file URL can use MO2's signed-in account to retrieve its metadata. Check the same URL again if the lookup is pending.
2. After the user selects one file, call `nexus_download` with its file URL or a mod URL plus `file_id`, a fresh profile and revision, and `mode` set to `premium` or `manual`. Premium mode uses MO2's verified Premium account for eligible files. Manual mode returns the exact Nexus page so the user can choose **Mod Manager Download**. Start manual tracking before clicking. If file eligibility is pending, repeat the same request once its metadata is ready.
3. Keep the `download_id` and call `nexus_download_status` to check the original request. Resume an interrupted transfer through MO2. The bridge keeps the reservation across chat or MO2 restarts and submits the exact file at most once. It returns the local archive, byte count, and SHA-256 only after checking new matching download evidence. Review the archive separately before installing it.

Use the matching bridge component and client to expose these actions. Public listings use the official Nexus catalog. Account credentials and the actual transfer stay with MO2 and the browser. When the public catalog is unavailable, select the file on the Nexus site and use manual handoff. [Nexus GraphQL catalog](https://graphql.nexusmods.com/#query-modFiles)

If MO2's queue is confirmed idle and the archive is absent, `nexus_download_status` reports `no_archive`. The status can apply after cancellation, failure, or removal. After explicit user approval, use `nexus_download_retry` with the previous `download_id` and fresh profile and revision. MO2 must be the sole open instance and its Downloads list must be clear of unfinished transfers. The bridge checks the exact file again and reserves one successor ID. Repeating that retry returns the same successor. Follow `next_download_id` when an older receipt reports `superseded`.

For Premium downloads, the public catalog and MO2 must agree on the exact file and category. MO2 checks the signed-in account and Nexus decides whether to grant the transfer. Some public catalog flags disagree with the Nexus download page. [MO2's native Nexus permission check](https://github.com/ModOrganizer2/modorganizer/blob/v2.5.2/src/nexusinterface.cpp#L846-L870)

## Safety and verification

Shared mod folders can affect every profile. Use fresh profile and revision checks for writes. Ambiguous launches keep writes blocked. Automatic SKSE exit recovery uses a Windows process identity API documented for Windows 11 build 26100.4770 and later. On uncertain results, check the original job ID and recovery record.

After verified SKSE and Skyrim exits, MO2 2.5.2 can leave its unhooked Steam client and verified Steam WebHelpers running. The guard requires the registered installation path, exact process generations, and the expected launch order. Every other survivor retains its ordinary guard checks.

The bridge uses authenticated localhost requests. Keep session configuration, audit logs and recovery files private. See [agent guidance](AGENTS.md) for operating details.

Run `python -B -m unittest discover -s tests -v` in a fixture workspace. All 709 tests passed on Windows with Python 3.13 and 3.15. Live MO2 2.5.2 checks covered Premium downloads, pause and resume across restarts, cancellation, retry and the corrected refusal when an archive already exists. The free-account download flow still awaits a live check.

To remove the bridge, close MO2, preserve its recovery files, remove the bridge component under `plugins`, and remove the connector registration.
