"""P5-9: the documents are tested like code.

Four things are checked, and each one exists because a document drifted from
the code at least once during development (GOAL.md section 1.8 is the list).

1. **Every JSON example is loaded and validated.** A `json` fence in
   `docs/RULES.md` or `docs/GUIDE.md` carrying `"config_version"` is a
   configuration file, so it goes through `config.migrate()` and then
   `config.validate()` against the synthetic fixture's container keys. Zero
   errors. Warnings are allowed and expected: a minimal example routes one
   category and therefore raises the unrouted-category warning, which
   `docs/RULES.md` says in prose right above the examples.

2. **Every `###` heading in `docs/TROUBLESHOOTING.md` is a sentence the code
   can actually say.** The heading is the refusal verbatim with `...` where a
   value renders, so the check is a two-sided wildcard match: the heading's
   `...` may stand for anything, and so may a `%s`/`%d`/`%r` in the source
   string it is matched against. A heading with no source string fails the
   build. That is the point of the ticket: a refusal whose wording changes
   without its entry changing leaves a person searching for a sentence nobody
   says any more.

3. **The reverse, softly.** Every refusal sentence a *player* can be shown
   should appear as a heading somewhere. This is printed as a gap list rather
   than failed on: a false gap here is cheap and a lane that adds a refusal
   should not go red before the documentation lane catches up. The count is
   printed so it can only go down.

   The scope is deliberately narrower than "every string the package can
   produce". `docs/TROUBLESHOOTING.md` documents what a person using the page
   can meet, and the request-shape errors -- "the request body was not valid
   JSON", "this endpoint takes a config and nothing else", "force has to be
   true or false" -- are answers to a malformed HTTP request that the page
   itself never sends. They were documented once, in an "API errors" section
   nobody reading it was the audience for, and it went. `REQUEST_SHAPE` below
   is the pattern that keeps them out of the gap list.

4. **Markdown hygiene.** No em dashes, en dashes or smart quotes anywhere in
   the documents (the house style is `--` and `"`, and a smart quote in a
   copy-pasteable JSON example is a broken example). No relative link to a file
   that does not exist.

Nothing here imports the server or touches a save. The only fixture used is
`container_keys`, which needs the synthetic save for its key list, so that the
examples are validated against a real container set rather than against an
empty one -- validating with no keys would accept `"store": "chest99"`.
"""
import ast
import io
import json
import os
import re

import pytest

from nms_sorter import config as cfgmod

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DOCS = os.path.join(ROOT, "docs")

#: the character that stands for "any run of text" on both sides of the match
WILD = "\x00"

#: `docs/*.md` plus the two documents at the repository root
MARKDOWN = ([os.path.join("docs", fn) for fn in sorted(os.listdir(DOCS))
             if fn.endswith(".md")]
            + ["README.md", "CHANGELOG.md"])

#: modules whose sentences the reverse check (3) reads. `server.py` and
#: `app.py` are in it for the sentences a page can show; `REQUEST_SHAPE` drops
#: the ones only a malformed request reaches. `cli.py`, `settings.py` and
#: `__main__.py` are out: what they say goes to a console the windowed build
#: does not have, or to the startup pages, which check 2 already covers from
#: the document's side.
SENTENCE_SOURCES = ("safety.py", "server.py", "app.py", "config.py",
                    "planner.py", "savemodel.py", "pages.py", "platform.py")

#: sentences the reverse check does not ask for a heading for: the answers to a
#: request the page never sends. Matched against the bare sentence, lower-cased.
#: Every one of these is a 400 on a body or a query parameter of the wrong
#: shape, and the only way to see one is to drive the HTTP API by hand, where
#: the sentence itself names the field.
REQUEST_SHAPE = (
    "the request body",
    "the content-length header",
    "this endpoint takes",
    "there was no config object in the request",
    "there was no settings object in the request",
    "has to be true or false",
    "has to be a whole number",
    "must be the name or path of a save file",
    "is not a token this server could have issued",
    "requests from another origin are refused",
    "bigger than the 8 mb this server accepts",
    "name the backup folder to restore",
    "name the kept configuration to put back",
    "has to be the updated stamp this page loaded",
    "restore only puts back a folder this server listed",
    "use only puts back a file this server kept",
    "the log file",
    "so the log is going to stderr",
)

#: report/plan helpers whose first two arguments read as "<step>: <message>"
#: in a heading. `r.fail("backup", "%s does not exist")` is the heading
#: `backup: ... does not exist`, and neither half is the sentence on its own.
JOINING_CALLS = ("fail", "ok", "info", "warn", "err", "note", "_note",
                 "refuse", "skip")

# A missing image is the one relative link allowed to be missing: the
# screenshots are produced by ticket P7-2 and the README links them already.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")


def _read(rel):
    with io.open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# normalising a format string into a wildcard pattern
# ---------------------------------------------------------------------------

_PCT = re.compile(r"%[-+ #0]*[0-9*]*(?:\.[0-9*]+)?[hlL]?[diouxXeEfFgGcrsa%]")

#: an HTML tag in a source string, dropped for the same reason backticks are
#: (see `_norm`). `pages.py` writes its refusals as markup -- `<p
#: class="risk">That folder holds no <code>save*.hg</code> files, ...` -- and
#: the document's heading is the sentence a person reads, with no tags in it.
#: Left in, `<code>` sat in the middle of the sentence with no wildcard to skip
#: it, so the heading matched *nothing* and the old matcher credited it to
#: `app.py`'s unrelated "there are no saveN.hg files in %s" through wildcard
#: absorption instead. Requires a lower-case tag name, so a `<` in prose ("more
#: than 8 <= n") is not a tag, and neither is `<!doctype html>` or
#: `settings.py`'s `<Settings port=...>` repr.
#:
#: It does also eat the angle-bracket placeholders in the CLI's help text --
#: `<file>`, `<os>`, `<pid>`, fourteen of them across the package -- because
#: nothing in the text tells them apart from a tag without a list of tag names.
#: Measured: no heading matches through one, and the undocumented-sentence list
#: is the same six with and without. A heading that ever wants to quote one
#: verbatim is the thing that would need the list.
_TAG = re.compile(r"</?[a-z][^>]*>")


