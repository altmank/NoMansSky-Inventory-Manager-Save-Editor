"""The only module in this package allowed to branch on the operating system.

Everything else imports from here. That rule exists because Windows-only
assumptions leak quietly: `ctypes.windll` at module scope, `mbcs` on a byte
string, `creationflags` on a `subprocess` call, `%APPDATA%` in a join. Each of
those is an `AttributeError` or a `ValueError` on Ubuntu CI at import time, at
which point the codec, the planner and the server -- none of which care what
operating system they are on -- stop being testable there (GOAL.md §9.1, the
"Windows-only assumptions leak into shared code" row).

So this module holds five kinds of answer:

* **the process check** (`game_status`), which is the gate the whole write
  sequence hangs off, and which off Windows says "not known" without trying;
* **the refusal sentence** for a check that cannot run, because the reason
  differs between "the process list would not read" and "there is no process
  list to read here" (`refusal_sentence`);
* **the machine's paths**: `default_save_root`, `state_dir`, `runtime_dir`;
* **`pid_alive`**, for the apply lock;
* **the single-instance lock** (`acquire_single_instance`) and its other half
  `exclusive_server_class`, because "only one of me" is a kernel object on
  Windows and a lock file everywhere else, and the bind that must fail for a
  second copy needs a Windows-only socket option;
* **the folders this process holds open**: `release_cwd`, which is why the
  download folder can be deleted while the exe runs, and
  `sweep_stale_runtime_dirs`, which is the one-file extraction litter;
* **the frozen-build helpers**: `is_frozen`, `has_console`, `ensure_streams`
  and `message_box`, which exist because a windowed PyInstaller build has no
  stdout and no console, so an unhandled exception in it is completely silent.

## Why there is no Linux or macOS process check

Not an omission. Under Proton the game runs inside a Wine prefix, and a native
`ps` does not list `NMS.exe`: it lists the Proton wrapper. A check written
there would answer "not running" while the game is running, and the one thing
the write gate must never do is answer "closed" about a save the game has open.
A wrong "not running" costs a play session. So off Windows the answer is
`known: False` and apply refuses (DECISIONS.md, 2026-09-14, "Apply is
Windows-only"). Plan, browse and the codec work everywhere.

## The two-second cache

`game_status()` shells out to `tasklist` when the Toolhelp32 snapshot fails,
and the page polls `GET /api/game` every 15 seconds while `/api/bootstrap`,
`/api/health` and every plan ask as well. Without a cache one page refresh can
be four process enumerations. The window is two seconds: long enough that one
page load is one check, short enough that the operator quitting the game and
pressing Apply sees the new answer. Anything that must not be cached calls
`forget_game_status()` first; the write sequence does not, because two seconds
before a write is well inside the 60-second autosave race it is guarding
against anyway.
"""
import ctypes
import errno
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

#: the executable the write gate looks for. One name, not a pattern: this is
#: the Steam and GOG Windows build, and a pattern that matched more would make
#: the gate refuse on something that is not the game.
GAME_PROCESS = "NMS.exe"

#: how long a `game_status()` answer is reused. See the module docstring.
STATUS_CACHE_SECONDS = 2.0

#: `%APPDATA%\HelloGames\NMS`, the one root this program probes. There is no
#: second strategy: GOG and Microsoft Store layouts were never observed here,
#: and "picking silently is how the wrong account gets sorted" (GOAL.md §2.3).
SAVE_ROOT_PARTS = ("HelloGames", "NMS")

#: read by `is_windows()` **only under pytest**, so the suite can prove the
#: non-Windows answers on a Windows machine. Deliberately not honoured in a
#: normal run: a environment variable that can turn the write gate into "not
#: known" is a foot-gun, and "not known" is a refusal, so the worst a mistake
#: here can do is refuse a write. Set it to `linux` or `darwin`.
FAKE_PLATFORM_ENV = "NMS_SORTER_FAKE_PLATFORM"

#: the sentence apply refuses with when there is no process list to read.
#: Written out as one literal rather than composed, because
#: `docs/TROUBLESHOOTING.md` and `docs/SAFETY.md` quote it verbatim and
#: `tests/test_docs.py` greps this source for the quoted form (GOAL.md P5-9).
UNSUPPORTED_REFUSAL = (
    "the process check needs Windows, so it cannot be told whether NMS.exe is "
    "running; plan and browse work here, apply does not")

#: what `game_status()["error"]` says off Windows. The short form, because the
#: long form is `UNSUPPORTED_REFUSAL` and the API returns both.
UNSUPPORTED_ERROR = "the process check needs Windows"

#: `detect_saves()` has no root to probe off Windows, and says so rather than
#: reporting an empty `%APPDATA%`.
SAVE_ROOT_UNSUPPORTED = (
    "the default save folder is a Windows path, so there is nothing to look "
    "in here; pass --folder, or set the save folder on the Settings section")


# ---------------------------------------------------------------------------
# which operating system is this
# ---------------------------------------------------------------------------

def platform_name():
    """-> `sys.platform`, or the faked one under pytest.

    Read on every call, never cached, so a test may monkeypatch `sys.platform`
    for one assertion and have the next one see the real value.
    """
    if "pytest" in sys.modules:
        fake = os.environ.get(FAKE_PLATFORM_ENV)
        if fake:
            return fake.strip().lower()
    return sys.platform


def is_windows():
    return platform_name().startswith("win")


# ---------------------------------------------------------------------------
# 1. is the game running
# ---------------------------------------------------------------------------

TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260

#: `subprocess.CREATE_NO_WINDOW`, spelled out because the constant itself only
#: exists on Windows. Read in `_game_status_windows` and nowhere else, so that
#: passing it to `subprocess` -- which raises `ValueError` on POSIX -- can only
#: happen on a branch POSIX never reaches.
CREATE_NO_WINDOW = 0x08000000


class PROCESSENTRY32(ctypes.Structure):
    """The Toolhelp32 record. Defining it is portable; walking it is not."""
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_char * MAX_PATH)]


def _snapshot_processes():
    """[(exe name, pid)] from a Toolhelp32 snapshot. Windows only."""
    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        raise OSError("CreateToolhelp32Snapshot failed")
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        out = []
        ok = k32.Process32First(snap, ctypes.byref(entry))
        while ok:
            out.append((entry.szExeFile.decode("mbcs", "replace"), entry.th32ProcessID))
            ok = k32.Process32Next(snap, ctypes.byref(entry))
        return out
    finally:
        k32.CloseHandle(snap)


