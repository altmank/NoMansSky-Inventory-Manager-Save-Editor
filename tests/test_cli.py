"""P2-1/P2-2: the command line, the Python floor and the port search.

`python -m nms_sorter` has to answer three questions before it can serve
anything: is this Python new enough, which settings file applies, and is the
port free. Each has a documented exit code (4, -, 3) and each is tested here
rather than in `test_server.py`, because none of them reaches a server.

The Python check lives in `__main__.py` *above* the import of `cli`, so it is
written as a function taking the version rather than reading
`sys.version_info`: the suite necessarily runs on a Python that passes.
"""
import io
import os
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from nms_sorter import __main__ as entry                           # noqa: E402
from nms_sorter import cli                                         # noqa: E402
from nms_sorter import platform as platformmod                     # noqa: E402
from nms_sorter import settings as settingsmod                     # noqa: E402


@pytest.fixture(autouse=True)
def _restore_logging():
    """`--log -` attaches a handler to pytest's captured stderr, which is
    closed when the test ends. Leaving it attached makes every later log call
    in the session write to a dead stream."""
    import logging
    log = logging.getLogger("nms_sorter")
    before = list(log.handlers)
    yield
    for h in list(log.handlers):
        if h not in before:
            log.removeHandler(h)
            h.close()


# ------------------------------------------------------------------ --help

def test_help_exits_zero_and_documents_every_flag():
    out = subprocess.run([sys.executable, "-m", "nms_sorter", "--help"],
                         cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    text = out.stdout
    for flag in ("--folder", "--config", "--backups", "--settings", "--host",
                 "--port", "--log", "--read-only",
                 "--strict-version-check", "--no-browser",
                 "--idle-exit-minutes"):
        assert flag in text, flag
    assert "127.0.0.1" in text


def test_help_does_not_need_a_settings_file_or_a_save_folder(tmp_path):
    """`--help` used to be the one command that could still fail on a machine
    with nothing set up (D24 was this shape). It must not read anything."""
    env = dict(os.environ, LOCALAPPDATA=str(tmp_path / "nothing-here"))
    out = subprocess.run([sys.executable, "-m", "nms_sorter", "--help"],
                         cwd=ROOT, capture_output=True, text=True, env=env,
                         timeout=120)
    assert out.returncode == 0, out.stderr
    assert not (tmp_path / "nothing-here").exists()


# ----------------------------------------------------- exit 4: old Python

def test_an_old_python_is_one_sentence_and_exit_4():
    buf = io.StringIO()
    assert entry.check_python((3, 8, 10), out=buf) == 4
    said = buf.getvalue()
    assert said.count("\n") == 1                  # one sentence, one line
    assert "3.9" in said and "3.8.10" in said
    assert "https://www.python.org/downloads/" in said
    assert "Traceback" not in said


def test_the_python_check_reads_sys_version_info_by_default(monkeypatch):
    monkeypatch.setattr(sys, "version_info", (3, 8, 0, "final", 0))
    buf = io.StringIO()
    assert entry.check_python(out=buf) == 4
    assert "3.8.0" in buf.getvalue()


@pytest.mark.parametrize("version", [(3, 9, 0), (3, 13, 2), (4, 0, 0)])
def test_a_new_enough_python_says_nothing(version):
    buf = io.StringIO()
    assert entry.check_python(version, out=buf) == 0
    assert buf.getvalue() == ""


def test_the_floor_matches_pyproject():
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        text = fh.read()
    assert 'requires-python = ">=%d.%d"' % entry.MIN_PYTHON in text


def test_main_is_not_imported_until_the_check_passes():
    """The point of the check: on 3.8 the modules below cannot even be parsed,
    so `__main__` must not import them at module level."""
    import ast
    with open(os.path.join(ROOT, "nms_sorter", "__main__.py"),
              encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert [a.name for n in top for a in n.names] == ["sys"], ast.dump(top[0])


# -------------------------------------------------- exit 3: no free port

def test_a_busy_port_moves_to_the_next_free_one(monkeypatch):
    busy = {8765, 8766, 8767}
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: port in busy)
    port, tried = cli.choose_port("127.0.0.1", 8765)
    assert port == 8768
    assert tried == [8765, 8766, 8767]


def test_a_free_port_is_used_as_is(monkeypatch):
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: False)
    assert cli.choose_port("127.0.0.1", 8765) == (8765, [])


def test_all_eleven_ports_busy_gives_up(monkeypatch):
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: True)
    port, tried = cli.choose_port("127.0.0.1", 8765)
    assert port is None
    assert tried == list(range(8765, 8776))       # the wanted one plus ten


def test_the_search_stops_at_the_top_of_the_port_range(monkeypatch):
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: True)
    port, tried = cli.choose_port("127.0.0.1", 65530)
    assert port is None
    assert tried[-1] == 65535


def _temp_args(tmp_path, *extra):
    return ["--no-browser", "--log", "-",
            "--settings", str(tmp_path / "settings.json"),
            "--config", str(tmp_path / "config.json"),
            "--backups", str(tmp_path / "backups")] + list(extra)


def _watch_browser_threads(monkeypatch):
    """Collect the browser threads `cli.main` starts. -> the list, to join.

    A test that lets `main` start its browser thread and then ends leaves that
    thread polling a port for three seconds with the real `webbrowser.open`
    restored behind it, because `monkeypatch` is rolled back at teardown. That
    is exactly how a full suite run used to put two tabs on the operator's
    desktop, on ports nothing was listening on. A test that reaches the
    serving path without `--no-browser` joins what this collects before it
    returns; `main` sets the thread's cancel event on the way out, so the join
    costs a millisecond.

    Only `open_when_ready` threads: the idle watchdog is started
    unconditionally and runs until the server stops, so joining that one would
    be a ten-second wait for nothing.
    """
    started = []
    real_thread = cli.threading.Thread

    def watched(*a, **kw):
        t = real_thread(*a, **kw)
        if kw.get("target") is cli.open_when_ready:
            started.append(t)
        return t

    monkeypatch.setattr(cli.threading, "Thread", watched)
    return started


def _join_browser_threads(threads, timeout=10):
    for t in threads:
        t.join(timeout=timeout)
        assert not t.is_alive(), "the browser thread outlived the test"


@pytest.fixture
def free_lock(monkeypatch):
    """Take the single-instance machinery out of a test that is not about it.

    `cli.main` takes `acquire_single_instance` *before* it probes the port,
    and that order is the fix for W3 -- so a sorter already holding the mutex
    for this port makes `main` take the second-launch path, print "a sorter is
    already running at ... ; opening that instead" and return 0 without ever
    calling `server.serve`. For a test about a banner line or a flag that is
    `KeyError: 'app'` on a machine where nothing is wrong, and per CLAUDE.md
    the owner is playing -- and running the sorter -- in every session.

    Pinning `port_answers` is not enough: the mutex is per port, so a chosen
    port can be free of a listener and not free of a lock, and probing both is
    a race. The tests that *are* about the lock fake it exactly like this.
    """
    monkeypatch.setattr(
        platformmod, "acquire_single_instance",
        lambda port: platformmod.InstanceLock(port, True, "faked-by-the-suite",
                                              "test-lock-%d" % port))


#: `main` probes a busy port to see whether a sorter is already there (P4-3),
#: which is a real network call. The port-search tests below hard-code 8795,
#: and a sorter answering on 8795 on the machine running the suite -- which
#: happened, another lane had a dev server there -- turned both of them from
#: "the port search works" into "the environment is quiet". They pin the probe;
#: `test_a_second_sorter_opens_the_first_instead_of_starting` is where the
#: probe itself is exercised, against a listener it owns.
def _no_sorter_anywhere(monkeypatch):
    monkeypatch.setattr(cli, "sorter_at", lambda host, port, **kw: None)


def test_main_returns_3_when_no_port_is_free(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: True)
    _no_sorter_anywhere(monkeypatch)
    code = cli.main(_temp_args(tmp_path, "--port", "8795"))
    assert code == 3
    said = capsys.readouterr().out
    assert "8795" in said
    assert "10 ports after it" in said
    assert "--port" in said                       # the fix is in the sentence


