"""Decode *and encode* a No Man's Sky .hg save. Read/write, zero dependencies.

This module is the only place in the package that turns save bytes into Python
objects or back: `loads`/`dumps` are the sole text boundary and the two points
where `surrogateescape` is applied. It is an ordinary module in an ordinary
package -- nothing is loaded by path and nothing is registered into
`sys.modules`.

Extends the original `nms-save-decode.py` prototype with the write path, the `mf_save*.hg` metadata
codec, and a self-test that proves the round trip is byte-for-byte lossless --
including text the game stores that is not valid UTF-8.

Save container format (Frontiers 3.60 and later, Steam/GOG):
    a stream of blocks, each a 16-byte header
        uint32 magic = 0xFEEDA1E5
        uint32 compressed_size
        uint32 uncompressed_size
        uint32 reserved (0)
    followed by compressed_size bytes of a raw LZ4 *block* (no LZ4 frame).
    The game chunks at exactly 0x80000 (524288) uncompressed bytes per block.
    The concatenated payload is compact JSON with obfuscated keys, terminated
    by exactly one NUL byte.

    A file whose first four bytes are not the magic is plaintext: the payload
    is the whole file. The game writes `accountdata.hg` that way itself.

Metadata format (`mf_<name>.hg`, 432 bytes on 5.50+, 384 on 5.00, 104 pre-3.60):
    XXTEA over little-endian uint32 words, key = b"NAESEVADNAYRTNRG" with word 0
    replaced by a murmur3-style mix of the storage slot index. Plaintext word 0
    is 0xEEEEEEBE and word 1 is the metadata format. See `meta_decode`.

Two encoding rules this file exists to enforce:
  * Bytes -> text uses UTF-8 with `surrogateescape`, never latin1. A live save
    contains both real multi-byte UTF-8 (base and player names in Chinese,
    Cyrillic and Greek) *and* procedurally generated item ids that are not
    valid UTF-8 at all. latin1 silently mangles the first; a plain utf-8
    decode silently destroys the second.
  * Float literals are preserved verbatim. The game prints floats with 17
    significant digits (`0.30000001192092898`); Python's repr prints the
    shortest round-tripping form (`0.30000001192092896`). Same double, different
    bytes, and a diff full of noise.

Usage:
    python -m nms_sorter.codec selftest [save.hg]
    python -m nms_sorter.codec decode <save.hg> <out.json> [--plain]
    python -m nms_sorter.codec encode <in.json> <out.hg> [--uncompressed]
    python -m nms_sorter.codec roundtrip <save.hg>
    python -m nms_sorter.codec meta <mf_save9.hg>
    python -m nms_sorter.codec blocks <save.hg>
"""
import json
import json.encoder
import os
import re
import struct
import sys

from importlib.resources import files as _res_files


def _read_data(name):
    """A file shipped inside `nms_sorter/data/`, read as package data.

    Not `os.path.join(dirname(__file__), "data", name)`: that spelling works in
    a source checkout and fails in a wheel, a zipapp and a PyInstaller bundle,
    which are three of the four ways this is meant to run.

    One `/` per segment, not `joinpath("data", name)`: multi-argument
    `joinpath` on a Traversable is 3.11 and later, and the declared floor is
    3.9.
    """
    return (_res_files("nms_sorter") / "data" / name).read_text(encoding="utf-8")


MAGIC = 0xFEEDA1E5
CHUNK = 0x80000            # the game's own uncompressed block size
META_MAGIC = 0xEEEEEEBE
META_KEY = b"NAESEVADNAYRTNRG"
M32 = 0xFFFFFFFF


# --------------------------------------------------------------------------
# LZ4 block codec
# --------------------------------------------------------------------------

def lz4_block_decompress(src, expected=0):
    out = bytearray()
    i = 0
    n = len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n:
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        match = token & 0x0F
        if match == 15:
            while True:
                b = src[i]
                i += 1
                match += b
                if b != 255:
                    break
        match += 4
        start = len(out) - offset
        if start < 0:
            raise ValueError("bad match offset")
        for k in range(match):
            out.append(out[start + k])
    if expected and len(out) != expected:
        raise ValueError("size mismatch: got %d want %d" % (len(out), expected))
    return bytes(out)