def _game_status_windows():
    """Toolhelp32, then `tasklist`, then "not known". Never raises.

    Two methods because the first one fails in real conditions: an elevated
    game process, a security product hooking `CreateToolhelp32Snapshot`, a
    32-bit interpreter on a 64-bit machine. `tasklist` is slower and shells
    out, which is why the answer is cached.
    """
    name = GAME_PROCESS.lower()
    try:
        procs = _snapshot_processes()
        pids = [pid for exe, pid in procs if exe.lower() == name]
        return {"running": bool(pids), "pids": pids, "method": "toolhelp32",
                "known": True, "process": GAME_PROCESS}
    except Exception as exc:
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", "IMAGENAME eq " + GAME_PROCESS, "/NH"],
                stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW)
            running = GAME_PROCESS.lower() in out.decode("mbcs", "replace").lower()
            return {"running": running, "pids": [], "method": "tasklist",
                    "known": True, "process": GAME_PROCESS}
        except Exception as exc2:
            return {"running": None, "pids": [], "method": "failed", "known": False,
                    "process": GAME_PROCESS,
                    "error": "%s / %s" % (exc, exc2)}


def _game_status_unsupported():
    """The off-Windows answer. Nothing is attempted: no `ps`, no `pgrep`, no
    `/proc` walk. See the module docstring for why a guess is worse than a
    refusal here."""
    return {"running": None, "known": False,
            "method": "unsupported: %s" % platform_name(),
            "pids": [], "process": GAME_PROCESS,
            "error": UNSUPPORTED_ERROR}


_status_lock = threading.Lock()
#: (monotonic time of the answer, the answer)
_status_cache = None


def _copy_status(st):
    """A caller that mutates what it got must not mutate the cache with it."""
    out = dict(st)
    out["pids"] = list(st.get("pids") or [])
    return out


def game_status(max_age=None):
    """-> {running, known, method, pids, process, error?}, cached briefly.

    `running` is a tri-state and the caller must treat it as one: `True` the
    game is up, `False` it is not, `None` we do not know. `known` is the same
    fact stated so it cannot be missed, because `if not st["running"]` reads
    as "it is closed" and would be wrong for `None`. A check that cannot run
    is a refusal, which is the oldest rule in this write path (GOAL.md §1.9).

    Pass `max_age=0` for an uncached answer.
    """
    global _status_cache
    if max_age is None:
        max_age = STATUS_CACHE_SECONDS
    now = time.monotonic()
    with _status_lock:
        cached = _status_cache
        if cached is not None and max_age > 0 and (now - cached[0]) < max_age:
            return _copy_status(cached[1])
    st = _game_status_windows() if is_windows() else _game_status_unsupported()
    with _status_lock:
        _status_cache = (time.monotonic(), _copy_status(st))
    return _copy_status(st)


def forget_game_status():
    """Drop the cached answer, so the next call really looks."""
    global _status_cache
    with _status_lock:
        _status_cache = None


def refusal_sentence(status):
    """The sentence apply refuses with when `status["known"]` is false.

    Two reasons, two sentences. "The process list would not read" is a Windows
    machine with something in the way, and names the error so the operator can
    act on it. "There is no process list here" is a platform fact and no error
    string would help. Both end in a refusal; only the wording differs, and
    the wording is the whole value of a refusal (GOAL.md §1.9).

    Both say what to do, and neither says "quit the game". A running game is
    not what apply refuses any more -- a running game in a loaded save is --
    and the thing a player can act on here is the process list itself, not the
    game.
    """
    status = status or {}
    if str(status.get("method") or "").startswith("unsupported"):
        return UNSUPPORTED_REFUSAL
    return ("could not read the process list (%s), so it cannot be told "
            "whether %s is running. Refusing rather than guessing: something "
            "on this machine is blocking the process list, and apply needs it."
            % (status.get("error"), GAME_PROCESS))


# ---------------------------------------------------------------------------
# the machine's paths
# ---------------------------------------------------------------------------

def default_save_root():
    """-> `%APPDATA%\\HelloGames\\NMS`, or None where that path means nothing.

    None rather than a POSIX-ish guess: `~/.steam/.../compatdata/275850/pfx/`
    has never been observed here, and a wrong guess sorts the wrong account.
    The caller offers the folder picker instead (GOAL.md §2.3).
    """
    if not is_windows():
        return None
    return os.path.join(os.environ.get("APPDATA", ""), *SAVE_ROOT_PARTS)


def state_dir():
    """Where the settings, the config, the backups and the logs live.

    Not next to the code: a wheel or a PyInstaller bundle may sit on a
    read-only path, and the state must survive a reinstall (GOAL.md §3.2).
    `%LOCALAPPDATA%` is read from the environment on every call rather than at
    import, so a test pointing it at a temp tree gets a temp state directory.

    `%LOCALAPPDATA%` wins wherever it is set, not only on Windows. Off Windows
    it is normally unset, so the answer there is `~/.local/share/NMS-Sorter`;
    but a test that points it at a temp tree must keep working under a faked
    platform, and a state directory that silently became the real `$HOME` in
    the middle of a test run is how a suite starts writing to the operator's
    own settings file.
    """
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        root = os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(root, "NMS-Sorter")


# ---------------------------------------------------------------------------
# the lock's pid check
# ---------------------------------------------------------------------------

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_INVALID_PARAMETER = 87


#: the two spellings of "no such process" this program can meet. `ESRCH` is
#: POSIX's; `EINVAL` is what Windows' own `os.kill(pid, 0)` raises for a pid
#: that never existed, which is the path taken when `platform_name()` is faked
#: on a Windows host (see `pid_alive`).
PID_GONE_ERRNOS = (errno.ESRCH, errno.EINVAL)


