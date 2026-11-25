"""Lossless text-format detection and deliberate configuration conversion."""

_BOMS = {'utf-8': b'\xef\xbb\xbf', 'utf-16-le': b'\xff\xfe', 'utf-16-be': b'\xfe\xff'}
_ENCODINGS = {'utf-8', 'utf-8-sig', 'utf-16', 'utf-16-le', 'utf-16-be', 'cp1252'}


def newline_info(text):
    counts = {'crlf': text.count('\r\n')}
    remaining = text.replace('\r\n', '')
    counts.update(lf=remaining.count('\n'), cr=remaining.count('\r'))
    present = [name for name, count in counts.items() if count]
    return {'newline': present[0] if len(present) == 1 else 'mixed' if present else 'none',
            'newline_counts': counts}


def decode_configuration(data):
    if not isinstance(data, bytes):
        raise TypeError('Configuration input must be bytes')
    if data.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')):
        raise ValueError('UTF-32 configuration is unsupported; refusing an ambiguous conversion')
    codec, bom = None, False
    for candidate, prefix in _BOMS.items():
        if data.startswith(prefix):
            codec, bom = candidate, True
            text = data[len(prefix):].decode(codec)
            break
    else:
        try:
            text, codec = data.decode('utf-8'), 'utf-8'
        except UnicodeError:
            text, codec = data.decode('cp1252'), 'cp1252'
    if '\x00' in text:
        raise ValueError('Configuration contains nulls or an unsupported unmarked text encoding')
    # The returned fixed-endian codec plus BOM flag disambiguates UTF-16 BE.
    # UTF-8-sig remains a supported explicit input, but new reads are precise.
    return {'text': text, 'encoding': codec, 'bom': bom, **newline_info(text)}


def encode_configuration(old, text, params):
    if not isinstance(text, str) or '\x00' in text:
        raise ValueError('Text must be a string without null characters')
    existing = decode_configuration(old) if old is not None else None
    explicit = params.get('encoding') if 'encoding' in params else None
    if 'encoding' in params and (not isinstance(explicit, str) or explicit not in _ENCODINGS):
        raise ValueError('Unsupported text encoding')
    if explicit is None:
        codec = existing['encoding'] if existing else 'utf-8'
        bom = existing['bom'] if existing else False
    elif explicit == 'utf-8-sig':
        codec, bom = 'utf-8', True
    elif explicit == 'utf-16':
        codec = existing['encoding'] if existing and existing['encoding'].startswith('utf-16-') else 'utf-16-le'
        bom = True
    else:
        codec = explicit
        bom = existing['bom'] if existing and existing['encoding'] == codec else False
    if 'bom' in params:
        if type(params['bom']) is not bool:
            raise ValueError('BOM must be a boolean')
        if params['bom'] is False and explicit in {'utf-8-sig', 'utf-16'}:
            raise ValueError('The explicit signature encoding requires a BOM; use a fixed codec to remove it')
        bom = params['bom']
    if bom and codec not in _BOMS:
        raise ValueError('This text encoding has no supported byte order mark')
    newline = params.get('newline', 'preserve')
    if newline not in {'preserve', 'lf', 'crlf', 'cr'}:
        raise ValueError('Newline must be preserve, lf, crlf or cr')
    if newline == 'preserve':
        newline = existing['newline'] if existing else 'none'
    if newline in {'lf', 'crlf', 'cr'}:
        normalized = text.replace('\r\n', '\n').replace('\r', '\n')
        text = normalized.replace('\n', {'lf':'\n', 'crlf':'\r\n', 'cr':'\r'}[newline])
    # Mixed/absent existing newlines are left exactly as supplied; no line-by-line
    # guessing or newline translation occurs in binary output.
    encoded = text.encode(codec)
    result = (_BOMS[codec] if bom else b'') + encoded
    return result, {'encoding': codec, 'bom': bom, **newline_info(text)}
