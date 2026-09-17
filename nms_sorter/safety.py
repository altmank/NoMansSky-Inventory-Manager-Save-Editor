"""Everything between "the operator pressed apply" and a byte on disk.

The sequence is `save-edit/research.md` section 4.3, in order, refusing at the
first failure. Nothing here is optional and nothing here is reordered. A lock
is taken before step 1 and released whatever happens.

  0. take the lock beside the save        -- one writer per save file
 0b. sweep a temp file a killed write left behind
  1. record whether NMS.exe is running, and refuse a running game that was
     not confirmed to be sitting on the main menu
 1b. the save gates: refuse an expedition outright, and refuse a `Version`
     outside `savemodel.SUPPORTED_VERSIONS` only when the operator asked for
     that with `strict_version_check` -- by default the version is recorded
     as an `info` step and the apply continues, because steps 4, 6 and 7
     catch a layout change on the file itself
  2. back up the save and its mf_          -- timestamped, hashed, verified
 2b. write the backup manifest, outcome "in progress"
  3. decode the backup copy, not the original
  4. prove the identity round trip on this specific file
  5. transform
  6. encode, decode the result again, compare
  7. prove nothing outside the named containers changed
  8. prove the mf_ is one this build can update: format 2004, no integrity hash
  9. temp file, fsync, atomic replace
 9b. rewrite the manifest, outcome "save written, metadata pending"
 10. mf_ sizes at 0x38/0x3C, and nothing else in that file
 11. re-read both files from disk
 12. rewrite the manifest, outcome "written"

The manifest used to be step 12 alone. That made the recovery artifact for the
worst failure -- a kill between the two writes -- the one thing the recovery
path cannot use, because restore verifies against the manifest and retention
refuses to prune a folder without one. It is now written the moment the copy
exists and rewritten as the outcome changes; a refusal after step 2 rewrites it
with the sentence it refused with and with an outcome that says what is on
disk: "refused" before step 9, "refused after write" after it. The outcome only
ever moves forwards, because "refused" means "changed nothing" and a run that
replaced the save cannot claim that (write-path review two, Q1).

The main menu is the documented place to run this from: the player saves,
quits to the menu, sorts, and loads the save again. The game on the menu is
holding the file at rest, which is why a confirmed menu is allowed and a
loaded save is not; the confirmation is the player's, because no process check
can tell a menu from a session.

`restore()` is the same shape in the other direction: the manifest's file names
and the folder it was taken from checked before anything is locked, the lock,
the game step, both copies verified against the manifest before anything is
written, both files written atomically, then re-read and re-hashed.
`prune_backups()` is the retention `backup_keep` always described and nothing
implemented; it prunes only folders whose manifest describes a settled run.

Step 4 is the one that makes the rest safe: if a save ever appears that we
cannot reproduce byte-for-byte, we do not know enough about it to edit it.

Step 8 is placed where it is on purpose. It reads the metadata and writes
nothing, and it used to be part of the metadata step *after* the save had
already been written -- so a save whose mf_ this build cannot update would
have been rewritten and then left with metadata contradicting it. A pair that
disagrees is worse than a pair that was never touched.
"""
import contextlib
import datetime
import errno
import hashlib
import json
import logging
import os
import re
import shutil
import struct
import time

from . import codec
from . import config as cfgmod
from .codec import (dumps, loads, frame_payload, read_payload,
                    meta_read, meta_slot, meta_decode, meta_encode)

log = logging.getLogger("nms_sorter.safety")

#: the metadata layout this build decoded field by field. 2003 and 2001 exist
#: in the wild (mf_save6.hg in the operator's corpus is a 2003) and put the
#: fields somewhere else.
META_FORMAT = 2004

#: 0x08..0x18 is a SpookyHash, 0x18..0x38 a SHA-256. Every mf_ seen so far
#: leaves both all-zero, which is the only reason the write path may rewrite a
#: file without recomputing them.
META_HASH_RANGE = (0x08, 0x38)

#: how many differing JSON paths the step-7 guard will enumerate. Hitting it is
#: a refusal, not a truncation: a guard that stops listing has stopped guarding.
DIFF_LIMIT = 100000

#: a lock older than this whose pid is gone is a leftover, not a live writer
STALE_LOCK_SECONDS = 600

LOCK_SUFFIX = ".nms-sorter.lock"


# ---------------------------------------------------------------------------
# 1. is the game running, and where is the player if it is
# ---------------------------------------------------------------------------
#
# A running game is no longer a refusal. The primary flow is: save in game,
# quit to the main menu, sort, load the save again -- and on the menu the game
# is not writing the file. What the process check cannot tell is a menu from a
# loaded session, so the player says which, and `at_main_menu` is that answer.
# Without it a running game is still refused, in a sentence that names the
# tick.
#
# The check itself lives in `platform.py`, which is the only module allowed to
# branch on the operating system (GOAL.md P4-1). It used to live here, and
# `ctypes.windll`, `mbcs` and a `creationflags` literal at module scope in the
# file that owns the write path meant this module could not even be imported on
# Ubuntu CI -- so the planner and the codec, which are platform-neutral, could
# not be tested there either.
#
# Both names are re-exported rather than referenced through the module,
# because `safety.game_status` and `safety.GAME_PROCESS` are the seam the test
# suite patches (`conftest.no_game_running`) and the server reads.
from .platform import (GAME_PROCESS, game_status,          # noqa: F401,E402
                       refusal_sentence, write_is_busy)

#: `platform.pid_alive` under the name the lock has always called it by. The
#: body used to live here, duplicating `platform`'s copy line for line; the
#: underscore name stays because it is the seam the suite patches to force the
#: two-writer interleaving (`tests/test_review1.py`), and a lock whose staleness
#: decision cannot be paused by a test cannot be proved atomic by one.
from .platform import pid_alive as _pid_alive               # noqa: F401,E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 0. the lock
# ---------------------------------------------------------------------------

def lock_path(save_path):
    """One lock per save file, beside the file it guards.

    Per save rather than per folder, because applying to `save2.hg` is no
    reason to block `save10.hg`; beside the file rather than in the state
    directory, because a lock that does not travel with what it guards guards
    nothing on a second machine.
    """
    return save_path + LOCK_SUFFIX


def temp_path(save_path):
    """The temp file a write goes through. The pid is in the name so two
    processes cannot collide on it even if the lock is somehow bypassed."""
    return "%s.nms-sorter-%d.tmp" % (save_path, os.getpid())