def test_main_serves_the_next_port_when_the_wanted_one_is_busy(
        tmp_path, monkeypatch, capsys):
    """The exit-3 sentence is the last resort, not the first answer: one busy
    port moves to the next and says so (P2-2 (e))."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: port == 8795)
    _no_sorter_anywhere(monkeypatch)
    served = {}

    class FakeServer(object):
        def serve_forever(self):
            raise KeyboardInterrupt

    def fake_serve(app, host, port):
        served["app"], served["host"], served["port"] = app, host, port
        return FakeServer()

    monkeypatch.setattr(cli.server, "serve", fake_serve)
    code = cli.main(_temp_args(tmp_path, "--port", "8795"))
    assert code == 0
    assert served["port"] == 8796
    said = capsys.readouterr().out
    assert "8795 was busy; using port 8796 instead" in said
    assert "http://127.0.0.1:8796/" in said


# --------------------------------------------- P4-3: two instances

class _Listener(object):
    """A real socket on a real port, answering a fixed HTTP response.

    A real listener rather than a monkeypatched `urlopen`, because the two
    things being tested are exactly the ones a fake would paper over: that a
    busy port is probed at all, and that the probe does not mistake something
    else for a sorter.
    """

    def __init__(self, body, status="200 OK", ctype="application/json"):
        import socket
        import threading
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self._resp = (
            ("HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
             "Connection: close\r\n\r\n" % (status, ctype, len(raw)))
            .encode("ascii") + raw)
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.port = self.srv.getsockname()[1]
        self.srv.listen(8)
        self.hits = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            try:
                self.hits.append(conn.recv(4096).decode("latin-1"))
                conn.sendall(self._resp)
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        """Stop answering, on both platforms.

        `close()` alone is enough on Windows, where closing a socket aborts
        the `accept()` another thread is blocked in. On Linux the blocked call
        keeps the listening socket alive -- the kernel holds it for the
        duration of the syscall -- so the port went on accepting connections
        and answering them after the test had closed it, and
        `test_sorter_at_is_none_when_nothing_answers` got a sorter's reply
        from a listener that was supposed to be gone. `shutdown` is what wakes
        the `accept`; it is Windows that raises here, not Linux.
        """
        import socket
        try:
            self.srv.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.srv.close()
        except OSError:
            pass
        self.thread.join(timeout=5)


@pytest.fixture
def listener():
    made = []

    def make(body, **kw):
        it = _Listener(body, **kw)
        made.append(it)
        return it
    yield make
    for it in made:
        it.close()


def test_sorter_at_recognises_a_sorter_by_its_version_payload(listener):
    it = listener('{"app": "1.2.3", "api": 1}')
    got = cli.sorter_at("127.0.0.1", it.port)
    assert got["app"] == "1.2.3"
    assert "GET /api/version" in it.hits[0]


def test_sorter_at_refuses_to_recognise_anything_else(listener):
    """8765 is not ours. Something else listening there must not be opened in
    a browser and called a sorter."""
    assert cli.sorter_at("127.0.0.1", listener("hello", ctype="text/plain").port) is None
    assert cli.sorter_at("127.0.0.1", listener('{"api": 1}').port) is None, \
        "no app field: not a sorter"
    assert cli.sorter_at("127.0.0.1", listener("[1, 2, 3]").port) is None
    assert cli.sorter_at("127.0.0.1", listener('{"app": ""}').port) is None
    assert cli.sorter_at("127.0.0.1",
                         listener('{"app": "1"}', status="503 Nope").port) is None


def test_sorter_at_is_none_when_nothing_answers():
    it = _Listener('{"app": "1"}')
    port = it.port
    it.close()
    assert cli.sorter_at("127.0.0.1", port, timeout=0.5) is None


def test_probe_host_turns_a_wildcard_bind_into_a_clickable_url():
    assert cli.probe_host("0.0.0.0") == "127.0.0.1"
    assert cli.probe_host("") == "127.0.0.1"
    assert cli.probe_host("::") == "127.0.0.1"
    assert cli.probe_host("127.0.0.1") == "127.0.0.1"
    assert cli.probe_host("192.168.1.9") == "192.168.1.9"


def test_a_second_sorter_opens_the_first_instead_of_starting(
        tmp_path, monkeypatch, capsys, listener, no_browser_env_off):
    """The case worth a round trip to avoid: two sorters on one save folder.
    Both would mint plans and hold a config; the second one's Apply would be
    refused by the lock only after it had been configured (GOAL.md P4-3)."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    it = listener('{"app": "0.1.0", "api": 1, "data_version": "25233815"}')
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda u: opened.append(u))
    monkeypatch.setattr(cli.server, "serve", _never_served)

    # no --no-browser here: opening the first instance is half the behaviour
    code = cli.main(["--log", "-", "--port", str(it.port),
                     "--settings", str(tmp_path / "settings.json"),
                     "--config", str(tmp_path / "config.json"),
                     "--backups", str(tmp_path / "backups")])
    assert code == 0
    said = capsys.readouterr().out
    assert ("a sorter is already running at http://127.0.0.1:%d/ ; opening "
            "that instead" % it.port) in said
    assert opened == ["http://127.0.0.1:%d/?notice=second-launch" % it.port], \
        "the page is the only place a windowed build can say this (W3)"


def test_the_already_running_path_respects_no_browser(
        tmp_path, monkeypatch, capsys, listener):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    it = listener('{"app": "0.1.0"}')
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda u: opened.append(u))
    monkeypatch.setattr(cli.server, "serve", _never_served)
    # _temp_args already passes --no-browser
    assert cli.main(_temp_args(tmp_path, "--port", str(it.port))) == 0
    assert "already running" in capsys.readouterr().out
    assert opened == []


def test_the_env_guard_reads_only_an_exact_1(monkeypatch):
    """`=0` and `=false` read as "off" to anybody who types them, and a guard
    that treated them as "on" would be the surprise it exists to prevent."""
    for value, want in [("1", True), (" 1 ", True), ("0", False),
                        ("false", False), ("", False), ("2", False),
                        ("yes", False)]:
        assert cli.browser_suppressed({cli.NO_BROWSER_ENV: value}) is want, \
            value
    assert cli.browser_suppressed({}) is False
    monkeypatch.setenv(cli.NO_BROWSER_ENV, "1")
    assert cli.browser_suppressed() is True


def test_the_env_guard_stops_the_second_launch_opening_a_tab(
        tmp_path, monkeypatch, capsys, listener):
    """Belt and braces over `--no-browser`. The flag has to be remembered on
    every launch, and the launch that hurts is the one made with the fewest
    arguments: a second copy started without it reads the *first* instance's
    settings, where `open_browser` is true, and opens a tab on the operator's
    desktop. One variable covers every launch from that environment."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    monkeypatch.setenv(cli.NO_BROWSER_ENV, "1")
    it = listener('{"app": "0.1.0", "api": 1}')
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda u: opened.append(u))
    monkeypatch.setattr(cli.server, "serve", _never_served)
    # Deliberately no --no-browser: the environment is the only guard here.
    code = cli.main(["--log", "-", "--port", str(it.port),
                     "--settings", str(tmp_path / "settings.json"),
                     "--config", str(tmp_path / "config.json"),
                     "--backups", str(tmp_path / "backups")])
    assert code == 0
    said = capsys.readouterr().out
    assert "already running" in said, said
    assert opened == [], "the environment said not to open a browser"


def test_the_env_guard_stops_the_serving_launch_opening_a_tab(
        tmp_path, monkeypatch, capsys):
    """The other path, and it is a different one: the instance that wins the
    lock opens its *own* page from a thread, so a guard only in the
    second-launch branch would still put a tab on the screen."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    monkeypatch.setenv(cli.NO_BROWSER_ENV, "1")
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: False)
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda u: opened.append(u))
    threads = []
    real_thread = cli.threading.Thread

    def watched(*a, **kw):
        t = real_thread(*a, **kw)
        threads.append(t)
        return t

    monkeypatch.setattr(cli.threading, "Thread", watched)

    class FakeServer(object):
        def serve_forever(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.server, "serve", lambda app, h, p: FakeServer())
    # No --no-browser, so `open_browser` stays true and the thread is started.
    assert cli.main(["--log", "-", "--port", "8796",
                     "--settings", str(tmp_path / "settings.json"),
                     "--config", str(tmp_path / "config.json"),
                     "--backups", str(tmp_path / "backups")]) == 0
    capsys.readouterr()
    for t in threads:
        t.join(timeout=10)
    assert opened == [], "the environment said not to open a browser"


