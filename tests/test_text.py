"""`nms_sorter.text`: display escaping for strings that are not valid UTF-8.

Converted from `selftest_legacy.py`, "text handling". One assertion changed
meaning on the way: the legacy check was `A or B` where A was a hand-built
string that never matched and B was `"\\x80" in ...`, so it could not fail for
the reason it claimed. The exact escape is asserted here instead.
"""
from nms_sorter import text
from tools.make_fixture import PROC_ID

# `^\x80\x80\xfd62\x95#03535` as the display layer must render it: every raw
# byte as an upper-case \xNN, everything printable left as itself. `#` is 0x23,
# above 0x20, so it stays a literal `#`.
PROC_ID_ESCAPED = "^\\x80\\x80\\xFD62\\x95#03535"


def test_non_utf8_id_is_escaped_for_display():
    assert text.safe(PROC_ID) == PROC_ID_ESCAPED, \
        "a non-UTF-8 id is escaped for display: %r" % text.safe(PROC_ID)


def test_non_utf8_id_round_trips_through_its_token():
    assert text.from_token(text.token(PROC_ID)) == PROC_ID, \
        "a non-UTF-8 id round-trips through its display token"


def test_real_utf8_is_left_alone():
    assert text.safe("Fixture基地") == "Fixture基地", "real UTF-8 is left alone"


def test_stem_strips_caret_and_hash():
    assert text.stem("^UP_SGUN4#86554") == "UP_SGUN4", "stem strips caret and hash"