def _emit_len(out, n):
    """Write the 255-extension bytes for a length that overflowed its nibble."""
    while n >= 255:
        out.append(255)
        n -= 255
    out.append(n)


def lz4_block_compress(src, greedy=True):
    """Produce a raw LZ4 block. `greedy=False` emits a literals-only block.

    A literals-only block is valid LZ4 and is the provably safe option: it
    cannot produce a bad match offset because it produces no matches. It costs
    roughly 7x the file size against the game's own output.

    `greedy=True` is a single-pass hash-table matcher. It obeys the two rules a
    conforming decoder relies on: the last 5 bytes are always literals, and no
    match starts within the last 12 bytes.
    """
    n = len(src)
    out = bytearray()
    if not greedy or n < 13:
        _emit_literals(out, src, 0, n)
        return bytes(out)

    table = {}
    anchor = 0
    i = 0
    limit = n - 12
    last = n - 5
    while i < limit:
        seq = src[i:i + 4]
        ref = table.get(seq, -1)
        table[seq] = i
        if ref < 0 or i - ref > 65535 or src[ref:ref + 4] != seq:
            i += 1
            continue
        ml = 4
        while i + ml < last and src[ref + ml] == src[i + ml]:
            ml += 1
        lit = i - anchor
        ext = ml - 4
        token = ((15 if lit >= 15 else lit) << 4) | (15 if ext >= 15 else ext)
        out.append(token)
        if lit >= 15:
            _emit_len(out, lit - 15)
        out += src[anchor:i]
        off = i - ref
        out.append(off & 0xFF)
        out.append((off >> 8) & 0xFF)
        if ext >= 15:
            _emit_len(out, ext - 15)
        i += ml
        anchor = i
    _emit_literals(out, src, anchor, n)
    return bytes(out)


def _emit_literals(out, src, start, end):
    lit = end - start
    out.append((15 if lit >= 15 else lit) << 4)
    if lit >= 15:
        _emit_len(out, lit - 15)
    out += src[start:end]


# --------------------------------------------------------------------------
# .hg container
# --------------------------------------------------------------------------

def read_blocks(raw):
    """Return [(compressed_size, uncompressed_size, reserved), ...] or []."""
    blocks = []
    pos = 0
    while pos + 16 <= len(raw):
        magic, csize, usize, res = struct.unpack_from("<IIII", raw, pos)
        if magic != MAGIC:
            break
        blocks.append((csize, usize, res))
        pos += 16 + csize
    return blocks


def read_payload_bytes(raw):
    """Return (payload_bytes, info) for a whole .hg file already in memory.

    The container-level half of the decoder. It used to be reachable only
    through a file path, so the self-test and `safety.py`'s verification step
    wrote the candidate bytes to a `tempfile.mkstemp` and read them straight
    back -- a save's worth of plaintext-adjacent bytes landing in `%TEMP%`,
    twice per apply, purely to satisfy a signature. Decoding in memory is the
    same work without the file.
    """
    if len(raw) < 4 or struct.unpack_from("<I", raw, 0)[0] != MAGIC:
        return raw, {"framed": False, "blocks": 0, "disk": len(raw)}
    out = bytearray()
    pos = 0
    blocks = 0
    while pos + 16 <= len(raw):
        magic, csize, usize, _ = struct.unpack_from("<IIII", raw, pos)
        if magic != MAGIC:
            raise ValueError("block %d at offset %d has magic 0x%08X" % (blocks, pos, magic))
        pos += 16
        out += lz4_block_decompress(raw[pos:pos + csize], usize)
        pos += csize
        blocks += 1
    if pos != len(raw):
        raise ValueError("%d trailing bytes after %d blocks" % (len(raw) - pos, blocks))
    return bytes(out), {"framed": True, "blocks": blocks, "disk": len(raw)}


# The name this function had while it was a self-test helper. `safety.py` calls
# it at two sites; the alias is a one-line compatibility shim so a rename in
# this lane is not an edit to a file another lane owns.
_read_payload_bytes = read_payload_bytes

# While the codec lived in `_codec_impl` and this module was a thin re-export,
# `from .codec import codec` handed callers the implementation module. Two
# files still spell it that way; `codec` is now this module itself, so the
# spelling keeps meaning the same thing. Removable the moment those two import
# lines are rewritten -- they belong to other lanes.
codec = sys.modules[__name__]


