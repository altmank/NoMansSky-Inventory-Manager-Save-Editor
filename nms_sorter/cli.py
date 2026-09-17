#!/usr/bin/env python3
"""No Man's Sky save-file inventory sorter -- local web application.

    python -m nms_sorter

Serves http://127.0.0.1:8765 and opens it. Standard library only. With no flags
at all it reads `%LOCALAPPDATA%\\NMS-Sorter\\settings.json`, and if that file is
not there yet it serves a first-run page that asks which folder holds the saves.

Every flag below overrides the settings file **for this run only** and is never
written back (GOAL.md 3.6), so a one-off `--port 9000` does not become the
stored port the next time Save is pressed on the Settings tab.

    --folder  PATH   save folder (settings: save_folder)
    --config  PATH   config file (default: %LOCALAPPDATA%\\NMS-Sorter\\config.json)
    --backups PATH   where backups go (settings: backup_folder)
    --settings PATH  the settings file itself
                     (default: %LOCALAPPDATA%\\NMS-Sorter\\settings.json)
    --host    ADDR   interface to bind (default 127.0.0.1). Anything but a
                     loopback address exposes your save to your network, and
                     nothing here authenticates, so do not.
    --port    N      default 8765; if a sorter is already answering there,
                     that one is opened instead of starting a second -- and
                     only one sorter per port can ever start, because the
                     lock for it is taken before anything is probed and the
                     bind itself is exclusive; if something else has the
                     port, the next ten are tried
    --log     PATH   log file (default: %LOCALAPPDATA%\\NMS-Sorter\\logs\\sorter.log,
                     rotating, 5 files of 1 MB). `--log -` logs to stderr.
    --read-only      refuse the apply endpoint outright; plan and browse only
    --strict-version-check
                     refuse to apply to a save whose version is outside the
                     range this build was verified on. Off by default: the
                     write sequence's own checks catch a layout change on the
                     file itself, so a game update does not turn the tool off.
    --no-browser     do not open a browser. `NMS_SORTER_NO_BROWSER=1` in the
                     environment does the same for every launch made from it,
                     including a second copy that would otherwise open the
                     running instance's page.
    --idle-exit-minutes N
                     stop by itself after N minutes with no request from the
                     page (settings: idle_exit_minutes, default 30). 0, or any
                     number below 1, keeps it running until it is stopped.
                     The page polls every 15 seconds, so an open tab never
                     idles out; a tab that was closed does.

Stopping it: ctrl-c here, the **stop the sorter** button on the Settings section
(`POST /api/quit`), or the idle exit above. All three return 0 and all three
write a `stopped:` line to the log, so a log that ends without one is a kill
or a crash rather than a stop.

Starting it twice is not an error and does not start a second one: the second
copy opens the first one's page with `?notice=second-launch` on the URL and
exits 0.

Exit codes: 0 stopped normally, 3 no free port, 4 Python too old (checked in
`__main__` before this module is imported).
"""
import argparse
import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser

from . import config as cfgmod
from . import pages
from . import platform as platformmod
from . import savemodel
from . import server
from . import settings as settingsmod
from .app import App, Degraded
from .platform import state_dir          # noqa: F401  (part of the surface)

LOG_BYTES = 1024 * 1024
LOG_KEEP = 5

#: how many ports after the wanted one are tried before giving up (P2-2 (e)).
PORT_TRIES = 10

#: set this to `1` and nothing in this module ever opens a browser, whatever
#: the settings file says and whichever path the run takes.
#:
#: Belt and braces over `--no-browser`, and it exists because the flag is not
#: enough for an unattended run. `--no-browser` has to be remembered on *every*
#: launch, and the path that hurts is the one that takes the fewest arguments:
#: a second copy started without it reads the *first* instance's settings,
#: where `open_browser` is true, and opens a tab on somebody's desktop. One
#: variable in the environment covers every launch made from that environment,
#: including the ones a script forgot a flag on.
NO_BROWSER_ENV = "NMS_SORTER_NO_BROWSER"


def browser_suppressed(env=None):
    """-> is `NMS_SORTER_NO_BROWSER` saying "not from this process". bool.

    Exactly `"1"`, trimmed. Not "any non-empty value": `=0` and `=false` both
    read as off to anybody who writes them, and a guard that treated them as on
    would be the surprise this is meant to prevent.
    """
    env = os.environ if env is None else env
    return (env.get(NO_BROWSER_ENV) or "").strip() == "1"