def test_open_when_ready_does_not_even_poll_under_the_env_guard(monkeypatch):
    """Not just "does not open": under the guard there is nothing at the end
    of the three-second poll to wait for, so it does not wait."""
    monkeypatch.setenv(cli.NO_BROWSER_ENV, "1")
    monkeypatch.setattr(cli.webbrowser, "open", lambda u: pytest.fail(
        "opened a browser with %s=1" % cli.NO_BROWSER_ENV))
    monkeypatch.setattr(cli, "port_answers", lambda *a, **k: pytest.fail(
        "polled the port with nothing to do at the end of it"))
    assert cli.open_when_ready("http://127.0.0.1:1/", "127.0.0.1", 1) is False


def test_open_in_browser_is_the_only_call_to_webbrowser():
    """One funnel, because the two call sites are reached by different paths
    and a guard that has to be repeated is one that gets missed."""
    with io.open(cli.__file__, encoding="utf-8") as fh:
        src = fh.read()
    calls = [line.strip() for line in src.splitlines()
             if "webbrowser.open(" in line and not line.strip().startswith("#")]
    assert calls == ["webbrowser.open(url)"], calls


#: flags that make `python -m nms_sorter` print and exit, so they never reach
#: a bind, a serve loop or a browser.
_NEVER_SERVES = ("--help", "--version")