def pid_alive(pid):
    """Is this pid a live process?

    Errs towards "yes", which errs towards refusing to write: a lock whose
    owner might still be alive is treated as held. On Windows, "access denied"
    is a process we are not allowed to look at, which is still a process; only
    `ERROR_INVALID_PARAMETER` means there is no such pid. On POSIX the same
    reading applies to `errno`: `ESRCH` is gone, `EPERM` is alive and not ours,
    and anything else is not an answer at all, so it counts as alive.

    Branched on **capability** (`ctypes.windll`), not on `is_windows()`.
    `platform_name()` is fakeable under pytest (`NMS_SORTER_FAKE_PLATFORM=
    linux`) so the suite can prove the off-Windows answers on this machine --
    but faking the *name* does not give the process a POSIX `os.kill`. Windows'
    `os.kill(pid, 0)` raises `EINVAL` for a pid that never existed rather than
    `ESRCH`, and the old blanket `except OSError: return True` therefore made
    every dead pid look alive: under the fake platform no lock was ever stale,
    which is the one branch those tests exist to cover.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if hasattr(ctypes, "windll"):
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            k32.CloseHandle(handle)
            return True
        return k32.GetLastError() != ERROR_INVALID_PARAMETER
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        # The type, not just the errno: `ProcessLookupError()` raised without
        # arguments carries `errno is None`, and it still means "no such
        # process" -- it is the class CPython maps ESRCH onto.
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno in PID_GONE_ERRNOS:
            return False
        if exc.errno == errno.EPERM:
            return True
        # Not an answer: a pid that cannot be asked about keeps its lock,
        # because the alternative is writing over a live writer's save.
        return True
    return True


# ---------------------------------------------------------------------------
# telling a permission from a share lock
# ---------------------------------------------------------------------------

#: `CreateFileW` arguments and the one error code that has to be told apart
#: from `ERROR_ACCESS_DENIED`. The C runtime maps both of them to `EACCES`,
#: which is why `os.open` cannot answer the question below.
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004   # read, write, delete
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_SHARING_VIOLATION = 32


def write_is_busy(path):
    """Is a refused write to `path` somebody else's handle rather than this
    file's permissions? -> bool.

    `safety.write_atomic` asks the file whether it can be written before it
    writes anything, and the answer decides which of two sentences a player
    reads: "the save folder refused the write", which is theirs to fix, or
    "the game has the save open", which the main menu fixes. Windows returns
    `ERROR_ACCESS_DENIED` for the first and `ERROR_SHARING_VIOLATION` for the
    second and the C runtime maps both to `EACCES`, so neither `os.open` nor
    `os.access` can tell them apart; `CreateFileW` is asked directly.

    -> True also when the write-open succeeds here after failing through
    `os.open`, which is the same statement: whatever refused it was not this
    file's permissions.

    POSIX has no share mode. An open there never fails because another
    process has the file, so an `EACCES` is always a permission and the
    answer is False. Branched on **capability** (`ctypes.windll`) rather than
    on `is_windows()`, like `pid_alive`: `NMS_SORTER_FAKE_PLATFORM=linux` on a
    Windows host still has a real share mode to run into, and a real Windows
    handle is what the suite holds against the save to reach the sentence.
    """
    if not hasattr(ctypes, "windll"):
        return False
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = ctypes.c_void_p
    handle = k32.CreateFileW(ctypes.c_wchar_p(path), _GENERIC_WRITE,
                             _FILE_SHARE_ALL, None, _OPEN_EXISTING,
                             _FILE_ATTRIBUTE_NORMAL, None)
    if handle and handle != _INVALID_HANDLE_VALUE:
        k32.CloseHandle(ctypes.c_void_p(handle))
        return True
    return k32.GetLastError() == ERROR_SHARING_VIOLATION


# ---------------------------------------------------------------------------
# the frozen build
# ---------------------------------------------------------------------------

def is_frozen():
    """Is this a PyInstaller bundle rather than a source checkout?"""
    return bool(getattr(sys, "frozen", False))


def bundle_dir():
    """Where the bundled data was unpacked (`sys._MEIPASS`), or the package
    directory in a source run.

    `importlib.resources` is what actually reads `data/*.json` (see
    `itemdb._read_data`) and PyInstaller's importer implements the resource
    reader, so this is only needed by the one place that still wants a real
    filesystem path: `server.STATIC`, which serves files by path and length.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return os.path.join(meipass, "nms_sorter")
    return os.path.dirname(os.path.abspath(__file__))


def exe_dir():
    """The folder the executable sits in, or None in a source run. This is
    where a `--console` log or a crash note would go next to the exe."""
    if not is_frozen():
        return None
    return os.path.dirname(os.path.abspath(sys.executable))


def has_console():
    """Is there a console window to print to?

    `NMS-Sorter-console.exe` is built with `console=True` and has one;
    `NMS-Sorter.exe` is built with `console=False` and does not, so every
    `print()` in it goes nowhere and an unhandled traceback is invisible.
    Telling those two apart is the entire job.

    A *source* run can answer False too -- a pythonw, a service, or a plain
    `python` whose output is a pipe with no console window attached -- which is
    why the only caller pairs this with `is_frozen()`. On its own it does not
    mean "nowhere to print": a piped source run still has a real `sys.stderr`,
    and a traceback there is the right answer.
    """
    if not is_windows():
        return True
    try:
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        return True


