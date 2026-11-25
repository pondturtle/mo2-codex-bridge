"""Validated native mod operations with verified saved state and recovery evidence."""
import hashlib
import json
import os
import shutil
from pathlib import Path

from .common import contained_path, digest, safe_name, write_json
from .mod_persistence import ModListPersistence
from .persistence import PluginPersistence
from .order_recovery import recovery_context, validate_plugin_state
from .prospective_plugins import read_headers, plan_activation, verify_activation, provider_identity, comparison_mods, native_relationship_order

_ARCHIVES = {'.zip', '.7z', '.rar', '.omod', '.fomod'}
_GENERIC = {'expected_profile', 'expected_revision'}


class _Change:
    def __init__(self, ops, backup, action):
        self.ops, self.backup = ops, Path(backup)
        self.before = ops.snapshot()
        self.context = recovery_context(self.before)
        self.progress = {'stage':'preflight', 'action':action,
                         'changes_may_have_occurred':False, 'completed_steps':[]}
        self.metadata_aliases = []
        self.comparison_before = {**self.before,'mods':comparison_mods(ops,self.before['mods'],self.metadata_aliases)}
        if self.metadata_aliases:
            write_json(self.backup/'metadata-identity-proof.json',{'mod_aliases':self.metadata_aliases})

    def __enter__(self):
        return self

    def __exit__(self, kind, error, traceback):
        if error is not None:
            nested = getattr(error, 'recovery_details', None)
            if isinstance(nested, dict):
                self.progress['nested_failure'] = nested
                if nested.get('changes_may_have_occurred') is True:
                    self.progress['changes_may_have_occurred'] = True
            error.recovery_details = {**self.progress, 'backup_id':self.backup.name,
                'automatic_rollback_attempted':False}
        return False

    def guard_context(self):
        """Read only stable organizer identity during native cache transitions.

        removeMod invalidates native mod indexes until the full refresh. Do
        not enumerate either model inside that window, including for guards.
        """
        ops = self.ops
        current = {'profile':ops.o.profileName(),'profile_path':ops.o.profilePath(),
                   'paths':{'mods':ops.o.modsPath(),'downloads':ops.o.downloadsPath(),
                            'overwrite':ops.o.overwritePath()},
                   'recovery_identity':{'bridge_data_path':str(ops.data.resolve()),
                                        'game':ops.o.managedGame().gameShortName()}}
        if recovery_context(current) != self.context:
            raise RuntimeError('Profile or instance changed; no further native setters applied')
        if ops.running:
            raise RuntimeError('A game or MO2 tool is running; no further native setters applied')
        return current

    def guard(self, *, unchanged=False):
        current = self.ops.snapshot()
        if recovery_context(current) != self.context:
            raise RuntimeError('Profile or instance changed; no further native setters applied')
        if self.ops.running:
            raise RuntimeError('A game or MO2 tool is running; no further native setters applied')
        if unchanged and current['revision'] != self.before['revision']:
            raise ValueError('MO2 state changed during preflight; inspect before retrying')
        return current

    def step(self, stage, *, mutating=False):
        self.progress['stage'] = stage
        if mutating:
            self.progress['changes_may_have_occurred'] = True

    def completed(self, **value):
        self.progress['completed_steps'].append(value)


def _fields(params, allowed):
    unexpected = set(params) - (set(allowed) | _GENERIC)
    if unexpected:
        raise ValueError('Unsupported operation parameters: ' + ', '.join(sorted(unexpected)))


def _boolean(params, key):
    if key in params and type(params[key]) is not bool:
        raise ValueError(key + ' must be a boolean')


def _priority(ops, value, *, creating=False):
    maximum = len(ops.o.modList().allModsByProfilePriority()) - (0 if creating else 1)
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError('Priority must be an integer within the prospective movable list')
    return value


def _new_name(ops, value, *, separator=False):
    name = safe_name(value)
    if separator and not name.endswith('_separator'):
        name += '_separator'
    name = safe_name(name)
    known = {item.casefold() for item in ops.o.modList().allMods()}
    if name.casefold() in known or (Path(ops.o.modsPath()) / name).exists():
        raise ValueError('A mod or separator with this name already exists')
    return name