def open_in_browser(url, log=None):
    """The **only** call to `webbrowser.open` in this program. -> did it open.

    One funnel rather than a check at each call site, because the two sites are
    reached by different paths -- the instance that serves opens its own page
    from a thread, the copy that loses opens the running instance's page on the
    way out -- and a guard that has to be repeated is a guard that gets missed
    on the path nobody was thinking about.
    """
    if browser_suppressed():
        if log is not None:
            try:
                log.info("not opening %s: %s=1", url, NO_BROWSER_ENV)
            except Exception:
                pass
        return False
    webbrowser.open(url)
    return True


def default_log_path():
    return os.path.join(state_dir(), "logs", "sorter.log")


def setup_logging(spec=None):
    """One INFO line per request, a warning per refusal, a traceback for the
    unexpected. -> the path being written, or None for stderr.

    A bug report attaches this file, which is why it rotates rather than
    truncates and why it lives beside the config rather than beside the code.
    The log path is deliberately not a setting: a settings file that cannot be
    read must still be loggable (DECISIONS.md 2026-09-14, resolution 3).
    """
    root = logging.getLogger("nms_sorter")
    root.setLevel(logging.INFO)
    root.propagate = False
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    if spec == "-":
        path, handler = None, logging.StreamHandler(sys.stderr)
    else:
        path = spec or default_log_path()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=LOG_BYTES, backupCount=LOG_KEEP, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s"))
    root.addHandler(handler)
    return path


def port_answers(host, port, timeout=0.25):
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def choose_port(host, wanted, tries=PORT_TRIES):
    """-> (a port nothing is listening on, [the ports that were busy]).

    The port is probed rather than merely bound, because Windows lets
    SO_REUSEADDR bind a port another process is already listening on, and two
    sorters on one port is a silent wrong-answer machine. A busy port is not
    fatal on its own: the next ten are tried, and only then does this give up,
    because at that point there is no server left to render a page from
    (P2-2 (e)).
    """
    busy = []
    for port in range(wanted, wanted + tries + 1):
        if port > 65535:
            break
        if not port_answers(host, port):
            return port, busy
        busy.append(port)
    return None, busy


#: how long the already-running probe waits for `/api/version`. One second:
#: long enough for a loaded sorter to answer, short enough that a port held by
#: something that accepts and never replies does not stall the start.
PROBE_TIMEOUT = 1.0


def probe_host(host):
    """Which address to ask about a busy port.

    A wildcard bind is not an address you can connect to on every stack, and
    the sentence the operator is given has to be a URL they can click, so the
    answer for `0.0.0.0` is loopback.
    """
    return "127.0.0.1" if host in ("", "0.0.0.0", "::", "*") else host


def sorter_at(host, port, timeout=PROBE_TIMEOUT):
    """-> the `/api/version` payload if a sorter answers on that port, else None.

    This is the two-instance check (GOAL.md P4-3, §2.3 "Two instances"). Two
    sorters on one save folder is the case worth spending a round trip to
    avoid: both would mint plans, both would hold a config in memory, and the
    second one's Apply would be refused by the lock only *after* the operator
    had configured it. So a busy port is interrogated rather than stepped over.

    Identified by `{"app": ...}` in the JSON, not by the port number: 8765 is
    not ours, and something else listening there must not be mistaken for a
    sorter and opened in a browser.
    """
    url = "http://%s:%d/api/version" % (host, port)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as fh:
            if getattr(fh, "status", 200) != 200:
                return None
            body = fh.read(65536)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(data, dict) and data.get("app"):
        return data
    return None


#: how long a second launch waits for the first one to answer before it gives
#: up on naming a URL. The first copy holds the lock from before it binds, so a
#: second double-click can arrive while the first is still loading the item
#: table; 2 s covers that and keeps the second process's whole life under the
#: three seconds a person waits before double-clicking a third time.
SECOND_LAUNCH_WAIT = 2.0

#: the query parameter the second copy adds to the URL it opens, so the page
#: can say what happened. The second process cannot tell the running one
#: anything -- it has no credentials, and an endpoint that any local program
#: could post a toast to is not worth having -- but the *browser* is about to
#: visit the running one anyway, so the message rides along on the URL.
#: Contract with `static/`: `?notice=second-launch` means "another copy of the
#: sorter was started and closed itself; this is the one that is running".
NOTICE_PARAM = "notice"
NOTICE_SECOND_LAUNCH = "second-launch"

