"""Property tests for the codec, under `hypothesis`.

Not part of the push run: `pytest tests -q --ignore=tests/test_fuzz.py` is what
CI does on every commit, and this file runs nightly at 10,000 examples per
property. Two profiles:

    HYPOTHESIS_PROFILE=dev   200 examples   (the default, a few seconds)
    HYPOTHESIS_PROFILE=ci  10000 examples   (the nightly job)

    python -m pytest tests/test_fuzz.py -q
    HYPOTHESIS_PROFILE=ci python -m pytest tests/test_fuzz.py -q \
        --hypothesis-seed=0 -p no:randomly

The four properties, in the order the codec applies them to a save:

  1. identity      bytes that came out of `dumps` survive `loads` and `dumps`
                   unchanged, and arbitrary bytes either parse or raise
                   `ValueError` -- never anything else
  2. framing       `read_payload_bytes(frame_payload(p)) == p`, greedy and
                   literal, across the 0x80000 block boundary
  3. LZ4           `lz4_block_decompress(lz4_block_compress(x)) == x`
  4. XXTEA         `meta_decode(meta_encode(p, slot), slot) == p`

Property 1 is the one that matters: it is the guarantee step 4 of the write
sequence checks on a real save before any edit, and the only reason this tool
is allowed to rewrite a file at all. `deadline=None` throughout, because the
LZ4 matcher and the JSON encoder are pure Python and a 300 KB example is
legitimately slow.

What this does *not* prove is in `docs/SAFETY.md`: a property test explores
the input space the strategies describe, so it can only find a bug the
strategies can express.
"""
import os
import struct

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from nms_sorter.codec import (CHUNK, META_MAGIC, RawFloat, dumps,
                              frame_payload, loads, lz4_block_compress,
                              lz4_block_decompress, meta_decode, meta_encode,
                              read_payload_bytes)


# --------------------------------------------------------------------------
# profiles
# --------------------------------------------------------------------------

_COMMON = dict(
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large,
                           HealthCheck.filter_too_much],
)
settings.register_profile("dev", max_examples=200, **_COMMON)
settings.register_profile("ci", max_examples=10000, **_COMMON)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))

pytestmark = pytest.mark.fuzz


# --------------------------------------------------------------------------
# strategies
# --------------------------------------------------------------------------

# Every byte the game escapes with a short form or a \uXXXX form, plus DEL.
CONTROL = "".join(chr(i) for i in range(0x20)) + "\x7f"

# Characters a real save is known to carry: an ASCII item id, a CJK base name,
# accented latin, and the characters JSON has to escape.
NOTABLE = CONTROL + '"\\/' + "小溪测试基地" + "éüßÅ" + "^#_0123456789"

# `Cs` (surrogates) is excluded here and reintroduced below only through
# `surrogate_text`, because a *lone high* surrogate is not encodable by
# `surrogateescape` and cannot appear in a save: the game writes bytes, and
# the only way a str in this codec holds a surrogate is
# `bytes.decode("utf-8", "surrogateescape")`, which produces U+DC80..U+DCFF.
unicode_text = st.text(
    alphabet=st.one_of(
        st.sampled_from(NOTABLE),
        st.characters(blacklist_categories=("Cs",)),
    ),
    max_size=24)

# The procedural-item-id case: bytes that are not valid UTF-8, carried as
# surrogates. This is the class of string that makes the codec necessary.
surrogate_text = st.binary(max_size=24).map(
    lambda b: b.decode("utf-8", "surrogateescape"))

text_values = st.one_of(unicode_text, surrogate_text)


# 17 significant digits is what CPython's repr emits for a double that needs
# them, and what the game writes for a position. The literal, not the value,
# is what must survive.

def _is_float_literal(s):
    """JSON decides float-versus-int by spelling, not by intent.

    `"%.17g" % 0.0` is `"0"`, which `json` parses with `parse_int`, so it
    never reaches `RawFloat` and is not a counter-example to anything. Only
    literals JSON reads as floats belong in this strategy.
    """
    return "." in s or "e" in s or "E" in s


