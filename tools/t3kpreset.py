#!/usr/bin/env python3
"""Read and write .t3kpreset files without the plugin (stdlib only).

  t3kpreset.py dump <file>                       print the header + body tree
  t3kpreset.py create <out.t3kpreset> <name> <model.nam> [--ir <ir.wav>]
                                                 new preset: one NAM block
                                                 (+ optional IR block), default
                                                 EQ/params, embedded model bytes

Format (plugin/include/PresetFile.h): "T3KH" | int32 LE headerLen | header
ValueTree | body ValueTree, in JUCE's ValueTree::writeToStream encoding.
`create` builds the same tree as TONE3000Processor::savePreset, with a "local"
tone like loadLocalTone's (id 0, local:true), so no TONE3000 account or
network is needed; the embedded bytes are what reopen the model.
"""
import json, os, struct, sys, uuid, zlib

# ---- JUCE ValueTree binary reader -------------------------------------------
class _R:
    def __init__(s, b, p=0): s.b, s.p = b, p
    def byte(s): v = s.b[s.p]; s.p += 1; return v
    def read(s, n): v = s.b[s.p:s.p + n]; s.p += n; return v
    def cint(s):
        n = s.byte(); size = n & 0x7f
        if size == 0: return 0
        v = int.from_bytes(s.read(size), 'little')
        return -v if n & 0x80 else v
    def cstr(s):
        e = s.b.index(0, s.p); v = s.b[s.p:e].decode(); s.p = e + 1; return v

def _rvar(r):
    n = r.cint()
    if n == 0: return None
    t = r.byte(); n -= 1
    if t == 1: return struct.unpack('<i', r.read(4))[0]
    if t == 2: return True
    if t == 3: return False
    if t == 4: return struct.unpack('<d', r.read(8))[0]
    if t == 5: return r.read(n)[:-1].decode()
    if t == 6: return struct.unpack('<q', r.read(8))[0]
    if t == 8: return ('bin', r.read(n))
    raise ValueError('unsupported var type %d' % t)

def _rtree(r):
    t = r.cstr(); props = {}
    for _ in range(r.cint()):
        k = r.cstr(); props[k] = _rvar(r)
    return {'type': t, 'props': props, 'children': [_rtree(r) for _ in range(r.cint())]}

def load(path):
    b = open(path, 'rb').read()
    if b[:4] != b'T3KH': raise ValueError('not a v2 .t3kpreset (magic %r)' % b[:4])
    hl = struct.unpack('<i', b[4:8])[0]
    r = _R(b, 8 + hl)
    return _rtree(_R(b[8:8 + hl])), _rtree(r)

# ---- writer -----------------------------------------------------------------
def _cint(v):
    m = abs(v); n = (m.bit_length() + 7) // 8
    return b'\0' if n == 0 else bytes([n | (0x80 if v < 0 else 0)]) + m.to_bytes(n, 'little')

def _wvar(v):
    if v is None: return _cint(0)
    if v is True: return _cint(1) + b'\x02'
    if v is False: return _cint(1) + b'\x03'
    if isinstance(v, int): return _cint(5) + b'\x01' + struct.pack('<i', v)
    if isinstance(v, float): return _cint(9) + b'\x04' + struct.pack('<d', v)
    if isinstance(v, str): e = v.encode() + b'\0'; return _cint(len(e) + 1) + b'\x05' + e
    if isinstance(v, tuple) and v[0] == 'bin': return _cint(len(v[1]) + 1) + b'\x08' + v[1]
    raise TypeError(v)

def _wtree(t):
    o = t['type'].encode() + b'\0' + _cint(len(t['props']))
    for k, v in t['props'].items(): o += k.encode() + b'\0' + _wvar(v)
    o += _cint(len(t['children']))
    for c in t['children']: o += _wtree(c)
    return o

def save(path, header, body):
    h = _wtree(header)
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f: f.write(b'T3KH' + struct.pack('<i', len(h)) + h + _wtree(body))
    os.replace(tmp, path)

# ---- building a preset ------------------------------------------------------
def T(type_, children=(), **props): return {'type': type_, 'props': props, 'children': list(children)}
def uid(): return uuid.uuid4().hex