#: both halves of "you already have one". The shared opening words are load
#: bearing: `docs/TROUBLESHOOTING.md` quotes them, and a person who sees either
#: sentence has the same question.
ALREADY_RUNNING = "a sorter is already running at %s ; opening that instead"
ALREADY_RUNNING_STARTING = ("a sorter is already running on port %d and is "
                            "still starting; opening %s and closing this copy")


def notice_url(base, notice=NOTICE_SECOND_LAUNCH, param=NOTICE_PARAM):
    """Add `?notice=...` to a URL that has no query string of its own.

    Built here rather than formatted inline so there is one place the page
    agent's contract is written down, and so a `base` that ever grows a query
    string keeps producing a valid URL.
    """
    if not notice:
        return base
    sep = "&" if "?" in base else "?"
    return "%s%s%s=%s" % (base, sep, param, notice)


def wait_for_sorter(host, port, deadline=SECOND_LAUNCH_WAIT,
                    step=0.1, sleep=time.sleep, now=time.monotonic):
    """-> the `/api/version` payload once a sorter answers, or None at `deadline`.

    Polled rather than asked once, because the process this one is deferring
    to may still be starting: it takes its lock before it binds, on purpose,
    so "the lock is held" and "the port answers" are several hundred
    milliseconds apart on a cold start.
    """
    end = now() + deadline
    while True:
        found = sorter_at(host, port, timeout=min(step * 5, 1.0))
        if found is not None:
            return found
        if now() >= end:
            return None
        sleep(step)


def report_already_running(host, port, open_browser, log, other=None,
                           because="", wait=SECOND_LAUNCH_WAIT):
    """Point the operator at the sorter that is already there. -> 0.

    Exit code 0, printed *and* logged, and the browser is opened with
    `?notice=second-launch`: in `NMS-Sorter.exe` there is no console, so the
    sentence on stdout goes to the log file and the only place the player can
    actually be told is the page they are about to be shown (W3). The page
    renders the notice; this side only guarantees it is on the URL.
    """
    phost = probe_host(host)
    base = "http://%s:%d/" % (phost, port)
    if other is None:
        other = wait_for_sorter(phost, port, wait)
    line = (ALREADY_RUNNING % base if other is not None
            else ALREADY_RUNNING_STARTING % (port, base))
    opening = notice_url(base)
    print(line)
    # The URL, in full, including the notice: in a windowed build this log is
    # the only record that the second copy existed at all, and "which page did
    # it open?" is the first question about a toast nobody saw.
    log.info("%s (%s; app %s; opening %s)", line, because or "-",
             (other or {}).get("app"),
             "nothing, --no-browser" if not open_browser
             else "nothing, %s=1" % NO_BROWSER_ENV if browser_suppressed()
             else opening)
    try:
        sys.stdout.flush()
    except Exception:
        pass
    if open_browser:
        open_in_browser(opening, log)
    return 0


def open_when_ready(url, host, port, deadline=3.0, log=None, cancel=None):
    """Open the browser once the port answers, not after a hopeful 0.4 s.

    The old timer raced the bind on a cold start and the operator saw a
    connection-refused page instead of the app.

    `cancel` is a `threading.Event` meaning "the process is no longer serving,
    so there is nothing on that port to show". `main` sets it on the way out,
    which is why the wait is `cancel.wait` and not `time.sleep`: a sorter that
    is quit inside three seconds, or whose serve loop fails, used to drop a
    connection-refused tab on the operator several seconds after the window
    had gone. The same lifetime bug put two tabs on a desktop per full pytest
    run, from threads that outlived the tests that made them.
    """
    if browser_suppressed():
        # Checked here as well as in `open_in_browser`, so that under the guard
        # this thread does not even spend three seconds polling a port in order
        # to do nothing at the end of it.
        return open_in_browser(url, log)
    if cancel is None:
        cancel = threading.Event()
    end = time.time() + deadline
    while time.time() < end:
        if port_answers(host, port, 0.1):
            break
        # Returns True the moment the event is set, so a cancelled thread ends
        # within one 50 ms step instead of sleeping out the whole deadline.
        if cancel.wait(0.05):
            return False
    # Checked once more: the port can answer on the same step the process
    # decides to stop, and it is the open that must not happen, not the poll.
    if cancel.is_set():
        return False
    return open_in_browser(url, log)


