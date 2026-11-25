"""Optional primary catalogs are not installed-plugin inventories."""
import copy
import hashlib
import json
import types
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import test_bridge
import test_order_recovery as order_fixtures
from mo2_codex_bridge.order_recovery import (
    build_restore_plan, revalidate_restore_plan, validate_plugin_state,
    verify_restore_result)

OPTIONAL_PRIMARY = 'ccasvsse001-almsivi.esm'


class PrimaryInventoryTests(unittest.TestCase):
    def setUp(self):
        self.current = order_fixtures.snapshot()
        self.plugins = self.current['plugins']
        self.order = [item['name'] for item in self.plugins]
        self.states = {item['name']: item['active'] for item in self.plugins}

    def validate(self, *, primary=None, order=None, states=None):
        validate_plugin_state(self.plugins, self.order if order is None else order,
                              self.states if states is None else states,
                              ['Master.esm', OPTIONAL_PRIMARY] if primary is None else primary,
                              self.order)

    def plan(self, before=None, primary=None):
        saved = copy.deepcopy(self.current if before is None else before)
        saved.pop('revision', None)
        saved['revision'] = hashlib.sha256(json.dumps(saved, sort_keys=True).encode()).hexdigest()
        return build_restore_plan(saved, self.current,
                                  ['Master.esm', OPTIONAL_PRIMARY] if primary is None else primary,
                                  plugin_origins=order_fixtures.providers)

    def test_absent_native_ccc_entry_does_not_require_installation(self):
        self.validate()
        plan = self.plan()
        self.assertEqual(plan['order'], self.order)
        self.assertEqual(plan['states'], self.states)
        self.assertIn(OPTIONAL_PRIMARY, plan['primary_plugins'])
        revalidate_restore_plan(plan, self.current,
                                primary_plugins=['Master.esm', OPTIONAL_PRIMARY])
        verify_restore_result(plan, self.current,
                              primary_plugins=['Master.esm', OPTIONAL_PRIMARY])

    def test_visible_and_absent_primary_identities_are_case_insensitive(self):
        self.plugins[1]['masters'] = ['MASTER.ESM']
        self.validate(primary=['MASTER.ESM', OPTIONAL_PRIMARY.upper()])
        plan = self.plan(primary=['master.ESM', OPTIONAL_PRIMARY.upper()])
        self.assertEqual(plan['order'], self.order)

    def test_current_visible_primary_still_cannot_be_disabled(self):
        states = dict(self.states, **{'Master.esm': False})
        with self.assertRaisesRegex(ValueError, 'Primary game plugins'):
            self.validate(states=states)

    def test_current_visible_primary_still_cannot_move(self):
        with self.assertRaisesRegex(ValueError, 'Primary game plugin anchors'):
            self.validate(order=['Child.esp', 'Master.esm'])

    def present_optional(self):
        self.plugins[1]['priority'] = 2
        self.plugins.append(dict(name=OPTIONAL_PRIMARY.upper(), priority=1, active=True,
                                 masters=[], origin='Data', master=True, light=False))
        self.order = ['Master.esm', OPTIONAL_PRIMARY.upper(), 'Child.esp']
        self.states[OPTIONAL_PRIMARY.upper()] = True

    def test_present_optional_primary_remains_an_enabled_anchor(self):
        self.present_optional()
        self.validate()
        states = dict(self.states, **{OPTIONAL_PRIMARY.upper(): False})
        with self.assertRaisesRegex(ValueError, 'Primary game plugins'):
            self.validate(states=states)

    def test_present_optional_primary_still_cannot_move(self):
        self.present_optional()
        with self.assertRaisesRegex(ValueError, 'Primary game plugin anchors'):
            self.validate(order=['Master.esm', 'Child.esp', OPTIONAL_PRIMARY.upper()])

    def test_absent_primary_is_not_implicitly_satisfied_as_a_master(self):
        self.plugins[1]['masters'].append(OPTIONAL_PRIMARY.upper())
        with self.assertRaisesRegex(ValueError, 'missing or disabled master'):
            self.validate(primary=['MASTER.ESM', OPTIONAL_PRIMARY])

    def test_inactive_plugin_may_retain_an_absent_master(self):
        self.plugins[1]['masters'].append(OPTIONAL_PRIMARY)
        self.plugins[1]['active'] = False
        self.states['Child.esp'] = False
        self.validate()
        self.assertFalse(self.plan()['states']['Child.esp'])

    def test_backup_listed_primary_missing_from_current_still_refuses(self):
        before = copy.deepcopy(self.current)
        order_fixtures.append_plugin(before, OPTIONAL_PRIMARY, origin='Data')
        before['plugins'][-1]['master'] = True
        with self.assertRaisesRegex(ValueError, 'absent from current visible inventory'):
            self.plan(before)

    def test_visible_primary_disappearing_after_plan_still_refuses(self):
        plan = self.plan()
        self.current['plugins'].pop(0)
        with self.assertRaisesRegex(ValueError, 'Plugin inventory, headers'):
            revalidate_restore_plan(plan, self.current)

    def test_duplicate_catalog_identity_remains_ambiguous(self):
        with self.assertRaisesRegex(ValueError, 'Primary game plugin identity is ambiguous'):
            self.validate(primary=['Master.esm', 'MASTER.ESM', OPTIONAL_PRIMARY])