# Defaults taken from a preset the plugin saved (real units, see
# TONE3000Processor::presetParameterIds).
PARAMS = [('inputLevel', .5), ('outputLevel', .5), ('outputBalance', .5), ('toneBass', 5.), ('toneMid', 5.),
    ('toneTreble', 5.), ('gateThreshold', -90.), ('gateEnabled', 1.), ('gateRelease', 50.), ('gateHold', 20.),
    ('gateRange', 80.), ('toneEqEnabled', 0.), ('pitchEnabled', 0.), ('pitchSemitones', 0.), ('pitchStep', 1.),
    ('pitchTonality', 20000.), ('pitchWindow', 1.), ('spreadEnabled', 0.), ('spreadOffset', .812),
    ('spreadWobble', .25), ('spreadWobbleEnabled', 1.), ('spreadCrossover', .5), ('spreadCrossoverEnabled', 1.),
    ('spreadDiffuseEnabled', 1.), ('alignEnabled', 0.), ('alignOffset', .5), ('alignWobble', .25),
    ('alignWobbleEnabled', 0.), ('alignCrossover', .5), ('alignCrossoverEnabled', 0.), ('alignDiffuseEnabled', 0.),
    ('chainPanLeft', 0.), ('chainPanRight', 1.), ('chainPanLinked', 1.), ('chainInvertLeft', 0.),
    ('chainInvertRight', 0.)]
BANDS = [('lowshelf', 100., .71), ('bell', 250., 1.), ('bell', 650., 1.), ('bell', 1600., 1.),
         ('bell', 3500., 1.4), ('highshelf', 8000., .71)]

def eq(pre):
    return T('Eq', [T('Band', type=t, freqHz=f, gainDb=0., q=q) for t, f, q in BANDS], enabled=True, pre=pre)

def insert_block():
    return T('ChainBlock', id=uid(), type='insert', enabled=True, normalize=True, slimSize=0., inputGain=.5,
             outputGain=.5, mix=1.)

def tone_block(kind, path):
    data = open(path, 'rb').read()
    name = os.path.splitext(os.path.basename(path))[0]
    mid = zlib.crc32(data) % 0x7ffffffe + 1   # any positive int; only compared within the block
    tone = {'id': 0, 'local': True, 'title': name, 'format': kind,
            'models': [{'id': mid, 'name': name, 'model_url': 'file://' + os.path.abspath(path)}]}
    return T('ChainBlock', [eq(kind == 'nam'), T('ModelCache', [T('CachedModel', modelId=mid, data=('bin', data))])],
             id=uid(), type=kind, enabled=True, normalize=True, slimSize=1., inputGain=.5, outputGain=.5, mix=1.,
             toneId=0, toneJson=json.dumps(tone, indent=2), activeModelId=mid)

def create(out, name, nam, ir=None):
    blocks = [tone_block('nam', nam)] + ([tone_block('ir', ir)] if ir else [])
    blocks += [insert_block() for _ in range(5 - len(blocks))]
    snap = T('ChainSnapshot', [T('ChainBlocks', blocks), T('RightChainBlocks', [insert_block() for _ in range(5)])],
             stereoEnabled=False, branchSide='left', branchAfterBlockId='')
    pid = uid()
    body = T('T3KPreset', [snap, T('Params', [T('Param', id=i, value=v) for i, v in PARAMS])],
             schemaVersion=1, name=name, id=pid)
    save(out, T('T3KPresetHeader', id=pid, name=name), body)

def dump(t, d=0):
    print(' ' * d + t['type'])
    for k, v in t['props'].items():
        if isinstance(v, tuple): v = '<binary %d bytes>' % len(v[1])
        elif isinstance(v, str) and len(v) > 80: v = v[:80] + '...(%d chars)' % len(v)
        print(' ' * d + '  %s=%r' % (k, v))
    for c in t['children']: dump(c, d + 2)

if __name__ == '__main__':
    a = sys.argv[1:]
    if len(a) == 2 and a[0] == 'dump':
        h, b = load(a[1]); dump(h); dump(b)
    elif len(a) in (4, 6) and a[0] == 'create' and (len(a) == 4 or a[4] == '--ir'):
        create(a[1], a[2], a[3], a[5] if len(a) == 6 else None)
    else:
        sys.exit(__doc__)
