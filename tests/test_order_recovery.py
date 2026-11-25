"""Pure/fake-folder safety regressions; never access a real MO2 instance."""
import copy
import hashlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if 'mo2_codex_bridge' not in sys.modules:
    package = types.ModuleType('mo2_codex_bridge')
    package.__path__ = [str(ROOT / 'mo2_codex_bridge')]
    sys.modules['mo2_codex_bridge'] = package
from mo2_codex_bridge.order_recovery import (
    build_restore_plan, revalidate_restore_plan, root_plugin_files,
    read_order_backup, verify_restore_result)


def snapshot():
    root = 'G:/isolated-fixture'
    return {'profile': 'Test', 'profile_path': root + '/profiles/Test',
            'paths': {'mods': root + '/mods', 'downloads': root + '/downloads',
                      'overwrite': root + '/overwrite'},
            'recovery_identity': {'bridge_data_path': root + '/bridge-data', 'game': 'SkyrimSE'},
            'mods': [dict(name=name, priority=index, active=True,
                          path=root + '/mods/' + name, foreign=False,
                          overwrite=False, separator=False)
                     for index, name in enumerate(['A', 'B'])],
            'plugins': [dict(name='Master.esm', priority=0, active=True,
                             masters=[], origin='Data', master=True, light=False),
                        dict(name='Child.esp', priority=1, active=True,
                             masters=['Master.esm'], origin='A', master=False, light=False)]}


def providers(name):
    return ['Data'] if name.casefold() == 'master.esm' else ['A']


def append_mod(state, name, *, active=False):
    state['mods'].append(dict(name=name, priority=len(state['mods']), active=active,
                              path=state['paths']['mods'] + '/' + name,
                              foreign=False, overwrite=False, separator=False))


def append_plugin(state, name, *, active=True, masters=None, origin='A'):
    state['plugins'].append(dict(name=name, priority=len(state['plugins']), active=active,
                                 masters=masters or [], origin=origin, master=False, light=False))


class OrderRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.before = snapshot()
        self.current = copy.deepcopy(self.before)
        self.mutations = []

    def plan(self, **kwargs):
        kwargs.setdefault('plugin_origins', providers)
        saved = copy.deepcopy(self.before)
        saved.pop('revision', None)
        saved['revision'] = hashlib.sha256(json.dumps(saved, sort_keys=True).encode()).hexdigest()
        return build_restore_plan(saved, self.current, ['Master.esm'], **kwargs)

    def execute_fake(self, **kwargs):
        # This models the integration contract: ALL validation precedes the
        # FIRST setter. It deliberately cannot hide mutations in the planner.
        plan = self.plan(**kwargs)
        for name, priority in plan['mod_moves']:
            self.mutations.append(('mod_priority', name, priority))
        for name, active in plan['mod_states']:
            self.mutations.append(('mod_active', name, active))
        self.mutations.append(('plugin_order', plan['order']))
        self.mutations.append(('plugin_states', plan['states']))
        return plan

    def refuse_before_mutation(self, message, **kwargs):
        with self.assertRaisesRegex(ValueError, message):
            self.execute_fake(**kwargs)
        self.assertEqual(self.mutations, [])

    def test_missing_current_master_refuses_before_any_setter(self):
        self.current['plugins'][1]['masters'] = ['Missing.esm']
        self.refuse_before_mutation('missing or disabled master')

    def test_uses_current_headers_not_backup_headers(self):
        self.before['plugins'][1]['masters'] = []
        self.current['plugins'][1]['masters'] = ['NewRequired.esm']
        self.refuse_before_mutation('NewRequired.esm')

    def test_restore_cannot_enable_plugin_with_target_disabled_master(self):
        self.before['plugins'][0]['active'] = False
        self.refuse_before_mutation('Primary game plugins')

    def test_mixed_case_masters_and_backup_names_are_valid(self):
        self.current['plugins'][1]['masters'] = ['MASTER.ESM']
        self.before['plugins'][1]['name'] = 'child.ESP'
        self.assertEqual(self.plan()['order'], ['Master.esm', 'Child.esp'])

    def test_current_new_master_after_restored_dependent_refuses(self):
        self.current['plugins'][1]['masters'] = ['New.esm']
        append_plugin(self.current, 'New.esm', origin='Data')
        self.current['plugins'][2]['master'] = True
        self.refuse_before_mutation('master must load before')

    def test_plugin_absent_from_current_inventory_is_not_silently_dropped(self):
        self.current['plugins'].pop()
        self.refuse_before_mutation('absent from current visible inventory')

    def test_added_plugin_preserves_its_enabled_state_and_is_validated(self):
        append_plugin(self.current, 'New.esp', masters=['Child.esp'])
        plan = self.plan()
        self.assertEqual(plan['order'], ['Master.esm', 'Child.esp', 'New.esp'])
        self.assertTrue(plan['states']['New.esp'])

    def test_added_plugin_missing_master_is_refused(self):
        append_plugin(self.current, 'New.esp', masters=['Absent.esm'])
        self.refuse_before_mutation('missing or disabled master')

    def test_plugin_activation_restore_does_not_require_mod_toggle(self):
        self.before['plugins'][1]['active'] = False
        plan = self.plan()
        self.assertFalse(plan['states']['Child.esp'])
        self.assertEqual(plan['mod_states'], [])

    def test_removed_mod_is_refused_before_any_setter(self):
        self.current['mods'].pop()
        self.refuse_before_mutation('removed mods')

    def test_new_inactive_mod_is_preserved_without_toggling(self):
        append_mod(self.current, 'New', active=False)
        plan = self.plan()
        self.assertEqual(plan['mod_states'], [])
        self.assertEqual(plan['mod_order'], ['A', 'B', 'New'])

    def test_new_active_plugin_mod_is_not_disabled_speculatively(self):
        append_mod(self.current, 'New', active=True)
        self.refuse_before_mutation('Plugin-bearing mod', plugin_files=lambda mod: ['New.esp'])

    def test_new_active_plugin_free_mod_can_be_disabled(self):
        append_mod(self.current, 'New', active=True)
        plan = self.plan(plugin_files=lambda mod: [])
        self.assertEqual(plan['mod_states'], [('New', False)])
        self.assertEqual(plan['extra_mods_disabled'], ['New'])

    def test_unavailable_prospective_inventory_fails_before_any_setter(self):
        self.before['mods'][1]['active'] = False
        def unavailable(mod):
            raise OSError('Unreadable folder')
        self.refuse_before_mutation('cannot be proved', plugin_files=unavailable)

    def test_visible_plugin_provider_disproves_plugin_free_claim(self):
        self.before['mods'][0]['active'] = False
        self.refuse_before_mutation('disagrees with visible providers', plugin_files=lambda mod: [])

    def test_plugin_free_enabled_state_change_is_supported(self):
        self.before['mods'][1]['active'] = False
        plan = self.plan(plugin_files=lambda mod: [])
        self.assertEqual(plan['mod_states'], [('B', False)])
        self.current['mods'][1]['active'] = False
        revalidate_restore_plan(plan, self.current)

    def test_priority_change_requires_unambiguous_providers(self):
        self.current['mods'][0]['priority'] = 1
        self.current['mods'][1]['priority'] = 0
        self.refuse_before_mutation('Ambiguous plugin providers', plugin_origins=lambda name: ['A', 'B'])

    def test_missing_provider_api_refuses_priority_changes(self):
        self.current['mods'][0]['priority'] = 1
        self.current['mods'][1]['priority'] = 0
        self.refuse_before_mutation('provider proof is unavailable', plugin_origins=None)

    def test_safe_priority_changes_plan_and_revalidate(self):
        self.current['mods'][0]['priority'] = 1
        self.current['mods'][1]['priority'] = 0
        plan = self.plan()
        self.assertEqual(plan['mod_moves'], [('A', 0)])
        self.current['mods'][0]['priority'] = 0
        self.current['mods'][1]['priority'] = 1
        revalidate_restore_plan(plan, self.current, plugin_origins=providers)

    def test_moves_use_simulated_priorities_not_stale_initial_positions(self):
        append_mod(self.current, 'New', active=False)
        self.current['mods'][0]['priority'] = 2
        self.current['mods'][1]['priority'] = 1
        self.current['mods'][2]['priority'] = 0
        plan = self.plan()
        self.assertEqual(plan['mod_moves'], [('A', 0), ('B', 1)])
        self.assertEqual(plan['mod_order'], ['A', 'B', 'New'])

    def test_wrong_profile_path_with_same_name_is_refused(self):
        self.current['profile_path'] += '-other'
        self.refuse_before_mutation('another profile, root or MO2 instance')

    def test_wrong_configured_root_is_refused(self):
        self.current['paths']['mods'] += '-other'
        self.refuse_before_mutation('another profile, root or MO2 instance')

    def test_wrong_bridge_instance_is_refused(self):
        self.current['recovery_identity']['bridge_data_path'] += '-other'
        self.refuse_before_mutation('another profile, root or MO2 instance')

    def test_changed_game_is_refused(self):
        self.current['recovery_identity']['game'] = 'OtherGame'
        self.refuse_before_mutation('another profile, root or MO2 instance')

    def test_legacy_backup_requires_exact_physical_context(self):
        self.before.pop('recovery_identity')
        self.assertTrue(self.plan()['legacy_context'])
        self.before.pop('profile_path')
        self.refuse_before_mutation('exact profile/root identity')

    def test_canonical_windows_context_is_case_insensitive(self):
        self.before['profile_path'] = self.before['profile_path'].upper().replace('/', '\\')
        self.assertEqual(self.plan()['order'][0], 'Master.esm')

    def test_duplicate_casefold_plugin_identity_is_rejected(self):
        append_plugin(self.current, 'CHILD.ESP')
        self.refuse_before_mutation('Duplicate case-insensitive')

    def test_primary_anchor_cannot_move(self):
        self.before['plugins'][0]['priority'] = 1
        self.before['plugins'][1]['priority'] = 0
        self.refuse_before_mutation('Primary game plugin anchors')

    def test_mod_path_change_is_refused(self):
        self.current['mods'][0]['path'] += '-renamed'
        self.refuse_before_mutation('Mod folder changed')

    def test_header_change_after_refresh_stops_before_plugin_setters(self):
        plan = self.plan()
        self.current['plugins'][1]['masters'] = ['Other.esm']
        with self.assertRaisesRegex(ValueError, 'Plugin inventory, headers'):
            revalidate_restore_plan(plan, self.current)

    def test_profile_switch_after_refresh_is_refused(self):
        plan = self.plan()
        self.current['profile'] = 'Other'
        with self.assertRaisesRegex(ValueError, 'Profile, root or instance'):
            revalidate_restore_plan(plan, self.current)

    def test_new_plugin_after_refresh_is_refused(self):
        plan = self.plan()
        append_plugin(self.current, 'Unexpected.esp')
        with self.assertRaisesRegex(ValueError, 'Plugin inventory, headers'):
            revalidate_restore_plan(plan, self.current)

    def test_provider_change_after_refresh_is_refused(self):
        self.current['mods'][0]['priority'] = 1
        self.current['mods'][1]['priority'] = 0
        plan = self.plan()
        self.current['mods'][0]['priority'] = 0
        self.current['mods'][1]['priority'] = 1
        with self.assertRaisesRegex(ValueError, 'Ambiguous plugin providers'):
            revalidate_restore_plan(plan, self.current, plugin_origins=lambda name: ['A', 'B'])

    def test_mod_activation_rejection_is_not_misreported_as_success(self):
        self.before['mods'][1]['active'] = False
        plan = self.plan(plugin_files=lambda mod: [])
        with self.assertRaisesRegex(ValueError, 'planned mod enabled state'):
            revalidate_restore_plan(plan, self.current)

    def test_input_snapshots_are_not_modified_by_planning(self):
        before_copy, current_copy = copy.deepcopy(self.before), copy.deepcopy(self.current)
        self.plan()
        self.assertEqual(self.before, before_copy)
        self.assertEqual(self.current, current_copy)

    def test_confined_physical_plugin_free_proof(self):
        with tempfile.TemporaryDirectory(prefix='mo2-order-recovery-test-') as temporary:
            root = Path(temporary)
            mod = root / 'OnlyAssets'
            mod.mkdir()
            (mod / 'texture.dds').write_bytes(b'fake')
            (mod / 'optional').mkdir()
            (mod / 'optional' / 'Dormant.esp').write_bytes(b'fake')
            entry = {'name': 'OnlyAssets', 'path': str(mod)}
            self.assertEqual(root_plugin_files(entry, str(root)), ())
            (mod / 'Actual.esp').write_bytes(b'fake')
            self.assertEqual(root_plugin_files(entry, str(root)), ('Actual.esp',))
            entry['path'] = str(root / 'Other')
            with self.assertRaisesRegex(ValueError, 'identity cannot be proved'):
                root_plugin_files(entry, str(root))

    def test_directory_disguised_as_plugin_prevents_proof(self):
        with tempfile.TemporaryDirectory(prefix='mo2-order-recovery-test-') as temporary:
            root = Path(temporary)
            mod = root / 'Assets'
            mod.mkdir()
            (mod / 'Odd.esp').mkdir()
            with self.assertRaisesRegex(ValueError, 'Ambiguous plugin entry'):
                root_plugin_files({'name': 'Assets', 'path': str(mod)}, str(root))

    def test_corrupted_before_snapshot_fails_before_any_setter(self):
        saved = copy.deepcopy(self.before)
        saved['revision'] = hashlib.sha256(json.dumps(saved, sort_keys=True).encode()).hexdigest()
        saved['plugins'][1]['active'] = False
        with self.assertRaisesRegex(ValueError, 'integrity check failed'):
            build_restore_plan(saved, self.current, ['Master.esm'])
        self.assertEqual(self.mutations, [])

    def test_missing_before_revision_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'integrity check failed'):
            build_restore_plan(self.before, self.current, ['Master.esm'])

    def test_backup_reader_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory(prefix='mo2-order-recovery-test-') as temporary:
            (Path(temporary) / 'before.json').write_text('{"profile":"A","profile":"B"}')
            with self.assertRaisesRegex(ValueError, 'duplicate keys'):
                read_order_backup(temporary)

    def test_final_verifier_accepts_planned_order_and_states(self):
        append_plugin(self.before, 'Other.esp')
        append_plugin(self.current, 'Other.esp')
        self.current['plugins'][1]['priority'] = 2
        self.current['plugins'][2]['priority'] = 1
        self.before['plugins'][1]['active'] = False
        plan = self.plan()
        self.current['plugins'][1]['priority'] = 1
        self.current['plugins'][2]['priority'] = 2
        self.current['plugins'][1]['active'] = False
        verify_restore_result(plan, self.current)

    def test_final_verifier_rejects_unexpected_mod_change_during_wait(self):
        plan = self.plan()
        self.current['mods'][1]['active'] = False
        with self.assertRaisesRegex(ValueError, 'planned mod enabled state'):
            verify_restore_result(plan, self.current)

    def test_final_verifier_rejects_incorrect_plugin_state(self):
        self.before['plugins'][1]['active'] = False
        plan = self.plan()
        with self.assertRaisesRegex(ValueError, 'Final plugin order or enabled states'):
            verify_restore_result(plan, self.current)

    def test_final_verifier_rejects_changed_header(self):
        plan = self.plan()
        self.current['plugins'][1]['master'] = True
        with self.assertRaisesRegex(ValueError, 'Plugin inventory, headers or provider'):
            verify_restore_result(plan, self.current)

    def test_changed_primary_designation_after_refresh_is_refused(self):
        plan = self.plan()
        with self.assertRaisesRegex(ValueError, 'Primary game plugin identity changed'):
            revalidate_restore_plan(plan, self.current, primary_plugins=['Master.esm', 'Child.esp'])

if __name__ == '__main__':
    unittest.main(verbosity=2)