def _norm(text):
    """`"%s is running as pid %s."` -> `"<WILD> is running as pid <WILD>."`.

    `%%` is a literal percent and is the reason this is a function and not a
    chain of `str.replace` calls.

    Backticks are dropped, because `headings()` drops them on the document's
    side: a markdown heading cannot carry a code span without the backticks
    ending up in the string being matched. Keeping them here left the two sides
    asymmetric, and the only way to document "a `fill` ceiling needs a `store`
    to apply it to" was to write `...` where the source has a word -- a heading
    nobody would search for. Stripped on both sides, the verbatim spelling is
    the one that matches.

    HTML tags go the same way, and for the same reason: the document's side has
    none, so a sentence `pages.py` renders as markup has to be reduced to its
    text or the two sides are being compared under different rules.
    """
    out = []
    pos = 0
    for m in _PCT.finditer(text):
        out.append(text[pos:m.start()])
        out.append("%" if m.group(0) == "%%" else WILD)
        pos = m.end()
    out.append(text[pos:])
    return _TAG.sub("", "".join(out)).replace("`", "")


def _static_str(node):
    """The wildcard pattern for an expression that is a string at import time.

    Handles the four shapes the source actually uses: a literal (implicit
    concatenation is already one `Constant` by the time `ast` sees it), a `+`
    chain, a `%` format, and an f-string. Anything else contributes a wildcard,
    because *something* goes there and the match must not care what.
    """
    if isinstance(node, ast.Constant):
        return _norm(node.value) if isinstance(node.value, str) else WILD
    if isinstance(node, ast.JoinedStr):
        return "".join(_static_str(v) for v in node.values)
    if isinstance(node, ast.FormattedValue):
        return WILD
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Add):
            return _static_str(node.left) + _static_str(node.right)
        if isinstance(node.op, ast.Mod):
            return _static_str(node.left)
    if isinstance(node, ast.Call):
        # `"...".format(x)` and `", ".join(...)`
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "format":
            return _static_str(fn.value)
        return WILD
    return WILD


def _is_stringish(node):
    """True for the nodes worth adding to the corpus in their own right."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    return isinstance(node, (ast.JoinedStr, ast.BinOp))


def _call_name(node):
    fn = node.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return None


def _python_strings(path):
    """(pattern, line) for every string this file can produce.

    Three kinds: literals, `+`/`%`/f-string expressions, and the `<step>:
    <message>` join that `Report.fail` and the validator's `err` produce.
    """
    with io.open(path, encoding="utf-8") as fh:
        src = fh.read()
    try:
        tree = ast.parse(src)
    except SyntaxError:                       # a 3.12-only file under 3.9
        return []
    out = []
    for node in ast.walk(tree):
        if _is_stringish(node):
            pat = _static_str(node)
            if pat and pat.strip(WILD):
                out.append((pat, getattr(node, "lineno", 0)))
        if isinstance(node, ast.Call) and _call_name(node) in JOINING_CALLS \
                and len(node.args) >= 2:
            a, b = _static_str(node.args[0]), _static_str(node.args[1])
            out.append((a + ": " + b, getattr(node, "lineno", 0)))
    return out


_JS_STR = re.compile(r"""(?<!\\)(['"`])((?:\\.|(?!\1)[^\\\n])*)\1""")
_JS_SUBST = re.compile(r"\$\{[^{}]*\}")


def _js_strings(path):
    """(pattern, line) for every string literal in a JavaScript file.

    A regex, not a parser: the standard library has no JS parser and the only
    thing needed here is the text between quotes. Template substitutions become
    wildcards; a literal that spans a line is not matched and does not need to
    be, because a refusal sentence is one line of source.
    """
    with io.open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    out = []
    for i, line in enumerate(lines, 1):
        for m in _JS_STR.finditer(line):
            body = m.group(2)
            if not body.strip():
                continue
            body = body.replace("\\n", " ").replace("\\'", "'").replace('\\"', '"')
            out.append((_norm(_JS_SUBST.sub(WILD, body)), i))
    return out


def _corpus():
    """[(pattern, "file:line")] over every string the product can print."""
    out = []
    pkg = os.path.join(ROOT, "nms_sorter")
    for base, dirs, files in os.walk(pkg):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "data")]
        for fn in sorted(files):
            path = os.path.join(base, fn)
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            if fn.endswith(".py"):
                for pat, line in _python_strings(path):
                    out.append((pat, "%s:%d" % (rel, line)))
            elif fn.endswith(".js"):
                for pat, line in _js_strings(path):
                    out.append((pat, "%s:%d" % (rel, line)))
    return out


CORPUS = _corpus()


# ---------------------------------------------------------------------------
# two-sided wildcard matching
# ---------------------------------------------------------------------------

#: the share of a heading's literal characters that has to land on *literal*
#: characters of the source string rather than be swallowed by one of its
#: wildcards.
#:
#: Most genuine matches score above 85 per cent (`This needs Python 3.9 or
#: newer and this is Python ...` scores 46/48) and the wildcard-absorption
#: false positives score 14 per cent and below, so the two clusters are far
#: apart. The floor is set by the lowest-scoring *real* match, which is
#: `verify the backup: ... could not be read from this backup (...), so this
#: backup is not one to restore from`: 0.620, because `_verify_copy` returns
#: the detail and a second call joins the step name and the tail on, so no one
#: string in the corpus carries the whole heading.
#:
#: It used to sit at 0.5 for the step-prefixed process-list refusal, whose
#: step name was eighteen characters (`game not running: `) of heading that
#: nothing in `platform.py` could supply. That step is called `game` now, so
#: the same pair scores 0.766 and the floor could come back up.
#: `_anchored` is what keeps a floor this low from being permissive:
#: absorption at the *start*, which is where a leading wildcard does its
#: damage, is rejected outright rather than scored.
MIN_LITERAL_COVER = 0.6


