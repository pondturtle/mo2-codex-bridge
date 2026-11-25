"""Confined TES4-header proof before activating newly installed mod contents."""
import hashlib
import heapq
import os
from pathlib import Path

from .common import contained_path, safe_name
from .order_recovery import validate_plugin_state

_SUFFIXES = {'.esp','.esm','.esl'}


def read_headers(ops, name):
    mod = ops.mod(name, writable=True)
    root = contained_path(ops.o.modsPath(), name)
    if root != Path(mod.absolutePath()).resolve() or not root.is_dir():
        raise ValueError('New mod folder identity cannot be proved')
    result = {}
    for path in sorted(root.iterdir(),key=lambda item:item.name.casefold()):
        if path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):
            raise ValueError('Linked new mod contents prevent activation proof')
        if path.suffix.casefold() not in _SUFFIXES:
            continue
        if not path.is_file():
            raise ValueError('Ambiguous plugin entry prevents activation proof')
        before = path.stat()
        with path.open('rb') as stream:
            header = stream.read(24)
            if len(header)!=24 or header[:4]!=b'TES4':
                raise ValueError('New plugin has no valid Skyrim TES4 header: ' + path.name)
            size = int.from_bytes(header[4:8],'little')
            if not 18 <= size <= 16*1024*1024 or before.st_size < 24+size:
                raise ValueError('New plugin header size is invalid: ' + path.name)
            body = stream.read(size)
        if len(body)!=size:
            raise ValueError('New plugin header is incomplete: ' + path.name)
        after = path.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
            raise ValueError('New plugin changed during activation preflight')
        flags = int.from_bytes(header[8:12],'little')
        if int.from_bytes(header[12:16],'little')!=0 or flags&0x40000:
            raise ValueError('New plugin TES4 identity/compression is unsupported: ' + path.name)
        position, extended, hedr, pending_master = 0, None, 0, False
        masters = []
        while position<len(body):
            if position+6>len(body):
                raise ValueError('Truncated TES4 subrecord: ' + path.name)
            signature = body[position:position+4]
            size16 = int.from_bytes(body[position+4:position+6],'little')
            position += 6
            length = extended if extended is not None else size16
            extended = None
            if position+length>len(body):
                raise ValueError('TES4 subrecord overruns its header: ' + path.name)
            value = body[position:position+length]
            position += length
            if signature==b'XXXX':
                if length!=4:
                    raise ValueError('Invalid extended TES4 subrecord size')
                extended = int.from_bytes(value,'little')
                continue
            if pending_master and signature!=b'DATA':
                raise ValueError('Master filename lacks its DATA record: ' + path.name)
            if signature==b'HEDR':
                if length!=12:
                    raise ValueError('Invalid HEDR size: ' + path.name)
                hedr += 1
            elif signature==b'MAST':
                if not value.endswith(b'\x00') or b'\x00' in value[:-1]:
                    raise ValueError('Invalid master filename string: ' + path.name)
                try:
                    master = value[:-1].decode('utf-8')
                except UnicodeError:
                    master = value[:-1].decode('cp1252')
                if not master or any(character in master for character in '/\\:') or Path(master).suffix.casefold() not in _SUFFIXES:
                    raise ValueError('Invalid master filename identity: ' + path.name)
                masters.append(master)
                pending_master = True
            elif signature==b'DATA' and pending_master:
                if length!=8:
                    raise ValueError('Invalid master DATA size: ' + path.name)
                pending_master = False
        if hedr!=1 or pending_master or extended is not None or len({master.casefold() for master in masters})!=len(masters):
            raise ValueError('Incomplete or ambiguous new plugin header: ' + path.name)
        key = path.name.casefold()
        if key in result:
            raise ValueError('Duplicate case-insensitive new plugin identity')
        result[key]={'name':path.name,'origin':name,'masters':masters,
                     'master':bool(flags&1) or path.suffix.casefold()=='.esm',
                     'light':bool(flags&0x200) or path.suffix.casefold()=='.esl',
                     'header_sha256':hashlib.sha256(header+body).hexdigest(),
                     'file_bytes':after.st_size,'mtime_ns':after.st_mtime_ns}
    return result


