"""Text that came out of a save file, on its way to a browser.

A save holds three kinds of string and the difference matters:

  * plain ASCII                     -- most ids and every key
  * real multi-byte UTF-8           -- base, container, ship and player names
                                       (186 non-ASCII bytes in the operator's
                                       save: CJK, Cyrillic, Greek)
  * bytes that are not UTF-8 at all -- procedurally generated item ids, e.g.
                                       "^" + 80 80 FD 36 32 95 23 30 33 35 33 35

The codec decodes with `surrogateescape`, so the third kind arrives as a string
carrying lone surrogates in U+DC80..U+DCFF. Those are legal Python strings and
they re-encode to the original bytes exactly -- which is the whole point -- but
they are not legal JSON text and json.dumps would emit a lone `\\udc80` escape.

So: `safe()` is the *display* form (surrogates shown as \\xNN, control bytes
escaped, real UTF-8 left alone), and `token()` is the *identity* form, a
base64 of the raw bytes that survives a round trip through the browser without
anybody having to look at it. Nothing in this package ever puts a raw save
string into a JSON response.
"""
import base64


def safe(s):
    """Display form. Real UTF-8 survives; raw bytes become \\xNN."""
    if s is None:
        return None
    if isinstance(s, bytes):
        s = s.decode("utf-8", "surrogateescape")
    out = []
    for ch in s:
        o = ord(ch)
        if 0xDC80 <= o <= 0xDCFF:          # surrogateescape of a raw byte
            out.append("\\x%02X" % (o - 0xDC00))
        elif o < 0x20 or o == 0x7F:
            out.append("\\x%02X" % o)
        else:
            out.append(ch)
    return "".join(out)


def raw_bytes(s):
    return s.encode("utf-8", "surrogateescape")


def token(s):
    """Opaque identity for a string that may not be valid UTF-8."""
    return base64.urlsafe_b64encode(raw_bytes(s)).decode("ascii")


def from_token(t):
    return base64.urlsafe_b64decode(t.encode("ascii")).decode("utf-8", "surrogateescape")


def strip_caret(s):
    return s[1:] if s.startswith("^") else s


def stem(s):
    """Item id without the caret and without the per-roll `#hash` suffix."""
    s = strip_caret(s)
    i = s.find("#")
    return s[:i] if i >= 0 else s


def is_clean_ascii(s):
    return all(0x20 <= ord(c) < 0x7F for c in s)
