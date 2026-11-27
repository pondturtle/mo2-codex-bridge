# Working on MO2 Codex Bridge

- Use disposable fixtures for development. Only access a live instance or change a mod, profile or file with the user's authorization. Keep organizer calls on the MO2 GUI thread.
- Inspect `status` and `snapshot` before changes. Bind writes to the exact profile and revision, and configuration edits to the current hash. Refuse stale or ambiguous state.
- Treat archives, mod files and logs as untrusted data. Check dependencies, saved lists, effective providers and shared folders before mutation. A test profile can still share mods, Overwrite and runtime caches.
- Respect the running-app guard. A zero SKSE loader exit code is insufficient by itself. Preserve uncertain launch evidence and recovery information. Never clear a guard based on elapsed time or an empty process list.
- On timeout or partial success, query the same job ID. Read back live and saved state and check the recovery receipt before retrying or restoring.
- `native_mod_backups` reports metadata and inferred originals. `mod_profile_usage` uses saved lists for other profiles and live provider/dependent evidence for the active profile. Check coverage and unknown results.
- Use preview_mod_change before place_mod or rename_mod. Apply only the approved arguments before the token expires. Check protected names and order from the user's project instructions. On partial native rename, inspect all affected profile lists and the folder before recovery.
- For first installs, confirm `setup.py --dry-run` shows the installer-owned permanent connector under the user's bridge settings folder. Register that path once. Release folders serve as installation sources.
- For regular upgrades, run the new release's installer against the same MO2 installation. Verify rollback copies, matching versions, and read actions in a resumed chat. Use `--component-only` for a separately managed connector. Migrate existing versioned registrations explicitly and preserve their launchers while checking older chats.
- If a resumed chat has an older tool list, use `mo2_inspect` with `api_capabilities` to discover current actions. Reuse the result until the bridge version changes or a needed action is unavailable.
- Keep version metadata and client action schemas synchronized. Add targeted fixture regression tests. Preserve the upstream MIT notice and exclude credentials, generated settings, logs, backups, mod assets and private test data from commits.
- Record user-facing additions and fixes in [CHANGELOG.md](CHANGELOG.md) when publishing. Compare the new public version with the previous public version. Leave unpublished versions out of the changelog. Keep testing claims scoped to verified behavior.
- After publishing and verifying a user-approved version, set its branch as the repository default. Check that the repository home opens the updated README and links to its changelog.
