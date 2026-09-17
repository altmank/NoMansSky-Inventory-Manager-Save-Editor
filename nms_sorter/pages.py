"""The HTML pages served when the application cannot start normally.

GOAL.md P2-2 and P2-3. A traceback in a console window is useless to the person
this tool is for: they double-clicked something and a black window flashed. So
every startup failure that can still bind a port renders as a page, and the page
says what happened, names the path involved, and offers the fix.

Two contracts hold for everything in here:

* **The first sentence of the problem is the `<h1>`, verbatim.**
  `docs/TROUBLESHOOTING.md` is keyed by these sentences, so a person who reads
  one on screen can search for it and find the entry. Changing an `<h1>` here
  means changing that document in the same commit.
* **No JavaScript is required.** Styling is inline, the fixes are plain
  `<form>` posts, and the first-run page works with scripting switched off. The
  first-run page adds a small script only to refresh the detected folder list
  in place; nothing depends on it.

Sizes are checked in `tests/test_settings.py`: each failure page stays under
3 KB, because the page is the whole product at that moment and a slow one on a
cold start reads as another failure.
"""
import html
import posixpath
import re

#: dark, one column, no webfont, no asset request. The page must render from
#: the bytes of the response and nothing else: a missing stylesheet on an error
#: page is a second error the operator cannot diagnose. That is why this is
#: inline rather than a link to `static/app.css` -- these pages are shown
#: exactly when something is wrong, and the one thing they must not depend on
#: is another request succeeding.
#:
#: X13 (UI review 3): it is the *same* design system as `static/app.css` all
#: the same, value for value -- the four surfaces, the three inks, amber for
#: the one primary action, `--link #8FBDEA` for a link. It had drifted to its
#: own background (#14161a against the app's #0B0E13) and a blue primary
#: button, and for a player whose external drive is unplugged this is the
#: first and possibly the only screen they ever see. The sentences are
#: untouched.
STYLE = (
    "html{color-scheme:dark}"
    ":root{--bg:#0B0E13;--surface:#12171F;--surface-2:#171D27;"
    "--surface-3:#1D2531;--line:#222B38;--line-2:#2F3A4B;--ink:#E3EAF4;"
    "--ink-2:#AFBCCE;--ink-3:#8A97AD;--amber:#FFB03A;--amber-ink:#241701;"
    "--warn:#E6BC63;--link:#8FBDEA;--focus:#FFC670}"
    "body{margin:0;padding:3rem 1.5rem;background:var(--bg);color:var(--ink);"
    "font:13px/1.55 'Segoe UI Variable Text','Segoe UI',system-ui,"
    "-apple-system,Arial,sans-serif}"
    "main{max-width:72ch;margin:0 auto}"
    "h1{font-size:18px;font-weight:600;line-height:1.3;margin:0 0 1rem;"
    "color:var(--ink)}"
    "h2{font-size:15px;font-weight:600;margin:1.6rem 0 .5rem;color:var(--ink)}"
    "p,li{margin:.6rem 0;color:var(--ink-2)}"
    "code,.mono{background:var(--surface-2);border:1px solid var(--line);"
    "border-radius:6px;padding:.1rem .3rem;font:12px/1.4 'Cascadia Mono',"
    "Consolas,ui-monospace,monospace;word-break:break-all;color:var(--ink)}"
    "a{color:var(--link)}"
    ".note{color:var(--ink-3);font-size:12px}"
    # a warning surface, always with the word as well as the colour
    ".risk{border-left:3px solid var(--warn);padding-left:.8rem;"
    "color:var(--ink-2)}"
    "form{margin:1rem 0}"
    # the one primary action per screen, in the page's own amber
    "button{font:inherit;font-weight:650;background:var(--amber);"
    "color:var(--amber-ink);border:1px solid #C9861A;border-radius:6px;"
    "padding:.45rem .8rem;cursor:pointer;min-height:30px}"
    "button.plain{background:var(--surface-2);color:var(--ink);"
    "border-color:var(--line-2);font-weight:500}"
    "button.plain:hover{border-color:var(--ink-3);background:var(--surface-3)}"
    "input[type=text]{font:inherit;width:100%;box-sizing:border-box;"
    "background:var(--surface-2);color:var(--ink);border:1px solid "
    "var(--line-2);border-radius:6px;padding:.45rem .5rem;min-height:30px}"
    # one visible focus ring, the same one the application uses
    ":focus-visible{outline:2px solid var(--focus);outline-offset:2px;"
    "border-radius:6px}"
    "ul{padding-left:1.2rem}"
    "li.folder{margin:.9rem 0}"
)