def read_lock(path):
    """{pid, created, age} for an existing lock file, as best it can be read.

    A lock whose contents are unreadable is still a lock: the file existing is
    the claim, and the JSON inside it is only there to name who to blame.
    """
    lp = lock_path(path)
    out = {"path": lp, "pid": None, "created": None, "age": None, "mtime": None}
    try:
        out["mtime"] = os.path.getmtime(lp)
        out["age"] = max(0.0, time.time() - out["mtime"])
    except OSError:
        return out
    try:
        with open(lp, encoding="utf-8") as fh:
            body = json.load(fh)
        out["pid"] = body.get("pid")
        out["created"] = body.get("created")
    except (ValueError, OSError):
        pass
    if not out["created"]:
        out["created"] = datetime.datetime.fromtimestamp(
            os.path.getmtime(lp), datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
    return out


#: the guard a writer takes before it may reclaim a stale lock. See
#: `_reclaim_stale_lock`.
RECLAIM_SUFFIX = ".reclaim"

#: how many times the acquire loop will go round. One pass per possible
#: reclaim, plus a final pass that can only refuse.
LOCK_ATTEMPTS = 3


def _is_stale(info):
    """Is this lock a leftover rather than a live writer?

    Two conditions, both required: older than ten minutes, and its pid gone.
    `pid_alive` errs towards "alive", so an unreadable pid keeps the lock.
    """
    return (info["age"] is not None
            and info["age"] > STALE_LOCK_SECONDS
            and not _pid_alive(info["pid"]))


def _guard_is_abandoned(guard):
    """Is this `.reclaim` guard a leftover rather than a live recovery?

    Either of two answers is enough, and both are needed:

    * its pid is gone -- a recovery whose process does not exist cannot finish,
      and there is no reason to wait ten minutes to say so. This is the half
      that closes Q8's window: a killed reclaimer used to make a stale lock
      unreclaimable for `STALE_LOCK_SECONDS`, with a refusal that blamed a
      writer that was dead.
    * it is older than `STALE_LOCK_SECONDS` -- the fallback for a guard whose
      pid cannot be read at all (a truncated write, or an empty file from the
      build that wrote nothing into it), and for a pid that has been reused.

    A guard whose pid is alive and which is younger than that is left alone:
    that is a real recovery in progress, and interrupting it is how two
    writers get inside one reclaim.
    """
    try:
        age = time.time() - os.path.getmtime(guard)
    except OSError:
        return False                     # gone already; nothing to reclaim
    pid = None
    try:
        with open(guard, encoding="utf-8") as fh:
            pid = json.load(fh).get("pid")
    except (ValueError, OSError):
        pid = None
    if pid is not None and not _pid_alive(pid):
        return True
    return age > STALE_LOCK_SECONDS


def _reclaim_stale_lock(lp, path, report=None):
    """Take over a lock judged stale, atomically. -> an open fd, or None.

    The old code was a check, an unlink and a create, and that is three
    operations where one is needed: two writers that both read the same stale
    lock could both get inside, because the second to unlink removed the
    *live* lock the first had just created (review one, R1). Measured with
    threads, and a scheduler produces the same interleaving across two
    processes, since the shared state is the filesystem.

    This is the same decision made once. `RECLAIM_SUFFIX` is created with
    `O_EXCL`, so only one writer is ever inside; it re-reads the lock under
    that guard, so a writer whose judgement was made before somebody else's
    recovery finds a fresh lock and refuses instead; and it moves the stale
    file aside with `os.replace` and creates its own with `O_EXCL` before
    releasing the guard, so the window where the lock does not exist is not
    one another writer can slip a create into unnoticed -- if it does, our
    create fails, we return None and the caller goes round to refuse.

    Nothing here ever unlinks a lock that is live: the only `os.replace` of
    one happens after a re-read under the guard said it was stale.

    Both files it creates are named so `sweep_temp_files` can take them: a
    kill runs no `finally`, and a guard or a moved-aside lock that nothing
    ever removes is R18's complaint about two more files in the operator's
    save folder (Q8). The guard carries its pid so a killed recovery can be
    told from a live one without waiting ten minutes.
    """
    guard = lp + RECLAIM_SUFFIX
    try:
        gfd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Somebody else is recovering, or was killed while doing so. Neither
        # is a reason to refuse yet: drop a guard that cannot belong to a live
        # recovery and let the caller go round again.
        if _guard_is_abandoned(guard):
            try:
                os.unlink(guard)
            except OSError:
                pass
        return None
    except OSError:
        return None
    try:
        # The pid, for the same reason the lock carries one: a guard left by a
        # kill used to be indistinguishable from a live recovery for ten
        # minutes, in which a stale lock could not be reclaimed and the
        # refusal named a process that was gone (Q8).
        try:
            os.write(gfd, json.dumps({"pid": os.getpid(),
                                      "created": utc_now()}).encode("utf-8"))
        except OSError:
            pass
        info = read_lock(path)
        if not _is_stale(info):
            return None                 # somebody got here first; re-decide
        moved = "%s.stale-%d" % (lp, os.getpid())
        try:
            os.replace(lp, moved)
        except OSError:
            return None
        log.info("removing a stale lock: %s (pid %s, %d seconds old, "
                 "that process is gone)", lp, info["pid"],
                 int(info["age"] or 0))
        if report is not None:
            report.info("lock", "removed a stale lock from pid %s, %d "
                        "seconds old" % (info["pid"], int(info["age"] or 0)))
        try:
            fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            fd = None
        try:
            os.unlink(moved)
        except OSError:
            log.warning("could not remove the reclaimed lock %s", moved)
        return fd
    finally:
        os.close(gfd)
        try:
            os.unlink(guard)
        except OSError:
            log.warning("could not remove the reclaim guard %s", guard)


def _acquire_lock(lp, path, report=None):
    """An open fd for the exclusive lock, or `Refused`. Never returns None.

    Written as one loop whose every branch either returns or raises, rather
    than as a loop that leaves an `fd` behind for the caller to check: a
    post-loop "if fd is None" would be a refusal that cannot fire, which is the
    R12 mistake and no better here than it was there.
    """
    def refuse():
        info = read_lock(path)
        msg = ("another sorter instance is writing this save (pid %s, since "
               "%s)" % (info["pid"], info["created"]))
        if report is not None:
            report.fail("lock", msg)
        raise Refused(msg)

    for _attempt in range(LOCK_ATTEMPTS):
        try:
            return os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if not _is_stale(read_lock(path)):
                refuse()                 # a live lock is a refusal, at once
            fd = _reclaim_stale_lock(lp, path, report)
            if fd is not None:
                return fd
            # Lost the reclaim race. Go round: whatever is there now gets a
            # fresh decision, and a lock that is live by then is refused above.
    # Every round found a stale lock and lost the race to take it over.
    # `LOCK_ATTEMPTS` bounds that rather than spinning: a save this contended
    # is one to refuse and tell somebody about.
    refuse()


@contextlib.contextmanager
def held_lock(path, report=None):
    """Hold the exclusive lock for `path`, or refuse.

    `O_CREAT | O_EXCL` is the whole mechanism: the filesystem decides, not a
    check followed by a create. A lock older than `STALE_LOCK_SECONDS` whose
    pid is gone is reclaimed with a log line (see `_reclaim_stale_lock`),
    because the alternative is that one crash makes a save unwritable until
    somebody deletes a file they have never heard of. A lock that is live is a
    refusal.

    After the lock is ours the pid is written, flushed and read back. It costs
    one stat and one small read, and it is the only way this process can find
    out that something else replaced the file underneath it.
    """
    lp = lock_path(path)
    fd = _acquire_lock(lp, path, report)
    try:
        os.write(fd, json.dumps({"pid": os.getpid(),
                                 "created": utc_now()}).encode("utf-8"))
    finally:
        os.close(fd)
    back = read_lock(path)
    if back["pid"] != os.getpid():
        # Not ours. Do not unlink it: whoever owns it is entitled to it.
        msg = ("the lock on this save was taken by another writer (pid %s) "
               "while this one was starting; nothing was changed"
               % (back["pid"],))
        if report is not None:
            report.fail("lock", msg)
        raise Refused(msg)
    try:
        yield lp
    finally:
        try:
            os.unlink(lp)
        except OSError:
            log.warning("could not remove the lock %s", lp)


def structural_diff(a, b, path=(), out=None, limit=400):
    """Every JSON path at which two decoded documents differ.

    Type-strict, because step 7 makes a claim about *bytes* ("nothing outside
    these containers changed") and this function is how it checks. Three edits
    used to change the payload and produce no differing path (review one, R17):

      * `true` -> `1`. `True == 1` in Python, and the old `isinstance(a, (int,
        float))` escape sent a bool and an int to a plain `!=`.
      * `0` -> `0.0`. Same escape, and `0 == 0.0`.
      * a change of dict key *order*, because the walk was over a set union.

    The one type difference that is not a byte difference is `RawFloat` against
    a plain `float`: both spell a JSON number, and which one a value is depends
    only on whether the decoder saw the literal. That pair falls through to the
    `raw` comparison below, which is a comparison of the spellings.
    """
    if out is None:
        out = []
    if len(out) >= limit:
        return out
    if type(a) is not type(b) and not (isinstance(a, float)
                                       and isinstance(b, float)):
        out.append("/".join(str(p) for p in path))
        return out
    if isinstance(a, dict):
        if list(a) != list(b) and set(a) == set(b):
            # Same keys, different order: the same object to Python and a
            # different byte sequence to the game. Reported at the dict's own
            # path, because no single key moved.
            out.append("/".join(str(p) for p in path))
        for k in set(a) | set(b):
            if k not in a or k not in b:
                out.append("/".join(str(p) for p in path + (k,)))
            else:
                structural_diff(a[k], b[k], path + (k,), out, limit)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append("/".join(str(p) for p in path) + "[len]")
        for i in range(min(len(a), len(b))):
            structural_diff(a[i], b[i], path + (i,), out, limit)
    else:
        if isinstance(a, float) and isinstance(b, float):
            ra = getattr(a, "raw", None)
            rb = getattr(b, "raw", None)
            if ra is not None and rb is not None:
                if ra != rb:
                    out.append("/".join(str(p) for p in path))
                return out
        if a != b:
            out.append("/".join(str(p) for p in path))
    return out


def first_last_diff(a, b):
    n = min(len(a), len(b))
    first = None
    for i in range(n):
        if a[i] != b[i]:
            first = i
            break
    if first is None and len(a) == len(b):
        return None, None
    if first is None:
        first = n
    last = None
    for i in range(1, n + 1):
        if a[-i] != b[-i]:
            last = max(len(a), len(b)) - i
            break
    return first, last


class Refused(Exception):
    """A safety check said no. The message is written for the operator.

    `report` is the `Report` the run had filled in when it refused, or None
    for a refusal raised with no report to hand. The server answers a refusal
    with 409 and used to carry only `str(exc)`, so an apply that refused at
    step 11 -- after the save had already been replaced -- told the page one
    sentence and nothing about the ten steps that had passed (write-path
    review two, Q1). Carrying the report is what lets the 409 include `steps`.
    """

    def __init__(self, *args, **kw):
        report = kw.pop("report", None)
        Exception.__init__(self, *args, **kw)
        self.report = report


class Report(object):
    def __init__(self):
        self.steps = []

    def ok(self, label, detail=""):
        self.steps.append({"state": "ok", "label": label, "detail": detail})

    def info(self, label, detail=""):
        self.steps.append({"state": "info", "label": label, "detail": detail})

    def fail(self, label, detail=""):
        self.steps.append({"state": "fail", "label": label, "detail": detail})
        raise Refused("%s: %s" % (label, detail) if detail else label,
                      report=self)


# ---------------------------------------------------------------------------
# the filesystem, in words
# ---------------------------------------------------------------------------

def _reason(exc):
    """The operating system's own words for an `OSError`, with its number."""
    text = getattr(exc, "strerror", None) or str(exc) or exc.__class__.__name__
    if getattr(exc, "errno", None) is not None:
        return "%s, errno %s" % (text, exc.errno)
    return text


def _write_denied(path):
    """-> the `OSError` that says `path` cannot be written, or None.

    An open, not a stat. `os.access(path, os.W_OK)` was the question, and on
    Windows it answers from the read-only *attribute* alone, so a save the
    operator had locked down with an ACL was replaced without complaint while
    the docstring claimed both mechanisms were honoured (review 5, finding 4).
    Measured there: `icacls /deny <user>:(W,WD,AD,WEA,WA)`, then
    `os.access(W_OK)` True and `open(path, "r+b")` `PermissionError 13`.

    `os.O_WRONLY` alone: no `O_CREAT`, no `O_TRUNC`, no `O_APPEND`. The call
    either hands back a descriptor for a file it has not touched, closed
    immediately, or raises. Nothing is written, created or truncated, which
    is what lets this run before the temp file and leave a refused folder
    exactly as it was.

    Two answers are deliberately *not* refusals:

    * a missing file. `os.replace` writes a *name* into the directory, so
      whether a name can be created there is the directory's question, and
      `_folder_denied` is where it is asked.
    * a sharing violation. That is the game holding the save, which has its
      own sentence and its own fix, and it is reached by letting the replace
      fail rather than by guessing here. `os.open` reports it as `EACCES`
      with no `winerror`, exactly like a permission, so the two are told
      apart by `platform.write_is_busy`, which asks `CreateFileW`.
    """
    try:
        fd = os.open(path, os.O_WRONLY)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return None if write_is_busy(path) else exc
    os.close(fd)
    return None


def _folder_denied(folder):
    """-> True when this process cannot create a new name in `folder`.

    Asked by creating one and removing it. `os.access(<folder>, os.W_OK)`
    answers True for a folder Windows will refuse to write in -- the
    read-only attribute it reads does not apply to directories at all --
    which left the folder half of `_denied_by_permissions` dead on the one
    platform apply runs on, and reported an ACL-denied folder as the game
    holding the save (review 5, finding 4 and note 5).

    The probe name carries the pid and is unlinked in a `finally`. It is
    created in the folder `write_atomic` is about to put its own temp file
    in, and only on a path where a write has already failed, so it adds
    nothing the write path was not already going to create.
    """
    if not os.path.isdir(folder):
        return False
    probe = os.path.join(folder, "nms-sorter-probe-%d.tmp" % os.getpid())
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        # Somebody else's probe, or our own from a killed run: the folder
        # plainly accepts a new name.
        return False
    except OSError:
        # No share lock to run into: a name that does not exist yet cannot be
        # held open by anybody, so a refusal here is the folder's own.
        return True
    try:
        os.close(fd)
    finally:
        try:
            os.unlink(probe)
        except OSError:
            log.warning("could not remove the probe file %s", probe)
    return False


def _denied_by_permissions(path):
    """Is a `PermissionError` on `path` the file's own permissions?

    -> True for a save this process cannot open for writing, or one in a
    folder it cannot create a name in: that is the operator's filesystem and
    the sentence for it is the one that names the folder. -> False for a file
    this process could write if nothing else had it open, which since apply
    may run with the game up is the case worth naming: the game has the save
    open.

    Two probes and nothing else. No modification times, no process
    heuristics: the two cases really do differ in whether the file can be
    opened for writing, and a guess here would be a guess about the one
    failure a player can fix.
    """
    if _write_denied(path) is not None:
        return True
    return _folder_denied(os.path.dirname(path) or ".")


def refuse_oserror(exc, path, what="write", after_write=False,
                   held_open=False):
    """Turn a filesystem failure into a refusal the operator can act on.

    A read-only save (the attribute set, or a sync client holding the file) is
    `PermissionError: [WinError 5] Access is denied`; a full disk is
    `OSError: [Errno 28]`. Both used to leave the server with an HTTP 500 and a
    traceback where every other failure in this file produces a sentence
    (review one, R7).

    `after_write` is which promise the sentence may make. "nothing was changed"
    was hardcoded and justified by "both callers are *before* the
    `os.replace`" -- true when `write_atomic` had one caller, and false once it
    had three: step 10 writes the `mf_` after the save has been replaced, and
    `restore` calls it once per file. Measured by failing `os.replace` only for
    the `mf_`: the save on disk was the new one and the operator was told
    nothing had changed (write-path review two, Q2).

    So the caller says. `after_write=False` keeps the promise it can keep;
    `after_write=True` says what is on disk instead of promising anything.

    `held_open` and a `PermissionError` together get their own pair of
    sentences, because since apply may run with the game up that combination
    has an obvious cause and an obvious fix: the game has the save or its
    `mf_` open, which is what a loaded session does and what the main menu
    does not. Windows refuses the `os.replace` in step 9 and the `mf_` patch
    in step 10 with `[WinError 5] Access is denied`, and "the save folder
    refused the write" told the player nothing they could act on.

    `held_open` is passed by `write_atomic` and by nothing else: it is a
    statement about *which file* is being written, not about the error.
    Step 2's `copy2` fails on the same errno when a sync client holds the
    save, and the copy that failed there is the one in the backup folder,
    which the game has never heard of. `_denied_by_permissions` then separates
    the two cases that remain. No mtime guessing and no process heuristics:
    the operating system said no, and this is what it means when it does.

    `EACCES` and nothing else, on top of that. `PermissionError` covers
    `EPERM` too, and `EPERM` is a different statement: a Linux immutable file
    (`chattr +i`) or an NFS export saying no is not the game holding the save,
    and "go to the main menu, or close the game" is unactionable advice for
    either (review 5, note 5). Windows refuses a share-locked file with
    `ERROR_ACCESS_DENIED`, which Python raises as `errno 13`, so the one case
    this sentence exists for keeps it.
    """
    denied = (getattr(exc, "errno", None) == errno.EACCES
              or getattr(exc, "winerror", None) == 5)
    if (held_open and isinstance(exc, PermissionError) and denied
            and not _denied_by_permissions(path)):
        if after_write:
            return Refused("%s: the game has the save open (%s: %s); go to "
                           "the main menu, or close the game, and try again. "
                           "The save was written but this file was not, so the "
                           "two files on disk disagree"
                           % (what, path, _reason(exc)))
        return Refused("%s: the game has the save open (%s: %s); go to the "
                       "main menu, or close the game, and try again"
                       % (what, path, _reason(exc)))
    if after_write:
        return Refused("%s: the save folder refused the write (%s: %s); the "
                       "save was written but this file was not, so the two "
                       "files on disk disagree"
                       % (what, path, _reason(exc)))
    return Refused("%s: the save folder refused the write (%s: %s); nothing "
                   "was changed" % (what, path, _reason(exc)))


# The six sentences across `refuse_oserror`, `refuse_read_oserror` and
# `read_hash` are each written out in full rather than assembled from a shared
# tail. A sentence built from fragments cannot be found in the source by the
# string a person searched the documentation for, and `tests/test_docs.py`
# checks exactly that: every `###` heading in `docs/TROUBLESHOOTING.md` has to
# exist as a literal somewhere in the package. A helper that saved six lines
# would have cost six documentable sentences.


def refuse_read_oserror(exc, path, what="restore", after_write=False):
    """The read half of `refuse_oserror`.

    `restore` read its source with a bare `open` while every other filesystem
    failure in this module is a sentence (R7). Nothing locks the backup root:
    `prune_backups` runs outside the save lock and `rmtree`s folders, so a
    second sorter process pruning while this one restores is an ordinary
    interleaving. Measured by deleting the `mf_` copy between the save's write
    and the metadata's read: a `FileNotFoundError` straight out of `restore`,
    which the server turns into a 500 with a reference number -- after the
    save had already been written back (write-path review two, Q5).
    """
    if after_write:
        return Refused("%s: the backup copy could not be read (%s: %s); the "
                       "save was written but this file was not, so the two "
                       "files on disk disagree"
                       % (what, path, _reason(exc)))
    return Refused("%s: the backup copy could not be read (%s: %s); nothing "
                   "was changed" % (what, path, _reason(exc)))


def read_all(path, what="restore", after_write=False):
    """Every byte of `path`, or a `Refused` naming it."""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        raise refuse_read_oserror(exc, path, what, after_write=after_write)


def read_hash(path, what="restore", after_write=False):
    """`sha256(path)`, or a `Refused` naming the file.

    The re-read at the end of a restore hashes a file this process has just
    written, so a failure there is not "the backup copy could not be read" and
    gets its own sentence. It is the last unhandled read on that path (Q5).
    """
    try:
        return sha256(path)
    except OSError as exc:
        if after_write:
            raise Refused("%s: the file could not be read back (%s: %s); the "
                          "save was written but this file was not, so the two "
                          "files on disk disagree"
                          % (what, path, _reason(exc)))
        raise Refused("%s: the file could not be read back (%s: %s); nothing "
                      "was changed" % (what, path, _reason(exc)))


#: `savemodel.on_disk_path` under the name this module has always called it by.
#: The body moved next to the other file-naming code because the `mf_` half of
#: the pair is named there (`meta_path`, `gates`, `list_saves`), and those
#: three sites were the last route left to R6's damage (Q9).
from .savemodel import on_disk_path                          # noqa: E402


def meta_path_for(save_path):
    """The `mf_` that belongs to `save_path`, spelled the way the filesystem
    spells it. It need not exist.

    One function for what used to be `"mf_" + os.path.basename(save_path)` at
    three sites. R6 resolved the *save* to the spelling on disk; the metadata
    half was not fixed, so `mf_SAVE9.HG` beside a `save9.hg` was found by
    `os.path.exists` under the invented spelling, backed up under it, and
    renamed by step 10's `os.replace`. Measured: one apply and the folder
    afterwards held `mf_save9.hg` (write-path review two, Q9).
    """
    return on_disk_path(os.path.join(os.path.dirname(save_path),
                                     "mf_" + os.path.basename(save_path)))


#: `save9.hg.nms-sorter-1234.tmp`: the temp file a killed write leaves behind.
TEMP_RE = re.compile(r"\.nms-sorter-\d+\.tmp$")

#: the two files `_reclaim_stale_lock` creates: `<lock>.reclaim`, the guard
#: that makes the reclaim one decision, and `<lock>.stale-<pid>`, the lock it
#: moved aside. Both are removed on the way out and neither survives anything
#: but a kill -- which is R18's argument, one release later, about two more
#: files in the operator's save folder that nothing swept (Q8).
RECLAIM_LEFTOVER_RE = re.compile(
    r"\.nms-sorter\.lock\.reclaim$|\.nms-sorter\.lock\.stale-\d+$")

#: everything the sweep will take, by name. The live lock (`.nms-sorter.lock`,
#: no suffix) is deliberately not in here: it is removed by the writer that
#: holds it and reclaimed by age and pid, not by a file sweep.
SWEEPABLE_RE = re.compile("%s|%s" % (TEMP_RE.pattern,
                                     RECLAIM_LEFTOVER_RE.pattern))

#: how old a stray temp file has to be before the sweep takes it. An hour is
#: far longer than any write takes and far shorter than the operator's patience
#: for a save folder that fills up a save's worth of bytes per crash.
TEMP_SWEEP_SECONDS = 3600


def sweep_temp_files(folder, report=None, older_than=TEMP_SWEEP_SECONDS):
    """Remove this program's leftovers from a save folder. -> [removed].

    Three names, all of them ours and all of them bounded by a regular
    expression rather than by a glob:

      * `<save>.nms-sorter-<pid>.tmp` -- the temp file a killed write leaves.
        `SIGKILL` runs no `finally`, so they accumulated a save's worth of
        bytes each and nothing ever cleaned them up (review one, R18).
      * `<save>.nms-sorter.lock.reclaim` -- the guard a killed *reclaim*
        leaves. Until it was swept, nothing would ever have looked at it (Q8).
      * `<save>.nms-sorter.lock.stale-<pid>` -- the lock a killed reclaim had
        already moved aside.

    The live lock file itself is not swept: it is removed by the writer that
    holds it and reclaimed by age *and* pid, which is a decision this function
    is not the place to make.

    Age rather than pid, deliberately: a pid is reused, and a temp file whose
    number happens to match a live process is not evidence of anything. An hour
    is long enough that a file this old cannot belong to a write in progress,
    and the lock is held while the sweep runs anyway.
    """
    removed = []
    try:
        names = os.listdir(folder)
    except OSError:
        return removed
    now = time.time()
    for name in names:
        if not SWEEPABLE_RE.search(name):
            continue
        p = os.path.join(folder, name)
        try:
            if now - os.path.getmtime(p) < older_than:
                continue
            os.unlink(p)
        except OSError:
            continue
        removed.append(name)
    if removed:
        log.info("swept %d stray file(s) from %s: %s",
                 len(removed), folder, ", ".join(sorted(removed)))
        if report is not None:
            report.info("temp files", "removed %d stray temp file(s) a killed "
                        "write left behind: %s"
                        % (len(removed), ", ".join(sorted(removed))))
    return removed


# ---------------------------------------------------------------------------
# verification that writes nothing
# ---------------------------------------------------------------------------

def verify_roundtrip(path, report=None):
    """Step 4, standalone. Proves this exact file re-serialises byte-for-byte."""
    r = report or Report()
    payload, info = read_payload(path)
    esc = b"\\/" in payload
    doc = loads(payload)
    again = dumps(doc, escape_slash=esc)
    if again != payload:
        f, l = first_last_diff(again, payload)
        r.fail("identity round trip",
               "this save does not re-serialise byte-for-byte; first difference at "
               "offset %s. We do not know enough about this file to edit it."
               % (f if f is not None else "?"))
    r.ok("identity round trip",
         "%d payload bytes decode and re-encode byte-for-byte%s"
         % (len(payload), " (pre-Frontiers \\/ escaping)" if esc else ""))
    reframed = frame_payload(payload)
    tmp_ok = codec.read_payload_bytes(reframed)[0] == payload
    if not tmp_ok:
        r.fail("container round trip", "reframing the payload does not decode back")
    r.ok("container round trip",
         "%d blocks, %d bytes on disk -> %d reframed"
         % (info["blocks"], info["disk"], len(reframed)))
    return r, payload, doc, esc


# ---------------------------------------------------------------------------
# the write
# ---------------------------------------------------------------------------

def backup(save_path, backup_root, report):
    """Step 2. Timestamped folder, hashed both ways, verified.

    The "does not exist" branch tests the basename, not `str.endswith`:
    `"...\\mf_save9.hg".endswith("save9.hg")` is true, so a save with no
    metadata beside it used to be refused as a missing *save*, and the three
    no-metadata branches downstream of here were all dead (review one, R4).
    """
    name = os.path.basename(save_path)
    # The `mf_` is resolved to the spelling on disk for the same reason the
    # save is: `"mf_" + name` is a name this code invents, and copying under
    # the invented spelling is what let step 10 rename the operator's metadata
    # file (Q9). `copied` then records the real name, which is what the
    # manifest carries and what `restore` writes back to.
    meta_src = meta_path_for(save_path)
    base = os.path.join(backup_root, "%s-%s" % (stamp(), os.path.splitext(name)[0]))
    folder, n = base, 1
    while os.path.exists(folder):          # two runs inside one second
        folder = "%s-%d" % (base, n)
        n += 1
    try:
        os.makedirs(folder, exist_ok=False)
    except OSError as exc:
        raise refuse_oserror(exc, folder, "backup")
    copied = []
    for src in [save_path, meta_src]:
        if not os.path.exists(src):
            if os.path.basename(src) == name:
                report.fail("backup", "%s does not exist" % src)
            report.info("backup", "no metadata file beside %s" % name)
            continue
        dst = os.path.join(folder, os.path.basename(src))
        try:
            shutil.copy2(src, dst)
        except OSError as exc:
            raise refuse_oserror(exc, dst, "backup")
        a, b = sha256(src), sha256(dst)
        if a != b or os.path.getsize(src) != os.path.getsize(dst):
            report.fail("backup", "copy of %s does not match the original"
                        % os.path.basename(src))
        copied.append({"file": os.path.basename(src), "sha256": a,
                       "size": os.path.getsize(src)})
    report.ok("backup", "%s -> %s" % (", ".join(c["file"] for c in copied), folder))
    return folder, copied


def write_atomic(path, data, what="write", after_write=False):
    """Step 9. Same directory, fsync, replace: a crash mid-write cannot leave a
    truncated save.

    The temp name carries the pid and the `finally` removes it on any failure.
    Both were missing: a fixed name meant two writers shared one temp file, and
    a raise between the write and the replace left it in the operator's save
    folder, where the game sees an unexpected file in its own directory.

    Every `OSError` out of the three operations becomes a `Refused` with a
    sentence naming the file and the reason (review one, R7). All three happen
    strictly before *this* target is replaced -- but this function has three
    callers now, and two of them run after another file has already been
    replaced. `after_write` is passed through to `refuse_oserror` so the
    sentence says what is on disk rather than promising that nothing moved
    (Q2).

    The target's own permissions are asked about first, because `os.replace`
    does not ask: replacing a file is a change to the *directory*, so a save
    the operator has marked read-only is replaced without complaint on Linux
    while Windows raises `PermissionError` from the same call. The refusal has
    to be the same sentence on both -- a save the operator has locked down is
    the operator saying "not this file", and honouring it on one platform
    only is the kind of difference this write path exists to not have.

    Asked by `_write_denied`, which opens for writing and closes again. It
    used to be `os.access(path, os.W_OK)`, which on Windows answers from the
    read-only attribute and not from the security descriptor, so the promise
    above held for one of the two Windows mechanisms: an `icacls /deny` save
    was replaced without complaint (review 5, finding 4). Nothing is written
    by the probe and it runs before the temp file, so a refusal leaves the
    folder exactly as it was.
    """
    denied = _write_denied(path)
    if denied is not None:
        raise refuse_oserror(denied, path, what, after_write=after_write)
    tmp = temp_path(path)
    try:
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            # `held_open`: this is a file in the save folder, so a
            # `PermissionError` here is something holding that file -- the
            # game, in a loaded save -- rather than the operator's own
            # permissions. `refuse_oserror` says which.
            raise refuse_oserror(exc, path, what, after_write=after_write,
                                 held_open=True)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                log.warning("could not remove the temp file %s", tmp)


def check_metadata_updatable(meta_path, report):
    """Step 8. Read the mf_ and decide whether step 10 could honestly rewrite
    it. Writes nothing.

    Two questions, both of which used to be answered by assumption: is this the
    layout whose fields were decoded, and does this file carry an integrity
    hash we cannot recompute? A stale hash written into a save is worse than a
    save left alone, and the format word is the difference between patching two
    size fields and patching two arbitrary bytes.
    """
    if not os.path.exists(meta_path):
        report.info("metadata is updatable",
                    "no mf_ file beside this save; there is nothing to update")
        return None
    try:
        meta = meta_read(meta_path)
    except Exception as exc:
        report.fail("metadata is updatable",
                    "this save's metadata could not be decoded (%s), so it "
                    "cannot be updated after the write; refusing rather than "
                    "leaving the pair disagreeing" % exc)
        return None
    if meta["format"] != META_FORMAT:
        report.fail("metadata is updatable",
                    "this metadata format (%d) is not one this build knows how "
                    "to update; only %d was decoded field by field"
                    % (meta["format"], META_FORMAT))
    lo, hi = META_HASH_RANGE
    if any(meta["plain"][lo:hi]):
        report.fail("metadata is updatable",
                    "this save's metadata carries an integrity hash this build "
                    "cannot recompute; refusing to write a stale one")
    report.ok("metadata is updatable",
              "mf_ format %d, %d bytes, 0x%02X..0x%02X all zero: the two size "
              "fields are the only thing that has to change"
              % (meta["format"], meta["raw_len"], lo, hi - 1))
    return meta


def update_metadata(meta_path, size_decompressed, size_disk, report):
    """Step 10. Two uint32 and nothing else, checked on the file afterwards.

    The check used to be an assertion over the bytes this function was about to
    write: `struct.pack_into` at `0x38` and `0x3C` can only touch
    `[0x38, 0x40)`, and `changed` was computed from exactly those two calls, so
    the refusal could not fire and the docstring's "and nothing else, asserted
    afterwards" asserted nothing (review one, R12).

    So it is done on the file that is now on disk, decrypted, against the
    plaintext as it was before: every byte outside `0x38..0x3F` has to be the
    byte it was, and the length has to be the length it was. That is a check
    and it is cheap -- the file is 432 bytes and it has just been read anyway.
    """
    slot = meta_slot(meta_path)
    raw_before = open(meta_path, "rb").read()
    plain_before = meta_decode(raw_before, slot)
    plain_after = bytearray(plain_before)
    struct.pack_into("<I", plain_after, 0x38, size_decompressed)
    struct.pack_into("<I", plain_after, 0x3C, size_disk)
    plain_after = bytes(plain_after)
    changed = [i for i in range(len(plain_before)) if plain_before[i] != plain_after[i]]
    blob = meta_encode(plain_after, slot)
    # `after_write`: step 9 has already replaced the save, so a failure here
    # must not end with "nothing was changed" (Q2).
    #
    # And it is its own step, not `write`: the default left the report with
    # two steps labelled `write` and opened the sentence `write: ...` for a
    # failure in the `mf_`, at the one moment the two files on disk disagree
    # and the difference between them is the whole message (review 5, note 6).
    write_atomic(meta_path, blob, "patch the mf_", after_write=True)
    check = meta_decode(open(meta_path, "rb").read(), slot)
    outside = [i for i in range(min(len(plain_before), len(check)))
               if not 0x38 <= i < 0x40 and plain_before[i] != check[i]]
    if outside or len(check) != len(plain_before):
        report.fail("metadata", "the size patch would touch a byte outside 0x38..0x3F")
    if check != plain_after:
        report.fail("metadata", "the rewritten mf_ does not decrypt back to what we wrote")
    report.ok("metadata",
              "mf_ 0x38 size_decompressed %d, 0x3C size_disk %d; %d byte(s) changed, "
              "all inside 0x38..0x3F. Timestamp, slot id, save name and summary "
              "untouched." % (size_decompressed, size_disk, len(changed)))
    return len(changed)


# ---------------------------------------------------------------------------
# the backup manifest
# ---------------------------------------------------------------------------

MANIFEST_NAME = "manifest.json"
STAMP_RE = re.compile(r"^(\d{8}-\d{6})")

#: the format `stamp()` writes and `prune_backups` parses back
STAMP_FORMAT = "%Y%m%d-%H%M%S"


def normalised_folder(path):
    """A folder path in the one spelling everything here compares against.

    Absolute and `normpath`ed, so `C:\\saves\\.` and `C:\\saves` are the same
    folder to the manifest and to `restore`. Not `normcase`d: the value is
    written into a manifest the operator reads, and lower-casing a Windows
    path there would be this code inventing a spelling again (R6). The
    comparison lower-cases; the record does not.
    """
    return os.path.normpath(os.path.abspath(path))


def same_folder(a, b):
    """Do these two paths name the same folder?

    `normcase` because Windows compares names case-insensitively, and then
    `realpath` as a second chance, because a save folder reached through a
    junction, a mapped drive or an 8.3 short name is still that folder and a
    restore into it is not the accident this comparison exists to catch.
    """
    if not a or not b:
        return False
    na, nb = normalised_folder(a), normalised_folder(b)
    if os.path.normcase(na) == os.path.normcase(nb):
        return True
    try:
        return (os.path.normcase(os.path.realpath(na))
                == os.path.normcase(os.path.realpath(nb)))
    except OSError:
        return False


def _app_version():
    import nms_sorter
    return getattr(nms_sorter, "__version__", "")


def _data_version():
    try:
        from .itemdb import data_version
        return data_version().get("game_build", "")
    except Exception:                       # data missing is not a write error
        return ""


#: the `outcome` field's vocabulary, in the order one apply moves through it.
#: `IN_PROGRESS` is written immediately after the backup, so the folder is
#: never a pile of unexplained bytes; `META_PENDING` is written between the
#: save's replace and the mf_'s, which is the one window in which the pair on
#: disk disagrees; `WRITTEN` is the end state; `REFUSED` is a run that stopped
#: after the backup was taken and changed nothing; `REFUSED_AFTER_WRITE` is a
#: run that replaced the save and then failed a check.
#:
#: That last value is the one Q1 was about. Six refusal sites can only fire
#: after step 9 -- `update_metadata`'s two and step 11's four -- and the
#: handler wrote `refused` for all of them, so the only machine-readable
#: record that an apply had happened said it had changed nothing, with the
#: plan row reset to null, at exactly the moment the operator needs the
#: backup. The outcome is monotonic now: nothing ever writes `refused` over a
#: marker that says the save has moved.
OUTCOME_IN_PROGRESS = "in progress"
OUTCOME_META_PENDING = "save written, metadata pending"
OUTCOME_WRITTEN = "written"
OUTCOME_REFUSED = "refused"
OUTCOME_REFUSED_AFTER_WRITE = "refused after write"

#: the outcomes that describe a run whose effect on disk is settled, and the
#: only ones retention may prune. `in progress`, `save written, metadata
#: pending` and `refused after write` all describe a save folder somebody may
#: still have to recover, so those folders are kept whatever `backup_keep`
#: says -- see `prune_backups`.
PRUNABLE_OUTCOMES = (OUTCOME_WRITTEN, OUTCOME_REFUSED)


def written_row(path):
    """`{file, sha256, size}` for a file this apply has just written, or None.

    The mirror image of the `save`/`meta` rows, which describe the *backup*
    copies: this one describes what the apply left in the save folder. Having
    both is what lets a restore tell "nobody has touched this since" from "the
    game has written this save since, and putting the backup back would throw
    that away".

    None when there is no such file -- a save with no `mf_` beside it -- which
    is the same answer `write_manifest` records for the backup half.

    `read_hash` rather than `sha256`, with `after_write=True`: this runs at
    step 12, after the operator's save has been replaced, so a file that will
    not read is a refusal whose sentence must not end "nothing was changed".
    No new sentence is minted for it; it is the one `read_hash` already has,
    and step 11 has just read the same file back, so reaching it means the
    filesystem went away between two reads.
    """
    if not path or not os.path.exists(path):
        return None
    return {"file": os.path.basename(path),
            "sha256": read_hash(path, "backup manifest", after_write=True),
            "size": os.path.getsize(path)}


def unchanged_since_apply(man, label):
    """-> (file name, set of digests that mean "not moved since"), or (None, None).

    `label` is `"save"` or `"meta"`. The answer is None when the question
    cannot be asked: a manifest with no `written` block (an older build, or an
    apply that did not reach step 12), or one whose name is not a plain file
    name -- the same guard `_verify_copy` applies, because this name is
    joined against the *save* folder and a manifest is a file the operator is
    invited to browse.

    Two digests are accepted, not one. `written` is what the apply left on
    disk, and is the real subject. The backup's own copy is the other, because
    restoring the same backup twice is not a session thrown away: after the
    first restore the file on disk *is* the backup's copy, which is exactly
    what a second restore would write, so there is nothing to discard and
    refusing would be a lie. Without it, pressing Undo twice -- or restoring a
    backup the page still lists after an Undo -- would be refused on the
    grounds that the game had written the save.
    """
    written = (man.get("written") or {}).get(label) or {}
    name, want = written.get("file"), written.get("sha256")
    if not name or not want or not is_plain_file_name(name):
        return None, None
    accept = set([want])
    copy = man.get(label) or {}
    if (copy.get("sha256")
            and os.path.normcase(copy.get("file") or "")
            == os.path.normcase(name)):
        accept.add(copy["sha256"])
    return name, accept


def write_manifest(folder, save_path, copied, plan, cfg,
                   outcome=OUTCOME_WRITTEN, refusal=None,
                   expected_fingerprint=None, written=None, game=None):
    """What this backup is, so a restore is not a guess.

    A backup folder with no manifest is a folder of bytes: nothing says which
    save they came from, what was about to be done to them, or whether they are
    still the bytes that were copied. Restore verifies against this file, and
    retention refuses to prune a folder that has none.

    Written three or four times per apply, not once at the end. It used to be
    step 12, which meant every failure after step 2 -- including the one that
    most needs a restore, a kill between the save's replace and the mf_'s --
    left the recovery artifact in exactly the state the recovery path cannot
    use, and retention could never prune it either (review one, R2, R3).
    Nothing in here depends on the write having succeeded: `copied` comes from
    step 2 and the plan from step 5.

    `outcome` is the string; `completed` is the same fact as a boolean, because
    a caller that only wants to know whether a backup describes a finished
    apply should not have to know the vocabulary.

    `save_dir` is the folder the save was taken from, absolute and normalised.
    It is what binds a backup to a place: the manifest used to record the
    save's *name* and nothing else, and `/api/restore` hands `restore`
    whatever folder is selected now, so a backup of one profile's `save9.hg`
    was put over another profile's `save9.hg` with every step reporting that
    the bytes matched -- which was true, and about the wrong file (write-path
    review two, Q4).

    `written` is what this apply left in the save folder, as
    `{"save": {...}, "meta": {...} or None}`, and only step 12 passes it: it
    is the hashes of the files *after* the write, which is a fact no earlier
    call has. It is the answer to the question restore could not ask. Restore
    used to verify that the backup's own copies still hashed to the manifest
    and that the folder matched, both of which stay true for as long as the
    backup sits there -- and neither of which says anything about the save.
    So a player who sorted, played for four hours and then pressed Undo got
    the pre-sort save back and lost the four hours, with every step reporting
    `ok`. The key is *absent*, not null, when there is nothing to record, so
    an older build's manifest and an apply that stopped before step 12 are
    the same case to `read_manifest`'s readers: unknown, say so, continue.

    No `fsync`: the manifest is rewritten up to four times per apply, and a
    manifest lost to a power cut is indistinguishable from one never written,
    which is the case `read_manifest` and `prune_backups` already handle.
    """
    name = os.path.basename(save_path)
    # Keyed on `normcase`, because `copied` carries the spelling the
    # filesystem has and `"mf_" + name` is the spelling this code can build:
    # on a case-insensitive filesystem those differ, and the lookup silently
    # missed, so a pair whose `mf_` is `mf_SAVE9.HG` recorded `meta: null`
    # and a restore had nothing to put back (Q9). The value keeps the real
    # name; only the key is folded.
    by_file = dict((os.path.normcase(c["file"]), c) for c in copied)
    save_row = by_file.get(os.path.normcase(name))
    meta_row = by_file.get(os.path.normcase("mf_" + name))
    if plan is not None:
        plan_row = {"fingerprint": plan.fingerprint,
                    "fingerprint_full": getattr(plan, "fingerprint_full", None),
                    "rows": len(plan.rows),
                    "containers": list(getattr(plan, "containers", []) or [])}
    else:
        # Before step 5 there is no plan object, only the digest the operator
        # approved. Recording that is the difference between "this backup
        # precedes the run you approved" and "this backup precedes something".
        plan_row = {"fingerprint": (expected_fingerprint or "")[:8] or None,
                    "fingerprint_full": (expected_fingerprint
                                         if expected_fingerprint
                                         and len(expected_fingerprint) == 64
                                         else None),
                    "rows": None, "containers": []}
    body = {
        "created": utc_now(),
        "app_version": _app_version(),
        "data_version": _data_version(),
        "outcome": outcome,
        "completed": outcome == OUTCOME_WRITTEN,
        "save_dir": normalised_folder(os.path.dirname(os.path.abspath(save_path))),
        "save": ({"file": save_row["file"], "sha256": save_row["sha256"],
                  "size": save_row["size"]} if save_row else None),
        "meta": ({"file": meta_row["file"], "sha256": meta_row["sha256"],
                  "size": meta_row["size"]} if meta_row else None),
        "plan": plan_row,
        "config_sha256": cfgmod.config_hash(cfg),
    }
    if game:
        # Step 1's own answer, kept because it is the one thing about a run
        # that cannot be reconstructed from the files afterwards: "game
        # running: at the main menu, as confirmed" and "game closed" are
        # different runs, and which one this was decides how a save that
        # looks wrong afterwards is read. It says what step 1 saw, not what
        # the run went on to do: the same note is written to the in-progress
        # and refusal manifests (review 5, finding 2).
        body["game"] = game
    if written:
        body["written"] = written
    if refusal:
        body["refusal"] = refusal
    path = os.path.join(folder, MANIFEST_NAME)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=2, ensure_ascii=False)
    return body