def read_payload(path):
    """Return (payload_bytes, info). Handles framed and plaintext files."""
    with open(path, "rb") as fh:
        return read_payload_bytes(fh.read())


def frame_payload(payload, compress=True, greedy=True, chunk=CHUNK):
    """Wrap a payload in the game's block framing. compress=False -> plaintext."""
    if not compress:
        return payload
    out = bytearray()
    for off in range(0, len(payload), chunk):
        piece = payload[off:off + chunk]
        blob = lz4_block_compress(piece, greedy=greedy)
        out += struct.pack("<IIII", MAGIC, len(blob), len(piece), 0)
        out += blob
    return bytes(out)


# --------------------------------------------------------------------------
# JSON, byte-exactly
# --------------------------------------------------------------------------

class RawFloat(float):
    """A float that remembers the literal it was parsed from."""
    __slots__ = ("raw",)

    def __new__(cls, text):
        o = float.__new__(cls, text)
        o.raw = text
        return o


# The game escapes a control byte as a backslash-u escape with four
# *upper-case* hex digits; Python's json writes lower case. Same string,
# different bytes, so every procedural item id that happens to contain a byte
# below 0x20 would show up in a diff. Observed in save3.hg and save4.hg, whose
# ship inventories hold ids carrying 0x1E and 0x1F. No save seen so far holds a
# tab, newline, quote or backslash inside a string, so the short forms are
# unobserved; emitting the u-escape for every control byte is the conservative
# choice and the round-trip check would catch it if a save ever disagreed.
_ESCAPE = re.compile(r'[\x00-\x1f\\"]')
# The game writes the short escapes JSON defines and \uXXXX for everything else.
# A procedurally generated id can contain any low byte -- one in this save holds
# 0x0C -- and emitting  where the game wrote \f is a byte difference that
# fails the identity round trip and blocks every edit. Same character either way;
# the point is to reproduce the game's bytes, not to be equivalent to them.
_ESCAPE_DCT = {'\\': '\\\\', '"': '\\"',
               '\b': '\\b', '\f': '\\f', '\n': '\\n',
               '\r': '\\r', '\t': '\\t'}
for _i in range(0x20):
    _ESCAPE_DCT.setdefault(chr(_i), '\\u%04X' % _i)


def encode_basestring(s):
    return '"' + _ESCAPE.sub(lambda m: _ESCAPE_DCT[m.group(0)], s) + '"'


class _Encoder(json.JSONEncoder):
    """JSONEncoder that prints RawFloat verbatim.

    CPython's encoder formats floats with `float.__repr__(o)`, which bypasses a
    subclass __repr__, and its C fast path bypasses the hook entirely. So the
    pure-Python iterencode is constructed by hand with our own float formatter.
    """

    escape_slash = False

    def iterencode(self, o, _one_shot=False):
        def floatstr(f, *_a, **_k):
            if isinstance(f, RawFloat):
                return f.raw
            return float.__repr__(f)
        esc = encode_basestring
        if self.escape_slash:
            # Builds before Frontiers wrote "MODELS\/COMMON\/...". The escape is
            # optional in JSON, so it is invisible to a parser and visible in a
            # byte diff. Only needed to byte-match a pre-3.60 save.
            esc = lambda s: encode_basestring(s).replace("/", "\\/")
        return json.encoder._make_iterencode(
            None, self.default, esc,
            self.indent, floatstr, self.key_separator, self.item_separator,
            self.sort_keys, self.skipkeys, False)(o, 0)


def loads(payload):
    """Payload bytes (with or without the trailing NUL) -> Python objects."""
    if payload.endswith(b"\x00"):
        payload = payload.rstrip(b"\x00")
    return json.loads(payload.decode("utf-8", "surrogateescape"),
                      parse_float=RawFloat)


def dumps(doc, nul=True, escape_slash=False):
    """Python objects -> payload bytes, matching the game's own serialiser."""
    enc = _Encoder(ensure_ascii=False, separators=(",", ":"))
    enc.escape_slash = escape_slash
    text = "".join(enc.iterencode(doc))
    out = text.encode("utf-8", "surrogateescape")
    return out + b"\x00" if nul else out


def load(path):
    payload, info = read_payload(path)
    return loads(payload), info


