"""Shared fixtures.

Nothing here reads or writes the operator's save folder. The save the suite
exercises is built in memory by `tools/make_fixture.py`, written to a pytest
temp directory and thrown away -- including a procedural item id that is not
valid UTF-8, which is the one thing a rival tool gets wrong and the reason the
codec exists.

    python -m pytest tests -q
    python -m pytest tests -q --real-saves PATH    adds the read-only round
                                                   trips over a folder of real
                                                   saves; skipped without it

`--real-saves` must be a *copy* of a save folder. Point it at
`%APPDATA%\\HelloGames\\NMS` and the suite still only reads, but the rule in
this repository is that tests never touch the live folder, so don't.
"""
import logging
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)       # the repository root, for nms_sorter and tools

from nms_sorter import config as cfgmod          # noqa: E402
from nms_sorter import safety as safetymod       # noqa: E402
from nms_sorter.codec import dumps               # noqa: E402
from nms_sorter import itemdb                    # noqa: E402
from nms_sorter.itemdb import db                 # noqa: E402
from nms_sorter import savemodel                 # noqa: E402
from nms_sorter.savemodel import SaveFile        # noqa: E402
from tools import make_fixture                   # noqa: E402


# --------------------------------------------------------------- logging

@pytest.fixture(autouse=True)
def package_logger_left_as_it_was():
    """Put the `nms_sorter` logger back after every test.

    `cli.setup_logging` does what an application's logging setup does: it sets
    the level on the `nms_sorter` logger, replaces its handlers with the one
    writing the log file, and turns propagation *off* so a line is written
    once and not again by whatever the host has attached to the root logger.
    Nothing undoes that, and a logger is process-wide, so the four tests that
    call it left every later test's `caplog` watching the root logger while
    the records went to a log file instead.

    The pytest that Python 3.13 installs hides it -- its `catching_logs` also
    attaches the capturing handler to every non-propagating logger it can
    find -- and the pytest that Python 3.9 installs does not, which is why ten
    tests asserting on `caplog.records` were green on one leg of the matrix
    and red on the other. The state was wrong, not the assertions: restoring
    it makes the suite order-independent and both versions agree.

    Handlers a test added are closed, not just detached: a `RotatingFileHandler`
    left open holds a temp log file that Windows then cannot remove.
    """
    logger = logging.getLogger("nms_sorter")
    level, propagate = logger.level, logger.propagate
    handlers = list(logger.handlers)
    try:
        yield
    finally:
        for handler in list(logger.handlers):
            if handler not in handlers:
                logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:                      # pragma: no cover
                    pass
        for handler in handlers:
            if handler not in logger.handlers:
                logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate


# ---------------------------------------------------------------- options

def pytest_addoption(parser):
    parser.addoption(
        "--real-saves", action="store", default=None, metavar="PATH",
        help="folder of real saveN.hg files (a copy, never the live folder); "
             "enables the read-only round-trip tests")


# ------------------------------------------------------- synthetic fixtures

@pytest.fixture(scope="session")
def repo_root():
    return ROOT


@pytest.fixture(scope="session")
def synthetic_dir(tmp_path_factory):
    """A directory holding the base `save9.hg` and a valid `mf_save9.hg`.

    Session-scoped because building it costs a full encode and nothing in the
    suite writes to it: every test that mutates a save copies it first, or
    takes `synthetic_save`, which re-reads the file.
    """
    d = tmp_path_factory.mktemp("synthetic")
    make_fixture.write_fixture(str(d), "base")
    return str(d)


@pytest.fixture(scope="session")
def synthetic_path(synthetic_dir):
    return os.path.join(synthetic_dir, make_fixture.SAVE_NAME)


@pytest.fixture(scope="session")
def synthetic_payload():
    """The uncompressed payload the fixture was framed from.

    Rebuilt rather than read back, so a test comparing the file against it is
    comparing the framing against the encoder and not against itself.
    """
    return dumps(make_fixture.build_doc("base"))


@pytest.fixture
def synthetic_save(synthetic_path):
    """A freshly decoded `SaveFile` over the base fixture.

    Function-scoped on purpose, unlike the directory it reads: `commit()` from
    a plan mutates the document in place, and the legacy self-test built a new
    `SaveFile` for every sub-case for exactly that reason. Sharing one across
    tests would make the suite order-dependent.
    """
    return SaveFile(synthetic_path)


