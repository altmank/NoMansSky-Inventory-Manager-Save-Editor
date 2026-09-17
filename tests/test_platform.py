"""P4-1: the one module allowed to branch on the operating system.

Two halves. The first proves the **non-Windows** answers on whatever machine
this runs on, by faking the platform: the shape of `game_status()`, the exact
refusal sentence, and -- the assertion that matters most -- that *nothing is
attempted*. A `ps`, a `pgrep`, a `/proc` walk or a `tasklist` that happens to
exist would all produce a confident "not running" for a game running under
Proton, which is the one wrong answer this gate must never give
(DECISIONS.md, 2026-09-14, "Apply is Windows-only").

The second half runs only on Windows and proves the real check answers with a
method it actually used and `known is True`.

`message_box` is never called from a test. It is a *modal* `MessageBoxW`: the
first call blocks the process until someone clicks OK, and a test suite that
opens a dialog per run hangs CI with nothing on stdout to say why.
"""
import errno
import io
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from nms_sorter import platform as platformmod                     # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Every test starts with no cached answer and leaves none behind.

    The two-second cache is the thing under test in one of these and a source
    of cross-test contamination in all the others: a `game_status()` from the
    previous test is still fresh when the next one starts.
    """
    platformmod.forget_game_status()
    yield
    platformmod.forget_game_status()


@pytest.fixture
def linux(monkeypatch):
    """Make this look like Linux to `platform.py` and to nothing else."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv(platformmod.FAKE_PLATFORM_ENV, raising=False)
    assert platformmod.is_windows() is False
    return "linux"


