"""MO2 optional-state loss when an update creates a no-provider gap."""
from pathlib import Path
from unittest import mock

import test_operation_fixes as base


class ProviderContinuityTests(base.OperationFixture):
    def setUp(self):
        super().setUp()
        self.registry['A']['Optional.esp']={'masters':['Master.esm'],'master':False,'light':False}
        self.refresh()
        self.organizer.pl.active.discard('Optional.esp')
        self.organizer.pl.refresh_indexes()
        self.organizer.pl.write_files()
        self.new_specs={name:dict(spec) for name,spec in self.registry['A'].items()}
        self.switches=[]
        self.provider_gaps=[]
        original=self.organizer.ml.setActive
        def native_active(name,value):
            result=original(name,value)
            # Native reacts to EACH mod state change. If the old version is
            # disabled first, its plugins disappear and disabled states are
            # forgotten before the replacement is enabled.
            self.refresh()
            self.switches.append((name,value))
            if 'Optional.esp' not in self.organizer.pl.order:
                self.provider_gaps.append((name,value))
            return result
        self.organizer.ml.setActive=native_active

    def update(self):
        return self.change('update_archive',mod='A',archive=str(self.archive),name='Updated',activate_update=True)

    def test_replacement_is_verified_enabled_before_old_provider_disappears(self):
        result=self.update()
        self.assertEqual(self.switches,[('Updated',True),('A',False)])
        self.assertEqual(self.provider_gaps,[])
        self.assertNotIn('Optional.esp',self.organizer.pl.active)
        self.assertIn('Child.esp',self.organizer.pl.active)
        self.assertEqual(self.organizer.pl.origin('Optional.esp'),'Updated')
        self.assertTrue(result['active'])
        self.assertFalse(result['previous_mod_active'])
        self.assertEqual(set(result['verified_files']),{'modlist.txt','loadorder.txt','plugins.txt'})
        entries=(self.root/'profile/plugins.txt').read_text(encoding='utf-8').splitlines()
        self.assertIn('Optional.esp',entries)
        self.assertNotIn('*Optional.esp',entries)

    def test_unrelated_enabled_state_drift_before_old_disable_is_preserved(self):
        original=self.organizer.ml.setActive
        def drift(name,value):
            result=original(name,value)
            if name=='Updated' and value: self.organizer.pl.active.discard('Child.esp')
            return result
        self.organizer.ml.setActive=drift
        with self.assertRaisesRegex(RuntimeError,'activation changed') as caught: self.update()
        self.assertTrue(self.ops.is_active('A'))
        self.assertTrue(self.ops.is_active('Updated'))
        self.assertNotIn(('A',False),self.switches)
        self.assertNotIn('Child.esp',self.organizer.pl.active)
        self.assertTrue(caught.exception.bridge_failure['changes_may_have_occurred'])

    def test_unconditional_optional_reset_is_refused_without_overwriting_it(self):
        original=self.organizer.ml.setActive
        def resets(name,value):
            result=original(name,value)
            if name=='Updated' and value: self.organizer.pl.active.add('Optional.esp')
            return result
        self.organizer.ml.setActive=resets
        with self.assertRaisesRegex(RuntimeError,'activation changed'): self.update()
        self.assertTrue(self.ops.is_active('A'))
        self.assertIn('Optional.esp',self.organizer.pl.active)
        self.assertNotIn(('A',False),self.switches)

    def test_native_rejecting_new_enable_does_not_disable_previous_version(self):
        original=self.organizer.ml.setActive
        def refuses(name,value):
            return False if name=='Updated' and value else original(name,value)
        self.organizer.ml.setActive=refuses
        with self.assertRaisesRegex(RuntimeError,'did not enable the replacement'): self.update()
        self.assertTrue(self.ops.is_active('A'))
        self.assertFalse(self.ops.is_active('Updated'))
        self.assertNotIn(('A',False),self.switches)

    def test_intermediate_save_failure_retains_previous_provider_and_recovery_id(self):
        original=self.organizer.pl.setPriority
        def fails(name,priority):
            if self.ops.is_active('A') and self.ops.is_active('Updated'):
                raise OSError('controlled save failure with both providers active')
            return original(name,priority)
        self.organizer.pl.setPriority=fails
        with self.assertRaises(OSError) as caught: self.update()
        failure=caught.exception.bridge_failure
        self.assertTrue(failure['changes_may_have_occurred'])
        self.assertTrue((self.ops.data/'backups'/failure['backup_id']/'before.json').is_file())
        self.assertTrue(self.ops.is_active('A'))
        self.assertNotIn(('A',False),self.switches)
        self.assertNotIn('Optional.esp',self.organizer.pl.active)

    def test_header_change_while_both_providers_active_blocks_old_disable(self):
        original=self.ops.wait_until
        edited=[]
        def waiter(check,timeout):
            result=original(check,timeout)
            if self.ops.is_active('A') and self.ops.is_active('Updated') and not edited:
                path=self.root/'mods/Updated/Optional.esp'
                value=base.plugin_bytes(masters=['Missing.esm'])
                path.write_bytes(value)
                edited.append((path,value))
            return result
        self.ops.wait_until=waiter
        with self.assertRaisesRegex((RuntimeError,ValueError),'headers changed'): self.update()
        self.assertTrue(self.ops.is_active('A'))
        self.assertNotIn(('A',False),self.switches)
        self.assertEqual(edited[0][0].read_bytes(),edited[0][1])

    def test_game_start_during_intermediate_save_blocks_old_disable(self):
        original=self.ops.wait_until
        def waiter(check,timeout):
            result=original(check,timeout)
            if self.ops.is_active('A') and self.ops.is_active('Updated'):
                self.ops.running.add('controlled fake tool')
            return result
        self.ops.wait_until=waiter
        with self.assertRaisesRegex(RuntimeError,'tool is running'): self.update()
        self.assertTrue(self.ops.is_active('A'))
        self.assertNotIn(('A',False),self.switches)

    def test_failure_after_old_disable_keeps_optional_state_and_reports_partial(self):
        original=self.organizer.ml.setActive
        def fails(name,value):
            result=original(name,value)
            if name=='A' and value is False: raise OSError('controlled failure after old disable')
            return result
        self.organizer.ml.setActive=fails
        with self.assertRaises(OSError) as caught: self.update()
        self.assertFalse(self.ops.is_active('A'))
        self.assertTrue(self.ops.is_active('Updated'))
        self.assertNotIn('Optional.esp',self.organizer.pl.active)
        self.assertTrue(caught.exception.bridge_failure['changes_may_have_occurred'])
        self.assertFalse(caught.exception.bridge_failure['recovery_details']['automatic_rollback_attempted'])
