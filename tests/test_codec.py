"""The codec, asserted rather than printed.

The codec's own `selftest()` still exists and still runs (it is the thing a
user can invoke against their own save with no test runner installed). These
tests cover the same ground under pytest, plus the two properties the inlining
in P0-2 introduced: the container decoder works on bytes with no temporary
file, and `_read_payload_bytes` still answers to its old name because
`safety.py` calls it that way.

Every check here is about *bytes*, not about equivalence. A save that decodes
to an equal document but re-encodes to different bytes is a failure: the whole
safety argument is "encode it, decode it, compare it to what we read", and that
argument is worth nothing if the comparison is fuzzy.

`--real-saves PATH` adds a read-only round trip over every `save*.hg` in a
folder. Nothing is written; the folder is opened for reading only.
"""
import glob
import os
import random
import struct

import pytest

from nms_sorter import codec
from nms_sorter.codec import (CHUNK, MAGIC, META_MAGIC, PROC_ID, RawFloat,
                              dumps, frame_payload, loads, lz4_block_compress,
                              lz4_block_decompress, meta_decode, meta_encode,
                              meta_slot, read_blocks, read_payload,
                              read_payload_bytes)


# --------------------------------------------------------------------------
# LZ4, both modes
# --------------------------------------------------------------------------

def _lz4_cases():
    rnd = random.Random(7)
    return [
        ("empty", b""),
        ("one byte", b"a"),
        ("runs", b"a" * 5),
        # 12 bytes: the boundary below which the greedy matcher must fall back
        # to literals, because no match may start in the last 12 bytes.
        ("under the match floor", b"abcd" * 3),
        ("exactly the match floor", b"abcd" * 4),
        ("highly repetitive", bytes(range(256)) * 40),
        ("mixed", b"".join(rnd.choice([b"the quick brown fox ", b"\x00" * 9,
                                       bytes([rnd.randrange(256)])])
                           for _ in range(4000))),
        ("incompressible", bytes(rnd.randrange(256) for _ in range(70000))),
    ]


@pytest.mark.parametrize("greedy", [False, True], ids=["literal", "greedy"])
@pytest.mark.parametrize("label,src", _lz4_cases(), ids=[c[0] for c in _lz4_cases()])
def test_lz4_block_round_trip(label, src, greedy):
    blob = lz4_block_compress(src, greedy=greedy)
    assert lz4_block_decompress(blob, len(src)) == src


def test_lz4_greedy_actually_compresses():
    """Without this, `greedy=True` could be silently emitting literals and
    every round-trip test above would still pass."""
    src = b"the quick brown fox " * 2000
    assert len(lz4_block_compress(src, greedy=True)) < len(src) // 10
    assert len(lz4_block_compress(src, greedy=False)) > len(src)


def test_lz4_decompress_refuses_a_bad_offset():
    # token: 0 literals, 4-byte match, at offset 1 with nothing emitted yet.
    with pytest.raises(ValueError):
        lz4_block_decompress(b"\x00\x01\x00")


def test_lz4_decompress_checks_the_expected_size():
    blob = lz4_block_compress(b"abcdef", greedy=False)
    with pytest.raises(ValueError):
        lz4_block_decompress(blob, 7)


# --------------------------------------------------------------------------
# JSON, byte-exactly
# --------------------------------------------------------------------------

def test_payload_is_nul_terminated_exactly_once():
    out = dumps({"a": 1})
    assert out.endswith(b"\x00")
    assert not out.endswith(b"\x00\x00")
    assert dumps({"a": 1}, nul=False) + b"\x00" == out


def test_control_byte_uses_upper_case_hex():
    """The game writes \\u001F; Python's json writes \\u001f. Same string,
    different bytes, and every procedural id carrying a low byte would show up
    in a diff."""
    out = dumps({"id": "^\x1f#00000"})
    assert b"\\u001F" in out
    assert b"\\u001f" not in out


def test_short_escapes_match_the_game():
    """0x0C is written \\f, not \\u000C: one save's ship inventory holds an id
    with 0x0C in it and the long form fails the identity round trip."""
    out = dumps({"id": "a\x0cb", "ws": "\t\n\r\b", "q": 'a"b\\c'})
    assert b"\\f" in out and b"\\u000C" not in out and b"\\u000c" not in out
    assert b"\\t" in out and b"\\n" in out and b"\\r" in out and b"\\b" in out
    assert b'\\"' in out and b"\\\\" in out


def test_del_and_high_control_bytes_are_not_escaped():
    # 0x7F is not a JSON control character; escaping it would be a byte
    # difference in the other direction.
    assert b"\x7f" in dumps({"a": "\x7f"})