# ---------------------------------------------------------------------------
# the idle exit
# ---------------------------------------------------------------------------

#: how often the watchdog looks at the clock. The exit is measured in minutes,
#: so five seconds costs nothing in accuracy and a thread that wakes twelve
#: times a minute costs nothing at all. It is also the granularity of the
#: `busy` deferral: a write that finishes just after a tick delays the exit by
#: one tick, which is the right way round.
IDLE_TICK_SECONDS = 5.0


def idle_watchdog(minutes, stop, idle=None, busy=None, sleep=time.sleep,
                  tick=IDLE_TICK_SECONDS):
    """Call `stop(minutes)` once nothing has asked this server for anything in
    `minutes` minutes. -> the idle seconds it fired at, or None if it never
    fired.

    The reason this exists: `NMS-Sorter.exe` has no console, so a player who
    closes the browser tab has closed the only thing that was telling them the
    server is up. The page itself is what keeps the timer alive -- it polls
    `GET /api/game` every 15 seconds -- so "no request in half an hour" means
    "no page open", not "nobody typed anything".

    `minutes` may be a number or a callable. A callable is what the running
    server passes, so that saving the setting on the Settings tab takes effect
    on the next tick rather than at the next restart; a plain 0 returns
    immediately, because a watchdog that is off should not own a thread.

    `busy` defers rather than cancels: a write in progress pushes the decision
    to the next tick, and since a request is what resets the clock, an apply
    that takes ten minutes does not bring the deadline forward -- it is
    followed by the page's own polling anyway.

    Every seam is an argument (`idle`, `busy`, `sleep`) so the unit test runs
    on a fake clock in microseconds instead of waiting half an hour.
    """
    if not callable(minutes) and not (minutes and minutes > 0):
        return None
    idle = idle or server.idle_seconds
    busy = busy or (lambda: False)
    while True:
        sleep(tick)
        want = minutes() if callable(minutes) else minutes
        if not want or want <= 0:
            # Turned off while it was running. Keep ticking rather than
            # returning: turning it back on must not need a restart.
            continue
        if busy():
            continue
        idled = idle()
        if idled >= want * 60.0:
            stop(want)
            return idled


def idle_minutes_of(app):
    """The live `idle_exit_minutes`, read off the app's settings. `getattr`,
    because `Degraded` carries a `Settings` too and an older one may not carry
    the field at all."""
    return getattr(getattr(app, "settings", None), "idle_exit_minutes", 0) or 0


def start_idle_watchdog(httpd, app, log, tick=IDLE_TICK_SECONDS):
    """Run `idle_watchdog` on a daemon thread. -> the thread.

    Started unconditionally, even when the setting is 0, because the setting is
    read on every tick: a sorter started with the exit off can be given one
    from the Settings tab without a restart.

    The whole body is guarded. A watchdog that raised would otherwise take its
    thread down silently and the server would keep running with nobody left to
    stop it -- the failure is invisible in exactly the build that has no
    console, which is the one this feature is for.
    """
    def stop(minutes):
        # Logged before the server is told, so the line is in the file
        # whichever order the two threads are scheduled in.
        log.info("stopped after %d idle minutes", minutes)
        # And printed only where a print can be read. In a windowed build
        # `print` is a writer that forwards to this same log (`ensure_streams`),
        # so printing unconditionally put the line in the file twice -- which
        # is measurable in the frozen build and was.
        if platformmod.streams_visible():
            print("stopped after %d idle minutes" % minutes)
            try:
                sys.stdout.flush()
            except Exception:
                pass
        httpd.shutdown()

    def run():
        try:
            idle_watchdog(lambda: idle_minutes_of(app), stop, tick=tick,
                          busy=lambda: bool(getattr(app, "busy", False)))
        except Exception:
            log.exception("the idle watchdog stopped; this sorter will now run "
                          "until it is stopped from the page or with ctrl-c")

    t = threading.Thread(target=run, name="nms-sorter-idle", daemon=True)
    t.start()
    return t