def _hash_file(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def validate_archive(value):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('Specify an absolute local mod archive path')
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('Specify an absolute local mod archive path')
    path = path.resolve()
    if not path.is_file() or path.suffix.casefold() not in _ARCHIVES:
        raise ValueError('Specify an existing local mod archive')
    return path


def _archive_identity(path):
    before = path.stat()
    value = {'path':str(path), 'bytes':before.st_size, 'mtime_ns':before.st_mtime_ns,
             'sha256':_hash_file(path)}
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Archive changed during preflight')
    return value


def _same_archive(path, expected):
    if _archive_identity(path) != expected:
        raise ValueError('Archive changed since preflight; installed outcome needs inspection')


def _install_plan(ops, params):
    _fields(params, {'name','archive','active','priority'})
    _boolean(params, 'active')
    name = _new_name(ops, params['name'])
    priority = _priority(ops, params['priority'], creating=True) if 'priority' in params else None
    archive = validate_archive(params['archive'])
    return {'name':name, 'archive':archive, 'archive_identity':_archive_identity(archive),
            'active':params.get('active'), 'priority':priority}


def _facts(items, *, skip=(), permit_active=(), provider_mods=None, resolver=None):
    skip, permit = {x.casefold() for x in skip}, {x.casefold() for x in permit_active}
    result = []
    for item in sorted(items, key=lambda value:value['priority']):
        if item['name'].casefold() in skip:
            continue
        row = {key:value for key,value in item.items() if key not in {'priority','display_name','version','latest_known_version','nexus_id','load_order'}}
        if item['name'].casefold() in permit:
            row.pop('active', None)
        if provider_mods is not None:
            row.update(provider_identity(item,provider_mods,resolver))
        result.append(row)
    return result


def _existing_preserved(change, current, new_name, *, permit_active=()):
    before_mods = change.comparison_before['mods']
    current_mods = comparison_mods(change.ops,current['mods'])
    old_names = {item['name'].casefold() for item in before_mods}
    observed = [item for item in current_mods if item['name'].casefold() in old_names]
    if _facts(observed, permit_active=permit_active) != _facts(before_mods, permit_active=permit_active):
        raise RuntimeError('Existing mod identity, activation or relative order changed during the operation')
    if {item['name'].casefold() for item in current_mods} != old_names | {new_name.casefold()}:
        raise RuntimeError('Unexpected mod inventory change during the operation')


def _mod_snapshot(current, name):
    found = [item for item in current['mods'] if item['name'].casefold() == name.casefold()]
    if len(found) != 1:
        raise RuntimeError('New mod identity is missing or ambiguous: ' + name)
    return found[0]


def _save_plugins(change):
    current = change.guard()
    order = [item['name'] for item in sorted(current['plugins'], key=lambda value:value['priority'])]
    states = {item['name']:item['active'] for item in current['plugins']}
    primary = list(change.ops.o.managedGame().primaryPlugins())
    validate_plugin_state(current['plugins'], order, states, primary, order)
    commit = PluginPersistence(change.ops.o, change.ops.m, change.ops.wait_until)
    change.step('verify-plugin-save')
    result = commit.commit(order, states)
    change.guard()
    return result


def _move(change, name, priority):
    change.guard()
    change.step('set-new-mod-priority', mutating=True)
    actual = change.ops.move_mod(name, priority)
    if actual != priority:
        raise RuntimeError('MO2 did not apply the requested new mod priority')
    change.completed(mod=name, priority=priority)


def create_separator(ops, params, backup):
    with _Change(ops, backup, 'create_separator') as change:
        _fields(params, {'name','priority'})
        name = _new_name(ops, params['name'], separator=True)
        priority = _priority(ops, params['priority'], creating=True) if 'priority' in params else None
        saved = ModListPersistence(ops.o, ops.wait_until)
        change.guard(unchanged=True)
        change.step('create-separator', mutating=True)
        created = ops.o.createMod(ops.m.GuessedString(name))
        if created is None:
            raise RuntimeError('MO2 did not create the separator')
        change.completed(created=name)
        change.guard()
        ops.refresh()
        current = change.guard()
        if not ops.mod(name).isSeparator():
            raise RuntimeError('MO2 did not recognize the new separator')
        if priority is not None:
            _move(change, name, priority)
        def check():
            current = change.guard()
            _existing_preserved(change, current, name)
            if not _mod_snapshot(current, name)['separator']:
                raise RuntimeError('Separator identity changed')
            if _facts(current['plugins'],provider_mods=comparison_mods(ops,current['mods']),resolver=ops.o.resolvePath) != _facts(change.before['plugins'],provider_mods=change.comparison_before['mods'],resolver=ops.o.resolvePath):
                raise RuntimeError('Plugin state changed during separator creation')
        current = change.guard()
        change.step('verify-mod-save')
        persistence = saved.verify(current['mods'], check)
        check()
        observed = _mod_snapshot(change.guard(), name)
        return {'name':name, 'priority':observed['priority'], 'separator':True, **persistence}


def _unchanged_plugins(change, current):
    if _facts(current['plugins'],provider_mods=comparison_mods(change.ops,current['mods']),resolver=change.ops.o.resolvePath) != _facts(change.before['plugins'],provider_mods=change.comparison_before['mods'],resolver=change.ops.o.resolvePath):
        raise RuntimeError('Existing plugin identity, state or relative order changed during archive staging')


def _commit_activation_order(change, activation, *, permit_active=()):
    """Complete a preplanned insertion after MO2's native master repair.

    Verify all non-order facts first, then bind the corrective setter to that
    exact observed revision. Later user/callback work is never rolled back.
    """
    ops = change.ops
    current = change.guard()
    _existing_preserved(change,current,activation['new_mod'],permit_active=permit_active)
    if not activation['introduced']:
        verify_activation(ops,activation,current)
        return
    verify_activation(ops,activation,current,before_order_commit=True)
    names = {item['name'].casefold():item['name'] for item in current['plugins']}
    order = [names[name] for name in activation['order']]
    actual = [item['name'] for item in sorted(current['plugins'],key=lambda item:item['priority'])]
    if actual == order:
        verify_activation(ops,activation,current)
        return
    if ops.o.appVersion().displayString() != '2.5.2':
        raise RuntimeError('Unrecognized native plugin ordering version; no order setter applied')
    fingerprint = native_relationship_order(activation,ops.o.pluginList().pluginNames(),
        list(ops.o.managedGame().primaryPlugins()),current['plugins'])
    if [name.casefold() for name in actual] != fingerprint:
        raise RuntimeError('Unrecognized plugin order change during refresh; no order setter applied')
    revision = current['revision']
    write_json(change.backup/'activation-order-plan.json',{
        'observed_revision':revision,'order':order,
        'existing_order':activation['existing_order'],
        'introduced':activation['introduced'],'native_relationship_fingerprint':fingerprint})
    if change.guard()['revision'] != revision:
        raise RuntimeError('MO2 state changed before planned insertion commit; no order setter applied')
    # Header files are outside snapshot revision; verify them independently.
    if read_headers(ops,activation['new_mod']) != activation['headers']:
        raise RuntimeError('New plugin headers changed before planned insertion commit')
    if change.guard()['revision'] != revision:
        raise RuntimeError('MO2 state changed before planned insertion commit; no order setter applied')
    change.step('commit-planned-plugin-insertion',mutating=True)
    ops.o.pluginList().setLoadOrder(order)
    change.completed(plugin_order=order)
    verify_activation(ops,activation,change.guard())
    _save_plugins(change)
    verify_activation(ops,activation,change.guard())


def _install(change, plan):
    ops = change.ops
    saved = ModListPersistence(ops.o, ops.wait_until)
    PluginPersistence(ops.o, ops.m, ops.wait_until)
    change.guard(unchanged=True)
    _same_archive(plan['archive'], plan['archive_identity'])
    _new_name(ops, plan['name'])
    write_json(change.backup / 'archive-install-plan.json', {
        **{key:value for key,value in plan.items() if key!='archive'}, 'archive':str(plan['archive'])})
    change.guard(unchanged=True)
    change.step('native-install', mutating=True)
    created = ops.o.installMod(str(plan['archive']), plan['name'])
    if created is None:
        current = change.guard()
        if current['revision'] != change.before['revision']:
            raise RuntimeError('Installer returned no mod after changing state; inspect before retrying')
        return {'installed':False,'changes_may_have_occurred':True,
                'reason':'Installer returned no mod; no visible model change, but partial files cannot be ruled out'}
    installed_name = created.name()
    change.completed(installed=installed_name)
    if installed_name != plan['name']:
        raise RuntimeError('Installer returned an unexpected mod name; inspect partial installation')
    change.guard()
    ops.refresh()
    current = change.guard()
    _existing_preserved(change,current,installed_name)
    # MO2 core creates regular mods disabled. Third-party installed callbacks may
    # enable one: report that partial outcome, never pretend proof preceded it.
    if _mod_snapshot(current,installed_name)['active']:
        raise RuntimeError('Native installer unexpectedly enabled the new mod before activation preflight')
    _unchanged_plugins(change,current)
    _same_archive(plan['archive'],plan['archive_identity'])
    activation = None
    if plan['active'] is True:
        headers = read_headers(ops,installed_name)
        staged = change.guard()
        staged = {**staged,'mods':comparison_mods(ops,staged['mods'])}
        activation = plan_activation(ops,change.comparison_before,staged,installed_name,headers,target_priority=plan['priority'])
    if plan['priority'] is not None:
        _move(change,installed_name,plan['priority'])
        _unchanged_plugins(change,change.guard())
    if activation is not None:
        change.guard()
        _unchanged_plugins(change,change.guard())
        if read_headers(ops,installed_name)!=headers:
            raise ValueError('New plugin headers changed before activation')
        change.step('set-new-mod-active',mutating=True)
        ops.o.modList().setActive(installed_name,True)
        change.completed(mod=installed_name,active=True)
        change.guard()
        ops.refresh()
        _commit_activation_order(change,activation)
    def check():
        current = change.guard()
        _existing_preserved(change,current,installed_name)
        new = _mod_snapshot(current,installed_name)
        if new['active']!=(plan['active'] is True):
            raise RuntimeError('MO2 did not apply the requested new mod activation')
        if plan['priority'] is not None and new['priority']!=plan['priority']:
            raise RuntimeError('MO2 did not apply the requested new mod priority')
        if activation is None:
            _unchanged_plugins(change,current)
        else:
            verify_activation(ops,activation,current)
    check()
    change.step('verify-mod-save')
    persistence = saved.verify(change.guard()['mods'],check)
    plugin_save = _save_plugins(change)
    check()
    observed = _mod_snapshot(change.guard(),installed_name)
    return {'installed':True,'name':observed['name'],'active':observed['active'],
            'priority':observed['priority'],'archive_sha256':plan['archive_identity']['sha256'],
            **persistence,**plugin_save,'verified_files':plugin_save['verified_files']+['modlist.txt']}


def install_archive(ops, params, backup):
    with _Change(ops, backup, 'install_archive') as change:
        return _install(change, _install_plan(ops, params))


def update_archive(ops,params,backup):
    with _Change(ops,backup,'update_archive') as change:
        _fields(params,{'mod','archive','name','activate_update','priority'})
        _boolean(params,'activate_update')
        old = ops.mod(params['mod'],writable=True)
        if old.isSeparator():
            raise ValueError('Separators cannot be updated from an archive')
        activate = params.get('activate_update',False)
        if activate and 'priority' in params:
            raise ValueError('An activated update retains the previous mod priority; omit priority')
        options = {key:params[key] for key in ('archive','name','priority') if key in params}
        options['active']=False
        plan = _install_plan(ops,options)
        old_name=old.name()
        old_record=_mod_snapshot(change.before,old_name)
        result=_install(change,plan)
        if not result['installed']:
            return result
        saved=ModListPersistence(ops.o,ops.wait_until)
        activation=None
        if activate:
            # Prove the intended headers before even moving the staged entry.
            # Its future priority is modeled while original providers stay active.
            if old_record['active']:
                headers=read_headers(ops,result['name'])
                staged=change.guard()
                staged={**staged,'mods':comparison_mods(ops,staged['mods'])}
                activation=plan_activation(ops,change.comparison_before,staged,result['name'],headers,
                    disabling=old_name,target_priority=old_record['priority'])
                intermediate=plan_activation(ops,change.comparison_before,staged,result['name'],headers,
                    target_priority=old_record['priority'])
                activation['native_seed']=intermediate['order']
            _move(change,result['name'],old_record['priority'])
            _unchanged_plugins(change,change.guard())
            if activation is not None:
                if read_headers(ops,result['name'])!=headers:
                    raise ValueError('Updated plugin headers changed before activation')
                change.guard()
                _unchanged_plugins(change,change.guard())
                # Keep an old provider visible until the replacement is enabled.
                # A no-provider gap makes MO2 forget disabled optional states.
                # At old priority the replacement is immediately BELOW the old
                # entry shifted upward, so old-owned plugins still win here.
                change.step('activate-updated-version-beside-previous',mutating=True)
                ops.o.modList().setActive(result['name'],True)
                change.completed(mod=result['name'],active=True,previous_still_active=True)
                change.guard()
                ops.refresh()
                current=change.guard()
                if not _mod_snapshot(current,result['name'])['active']:
                    raise RuntimeError('MO2 did not enable the replacement; previous version remains enabled')
                _commit_activation_order(change,intermediate)
                _save_plugins(change)
                current=change.guard()
                _existing_preserved(change,current,result['name'])
                verify_activation(ops,intermediate,current)
                if read_headers(ops,result['name'])!=headers:
                    raise ValueError('Updated plugin headers changed before disabling the previous version')
                current=change.guard()
                _existing_preserved(change,current,result['name'])
                verify_activation(ops,intermediate,current)
                if not _mod_snapshot(current,result['name'])['active']:
                    raise RuntimeError('Replacement activation changed before previous version deactivation')
                change.step('disable-previous-version',mutating=True)
                ops.o.modList().setActive(old_name,False)
                change.completed(mod=old_name,active=False)
                change.guard()
                ops.refresh()
                _commit_activation_order(change,activation,permit_active=(old_name,))
        def check():
            current=change.guard()
            _existing_preserved(change,current,result['name'],permit_active=(old_name,) if activate else ())
            previous=_mod_snapshot(current,old_name)
            updated=_mod_snapshot(current,result['name'])
            if previous['active']!=(False if activate else old_record['active']):
                raise RuntimeError('Previous version activation differs from update plan')
            if updated['active']!=(old_record['active'] if activate else False):
                raise RuntimeError('Updated version activation differs from update plan')
            if activate and updated['priority']!=old_record['priority']:
                raise RuntimeError('Updated version priority differs from update plan')
            if not Path(previous['path']).is_dir():
                raise RuntimeError('Previous version folder is missing')
            if activation is None:
                _unchanged_plugins(change,current)
            else:
                verify_activation(ops,activation,current)
        check()
        change.step('verify-update-mod-save')
        mod_save=saved.verify(change.guard()['mods'],check)
        plugin_save=_save_plugins(change)
        check()
        current=change.guard()
        previous=_mod_snapshot(current,old_name)
        updated=_mod_snapshot(current,result['name'])
        return {**result,'active':updated['active'],'priority':updated['priority'],
                'previous_mod':old_name,'previous_mod_preserved':True,
                'previous_mod_active':previous['active'],'previous_mod_priority':previous['priority'],
                'update_activated':activate,**mod_save,**plugin_save,
                'verified_files':plugin_save['verified_files']+['modlist.txt'],
                'scope':'New version preserved separately; enabled-state changes affect only the current profile'}


def _tree_manifest(root):
    root = Path(root)
    if not root.is_dir() or root.is_symlink() or (hasattr(root,'is_junction') and root.is_junction()):
        raise ValueError('Removal requires an unlinked regular mod folder')
    directories, files = [], []
    for folder, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            entry = Path(folder) / name
            if entry.is_symlink() or (hasattr(entry,'is_junction') and entry.is_junction()):
                raise ValueError('Mod contains links or junctions; removal refused')
        directories += [(Path(folder)/name).relative_to(root).as_posix() for name in dirs]
        for name in names:
            entry = Path(folder)/name
            if not entry.is_file():
                raise ValueError('Unexpected mod entry prevents verified removal')
            before = entry.stat()
            value = _hash_file(entry)
            after = entry.stat()
            if (before.st_size,before.st_mtime_ns) != (after.st_size,after.st_mtime_ns):
                raise ValueError('Mod file changed while its removal backup was being prepared')
            files.append({'path':entry.relative_to(root).as_posix(),'bytes':after.st_size,'sha256':value})
    return {'directories':sorted(directories),'files':sorted(files,key=lambda item:item['path'])}


def _removal_plan(change, name):
    before = change.comparison_before
    primary = {item.casefold() for item in change.ops.o.managedGame().primaryPlugins()}
    removed = set()
    for plugin in before['plugins']:
        if provider_identity(plugin,before['mods'],change.ops.o.resolvePath)['origin'] != name.casefold():
            continue
        origins = change.ops.o.getFileOrigins(plugin['name'])
        if not isinstance(origins,(list,tuple)) or {item.casefold() for item in origins} != {name.casefold()}:
            raise ValueError('Alternate plugin providers prevent safe removal: ' + plugin['name'])
        if plugin['name'].casefold() in primary:
            raise ValueError('Primary game plugins cannot be removed')
        removed.add(plugin['name'].casefold())
    surviving = [item for item in before['plugins'] if item['name'].casefold() not in removed]
    order = [item['name'] for item in sorted(surviving,key=lambda value:value['priority'])]
    states = {item['name']:item['active'] for item in surviving}
    validate_plugin_state(surviving,order,states,list(change.ops.o.managedGame().primaryPlugins()),order)
    if not any(item['name'].casefold().endswith('.esp') and not item['master']
               and item['name'].casefold() not in primary for item in surviving):
        raise ValueError('No surviving ordinary ESP is available to verify native saved plugin lists')
    return {'mods':_facts(before['mods'],skip=(name,)), 'plugins':_facts(surviving,provider_mods=before['mods'],resolver=change.ops.o.resolvePath),
            'order':order,'states':states,'removed_plugins':sorted(removed)}


def remove_mod(ops, params, backup):
    with _Change(ops, backup, 'remove_mod') as change:
        _fields(params, {'mod','acknowledge_shared_folder'})
        if params.get('acknowledge_shared_folder') is not True:
            raise ValueError('Removing a mod affects every profile. Explicitly acknowledge_shared_folder=true.')
        mod = ops.mod(params['mod'], writable=True)
        if mod.isSeparator():
            raise ValueError('Use remove_empty_separator for a separator')
        name = mod.name()
        root = Path(mod.absolutePath())
        plan = _removal_plan(change, name)
        saved = ModListPersistence(ops.o, ops.wait_until)
        PluginPersistence(ops.o, ops.m, ops.wait_until)
        change.step('preserve-removal-folder')
        manifest = _tree_manifest(root)
        total = sum(item['bytes'] for item in manifest['files'])
        if shutil.disk_usage(change.backup).free < total + 64 * 1024 * 1024:
            raise ValueError('Not enough space to preserve the mod before removal')
        copy = change.backup / 'removed-mod'
        record = change.backup / 'removed-mod.json'
        if copy.exists() or record.exists():
            raise FileExistsError('Removal recovery files already exist')
        shutil.copytree(root, copy)
        if _tree_manifest(copy) != manifest or _tree_manifest(root) != manifest:
            raise ValueError('Removal recovery bytes or original files changed; no removal made')
        write_json(record, {'mod':name,'original_path':str(root),'context':change.context,
                           'payload':manifest,'bytes':total})
        if json.loads(record.read_text(encoding='utf-8'))['payload'] != manifest:
            raise ValueError('Saved removal recovery manifest failed verification')
        change.progress.update(mod_files_backup=str(copy), removal_backup_verified=True)
        change.guard(unchanged=True)
        if _tree_manifest(copy) != manifest or _tree_manifest(root) != manifest:
            raise ValueError('Removal recovery changed immediately before native removal')
        change.guard(unchanged=True)
        change.step('native-remove', mutating=True)
        removed = ops.o.modList().removeMod(mod)
        change.completed(native_remove_returned=bool(removed),mod=name)
        if not removed:
            raise RuntimeError('MO2 did not confirm mod removal; inspect the retained recovery copy')
        change.guard_context()
        change.step('refresh-after-native-remove')
        ops.refresh()
        change.guard_context()
        def check():
            current = change.guard()
            if any(item['name'].casefold()==name.casefold() for item in current['mods']) or root.exists():
                raise RuntimeError('Removed mod is still present')
            if _facts(comparison_mods(ops,current['mods'])) != plan['mods']:
                raise RuntimeError('Other mod identity, order or enabled state changed during removal')
            if _facts(current['plugins'],provider_mods=comparison_mods(ops,current['mods']),resolver=ops.o.resolvePath) != plan['plugins']:
                raise RuntimeError('Surviving plugin identity, headers, order or enabled state changed during removal')
        check()
        change.step('persist-removed-plugin-lists')
        persistence = PluginPersistence(ops.o, ops.m, ops.wait_until).commit(plan['order'],plan['states'])
        check()
        change.step('verify-removed-mod-save')
        mod_save = saved.verify(change.guard()['mods'],check)
        check()
        return {'removed':True,'name':name,'mod_files_backup':str(copy),
                'removal_backup_verified':True,'removed_plugins':plan['removed_plugins'],
                **persistence,**mod_save,
                'verified_files':persistence['verified_files']+['modlist.txt'],
                'scope':'Removed from shared Mods folder; every profile is affected'}