def test_every_subprocess_launch_of_the_sorter_passes_no_browser():
    """The suite's own argv lists, checked as source.

    A test that spawns `python -m nms_sorter` without `--no-browser` opens a
    page on the desktop of whoever ran the suite: the child reads the *first*
    instance's settings, where `open_browser` is true. Both current call sites
    pass. This is here to keep the third one honest, because the failure is a
    tab on somebody's screen and not a red test.
    """
    import ast

    bad = []
    for name in sorted(os.listdir(HERE)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(HERE, name)
        with io.open(path, encoding="utf-8") as fh:
            text = fh.read()
        for node in ast.walk(ast.parse(text, path)):
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            # None for anything that is not a literal string, so "-m" and
            # "nms_sorter" have to be *adjacent* in the real argv, not merely
            # both present somewhere in it.
            words = [e.value if isinstance(e, ast.Constant)
                     and isinstance(e.value, str) else None
                     for e in node.elts]
            spawn = any(words[i] == "-m" and words[i + 1] == "nms_sorter"
                        for i in range(len(words) - 1))
            if not spawn:
                continue
            if "--no-browser" in words:
                continue
            if any(f in words for f in _NEVER_SERVES):
                continue
            bad.append("%s:%d" % (name, node.lineno))
    assert bad == [], ("these spawn python -m nms_sorter without "
                       "--no-browser: %s" % ", ".join(bad))


def test_something_else_on_the_port_still_steps_to_the_next_one(
        tmp_path, monkeypatch, capsys, listener):
    """The next-ten behaviour is not replaced, only preceded. A busy port that
    is not a sorter is somebody else's server and moving over it is right."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    it = listener("not a sorter", ctype="text/plain")
    served = {}

    class FakeServer(object):
        def serve_forever(self):
            raise KeyboardInterrupt

    def fake_serve(app, host, port):
        served["port"] = port
        return FakeServer()

    monkeypatch.setattr(cli.server, "serve", fake_serve)
    code = cli.main(_temp_args(tmp_path, "--port", str(it.port)))
    assert code == 0
    assert served["port"] != it.port
    said = capsys.readouterr().out
    assert "was busy; using port %d instead" % served["port"] in said
    assert "already running" not in said


def test_the_probe_asks_about_the_wanted_port_only(tmp_path, monkeypatch,
                                                   capsys):
    """A free wanted port is not probed at all: no round trip on the happy
    path, which is every normal start."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: False)
    asked = []
    monkeypatch.setattr(cli, "sorter_at",
                        lambda h, p, **k: asked.append(p) or None)

    class FakeServer(object):
        def serve_forever(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.server, "serve",
                        lambda app, host, port: FakeServer())
    assert cli.main(_temp_args(tmp_path, "--port", "8795")) == 0
    assert asked == []
    capsys.readouterr()


def test_no_free_port_and_no_sorter_is_still_exit_3(tmp_path, monkeypatch,
                                                    capsys):
    """The two-instance answer must not swallow the no-port refusal: a wanted
    port held by something that is not a sorter, with ten more behind it, is
    still "stop it or pass --port"."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: True)
    monkeypatch.setattr(cli, "sorter_at", lambda h, p, **k: None)
    assert cli.main(_temp_args(tmp_path, "--port", "8795")) == 3
    assert "10 ports after it" in capsys.readouterr().out


# ------------------------------------------- P2-8: the frozen windowed build

def test_main_does_not_swallow_exceptions_in_a_source_run(monkeypatch):
    """A traceback in a terminal is the most useful thing that can happen
    there, so the catch-all is deliberately not armed off the frozen path."""
    def boom(argv=None):
        raise RuntimeError("from _main")
    monkeypatch.setattr(cli, "_main", boom)
    with pytest.raises(RuntimeError):
        cli.main([])


def test_a_frozen_windowed_build_reports_instead_of_dying_silently(
        monkeypatch, caplog):
    """`NMS-Sorter.exe` has nowhere visible to print: an unhandled traceback
    there means the operator double-clicks and nothing happens at all. Every
    exception is caught, logged, and shown in a message box.

    The predicate is `streams_visible()`, not "is there a console window": the
    console build has no window when its output is redirected, and swallowing
    its traceback in favour of a dialog would be a downgrade.

    `message_box` is patched, never called: it is a modal `MessageBoxW` and a
    real one blocks the suite until somebody clicks OK."""
    import logging
    shown = []
    monkeypatch.setattr(cli.platformmod, "is_frozen", lambda: True)
    monkeypatch.setattr(cli.platformmod, "streams_visible", lambda: False)
    monkeypatch.setattr(cli.platformmod, "message_box",
                        lambda t, m: shown.append((t, m)) or True)

    def boom(argv=None):
        raise RuntimeError("the config folder is on a dead network drive")

    monkeypatch.setattr(cli, "_main", boom)
    cli._last_log_path[0] = r"C:\state\logs\sorter.log"
    with caplog.at_level(logging.ERROR, logger="nms_sorter"):
        assert cli.main([]) == 1
    assert shown, "a windowed failure must be visible"
    title, text = shown[0]
    assert "could not start" in title
    assert "dead network drive" in text
    assert r"C:\state\logs\sorter.log" in text
    assert "NMS-Sorter-console.exe" in text, "say how to see more"
    assert any("failed to start" in r.message for r in caplog.records)
    assert any(r.exc_info for r in caplog.records), "the traceback is logged"


def test_a_frozen_windowed_build_still_honours_sys_exit(monkeypatch):
    """`SystemExit` is the normal way out and must not become exit code 1."""
    monkeypatch.setattr(cli.platformmod, "is_frozen", lambda: True)
    monkeypatch.setattr(cli.platformmod, "streams_visible", lambda: False)
    monkeypatch.setattr(cli.platformmod, "message_box",
                        lambda t, m: pytest.fail("no dialog for a clean exit"))

    def leave(argv=None):
        raise SystemExit(0)

    monkeypatch.setattr(cli, "_main", leave)
    with pytest.raises(SystemExit):
        cli.main([])


def test_state_dir_comes_from_platform_now():
    """One implementation, in the one module allowed to branch on the OS."""
    from nms_sorter import platform as platformmod
    assert cli.state_dir is platformmod.state_dir
    assert settingsmod.state_dir is platformmod.state_dir


def _never_served(app, host, port):
    raise AssertionError("a second server must not be started")


def test_main_returns_3_when_the_bind_itself_fails(tmp_path, monkeypatch,
                                                   capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: False)

    def boom(app, host, port):
        raise OSError(10013, "an attempt was made to access a socket")

    monkeypatch.setattr(cli.server, "serve", boom)
    assert cli.main(_temp_args(tmp_path, "--port", "8795")) == 3
    assert "could not be opened" in capsys.readouterr().out


# ------------------------------------------------- flags, settings, paths

def test_flags_are_in_force_for_the_run_and_not_written_back(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    spath = tmp_path / "settings.json"
    settingsmod.store(str(spath), settingsmod.Settings(port=8100,
                                                       read_only=False))
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: False)
    seen = {}

    class FakeServer(object):
        def serve_forever(self):
            raise KeyboardInterrupt

    def fake_serve(app, host, port):
        seen["app"], seen["port"] = app, port
        return FakeServer()

    monkeypatch.setattr(cli.server, "serve", fake_serve)
    code = cli.main(["--no-browser", "--log", "-", "--read-only",
                     "--port", "8796",
                     "--settings", str(spath),
                     "--config", str(tmp_path / "config.json"),
                     "--backups", str(tmp_path / "backups")])
    assert code == 0
    assert seen["port"] == 8796
    assert seen["app"].read_only is True
    assert "READ ONLY" in capsys.readouterr().out
    # the file is untouched: a flag is a flag (GOAL.md §3.6)
    import json
    with open(str(spath), encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert on_disk["port"] == 8100 and on_disk["read_only"] is False


def test_the_version_gate_flag_is_in_force_for_the_run_and_not_written_back(
        tmp_path, monkeypatch, capsys, free_lock):
    """`--strict-version-check` is in force for the run, the banner says
    which way the gate is set, and the settings file is not touched (GOAL.md
    3.6)."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    spath = tmp_path / "settings.json"
    settingsmod.store(str(spath),
                      settingsmod.Settings(port=8100,
                                           strict_version_check=False))
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: False)
    seen = {}

    class FakeServer(object):
        def serve_forever(self):
            raise KeyboardInterrupt

    def fake_serve(app, host, port):
        seen["app"] = app
        return FakeServer()

    monkeypatch.setattr(cli.server, "serve", fake_serve)
    code = cli.main(["--no-browser", "--log", "-", "--strict-version-check",
                     "--port", "8797",
                     "--settings", str(spath),
                     "--config", str(tmp_path / "config.json"),
                     "--backups", str(tmp_path / "backups")])
    assert code == 0
    assert seen["app"].strict_version_check is True
    assert "save-version check STRICT" in capsys.readouterr().out
    import json
    with open(str(spath), encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert on_disk["strict_version_check"] is False,         "a flag is a flag: the file keeps what it said"


def test_the_banner_says_the_version_gate_is_off_by_default(tmp_path,
                                                            monkeypatch,
                                                            capsys,
                                                            free_lock):
    """The game-running line only appears when that gate is off, and the same
    rule applied here would leave the common case unstated: the version gate is
    off unless it is asked for, so the banner says so on an ordinary run."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: False)

    class FakeServer(object):
        def serve_forever(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.server, "serve",
                        lambda app, host, port: FakeServer())
    assert cli.main(_temp_args(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "save-version check DISABLED" in out, out
    assert "--strict-version-check" in out, out


def test_the_log_path_the_app_reports_is_the_file_being_written(tmp_path,
                                                                monkeypatch):
    """`GET /api/log` reads `app.log_path`, so that attribute has to name the
    file the logger actually opened -- and `--log -` has to leave it None,
    which is what the route turns into a 404 with a sentence rather than an
    empty list of lines."""
    import logging
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    path = cli.setup_logging(str(tmp_path / "logs" / "sorter.log"))
    assert path == str(tmp_path / "logs" / "sorter.log")
    logging.getLogger("nms_sorter").info("a line to prove it opened")
    for h in logging.getLogger("nms_sorter").handlers:
        h.flush()
    assert os.path.isfile(path)
    with io.open(path, encoding="utf-8") as fh:
        assert "a line to prove it opened" in fh.read()
    assert cli.setup_logging("-") is None


def test_the_state_dir_is_localappdata(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert cli.state_dir() == os.path.join(str(tmp_path), "NMS-Sorter")
    assert cli.default_log_path() == os.path.join(
        str(tmp_path), "NMS-Sorter", "logs", "sorter.log")
    assert cli.state_dir is settingsmod.state_dir


# ===================================================== the idle exit
#
# A fake clock throughout: the thing being tested is measured in minutes, and a
# test that waits for one is a test nobody runs. `idle_watchdog` takes its
# clock, its sleep and its busy predicate as arguments for exactly this.

class _FakeClock(object):
    """A sleep that advances a clock instead of waiting, with a budget.

    The budget is what keeps a watchdog that never fires from hanging the
    suite: once it is spent the sleep raises, which is also how "it did not
    fire" is asserted below.
    """

    class Spent(Exception):
        pass

    def __init__(self, ticks=1000):
        self.now = 0.0
        self.last_request = 0.0
        self.ticks = ticks
        self.slept = 0

    def sleep(self, seconds):
        if self.slept >= self.ticks:
            raise self.Spent("the watchdog is still ticking after %d ticks"
                             % self.slept)
        self.slept += 1
        self.now += seconds

    def idle(self):
        return self.now - self.last_request

    def request(self):
        self.last_request = self.now


def test_the_idle_watchdog_fires_after_the_minutes_it_was_given():
    clock = _FakeClock()
    stopped = []
    idled = cli.idle_watchdog(30, stopped.append, idle=clock.idle,
                              sleep=clock.sleep, tick=5.0)
    assert stopped == [30], "and it names the number it fired at"
    assert idled >= 30 * 60
    # the tick before the deadline must not have fired: 30 minutes means 30
    assert idled < 30 * 60 + 5.0


def test_a_request_pushes_the_idle_exit_back():
    """The property the whole feature rests on: the page polls every 15
    seconds, so an open tab can never idle out."""
    clock = _FakeClock()
    stopped = []

    def idle():
        # a request every minute of fake time, up to twenty minutes past the
        # limit, and then nothing
        if clock.now < 50 * 60 and clock.now - clock.last_request >= 60:
            clock.request()
        return clock.idle()

    idled = cli.idle_watchdog(10, stopped.append, idle=idle,
                              sleep=clock.sleep, tick=5.0)
    assert stopped == [10]
    assert clock.now > 50 * 60, \
        "it cannot have fired while something was still asking (%s)" % clock.now
    assert idled >= 10 * 60


def test_a_write_in_progress_defers_the_idle_exit():
    """An apply is not a reason to cancel the exit, and not a reason to be
    stopped mid-write either: it defers the decision one tick at a time."""
    clock = _FakeClock()
    stopped = []
    busy = {"until": 45 * 60.0}
    idled = cli.idle_watchdog(10, stopped.append, idle=clock.idle,
                              busy=lambda: clock.now < busy["until"],
                              sleep=clock.sleep, tick=5.0)
    assert stopped == [10]
    assert idled >= busy["until"], \
        "the exit waited for the write, not for the clock (%s)" % idled


def test_a_write_that_never_ends_never_gets_an_idle_exit():
    clock = _FakeClock(ticks=200)
    stopped = []
    with pytest.raises(_FakeClock.Spent):
        cli.idle_watchdog(1, stopped.append, idle=clock.idle,
                          busy=lambda: True, sleep=clock.sleep, tick=5.0)
    assert stopped == [], "a write in progress is never stopped underneath"


def test_zero_minutes_never_fires_and_costs_nothing():
    clock = _FakeClock()
    stopped = []
    assert cli.idle_watchdog(0, stopped.append, idle=clock.idle,
                             sleep=clock.sleep) is None
    assert stopped == [] and clock.slept == 0, \
        "off means off: no thread work, no clock read"


def test_a_setting_of_zero_read_on_every_tick_keeps_it_running():
    """The setting is read live, so the watchdog is started even when the exit
    is off -- turning it on from the Settings tab must not need a restart."""
    clock = _FakeClock(ticks=100)
    stopped = []
    with pytest.raises(_FakeClock.Spent):
        cli.idle_watchdog(lambda: 0, stopped.append, idle=clock.idle,
                          sleep=clock.sleep, tick=5.0)
    assert stopped == []
    # and the same watchdog fires once the setting is turned on
    clock = _FakeClock()
    setting = {"minutes": 0}

    def minutes():
        if clock.now > 60:
            setting["minutes"] = 1
        return setting["minutes"]

    idled = cli.idle_watchdog(minutes, stopped.append, idle=clock.idle,
                              sleep=clock.sleep, tick=5.0)
    assert stopped == [1] and idled >= 60


def test_idle_minutes_comes_off_the_live_settings():
    from types import SimpleNamespace
    app = SimpleNamespace(settings=settingsmod.Settings(idle_exit_minutes=7))
    assert cli.idle_minutes_of(app) == 7
    app.settings.idle_exit_minutes = 0
    assert cli.idle_minutes_of(app) == 0
    assert cli.idle_minutes_of(SimpleNamespace()) == 0


def test_the_banner_names_all_three_ways_to_stop_it():
    """The line exists because the windowed build has no console: "ctrl-c
    here" is not an answer for somebody who double-clicked an exe."""
    said = cli.idle_banner(30)
    assert said == ("stop     from the Settings section, or ctrl-c here; "
                    "idle exit after 30 minutes")
    off = cli.idle_banner(0)
    assert off.startswith("stop     from the Settings section, or ctrl-c here;")
    assert "no idle exit" in off and "30" not in off


def test_the_banner_line_is_printed_at_startup(tmp_path, monkeypatch, capsys,
                                               free_lock):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: False)

    class FakeServer(object):
        def serve_forever(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.server, "serve",
                        lambda app, host, port: FakeServer())
    assert cli.main(_temp_args(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "stop     from the Settings section, or ctrl-c here" in out, out
    assert "idle exit after 30 minutes" in out, out


def test_the_idle_flag_is_in_force_for_the_run_and_not_written_back(
        tmp_path, monkeypatch, capsys, free_lock):
    """Same rule as every other flag (GOAL.md 3.6), and the one case where 0 is
    a value rather than "not passed"."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    spath = tmp_path / "settings.json"
    settingsmod.store(str(spath),
                      settingsmod.Settings(idle_exit_minutes=30))
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: False)
    seen = {}

    class FakeServer(object):
        def serve_forever(self):
            raise KeyboardInterrupt

    def fake_serve(app, host, port):
        seen["app"] = app
        return FakeServer()

    monkeypatch.setattr(cli.server, "serve", fake_serve)
    code = cli.main(["--no-browser", "--log", "-", "--idle-exit-minutes", "0",
                     "--settings", str(spath),
                     "--config", str(tmp_path / "config.json"),
                     "--backups", str(tmp_path / "backups")])
    assert code == 0
    assert seen["app"].settings.idle_exit_minutes == 0
    assert "no idle exit" in capsys.readouterr().out
    import json
    with open(str(spath), encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert on_disk["idle_exit_minutes"] == 30, \
        "a flag is a flag: the file keeps what it said"


@pytest.mark.parametrize("visible,printed", [(True, 1), (False, 0)])
def test_the_idle_exit_says_so_once_wherever_it_can_be_read(monkeypatch, capsys,
                                                            visible, printed):
    """Logged always, printed only where a print reaches a person.

    A windowed build's `print` is a writer that forwards to this very log
    (`platform.ensure_streams`), so printing unconditionally wrote the line
    into the file twice -- observed in the frozen build. The console build and
    a source run still get it on stdout, where it is the only sign the window
    is about to end.
    """
    said = []

    class FakeLog(object):
        def info(self, msg, *a):
            said.append(msg % a if a else msg)

        def exception(self, msg, *a):
            raise AssertionError(msg)

    class FakeServer(object):
        shut = False

        def shutdown(self):
            self.shut = True

    monkeypatch.setattr(cli.platformmod, "streams_visible", lambda: visible)
    monkeypatch.setattr(cli, "idle_watchdog",
                        lambda minutes, stop, **kw: stop(7))
    httpd = FakeServer()
    cli.start_idle_watchdog(httpd, None, FakeLog()).join(timeout=5)
    assert said == ["stopped after 7 idle minutes"]
    assert httpd.shut is True
    out = capsys.readouterr().out
    assert out.count("stopped after 7 idle minutes") == printed, out


def test_the_watchdog_thread_survives_a_broken_clock(tmp_path, monkeypatch):
    """A watchdog that raised would take its own thread down and leave a
    server nobody can stop -- invisible in the build with no console, which is
    the one this is for. So the thread body is guarded and says so in the log.
    """
    logs = []

    class FakeLog(object):
        def info(self, *a):
            pass

        def exception(self, msg, *a):
            logs.append(msg)

    class FakeServer(object):
        shut = False

        def shutdown(self):
            self.shut = True

    monkeypatch.setattr(cli, "idle_watchdog",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            RuntimeError("the clock went backwards")))
    httpd = FakeServer()
    t = cli.start_idle_watchdog(httpd, None, FakeLog())
    t.join(timeout=5)
    assert not t.is_alive()
    assert logs and "idle watchdog" in logs[0]
    assert httpd.shut is False, "and it did not stop the server on the way out"


# ===========================================================================
# W3: three double-clicks, one sorter
# ===========================================================================
#
# The review's reproduction: three fast launches from a clean state produced
# three `LISTENING` rows on 127.0.0.1:8765, three processes and three `%TEMP%`
# extractions, and ending one task left the page answering from a survivor --
# so Task Manager appeared not to work. The probe above is advisory and always
# was; what makes it exclusive is a lock taken *before* it and a bind that a
# second process cannot win.


def test_the_lock_is_taken_before_anything_is_probed(tmp_path, monkeypatch,
                                                     capsys):
    """Order is the fix. A probe cannot see a server that has not bound yet,
    which is why three launches in the same millisecond all passed it."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    order = []
    real = platformmod.acquire_single_instance

    def watched(port):
        order.append("lock")
        return real(port)

    monkeypatch.setattr(platformmod, "acquire_single_instance", watched)
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25:
                        order.append("probe") or False)

    class FakeServer(object):
        def serve_forever(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.server, "serve",
                        lambda app, host, port: FakeServer())
    assert cli.main(_temp_args(tmp_path, "--port", "8795")) == 0
    capsys.readouterr()
    assert order and order[0] == "lock", order


def test_a_launch_that_cannot_take_the_lock_opens_the_running_page(
        tmp_path, monkeypatch, capsys, listener, no_browser_env_off):
    """The whole of W3's fix from the second copy's point of view: it does not
    start, it exits 0, and it says so somewhere the player can see -- which,
    in a build with no console, is the page and not stdout."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    it = listener('{"app": "0.1.0", "api": 1}')
    held = platformmod.acquire_single_instance(it.port)
    assert held.acquired, "the fixture could not take the lock it is holding"
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda u: opened.append(u))
    monkeypatch.setattr(cli.server, "serve", _never_served)
    try:
        code = cli.main(["--log", "-", "--port", str(it.port),
                         "--settings", str(tmp_path / "settings.json"),
                         "--config", str(tmp_path / "config.json"),
                         "--backups", str(tmp_path / "backups")])
    finally:
        held.release()
    assert code == 0
    said = capsys.readouterr().out
    assert "a sorter is already running" in said
    assert opened == ["http://127.0.0.1:%d/?notice=second-launch" % it.port]


def test_a_second_launch_that_beats_the_first_ones_bind_still_exits_zero(
        tmp_path, monkeypatch, capsys, no_browser_env_off):
    """The lock is held from before the bind on purpose, so a second
    double-click can arrive while the first copy is still loading the item
    table. There is nothing to open yet and nothing to wait for: say which
    port it is starting on, point the browser at it, and get out of the way."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    held = platformmod.acquire_single_instance(8795)
    assert held.acquired
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda u: opened.append(u))
    monkeypatch.setattr(cli, "wait_for_sorter", lambda *a, **k: None)
    monkeypatch.setattr(cli.server, "serve", _never_served)
    try:
        code = cli.main(["--log", "-", "--port", "8795",
                         "--settings", str(tmp_path / "settings.json"),
                         "--config", str(tmp_path / "config.json"),
                         "--backups", str(tmp_path / "backups")])
    finally:
        held.release()
    assert code == 0
    said = capsys.readouterr().out
    assert "a sorter is already running on port 8795" in said
    assert "still starting" in said
    assert opened == ["http://127.0.0.1:8795/?notice=second-launch"]


