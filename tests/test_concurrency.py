"""P6-5: two of anything. Threads, two applies, two processes, a stale lock.

GOAL.md 3.8 (the lock), 3.9 (the fingerprint) and write-path review one R1,
R2, R3 and R15. Every test here is about the *second* writer: the one that
arrives while the first is halfway through, or after the first has changed the
thing the second was told about.

Rule 4 -- "a fix reported as working was still broken on the live port, twice
in one session" -- so nothing below calls a handler method. Each test binds an
ephemeral port, serves a synthetic save out of `tmp_path` and talks to it with
`http.client`, exactly as `tests/test_server.py` does; two of them go further
and start a second `python -m nms_sorter` or a second interpreter, because a
lock file is the only state two *processes* share and a thread test cannot
prove anything about it.

What each test is for, in the order they appear:

1. two browser tabs saving the config    -> one whole document, never a mix
2. plan, change the config, apply        -> "has not printed a plan"
3. apply while a live lock exists        -> "another sorter instance"
4. the game writes during a plan (R15)   -> "changed on disk", nothing written
5. two applies at once                   -> exactly one writes
6. two processes                         -> the refusal names the other pid
7. restore against a held lock           -> refused, then exact bytes back
8. a stale lock                          -> reclaimed, and only when stale

Nothing here reads the operator's save folder: the save is built by
`tools/make_fixture.py` into a pytest temp directory and thrown away. The
subprocess tests are given their own settings, config and backup folders for
the same reason.
"""
import contextlib
import copy
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from nms_sorter import app as appmod                                # noqa: E402
from nms_sorter import codec                                        # noqa: E402
from nms_sorter import config as cfgmod                             # noqa: E402
from nms_sorter import safety                                       # noqa: E402
from nms_sorter import savemodel                                    # noqa: E402
from nms_sorter import server                                       # noqa: E402
from nms_sorter.app import App                                      # noqa: E402
from tools import make_fixture                                      # noqa: E402


# ==========================================================================
# a server, a client, and a save that is thrown away
# ==========================================================================

