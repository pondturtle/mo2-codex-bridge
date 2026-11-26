"""Run MO2 2.5.2's native mod-name edit through its Qt source model."""

import re

from .common import safe_name


def rename_in_mod_model(organizer, old_name, new_name):
    """Attempt one native edit. The caller must verify disk and profile state."""
    if organizer.appVersion().displayString(3) != '2.5.2':
        raise ValueError('Native mod renaming is supported on MO2 2.5.2 only')

    old_name, new_name = safe_name(old_name), safe_name(new_name)
    new_key = new_name.casefold()
    if new_key == old_name.casefold():
        raise ValueError('Use a distinct mod name. Case-only renames are unavailable.')
    if (' '.join(new_name.split()) != new_name or new_key == 'overwrite'
            or new_key.endswith('_separator') or re.search(r'backup[0-9]*\Z', new_key)):
        raise ValueError('The new name would be normalized or reserved by MO2')

    from PyQt6.QtCore import QAbstractItemModel, QAbstractProxyModel, QThread, Qt
    from PyQt6.QtWidgets import QApplication, QMainWindow, QTreeView

    app = QApplication.instance()
    if not isinstance(app, QApplication) or QThread.currentThread() != app.thread():
        raise RuntimeError('Native mod renaming requires the MO2 GUI thread')
    if app.activeModalWidget() is not None:
        raise RuntimeError('Close the active MO2 dialog before renaming a mod')

    windows = [widget for widget in app.topLevelWidgets()
               if isinstance(widget, QMainWindow) and widget.objectName() == 'MainWindow'
               and widget.metaObject().className() == 'MainWindow']
    if len(windows) != 1 or not windows[0].isVisible() or not windows[0].isEnabled():
        raise RuntimeError('A unique available MO2 main window is required')
    views = windows[0].findChildren(QTreeView, 'modList')
    if (len(views) != 1 or views[0].metaObject().className() != 'ModListView'
            or not views[0].isEnabled()):
        raise RuntimeError('A unique available MO2 mod view is required')
    model = views[0].model()
    for _ in range(2):
        if not isinstance(model, QAbstractProxyModel):
            break
        model = model.sourceModel()
    if (not isinstance(model, QAbstractItemModel)
            or model.metaObject().className() != 'ModList'):
        raise RuntimeError('The expected MO2 mod source model is unavailable')

    mods = organizer.modList()
    names = list(mods.allMods())
    matching = [(row, name) for row, name in enumerate(names)
                if isinstance(name, str) and name.casefold() == old_name.casefold()]
    if len(matching) != 1 or matching[0][1] != old_name or model.rowCount() != len(names):
        raise RuntimeError('The exact source mod could not be matched to the MO2 model')
    if any(isinstance(name, str) and name.casefold() == new_key for name in names):
        raise ValueError('The new mod name is already in use')
    source = mods.getMod(old_name)
    if (source is None or source.name() != old_name or source.isForeign()
            or source.isBackup() or source.isOverwrite() or source.isSeparator()):
        raise ValueError('Native renaming requires a regular MO2-managed mod')

    # MO2 2.5.2 exposes its own name-edit path via column 0 and Qt's EditRole.
    # The Python renameMod wrapper can look up a missing destination on failure.
    row = matching[0][0]
    index = model.index(row, 0)
    edit = Qt.ItemDataRole.EditRole
    required = Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsEnabled
    if (not index.isValid() or index.model() != model or index.row() != row
            or index.column() != 0 or model.data(index, edit) != old_name
            or model.flags(index) & required != required):
        raise RuntimeError('The MO2 mod name cell is not the expected editable item')
    return model.setData(index, new_name, edit)
