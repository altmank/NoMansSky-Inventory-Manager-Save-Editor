"""The local HTTP server. Standard library only, bound to 127.0.0.1.

No cloud, no telemetry, no auth, no framework. The page is one document plus
one stylesheet plus one script; everything else is a small JSON API.

Write access is gated three ways and all three must hold:
  * the process was not started with --read-only
  * `nms_sorter.safety` proves NMS.exe is not running, at the moment of the write
  * the fingerprint the operator approved matches a plan re-computed from the
    exact bytes about to be rewritten

Every response is JSON, including every error (GOAL.md §3.3):

    {"error": "one sentence for a person",
     "kind": "refused|invalid|not_found|internal",
     "where": "optional field path"}

`safety.Refused` -> 409, `app.Invalid` -> 400, an unknown route -> 404, and
anything else -> 500 carrying a short `ref` while the traceback goes to the log
and never to the browser.
"""
import json
import logging
import mimetypes
import os
import platform as _platform
import posixpath
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from . import config as cfgmod
from . import itemdb as itemdbmod
from . import naming
from . import pages
from . import planner
from . import platform as platformmod
from . import safety
from . import settings as settingsmod
from . import text
from . import savemodel
from .app import App, Invalid  # noqa: F401  (both re-exported: part of the CLI surface)
from .itemdb import db
from .savemodel import list_saves

#: where `static/index.html`, `app.js` and `app.css` are read from.
#: `platform.bundle_dir()` rather than `dirname(__file__)` because a
#: PyInstaller one-file build unpacks the package data under `sys._MEIPASS`,
#: and this is the one loader in the program that wants a real filesystem path
#: (it serves by path, length and mime type) rather than `importlib.resources`.
STATIC = os.path.join(platformmod.bundle_dir(), "static")

#: where `docs/*.md` is read from, in order. Two candidates and no copy step:
#:
#:   * `<bundle>/nms_sorter/docs` -- the frozen build. `NMS-Sorter.spec` maps
#:     the repository's `docs/` there, the same way it already maps `data/` and
#:     `static/`, so the exe carries the documents it was built from.
#:   * `<repo>/docs` -- a source run, read where the author edits them, so a
#:     document fixed in the working tree is the one the page serves.
#:
#: The alternative was a `packaging/sync_docs.py` copying `docs/*.md` into
#: `nms_sorter/docs/` as package data. That is more moving parts for the same
#: two cases and it puts a second copy of every document in the source tree,
#: which is a thing to forget to regenerate. The one case neither candidate
#: covers is a non-editable wheel install; `/docs/` says so in a sentence there
#: rather than pretending the documents are missing from the release.
DOC_DIRS = (os.path.join(platformmod.bundle_dir(), "docs"),
            os.path.join(os.path.dirname(platformmod.bundle_dir()), "docs"))

#: a config file is a few hundred KB; 8 MB is room for a pathological one and
#: a hard stop on anything that is not a config at all (GOAL.md §3.3).
MAX_BODY = 8 * 1024 * 1024
#: the shape version of this API, reported by GET /api/version
API_VERSION = 1
#: the most lines `GET /api/log` will return, whatever `tail` asks for. The log
#: rotates at 1 MB and the panel that reads this is a bug-report helper, not a
#: log viewer; 2,000 lines is more than anybody pastes and small enough that a
#: client cannot turn the route into a file download (GOAL.md §3.3).
LOG_TAIL_MAX = 2000
LOG_TAIL_DEFAULT = 200

log = logging.getLogger("nms_sorter.server")


# ---------------------------------------------------------------------------
# stopping
# ---------------------------------------------------------------------------

#: what `POST /api/quit` refuses with while an apply or a restore is running.
#: `docs/TROUBLESHOOTING.md` quotes it verbatim. A 409, like every other
#: refusal: the request was understood and the answer is no.
QUIT_BUSY = ("an apply or restore is running; wait for it to finish, then "
             "stop the sorter")

#: what the two start-over routes refuse with while an apply or a restore is
#: running. The same `App.busy` guard as `POST /api/quit`, its own sentence:
#: the next action here is not "stop the sorter", and a refusal that names the
#: wrong one is worse than a second sentence to maintain.
CONFIG_BUSY = ("an apply or restore is running; wait for it to finish, then "
               "try again")

#: the sentence the 200 carries. The page replaces itself with its own wording
#: afterwards; this is what a `curl` sees, and what goes in the log.
QUIT_STOPPING = "the sorter is stopping; you can close this tab"

#: how long the stop thread waits before pulling the server down. The response
#: is already on the wire when the thread starts -- `wfile` is unbuffered, and
#: `shutdown()` is deliberately not called from the request thread -- so this
#: is only the margin that lets `finish()` close the socket first. A client
#: that gets an RST instead of its 200 would be told the sorter crashed, which
#: is the one wrong thing this route could say.
QUIT_DELAY = 0.05


def shutdown_later(httpd, delay=QUIT_DELAY):
    """Stop `httpd` from a thread of its own. -> the thread.

    `ThreadingHTTPServer.shutdown()` blocks until `serve_forever` has left its
    loop, so calling it from the handler that is answering the request means
    the 200 is still unsent while the server is being torn down, and the
    handler cannot return until the loop notices. A separate daemon thread
    keeps the two apart: the request finishes normally, the connection closes,
    and `cli.main` falls out of `serve_forever` and returns 0.
    """
    def run():
        time.sleep(delay)
        try:
            httpd.shutdown()
        except Exception as exc:                  # pragma: no cover -- defence
            # There is nobody left to tell: the page has been answered and the
            # process is meant to be ending. The log is the only reader.
            log.error("the server did not stop cleanly (%s: %s)",
                      type(exc).__name__, exc)

    t = threading.Thread(target=run, name="nms-sorter-stop", daemon=True)
    t.start()
    return t


#: when the last request was answered, on `time.monotonic`'s clock. A
#: one-element list for the same reason `cli._last_log_path` is one: the module
#: is imported, the value is only ever overwritten, and a rebind through
#: `global` would be a second spelling of the same thing.
#:
#: This is what `cli.idle_watchdog` reads. Every request counts, which includes
#: `GET /api/game` -- the page polls it every 15 seconds, so an open tab can
#: never idle out, and that is the property the whole feature rests on.
_LAST_REQUEST = [time.monotonic()]


def touch():
    _LAST_REQUEST[0] = time.monotonic()


def idle_seconds():
    """How long since the last request this process answered. Never negative:
    a monotonic clock cannot go backwards, but the max costs nothing and makes
    a substituted clock in a test harmless."""
    return max(0.0, time.monotonic() - _LAST_REQUEST[0])


#: the two exceptions that mean "the save was read and this build will not act
#: on it", both answered as 409 `refused`. `SaveGate` is the expedition save
#: and the `Version` outside `savemodel.SUPPORTED_VERSIONS`; `UnsupportedSave`
#: is a document shape this build cannot navigate at all. Neither is bad input
#: and neither is an unexpected error, which are the only two other things the
#: dispatcher could call them.
SAVE_GATES = (savemodel.UnsupportedSave, savemodel.SaveGate)


def docs_dir():
    """-> the folder `docs/*.md` is served from, or None. See `DOC_DIRS`."""
    for folder in DOC_DIRS:
        if os.path.isdir(folder):
            return folder
    return None


#: served from *beside* the docs folder, because that is where they live in
#: both layouts: the repository root in a source run, and `nms_sorter/` in the
#: bundle, where the spec puts them. `docs/GUIDE.md` links to `../README.md`,
#: and a served document with a dead link in it is the same complaint this
#: route exists to answer.
DOC_SIBLINGS = ("README.md", "CHANGELOG.md")


def doc_map():
    """-> `{name: absolute path}` for every document this build serves.

    The map *is* the allow-list: a name that is not a key is a 404, so there is
    no path to sanitise and no traversal to defend against -- `..`, an absolute
    path and `static/app.js` are simply not keys. `README.md` first, then the
    `docs/` folder alphabetically, which is the order to read them in.
    """
    folder = docs_dir()
    out = {}
    if not folder:
        return out
    beside = os.path.dirname(folder)
    for name in DOC_SIBLINGS:
        path = os.path.join(beside, name)
        if os.path.isfile(path):
            out[name] = path
    try:
        names = sorted(n for n in os.listdir(folder)
                       if n.lower().endswith(".md")
                       and os.path.isfile(os.path.join(folder, n)))
    except OSError as exc:
        log.warning("the documents in %s could not be listed: %s", folder, exc)
        names = []
    for name in names:
        out.setdefault(name, os.path.join(folder, name))
    return out


def doc_names():
    return list(doc_map())


def read_doc(name):
    """-> (the real file name, its text), or (None, None).

    The name is matched against `doc_map` rather than joined onto a path, and
    `savemodel.match_name` is the comparison: the exact name, or the one name
    that differs from it by case alone. The *real* name is what comes back, so
    every link on the rendered page is spelled the way the file is.

    `os.path.normcase` was the comparison, and it does nothing on Linux:
    `readme.MD` -- a spelling a document's own link can carry -- was a
    document on Windows and a 404 for a source run on Linux. A player browsing
    from source is not told a file is not there because of its case.
    """
    if not name:
        return None, None
    docs = doc_map()
    real = savemodel.match_name(list(docs), name.strip())
    if real is None:
        return None, None
    with open(docs[real], encoding="utf-8", errors="replace") as fh:
        return real, fh.read()


def save_platform(save):
    """The save's own root `Platform` word -- `"Win|Final"` -- or None.

    Not the host's platform: this is what the *game* wrote, and the two answer
    different questions. `platform.platform()` says which machine the sorter
    ran on; this says which build of the game wrote the file, and a save from
    a platform this tool has never been pointed at (a console export, a Game
    Pass copy) is the first thing to establish in a bug report. The issue
    template asks for it, and the health panel's copy-as-text is what gets
    pasted, so it has to be in `/api/health` and not only in the save view.

    Read through `Doc.get`, because the key is obfuscated in a normal save
    (`8>q`) and in the clear in one that has been through `codec.remap`.
    """
    try:
        value = save.d.get(save.doc, "Platform")
    except Exception:
        return None
    if isinstance(value, str) and value.strip():
        return text.safe(value.strip())
    return None


def save_warnings(save):
    """`SaveFile.gates()` as a list.

    A gate that refuses raises; a gate that only warns -- "this save reports
    Version 4740 and 4735 is the newest one that was verified" -- has to reach
    the page, or the operator approves a plan drawn with a caveat nobody showed
    them (GOAL.md §3.3, P6-2).

    The `except` is not a seam: `gates()` reads the `mf_` and the document, and
    a warning that cannot be computed must not take down the save view that
    would have carried it.
    """
    try:
        rows = save.gates() or []
    except Exception as exc:
        return [{"level": "warning", "where": "save",
                 "message": "the save's own checks could not be read (%s: %s)"
                            % (type(exc).__name__, exc)}]
    return list(rows)