@contextlib.contextmanager
def running(app, **extra):
    """The server the operator gets, on an ephemeral port.

    `server.serve` hangs the app off the handler *class*, so one of these runs
    at a time within this process. That is why the two-process tests below
    drive a subprocess rather than a second `serve()`.
    """
    httpd = server.serve(app, "127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(port=port, app=app, **extra)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _save_folder(tmp_path):
    """A folder holding `save9.hg` and a valid `mf_save9.hg`, plus a config.

    The fixture builder rather than a copy of anything real, and rebuilt per
    test rather than shared: every test in this file writes to the save, and a
    session-scoped copy would make the file order-dependent.
    """
    folder = tmp_path / "saves"
    folder.mkdir()
    out = make_fixture.write_fixture(str(folder))
    return folder, out


def _app(tmp_path, folder, **kw):
    cfg_path = tmp_path / "config.json"
    backups = tmp_path / "backups"
    backups.mkdir(exist_ok=True)
    app = App(str(folder), str(cfg_path), str(backups),
              settings_path=str(tmp_path / "settings.json"),
              log_path=str(tmp_path / "sorter.log"), **kw)
    app.config = cfgmod.store(str(cfg_path), make_fixture.synthetic_config())
    app.sync_taxonomy()
    return app


#: what `safety.game_status()` answers for these tests. The write path reads
#: the process check and a check with no answer is a refusal, so the answer is
#: arranged. It used to be `allow_game_running=True` on the app, which is not a
#: state a player can be in any more.
GAME_CLOSED = {"running": False, "pids": [], "method": "test", "known": True,
               "process": "NMS.exe"}


@pytest.fixture(autouse=True)
def game_closed(monkeypatch):
    monkeypatch.setattr(safety, "game_status", lambda *a, **kw: dict(GAME_CLOSED))


@pytest.fixture
def srv(tmp_path):
    folder, built = _save_folder(tmp_path)
    app = _app(tmp_path, folder)
    with running(app, folder=folder, save=folder / make_fixture.SAVE_NAME,
                 tmp=tmp_path, built=built) as s:
        yield s


# ------------------------------------------------------------------ client

def _port_of(target):
    return target if isinstance(target, int) else target.port


def _conn(target, timeout=60):
    return http.client.HTTPConnection("127.0.0.1", _port_of(target),
                                      timeout=timeout)


def get(target, path, timeout=60):
    c = _conn(target, timeout)
    try:
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        return r.status, json.loads(body.decode("utf-8"))
    finally:
        c.close()


def post(target, path, obj=None, timeout=60):
    raw = json.dumps(obj if obj is not None else {}).encode("utf-8")
    c = _conn(target, timeout)
    try:
        c.putrequest("POST", path)
        c.putheader("Content-Type", "application/json")
        c.putheader("Content-Length", str(len(raw)))
        c.endheaders()
        c.send(raw)
        r = c.getresponse()
        body = r.read()
        ct = r.getheader("Content-Type") or ""
        assert "json" in ct, "%s answered %s with %r" % (path, ct, body[:160])
        return r.status, json.loads(body.decode("utf-8"))
    finally:
        c.close()


def refused(status, body, sentence):
    """A 409 whose one sentence carries `sentence`. The shape is §3.3's."""
    assert status == 409, (status, body)
    assert body.get("kind") == "refused", body
    # `steps` is there when the refusal carried a report: `safety.Refused`
    # holds the `Report` it refused with, so a 409 can list the steps that
    # passed before the one that did not (write-path review two, Q1).
    assert set(body) <= {"error", "kind", "where", "ref", "steps"}, body
    assert sentence in body["error"], body["error"]
    return True


def plan_of(srv):
    status, plan = post(srv, "/api/plan", {"file": str(srv.save)})
    assert status == 200, plan
    assert plan.get("fingerprint"), plan
    return plan


# ------------------------------------------------------------- small tools

def census(path):
    """Per-item totals across every sortable container in the save at `path`.

    The invariant a sort has to keep: items move, totals do not.
    """
    out = {}
    for c in savemodel.SaveFile(path).containers():
        if not c.sortable:
            continue
        for s in c.slots():
            out[s.id] = out.get(s.id, 0) + s.amount
    return out


def signature_of(path):
    """`name|size|int(mtime)|sha256`, however this build spells it.

    `app.disk_signature` is the one reader that already prefers
    `savemodel.signature_of` when it exists, so going through it keeps this
    file on the same definition as the write path the day lane B moves it.
    """
    return appmod.disk_signature(str(path))


#: a pid nothing has. Whether it can be *proved* gone depends on the kernel
#: rather than on the platform the suite pretends to be (see the same constant
#: and the same skip in `tests/test_safety.py`).
DEAD_PID = 999999999


def needs_a_provably_dead_pid():
    if safety._pid_alive(DEAD_PID):
        pytest.skip("this host cannot prove pid %d is gone, so a lock from it "
                    "is never stale" % DEAD_PID)


@contextlib.contextmanager
def a_lock_file(save, pid, age=0.0):
    """A lock file beside `save`, owned by `pid`, `age` seconds old.

    Removed in the `finally` whatever happens: a lock this process does not
    own is never unlinked by the code under test, which is the correct
    behaviour and would otherwise leak into the next test.
    """
    lp = safety.lock_path(str(save))
    with open(lp, "w", encoding="utf-8") as fh:
        json.dump({"pid": pid, "created": safety.utc_now()}, fh)
    if age:
        when = time.time() - age
        os.utime(lp, (when, when))
    try:
        yield lp
    finally:
        for leftover in (lp, lp + safety.RECLAIM_SUFFIX):
            if os.path.exists(leftover):
                try:
                    os.unlink(leftover)
                except OSError:
                    pass


def rewrite_same_size_same_mtime(path):
    """Replace the save with different bytes of the same length and mtime.

    What a tool that preserves timestamps produces, and what the game itself
    looks like to a `stat`-based cache: the name, the size and the whole-second
    mtime all survive, so only the contents say anything changed (R15). The
    edit is one digit of `Units`, because that is a same-length change to a
    field no container path names.

    -> the new bytes, or None if no candidate came out the same length on this
    build, which is a skip rather than a failure.
    """
    was = os.stat(path)
    units = savemodel.SaveFile(path).d.get(savemodel.SaveFile(path).d.player,
                                           "Units")
    if not isinstance(units, int):
        return None
    digits = len(str(abs(units)))
    for delta in (1, -1, 2, -2, 3, -3, 11, -11, 111, -111):
        cand = units + delta
        if cand < 0 or len(str(cand)) != digits:
            continue
        edited = savemodel.SaveFile(path)
        edited.d.set(edited.d.player, "Units", cand)
        framed = codec.frame_payload(codec.dumps(edited.doc))
        if len(framed) != was.st_size:
            continue
        with open(path, "wb") as fh:
            fh.write(framed)
        os.utime(path, (was.st_atime, was.st_mtime))
        now = os.stat(path)
        assert now.st_size == was.st_size and int(now.st_mtime) == int(
            was.st_mtime), "the rewrite kept the size and the second"
        return framed
    return None


# ==========================================================================
# 1. two browser tabs saving the config
# ==========================================================================

def _norm(cfg):
    """A config as it is stored, with the one volatile field dropped.

    `store()` stamps `updated` and sets `config_version`, so those two are not
    evidence of anything; everything else in the file is what the tab sent.
    """
    c = copy.deepcopy(cfg)
    c.pop("updated", None)
    c["config_version"] = cfgmod.CONFIG_VERSION
    return cfgmod.canonical_json(c)


def test_two_tabs_saving_the_config_leave_one_whole_document(srv):
    """Fifty rounds of two tabs saving different configs.

    The failure this rules out is a *mix*: `store()` writes a temp file and
    `os.replace`s it, but the temp name carries only the pid (review one, R14),
    so two threads of one process share it. Whatever the interleaving, the file
    left behind has to be one of the two documents in full -- and the `.bak`
    rotation has to stay a rotation, because it is the only copy of the
    revision before the one you just made.
    """
    a = make_fixture.synthetic_config()
    a["name"] = "tab A"
    a["sources"] = ["suit"]
    a["options"]["max_new_stacks"] = 11
    b = make_fixture.synthetic_config()
    b["name"] = "tab B"
    b["sources"] = ["suit", "ship0"]
    b["options"]["max_new_stacks"] = 22

    rounds = 50
    bad, saved = [], {"A": 0, "B": 0}
    guard = threading.Lock()

    def tab(which, doc):
        for _i in range(rounds):
            try:
                status, body = post(srv, "/api/config",
                                    {"config": copy.deepcopy(doc),
                                     "force": True})
            except Exception as exc:                # a torn connection counts
                with guard:
                    bad.append("tab %s raised %r" % (which, exc))
                return
            if status != 200 or not body.get("saved"):
                with guard:
                    bad.append("tab %s -> %s %s" % (which, status, body))
                return
            with guard:
                saved[which] += 1

    threads = [threading.Thread(target=tab, args=("A", a)),
               threading.Thread(target=tab, args=("B", b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not any(t.is_alive() for t in threads), "both tabs finished"
    assert not bad, bad[:4]
    assert saved == {"A": rounds, "B": rounds}, saved

    path = str(srv.tmp / "config.json")
    with open(path, encoding="utf-8") as fh:
        stored = json.load(fh)
    assert _norm(stored) in (_norm(a), _norm(b)), \
        ("the file is one of the two documents in full, not a mix: name=%r "
         "sources=%r max_new_stacks=%r"
         % (stored.get("name"), stored.get("sources"),
            (stored.get("options") or {}).get("max_new_stacks")))

    # the rotation: .bak.1..5 present, contiguous, each one a whole document
    slots = [n for n in range(1, cfgmod.BAK_KEEP + 1)
             if os.path.exists("%s.bak.%d" % (path, n))]
    assert slots == list(range(1, cfgmod.BAK_KEEP + 1)), \
        "every rotation slot exists with no gap: %s" % slots
    assert not os.path.exists("%s.bak.%d" % (path, cfgmod.BAK_KEEP + 1)), \
        "and the rotation does not grow past %d" % cfgmod.BAK_KEEP
    for n in slots:
        with open("%s.bak.%d" % (path, n), encoding="utf-8") as fh:
            rev = json.load(fh)                         # valid JSON, or raises
        assert _norm(rev) in (_norm(a), _norm(b)), \
            ".bak.%d is one of the two documents in full" % n
    leftovers = [fn for fn in os.listdir(str(srv.tmp)) if fn.endswith(".tmp")]
    assert not leftovers, "no temp file survives the race: %s" % leftovers


# ==========================================================================
# 2. plan, change the config, apply
# ==========================================================================

def test_a_config_change_between_the_plan_and_the_apply_refuses(srv):
    """The other tab saved the config while this one was looking at a plan.

    The plan cache is cleared by `/api/config`, so the fingerprint the page is
    still showing names a plan this server no longer stands behind -- and the
    refusal says so in the sentence about a plan it has not printed, rather
    than applying a plan computed from rules nobody approved.
    """
    before = srv.save.read_bytes()
    plan = plan_of(srv)
    other = make_fixture.synthetic_config()
    other["options"]["max_new_stacks"] = 7
    status, body = post(srv, "/api/config", {"config": other, "force": True})
    assert status == 200 and body["saved"], body

    status, body = post(srv, "/api/apply",
                        {"file": str(srv.save),
                         "fingerprint": plan["fingerprint"]})
    refused(status, body, "has not printed a plan")
    assert srv.save.read_bytes() == before, "a refused apply writes nothing"


# ==========================================================================
# 3. apply while a live lock exists
# ==========================================================================

def test_an_apply_against_a_live_lock_is_refused(srv):
    """Step 0 of the write sequence, over HTTP.

    The lock is created with *this* pid, which `pid_alive` can always prove is
    alive, so the staleness branch cannot fire and what is under test is the
    plain refusal. `tests/test_safety.py` covers the same gate at the function
    level; this one covers the route, because the 409 and the sentence are
    what the page shows.
    """
    before = srv.save.read_bytes()
    plan = plan_of(srv)
    with a_lock_file(srv.save, os.getpid()) as lp:
        status, body = post(srv, "/api/apply",
                            {"file": str(srv.save),
                             "fingerprint": plan["fingerprint"]})
        refused(status, body, "another sorter instance")
        assert str(os.getpid()) in body["error"], body["error"]
        assert os.path.exists(lp), \
            "a lock this apply did not take is not one it may remove"
    assert srv.save.read_bytes() == before, "a refused apply writes nothing"


# ==========================================================================
# 4. the game writes during a plan (R15)
# ==========================================================================

def test_a_same_size_same_second_write_during_a_plan_refuses(srv):
    """The game autosaved over the bytes the plan was drawn from.

    Same name, same size, same whole-second mtime, different contents: the
    pre-R15 cache served the old document and the signature still matched, so
    the operator could approve a plan describing bytes that were gone. The gate
    is the full signature now, so the apply refuses and the file is left
    exactly as the other writer left it.
    """
    plan = plan_of(srv)
    theirs = rewrite_same_size_same_mtime(str(srv.save))
    if theirs is None:
        pytest.skip("no same-length edit of Units on this build")

    status, body = post(srv, "/api/apply",
                        {"file": str(srv.save),
                         "fingerprint": plan["fingerprint"]})
    refused(status, body, "the save file has changed on disk")
    assert srv.save.read_bytes() == theirs, \
        "the other writer's bytes are still the ones on disk"
    assert not os.path.exists(safety.lock_path(str(srv.save))), \
        "and no lock outlives the refusal"
    assert not os.listdir(str(srv.tmp / "backups")), \
        "nothing was backed up, because nothing was going to be written"


# ==========================================================================
# 5. two applies at once
# ==========================================================================

def test_two_simultaneous_applies_leave_exactly_one_write(srv):
    """Two threads, one fingerprint, one save.

    Both are entitled to apply when they start; only one may finish. Which one
    wins is not the property under test -- "exactly one wrote, and the loser
    was told why" is. The loser may lose at any of the three gates that can
    catch it (the lock, the cleared plan cache, or the signature that the
    winner's write moved), so all three sentences are accepted; what is not
    accepted is a second success, a 500, or a save whose totals moved.
    """
    plan = plan_of(srv)
    fp = plan["fingerprint"]
    before = census(str(srv.save))
    answers = []
    guard = threading.Lock()
    start = threading.Event()

    def apply_now():
        start.wait(10)
        try:
            status, body = post(srv, "/api/apply",
                                {"file": str(srv.save), "fingerprint": fp})
        except Exception as exc:
            with guard:
                answers.append(("raised", repr(exc)))
            return
        with guard:
            answers.append((status, body))

    threads = [threading.Thread(target=apply_now) for _i in range(2)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join(timeout=120)
    assert not any(t.is_alive() for t in threads), "both applies finished"
    assert len(answers) == 2, answers

    wins = [b for s, b in answers if s == 200]
    losses = [(s, b) for s, b in answers if s != 200]
    assert len(wins) == 1, \
        "exactly one apply wrote: %s" % [(s, str(b)[:120]) for s, b in answers]
    assert len(losses) == 1, losses
    status, body = losses[0]
    assert status == 409, (status, body)
    assert any(w in body.get("error", "")
               for w in ("another sorter instance", "has not printed a plan",
                         "the save file has changed on disk")), body

    # the winner's write is a whole one: it round trips and conserves totals
    rep = safety.Report()
    safety.verify_roundtrip(str(srv.save), rep)
    assert all(s["state"] == "ok" for s in rep.steps), rep.steps
    assert census(str(srv.save)) == before, \
        "per-item totals across sortable containers are unchanged"
    assert wins[0]["result"]["rows"], "the winner actually moved something"


# ==========================================================================
# 6. two processes
# ==========================================================================

#: The holder writes its *own* pid into the ready file rather than letting the
#: test read `Popen.pid`: `sys.executable` here is a launcher shim, so the pid
#: Python hands back is the shim's and the pid in the lock is the interpreter
#: it spawned. Asserting on the wrong one of those passes for the wrong reason
#: on a machine where they happen to be the same.
HOLDER = r"""
import os, sys, time
sys.path.insert(0, sys.argv[1])
from nms_sorter import safety
target, ready, release = sys.argv[2], sys.argv[3], sys.argv[4]
with safety.held_lock(target):
    with open(ready, "w") as fh:
        fh.write(str(os.getpid()))
    deadline = time.time() + 60
    while time.time() < deadline and not os.path.exists(release):
        time.sleep(0.02)
"""


def _wait_for(path, proc, what, timeout=60):
    """Wait for a sentinel file with something in it. -> its contents.

    Non-empty, because the file appears one syscall before it is written and a
    test that read it in that window would assert against "".
    """
    end = time.time() + timeout
    while time.time() < end:
        try:
            with open(path, encoding="utf-8") as fh:
                body = fh.read().strip()
            if body:
                return body
        except OSError:
            pass
        if proc.poll() is not None:
            raise AssertionError("%s exited (%s) before %s"
                                 % (what, proc.returncode, path))
        time.sleep(0.02)
    raise AssertionError("%s never produced %s" % (what, path))


def test_a_second_process_holding_the_lock_stops_this_one(srv, tmp_path):
    """A whole other interpreter takes the lock; this server must refuse.

    The thread tests above share a `pid`, which means they cannot tell a
    correct lock from one that only looks correct because `pid_alive` is
    answering about ourselves. Here the holder is a real second process, so
    the refusal naming *its* pid is the evidence: the shared state is the
    filesystem and nothing else.

    Deviation from the ticket, recorded on purpose: the holder waits for a
    sentinel file rather than sleeping five seconds. The condition under test
    is identical and the test costs a fraction of a second instead of five.
    """
    before = srv.save.read_bytes()
    plan = plan_of(srv)
    script = tmp_path / "holder.py"
    script.write_text(HOLDER, encoding="utf-8")
    ready = tmp_path / "held.flag"
    release = tmp_path / "release.flag"
    proc = subprocess.Popen(
        [sys.executable, str(script), ROOT, str(srv.save),
         str(ready), str(release)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        holder_pid = _wait_for(str(ready), proc, "the lock holder")
        assert holder_pid != str(os.getpid()), holder_pid
        status, body = post(srv, "/api/apply",
                            {"file": str(srv.save),
                             "fingerprint": plan["fingerprint"]})
        refused(status, body, "another sorter instance")
        assert holder_pid in body["error"], \
            ("the refusal names the process holding it (pid %s): %s"
             % (holder_pid, body["error"]))
        assert srv.save.read_bytes() == before, "and nothing was written"
    finally:
        release.write_text("go", encoding="utf-8")
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    assert not os.path.exists(safety.lock_path(str(srv.save))), \
        "the holder released the lock on its way out"

    # the reverse: with the lock gone the same apply goes through
    plan = plan_of(srv)
    status, body = post(srv, "/api/apply",
                        {"file": str(srv.save),
                         "fingerprint": plan["fingerprint"]})
    assert status == 200, body
    assert srv.save.read_bytes() != before, "the apply wrote once it could"


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


@pytest.fixture
def second_sorter(tmp_path):
    """A real `python -m nms_sorter` on its own port, its own state, one save.

    `--settings`, `--config` and `--backups` all point into a second temp
    directory: two sorters sharing one save folder is the ordinary case (a
    second tab is not, a second *install* is), and the only thing they are
    meant to share is the save and the lock beside it.

    Yields a callable so the test can choose the folder it serves.
    """
    procs = []

    def start(folder):
        other = tmp_path / "process2"
        other.mkdir(exist_ok=True)
        cfg_path = other / "config.json"
        cfgmod.store(str(cfg_path), make_fixture.synthetic_config())
        port = _free_port()
        log = open(str(other / "stdout.txt"), "wb")
        proc = subprocess.Popen(
            [sys.executable, "-m", "nms_sorter", "--no-browser",
             "--folder", str(folder),
             "--port", str(port), "--settings", str(other / "settings.json"),
             "--config", str(cfg_path), "--backups", str(other / "backups"),
             "--log", "-"],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        procs.append((proc, log, other))
        end = time.time() + 60
        while time.time() < end:
            if proc.poll() is not None:
                log.flush()
                raise AssertionError(
                    "the second sorter exited (%s):\n%s"
                    % (proc.returncode,
                       (other / "stdout.txt").read_text(errors="replace")))
            try:
                status, _b = get(port, "/api/version", timeout=3)
                if status == 200:
                    return port
            except Exception:
                time.sleep(0.1)
        raise AssertionError("the second sorter never answered on %d" % port)

    try:
        yield start
    finally:
        for proc, log, _other in procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
            log.close()


def test_two_sorter_processes_cannot_both_write_one_save(tmp_path,
                                                         second_sorter):
    """The whole program, twice, against one save folder.

    No in-process server here: this is `python -m nms_sorter` refusing, so the
    gate being proved is the one an operator hits when they start the sorter
    twice -- not one this test process arranged. The lock is held by *this*
    interpreter while the subprocess is asked to apply, and released before it
    is asked again; the second answer has to be a write, or the lock would be
    a way to wedge a save rather than to protect it.
    """
    folder, _built = _save_folder(tmp_path)
    save = folder / make_fixture.SAVE_NAME
    before = save.read_bytes()
    port = second_sorter(folder)

    status, plan = post(port, "/api/plan", {"file": str(save)})
    assert status == 200, plan
    with safety.held_lock(str(save)):
        status, body = post(port, "/api/apply",
                            {"file": str(save),
                             "fingerprint": plan["fingerprint"]})
        refused(status, body, "another sorter instance")
        assert str(os.getpid()) in body["error"], body["error"]
        assert save.read_bytes() == before, "the refused process wrote nothing"

    status, plan = post(port, "/api/plan", {"file": str(save)})
    assert status == 200, plan
    # A real second process, so `game_status` is that process's own answer and
    # cannot be patched: `at_main_menu` is what a player sends, and it covers
    # both a game that is up and one that is not.
    status, body = post(port, "/api/apply",
                        {"file": str(save), "fingerprint": plan["fingerprint"],
                         "at_main_menu": True})
    if os.name == "nt":
        assert status == 200, body
        assert save.read_bytes() != before, "the lock released, the apply ran"
        assert body["result"]["rows"], body["result"]
    else:
        # Apply is Windows-only: off Windows there is no process list to read
        # and a check with no answer is a refusal. The lock is what this test
        # is about, and the sentence proves the request got past it.
        refused(status, body, "the process check needs Windows")
        assert save.read_bytes() == before


# ==========================================================================
# 7. restore against a held lock
# ==========================================================================

def test_a_restore_waits_for_the_lock_and_then_puts_the_bytes_back(srv):
    """Restore takes the same lock as apply, and puts the mtime back with it.

    Two halves, in one test because the second needs the first: a restore that
    collides with a writer is refused in apply's own sentence, and a restore
    that does not is expected to reproduce the *signature* -- name, size,
    whole-second mtime and hash -- and not merely the bytes. The signature is
    what a plan is approved against (3.9), so a restore that left `now` in the
    mtime would give back a save the pre-apply plan could no longer be
    re-approved for.
    """
    if getattr(safety, "restore", None) is None:
        pytest.skip("safety.restore has not landed yet (lane B, P2-5)")
    before = srv.save.read_bytes()
    was = signature_of(srv.save)

    plan = plan_of(srv)
    status, body = post(srv, "/api/apply",
                        {"file": str(srv.save),
                         "fingerprint": plan["fingerprint"]})
    assert status == 200, body
    folder = os.path.basename(body["result"]["backup"])
    assert srv.save.read_bytes() != before, "the apply wrote something"

    with a_lock_file(srv.save, os.getpid()):
        status, body = post(srv, "/api/restore", {"folder": folder})
        refused(status, body, "another sorter instance")
        assert srv.save.read_bytes() != before, \
            "the refused restore put nothing back"

    status, body = post(srv, "/api/restore", {"folder": folder})
    assert status == 200, body
    assert srv.save.read_bytes() == before, "the pre-apply bytes are back"
    assert signature_of(srv.save) == was, \
        ("and so is the signature the pre-apply plan was approved against:\n"
         "  before %s\n  after  %s" % (was, signature_of(srv.save)))


# ==========================================================================
# 8. a stale lock: both conditions, or neither
# ==========================================================================

def test_a_stale_lock_is_reclaimed_and_the_report_says_so(srv):
    """Dead pid and older than ten minutes: whoever took it is gone.

    The reclaim is on the report rather than only in the log, because the page
    shows the report: a write that had to step over somebody else's lock is
    not the same event as one that did not, even when both succeed.
    """
    needs_a_provably_dead_pid()
    before = srv.save.read_bytes()
    plan = plan_of(srv)
    with a_lock_file(srv.save, DEAD_PID,
                     age=safety.STALE_LOCK_SECONDS + 60) as lp:
        status, body = post(srv, "/api/apply",
                            {"file": str(srv.save),
                             "fingerprint": plan["fingerprint"]})
        assert status == 200, body
        assert body["result"]["rows"], "the apply ran"
        said = [s for s in body["steps"]
                if s["label"] == "lock" and s["state"] == "info"]
        assert said, ("the report carries the removal: %s"
                      % [(s["state"], s["label"]) for s in body["steps"]])
        assert "stale lock" in said[0]["detail"], said
        assert str(DEAD_PID) in said[0]["detail"], said
        assert not os.path.exists(lp), "and the lock is gone afterwards"
    assert srv.save.read_bytes() != before


def test_a_dead_pid_with_a_fresh_lock_is_still_refused(srv):
    """Both conditions are required, and this is the half that is easy to lose.

    A lock whose owner has just died is indistinguishable, for ten minutes,
    from one whose owner is between two syscalls -- and on Windows a pid is
    reused. So age alone is not enough and neither is liveness: a fresh lock
    from a dead pid is refused, and `--help` is not the answer either. The
    operator's way out is the ten minutes, or deleting the file.
    """
    needs_a_provably_dead_pid()
    before = srv.save.read_bytes()
    plan = plan_of(srv)
    with a_lock_file(srv.save, DEAD_PID, age=5.0) as lp:
        status, body = post(srv, "/api/apply",
                            {"file": str(srv.save),
                             "fingerprint": plan["fingerprint"]})
        refused(status, body, "another sorter instance")
        assert str(DEAD_PID) in body["error"], body["error"]
        assert os.path.exists(lp), "the lock is left where it was found"
    assert srv.save.read_bytes() == before, "and nothing was written"
