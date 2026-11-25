# MO2 Codex Bridge

Community fork of [shadowpendant69/mo2-codex-bridge](https://github.com/shadowpendant69/mo2-codex-bridge). This branch carries `0.1.3-local.4-candidate.1`. The upstream project and fork changes were developed with AI assistance. [MIT license](LICENSE).

## Why this fork exists

This fork fixes bugs in the bridge's launch tracking, mod operations, and recovery.

- **SKSE exit tracking.** Keeps writes blocked while Skyrim or a related process may still be running. Retains verified exit evidence so the guard can clear in the same MO2 session, even if a tracked helper exits late.
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
- Install already-downloaded archives, adjust priority and activation, and edit supported configuration with recovery records.

Native backup creation and restore, archive-content and plugin-record conflict analysis, and physical hiding of ESP/ESM/ESL files remain outside the supported operations.

## Safety and verification

Shared mod folders can affect every profile. Use fresh profile and revision checks for writes. Ambiguous launches keep writes blocked. Automatic SKSE exit recovery uses a Windows process identity API documented for Windows 11 build 26100.4770 and later. On uncertain results, check the original job ID and recovery record.

The bridge uses authenticated localhost requests. Keep session configuration, audit logs and recovery files private. See [agent guidance](AGENTS.md) for operating details.

Run `python -B -m unittest discover -s tests -v` in a fixture workspace. The `.4` baseline passed 501 tests on Python 3.13 and 3.15. Live checks on MO2 2.5.2 covered both inventory actions and two sequential SKSE launches, including a delayed guard release.

To remove the bridge, close MO2, preserve its recovery files, remove the bridge component under `plugins`, and remove the connector registration.