def read_manifest(folder):
    """The manifest in a backup folder, or None if it has none."""
    path = os.path.join(folder, MANIFEST_NAME)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return None


def is_plain_file_name(name):
    """Is this string a file name, rather than a path?

    Everything the manifest names has to live in the folder the manifest is
    in. `os.path.basename` is the test rather than a `".." not in name`
    substring check, because a name is a path on both platforms' rules at
    once here: `a/b`, `a\\b`, `C:\\x` and `..` all have to fail, and only the
    library knows all of them. `os.path.basename("a/b")` is `"b"` on Windows
    *and* on POSIX, so a name that is not its own basename is a path.
    """
    if not name or not isinstance(name, str):
        return False
    if name in (".", "..") or os.path.isabs(name):
        return False
    # Both separators, whichever platform we are on: a manifest written on
    # Windows is restored on Linux by the same code.
    if "/" in name or "\\" in name:
        return False
    return os.path.basename(name) == name and os.path.dirname(name) == ""


def _verify_copy(folder, row):
    """-> (state, detail) for one file the manifest names.

    `state` is True verified, False it does not hash, None there is nothing to
    check. "Missing" is its own answer rather than a hash failure, because a
    backup with no `mf_` copy and a backup whose `mf_` copy has rotted are
    different problems with different fixes.
    """
    if not row or not row.get("file"):
        return None, "the manifest names no file"
    if not is_plain_file_name(row["file"]):
        # The manifest is a plain JSON file in a folder the operator is invited
        # to browse, and this function joins the name it carries against a
        # folder. A `..` in it reached outside the backup folder and got
        # hashed, for `list_backups` as well as for `restore` (Q3).
        return False, ("the manifest names a file outside this backup folder "
                       "(%s)" % row["file"])
    path = os.path.join(folder, row["file"])
    if not os.path.exists(path):
        return False, "%s is missing from this backup" % row["file"]
    if not row.get("sha256"):
        return None, "the manifest carries no hash for %s" % row["file"]
    try:
        got = sha256(path)
    except OSError as exc:
        # A copy that exists and cannot be read is a third answer, and the one
        # a second process's retention pass produces. Reported rather than
        # raised, because this function's callers are `list_backups` -- which
        # must not fail a listing over one folder -- and restore's step 2,
        # which turns any non-True state into its own refusal (Q5).
        return False, ("%s could not be read from this backup (%s)"
                       % (row["file"], _reason(exc)))
    if got != row["sha256"]:
        return False, "%s does not hash to what the manifest says" % row["file"]
    if row.get("size") is not None and os.path.getsize(path) != row["size"]:
        return False, "%s is not the size the manifest records" % row["file"]
    return True, "%s verified" % row["file"]