def _esc(v):
    return html.escape("" if v is None else str(v), quote=True)


def page(h1, body, title=None):
    """One document. `h1` is the problem's first sentence; `body` is HTML."""
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>%s</title><style>%s</style></head>\n<body><main>\n"
        "<h1>%s</h1>\n%s\n"
        "<p class=\"note\">No Man's Sky inventory sorter. Nothing has been "
        "written to any save.</p>\n</main></body></html>\n"
        % (_esc(title or h1), STYLE, _esc(h1), body))


# ---------------------------------------------------------------------------
# (b) the config file cannot be read
# ---------------------------------------------------------------------------

CONFIG_UNREADABLE_H1 = "The configuration file could not be read."


def config_unreadable(path, detail, offer_reset=True):
    body = [
        "<p>The sorter reads your rules from this file, and this time it did "
        "not come back as valid JSON, so it has stopped before touching "
        "anything:</p>",
        "<p><code>%s</code></p>" % _esc(path),
        "<p class=\"note\">%s</p>" % _esc(detail),
        "<h2>What to do</h2>",
        "<p>Either fix the JSON in that file with a text editor and restart, "
        "or move it aside and start again from the shipped default. Your "
        "rules are only in that one file, so moving it aside loses them; the "
        "old file is kept next to it with a <code>.bad-</code> stamp in the "
        "name.</p>",
    ]
    if offer_reset:
        body.append(
            "<form method=\"post\" action=\"/api/config/reset\">"
            "<button type=\"submit\">Move it aside and start fresh</button>"
            "</form>")
    return page(CONFIG_UNREADABLE_H1, "\n".join(body))


# ---------------------------------------------------------------------------
# (c) the config file is newer than this build
# ---------------------------------------------------------------------------

CONFIG_TOO_NEW_H1 = ("This configuration file was written by a newer version "
                     "of the sorter.")


def config_too_new(path, file_version, build_version):
    body = [
        "<p>The file says it is version <code>%s</code>. This build reads "
        "version <code>%s</code>, and it will not guess at what changed "
        "between them, so it has stopped without touching the file:</p>"
        % (_esc(file_version), _esc(build_version)),
        "<p><code>%s</code></p>" % _esc(path),
        "<h2>What to do</h2>",
        "<p>Install the newer version of the sorter again and use that, or "
        "point this one at a different file with "
        "<code>--config &lt;path&gt;</code>. If you would rather start over "
        "from the shipped default, move the file aside by hand first: this "
        "page does not offer to do it for you, because a newer file is "
        "somebody's working configuration and nothing here can read it back "
        "once it is gone.</p>",
    ]
    return page(CONFIG_TOO_NEW_H1, "\n".join(body))


# ---------------------------------------------------------------------------
# (d) the packaged data files are missing
# ---------------------------------------------------------------------------

DATA_MISSING_H1 = "This copy of the sorter is missing its data files."


def data_missing(detail, filename=None):
    body = [
        "<p>The item table ships inside the program, in "
        "<code>nms_sorter/data/</code>. One of those files could not be read, "
        "so the install is incomplete and nothing can be sorted:</p>",
        "<p><code>%s</code></p>" % _esc(filename or "nms_sorter/data/"),
        "<p class=\"note\">%s</p>" % _esc(detail),
        "<h2>What to do</h2>",
        "<p>Unzip the release again into an empty folder, or re-clone the "
        "repository: a half-copied folder and an antivirus quarantine both "
        "look like this. The tables are not rebuilt at start-up and there is "
        "nothing to run here: they are generated from a decompiled game "
        "install by whoever maintains this tool, so a complete copy is the "
        "only repair.</p>",
    ]
    return page(DATA_MISSING_H1, "\n".join(body))


# ---------------------------------------------------------------------------
# (a) / P2-3: the first run
# ---------------------------------------------------------------------------