def streams_visible():
    """Can anything written to stdout or stderr be seen by anybody? -> bool.

    This, not `has_console()`, is what the two frozen-build decisions turn on,
    and the difference was measured rather than reasoned about.
    `GetConsoleWindow()` returns 0 for `NMS-Sorter-console.exe` when it is
    started from a pseudo-console or with its output redirected -- there is no
    console *window*, but stdout is a perfectly good pipe and everything
    printed to it is visible. Using the window as the test sent the console
    build's own banner to the log file instead of to the redirect it was asked
    for.

    A real file descriptor is the honest test. PyInstaller's windowed build
    substitutes a writer that discards and has no `fileno`, so it answers
    False; a pipe, a file and a console all answer True.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            fd = stream.fileno()
        except Exception:
            continue
        if isinstance(fd, int) and fd >= 0:
            return True
    return False


class _LogStream(object):
    """A `sys.stdout` substitute that forwards whole lines to a logger.

    In a windowed build the startup banner -- which save folder, which config,
    which backup folder, which mode, and the `PROBLEM` line -- is written with
    `print()` and goes nowhere. It is also the first thing an issue asks for.
    So it goes to the log file instead of being discarded.

    Lines are buffered until a newline so that one `print()` is one log
    record, and `write` is re-entrancy guarded because `--log -` installs a
    `StreamHandler(sys.stderr)`: if that ever ends up pointed back at one of
    these, a single log call would otherwise recurse until the stack ran out.
    """

    def __init__(self, logger, level=20):
        self._logger, self._level, self._buf = logger, level, ""
        self._inside = False

    def write(self, s):
        if not isinstance(s, str):
            s = str(s)
        self._buf += s
        if self._inside:
            return len(s)
        self._inside = True
        try:
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    self._logger.log(self._level, "%s", line.rstrip())
        finally:
            self._inside = False
        return len(s)

    def flush(self):
        if self._buf.strip() and not self._inside:
            self._inside = True
            try:
                self._logger.log(self._level, "%s", self._buf.rstrip())
            finally:
                self._inside = False
        self._buf = ""

    def isatty(self):
        return False

    def writable(self):
        return True


def ensure_streams(logger=None, force=None):
    """Point `sys.stdout` at the log when there is nowhere else. -> names replaced.

    Called first thing by the packaged entry point. Two conditions, and the
    second one was found by running the build rather than by reading about it:

    * `sys.stdout is None`. The documented state of a windowed build, and the
      one that makes the first `print()` raise `AttributeError` in a process
      with no console -- a completely silent exit on a double click.
    * **frozen, with nothing visible to write to** (`streams_visible()` is
      false). PyInstaller 6 does not leave `sys.stdout` as None; it substitutes
      a writer that discards. Nothing raises, and the whole banner vanishes.
      Measured on `NMS-Sorter.exe`: started detached with no handles, the log
      held the `serving` line from `log.info` and not one line of the banner.
      The test is a usable file descriptor and not `has_console()`, because the
      *console* build has no console window when its output is redirected and
      must still write to the redirect.

    `sys.stderr` is replaced only when it is None, never on the `force` path.
    `--log -` builds a `StreamHandler(sys.stderr)`, and pointing that at a
    stream that logs would be a loop; a traceback in the windowed build is
    handled by `cli.report_silent_failure`, which writes to the log directly.
    """
    import logging
    if logger is None:
        logger = logging.getLogger("nms_sorter")
    if force is None:
        force = is_frozen() and not streams_visible()
    replaced = []
    if getattr(sys, "stdout", None) is None or force:
        sys.stdout = _LogStream(logger, logging.INFO)
        replaced.append("stdout")
    if getattr(sys, "stderr", None) is None:
        sys.stderr = _LogStream(logger, logging.WARNING)
        replaced.append("stderr")
    return replaced


#: MB_OK | MB_ICONERROR | MB_SETFOREGROUND
_MB_ERROR = 0x00000010 | 0x00010000


def message_box(title, text):
    """Show one modal Windows message box. -> was it shown.

    The only user-visible output a windowed build has left when it cannot
    start. Windows only, wrapped in a `try`, and never called from a path that
    depends on its return value: a failure to show a dialog must not become the
    reason the process dies differently.
    """
    if not is_windows():
        return False
    try:
        ctypes.windll.user32.MessageBoxW(None, str(text), str(title), _MB_ERROR)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# only one of me: the single-instance lock
# ---------------------------------------------------------------------------
#
# Three fast double-clicks on `NMS-Sorter.exe` produced three servers
# `LISTENING` on 127.0.0.1:8765 (player-day review, W3). Two things were wrong
# and both had to be fixed, because either one alone still loses:
#
# * the "is a sorter already answering?" probe runs *before* the bind, so
#   three processes that probe within the same millisecond all see a free
#   port. No amount of probing fixes that -- it is a race by construction.
# * Windows' `SO_REUSEADDR`, which `socketserver` sets by default
#   (`allow_reuse_address = 1`), lets a second process bind a port another
#   process is already *listening* on. On Linux that bind fails; on Windows it
#   succeeds, the two servers then share arriving connections unpredictably,
#   and the loser of the race is never told it lost.
#
# `acquire_single_instance` closes the race with a kernel object taken before
# the probe; `exclusive_server_class` makes the bind itself refuse. The lock is
# the authority -- it is what the second process reports on -- and the bind is
# the backstop that also catches a sorter from a build predating the lock.

#: the name of the mutex, and the stem of the lock file. Per **port**, not per
#: machine: `--port 9000` is a deliberate second sorter on a second port and
#: must not be refused, while two on one port are the bug.
#:
#: The port alone, not the host and port. One consequence, accepted
#: deliberately: a sorter on `127.0.0.1:8765` now refuses a second one on
#: `192.168.1.9:8765`, which the bind alone would have allowed. That is the
#: right way round -- the two would share one save folder, one config file and
#: one apply lock, which is the failure the whole check exists to prevent --
#: and `--host` anything but loopback is already the flag the help text tells
#: the operator not to use.
INSTANCE_NAME = "NMS-Sorter-%d"

#: `Local\` is the per-session namespace. Not `Global\`: that one needs a
#: privilege on some configurations, and it would also make two *different*
#: users on one machine fight over one sorter, which is not the case being
#: prevented here.
INSTANCE_MUTEX_PREFIX = "Local\\"

ERROR_ALREADY_EXISTS = 183


class InstanceLock(object):
    """"I am the one sorter on this port", held for the life of the process.

    `acquired` is the whole answer; everything else is for the log line and
    for the tests. `release()` is idempotent, and a lock that was never
    acquired releases to nothing -- so a caller can hold one variable and
    always call `release()` on the way out.

    A Windows mutex handle is closed by the kernel when the process dies, by
    any route including Task Manager, so there is no such thing as a stale
    one. A lock *file* has no such guarantee, which is why it carries a pid
    and a dead owner's file is taken over rather than obeyed.
    """

    def __init__(self, port, acquired, kind, name, handle=None, path=None,
                 fd=None, owner_pid=None, error=None):
        # `name` is what the log line calls the lock -- a mutex name on
        # Windows, the lock file's path off it -- and `path` is only set when
        # there is really a file, because `path` is what `release()` unlinks.
        self.port = port
        self.acquired = bool(acquired)
        self.kind = kind
        self.name = name
        self.handle = handle
        self.path = path
        self.fd = fd
        self.owner_pid = owner_pid
        self.error = error
        self._released = False

    def release(self):
        """Give the lock up. Never raises: this runs on the way out, and a
        failure to tidy up must not become the reason the exit code changes."""
        if self._released:
            return False
        self._released = True
        if not self.acquired:
            return False
        if self.handle is not None:
            try:
                ctypes.windll.kernel32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None
            return True
        if self.fd is not None:
            fcntl = sys.modules.get("fcntl")
            if fcntl is not None:
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
                except Exception:
                    pass
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None
        if self.path:
            try:
                os.remove(self.path)
            except OSError:
                pass
        return True

    def __repr__(self):
        return "<InstanceLock %s %s acquired=%s>" % (
            self.kind, self.name, self.acquired)


def instance_lock_path(port):
    """Where the off-Windows lock file goes.

    Beside the settings, not in `/tmp`: `state_dir()` is per-user and is
    already the one directory this program is allowed to write to, while a
    lock in a world-writable `/tmp` is one another user can hold against you.
    """
    return os.path.join(state_dir(), INSTANCE_NAME % int(port) + ".lock")


def _acquire_mutex(port):
    """Windows: `CreateMutexW`, and `ERROR_ALREADY_EXISTS` is the answer.

    The handle is kept, never waited on -- this is a name that either exists
    or does not, so there is no abandoned-mutex case to reason about.
    `CreateMutexW` hands back a handle either way; the loser closes its own
    and reports.
    """
    name = INSTANCE_MUTEX_PREFIX + (INSTANCE_NAME % int(port))
    k32 = ctypes.windll.kernel32
    k32.SetLastError(0)
    handle = k32.CreateMutexW(None, False, name)
    err = k32.GetLastError()
    if not handle:
        # The object cannot be created at all. Fail *open*: refusing to start
        # because a mutex could not be made would turn a locked-down machine
        # into "double-clicking does nothing", which is the worst failure this
        # program has. The exclusive bind still catches a real duplicate.
        return InstanceLock(port, True, "mutex-unavailable", name,
                            error="CreateMutexW failed (%d)" % err)
    if err == ERROR_ALREADY_EXISTS:
        k32.CloseHandle(handle)
        return InstanceLock(port, False, "mutex", name)
    return InstanceLock(port, True, "mutex", name, handle=handle)


def _read_pid(path):
    try:
        with open(path, "r", encoding="ascii", errors="replace") as fh:
            return int((fh.read(32) or "").strip() or 0) or None
    except (OSError, ValueError):
        return None


def _acquire_lockfile(port):
    """Off Windows: `flock` where there is one, `O_EXCL` plus a pid where not.

    Both legs have to exist and both have to be importable *on Windows*,
    because that is where this suite runs: `NMS_SORTER_FAKE_PLATFORM=linux`
    takes this branch on a machine with no `fcntl`, which is exactly the
    fallback's own test. `fcntl` is imported here rather than at module scope
    for the reason the rest of this module branches late -- an `ImportError`
    at import time takes the whole program down with it.

    The `O_EXCL` leg needs the pid check because nothing releases the file
    when the owner is killed. `pid_alive` errs towards "alive", so the worst a
    mistake there makes is a second copy that opens the first one's page.
    """
    path = instance_lock_path(port)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError as exc:
        return InstanceLock(port, True, "lockfile-unavailable", path,
                            path=path, error=str(exc))
    try:
        import fcntl
    except ImportError:
        fcntl = None

    if fcntl is not None:
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            return InstanceLock(port, True, "lockfile-unavailable", path,
                                path=path, error=str(exc))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            owner = _read_pid(path)
            os.close(fd)
            return InstanceLock(port, False, "flock", path, path=path,
                                owner_pid=owner)
        try:
            os.ftruncate(fd, 0)
            os.write(fd, ("%d\n" % os.getpid()).encode("ascii"))
        except OSError:
            pass
        return InstanceLock(port, True, "flock", path, path=path, fd=fd)

    for attempt in (0, 1):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            owner = _read_pid(path)
            if attempt == 0 and (owner is None or not pid_alive(owner)):
                # The owner is gone and took no handle with it. Take the file
                # over rather than telling the operator that a dead process
                # owns their port -- which, in a build with no console, reads
                # as "double-clicking does nothing".
                try:
                    os.remove(path)
                except OSError:
                    return InstanceLock(port, False, "pidfile", path,
                                        path=path, owner_pid=owner)
                continue
            return InstanceLock(port, False, "pidfile", path, path=path,
                                owner_pid=owner)
        except OSError as exc:
            return InstanceLock(port, True, "lockfile-unavailable", path,
                                path=path, error=str(exc))
        try:
            os.write(fd, ("%d\n" % os.getpid()).encode("ascii"))
        except OSError:
            pass
        return InstanceLock(port, True, "pidfile", path, path=path, fd=fd)
    return InstanceLock(port, False, "pidfile", path, path=path)


def acquire_single_instance(port):
    """-> an `InstanceLock` for this port. `acquired` false means: somebody else.

    Branched on `is_windows()` and not on `hasattr(ctypes, "windll")` -- the
    opposite of `pid_alive`, on purpose. Faking the platform name does not
    give this process a POSIX `os.kill`, which is why `pid_alive` asks about
    the capability instead; but the lock *file* leg works perfectly well on
    Windows, so branching on the name is what lets
    `NMS_SORTER_FAKE_PLATFORM=linux` prove the POSIX path on the machine the
    suite actually runs on.

    Every failure that is not "somebody else has it" returns `acquired=True`
    with a `kind` ending in `-unavailable`. A lock that cannot be taken must
    not be able to stop the program from starting: the exclusive bind is still
    there, and a sorter that refuses to run because of its own lock is a worse
    bug than the one being fixed.
    """
    port = int(port)
    if is_windows() and hasattr(ctypes, "windll"):
        return _acquire_mutex(port)
    return _acquire_lockfile(port)


# ---------------------------------------------------------------------------
# the bind a second copy cannot win
# ---------------------------------------------------------------------------

def exclusive_server_class(base):
    """-> a subclass of `base` whose bind fails if anything else is listening.

    Windows' `SO_REUSEADDR` means "bind even if somebody is already listening
    here", which is not what it means on POSIX and is not what `socketserver`
    intends when it sets `allow_reuse_address = 1`. `SO_EXCLUSIVEADDRUSE` is
    the Windows-only opposite: it makes the *bind* fail rather than silently
    creating a second listener on one port.

    Off Windows the option does not exist and `allow_reuse_address = False` is
    the whole fix, because `bind` there already refuses an address in use. The
    price is the `TIME_WAIT` restart: for up to a minute or so after a stop, a
    restart on the same port can be refused. That was weighed and accepted --
    this server holds no long-lived outbound connections, the caller already
    tries the next ten ports and says which one it used, and the failure being
    prevented is two sorters writing one save folder.

    A subclass, rather than a flag set on the base class, because the base is
    `http.server.ThreadingHTTPServer`: a standard library class that anything
    else in this interpreter may also be using.
    """
    opt = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)

    class ExclusiveServer(base):
        allow_reuse_address = False
        allow_reuse_port = False

        def server_bind(self):
            if opt is not None:
                try:
                    self.socket.setsockopt(socket.SOL_SOCKET, opt, 1)
                except OSError:
                    # An option the stack refuses is not a reason not to
                    # serve; `allow_reuse_address = False` above still stands.
                    pass
            return base.server_bind(self)

    ExclusiveServer.__name__ = "Exclusive" + getattr(base, "__name__", "Server")
    ExclusiveServer.__qualname__ = ExclusiveServer.__name__
    return ExclusiveServer


# ---------------------------------------------------------------------------
# the folders this process holds open
# ---------------------------------------------------------------------------

def release_cwd(force=None):
    """Stop holding the exe's own folder open. -> the new directory, or None.

    Nothing in this program calls `os.chdir` and nothing resolves a relative
    path, but a double-clicked executable inherits Explorer's idea of a
    working directory: the folder the exe is in. Windows will not delete a
    directory that is some process's cwd, so "delete the exe and that folder"
    -- which is the whole of the uninstall instructions -- failed on the
    folder while the sorter ran, and left a server answering 8765 with its own
    executable already gone (player-day review, W2).

    So the *frozen* build moves out of that folder at startup: to
    `state_dir()`, which it is already writing to and which no uninstall
    deletes, or to the temp directory if that cannot be made. A source run is
    left exactly where it is -- `python -m nms_sorter` runs in a shell whose
    directory belongs to the operator, `packaging/start.bat` deliberately runs
    from the repository root, and moving either silently would be a surprise
    with no upside, because there is no exe folder to release.

    **What this does not fix, measured rather than assumed.** A one-file build
    is *two* processes: the PyInstaller bootloader, and the Python child it
    spawns. This function runs in the child, and `psutil` confirms the split
    on the built exe -- child `cwd` becomes `%LOCALAPPDATA%\\NMS-Sorter` while
    the bootloader's stays on the folder it was launched from, and an emptied
    download folder therefore still fails to `rmdir` while the sorter runs
    (it succeeds when the same exe is started from anywhere else, which is
    what identifies the bootloader as the remaining holder). Nothing in Python
    can change a parent process's working directory. What the move does buy is
    everything downstream of the child: no relative path can resolve into the
    download folder, and the folder's *files* can be moved or deleted, so the
    uninstall gets as far as an empty directory. Finishing it needs the sorter
    stopped first, which is the sentence `docs/GUIDE.md` has to carry.
    """
    if force is None:
        force = is_frozen()
    if not force:
        return None
    here = exe_dir()
    try:
        cwd = os.getcwd()
    except OSError:
        # The cwd has already been deleted from under us. Moving is then not
        # an optimisation; it is the only way `os.path.abspath` keeps working.
        cwd = None
    if here is not None and cwd is not None:
        try:
            same = os.path.samefile(cwd, here)
        except OSError:
            same = (os.path.normcase(os.path.abspath(cwd))
                    == os.path.normcase(os.path.abspath(here)))
        if not same:
            return None
    for candidate in (state_dir(), tempfile.gettempdir()):
        try:
            os.makedirs(candidate, exist_ok=True)
            os.chdir(candidate)
            return candidate
        except OSError:
            continue
    return None


def runtime_dir():
    """`%LOCALAPPDATA%\\NMS-Sorter\\runtime`: where the one-file bundle unpacks.

    The spec hands that path to `runtime_tmpdir` as a literal string with the
    environment variable still in it, and the Windows bootloader expands it
    itself -- measured, and the reason the spec only sets it on Windows, since
    PyInstaller documents that the POSIX bootloader does no expansion at all.
    Naming the same directory here is what lets the sweep look in the new
    place as well as the old one.
    """
    return os.path.join(state_dir(), "runtime")


#: the prefix PyInstaller gives its one-file extraction folders.
RUNTIME_DIR_PREFIX = "_MEI"

#: proof that a `_MEI*` folder is **ours**. `%TEMP%` is shared with every other
#: one-file PyInstaller program on the machine, and their abandoned folders are
#: not this program's business to delete. Every bundle built from
#: `packaging/NMS-Sorter.spec` unpacks `nms_sorter/data`, `nms_sorter/static`
#: and `nms_sorter/docs`, so this one subdirectory is both necessary and
#: sufficient evidence of ownership.
RUNTIME_MARKER = "nms_sorter"

#: the name of the stamp a running instance leaves in its own extraction
#: folder, and the whole reason this module now has a liveness test at all.
#:
#: **The incident.** An instance that started at 12:21 was still serving at
#: 16:58 when its owner double-clicked the exe again. The second copy ran the
#: sweep *before* it found out it had lost the single-instance lock; the live
#: folder was by then five hours old, and the rename probe -- which this module
#: claimed was "the strongest available evidence that nobody is home" --
#: succeeded. The sweep renamed the running server's own bundle out from under
#: it and emptied it, and the live server started answering 404 JSON at `/`
#: because its `static/` and `docs/` no longer existed.
#:
#: **Why the probe was worthless.** Windows refuses to rename a directory that
#: is a process's *current directory*; it does not refuse to rename one merely
#: because files inside it are open or memory-mapped. The earlier measurement
#: that suggested otherwise was of a folder that happened to be the
#: bootloader's cwd, which is a different fact about a different handle. The
#: probe therefore never tested what it was documented to test.
#:
#: The fix is evidence rather than inference: the owner writes down who it is.
OWNER_FILE = "owner.pid"

#: how old a `_MEI*` folder with **no owner stamp** must be before the sweep
#: will consider it. An hour, and it is now only one of three conditions --
#: age is what is left when there is nothing better, and after the incident
#: above it is never again the deciding one on its own.
RUNTIME_STALE_SECONDS = 3600.0

#: the process name an older build's abandoned folder has to be weighed
#: against. Not `GAME_PROCESS`: this one is us.
SORTER_PROCESS = "NMS-Sorter.exe"


def _own_runtime_dirs():
    """This process's own extraction folder and the exe's folder, neither of
    which may ever be a sweep candidate."""
    out = set()
    for path in (getattr(sys, "_MEIPASS", None), exe_dir()):
        if path:
            out.add(os.path.normcase(os.path.abspath(path)))
    return out


def process_start_time(pid):
    """-> the unix time a pid was created, or None if that cannot be read.

    Windows only, via `GetProcessTimes`, and it exists for exactly one
    purpose: a pid on its own is not an identity. Pids are reused, and a
    recycled one would make a dead owner's folder look alive for ever.
    Comparing the creation time against what the owner wrote down tells the
    two apart. `None` is "no answer", and every caller treats no answer as
    "assume it is the owner", which errs towards keeping a folder.
    """
    if not hasattr(ctypes, "windll"):
        return None
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created = ctypes.c_ulonglong()
        exited = ctypes.c_ulonglong()
        kernel = ctypes.c_ulonglong()
        user = ctypes.c_ulonglong()
        ok = k32.GetProcessTimes(handle, ctypes.byref(created),
                                 ctypes.byref(exited), ctypes.byref(kernel),
                                 ctypes.byref(user))
        if not ok or not created.value:
            return None
        # FILETIME: 100-nanosecond ticks since 1601-01-01.
        return created.value / 10000000.0 - 11644473600.0
    except Exception:
        return None
    finally:
        k32.CloseHandle(handle)


def write_runtime_owner(folder=None, now=None):
    """Stamp an extraction folder with the pid that is using it. -> the path.

    Called once, as early in startup as there is anything to call it from, by
    every frozen run -- the one that goes on to serve and the ones that find
    out they have lost the lock and exit. Each has its own extraction folder
    and each protects its own for as long as it lives.

    Two fields, and the second one matters: `pid` says who, and `started` says
    *which* process of that number, so that a recycled pid cannot keep an
    abandoned folder alive for ever (see `process_start_time`). Written with a
    plain `write` and no fsync: losing this file to a power cut costs one
    folder's worth of litter, and the rest of the startup path must not wait
    for a disk.

    A no-op outside a one-file bundle, where there is no folder to stamp.
    """
    folder = folder or getattr(sys, "_MEIPASS", None)
    if not folder or not os.path.isdir(folder):
        return None
    started = process_start_time(os.getpid())
    if started is None:
        started = time.time() if now is None else now
    path = os.path.join(folder, OWNER_FILE)
    try:
        with open(path, "w", encoding="ascii") as fh:
            fh.write("%d %.3f\n" % (os.getpid(), started))
    except OSError:
        return None
    return path


def read_runtime_owner(folder):
    """-> {"pid": int, "started": float} from a folder's stamp, or None."""
    try:
        with open(os.path.join(folder, OWNER_FILE), encoding="ascii",
                  errors="replace") as fh:
            parts = (fh.read(128) or "").split()
    except OSError:
        return None
    if not parts:
        return None
    try:
        pid = int(parts[0])
    except ValueError:
        return None
    try:
        started = float(parts[1]) if len(parts) > 1 else None
    except ValueError:
        started = None
    return {"pid": pid, "started": started}