@pytest.fixture
def synthetic_config():
    """A fresh copy per test: the planner cases mutate it."""
    return make_fixture.synthetic_config()


@pytest.fixture(scope="session")
def fixture_variant(tmp_path_factory):
    """`fixture_variant("expedition")` -> {"variant","save","meta","payload"}.

    Each variant is built once per session and cached.
    """
    cache = {}

    def build(name):
        if name not in cache:
            d = tmp_path_factory.mktemp("variant-" + name.replace("-", "_"))
            cache[name] = make_fixture.write_fixture(str(d), name)
        return cache[name]

    return build


# ------------------------------------------------------------- item table

@pytest.fixture(scope="session")
def idb():
    """The item database.

    It is a process-wide singleton that `apply_overrides()` mutates, and
    `planner.build_plan` calls `apply_overrides(cfg)` on every plan. The
    finalizer puts the generated taxonomy back so a failing test cannot leak a
    custom bucket into the next one.
    """
    inst = db()
    try:
        yield inst
    finally:
        inst.apply_overrides({})


@pytest.fixture(autouse=True)
def _reset_item_overrides():
    """Reset the mutated singleton after every test, not just at session end.

    `apply_overrides({})` is microseconds; an order-dependent suite is not.

    Only if something already built it. Calling `db()` here unconditionally
    would make every test in the suite -- `test_text.py` included -- depend on
    the item table loading, and a text-escaping test has no business failing
    because `data/` moved.
    """
    yield
    if getattr(itemdb, "_DB", None) is not None:
        itemdb._DB.apply_overrides({})


@pytest.fixture(scope="session", autouse=True)
def _browser_guard_for_the_session():
    """`NMS_SORTER_NO_BROWSER=1` for the whole run, and for every child of it.

    This used to be a `delenv`, so that the tests *about* browser opening
    could not be decided by ambient state. Its blast radius was the suite: an
    agent that exported the variable had no protection at all under pytest,
    and neither did any `python -m nms_sorter` a test spawned, because the
    child inherited the stripped environment.

    Set for the session instead. The handful of tests that are about the
    unguarded behaviour take `no_browser_env_off`, which clears it for that
    test only, so ambient state still decides nothing.
    """
    before = os.environ.get("NMS_SORTER_NO_BROWSER")
    os.environ["NMS_SORTER_NO_BROWSER"] = "1"
    yield
    if before is None:
        os.environ.pop("NMS_SORTER_NO_BROWSER", None)
    else:
        os.environ["NMS_SORTER_NO_BROWSER"] = before


@pytest.fixture
def no_browser_env_off(monkeypatch):
    """Clear `NMS_SORTER_NO_BROWSER` for one test.

    Opt in from a test that has to prove what happens with the guard *off* --
    that a second launch opens the running sorter's page, for instance. Every
    other test keeps the session guard.
    """
    monkeypatch.delenv("NMS_SORTER_NO_BROWSER", raising=False)