FIRST_RUN_H1 = "Which folder holds your No Man's Sky saves?"
NO_SAVES_H1 = "No No Man's Sky save folder was found."

#: the only three places this program will talk about. It probes exactly one
#: root (`%APPDATA%\HelloGames\NMS`); everything else is a sentence and a text
#: field, because a wrong guess sorts the wrong account (GOAL.md Rule 1).
PATH_HINTS = [
    ("Steam", "%APPDATA%\\HelloGames\\NMS\\st_&lt;your steam id&gt;"),
    ("GOG", "%APPDATA%\\HelloGames\\NMS\\DefaultUser"),
    ("Microsoft Store / Game Pass",
     "not supported; the save is in a container format this tool has never "
     "seen. See docs/TROUBLESHOOTING.md."),
]

_SCRIPT = (
    "<script>\n"
    "// Progressive enhancement only: the list above was rendered by the\n"
    "// server and this page works with scripting switched off.\n"
    "var b=document.getElementById('rescan');\n"
    "if(b){b.onclick=function(){b.disabled=true;"
    "fetch('/api/detect-saves').then(function(r){return r.json()})"
    ".then(function(d){var n=(d.folders||[]).length;"
    "b.textContent=n?('found '+n+' - reloading'):'still nothing found';"
    "if(n){location.reload()}else{b.disabled=false}})"
    ".catch(function(){b.textContent='could not look';b.disabled=false})};}\n"
    "</script>"
)


def _folder_form(path, label, primary=True, name=None):
    """One "Use this folder" offer.

    W16: the page rendered two or three buttons reading exactly "Use this
    folder", so a screen reader announced the same name for every offer and
    nothing said which one had been pressed. The visible word stays the same in
    every row -- the row above it *is* the difference -- and the accessible
    name carries that difference, which is what `aria-label` is for.
    """
    return (
        "<form method=\"post\" action=\"/api/settings\">"
        "<input type=\"hidden\" name=\"save_folder\" value=\"%s\">"
        "<button type=\"submit\"%s%s>%s</button></form>"
        % (_esc(path), "" if primary else " class=\"plain\"",
           (" aria-label=\"%s\"" % _esc(name)) if name else "",
           _esc(label)))


def _folder_summary(f):
    """The one line that tells two Steam profiles apart. -> HTML, or "".

    W10: the page listed `st_76561198000000001` plus "1 save file, newest
    2026-09-14 19:31" per candidate, and in the operator's own run both
    timestamps were identical -- so it asked them to choose between two ids
    they had never seen. The main page already renders "Aboard Iigash Station
    Sigma - 24h played" for a save; that is the sentence a person recognises,
    so it is offered here too.

    Filled by `server.detect_saves` out of the newest save's `mf_` metadata,
    which opens no save file: a first-run page must not decode a save to render
    itself.
    """
    bits = [b for b in (f.get("newest_save_summary"),
                        f.get("newest_save_playtime")) if b]
    if not bits:
        return ""
    return "<br><span class=\"note\">%s</span>" % _esc(" · ".join(bits))


def _folder_line(f):
    when = f.get("newest_save_label") or "no save file in it"
    return ("<li class=\"folder\"><code>%s</code><br>"
            "<span class=\"note\">%s save file%s, newest %s</span>%s%s</li>"
            % (_esc(f.get("path")), f.get("save_count", 0),
               "" if f.get("save_count") == 1 else "s", _esc(when),
               _folder_summary(f),
               _folder_form(f.get("path"), "Use this folder",
                            name="Use this folder: %s" % f.get("path"))))


def _look_again():
    """The re-probe button, on every branch of the first-run page.

    It used to be rendered only when nothing was found, so a player who had
    the wrong drive plugged in -- or who copied a save folder into place while
    this page was open -- had to restart the sorter to be offered it (P5-2
    walkthrough, D6). Probing again is a directory listing under one root; it
    is the cheapest thing this program does and there is no reason to make a
    restart the way to ask for it.
    """
    return ("<p><button id=\"rescan\" class=\"plain\" type=\"button\">Look "
            "again</button> <span class=\"note\" id=\"rescan-note\">reads "
            "the folder list again; changes nothing</span></p>")