def test_the_lock_moves_to_the_port_actually_served(tmp_path, monkeypatch,
                                                    capsys, listener):
    """The lock means "one sorter on *this* port". When the wanted port is
    taken by something else and the search moves on, the lock has to move with
    it -- otherwise 8765 stays locked against a legitimate start once whatever
    was squatting on it goes away."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    it = listener("not a sorter", ctype="text/plain")
    taken = []
    real = platformmod.acquire_single_instance
    monkeypatch.setattr(platformmod, "acquire_single_instance",
                        lambda port: taken.append(port) or real(port))
    served = {}

    class FakeServer(object):
        def serve_forever(self):
            raise KeyboardInterrupt

    def fake_serve(app, host, port):
        served["port"] = port
        # While we are serving, the lock for the port we are on is held and
        # the lock for the one we walked away from is not. Asked through
        # `real`, not through the wrapper: these are the test's questions and
        # do not belong in the record of what `main` took.
        served["mine"] = real(port).acquired
        served["given_up"] = real(it.port)
        return FakeServer()

    monkeypatch.setattr(cli.server, "serve", fake_serve)
    assert cli.main(_temp_args(tmp_path, "--port", str(it.port))) == 0
    capsys.readouterr()
    assert served["port"] != it.port
    assert taken == [it.port, served["port"]]
    assert served["mine"] is False, "the port being served is locked"
    assert served["given_up"].acquired is True, \
        "the port we moved off is not left locked"
    served["given_up"].release()


def test_a_refused_bind_opens_the_sorter_that_refused_it(
        tmp_path, monkeypatch, capsys, listener, no_browser_env_off):
    """With an exclusive bind, `OSError` on the port is how a second copy
    finds out it lost -- not a fault to report. So it asks what is there
    first, and only says "could not be opened" if the answer is not a
    sorter."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    it = listener('{"app": "0.1.0"}')
    # The probe passes (this stands for the millisecond before the other
    # copy's bind completes) and the bind is then refused.
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: False)
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda u: opened.append(u))

    def boom(app, host, port):
        raise OSError(10048, "only one usage of each socket address")

    monkeypatch.setattr(cli.server, "serve", boom)
    code = cli.main(["--log", "-", "--port", str(it.port),
                     "--settings", str(tmp_path / "settings.json"),
                     "--config", str(tmp_path / "config.json"),
                     "--backups", str(tmp_path / "backups")])
    assert code == 0
    assert "a sorter is already running" in capsys.readouterr().out
    assert opened == ["http://127.0.0.1:%d/?notice=second-launch" % it.port]


