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


# ------------------------------------- the Sources card, 2026-09-17 (owner)
#
# "Sources ui is unusable, card is too small and trying to delete a source
# causes a confirm ui that you can't even read." Measured at 1280x720 before
# the fix: `#sources` was 63 px of client height over 448 px of content, one
# of thirteen rows, and the remove confirmation was a 73 px column of wrapped
# words 292 px tall with 239 px of itself outside that scroller. After: 252 px
# and seven rows, the confirmation 283x71 with nothing clipped.


@pytest.fixture(scope="module")
def html():
    return _read("index.html")


def test_a_source_row_carries_a_remove_control_and_one_readable_sentence(js):
    """The confirmation names the container the way the rest of the page names
    one -- the label, its key only on a collision -- and says one thing.

    It also anchors on the remove button rather than on the cell that holds
    it: `close()` puts the focus back on the anchor, and a `td` cannot take
    it, so cancel used to drop focus to `<body>`.
    """
    fn = re.search(r"function renderSources\(\) \{(?P<body>.*?)\n\}", js, re.S)
    assert fn, "renderSources moved or was rewritten"
    body = fn.group("body")
    # a remove control on every row, with a stable focus key per index
    assert 'const x = el("button", "iconbtn del");' in body
    assert "fk(x, `src-${i}-remove`);" in body
    # the confirmation: anchored on that control, one sentence, the label in it
    assert "confirmInline(x," in body, \
        "the confirmation anchors on the remove control"
    assert "`Stop taking items from ${contSay(key)}?`" in body
    # one sentence: no second clause, and `contSay` is the visible-prose rule
    sentence = re.search(r"`Stop taking items from \$\{contSay\(key\)\}\?`",
                         body)
    assert sentence and body.count("Stop taking items") == 1
    assert "Stop draining" not in js, "the two-sentence version is gone"
    # the key stays in the attributes, where it costs no layout
    assert "nm.title = c ? `${name} (${key})` : name;" in body
    # and the buttons say what the press does
    assert 'const REMOVE_YN = { yes: "yes, remove", no: "cancel" };' in js
    assert re.search(r"undoable\(`\$\{name\} is no longer a source`, before\);"
                     r"\s*\}, null, null, REMOVE_YN\);", body)


def test_a_confirmation_in_a_list_goes_under_the_whole_row(js, css):
    """`insertAdjacentElement("afterend")` on a control in a `tr` makes an
    anonymous table cell as wide as that cell, which is how a 33-character
    question became 73 px wide and 292 px tall inside a 63 px scroller. The
    box goes under the row now, in one spanning cell.
    """
    fn = re.search(r"function confirmRowHost\(anchor\) \{(?P<body>.*?)\n\}",
                   js, re.S)
    assert fn, "confirmRowHost moved or was rewritten"
    host = fn.group("body")
    assert 'const tr = anchor.closest("tr");' in host
    assert "cells: tr.cells.length" in host
    # the rule row is the other one that laid the box over its own controls,
    # and it is the only block selector here: `#store-grid` is a role=grid
    # whose children have to be rows, so the chip confirmation stays in the
    # chip, where it measured 372x71 with nothing clipped.
    assert 'const row = anchor.closest(".rule");' in host
    # the insertion: a spanning cell, and the row is what gets removed again
    cf = re.search(r"function confirmInline\(anchor, question, onYes, tick, "
                   r"whyOff, labels\) \{(?P<body>.*?)\n\}", js, re.S)
    assert cf, "confirmInline's signature moved"
    box = cf.group("body")
    assert 'const tr = el("tr", "confirm-row");' in box
    assert "td.colSpan = host.cells;" in box
    assert 'box.closest("tr.confirm-row")' in box
    # Escape cancels, and cancel hands the focus back to the control
    assert 'if (e.key === "Escape") { e.stopPropagation(); close(); }' in box
    assert "if (anchor.isConnected) anchor.focus();" in box
    # the sentence is scrolled to before the focus lands on a button
    assert 'box.scrollIntoView({ block: "nearest" });' in box
    # and the cell is not padded into a second layout
    assert "tr.confirm-row>td.confirm-cell{padding:0;border-bottom:0}" in css


def test_the_no_destination_shelf_is_the_one_behind_a_fold(html, css, js):
    """Both lists cannot fit: at 1280x720 the column is 431 px and the two
    cards wanted 468. The rarer action folds -- routing a category from the
    shelf -- not the list the owner works down every session.
    """
    assert re.search(r'<details class="nd-fold" id="nd-fold">\s*<summary>\s*'
                     r'<h2>No destination</h2>\s*'
                     r'<span class="hint" id="unrouted-count"></span>\s*'
                     r'</summary>\s*<div class="bd" id="bucket-shelf"></div>',
                     html), "the fold, its summary or the shelf moved"
    # closed by default: no `open` attribute on it
    assert '<details class="nd-fold" id="nd-fold" open' not in html
    # the card is sized by its content now, and the shelf opens to a scroller
    assert "#tab-categories .colstack>.card.fixed{flex:none}" in css
    assert re.search(r"#bucket-shelf\{flex:none;min-height:34px;"
                     r"max-height:106px;overflow-y:auto;", css)
    # the Sources list fills what is left, with a floor of two rows
    assert "#sources.scrollin{min-height:68px}" in css
    # and anything that sends the player into the shelf opens it first: a
    # control inside a closed `details` cannot take the focus
    assert re.search(r"function openShelf\(\) \{\s*const f = \$\(\"#nd-fold\"\);"
                     r"\s*if \(f && !f\.open\) f\.open = true;", js)
    assert 'if (/^unrouted-/.test(key)) openShelf();' in js
    # the drop target is the fold, so the drag gesture survives it being shut
    assert "dropzone(fold || shelf, b => unrouteEverywhere(b, fold || shelf));" \
        in js


def test_no_row_in_the_sources_list_is_under_the_sheet_floor(css):
    """24 px is this sheet's own floor for anything pressable (WCAG 2.5.8) and
    a source row carries four buttons. The rows measured 34 px and the shelf's
    33 px after the fix; nothing in the column was shrunk to buy the space.
    """
    assert "table.srctable tr{height:30px}" in css
    assert "button,.btn,.iconbtn,.chip .x,.del,.linkbtn,.fchip{min-height:24px}" \
        in css
    # the space came off the chrome, not the rows
    assert "#tab-categories .colstack>.card{padding:var(--s2) var(--s3)}" in css
    assert re.search(r"#tab-categories \.colstack>\.card>\.hd,\s*"
                     r"#tab-categories \.colstack>\.card \.nd-fold>summary\{"
                     r"padding:4px var\(--s3\)\}", css)