def start_runtime_sweep(log, sweep=None):
    """Remove earlier runs' one-file extraction folders. -> the thread, or None.

    On a daemon thread: this deletes up to a few hundred megabytes that
    previous builds left in `%TEMP%` (W4), and the page must not wait for it.

    **Called from exactly one place**, and late: after the single-instance
    lock has been won and after the port has been bound, which is the only
    moment this process knows it is the sorter rather than a copy that is
    about to open somebody else's page and exit. That is not tidiness. An
    instance that had been serving since 12:21 was emptied at 16:58 by a
    second copy which ran this on the way in and found out it had lost the
    lock afterwards; the server it broke was the operator's own. A copy that
    is leaving does not tidy up.

    Frozen builds only -- a source checkout has no extraction folder of its
    own, and a test run that quietly deleted things in `%TEMP%` would be a
    surprise in the wrong direction.
    """
    if not platformmod.is_frozen():
        return None
    sweep = sweep or platformmod.sweep_stale_runtime_dirs

    def run():
        try:
            # `log` is passed in, not just used for the summary: every folder
            # the sweep *keeps* is logged with its reason, which is the only
            # way a decision this destructive can be reviewed after the fact.
            removed = sweep(log=log)
        except Exception:
            log.exception("the leftover-folder sweep failed; nothing was "
                          "deleted and the sorter is unaffected")
            return
        if removed:
            log.info("removed %d leftover extraction folder(s) from earlier "
                     "runs: %s", len(removed), "; ".join(removed[:8]))

    t = threading.Thread(target=run, name="nms-sorter-sweep", daemon=True)
    t.start()
    return t


#: the sentence W12 asked for, verbatim, because `docs/GUIDE.md` and
#: `docs/TROUBLESHOOTING.md` quote it and `tests/test_docs.py` greps for the
#: quoted form. Closing the tab is the thing every player did, and nothing
#: told them the way back.
AGAIN_SENTENCE = ("if you close the browser, double-click NMS-Sorter.exe "
                  "again to get the page back")


def again_banner(url, frozen=None):
    """The `again` line of the startup block.

    Two wordings because there are two programs. A player has
    `NMS-Sorter.exe` and double-clicks it; somebody running from a checkout
    has no such file and does have the URL on the screen in front of them, and
    telling them to double-click an executable they never downloaded is the
    kind of sentence that makes a reader distrust the rest of the page.
    """
    if frozen is None:
        frozen = platformmod.is_frozen()
    if frozen:
        return "again    " + AGAIN_SENTENCE
    return "again    if you close the browser, open %s again to get the " \
           "page back" % url


def idle_banner(minutes):
    """The `stop` line of the startup block.

    One line for all three ways out, because the question it answers is "how do
    I turn this off?" and a person with a windowed build has no console to
    press ctrl-c in -- which is why the Settings tab comes first.
    """
    return ("stop     from the Settings section, or ctrl-c here; "
            + ("idle exit after %d minutes" % minutes if minutes and minutes > 0
               else "no idle exit, so it runs until you stop it"))


# ---------------------------------------------------------------------------
# startup failures that still get a page (P2-2)
# ---------------------------------------------------------------------------

