"""`settings.json`: the flags that used to exist only on the command line.

GOAL.md §3.6. One file, eight fields, a version, and a one-sentence description
per field so the Settings tab and `GET /api/settings` say the same thing. The
file lives in `%LOCALAPPDATA%\\NMS-Sorter\\settings.json` (§3.2) because an
executable may be unpacked somewhere read-only and the state has to survive a
reinstall.

Three rules this module exists to keep:

* **Never crash on a bad file.** A settings file that cannot be read means
  defaults plus a note, because the page that would let the operator fix it is
  served by the process that just failed to read it. The same reasoning as
  `GET /api/bootstrap` returning 200 with `issues`.
* **A command-line flag overrides for one run and is never written back.**
  `--port 9999` for one debugging session must not become the stored port when
  the operator later presses Save on the Settings tab. So a `Settings` carries
  both the values in force (`save_folder`, `port`, ...) and the values that came
  off disk (`file_values`); `store` writes the second for any field the CLI
  overrode.
* **Unknown keys are kept.** A file written by a newer build is round-tripped,
  not truncated, with a warning naming each key. Deleting a field somebody typed
  is not this program's business.

The log path is deliberately *not* a setting: a broken settings file must still
be loggable, so `--log` and `%LOCALAPPDATA%\\NMS-Sorter\\logs\\` own it
(DECISIONS.md, 2026-09-14, resolution 3).
"""
import copy
import itertools
import json
import os

from . import platform as platformmod

#: bumped when a field changes meaning; a higher number in a file is a warning,
#: never a crash, because the fields are independent of each other.
SETTINGS_VERSION = 1

#: changing one of these does nothing until the process restarts, and the API
#: says so rather than letting the operator wonder (GOAL.md P2-4).
RESTART_REQUIRED = ("port", "open_browser")

PORT_MIN, PORT_MAX = 1024, 65535
KEEP_MIN, KEEP_MAX = 1, 500
#: `idle_exit_minutes`. Zero is "never", which is why the floor is 0 and not 1 --
#: the field is the only one whose off switch is a number. The ceiling is a day:
#: a watchdog set to a fortnight is indistinguishable from one that is off, and
#: a number nobody can reach is a number nobody can proof-read.
IDLE_MIN, IDLE_MAX = 0, 1440

#: key, type, and the one sentence the Settings tab shows under the control.
#: `strict_version_check` is the one opt-in of the risky shape, and it is off
#: because the check it turns on is *over*-cautious rather than dangerous: see
#: DECISIONS.md, "A game update must not turn the tool off".
#:
#: `allow_game_running` used to be here, carrying the risk sentence. It is
#: gone: applying from the main menu is the documented flow rather than an
#: opt-in, and the confirmation belongs to one apply -- a stored setting that
#: says "I am at the main menu" would still say it a week later. A stale key in
#: somebody's file is kept verbatim like any other unknown key.
FIELDS = [
    {"key": "save_folder", "type": "path",
     "description": "The folder holding save.hg, save2.hg and their mf_ "
                    "partners; this is the only game folder the sorter reads."},
    {"key": "backup_folder", "type": "path",
     "description": "Where a copy of the save and its mf_ is written before "
                    "any edit, one timestamped folder per apply."},
    {"key": "backup_keep", "type": "int",
     "description": "How many backup folders to keep; older ones are pruned, "
                    "the most recent is never pruned."},
    {"key": "port", "type": "int",
     "description": "The loopback port this page is served on; a change takes "
                    "effect the next time the sorter starts."},
    {"key": "read_only", "type": "bool",
     "description": "Refuse the apply endpoint outright, so this copy can plan "
                    "and browse but can never write to a save."},
    {"key": "strict_version_check", "type": "bool",
     "description": "Refuse to apply to a save whose version is outside the "
                    "range this build was verified on. Off by default: a game "
                    "update must not turn the tool off, and the write "
                    "sequence's own checks catch a layout change on the file "
                    "itself."},
    {"key": "open_browser", "type": "bool",
     "description": "Open a browser window when the sorter starts; a change "
                    "takes effect the next time the sorter starts."},
    {"key": "idle_exit_minutes", "type": "int",
     "description": "Stop the sorter by itself after this many minutes with no "
                    "request from the page, so a window that was closed does "
                    "not leave the server running. 0 keeps it running until "
                    "you stop it."},
]

KEYS = tuple(f["key"] for f in FIELDS)
_TYPE = dict((f["key"], f["type"]) for f in FIELDS)

#: the range each `int` field accepts. A table rather than a branch, because
#: there are three of them now and the branch it replaces read "port, else the
#: backup range" -- which silently gave a new int field the retention bounds.
INT_BOUNDS = {"port": (PORT_MIN, PORT_MAX),
              "backup_keep": (KEEP_MIN, KEEP_MAX),
              "idle_exit_minutes": (IDLE_MIN, IDLE_MAX)}


# ---------------------------------------------------------------------------
# where things live
# ---------------------------------------------------------------------------

