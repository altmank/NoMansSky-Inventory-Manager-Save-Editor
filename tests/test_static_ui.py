"""The six defects of `design/qa2-2026-09-15.md`, as source assertions.

The page is one script and one stylesheet with no build step and no test
runner, so a browser measured every one of these fixes and the numbers are in
the commit message. What is left for the suite is the part that can rot
silently: the *rule* each fix encodes. A count beside a switch must be the
number of things the switch moves; a sentence a player is asked to agree to
must name a container the way the rest of the page names one; a border that is
the whole of a control's shape must carry the token that passes 1.4.11.

These read `nms_sorter/static/` as text on purpose. A regex over a source file
is a weak test of behaviour and a strong test of intent: it fails when someone
puts the container key back into the confirm, or the slot count back into the
label, which is exactly how both defects arrived.
"""
import os
import re

import pytest


HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(os.path.dirname(HERE), "nms_sorter", "static")


def _read(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def js():
    return _read("app.js")


@pytest.fixture(scope="module")
def css():
    return _read("app.css")


# ------------------------------------------------------------------ item 4


def test_what_the_save_section_does_not_draw_is_one_sentence(js):
    """The "Show unallocated" checkbox is gone, and this is what took its
    place.

    Its whole population was grids with no cells: Waypoint (4.0) merged a
    starship's and the exosuit's cargo grid into the main one and gave no way
    to put a cell back, so those can never hold anything and the server does
    not send them. The box switched between six cards of `0 / 0 cells` and
    none, and its count was a promise about a switch that had nothing to
    switch. What it hid is now stated once, and the slots nobody owns -- which
    it never drew at either setting -- are still in that sentence.
    """
    assert "hide-unalloc" not in js, "the checkbox and its listener are gone"
    block = re.search(r"function renderSaveHidden\(\) \{(?P<body>.*?)\n\}",
                      js, re.S)
    assert block, "renderSaveHidden moved or was rewritten"
    body = block.group("body")
    assert "S.save.unowned_slots" in body, \
        "the slots nobody owns are counted by the server, not drawn"
    assert "cargo grids the game merged into the main one" in body
    assert "technology grids" in body
    # and the pool the chips count is the drawn set, with no third term
    pool = re.search(r"function savePool\(\) \{(?P<body>.*?)\n\}", js, re.S)
    assert pool and "c => !c.slot_empty" in pool.group("body"), pool
    assert "showUn" not in js, "no checkbox state is read anywhere"


# ------------------------------------------------------------------ item 2


def test_the_chip_remove_confirm_names_the_container_not_its_key(js):
    """QA 2 item 14: `Stop routing Raw Resources to Raw Resources (chest1)?`
    printed an internal key in the one sentence the player is asked to agree
    to. Visible prose uses `contOpt`'s rule -- the label, and `label key` only
    where two containers in this save read the same name -- while the `title`
    and the accessible name keep the key, which is the rule the rest of the
    page already follows.
    """
    line = re.search(r'^\s*`Stop routing \$\{b\.label\} to (?P<what>[^`]*)`,$',
                     js, re.M)
    assert line, "the chip-remove confirm sentence moved or was rewritten"
    assert line.group("what") == "${contSay(storeKey)}?"
    # and `contSay` is `contOpt`'s rule, from a key
    fn = re.search(r'const contSay = key => \{(?P<body>.*?)\n\};', js, re.S)
    assert fn and "contOpt(c)" in fn.group("body")
    # the attribute keeps the key: that is deliberate, not an oversight
    assert ('x.title = `stop routing ${b.label} to ${contPhrase(storeKey)}`'
            in js)


# ------------------------------------------------------------------ item 5


def test_no_option_in_the_page_is_joined_with_two_spaces(js):
    """QA 2, other findings 6: `Stellar Extractor 1  (drain only)` on all
    fourteen core options, from a join that carried two spaces.
    """
    assert '"  (drain only)"' not in js
    assert '(c.drain_only ? " (drain only)" : "")' in js
    # nothing else in the script builds visible text out of a double space
    doubles = [m.group(0) for m in re.finditer(r'"[^"\n]*\S  \S[^"\n]*"', js)]
    assert doubles == [], doubles


# ------------------------------------------------------------------ item 6


def test_home_and_end_reach_the_ends_of_a_row_from_a_cell(js):
    """QA 2 item 6: with focus on a cell, Home and End did nothing at all and
    the scroller moved under a focus ring that stayed put. ARIA's grid pattern:
    unmodified they are the ends of the row, with Control the ends of the grid.
    """
    fn = re.search(r'function destGridKeys\(tr\) \{(?P<body>.*?)\n\}\n', js,
                   re.S)
    assert fn, "destGridKeys moved or was rewritten"
    body = fn.group("body")
    end = body[body.index('if (e.key === "Home" || e.key === "End")'):]
    # the cell case: the ends of this row
    assert "if (!onRow && !e.ctrlKey && inner.length) {" in end
    assert "(e.key === \"Home\" ? inner[0] : inner[inner.length - 1]).focus();" \
        in end
    # the row case, and Control from anywhere: the ends of the grid
    assert "const to = e.key === \"Home\" ? rows[0] : rows[rows.length - 1];" \
        in end
    # and a caret in a text field still owns them
    assert '(a.tagName === "INPUT" || a.tagName === "TEXTAREA")' in end
    # the handler no longer gives up when focus is not on the row
    assert "if (!onRow) return;" not in end


# ------------------------------------------------------------------ item 3


def test_the_chip_border_carries_the_three_to_one_token(css):
    """QA 2 item 9: every other border that is a control's whole shape was
    raised to `--line-3` and `.chip` was left on `--line-2` -- 1.56:1 for a
    category chip, 1.68:1 for a save filter chip, over fills about 1.09:1
    apart from the card behind them. Measured after this change: 3.33:1 and
    3.58:1 against the backdrop, 3.13:1 against the chip's own fill.
    """
    rule = re.search(r'\n\.chip\{(?P<body>[^}]*)\}', css)
    assert rule, "the .chip rule moved or was renamed"
    assert "border:1px solid var(--line-3)" in rule.group("body")
    assert "--line-2" not in rule.group("body")


# ------------------------------------------------------------------ item 1


def test_a_rule_sentence_wraps_rather_than_clipping_a_control(css):
    """QA 2 item 13: at 1280 the Rules column is 659 px while the viewport is
    over the 1100 px wrap threshold, so the longest sentence laid out at 781 px
    inside an `overflow-x:hidden` host and rule 5's cap input sat 106 px past
    the clip edge -- present, labelled, keyboard-reachable, invisible to a
    mouse. The line wraps on the box instead of on the window.
    """
    rule = re.search(r'\n\.say\{(?P<body>[^}]*)\}', css)
    assert rule, "the .say rule moved or was renamed"
    assert "flex-wrap:wrap" in rule.group("body")
    assert "flex-wrap:nowrap" not in rule.group("body")
    # a flex line breaks on base sizes before it shrinks anything, so the two
    # controls that can give way are sized from a small basis and grow back
    says = re.search(r'\.say select\.says\{(?P<body>[^}]*)\}', css)
    assert says and "flex:1 1 96px" in says.group("body")
    tok = re.search(r'\.say \.tok\{(?P<body>[^}]*)\}', css)
    assert tok and "flex:1 1 112px" in tok.group("body")
    # and the viewport rule that used to own the wrap no longer does
    assert not re.search(
        r'@media \(max-width:1100px\)\{\s*\.say\{flex-wrap:wrap\}', css)


# --------------------------------------------------- QA 3: two leaked strings

HERE_PKG = os.path.join(os.path.dirname(HERE), "nms_sorter")

#: `prettyWhy`'s boundary, lifted out of the script so the assertion below is
#: run against the rule the page actually applies rather than a copy of it.
JS_KEY_BOUNDARY = re.compile(
    r'new RegExp\(\s*"(?P<pre>[^"]*)"\s*\+\s*c\.key\s*\+\s*"(?P<post>[^"]*)"')


def test_the_decided_by_column_never_prints_a_container_key(js):
    r"""QA 3: `Raw Resources -> Raw Resources (keep 500 in suit)`.

    `prettyWhy` has rewritten a bare key into the container's name since QA 2,
    but its boundary was a list of the separators somebody remembered --
    `(^|[\s>])key(?=$|[\s,.;])` -- and the planner brackets every qualifier
    it writes. A key against `(` or `)` was outside the class, which is two of
    the three places the column can print one, so `suit` reached the screen in
    the prose a player is asked to read before pressing Apply. A word boundary
    is the rule now, and it is asserted against the planner's own sentences.
    """
    m = JS_KEY_BOUNDARY.search(js)
    assert m, "prettyWhy's key boundary moved or was rewritten"
    rx = re.compile(m.group("pre") + "suit" + m.group("post"))
    said = ["Raw Resources -> chest1 (keep 500 in suit)",
            "rule 2: keep 500 in suit, the surplus falls through to the "
            "bucket rules",
            "Raw Resources -> chest1 (overflows to chest7, suit)",
            "suit -> chest1",
            "-> suit"]
    for sentence in said:
        assert rx.sub(lambda mm: mm.group(1) + "Exosuit", sentence) != sentence, \
            sentence
    # and a key is still never matched inside a longer key
    assert rx.search("suit_cargo is full") is None
    assert rx.search("packed into suitcase") is None
    # the key is not lost, it moves to the attribute
    assert "d.title = whySentence(x);" in js
    assert 'd.title = "limit: " + whySentence(x);' in js


def test_the_planner_still_brackets_the_key_the_column_rewrites():
    """The other half of the test above: the sentence it is asserted against
    is the sentence the planner writes. If the planner stops bracketing its
    qualifiers the boundary is over-specified rather than wrong, and this says
    so instead of leaving a passing test measuring nothing.
    """
    with open(os.path.join(HERE_PKG, "planner.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert '"keep %d in %s" % (d["keep"], d["keep_in"] or src_key)' in src
    assert '(" (" + ", ".join(bits) + ")") if bits else ""' in src
    assert '"overflows to " + ", ".join(d["dsts"][1:])' in src


def test_no_gate_label_prints_a_plan_fingerprint(js):
    """QA 3: the Apply gate read `Plan 67abcb3f is fixed`.

    A digest is not a player's word for anything, and "is fixed" reads as a
    repair. What the gate asserts is that the printed plan belongs to the save
    as it was read -- apply re-plans from the exact bytes and refuses on any
    difference -- so that is what it says. The fingerprint stays reachable as
    an attribute, and in full on the plan's meta line.
    """
    assert "Plan ${planShort} is fixed" not in js
    assert "planShort" not in js, "the short-digest variable is gone"
    assert '"Plan pinned to the save as it was read"' in js
    # nothing in the script pastes a digest after the word "plan" any more
    assert not re.search(r'[Pp]lan \$\{[^}]*(?:fingerprint|planShort)', js), \
        "a plan fingerprint is back in visible text"
    assert not re.search(r'[Pp]lan \$\{S\.plan\.fingerprint\}', js)
    # the digest is an attribute on the gate, from the fourth column of the row
    assert '"the fingerprint apply compares: "' in js
    assert "rows.forEach(([ok, txt, label, title], i)" in js
    assert "if (title) t.title = title;" in js
    # and the dry-run toast, which has no attribute to hide one in, says what
    # happened instead
    assert '"plan printed: "' in js