def _fragments(pattern):
    """The non-empty literal runs of a wildcard pattern, in order."""
    return [f for f in pattern.split(WILD) if f]


def _anchored(pattern, candidate):
    """Do the two patterns share an end that is not a wildcard?

    One half of the guard against a match made entirely of wildcard
    absorption. Either the heading opens with text the source spells out
    literally somewhere, or the source opens with text the heading spells out
    literally somewhere. Both directions are needed because either side may be
    the one that starts with an interpolated value: `... is not a container in
    this save` starts with a `%r`, and `game: %s is running as pid %s`
    starts with a literal the heading renders as `game: NMS.exe`.

    A fragment has to sit *inside a single* fragment of the other side. A run
    that straddles a wildcard boundary is exactly what this is here to reject.
    """
    pf, cf = _fragments(pattern), _fragments(candidate)
    if not pf or not cf:
        return False
    return (any(pf[0] in frag for frag in cf)
            or any(cf[0] in frag for frag in pf))


def _cover(a, b):
    """-> the most of `a`'s literal characters an embedding of `a` in `b` can
    match against literal characters of `b`, or -1 if there is no embedding.

    The same two-sided wildcard match as before, turned from "is there a path"
    into "what is the best path", because the answer to the first question is
    almost always yes: a wildcard anywhere in `b` can absorb the whole of `a`,
    so `_fits` used to report a hit whenever the anchor pre-filter let a
    candidate through. Bottom-up over `i` and `j` descending, which is the
    order the three transitions need.
    """
    la, lb = len(a), len(b)
    row_next = [0] * (lb + 1)      # i == la: `a` is exhausted, nothing left
    for i in range(la - 1, -1, -1):
        ai = a[i]
        row = [-1] * (lb + 1)
        for j in range(lb, -1, -1):
            best = -1
            if ai == WILD:
                # a wildcard in `a` matches the empty string or one more
                # character of `b`, and covers nothing either way
                best = row_next[j]
                if j < lb and row[j + 1] > best:
                    best = row[j + 1]
            elif j < lb:
                bj = b[j]
                if bj == WILD:
                    # skip the wildcard, or let it swallow this character of
                    # `a` -- which is the case that covers nothing
                    best = row[j + 1]
                    if row_next[j] > best:
                        best = row_next[j]
                elif ai == bj:
                    nxt = row_next[j + 1]
                    if nxt >= 0:
                        best = nxt + 1
            row[j] = best
        row_next = row
    return row_next[0]


def _fits(pattern, candidate):
    """Is there a string that both patterns describe, `pattern` anywhere in it?

    Both sides carry wildcards, which is what makes this more than a substring
    search: the heading `game: NMS.exe is running as pid ...` has a
    literal `NMS.exe` exactly where the source has `%s`, because the source
    interpolates `GAME_PROCESS`. A one-sided match would call that heading
    missing and the fix would be to make the heading vaguer, which is the wrong
    direction.

    `pattern` is matched free-floating: a heading is the *first sentence* of a
    message, so the source string is allowed to carry more after it.

    Two-sided wildcards on their own are too generous to mean anything, which
    is what the P5-4 documentation review found. `pattern` is matched as
    `WILD + pattern + WILD`, so the leading wildcard can walk `candidate`
    forward to a `%s`, and that `%s` then absorbs the entire heading: every
    source string carrying a single interpolation matched every heading sharing
    one eight-character run with it. `metadata: the size patch would touch a
    byte outside 0x38..0x3F` was reported as documented against `metadata is
    updatable: this save's metadata could not be decoded (%s), ...` -- a
    different refusal in a different step -- and the heading's real source
    (`safety.py`, step 10) was never the one named.

    So two further conditions, both of them about literal text:

    * the literal fragments of `candidate` have to cover at least
      `MIN_LITERAL_COVER` of `pattern`'s non-wildcard characters, so a match
      cannot be mostly absorption;
    * and `_anchored`, so it cannot be absorption at the *start*, which is
      where the leading wildcard does its damage.
    """
    if not pattern:
        return False
    if not _anchored(pattern, candidate):
        return False
    cover = _cover(WILD + pattern + WILD, candidate)
    if cover < 0:
        return False
    literal = len(pattern.replace(WILD, ""))
    return not literal or cover >= MIN_LITERAL_COVER * literal


_RUN = re.compile(r"[a-z][a-z ]{7,}")


def _anchors(pattern):
    """Long lower-case runs of a pattern, used to pre-filter the corpus.

    Without this the check is 60 headings times 6,000 strings times a dynamic
    program, which is slow enough to be skipped, and a doc test that is skipped
    is not a doc test. A run of eight lower-case letters and spaces cannot
    straddle a `%s`, so a candidate that shares none of a heading's runs cannot
    match it.
    """
    return _RUN.findall(pattern.lower())


_ELLIPSIS = re.compile(r"\s*\.\.\.\s*$")


def heading_pattern(heading):
    """A `###` heading as a wildcard pattern.

    Two conventions, both of them in the document's own preamble:

    * `...` stands for a value that renders, so it becomes a wildcard.
    * a *trailing* `...` means the sentence carries on. It is dropped rather
      than turned into a wildcard, because the match is unanchored at the end
      anyway, and because "a rule cannot have both keep and stock ..." is
      followed in the source by `;` with no space before it -- a wildcard that
      has to match the empty string *and* eat the space in front of it is a
      rule nobody would guess from reading the document.

    Backticks are dropped for the reason `_norm` drops them: `headings()`
    already strips them out of a `###` heading, so a sentence quoted verbatim
    anywhere else -- which arrives here with its code spans intact -- has to be
    normalised the same way or the two would be compared under different rules.
    """
    return _ELLIPSIS.sub("", heading).replace("...", WILD).replace("`", "")