#: `%LOCALAPPDATA%\NMS-Sorter`, re-exported so that
#: `settings.state_dir` keeps working for every caller that already imports it
#: from here. The implementation moved to `platform.py`, which is the only
#: module allowed to branch on the operating system (GOAL.md P4-1); this name
#: is the surface, that one is the answer.
state_dir = platformmod.state_dir


def default_path():
    return os.path.join(state_dir(), "settings.json")


def default_backup_folder():
    return os.path.join(state_dir(), "backups")


def defaults():
    """The shipped values, resolved against this machine's `%LOCALAPPDATA%`.

    Resolved on every call rather than at import, so a test that points
    `LOCALAPPDATA` at a temp tree gets a temp backup folder.
    """
    return {
        "settings_version": SETTINGS_VERSION,
        "save_folder": "",
        "backup_folder": default_backup_folder(),
        "backup_keep": 20,
        "port": 8765,
        "read_only": False,
        "strict_version_check": False,
        "open_browser": True,
        "idle_exit_minutes": 30,
    }


def fields():
    """`FIELDS` with this machine's default filled in, for the API."""
    d = defaults()
    return [{"key": f["key"], "type": f["type"],
             "description": f["description"], "default": d[f["key"]]}
            for f in FIELDS]


# ---------------------------------------------------------------------------
# the object
# ---------------------------------------------------------------------------

class Settings(object):
    """The values in force for this run.

    `file_values` is what the file said, `overrides` is the set of fields a
    command-line flag replaced for this run. `store` writes `file_values` for
    anything in `overrides`, which is how a flag stays a flag.
    """

    def __init__(self, **kw):
        d = defaults()
        unknown = sorted(k for k in kw if k not in d)
        if unknown:
            raise TypeError("Settings got an unknown field %r" % unknown[0])
        d.update(kw)
        for k, v in d.items():
            setattr(self, k, v)
        self.extra = {}            # keys a newer build wrote; kept verbatim
        self.file_values = {}      # what was on disk, before any override
        self.overrides = set()     # fields a CLI flag replaced for this run
        self.first_run = False     # no settings file, or no usable save folder
        self.path = None
        #: W15: why every value here is a shipped default -- the file exists
        #: and could not be parsed. It was only ever reported in the window the
        #: sorter started in, which `NMS-Sorter.exe` does not have, so it is
        #: carried on the object and `GET /api/settings` hands it to the page.
        #: None is the ordinary case, a file that is simply not there included:
        #: that is the first run and nothing is wrong.
        self.load_error = None

    # ------------------------------------------------------------- shaping

    def as_dict(self):
        """The values in force, plus any keys a newer build wrote."""
        out = dict(self.extra)
        out["settings_version"] = SETTINGS_VERSION
        for k in KEYS:
            out[k] = getattr(self, k)
        return out

    def stored_dict(self):
        """What `store` would write: the values in force, except that a field a
        CLI flag overrode keeps whatever the file said (or the default)."""
        out = self.as_dict()
        d = defaults()
        for k in sorted(self.overrides):
            out[k] = self.file_values.get(k, d[k])
        return out

    def copy(self):
        other = Settings()
        for k in KEYS:
            setattr(other, k, getattr(self, k))
        other.extra = copy.deepcopy(self.extra)
        other.file_values = dict(self.file_values)
        other.overrides = set(self.overrides)
        other.first_run = self.first_run
        other.path = self.path
        other.load_error = self.load_error
        return other

    def override(self, **kw):
        """Apply command-line flags. Only a value that is not None counts, so
        `override(port=None)` is "the flag was not passed"."""
        for k, v in kw.items():
            if k not in KEYS:
                raise TypeError("no such setting: %r" % k)
            if v is None:
                continue
            setattr(self, k, v)
            self.overrides.add(k)
        return self

    # -------------------------------------------------------------- derived

    def resolved_backup_folder(self):
        return self.backup_folder or default_backup_folder()

    def folder_ok(self):
        """True when `save_folder` names a directory holding at least one
        `save*.hg`. The first-run page is served whenever this is false, so it
        is the one predicate the whole of P2-3 hangs off."""
        return bool(save_count(self.save_folder))

    def __repr__(self):
        return "<Settings port=%d save_folder=%r first_run=%s>" % (
            self.port, self.save_folder, self.first_run)


def save_count(folder):
    """How many `save*.hg` files a folder holds; 0 for a folder that is not
    there. Deliberately not `savemodel.list_saves`: this runs before the item
    table and the codec are known to be loadable, and it must not decode
    anything."""
    if not folder or not isinstance(folder, str) or not os.path.isdir(folder):
        return 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    return sum(1 for n in names
               if n.lower().startswith("save") and n.lower().endswith(".hg"))


# ---------------------------------------------------------------------------
# load and validate
# ---------------------------------------------------------------------------

def _err(notes, where, message):
    notes.append({"level": "error", "where": where, "message": message})


def _warn(notes, where, message):
    notes.append({"level": "warning", "where": where, "message": message})


