import sys
import unittest
from enum import IntEnum, IntFlag
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from mo2_codex_bridge.native_rename_model import rename_in_mod_model


class Role(IntEnum):
    EditRole = 2


class Flag(IntFlag):
    ItemIsEditable = 2
    ItemIsEnabled = 32


class Widget:
    def __init__(self, name, class_name):
        self.name = name
        self.class_name = class_name
        self.visible = True
        self.enabled = True

    def objectName(self):
        return self.name

    def metaObject(self):
        return SimpleNamespace(className=lambda: self.class_name)

    def isVisible(self):
        return self.visible

    def isEnabled(self):
        return self.enabled


class TreeView(Widget):
    def __init__(self, model):
        super().__init__('modList', 'ModListView')
        self.source = model

    def model(self):
        return self.source


class MainWindow(Widget):
    def __init__(self, views):
        super().__init__('MainWindow', 'MainWindow')
        self.views = views

    def findChildren(self, kind, name):
        return [view for view in self.views if isinstance(view, kind) and view.name == name]


class App:
    def __init__(self, windows):
        self.windows = windows
        self.modal = None
        self.gui_thread = object()

    def thread(self):
        return self.gui_thread

    def activeModalWidget(self):
        return self.modal

    def topLevelWidgets(self):
        return self.windows


class Model(Widget):
    def __init__(self, names):
        super().__init__('', 'ModList')
        self.names = names
        self.writes = []
        self.result = True
        self.flag_value = Flag.ItemIsEditable | Flag.ItemIsEnabled

    def rowCount(self):
        return len(self.names)

    def index(self, row, column):
        return SimpleNamespace(isValid=lambda: True, model=lambda: self,
                               row=lambda: row, column=lambda: column)

    def data(self, index, role):
        if role != Role.EditRole:
            raise AssertionError('Unexpected data role')
        return self.names[index.row()]

    def flags(self, index):
        return self.flag_value

    def setData(self, index, name, role):
        self.writes.append((index.row(), index.column(), name, role))
        return self.result


class Proxy:
    def __init__(self, source):
        self.source = source

    def sourceModel(self):
        return self.source


