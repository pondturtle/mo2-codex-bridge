# MO2 Codex Bridge

Independent fork of [shadowpendant69/mo2-codex-bridge](https://github.com/shadowpendant69/mo2-codex-bridge). The current version is `0.1.3-local.8-candidate.7`. The upstream project and fork changes were developed with AI assistance. [Changelog](CHANGELOG.md) · [MIT license](LICENSE)

## Why this fork exists

This fork adds verified Nexus downloads through MO2, mod placement and renaming, and read-only backup and profile inventories. It also fixes bugs in launch tracking, mod operations, and recovery.

- **SKSE exit tracking.** Keeps writes blocked while Skyrim or a related tool may still be running. After a verified exit, waits only for processes linked to the launch. Steam can stay open.
- **Download recovery.** Reports when a canceled download leaves no archive, allows one explicitly approved retry, and rejects a retry if the original archive is still present.
- **Interrupted requests.** Keeps job records across restarts. If an install or write times out, you can look up the original job without running the operation again.
- **Configuration and load-order recovery.** Checks the target profile, backup integrity, and dependency order before restoring. Preserves configuration file encoding and saves a recovery copy before applying a restore.
- **Mod installs, updates, and removals.** Preserves the relative order of existing plugins in supported installs and keeps optional plugins disabled during supported updates. Checks that MO2 saved the expected lists after a removal.
- **Bridge upgrades.** Detects running MO2 on localized Windows, upgrades the connector in place, and verifies rollback copies of the connector and MO2 component.

## Install

Use Windows, MO2 2.5.2 with Python plugin support, and an external Python. The client was tested on Python 3.13 and 3.15.

### First installation

Put the verified release in a staging folder. Close MO2 and use your own MO2 path in the commands below.

```powershell
python setup.py --mo2 'C:\Tools\MO2' --dry-run
python setup.py --mo2 'C:\Tools\MO2' --register-codex
```

The dry run shows the permanent server path under `%USERPROFILE%\.mo2-codex-bridge\connector\mo2_bridge.pyz`. Setup installs the connector there and registers that path. The folder serves one MO2 installation per Windows user. For manual registration, use the generated `.mcp.json` beside the installed connector.

Reopen MO2 and connect the client. Compare the connector version from MCP discovery, when available, with the MO2 component version from `status`. Take a fresh `snapshot` before making changes.

### Upgrade the same installation

1. Stage and verify the new release. Finish pending bridge jobs, then close MO2 and the connector clients.
2. Run `python setup.py --mo2 'C:\Tools\MO2'` from the new release. Setup updates the permanent connector and the MO2 component. It saves the prior artifacts separately and reports their recovery paths. Existing registration keeps the same launch path.
3. Reopen MO2 and reconnect. Check the versions and one relevant read action. If an older Work chat is in use, verify there too before relying on new tools.
4. If the update needs to be reversed, close MO2 and the connector clients and restore the verified previous connector and MO2 component together.

`--component-only` updates only the MO2 plugin for a separately managed connector. Existing registrations that point to a versioned release are preserved. Migrate versioned registrations explicitly. Keep any older launcher that resumed chats still call and verify that it forwards to the permanent path before relying on it.

An existing chat can retain an older tool list after its connector is updated. Call `mo2_inspect` with action `api_capabilities` to discover the current actions. Reuse that list until the bridge version changes or a needed action is unavailable. Verify the connection with a read-only action.

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

Shared mod folders can affect every profile. Use fresh profile and revision checks for writes. Ambiguous launches keep writes blocked. SKSE exit recovery requires verified loader and game exits and holds for survivors linked by observed process ancestry. It uses Windows process identity APIs on Windows 11 build 26100.4770 or later. A helper whose entire ancestry is missed between snapshots cannot be identified. On uncertain results, check the original job ID and recovery record.

After verified SKSE and Skyrim exits, MO2 2.5.2 can leave its unhooked Steam client and verified Steam WebHelpers running. The guard requires the registered installation path, exact process generations, and the expected launch order. Every other survivor retains its ordinary guard checks.

The bridge uses authenticated localhost requests. Keep session configuration, audit logs and recovery files private. See [agent guidance](AGENTS.md) for operating details.

Run `python -B -m unittest discover -s tests -v` in a fixture workspace. All 730 tests passed on both Windows Python 3.13 and 3.15. Live MO2 2.5.2 checks verified the connector upgrade, a read-only Nexus lookup in a resumed Work chat, and write refusal during Skyrim play followed by clearance after exit with Steam still open. The free-account download flow awaits live verification.

To remove the bridge, close MO2, preserve its recovery files, remove the bridge component under `plugins`, and remove the connector registration.