def comparison_mods(ops, mods, proofs=None):
    """Canonicalize only a proved stale UTF-8/CP1252 API-cache spelling.

    The raw folder must be absent before the operation, while the unique
    canonical child folder and already-saved profile entry both exist. This
    derives comparison metadata; it never renames a folder or rewrites a list.
    """
    result = []
    for item in mods:
        row = dict(item)
        raw_path = Path(item['path'])
        row['path'] = os.path.normcase(os.path.normpath(str(raw_path)))
        try:
            decoded = item['name'].encode('cp1252').decode('utf-8')
        except UnicodeError:
            decoded = item['name']
        if decoded != item['name'] and (raw_path.is_symlink() or (hasattr(raw_path,'is_junction') and raw_path.is_junction())):
            raise ValueError('Linked raw provider entry prevents cached spelling proof')
        if decoded != item['name'] and not os.path.lexists(raw_path):
            safe_name(decoded)
            configured_raw = Path(ops.o.modsPath()) / item['name']
            if os.path.normcase(os.path.normpath(str(raw_path))) != os.path.normcase(os.path.normpath(str(configured_raw))):
                raise ValueError('Cached mod spelling has no configured Mods path proof')
            target = contained_path(ops.o.modsPath(),decoded)
            if not target.is_dir():
                raise ValueError('Cached mod spelling has no canonical folder proof')
            text = (Path(ops.o.profilePath())/'modlist.txt').read_text(encoding='utf-8-sig')
            entries = [line[1:] for line in text.splitlines() if line[:1] in {'+','-','*'}]
            if entries.count(decoded) != 1 or item['name'] in entries:
                raise ValueError('Cached mod spelling lacks a unique saved profile entry')
            if any(other is not item and other['name'].casefold() == decoded.casefold() for other in mods):
                raise ValueError('Cached mod spelling has an ambiguous canonical identity')
            row.update(name=decoded,path=os.path.normcase(os.path.normpath(str(target))))
            if proofs is not None:
                proofs.append({'raw_name':item['name'],'raw_path':item['path'],
                               'canonical_name':decoded,'canonical_path':str(target),
                               'proof':'raw folder absent; canonical direct child exists; saved profile entry unique'})
        result.append(row)
    if len({item['name'].casefold() for item in result}) != len(result):
        raise ValueError('Comparison mod identity is ambiguous')
    return result


def provider_identity(plugin, mods, resolver=None):
    origin = plugin['origin']
    matches = [item for item in mods if item['name'].casefold() == origin.casefold()]
    alias = False
    if not matches:
        try:
            decoded = origin.encode('cp1252').decode('utf-8')
        except (UnicodeError,LookupError):
            decoded = origin
        if decoded != origin:
            matches = [item for item in mods if item['name'] == decoded]
            alias = bool(matches)
    if not matches:
        return {'origin':origin.casefold(),'origin_path':None}
    if len(matches) != 1:
        raise ValueError('Plugin provider identity is ambiguous: '+plugin['name'])
    mod = matches[0]
    root = Path(mod['path']).resolve()
    if alias:
        if not callable(resolver):
            raise ValueError('Cached provider alias requires current VFS path proof')
        winner = Path(resolver(plugin['name'])).resolve()
        if (Path(plugin['name']).name != plugin['name'] or winner != root/plugin['name']
                or not winner.is_file()):
            raise ValueError('Cached provider alias differs from current VFS winner: '+plugin['name'])
    return {'origin':mod['name'].casefold(),'origin_path':str(root).casefold()}


def facts(plugin, mods, resolver=None):
    return {'name':plugin['name'].casefold(),**provider_identity(plugin,mods,resolver),
            'masters':sorted(master.casefold() for master in plugin['masters']),
            'master':plugin['master'],'light':plugin['light']}