def test_raw_float_literal_is_preserved_verbatim():
    """`repr(0.30000001192092898)` is `0.30000001192092896`: the same double,
    a different literal. The game writes 17 significant digits, so we do."""
    doc = loads(b'{"f":0.30000001192092898,"g":1.0,"h":-2.9802322387695312e-08}')
    assert isinstance(doc["f"], RawFloat)
    assert doc["f"].raw == "0.30000001192092898"
    assert repr(float(doc["f"])) == "0.30000001192092896"
    assert dumps(doc, nul=False) == \
        b'{"f":0.30000001192092898,"g":1.0,"h":-2.9802322387695312e-08}'


def test_plain_float_still_serialises():
    assert dumps({"f": 0.5}, nul=False) == b'{"f":0.5}'


def test_large_ints_are_not_turned_into_floats():
    src = b'{"a":0,"b":-1,"c":4294967296,"d":9007199254740993}'
    assert dumps(loads(src), nul=False) == src


def test_non_utf8_procedural_id_survives_both_halves():
    """The one thing every other editor gets wrong. `bytes.decode("utf-8")`
    raises or replaces; latin1 survives this and mangles every real name."""
    doc = {"id": PROC_ID.decode("utf-8", "surrogateescape")}
    out = dumps(doc)
    assert PROC_ID in out
    assert loads(out)["id"].encode("utf-8", "surrogateescape") == PROC_ID


@pytest.mark.parametrize("label,s", [
    ("CJK", "测试基地"),
    ("Cyrillic", "Колония 00αβ000γδ"),
    ("Greek", "Δοκιμαστική βάση"),
    ("Arabic presentation form", "﷼﷼"),
])
def test_real_utf8_is_written_as_utf8_not_latin1(label, s):
    out = dumps({"n": s})
    assert s.encode("utf-8") in out
    assert loads(out)["n"] == s


def test_sample_document_round_trips_byte_exact():
    doc = dict(codec.SAMPLE)
    doc["proc"] = PROC_ID.decode("utf-8", "surrogateescape")
    payload = dumps(doc)
    assert dumps(loads(payload)) == payload


def test_sample_holds_no_personal_names():
    """P0-2 replaced the four names lifted out of a real save. They exercised
    four classes of byte, so the classes are what the replacements have to
    keep, and the names themselves must not come back."""
    blob = dumps(codec.SAMPLE)
    for gone in ("Fixture基地", "Колония 11", "Cataratas", "﷽"):
        assert gone.encode("utf-8") not in blob, gone
    assert "测试基地".encode("utf-8") in blob            # CJK
    assert "Колония".encode("utf-8") in blob             # Cyrillic
    assert "Δοκιμαστική".encode("utf-8") in blob         # Greek
    assert "﷼".encode("utf-8") in blob              # Arabic presentation form
    assert b"^\x80\x81" in blob                          # bytes that are not UTF-8


def test_escape_slash_only_when_asked():
    doc = {"p": "MODELS/COMMON/X"}
    assert b"\\/" not in dumps(doc)
    assert b"MODELS\\/COMMON\\/X" in dumps(doc, escape_slash=True)


def test_loads_tolerates_a_missing_nul():
    assert loads(b'{"a":1}') == loads(b'{"a":1}\x00')


# --------------------------------------------------------------------------
# Container framing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("greedy", [False, True], ids=["literal", "greedy"])
def test_framed_round_trip(greedy):
    payload = dumps(dict(codec.SAMPLE))
    blob = frame_payload(payload, compress=True, greedy=greedy)
    assert struct.unpack_from("<I", blob, 0)[0] == MAGIC
    got, info = read_payload_bytes(blob)
    assert got == payload
    assert info["framed"] is True and info["blocks"] == 1
    assert info["disk"] == len(blob)


def test_plaintext_fallback():
    """A file whose first four bytes are not the magic is the payload. The game
    writes `accountdata.hg` that way, and the 2021-era saves in the corpus are
    whole files of plaintext JSON."""
    payload = dumps({"a": 1})
    blob = frame_payload(payload, compress=False)
    assert blob == payload
    got, info = read_payload_bytes(blob)
    assert got == payload
    assert info["framed"] is False and info["blocks"] == 0
    assert read_blocks(blob) == []


def test_multi_block_framing_at_the_games_own_chunk_size():
    big = dumps({"pad": list(range(200000))})
    assert len(big) > 2 * CHUNK
    blob = frame_payload(big)
    blocks = read_blocks(blob)
    assert len(blocks) == (len(big) + CHUNK - 1) // CHUNK > 1
    assert all(u == CHUNK for _, u, _ in blocks[:-1])
    assert blocks[-1][1] == len(big) - CHUNK * (len(blocks) - 1)
    assert all(r == 0 for _, _, r in blocks)
    got, info = read_payload_bytes(blob)
    assert got == big and info["blocks"] == len(blocks)