float_literals = st.one_of(
    st.floats(allow_nan=False, allow_infinity=False).map(float.__repr__),
    st.floats(allow_nan=False, allow_infinity=False, width=64).map(
        lambda f: "%.17g" % f),
    st.sampled_from(["0.0", "-0.0", "1.0", "1e+20", "1E-20", "3.0517578125E-05",
                     "0.10000000000000001", "-46105.5", "1.7976931348623157e+308",
                     "5e-324", "123456789012345678901234567890.0"]),
).filter(_is_float_literal)

leaves = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2 ** 63), max_value=2 ** 64),
    float_literals.map(RawFloat),
    text_values,
)

documents = st.recursive(
    leaves,
    lambda child: st.one_of(
        st.lists(child, max_size=6),
        st.dictionaries(text_values, child, max_size=6),
    ),
    max_leaves=24)


def _keys_collapse(doc):
    """True if two keys in one object encode to the same bytes.

    `"\\udcc3\\udcbf"` and `"ÿ"` both encode to `b"\\xc3\\xbf"`, so an object
    holding both serialises to a JSON object with a duplicate key, and a
    re-parse keeps only the last. That is a property of JSON, not of this
    codec, and no save contains it -- the game's keys are three ASCII
    characters. Filtered out rather than asserted away, so the report says so.
    """
    if isinstance(doc, dict):
        enc = [k.encode("utf-8", "surrogateescape") for k in doc]
        if len(set(enc)) != len(enc):
            return True
        return any(_keys_collapse(v) for v in doc.values())
    if isinstance(doc, list):
        return any(_keys_collapse(v) for v in doc)
    return False


# --------------------------------------------------------------------------
# 1. identity
# --------------------------------------------------------------------------

@given(documents)
def test_dumps_loads_is_identity_on_bytes(doc):
    """The invariant the write sequence bets a save on.

    Bytes, not objects: `loads` is allowed to return a different object graph
    (a duplicate key collapses, an int key becomes a string) as long as the
    bytes it re-emits are the same bytes.
    """
    assume(not _keys_collapse(doc))
    payload = dumps(doc)
    assert dumps(loads(payload)) == payload


@given(documents)
def test_identity_holds_with_escaped_slashes(doc):
    """Pre-Frontiers saves escape every `/`. Same property, other flag."""
    assume(not _keys_collapse(doc))
    payload = dumps(doc, escape_slash=True)
    assert dumps(loads(payload), escape_slash=True) == payload


@given(documents)
def test_the_trailing_nul_is_optional(doc):
    assume(not _keys_collapse(doc))
    with_nul = dumps(doc, nul=True)
    without = dumps(doc, nul=False)
    assert with_nul == without + b"\x00"
    assert loads(with_nul) == loads(without)


@given(st.binary(max_size=256))
def test_loads_of_arbitrary_bytes_raises_cleanly_or_succeeds(raw):
    """No surprise exception type out of the one function that touches
    untrusted bytes. `json.JSONDecodeError` is a `ValueError` subclass, so
    one clause covers both."""
    try:
        loads(raw)
    except ValueError:
        pass


@given(st.text(alphabet=st.sampled_from("[]{},:\"0123456789.eE+-\\/ntruefals \t"),
               max_size=120))
def test_loads_of_json_shaped_text_raises_cleanly_or_succeeds(text):
    """The same property aimed at the boundary: text that looks enough like
    JSON to reach the interesting parts of the parser."""
    try:
        loads(text.encode("utf-8"))
    except ValueError:
        pass


@given(float_literals)
def test_float_literals_survive_verbatim(literal):
    """A float is reproduced as the game spelled it, not as CPython would.

    Built as JSON text and taken through `loads`, because that is the
    direction a save arrives in: the literal in the file is the ground truth
    and `RawFloat` exists to carry it.
    """
    payload = ('{"a":%s}' % literal).encode("utf-8") + b"\x00"
    doc = loads(payload)
    assert isinstance(doc["a"], RawFloat)
    assert doc["a"].raw == literal
    assert dumps(doc) == payload