def find_source(pattern, corpus=None, restrict=None):
    """-> "file:line" for the first string that can produce `pattern`, or None."""
    corpus = CORPUS if corpus is None else corpus
    pattern = heading_pattern(pattern)
    anchors = _anchors(pattern)
    for pat, where in corpus:
        if restrict and not where.startswith(restrict):
            continue
        if anchors and not any(a in pat.lower() for a in anchors):
            continue
        if _fits(pattern, pat):
            return where
    return None


# ---------------------------------------------------------------------------
# reading the documents
# ---------------------------------------------------------------------------

def headings(text, level=3):
    """[(heading, line)] at exactly `level` hashes, markdown escapes undone."""
    prefix = "#" * level + " "
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.startswith(prefix) or line.startswith(prefix + "#"):
            continue
        h = line[len(prefix):].strip()
        for esc in ("\\<", "\\>", "\\_", "\\*", "\\`", "\\[", "\\]"):
            h = h.replace(esc, esc[1])
        out.append((h.replace("`", ""), i))
    return out


def sections(text, level=2):
    """{heading -> [lines]} so a heading's enclosing section can be named."""
    out, current = {}, None
    for line in text.splitlines():
        if line.startswith("#" * level + " "):
            current = line[level + 1:].strip()
            out[current] = []
        elif current is not None:
            out[current].append(line)
    return out


def json_blocks(rel):
    """[(index, text, line)] for every ```json fence in a document."""
    text = _read(rel)
    out = []
    fence, buf, start = None, [], 0
    for i, line in enumerate(text.splitlines(), 1):
        if fence is None:
            if line.startswith("```"):
                fence, buf, start = line[3:].strip(), [], i
            continue
        if line.startswith("```"):
            if fence == "json":
                out.append((len(out), "\n".join(buf), start))
            fence = None
            continue
        buf.append(line)
    return out


TROUBLE = _read("docs/TROUBLESHOOTING.md")
TROUBLE_HEADINGS = headings(TROUBLE, 3)


# ===========================================================================
# 1. every JSON example loads, migrates and validates
# ===========================================================================

#: a freighter base tops out at fourteen Stellar Extractor rooms, and the
#: operator's own configuration names all fourteen as sources.
MAX_EXTRACTORS = 14


@pytest.fixture(scope="module")
def doc_container_keys(fixture_variant):
    """(every container key a document may legally name, the ones it may send to).

    Not the `container_keys` fixture in `conftest.py`, for two reasons.

    It lists `save.containers()` only, while `App.container_keys()` -- the thing
    the running server validates against -- lists `save.containers() +
    save.extractor_containers()`, with the cores left out of the second list
    because nothing is ever sorted into one.

    And a worked example is written for the save it describes, not for the test
    fixture: the twelve-chest example names `chest11`, and the extractor-drain
    example names three cores. Validating them against one save would report
    "not a container in this save" for a container the document is explicitly
    telling the reader they might have. So the universe here is the whole legal
    vocabulary -- the `twelve-chests` variant's containers plus fourteen
    extractor keys -- and what is still being checked is the thing that matters:
    that no example names a container that cannot exist.
    """
    from nms_sorter.savemodel import SaveFile
    save = SaveFile(fixture_variant("twelve-chests")["save"])
    cs = save.containers() + save.extractor_containers()
    keys = [c.key for c in cs]
    sortable = [c.key for c in cs if c.sortable]
    for i in range(1, MAX_EXTRACTORS + 1):
        key = "extractor%d" % i
        if key not in keys:
            keys.append(key)
    return keys, sortable


CONFIG_EXAMPLES = [
    (rel, idx, body, line)
    for rel in ("docs/RULES.md", "docs/GUIDE.md")
    for idx, body, line in json_blocks(rel)
    if "config_version" in body
]


def test_the_documents_carry_config_examples_at_all():
    """A reference with no worked example is the failure mode this guards.

    `docs/RULES.md` promises "one example per mode" and there are five modes,
    so five is the floor. It is asserted rather than printed because the next
    test is vacuous without it: zero examples pass trivially.
    """
    assert len(CONFIG_EXAMPLES) >= 5, \
        ("at least one config example per rule mode: found %d (%s)"
         % (len(CONFIG_EXAMPLES),
            ", ".join("%s#%d" % (r, i) for r, i, _b, _l in CONFIG_EXAMPLES)))


@pytest.mark.parametrize(
    "rel,idx,body,line", CONFIG_EXAMPLES,
    ids=["%s-%d" % (os.path.basename(r).split(".")[0], i)
         for r, i, _b, _l in CONFIG_EXAMPLES])
def test_a_json_example_parses(rel, idx, body, line):
    cfg = json.loads(body)
    assert isinstance(cfg, dict), "%s:%d is a JSON object" % (rel, line)


@pytest.mark.parametrize(
    "rel,idx,body,line", CONFIG_EXAMPLES,
    ids=["%s-%d" % (os.path.basename(r).split(".")[0], i)
         for r, i, _b, _l in CONFIG_EXAMPLES])