#: how far apart a recorded start time and `GetProcessTimes` may be and still
#: be the same process. Two seconds: the stamp is written by the process
#: itself, within milliseconds of the number it wrote, and the only reason for
#: any slack at all is that the fallback path records `time.time()` instead.
OWNER_START_SLACK = 2.0


def runtime_owner_state(folder):
    """Is somebody using this extraction folder? -> (state, why).

    `state` is one of:

    * `"live"`   -- a stamp naming a process that is running. Hands off.
    * `"dead"`   -- a stamp naming a process that is gone. Safe to remove,
                    with no age test and no probe: the owner said who it was
                    and that owner no longer exists, which is the only direct
                    evidence of abandonment this program can get.
    * `"unknown"` -- no stamp at all. An older build's folder, or one this
                    program's own broken sweep already emptied; it falls
                    through to the conservative rules.

    `pid_alive` errs towards "alive", and so does everything here.
    """
    owner = read_runtime_owner(folder)
    if owner is None:
        return "unknown", "no %s" % OWNER_FILE
    pid = owner["pid"]
    if not pid_alive(pid):
        return "dead", "owner pid %d is gone" % pid
    started = owner.get("started")
    actual = process_start_time(pid)
    if started is not None and actual is not None \
            and abs(actual - started) > OWNER_START_SLACK:
        # The number is in use by a different process than the one that wrote
        # it: a recycled pid, not the owner.
        return "dead", ("pid %d is alive but started %.0f s from the stamp, so"
                        " it is a different process" % (pid, actual - started))
    return "live", "owner pid %d is running" % pid