def test_chunk_boundary_exactly():
    """A payload that is an exact multiple of 0x80000 must not produce a
    trailing empty block, and must not lose its last block either."""
    payload = b"x" * CHUNK
    blob = frame_payload(payload)
    assert len(read_blocks(blob)) == 1
    assert read_payload_bytes(blob)[0] == payload


def test_trailing_bytes_are_refused():
    blob = frame_payload(dumps({"a": 1})) + b"junk"
    with pytest.raises(ValueError):
        read_payload_bytes(blob)


def test_bad_magic_mid_stream_is_refused():
    blob = bytearray(frame_payload(dumps({"pad": list(range(200000))})))
    # corrupt the second block's magic, leaving the first intact
    first = read_blocks(bytes(blob))[0]
    struct.pack_into("<I", blob, 16 + first[0], 0xDEADBEEF)
    with pytest.raises(ValueError):
        read_payload_bytes(bytes(blob))


# --------------------------------------------------------------------------
# In-memory decode (P0-2)
# --------------------------------------------------------------------------

def test_read_payload_bytes_needs_no_temp_file():
    """The old helper wrote the candidate bytes to `%TEMP%` and read them
    straight back -- twice per apply, on the write path. `tempfile` is not
    imported by this module any more, which is the observable form of that."""
    assert not hasattr(codec, "tempfile")


def test_legacy_alias_still_answers():
    """safety.py calls `codec._read_payload_bytes(framed)` at two sites."""
    assert codec._read_payload_bytes is read_payload_bytes


def test_read_payload_file_wrapper_agrees_with_the_bytes_form(tmp_path):
    payload = dumps(dict(codec.SAMPLE))
    blob = frame_payload(payload)
    p = tmp_path / "save9.hg"
    p.write_bytes(blob)
    assert read_payload(str(p)) == read_payload_bytes(blob)


def test_save_and_load_round_trip(tmp_path):
    p = tmp_path / "save9.hg"
    doc = dict(codec.SAMPLE)
    codec.save(str(p), doc)
    back, info = codec.load(str(p))
    assert dumps(back) == dumps(doc) and info["framed"]


# --------------------------------------------------------------------------
# the save-key map
# --------------------------------------------------------------------------

def test_load_keymap_reads_the_packaged_json():
    """It used to read `data/jsonmap.txt` by path and invert it at start-up.
    The generator has already proved the map injective and written both
    directions, so this is a load, not a derivation."""
    fwd, rev = codec.load_keymap()
    assert fwd["F2P"] == "Version" and rev["Version"] == "F2P"
    assert len(fwd) == len(rev) > 1000


def test_load_keymap_accepts_an_explicit_file(tmp_path):
    p = tmp_path / "savekeys.json"
    p.write_text('{"forward":{"a":"A"},"reverse":{"A":"a"}}', encoding="utf-8")
    assert codec.load_keymap(str(p)) == ({"a": "A"}, {"A": "a"})


def test_load_keymap_refuses_a_lopsided_map(tmp_path):
    """Two obfuscated keys mapping to one plain name cannot be inverted, and
    `savemodel.py` inverts it to find containers by name."""
    p = tmp_path / "savekeys.json"
    p.write_text('{"forward":{"a":"A","b":"A"},"reverse":{"A":"a"}}',
                 encoding="utf-8")
    with pytest.raises(ValueError):
        codec.load_keymap(str(p))


def test_remap_renames_keys_and_passes_unknowns_through():
    fwd, _ = codec.load_keymap()
    doc = {"F2P": 1, "not_a_key": {"8>q": "x"}, "list": [{"F2P": 2}]}
    out = codec.remap(doc, fwd)
    assert out == {"Version": 1, "not_a_key": {"Platform": "x"},
                   "list": [{"Version": 2}]}


# --------------------------------------------------------------------------
# mf_*.hg metadata
# --------------------------------------------------------------------------

@pytest.mark.parametrize("size", [432, 384, 104],
                         ids=["5.50+", "5.00 (mf_save6)", "pre-3.60"])
def test_meta_xxtea_round_trip(size):
    plain = bytearray(size)
    struct.pack_into("<II", plain, 0, META_MAGIC, 2004)
    struct.pack_into("<II", plain, 0x38 if size >= 0x40 else 8, 2602956, 349603)
    slot = meta_slot("save9.hg")
    blob = meta_encode(bytes(plain), slot)
    assert blob != bytes(plain)
    assert meta_decode(blob, slot) == bytes(plain)