@pytest.fixture
def no_subprocess(monkeypatch):
    """Any attempt to shell out is a test failure, not a fallback."""
    def boom(*a, **k):
        raise AssertionError("the off-Windows branch shelled out: %r" % (a,))
    monkeypatch.setattr(subprocess, "check_output", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    return boom


# --------------------------------------------------------------- is_windows

def test_is_windows_reads_sys_platform_on_every_call(monkeypatch):
    monkeypatch.delenv(platformmod.FAKE_PLATFORM_ENV, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    assert platformmod.is_windows() is True
    monkeypatch.setattr(sys, "platform", "linux")
    assert platformmod.is_windows() is False
    monkeypatch.setattr(sys, "platform", "darwin")
    assert platformmod.is_windows() is False
    monkeypatch.setattr(sys, "platform", "cygwin")
    assert platformmod.is_windows() is False, "cygwin is not the Windows API"


def test_the_fake_platform_env_var_is_honoured_only_under_pytest(monkeypatch):
    """It is read here because `"pytest" in sys.modules`. In a real run it is
    ignored, so an environment variable cannot turn the write gate off."""
    monkeypatch.setenv(platformmod.FAKE_PLATFORM_ENV, "linux")
    monkeypatch.setattr(sys, "platform", "win32")
    assert platformmod.platform_name() == "linux"
    monkeypatch.delitem(sys.modules, "pytest")
    try:
        assert platformmod.platform_name() == "win32"
    finally:
        sys.modules["pytest"] = pytest


# ------------------------------------------------- the off-Windows answers

def test_off_windows_game_status_has_the_documented_shape(linux, no_subprocess):
    st = platformmod.game_status(max_age=0)
    assert st["running"] is None, "tri-state: not False, which reads as closed"
    assert st["known"] is False
    assert st["method"] == "unsupported: linux"
    assert st["pids"] == []
    assert st["process"] == "NMS.exe"
    assert st["error"] == platformmod.UNSUPPORTED_ERROR
    assert set(st) == {"running", "known", "method", "pids", "process", "error"}


def test_off_windows_nothing_is_attempted(linux, no_subprocess, monkeypatch):
    """No `tasklist`, and no `ctypes.windll` either. `no_subprocess` would
    raise on the first shell-out; `windll` does not exist off Windows at all,
    so touching it is an `AttributeError` on the platform this pretends to be.
    """
    import ctypes
    calls = []
    monkeypatch.setattr(platformmod, "_snapshot_processes",
                        lambda: calls.append("snapshot") or [])
    monkeypatch.setattr(platformmod, "_game_status_windows",
                        lambda: calls.append("windows") or {})
    st = platformmod.game_status(max_age=0)
    assert calls == []
    assert st["known"] is False
    assert hasattr(ctypes, "windll") or True    # the real guard is `calls`


def test_off_windows_the_refusal_sentence_is_exact(linux):
    st = platformmod.game_status(max_age=0)
    assert platformmod.refusal_sentence(st) == (
        "the process check needs Windows, so it cannot be told whether "
        "NMS.exe is running; plan and browse work here, apply does not")


def test_the_windows_unknown_sentence_is_untouched():
    """A Windows machine whose process list will not read keeps its own
    wording, which names the error. Only the platform case is rephrased."""
    said = platformmod.refusal_sentence(
        {"known": False, "method": "failed", "error": "denied / no tasklist"})
    assert said.startswith("could not read the process list (denied / no tasklist)")
    assert "Refusing rather than guessing" in said
    assert "blocking the process list" in said, \
        "and it says what to do about it: %s" % said
    assert "needs Windows" not in said


def test_refusal_sentence_survives_a_missing_status():
    assert "could not read the process list" in platformmod.refusal_sentence(None)
    assert "could not read the process list" in platformmod.refusal_sentence({})


def test_off_windows_there_is_no_default_save_root(linux):
    assert platformmod.default_save_root() is None


@pytest.fixture
def no_windll(monkeypatch):
    """Take `ctypes.windll` away, so `pid_alive` uses its POSIX branch here.

    `pid_alive` branches on **capability**, not on `platform_name()`: faking
    the platform does not give this process a POSIX `os.kill`, and on Windows
    `os.kill(pid, 0)` raises `EINVAL` for a pid that never existed rather than
    `ESRCH`. Branching on the name meant the Windows `os.kill` was answering
    the POSIX branch's question, every dead pid looked alive, and no lock was
    ever stale under the fake platform. So this reaches the POSIX branch the
    only honest way: by removing what the Windows branch needs.
    """
    monkeypatch.delattr(platformmod.ctypes, "windll", raising=False)
    assert not hasattr(platformmod.ctypes, "windll")


def test_off_windows_pid_alive_uses_os_kill(linux, no_windll, monkeypatch):
    seen = []

    def fake_kill(pid, sig):
        seen.append((pid, sig))
        if pid == 999999:
            raise ProcessLookupError()

    monkeypatch.setattr(os, "kill", fake_kill)
    assert platformmod.pid_alive(os.getpid()) is True
    assert platformmod.pid_alive(999999) is False
    assert seen[0][1] == 0, "signal 0 asks without sending anything"
    assert platformmod.pid_alive(0) is False
    assert platformmod.pid_alive(-4) is False
    assert platformmod.pid_alive(None) is False
    assert platformmod.pid_alive("not a pid") is False


@pytest.mark.parametrize("code,alive,why", [
    (errno.ESRCH, False, "POSIX: no such process"),
    (errno.EINVAL, False, "Windows' own os.kill, for a pid that never existed"),
    (errno.EPERM, True, "alive, and not ours to signal"),
    (errno.EACCES, True, "not an answer, so the pid keeps its lock"),
    (errno.EIO, True, "not an answer, so the pid keeps its lock"),
])
def test_pid_alive_maps_every_errno_it_can_get(linux, no_windll, monkeypatch,
                                               code, alive, why):
    """Two spellings of "gone", one of "alive", and everything else erring
    towards alive -- because the alternative is calling a live writer's lock
    stale and writing over the save it is holding."""
    def fake_kill(pid, sig):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(os, "kill", fake_kill)
    assert platformmod.pid_alive(4242) is alive, why


def test_the_gone_errnos_are_named_and_not_inlined(linux):
    """Both spellings live in one tuple: the Windows one is the surprising half
    and a bare `== ESRCH` is how it came to be missed."""
    assert errno.ESRCH in platformmod.PID_GONE_ERRNOS
    assert errno.EINVAL in platformmod.PID_GONE_ERRNOS
    assert errno.EPERM not in platformmod.PID_GONE_ERRNOS


def test_pid_alive_asks_windows_whenever_windows_is_there(linux, monkeypatch):
    """The capability branch, from the faked-Linux side: with `ctypes.windll`
    present, `pid_alive` must not go near `os.kill` even when `platform_name()`
    says linux. That combination is a test rig, and the check that actually
    works on this machine is the one to use."""
    if not hasattr(platformmod.ctypes, "windll"):
        pytest.skip("this machine has no ctypes.windll to prefer")

    def boom(pid, sig):
        raise AssertionError("os.kill was used where OpenProcess would work")

    monkeypatch.setattr(os, "kill", boom)
    assert platformmod.pid_alive(os.getpid()) is True
    assert platformmod.pid_alive(0) is False


def test_off_windows_message_box_is_a_no_op(linux):
    """Never shown, and never raises. This is the only test that may call it:
    off Windows it returns before touching `user32`."""
    assert platformmod.message_box("title", "text") is False


# -------------------------------------------------------- the 2 s cache

def test_game_status_is_cached_for_two_seconds(linux, monkeypatch):
    assert platformmod.STATUS_CACHE_SECONDS == 2.0
    calls = []
    monkeypatch.setattr(platformmod, "_game_status_unsupported",
                        lambda: calls.append(1) or {
                            "running": None, "known": False, "pids": [],
                            "method": "unsupported: linux", "process": "NMS.exe"})
    now = [1000.0]
    monkeypatch.setattr(platformmod.time, "monotonic", lambda: now[0])

    platformmod.game_status()
    platformmod.game_status()
    platformmod.game_status()
    assert len(calls) == 1, "three asks, one look"

    now[0] += 1.9
    platformmod.game_status()
    assert len(calls) == 1, "still inside the window"

    now[0] += 0.2
    platformmod.game_status()
    assert len(calls) == 2, "past two seconds, it looks again"


def test_max_age_zero_bypasses_the_cache(linux, monkeypatch):
    calls = []
    monkeypatch.setattr(platformmod, "_game_status_unsupported",
                        lambda: calls.append(1) or {"known": False, "pids": []})
    platformmod.game_status(max_age=0)
    platformmod.game_status(max_age=0)
    assert len(calls) == 2


def test_forget_game_status_makes_the_next_call_look(linux, monkeypatch):
    calls = []
    monkeypatch.setattr(platformmod, "_game_status_unsupported",
                        lambda: calls.append(1) or {"known": False, "pids": []})
    platformmod.game_status()
    platformmod.forget_game_status()
    platformmod.game_status()
    assert len(calls) == 2


def test_a_caller_cannot_poison_the_cache(linux):
    """The answer is handed out as a copy. A route that annotates the dict it
    got -- and `bootstrap` does put it inside a bigger one -- must not change
    what the next caller, which may be the write gate, is told."""
    first = platformmod.game_status()
    first["running"] = False
    first["known"] = True
    first["pids"].append(4321)
    second = platformmod.game_status()
    assert second["running"] is None and second["known"] is False
    assert second["pids"] == []
    assert first is not second


# ------------------------------------------------------------------ paths

def test_state_dir_is_localappdata_when_it_is_set(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert platformmod.state_dir() == os.path.join(str(tmp_path), "NMS-Sorter")


def test_state_dir_falls_back_to_local_share(monkeypatch, tmp_path):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    got = platformmod.state_dir()
    assert got.endswith(os.path.join(".local", "share", "NMS-Sorter"))


def test_the_save_root_is_one_path_and_no_second_strategy(monkeypatch, tmp_path):
    monkeypatch.delenv(platformmod.FAKE_PLATFORM_ENV, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert platformmod.default_save_root() == os.path.join(
        str(tmp_path), "HelloGames", "NMS")
    assert platformmod.SAVE_ROOT_PARTS == ("HelloGames", "NMS")


# -------------------------------------------------------- the frozen build

def test_not_frozen_in_a_source_run():
    assert platformmod.is_frozen() is False
    assert platformmod.exe_dir() is None


def test_bundle_dir_is_the_package_in_a_source_run():
    got = platformmod.bundle_dir()
    assert os.path.isdir(os.path.join(got, "static"))
    assert os.path.isdir(os.path.join(got, "data"))


def test_bundle_dir_follows_meipass_under_pyinstaller(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert platformmod.bundle_dir() == os.path.join(str(tmp_path), "nms_sorter")


def test_ensure_streams_substitutes_only_what_is_missing(monkeypatch, caplog):
    """A windowed build has `sys.stdout is None` and the first `print()` in
    `cli.main` would raise in a process with nowhere to show it."""
    import logging
    monkeypatch.setattr(sys, "stdout", None)
    real_stderr = sys.stderr
    replaced = platformmod.ensure_streams(logging.getLogger("nms_sorter.test"))
    try:
        assert replaced == ["stdout"], "stderr was real and is left alone"
        assert sys.stderr is real_stderr
        with caplog.at_level(logging.INFO, logger="nms_sorter.test"):
            print("  serving http://127.0.0.1:8765/")
            sys.stdout.flush()
        assert any("serving http://127.0.0.1:8765/" in r.message
                   for r in caplog.records)
        assert sys.stdout.isatty() is False
    finally:
        monkeypatch.undo()


def test_streams_visible_asks_for_a_file_descriptor_not_a_window(monkeypatch):
    """The predicate both frozen decisions turn on.

    `GetConsoleWindow()` answers 0 for `NMS-Sorter-console.exe` when it is
    started from a pseudo-console or with its output redirected. Using it sent
    the console build's own banner to the log instead of to the redirect it
    was asked for -- observed on the packaged binary. A usable file descriptor
    is the honest test.
    """
    class NoFileno(object):
        def write(self, s):
            return len(s)

        def fileno(self):
            raise OSError("this one discards")

    class Piped(object):
        def fileno(self):
            return 7

    monkeypatch.setattr(sys, "stdout", NoFileno())
    monkeypatch.setattr(sys, "stderr", NoFileno())
    assert platformmod.streams_visible() is False
    monkeypatch.setattr(sys, "stdout", Piped())
    assert platformmod.streams_visible() is True
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", Piped())
    assert platformmod.streams_visible() is True, "stderr alone is enough"
    monkeypatch.setattr(sys, "stderr", None)
    assert platformmod.streams_visible() is False


def test_streams_are_visible_under_pytest():
    """Sanity: pytest's capture gives both streams a descriptor, so nothing in
    a source run ever takes the frozen path by accident."""
    assert platformmod.streams_visible() is True


def test_ensure_streams_is_a_no_op_when_there_are_streams():
    assert platformmod.ensure_streams() == []


def test_ensure_streams_replaces_a_discarding_stdout_when_forced(monkeypatch,
                                                                 caplog):
    """The condition the packaged build actually presents.

    PyInstaller 6 does not leave `sys.stdout` as None in a windowed build; it
    substitutes a writer that throws the bytes away. Nothing raises and the
    entire startup banner disappears -- measured on `NMS-Sorter.exe`, started
    detached with no handles, whose log held the `serving` line and none of
    the banner. So the frozen-and-no-console case forces the substitution
    rather than waiting for a None that never comes.
    """
    import logging

    class Discards(object):
        def write(self, s):
            return len(s)

        def flush(self):
            pass

    monkeypatch.setattr(sys, "stdout", Discards())
    real_stderr = sys.stderr
    replaced = platformmod.ensure_streams(
        logging.getLogger("nms_sorter.test"), force=True)
    try:
        assert replaced == ["stdout"]
        assert sys.stderr is real_stderr, "never forced: --log - writes there"
        with caplog.at_level(logging.INFO, logger="nms_sorter.test"):
            print("  backups  C:\\tmp\\backups")
            sys.stdout.flush()
        assert any("backups" in r.message for r in caplog.records)
    finally:
        monkeypatch.undo()


def test_the_log_stream_cannot_recurse(monkeypatch):
    """`--log -` attaches a `StreamHandler(sys.stderr)`. If that ever points
    back at a `_LogStream`, one log call must not exhaust the stack."""
    import logging
    logger = logging.getLogger("nms_sorter.test.loop")
    stream = platformmod._LogStream(logger, logging.INFO)
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    logger.propagate = False
    try:
        stream.write("round and round" + chr(10))
        logger.info("and again")
    finally:
        logger.removeHandler(handler)
        logger.propagate = True


# ------------------------------------------------------- the Windows half

windows_only = pytest.mark.skipif(
    not platformmod.is_windows(),
    reason="the real process check only exists on Windows")


@windows_only
def test_on_windows_the_check_names_the_method_it_used():
    st = platformmod.game_status(max_age=0)
    assert st["known"] is True
    assert st["method"] in ("toolhelp32", "tasklist")
    assert st["running"] in (True, False)
    assert isinstance(st["pids"], list)
    assert st["process"] == "NMS.exe"
    assert "error" not in st


@windows_only
def test_on_windows_the_snapshot_lists_this_process():
    procs = platformmod._snapshot_processes()
    assert (os.path.basename(sys.executable), os.getpid()) in [
        (exe, pid) for exe, pid in procs], "the snapshot missed our own pid"


@windows_only
def test_on_windows_the_tasklist_fallback_answers_when_toolhelp32_fails(
        monkeypatch):
    """The fallback is not decoration: a security product hooking
    `CreateToolhelp32Snapshot` is the case it was written for."""
    def boom():
        raise OSError("CreateToolhelp32Snapshot failed")
    monkeypatch.setattr(platformmod, "_snapshot_processes", boom)
    st = platformmod.game_status(max_age=0)
    assert st["method"] == "tasklist"
    assert st["known"] is True
    assert st["running"] in (True, False)


@windows_only
def test_on_windows_both_legs_failing_is_not_known(monkeypatch):
    def boom():
        raise OSError("no snapshot")

    def boom2(*a, **k):
        raise OSError("no tasklist")
    monkeypatch.setattr(platformmod, "_snapshot_processes", boom)
    monkeypatch.setattr(subprocess, "check_output", boom2)
    st = platformmod.game_status(max_age=0)
    assert st["known"] is False and st["running"] is None
    assert st["method"] == "failed"
    assert "no snapshot" in st["error"] and "no tasklist" in st["error"]
    # and this is the branch that keeps the Windows wording
    assert "needs Windows" not in platformmod.refusal_sentence(st)


@windows_only
def test_on_windows_pid_alive_knows_this_process_and_not_a_silly_one():
    assert platformmod.pid_alive(os.getpid()) is True
    assert platformmod.pid_alive(0) is False
    # pid 4 is the Windows System process: present, and not openable by us.
    # Either answer is fine; what must not happen is an exception.
    assert platformmod.pid_alive(4) in (True, False)


@windows_only
def test_on_windows_has_console_answers_without_raising():
    assert platformmod.has_console() in (True, False)


# ------------------------------------- the refusal, through the write path

def test_apply_refuses_off_windows_with_exactly_the_documented_sentence(
        linux, no_subprocess, tmp_path):
    """The sentence has to come out of `safety.apply_plan` step 1, not just
    out of `refusal_sentence`, and it has to come out *before* anything is
    copied, decoded or written.

    Deliberately not using `conftest.no_game_running`: that fixture stubs
    `game_status` to "closed and known", which is what lets the other 900
    tests exercise steps 2 to 12 on this machine and off it. This one test
    needs the real off-Windows answer.
    """
    from nms_sorter import safety

    save = tmp_path / "save.hg"
    save.write_bytes(b"not a real save, and it is never read")
    (tmp_path / "mf_save.hg").write_bytes(b"nor is this")
    backups = tmp_path / "backups"

    def plan_builder(*a, **kw):
        raise AssertionError("step 1 must refuse before a plan is built")

    with pytest.raises(safety.Refused) as e:
        safety.apply_plan(str(save), {}, "deadbeef", plan_builder, str(backups))

    said = str(e.value)
    assert said == (
        "game: the process check needs Windows, so it cannot be told whether "
        "NMS.exe is running; plan and browse work here, apply does not")
    # nothing was written, not even a backup folder
    assert not backups.exists()
    assert save.read_bytes() == b"not a real save, and it is never read"
    # and the lock it took is released
    assert not os.path.exists(str(save) + safety.LOCK_SUFFIX)


def test_plan_and_browse_are_unaffected_off_windows(linux, no_subprocess):
    """Only apply refuses. The item table, the codec and the bucket list are
    platform-neutral and stay that way -- Ubuntu CI is what proves it, and
    this asserts the same thing on the machine doing the faking."""
    from nms_sorter.itemdb import db
    d = db()
    assert len(d.buckets) >= 17
    from nms_sorter import codec
    # the trailing NUL is the save format's, and it is the codec's job to add
    # it on every platform alike
    payload = codec.dumps(codec.loads(b'{"a":[1,2,3]}'))
    assert payload.rstrip(bytes([0])) == b'{"a":[1,2,3]}'
    assert codec.loads(payload) == {"a": [1, 2, 3]}


def test_the_refusal_sentence_is_in_the_docs(linux):
    """P5-9 in miniature: a refusal whose sentence is not in
    `TROUBLESHOOTING.md` is a refusal the operator cannot look up."""
    for name in ("SAFETY.md", "TROUBLESHOOTING.md"):
        path = os.path.join(ROOT, "docs", name)
        if not os.path.exists(path):
            pytest.skip("%s not written yet (lane E)" % name)
    hits = 0
    for name in ("SAFETY.md", "TROUBLESHOOTING.md"):
        with io.open(os.path.join(ROOT, "docs", name), encoding="utf-8") as fh:
            if "the process check needs Windows" in fh.read():
                hits += 1
    assert hits, ("neither docs/SAFETY.md nor docs/TROUBLESHOOTING.md carries "
                  "the platform refusal sentence; the paragraphs are in the "
                  "P4-1 report for lane E to paste")


# ===========================================================================
# W3: only one of me
# ===========================================================================
#
# The bug these cover: three fast double-clicks produced three servers
# `LISTENING` on 127.0.0.1:8765, because the "already running?" probe runs
# before the bind and Windows' `SO_REUSEADDR` lets the second bind succeed.
# Both halves are tested here; `tests/test_cli.py` tests them wired together
# and, with three real subprocesses, the race itself.


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A `state_dir()` in a temp tree, so a lock file is never the real one."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    return platformmod.state_dir()


#: a port number nothing is asked to listen on. The lock is a *name*, not a
#: socket, so the tests below never bind: they only need two numbers that no
#: parallel pytest run is likely to be holding.
LOCK_PORT = 18760
LOCK_PORT_B = 18761


def test_a_second_instance_cannot_take_the_lock(state):
    first = platformmod.acquire_single_instance(LOCK_PORT)
    try:
        assert first.acquired is True
        assert not first.kind.endswith("-unavailable"), \
            "the machine could not make the lock at all; the rest of this " \
            "test is not measuring anything"
        second = platformmod.acquire_single_instance(LOCK_PORT)
        assert second.acquired is False
        assert second.release() is False, \
            "a lock that was never taken must release to nothing"
    finally:
        first.release()


def test_releasing_the_lock_lets_the_next_launch_have_it(state):
    first = platformmod.acquire_single_instance(LOCK_PORT)
    assert first.acquired is True
    assert first.release() is True
    assert first.release() is False, "release is idempotent"
    second = platformmod.acquire_single_instance(LOCK_PORT)
    try:
        assert second.acquired is True, \
            "the lock outlived the process that held it"
    finally:
        second.release()


def test_the_lock_is_per_port_not_per_machine(state):
    """`--port 9000` beside the default is a deliberate second sorter on a
    second port, and refusing it would break the flag. Two on one port are the
    bug; two on two ports are a feature."""
    a = platformmod.acquire_single_instance(LOCK_PORT)
    b = platformmod.acquire_single_instance(LOCK_PORT_B)
    try:
        assert (a.acquired, b.acquired) == (True, True)
    finally:
        a.release()
        b.release()


# `platformmod.is_windows()`, not `sys.platform`: the lock follows the faked
# platform name (that is what makes the POSIX leg testable here), so a guard
# that read the real OS ran this against the lock file under
# `NMS_SORTER_FAKE_PLATFORM=linux` and asserted `pidfile == mutex`. The two
# guards below stay on `sys.platform` on purpose -- they ask "does this machine
# have `fcntl`?", which faking a name does not change.
@pytest.mark.skipif(not platformmod.is_windows(),
                    reason="the mutex leg is the Windows one")
def test_on_windows_the_lock_is_a_named_mutex(state):
    lock = platformmod.acquire_single_instance(LOCK_PORT)
    try:
        assert lock.kind == "mutex"
        assert lock.name == r"Local\NMS-Sorter-%d" % LOCK_PORT
        assert lock.handle, "the handle is what holds the name open"
        # Nothing was written to disk: a mutex is closed by the kernel when
        # the process dies, which is why this leg needs no pid and no cleanup.
        assert not os.path.exists(platformmod.instance_lock_path(LOCK_PORT))
    finally:
        lock.release()


def test_off_windows_the_lock_is_a_file_in_the_state_dir(state, linux):
    """The POSIX leg, proved on this machine by faking the platform name.

    `is_windows()` is what `acquire_single_instance` branches on -- unlike
    `pid_alive`, which branches on the capability -- precisely so that this
    test can run where the suite runs.
    """
    lock = platformmod.acquire_single_instance(LOCK_PORT)
    try:
        assert lock.acquired is True
        assert lock.kind in ("flock", "pidfile"), lock.kind
        path = platformmod.instance_lock_path(LOCK_PORT)
        assert lock.path == path
        assert os.path.abspath(path).startswith(os.path.abspath(state))
        assert os.path.exists(path)
        second = platformmod.acquire_single_instance(LOCK_PORT)
        assert second.acquired is False
        assert second.owner_pid == os.getpid(), \
            "the file names its owner, so the second copy can say who has it"
    finally:
        lock.release()
    assert not os.path.exists(path), "the lock file goes with the lock"


@pytest.mark.skipif(not sys.platform.startswith("win"),
                    reason="this is the no-fcntl fallback, and Windows is "
                           "the platform that has no fcntl")
def test_the_lock_file_fallback_takes_over_a_dead_owners_file(state, linux):
    """Nothing releases a lock file when its owner is killed.

    So the `O_EXCL` leg -- the one a platform without `fcntl` takes, which is
    the one this machine takes under the fake platform -- reads the pid and
    asks whether it is alive. A dead owner's file is taken over; the
    alternative is telling the operator that a process which no longer exists
    owns their port, and in a build with no console that reads as "double
    clicking does nothing".
    """
    path = platformmod.instance_lock_path(LOCK_PORT)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="ascii") as fh:
        fh.write("999999999\n")            # a pid no machine has
    assert platformmod.pid_alive(999999999) is False
    lock = platformmod.acquire_single_instance(LOCK_PORT)
    try:
        assert lock.acquired is True
        assert lock.kind == "pidfile"
        with io.open(path, encoding="ascii") as fh:
            assert fh.read().strip() == str(os.getpid())
    finally:
        lock.release()


@pytest.mark.skipif(not sys.platform.startswith("win"),
                    reason="needs a machine with no fcntl to reach the leg")
def test_a_live_owners_lock_file_is_obeyed(state, linux):
    """The other side of the pid check: this process is alive, so its own
    file is held against a second launch rather than taken over."""
    path = platformmod.instance_lock_path(LOCK_PORT)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="ascii") as fh:
        fh.write("%d\n" % os.getpid())
    lock = platformmod.acquire_single_instance(LOCK_PORT)
    assert lock.acquired is False
    assert lock.owner_pid == os.getpid()
    os.remove(path)


def test_a_lock_that_cannot_be_made_does_not_stop_the_program(tmp_path,
                                                              monkeypatch,
                                                              linux):
    """Fail open, and say so in `kind`.

    A sorter that refuses to start because of its own lock is a worse bug
    than the one the lock fixes: the symptom is a double click that does
    nothing, with no console to explain it. The exclusive bind is still there
    to catch a real duplicate.
    """
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    def no_dirs(*a, **k):
        raise OSError(13, "denied")

    monkeypatch.setattr(os, "makedirs", no_dirs)
    lock = platformmod.acquire_single_instance(LOCK_PORT)
    assert lock.acquired is True
    assert lock.kind.endswith("-unavailable")
    assert lock.error


# --------------------------------------------------- the exclusive bind

def _bind(cls, port):
    from http.server import BaseHTTPRequestHandler
    return cls(("127.0.0.1", port), BaseHTTPRequestHandler)


@pytest.fixture
def free_port():
    import socket as _socket
    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_the_exclusive_class_refuses_a_second_listener(free_port):
    from http.server import ThreadingHTTPServer
    cls = platformmod.exclusive_server_class(ThreadingHTTPServer)
    assert cls.allow_reuse_address is False
    first = _bind(cls, free_port)
    try:
        with pytest.raises(OSError):
            _bind(cls, free_port).server_close()
    finally:
        first.server_close()


@pytest.mark.skipif(not sys.platform.startswith("win"),
                    reason="SO_REUSEADDR only means 'bind over a listener' "
                           "on Windows; POSIX bind already refuses")
def test_the_plain_class_is_what_let_three_sorters_share_one_port(free_port):
    """The bug itself, as a test, so nobody removes the fix as unnecessary.

    Two plain `ThreadingHTTPServer`s bind one port on Windows and both
    "listen"; arriving connections then go to whichever the stack picks, which
    is why ending one task in Task Manager left the page still answering
    (W3). The same second bind against an exclusive first one is refused.
    """
    from http.server import ThreadingHTTPServer
    plain = _bind(ThreadingHTTPServer, free_port)
    try:
        twin = _bind(ThreadingHTTPServer, free_port)
        twin.server_close()
    except OSError:                                  # pragma: no cover
        pytest.skip("this stack already refuses the duplicate bind")
    finally:
        plain.server_close()

    cls = platformmod.exclusive_server_class(ThreadingHTTPServer)
    first = _bind(cls, free_port)
    try:
        with pytest.raises(OSError):
            _bind(ThreadingHTTPServer, free_port).server_close()
    finally:
        first.server_close()


def test_the_exclusive_class_is_a_subclass_and_leaves_the_base_alone():
    """The base is `http.server.ThreadingHTTPServer`: a standard library class
    anything else in this interpreter may also be using."""
    from http.server import ThreadingHTTPServer
    before = ThreadingHTTPServer.allow_reuse_address
    cls = platformmod.exclusive_server_class(ThreadingHTTPServer)
    assert issubclass(cls, ThreadingHTTPServer)
    assert cls is not ThreadingHTTPServer
    assert cls.__name__ == "ExclusiveThreadingHTTPServer"
    assert ThreadingHTTPServer.allow_reuse_address == before


# ===========================================================================
# W2: the folder this process holds open
# ===========================================================================

def test_release_cwd_does_nothing_in_a_source_run(monkeypatch):
    """`python -m nms_sorter` runs in a directory that belongs to the
    operator, and there is no exe folder to let go of."""
    monkeypatch.setattr(platformmod, "is_frozen", lambda: False)
    before = os.getcwd()
    assert platformmod.release_cwd() is None
    assert os.getcwd() == before


def test_a_frozen_build_leaves_the_folder_it_was_double_clicked_in(
        tmp_path, monkeypatch):
    """W2: Windows will not delete a directory that is a process's cwd, so the
    exe's own folder could not be deleted while the sorter ran -- which is the
    whole of the uninstall instructions."""
    download = tmp_path / "Downloads" / "nms-sorter-v1"
    download.mkdir(parents=True)
    state = tmp_path / "local"
    monkeypatch.setenv("LOCALAPPDATA", str(state))
    monkeypatch.setattr(platformmod, "is_frozen", lambda: True)
    monkeypatch.setattr(platformmod, "exe_dir", lambda: str(download))
    before = os.getcwd()
    os.chdir(str(download))
    try:
        moved = platformmod.release_cwd()
        assert moved == platformmod.state_dir()
        assert os.path.samefile(os.getcwd(), moved)
        # The point of the exercise: the folder can now go.
        import shutil as _shutil
        _shutil.rmtree(str(download))
        assert not download.exists()
    finally:
        os.chdir(before)


def test_a_frozen_build_stays_where_it_was_started_from(tmp_path, monkeypatch):
    """Only the exe's *own* folder is released. A player who ran it from a
    shell in some other directory chose that directory, and moving out of it
    would be a change with no reason behind it."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(platformmod, "is_frozen", lambda: True)
    monkeypatch.setattr(platformmod, "exe_dir", lambda: str(tmp_path / "exe"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    before = os.getcwd()
    os.chdir(str(elsewhere))
    try:
        assert platformmod.release_cwd() is None
        assert os.path.samefile(os.getcwd(), str(elsewhere))
    finally:
        os.chdir(before)


def test_release_cwd_falls_back_to_the_temp_directory(tmp_path, monkeypatch):
    """A `%LOCALAPPDATA%` that cannot be created must not leave the process
    holding the folder it is trying to let go of."""
    download = tmp_path / "dl"
    download.mkdir()
    monkeypatch.setattr(platformmod, "is_frozen", lambda: True)
    monkeypatch.setattr(platformmod, "exe_dir", lambda: str(download))
    # A directory whose parent is a file: `makedirs` raises `NotADirectoryError`
    # there, which is the OSError shape the fallback is written for.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setattr(platformmod, "state_dir",
                        lambda: str(blocker / "NMS-Sorter"))
    before = os.getcwd()
    os.chdir(str(download))
    try:
        import tempfile as _tempfile
        moved = platformmod.release_cwd()
        assert moved == _tempfile.gettempdir()
    finally:
        os.chdir(before)


# ===========================================================================
# W4: the one-file extraction folders
# ===========================================================================

def _meidir(root, name, marker=True, age=None, owner=None,
            started=None):
    """A plausible `_MEI*` folder.

    `age` in seconds, backdated. `owner` writes an `owner.pid` naming that
    pid, which is what a live instance leaves behind and what the sweep now
    decides on.
    """
    import time as _time
    path = os.path.join(str(root), name)
    os.makedirs(path, exist_ok=True)
    if marker:
        os.makedirs(os.path.join(path, platformmod.RUNTIME_MARKER),
                    exist_ok=True)
        with io.open(os.path.join(path, platformmod.RUNTIME_MARKER,
                                  "data.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
    if owner is not None:
        if started is None:
            started = platformmod.process_start_time(owner)
        with io.open(os.path.join(path, platformmod.OWNER_FILE), "w",
                     encoding="ascii") as fh:
            fh.write("%d %.3f\n" % (owner, started if started is not None
                                    else _time.time()))
    if age:
        old = _time.time() - age
        os.utime(path, (old, old))
    return path


#: "nothing else of ours is running", injected rather than measured. The real
#: answer comes from the machine's process list, and a test whose result
#: depends on whether the operator happens to have the sorter open is not a
#: test. The case where something *is* running has its own test below.
def _nothing_running():
    return 0, "no other NMS-Sorter.exe process (test)"


def test_the_sweep_removes_only_abandoned_folders_of_this_program(tmp_path):
    """Four filters, and every one of them is load bearing.

    `%TEMP%` is shared with every other one-file PyInstaller program on the
    machine, so the marker is what keeps this from deleting another vendor's
    files; the age is the only evidence of abandonment PyInstaller leaves.
    """
    root = tmp_path / "temp"
    root.mkdir()
    stale = _meidir(root, "_MEI0000a", age=7200)
    fresh = _meidir(root, "_MEI0000b", age=10)
    other_app = _meidir(root, "_MEI0000c", marker=False, age=7200)
    not_ours = _meidir(root, "someapp-cache", age=7200)
    plain_file = root / "_MEI0000d"
    plain_file.write_text("not a directory")

    removed = platformmod.sweep_stale_runtime_dirs(roots=[str(root)],
                                                  others=_nothing_running)

    assert removed == [stale]
    assert not os.path.exists(stale)
    for kept in (fresh, other_app, not_ours):
        assert os.path.isdir(kept), kept
    assert plain_file.exists()


def test_the_sweep_never_touches_the_folder_this_process_runs_from(
        tmp_path, monkeypatch):
    """`sys._MEIPASS` is where the running program's own `data/` and
    `static/` are. Deleting it mid-session is the one way this housekeeping
    could break the thing it is tidying up after."""
    root = tmp_path / "temp"
    root.mkdir()
    mine = _meidir(root, "_MEI0000own", age=7200)
    theirs = _meidir(root, "_MEI0000old", age=7200)
    monkeypatch.setattr(sys, "_MEIPASS", mine, raising=False)
    said = []

    class FakeLog(object):
        def info(self, msg, *a):
            said.append(msg % a)

    removed = platformmod.sweep_stale_runtime_dirs(roots=[str(root)],
                                                   others=_nothing_running,
                                                   log=FakeLog())
    assert removed == [theirs]
    assert os.path.isdir(mine)
    # Said out loud, so that a log from a live session is evidence the sweep
    # looked at its own bundle and left it, rather than evidence of nothing.
    assert any(mine in line and "this process is running out of it" in line
               for line in said), said


def test_the_sweep_keeps_a_folder_it_cannot_rename(tmp_path, monkeypatch):
    """The last filter on an *unstamped* folder, and no longer the deciding
    one. A rename that fails means something is holding the directory -- a
    process's current directory, or a filesystem that will not have it. A
    rename that succeeds proves much less than this module once claimed (see
    `platform.OWNER_FILE`), which is why it now comes after the owner stamp
    and after "is another sorter running"."""
    root = tmp_path / "temp"
    root.mkdir()
    busy = _meidir(root, "_MEI0000busy", age=7200)
    monkeypatch.setattr(os, "rename",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError(13, "in use")))
    assert platformmod.sweep_stale_runtime_dirs(
        roots=[str(root)], others=_nothing_running) == []
    assert os.path.isdir(busy)
    assert os.path.isdir(os.path.join(busy, platformmod.RUNTIME_MARKER)), \
        "a failed probe must leave the folder whole, not half emptied"


def test_the_sweep_survives_a_root_that_is_not_there(tmp_path):
    """Housekeeping that can stop the program starting is worse than litter."""
    assert platformmod.sweep_stale_runtime_dirs(
        roots=[str(tmp_path / "gone"), "", None],
        others=_nothing_running) == []


def test_the_sweep_looks_in_the_runtime_dir_and_in_temp(monkeypatch, tmp_path):
    """Two roots, because the exe now unpacks to `runtime_dir()` while the
    litter of every earlier build is still in `%TEMP%` and nothing else is
    ever going to remove it."""
    import tempfile as _tempfile
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    seen = []
    monkeypatch.setattr(os, "listdir", lambda root: seen.append(root) or [])
    platformmod.sweep_stale_runtime_dirs()
    assert seen == [platformmod.runtime_dir(), _tempfile.gettempdir()]


def test_the_runtime_dir_is_under_the_state_dir(monkeypatch, tmp_path):
    """It has to be the path the spec gives the bootloader, expanded; the
    spec's own assertion is in `tests/test_packaging.py`."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert platformmod.runtime_dir() == os.path.join(
        platformmod.state_dir(), "runtime")


# ===========================================================================
# The incident: the sweep emptied a live server's own bundle
# ===========================================================================
#
# An instance that had been serving since 12:21 was still up at 16:58 when its
# owner double-clicked the exe again. The second copy swept before it found
# out it had lost the lock; the live folder was five hours old; the rename
# probe succeeded, because Windows renames a directory quite happily while the
# DLLs inside it are mapped. `static/` and `docs/` went, and the running
# server answered 404 JSON at `/`.
#
# Three things had to change and all three are tested here: the owner writes
# down who it is, an unstamped folder is left alone while any sorter is
# running, and the probe puts the folder back before anything is decided.
# `tests/test_cli.py` tests the fourth: only the instance that won the lock
# sweeps at all.


def test_the_owner_stamp_round_trips(tmp_path):
    folder = tmp_path / "_MEI0000live"
    folder.mkdir()
    written = platformmod.write_runtime_owner(str(folder))
    assert written == str(folder / platformmod.OWNER_FILE)
    got = platformmod.read_runtime_owner(str(folder))
    assert got["pid"] == os.getpid()
    assert got["started"] > 0
    state, why = platformmod.runtime_owner_state(str(folder))
    assert state == "live", why
    assert str(os.getpid()) in why


def test_the_owner_stamp_is_a_no_op_without_a_bundle(tmp_path, monkeypatch):
    """A source run has no extraction folder to claim, and `sys._MEIPASS` is
    the only place this is ever written."""
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert platformmod.write_runtime_owner() is None
    assert platformmod.write_runtime_owner(str(tmp_path / "not there")) is None


def test_a_folder_owned_by_a_live_pid_survives_the_sweep(tmp_path):
    """The incident, as a test. Old enough, ours, renamable -- and in use."""
    root = tmp_path / "temp"
    root.mkdir()
    live = _meidir(root, "_MEI0000live", age=5 * 3600, owner=os.getpid())
    said = []

    class FakeLog(object):
        def info(self, msg, *a):
            said.append(msg % a)

    removed = platformmod.sweep_stale_runtime_dirs(
        roots=[str(root)], others=_nothing_running, log=FakeLog())
    assert removed == []
    assert os.path.isdir(os.path.join(live, platformmod.RUNTIME_MARKER)), \
        "the running server's static/ and docs/ are still there"
    assert any("owner pid %d is running" % os.getpid() in line
               for line in said), said


def test_a_folder_owned_by_a_dead_pid_is_removed(tmp_path):
    """The other side. A stamp naming a process that no longer exists is the
    only *direct* evidence of abandonment there is, so it needs no age test
    and no probe."""
    root = tmp_path / "temp"
    root.mkdir()
    dead = _meidir(root, "_MEI0000dead", age=10, owner=999999999,
                   started=1.0)
    assert platformmod.pid_alive(999999999) is False
    removed = platformmod.sweep_stale_runtime_dirs(
        roots=[str(root)], others=_nothing_running)
    assert removed == [dead]
    assert not os.path.exists(dead)


def test_a_recycled_pid_does_not_protect_a_folder_for_ever(tmp_path):
    """A pid is not an identity: this machine will hand the number out again.
    The stamp records when the owner started, and `GetProcessTimes` says when
    the process holding that number now started; far apart means it is not the
    owner."""
    if platformmod.process_start_time(os.getpid()) is None:
        pytest.skip("no GetProcessTimes here, so there is no start time to "
                    "compare and pid_alive is the whole answer")
    root = tmp_path / "temp"
    root.mkdir()
    # This pid is alive -- it is us -- but the stamp says its owner started in
    # 1970, so the process using that number now is somebody else.
    folder = _meidir(root, "_MEI0000recycled", age=2 * 3600,
                     owner=os.getpid(), started=1.0)
    state, why = platformmod.runtime_owner_state(folder)
    assert state == "dead", why
    assert "different process" in why
    assert platformmod.sweep_stale_runtime_dirs(
        roots=[str(root)], others=_nothing_running) == [folder]


def test_an_unstamped_folder_survives_while_any_sorter_is_running(tmp_path):
    """An older build's folder says nothing about who is using it. A running
    `NMS-Sorter.exe` might be that somebody, and there is nothing to gain by
    guessing: the folder will still be there next time, when nothing is."""
    root = tmp_path / "temp"
    root.mkdir()
    old_build = _meidir(root, "_MEI0000older", age=5 * 3600)
    said = []

    class FakeLog(object):
        def info(self, msg, *a):
            said.append(msg % a)

    removed = platformmod.sweep_stale_runtime_dirs(
        roots=[str(root)], log=FakeLog(),
        others=lambda: (1, "1 other NMS-Sorter.exe process(es): [55060]"))
    assert removed == []
    assert os.path.isdir(old_build)
    assert any("55060" in line and platformmod.OWNER_FILE in line
               for line in said), said

    # And with nothing running, the same folder goes.
    assert platformmod.sweep_stale_runtime_dirs(
        roots=[str(root)], others=_nothing_running) == [old_build]


def test_an_unreadable_process_list_counts_as_something_running(tmp_path):
    """`None` from `sorters_running` is "there is no process list to read
    here", and that is not permission to delete."""
    root = tmp_path / "temp"
    root.mkdir()
    old_build = _meidir(root, "_MEI0000older", age=5 * 3600)
    assert platformmod.sweep_stale_runtime_dirs(
        roots=[str(root)],
        others=lambda: (None, "the process list would not read")) == []
    assert os.path.isdir(old_build)


def test_the_probe_puts_the_folder_back_before_deciding(tmp_path, monkeypatch):
    """A probe must not be the thing that breaks a running server. The rename
    is undone before any decision, so the live path never sees a `.stale-*` or
    `.probe-*` name -- which is how the incident's folder was lost: it was
    renamed aside and then half deleted, leaving the owner with no path to its
    own bundle."""
    root = tmp_path / "temp"
    root.mkdir()
    folder = _meidir(root, "_MEI0000older", age=5 * 3600)
    names = []
    real = os.rename

    def watched(src, dst):
        names.append((os.path.basename(src), os.path.basename(dst)))
        return real(src, dst)

    monkeypatch.setattr(os, "rename", watched)
    assert platformmod._renamable(folder) is True
    assert len(names) == 2, names
    assert names[1][1] == "_MEI0000older", "it is put back under its own name"
    assert sorted(os.listdir(str(root))) == ["_MEI0000older"]

    # And after a real sweep there is no probe name left anywhere either.
    platformmod.sweep_stale_runtime_dirs(roots=[str(root)],
                                         others=_nothing_running)
    assert [n for n in os.listdir(str(root)) if ".probe-" in n
            or ".stale-" in n] == []


def test_a_folder_an_earlier_broken_sweep_emptied_is_still_ours(tmp_path):
    """`_MEI0000x.stale-56128` is a name only this program's own sweep ever
    produced, and the folder it names has no `nms_sorter/` left in it because
    that is what the sweep removed. It is still litter, and it is still ours
    to clear up."""
    root = tmp_path / "temp"
    root.mkdir()
    orphan = _meidir(root, "_MEI0000x.stale-56128", marker=False,
                     age=5 * 3600)
    assert platformmod.sweep_stale_runtime_dirs(
        roots=[str(root)], others=_nothing_running) == [orphan]


def test_another_vendors_folder_is_still_never_touched(tmp_path):
    """The marker test comes first and is unchanged: `%TEMP%` is shared."""
    root = tmp_path / "temp"
    root.mkdir()
    theirs = _meidir(root, "_MEI0000other", marker=False, age=5 * 3600)
    said = []

    class FakeLog(object):
        def info(self, msg, *a):
            said.append(msg % a)

    assert platformmod.sweep_stale_runtime_dirs(
        roots=[str(root)], others=_nothing_running, log=FakeLog()) == []
    assert os.path.isdir(theirs)
    assert any("some other one-file application" in line for line in said), \
        said


@pytest.fixture
def a_process_list(monkeypatch):
    """The mirror of `no_windll`: pretend this machine has a process list.

    `sorters_running` branches on capability, `hasattr(ctypes, "windll")`, and
    answers "there is no process list to read here" without it -- which is the
    right answer on Ubuntu and is why the counting below never ran there. The
    snapshot is monkeypatched by the test, so the attribute only has to exist;
    nothing calls through it.
    """
    if not hasattr(platformmod.ctypes, "windll"):
        monkeypatch.setattr(platformmod.ctypes, "windll", object(),
                            raising=False)


def test_sorters_running_excludes_this_process_and_its_bootloader(
        monkeypatch, a_process_list):
    """A one-file build is two processes, so "is another sorter running?" has
    to leave out both halves of this one -- otherwise the sweep never runs in
    the only instance that is allowed to run it."""
    monkeypatch.setattr(platformmod, "_snapshot_processes",
                        lambda: [("NMS-Sorter.exe", os.getpid()),
                                 ("NMS-Sorter.exe", os.getppid()),
                                 ("explorer.exe", 4)])
    count, why = platformmod.sorters_running()
    assert count == 0, why
    monkeypatch.setattr(platformmod, "_snapshot_processes",
                        lambda: [("NMS-Sorter.exe", os.getpid()),
                                 ("NMS-Sorter.exe", 55060)])
    count, why = platformmod.sorters_running()
    assert count == 1
    assert "55060" in why
