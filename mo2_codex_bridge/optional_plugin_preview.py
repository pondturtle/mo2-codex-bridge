"""Read the current MO2 evidence for one plugin before a possible toggle."""
from pathlib import Path

from .common import safe_name
from .profile_usage import MAX_DEPENDENTS, MAX_PLUGINS, _bounded, _plain_name

_PLUGIN_SUFFIXES = {".esp", ".esm", ".esl"}
_MAX_MASTERS = 512
_MAX_ORIGINS = 128


def _plugin_name(name):
    return _plain_name(name) and Path(name).suffix.casefold() in _PLUGIN_SUFFIXES


def _names(values, limit, label):
    names = _bounded(values, limit, label)
    if not all(_plugin_name(name) for name in names) or len({name.casefold() for name in names}) != len(names):
        raise ValueError(label + " contains invalid or ambiguous plugin names")
    return names


def _provider(organizer, plugins, name):
    facts = {"status": "unknown", "plugin_origin": None, "reported_winner": None,
             "resolved_path": None, "same_name_alternatives": None}
    try:
        origin = plugins.origin(name)
        if _plain_name(origin):
            facts["plugin_origin"] = origin
        resolved = organizer.resolvePath(name)
        if (not isinstance(resolved, str) or not Path(resolved).is_absolute()
                or Path(resolved).name.casefold() != name.casefold()):
            raise ValueError("MO2 did not resolve an absolute winning plugin path")
        facts["resolved_path"] = resolved
        origins = _bounded(organizer.getFileOrigins(name), _MAX_ORIGINS, "Indexed file origins")
        if not origins or not all(_plain_name(row) for row in origins):
            raise ValueError("MO2 did not report valid indexed file origins")
        facts["reported_winner"] = origins[0]
        if facts["plugin_origin"] is None or origins[0].casefold() != facts["plugin_origin"].casefold():
            raise ValueError("The plugin list origin and indexed winner do not agree")
        if len({row.casefold() for row in origins}) != len(origins):
            raise ValueError("The indexed origin list is ambiguous")
        facts.update(status="observed", same_name_alternatives=origins[1:])
    except Exception:
        facts["reason"] = "MO2 did not provide consistent current provider evidence"
    return facts


def _activation(plugins, plugin_state, names):
    result = {}
    for name in names:
        try:
            state = plugins.state(name)
            result[name.casefold()] = (True if state == plugin_state.ACTIVE else
                                       False if state == plugin_state.INACTIVE else None)
        except Exception:
            result[name.casefold()] = None
    return result


def _dependency_evidence(plugins, name, names, states):
    key = name.casefold()
    try:
        masters = _names(plugins.masters(name), _MAX_MASTERS, "Required masters")
        required = [{"name": master, "state": (
            "absent" if master.casefold() not in states else
            {True: "active", False: "inactive", None: "unknown"}[states[master.casefold()]])}
                    for master in masters]
    except Exception:
        required = None
    dependents = []
    unknown_count = 0
    for other in names:
        state = states[other.casefold()]
        if other == name or state is False:
            continue
        try:
            masters = _names(plugins.masters(other), _MAX_MASTERS, "Plugin masters")
            if key in {master.casefold() for master in masters}:
                if state is True:
                    dependents.append(other)
                else:
                    unknown_count += 1
        except Exception:
            unknown_count += 1
    return {"masters": required,
            "active_dependents": sorted(dependents, key=str.casefold)[:MAX_DEPENDENTS],
            "active_dependent_count": len(dependents),
            "active_dependents_truncated": len(dependents) > MAX_DEPENDENTS,
            "active_dependent_evidence_complete": unknown_count == 0,
            "unresolved_dependent_checks": unknown_count}


def _game_membership(game, method, name):
    try:
        rows = _names(getattr(game, method)(), MAX_PLUGINS, "Managed game plugins")
        return name.casefold() in {row.casefold() for row in rows}
    except Exception:
        return None


def preview_optional_plugin(ops, params):
    if set(params) != {"plugin"}:
        raise ValueError("Supply only an exact visible plugin name")
    name = safe_name(params["plugin"])
    if not _plugin_name(name):
        raise ValueError("Supply an exact visible ESP, ESM, or ESL name")

    organizer = ops.o
    profile = (organizer.profileName(), organizer.profilePath())
    if not profile[0] or not Path(profile[1]).is_absolute():
        raise ValueError("Current MO2 profile identity is unavailable")
    plugins = organizer.pluginList()
    names = _names(plugins.pluginNames(), MAX_PLUGINS, "Visible plugin list")
    if name not in names:
        raise ValueError("Plugin is not present under that exact name in the current profile")
    states = _activation(plugins, ops.m.PluginState, names)
    dependencies = _dependency_evidence(plugins, name, names, states)
    provider = _provider(organizer, plugins, name)

    game = organizer.managedGame()
    primary = _game_membership(game, "primaryPlugins", name)
    forced = _game_membership(game, "enabledPlugins", name)
    active = states[name.casefold()]
    blockers = []
    if primary is True:
        blockers.append("primary_game_plugin")
    if forced is True:
        blockers.append("force_enabled_by_game")
    if dependencies["active_dependent_count"]:
        blockers.append("active_dependents")
    if blockers:
        assessment = "blocked"
    elif active is False:
        assessment = "already_inactive"
    elif (active is None or primary is None or forced is None
          or not dependencies["active_dependent_evidence_complete"]):
        assessment = "unknown"
    else:
        assessment = "no_known_plugin_list_blocker"

    if (organizer.profileName(), organizer.profilePath()) != profile:
        raise ValueError("The active profile changed during plugin observation")

    return {"profile": profile[0], "plugin": name, "active": active, "primary_game_plugin": primary,
            "force_enabled_by_game": forced, "provider": provider, **dependencies,
            "deactivation": {"status": assessment, "blockers": blockers},
            "scope": "Current profile plugin-list and indexed MO2 file evidence. Optionality, "
                     "asset and save effects, and other profiles require separate evidence."}