def save(path, doc, compress=True, greedy=True):
    data = frame_payload(dumps(doc), compress=compress, greedy=greedy)
    with open(path, "wb") as fh:
        fh.write(data)
    return len(data)


# --------------------------------------------------------------------------
# Key deobfuscation (optional; the codec never needs it)
# --------------------------------------------------------------------------

def load_keymap(path=None):
    """Read the packaged `data/savekeys.json`. Returns (obf->name, name->obf).

    Both directions are read rather than one inverted here: the generator has
    already proved the map is injective (it refuses to write a colliding one),
    so inverting it again at every start-up would be re-deriving a fact that is
    already in the file. `path` names an alternative JSON file of the same
    shape, for a test.
    """
    if path is None:
        blob = _read_data("savekeys.json")
    else:
        blob = open(path, encoding="utf-8").read()
    m = json.loads(blob)
    fwd, rev = m["forward"], m["reverse"]
    if len(fwd) != len(rev):
        raise ValueError("savekeys.json: %d forward keys but %d reverse"
                         % (len(fwd), len(rev)))
    return fwd, rev


def remap(obj, table):
    """Rename every object key through `table`; unknown keys pass through."""
    if isinstance(obj, dict):
        return dict((table.get(k, k), remap(v, table)) for k, v in obj.items())
    if isinstance(obj, list):
        return [remap(v, table) for v in obj]
    return obj


# --------------------------------------------------------------------------
# mf_*.hg metadata: XXTEA
# --------------------------------------------------------------------------

_DELTA = 0x9E3779B9


def _rotl32(v, n):
    v &= M32
    return ((v << n) | (v >> (32 - n))) & M32


def meta_slot(filename):
    """Storage slot index the metadata key is derived from.

    `save.hg` -> 2, `save2.hg` -> 3, ... `save9.hg` -> 10, `save10.hg` -> 11.
    (cTkStoragePersistent::Slot; 0 = UserSettings, 1 = AccountData.)
    """
    base = os.path.basename(filename)
    if base.startswith("mf_"):
        base = base[3:]
    m = re.match(r"save(\d*)\.hg$", base, re.I)
    if not m:
        raise ValueError("not a save filename: %r" % base)
    return (int(m.group(1)) if m.group(1) else 1) + 1


def _meta_key(slot):
    k = list(struct.unpack("<4I", META_KEY))
    k[0] = (_rotl32(slot ^ 0x1422CB8C, 13) * 5 + 0xE6546B64) & M32
    return k


