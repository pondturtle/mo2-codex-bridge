"""Installer signal and stale-dialog regressions using isolated Qt contract doubles."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class Widget:
    def __init__(self, name='', text=''):
        self._name, self._text, self.visible, self.enabled = name, text, True, True
        self.deleted = False
    def objectName(self): return self._name
    def text(self): return self._text
    def isVisible(self): return self.visible
    def isEnabled(self): return self.enabled


class Group:
    def __init__(self, exclusive=True): self.value, self.members = exclusive, []
    def exclusive(self): return self.value


class Button(Widget):
    def __init__(self, name='', text='', checked=False, checkable=False, exclusive=False, group=None):
        super().__init__(name, text)
        self.checked, self.checkable, self.exclusive, self._group = checked, checkable, exclusive, group
        self.clicks, self.sets, self.on_click = 0, 0, lambda: None
        if group is not None: group.members.append(self)
    def isCheckable(self): return self.checkable
    def isChecked(self): return self.checked
    def autoExclusive(self): return self.exclusive
    def group(self): return self._group
    def setChecked(self, value): self.sets += 1; self.checked = value
    def click(self):
        if not self.enabled: return
        self.clicks += 1
        if self.checkable:
            exclusive = self._group.exclusive() if self._group else self.exclusive
            if not self.checked or not exclusive:
                self.checked = not self.checked
                if self.checked and self._group and self._group.exclusive():
                    for other in self._group.members:
                        if other is not self: other.checked = False
        self.on_click()


class Combo(Widget):
    def __init__(self, name='choice', values=('A', 'B')):
        super().__init__(name)
        self.values, self.index = values, 0
    def currentText(self): return self.values[self.index]
    def currentIndex(self): return self.index
    def count(self): return len(self.values)
    def itemText(self, index): return self.values[index]
    def setCurrentIndex(self, index): self.index = index


class Line(Widget):
    class EchoMode: Normal = 0
    def echoMode(self): return 0
    def isReadOnly(self): return False


class Label(Widget): pass


class Dialog(Widget):
    def __init__(self, name='FomodInstallerDialog', title='Test installer'):
        super().__init__(name)
        self.title, self.children = title, []
    def windowTitle(self): return self.title
    def findChildren(self, kind): return [w for w in self.children if isinstance(w, kind)]
    def findChild(self, kind, name): return next((w for w in self.children if isinstance(w, kind) and w.objectName() == name), None)


class Application:
    dialog = None
    @classmethod
    def activeModalWidget(cls): return cls.dialog


fakeqt = types.ModuleType('PyQt6')
fakeqt.sip = types.SimpleNamespace(unwrapinstance=lambda w: id(w), isdeleted=lambda w: w.deleted)
widgets = types.ModuleType('PyQt6.QtWidgets')
for name, value in {'QAbstractButton': Button, 'QApplication': Application, 'QComboBox': Combo, 'QDialog': Dialog,
                    'QLabel': Label, 'QLineEdit': Line}.items(): setattr(widgets, name, value)
with patch.dict(sys.modules, {'PyQt6': fakeqt, 'PyQt6.QtWidgets': widgets}):
    spec = importlib.util.spec_from_file_location('mo2_codex_bridge.dialogs_contract_under_test', ROOT / 'mo2_codex_bridge/dialogs.py')
    dialogs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dialogs)


class InstallerChoiceTests(unittest.TestCase):
    def setUp(self):
        self.profile, self.allow = 'Test Profile', True
        self.owner = types.SimpleNamespace(busy=True, active_job={'job_id': 'fixtureinstall', 'action': 'install_archive',
            'params': {'expected_profile': 'Test Profile', 'name': 'fixture AI MADE'}}, name=lambda: 'bridge',
            ops=types.SimpleNamespace(running=set()), organizer=types.SimpleNamespace(
                profileName=lambda: self.profile, pluginSetting=lambda *_: self.allow))
        self.dialog = Dialog()
        Application.dialog = self.dialog
    def tearDown(self): Application.dialog = None
    def request(self, control, **extra):
        current = dialogs.state(self.owner)
        return dict(dialog_id=current['dialog_id'], dialog_revision=current['dialog_revision'], control_id=str(id(control)), **extra)
    def test_checkbox_emits_native_clicked_and_updates_next_button(self):
        choice, next_button = Button('option', checkable=True), Button('next')
        next_button.enabled = False
        choice.on_click = lambda: setattr(next_button, 'enabled', choice.checked)
        self.dialog.children = [choice, next_button]
        result = dialogs.choose(self.owner, self.request(choice, checked=True))
        self.assertTrue(choice.checked)
        self.assertTrue(next_button.enabled)
        self.assertEqual((choice.clicks, choice.sets), (1, 0))
        self.assertTrue(result['installer_state']['installer_open'])
    def test_checkbox_deselect_uses_click_and_updates_native_state(self):
        choice = Button('option', checked=True, checkable=True)
        self.dialog.children = [choice]
        dialogs.choose(self.owner, self.request(choice, checked=False))
        self.assertFalse(choice.checked)
        self.assertEqual(choice.clicks, 1)
    def test_same_selection_is_idempotent(self):
        choice = Button('option', checked=True, checkable=True)
        self.dialog.children = [choice]
        result = dialogs.choose(self.owner, self.request(choice, checked=True))
        self.assertFalse(result['changed'])
        self.assertEqual(choice.clicks, 0)
    def test_radio_selection_preserves_native_exclusivity(self):
        group = Group()
        a, b = Button('a', checked=True, checkable=True, group=group), Button('b', checkable=True, group=group)
        self.dialog.children = [a, b]
        dialogs.choose(self.owner, self.request(b, checked=True))
        self.assertEqual((a.checked, b.checked), (False, True))
        self.assertEqual(b.clicks, 1)
    def test_selected_exclusive_radio_cannot_be_unchecked_directly(self):
        a = Button('a', checked=True, checkable=True, exclusive=True)
        self.dialog.children = [a]
        with self.assertRaisesRegex(ValueError, 'exclusive choice'):
            dialogs.choose(self.owner, self.request(a, checked=False))
        self.assertTrue(a.checked)
        self.assertEqual(a.clicks, 0)
    def test_nonexclusive_button_group_allows_unchecking(self):
        group = Group(False)
        a = Button('a', checked=True, checkable=True, exclusive=True, group=group)
        self.dialog.children = [a]
        dialogs.choose(self.owner, self.request(a, checked=False))
        self.assertFalse(a.checked)
    def test_stale_revision_stops_before_click(self):
        a = Button('a', checkable=True)
        self.dialog.children = [a]
        params = self.request(a, checked=True)
        a._text = 'changed option'
        with self.assertRaisesRegex(ValueError, 'Installer changed'):
            dialogs.choose(self.owner, params)
        self.assertEqual(a.clicks, 0)
    def test_last_moment_revision_change_stops_before_click(self):
        a = Button('a', checkable=True)
        self.dialog.children = [a]
        params = self.request(a, checked=True)
        real, calls = dialogs.state, []
        def changed(owner):
            calls.append(True)
            if len(calls) == 2: a._text = 'changed during validation'
            return real(owner)
        with patch.object(dialogs, 'state', changed), self.assertRaisesRegex(ValueError, 'Installer changed'):
            dialogs.choose(self.owner, params)
        self.assertEqual(a.clicks, 0)
    def test_active_job_replacement_invalidates_revision(self):
        a = Button('a', checkable=True)
        self.dialog.children = [a]
        params = self.request(a, checked=True)
        self.owner.active_job = dict(self.owner.active_job, job_id='newinstall')
        with self.assertRaisesRegex(ValueError, 'Installer changed'):
            dialogs.choose(self.owner, params)
        self.assertEqual(a.clicks, 0)
    def test_group_membership_revision_change_stops_click(self):
        a = Button('a', checkable=True, group=Group())
        self.dialog.children = [a]
        params = self.request(a, checked=True)
        a._group = Group()
        with self.assertRaisesRegex(ValueError, 'Installer changed'):
            dialogs.choose(self.owner, params)
        self.assertEqual(a.clicks, 0)
    def test_profile_permissions_and_running_tools_stop_click(self):
        for field, value in [('profile', 'Original Profile'), ('allow', False), ('running', {'SkyrimSE.exe'})]:
            self.setUp()
            a = Button('a', checkable=True)
            self.dialog.children = [a]
            params = self.request(a, checked=True)
            if field == 'running': self.owner.ops.running = value
            else: setattr(self, field, value)
            with self.subTest(field=field), self.assertRaises(ValueError):
                dialogs.choose(self.owner, params)
            self.assertEqual(a.clicks, 0)
    def test_refused_native_selection_is_not_reported_success(self):
        a = Button('a', checkable=True)
        self.dialog.children = [a]
        a.on_click = lambda: setattr(a, 'checked', False)
        with self.assertRaisesRegex(RuntimeError, 'did not retain'):
            dialogs.choose(self.owner, self.request(a, checked=True))
    def test_push_button_executes_guarded_click_before_success(self):
        a = Button('next')
        self.dialog.children = [a]
        result = dialogs.choose(self.owner, self.request(a))
        self.assertEqual(a.clicks, 1)
        self.assertEqual(result['action'], 'clicked')
    def test_deleted_or_disabled_control_is_refused(self):
        for field in ('deleted', 'enabled'):
            a = Button('a', checkable=True)
            self.dialog.children = [a]
            params = self.request(a, checked=True)
            setattr(a, field, field == 'deleted')
            with self.subTest(field=field), self.assertRaises(ValueError):
                dialogs.choose(self.owner, params)
            self.assertEqual(a.clicks, 0)
    def test_quick_install_name_remains_fixed(self):
        self.dialog._name = 'SimpleInstallDialog'
        combo = Combo('nameCombo', ('fixture AI MADE', 'unexpected'))
        button = Button('okBtn')
        self.dialog.children = [combo, button]
        dialogs.accept_quick_install(self.owner)
        self.assertEqual(button.clicks, 1)
        combo.index = 1
        dialogs.accept_quick_install(self.owner)
        self.assertEqual(button.clicks, 1)
        with self.assertRaisesRegex(ValueError, 'target name is fixed'):
            dialogs.choose(self.owner, self.request(combo, index=0))
    def test_regular_combo_validates_and_verifies_index(self):
        combo = Combo()
        self.dialog.children = [combo]
        for bad in (True, -1, 2, None):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'Invalid choice'):
                dialogs.choose(self.owner, self.request(combo, index=bad))
        self.assertTrue(dialogs.choose(self.owner, self.request(combo, index=1))['selection_applied'])
        self.assertEqual(combo.index, 1)
    def test_inapplicable_selection_fields_refused_before_action(self):
        checkbox = Button('a', checkable=True)
        combo = Combo()
        self.dialog.children = [checkbox, combo]
        with self.assertRaisesRegex(ValueError, 'do not accept'):
            dialogs.choose(self.owner, self.request(checkbox, checked=True, index=1))
        with self.assertRaisesRegex(ValueError, 'do not accept'):
            dialogs.choose(self.owner, self.request(combo, index=1, checked=True))
        self.assertEqual(checkbox.clicks, 0)
        self.assertEqual(combo.index, 0)

    def test_unrelated_modal_is_not_controllable(self):
        self.dialog._name, self.dialog.title = 'Settings', 'Settings'
        a = Button('delete')
        self.dialog.children = [a]
        self.assertFalse(dialogs.state(self.owner)['installer_open'])


if __name__ == '__main__':
    unittest.main()