def _newest_first(root):
    """Every name in the backup root, newest first by parsed stamp.

    `sorted(..., reverse=True)` over the names put a folder somebody renamed
    wherever its name happened to sort -- the same flaw as retention's, from
    the other side (Q7). A folder whose name is not a stamp has no age this
    code can establish, so it goes last, in reverse name order like the rest.
    """
    try:
        names = os.listdir(root)
    except OSError:
        return []
    def key(name):
        when = parsed_stamp(name)
        # (dated?, when, name), all reversed: dated folders first, newest of
        # them first, and `datetime.min` for the undated so they fall to the
        # end rather than sorting among real stamps.
        return (when is not None, when or datetime.datetime.min, name)
    return sorted(names, key=key, reverse=True)


def list_backups(root):
    """-> [{folder, stamp, save, size, sha256_ok, has_manifest, outcome, ...}],
    newest first.

    `sha256_ok` is None when there is nothing to check against, False when a
    file no longer hashes to what the manifest says, True when both halves of
    the pair do. The three answers are different and the page needs to be able
    to say which: "unknown" is not "corrupt".

    Both halves, not just the save: `sha256_ok` used to cover the save alone,
    so a backup whose `mf_` copy had rotted, or was simply absent, reported
    itself as verified -- and restore reads this field (review one, R9).
    `files` carries the per-file answer, and `problems` the sentences.

    `outcome` is the manifest's, so the page can show that an apply did not
    finish (`in progress`, `save written, metadata pending`) or changed nothing
    (`refused`).

    `current_matches` is the other direction: True when the files in the save
    folder still hash to what that apply left there, False when they do not --
    the game has written the save since, so restoring this backup would
    discard the play -- and None when it cannot be told, because the manifest
    records no `written` block or the file is not there now. It is what lets
    the page grey out an Undo that would throw a session away instead of
    offering it and then answering 409.

    The save folder is hashed at most once per file per call, not once per
    backup: `cache` is keyed on the resolved path, and every backup of one
    save names the same two files, so a root holding ten folders costs two
    hashes rather than twenty. The folder comes from each manifest's own
    `save_dir`, which is the folder restore would bind to anyway.
    """
    out = []
    if not os.path.isdir(root):
        return out
    cache = {}
    for name in _newest_first(root):
        folder = os.path.join(root, name)
        if not os.path.isdir(folder):
            continue
        man = read_manifest(folder)
        m = STAMP_RE.match(name)
        save = (man or {}).get("save") or {}
        meta = (man or {}).get("meta") or {}
        fn = save.get("file")
        if not fn:
            hgs = sorted(f for f in os.listdir(folder)
                         if f.lower().endswith(".hg") and not f.startswith("mf_"))
            fn = hgs[0] if hgs else None
        path = os.path.join(folder, fn) if fn else None
        size = os.path.getsize(path) if path and os.path.exists(path) else None
        files, problems, states = {}, [], []
        if man:
            for label, row in (("save", save), ("meta", meta)):
                if not row:
                    continue
                state, detail = _verify_copy(folder, row)
                files[label] = {"file": row.get("file"), "sha256_ok": state,
                                "detail": detail}
                states.append(state)
                if state is False:
                    problems.append(detail)
        if not states:
            ok = None
        elif False in states:
            ok = False
        elif all(s is True for s in states):
            ok = True
        else:
            ok = None
        current = _current_matches(man, cache) if man else None
        out.append({"folder": folder, "stamp": m.group(1) if m else None,
                    "save": fn, "size": size, "sha256_ok": ok,
                    "has_manifest": man is not None,
                    # Which folder this backup was taken from, so the page can
                    # say so before somebody restores it into another one (Q4).
                    "save_dir": (man or {}).get("save_dir"),
                    "outcome": (man or {}).get("outcome"),
                    "completed": (man or {}).get("completed"),
                    "refusal": (man or {}).get("refusal"),
                    # True/False/None: does the save folder still hold what
                    # this apply wrote? False means an Undo would discard
                    # whatever has been played since.
                    "current_matches": current,
                    "files": files, "problems": problems})
    return out


