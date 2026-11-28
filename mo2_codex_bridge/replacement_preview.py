"""Read-only plugin-impact forecast for an already staged replacement."""
import json
from pathlib import Path

from .mod_placement import checked_mod_folder, ordered_mods
from .optional_plugin_preview import _provider
from .planned_profile_state import check_saved_plugins, expected_modlist, modlist_rows, read_profile_files
from .profile_usage import MAX_MODS, MAX_PLUGINS, _lexical, _plain_name
from .prospective_plugins import comparison_mods, facts, plan_activation, read_headers

_MAX_AFFECTED = 256
_MAX_REPORT_BYTES = 512 * 1024


def _plugin_view(row):
    return {"name": row["name"], "origin": row["origin"], "masters": list(row["masters"]),
            "master": row["master"], "light": row["light"]}


def _after_view(row, projection, replacement, header):
    if projection is None:
        return None
    source = header if projection["origin"] == replacement.casefold() else row
    return {**_plugin_view(source), "active": projection["active"]}


def _read_evidence(ops, name):
    try:
        return read_headers(ops, name, preview=True)
    except OSError as error:
        raise ValueError("Replacement plugin headers could not be read safely") from error


def preview_mod_replacement(ops, params):
    if set(params) != {"mod", "replacement"} or not all(_plain_name(value) for value in params.values()):
        raise ValueError("Specify exact mod and replacement names")
    previous, replacement = params["mod"], params["replacement"]
    if previous.casefold() == replacement.casefold():
        raise ValueError("The active mod and staged replacement must be different")
    before = ops.snapshot()
    if len(before["mods"]) > MAX_MODS or len(before["plugins"]) > MAX_PLUGINS:
        raise ValueError("MO2's current inventory exceeds the preview limits")
    old_folder = checked_mod_folder(ops, previous)
    new_folder = checked_mod_folder(ops, replacement)
    if old_folder[1] == new_folder[1]:
        raise ValueError("The active mod and replacement folders are not distinct")
    native_mods = ordered_mods(before)
    by_name = {row["name"]: row for row in native_mods}
    if previous not in by_name or replacement not in by_name:
        raise ValueError("Both exact mod names must be visible in the current profile")
    if by_name[previous]["active"] is not True or by_name[replacement]["active"] is not False:
        raise ValueError("The previous mod must be active and the staged replacement disabled")

    folder = Path(before["profile_path"])
    saved = read_profile_files(folder)
    mods = comparison_mods(ops, native_mods, saved_modlist=saved["modlist.txt"])
    if modlist_rows(saved["modlist.txt"]) != expected_modlist(mods):
        raise ValueError("The saved modlist differs from the current profile")
    check_saved_plugins(ops.o, saved, before["plugins"])
    snapshot = {**before, "mods": mods}
    headers = _read_evidence(ops, replacement)
    original = {row["name"].casefold(): row for row in before["plugins"]}
    old_key = previous.casefold()
    current_facts = {key: facts(row, mods, ops.o.resolvePath) for key, row in original.items()}
    if any(row["origin"] == replacement.casefold() for row in current_facts.values()):
        raise ValueError("MO2 reports a disabled replacement as a current plugin provider")
    old_plugins = {key for key, row in current_facts.items() if row["origin"] == old_key}
    affected = sorted(set(headers) | old_plugins)
    if len(affected) > _MAX_AFFECTED:
        raise ValueError("The number of affected plugins exceeds the preview limit")

    priority = [row["name"] for row in native_mods if row["name"] != replacement].index(previous)
    plans = {}
    blockers = []
    plugin_list = ops.o.pluginList()
    providers = {key: _provider(ops.o, plugin_list, original[key]["name"])
                 for key in affected if key in original}
    for key, provider in providers.items():
        path = current_facts[key]["origin_path"]
        if current_facts[key]["origin"] == "overwrite":
            path = ops.o.overwritePath()
        name = original[key]["name"]
        if (provider["status"] != "observed"
                or (path is not None and _lexical(provider["resolved_path"]) != _lexical(Path(path) / name))):
            blockers.append({"stage": "current_provider", "plugin": name,
                             "reason": "MO2 did not establish the current winning file"})
    if not blockers:
        for stage, disabling in (("final", previous), ("both_enabled", None)):
            try:
                plans[stage] = plan_activation(ops, snapshot, snapshot, replacement, headers,
                                               disabling=disabling, target_priority=priority)
            except ValueError as error:
                blockers.append({"stage": stage, "reason": str(error)})
    forecast = plans["final"]["expected"] if not blockers else {}

    plugins = []
    for key in affected:
        old, staged = original.get(key), headers.get(key)
        after = _after_view(old, forecast.get(key), replacement, staged)
        old_state = None if old is None else {**_plugin_view(old), "active": old["active"]}
        staged_view = None if staged is None else _plugin_view(staged)
        change = {"before": old_state, "staged": staged_view, "after": after}
        if after is not None:
            previous_masters = {} if old is None else {n.casefold(): n for n in old["masters"]}
            next_masters = {n.casefold(): n for n in after["masters"]}
            change.update(masters_added=[n for k, n in next_masters.items() if k not in previous_masters],
                          masters_removed=[n for k, n in previous_masters.items() if k not in next_masters])
        plugins.append(change)

    if (checked_mod_folder(ops, previous) != old_folder or checked_mod_folder(ops, replacement) != new_folder
            or _read_evidence(ops, replacement) != headers or read_profile_files(folder) != saved
            or any(_provider(ops.o, plugin_list, original[key]["name"]) != provider
                   for key, provider in providers.items())
            or ops.snapshot()["revision"] != before["revision"]):
        raise ValueError("MO2 state or replacement evidence changed during the preview")

    report = {"profile": before["profile"], "revision": before["revision"], "mod": previous,
            "replacement": replacement, "placement": "immediately_before_previous",
            "projected_priority": priority, "status": "blocked" if blockers else "modeled",
            "blockers": blockers, "plugins": plugins,
            "previous_plugins_missing_from_replacement": [original[key]["name"] for key in affected
                                                           if key in old_plugins and key not in headers],
            "scope": "Current-profile plugin providers, headers and enabled states. New plugin enabled "
                     "defaults, asset winners, records and effects on saves require separate evidence."}
    if len(json.dumps(report, ensure_ascii=True).encode("ascii")) > _MAX_REPORT_BYTES:
        raise ValueError("The replacement preview exceeds the report size limit")
    return report