class PrimaryInventoryIntegrationTests(unittest.TestCase):
    setUp = test_bridge.BridgeTests.setUp
    tearDown = test_bridge.BridgeTests.tearDown
    change = test_bridge.BridgeTests.change

    def optional_catalog(self):
        self.organizer.managedGame = lambda: types.SimpleNamespace(
            gameShortName=lambda: 'SkyrimSE',
            primaryPlugins=lambda: ['MASTER.ESM', OPTIONAL_PRIMARY])

    def test_full_restore_persists_with_absent_optional_primary(self):
        self.optional_catalog()
        backup_id = self.change('set_mod_priority', mod='A', priority=1)['backup_id']
        result = self.change('restore_order', backup_id=backup_id)
        self.assertTrue(result['persisted'])
        self.assertTrue(result['modlist_persisted'])
        self.assertEqual(set(result['verified_files']), {'modlist.txt','plugins.txt','loadorder.txt'})
        self.assertEqual(self.organizer.ml.order, ['A','B'])
        self.assertEqual(self.organizer.pl.order, ['Master.esm','Child.esp'])

    def test_absent_catalog_dependency_refuses_before_all_setters(self):
        self.optional_catalog()
        backup_id = self.change('set_mod_priority', mod='A', priority=1)['backup_id']
        self.organizer.pl.masters = lambda name: ['Master.esm', OPTIONAL_PRIMARY.upper()] if name=='Child.esp' else []
        self.organizer.pl.active.discard('Child.esp')
        self.organizer.pl.refresh_indexes()
        self.organizer.pl.write_files()
        original_lists = {name:(self.root/'profile'/name).read_bytes()
                          for name in ['modlist.txt','plugins.txt','loadorder.txt']}
        with ExitStack() as stack:
            calls = [stack.enter_context(patch.object(target, method, wraps=getattr(target, method)))
                     for target,method in [(self.organizer.ml,'setPriority'),
                                           (self.organizer.ml,'setActive'),
                                           (self.organizer.pl,'setLoadOrder'),
                                           (self.organizer.pl,'setState')]]
            with self.assertRaisesRegex(ValueError, 'missing or disabled master') as caught:
                self.change('restore_order', backup_id=backup_id)
            self.assertTrue(all(call.call_count==0 for call in calls))
        self.assertFalse(caught.exception.bridge_failure['changes_may_have_occurred'])
        self.assertNotIn('Child.esp',self.organizer.pl.active)
        for name,data in original_lists.items():
            self.assertEqual((self.root/'profile'/name).read_bytes(),data)


if __name__ == '__main__':
    unittest.main(verbosity=2)