def _config_file_version(path):
    """The `config_version` the file claims, for the too-new page. None if it
    cannot be read -- the page then says so rather than inventing a number."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh).get("config_version")
    except Exception:
        return None


def _data_file_of(message):
    """The `nms_sorter/data/<file>` a load error named, if it named one."""
    for word in str(message).replace(",", " ").split():
        if word.startswith("nms_sorter/data/"):
            return word.rstrip(".:")
    return None


def build_app(st, config_path, settings_path, log_path):
    """-> a live `App`, or a `Degraded` serving one page that explains why not.

    The three failures handled here are fatal to `App.__init__` and none of them
    is helped by a traceback in a console window the operator may never see.
    `ConfigTooNew` is lane B's (config v2); the `getattr` keeps this working
    both before and after it lands, and a plain `ValueError` that talks about a
    version is treated the same way, because that is what today's `config.load`
    raises for a version it does not read.
    """
    def make():
        return App(config_path=config_path, settings=st,
                   settings_path=settings_path, log_path=log_path)

    too_new = getattr(cfgmod, "ConfigTooNew", None)
    try:
        return make()
    except Exception as exc:
        err = exc
        if too_new is not None and isinstance(err, too_new):
            kind = "config_too_new"
        elif isinstance(err, json.JSONDecodeError):
            kind = "config_unreadable"
        elif isinstance(err, RuntimeError):
            kind = "data_missing"
        elif isinstance(err, ValueError) and "version" in str(err).lower():
            kind = "config_too_new"
        elif isinstance(err, (ValueError, UnicodeDecodeError, OSError)):
            kind = "config_unreadable"
        else:
            raise

    detail = "%s: %s" % (type(err).__name__, err)
    if kind == "config_too_new":
        html = pages.config_too_new(config_path,
                                    _config_file_version(config_path),
                                    cfgmod.CONFIG_VERSION)
        problem = "the configuration file was written by a newer version"
        allow_reset = False
    elif kind == "config_unreadable":
        html = pages.config_unreadable(config_path, detail)
        problem = "the configuration file could not be read"
        allow_reset = True
    else:
        html = pages.data_missing(detail, _data_file_of(err))
        problem = "a packaged data file is missing"
        allow_reset = False
    logging.getLogger("nms_sorter").error("starting in reduced mode: %s (%s)",
                                          problem, detail)
    return Degraded(kind, problem, html, st, settings_path=settings_path,
                    config_path=config_path, log_path=log_path, detail=detail,
                    allow_reset=allow_reset, rebuild=make)


# ---------------------------------------------------------------------------

NL = chr(10)
BLANK = NL + NL


def report_silent_failure(log_path):
    """A windowed build has nowhere to print. Say it twice: log, then dialog.

    `NMS-Sorter.exe` is built with `console=False`, so `print()` goes nowhere
    and an unhandled traceback exits with a code nobody sees -- the operator
    double-clicks and *nothing happens*, which is the worst failure this
    program has, because there is nothing to report. -> exit code 1.
    """
    log = logging.getLogger("nms_sorter")
    log.exception("failed to start")
    lines = traceback.format_exc().strip().splitlines()
    last = lines[-1] if lines else "an unknown error"
    told = ("%s" + BLANK
            + "The full details are in:" + NL + "%s" + BLANK
            + "Run NMS-Sorter-console.exe to see this in a window that "
            + "stays open.") % (
        last, log_path or "(the log file could not be opened either)")
    platformmod.message_box("NMS Sorter could not start", told)
    return 1


def main(argv=None):
    """Start the server, or explain why not. -> the process exit code.

    In a frozen build with nowhere visible to print -- which is what
    `NMS-Sorter.exe` is -- every exception is caught here, logged, and shown in
    a message box, because otherwise the operator double-clicks and nothing
    happens at all. In a source run, and in the console build, and in the
    windowed build with its output redirected, it is deliberately not caught: a
    traceback in a terminal or a log is the most useful thing that can happen
    there, and swallowing it in favour of a dialog would be a downgrade.
    """
    if platformmod.is_frozen() and not platformmod.streams_visible():
        try:
            return _main(argv)
        except SystemExit:
            raise
        except BaseException:
            return report_silent_failure(_last_log_path[0])
    return _main(argv)


#: the log path `_main` last set up, so `report_silent_failure` can name it in
#: the dialog even when the failure happened before `_main` returned anything.
#: A one-element list rather than a global rebind: this module is imported, not
#: re-executed, and the value is only ever read on the way out.
_last_log_path = [None]


def _main(argv=None):
    ap = argparse.ArgumentParser(prog="nms-sorter", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    state = state_dir()
    ap.add_argument("--folder")
    ap.add_argument("--config", default=os.path.join(state, "config.json"))
    ap.add_argument("--backups", default=None)
    ap.add_argument("--settings", default=None)
    ap.add_argument("--host", default="127.0.0.1",
                    help="interface to bind; keep it on loopback")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--log", default=None, help="log file, or - for stderr")
    ap.add_argument("--read-only", action="store_true")
    ap.add_argument("--strict-version-check", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--idle-exit-minutes", type=int, default=None,
                    metavar="N",
                    help="stop by itself after N minutes with no request from "
                         "the page; 0 never does")
    args = ap.parse_args(argv)

    log_path = setup_logging(args.log)
    _last_log_path[0] = log_path
    log = logging.getLogger("nms_sorter")

    settings_path = args.settings or settingsmod.default_path()
    existed = os.path.exists(settings_path)
    st, notes = settingsmod.load(settings_path)
    # The flags, for this run only. `None` means "not passed", which is why
    # --port defaults to None and the two switches pass None when they are off.
    st.override(save_folder=args.folder, backup_folder=args.backups,
                port=args.port, read_only=args.read_only or None,
                strict_version_check=args.strict_version_check or None,
                open_browser=False if args.no_browser else None,
                # Passed straight through, not `or None` like the switches
                # above: `--idle-exit-minutes 0` is a value -- it is how the
                # exit is turned off for one run -- and `override` already
                # treats only None as "the flag was not given".
                idle_exit_minutes=args.idle_exit_minutes)
    for n in notes:
        print("  settings %s: %s" % (n["level"], n["message"]))
        log.warning("settings %s: %s", n["level"], n["message"])

    os.makedirs(os.path.dirname(os.path.abspath(args.config)), exist_ok=True)
    backups = st.resolved_backup_folder()
    try:
        os.makedirs(backups, exist_ok=True)
    except OSError as exc:
        print("  warning: the backup folder %s could not be created (%s)"
              % (backups, exc))

    # Taken **before** anything is probed, and that order is the whole fix:
    # a probe cannot see a server that has not bound yet, so three launches
    # within one millisecond all used to pass it and all three then bound
    # (W3). Only one process can hold this lock, however close together they
    # start, and it is released by the kernel if this one is killed.
    lock = platformmod.acquire_single_instance(st.port)

    # W15: may this run *adopt* a sorter that is already on the wanted port?
    #
    # Only if somebody chose that port. A settings file that could not be
    # parsed leaves every value a shipped default, the default port included,
    # and the review watched that default port adopt a running instance
    # pointed at a different save folder -- which is the one mistake §2.3
    # refuses to make on the operator's behalf, because sorting the wrong
    # account is not something you can undo. So with a load error nothing is
    # adopted: the search moves on and the operator gets their own sorter on
    # the next free port, with their own first-run page.
    adopt = not getattr(st, "load_error", None)
    wanted = st.port
    if not lock.acquired:
        if adopt:
            return report_already_running(args.host, st.port, st.open_browser,
                                          log,
                                          because="another copy holds the %s "
                                                  "lock" % lock.kind)
        log.warning("port %d is held by another sorter, but this run's port is"
                    " a shipped default because the settings file could not be"
                    " read, so that sorter is not this one's and is not opened"
                    "; looking for a free port from %d", st.port, st.port + 1)
        wanted = st.port + 1

    port, busy = choose_port(args.host, wanted)
    if st.port in busy:
        # The wanted port is taken by something this lock knows nothing about
        # -- a sorter from a build older than the lock, or another program.
        # Ask which: if it is a sorter, a second one is the wrong answer and
        # the operator almost certainly just double-clicked twice (P4-3).
        other = sorter_at(probe_host(args.host), st.port) if adopt else None
        if other is not None:
            lock.release()
            return report_already_running(args.host, st.port, st.open_browser,
                                          log, other=other,
                                          because="it answered the probe")
        if not adopt:
            log.warning("port %d is busy and is not probed: this run's port is"
                        " a shipped default because the settings file could"
                        " not be read, so whatever is there is not known to be"
                        " this operator's sorter", st.port)
    if port is None:
        lock.release()
        print("Port %d is already in use, and so are the %d ports after it -- "
              "another sorter, or something else, is listening there; stop it "
              "or pass --port with a free number."
              % (st.port, len(busy) - 1))
        return 3
    if port != st.port:
        # Serving somewhere else, so the lock moves with us: it means "one
        # sorter on this port", and the port it was taken for is not ours.
        lock.release()
        lock = platformmod.acquire_single_instance(port)
        if not lock.acquired:
            return report_already_running(args.host, port, st.open_browser,
                                          log,
                                          because="another copy holds the %s "
                                                  "lock" % lock.kind)

    app = build_app(st, args.config, settings_path, log_path)
    try:
        # `server.serve` binds exclusively (`platform.exclusive_server_class`),
        # so this `OSError` is the second half of the single-instance check and
        # not an unexpected failure.
        httpd = server.serve(app, args.host, port)
    except OSError as exc:
        # With `SO_EXCLUSIVEADDRUSE` a refused bind is the *normal* way a
        # second copy loses, not an unexpected failure, so ask what is there
        # before reporting a problem the operator has to act on.
        other = sorter_at(probe_host(args.host), port) if adopt else None
        lock.release()
        if other is not None:
            return report_already_running(args.host, port, st.open_browser,
                                          log, other=other,
                                          because="the bind was refused (%s)"
                                                  % (exc.strerror or exc))
        print("Port %d could not be opened (%s) -- stop whatever is using it "
              "or pass --port with a free number."
              % (port, exc.strerror or exc))
        return 3
    url = "http://%s:%d/" % (args.host, port)

    print("No Man's Sky inventory sorter")
    print("  saves    %s" % (st.save_folder or "(not chosen yet)"))
    print("  settings %s%s" % (settings_path,
                               "" if existed else "   (not written yet)"))
    print("  config   %s%s" % (args.config,
                               "   (created)" if getattr(app, "created", False)
                               else ""))
    print("  backups  %s" % backups)
    print("  log      %s" % (log_path or "stderr"))
    print("  mode     %s" % ("READ ONLY -- apply is disabled"
                             if st.read_only else "plan and apply"))
    # Printed either way, because the interesting state is the default one: the
    # save-version gate is advisory unless it is asked for, and a banner that
    # only spoke when the gate was on would leave the common case unstated.
    if st.strict_version_check:
        print("  gate     save-version check STRICT -- a save outside %d to %d "
              "is refused at apply" % savemodel.SUPPORTED_VERSIONS)
    else:
        print("  gate     save-version check DISABLED -- an unverified version "
              "plans and applies; --strict-version-check refuses it")
    print("  %s" % idle_banner(st.idle_exit_minutes))
    print("  %s" % again_banner(url))
    if port != st.port:
        print("  port     %d was busy; using port %d instead" % (st.port, port))
    if app.degraded is not None:
        print("  PROBLEM  %s -- open %s for what to do"
              % (app.degraded.problem, url))
    elif app.first_run:
        print("  first run -- open %s and choose your save folder" % url)
    print("  serving  %s   (ctrl-c to stop)" % url)
    # Flushed explicitly. `print` to a pipe is block-buffered, so a bug report
    # that runs `NMS-Sorter-console.exe > out.txt` gets an empty file for as
    # long as the server runs -- which is the whole time anybody would look at
    # it. Observed on the packaged build; the banner is the first thing asked
    # for in an issue.
    try:
        sys.stdout.flush()
    except Exception:
        pass
    # Two lines, and the first one is the one an issue report needs: the pid
    # says which process in Task Manager, the port says which page, and the
    # state directory says where the settings, the config, the backups and
    # this log itself are. Between it and the `stopped:` line below, a log
    # tells the whole story of a session without anything else being asked
    # for (W12, W13).
    log.info("started, pid %d, port %d, state dir %s, lock %s",
             os.getpid(), port, state, lock.kind)
    log.info("serving %s saves=%s config=%s read_only=%s degraded=%s", url,
             st.save_folder, args.config, st.read_only,
             app.degraded.kind if app.degraded else None)
    # Set in the `finally` below, so the polling thread cannot outlive the
    # serve loop it is waiting for. Without it the thread is a three-second
    # promise to open a page on a port this process may already have given up.
    browser_cancel = threading.Event()
    if st.open_browser:
        threading.Thread(target=open_when_ready,
                         args=(url, args.host, port),
                         kwargs={"log": log, "cancel": browser_cancel},
                         daemon=True).start()
    # Now, and not before: the lock is held, the port is bound, and this
    # process is therefore the sorter. See `start_runtime_sweep`.
    start_runtime_sweep(log)
    start_idle_watchdog(httpd, app, log)
    # "the page's stop button or the idle exit" rather than a guess between
    # them: both of those log their own reason first, and this line's job is
    # to exist. A log that ends without a `stopped:` line was killed or
    # crashed, which is the distinction W13 could not make.
    reason = "asked to stop (the page's stop button, or the idle exit)"
    try:
        # Returns when `POST /api/quit` or the idle watchdog calls
        # `httpd.shutdown()`, which is the same 0 as ctrl-c: a stop that was
        # asked for is not a failure.
        httpd.serve_forever()
    except KeyboardInterrupt:
        reason = "ctrl-c"
        print("\nstopped")
    except BaseException as exc:
        reason = "%s: %s" % (type(exc).__name__, exc)
        raise
    finally:
        # First, and before anything that can itself fail: this process is not
        # serving any more, so the browser thread must not open its page.
        browser_cancel.set()
        log.info("stopped: %s (pid %d, port %d)", reason, os.getpid(), port)
        # Released explicitly rather than at interpreter exit, for the same
        # reason the socket is: the next launch asks whether this port is
        # taken, and an answer that lags the process is an answer that sends
        # the operator to a page that is not there.
        lock.release()
        # The port is released before this process exits rather than when the
        # kernel gets round to it, because "a sorter is already running there"
        # is a probe on the port and a lingering listener would make a restart
        # open a server that is no longer there.
        close = getattr(httpd, "server_close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
