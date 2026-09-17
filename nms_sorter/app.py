"""`App`: the process-wide state the HTTP layer serves.

Holds the paths (save folder, config file, backup root), the write-gate flags,
the loaded configuration, the cached `SaveFile` and the minted plans. The
server module owns routes; this module owns state.

Everything mutable here is guarded by one re-entrant lock (`App.lock`), because
`ThreadingHTTPServer` runs every request in its own thread and three pieces of
state are shared: the config object, the cached save, and the *process-global*
item database that `sync_taxonomy` mutates (GOAL.md D13). The lock is
re-entrant so a locked route may call `container_keys` -> `save` freely.
"""
import collections
import logging
import os
import threading

from . import config as cfgmod
from . import safety
from . import settings as settingsmod
from .itemdb import db
from .savemodel import (SaveFile, folded as _folded, is_dead_container,
                        list_saves, match_name)

#: the most fingerprints kept in memory; oldest is evicted (GOAL.md §3.9)
MAX_PLANS = 16

log = logging.getLogger("nms_sorter.app")


def normal_folder(path):
    """One spelling for every folder path this API emits.

    `--folder C:/Users/x/saves` and the same folder typed with backslashes and
    a trailing separator are the same directory, and the page compared the two
    strings: `bootstrap.saves[].path` was built from the folder as typed while
    `save.path` came back from `os.path.abspath`, so a row the operator clicked
    did not match the save that was loaded (lane D). Normalised once, here,
    rather than at each of the four readers.

    An empty folder is left empty: `os.path.abspath("")` is the process working
    directory, which is how "no save folder chosen yet" would silently become
    "sort whatever is in the directory the sorter was started from".
    """
    if not path:
        return path
    return os.path.normpath(os.path.abspath(path))


def disk_signature(path):
    """The identity of the bytes at `path`, without decoding the save.

    R15: the cache used to turn on `st_mtime` alone, so a same-length edit
    written back with the original timestamp was invisible -- the page and the
    plan then described bytes that were no longer on disk. Name, size and
    mtime are all things a tool can preserve while changing the contents; a
    content hash is not.

    Deliberately the same string `SaveFile.signature()` builds -- name, size,
    whole-second mtime, SHA-256 of the file -- so that the comparison in
    `save()` below is "the signature of the bytes on disk against the signature
    the cached save answers with", and not two nearly-identical notions of
    identity that could drift apart. `tests/test_server.py` asserts the two
    agree on a file neither has touched; if `savemodel` ever grows a
    path-taking version of this, delete the body here and call it.

    Cost, measured on the real `save10.hg` (468,870 bytes) on this machine:
    **0.71 ms** per call, against 206 ms for the decode it stands in front of
    and 0.04 ms for the cached `signature()` it is compared with. Well under
    the 50 ms that would have justified caching by (size, mtime) and only
    rehashing when those moved, so it hashes on every request.
    """
    st = os.stat(path)
    return "%s|%d|%d|%s" % (os.path.basename(path), st.st_size,
                            int(st.st_mtime), safety.sha256(path))


class Invalid(ValueError):
    """The client sent something this server cannot act on -> HTTP 400.

    Distinct from `safety.Refused` (409: the request was understood and the
    write path said no) and from an unexpected exception (500). Carries an
    optional `where`, the field path the operator should look at.
    """

    def __init__(self, message, where=None):
        ValueError.__init__(self, message)
        self.where = where