def test_a_json_example_migrates_and_validates_with_no_errors(
        rel, idx, body, line, doc_container_keys):
    """Zero errors after `migrate()`. Warnings are allowed and named.

    The examples are written at `config_version: 1` on purpose -- they are what
    a person's existing file looks like -- so validating them without migrating
    first would only ever report the version, and prove nothing about the rule
    fields.
    """
    all_keys, sortable = doc_container_keys
    cfg = json.loads(body)
    cfg, notes = cfgmod.migrate(cfg)
    assert cfg.get("config_version") == cfgmod.CONFIG_VERSION, \
        "migrate() brings %s:%d to the current version" % (rel, line)
    issues = cfgmod.validate(cfg, all_keys, sortable)
    errors = [i for i in issues if i["level"] == "error"]
    assert not errors, \
        ("%s:%d (example %d) validates with no errors; got %s"
         % (rel, line, idx, [(i["where"], i["message"]) for i in errors]))
    warnings = [i["message"] for i in issues if i["level"] != "error"]
    print("%s:%d example %d: %d migration note(s), %d warning(s): %s"
          % (rel, line, idx, len(notes), len(warnings), "; ".join(warnings)))


# ===========================================================================
# 2. every TROUBLESHOOTING heading is a sentence the code can say
# ===========================================================================

#: the two headings that are on-screen text rather than an error, and must be
#: found in `pages.py` specifically: a match anywhere else would mean the
#: first-run page and this document had drifted apart while both looked fine.
PAGE_HEADINGS = ("Which folder holds your No Man's Sky saves?",
                 "No No Man's Sky save folder was found.")


def test_troubleshooting_has_headings_at_all():
    assert len(TROUBLE_HEADINGS) >= 40, \
        ("docs/TROUBLESHOOTING.md is keyed by heading: found %d"
         % len(TROUBLE_HEADINGS))


@pytest.mark.parametrize("heading,line", TROUBLE_HEADINGS,
                         ids=[h[:60] for h, _l in TROUBLE_HEADINGS])
def test_a_troubleshooting_heading_exists_in_the_source(heading, line):
    where = find_source(heading)
    assert where, \
        ("docs/TROUBLESHOOTING.md:%d has no matching string in the source: %r\n"
         "Either the code's wording changed and the heading has to follow, or "
         "the entry describes a message that was removed." % (line, heading))


@pytest.mark.parametrize("heading", PAGE_HEADINGS)
def test_an_on_screen_heading_comes_from_pages(heading):
    where = find_source(heading, restrict="nms_sorter/pages.py")
    assert where, \
        ("%r is on-screen text and must come from nms_sorter/pages.py" % heading)


def test_the_heading_match_table(capsys):
    """The table the ticket asks for, as one test so the output is one block."""
    rows, missing = [], []
    for heading, line in TROUBLE_HEADINGS:
        where = find_source(heading)
        rows.append((heading, where or "MISSING"))
        if not where:
            missing.append((line, heading))
    with capsys.disabled():
        print("\n  heading -> source (%d headings, %d matched, %d MISSING)"
              % (len(rows), len(rows) - len(missing), len(missing)))
        for heading, where in rows:
            print("    %-34s %s" % (where, heading[:96]))
    assert not missing, \
        ("every heading matches a string in the source; MISSING: %s"
         % [h for _l, h in missing])


# ---------------------------------------------------------------------------
# the matcher itself: what it must not match, and what it must still match
# ---------------------------------------------------------------------------

#: the exact pair the P5-4 documentation review found. Two different
#: refusals from two different steps -- step 10's `mf_` guard and step 8's
#: metadata check -- with nothing in common but the word `metadata`, which is
#: enough to get past the anchor pre-filter. The old matcher then said yes,
#: because the `%s` in the source could swallow the whole heading.
ABSORPTION_HEADING = ("metadata: the size patch would touch a byte outside "
                      "0x38..0x3F")
ABSORPTION_SOURCE = _norm(
    "metadata is updatable: this save's metadata could not be decoded (%s), "
    "so it cannot be updated after the write; refusing rather than leaving "
    "the pair disagreeing")

#: `%s is running as pid %s` opens with an interpolation, so `_norm` gives it a
#: leading wildcard -- the shape the review names. It must not match a heading
#: it shares an anchor with and nothing else.
LEADING_WILDCARD_SOURCE = _norm("%s is running as pid %s, since %s")


def test_a_wildcard_in_the_source_cannot_absorb_a_whole_heading():
    """The regression this matcher change exists for.

    Asserted in four steps so a failure says which one broke: both halves of
    the pair are real, the anchor pre-filter does let the candidate through
    (otherwise the test would pass for the wrong reason), the match is refused,
    and the heading's own source is the one `find_source` names.
    """
    pattern = heading_pattern(ABSORPTION_HEADING)
    assert ("### " + ABSORPTION_HEADING) in TROUBLE, \
        "docs/TROUBLESHOOTING.md still carries the heading"
    assert any(pat == ABSORPTION_SOURCE for pat, _w in CORPUS), \
        "and nms_sorter still says the other sentence verbatim"
    anchors = _anchors(pattern)
    assert anchors and any(a in ABSORPTION_SOURCE.lower() for a in anchors), \
        ("the pre-filter is not what rejects this pair -- if it were, the "
         "assertion below would prove nothing")
    assert not _fits(pattern, ABSORPTION_SOURCE), \
        ("a heading may not match a source string it overlaps only through "
         "that string's wildcards: %r vs %r"
         % (ABSORPTION_HEADING, ABSORPTION_SOURCE.replace(WILD, "%s")))
    assert not _fits(pattern, LEADING_WILDCARD_SOURCE), \
        "nor through a wildcard the source string opens with"
    where = find_source(ABSORPTION_HEADING)
    assert where and where.startswith("nms_sorter/safety.py"), \
        ("and the sentence it does match is safety.py's step-10 guard, not "
         "whichever wildcard-carrying string the walk reached first; got %s"
         % where)


#: the heading whose source is markup. `pages.py` builds it as one implicitly
#: concatenated literal with a `<code>` span in the middle of the sentence.
MARKUP_HEADING = ("That folder holds no save*.hg files, so it is not a save "
                  "folder. Nothing was changed.")