def _hints():
    rows = ["<li><b>%s</b>: <code>%s</code></li>" % (name, path)
            for name, path in PATH_HINTS]
    return "<ul>%s</ul>" % "".join(rows)


def _typein(current=""):
    return (
        "<h2>Or type the folder</h2>"
        "<form method=\"post\" action=\"/api/settings\">"
        "<p><input type=\"text\" name=\"save_folder\" value=\"%s\" "
        "placeholder=\"C:\\Users\\you\\AppData\\Roaming\\HelloGames\\NMS\\st_...\" "
        "autofocus></p>"
        "<button type=\"submit\" aria-label=\"Use the folder typed above\">"
        "Use this folder</button></form>"
        "<p class=\"note\">Paste the folder that holds <code>save.hg</code>, "
        "<code>save2.hg</code> and their <code>mf_</code> partners, not one of "
        "the files.</p>" % _esc(current))


#: the two answers `invalid=` can carry, and the sentence for each. W6: one
#: sentence used to cover both mistakes -- `D:\my nms saves` with no such drive
#: and an existing empty folder both said "That folder holds no save*.hg
#: files", so a player whose path was simply wrong went hunting for save files
#: instead of checking the path. The second spelling is unchanged, because
#: `docs/TROUBLESHOOTING.md` is keyed on its opening fragment.
INVALID_NO_FOLDER = "no_folder"
INVALID_NO_SAVES = "no_saves"


def _invalid_sentence(kind, current):
    if kind == INVALID_NO_FOLDER:
        return ("<p class=\"risk\">There is no folder at <code>%s</code>, so "
                "there was nothing to look in. Check the path, or the drive it "
                "is on. Nothing was changed.</p>" % _esc(current))
    return ("<p class=\"risk\">That folder holds no <code>save*.hg</code> "
            "files, so it is not a save folder. Nothing was changed.</p>")


def first_run(folders, root=None, current="", invalid=False):
    """The first-run page. `folders` is what `GET /api/detect-saves` returned.

    Exactly one candidate is shown as one offer plus "choose another"; several
    are listed with their newest save time and nothing is picked for the
    operator (Rule 1: "picking silently is how the wrong account gets
    sorted"); none is the three paths and a text field.

    `invalid` is `INVALID_NO_FOLDER`, `INVALID_NO_SAVES`, or falsey. `True` is
    still accepted and reads as `INVALID_NO_SAVES`, which is what it always
    meant.
    """
    folders = list(folders or [])
    if len(folders) == 1:
        h1 = FIRST_RUN_H1
        body = [
            "<p>One save folder was found on this machine:</p>",
            "<ul>%s</ul>" % _folder_line(folders[0]),
            "<p class=\"note\">Nothing is written to your save until you ask "
            "for it, and this button only records the folder.</p>",
            _look_again(),
            _typein(current),
        ]
    elif folders:
        h1 = FIRST_RUN_H1
        body = [
            "<p>%d save folders were found. Pick the one you play, by its "
            "newest save: this tool will not choose for you, because sorting "
            "the wrong account is not undoable from in the game.</p>"
            % len(folders),
            "<ul>%s</ul>" % "".join(_folder_line(f) for f in folders),
            _look_again(),
            _typein(current),
        ]
    else:
        h1 = NO_SAVES_H1
        body = [
            "<p>Nothing was found under <code>%s</code>. That is the only "
            "place this tool looks; the three layouts it knows about are:</p>"
            % _esc(root or "%APPDATA%\\HelloGames\\NMS"),
            _hints(),
            _look_again(),
            _typein(current),
        ]
    if invalid:
        kind = invalid if isinstance(invalid, str) else INVALID_NO_SAVES
        body.insert(0, _invalid_sentence(kind, current))
    return page(h1, "\n".join(body) + "\n" + _SCRIPT)


# ---------------------------------------------------------------------------
# (f) the configured save folder is not there any more
# ---------------------------------------------------------------------------

#: W5. Rendered with the path in it, so the `<h1>` a player reads names the
#: folder that is missing. The template is the searchable part.
MISSING_FOLDER_H1 = "The save folder %s is not there."