def test_a_refused_bind_with_nothing_behind_it_is_still_exit_3(
        tmp_path, monkeypatch, capsys):
    """The other half: a port held by something that is not a sorter is still
    "stop whatever is using it", and that sentence must not be swallowed by
    the single-instance path."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: False)
    monkeypatch.setattr(cli, "sorter_at", lambda h, p, **k: None)

    def boom(app, host, port):
        raise OSError(10013, "an attempt was made to access a socket")

    monkeypatch.setattr(cli.server, "serve", boom)
    assert cli.main(_temp_args(tmp_path, "--port", "8795")) == 3
    assert "could not be opened" in capsys.readouterr().out


def test_the_lock_is_released_when_the_server_stops(tmp_path, monkeypatch,
                                                    capsys):
    """Explicitly, and in a `finally`, for the same reason the socket is
    closed there: the next launch asks whether this port is taken, and an
    answer that lags the process sends the operator to a page that is not
    there."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: False)

    class FakeServer(object):
        def serve_forever(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.server, "serve",
                        lambda app, host, port: FakeServer())
    assert cli.main(_temp_args(tmp_path, "--port", "8795")) == 0
    capsys.readouterr()
    after = platformmod.acquire_single_instance(8795)
    try:
        assert after.acquired is True
    finally:
        after.release()


def test_a_corrupt_settings_file_never_adopts_a_running_sorter(
        tmp_path, monkeypatch, capsys, listener):
    """W15's other half. A settings file that could not be parsed leaves every
    value a shipped default, the port included -- and the review watched that
    default port adopt a running instance pointed at a different save folder.
    Sorting the wrong account is not something you can undo (GOAL.md 2.3), so
    a port nobody chose adopts nothing: the search moves on and the operator
    gets their own sorter."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    spath = tmp_path / "settings.json"
    spath.write_text("{not json at all", encoding="utf-8")
    it = listener('{"app": "0.1.0", "api": 1}')
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda u: opened.append(u))
    served = {}

    class FakeServer(object):
        def serve_forever(self):
            raise KeyboardInterrupt

    def fake_serve(app, host, port):
        served["port"] = port
        return FakeServer()

    monkeypatch.setattr(cli.server, "serve", fake_serve)
    # No --no-browser, because `open_browser` staying true is part of what
    # this asserts: the browser thread is started and must be gone before the
    # test is, or it opens its tab after teardown.
    browser = _watch_browser_threads(monkeypatch)
    code = cli.main(["--log", "-", "--port", str(it.port),
                     "--settings", str(spath),
                     "--config", str(tmp_path / "config.json"),
                     "--backups", str(tmp_path / "backups")])
    _join_browser_threads(browser)
    assert code == 0
    said = capsys.readouterr().out
    assert "already running" not in said
    assert opened == [], "a sorter on a port nobody chose is not this one's"
    assert served["port"] != it.port, "it moved to a free port instead"
    assert "using port %d instead" % served["port"] in said, \
        "and the operator is told, because the URL is not the expected one"


def test_a_corrupt_settings_file_does_not_adopt_the_lock_holder_either(
        tmp_path, monkeypatch, capsys):
    """The lock is the other way in. A held lock means a sorter is on that
    port, and the reasoning is the same one: this run's port is a default, so
    that sorter is not known to be this operator's."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    spath = tmp_path / "settings.json"
    spath.write_text("{not json at all", encoding="utf-8")
    held = platformmod.acquire_single_instance(8795)
    assert held.acquired
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda u: opened.append(u))
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: False)
    monkeypatch.setattr(cli, "sorter_at", lambda h, p, **k: (_ for _ in ()).
                        throw(AssertionError("it probed a default port")))
    served = {}

    class FakeServer(object):
        def serve_forever(self):
            raise KeyboardInterrupt

    def fake_serve(app, host, port):
        served["port"] = port
        return FakeServer()

    monkeypatch.setattr(cli.server, "serve", fake_serve)
    # Same reason as above: `open_browser` is deliberately left true, so the
    # thread it starts is joined rather than orphaned. Without this the tab
    # landed on 127.0.0.1:8796 about three seconds later, with no test running.
    browser = _watch_browser_threads(monkeypatch)
    try:
        code = cli.main(["--log", "-", "--port", "8795",
                         "--settings", str(spath),
                         "--config", str(tmp_path / "config.json"),
                         "--backups", str(tmp_path / "backups")])
    finally:
        held.release()
    _join_browser_threads(browser)
    assert code == 0
    assert opened == []
    assert served["port"] == 8796, "the search starts after the locked port"
    assert "using port 8796 instead" in capsys.readouterr().out