@given(st.lists(float_literals, min_size=1, max_size=20))
def test_float_arrays_survive_verbatim(literals):
    """The shape a position actually has in a save."""
    payload = ('{"wMC":[%s]}' % ",".join(literals)).encode("utf-8") + b"\x00"
    assert dumps(loads(payload)) == payload


@pytest.mark.parametrize("depth", [1, 2, 50, 199, 200])
def test_deep_nesting(depth):
    payload = (b'{"a":' * depth) + b"1" + (b"}" * depth) + b"\x00"
    assert dumps(loads(payload)) == payload


def test_nesting_past_the_interpreter_limit_is_a_recursion_error():
    """Documented, not asserted away: `loads` on absurdly nested input raises
    `RecursionError`, which is not a `ValueError`.

    Every caller in this package feeds `loads` the contents of a save file
    that the framing layer already accepted, so the input is not attacker
    chosen. It is recorded here so the next person to point `loads` at an
    HTTP request body knows what they are taking on.
    """
    payload = (b"[" * 20000) + (b"]" * 20000) + b"\x00"
    with pytest.raises(RecursionError):
        loads(payload)


def test_ten_thousand_slots():
    """`Slots` at a size no real container reaches, to prove the encoder is
    linear and the identity does not depend on document size."""
    slot = {"Id": "^CATALYST1", "Amount": 9999, "MaxAmount": 9999,
            "DamageFactor": RawFloat("0.0"),
            "Index": {"X": 0, "Y": 0}}
    doc = {"Slots": [dict(slot, Amount=i) for i in range(10000)]}
    payload = dumps(doc)
    assert len(payload) > 500000
    assert dumps(loads(payload)) == payload


@given(st.integers(min_value=0, max_value=0x1F))
def test_every_control_byte_round_trips_and_is_upper_case_escaped(code):
    """The game writes `\\u001F`, CPython writes `\\u001f`. Same string,
    different bytes, and a save carrying one in a procedural id would fail the
    identity check if the case were wrong."""
    doc = {"Id": "^A" + chr(code) + "B"}
    payload = dumps(doc)
    assert dumps(loads(payload)) == payload
    short = {0x08: b"\\b", 0x09: b"\\t", 0x0A: b"\\n",
             0x0C: b"\\f", 0x0D: b"\\r"}
    if code in short:
        assert short[code] in payload
    else:
        assert (b"\\u%04X" % code) in payload
        if ("%04X" % code) != ("%04x" % code):    # only 0x0A..0x1F have a letter
            assert (b"\\u%04x" % code) not in payload


# --------------------------------------------------------------------------
# 2. framing
# --------------------------------------------------------------------------

@given(st.binary(max_size=4096), st.booleans())
def test_frame_then_read_is_identity(payload, greedy):
    framed = frame_payload(payload, greedy=greedy)
    back, info = read_payload_bytes(framed)
    assert back == payload
    if payload:
        assert info["framed"] is True


@given(st.binary(max_size=4096))
def test_plaintext_framing_is_a_pass_through(payload):
    """`compress=False` is what a pre-Frontiers save looks like."""
    assert frame_payload(payload, compress=False) == payload
    back, info = read_payload_bytes(payload)
    if not payload[:4] == struct.pack("<I", 0xFEEDA1E5):
        assert back == payload
        assert info["framed"] is False


@pytest.mark.parametrize("size", [CHUNK - 1, CHUNK, CHUNK + 1, 2 * CHUNK + 1])
@pytest.mark.parametrize("greedy", [True, False])
def test_block_boundary_sizes(size, greedy):
    """The block size is 0x80000 and the interesting sizes are the three
    around it plus one that needs three blocks."""
    payload = _mixed_bytes(size)
    assert len(payload) == size
    framed = frame_payload(payload, greedy=greedy)
    back, info = read_payload_bytes(framed)
    assert back == payload
    assert info["blocks"] == (size + CHUNK - 1) // CHUNK