def _current_hash(cache, save_dir, name):
    """`sha256` of `name` in `save_dir` as it is now, or None.

    None for "cannot be told": no folder recorded, a name that is not a plain
    file name, a file that is not there, or one that will not read. A listing
    must not fail over one folder, and "unknown" is a real answer here -- the
    slot may simply be empty, and restoring into an empty slot discards
    nothing.

    `cache` is the per-`list_backups` memo, keyed on the normalised path so
    the same save is hashed once however many backups of it are listed.
    """
    if not save_dir or not name or not is_plain_file_name(name):
        return None
    path = os.path.join(save_dir, name)
    key = os.path.normcase(os.path.abspath(path))
    if key not in cache:
        try:
            cache[key] = sha256(path)
        except OSError:
            cache[key] = None
    return cache[key]


def _current_matches(man, cache):
    """True/False/None for one manifest: see `list_backups`.

    Both halves of the pair, for the same reason `sha256_ok` covers both: the
    refusal in `restore` fires on either, so a page that only looked at the
    save would offer an Undo the server then refuses. False wins over
    unknown -- one file that has demonstrably moved is enough to know an Undo
    would discard something.
    """
    states = []
    for label in ("save", "meta"):
        name, accept = unchanged_since_apply(man, label)
        if not name:
            continue
        got = _current_hash(cache, man.get("save_dir"), name)
        states.append(None if got is None else got in accept)
    if not states:
        return None
    if False in states:
        return False
    if all(s is True for s in states):
        return True
    return None


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------

def manifest_state(folder):
    """`"ok"`, `"missing"` or `"unreadable"` for a backup folder's manifest.

    `read_manifest` answers None for an absent manifest and for a truncated
    one, which makes a folder whose manifest was half-written indistinguishable
    from a folder that never had one. Retention treats both as "leave alone",
    but it says which in the log, because they are different accidents.
    """
    path = os.path.join(folder, MANIFEST_NAME)
    if not os.path.exists(path):
        return "missing"
    try:
        with open(path, encoding="utf-8") as fh:
            json.load(fh)
    except (ValueError, OSError):
        return "unreadable"
    return "ok"


def parsed_stamp(name):
    """The `datetime` a backup folder's name begins with, or None.

    `STAMP_RE` says the shape; `strptime` says whether it is a time. Both are
    needed: `20261301-000000` matches the shape and is not a date, and a
    folder whose name is not a stamp at all is a folder whose age nothing
    knows.
    """
    m = STAMP_RE.match(name or "")
    if not m:
        return None
    try:
        return datetime.datetime.strptime(m.group(1), STAMP_FORMAT)
    except ValueError:
        return None