#: where `missing_folder` sends a player who wants the picker. Its own path
#: rather than a query on `/`, because `/` is the route that decides *which*
#: page a player gets, and this link has to mean "the picker, whatever `/`
#: would have decided".
CHOOSE_FOLDER_PATH = "/choose-folder"


def missing_folder(path):
    """The configured save folder does not exist. Its own page, not the first
    run.

    W5: `save_folder: E:\\NMS saves on my external drive` with the drive
    unplugged degraded to the first-run page, which reported "That folder holds
    no `save*.hg` files" without ever naming `E:\\...`, said "first run" though
    `settings.json` existed, and offered a *different* `st_` profile behind a
    "Use this folder" button. GOAL.md 2.3 refuses to choose between profiles
    precisely because "sorting the wrong account is not something you can undo
    from in the game", and that path walked around the refusal in one click.

    So: the path is the heading, the two ordinary causes are named, looking
    again is one button, and choosing another folder is a link to the page that
    does that -- one click further away, and with nothing pre-selected here.
    """
    return page(
        MISSING_FOLDER_H1 % path,
        "\n".join([
            "<p>Your saves are recorded as living here, and there is nothing "
            "at that path now:</p>",
            "<p><code>%s</code></p>" % _esc(path),
            "<h2>What it means</h2>",
            "<p>Either the drive it is on is not plugged in, or the folder has "
            "been moved or renamed. Nothing has been read and nothing has been "
            "written. The folder is still recorded in your settings, so "
            "plugging the drive back in and looking again is all it takes.</p>",
            # A GET form, so the button needs no script: the page at `/` works
            # this out per request, and reloading it *is* looking again.
            "<form method=\"get\" action=\"/\">"
            "<button type=\"submit\">Look again</button></form>",
            "<h2>Or use a different folder</h2>",
            "<p>This page offers you nothing to press but the button above: a "
            "folder that is not there is not a reason to sort a different "
            "account. <a href=\"%s\">Choose another folder</a> if that is what "
            "you meant, and pick it there yourself.</p>" % CHOOSE_FOLDER_PATH,
        ]),
        title="The save folder is not there")


# ---------------------------------------------------------------------------
# (e) the shipped documents, rendered offline
# ---------------------------------------------------------------------------

#: added to `STYLE` for a document page: tables, because `docs/SAFETY.md` is
#: mostly a refusal table and a table rendered as paragraphs is unreadable.
#: X13: the same tokens as `STYLE` above, so a gate's "what this means" link
#: does not land on a differently-coloured document.
DOC_STYLE = (
    "main{max-width:84ch}"
    "h1{margin-top:0}"
    "h3,h4{font-size:13px;font-weight:600;margin:1.6rem 0 .4rem;"
    "color:var(--ink)}"
    "pre{background:var(--surface-2);border:1px solid var(--line);"
    "border-radius:6px;padding:.7rem .8rem;overflow-x:auto}"
    "pre code{background:none;border:0;padding:0;word-break:normal;"
    "white-space:pre}"
    "table{border-collapse:collapse;width:100%;margin:1rem 0;font-size:12px;"
    "display:block;overflow-x:auto}"
    "th,td{border:1px solid var(--line);padding:.35rem .5rem;text-align:left;"
    "vertical-align:top;color:var(--ink-2)}"
    "th{background:var(--surface-2);color:var(--ink-3);font-weight:600}"
    "blockquote{margin:.8rem 0;padding-left:.8rem;"
    "border-left:3px solid var(--line-2);color:var(--ink-2)}"
    "hr{border:0;border-top:1px solid var(--line);margin:2rem 0}"
    ".docnav{font-size:12px;color:var(--ink-3)}"
    ".noimg{color:var(--ink-3);font-size:12px;font-style:italic}"
)

#: GitHub's slugger keeps hyphens and *underscores* and drops the rest of the
#: punctuation. The underscore matters here: half the headings in this
#: repository's documents are `mf_`, `item_rules`, `save_folder`.
_SLUG_DROP = re.compile(r"[^a-z0-9 \-_]")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
_ULI = re.compile(r"^\s*[-*+]\s+(.*)$")
_OLI = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_CODE_SPAN = re.compile(r"`([^`]+)`")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")