def sorters_running(exclude=None, name=SORTER_PROCESS):
    """-> (how many other `NMS-Sorter.exe` processes, a sentence), or (None, …).

    `None` is "there is no process list to read here", which every caller
    treats as "one might be running". The list is read the same two ways the
    game check reads it -- a Toolhelp32 snapshot, then `tasklist` -- because
    the same things break the first one, and this answer decides whether an
    unstamped folder is left alone.

    A one-file build is two processes, the bootloader and its Python child, so
    this process's own pid *and* its parent's are excluded by default.
    """
    if exclude is None:
        exclude = set()
        exclude.add(os.getpid())
        try:
            exclude.add(os.getppid())
        except (AttributeError, OSError):
            pass
    exclude = set(int(p) for p in exclude)
    wanted = name.lower()
    if hasattr(ctypes, "windll"):
        try:
            found = [pid for exe, pid in _snapshot_processes()
                     if exe.lower() == wanted and pid not in exclude]
            return len(found), ("%d other %s process(es): %s"
                                % (len(found), name, found) if found
                                else "no other %s process" % name)
        except Exception:
            pass
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", "IMAGENAME eq " + name, "/NH"],
                stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW)
            text = out.decode("mbcs", "replace")
            rows = [r for r in text.splitlines() if wanted in r.lower()]
            found = []
            for row in rows:
                parts = row.split()
                for part in parts[1:]:
                    if part.isdigit():
                        if int(part) not in exclude:
                            found.append(int(part))
                        break
            return len(found), ("tasklist: %d other %s process(es)"
                                % (len(found), name))
        except Exception as exc:
            return None, "the process list would not read (%s)" % exc
    return None, "there is no process list to read on %s" % platform_name()