def prunable_folders(rows, keep):
    """The folders retention may remove, oldest first. -> [folder].

    `rows` is `[(stamp, folder)]` sorted oldest first. Split out from
    `prune_backups` because "the newest is never removed" was a `continue`
    that could not run: `keep < 1` returns earlier, and for `keep >= 1`
    `rows[:len(rows) - keep]` cannot contain `rows[-1]`, so the line that
    claimed the rule was unreachable and the docstring's promise was made by
    the slice above it instead. That is R12's mistake, in the function written
    in the commit that fixed R12 (write-path review two, Q10).

    Now the newest is excluded from the candidates *before* the arithmetic, so
    the rule is a property of the code rather than a comment about it -- and
    it can be tested with a `keep` the arithmetic would otherwise satisfy by
    deleting everything.
    """
    surplus = len(rows) - int(keep)
    if surplus <= 0:
        return []
    return [folder for _stamp, folder in rows[:-1][:surplus]]


#: a backup folder being restored from right now carries this file. Retention
#: runs outside the save lock -- `server.do_apply` calls it after `apply_plan`
#: has released it -- so a second sorter process pruning the folder this one is
#: restoring from is an ordinary interleaving (Q5).
RESTORING_MARKER = ".restoring"

#: how long a `.restoring` marker is believed. A restore takes milliseconds; a
#: marker older than this is one a killed restore left behind, and honouring it
#: for ever would make a folder unprunable because of a crash.
RESTORING_MARKER_SECONDS = 3600


def is_being_restored(folder):
    """Is somebody restoring from this backup folder right now?"""
    p = os.path.join(folder, RESTORING_MARKER)
    try:
        return time.time() - os.path.getmtime(p) < RESTORING_MARKER_SECONDS
    except OSError:
        return False


@contextlib.contextmanager
def restoring_marker(folder, report=None):
    """Mark this backup folder in use for as long as the block runs.

    Advisory, not a lock: it is read by `prune_backups`, which is the only
    thing that deletes a backup folder, and the restore that holds it already
    holds the save's own lock. A marker that cannot be created is a log line
    rather than a refusal -- the restore itself is still safe, because every
    byte it writes has already been hashed against the manifest; what is lost
    is the guarantee that the source will still be there for a second attempt.
    """
    p = os.path.join(folder, RESTORING_MARKER)
    made = False
    try:
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"pid": os.getpid(), "created": utc_now()}))
        made = True
    except OSError as exc:
        log.warning("could not mark %s as being restored from (%s); retention "
                    "in another process could remove it mid-restore", folder,
                    exc)
        if report is not None:
            report.info("restore", "could not mark this backup as in use (%s); "
                        "it is still verified byte for byte" % _reason(exc))
    try:
        yield p
    finally:
        if made:
            try:
                os.unlink(p)
            except OSError:
                log.warning("could not remove the restore marker %s", p)


def prune_backups(root, keep):
    """Remove the oldest backup folders beyond `keep`. -> [removed folders].

    `backup_keep` was a setting with a label on the Settings page, a default in
    `settings.py` and no reader anywhere: every apply left a full copy of the
    save forever while the page stated a policy that did not exist (review one,
    R8).

    Six rules, each of them a thing that could otherwise go wrong:

    * `keep < 1` prunes nothing. "Keep none" is not a policy anybody means, and
      a zero from a mistyped setting must not delete every backup.
    * The newest is never removed, whatever the arithmetic says. See
      `prunable_folders`, which is where that is now true rather than claimed.
    * A folder with no manifest, or one that will not parse, is never removed.
      An unknown folder in the backup root is somebody's, and deleting saves
      because we cannot read a JSON file is the opposite of the job.
    * A folder whose manifest does not describe a settled run is never removed
      -- `in progress`, `save written, metadata pending` and `refused after
      write` all mean a save somebody may still have to recover, and the third
      of those is the one the operator needs *most* (Q1). Only
      `PRUNABLE_OUTCOMES` may go.
    * A folder somebody is restoring from is never removed, because retention
      runs outside the save lock and `rmtree` under a restore is a half-written
      save folder (Q5).
    * Oldest first, by the stamp in the folder name, *parsed* -- and a folder
      whose name does not parse is left alone rather than sorted first. The old
      code substituted `""` for an unparseable name, and the empty string sorts
      before every real stamp, so a folder the operator had renamed to say what
      it was for was the first thing retention deleted (Q7).

    None of the folders this function leaves alone counts towards `keep`
    either: they cannot be pruned, and counting them would keep fewer real
    backups than asked for.

    The stamp is local time, which is what `stamp()` writes, so across a DST
    fallback an hour of backups sort before backups taken an hour earlier. That
    is a known and accepted ordering: the alternative -- mtime -- is moved by a
    copy or a sync client, and the manifest's UTC `created` is not what the
    folder is named after, so a rename would make the two disagree. A backup
    an hour out of order inside one ambiguous hour costs one wrong retention
    decision; a stamp nobody can see costs the operator's ability to tell which
    folder is which.
    """
    removed = []
    if not keep or keep < 1 or not os.path.isdir(root):
        return removed
    rows = []
    for name in sorted(os.listdir(root)):
        folder = os.path.join(root, name)
        if not os.path.isdir(folder):
            continue
        state = manifest_state(folder)
        if state != "ok":
            log.info("leaving %s alone: its manifest is %s", folder, state)
            continue
        when = parsed_stamp(name)
        if when is None:
            log.info("leaving %s alone: its name does not begin with a stamp, "
                     "so its age cannot be established", folder)
            continue
        outcome = (read_manifest(folder) or {}).get("outcome")
        if outcome not in PRUNABLE_OUTCOMES:
            log.info("leaving %s alone: its outcome is %r, which is not a "
                     "finished run", folder, outcome)
            continue
        if is_being_restored(folder):
            log.info("leaving %s alone: a restore from it is in progress",
                     folder)
            continue
        rows.append((when, folder))
    rows.sort(key=lambda t: (t[0], t[1]))            # oldest first
    for folder in prunable_folders(rows, keep):
        try:
            shutil.rmtree(folder)
        except OSError as exc:
            log.warning("could not remove the old backup %s (%s)", folder, exc)
            continue
        log.info("pruned the old backup %s (keeping %d)", folder, int(keep))
        removed.append(folder)
    return removed


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------

def restore(folder, save_dir, report=None, at_main_menu=False,
            force=False):
    """Put a backup's two files back. Returns (report, result).

    A wrapper for the same reason `apply_plan` is one: a refusal raised outside
    a `Report.fail` -- an unreadable backup copy, a write the folder refused --
    carries no report of its own, and the server's 409 needs the steps that
    passed (Q1). The sequence is `_restore`.

    `at_main_menu` is the player's answer to the one question the process
    check cannot answer; see step 1 and the module docstring.
    """
    r = report or Report()
    try:
        return _restore(folder, save_dir, r, at_main_menu, force)
    except Refused as exc:
        if getattr(exc, "report", None) is None:
            exc.report = r
        raise


def _restore(folder, save_dir, report=None, at_main_menu=False,
             force=False):
    """Put a backup's two files back, with no report plumbing. See `restore`.

    The same shape as an apply and for the same reasons: one writer per save
    file, the game step first, nothing written until both copies have been
    proved against the manifest, and both files re-read and re-hashed
    afterwards. It writes two files and touches nothing else -- no metadata
    patch, because the metadata being restored is the metadata that described
    the save being restored.

    The manifest is read before the lock is taken, because the lock is named
    after the save and the manifest is what names the save. It is a read with
    no side effects; a folder with no manifest is refused before anything is
    locked at all. So are the two things the manifest has to prove before it
    may be acted on at all, because both of them decide *which file* gets
    written and neither needs a lock to answer:

    * every name it carries has to be a file name, not a path. It is a plain
      JSON file in a folder the operator is invited to browse, and the names
      went straight into `os.path.join` twice -- once against the backup folder
      to hash and once against the save folder to write. Measured: a manifest
      whose `save.file` was `..\\x.hg` replaced a file *outside the save folder*
      with the backup's bytes and reported all four steps `ok` (Q3).
    * it has to have come from the folder being written to. `server.do_restore`
      passes whatever folder is selected now, and the manifest recorded the
      save's name and nothing about where it lived, so a backup of one
      profile's `save9.hg` went over another profile's with every step
      reporting a byte-perfect match of the wrong file (Q4). `force` is for an
      operator who means it; the server does not expose it.

    Step 2b is the third thing `force` overrides, and the one a player is most
    likely to hit: whether the save in the folder is still the one the apply
    left there. Everything else this sequence checks is a property of the
    backup, which does not change while it sits on disk; the save does. See
    the step's own comment.

    After each file is written its modification time is set back to the backup
    copy's. `SaveFile.signature()` -- what the save cache compares -- carries
    `int(st_mtime)`, so a restore that left `now` there would make the page
    re-read a file whose bytes it already had. `shutil.copy2` preserved the
    original's timestamps into the backup, so the backup copy carries exactly
    the value to put back.
    """
    r = report or Report()
    folder = os.path.abspath(folder)
    save_dir = normalised_folder(save_dir)
    man = read_manifest(folder)
    if not man:
        r.fail("restore", "this backup has no manifest, so its contents cannot "
                          "be verified; copy the files back by hand if you are "
                          "sure")
    save_row = man.get("save") or {}
    meta_row = man.get("meta") or {}
    if not save_row.get("file"):
        r.fail("restore", "this backup's manifest does not name a save file, so "
                          "there is nothing to put back")
    for row in (save_row, meta_row):
        name = row.get("file")
        if name and not is_plain_file_name(name):
            r.fail("restore", "the manifest names a file outside its folder "
                              "(%s); refusing" % name)
    taken_from = man.get("save_dir")
    if not taken_from:
        # Backups written before the field existed. Restoring one is still the
        # right thing to do -- it is the operator's own backup -- but which
        # folder it came from is unknowable, so the check cannot be made and
        # says so instead of passing silently.
        r.info("restore", "this backup does not record which folder it was "
                          "taken from, so it cannot be checked against %s"
               % save_dir)
    elif not same_folder(taken_from, save_dir):
        if not force:
            r.fail("restore", "this backup was taken from %s; refusing to "
                              "write it into %s" % (taken_from, save_dir))
        # Forced. Not silent: the one thing this step exists to notice has
        # been overridden, and the report is where that belongs.
        r.info("restore", "this backup was taken from %s and is being written "
                          "into %s because force was asked for"
               % (taken_from, save_dir))
    target = on_disk_path(os.path.join(save_dir, save_row["file"]))
    with held_lock(target, r):
        # ---- 1: the same step as an apply, and the same reasoning. A running
        # game is not a refusal; a running game in a loaded save is, because
        # the game holds its own copy of the save and writes it out on its own
        # schedule. The main menu is where it is not doing that, and only the
        # player can say that is where they are.
        st = game_status()
        pids = ", ".join(str(p) for p in st["pids"]) or "?"
        if st["running"]:
            if not at_main_menu:
                r.fail("game", "%s is running as pid %s. Go to the main menu "
                       "in the game, then tick \"I am at the main menu, not in "
                       "a loaded save\" and try again."
                       % (GAME_PROCESS, pids))
            # The same wording as an apply, and for the same reason: this
            # line is printed before the restore has done anything, so it
            # cannot say what the restore did (review 5, finding 2).
            r.info("game", "running as pid %s: at the main menu, as confirmed"
                   % pids)
        elif not st["known"]:
            # Still a refusal, and on principle: an answer that cannot be got
            # is not an answer, and the tick is about *where* the player is,
            # not about whether the game is up at all.
            r.fail("game", refusal_sentence(st))
        else:
            r.ok("game", "%s is not in the process list (%s)"
                 % (GAME_PROCESS, st["method"]))

        # ---- 2: prove every copy before writing any of them
        rows = [("save", save_row)]
        if meta_row.get("file"):
            rows.append(("meta", meta_row))
        for _label, row in rows:
            state, detail = _verify_copy(folder, row)
            if state is not True:
                r.fail("verify the backup",
                       "%s, so this backup is not one to restore from" % detail)
        r.ok("verify the backup",
             "%s in %s hash to what the manifest records"
             % (", ".join(row["file"] for _l, row in rows), folder))

        # ---- 2b: has the save moved since the apply this backup precedes?
        #
        # Steps 2 and the folder binding both pass for a backup that is
        # perfectly intact and perfectly wrong to restore: they check the
        # *backup*, which does not change, and the folder, which does not
        # change either. Neither looks at the save. So sort, play for four
        # hours, press Undo, and the pre-sort save goes back over four hours
        # of play with every step reporting `ok`. Nothing in the report even
        # hints at it, which is the worst property a safety sequence can have.
        #
        # Under the lock and before any write, because it is a read of the
        # file step 3 is about to replace. Silent when it passes: a restore
        # reports four steps and the common case has nothing to add to them.
        for label, _row in rows:
            name, accept = unchanged_since_apply(man, label)
            if not name:
                continue
            current = on_disk_path(os.path.join(save_dir, name))
            if not os.path.exists(current):
                # Restoring into an empty slot is the case restore exists for.
                # There is nothing there to discard.
                continue
            if read_hash(current, "restore") in accept:
                continue
            # The sentence is written out twice rather than assembled once
            # into a local, for the reason given above `refuse_read_oserror`:
            # `tests/test_docs.py` reads the *call arguments* out of the AST,
            # so a message held in a variable is a message no documentation
            # check can see and nobody can find by searching the source for
            # the sentence they were shown.
            if not force:
                r.fail("restore",
                       "%s has changed since this backup was taken; the game "
                       "(or something else) has written it since, and "
                       "restoring would discard that. Copy the backup out by "
                       "hand if you are sure" % name)
            # Forced. Said out loud for the same reason the folder-binding
            # override is: the one check that would have stopped this has been
            # overridden, and the report is where that belongs.
            r.info("restore",
                   "%s has changed since this backup was taken; the game (or "
                   "something else) has written it since, and restoring would "
                   "discard that. Copy the backup out by hand if you are sure"
                   % name)
        if not unchanged_since_apply(man, "save")[0]:
            # An older build's manifest, or an apply that stopped before step
            # 12. The backup is still the operator's own and still worth
            # restoring; what cannot be established is whether anything has
            # been played since, so it is said rather than passed over.
            r.info("restore", "the manifest does not record what the apply "
                              "wrote, so whether the save changed since "
                              "cannot be checked")

        # ---- 3: write, save first, then its metadata.
        #
        # Under the `.restoring` marker: retention runs outside this lock --
        # `server.do_apply` calls `prune_backups` after `apply_plan` has
        # released it -- so a second sorter process is free to `rmtree` the
        # folder these bytes are coming out of (Q5). The marker is created
        # under the lock and removed in the `finally`; a killed restore leaves
        # one, which `is_being_restored` stops honouring after an hour.
        restored = []
        with restoring_marker(folder, r):
            for _label, row in rows:
                src = os.path.join(folder, row["file"])
                dst = on_disk_path(os.path.join(save_dir, row["file"]))
                # `after_write` from the second file on: by then the save has
                # been replaced, and a sentence ending "nothing was changed"
                # would be false (Q2).
                after = bool(restored)
                data = read_all(src, "restore", after_write=after)
                write_atomic(dst, data, "restore", after_write=after)
                try:
                    stat = os.stat(src)
                    os.utime(dst, (stat.st_atime, stat.st_mtime))
                except OSError as exc:
                    # The bytes are right; only the timestamp is not. Worth a
                    # line rather than a refusal: the page will re-read the
                    # save, which is the only reader of that field.
                    r.info("restore", "could not put %s's timestamp back (%s); a plan "
                           "printed before the apply will have to be re-run"
                           % (row["file"], _reason(exc)))
                restored.append(os.path.basename(dst))
        r.ok("restore", "%s written back into %s"
             % (", ".join(restored), save_dir))

        # ---- 4: re-read both files from disk and hash them again
        for _label, row in rows:
            dst = on_disk_path(os.path.join(save_dir, row["file"]))
            got = read_hash(dst, "re-read from disk", after_write=True)
            if got != row["sha256"]:
                r.fail("re-read from disk",
                       "%s was written but does not hash to the backup's copy; "
                       "the file on disk is %s" % (row["file"], got[:8]))
        r.ok("re-read from disk",
             "%d file(s) re-read and re-hashed: every one matches the backup"
             % len(rows))

    return r, {"restored": restored, "backup": folder, "manifest": man}