def validate(raw, notes=None):
    """`raw` (a dict off disk or off the wire) -> (values, notes, extra keys).

    Never raises. A field that fails its check is replaced by the default and
    named in a note, because a settings file is not a plan: the operator gets
    the page and fixes it there.
    """
    notes = notes if notes is not None else []
    values = defaults()
    extra = {}
    if not isinstance(raw, dict):
        _err(notes, None, "the settings have to be a JSON object with one key "
                          "per setting.")
        return values, notes, extra

    ver = raw.get("settings_version", SETTINGS_VERSION)
    if not isinstance(ver, int) or isinstance(ver, bool):
        _warn(notes, "settings_version",
              "settings_version is %r, which is not a whole number; reading "
              "the file as version %d." % (ver, SETTINGS_VERSION))
    elif ver > SETTINGS_VERSION:
        _warn(notes, "settings_version",
              "this settings file is version %d and this build writes version "
              "%d; fields it does not recognise are kept as they are."
              % (ver, SETTINGS_VERSION))

    for key in raw:
        if key == "settings_version" or key in _TYPE:
            continue
        extra[key] = raw[key]
        _warn(notes, key, "%r is not a setting this build knows; it is kept in "
                          "the file untouched." % key)

    for key in KEYS:
        if key not in raw:
            continue
        v = raw[key]
        kind = _TYPE[key]
        if kind == "path":
            if not isinstance(v, str):
                _err(notes, key, "%s has to be a folder path written as text; "
                                 "%r is not. Using the default." % (key, v))
                continue
            values[key] = v.strip()
        elif kind == "bool":
            if not isinstance(v, bool):
                _err(notes, key, "%s has to be true or false, not %r. Using "
                                 "%r." % (key, v, values[key]))
                continue
            values[key] = v
        elif kind == "int":
            if isinstance(v, bool) or not isinstance(v, int):
                _err(notes, key, "%s has to be a whole number, not %r. Using "
                                 "%d." % (key, v, values[key]))
                continue
            low, high = INT_BOUNDS[key]
            if v < low or v > high:
                _err(notes, key, "%s has to be between %d and %d; %d is "
                                 "outside that. Using %d."
                     % (key, low, high, v, values[key]))
                continue
            values[key] = v
    return values, notes, extra


def load(path=None):
    """-> (Settings, notes). Never raises for a file this program can reach.

    A missing file is the first run: defaults, `first_run=True`, and no note,
    because nothing is wrong. An unreadable file is a note and the defaults, so
    the server still starts and the Settings tab can rewrite it.
    """
    path = path or default_path()
    notes = []
    raw = None
    if not os.path.exists(path):
        s = Settings()
        s.path = path
        s.first_run = True
        return s, notes
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.loads(fh.read())
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        message = ("the settings file %s could not be read (%s: %s), so this "
                   "run uses the defaults. Saving from the Settings section "
                   "rewrites it." % (path, type(exc).__name__, exc))
        _err(notes, None, message)
        s = Settings()
        s.path = path
        s.first_run = True
        # W15: the same sentence, on the object, so that the page can say it
        # and the caller choosing a port can tell that *this* run's port is a
        # default rather than a value anybody chose. A default port that
        # happens to be busy must not be read as "my own sorter is already
        # running there": that other process may be pointed at another save
        # folder entirely.
        s.load_error = message
        return s, notes

    values, notes, extra = validate(raw, notes)
    s = Settings(**dict((k, values[k]) for k in KEYS))
    s.extra = extra
    if not isinstance(raw, dict):
        # Valid JSON of the wrong shape: every value above is a default, so
        # the same rule as an unparseable file applies (W15): say so on the
        # page, and never adopt another instance on a port nobody chose.
        s.load_error = ("the settings file %s is not a JSON object with one "
                        "key per setting, so this run uses the shipped "
                        "defaults" % path)
    s.file_values = dict((k, values[k]) for k in KEYS)
    s.path = path
    s.first_run = not s.folder_ok()
    return s, notes


#: bumped per `store` call, so two writes in one process cannot collide either
_tmp_serial = itertools.count(1)


def temp_name(path):
    """The temp file `store` writes through: `<path>.tmp-<pid>-<n>`.

    R14: the name used to be `<path>.tmp` flat, and two sorters against one
    state directory is the ordinary case rather than the odd one -- they would
    have shared one temp file and the loser's half-written JSON could have been
    renamed over the settings. `safety.temp_path` puts the pid in the name for
    exactly this reason; this is the same rule for the two files in
    `%LOCALAPPDATA%`.
    """
    return "%s.tmp-%d-%d" % (path, os.getpid(), next(_tmp_serial))


def store(path, settings):
    """Write the settings atomically: a temp file beside the real one, then
    `os.replace`.

    A half-written settings file is the one failure this module could cause
    that the operator cannot fix from the page, so the write is the same shape
    as `config.store`: full write, `fsync`, rename. The `finally` removes the
    temp file when the write raised between the two, so a full disk does not
    leave `settings.json.tmp-4812-1` in the state directory for ever.
    """
    path = path or settings.path or default_path()
    out = settings.stored_dict()
    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = temp_name(path)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, ensure_ascii=False, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    settings.file_values = dict((k, out[k]) for k in KEYS)
    settings.path = path
    return out
