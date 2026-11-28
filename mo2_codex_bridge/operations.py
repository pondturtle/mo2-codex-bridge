"""All organizer access in this module runs on MO2's Qt GUI thread."""
import json
import os
import secrets
import shutil
import time
import uuid
from pathlib import Path

from .common import VERSION, atomic_write, contained_path, digest, safe_name, write_json
from .persistence import PluginPersistence
from .configuration_encoding import decode_configuration
from .managed_operations import (create_separator as create_managed_separator,
    install_archive as install_managed_archive, update_archive as update_managed_archive,
    remove_mod as remove_managed_mod, validate_archive)
from .config_recovery import write_configuration, restore_configuration
from .order_recovery import read_order_backup, build_restore_plan, revalidate_restore_plan, verify_restore_result, recovery_context
from .mod_persistence import ModListPersistence
from .native_backups import browse_native_mod_backups
from .profile_usage import report_mod_profile_usage
from .optional_plugin_preview import preview_optional_plugin
from .replacement_preview import preview_mod_replacement
from .mod_change_previews import ModChangePreviews
from .mod_placement import build_placement
from .mod_rename import build_rename

READ_ACTIONS = {'status', 'snapshot', 'downloads', 'file_conflict', 'mod_conflicts',
                'read_config', 'diagnostics', 'backups', 'logs', 'api_capabilities', 'profiles',
                'native_mod_backups', 'mod_profile_usage', 'preview_mod_change', 'preview_optional_plugin', 'preview_mod_replacement',
                'nexus_files', 'nexus_download_status', 'nexus_find_archive'}
WRITE_ACTIONS = {'create_separator', 'set_mod_priority', 'set_mod_active', 'set_plugin_active',
                 'set_plugin_order', 'install_archive', 'update_archive', 'write_config',
                 'restore_config', 'hide_file', 'unhide_file', 'restore_order', 'remove_empty_separator',
                 'remove_mod', 'select_profile', 'place_mod', 'rename_mod', 'nexus_download', 'nexus_download_retry'}