def test_a_readable_settings_file_still_adopts(
        tmp_path, monkeypatch, capsys, listener, no_browser_env_off):
    """The guard is about a port *nobody chose*. A settings file that reads
    fine names its port, and two sorters on it are still the double-click
    case the whole check exists for."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    it = listener('{"app": "0.1.0", "api": 1}')
    spath = tmp_path / "settings.json"
    settingsmod.store(str(spath), settingsmod.Settings(port=it.port))
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda u: opened.append(u))
    monkeypatch.setattr(cli.server, "serve", _never_served)
    assert cli.main(["--log", "-", "--settings", str(spath),
                     "--config", str(tmp_path / "config.json"),
                     "--backups", str(tmp_path / "backups")]) == 0
    assert "already running" in capsys.readouterr().out
    assert opened == ["http://127.0.0.1:%d/?notice=second-launch" % it.port]


def test_a_refused_bind_on_a_default_port_is_not_adopted_either(
        tmp_path, monkeypatch, capsys, listener):
    """The third way in, closed for the same reason: an `OSError` from the
    exclusive bind is only a reason to open somebody else's page when the port
    was the operator's choice."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    spath = tmp_path / "settings.json"
    # Unparseable, which is what sets `Settings.load_error`. A file that is
    # valid JSON of the wrong *shape* is reported as a note and does not set
    # it, so this guard does not cover that case -- see the report.
    spath.write_text("{ nope", encoding="utf-8")
    it = listener('{"app": "0.1.0"}')
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: False)
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda u: opened.append(u))

    def boom(app, host, port):
        raise OSError(10048, "only one usage of each socket address")

    monkeypatch.setattr(cli.server, "serve", boom)
    code = cli.main(["--log", "-", "--port", str(it.port),
                     "--settings", str(spath),
                     "--config", str(tmp_path / "config.json"),
                     "--backups", str(tmp_path / "backups")])
    assert code == 3
    assert opened == []
    assert "could not be opened" in capsys.readouterr().out


# ------------------------------------------------ the ?notice= contract

def test_notice_url_is_the_contract_the_page_renders():
    """The second copy cannot tell the running one anything -- it has no
    credentials, and an endpoint any local program could post a toast to is
    not worth having -- so the message rides on the URL the browser is about
    to open anyway."""
    assert cli.notice_url("http://127.0.0.1:8765/") == \
        "http://127.0.0.1:8765/?notice=second-launch"
    assert cli.notice_url("http://127.0.0.1:8765/?tab=settings") == \
        "http://127.0.0.1:8765/?tab=settings&notice=second-launch"
    assert cli.notice_url("http://x/", None) == "http://x/"
    assert cli.NOTICE_SECOND_LAUNCH == "second-launch"


def test_wait_for_sorter_gives_up_rather_than_hanging(monkeypatch):
    """A second copy's whole life has to fit inside the three seconds a person
    waits before double-clicking a third time."""
    asked = []
    slept = []
    monkeypatch.setattr(cli, "sorter_at",
                        lambda h, p, **k: asked.append(p) or None)
    clock = [0.0]
    got = cli.wait_for_sorter("127.0.0.1", 8765, deadline=1.0, step=0.25,
                              sleep=lambda s: (slept.append(s),
                                               clock.__setitem__(
                                                   0, clock[0] + s)),
                              now=lambda: clock[0])
    assert got is None
    assert sum(slept) <= 1.0 + 0.25
    assert len(asked) >= 2, "it polls, because the first copy may be starting"


def test_wait_for_sorter_returns_as_soon_as_the_first_copy_answers(
        monkeypatch):
    answers = [None, None, {"app": "0.1.0"}]
    monkeypatch.setattr(cli, "sorter_at", lambda h, p, **k: answers.pop(0))
    got = cli.wait_for_sorter("127.0.0.1", 8765, deadline=5.0, step=0.0,
                              sleep=lambda s: None)
    assert got == {"app": "0.1.0"}
    assert answers == [], "it stopped asking once it had an answer"


# ---------------------------------------- three launches, three processes

def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_three_simultaneous_launches_leave_exactly_one_listening(tmp_path):
    """The review's own reproduction, as a test: three processes, one port.

    Started as close together as `Popen` allows and against one state
    directory, which is the double-click case. Exactly one may end up
    listening; the other two have to exit 0 quickly and say why, because a
    launch that is refused and silent is what sent the player to Task Manager.

    Real subprocesses rather than threads: the lock is a per-process kernel
    object, and a thread in this interpreter would share the winner's handle
    and prove nothing.
    """
    import json as _json
    import urllib.request as _req

    port = _free_port()
    state = tmp_path / "state"
    state.mkdir()
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(state)
    env.pop("NMS_SORTER_FAKE_PLATFORM", None)
    procs = []
    for i in range(3):
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "nms_sorter", "--no-browser",
             "--port", str(port),
             "--log", str(tmp_path / ("p%d.log" % i)),
             "--settings", str(tmp_path / "settings.json"),
             "--config", str(tmp_path / "config.json"),
             "--backups", str(tmp_path / "backups")],
            cwd=ROOT, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True))
    try:
        # The two losers: exit 0, and soon. `SECOND_LAUNCH_WAIT` is 2 s and
        # the interpreter takes a moment to start, so 10 s is a generous
        # ceiling that still fails if one of them hangs or serves.
        losers = []
        deadline = time.time() + 10
        while time.time() < deadline and len(losers) < 2:
            for p in procs:
                if p not in losers and p.poll() is not None:
                    losers.append(p)
            time.sleep(0.1)
        assert len(losers) == 2, \
            "%d of 3 launches exited; the other(s) are still running" % len(
                losers)
        winner = [p for p in procs if p not in losers][0]
        assert winner.poll() is None

        for p in losers:
            out = p.communicate(timeout=10)[0]
            assert p.returncode == 0, (p.returncode, out)
            assert "a sorter is already running" in out, out

        # And the one that is left is a working sorter on that port.
        with _req.urlopen("http://127.0.0.1:%d/api/version" % port,
                          timeout=10) as fh:
            assert _json.loads(fh.read().decode("utf-8")).get("app")

        if sys.platform.startswith("win"):
            rows = subprocess.run(["netstat", "-ano"], capture_output=True,
                                  text=True, timeout=60).stdout
            listening = [r for r in rows.splitlines()
                         if (":%d " % port) in r and "LISTENING" in r]
            assert len(listening) == 1, listening

        # Stopped the way the page stops it, and the log says so.
        req = _req.Request("http://127.0.0.1:%d/api/quit" % port,
                           data=b"{}", method="POST",
                           headers={"Content-Type": "application/json"})
        with _req.urlopen(req, timeout=10) as fh:
            assert _json.loads(fh.read().decode("utf-8"))["stopping"] is True
        assert winner.wait(timeout=20) == 0
        won = [i for i, p in enumerate(procs) if p is winner][0]
        with io.open(str(tmp_path / ("p%d.log" % won)), encoding="utf-8") as fh:
            log = fh.read()
        import re as _re
        started = _re.search(r"started, pid (\d+), port %d, state dir (.+)"
                             % port, log)
        assert started, log[-2000:]
        assert int(started.group(1)) > 0
        assert str(state) in started.group(2)
        assert "stopped:" in log, \
            "a log with no stopped line is a kill or a crash (W13)"
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=30)


# ===========================================================================
# W2: the working directory
# ===========================================================================