class _BrowserOpenRecorder(object):
    """What `webbrowser.open` becomes for the whole session."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, *a, **kw):
        self.calls.append(url)
        return True


@pytest.fixture(scope="session", autouse=True)
def no_browser_for_the_whole_session():
    """`webbrowser` cannot open anything while the suite runs, and says so.

    Session-scoped, and that is the whole point. The leak this replaces was a
    daemon thread started by `cli.main` inside a test, polling a port for
    three seconds, and reaching `webbrowser.open` *after* the test had ended
    and its function-scoped `monkeypatch` had put the real function back. Two
    tabs on the operator's desktop per full run, on ports nothing was
    listening on. A patch that is restored while the thread is still alive
    cannot catch that; one that lives as long as the interpreter can.

    A test that wants to assert on opens still patches `cli.webbrowser.open`
    itself; this sits underneath and catches whatever escapes, whenever it
    escapes. At session end an empty recorder is the assertion.
    """
    import webbrowser

    rec = _BrowserOpenRecorder()
    before = (webbrowser.open, webbrowser.open_new, webbrowser.open_new_tab)
    webbrowser.open = rec
    webbrowser.open_new = rec
    webbrowser.open_new_tab = rec
    try:
        yield rec
    finally:
        (webbrowser.open, webbrowser.open_new,
         webbrowser.open_new_tab) = before
    assert not rec.calls, (
        "the suite opened %d browser page(s): %s"
        % (len(rec.calls), ", ".join(repr(u) for u in rec.calls)))


@pytest.fixture(scope="session")
def data_counts():
    """`data/DATA_COUNTS` as a dict, or None if the generator has not run.

    The exact table dimensions belong in a generated file rather than in an
    assertion, because a regeneration moves them: the Corvette bucket took the
    taxonomy from 17 to 18, and any test that had typed 17 would have had to be
    edited in the same change. `itemdb.data_counts()` is the accessor; this
    fixture only adds the "not there yet" case, so a test can skip with a
    reason instead of erroring.
    """
    try:
        return itemdb.data_counts()
    except Exception:
        return None


# ----------------------------------------------------------------- safety

NOT_A_PROCESS = "nms-selftest-not-a-real-process.exe"

#: what `safety.game_status()` answers for a machine with the game closed
GAME_CLOSED = {"running": False, "pids": [], "method": "test", "known": True,
               "process": "NMS.exe"}


@pytest.fixture(scope="module")
def no_game_running():
    """Make step 1 of the write sequence run, and pass.

    It refuses a running game nobody has confirmed is on the main menu, and a
    check that *cannot run* is a refusal too -- which is what happens off
    Windows, where there is no `ctypes.windll` and no `tasklist`. Renaming
    `GAME_PROCESS` was enough on Windows and left every apply test erroring
    in its fixture on the Ubuntu CI leg, so the whole answer is replaced
    instead: `game_status` returns "closed, and known to be closed".

    Deliberately not the main-menu confirmation, which is about a game that
    *is* running and would leave the closed-game branch untested. A test that
    wants the running or the unknown-process-list branch patches `game_status`
    itself; `test_safety.py` has both.

    Module-scoped, via `pytest.MonkeyPatch` rather than the builtin
    function-scoped `monkeypatch`, so a module-scoped apply fixture can hold
    the patch across the assertions it feeds.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(safetymod, "GAME_PROCESS", NOT_A_PROCESS)
        mp.setattr(safetymod, "game_status", lambda: dict(GAME_CLOSED))
        yield NOT_A_PROCESS


# ------------------------------------------------------------- real saves

@pytest.fixture(scope="session")
def real_saves(pytestconfig):
    """The folder named by `--real-saves`, or a skip.

    A string, not a list: `test_codec.py` globs inside it for `mf_save*.hg`
    and needs the directory. `real_save_paths` is the list of saves in it.
    The repository ships no real save and never will, so a run without the
    option skips rather than silently passing on missing input.
    """
    path = pytestconfig.getoption("--real-saves")
    if not path:
        pytest.skip("needs --real-saves PATH (a copy of a save folder)")
    if not os.path.isdir(path):
        raise AssertionError("--real-saves %r is not a directory" % path)
    return path


# Kept as a name because it reads better at the call site than `real_saves`
# when the folder, not the saves, is the subject.
real_saves_dir = real_saves


@pytest.fixture(scope="session")
def real_save_paths(real_saves):
    """Every `saveN.hg` in the folder given to `--real-saves`, sorted."""
    out = [os.path.join(real_saves, fn)
           for fn in sorted(os.listdir(real_saves))
           if fn.lower().startswith("save") and fn.lower().endswith(".hg")]
    if not out:
        pytest.skip("no saveN.hg files in %s" % real_saves)
    return out


# ------------------------------------------------------------------ misc

@pytest.fixture(scope="session")
def container_keys(synthetic_path):
    """(all keys, sortable keys, keys with no cells) -- what
    `config.validate` needs to check a container name against.

    Exactly what `App.container_keys()` builds, extractor cores included. This
    fixture used to list `containers()` alone, so every validator test ran
    against a container set the running program does not have: a rule naming
    `extractor1` as a source was an error here and valid in production, which
    is the shape of defect that ships (write-path review one, defect 4). The
    cores are absent from the *sortable* list on purpose -- nothing is ever
    sorted into one -- and that asymmetry is the thing worth checking.
    """
    save = SaveFile(synthetic_path)
    cs = save.containers() + save.extractor_containers()
    return ([c.key for c in cs], [c.key for c in cs if c.sortable],
            [c.key for c in cs if savemodel.is_dead_container(c)])


@pytest.fixture(scope="session")
def default_config():
    return cfgmod.default_config()