# ---------------------------------------------------------------------------
# the sequence
# ---------------------------------------------------------------------------

def _document_path(save, container):
    """A container's path as the keys this document actually carries, or None.

    `Container.path` is plain names (`BaseContext/PlayerStateData/Inventory`)
    with integer indices for the ship and vehicle lists. Which *key* each name
    is stored under depends on the file: the game obfuscates most of them, but
    not `Chest11Inventory` or `Chest12Inventory`, and a document that has been
    through `codec.remap` carries every one in the clear. `Doc.key` answers
    that question per node and is why those two chests work everywhere else;
    this walks the document with it, the way `Doc.at` does.

    None means the path does not resolve on this document, which is a refusal
    at the call site: a container the plan named and the model cannot find is
    not something to write around.
    """
    node = save.doc
    out = []
    for step in container.path:
        if isinstance(step, int):
            if not isinstance(node, list) or step >= len(node):
                return None
            out.append(str(step))
            node = node[step]
            continue
        if not isinstance(node, dict):
            return None
        k = save.d.key(node, step)
        if k is None:
            return None
        out.append(str(k))
        node = node[k]
    return "/".join(out)


def _fingerprint_matches(plan, expected):
    """Compare the full digest when the caller has one, the short form when it
    does not.

    The server mints and hands back `plan.fingerprint`, the 8 characters a
    person can read off the screen, and the page echoes that. Comparing the
    full digest is the point of 3.9, so accept it whenever it is what arrived
    and fall back to the prefix while the client still sends the short form.

    Both sides have to be non-empty. `expected or ""` against
    `plan.fingerprint or ""` made "nobody approved anything" a match for "this
    plan has no fingerprint" (review one, R11) -- unreachable through the
    server, which refuses an empty token first, and through `build_plan`, which
    always mints one, so it was a guard that held only because of its callers,
    in the one function whose whole job is not to trust them.
    """
    expected = expected or ""
    full = getattr(plan, "fingerprint_full", None) or ""
    short = plan.fingerprint or ""
    if not expected or not (full or short):
        return False, (full or short or None)
    if len(expected) == 64 and full:
        return expected == full, full
    return expected == short, plan.fingerprint


def _shown_digest(value):
    """A digest as a person reads it: the first 8 hex characters and an ellipsis.

    GOAL.md 3.9 is explicit -- "the first 8 hex characters are shown to the
    person, the full digest is compared" -- and every other surface obeys it:
    `fingerprint 8af8fd73`, "Plan 8af8fd73 is fixed", the manifest's own
    `fingerprint` field. The step-5 refusal was the one place that printed all
    64, twice in one sentence, at the exact moment the reader is trying to tell
    two values apart (UI review 3, X7). Eight characters is what tells them
    apart; the rest is noise the comparison does not need a human for.

    The comparison itself is untouched: this formats the *message* only.
    Anything that is not a 64-character digest is passed through as it is, so a
    short token still prints whole and `None` still prints as "(none)".
    """
    v = "" if value is None else str(value)
    if not v:
        return "(none)"
    if len(v) == 64:
        return v[:8] + "..."
    return v


def apply_plan(save_path, cfg, expected_fingerprint, plan_builder, backup_root,
               compress=True, at_main_menu=False,
               strict_version_check=False):
    """The whole sequence, under the lock. Returns (report, result).

    `at_main_menu` is the player's confirmation that a running game is sitting
    on its main menu rather than in a loaded save. It is the documented flow
    (save, quit to the menu, sort, load) and it is the only thing that makes a
    running game something other than a refusal; see step 1.

    `save_path` is resolved to the spelling the filesystem has before anything
    else happens, so the lock, the backup folder's name, the plan signature and
    the file `os.replace` lands on all agree, and an apply through `SAVE9.HG`
    cannot rename the operator's save (review one, R6).

    Every refusal that leaves here carries the report it refused with, so the
    409 the server answers can list the steps that passed. A refusal raised
    outside a `Report.fail` -- `refuse_oserror` out of `write_atomic`, for one
    -- has no report of its own, and this is where it is given one (Q1).
    """
    save_path = on_disk_path(os.path.abspath(save_path))
    r = Report()
    try:
        with held_lock(save_path, r):
            return _apply_locked(save_path, cfg, expected_fingerprint,
                                 plan_builder, backup_root, compress,
                                 at_main_menu, strict_version_check, r)
    except Refused as exc:
        if getattr(exc, "report", None) is None:
            exc.report = r
        raise


def _apply_locked(save_path, cfg, expected_fingerprint, plan_builder, backup_root,
                  compress=True, at_main_menu=False,
                  strict_version_check=False, r=None):
    """Steps 1 to 12, with the lock already held. Raises Refused."""
    from .savemodel import SUPPORTED_VERSIONS, SaveFile

    # ---- 0b: a killed write leaves its temp file behind; take it now, under
    # the lock, before anything else writes to this folder.
    sweep_temp_files(os.path.dirname(save_path), r)

    # ---- 1
    # A running game is not a refusal. The documented flow is to save, quit to
    # the main menu and sort from there: the game on the menu is not writing
    # the save, and it is where a player already is when they alt-tab. What
    # cannot be checked is a menu against a loaded session, so the player
    # confirms it and the run records which case it was. A loaded session is
    # still a refusal -- the game holds its own copy of the save and writes it
    # out on its own schedule, so whichever writes last wins outright.
    st = game_status()
    pids = ", ".join(str(p) for p in st["pids"]) or "?"
    game_note = None
    if st["running"]:
        if not at_main_menu:
            r.fail("game", "%s is running as pid %s. Go to the main menu in "
                   "the game, then tick \"I am at the main menu, not in a "
                   "loaded save\" and try again." % (GAME_PROCESS, pids))
        # No verb that asserts the outcome. This note is written into the
        # in-progress manifest and into the refusal manifest as well as the
        # written one, so "applied from the main menu" put "nothing was
        # applied" and "applied" in one file -- on the one field that exists
        # because it cannot be reconstructed from the files afterwards
        # (review 5, finding 2). Measured there: `outcome: refused | game:
        # 'game running: applied from the main menu, as confirmed'`. What
        # step 1 knows is the process and the confirmation, and that is true
        # of all three outcomes.
        r.info("game", "running as pid %s: at the main menu, as confirmed"
               % pids)
        game_note = "game running: at the main menu, as confirmed"
    elif not st["known"]:
        # Two reasons a check can fail, two sentences, one refusal -- and it
        # is still a refusal, because the tick says where the player is, not
        # whether the game is up. `platform.refusal_sentence` keeps the
        # Windows wording (which names the error) and substitutes the platform
        # sentence when the method is `unsupported: <os>`, because no error
        # string helps somebody whose operating system has no NMS.exe to look
        # for.
        r.fail("game", refusal_sentence(st))
    else:
        r.ok("game", "%s is not in the process list (%s)"
             % (GAME_PROCESS, st["method"]))
        game_note = "game closed"

    # ---- 1b: the save gates
    #
    # Before the backup, because a save this build will not write to is a save
    # nothing should be copied for. Read from the original rather than from the
    # backup for the same reason: there is no backup yet. `build_plan` raises
    # `SaveGate` for a `refuse` gate, so an expedition cannot reach here with a
    # plan at all; this is where the remaining gates are acted on.
    #
    # The version gate is **advisory by default** (DECISIONS.md, "A game update
    # must not turn the tool off"). Nothing was ever measured that says a new
    # save version moves the container layout -- 4670, 4734 and 4735 have
    # identical layouts, and the breaks anybody can name were Waypoint moving
    # the player state under `BaseContext`, the 3.60 compression change and the
    # `mf_` format going 2001 -> 2004, each of which this build already detects
    # by shape. So a version it has not seen is recorded and the apply
    # continues, under steps 4, 6 and 7, which test the actual file rather than
    # a number in it. `strict_version_check` puts the refusal back for an
    # operator who wants it.
    #
    # The expedition `refuse` gate is not covered by that flag, in either mode:
    # it is a measured structural difference (the live inventory may be the
    # season copy), not a version number.
    gated = SaveFile(save_path)
    gates = gated.gates()
    lo, hi = SUPPORTED_VERSIONS
    for g in gates:
        # A `note` gate is a caveat, not a reason to stop: "there is no mf_ to
        # confirm this is not an expedition" is said out loud here and the
        # apply runs (Q12).
        if g.get("level") == "note":
            r.info("save is one this build was verified on", g["message"])
        elif g.get("level") == "warn" and g.get("where") == "version"                 and not strict_version_check:
            r.info("save is one this build was verified on",
                   "this save reports version %s, outside the range this "
                   "build was verified on (%d to %d); continuing, because the "
                   "round-trip and nothing-else-changed checks run on this "
                   "file regardless" % (gated.version(), lo, hi))
        else:
            r.fail("save is one this build was verified on", g["message"])
    r.ok("save is one this build was verified on",
         "version %s, not an expedition" % (gated.version(),))
    del gated

    # ---- 2
    folder, copied = backup(save_path, backup_root, r)

    # ---- 2b: describe the backup immediately. Every failure from here on
    # leaves a folder a restore can verify, and one retention may prune.
    write_manifest(folder, save_path, copied, None, cfg,
                   outcome=OUTCOME_IN_PROGRESS,
                   expected_fingerprint=expected_fingerprint, game=game_note)
    progress = _Progress()
    try:
        return _apply_after_backup(save_path, cfg, expected_fingerprint,
                                   plan_builder, folder, copied, compress, r,
                                   progress, game=game_note)
    except Refused as exc:
        # The outcome is monotonic: nothing writes "refused" -- defined as a
        # run that stopped after the backup was taken and changed nothing --
        # over a marker that says the save has already moved. Six refusal
        # sites can only fire after step 9, and all six used to record the
        # opposite of what happened, with the plan row reset to null and the
        # folder left eligible for pruning (Q1).
        if progress.wrote_save:
            r.info("write", progress.after_write_sentence(folder))
        write_manifest(folder, save_path, copied, progress.plan, cfg,
                       outcome=progress.refused_outcome(), refusal=str(exc),
                       expected_fingerprint=expected_fingerprint,
                       game=game_note)
        if getattr(exc, "report", None) is None:
            exc.report = r
        raise