class NativeRenameModelTests(unittest.TestCase):
    old_name = 'Original Mod'
    new_name = 'Renamed Mod'

    def setUp(self):
        self.model = Model(['Unrelated', self.old_name, 'Yet Another'])
        self.view = TreeView(Proxy(Proxy(self.model)))
        self.window = MainWindow([self.view])
        self.app = App([self.window])
        self.current_thread = self.app.thread()
        self.version = '2.5.2'
        self.native_names = self.model.names[:]
        self.native_mod = SimpleNamespace(name=lambda: self.old_name, isForeign=lambda: False,
                                          isBackup=lambda: False, isOverwrite=lambda: False,
                                          isSeparator=lambda: False)
        self.native_list = SimpleNamespace(allMods=lambda: self.native_names,
                                           getMod=lambda name: self.native_mod)
        self.organizer = SimpleNamespace(
            appVersion=lambda: SimpleNamespace(displayString=lambda segments: self.version),
            modList=lambda: self.native_list,
        )
        core = ModuleType('PyQt6.QtCore')
        core.QAbstractItemModel = Model
        core.QAbstractProxyModel = Proxy
        core.QThread = SimpleNamespace(currentThread=lambda: self.current_thread)
        core.Qt = SimpleNamespace(ItemDataRole=Role, ItemFlag=Flag)
        widgets = ModuleType('PyQt6.QtWidgets')
        widgets.QApplication = App
        widgets.QMainWindow = MainWindow
        widgets.QTreeView = TreeView
        with_instance = patch.object(App, 'instance', lambda: self.app, create=True)
        with_modules = patch.dict(sys.modules, {
            'PyQt6': ModuleType('PyQt6'), 'PyQt6.QtCore': core, 'PyQt6.QtWidgets': widgets,
        })
        self.enterContext(with_instance)
        self.enterContext(with_modules)

    def rename(self, old_name=None, new_name=None):
        return rename_in_mod_model(self.organizer,
                                   self.old_name if old_name is None else old_name,
                                   self.new_name if new_name is None else new_name)

    def test_exact_source_column_one_native_edit(self):
        self.assertTrue(self.rename())
        self.assertEqual(self.model.writes, [(1, 0, self.new_name, Role.EditRole)])

    def test_false_native_result_is_reported_without_retry(self):
        self.model.result = False
        self.assertFalse(self.rename())
        self.assertEqual(len(self.model.writes), 1)

    def test_unsupported_version_does_not_open_ui(self):
        self.version = '2.5.2beta'
        with self.assertRaisesRegex(ValueError, '2.5.2 only'):
            self.rename()
        self.assertEqual(self.model.writes, [])

    def test_case_only_and_normalized_names_are_rejected(self):
        for name in ('original mod', 'two  spaces', 'wide\u2003space', 'Overwrite',
                     'Name_backup2', 'Name ends in backup', 'Name_separator', 'COM3', 'bad/name'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.rename(new_name=name)
        self.assertEqual(self.model.writes, [])

    def test_destination_collision_is_rejected_case_insensitively(self):
        with self.assertRaisesRegex(ValueError, 'already in use'):
            self.rename(new_name='uNrElAtEd')
        self.assertEqual(self.model.writes, [])

    def test_source_must_have_exact_case(self):
        with self.assertRaisesRegex(RuntimeError, 'exact source mod'):
            self.rename(old_name=self.old_name.lower())
        self.assertEqual(self.model.writes, [])

    def test_unreliable_rows_are_rejected(self):
        for names in ([self.old_name, 'Unrelated', 'Yet Another'],
                      [self.old_name], ['Other', self.old_name.swapcase(), self.old_name]):
            with self.subTest(names=names):
                self.native_names = names
                with self.assertRaises(RuntimeError):
                    self.rename()
        self.assertEqual(self.model.writes, [])

    def test_foreign_backup_overwrite_and_separator_are_ineligible(self):
        for method in ('isForeign', 'isBackup', 'isOverwrite', 'isSeparator'):
            with self.subTest(method=method), patch.object(self.native_mod, method, lambda: True):
                with self.assertRaisesRegex(ValueError, 'regular MO2-managed mod'):
                    self.rename()
        self.assertEqual(self.model.writes, [])

    def test_noneditable_and_disabled_cells_are_rejected(self):
        for flags in (Flag(0), Flag.ItemIsEnabled, Flag.ItemIsEditable):
            with self.subTest(flags=flags):
                self.model.flag_value = flags
                with self.assertRaisesRegex(RuntimeError, 'expected editable item'):
                    self.rename()
        self.assertEqual(self.model.writes, [])

    def test_proxy_depth_and_wrong_source_type_are_rejected(self):
        for source in (Proxy(self.view.source), None, object()):
            with self.subTest(source=source), patch.object(self.view, 'source', source):
                with self.assertRaisesRegex(RuntimeError, 'source model'):
                    self.rename()
        self.assertEqual(self.model.writes, [])

    def test_wrong_thread_and_active_modal_dialog_are_rejected(self):
        with patch.object(self, 'current_thread', object()):
            with self.assertRaisesRegex(RuntimeError, 'GUI thread'):
                self.rename()
        self.app.modal = object()
        with self.assertRaisesRegex(RuntimeError, 'active MO2 dialog'):
            self.rename()
        self.assertEqual(self.model.writes, [])

    def test_ambiguous_main_window_and_view_are_rejected(self):
        self.app.windows.append(MainWindow([]))
        with self.assertRaisesRegex(RuntimeError, 'unique available MO2 main window'):
            self.rename()
        self.app.windows.pop()
        self.window.views.append(TreeView(self.model))
        with self.assertRaisesRegex(RuntimeError, 'unique available MO2 mod view'):
            self.rename()
        self.assertEqual(self.model.writes, [])