def test_trailing_garbage_after_the_last_block_is_refused():
    framed = frame_payload(b"x" * 100)
    with pytest.raises(ValueError):
        read_payload_bytes(framed + b"junk")


def test_a_bad_block_magic_is_refused():
    framed = bytearray(frame_payload(b"x" * 100) * 2)
    framed[16 + len(frame_payload(b"x" * 100)) - 16] ^= 0xFF
    with pytest.raises(ValueError):
        read_payload_bytes(bytes(framed))


# --------------------------------------------------------------------------
# 3. LZ4
# --------------------------------------------------------------------------

@given(st.binary(max_size=8192), st.booleans())
def test_lz4_round_trip(data, greedy):
    blob = lz4_block_compress(data, greedy=greedy)
    assert lz4_block_decompress(blob, len(data)) == data


@given(st.lists(st.binary(min_size=1, max_size=8), min_size=1, max_size=40),
       st.integers(min_value=1, max_value=400))
def test_lz4_round_trip_on_repetitive_data(chunks, repeats):
    """Repetition is where a matcher earns its keep and where an off-by-one in
    a match offset or length shows up."""
    data = (b"".join(chunks) * repeats)[:300000]
    blob = lz4_block_compress(data)
    assert lz4_block_decompress(blob, len(data)) == data


@pytest.mark.parametrize("size", [0, 1, 12, 13, 65535, 65536, 65537, 300000])
def test_lz4_round_trip_at_sizes(size):
    for data in (_mixed_bytes(size), b"\x00" * size, (b"abcd" * size)[:size]):
        for greedy in (True, False):
            blob = lz4_block_compress(data, greedy=greedy)
            assert lz4_block_decompress(blob, len(data)) == data


def test_lz4_compresses_repetitive_data_at_all():
    """A matcher that never matches would pass every round trip above. This is
    the test that says it is doing the job."""
    data = b"^CATALYST1" * 20000
    assert len(lz4_block_compress(data)) < len(data) // 20
    assert len(lz4_block_compress(data, greedy=False)) > len(data)


# --------------------------------------------------------------------------
# 4. XXTEA metadata
# --------------------------------------------------------------------------

@given(st.sampled_from([432, 384, 104]),
       st.integers(min_value=1, max_value=12),
       st.randoms(use_true_random=True))
def test_meta_round_trip(size, slot, rnd):
    plain = bytearray(bytes(rnd.getrandbits(8) for _ in range(size)))
    struct.pack_into("<I", plain, 0x00, META_MAGIC)
    plain = bytes(plain)
    assert meta_decode(meta_encode(plain, slot), slot) == plain


@given(st.integers(min_value=1, max_value=12), st.integers(min_value=1, max_value=12))
def test_meta_decode_with_the_wrong_slot_key_is_refused(a, b):
    assume(a != b)
    plain = bytearray(432)
    struct.pack_into("<I", plain, 0x00, META_MAGIC)
    blob = meta_encode(bytes(plain), a)
    with pytest.raises(ValueError):
        meta_decode(blob, b)


@given(st.binary(max_size=16).filter(lambda b: len(b) % 4 or len(b) < 8))
def test_meta_decode_refuses_a_bad_length(raw):
    with pytest.raises(ValueError):
        meta_decode(raw, 10)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _mixed_bytes(n):
    """`n` bytes that are neither random nor uniform.

    A purely random buffer never matches and a uniform one always does; the
    LZ4 matcher's interesting paths need both in one input. Deterministic, so
    a failure at a boundary size reproduces.
    """
    if n <= 0:
        return b""
    pattern = b"^CATALYST1,{}[]0123456789\x00\x1f\xff\xfe"
    out = bytearray()
    x = 0x12345678
    while len(out) < n:
        x = (1103515245 * x + 12345) & 0xFFFFFFFF
        if x & 0x10000:
            out += pattern
        else:
            out += struct.pack("<I", x)
    return bytes(out[:n])
