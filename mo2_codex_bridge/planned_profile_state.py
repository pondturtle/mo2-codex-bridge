"""Bounded saved-profile observations for planned mod changes."""
from pathlib import Path

from .common import atomic_write, digest
from .persistence import plugin_file_lines, saved_plugin_rows_match
from .profile_usage import MAX_TOTAL_BYTES, _read_saved, _safe_stat

PROFILE_FILES = ('modlist.txt', 'plugins.txt', 'loadorder.txt')


def read_profile_files(folder, names=PROFILE_FILES, budget=None):
    folder = Path(folder)
    if not folder.is_absolute():
        raise ValueError('An absolute profile path is required')
    _safe_stat(folder, directory=True)
    if budget is None:
        budget = [MAX_TOTAL_BYTES]
    try:
        return {name: _read_saved(folder / name, budget) for name in names}
    except OSError as error:
        raise ValueError('Could not read the expected saved profile files: ' + str(error)) from error


def profile_file_hashes(files):
    return {name: digest(data) for name, data in files.items()}


def keep_profile_files(backup, files):
    for name, data in files.items():
        atomic_write(backup / name, data)
        if _read_saved(backup / name, [MAX_TOTAL_BYTES]) != data:
            raise RuntimeError('Recovery copy could not be verified: ' + name)


def modlist_rows(data):
    rows, seen = [], set()
    for line in data.decode('utf-8-sig').split('\n'):
        line = line.strip(' \t\r\v\f')
        if not line or line.startswith('#'):
            continue
        if line[0] not in '+-*' or not line[1:] or line[1:] != line[1:].strip():
            raise ValueError('Unsupported saved modlist entry')
        name = line[1:]
        key = name.casefold()
        if key in seen or any(ord(char) < 32 for char in name):
            raise ValueError('Ambiguous saved modlist entry')
        seen.add(key)
        rows.append((line[0], name))
    return rows


def expected_modlist(mods):
    # The native writer saves highest priority first and uses the translated
    # display name for foreign rows only.
    return [('*' if mod['foreign'] else '+' if mod['active'] else '-',
             mod['display_name'] if mod['foreign'] else mod['name'])
            for mod in reversed(mods)]



def check_saved_plugins(organizer, files, plugins):
    game = organizer.managedGame()
    if game.gameShortName().casefold() != 'skyrimse':
        raise ValueError('Planned mod changes support verified Skyrim SE profile lists only')
    primary = {name.casefold() for name in game.primaryPlugins()}
    expected = [row['name'].casefold() for row in plugins]
    states = {row['name'].casefold(): row['active'] for row in plugins}
    if len(states) != len(expected):
        raise ValueError('Live plugin names are ambiguous')
    matches, reason = saved_plugin_rows_match(
        plugin_file_lines(files['loadorder.txt']), plugin_file_lines(files['plugins.txt']),
        expected, states, primary,
    )
    if not matches:
        raise ValueError('Saved plugin state differs from the current MO2 profile: ' + reason)