def slug(text):
    """A heading's anchor, GitHub's rules as far as this needs them.

    The links this exists for are `docs/SAFETY.md#the-ten-step-write-sequence`,
    written against GitHub's rendering: lower-cased, punctuation dropped,
    spaces to hyphens. If a link ever misses, it lands at the top of the right
    document rather than nowhere, which is the failure mode to prefer.
    """
    text = _CODE_SPAN.sub(r"\1", text or "").replace("`", "")
    text = _LINK.sub(r"\1", text)
    return _SLUG_DROP.sub("", text.strip().lower()).replace(" ", "-")


def doc_href(target):
    """Rewrite a markdown link so it resolves on this server, or drop it.

    Three kinds survive: an in-page `#anchor`, another shipped document
    (`SAFETY.md`, `docs/SAFETY.md`, `../README.md`, with or without an anchor)
    which becomes `/docs/<name>`, and an absolute `http(s)` URL, which is left
    alone because a document that cites a URL means that URL. Everything else
    -- `javascript:`, a `file:` path, a link to a source file that is not
    served -- returns None and the link is rendered as plain text: a page
    served by a tool that promises to touch nothing must not offer a link it
    cannot honour.
    """
    target = (target or "").strip()
    if not target:
        return None
    if target.startswith("#"):
        return target
    low = target.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return target
    if low.startswith("mailto:"):
        return target
    name, _sep, anchor = target.partition("#")
    if name.lower().endswith(".md"):
        base = posixpath.basename(name.replace("\\", "/"))
        return "/docs/%s%s%s" % (base, "#" if anchor else "", anchor)
    return None


def _inline(text):
    """Escaped text with code spans, images, links and bold turned into HTML.

    Code spans are lifted out first and put back last, so a backticked
    `[thing](not a link)` in a refusal sentence stays a backticked thing.
    Images are handled before links, because `![alt](x.png)` also matches the
    link pattern and would otherwise render as a stray `!`.
    """
    spans = []

    def hold(m):
        spans.append("<code>%s</code>" % _esc(m.group(1)))
        return "\x00%d\x00" % (len(spans) - 1)

    out = _CODE_SPAN.sub(hold, text or "")
    out = _esc(out)

    def image(m):
        """An image becomes its alt text.

        `docs/images/` is 6.8 MB of screenshots and a 3.4 MB GIF, and bundling
        it doubled the size of both executables for pictures this viewer
        cannot show anyway (it serves `.md` files and nothing else). The alt
        text is what a screen reader would have been given, so it is what a
        reader gets here; the pictures are in the repository and on the
        release page.
        """
        alt = m.group(1).strip()
        return ("<span class=\"noimg\">[image: %s]</span>" % alt) if alt \
            else "<span class=\"noimg\">[image]</span>"

    out = _IMAGE.sub(image, out)

    def link(m):
        href = doc_href(html.unescape(m.group(2)))
        label = m.group(1)
        if href is None:
            return label
        return "<a href=\"%s\">%s</a>" % (_esc(href), label)

    out = _LINK.sub(link, out)
    out = _BOLD.sub(r"<b>\1</b>", out)
    for i, span in enumerate(spans):
        out = out.replace("\x00%d\x00" % i, span)
    return out


def _table(rows):
    """`[[cell, ...], ...]` -> one `<table>`. Row two is the divider."""
    body = list(rows)
    head = None
    if len(body) > 1 and all(set(c) <= set("-: ") and c for c in body[1]):
        head, body = body[0], body[2:]
    out = ["<table>"]
    if head:
        out.append("<thead><tr>%s</tr></thead>"
                   % "".join("<th>%s</th>" % _inline(c) for c in head))
    out.append("<tbody>")
    for row in body:
        out.append("<tr>%s</tr>"
                   % "".join("<td>%s</td>" % _inline(c) for c in row))
    out.append("</tbody></table>")
    return "".join(out)