def _activation_order(expected, existing_order, primary):
    """Merge new names without changing any original pair's relative order.

    MO2 refresh repairs the enlarged master section by moving individual old
    ordinary plugins. Plan the final order before that native refresh instead
    of accepting its incidental rearrangement of existing plugins.
    """
    original = set(existing_order)
    introduced = set(expected) - original
    boundary = '\x00master-boundary'
    edges = {name:set() for name in expected}
    edges[boundary] = set()
    incoming = {name:0 for name in edges}
    def precedes(first, second):
        if second not in edges[first]:
            edges[first].add(second)
            incoming[second] += 1
    for first, second in zip(existing_order, existing_order[1:]):
        precedes(first, second)
    for name, item in expected.items():
        master_like = item['master'] or name.endswith('.esl')
        precedes(name, boundary) if master_like else precedes(boundary, name)
        for master in item['masters']:
            if master in expected:
                precedes(master, name)
    for name in primary & original:
        for new in introduced:
            precedes(name, new)
    old_rank = {name:index for index,name in enumerate(existing_order)}
    def rank(name):
        if name == boundary:
            return (2, 0, name)
        master_like = expected[name]['master'] or name.endswith('.esl')
        if name in original:
            return (0 if master_like else 4, old_rank[name], name)
        return (1 if master_like else 3, 0, name)
    ready = [rank(name) for name,count in incoming.items() if count == 0]
    heapq.heapify(ready)
    result = []
    seen = 0
    while ready:
        _, _, name = heapq.heappop(ready)
        seen += 1
        if name != boundary:
            result.append(name)
        for next_name in edges[name]:
            incoming[next_name] -= 1
            if incoming[next_name] == 0:
                heapq.heappush(ready,rank(next_name))
    if seen != len(edges):
        raise ValueError('Prospective plugin order cannot preserve existing relative order and master relationships')
    validate_plugin_state([{**expected[name],'priority':index,'active':expected[name]['active'] is not False} for index,name in enumerate(result)],result,
        {name:item['active'] is not False for name,item in expected.items()},
        list(primary),existing_order)
    return result


def plan_activation(ops, before, current, new_name, headers, *, disabling=None, target_priority=None):
    original = {item['name'].casefold():item for item in before['plugins']}
    expected = {key:{**facts(item,before['mods'],ops.o.resolvePath),'active':item['active']} for key,item in original.items()}
    mod_order = [item['name'].casefold() for item in sorted(current['mods'],key=lambda item:item['priority'])]
    if target_priority is not None:
        mod_order.remove(new_name.casefold())
        mod_order.insert(target_priority,new_name.casefold())
    priorities = {name:index for index,name in enumerate(mod_order)}
    new_priority = priorities[new_name.casefold()]
    primary = {name.casefold() for name in ops.o.managedGame().primaryPlugins()}
    disabled = disabling.casefold() if disabling else None
    old_owned = {key for key,item in expected.items() if item['origin']==disabled}
    if old_owned-set(headers):
        raise ValueError('Updated contents omit an existing plugin identity; explicit review required')
    for key, header in headers.items():
        old = original.get(key)
        if old is None or expected[key]['origin']==disabled:
            wins = True
        elif expected[key]['origin'] in priorities:
            wins = new_priority>priorities[expected[key]['origin']]
        elif expected[key]['origin']=='overwrite':
            wins = False  # MO2 Overwrite always outranks a regular mod.
        elif expected[key]['origin']=='data':
            wins = True
        else:
            raise ValueError('Existing plugin provider priority cannot be proved: '+old['name'])
        if not wins:
            continue
        if old is not None and key in primary:
            raise ValueError('New contents would replace a visible primary plugin provider')
        expected[key]={**facts(header,current['mods'],ops.o.resolvePath),'active':old['active'] if old is not None else None}
    # A new plugin's native enabled default is not assumed. Require safe masters
    # even if MO2 elects to enable it; existing named-plugin states must persist.
    for key,item in expected.items():
        if item['active'] is False:
            continue
        for master in item['masters']:
            if master not in expected or expected[master]['active'] is False:
                raise ValueError('Prospective activation has a missing or disabled master: '+item['name']+' -> '+master)
    visiting, complete = set(), set()
    def visit(name):
        if name in visiting:
            raise ValueError('Prospective activation contains a master dependency cycle: '+name)
        if name in complete or expected[name]['active'] is False:
            return
        visiting.add(name)
        for master in expected[name]['masters']:
            visit(master)
        visiting.remove(name)
        complete.add(name)
    for name in expected:
        visit(name)
    existing_order = [item['name'].casefold() for item in sorted(before['plugins'],key=lambda item:item['priority'])]
    order = _activation_order(expected,existing_order,primary)
    return {'expected':expected,'existing_order':existing_order,'order':order,
            'introduced':sorted(set(expected)-set(existing_order)),
            'headers':headers,'new_mod':new_name}