class App(object):
    """The live application. `degraded` is always None here; see `Degraded`.

    The four values a `Settings` owns -- the save folder, the backup folder,
    `read_only` and `strict_version_check` -- are read through properties rather
    than copied, so that `POST /api/settings` changing one of them changes what
    the next write actually does. Copying them into attributes is how a
    settings page ends up lying to the operator.
    """

    degraded = None

    #: True while `POST /api/apply` or `POST /api/restore` is inside a write.
    #:
    #: `POST /api/quit` is the reader, and it reads this *without* taking
    #: `lock`: the whole point of the flag is to answer while a write is
    #: holding the lock, and a quit that waited for it would shut the server
    #: down the instant the write finished -- which is the one moment it must
    #: not. So it is a plain attribute, written under `_busy_lock` by
    #: `begin_write`/`end_write` and readable by anybody.
    busy = False

    def __init__(self, save_folder=None, config_path=None, backup_root=None,
                 read_only=False, strict_version_check=False, settings=None,
                 settings_path=None, log_path=None):
        if settings is None:
            settings = settingsmod.Settings()
            settings.first_run = not settingsmod.save_count(save_folder or "")
        settings.override(save_folder=save_folder, backup_folder=backup_root,
                          read_only=read_only or None,
                          strict_version_check=strict_version_check or None)
        self.settings = settings
        self.settings_path = (settings_path or settings.path
                              or settingsmod.default_path())
        self.config_path = config_path
        self.log_path = log_path
        self.lock = threading.RLock()
        self._busy_lock = threading.Lock()
        self._writes = 0
        self._plans = collections.OrderedDict()
        self._plans_full = {}
        self._roundtrip = (None, None)
        notes = []
        self.config, self.created = cfgmod.load(config_path, notes)
        self.sync_taxonomy()
        self._save = None
        self._save_path = None
        self._save_token = None
        if self.created:
            notes.extend(self.fit_default_to_save())
        #: what `cfgmod.load` and `fit_default_to_save` had to say about the
        #: configuration this run started with. `/api/bootstrap` carries them:
        #: a config that means less than it appears to must not be silent.
        self.config_notes = notes

    def fit_default_to_save(self):
        """Drop the shipped rules this save has nowhere to put. -> [notes].

        W14. `default_config()` routes thirteen categories into
        `chest1`..`chest10` plus the Nutrient Processor, and those keys are the
        save's own; the page labels a container by the *player's* name for it.
        On a save where the chests have been named, the routing shelf therefore
        rendered pairings like "Raw Resources -> Raw Resources (chest1)" and
        "Fish -> S-Class and Illegal Modules +2 more" -- rules nobody wrote,
        read as advice, on the first screen a first-timer sees.

        Only on first creation, and only rules whose destination this save does
        not have: a configuration somebody has edited is a statement to
        respect (the same rule `cfgmod.load` keeps about absent keys), and a
        rule pointing at a container that *is* there is a rule this tool can
        honour. A save with no storage containers at all therefore starts with
        no routing, which is what the guided empty state on the Configuration
        tab is for.

        Written back, not only held in memory: the file was created a moment
        ago by `load`, and a default that says less than it appears to is worse
        on disk than in a variable.

        A save that cannot be read leaves the rules alone. There is nothing to
        compare against, and stripping every rule because the save folder is
        on a drive that is not plugged in would be the destructive reading of
        "not present in this save".
        """
        rules = list(self.config.get("bucket_rules") or [])
        if not rules:
            return []
        try:
            keys, _sortable, _dead = self.container_keys()
        except Exception as exc:
            log.info("the shipped rules were kept as they are: this save's "
                     "containers could not be listed (%s: %s)",
                     type(exc).__name__, exc)
            return []
        have = set(keys or [])
        kept, gone = [], []
        for rule in rules:
            dests = cfgmod.stores_of(rule)
            if dests and all(d not in have for d in dests):
                gone.append(rule)
            else:
                kept.append(rule)
        if not gone:
            return []
        with self.lock:
            self.config["bucket_rules"] = kept
            self.config = cfgmod.store(self.config_path, self.config,
                                       backup=False)
            self.sync_taxonomy()
        names = sorted(set(d for rule in gone
                           for d in cfgmod.stores_of(rule)))
        log.info("the shipped default dropped %d rule(s) naming %s: not in %s",
                 len(gone), ", ".join(names), self.save_folder)
        return ["the shipped rules for %s were dropped from the new "
                "configuration: this save has no such container, and a rule "
                "that can never fire is not a starting point. %s"
                % (", ".join(names),
                   "Route your categories on the Configuration tab."
                   if kept else
                   "Nothing is routed yet; the Configuration tab is where you "
                   "say where each category goes.")]

    # ------------------------------------------------------------ settings

    @property
    def save_folder(self):
        return normal_folder(self.settings.save_folder)

    @property
    def backup_root(self):
        return normal_folder(self.settings.resolved_backup_folder())

    @property
    def read_only(self):
        return self.settings.read_only

    @property
    def strict_version_check(self):
        return self.settings.strict_version_check

    @property
    def first_run(self):
        """True while there is nothing to show: no settings file yet, or a save
        folder that holds no `save*.hg`. The server serves the first-run page
        at `/` for exactly this condition, and stops as soon as it clears."""
        return not self.settings.folder_ok()

    def apply_settings(self, new):
        """Swap in a validated `Settings` and drop anything derived from the
        old one. -> the changed keys that need a restart.

        The save cache and the minted plans both belong to a save folder: a
        fingerprint printed for `save2.hg` in one folder must not stay
        approvable after the folder changes underneath it.
        """
        with self.lock:
            old = self.settings
            self.settings = new
            changed = [k for k in settingsmod.KEYS
                       if getattr(old, k) != getattr(new, k)]
            if "save_folder" in changed:
                self._save = None
                self._save_path = None
                self._save_token = None
                self._roundtrip = (None, None)
                self.clear_plans()
            root = new.resolved_backup_folder()
            if root:
                try:
                    os.makedirs(root, exist_ok=True)
                except OSError:
                    pass
            return [k for k in changed if k in settingsmod.RESTART_REQUIRED]

    # --------------------------------------------------------------- writes

    def begin_write(self):
        """Mark a write as in progress. Paired with `end_write` in a `finally`.

        Counted rather than a bare `busy = True`, because two applies arriving
        together are both "a write is running": the second waits on `lock`, and
        the first to finish would otherwise clear the flag while the second is
        still in the write sequence. `POST /api/quit` would then be allowed to
        pull the server down mid-apply, which is the exact thing the flag
        exists to prevent.
        """
        with self._busy_lock:
            self._writes += 1
            self.busy = True

    def end_write(self):
        with self._busy_lock:
            self._writes = max(0, self._writes - 1)
            self.busy = self._writes > 0

    # ------------------------------------------------------------- backups

    def list_backups(self):
        """Every backup folder, newest first, or `[]`.

        The `[]` is for a backup root that cannot be listed -- a folder on a
        disconnected drive, a permission change -- and not for a missing
        `safety.list_backups`: the route and the health panel both call this on
        every poll, and neither has anywhere to put an exception.
        """
        try:
            return safety.list_backups(self.backup_root) or []
        except Exception as exc:
            log.warning("the backup folder %s could not be listed: %s",
                        self.backup_root, exc)
            return []

    def prune_backups(self):
        """Apply the `backup_keep` retention to the backup root. -> [removed].

        R8: `backup_keep` had no reader anywhere, so the Settings tab stated a
        policy that did not exist and every apply left a full copy of the save
        forever. The rules are `safety.prune_backups`': never the newest, never
        a folder with no manifest. This is only the call site, and it answers
        `[]` rather than raising -- a backup that could not be removed must not
        turn a proved write into an error. It says so at `warning`, so a
        retention that is failing on every apply is visible in `/api/log`
        instead of looking like a policy nobody configured (review two, Q14).
        """
        keep = self.settings.backup_keep
        if not keep:
            return []
        try:
            return list(safety.prune_backups(self.backup_root, keep) or [])
        except Exception as exc:
            # Named in full: a retention that raises on every apply looks
            # exactly like a retention nobody configured, and the only place
            # the difference can show up is this line in `/api/log`.
            log.warning("backup retention did not run on %s (keep %s): %s: %s",
                        self.backup_root, keep, type(exc).__name__, exc)
            return []

    def roundtrip(self):
        """`{state, detail}` for the selected save's identity round trip,
        cached per save signature.

        Step 4 of the write sequence, run early so the health panel can say
        "this save re-serialises byte-for-byte" before anyone presses apply.
        It decodes and re-encodes a whole save, which is why the answer is
        cached: the health panel is polled.
        """
        with self.lock:
            try:
                save = self.save()
                sig = save.signature()
            except Exception as exc:
                return {"state": "unknown", "detail": "%s" % exc}
            if self._roundtrip[0] == sig and self._roundtrip[1]:
                return self._roundtrip[1]
            rep = safety.Report()
            try:
                safety.verify_roundtrip(save.path, rep)
                last = rep.steps[-1] if rep.steps else {}
                out = {"state": "ok",
                       "detail": last.get("detail", "re-serialises byte-for-byte")}
            except safety.Refused as exc:
                out = {"state": "fail", "detail": "%s" % exc}
            except Exception as exc:
                out = {"state": "unknown",
                       "detail": "%s: %s" % (type(exc).__name__, exc)}
            self._roundtrip = (sig, out)
            return out

    # ------------------------------------------------------------- loading

    def resolve_save(self, path):
        """A client's spelling of a save file -> the path the filesystem has.

        R6: the folder was compared exactly, so on NTFS `SAVE9.HG` was accepted
        (same directory string) and `.../SAVES/save9.hg` was refused, and the
        accepted half was the damaging one -- `write_atomic` replaces onto the
        *client's* spelling, which measurably renamed the operator's files
        (`save9.hg` -> `SAVE9.HG`, `mf_save9.hg` -> `mf_SAVE9.HG`), named the
        backup folder after it and split the plan signature, which is taken
        from the basename.

        So both halves are answered the same way on every filesystem: a folder
        whose case differs is the same folder, and a file name that matches only
        case-insensitively is accepted -- as the name in the directory listing,
        never as the one that was typed (`savemodel.match_name`, which refuses
        an ambiguous listing rather than picking). A name with no match at all
        is still `Invalid`, because this program does not create saves.

        `os.path.normcase` alone answered for the interpreter's platform and
        not for the filesystem, so on Linux the page's own `SAVE.HG` came back
        as "there is no SAVE.HG in the selected save folder". The comparison
        is folded here, and the path returned is still built from the selected
        folder and the listed name -- never from what the client typed.
        """
        if not isinstance(path, str):
            raise Invalid("'file' must be the name or path of a save file",
                          where="file")
        # A bare `save10.hg` is resolved against the save folder, not the
        # process working directory: `os.path.abspath` would put it wherever
        # the sorter happens to have been started from and the check below
        # would then reject a name this server itself listed.
        if not os.path.dirname(path):
            path = os.path.join(self.save_folder, path)
        path = os.path.normpath(os.path.abspath(path))
        folder = self.save_folder
        if _folded(os.path.dirname(path)) != _folded(folder):
            raise Invalid("%s is not in the selected save folder"
                          % os.path.basename(path), where="file")
        try:
            names = os.listdir(folder)
        except OSError as exc:
            raise Invalid("the save folder %s could not be read (%s)"
                          % (folder, exc), where="file")
        name = match_name(names, os.path.basename(path))
        if name is not None:
            real = os.path.join(folder, name)
            if os.path.isfile(real):
                return real
        raise Invalid("there is no %s in the selected save folder"
                      % os.path.basename(path), where="file")

    def save(self, path=None, reload=False):
        with self.lock:
            path = path or self._save_path
            if path is None:
                saves = list_saves(self.save_folder)
                if not saves:
                    raise Invalid("there are no saveN.hg files in %s"
                                  % self.save_folder, where="file")
                path = saves[0]["path"]
            path = self.resolve_save(path)
            # R15: the bytes, not just the timestamp. The cached save is asked
            # for its own `signature()` -- pinned in `SaveFile.__init__` to the
            # bytes it was parsed from -- and that is compared with the
            # signature of the file on disk, the same string built the same
            # way, so a same-length edit written back with the original
            # timestamp is a miss instead of a hit.
            #
            # `_save_token` is the belt to that braces: a build whose
            # `signature()` carries no hash cannot compare equal to a
            # hash-bearing one, and reloading on every request because of that
            # would be a 206 ms decode per poll. Both differing is what makes
            # it stale, so the guard is correct whichever shape `signature()`
            # has.
            token = disk_signature(path)
            cached = (self._save.signature()
                      if self._save is not None and self._save_path == path
                      else None)
            if reload or cached is None \
                    or (cached != token and self._save_token != token):
                self._save = SaveFile(path)
                self._save_path = path
                # recomputed, not the token from above: this one describes the
                # file as of *after* the read, so a write that landed while the
                # save was being decoded is caught by the next request rather
                # than being recorded as the state we loaded.
                self._save_token = disk_signature(path)
                self.clear_plans()
            return self._save

    def container_keys(self):
        """-> (every container key, the keys a rule may send items TO, the
        keys with no cells at all).

        Extractor cores count as containers: they can be named as a source. They
        are deliberately absent from the second list, because nothing is ever
        sorted into one.

        The third list is the containers a rule may name and get nothing from:
        no cells, so nothing can be in one and nothing can be put in one. It
        is separate from "not sortable" because the two deserve different
        answers -- a technology grid is a rule somebody should fix, and a
        cargo grid the game merged away in Waypoint is not.
        """
        with self.lock:
            s = self.save()
            cs = s.containers() + s.extractor_containers()
            return ([c.key for c in cs],
                    [c.key for c in cs if c.sortable],
                    [c.key for c in cs if is_dead_container(c)])

    def sync_taxonomy(self):
        """Push the config's custom buckets and item overrides into the item
        table. Called wherever the config is loaded or replaced.

        `apply_overrides` mutates the module-level singleton, so every caller
        must hold the lock.
        """
        with self.lock:
            db().apply_overrides(self.config)
            return self.config

    def validate(self):
        with self.lock:
            try:
                ck, sk, dk = self.container_keys()
            except Exception:
                ck, sk, dk = None, None, None
            return cfgmod.validate(self.config, ck, sk, dk)

    # --------------------------------------------------------------- plans

    def mint_plan(self, fingerprint, signature, full=None):
        """Remember a fingerprint this server printed, bounded to MAX_PLANS.

        `full` is the 64-character digest the same plan carries, kept against
        the 8-character token the operator reads off the screen: R10, so that
        `do_apply` can hand the write path the full digest even when the client
        echoes the short form. It is a separate map rather than a richer value
        because `plan_signature` is read by the tests and by `do_apply` as the
        signature and nothing else.
        """
        with self.lock:
            self._plans.pop(fingerprint, None)
            self._plans[fingerprint] = signature
            if full:
                self._plans_full[fingerprint] = full
            while len(self._plans) > MAX_PLANS:
                gone, _sig = self._plans.popitem(last=False)
                self._plans_full.pop(gone, None)

    def plan_signature(self, fingerprint):
        with self.lock:
            return self._plans.get(fingerprint)

    def plan_full(self, fingerprint):
        """-> the full digest minted for this token, or None.

        A client that sends 64 hex is answered with what it sent: that *is*
        the full digest, and a plan minted before `fingerprint_full` existed
        has none to look up.
        """
        with self.lock:
            if fingerprint and len(fingerprint) == 64:
                return fingerprint
            return self._plans_full.get(fingerprint)

    def clear_plans(self):
        with self.lock:
            self._plans.clear()
            self._plans_full.clear()