def markdown_html(text):
    """Enough markdown for the documents this repository ships, and no more.

    Headings (with anchors), paragraphs, fenced code, bullet and numbered
    lists, tables, block quotes, rules, and inline code, links and bold. No
    dependency, because the sorter is standard library only and a document
    viewer is not a reason to break that; and no HTML pass-through, because the
    input is a file on disk that this program did not write and the output goes
    into a page: every character is escaped, and the only tags in the result
    are the ones produced here.
    """
    out, para, items, list_tag, table = [], [], [], None, []
    fence, code = None, []

    def close_para():
        if para:
            out.append("<p>%s</p>" % _inline(" ".join(para)))
            del para[:]

    def close_list():
        if items:
            out.append("<%s>%s</%s>"
                       % (list_tag, "".join("<li>%s</li>" % _inline(i)
                                            for i in items), list_tag))
            del items[:]

    def close_table():
        if table:
            out.append(_table(table))
            del table[:]

    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if fence is not None:
            if stripped.startswith(fence):
                out.append("<pre><code>%s</code></pre>"
                           % _esc("\n".join(code)))
                del code[:]
                fence = None
            else:
                code.append(raw)
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            close_para()
            close_list()
            close_table()
            fence = stripped[:3]
            continue
        if stripped.startswith("|"):
            close_para()
            close_list()
            table.append([c.strip()
                          for c in stripped.strip("|").split("|")])
            continue
        close_table()
        if not stripped:
            close_para()
            close_list()
            continue
        m = _HEADING.match(stripped)
        if m:
            close_para()
            close_list()
            level = min(len(m.group(1)), 6)
            out.append("<h%d id=\"%s\">%s</h%d>"
                       % (level, _esc(slug(m.group(2))), _inline(m.group(2)),
                          level))
            continue
        if stripped in ("---", "***", "___"):
            close_para()
            close_list()
            out.append("<hr>")
            continue
        if stripped.startswith("> "):
            close_para()
            close_list()
            out.append("<blockquote>%s</blockquote>" % _inline(stripped[2:]))
            continue
        m = _ULI.match(line) or _OLI.match(line)
        if m:
            tag = "ul" if _ULI.match(line) else "ol"
            if items and tag != list_tag:
                close_list()
            list_tag = tag
            close_para()
            items.append(m.group(1))
            continue
        if items:
            # a wrapped list item: `- one long line` continued underneath
            items[-1] = "%s %s" % (items[-1], stripped)
            continue
        para.append(stripped)

    if fence is not None:                       # an unterminated fence
        out.append("<pre><code>%s</code></pre>" % _esc("\n".join(code)))
    close_para()
    close_list()
    close_table()
    return "\n".join(out)


def document(name, text, others=()):
    """One shipped document as a page. `others` names the rest, for the nav.

    Its own shell rather than `page()`: that one ends every page with "Nothing
    has been written to any save", which is true of a startup failure and is
    not a sentence to staple onto the bottom of `docs/SAFETY.md`.
    """
    nav = " · ".join("<a href=\"/docs/%s\">%s</a>" % (_esc(o), _esc(o))
                     for o in others if o != name)
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>%s</title><style>%s%s</style></head>\n<body><main>\n"
        "<p class=\"docnav\"><a href=\"/\">&larr; the sorter</a>%s</p>\n"
        "%s\n"
        "<hr>\n<p class=\"note\">Served from this machine by the sorter "
        "itself: %s as it shipped in this build.</p>\n"
        "</main></body></html>\n"
        % (_esc(name), STYLE, DOC_STYLE,
           (" &nbsp; " + nav) if nav else "",
           markdown_html(text), _esc(name)))


DOC_INDEX_H1 = "The documents this build ships."


def document_index(names):
    """The list at `/docs/`. Not a document itself, so it uses `page()`."""
    if not names:
        return page(DOC_INDEX_H1,
                    "<p>This build ships no copy of the documents. They are in "
                    "the repository, under <code>docs/</code>.</p>")
    rows = "".join("<li><a href=\"/docs/%s\">%s</a></li>" % (_esc(n), _esc(n))
                   for n in names)
    return page(DOC_INDEX_H1,
                "<p>Read from this machine; nothing here goes to the "
                "network.</p><ul>%s</ul>" % rows)


# ---------------------------------------------------------------------------
# refusal body for every other route while degraded
# ---------------------------------------------------------------------------

REFUSED_KIND = "refused"


def refusal(problem):
    """The one sentence every non-page route answers with while degraded."""
    return ("the sorter started in a reduced mode because %s, so this part of "
            "it is not available; the page at / says what to do" % problem)