def test_meta_decode_refuses_the_wrong_slot():
    plain = bytearray(432)
    struct.pack_into("<II", plain, 0, META_MAGIC, 2004)
    blob = meta_encode(bytes(plain), meta_slot("save9.hg"))
    with pytest.raises(ValueError):
        meta_decode(blob, meta_slot("save3.hg"))


def test_meta_encode_refuses_a_ragged_plaintext():
    with pytest.raises(ValueError):
        meta_encode(b"\x00" * 431, 2)


@pytest.mark.parametrize("name,slot", [
    ("save.hg", 2), ("save2.hg", 3), ("save9.hg", 10), ("save10.hg", 11),
    ("mf_save.hg", 2), ("mf_save10.hg", 11),
    (os.path.join("x", "y", "mf_save6.hg"), 7),
])
def test_meta_slot(name, slot):
    assert meta_slot(name) == slot


@pytest.mark.parametrize("name", ["accountdata.hg", "save.txt", "mf_x.hg", ""])
def test_meta_slot_refuses_a_non_save_name(name):
    with pytest.raises(ValueError):
        meta_slot(name)


def test_meta_update_sizes_changes_only_the_sizes(tmp_path):
    plain = bytearray(432)
    struct.pack_into("<II", plain, 0, META_MAGIC, 2004)
    struct.pack_into("<II", plain, 0x38, 111, 222)
    plain[0x58:0x58 + 5] = b"Test\x00"
    p = tmp_path / "mf_save9.hg"
    p.write_bytes(meta_encode(bytes(plain), meta_slot("save9.hg")))
    codec.meta_update_sizes(str(p), 333, 444)
    m = codec.meta_read(str(p))
    assert (m["size_decompressed"], m["size_disk"]) == (333, 444)
    assert m["save_name"] == "Test"
    struct.pack_into("<II", plain, 0x38, 333, 444)
    assert m["plain"] == bytes(plain)


# --------------------------------------------------------------------------
# The codec's own self-test, so the user-facing command cannot rot
# --------------------------------------------------------------------------

def test_codec_selftest_passes(capsys):
    codec.selftest()
    assert "all checks passed" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Real saves, opt-in, read-only
# --------------------------------------------------------------------------

def _saves(folder):
    out = sorted(p for p in glob.glob(os.path.join(folder, "save*.hg"))
                 if not os.path.basename(p).startswith("mf_"))
    if not out:
        raise AssertionError("no save*.hg in %r" % folder)
    return out


def test_real_saves_round_trip_byte_exact(real_saves):
    """Read-only. For every save in the folder: decode the container, decode
    the JSON, re-encode both halves and compare bytes.

    This is the check that licenses the write path. `escape_slash` is chosen by
    looking at the bytes we read, not guessed: pre-Frontiers builds wrote
    `MODELS\\/COMMON\\/`, later ones did not, and both must reproduce.
    """
    report = []
    for path in _saves(real_saves):
        payload, info = read_payload(path)
        esc = b"\\/" in payload
        doc = loads(payload)
        assert dumps(doc, escape_slash=esc) == payload, path
        for greedy in (False, True):
            blob = frame_payload(payload, greedy=greedy)
            assert read_payload_bytes(blob)[0] == payload, (path, greedy)
        if info["framed"]:
            assert read_payload_bytes(open(path, "rb").read())[0] == payload
        report.append("%-12s framed=%-5s blocks=%-3d payload=%-9d escape_slash=%s"
                      % (os.path.basename(path), info["framed"], info["blocks"],
                         len(payload), esc))
    print("\n" + "\n".join(report))


def test_real_metadata_re_encrypts_identically(real_saves):
    """An mf_ file we cannot reproduce byte-for-byte is an mf_ file we must not
    rewrite, so this gate is checked before the size fields are ever touched."""
    found = 0
    for path in sorted(glob.glob(os.path.join(real_saves, "mf_save*.hg"))):
        raw = open(path, "rb").read()
        m = codec.meta_read(path)
        assert meta_encode(m["plain"], meta_slot(path)) == raw, path
        found += 1
        save = os.path.join(real_saves, os.path.basename(path)[3:])
        if os.path.exists(save):
            payload, info = read_payload(save)
            assert m["size_disk"] == info["disk"], path
            assert m["size_decompressed"] == len(payload), path
        print("%-14s len=%-4d format=%-5d size_disk=%-9d" %
              (os.path.basename(path), m["raw_len"], m["format"], m["size_disk"]))
    assert found, "no mf_save*.hg in %r" % real_saves