def _renamable(path):
    """Can this directory be renamed, and put straight back? -> bool.

    The last filter on an unstamped folder, and it is worth being precise
    about what it does and does not prove. It catches the folder whose *own*
    process has it as a current directory, and it catches one on a filesystem
    that will not let this user move it. It does **not** prove that nobody is
    using the folder -- Windows renames a directory quite happily while the
    DLLs inside it are mapped, which is how the live instance in this module's
    incident note came to be emptied.

    The rename is undone before anything is decided, so the live path never
    sees a `.stale-*` name and a probe can never be the thing that breaks a
    running server. If putting it back fails, that is reported as "do not
    touch" *and* the folder is left under the probe name, which is the one
    case where this leaves a mess -- and it is still better than deleting.
    """
    staged = "%s.probe-%d" % (path, os.getpid())
    try:
        os.rename(path, staged)
    except OSError:
        return False
    try:
        os.rename(staged, path)
    except OSError:
        return False
    return True


def sweep_stale_runtime_dirs(roots=None, older_than=RUNTIME_STALE_SECONDS,
                             now=None, marker=RUNTIME_MARKER, log=None,
                             others=None):
    """Delete abandoned `_MEI*` extraction folders of **this** program.

    -> [the paths removed]. Every folder that is kept is logged with the
    reason, because a sweep that deletes the wrong thing is invisible until
    something 404s, and the reasons are what makes the decision reviewable.

    A one-file build unpacks ~23 MB per run and removes it only on a graceful
    exit; before the stop button existed there was no graceful exit, so a
    player's `%TEMP%` held 15 folders and 344 MB (player-day review, W4). The
    exe now extracts to `runtime_dir()` instead, but the old litter is still
    sitting on the machine of everybody who ran an earlier build, and nothing
    else is ever going to remove it.

    **The ladder, in order.** A folder is removed only when one of these two
    reaches the bottom:

    1. it carries an `owner.pid` naming a process that no longer exists --
       direct evidence, no age test, no probe; or
    2. it carries no stamp at all (an older build's, or one an earlier version
       of this function already emptied), **and** no other `NMS-Sorter.exe` is
       running anywhere on the machine, **and** it is older than `older_than`,
       **and** it can be renamed and put back.

    Condition 2's first clause is the one that would have prevented the
    incident: an unstamped folder plus a running sorter is exactly the case
    where guessing costs somebody their session, and there is nothing to gain
    by guessing -- the folder will still be there next time, when nothing is
    running.

    This must only be called by the instance that holds the single-instance
    lock and is about to serve. A copy that is about to exit has no business
    tidying up, and in the incident it was the copy about to exit that did the
    damage; `cli.start_runtime_sweep` is where that is enforced.

    Never raises, whatever the filesystem does: this is housekeeping, and
    housekeeping that can stop the program starting is worse than litter.
    """
    if roots is None:
        roots = [runtime_dir(), tempfile.gettempdir()]
    now = time.time() if now is None else now
    mine = _own_runtime_dirs()
    removed = []
    seen = set()
    others_count, others_why = (None, "not asked")
    asked_others = False

    def keep(path, why):
        if log is not None:
            try:
                log.info("kept the extraction folder %s: %s", path, why)
            except Exception:
                pass

    for root in roots:
        if not root:
            continue
        key = os.path.normcase(os.path.abspath(root))
        if key in seen:
            continue
        seen.add(key)
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            if not name.startswith(RUNTIME_DIR_PREFIX):
                continue
            path = os.path.join(root, name)
            if os.path.normcase(os.path.abspath(path)) in mine:
                # Logged rather than passed over in silence, and it is the one
                # line in this function worth the disk: the folder the incident
                # destroyed was this one, and "the sweep ran and recognised its
                # own bundle" is otherwise indistinguishable in a log from "the
                # sweep never looked at it".
                keep(path, "this process is running out of it")
                continue
            try:
                if not os.path.isdir(path):
                    continue
            except OSError:
                continue
            # Ours? Either the bundle's own subdirectory is in it, or the name
            # is one only this program's own earlier sweep produced.
            try:
                ours = (not marker
                        or os.path.exists(os.path.join(path, marker))
                        or ".stale-" in name or ".probe-" in name)
            except OSError:
                continue
            if not ours:
                keep(path, "no %s inside it, so it belongs to some other "
                           "one-file application" % marker)
                continue

            state, why = runtime_owner_state(path)
            if state == "live":
                keep(path, why)
                continue
            if state == "dead":
                if _remove_tree(path):
                    removed.append(path)
                else:
                    keep(path, "%s, but it could not be deleted" % why)
                continue

            # No stamp. An older build's folder, and nothing in it says who
            # was using it, so the only safe question is whether anybody could
            # be.
            if not asked_others:
                others_count, others_why = (others() if others is not None
                                            else sorters_running())
                asked_others = True
            if others_count is None or others_count > 0:
                keep(path, "%s and it carries no %s, so it cannot be told "
                           "apart from a running instance's own folder"
                           % (others_why, OWNER_FILE))
                continue
            try:
                age = now - os.path.getmtime(path)
            except OSError:
                continue
            if age < older_than:
                keep(path, "no %s, and it was touched %.0f s ago (under the "
                           "%.0f s an unstamped folder has to be idle for)"
                           % (OWNER_FILE, age, older_than))
                continue
            if not _renamable(path):
                keep(path, "no %s, old enough, but it cannot be renamed, so "
                           "something is holding it" % OWNER_FILE)
                continue
            if _remove_tree(path):
                removed.append(path)
            else:
                keep(path, "no %s, old enough, renamable, but the delete "
                           "itself failed" % OWNER_FILE)
    return removed


def _remove_tree(path):
    """Delete a folder in place. -> did it go.

    In place, and never after renaming it aside. Deleting under a temporary
    name was this module's own idea of safety and it was the opposite: when
    the delete then failed half way -- which it does, because a running
    process's DLLs cannot be unlinked -- the live instance was left with a
    folder it no longer had a path to and no `static/`. Deleting where it
    stands means a partial failure leaves the folder where its owner still
    expects it.
    """
    shutil.rmtree(path, ignore_errors=True)
    try:
        return not os.path.exists(path)
    except OSError:
        return False