class Degraded(object):
    """The stand-in served when `App` could not be built at all (GOAL.md P2-2).

    Three failures are fatal to `App.__init__` and none of them is the
    operator's fault in a way a traceback would help with: an unreadable config
    file, a config file from a newer build, and a missing packaged data file. In
    each case the process still binds the port and serves one page that names
    the path and the fix, plus the two read-only routes that page needs. Every
    other route answers 503 with the one error shape.

    It carries the same attribute names as `App` wherever the health route
    reads them, so `GET /api/health` is one function rather than two.
    """

    #: nothing here writes to a save -- apply and restore are 503 while
    #: degraded -- so the flag `POST /api/quit` reads is simply never set. It is
    #: named rather than left to `getattr`, because "this shape carries the
    #: attributes the routes read" is the contract this class exists to keep.
    busy = False

    #: the config-unreadable page is the only one that offers a repair, because
    #: it is the only one where "start again from the default" is not
    #: destroying something this build cannot read back.
    def __init__(self, kind, problem, html_page, settings, settings_path=None,
                 config_path=None, log_path=None, detail=None,
                 allow_reset=False, rebuild=None):
        self.degraded = self
        self.kind = kind                # config_unreadable|config_too_new|data_missing
        self.problem = problem          # a clause for the 503 sentence
        self.html = html_page
        self.detail = detail
        self.allow_reset = allow_reset
        self.settings = settings
        self.settings_path = settings_path or settings.path
        self.config_path = config_path
        self.log_path = log_path
        self.config = None
        self.created = False
        self.lock = threading.RLock()
        self._rebuild = rebuild         # () -> App, after the fix is applied

    @property
    def save_folder(self):
        return normal_folder(self.settings.save_folder)

    @property
    def backup_root(self):
        return normal_folder(self.settings.resolved_backup_folder())

    @property
    def read_only(self):
        return self.settings.read_only

    @property
    def strict_version_check(self):
        return self.settings.strict_version_check

    @property
    def first_run(self):
        return not self.settings.folder_ok()

    def list_backups(self):
        return []

    def prune_backups(self):
        return []

    def roundtrip(self):
        return {"state": "unknown",
                "detail": "the sorter is in a reduced mode and has not read a "
                          "save"}

    def apply_settings(self, new):
        """Settings can still be changed while degraded -- the page may need to
        point at a different save folder before the config is fixed -- but
        nothing derived from them exists yet, so there is nothing to drop."""
        with self.lock:
            old = self.settings
            self.settings = new
            return [k for k in settingsmod.KEYS
                    if getattr(old, k) != getattr(new, k)
                    and k in settingsmod.RESTART_REQUIRED]

    def recover(self):
        """-> a live `App`, or raise. Called after the offered fix ran."""
        if self._rebuild is None:
            raise Invalid("there is nothing this page can retry; restart the "
                          "sorter")
        return self._rebuild()
