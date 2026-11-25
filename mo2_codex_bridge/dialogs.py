"""Controls scoped exclusively to the native installer opened by a bridge job."""
import json
from PyQt6 import sip

from PyQt6.QtWidgets import (QAbstractButton, QApplication, QComboBox, QDialog,
                             QLabel, QLineEdit)

from .common import digest


def installer_widget(owner):
    active = getattr(owner, 'active_job', None)
    if not owner.busy or not active or active['action'] not in {'install_archive', 'update_archive'}:
        return None
    dialog = QApplication.activeModalWidget()
    # A tool may not interact with unrelated MO2 settings, authentication or file pickers.
    if not isinstance(dialog, QDialog):
        return None
    identity = (dialog.objectName() + ' ' + dialog.windowTitle()).lower()
    if not any(term in identity for term in ('simpleinstalldialog', 'fomodinstallerdialog', 'fomoddialog', 'manualinstalldialog', 'mod installation')):
        return None
    return dialog


def state(owner):
    dialog = installer_widget(owner)
    if dialog is None:
        return {'installer_open': False, 'message': 'No supported installer dialog is active'}
    controls = []
    owner.installer_dialog = dialog
    owner.installer_controls = {}
    widgets = dialog.findChildren(QAbstractButton) + dialog.findChildren(QComboBox) + dialog.findChildren(QLineEdit)
    for widget in widgets:
        if not widget.isVisible():
            continue
        item = {'id': str(sip.unwrapinstance(widget)), 'name': widget.objectName(), 'enabled': widget.isEnabled()}
        if isinstance(widget, QAbstractButton):
            item.update(kind='button', text=widget.text(), checkable=widget.isCheckable(), checked=widget.isChecked())
            if widget.isCheckable():
                group = widget.group()
                item.update(exclusive=_exclusive(widget), group_id=str(sip.unwrapinstance(group)) if group is not None else None)
        elif isinstance(widget, QComboBox):
            item.update(kind='choice', value=widget.currentText(), index=widget.currentIndex(),
                        choices=[widget.itemText(i) for i in range(widget.count())])
        elif isinstance(widget, QLineEdit):
            # Installer names are visible; password or hidden fields never appear here.
            if widget.echoMode() != QLineEdit.EchoMode.Normal:
                continue
            item.update(kind='text', value=widget.text(), editable=not widget.isReadOnly())
        controls.append(item)
        owner.installer_controls[item['id']] = widget
    result = {'installer_open': True, 'dialog_id': str(sip.unwrapinstance(dialog)), 'title': dialog.windowTitle(),
              'labels': [w.text() for w in dialog.findChildren(QLabel) if w.isVisible()],
              'controls': controls, 'install_job_id': owner.active_job.get('job_id')}
    result['dialog_revision'] = digest(json.dumps(result, sort_keys=True).encode())
    return result


def _authorize(owner, params):
    current = state(owner)
    if (not current['installer_open'] or params.get('dialog_id') != current['dialog_id']
            or params.get('dialog_revision') != current['dialog_revision']):
        raise ValueError('Installer changed. Read installer_state again before selecting an option.')
    active = getattr(owner, 'active_job', None)
    if not active or owner.organizer.profileName() != active['params'].get('expected_profile'):
        raise ValueError('Profile changed while the installer was open')
    if owner.organizer.pluginSetting(owner.name(), 'allow_changes') is False:
        raise ValueError('Changes are disabled in bridge settings')
    if getattr(getattr(owner, 'ops', None), 'running', None):
        raise ValueError('Close the game and launched tools before selecting installer options')
    return current


def _exclusive(widget):
    group = widget.group()
    return group.exclusive() if group is not None else widget.autoExclusive()


def choose(owner, params):
    current = _authorize(owner, params)
    widget = owner.installer_controls.get(params.get('control_id'))
    if widget is None or sip.isdeleted(widget) or not widget.isEnabled() or not widget.isVisible():
        raise ValueError('Installer control is unavailable')
    if isinstance(widget, QAbstractButton):
        desired = params.get('checked')
        if widget.isCheckable():
            if 'index' in params:
                raise ValueError('Checkable buttons do not accept a choice index')
            if type(desired) is not bool:
                raise ValueError('A boolean checked value is required')
            if not desired and widget.isChecked() and _exclusive(widget):
                raise ValueError('An exclusive choice cannot be unchecked directly; select another option in its group')
        elif 'checked' in params or 'index' in params:
            raise ValueError('Push buttons do not accept checked or index parameters')
    elif isinstance(widget, QComboBox):
        if 'checked' in params:
            raise ValueError('Combo choices do not accept a checked value')
        dialog = installer_widget(owner)
        if dialog.objectName() == 'SimpleInstallDialog':
            raise ValueError('The archive target name is fixed by the install request')
        index = params.get('index')
        if type(index) is not int or not 0 <= index < widget.count():
            raise ValueError('Invalid choice index')
    elif isinstance(widget, QLineEdit):
        raise ValueError('Text fields are inspect-only; change archive names in the install request')
    else:
        raise ValueError('Unsupported installer control')
    # Re-read the full dialog immediately before the user action. In particular,
    # never queue a later click against a dialog/revision that may have changed.
    _authorize(owner, params)
    if owner.installer_controls.get(params.get('control_id')) is not widget or sip.isdeleted(widget):
        raise ValueError('Installer control changed before selection')
    if isinstance(widget, QAbstractButton):
        if widget.isCheckable():
            changed = widget.isChecked() != desired
            if changed:
                widget.click()  # Emits clicked and respects native/exclusive choice handling.
            if sip.isdeleted(widget) or widget.isChecked() != desired:
                raise RuntimeError('Native installer did not retain the requested selection; inspect installer_state')
            after = state(owner)
            return {'selection_applied': True, 'changed': changed, 'control_id': params['control_id'],
                    'installer_state': after}
        widget.click()
        return {'selection_applied': True, 'control_id': params['control_id'], 'action': 'clicked'}
    widget.setCurrentIndex(index)
    if sip.isdeleted(widget) or widget.currentIndex() != index:
        raise RuntimeError('Native installer did not retain the requested choice; inspect installer_state')
    return {'selection_applied': True, 'control_id': params['control_id'], 'installer_state': state(owner)}


def accept_quick_install(owner):
    current = state(owner)
    if not current['installer_open']:
        return
    dialog = installer_widget(owner)
    if dialog is None or dialog.objectName() != 'SimpleInstallDialog':
        return
    combo = dialog.findChild(QComboBox, 'nameCombo')
    button = dialog.findChild(QAbstractButton, 'okBtn')
    expected = owner.active_job['params'].get('name')
    if (combo is not None and button is not None and combo.currentText() == expected
            and button.isVisible() and button.isEnabled()):
        choose(owner, {'dialog_id': current['dialog_id'], 'dialog_revision': current['dialog_revision'],
                       'control_id': str(sip.unwrapinstance(button))})