class _Progress(object):
    """How far the write got, so a refusal can say what is on disk.

    The manifest and the sentence both need the answer, and the answer cannot
    be derived from the exception: a refusal at step 11 and a refusal at step 8
    are the same `Refused` class and the same handler, and one of them means
    the operator's save has been replaced. So the sequence records its own
    progress as it goes, which is the only thing that knows.

    `plan` is kept for the same reason: the handler passed `plan=None`, so the
    `rows` and `containers` the step-9b manifest had recorded were reset to
    null and `[]` by the refusal that followed them.
    """

    def __init__(self):
        self.plan = None
        self.wrote_save = False
        self.wrote_meta = False

    def refused_outcome(self):
        return (OUTCOME_REFUSED_AFTER_WRITE if self.wrote_save
                else OUTCOME_REFUSED)

    def after_write_sentence(self, folder):
        """What the operator has to know, in the case where they have to act.

        Two shapes, because "the pair on disk disagrees" and "the pair agrees
        and a later check failed" need different repairs.
        """
        if not self.wrote_meta:
            return ("the metadata could not be updated after the save was "
                    "written; the save on disk is the new one and its mf_ is "
                    "the old one; restore this backup (%s) to put both back"
                    % folder)
        return ("a check failed after the save was written; the save on disk "
                "is the new one; restore this backup (%s) to put it back"
                % folder)


def _apply_after_backup(save_path, cfg, expected_fingerprint, plan_builder,
                        folder, copied, compress, r, progress=None,
                        game=None):
    """Steps 3 to 12. Split out so a refusal can be recorded in the manifest
    without wrapping the gates and the backup in their own handler."""
    from .savemodel import SaveFile

    progress = progress if progress is not None else _Progress()

    # ---- 3
    backup_save = os.path.join(folder, os.path.basename(save_path))
    r.ok("decode the backup", "working from %s, not from the original" % backup_save)

    # ---- 4
    r, payload, _doc, esc = verify_roundtrip(backup_save, r)
    before_doc = loads(payload)

    # ---- 5
    save = SaveFile(backup_save)
    plan, commit = plan_builder(save, cfg)
    # Step 7's map, built on the document the plan named its containers on.
    # `container_map()` and `extractor_containers()` used to be called after
    # `commit()`, so the guard's idea of which containers can be named depended
    # on state the transform had already changed (review one, R16).
    cmap = save.container_map()
    for c in save.extractor_containers():
        cmap.setdefault(c.key, c)
    document_paths = dict((k, _document_path(save, c)) for k, c in cmap.items())
    matched, compared = _fingerprint_matches(plan, expected_fingerprint)
    if not matched:
        r.fail("plan matches what you saw",
               "the plan computed from these bytes fingerprints %s, but you approved "
               "%s. Something changed between the dry run and now. Re-run the plan."
               % (_shown_digest(compared), _shown_digest(expected_fingerprint)))
    if not plan.rows:
        r.fail("plan matches what you saw", "the plan is empty; there is nothing to do")
    touched = commit()
    # From here on the manifest can describe the run rather than the digest
    # that was approved, whichever way this ends (Q1).
    plan.containers = touched
    progress.plan = plan
    r.ok("plan matches what you saw",
         "fingerprint %s, %d row(s), %d container(s) changed"
         % (plan.fingerprint, len(plan.rows), len(touched)))

    # ---- 6
    new_payload = dumps(save.doc, escape_slash=esc)
    reparsed = loads(new_payload)
    d = structural_diff(save.doc, reparsed)
    if d:
        r.fail("encode, decode, compare",
               "the transformed document does not survive its own serialisation; "
               "first difference at %s" % d[0])
    framed = frame_payload(new_payload, compress=compress)
    got, info = codec.read_payload_bytes(framed)
    if got != new_payload:
        r.fail("encode, decode, compare", "the framed file does not decode back to the "
                                          "payload we built")
    r.ok("encode, decode, compare",
         "%d payload bytes -> %d bytes on disk in %d block(s), decoded back identically"
         % (len(new_payload), len(framed), info["blocks"]))

    # ---- 7
    #
    # `obf` is each touched container's path spelled in the keys this document
    # actually uses. It used to be built by mapping every step of the plain
    # path through `save.d.rev`, which is right only for a save whose keys are
    # obfuscated: on a document written in the clear -- which the codec
    # accepts, `loads`/`dumps` round-trip, `Doc.key` navigates and
    # `codec.remap` produces -- the guard compared obfuscated paths against
    # plain ones and called all 31 changes strays, after the plan had been
    # built and committed (review one, R5).
    obf = []
    for key in touched:
        p = document_paths.get(key)
        if p:
            obf.append(p)
        else:
            r.fail("nothing else changed",
                   "the plan touched %r, which is not a container this build can "
                   "name a path for. Refusing rather than writing blind." % key)
    diffs = structural_diff(before_doc, reparsed, limit=DIFF_LIMIT)
    if len(diffs) >= DIFF_LIMIT:
        # The enumeration stops at the limit, so past it the list is not "every
        # differing path" any more and the test below stops meaning anything.
        # A guard that cannot see the whole change set has to say so.
        r.fail("nothing else changed",
               "the change set is larger than the guard can enumerate; refusing "
               "rather than writing blind")
    stray = []
    for path in diffs:
        if not any(path.startswith(o + "/") or path == o for o in obf):
            stray.append(path)
    if stray:
        r.fail("nothing else changed",
               "%d path(s) outside the containers this plan names differ, e.g. %s"
               % (len(stray), stray[0]))
    r.ok("nothing else changed",
         "%d changed path(s), every one of them inside: %s"
         % (len(diffs), ", ".join(touched) or "(none)"))

    # ---- 8
    #
    # Through `meta_path_for`, so the file that gets read here, and replaced at
    # step 10, is the one the filesystem has rather than the one this code can
    # spell: `mf_SAVE9.HG` beside a `save9.hg` was found, backed up under the
    # invented name and renamed by the write (Q9).
    meta_path = meta_path_for(save_path)
    check_metadata_updatable(meta_path, r)

    # ---- 9
    write_atomic(save_path, framed)
    # Before the report line and before the manifest: from this instruction on,
    # the operator's save is the new one, and every refusal from here has to
    # say so (Q1).
    progress.wrote_save = True
    r.ok("write", "%s, %d bytes, written to a temp file in the same directory and "
                  "renamed over the target" % (save_path, len(framed)))

    # ---- 9b: the one window in which the pair on disk disagrees. The save has
    # been replaced and its mf_ still records the old two sizes, so a kill here
    # leaves a save whose own metadata denies it (review one, R3). Rewriting a
    # 400-byte JSON file is cheap; knowing afterwards which of the two files
    # moved is not obtainable any other way.
    write_manifest(folder, save_path, copied, plan, cfg,
                   outcome=OUTCOME_META_PENDING,
                   expected_fingerprint=expected_fingerprint, game=game)

    # ---- 10
    if os.path.exists(meta_path):
        update_metadata(meta_path, len(new_payload), len(framed), r)
    else:
        r.info("metadata", "no mf_ file beside this save; nothing to update")
    # Either the mf_ now agrees with the save or there was none to disagree.
    progress.wrote_meta = True

    # ---- 11
    back_payload, back_info = read_payload(save_path)
    if back_payload != new_payload:
        r.fail("re-read from disk", "the file on disk does not decode to what we wrote")
    back_doc = loads(back_payload)
    d2 = structural_diff(reparsed, back_doc)
    if d2:
        r.fail("re-read from disk", "re-decoding the written file differs at %s" % d2[0])
    if os.path.exists(meta_path):
        m = meta_read(meta_path)
        if m["size_disk"] != os.path.getsize(save_path):
            r.fail("re-read from disk", "mf_ size_disk %d does not match the file's %d"
                   % (m["size_disk"], os.path.getsize(save_path)))
        if m["size_decompressed"] != len(back_payload):
            r.fail("re-read from disk", "mf_ size_decompressed %d does not match the "
                   "payload's %d" % (m["size_decompressed"], len(back_payload)))
    r.ok("re-read from disk",
         "%d bytes, %d block(s), decodes to exactly the document we built"
         % (os.path.getsize(save_path), back_info["blocks"]))

    # ---- 12
    #
    # `written` is taken here rather than at step 9b, because the pair is only
    # settled once step 10 has replaced the `mf_` too, and because a hash of
    # the save taken before step 11 would be a hash of a file nothing had
    # proved yet. It is what a later restore compares the save folder against,
    # so that pressing Undo after four hours of play is refused rather than
    # silently throwing the four hours away.
    written = {"save": written_row(save_path),
               "meta": written_row(meta_path)}
    manifest = write_manifest(folder, save_path, copied, plan, cfg,
                              outcome=OUTCOME_WRITTEN,
                              expected_fingerprint=expected_fingerprint,
                              written=written, game=game)
    r.ok("backup manifest",
         "%s describes the backup: %s, %d row(s), config %s"
         % (MANIFEST_NAME, manifest["save"]["file"] if manifest["save"] else "?",
            manifest["plan"]["rows"], manifest["config_sha256"][:8]))

    return r, {
        "backup": folder,
        "backup_files": copied,
        "manifest": manifest,
        "written": save_path,
        "size": os.path.getsize(save_path),
        "payload": len(new_payload),
        "rows": len(plan.rows),
        "fingerprint": plan.fingerprint,
        "fingerprint_full": getattr(plan, "fingerprint_full", None),
        "containers": touched,
    }