#: the one root this program probes, re-exported from `platform.py`. There is
#: no second strategy: GOG and Microsoft Store layouts were never observed
#: here, and "picking silently is how the wrong account gets sorted"
#: (GOAL.md Rule 1, §2.3).
SAVE_ROOT_PARTS = platformmod.SAVE_ROOT_PARTS


def save_root():
    """-> the %APPDATA% HelloGames/NMS root, or None off Windows, where no
    such path exists and a guess would sort the wrong account (P4-1)."""
    return platformmod.default_save_root()


def _playtime_words(secs):
    """`86400` -> `"24h played"`. The page's own spelling, in Python.

    Whole hours, or minutes under the hour: the first-run page is choosing
    between two accounts, and "24h played" against "3h played" decides it
    while "86400 seconds" does not.
    """
    try:
        secs = int(secs or 0)
    except (TypeError, ValueError):
        return None
    if secs <= 0:
        return None
    if secs >= 3600:
        return "%dh played" % (secs // 3600)
    return "%dm played" % max(1, secs // 60)


def folder_summary(path):
    """-> (summary, playtime) for the newest save in `path`, both or either
    None.

    W10: two `st_` profiles with one save each and the same timestamp are two
    ids a player has never seen. The save's own `mf_` carries the sentence the
    game shows -- "Aboard Iigash Station Sigma" -- and the play time, and the
    main page already renders both.

    `list_saves` reads the `mf_` partner of each save and never opens the save
    itself, which is the property that matters here: the first-run page must
    not decode a save to render itself, and on a folder that is not this
    build's it must not fail either. So every failure is None: a candidate row
    that cannot describe itself is still a candidate, and the id and the count
    above it are unaffected.
    """
    try:
        rows = list_saves(path)
    except Exception as exc:
        log.debug("no metadata summary for %s: %s", path, exc)
        return None, None
    for row in rows or []:                      # newest first
        meta = row.get("meta") or {}
        summary = (meta.get("summary") or "").strip() or None
        played = _playtime_words(meta.get("play_time"))
        if summary or played:
            return summary, played
    return None, None


def detect_saves():
    """-> ({folders, root, root_exists}) for the first-run page.

    Reads directory listings under %APPDATA%/HelloGames/NMS, plus the `mf_`
    metadata of the newest save in each candidate (W10, `folder_summary`): no
    save file is opened, no save is decoded, and no other folder on the machine
    is looked at. A folder qualifies if it is named `st_*` or `DefaultUser`, or
    if it is the root itself and holds `save*.hg` (which is what a hand-copied
    folder looks like).
    """
    root = save_root()
    out = {"root": root, "root_exists": bool(root) and os.path.isdir(root),
           "folders": []}
    if not out["root_exists"]:
        if root is None:
            # Not an empty %APPDATA%: there is no such path here at all, and
            # the page says so rather than implying the folder is missing.
            out["error"] = platformmod.SAVE_ROOT_UNSUPPORTED
        return out
    try:
        names = sorted(os.listdir(root))
    except OSError as exc:
        out["error"] = "%s" % exc
        return out
    cands = [os.path.join(root, n) for n in names
             if (n.lower().startswith("st_") or n.lower() == "defaultuser")
             and os.path.isdir(os.path.join(root, n))]
    if settingsmod.save_count(root):
        cands.append(root)
    for path in cands:
        newest, count = None, 0
        try:
            for fn in os.listdir(path):
                low = fn.lower()
                if low.startswith("save") and low.endswith(".hg"):
                    count += 1
                    m = os.stat(os.path.join(path, fn)).st_mtime
                    newest = m if newest is None else max(newest, m)
        except OSError:
            continue
        summary, played = folder_summary(path) if count else (None, None)
        out["folders"].append({
            "path": path,
            "save_count": count,
            "newest_save_mtime": int(newest) if newest else None,
            # Local time, like every other timestamp a player is shown (W17).
            "newest_save_label": (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(newest))
                if newest else None),
            "newest_save_summary": summary,
            "newest_save_playtime": played,
        })
    out["folders"].sort(key=lambda f: f["newest_save_mtime"] or 0, reverse=True)
    return out


def stored_config_stamp(path, fallback=None):
    """The `updated` stamp of the configuration *file*. -> a string, or
    `fallback`.

    W7's comparison point. Read from disk on every save rather than from
    `app.config`, because the other writer this guards against may be a
    second copy of the sorter: an in-memory stamp only ever knows about this
    process's own writes.

    A file that will not read is not this check's problem -- `load` and the
    reduced-mode page own that -- so it answers with what memory says and lets
    the write proceed. Refusing a save because the check itself failed would
    make an unreadable file unfixable from the page.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            body = json.load(fh)
    except (OSError, ValueError, TypeError):
        return fallback
    if isinstance(body, dict) and isinstance(body.get("updated"), str):
        return body["updated"]
    return fallback


def readable_stamp(stamp):
    """`2026-09-14T19:23:32` -> `2026-09-14 19:23:32`.

    `config.store` writes a local-time ISO stamp, which is already the right
    clock for a sentence a player reads (W17); the `T` is the only part of it
    that is machine punctuation.
    """
    return str(stamp or "").replace("T", " ")


def missing_save_folder(app):
    """-> the configured save folder, when it is not there at all; else None.

    W5. The difference this answers is between "a folder was chosen and the
    drive it is on is not plugged in" and "no folder has been chosen yet".
    `Settings.folder_ok()` is false for both, so the first-run page was served
    for both, and the missing-drive case was then told "that folder holds no
    save*.hg files" about a path it never printed.

    `os.path.isdir` and nothing else: a folder that exists but holds no save is
    the *other* case and keeps the first-run page, which is where the type-in
    field is.
    """
    folder = getattr(app, "save_folder", None)
    if not folder:
        return None
    try:
        if os.path.isdir(folder):
            return None
    except OSError:
        pass
    return folder


def data_version():
    """The game build the shipped item table was generated from, or None.

    `data/DATA_VERSION` is a `key=value` block (`game_build`, `mbincompiler`,
    `generated`, `generator`), so the game build is lifted out of it: this
    value lands in a health panel and a plan banner, where a four-line blob
    would be useless.

    Read through `itemdb`, which reads it with `importlib.resources`. This used
    to open `os.path.join(dirname(__file__), "data", ...)` directly, which is
    the one spelling GOAL.md §3.1 forbids: it works in a source checkout and
    fails in a wheel and in a PyInstaller bundle. The frozen build found it
    immediately -- `/api/version` and `/api/bootstrap` both returned 500.
    """
    try:
        pairs = itemdbmod.data_version()
    except Exception:
        # A data file that will not read is the `data_missing` startup page's
        # problem, not this view's: the version line is decoration and must
        # not be the reason a health panel 500s.
        return None
    build = (pairs.get("game_build") or "").strip()
    if build:
        return build
    for value in pairs.values():
        if value.strip():
            return value.strip()
    return None


def move_aside(path):
    """Rename a file out of the way, keeping it. -> the new path, or None.

    Used by the config-unreadable repair. `.bad-<stamp>` rather than `.bak`,
    because `.bak` is the rotation `config.store` owns and a broken file must
    never land in that series.
    """
    if not path or not os.path.exists(path):
        return None
    dest = "%s.bad-%s" % (path, safety.stamp())
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = "%s.bad-%s-%d" % (path, safety.stamp(), n)
    os.replace(path, dest)
    return dest


def version_view():
    return {
        "app": __version__,
        "api": API_VERSION,
        "data_version": data_version(),
        "config_version": cfgmod.CONFIG_VERSION,
        "python": _platform.python_version(),
        "platform": _platform.platform(),
    }


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------

def _name_of(by_key, key):
    c = by_key.get(key)
    return c["label"] if c else key


def annotate_strays(conts, cfg):
    """Mark the stacks sitting in a container its own buckets do not name.

    This answers "why is there junk in my Raw Resources chest?" in the container
    itself. A stray has one of four reasons and they are not interchangeable:

      unrouted    its bucket has no destination at all, so nothing will ever
                  move it. This is the common one, and the config is the fix.
      elsewhere   its bucket is routed somewhere else; the next run moves it.
      not_source  it would move, but nothing is ever taken out of this container.
      never       unsorted, or a never_bucket: deliberately left alone.
    """
    idb = db()
    dests = {}
    for r in (cfg.get("bucket_rules") or []):
        dests.setdefault(r.get("bucket"), []).append(r.get("store"))
    pinned = {}
    for r in (cfg.get("item_rules") or []):
        if r.get("store"):
            pinned[(r.get("item") or "").strip()] = r["store"]
    never = (set(cfg.get("never_buckets") or [])
             | {"unsorted"} | set(itemdbmod.UNROUTABLE))
    sources = set(cfg.get("sources") or [])
    by_key = dict((c["key"], c) for c in conts)

    for c in conts:
        key = c["key"]
        own = set(b for b, stores in dests.items() if key in (stores or []))
        is_src = key in sources
        strays, counts = [], {}
        for it in c.get("items") or []:
            b = it.get("bucket")
            want = pinned.get(it.get("stem")) or pinned.get(it.get("id"))
            if want == key or (not want and b in own):
                continue
            if not want and b in never:
                why = "never"
                reason = "%s is never sorted; it stays where it is" % idb.label(b)
            elif want or dests.get(b):
                target = _name_of(by_key, want or dests[b][0])
                if is_src:
                    why, reason = "elsewhere", "belongs in %s; the next run moves it" % target
                else:
                    why = "not_source"
                    reason = ("belongs in %s, but this container is not a source, "
                              "so nothing ever leaves it" % target)
            else:
                why = "unrouted"
                reason = ("%s has no destination configured, so this stays put"
                          % idb.label(b))
            strays.append({"i": it["i"], "why": why, "reason": reason})
            counts[why] = counts.get(why, 0) + 1
        c["strays"] = strays
        c["stray_counts"] = counts
        c["is_source"] = is_src
        c["own_buckets"] = sorted(idb.label(b) for b in own)
    return conts


#: the section the page no longer has, and the five keys that were in it or
#: read like it. Removed from the page, not from the model: the planner still
#: refuses a technology grid with its own sentence, `Container.is_tech` and
#: `savemodel.NEVER_SORT` are unchanged, and `App.container_keys` still names
#: them so a hand-edited rule pointing at one is refused with the sentence it
#: has always been refused with ("not sortable right now").
#:
#: Why it goes: nothing is ever sorted into or out of one, so every picker
#: already filtered them out and all that was left was 12 cards of somebody
#: else's machinery on the first tab a player sees -- 116 stacks whose Amount
#: is a charge level rather than a count, under a heading that invited reading
#: them as storage.
HIDDEN_SECTION = savemodel.TECH
HIDDEN_KEYS = frozenset(savemodel.NEVER_SORT)


def hidden_from_the_page(view):
    """Is this container view one the page no longer shows?

    Three kinds. A technology grid, which nothing is ever sorted into. A
    single-cell machine that holds its own bait or its own cooker. And a grid
    with no cells at all, which can never hold anything: on all seven
    readable corpus saves that is every starship's `Inventory_Cargo` and the
    exosuit's, because Waypoint (4.0) merged those into the main grid and the
    game gives a player no way to add a cell back. The freighter is the one
    vessel that still has a real second grid.

    The model keeps all three so a hand-edited rule naming one is answered
    rather than silently ignored.
    """
    return bool(view.get("is_tech")
                or view.get("section") == HIDDEN_SECTION
                or view.get("key") in HIDDEN_KEYS
                or view.get("no_cells"))


def display_label(name):
    """A `labels` value as a name to draw, or None for "no name here".

    Three things, all of them about a value typed into the file by hand rather
    than through the rename box (review 4, findings 4 and 5):

      * a value that is not text is no name at all, so the card goes back to
        the name the save and the sorter work out between them, rather than
        rendering `42` or `['x']`;
      * whitespace is the same as empty, which is what `docs/RULES.md` has
        always said and what the rename box already does with `.trim()`;
      * and it is cut to `config.LABEL_MAX`, because a card, three `<option>`
        lists and the plan's container table all draw this string and none of
        them can lay out 300 characters.
    """
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name:
        return None
    return naming._fit(name, cfgmod.LABEL_MAX)


def extractor_totals(cores):
    """-> [{id, name, amount, max}] per substance, summed across the cores.

    In the save's own slot order, first seen first: a core holds Chromatic
    Metal *and* the four gases, and the order is the game's. The page used to
    total Chromatic Metal alone and say "mining Chromatic Metal into a buffer",
    so a freighter banking 3,000 Oxygen showed zeros and a sentence that was
    wrong about what the machinery does.
    """
    order, by_id = [], {}
    for core in cores or []:
        for s in core.get("substances") or []:
            key = text.stem(s.get("id") or "")
            if key not in by_id:
                order.append(key)
                by_id[key] = {"id": s.get("id"), "name": s.get("name"),
                              "amount": 0, "max": 0}
            row = by_id[key]
            row["amount"] += s.get("amount") or 0
            row["max"] += s.get("max") or 0
            if not row["name"] and s.get("name"):
                row["name"] = s.get("name")
    return [by_id[k] for k in order]


def last_apply(app, save):
    """-> `{created, folder}` when the bytes on disk are this tool's own last
    apply of `save`, else None.

    W8: the page warns "this file was modified <t>, later than the game's own
    stamp: something other than the game has written to it" whenever the
    filesystem mtime and the `mf_`'s timestamp are more than a minute apart.
    The apply deliberately leaves the game's stamp alone while the mtime
    becomes now, so after a player's first successful sort that warning was
    permanent on that save -- and it read as an accusation of tampering by the
    one program that was entitled to write there.

    The backup manifest is what can tell the difference, because `safety`
    records the hash of what it wrote. So: newest backup first, find the one
    that names this save in this folder, and compare its `written.save.sha256`
    against the save's current digest. Equal means nothing has written since.
    Not equal -- the game saved again, or something else did -- keeps the
    warning, which is the direction to fail in.

    Every read is through `.get`, and `written` is the field the write path is
    growing right now: a manifest without it answers None and the page falls
    back to the old sentence.
    """
    try:
        digest = save.digest()
    except Exception:
        return None
    if not digest:
        return None
    want = os.path.normcase(os.path.basename(save.path))
    folder = os.path.normcase(os.path.dirname(os.path.abspath(save.path)))
    for row in (app.list_backups() or []):
        try:
            man = safety.read_manifest(row.get("folder")) or {}
        except Exception:
            continue
        named = ((man.get("save") or {}) if isinstance(man, dict) else {})
        if os.path.normcase(str(named.get("file") or "")) != want:
            continue
        taken = man.get("save_dir")
        if taken and os.path.normcase(str(taken)) != folder:
            continue                    # a backup of another profile's save
        written = man.get("written")
        wrote = ((written or {}).get("save") or {}) \
            if isinstance(written, dict) else {}
        sha = wrote.get("sha256")
        if not sha:
            # Either an older manifest or one the write path did not finish.
            # Only the newest backup for this save can answer, so stop here
            # rather than crediting an earlier apply with these bytes.
            return None
        if str(sha).lower() != str(digest).lower():
            return None
        return {"created": man.get("created"), "folder": row.get("folder")}
    return None


def save_view(save, labels=None, cfg=None, applied=None):
    """`labels` lets the operator name a container the save does not name -- the
    seven exocraft slots carry no name at all, and guessing which is a Roamer
    from a grid size is exactly the kind of inference this project has been
    burned by three times.

    `applied` is `last_apply`'s answer, passed in rather than computed here
    because it needs the backup root and this function is given a save.
    """
    difficulty = save.difficulty()
    # Not `labels or {}`: this field is hand editable and this function draws
    # the whole Save section, so a string, a number or a list here used to
    # raise out of /api/select and /api/bootstrap and leave a player with a
    # config that saved cleanly and a section that 500s. `config.validate`
    # refuses all of those shapes now; this guard stays, because a file that is
    # already on disk was never validated by this build.
    labels = labels if isinstance(labels, dict) else {}
    named = []
    for c in save.containers():
        v = c.view(difficulty)
        nm = display_label(labels.get(c.key))
        if nm:
            v["label"] = nm
            v["renamed"] = True
        named.append(v)
    # A vessel's grids are headed by the vessel's main grid, so a rename
    # through `labels` has to carry to the heading: the rename box writes
    # exactly that key, and a card headed "Starship 2" over a grid the player
    # has named something else is two answers to one question.
    #
    # Taken over every grid, before the ones the page does not draw are
    # dropped: the freighter's second grid is the only one a player sees
    # beside its own vessel, and on a save where the *first* grid is the dead
    # one the heading would otherwise have nothing to come from.
    heads = dict((v["key"], v["label"]) for v in named)
    # How many vessel slots the player does not own, counted over every grid
    # the save carries rather than over the ones that survive the filter
    # below: an unowned slot usually has no cells either, so it is dropped
    # twice over and the page could not count what it was not sent. One
    # sentence on the Save section says what is not drawn, and this is the
    # only number in it.
    unowned = set(v["vessel"] for v in named
                  if v.get("slot_empty") and v.get("vessel"))
    conts = []
    for v in named:
        if v.get("vessel") in heads:
            v["vessel_label"] = heads[v["vessel"]]
        if hidden_from_the_page(v):
            continue
        conts.append(v)
    for c in save.extractor_containers():
        v = c.view(difficulty)
        v["drain_only"] = True
        v["sortable"] = False
        v["section"] = savemodel.EXTRACTORS
        conts.append(v)
    if cfg:
        annotate_strays(conts, cfg)
    # The *live* cores only, in the order `extractor_containers` keys them, so
    # the card's "core 3" and a rule's `extractor3` are the same extractor. A
    # buffer left behind by a rebuilt room is counted in `stale` and shown
    # nowhere: the game stopped reading it when the room went, so its contents
    # are not inventory, and a card showing 350 Chromatic Metal that the sorter
    # will never move would be its own kind of lie.
    report = save.extractor_report()
    rooms = report["rooms"]
    cores = []
    chromatic = 0
    cap = 0
    for n, w in enumerate(save.extractor_match()["live"]):
        e = w["core"]
        subs = e.substances()
        for s in subs:
            if text.stem(s["id"]) == "STELLAR2":
                chromatic += s["amount"]
                cap += s["max"]
        pos = w["position"]
        cores.append({"index": e.index, "key": "extractor%d" % (n + 1),
                      "room": w["room"],
                      "position": (None if pos is None
                                   else [round(float(p), 1) for p in pos]),
                      "substances": subs})
    return {
        "file": os.path.basename(save.path),
        "path": save.path,
        "signature": save.signature(),
        "version": save.version(),
        # what the *game* wrote, e.g. "Win|Final"; the host's platform is a
        # separate field on /api/health and they answer different questions.
        "save_platform": save_platform(save),
        # the save's own warnings, not the config's: "verified up to 4735" is
        # a caveat on the plan, and a caveat nobody is shown is not a caveat.
        "gates": save_warnings(save),
        "difficulty": difficulty,
        "units": save.units(),
        "freighter": save.freighter_name(),
        "containers": conts,
        # The order the page draws the sections in. The extractors are last
        # and separate: a core is machinery on a freighter base, not a vessel
        # in the fleet, and reading fourteen read-only buffers under "Fleet"
        # invited the question the owner asked.
        "sections": ["Personal", "Storage", "Base", "Ships", "Exocraft",
                     "Fleet", savemodel.EXTRACTORS],
        "unowned_slots": len(unowned),
        "extractors": {
            "cores": cores,
            "count": len(cores),
            "rooms": rooms,
            # Stale buffers are a normal fact about a played save, not a
            # problem: the room was rebuilt, the live buffer is right here and
            # offered, and there is nothing for anyone to do. One number and
            # one sentence, rather than three cards of numbers the game
            # ignores.
            "stale": len(report["stale"]),
            # These two are the reasons something is *missing*, and they are
            # what `consistent` now means: a room whose buffer could not be
            # found, and a buffer that belongs to no room.
            "unplaced": len(report["unplaced"]),
            "coreless": len(report["coreless"]),
            "issue": save.extractor_issue,
            "consistent": save.extractor_issue is None,
            # Every substance the cores hold, summed, in the save's slot
            # order: a core mines gas as well as metal.
            "substances": extractor_totals(cores),
            # Kept beside it because the header's "Chromatic banked" stat and
            # the per-core bar are both about that one substance.
            "chromatic": chromatic,
            "chromatic_cap": cap,
        },
        # W8: `{created, folder}` when these bytes are this tool's own last
        # apply, so the page can say so instead of warning about them.
        "last_apply": applied,
    }


def save_view_for(app, save):
    """`save_view` with everything this application knows about that save.

    One place, because there are five routes that render a save and the W8
    "sorted by this tool" answer has to be on all of them or the header
    contradicts itself the moment the page re-reads the save.
    """
    return save_view(save, app.config.get("labels"), app.config,
                     applied=last_apply(app, save))


def restored_save_name(res):
    """Which save `safety.restore` just put back. -> a file name, or None.

    `restore` answers `{restored: [<file names>], backup, manifest}` -- two
    names, the save and its `mf_`, in the order they were written -- so the one
    to re-read is the one that is not the metadata. The manifest is asked first
    because that is the field that *names* the save; the list is the fallback.
    None means "whatever is selected", which is what a restore into the
    currently selected slot amounts to anyway.
    """
    if not isinstance(res, dict):
        return None
    man = res.get("manifest") or {}
    named = ((man.get("save") or {}) if isinstance(man, dict) else {}).get("file")
    if isinstance(named, str) and named:
        return named
    rows = res.get("restored")
    if isinstance(rows, str):
        rows = [rows]
    for name in (rows or []):
        if isinstance(name, str) and not os.path.basename(name).startswith("mf_"):
            return name
    return None


def save_row(row):
    """One row of `list_saves` as the API emits it.

    `sizes_match` -- does the `mf_`'s `size_disk` agree with the save's actual
    size -- is the one field of the metadata summary the save *list* has to
    carry: a disagreeing pair is the state R3 found unmarked, and the page
    flags the row before the operator selects it. Lane B is lifting it to the
    row; until then it is read out of the summary, so the key exists either
    way rather than appearing when their change lands.
    """
    out = dict(row)
    if out.get("sizes_match") is None:
        out["sizes_match"] = (row.get("meta") or {}).get("sizes_match")
    return out


def backups_view(app):
    """`app.list_backups()` plus each manifest's `created`, where it has one.

    W17: the folder name is a local-time stamp (`20260914-192329-save10`) and
    the manifest records UTC (`2026-09-15T00:23:32Z`), so the two disagreed by
    five hours in the operator's own run and the card showed only one of them.
    The card now shows both -- the folder's stamp, and when the apply itself
    was recorded -- and the page renders the UTC one in local time.

    A row whose folder has no readable manifest is passed through untouched
    rather than given `created: null`: `safety.list_backups` already says
    `has_manifest`, and this route's contract is that it adds nothing it cannot
    read.
    """
    out = []
    for row in (app.list_backups() or []):
        row = dict(row)
        try:
            man = safety.read_manifest(row.get("folder")) or {}
        except Exception:
            man = {}
        created = man.get("created") if isinstance(man, dict) else None
        if created:
            row["created"] = created
        out.append(row)
    return out


def buckets_view():
    """Every category, with `routable` saying whether a rule may name it.

    `system_meta` is not routable: nothing in it is ever in a container slot,
    so a destination for it is a row that can never fire. It stays on this
    list because the Items table is a lookup and every item has a category --
    hiding it there would leave 134 ids with no answer to "what is this" --
    and the page leaves it out of every routing control instead.
    """
    idb = db()
    return [{"key": b["key"], "label": b["label"], "note": b["note"],
             "custom": bool(b.get("custom")),
             "routable": b["key"] not in itemdbmod.UNROUTABLE,
             "items": sum(1 for v in idb.by_id.values() if v == b["key"])}
            for b in idb.buckets]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class NotFound(Exception):
    """An unknown route, or a static file that is not there -> HTTP 404.

    `sentence` overrides the default body for the case where the route exists
    but the *thing* does not, and the difference matters to the operator: "the
    log is going to stderr for this run" and "there is nothing at /api/log on
    this server" are both 404 and only one of them is true.
    """

    def __init__(self, what="", sentence=None):
        Exception.__init__(self, what)
        self.sentence = sentence


class Forbidden(Exception):
    """The Origin header named a page this server did not serve -> 403."""


class TooLarge(Exception):
    """A request body over MAX_BODY -> 413."""


class WrongType(Exception):
    """A POST that is not application/json -> 415."""


class Unavailable(Exception):
    """The server started degraded and this route needs the part that failed
    -> 503, with the one error shape and `kind: refused` (GOAL.md P2-2)."""


def allowed_origins(port):
    """The Origin headers a browser on this machine may legitimately send.

    A page served from this port is the only thing meant to call this API. Any
    other origin is a page the operator did not open here, so the answer is a
    refusal rather than a CORS negotiation: the drive-by class of issue is real
    for every tool that listens on localhost.
    """
    return ("http://127.0.0.1:%d" % port, "http://localhost:%d" % port)


FORM_TYPE = "application/x-www-form-urlencoded"


def _form_settings(raw):
    """A settings object out of a `<form>` body.

    The startup pages have to work with scripting switched off (P2-2: "plain
    HTML ... no JS dependency"), and a form cannot post JSON. Only the fields
    `settings.FIELDS` names are read, coerced to that field's type; anything
    else is dropped here rather than becoming a warning about a key the
    operator never typed. A checkbox that is off is simply absent, so a
    hidden `<input name="read_only" value="false">` is what sets one to false.
    """
    pairs = urllib.parse.parse_qs(raw.decode("utf-8", "replace"),
                                  keep_blank_values=True)
    out = {}
    for f in settingsmod.FIELDS:
        if f["key"] not in pairs:
            continue
        v = (pairs[f["key"]] or [""])[-1]
        if f["type"] == "bool":
            out[f["key"]] = v.strip().lower() in ("1", "true", "on", "yes")
        elif f["type"] == "int":
            try:
                out[f["key"]] = int(v.strip())
            except ValueError:
                raise Invalid("%s has to be a whole number, not %r"
                              % (f["key"], v[:40]), where=f["key"])
        else:
            out[f["key"]] = v
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "nms-sorter"
    app = None

    # The stdlib default is HTTP/1.0: one response per connection, so an error
    # cannot desynchronise a kept-alive stream. It still has to *consume* the
    # body it is refusing -- closing a socket with unread bytes in the receive
    # buffer sends RST on Windows and the client sees a connection error
    # instead of the JSON. Hence `_read_body` runs before the Origin and
    # content-type checks, and only an over-cap body is refused unread.

    def log_message(self, fmt, *args):
        """Silence the stderr common-log line; `_dispatch` logs one line."""
        log.debug("%s %s", self.address_string(), fmt % args)

    def log_error(self, fmt, *args):
        log.warning("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------- plumbing

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._status = code
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _html(self, body, code=200):
        self._send(code, body, "text/html; charset=utf-8")

    def _redirect(self, location, code=303):
        """A form post answers with a redirect, so the startup pages work with
        scripting switched off and a reload does not re-post."""
        self._status = code
        self.send_response(code)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, allow_nan=False,
                                    default=float))

    def _err(self, code, kind, message, where=None, ref=None, steps=None):
        out = {"error": message, "kind": kind}
        if where:
            out["where"] = where
        if ref:
            out["ref"] = ref
        if steps:
            # Only a refusal carrying a `Report` has these, and only then is
            # the key present: "no steps" and "the run got nowhere" are
            # different, and an empty list would say the second (GOAL.md §3.3
            # keeps the error shape minimal, so this is the one addition).
            out["steps"] = list(steps)
        self._json(out, code)

    def _read_body(self):
        """-> the raw request body, or TooLarge without reading a byte of it.

        An over-cap body is the one case left unread: draining what a hostile
        client merely *announced* is how a local tool hangs a worker thread.
        """
        raw_len = self.headers.get("Content-Length") or "0"
        try:
            n = int(raw_len)
        except ValueError:
            raise Invalid("the Content-Length header was not a number",
                          where="Content-Length")
        if n > MAX_BODY:
            raise TooLarge("that request body is bigger than the %d MB this "
                           "server accepts" % (MAX_BODY // (1024 * 1024)))
        if n <= 0:
            return b""
        return self.rfile.read(n)

    @staticmethod
    def _parse_body(raw):
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise Invalid("the request body was not valid JSON")
        if not isinstance(body, dict):
            raise Invalid("the request body has to be a JSON object")
        return body

    def _static(self, rel):
        rel = posixpath.normpath("/" + rel).lstrip("/")
        path = os.path.abspath(os.path.join(STATIC, rel.replace("/", os.sep)))
        try:
            inside = os.path.commonpath([path, STATIC]) == STATIC
        except ValueError:        # different drives: outside, by definition
            inside = False
        if not inside or not os.path.isfile(path):
            raise NotFound(rel)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        with open(path, "rb") as fh:
            data = fh.read()
        self._send(200, data, ctype)

    # ----------------------------------------------------------- validation

    @staticmethod
    def _qint(q, name, default, low=None, clamp=None):
        raw = (q.get(name) or [None])[0]
        if raw is None or raw == "":
            v = default
        else:
            try:
                v = int(raw)
            except ValueError:
                raise Invalid("%s has to be a whole number, not %r"
                              % (name, raw[:40]), where=name)
        if low is not None and v < low:
            raise Invalid("%s cannot be below %d" % (name, low), where=name)
        if clamp is not None and v > clamp:
            v = clamp
        return v

    @staticmethod
    def _token(body, name):
        """A fingerprint is an opaque token this server minted, so anything
        that is not a short printable token is bad input, not a refusal."""
        v = body.get(name)
        if v is None or v == "":
            return None
        if not isinstance(v, str) or len(v) > 128 \
                or not all(32 < ord(c) < 127 for c in v):
            raise Invalid("%s is not a token this server could have issued"
                          % name, where=name)
        return v

    def _check_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return                              # a non-browser client: fine
        if origin in allowed_origins(self.server.server_address[1]):
            return
        raise Forbidden("requests from another origin are refused; open the "
                        "page from this server's own address")

    def _is_form(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        return ctype.lower() == FORM_TYPE

    def _check_content_type(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype.lower() != "application/json":
            raise WrongType("this API takes JSON; send the request with "
                            "Content-Type: application/json")

    # -------------------------------------------------------------- routing

    def do_GET(self):
        self._dispatch("GET", self._route_get)

    def do_POST(self):
        self._dispatch("POST", self._route_post)

    def _dispatch(self, method, route):
        t0 = time.time()
        # Before the route runs, and for every route including the ones that
        # refuse: "the page is still there" is what this timestamp means, and a
        # request that was answered with a 400 proves that as well as a 200
        # does. The idle watchdog in `cli` is the only reader.
        touch()
        self._status = 500
        try:
            route()
        except safety.Refused as exc:
            log.warning("%s %s refused: %s", method, self.path, exc)
            # Q1: a refusal that happened *after* the backup was taken -- or
            # after the save was written and a check then failed -- carries the
            # report of everything that did happen. The 409 hands those steps
            # to the page, which renders `steps` on a failure exactly as it
            # does on a success; without them the operator is told one sentence
            # about a write that got a long way through. `getattr` because
            # `Refused.report` is only set at the sites that have a report to
            # attach: a refusal raised before there is one has none, and that
            # is not a seam.
            # `where` for the same reason `Invalid` carries one: the two-tab
            # refusal (W7) has a control the page should offer -- "reload from
            # disk" -- and a page that had to match on the sentence to find it
            # would break the next time the sentence is reworded. Only the
            # sites that have a field to name set it.
            self._err(409, "refused", str(exc), getattr(exc, "where", None),
                      steps=getattr(getattr(exc, "report", None), "steps", None))
        except SAVE_GATES as exc:
            # A gate is a refusal, not bad input: the request was understood,
            # the save was read, and this build will not act on it (GOAL.md
            # Rule 1). Both this and `Invalid` are `ValueError` subclasses but
            # neither is the other's, so the clause has to exist: without it
            # `UnsupportedSave` fell through to the 500 handler and an
            # expedition save read as "the server hit an error".
            # `/api/bootstrap` is unaffected -- it catches the same exception
            # itself and answers 200 with `issues`, because the page has to
            # render its settings even when no save can be loaded.
            log.warning("%s %s refused by a save gate: %s", method, self.path,
                        exc)
            self._err(409, "refused", str(exc))
        except Invalid as exc:
            log.warning("%s %s invalid: %s", method, self.path, exc)
            self._err(400, "invalid", str(exc), getattr(exc, "where", None))
        except Forbidden as exc:
            log.warning("%s %s from origin %s refused", method, self.path,
                        self.headers.get("Origin"))
            self._err(403, "refused", str(exc))
        except TooLarge as exc:
            log.warning("%s %s body too large (%s bytes)", method, self.path,
                        self.headers.get("Content-Length"))
            self._err(413, "invalid", str(exc))
        except WrongType as exc:
            log.warning("%s %s wrong content type (%s)", method, self.path,
                        self.headers.get("Content-Type"))
            self._err(415, "invalid", str(exc))
        except Unavailable as exc:
            log.warning("%s %s unavailable: %s", method, self.path, exc)
            self._err(503, "refused", str(exc))
        except NotFound as exc:
            self._err(404, "not_found",
                      getattr(exc, "sentence", None)
                      or "there is nothing at %s on this server" % self.path)
        except Exception as exc:
            ref = uuid.uuid4().hex[:8]
            log.error("%s %s failed (ref %s)", method, self.path, ref,
                      exc_info=True)
            self._err(500, "internal",
                      "the server hit an error it did not expect (%s); the "
                      "details are in the log, quote %s in a bug report"
                      % (type(exc).__name__, ref), ref=ref)
        finally:
            log.info("%s %s %s %dms", method, self.path,
                     getattr(self, "_status", "-"),
                     (time.time() - t0) * 1000)

    def _route_get(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        deg = getattr(self.app, "degraded", None)
        if deg is not None:
            return self._degraded_get(deg, u.path)
        if u.path == "/api/settings":
            return self._json(self.get_settings())
        if u.path == "/api/health":
            return self._json(self.health())
        if u.path == "/api/backups":
            return self._json({"root": self.app.backup_root,
                               "backups": backups_view(self.app)})
        if u.path == "/api/log":
            return self._json(self.log_tail(
                self._qint(q, "tail", LOG_TAIL_DEFAULT, low=0,
                           clamp=LOG_TAIL_MAX)))
        if u.path == "/api/detect-saves":
            return self._json(detect_saves())
        if u.path == "/api/config/kept":
            return self._json(self.kept_configs())
        if u.path in ("/", "/index.html"):
            # The first-run page is not a startup failure: the application is
            # up, it simply has no folder to read yet. So this is decided per
            # request, and the moment POST /api/settings names a folder with a
            # save in it, the same URL serves the application (P2-3).
            #
            # W5: a folder that was chosen and is now missing is a third
            # answer, and it is checked first. It is not the first run -- there
            # is a settings file and it names a folder -- and the page for it
            # must not be the one that offers a different account.
            gone = missing_save_folder(self.app)
            if gone:
                return self._html(pages.missing_folder(gone))
            if self.app.first_run:
                return self._html(self.first_run_page())
            return self._static("index.html")
        if u.path == pages.CHOOSE_FOLDER_PATH:
            # The picker, asked for by name. `/` cannot serve it while the
            # configured folder is missing, and the missing-folder page has to
            # be able to link to it (W5).
            return self._html(self.first_run_page(chosen=False))
        if u.path.startswith("/static/"):
            return self._static(u.path[len("/static/"):])
        if u.path == "/docs" or u.path.startswith("/docs/"):
            return self.serve_doc(u.path)
        if u.path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if u.path == "/api/version":
            return self._json(version_view())
        if u.path == "/api/bootstrap":
            return self._json(self.bootstrap())
        if u.path == "/api/game":
            return self._json(safety.game_status())
        if u.path == "/api/save":
            s = self.app.save((q.get("file") or [None])[0], reload=True)
            return self._json(save_view_for(self.app, s))
        if u.path == "/api/browse":
            idb = db()
            rows, total = idb.browse(
                q=(q.get("q") or [""])[0],
                bucket=(q.get("bucket") or [""])[0] or None,
                kind=(q.get("kind") or [""])[0] or None,
                only=(q.get("only") or [""])[0] or None,
                offset=self._qint(q, "offset", 0, low=0),
                limit=self._qint(q, "limit", 200, low=0, clamp=1000))
            return self._json({
                "items": rows, "total": total,
                "kinds": idb.kinds(),
                "counts": idb.bucket_counts(),
                "buckets": buckets_view(),
            })
        if u.path == "/api/items":
            n = self._qint(q, "n", 40, low=0, clamp=200)
            return self._json({"items": db().search((q.get("q") or [""])[0], n)})
        raise NotFound(u.path)

    def _route_post(self):
        u = urllib.parse.urlparse(self.path)
        raw = self._read_body()          # consume it, then judge it: see above
        self._check_origin()
        deg = getattr(self.app, "degraded", None)
        if self._is_form():
            # Only the two routes the no-JavaScript startup pages post to. The
            # answer is a redirect to `/`, which is either the fixed
            # application or the same page with the reason it did not work.
            if u.path == "/api/settings":
                self.put_settings({"settings": _form_settings(raw)})
                return self._redirect("/")
            if u.path == "/api/config/reset":
                self.reset_config()
                return self._redirect("/")
            raise WrongType("this API takes JSON; send the request with "
                            "Content-Type: application/json")
        self._check_content_type()
        body = self._parse_body(raw)
        # Before the degraded check: a reduced-mode server is one of the things
        # most worth being able to stop from the page, and stopping needs
        # nothing that a startup failure could have broken.
        if u.path == "/api/quit":
            return self.quit_now(body)
        if deg is not None:
            if u.path == "/api/settings":
                return self._json(self.put_settings(body))
            if u.path == "/api/config/reset" and deg.allow_reset:
                return self._json(self.reset_config())
            raise Unavailable(pages.refusal(deg.problem))
        if u.path == "/api/settings":
            return self._json(self.put_settings(body))
        if u.path == "/api/config":
            return self._json(self.put_config(body))
        if u.path == "/api/validate":
            return self._json(self.validate_config(body))
        if u.path == "/api/config/reload":
            return self._json(self.reload_config())
        if u.path == "/api/config/reset":
            return self._json(self.reset_config(body))
        if u.path == "/api/config/use":
            return self._json(self.use_config(body))
        if u.path == "/api/select":
            s = self.app.save(body.get("file"), reload=True)
            return self._json(save_view_for(self.app, s))
        if u.path == "/api/verify":
            s = self.app.save(body.get("file"))
            rep = safety.Report()
            safety.verify_roundtrip(s.path, rep)
            return self._json({"steps": rep.steps})
        if u.path == "/api/plan":
            return self._json(self.make_plan(body))
        if u.path == "/api/apply":
            return self._json(self.do_apply(body))
        if u.path == "/api/restore":
            return self._json(self.do_restore(body))
        raise NotFound(u.path)

    # ------------------------------------------------------- degraded mode

    def _degraded_get(self, deg, path):
        """While degraded, exactly one page and the read-only routes that page
        needs. Everything else is 503 and says so in one sentence.

        `/api/log` is on this list deliberately: reduced mode is the state a
        bug report is most likely filed from, and the log is what the report
        needs. It reads one file and touches nothing the failure broke.
        """
        if path in ("/", "/index.html"):
            return self._html(deg.html)
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if path == "/api/health":
            return self._json(self.health())
        if path == "/api/log":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._json(self.log_tail(
                self._qint(q, "tail", LOG_TAIL_DEFAULT, low=0,
                           clamp=LOG_TAIL_MAX)))
        if path == "/api/settings":
            return self._json(self.get_settings())
        # The documents are files on disk and none of the three startup
        # failures touches them, so reduced mode is exactly when "what does
        # this mean?" needs answering.
        if path == "/docs" or path.startswith("/docs/"):
            return self.serve_doc(path)
        if path == "/api/detect-saves":
            return self._json(detect_saves())
        if path == "/api/version":
            return self._json(version_view())
        raise Unavailable(pages.refusal(deg.problem))

    def first_run_page(self, chosen=True):
        """P2-3. Rendered from `detect_saves()` server-side, so it works with
        scripting off; the page's own script only re-runs the probe.

        `chosen=False` is the "I want to choose another folder" arrival
        (`/choose-folder`): the folder already recorded is not repeated into
        the text field and no complaint is made about it, because the player
        came here to type a different one.

        W6: the complaint, when there is one, distinguishes "there is no folder
        at that path" from "that folder holds no `save*.hg` files". Both used
        to be the second sentence, which sent a player with a mistyped drive
        letter hunting for save files.
        """
        det = detect_saves()
        current = (self.app.settings.save_folder or "") if chosen else ""
        invalid = False
        if current:
            invalid = (pages.INVALID_NO_SAVES if os.path.isdir(current)
                       else pages.INVALID_NO_FOLDER)
        return pages.first_run(det["folders"], root=det["root"],
                               current=current, invalid=invalid)

    # ------------------------------------------------------------- handlers

    def get_settings(self):
        app = self.app
        return {
            "settings": app.settings.as_dict(),
            "fields": settingsmod.fields(),
            "path": app.settings_path,
            "first_run": app.first_run,
            # which fields a command-line flag is holding for this run: the tab
            # has to say so, because saving would otherwise appear to do
            # nothing to them (they are never written back, §3.6).
            "overridden": sorted(app.settings.overrides),
            # W15: the settings file could not be parsed, so every value above
            # is the shipped default for this run. It was reported in the
            # window the sorter started in, which `NMS-Sorter.exe` does not
            # have, so the page says it too -- and the page is also where
            # saving once rewrites the file and clears this.
            "load_error": getattr(app.settings, "load_error", None),
        }

    def put_settings(self, body):
        """`{settings: {...}}`, a partial object being the normal case: the
        first-run page sends one field. Validates, stores, and applies what can
        be applied without a restart."""
        app = self.app
        unknown = sorted(k for k in body if k != "settings")
        if unknown:
            raise Invalid("this endpoint takes a settings object and nothing "
                          "else; %s is not a field it knows" % unknown[0],
                          where=unknown[0])
        raw = body.get("settings")
        if not isinstance(raw, dict):
            raise Invalid("there was no settings object in the request",
                          where="settings")
        values, notes, extra = settingsmod.validate(raw)
        bad = [n for n in notes if n["level"] == "error"]
        if bad:
            raise Invalid(bad[0]["message"], where=bad[0]["where"])
        with app.lock:
            new = app.settings.copy()
            for key in settingsmod.KEYS:
                if key in raw:
                    setattr(new, key, values[key])
                    # typed on the page, so it stops being a flag override and
                    # starts being the stored value
                    new.overrides.discard(key)
            new.extra.update(extra)
            new.first_run = not new.folder_ok()
            restart = app.apply_settings(new)
            settingsmod.store(app.settings_path, new)
            # The file has just been rewritten from a complete object, so
            # whatever could not be parsed at startup is gone and the page must
            # stop saying so (W15).
            new.load_error = None
            if new.save_folder and not new.folder_ok():
                notes.append({"level": "warning", "where": "save_folder",
                              "message": "there are no save*.hg files in %s, "
                                         "so there is nothing to sort there "
                                         "yet." % new.save_folder})
            out = self.get_settings()
            out["restart_required"] = restart
            out["notes"] = notes
            return out

    def quit_now(self, body):
        """`POST /api/quit` -> 200 `{stopping, message}`, then stop the server.

        The windowed executable has no console, so before this route existed
        the only way a player could stop `NMS-Sorter.exe` was Task Manager.
        That is a bad answer for a tool whose whole promise is that it touches
        nothing it was not asked to: "I could not turn it off" is the kind of
        thing that gets a program deleted rather than reported.

        Two properties matter and both are visible here.

        It refuses while a write is running. `App.busy` is set for the whole of
        `do_apply` and `do_restore` -- from before the lock is taken, so a
        queued second write counts too -- and it is read here *without* taking
        `App.lock`: a quit that waited for the lock would be granted the
        instant the apply released it, which is the one moment the process must
        stay up. The save is already backed up and verified by then, so this is
        not about corruption; it is about a restore or a re-read that has not
        finished being reported to the page.

        And the answer goes out before anything is torn down. The JSON is
        written here, by hand rather than by returning a dict, so that
        `shutdown_later` is only started once the bytes are on the socket.
        """
        unknown = sorted(body)
        if unknown:
            raise Invalid("this endpoint takes an empty object; %s is not a "
                          "field it knows" % unknown[0], where=unknown[0])
        if getattr(self.app, "busy", False):
            raise safety.Refused(QUIT_BUSY)
        self._json({"stopping": True, "message": QUIT_STOPPING})
        try:
            self.wfile.flush()
        except Exception:
            # An unbuffered writer's `flush` is a no-op and a broken pipe here
            # means the client left; either way the server is still stopping.
            pass
        # One INFO line, because "why did the sorter stop?" is a question the
        # log has to be able to answer on its own.
        log.info("stopped from the page")
        shutdown_later(self.server)

    def health(self):
        """Everything a bug report should carry, in one object (P2-7)."""
        app = self.app
        try:
            saves = len(list_saves(app.save_folder))
        except Exception:
            saves = 0
        try:
            counts = itemdbmod.data_counts()
        except Exception:
            counts = None
        backups = app.list_backups()
        cfg = getattr(app, "config", None) or {}
        # The selected save's own platform word. Read through the loaded save
        # rather than by opening a file, and None whenever there is no save to
        # ask -- the health panel is the one route that has to answer while
        # every other part of the program is broken, so nothing here may raise.
        try:
            selected = save_platform(app.save())
        except Exception:
            selected = None
        return {
            "save_folder": app.save_folder,
            "saves": saves,
            "save_platform": selected,
            "game": safety.game_status(),
            "roundtrip": app.roundtrip(),
            "data_version": data_version(),
            "data_counts": counts,
            "config": {"path": app.config_path,
                       "version": cfg.get("config_version")},
            "backups": {"root": app.backup_root, "count": len(backups)},
            "log_path": app.log_path,
            "app_version": __version__,
            "python": _platform.python_version(),
            "platform": _platform.platform(),
            "settings_path": app.settings_path,
            "first_run": app.first_run,
            "degraded": (app.degraded.kind if app.degraded else None),
            "read_only": app.read_only,
            "strict_version_check": app.strict_version_check,
            # The live setting, not the number the banner printed at startup:
            # saving it on the Settings tab changes what the watchdog does on
            # its next tick, so a panel showing the startup value would be
            # stating something that stopped being true.
            "idle_exit_minutes": getattr(app.settings, "idle_exit_minutes", 0),
        }

    def serve_doc(self, path):
        """`GET /docs/`, `GET /docs/<name>.md` -> a rendered page, or 404 JSON.

        The gate warnings and the explainers link at the documents by name, and
        until this route existed those links went to `github.com`: a tool that
        binds to loopback and promises to touch nothing sent the operator to
        the internet to read its own safety copy, and those links 404 until the
        repository is public (P5-2 walkthrough, D4). So the documents are
        served from this machine, rendered by `pages.markdown_html`.

        A name that is not one of the shipped documents is a JSON 404 like
        every other unknown path -- not an HTML page, because a page saying
        "there is no such document" is hard to tell from a document about
        nothing. There is no path to sanitise: `read_doc` matches the name
        against the directory listing and joins nothing.
        """
        rel = path[len("/docs"):].lstrip("/")
        names = doc_names()
        if not rel:
            return self._html(pages.document_index(names))
        real, text = read_doc(rel)
        if real is None:
            raise NotFound(rel, sentence=(
                "there is no document called %s in this build; it ships %s"
                % (os.path.basename(rel.rstrip("/")) or rel,
                   ", ".join(names) or "none")))
        return self._html(pages.document(real, text, others=names))

    def reload_config(self):
        """Re-read `config.json` from disk. -> the `/api/config` shape.

        The file is read once at startup, so the workflow every document
        describes -- "copy this JSON into your config file" -- did nothing
        until the sorter was restarted, and pressing **save configuration**
        first overwrote the file that had just been pasted in (P5-2
        walkthrough, C2). This is the missing half: migrate and validate the
        file exactly as `load` does at startup, swap it in, and drop the minted
        plans, because a plan printed under the old rules must not stay
        approvable -- the same reason `/api/config` and `/api/config/reset`
        clear them.

        It writes nothing, so `saved` is always False; it is in the answer only
        so the page can render one shape for all three config routes.
        """
        app = self.app
        with app.lock:
            notes = []
            try:
                cfg, created = cfgmod.load(app.config_path, notes)
            except (OSError, ValueError) as exc:
                # `ConfigTooNew` is a `ValueError` and lands here too: a file
                # from a newer build is refused in one sentence rather than
                # taking a running server down into reduced mode.
                raise safety.Refused(
                    "%s could not be read (%s: %s), so the rules already in "
                    "memory are still the ones in use. Fix the file and "
                    "reload again."
                    % (app.config_path, type(exc).__name__, exc))
            app.config = cfg
            app.sync_taxonomy()
            app.clear_plans()
            log.info("config reloaded from %s (%d migration note(s))",
                     app.config_path, len(notes))
            return {"saved": False, "reloaded": True, "created": created,
                    "notes": notes, "issues": app.validate(),
                    "config": cfgmod.enrich(app.config)}

    def log_tail(self, n):
        """-> `{path, lines, total}`: the last `n` lines of the current log.

        The file the running process is writing, `app.log_path`, not a guess
        at where a log might be: `--log -` sends the log to stderr and there is
        then no file to read, which is a 404 and not an empty list -- "there is
        no log file" and "the log is empty" are different answers and a bug
        report needs to be able to tell them apart.

        Read whole and sliced. The log rotates at 1 MB, and seeking backwards
        through a UTF-8 file for a line count is a second parser to get wrong
        for no gain at that size.
        """
        path = getattr(self.app, "log_path", None)
        if not path:
            raise NotFound("the log", sentence=(
                "this sorter was started with --log - , so the log is going to "
                "stderr and there is no file to read"))
        if not os.path.isfile(path):
            raise NotFound(path, sentence=(
                "the log file %s has not been written yet" % path))
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError as exc:
            raise Invalid("the log file %s could not be read (%s)"
                          % (path, exc), where="tail")
        return {"path": path, "total": len(lines),
                "lines": lines[-n:] if n else []}

    def validate_config(self, body):
        """`{config}` -> `{issues}`. Validates and stores nothing.

        The Settings and Rules tabs need to show what is wrong with an edit
        before it is saved, and the only route that answered that question also
        wrote the file. Same validator, same container keys, no side effect:
        no store, no taxonomy sync, no plan cache to clear (lane D).
        """
        app = self.app
        unknown = sorted(k for k in body if k != "config")
        if unknown:
            raise Invalid("this endpoint takes a config and nothing else; %s "
                          "is not a field it knows" % unknown[0],
                          where=unknown[0])
        cfg = body.get("config")
        if not isinstance(cfg, dict):
            raise Invalid("there was no config object in the request",
                          where="config")
        with app.lock:
            try:
                ck, sk, dk = app.container_keys()
            except Exception:
                ck, sk, dk = None, None, None
            return {"issues": cfgmod.validate(cfg, ck, sk, dk)}

    def bootstrap(self):
        app = self.app
        saves = list_saves(app.save_folder)
        gone = missing_save_folder(app)
        out = {
            "save_folder": app.save_folder,
            # W5: a folder that is not there is named here as well, so a page
            # that is already open when the drive is unplugged has the same
            # sentence the page at `/` would have served. Still a 200: the
            # Settings tab has to render, and that is what it is for.
            "save_folder_missing": gone,
            "config_path": app.config_path,
            "config_created": app.created,
            # W14: what the shipped default had to drop to fit this save, if
            # anything, in the words the load notes used.
            "config_notes": list(getattr(app, "config_notes", []) or []),
            # W7: the stamp this page loaded, which it sends back as
            # `based_on` when it saves.
            "config_updated": (app.config or {}).get("updated"),
            "backup_root": app.backup_root,
            "read_only": app.read_only,
            "strict_version_check": app.strict_version_check,
            "saves": [save_row(s) for s in saves],
            "buckets": buckets_view(),
            "config": cfgmod.enrich(app.config),
            "game": safety.game_status(),
            "item_count": len(db().items),
            "version": version_view(),
        }
        try:
            s = app.save()
            out["save"] = save_view_for(app, s)
            # A refusing gate raises and lands in the `except` below; a warning
            # gate belongs beside the config's own issues, which is the list
            # the page already renders.
            out["issues"] = save_warnings(s) + app.validate()
        except Exception as exc:
            out["save"] = None
            out["issues"] = [{"level": "error", "where": "save",
                              "message": "%s" % exc}]
        return out

    def put_config(self, body):
        """One request shape and one only:
        {config: {...}, force: bool, based_on: str}.

        Three shapes used to be accepted here, which meant a typo in the client
        was indistinguishable from a deliberate partial update.

        `based_on` is the `updated` stamp the page loaded (W7). Two tabs of one
        sorter used to clobber each other in silence: tab B set OXYGEN's keep
        to 111 and saved, tab A then saved its stale copy, and on disk OXYGEN
        was back to 250 with tab A's own edit alongside it -- no warning in
        either tab, because there was no ETag, no If-Match and no mtime check
        on this write. The bounded damage (`config.json.bak.1`..`.bak.5`) was
        real and nothing pointed at it.

        A request with no `based_on` is unchecked, which is what a non-browser
        client and every build before this one send; `force: true` is the
        deliberate override, and the page only sends it for an edit it has
        just re-read from disk.
        """
        app = self.app
        unknown = sorted(k for k in body
                         if k not in ("config", "force", "based_on"))
        if unknown:
            raise Invalid("this endpoint takes a config and a force flag and "
                          "nothing else; %s is not a field it knows. The stamp "
                          "the page loaded is sent as based_on, which this "
                          "endpoint also reads." % unknown[0],
                          where=unknown[0])
        cfg = body.get("config")
        if not isinstance(cfg, dict):
            raise Invalid("there was no config object in the request",
                          where="config")
        if "force" in body and not isinstance(body["force"], bool):
            raise Invalid("force has to be true or false", where="force")
        based_on = body.get("based_on")
        if based_on is not None and not isinstance(based_on, str):
            raise Invalid("based_on has to be the updated stamp this page "
                          "loaded, as a string", where="based_on")
        with app.lock:
            if based_on and not body.get("force"):
                self._check_based_on(app, based_on)
            cfg["config_version"] = cfgmod.CONFIG_VERSION
            try:
                ck, sk, dk = app.container_keys()
            except Exception:
                ck, sk, dk = None, None, None
            issues = cfgmod.validate(cfg, ck, sk, dk)
            blocking = [i for i in issues if i["level"] == "error"]
            if blocking and not body.get("force"):
                return {"saved": False, "issues": issues,
                        "config": cfgmod.enrich(app.config)}
            app.config = cfgmod.store(app.config_path, cfg)
            app.sync_taxonomy()
            app.clear_plans()
            return {"saved": True, "issues": issues,
                    "config": cfgmod.enrich(app.config)}

    @staticmethod
    def _check_based_on(app, based_on):
        """Refuse a write based on a configuration that is no longer the
        stored one (W7). Raises `safety.Refused` -> 409, and stores nothing.

        The stamp is read off the *file*, not out of memory, so the other
        writer may be another tab of this sorter or another copy of it
        entirely: in-memory would catch only the first of those, and the
        second is the one that can be pointed at a different save folder.

        `where` is set on the exception so the page can tell this 409 from
        every other refusal without matching on the sentence.

        The stamp's resolution is one second (`config._now`), so two saves
        inside one second cannot be told apart by it. That direction is the
        safe one: a conflict can be *missed* in the same second, never
        invented, and the case this guards is a person in another tab. A finer
        stamp would be the only fix and it would change a field written into
        every configuration file, which is not worth a sub-second race.
        """
        current = stored_config_stamp(app.config_path,
                                     (app.config or {}).get("updated"))
        if not current or current == based_on:
            return
        exc = safety.Refused(
            "the configuration was saved by another tab (or another copy) at "
            "%s after this page loaded it; reload from disk, then redo your "
            "change" % readable_stamp(current))
        exc.where = "based_on"
        raise exc

    def _not_while_writing(self):
        """Refuse a configuration swap while an apply or a restore is running.

        `App.busy`, read the way `POST /api/quit` reads it: without taking
        `App.lock`, because a swap that queued on the lock would be granted
        the instant the apply released it, and the report the page is about to
        render describes a run made under the configuration this would have
        replaced. Counted from before the write takes the lock, so a second
        write still waiting for it counts too.
        """
        if getattr(self.app, "busy", False):
            raise safety.Refused(CONFIG_BUSY)

    def _swap_shape(self, body, takes, fields=()):
        """The request shape both start-over routes share. -> (based_on, force).

        Shape first, before anything is read off the disk: a body with a typo
        in it is answered by naming the field, not by a refusal about a file
        that was never the problem. `based_on` and `force` mean exactly what
        they mean on `/api/config` -- the stamp the page loaded, and the
        deliberate override for an edit it has just re-read -- and the two-tab
        check itself runs inside the lock, in the caller.
        """
        unknown = sorted(k for k in body
                         if k not in fields + ("force", "based_on"))
        if unknown:
            raise Invalid("this endpoint takes %s; %s is not a field it knows. "
                          "The stamp the page loaded is sent as based_on, "
                          "which this endpoint also reads."
                          % (takes, unknown[0]), where=unknown[0])
        if "force" in body and not isinstance(body["force"], bool):
            raise Invalid("force has to be true or false", where="force")
        based_on = body.get("based_on")
        if based_on is not None and not isinstance(based_on, str):
            raise Invalid("based_on has to be the updated stamp this page "
                          "loaded, as a string", where="based_on")
        return based_on, bool(body.get("force"))

    def reset_config(self, body=None):
        """Back to the shipped default. Clears the minted plans for the same
        reason /api/config does: a plan printed under the old config must not
        stay approvable (GOAL.md D12). It used to be caught two layers down by
        the re-plan, by accident.

        The configuration it replaces is kept first, as
        `config.before-reset-<stamp>.json` beside `config.json`, and the
        basename is in the answer so the page can name the file rather than
        say "a copy was kept somewhere". That is the whole of "don't lose my
        existing config": the `.bak` rotation could not carry it, because five
        saves after a reset the copy would have fallen off the end.

        While degraded because the config file could not be read, this is also
        the offered repair, and that path is unchanged: the unreadable file is
        renamed (never deleted -- somebody typed it and this build could not
        read it, which is not the same as it being worthless), the default is
        written, and the server is promoted to a live application in place, so
        the operator lands on the working page rather than on "now restart
        me". It keeps its `.bad-<stamp>` name rather than a kept one, because
        a file this build cannot parse is not a configuration anybody can be
        offered back.
        """
        app = self.app
        deg = getattr(app, "degraded", None)
        if deg is not None:
            if not deg.allow_reset:
                raise Unavailable(pages.refusal(deg.problem))
            moved = move_aside(deg.config_path)
            live = deg.recover()
            Handler.app = live
            log.warning("config %s moved to %s; started fresh from the default",
                        deg.config_path, moved)
            return {"recovered": True, "moved_to": moved,
                    "config_path": live.config_path,
                    "config": cfgmod.enrich(live.config),
                    "issues": live.validate()}
        based_on, force = self._swap_shape(body or {}, "a force flag")
        self._not_while_writing()
        with app.lock:
            if based_on and not force:
                self._check_based_on(app, based_on)
            kept = cfgmod.keep_aside(app.config_path)
            app.config = cfgmod.store(app.config_path, cfgmod.default_config())
            app.sync_taxonomy()
            app.clear_plans()
            log.warning("config %s reset to the shipped default; the previous "
                        "one is kept as %s", app.config_path, kept)
            return {"kept": kept, "config": cfgmod.enrich(app.config),
                    "issues": app.validate()}

    def kept_configs(self):
        """`GET /api/config/kept` -> `{dir, config_path, kept: [...]}`.

        Read-only, and the folder is in the answer because the page states
        where the files are exactly once, beside the list rather than in every
        row.
        """
        app = self.app
        return {"dir": cfgmod.kept_dir(app.config_path),
                "config_path": app.config_path,
                "kept": cfgmod.list_kept(app.config_path)}

    #: `file` named something this server did not keep. A 400 naming the
    #: folder, for the reason `RESTORE_NOT_A_BACKUP` is one: a route that
    #: copies *from* a client-named path only ever accepts a name it wrote.
    USE_NOT_KEPT = ("%s is not one of the kept configurations in %s. Use only "
                    "puts back a file this server kept.")

    def use_config(self, body):
        """`POST /api/config/use {file}` -> the `/api/config` shape plus `kept`.

        The other half of starting over: the configuration on disk now is kept
        under its own name, and the file named by `file` becomes
        `config.json`.

        `file` is a basename matched against `config.kept_re`, which is
        anchored and built out of the configuration's own name. A basename
        that matches cannot hold a separator, so the path is inside the
        configuration folder by construction and there is no traversal to
        check for; anything else is a 400 that names the folder.

        The file is read with `load` *before* anything moves, so a kept file
        this build cannot read is refused with the configuration in use
        untouched -- rather than swapped in and then discovered.
        """
        app = self.app
        based_on, force = self._swap_shape(
            body, "a kept configuration's file name and a force flag",
            ("file",))
        raw = body.get("file")
        if not isinstance(raw, str) or not raw.strip():
            raise Invalid("name the kept configuration to put back, as `file`",
                          where="file")
        name = raw.strip()
        folder = cfgmod.kept_dir(app.config_path)
        if not cfgmod.kept_re(app.config_path).match(name):
            raise Invalid(self.USE_NOT_KEPT % (name[:120], folder),
                          where="file")
        src = os.path.join(folder, name)
        if not os.path.isfile(src):
            raise Invalid(self.USE_NOT_KEPT % (name, folder), where="file")
        try:
            cfgmod.load(src)
        except (OSError, ValueError) as exc:
            # `ConfigTooNew` is a `ValueError` and lands here too, which is
            # the case worth naming: a configuration kept by a later build is
            # not a file this one may put back.
            raise safety.Refused(
                "%s could not be read (%s: %s), so it was not put back and "
                "the configuration in use is untouched."
                % (name, type(exc).__name__, exc))
        self._not_while_writing()
        with app.lock:
            if based_on and not force:
                self._check_based_on(app, based_on)
            kept = cfgmod.keep_aside(app.config_path)
            cfgmod.install(app.config_path, src)
            notes = []
            app.config, _created = cfgmod.load(app.config_path, notes)
            app.sync_taxonomy()
            app.clear_plans()
            log.warning("config %s put back from %s; the previous one is kept "
                        "as %s", app.config_path, name, kept)
            return {"used": name, "kept": kept, "notes": notes,
                    "config": cfgmod.enrich(app.config),
                    "issues": app.validate()}

    def make_plan(self, body):
        app = self.app
        with app.lock:
            s = app.save(body.get("file"), reload=True)
            plan, _commit = planner.build_plan(s, app.config)
            d = plan.as_dict()
            d["signature"] = s.signature()
            d["file"] = os.path.basename(s.path)
            d["game"] = safety.game_status()
            d["issues"] = app.validate()
            # Both forms of the fingerprint, keyed to the same signature: the
            # short one is what a person reads off the screen and what older
            # clients send back, the full digest is what the write path
            # compares (GOAL.md §3.9). `fingerprint_full` appears the moment
            # the planner mints one; until then the short form is all there is.
            full = getattr(plan, "fingerprint_full", None)
            # Both tokens are minted *with* the full digest, so whichever one
            # the client echoes, `do_apply` can hand the write path the 64
            # characters rather than the 8 it was given (R10).
            app.mint_plan(plan.fingerprint, s.signature(), full=full)
            if full:
                d["fingerprint_full"] = full
                if full != plan.fingerprint:
                    app.mint_plan(full, s.signature(), full=full)
            return d

    #: the field the page sends when the game is up and the player has said
    #: where they are. One apply, one answer: it is not a setting, because a
    #: stored "I am at the main menu" would still be stored next week.
    MAIN_MENU_FIELD = "at_main_menu"

    def _at_main_menu(self, body):
        """`at_main_menu` out of a request body, as a strict boolean.

        Absent is false, which is the right default: the confirmation has to
        be made, and a request that does not carry it while the game is
        running is refused by `safety`'s step 1 in the sentence that names the
        tick. Anything that is not a boolean is bad input rather than a
        refusal, like every other typed field on these routes.
        """
        v = body.get(self.MAIN_MENU_FIELD)
        if v is None:
            return False
        if not isinstance(v, bool):
            raise Invalid("%s has to be true or false" % self.MAIN_MENU_FIELD,
                          where=self.MAIN_MENU_FIELD)
        return v

    def do_apply(self, body):
        app = self.app
        fp = self._token(body, "fingerprint")
        at_main_menu = self._at_main_menu(body)
        # Marked before the lock is taken, not inside it: a request queued on
        # the lock is still "an apply is running" as far as `POST /api/quit` is
        # concerned, and the window between arriving and acquiring is exactly
        # when a stop would look safe and not be.
        app.begin_write()
        try:
            return self._apply(app, body, fp, at_main_menu)
        finally:
            app.end_write()

    def _apply(self, app, body, fp, at_main_menu=False):
        with app.lock:
            if app.read_only:
                raise safety.Refused(
                    "this server was started with --read-only, so it will not write to a "
                    "save. Restart without that flag if you meant to apply.")
            if not fp:
                raise safety.Refused("no plan fingerprint; run a dry run first")
            if app.plan_signature(fp) is None:
                raise safety.Refused(
                    "this server has not printed a plan with fingerprint %s. Apply only "
                    "runs a plan you have seen." % fp)
            s = app.save(body.get("file"))
            if app.plan_signature(fp) != s.signature():
                raise safety.Refused(
                    "the save file has changed on disk since that plan was printed. "
                    "Re-run the dry run.")
            # R10: the gate is the full digest. The server minted it, so the
            # short token the operator read off the screen is looked up rather
            # than forwarded -- passing the prefix on would have narrowed the
            # comparison `safety` performs to 32 bits. `apply_plan` still
            # accepts a prefix (`tests/test_safety.py` asserts that on
            # purpose); what changes is that this route never sends one. The
            # fallback exists for a planner build with no `fingerprint_full`
            # at all, where there is no wider digest to compare.
            expected = app.plan_full(fp) or fp
            if len(expected) == 64:
                # Logged at INFO because this is the line that proves the gate
                # was the full digest and not the token the client sent: the
                # two are visibly different lengths in the log.
                log.info("apply %s: comparing the full digest %s (the client "
                         "sent %d characters)", os.path.basename(s.path),
                         expected, len(fp))
            else:
                log.warning("applying against the short fingerprint %s: this "
                            "plan carries no full digest", expected)
            rep, res = safety.apply_plan(s.path, app.config, expected,
                                         planner.build_plan,
                                         app.backup_root,
                                         at_main_menu=at_main_menu,
                                         strict_version_check=app.strict_version_check)
            # R8: retention has a reader now, and it runs after the write has
            # been proved rather than before it -- a pruned backup must never
            # be the price of a write that then failed.
            if isinstance(res, dict):
                res["pruned"] = app.prune_backups()
            app.clear_plans()
            app.save(s.path, reload=True)
            return {"steps": rep.steps, "result": res,
                    "save": save_view_for(app, app.save())}

    # ------------------------------------------------------------- restore

    #: the sentence `/api/restore` refuses with before anything is read.
    #: docs/TROUBLESHOOTING.md quotes it verbatim.
    RESTORE_NOT_A_BACKUP = ("%s is not one of the backup folders in %s. "
                            "Restore only puts back a folder this server "
                            "listed.")

    def _backup_folder(self, raw):
        """A client's `folder` -> the absolute path of a direct child of the
        backup root, or `Invalid`.

        Restore is the one route that takes a path from the client and copies
        *from* it, so the path is checked the way the save folder is: it has to
        be a direct child of the backup root by name -- not merely "under" it,
        which `..` and a symlink both satisfy -- and it has to exist. Anything
        else is a 400 naming the root, because a restore that reaches outside
        the folder this server created is not a feature with a use.
        """
        app = self.app
        root = app.backup_root
        if not isinstance(raw, str) or not raw.strip():
            raise Invalid("name the backup folder to restore, as `folder`",
                          where="folder")
        raw = raw.strip()
        path = raw if os.path.dirname(raw) else os.path.join(root or "", raw)
        path = os.path.normpath(os.path.abspath(path))
        parent = os.path.normcase(os.path.dirname(path))
        if not root or parent != os.path.normcase(
                os.path.normpath(os.path.abspath(root))):
            raise Invalid(self.RESTORE_NOT_A_BACKUP
                          % (os.path.basename(path) or raw, root or "(none)"),
                          where="folder")
        if not os.path.isdir(path):
            raise Invalid(self.RESTORE_NOT_A_BACKUP
                          % (os.path.basename(path), root), where="folder")
        return path

    def do_restore(self, body):
        """`{folder}` -> `{steps, result, save}`, the same shape as apply.

        Put back the two files a backup folder holds, verified against its
        manifest, under the same gates as the write that made it: `--read-only`
        refuses in the sentence apply uses, `safety.restore` enforces the
        game step and is passed the player's main-menu confirmation, and the
        whole thing runs under `App.lock` and `safety`'s own lock file so a restore
        cannot race an apply. On success the plan cache is dropped and the save
        is re-read, because every fingerprint that was approvable described the
        bytes this just replaced.
        """
        app = self.app
        unknown = sorted(k for k in body
                         if k not in ("folder", self.MAIN_MENU_FIELD))
        if unknown:
            raise Invalid("this endpoint takes a backup folder and nothing "
                          "else; %s is not a field it knows" % unknown[0],
                          where=unknown[0])
        at_main_menu = self._at_main_menu(body)
        # As in `do_apply`: a restore is a write, and `POST /api/quit` has to
        # see it from the moment it arrives rather than from the moment it gets
        # the lock.
        app.begin_write()
        try:
            return self._restore(app, body, at_main_menu)
        finally:
            app.end_write()

    def _restore(self, app, body, at_main_menu=False):
        with app.lock:
            if app.read_only:
                # The same sentence apply refuses with: one wording for "this
                # server does not write", whichever route asked.
                raise safety.Refused(
                    "this server was started with --read-only, so it will not write to a "
                    "save. Restart without that flag if you meant to apply.")
            folder = self._backup_folder(body.get("folder"))
            rep = safety.Report()
            _rep, res = safety.restore(folder, app.save_folder, report=rep,
                                       at_main_menu=at_main_menu)
            app.clear_plans()
            out = {"steps": rep.steps, "result": res}
            try:
                name = restored_save_name(res)
                s = app.save(name, reload=True) if name \
                    else app.save(reload=True)
                out["save"] = save_view_for(app, s)
            except Exception as exc:
                # The files are back; failing to re-render them is not a
                # failed restore, and saying so is better than a 500 that
                # implies the restore itself did not happen.
                log.warning("restored %s but could not re-read the save: %s",
                            folder, exc)
                out["save"] = None
                out["warning"] = ("the backup was put back, but the save could "
                                  "not be re-read afterwards (%s)" % exc)
            return out


#: the server class every caller gets. Built once, at import, because it is a
#: subclass of `ThreadingHTTPServer` and there is no reason to mint a new type
#: per `serve()` call.
#:
#: Exclusive, and not by a seam a caller can leave out: `socketserver` sets
#: `allow_reuse_address = 1`, and on Windows that means "bind even if somebody
#: is already listening here". Three double-clicks therefore bound three
#: servers to one port, the OS handed each connection to whichever accepted
#: first, and the page a player was looking at belonged to a different process
#: from one request to the next -- three servers writing one save folder
#: (player-day review, W3). `platform.exclusive_server_class` is the fix:
#: `SO_EXCLUSIVEADDRUSE` on Windows, `allow_reuse_address = False` everywhere,
#: so the second bind raises `OSError` and the caller moves to the next port.
ExclusiveHTTPServer = platformmod.exclusive_server_class(ThreadingHTTPServer)
ExclusiveHTTPServer.daemon_threads = True


def serve(app, host="127.0.0.1", port=8765):
    """Bind and return the server. It does not serve until somebody calls
    `serve_forever`.

    The bind is exclusive, so a second `serve()` on a port this process (or any
    other) is already listening on raises `OSError` rather than quietly
    succeeding. `daemon_threads` is set on the subclass rather than on
    `ThreadingHTTPServer` itself: the base is a standard library class that
    anything else in this interpreter may also be using.
    """
    Handler.app = app
    httpd = ExclusiveHTTPServer((host, port), Handler)
    # The idle clock starts when the server does, not when this module was
    # imported: a build that spends a second loading the item table must not
    # start life a second closer to its idle exit.
    touch()
    return httpd