def native_relationship_order(plan, iterator_order, primary, raw_plugins):
    """Pinned MO2 v2.5.2 pluginlist.cpp 301-342,1518-1599 fingerprint.

    pluginNames exposes m_ESPs iteration order, independent of its mutable
    priority fields. Only newly discovered names use that order as a seed;
    all existing priorities come from the immutable preactivation snapshot.
    """
    iterator = [name.casefold() for name in iterator_order]
    expected = plan['expected']
    raw = {item['name'].casefold():item for item in raw_plugins}
    exact = {item['name']:item['name'].casefold() for item in raw_plugins}
    if len(raw) != len(raw_plugins) or set(raw) != set(expected):
        raise RuntimeError('Native raw plugin facts differ from activation inventory')
    if len(iterator) != len(set(iterator)) or set(iterator) != set(expected):
        raise RuntimeError('Native plugin iteration inventory differs from activation proof')
    seed = list(plan.get('native_seed',plan['existing_order']))
    if len(seed) != len(set(seed)) or not set(plan['existing_order']) <= set(seed) <= set(expected):
        raise RuntimeError('Native relationship seed differs from the preflight inventory')
    order = seed + [name for name in iterator if name not in seed]
    primary = {name.casefold() for name in primary}
    def master_like(name):
        return expected[name]['master'] or name.endswith('.esl')
    def move(name, requested):
        target = max(0,min(len(order)-1,requested))
        if not master_like(name):
            while target < len(order)-1 and master_like(order[target]):
                target += 1
        else:
            while target > 0 and not master_like(order[target]):
                target -= 1
            while target < len(order)-1 and order[target] in primary:
                target += 1
        old = order.index(name)
        if target < old:
            for master in raw[name]['masters']:
                if master in exact:
                    target = max(target,order.index(exact[master])+1)
        elif target > old:
            index = old+1
            while index <= target:
                other = order[index]
                if name in expected[other]['masters']:
                    target = index-1
                index += 1
        if not 0 <= target < len(order):
            raise RuntimeError('Unrecognized native relationship boundary; no order setter applied')
        if target != old:
            order.pop(old)
            order.insert(target,name)
    master_count = sum(master_like(name) for name in iterator)
    for name in iterator:
        position = order.index(name)
        if (master_like(name) and position > master_count) or (not master_like(name) and position < master_count):
            move(name,master_count+1)
    for name in iterator:
        position = order.index(name)
        requested = max([position]+[order.index(exact[master]) for master in raw[name]['masters'] if master in exact])
        if requested != position:
            move(name,requested)
    return order


def verify_activation(ops, plan, current, *, before_order_commit=False):
    current_mods = comparison_mods(ops,current['mods'])
    actual = {item['name'].casefold():item for item in current['plugins']}
    if set(actual)!=set(plan['expected']):
        raise RuntimeError('Observed plugin inventory differs from prospective activation proof')
    order = [item['name'] for item in sorted(current['plugins'],key=lambda item:item['priority'])]
    if before_order_commit:
        if not plan['introduced']:
            raise RuntimeError('Order repair requires newly introduced plugin identities')
    elif [name.casefold() for name in order] != plan['order']:
        raise RuntimeError('Plugin order differs from the preflight insertion plan')
    for key,wanted in plan['expected'].items():
        item = actual[key]
        if facts(item,current_mods,ops.o.resolvePath)!={name:value for name,value in wanted.items() if name!='active'}:
            raise RuntimeError('Observed plugin provider/header differs from prospective proof: '+item['name'])
        if wanted['active'] is not None and item['active']!=wanted['active']:
            raise RuntimeError('Existing plugin activation changed during archive operation: '+item['name'])
    desired = [actual[name]['name'] for name in plan['order']]
    validate_plugin_state(current['plugins'],desired,{item['name']:item['active'] for item in current['plugins']},
                          list(ops.o.managedGame().primaryPlugins()),order)
    if read_headers(ops,plan['new_mod'])!=plan['headers']:
        raise RuntimeError('New mod plugin headers changed after activation preflight')