def test_main_does_not_change_the_working_directory(tmp_path, monkeypatch,
                                                    capsys):
    """Nothing in a source run may move: `python -m nms_sorter` is started
    from a directory that belongs to the operator. The frozen build's move out
    of the exe's folder is `platform.release_cwd`, called from
    `packaging/entry.py`, and tested in `test_platform.py`."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: False)
    before = os.getcwd()

    class FakeServer(object):
        def serve_forever(self):
            assert os.getcwd() == before
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.server, "serve",
                        lambda app, host, port: FakeServer())
    assert cli.main(_temp_args(tmp_path, "--port", "8795")) == 0
    capsys.readouterr()
    assert os.getcwd() == before


def test_nothing_in_the_program_calls_chdir():
    """W2 asked for any `os.chdir` into the exe's folder to be removed. There
    was none -- the cwd came from Explorer, not from code -- and this keeps it
    that way: `platform.release_cwd` is the one place allowed to call it, so
    that "where does this process's cwd come from?" has one answer."""
    import ast as _ast
    offenders = []
    for folder in (os.path.join(ROOT, "nms_sorter"),
                   os.path.join(ROOT, "packaging")):
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(folder, name)
            with io.open(path, encoding="utf-8") as fh:
                tree = _ast.parse(fh.read(), path)
            for node in _ast.walk(tree):
                if (isinstance(node, _ast.Attribute)
                        and node.attr == "chdir"):
                    offenders.append("%s:%d" % (name, node.lineno))
    assert offenders == ["platform.py:%s" % offenders[0].split(":")[1]], \
        "os.chdir outside platform.release_cwd: %s" % offenders


# ===========================================================================
# W4: the extraction folders
# ===========================================================================

def test_a_launch_that_is_about_to_exit_does_not_sweep(
        tmp_path, monkeypatch, capsys, listener):
    """The incident, from `cli`'s side.

    An instance serving since 12:21 had its own extraction folder emptied at
    16:58 by a second copy that swept on the way in and only afterwards found
    out it had lost the lock. A copy that is leaving does not tidy up: the
    sweep is reached only after the lock is won *and* the port is bound.
    """
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    it = listener('{"app": "0.1.0", "api": 1}')
    held = platformmod.acquire_single_instance(it.port)
    assert held.acquired
    swept = []
    monkeypatch.setattr(cli, "start_runtime_sweep",
                        lambda *a, **k: swept.append(a) or None)
    monkeypatch.setattr(cli.webbrowser, "open", lambda u: None)
    monkeypatch.setattr(cli.server, "serve", _never_served)
    try:
        code = cli.main(["--log", "-", "--port", str(it.port),
                         "--settings", str(tmp_path / "settings.json"),
                         "--config", str(tmp_path / "config.json"),
                         "--backups", str(tmp_path / "backups")])
    finally:
        held.release()
    assert code == 0
    assert "a sorter is already running" in capsys.readouterr().out
    assert swept == [], "the copy that lost the lock swept anyway"


def test_the_instance_that_wins_the_lock_does_sweep(tmp_path, monkeypatch,
                                                    capsys):
    """The other half, so the fix above cannot be "never sweep at all": the
    one instance that holds the lock and has the port is the one that tidies
    up, and it does it after the bind, not before."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: False)
    order = []
    monkeypatch.setattr(cli, "start_runtime_sweep",
                        lambda *a, **k: order.append("sweep") or None)

    class FakeServer(object):
        def serve_forever(self):
            raise KeyboardInterrupt

    def fake_serve(app, host, port):
        order.append("bound")
        return FakeServer()

    monkeypatch.setattr(cli.server, "serve", fake_serve)
    assert cli.main(_temp_args(tmp_path, "--port", "8795")) == 0
    capsys.readouterr()
    assert order == ["bound", "sweep"], order


def test_the_sweep_passes_the_log_so_its_decisions_are_reviewable(monkeypatch):
    """Every folder the sweep keeps is logged with the reason. A sweep that
    deletes the wrong thing is invisible until something 404s, so the reasons
    have to be in the file that gets attached to the issue."""
    monkeypatch.setattr(platformmod, "is_frozen", lambda: True)
    got = {}

    class FakeLog(object):
        def info(self, msg, *a):
            pass

        def exception(self, msg, *a):
            pass

    log = FakeLog()

    def sweep(**kw):
        got.update(kw)
        return []

    t = cli.start_runtime_sweep(log, sweep=sweep)
    t.join(timeout=10)
    assert got.get("log") is log


def test_the_sweep_runs_only_in_a_frozen_build(monkeypatch):
    """A source checkout has no extraction folder of its own, and a test run
    that quietly deleted things under `%TEMP%` would be a surprise in the
    wrong direction."""
    import logging as _logging
    calls = []
    monkeypatch.setattr(platformmod, "is_frozen", lambda: False)
    assert cli.start_runtime_sweep(_logging.getLogger("nms_sorter"),
                                  sweep=lambda: calls.append(1) or []) is None
    assert calls == []


def test_the_sweep_logs_what_it_removed(monkeypatch):
    monkeypatch.setattr(platformmod, "is_frozen", lambda: True)
    said = []

    class FakeLog(object):
        def info(self, msg, *a):
            said.append(msg % a)

        def exception(self, msg, *a):
            said.append("EXC " + (msg % a if a else msg))

    t = cli.start_runtime_sweep(FakeLog(),
                                sweep=lambda **k: ["C:\\t\\_MEI1"])
    t.join(timeout=10)
    assert said and "_MEI1" in said[0]
    assert "leftover extraction folder" in said[0]


def test_a_failing_sweep_is_logged_and_nothing_else(monkeypatch):
    """Housekeeping that can stop the program starting is worse than litter."""
    monkeypatch.setattr(platformmod, "is_frozen", lambda: True)
    said = []

    class FakeLog(object):
        def info(self, msg, *a):
            said.append("INFO")

        def exception(self, msg, *a):
            said.append(msg)

    def boom():
        raise OSError(5, "denied")

    t = cli.start_runtime_sweep(FakeLog(), sweep=boom)
    t.join(timeout=10)
    assert said and "sweep failed" in said[0]


# ===========================================================================
# W12, W13: what the log and the banner have to say
# ===========================================================================

def test_the_banner_says_how_to_get_the_page_back():
    """W12: double-clicking the exe again is the way back to the page, it
    works, and nothing said so. The player had closed the tab."""
    assert cli.again_banner("http://127.0.0.1:8765/", frozen=True) == \
        "again    " + cli.AGAIN_SENTENCE
    assert "double-click NMS-Sorter.exe again" in cli.AGAIN_SENTENCE
    # A checkout has no such file, and telling somebody to double-click an
    # executable they never downloaded is how a page loses a reader's trust.
    source = cli.again_banner("http://127.0.0.1:8765/", frozen=False)
    assert "NMS-Sorter.exe" not in source
    assert "http://127.0.0.1:8765/" in source
    assert "get the page back" in source


def test_the_startup_block_carries_the_again_line(tmp_path, monkeypatch,
                                                  capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: False)

    class FakeServer(object):
        def serve_forever(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.server, "serve",
                        lambda app, host, port: FakeServer())
    assert cli.main(_temp_args(tmp_path, "--port", "8795")) == 0
    assert "get the page back" in capsys.readouterr().out


def test_the_log_names_the_pid_the_port_and_the_state_dir(tmp_path,
                                                          monkeypatch,
                                                          capsys):
    """W12/W13: a log that has to be read by somebody who was not there needs
    to say which process, which page and where the state is -- and then say
    that it stopped, so a log ending without that line is a kill or a crash."""
    state = tmp_path / "state"
    monkeypatch.setenv("LOCALAPPDATA", str(state))
    logfile = tmp_path / "sorter.log"
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: False)

    class FakeServer(object):
        def serve_forever(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.server, "serve",
                        lambda app, host, port: FakeServer())
    code = cli.main(["--no-browser", "--log", str(logfile), "--port", "8795",
                     "--settings", str(tmp_path / "settings.json"),
                     "--config", str(tmp_path / "config.json"),
                     "--backups", str(tmp_path / "backups")])
    assert code == 0
    capsys.readouterr()
    import logging as _logging
    for h in list(_logging.getLogger("nms_sorter").handlers):
        h.close()
    with io.open(str(logfile), encoding="utf-8") as fh:
        text = fh.read()
    assert "started, pid %d, port 8795" % os.getpid() in text
    assert str(platformmod.state_dir()) in text
    assert "stopped: ctrl-c" in text


def test_the_stop_reason_names_the_page_or_the_idle_exit(tmp_path, monkeypatch,
                                                         capsys):
    """A clean return from `serve_forever` is `POST /api/quit` or the idle
    watchdog; both log their own line first, so this one only has to exist and
    not lie about which."""
    state = tmp_path / "state"
    monkeypatch.setenv("LOCALAPPDATA", str(state))
    logfile = tmp_path / "sorter.log"
    monkeypatch.setattr(cli, "port_answers",
                        lambda host, port, timeout=0.25: False)

    class FakeServer(object):
        def serve_forever(self):
            return None

        def server_close(self):
            pass

    monkeypatch.setattr(cli.server, "serve",
                        lambda app, host, port: FakeServer())
    assert cli.main(["--no-browser", "--log", str(logfile), "--port", "8795",
                     "--settings", str(tmp_path / "settings.json"),
                     "--config", str(tmp_path / "config.json"),
                     "--backups", str(tmp_path / "backups")]) == 0
    capsys.readouterr()
    import logging as _logging
    for h in list(_logging.getLogger("nms_sorter").handlers):
        h.close()
    with io.open(str(logfile), encoding="utf-8") as fh:
        text = fh.read()
    assert "stopped: asked to stop (the page's stop button, or the idle exit)" \
        in text