def _mx(y, z, s, k, p, e):
    return ((((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^
            ((s ^ y) + (k[(p & 3) ^ e] ^ z))) & M32


def _xxtea_decrypt(v, k, q):
    n = len(v)
    s = (_DELTA * q) & M32
    y = v[0]
    while s:
        e = (s >> 2) & 3
        for p in range(n - 1, 0, -1):
            z = v[p - 1]
            y = v[p] = (v[p] - _mx(y, z, s, k, p, e)) & M32
        z = v[n - 1]
        y = v[0] = (v[0] - _mx(y, z, s, k, 0, e)) & M32
        s = (s - _DELTA) & M32
    return v


def _xxtea_encrypt(v, k, q):
    n = len(v)
    s = 0
    z = v[n - 1]
    for _ in range(q):
        s = (s + _DELTA) & M32
        e = (s >> 2) & 3
        for p in range(n - 1):
            y = v[p + 1]
            z = v[p] = (v[p] + _mx(y, z, s, k, p, e)) & M32
        y = v[0]
        z = v[n - 1] = (v[n - 1] + _mx(y, z, s, k, n - 1, e)) & M32
    return v


def _meta_rounds(nbytes):
    return 8 if nbytes == 104 else 6


def meta_decode(raw, slot):
    """Decrypt an mf_ file to its plaintext bytes. Raises on a wrong slot."""
    if len(raw) < 8 or len(raw) % 4:
        raise ValueError("invalid metadata length: %d" % len(raw))
    v = list(struct.unpack("<%dI" % (len(raw) // 4), raw))
    _xxtea_decrypt(v, _meta_key(slot), _meta_rounds(len(raw)))
    out = struct.pack("<%dI" % len(v), *v)
    magic = struct.unpack_from("<I", out, 0)[0]
    if magic != META_MAGIC:
        raise ValueError("metadata magic 0x%08X, wrong slot key?" % magic)
    return out


def meta_encode(plain, slot):
    if len(plain) % 4:
        raise ValueError("metadata plaintext must be a multiple of 4 bytes")
    v = list(struct.unpack("<%dI" % (len(plain) // 4), plain))
    _xxtea_encrypt(v, _meta_key(slot), _meta_rounds(len(plain)))
    return struct.pack("<%dI" % len(v), *v)


# Field offsets inside the decrypted metadata, in bytes from its start.
META_FIELDS = {
    "magic": 0x00, "format": 0x04, "spooky": 0x08, "sha256": 0x18,
    "size_decompressed": 0x38, "size_disk": 0x3C, "profile_hash": 0x40,
    "base_version": 0x44, "game_mode": 0x48, "play_time": 0x4C,
    "save_name": 0x58, "save_summary": 0xD8, "difficulty": 0x158,
    "slot_id": 0x15C, "timestamp": 0x164, "format2": 0x168,
}


def meta_read(path):
    """Decode mf_<save>.hg next to, or named as, a save file."""
    raw = open(path, "rb").read()
    plain = meta_decode(raw, meta_slot(path))
    g = lambda o: struct.unpack_from("<I", plain, o)[0]
    s = lambda o: plain[o:plain.index(b"\x00", o)].decode("utf-8", "surrogateescape")
    return {
        "raw_len": len(raw), "plain": plain,
        "format": g(0x04),
        "spooky": plain[0x08:0x18], "sha256": plain[0x18:0x38],
        "size_decompressed": g(0x38), "size_disk": g(0x3C),
        "base_version": g(0x44), "game_mode": g(0x48) & 0xFFFF,
        "season": g(0x48) >> 16,
        "play_time": struct.unpack_from("<Q", plain, 0x4C)[0],
        "save_name": s(0x58), "save_summary": s(0xD8),
        "slot_id": plain[0x15C:0x164].hex(), "timestamp": g(0x164),
    }


def meta_update_sizes(path, size_decompressed, size_disk, out_path=None):
    """Rewrite only the two size fields, re-encrypt, write. Everything else --
    timestamp, slot id, save name, summary -- is left byte-identical."""
    slot = meta_slot(path)
    plain = bytearray(meta_decode(open(path, "rb").read(), slot))
    struct.pack_into("<I", plain, 0x38, size_decompressed)
    struct.pack_into("<I", plain, 0x3C, size_disk)
    blob = meta_encode(bytes(plain), slot)
    with open(out_path or path, "wb") as fh:
        fh.write(blob)
    return blob


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

# A procedural item id in the shape the game generates them. It is not valid
# UTF-8; `bytes.decode("utf-8")` destroys it and latin1 mangles every real
# non-ASCII name in the same file. Kept verbatim because it is the canonical
# counter-example to both wrong answers, and an item id carries no personal
# data.
PROC_ID = b"^\x80\x80\xfd62\x95#03535"

# The strings below are invented, not taken from anybody's save. Each one is
# here for a class of byte the codec has to reproduce exactly, and the class is
# what the test asserts on -- so they can be replaced by any other string of
# the same class without weakening anything:
#
#   CJK, Cyrillic, Greek     multi-byte UTF-8 that latin1 would mangle
#   U+FDFC (Arabic form)     a presentation form: three UTF-8 bytes, one char
#   PROC_ID / "escapes"[2]   bytes that are not UTF-8 at all, plus the control
#                            bytes 0x1F and 0x0C that decide upper-case \uXXXX
#                            versus the short \f escape
CJK_MARK = "测试基地"
# Written as bytes and decoded, not as a str literal: "\x80" in a str literal
# is U+0080, which encodes to two *valid* UTF-8 bytes and so tests nothing.
# Only surrogateescape produces the raw-byte case, and this is how it is spelt.
RAW_BYTE_ID = b"^\x80\x81\x1f\x0c#00000".decode("utf-8", "surrogateescape")
SAMPLE = {
    "F2P": 4735,
    "8>q": "Win|Final",
    "floats": [RawFloat("0.30000001192092898"), RawFloat("1.0"),
               RawFloat("-2.9802322387695312e-08"), RawFloat("0.0")],
    "ints": [0, -1, 4294967296, 9007199254740993],
    "names": ["Sample" + CJK_MARK, "样本旗",
              "Колония 00αβ000γδ",
              "Δοκιμαστική βάση 00ωγ000οδ",
              "﷼﷼﷼﷼"],
    "escapes": ["quote\" backslash\\ tab\t newline\n del\x7f", "",
                RAW_BYTE_ID],   # id shape: raw bytes plus 0x1F and 0x0C
    "nested": {"a": [{"b": []}, {}, [[]]], "": None, "t": True, "f": False},
}


def _check(ok, label):
    print("  %-58s %s" % (label, "ok" if ok else "FAIL"))
    if not ok:
        raise SystemExit("self-test failed: " + label)


def selftest(real_save=None):
    print("nms-save-codec self-test")

    # 1. LZ4 block codec, both modes, on data with and without repetition.
    import random
    random.seed(7)
    cases = [b"", b"a", b"a" * 5, b"abcd" * 3, bytes(range(256)) * 40,
             b"".join(random.choice([b"the quick brown fox ", b"\x00" * 9,
                                     bytes([random.randrange(256)])])
                      for _ in range(4000)),
             bytes(random.randrange(256) for _ in range(70000))]
    for greedy in (False, True):
        for src in cases:
            blob = lz4_block_compress(src, greedy=greedy)
            _check(lz4_block_decompress(blob, len(src)) == src,
                   "lz4 %s block, %d bytes -> %d" %
                   ("greedy" if greedy else "literal", len(src), len(blob)))

    # 2. JSON round trip is byte-exact, including text that is not UTF-8.
    doc = dict(SAMPLE)
    doc["proc"] = PROC_ID.decode("utf-8", "surrogateescape")
    payload = dumps(doc)
    _check(payload.endswith(b"\x00"), "payload is NUL terminated")
    _check(PROC_ID in payload, "non-UTF-8 procedural id survives serialisation")
    _check(CJK_MARK.encode("utf-8") in payload, "CJK is written as UTF-8, not latin1")
    _check(b"0.30000001192092898" in payload, "17-digit float literal preserved")
    _check(b"\\u001F" in payload and b"\\u001f" not in payload,
           "control byte escaped with upper-case hex, as the game does")
    _check(b"\\f" in payload and b"\\u000C" not in payload,
           "0x0C uses the short \\f escape, as the game does")
    again = dumps(loads(payload))
    _check(again == payload, "json decode -> encode is byte-exact")
    back = loads(payload)
    _check(back["proc"].encode("utf-8", "surrogateescape") == PROC_ID,
           "procedural id survives the decode half too")

    # 3. Whole-file round trip through the block framing, both modes.
    for greedy in (False, True):
        blob = frame_payload(payload, compress=True, greedy=greedy)
        _check(struct.unpack_from("<I", blob, 0)[0] == MAGIC, "framed file starts with the magic")
        got, info = read_payload_bytes(blob)
        _check(got == payload and info["framed"], "framed %s round trip" % ("greedy" if greedy else "literal"))
    plain = frame_payload(payload, compress=False)
    got, info = read_payload_bytes(plain)
    _check(got == payload and not info["framed"], "plaintext round trip")

    # 4. Multi-block chunking at the game's own size.
    big = dumps({"pad": [i for i in range(200000)]})
    blob = frame_payload(big)
    blocks = read_blocks(blob)
    _check(len(blocks) > 1, "payload larger than 0x80000 produces %d blocks" % len(blocks))
    _check(all(u == CHUNK for _, u, _ in blocks[:-1]), "every full block is 0x80000 uncompressed")
    _check(all(r == 0 for _, _, r in blocks), "reserved word is zero")
    _check(read_payload_bytes(blob)[0] == big, "multi-block round trip")

    # 5. Metadata XXTEA round trip.
    plain_meta = bytearray(432)
    struct.pack_into("<II", plain_meta, 0, META_MAGIC, 2004)
    struct.pack_into("<II", plain_meta, 0x38, 2602956, 349603)
    blob = meta_encode(bytes(plain_meta), meta_slot("save9.hg"))
    _check(meta_decode(blob, meta_slot("save9.hg")) == bytes(plain_meta), "mf_ XXTEA round trip")
    _check(meta_slot("save.hg") == 2 and meta_slot("save9.hg") == 10 and
           meta_slot("mf_save10.hg") == 11, "slot index derivation")

    # 6. Against a real save, if one was handed over.
    if real_save:
        payload, info = read_payload(real_save)
        print("  real save: %s, %d blocks, %d payload bytes" %
              (os.path.basename(real_save), info["blocks"], len(payload)))
        doc = loads(payload)
        esc = b"\\/" in payload            # pre-Frontiers escaped forward slashes
        _check(dumps(doc, escape_slash=esc) == payload,
               "real save json round trip is byte-exact%s" % (" (\\/ escaped)" if esc else ""))
        for greedy in (False, True):
            blob = frame_payload(payload, greedy=greedy)
            got, _ = read_payload_bytes(blob)
            _check(got == payload, "real save reframed (%s) decodes identically%s" %
                   ("greedy" if greedy else "literal",
                    "  [%d -> %d bytes]" % (info["disk"], len(blob))))
        mf = os.path.join(os.path.dirname(real_save), "mf_" + os.path.basename(real_save))
        if os.path.exists(mf):
            m = meta_read(mf)
            _check(m["size_disk"] == info["disk"],
                   "mf_ size_disk %d == file size %d" % (m["size_disk"], info["disk"]))
            _check(m["size_decompressed"] == len(payload),
                   "mf_ size_decompressed %d == payload %d" % (m["size_decompressed"], len(payload)))
            _check(m["spooky"] == b"\x00" * 16 and m["sha256"] == b"\x00" * 32,
                   "mf_ carries no integrity hash on this build")
            _check(meta_encode(m["plain"], meta_slot(mf)) == open(mf, "rb").read(),
                   "mf_ re-encrypts byte-identically")
    print("all checks passed")


# --------------------------------------------------------------------------

def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1]
    if cmd == "selftest":
        selftest(argv[2] if len(argv) > 2 else None)
    elif cmd == "blocks":
        raw = open(argv[2], "rb").read()
        blocks = read_blocks(raw)
        if not blocks:
            print("plaintext, %d bytes" % len(raw))
        for i, (c, u, r) in enumerate(blocks):
            print("block %-3d csize=%-8d usize=%-8d reserved=%d" % (i, c, u, r))
        print("total uncompressed=%d disk=%d" % (sum(u for _, u, _ in blocks), len(raw)))
    elif cmd == "meta":
        m = meta_read(argv[2])
        for k in ("format", "size_decompressed", "size_disk", "base_version",
                  "game_mode", "season", "play_time", "save_name",
                  "save_summary", "slot_id", "timestamp"):
            print("%-18s %s" % (k, m[k]))
        print("%-18s %s" % ("spooky", m["spooky"].hex()))
        print("%-18s %s" % ("sha256", m["sha256"].hex()))
    elif cmd == "decode":
        doc, info = load(argv[2])
        if "--plain" in argv:
            fwd, _ = load_keymap()
            doc = remap(doc, fwd)
        with open(argv[3], "w", encoding="utf-8", errors="surrogateescape") as fh:
            json.dump(doc, fh, indent=1, ensure_ascii=False)
        print("blocks=%d payload=%d -> %s" % (info["blocks"], info["disk"], argv[3]))
    elif cmd == "encode":
        with open(argv[2], encoding="utf-8", errors="surrogateescape") as fh:
            doc = json.load(fh, parse_float=RawFloat)
        n = save(argv[3], doc, compress="--uncompressed" not in argv)
        print("wrote %d bytes -> %s" % (n, argv[3]))
    elif cmd == "roundtrip":
        payload, info = read_payload(argv[2])
        doc = loads(payload)
        again = dumps(doc, escape_slash=b"\\/" in payload)
        print("payload %d bytes, framed=%s, blocks=%d" %
              (len(payload), info["framed"], info["blocks"]))
        print("json round trip byte-exact: %s" % (again == payload))
        for greedy in (False, True):
            blob = frame_payload(payload, greedy=greedy)
            got, _ = read_payload_bytes(blob)
            print("reframe %-8s %8d bytes  decodes identically: %s" %
                  ("greedy" if greedy else "literal", len(blob), got == payload))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