#: the heading no single string in the corpus can supply: `_verify_copy`
#: returns the detail, and restore's step 2 joins the step name on the front
#: and a reason on the end. It is the match that sets `MIN_LITERAL_COVER`.
STEP_PREFIX_HEADING = ("verify the backup: ... could not be read from this "
                       "backup (...), so this backup is not one to restore "
                       "from")


def test_a_refusal_written_as_markup_matches_its_heading():
    """`<code>` inside a sentence is not a difference between the two sides.

    The source really is markup and the heading really is not, so without
    `_TAG` there is no embedding at all -- and what the matcher did instead was
    worse than reporting nothing: it credited the heading to `app.py`'s "there
    are no saveN.hg files in %s", a different message about a different folder,
    purely through that string's wildcard.
    """
    assert ("### " + MARKUP_HEADING) in TROUBLE, \
        "docs/TROUBLESHOOTING.md still carries the heading"
    raw = [pat for pat, where in CORPUS
           if where.startswith("nms_sorter/pages.py") and "save*.hg" in pat]
    assert raw, "nms_sorter/pages.py still builds this refusal"
    assert not any("<" in pat for pat in raw), \
        ("_norm reduced the markup to its text; if this fails the tag "
         "pattern stopped matching: %s" % raw)
    where = find_source(MARKUP_HEADING, restrict="nms_sorter/pages.py")
    assert where, ("%r is on-screen text from pages.py and must match it "
                   "despite the code span in the source" % MARKUP_HEADING)
    assert find_source(MARKUP_HEADING) == where, \
        ("and pages.py must be the first thing the walk matches, not "
         "app.py's unrelated sentence; got %s" % find_source(MARKUP_HEADING))


def test_a_heading_that_carries_a_step_prefix_its_source_cannot_supply():
    """The match `MIN_LITERAL_COVER` is set for, asserted with its number.

    The floor is the one number in this matcher that is a judgement rather
    than a rule, so the case that pins it is spelled out: if a change pushes
    this pair below the floor the failure says so, instead of turning up as
    one more MISSING row in the table with no explanation.
    """
    assert ("### " + STEP_PREFIX_HEADING) in TROUBLE, \
        "docs/TROUBLESHOOTING.md still carries the heading"
    pattern = heading_pattern(STEP_PREFIX_HEADING)
    literal = len(pattern.replace(WILD, ""))
    best = max((_cover(WILD + pattern + WILD, pat), where)
               for pat, where in CORPUS
               if _anchored(pattern, pat))
    cover, where = best
    assert where.startswith("nms_sorter/safety.py"), \
        ("safety.py joins this sentence together; the best cover was %s"
         % where)
    assert cover < 0.7 * literal, \
        ("this test is only interesting while the pair is a near miss: "
         "%d/%d is above seven tenths, so the floor could go back up"
         % (cover, literal))
    assert cover >= MIN_LITERAL_COVER * literal, \
        ("and the floor has to admit it: %d/%d = %.1f%% against a floor of "
         "%.0f%%" % (cover, literal, 100.0 * cover / literal,
                     100.0 * MIN_LITERAL_COVER))
    assert find_source(STEP_PREFIX_HEADING), \
        "so the heading matches"


#: (heading, the module that says it). Every one of these renders a constant
#: through a `%s`/`%d`/`%r`, which is the case two-sided matching was written
#: to allow and the case a stricter matcher is most likely to break:
#: `GAME_PROCESS` twice, `MIN_PYTHON`, `MAX_BODY`, and a `%r` of a key the
#: person typed. If one of these ever fails, the fix is the matcher -- not a
#: vaguer heading, which is the wrong direction (see `_fits`).
INTERPOLATED_HEADINGS = [
    ("game: NMS.exe is running as pid ... Go to the main menu in the game, "
     "then tick \"I am at the main menu, not in a loaded save\" and try "
     "again.",
     "nms_sorter/safety.py"),
    ("the process check needs Windows, so it cannot be told whether NMS.exe "
     "is running; plan and browse work here, apply does not",
     "nms_sorter/platform.py"),
    ("This needs Python 3.9 or newer and this is Python ...",
     "nms_sorter/__main__.py"),
    ("... is not a container in this save",
     "nms_sorter/config.py"),
]


@pytest.mark.parametrize("heading,module", INTERPOLATED_HEADINGS,
                         ids=[h[:48] for h, _m in INTERPOLATED_HEADINGS])
def test_a_heading_that_renders_a_constant_still_matches_its_source(heading,
                                                                    module):
    """The other side of the change: the literal-cover floor is not too high.

    `... is not a container in this save` is the leading-wildcard shape on the
    *document's* side, which is legitimate and has to keep working: it is the
    heading's first characters that are absorbed there, not the source's.
    """
    assert ("### " + heading) in TROUBLE, \
        "docs/TROUBLESHOOTING.md still carries the heading"
    where = find_source(heading)
    assert where, ("%r renders a constant through a format placeholder and "
                   "must still match" % heading)
    assert where.startswith(module), \
        ("and it must match the module that says it, %s, not %s"
         % (module, where))


#: a refusal that carries a code span, which is a shape the matcher used to be
#: unable to see: `headings()` strips the backticks out of the document and
#: `_norm` used to keep them in the source, so the only spelling that matched
#: was one with `...` where the source has a word. What is asserted here is
#: that the verbatim spelling *would* match, so the next person to write one is
#: not forced into the vaguer heading.
#:
#: There used to be two. The other, "name the backup folder to restore, as
#: `folder`", went with the API-error section: `POST /api/restore` with no
#: `folder` is not something the page can do.
BACKTICKED_REFUSALS = ("a `fill` ceiling needs a `store` to apply it to",)