class Operations:
    def __init__(self, organizer, mobase, data_path, refresh, select_profile=None, wait_until=None):
        self.o, self.m = organizer, mobase
        self.data = Path(data_path)
        self.refresh = refresh
        self.profile_selector = select_profile
        self.wait_until = wait_until
        self.running = set()
        self.nexus = None
        self.mod_previews = ModChangePreviews(self, secrets.token_bytes(32), {
            'place_mod': lambda params, before: build_placement(self, params, before),
            'rename_mod': lambda params, before: build_rename(self, params, before),
        })

    def is_active(self, name):
        return bool(self.o.modList().state(name) & self.m.ModState.ACTIVE)

    def mod(self, name, writable=False):
        mod = self.o.modList().getMod(name)
        if mod is None:
            raise ValueError('Unknown mod: ' + str(name))
        if writable and (mod.isForeign() or mod.isOverwrite() or mod.isBackup()):
            raise ValueError('This operation requires a regular MO2-managed mod')
        if writable:
            root = Path(mod.absolutePath()).resolve()
            if not root.is_relative_to(Path(self.o.modsPath()).resolve()):
                raise ValueError('Mod folder is outside configured Mods directory')
        return mod

    def snapshot(self):
        mods = []
        ml = self.o.modList()
        for name in ml.allModsByProfilePriority():
            mod = ml.getMod(name)
            if mod is None:
                continue
            mods.append({'name': name, 'display_name': ml.displayName(name),
                         'priority': ml.priority(name), 'active': self.is_active(name),
                         'separator': mod.isSeparator(), 'foreign': mod.isForeign(),
                         'overwrite': mod.isOverwrite(), 'path': mod.absolutePath(),
                         'version': mod.version().displayString(), 'nexus_id': mod.nexusId(),
                         'latest_known_version': mod.newestVersion().displayString()})
        pl = self.o.pluginList()
        plugins = []
        for name in pl.pluginNames():
            plugins.append({'name': name, 'priority': pl.priority(name), 'load_order': pl.loadOrder(name),
                            'active': pl.state(name) == self.m.PluginState.ACTIVE,
                            'origin': pl.origin(name), 'masters': list(pl.masters(name)),
                            'light': pl.isLightFlagged(name) or pl.hasLightExtension(name),
                            'master': pl.isMasterFlagged(name) or pl.hasMasterExtension(name)})
        plugins.sort(key=lambda item: item['priority'])
        result = {'bridge_version': VERSION, 'mo2_version': self.o.appVersion().displayString(),
                  'profile': self.o.profileName(), 'profile_path': self.o.profilePath(),
                  'paths': {'mods': self.o.modsPath(), 'downloads': self.o.downloadsPath(),
                            'overwrite': self.o.overwritePath()}, 'mods': mods, 'plugins': plugins,
                  'running_applications': sorted(self.running),
                  'recovery_identity': {'bridge_data_path': str(self.data.resolve()),
                                        'game': self.o.managedGame().gameShortName()}}
        result['revision'] = digest(json.dumps(result, sort_keys=True).encode())
        return result

    def dispatch(self, action, p):
        backup = None
        backup_created = False
        operation_entered = False
        phase = 'preflight'
        result = None
        prepared = None
        mutating = isinstance(action, str) and action in WRITE_ACTIONS
        try:
            if action not in READ_ACTIONS | WRITE_ACTIONS:
                raise ValueError('Unknown action: ' + str(action))
            if not isinstance(p, dict):
                raise ValueError('Parameters must be a JSON object')
            if mutating:
                before = self.snapshot()
                if p.get('expected_profile') != before['profile'] or p.get('expected_revision') != before['revision']:
                    raise ValueError('Profile or state changed. Read snapshot again before making changes.')
                if self.running:
                    raise ValueError('Close the game or running MO2-launched tools before changing mods')
                if action in {'nexus_download', 'nexus_download_retry'}:
                    def before_claim():
                        nonlocal phase, operation_entered
                        phase = 'operation'
                        operation_entered = True

                    return getattr(self, action)(p, None, before_claim=before_claim)
                if action in self.mod_previews.builders:
                    prepared = self.mod_previews.prepare_apply(action, p, before)
                if prepared is None or not prepared.summary['no_op']:
                    phase = 'backup'
                    backup = self.data / 'backups' / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
                    backup.mkdir(parents=True)
                    backup_created = True
                    write_json(backup / 'before.json', before)
            phase = 'operation'
            operation_entered = True
            if prepared is not None:
                result = prepared.apply(backup)
            else:
                result = self.snapshot() if action == 'snapshot' else getattr(self, action)(p, backup)
            if backup is not None:
                phase = 'result_record'
                result['backup_id'] = backup.name
                write_json(backup / 'result.json', {'action': action, 'result': result})
            return result
        except Exception as error:
            # Preserve the original exception type for existing callers/tests.
            # Unknown partial outcomes must never be presented as safe retries.
            inherited = getattr(error, 'bridge_failure', {})
            failure = dict(inherited) if isinstance(inherited, dict) else {}
            recovery = getattr(error, 'recovery_details', None)
            if isinstance(recovery, dict):
                failure['recovery_details'] = recovery
                if type(recovery.get('changes_may_have_occurred')) is bool:
                    failure.setdefault('changes_may_have_occurred', recovery['changes_may_have_occurred'])
            failure.setdefault('message', str(error))
            failure.setdefault('error_type', type(error).__name__)
            failure.setdefault('action', action if isinstance(action, str) else None)
            failure.setdefault('phase', phase)
            failure.setdefault('changes_may_have_occurred', bool(mutating and operation_entered))
            if backup_created:
                failure['backup_id'] = backup.name
            if result is not None and phase == 'result_record':
                failure['completed_result'] = result
            if failure['changes_may_have_occurred']:
                try:
                    observed = self.snapshot()
                    failure['observed_state'] = {key: observed[key] for key in ('profile', 'profile_path', 'revision')}
                except Exception as observation_error:
                    failure['observation_error'] = str(observation_error)
            if backup_created:
                try:
                    write_json(backup / 'error.json', failure)
                    failure['error_recorded'] = True
                except Exception as record_error:
                    # A full disk or failed backup write must not mask the cause.
                    failure['error_recorded'] = False
                    failure['error_record_error'] = str(record_error)
            error.bridge_failure = failure
            raise

    def status(self, p, b):
        result = {'bridge_version': VERSION, 'profile': self.o.profileName(), 'running_applications': sorted(self.running)}
        if callable(getattr(self.running, 'describe', None)):
            result['running_application_tracking'] = self.running.describe()
        if callable(getattr(self, 'job_retention', None)):
            result['job_retention'] = self.job_retention()
        return result

    def api_capabilities(self, p, b):
        result = {'read_actions': sorted(READ_ACTIONS), 'write_actions': sorted(WRITE_ACTIONS),
                  'organizer_methods': [x for x in dir(self.o) if not x.startswith('_')]}
        if callable(getattr(self.running, 'describe', None)):
            result['running_application_tracking'] = self.running.describe()
        if callable(getattr(self, 'job_retention', None)):
            result['job_retention'] = self.job_retention()
        return result

    def preview_mod_change(self, p, b):
        return self.mod_previews.preview(p)

    def preview_optional_plugin(self, p, b):
        return preview_optional_plugin(self, p)

    def preview_mod_replacement(self, p, b):
        return preview_mod_replacement(self, p)

    def native_mod_backups(self, p, b):
        return browse_native_mod_backups(self.o, p)

    def mod_profile_usage(self, p, b):
        return report_mod_profile_usage(self, p)

    def profiles(self, p, b):
        root = Path(self.o.profilePath()).parent
        return {'profiles': sorted(x.name for x in root.iterdir() if x.is_dir() and (x / 'modlist.txt').is_file()),
                'current': self.o.profileName()}

    def select_profile(self, p, b):
        if self.profile_selector is None or p['profile'] not in self.profiles({}, None)['profiles']:
            raise ValueError('Unknown profile or profile selection unavailable')
        self.profile_selector(p['profile'])
        self.refresh()
        if self.o.profileName() != p['profile']:
            raise RuntimeError('MO2 did not switch profiles')
        return {'profile': self.o.profileName()}

    def snapshot_action(self, p, b):
        return self.snapshot()

    def downloads(self, p, b):
        files = []
        for path in sorted(Path(self.o.downloadsPath()).iterdir()):
            if path.is_file() and path.suffix.lower() in {'.zip', '.7z', '.rar', '.omod', '.fomod'}:
                files.append({'name': path.name, 'path': str(path), 'bytes': path.stat().st_size})
        return {'archives': files}

    def nexus_files(self, p, b):
        if self.nexus is None:
            raise ValueError('Nexus operations are unavailable in this MO2 session')
        return self.nexus.files(p)

    def nexus_find_archive(self, p, b):
        if self.nexus is None:
            raise ValueError('Nexus operations are unavailable in this MO2 session')
        return self.nexus.find_archive(p)

    def _nexus_download_change(self, operation, p, *, before_claim=None):
        if self.nexus is None:
            raise ValueError('Nexus operations are unavailable in this MO2 session')

        def revalidate():
            current = self.snapshot()
            if current['profile'] != p['expected_profile'] or current['revision'] != p['expected_revision']:
                raise ValueError('Profile or state changed while preparing the Nexus download')
            if self.running:
                raise ValueError('Close the game or running MO2-launched tools before requesting a download')

        return getattr(self.nexus, operation)(p, revalidate=revalidate, before_claim=before_claim)

    def nexus_download(self, p, b, *, before_claim=None):
        return self._nexus_download_change('start', p, before_claim=before_claim)

    def nexus_download_retry(self, p, b, *, before_claim=None):
        return self._nexus_download_change('retry', p, before_claim=before_claim)

    def nexus_download_status(self, p, b):
        if self.nexus is None:
            raise ValueError('Nexus operations are unavailable in this MO2 session')
        return self.nexus.status(p)

    def file_conflict(self, p, b):
        relative = p['path'].replace('\\', '/')
        contained_path(self.o.overwritePath(), relative)
        return {'path': relative, 'origins_in_priority_order': list(self.o.getFileOrigins(relative)),
                'winning_physical_path': self.o.resolvePath(relative),
                'scope': 'MO2 virtual file origins, including archives when indexed by MO2; not ESP record conflicts'}

    def mod_conflicts(self, p, b):
        mod = self.mod(p['mod'])
        root = Path(mod.absolutePath())
        maximum = p.get('max_files', 300)
        cursor = p.get('offset', 0)
        if type(maximum) is not int or not 1 <= maximum <= 2000:
            raise ValueError('max_files must be an integer from 1 through 2000')
        if type(cursor) is not int or cursor < 0:
            raise ValueError('offset must be a nonnegative integer')
        files = []
        visited = 0
        more = False
        for folder, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not (Path(folder) / d).is_symlink()
                             and not (hasattr(Path, 'is_junction') and (Path(folder) / d).is_junction()))
            for name in sorted(names):
                path = Path(folder) / name
                if name.lower() == 'meta.ini' or path.is_symlink() or name.endswith('.mohidden'):
                    continue
                if visited < cursor:
                    visited += 1
                    continue
                if visited - cursor >= maximum:
                    more = True
                    break
                relative = path.relative_to(root).as_posix()
                report = self.file_conflict({'path': relative}, None)
                if len(report['origins_in_priority_order']) > 1:
                    files.append(report)
                visited += 1
            if more:
                break
        return {'mod': p['mod'], 'conflicts': files, 'scanned': max(0, visited - cursor),
                'next_offset': visited if more else None,
                'scope': 'Files physically present in this mod, inspected through MO2 VFS; archive contents not enumerated'}

    def config_path(self, p):
        scope = p.get('scope', 'mod')
        if scope == 'mod':
            mod = self.mod(p['mod'], writable=True)
            if mod.isSeparator():
                raise ValueError('Separators do not have editable mod configuration')
            return contained_path(mod.absolutePath(), p['path'], text_only=True)
        if scope == 'profile':
            filename = p['path']
            if filename.lower() not in {'skyrim.ini', 'skyrimprefs.ini', 'skyrimcustom.ini'}:
                raise ValueError('Only profile-local Skyrim game INIs can be edited')
            if not self.o.profile().localSettingsEnabled():
                raise ValueError('Enable profile-specific game settings first; global game INIs are not edited')
            return contained_path(self.o.profilePath(), filename, text_only=True)
        if scope == 'overwrite':
            return contained_path(self.o.overwritePath(), p['path'], text_only=True)
        raise ValueError('Unknown configuration scope')

    def read_config(self, p, b):
        path = self.config_path(p)
        if not path.exists():
            return {'path': str(path), 'exists': False, 'sha256': None, 'text': ''}
        if path.stat().st_size > 1024 * 1024:
            raise ValueError('Configuration file exceeds the 1 MiB limit')
        content = path.read_bytes()
        return {'path': str(path), 'exists': True, 'sha256': digest(content), **decode_configuration(content)}

    def write_config(self, p, b):
        return write_configuration(self, p, b)

    def get_backup(self, name):
        name = safe_name(name)
        path = contained_path(self.data / 'backups', name)
        if not path.is_dir():
            raise ValueError('Unknown backup ID')
        return path

    def restore_config(self, p, b):
        return restore_configuration(self, p, b)

    def create_separator(self, p, b):
        return create_managed_separator(self, p, b)

    def move_mod(self, name, priority):
        self.mod(name, writable=True)
        maximum = len(self.o.modList().allModsByProfilePriority()) - 1  # backups/Overwrite are automatic
        if type(priority) is not int or not 0 <= priority <= maximum:
            raise ValueError('Priority must be an integer within the movable list')
        if not self.o.modList().setPriority(name, priority):
            if self.o.modList().priority(name) != priority:
                raise RuntimeError('MO2 rejected this mod priority')
        return self.o.modList().priority(name)

    def set_mod_priority(self, p, b):
        return {'name': p['mod'], 'priority': self.move_mod(p['mod'], p['priority'])}

    def set_mod_active(self, p, b):
        mod = self.mod(p['mod'], writable=True)
        if mod.isSeparator() or type(p['active']) is not bool:
            raise ValueError('Specify a regular mod and a boolean active value')
        self.o.modList().setActive(p['mod'], p['active'])
        self.refresh()
        if self.is_active(p['mod']) != p['active']:
            raise RuntimeError('MO2 did not apply the requested enabled state')
        return {'mod': p['mod'], 'active': self.is_active(p['mod'])}

    def plugin_state(self):
        pl = self.o.pluginList()
        return {n: pl.state(n) == self.m.PluginState.ACTIVE for n in pl.pluginNames()}

    def set_plugin_active(self, p, b):
        pl = self.o.pluginList()
        name = p['plugin']
        if name not in pl.pluginNames() or type(p['active']) is not bool:
            raise ValueError('Specify an existing plugin and a boolean active value')
        names = {n.casefold(): n for n in pl.pluginNames()}
        if p['active']:
            inactive = [master for master in pl.masters(name)
                        if master.casefold() not in names
                        or pl.state(names[master.casefold()]) != self.m.PluginState.ACTIVE]
            if inactive:
                raise ValueError('Enable required masters first: ' + ', '.join(inactive))
        else:
            dependents = [n for n in pl.pluginNames() if pl.state(n) == self.m.PluginState.ACTIVE
                          and name.casefold() in {m.casefold() for m in pl.masters(n)}]
            if dependents:
                raise ValueError('Disable dependent plugins first: ' + ', '.join(dependents))
        commit = PluginPersistence(self.o, self.m, self.wait_until)
        if not p['active'] and name.casefold() in commit.primary:
            raise ValueError('Primary game plugins cannot be disabled')
        order = sorted(pl.pluginNames(), key=pl.priority)
        states = self.plugin_state()
        states[name] = p['active']
        pl.setState(name, self.m.PluginState.ACTIVE if p['active'] else self.m.PluginState.INACTIVE)
        persistence = commit.commit(order, states)
        return {'plugin': name, 'active': p['active'], **persistence}

    def set_plugin_order(self, p, b):
        pl = self.o.pluginList()
        order = p['plugins']
        if (not isinstance(order, list) or any(not isinstance(n, str) for n in order)
                or len(order) != len(set(order)) or set(order) != set(pl.pluginNames())):
            raise ValueError('Supply every currently visible plugin exactly once')
        positions = {name.casefold(): i for i, name in enumerate(order)}
        for name in order:
            if any(master.casefold() in positions and positions[master.casefold()] > positions[name.casefold()]
                   for master in pl.masters(name)):
                raise ValueError('A master must load before its dependent: ' + name)
        commit = PluginPersistence(self.o, self.m, self.wait_until)
        states = self.plugin_state()
        pl.setLoadOrder(order)
        persistence = commit.commit(order, states)
        return {'requested': order, 'actual': sorted(pl.pluginNames(), key=pl.priority),
                'exact_match': True, **persistence}

    def archive(self, p):
        return validate_archive(p['archive'])

    def install_archive(self, p, b):
        return install_managed_archive(self, p, b)

    def update_archive(self, p, b):
        return update_managed_archive(self, p, b)

    def hide_file(self, p, b):
        mod = self.mod(p['mod'], writable=True)
        path = contained_path(mod.absolutePath(), p['path'])
        if path.name.lower() == 'meta.ini' or path.suffix.lower() in {'.esp', '.esm', '.esl'}:
            raise ValueError('Use plugin tools for ESP/ESM/ESL files; mod metadata is protected')
        hidden = Path(str(path) + '.mohidden')
        if not path.is_file() or hidden.exists():
            raise ValueError('File missing or an existing hidden copy would be overwritten')
        path.rename(hidden)
        self.refresh()
        return {'hidden': p['path'], 'mod': p['mod'], 'scope': 'Shared mod file: affects every profile using this mod'}

    def unhide_file(self, p, b):
        mod = self.mod(p['mod'], writable=True)
        path = contained_path(mod.absolutePath(), p['path'])
        hidden = contained_path(mod.absolutePath(), p['path'] + '.mohidden')
        if path.exists() or not hidden.is_file():
            raise ValueError('Hidden file missing or visible file already exists')
        hidden.rename(path)
        self.refresh()
        return {'unhidden': p['path'], 'mod': p['mod']}

    def backups(self, p, b):
        root = self.data / 'backups'
        return {'backup_ids': sorted((x.name for x in root.iterdir() if x.is_dir()), reverse=True) if root.exists() else []}

    def restore_order(self, p, b):
        progress = {'stage': 'restore-preflight', 'changes_may_have_occurred': False,
                    'completed_steps': []}
        try:
            source = self.get_backup(p['backup_id'])
            before = read_order_backup(source)
            current = self.snapshot()
            primary = list(self.o.managedGame().primaryPlugins())
            # Both save verifiers must be available BEFORE any mutation.
            PluginPersistence(self.o, self.m, self.wait_until)
            mod_save = ModListPersistence(self.o, self.wait_until)
            plan = build_restore_plan(before, current, primary,
                                      plugin_origins=self.o.getFileOrigins)

            def guard_context():
                if recovery_context(self.snapshot()) != plan['context']:
                    raise RuntimeError('Profile or physical context changed during restore; no further setters applied')
                if self.running:
                    raise RuntimeError('A game or MO2 tool started during restore; no further setters applied')

            for name, priority in plan['mod_moves']:
                guard_context()
                progress.update(stage='restore-mod-priority', changes_may_have_occurred=True)
                self.move_mod(name, priority)
                progress['completed_steps'].append({'mod': name, 'priority': priority})
            for name, active in plan['mod_states']:
                guard_context()
                progress.update(stage='restore-mod-state', changes_may_have_occurred=True)
                self.o.modList().setActive(name, active)
                progress['completed_steps'].append({'mod': name, 'active': active})
            if plan['mod_moves'] or plan['mod_states']:
                progress['stage'] = 'restore-mod-refresh'
                self.refresh()
            progress['stage'] = 'restore-revalidate'
            guard_context()
            revalidate_restore_plan(plan, self.snapshot(),
                plugin_origins=self.o.getFileOrigins,
                primary_plugins=list(self.o.managedGame().primaryPlugins()))
            commit = PluginPersistence(self.o, self.m, self.wait_until)
            pl = self.o.pluginList()
            guard_context()
            progress.update(stage='restore-plugin-state', changes_may_have_occurred=True)
            pl.setLoadOrder(plan['order'])
            for name in plan['order']:
                guard_context()
                pl.setState(name, self.m.PluginState.ACTIVE if plan['states'][name]
                            else self.m.PluginState.INACTIVE)
            progress['completed_steps'].append({'plugin_order_and_states_applied': True})
            progress['stage'] = 'verify-plugin-save'
            guard_context()
            persistence = commit.commit(plan['order'], plan['states'])

            def check_final():
                guard_context()
                verify_restore_result(plan, self.snapshot(),
                    plugin_origins=self.o.getFileOrigins,
                    primary_plugins=list(self.o.managedGame().primaryPlugins()))
                matches, why = commit.saved_matches(plan['order'], plan['states'])
                if not matches:
                    raise RuntimeError('Saved plugin files changed during final restore verification: ' + why)

            progress['stage'] = 'verify-mod-save'
            final_snapshot = self.snapshot()
            check_final()
            mod_persistence = mod_save.verify(final_snapshot['mods'], check_final)
            check_final()
            return {**persistence, **mod_persistence,
                    'verified_files': persistence['verified_files'] + ['modlist.txt'],
                    'restored_profile_order': before['profile'],
                    'extra_mods_disabled': plan['extra_mods_disabled'],
                    'scope': 'Priorities and enabled states only; mod files and removed mods are not restored'}
        except Exception as error:
            error.recovery_details = {**progress, 'automatic_rollback_attempted': False}
            raise

    def remove_empty_separator(self, p, b):
        mod = self.mod(p['mod'], writable=True)
        if not mod.isSeparator():
            raise ValueError('Only empty separators can be removed through this tool')
        contents = list(Path(mod.absolutePath()).iterdir())
        if any(x.name.lower() != 'meta.ini' or not x.is_file() for x in contents):
            raise ValueError('Separator contains unexpected files; removal refused')
        removed = self.o.modList().removeMod(mod)
        self.refresh()
        return {'removed': bool(removed), 'name': p['mod']}

    def remove_mod(self, p, b):
        return remove_managed_mod(self, p, b)

    def diagnostics(self, p, b):
        snapshot = self.snapshot()
        enabled = {x['name'].casefold() for x in snapshot['plugins'] if x['active']}
        positions = {x['name'].casefold(): x['priority'] for x in snapshot['plugins']}
        problems = []
        for plugin in snapshot['plugins']:
            if not plugin['active']:
                continue
            for master in plugin['masters']:
                if master.casefold() not in enabled:
                    problems.append({'type': 'missing_or_disabled_master', 'plugin': plugin['name'], 'master': master})
                elif positions[master.casefold()] > plugin['priority']:
                    problems.append({'type': 'master_loads_after_dependent', 'plugin': plugin['name'], 'master': master})
        full = sum(x['active'] and not x['light'] for x in snapshot['plugins'])
        light = sum(x['active'] and x['light'] for x in snapshot['plugins'])
        if full > 254:
            problems.append({'type': 'full_plugin_limit', 'count': full})
        if light > 4096:
            problems.append({'type': 'light_plugin_limit', 'count': light})
        return {'profile': snapshot['profile'], 'problems': problems, 'full_plugins': full, 'light_plugins': light,
                'limits': 'Checks plugin dependencies and budgets; cannot certify DLL, animation or ESP record compatibility'}

    def logs(self, p, b):
        # Logs are data, never executable instructions. Do not expose MO2 credentials.
        root = Path(self.data).parents[2] / 'logs'
        candidates = sorted(root.glob('mo_interface*.log'), key=lambda x: x.stat().st_mtime, reverse=True)
        if not candidates:
            return {'log': None, 'lines': []}
        path = candidates[0]
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 32768))
            text = stream.read().decode('utf-8', errors='replace')
        lines = text.splitlines()[-min(200, max(1, int(p.get('lines', 50)))):]
        lines = ['[private line omitted]' if any(k in line.lower() for k in ('apikey', 'api key', 'authorization:', 'access_token', 'password=')) else line for line in lines]
        return {'log': str(path), 'lines': lines}


# Keep the public snapshot method callable internally without a parameter signature.
Operations.snapshot_rpc = Operations.snapshot_action