#: every module the sentence check reads, concatenated, for the "is this still
#: the source's own wording" half of the test below
SOURCE_TEXT = "\n".join(
    io.open(os.path.join(ROOT, "nms_sorter", fn), encoding="utf-8").read()
    for fn in SENTENCE_SOURCES
    if os.path.exists(os.path.join(ROOT, "nms_sorter", fn)))


@pytest.mark.parametrize("sentence", BACKTICKED_REFUSALS)
def test_a_refusal_with_a_code_span_can_be_documented_verbatim(sentence):
    """The matcher is symmetric about backticks, in both directions.

    Both spellings have to work: the verbatim one, because that is what
    somebody searching has in front of them, and the code-span-free one,
    because that is what a markdown heading turns into.
    """
    assert sentence in SOURCE_TEXT, \
        "%r is still the source's own wording" % sentence
    assert find_source(sentence), \
        ("%r is a sentence the code says, so a heading may be written that "
         "way verbatim" % sentence)
    assert find_source(sentence.replace("`", "")), \
        ("and the same sentence without its code spans, which is what "
         "headings() hands the matcher: %r" % sentence)


def _non_refusal_headings():
    """[(rel, heading)] for every `####` under a "Not refusals" section.

    The document's own convention: a refusal is a `###` heading and is checked
    against the source above; something a person sees *around* the program --
    SmartScreen, an antivirus quarantine -- is a `####` under "Not refusals",
    because the program never says it.
    """
    out = []
    for rel in MARKDOWN:
        text = _read(rel)
        for title, lines in sections(text, 2).items():
            if "not refusals" not in title.lower():
                continue
            for heading, _line in headings("\n".join(lines), 4):
                out.append((rel, heading))
    return out


NON_REFUSAL_HEADINGS = _non_refusal_headings()


def test_a_non_refusal_heading_is_not_a_sentence_the_code_says():
    """The complement of check 2: under "Not refusals", a match is the bug.

    Check 2 fails a `###` heading with no source sentence. This is the other
    direction, for the entries that are deliberately *not* refusals -- what
    SmartScreen says, what an antivirus does -- which is why the document puts
    them under their own `##` section at `####`. Promote one to `###` by
    accident and check 2 starts demanding a source sentence for a message the
    program never says; leave one at `####` while the program grows that exact
    wording and the entry is in the wrong half of the document. This test is
    the second of those two.

    Skipped while no document has such a section: lane E's rewrite of
    `docs/TROUBLESHOOTING.md` (113 headings, one per sentence the source can
    say) moved the SmartScreen material into `docs/GUIDE.md` as prose, so
    there is nothing at `####` to check today. It is kept rather than deleted
    because the section shape is the one the document is likely to grow back:
    the material is real and it is not a refusal.
    """
    if not NON_REFUSAL_HEADINGS:
        pytest.skip("no document has a 'Not refusals' section with #### "
                    "entries today; this guard arms itself when one does")
    bad = [(rel, heading, find_source(heading))
           for rel, heading in NON_REFUSAL_HEADINGS if find_source(heading)]
    assert not bad, \
        ("a non-refusal heading matches a sentence the code says, so it is in "
         "the wrong half of the document (it belongs above, as ###): %s"
         % ["%s: %r matches %s" % row for row in bad])


# ===========================================================================
# 3. the reverse: every sentence in the source is documented (soft)
# ===========================================================================

#: calls whose message argument is a sentence shown to a person
SENTENCE_CALLS = ("Refused", "Invalid", "RuntimeError", "ValueError",
                  "UnsupportedSave", "fail", "err", "warn", "_note", "refuse")

#: a sentence shorter than this is a label, not a sentence
MIN_SENTENCE = 24


def _request_shape(bare):
    """Is this one of the answers to a malformed HTTP request?

    Documented in the module docstring, check 3: the page sends what the API
    expects, so these sentences reach nobody who is not writing a script
    against it, and the sentence itself names the field. Keeping them in the
    gap list is how a gap list stops being read.
    """
    low = bare.lower()
    return any(frag in low for frag in REQUEST_SHAPE)


def _sentences():
    """[(variants, "file:line")] for every refusal the named modules can raise.

    `variants` is the set of spellings the same message can be documented
    under. A `Report` step is raised as `r.fail("backup", "%s does not
    exist")` and reads to a person as `backup: ... does not exist`, so both the
    bare message and the joined form count as a hit: the one that is in the
    document is the one the person searching sees.
    """
    out = []
    for fn in SENTENCE_SOURCES:
        path = os.path.join(ROOT, "nms_sorter", fn)
        if not os.path.exists(path):
            continue
        with io.open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node) not in SENTENCE_CALLS or not node.args:
                continue
            label = None
            if len(node.args) >= 2:
                first = _static_str(node.args[0])
                if WILD not in first and 0 < len(first) <= 40:
                    label = first
                args = node.args[1:]
            else:
                args = node.args
            for arg in args:
                pat = _static_str(arg)
                bare = pat.replace(WILD, "").strip()
                if len(bare) < MIN_SENTENCE:
                    continue
                if _request_shape(bare):
                    continue
                variants = [pat]
                if label:
                    variants.append(label + ": " + pat)
                out.append((variants, "%s:%d" % (fn, node.lineno)))
    path = os.path.join(ROOT, "nms_sorter", "pages.py")
    with io.open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    wanted = ("CONFIG_UNREADABLE_H1", "CONFIG_TOO_NEW_H1", "DATA_MISSING_H1",
              "FIRST_RUN_H1", "NO_SAVES_H1", "MISSING_FOLDER_H1")
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in wanted
                for t in node.targets):
            out.append(([_static_str(node.value)], "pages.py:%d" % node.lineno))
    return out


def _doc_patterns():
    """Every `###` heading, as a wildcard pattern.

    One document: `docs/TROUBLESHOOTING.md` keys its entries by the message's
    first sentence, so a heading there is the whole of what "documented" means.
    `docs/SAFETY.md` used to carry a second, prose copy of the same list under
    "Every refusal, verbatim", read here as well; it is gone, and two lists of
    the same sentences was the reason one of them went stale.
    """
    out = [heading_pattern(h) for h, _l in TROUBLE_HEADINGS]
    return [p for p in out if p.replace(WILD, "").strip()]


DOC_PATTERNS = _doc_patterns()


def _documented(variants):
    """Does some documented sentence match one of these spellings?

    One direction only: the *document's* pattern is searched inside the
    *source's* pattern. The other direction is vacuous -- a heading such as
    `... is not a container in this save` begins with a wildcard, and a
    wildcard at the start of the string being searched matches anything at all,
    which made an earlier draft of this check report zero gaps for every input
    including invented ones.
    """
    for pat in variants:
        low = pat.lower()
        for doc in DOC_PATTERNS:
            anchors = _anchors(doc)
            if anchors and not any(a in low for a in anchors):
                continue
            if _fits(doc, pat):
                return True
    return False


def test_every_refusal_sentence_in_the_source_is_documented(capsys):
    """Soft: prints the gap list and the count, and does not fail.

    The measurement, not a gate. A hard failure here would make every lane that
    adds a refusal red until the documentation catches up, and the number going
    up is visible on every run without that.
    """
    sentences = _sentences()
    gaps = []
    seen = set()
    for variants, where in sentences:
        bare = variants[0].replace(WILD, " ").strip()
        if bare in seen:
            continue
        seen.add(bare)
        if not _documented(variants):
            gaps.append((where, bare))
    with capsys.disabled():
        print("\n  refusal sentences in the source: %d distinct, %d undocumented"
              % (len(seen), len(gaps)))
        for where, bare in sorted(gaps):
            print("    UNDOCUMENTED %-22s %s" % (where, bare[:110]))
    assert True                     # a measurement, not a gate (see docstring)


# ===========================================================================
# 4. markdown hygiene
# ===========================================================================

BANNED = ((u"—", "em dash"), (u"–", "en dash"),
          (u"‘", "left single quote"), (u"’", "right single quote"),
          (u"“", "left double quote"), (u"”", "right double quote"))


@pytest.mark.parametrize("rel", MARKDOWN)
def test_a_document_uses_plain_ascii_punctuation(rel):
    """No em dash, en dash or smart quote.

    Not a style preference: a smart quote inside one of the JSON examples above
    makes it fail to parse when it is pasted, and the same examples are meant
    to be pasted. `--` and `"` are the house forms.
    """
    text = _read(rel)
    found = []
    for ch, name in BANNED:
        if ch in text:
            lines = [i for i, line in enumerate(text.splitlines(), 1)
                     if ch in line]
            found.append("%s x%d (lines %s)"
                         % (name, text.count(ch),
                            ", ".join(str(n) for n in lines[:8])))
    assert not found, "%s uses %s" % (rel, "; ".join(found))


_LINK = re.compile(r"\[[^\]]*\]\(\s*([^)\s]+)")


def _relative_links(rel):
    """[(target, line)] for every markdown link that is not a URL."""
    out = []
    for i, line in enumerate(_read(rel).splitlines(), 1):
        for m in _LINK.finditer(line):
            target = m.group(1)
            if target.startswith(("http://", "https://", "mailto:", "#", "<")):
                continue
            out.append((target, i))
    return out


@pytest.mark.parametrize("rel", MARKDOWN)
def test_a_documents_relative_links_resolve(rel):
    """Every `[..](path)` points at a file that exists.

    Images are exempt and reported instead: the screenshots are produced by
    ticket P7-2 and the README already links them, so failing here would block
    every other lane on a file nobody can generate yet.
    """
    base = os.path.dirname(os.path.join(ROOT, rel))
    missing, pending = [], []
    for target, line in _relative_links(rel):
        path = target.split("#")[0]
        if not path:
            continue
        full = os.path.normpath(os.path.join(base, path))
        if os.path.exists(full):
            continue
        if path.lower().endswith(IMAGE_SUFFIXES):
            pending.append("%s:%d -> %s" % (rel, line, target))
        else:
            missing.append("%s:%d -> %s" % (rel, line, target))
    for note in pending:
        print("pending image (P7-2): %s" % note)
    assert not missing, "%s links to files that do not exist: %s" % (rel, missing)


@pytest.mark.parametrize("rel", MARKDOWN)
def test_a_documents_anchors_resolve(rel):
    """Best-effort: `[..](FILE.md#anchor)` names a heading in that file.

    Best-effort because GitHub's slug rules are not the anchor text: the check
    is that some heading in the target file reduces to the same slug under the
    common rule (lower-case, non-word characters dropped, spaces to hyphens).
    An anchor that does not reduce is printed, not failed.
    """
    base = os.path.dirname(os.path.join(ROOT, rel))
    unresolved = []
    for target, line in _relative_links(rel):
        if "#" not in target:
            continue
        path, anchor = target.split("#", 1)
        if not path or not anchor:
            continue
        full = os.path.normpath(os.path.join(base, path))
        if not full.lower().endswith(".md") or not os.path.exists(full):
            continue
        with io.open(full, encoding="utf-8") as fh:
            slugs = set()
            for l in fh:
                if l.startswith("#"):
                    h = l.lstrip("#").strip().lower()
                    h = re.sub(r"[^\w\s-]", "", h)
                    slugs.add(re.sub(r"\s+", "-", h))
        if anchor.lower() not in slugs:
            unresolved.append("%s:%d -> %s" % (rel, line, target))
    for note in unresolved:
        print("anchor not resolved (best effort): %s" % note)
    assert True
