"""P1-5: the HTTP surface. Every test here runs against a *running* server.

GOAL.md Rule 4: "a fix to `nmsweb/*.py` verified by a fresh `python -c` import
and reported as working was still broken on the live port, twice in one
session." So nothing below pokes a handler method in isolation; each test binds
an ephemeral port, serves a synthetic save out of `tmp_path`, and talks to it
with `http.client`.

Closes D12 (reset leaves plans approvable), D13 (no locking under
`ThreadingHTTPServer`) and D14 (tracebacks to the browser, 500 where 400 is
meant, unbounded bodies).

Deliberately no `conftest.py` dependency: the fixture is here so the file can
be run on its own, and because lane F owns that file.
"""
import contextlib
import http.client
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.parse
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if os.path.dirname(HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(HERE))      # for nms_sorter

from tools import make_fixture as legacy                            # noqa: E402
from nms_sorter import cli                                         # noqa: E402
from nms_sorter import codec                                       # noqa: E402
from nms_sorter import config as cfgmod                            # noqa: E402
from nms_sorter import itemdb as itemdbmod                         # noqa: E402
from nms_sorter import pages                                       # noqa: E402
from nms_sorter import planner                                     # noqa: E402
from nms_sorter import safety                                      # noqa: E402
from nms_sorter import savemodel                                   # noqa: E402
from nms_sorter import server                                      # noqa: E402
from nms_sorter import platform as platformmod                # noqa: E402
from nms_sorter import settings as settingsmod                     # noqa: E402
from nms_sorter.app import App                                     # noqa: E402


# --------------------------------------------------------------------------
# a server on an ephemeral port, against a save that is thrown away
# --------------------------------------------------------------------------

@contextlib.contextmanager
def running(app, **extra):
    """The same server the operator gets, on an ephemeral port (Rule 4).

    `server.serve` puts the app on the handler class, so these run one at a
    time; the P2-2 tests below lean on that when the degraded server promotes
    itself to a live one in place.
    """
    httpd = server.serve(app, "127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(port=port, app=app,
                              origin="http://127.0.0.1:%d" % port, **extra)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


#: what `safety.game_status()` answers on a machine with the game closed, and
#: what every server test gets unless it says otherwise. It used to be
#: `allow_game_running=True` on the app, which is not a state a player can be
#: in any more: the write path reads the process check, so the *answer* is what
#: a test has to arrange. `known: True` matters as much as `running: False` --
#: an answer that could not be got is still a refusal.
GAME_CLOSED = {"running": False, "pids": [], "method": "test", "known": True,
               "process": "NMS.exe"}

#: the game up, which is the primary flow's state. A test that wants it patches
#: `safety.game_status` with this and sends `at_main_menu` or does not.
GAME_RUNNING = {"running": True, "pids": [4242], "method": "test",
                "known": True, "process": "NMS.exe"}

#: no answer at all: off Windows, or a process list something is blocking.
GAME_UNKNOWN = {"running": None, "pids": [], "method": "failed",
                "known": False, "process": "NMS.exe",
                "error": "no windll / no tasklist"}


@pytest.fixture
def srv(tmp_path):
    folder = tmp_path / "saves"
    folder.mkdir()
    save = folder / "save.hg"
    legacy.build_save(str(save))
    cfg_path = tmp_path / "config.json"
    backups = tmp_path / "backups"
    backups.mkdir()

    app = App(str(folder), str(cfg_path), str(backups),
              settings_path=str(tmp_path / "settings.json"),
              log_path=str(tmp_path / "sorter.log"))
    app.config = cfgmod.store(str(cfg_path), legacy.synthetic_config())
    app.sync_taxonomy()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(safety, "game_status", lambda *a, **kw: dict(GAME_CLOSED))
        with running(app, folder=folder, save=save, tmp=tmp_path) as s:
            yield s


# ------------------------------------------------------------------ client

def _conn(srv):
    return http.client.HTTPConnection("127.0.0.1", srv.port, timeout=20)


def get(srv, path, headers=None):
    c = _conn(srv)
    try:
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        return r.status, r.getheader("Content-Type") or "", r.read()
    finally:
        c.close()


def jget(srv, path, headers=None):
    status, ctype, raw = get(srv, path, headers)
    assert "json" in ctype, "%s answered %s with %r" % (path, ctype, raw[:120])
    return status, json.loads(raw.decode("utf-8"))


def post(srv, path, obj=None, raw=None, ctype="application/json",
         origin=None, fake_length=None):
    """One POST. `fake_length` declares a body size without sending it, which
    is how the 8 MB cap is tested without pushing 8 MB up the loopback."""
    if raw is None:
        raw = json.dumps(obj if obj is not None else {}).encode("utf-8")
    c = _conn(srv)
    try:
        c.putrequest("POST", path)
        if ctype:
            c.putheader("Content-Type", ctype)
        if origin:
            c.putheader("Origin", origin)
        c.putheader("Content-Length", str(fake_length if fake_length is not None
                                          else len(raw)))
        c.endheaders()
        if fake_length is None:
            c.send(raw)
        r = c.getresponse()
        body = r.read()
        ct = r.getheader("Content-Type") or ""
        assert "json" in ct, "%s answered %s with %r" % (path, ct, body[:120])
        return r.status, json.loads(body.decode("utf-8"))
    finally:
        c.close()


def is_error(body, kind):
    """Every error body is one sentence plus a machine-readable kind (§3.3).

    `steps` is the one permitted addition and only on a 409: a refusal that
    carries a `safety.Report` hands the page what did happen before the
    refusal (review two, Q1).
    """
    assert set(body) <= {"error", "kind", "where", "ref", "steps"}, body
    assert "steps" not in body or body["kind"] == "refused", body
    assert body.get("kind") == kind, body
    assert isinstance(body.get("error"), str) and body["error"].strip(), body
    assert "Traceback" not in body["error"], body
    return True


# --------------------------------------------------------------------------
# the happy paths
# --------------------------------------------------------------------------

def test_bootstrap_shape(srv):
    status, b = jget(srv, "/api/bootstrap")
    assert status == 200
    for key in ("save_folder", "config_path", "backup_root", "read_only",
                "strict_version_check", "saves",
                "buckets", "config", "game", "item_count", "save", "issues"):
        assert key in b, key
    assert b["save_folder"] == str(srv.folder)
    assert [s["file"] for s in b["saves"]] == ["save.hg"]
    assert b["save"]["file"] == "save.hg"
    assert any(c["key"] == "suit" for c in b["save"]["containers"])


def test_version_shape(srv):
    status, b = jget(srv, "/api/version")
    assert status == 200
    assert set(b) == {"app", "api", "data_version", "config_version", "python",
                      "platform"}
    assert b["api"] == 1
    assert b["config_version"] == cfgmod.CONFIG_VERSION
    assert b["python"].startswith("3.")
    assert b["data_version"] is None or isinstance(b["data_version"], str)


def test_static_still_serves_the_page(srv):
    status, ctype, raw = get(srv, "/")
    assert status == 200 and ctype.startswith("text/html")
    assert b"<html" in raw.lower()
    status, ctype, raw = get(srv, "/static/app.js")
    assert status == 200 and "javascript" in ctype
    # app.js reads `.error` off every failure body (its `api()` wrapper does
    # `new Error(body.error || ...)`), so the key has to survive P1-5
    assert b".error" in raw


# --------------------------------------------------------------------------
# D14: the error shape, the status codes, the caps
# --------------------------------------------------------------------------

def test_unknown_route_is_json_404(srv):
    status, b = jget(srv, "/nope")
    assert status == 404
    assert is_error(b, "not_found")


def test_unknown_static_file_is_json_404(srv):
    status, b = jget(srv, "/static/not-a-file.js")
    assert status == 404
    assert is_error(b, "not_found")


def test_unknown_post_route_is_json_404(srv):
    status, b = post(srv, "/api/nope")
    assert status == 404
    assert is_error(b, "not_found")


def test_bad_json_body_is_400(srv):
    status, b = post(srv, "/api/config", raw=b"{not json at all")
    assert status == 400
    assert is_error(b, "invalid")


def test_missing_config_is_400(srv):
    status, b = post(srv, "/api/config", {})
    assert status == 400
    assert is_error(b, "invalid")
    assert b.get("where") == "config"


def test_unknown_config_field_is_400(srv):
    status, b = post(srv, "/api/config",
                     {"config": cfgmod.default_config(), "patch": {"a": 1}})
    assert status == 400
    assert is_error(b, "invalid")


def test_a_bare_save_name_resolves_against_the_save_folder(srv):
    """The server lists `save.hg` by name, so a client may send it by name.
    It used to be resolved against the process working directory and then
    rejected as "not in the selected save folder"."""
    status, b = post(srv, "/api/select", {"file": "save.hg"})
    assert status == 200, b
    assert b["file"] == "save.hg"


def test_unknown_save_file_is_400_not_500(srv):
    status, b = post(srv, "/api/select", {"file": "save99.hg"})
    assert status == 400
    assert is_error(b, "invalid")


def test_non_integer_query_int_is_400(srv):
    status, b = jget(srv, "/api/items?q=sodium&n=lots")
    assert status == 400
    assert is_error(b, "invalid")


def test_negative_offset_is_400(srv):
    status, b = jget(srv, "/api/browse?offset=-1")
    assert status == 400
    assert is_error(b, "invalid")


def test_bad_fingerprint_token_is_400(srv):
    status, b = post(srv, "/api/apply", {"fingerprint": {"not": "a token"}})
    assert status == 400
    assert is_error(b, "invalid")


def test_wrong_origin_is_403(srv):
    status, b = post(srv, "/api/plan", {}, origin="http://evil.example")
    assert status == 403
    assert is_error(b, "refused")
    assert b["error"] == ("requests from another origin are refused; open the "
                          "page from this server's own address")


def test_own_origin_is_allowed(srv):
    status, _b = post(srv, "/api/plan", {}, origin=srv.origin)
    assert status == 200
    status, _b = post(srv, "/api/plan", {},
                      origin="http://localhost:%d" % srv.port)
    assert status == 200


def test_body_over_cap_is_413(srv):
    status, b = post(srv, "/api/config", fake_length=server.MAX_BODY + 1)
    assert status == 413
    assert is_error(b, "invalid")


def test_wrong_content_type_is_415(srv):
    status, b = post(srv, "/api/plan", {}, ctype="text/plain")
    assert status == 415
    assert is_error(b, "invalid")


def test_items_n_is_clamped(srv):
    status, b = jget(srv, "/api/items?q=&n=1000000000")
    assert status == 200
    assert len(b["items"]) <= 200


def test_browse_limit_is_clamped(srv):
    status, b = jget(srv, "/api/browse?limit=1000000000")
    assert status == 200
    assert len(b["items"]) <= 1000


def test_internal_error_gives_a_ref_not_a_traceback(srv, monkeypatch):
    def boom():
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(server, "buckets_view", boom)
    status, b = jget(srv, "/api/browse")
    assert status == 500
    assert is_error(b, "internal")
    assert len(b.get("ref") or "") >= 6
    assert "trace" not in b and "Traceback" not in json.dumps(b)
    assert os.path.abspath(__file__) not in json.dumps(b)
    assert "nms_sorter" not in b["error"]


# --------------------------------------------------------------------------
# plans: minting, refusing, and D12
# --------------------------------------------------------------------------

def _plan(srv):
    status, plan = post(srv, "/api/plan", {"file": str(srv.save)})
    assert status == 200, plan
    assert plan.get("fingerprint"), plan
    return plan


def test_apply_with_a_wrong_fingerprint_is_refused_and_writes_nothing(srv):
    before = srv.save.read_bytes()
    _plan(srv)
    status, b = post(srv, "/api/apply",
                     {"file": str(srv.save), "fingerprint": "deadbeef"})
    assert status == 409
    assert is_error(b, "refused")
    assert "has not printed a plan" in b["error"]
    assert srv.save.read_bytes() == before


def test_reset_clears_the_minted_plans(srv):
    """D12: /api/config/reset used to leave the plan approvable; the re-plan two
    layers down caught it by accident."""
    before = srv.save.read_bytes()
    plan = _plan(srv)
    status, _b = post(srv, "/api/config/reset", {})
    assert status == 200
    status, b = post(srv, "/api/apply",
                     {"file": str(srv.save), "fingerprint": plan["fingerprint"]})
    assert status == 409
    assert is_error(b, "refused")
    assert "has not printed a plan" in b["error"]
    assert srv.save.read_bytes() == before


def test_saving_the_config_clears_the_minted_plans(srv):
    plan = _plan(srv)
    status, _b = post(srv, "/api/config",
                      {"config": legacy.synthetic_config(), "force": True})
    assert status == 200
    status, b = post(srv, "/api/apply",
                     {"file": str(srv.save), "fingerprint": plan["fingerprint"]})
    assert status == 409
    assert "has not printed a plan" in b["error"]


def test_minted_plans_are_bounded(srv):
    """D13/§3.9: the fingerprint dict used to grow without limit."""
    app = srv.app
    for i in range(40):
        app.mint_plan("fp%02d" % i, "sig")
    assert len(app._plans) == 16
    assert app.plan_signature("fp00") is None
    assert app.plan_signature("fp39") == "sig"


# --------------------------------------------------------------------------
# D13: the threaded server and the shared item database
# --------------------------------------------------------------------------

def test_concurrent_plans_and_config_saves_never_500(srv):
    """Eight threads for two seconds over the two routes that mutate shared
    state: `/api/config` replaces the config and re-applies the taxonomy onto
    the process-global item DB, while `/api/plan` reads it."""
    deadline = time.time() + 2.0
    bad = []
    counts = {"plan": 0, "config": 0}
    lock = threading.Lock()

    def hammer(which):
        cfg = legacy.synthetic_config()
        while time.time() < deadline:
            try:
                if which == "plan":
                    status, body = post(srv, "/api/plan",
                                        {"file": str(srv.save)})
                else:
                    status, body = post(srv, "/api/config",
                                        {"config": json.loads(json.dumps(cfg)),
                                         "force": True})
            except Exception as exc:               # a torn connection counts
                with lock:
                    bad.append("%s raised %r" % (which, exc))
                return
            if status >= 500:
                with lock:
                    bad.append("%s -> %s %s" % (which, status, body))
                return
            with lock:
                counts[which] += 1

    threads = [threading.Thread(target=hammer,
                                args=("plan" if i % 2 else "config",))
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not bad, bad[:4]
    assert counts["plan"] > 0 and counts["config"] > 0, counts
    assert len(srv.app._plans) <= 16


# ==========================================================================
# P2-1: the settings routes
# ==========================================================================

def test_get_settings_shape(srv):
    status, b = jget(srv, "/api/settings")
    assert status == 200
    assert set(b) == {"settings", "fields", "path", "first_run", "overridden",
                      "load_error"}
    # W15: None unless the settings file exists and could not be parsed.
    assert b["load_error"] is None
    assert b["path"] == str(srv.tmp / "settings.json")
    assert b["first_run"] is False
    assert b["settings"]["settings_version"] == 1
    assert b["settings"]["save_folder"] == str(srv.folder)
    keys = [f["key"] for f in b["fields"]]
    assert keys == [f["key"] for f in settingsmod.FIELDS]
    for f in b["fields"]:
        assert set(f) == {"key", "type", "description", "default"}
        assert f["description"].strip()
    assert "allow_game_running" not in [f["key"] for f in b["fields"]], \
        "the opt-in is gone: the main menu is the documented flow"


def test_post_settings_stores_and_applies(srv):
    status, b = post(srv, "/api/settings",
                     {"settings": {"backup_keep": 7, "read_only": True}})
    assert status == 200, b
    assert b["settings"]["backup_keep"] == 7
    assert b["settings"]["read_only"] is True
    assert b["restart_required"] == []
    # applied live, not just stored
    assert srv.app.read_only is True
    # and stored, not just applied
    with open(str(srv.tmp / "settings.json"), encoding="utf-8") as fh:
        assert json.load(fh)["backup_keep"] == 7
    # read_only is a real gate the moment it is set
    status, b = post(srv, "/api/apply", {"fingerprint": "deadbeef"})
    assert status == 409
    assert "--read-only" in b["error"]


def test_post_settings_says_what_needs_a_restart(srv):
    status, b = post(srv, "/api/settings",
                     {"settings": {"port": 8123, "open_browser": False,
                                   "backup_keep": 9}})
    assert status == 200, b
    assert sorted(b["restart_required"]) == ["open_browser", "port"]
    assert b["settings"]["port"] == 8123
    # the running server did not move
    status, _ = jget(srv, "/api/version")
    assert status == 200


def test_post_settings_wrong_shape_is_400_with_where(srv):
    status, b = post(srv, "/api/settings", {})
    assert status == 400 and is_error(b, "invalid")
    assert b["where"] == "settings"

    status, b = post(srv, "/api/settings", {"settings": [1, 2]})
    assert status == 400 and b["where"] == "settings"

    status, b = post(srv, "/api/settings", {"settings": {}, "force": True})
    assert status == 400 and b["where"] == "force"

    status, b = post(srv, "/api/settings", {"settings": {"port": 80}})
    assert status == 400 and b["where"] == "port"
    assert "1024" in b["error"]

    status, b = post(srv, "/api/settings", {"settings": {"backup_keep": 0}})
    assert status == 400 and b["where"] == "backup_keep"


def test_changing_the_save_folder_relists_the_saves_and_drops_the_plans(
        srv, tmp_path):
    plan = _plan(srv)
    other = tmp_path / "other-saves"
    other.mkdir()
    legacy.build_save(str(other / "save5.hg"))

    status, b = post(srv, "/api/settings",
                     {"settings": {"save_folder": str(other)}})
    assert status == 200, b
    assert b["settings"]["save_folder"] == str(other)
    assert b["first_run"] is False

    status, boot = jget(srv, "/api/bootstrap")
    assert status == 200
    assert boot["save_folder"] == str(other)
    assert [s["file"] for s in boot["saves"]] == ["save5.hg"]

    # a fingerprint minted against the old folder is no longer approvable
    status, b = post(srv, "/api/apply",
                     {"fingerprint": plan["fingerprint"]})
    assert status == 409
    assert "has not printed a plan" in b["error"]


def test_a_save_folder_with_no_saves_is_a_warning_not_an_error(srv, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    status, b = post(srv, "/api/settings",
                     {"settings": {"save_folder": str(empty)}})
    assert status == 200, b
    assert b["first_run"] is True
    assert any(n["where"] == "save_folder" and n["level"] == "warning"
               for n in b["notes"])
    # and / falls back to the first-run page while that is true
    status, ctype, raw = get(srv, "/")
    assert status == 200 and ctype.startswith("text/html")
    assert b"holds no" in raw


def test_settings_posted_as_a_form_redirects_to_the_page(srv, tmp_path):
    """The first-run page has to work with scripting switched off, so the two
    routes it posts to accept a form body and answer with a redirect."""
    other = tmp_path / "form-saves"
    other.mkdir()
    legacy.build_save(str(other / "save3.hg"))
    c = _conn(srv)
    try:
        body = urllib.parse.urlencode({"save_folder": str(other)})
        c.request("POST", "/api/settings", body=body, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": srv.origin})
        r = c.getresponse()
        r.read()
        assert r.status == 303
        assert r.getheader("Location") == "/"
    finally:
        c.close()
    assert srv.app.save_folder == str(other)


def test_a_form_post_to_another_route_is_415(srv):
    c = _conn(srv)
    try:
        c.request("POST", "/api/plan", body="file=x", headers={
            "Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse()
        body = json.loads(r.read().decode("utf-8"))
        assert r.status == 415
        assert is_error(body, "invalid")
    finally:
        c.close()


# ==========================================================================
# P2-1: health and backups
# ==========================================================================

def test_health_shape(srv):
    status, b = jget(srv, "/api/health")
    assert status == 200
    assert set(b) == {"save_folder", "saves", "save_platform", "game",
                      "roundtrip", "data_version", "data_counts", "config",
                      "backups", "log_path", "app_version", "python",
                      "platform", "settings_path", "first_run", "degraded",
                      "read_only",
                      "strict_version_check", "idle_exit_minutes"}
    assert b["save_folder"] == str(srv.folder)
    assert b["idle_exit_minutes"] == 30
    assert b["saves"] == 1
    assert set(b["roundtrip"]) == {"state", "detail"}
    assert b["roundtrip"]["state"] == "ok"
    assert b["config"] == {"path": str(srv.tmp / "config.json"),
                           "version": cfgmod.CONFIG_VERSION}
    assert b["backups"] == {"root": str(srv.tmp / "backups"), "count": 0}
    assert b["log_path"] == str(srv.tmp / "sorter.log")
    assert b["settings_path"] == str(srv.tmp / "settings.json")
    assert b["first_run"] is False
    assert b["degraded"] is None
    assert b["python"].startswith("3.")
    assert isinstance(b["game"], dict) and "running" in b["game"]
    assert b["data_counts"] is None or isinstance(b["data_counts"], dict)


def test_health_and_the_save_view_carry_the_saves_own_platform(srv):
    """The save's root `Platform` -- "Win|Final" -- next to, and distinct from,
    the host's `platform`. The health panel's copy-as-text is what a bug report
    pastes and the issue template asks for it: a save written by a build this
    tool has never been pointed at is the first thing to establish, and the
    machine the sorter ran on does not answer that."""
    status, b = jget(srv, "/api/health")
    assert status == 200
    said = b["save_platform"]
    assert said and isinstance(said, str)
    assert "|" in said, said                       # "<platform>|<build>"
    assert said != b["platform"], "the save's word, not the host's"

    status, view = post(srv, "/api/select", {"file": "save.hg"})
    assert status == 200
    assert view["save_platform"] == said

    # and it is the document's own field, not a guess from the host
    save = savemodel.SaveFile(str(srv.save))
    assert save.d.get(save.doc, "Platform") == said


def test_a_save_with_no_platform_field_reports_none(srv, monkeypatch):
    """An older save, or one this build has not seen: the field is absent
    rather than invented, so the panel can say "not recorded" instead of
    showing the host's platform twice."""
    save = srv.app.save()
    monkeypatch.delitem(save.doc, save.d.key(save.doc, "Platform"))
    assert server.save_platform(save) is None
    status, b = jget(srv, "/api/health")
    assert status == 200 and b["save_platform"] is None


def test_health_caches_the_roundtrip_per_signature(srv, monkeypatch):
    jget(srv, "/api/health")
    calls = []
    real = safety.verify_roundtrip

    def counted(path, report=None):
        calls.append(path)
        return real(path, report)

    monkeypatch.setattr(safety, "verify_roundtrip", counted)
    jget(srv, "/api/health")
    jget(srv, "/api/health")
    assert calls == []                      # answered from the cache
    srv.app._roundtrip = (None, None)
    jget(srv, "/api/health")
    assert len(calls) == 1


def test_backups_is_an_empty_list_until_one_exists(srv):
    status, b = jget(srv, "/api/backups")
    assert status == 200
    assert b == {"root": str(srv.tmp / "backups"), "backups": []}


def test_backups_is_a_pass_through_of_safety_list_backups(srv, monkeypatch):
    """The route adds nothing and drops nothing: whatever `safety.list_backups`
    reports about a folder is what the page is shown, because the page's job is
    to say `sha256_ok` and `has_manifest` out loud."""
    rows = [{"folder": "x", "stamp": "20260914-010203", "save": "save.hg",
             "size": 1, "sha256_ok": True, "has_manifest": True}]
    monkeypatch.setattr(safety, "list_backups", lambda root: rows)
    status, b = jget(srv, "/api/backups")
    assert status == 200
    assert b["backups"] == rows


def test_a_backup_root_that_cannot_be_listed_does_not_break_the_route(
        srv, monkeypatch):
    """A disconnected drive or a permission change. `/api/backups` and the
    health panel are both polled and neither has anywhere to put an
    exception, so the answer is `[]` and a line in the log."""
    def boom(root):
        raise OSError("the backup folder went away")

    monkeypatch.setattr(safety, "list_backups", boom)
    status, b = jget(srv, "/api/backups")
    assert status == 200 and b["backups"] == []


# ==========================================================================
# P2-3: detect-saves and the first-run page
# ==========================================================================

@pytest.fixture
def appdata(tmp_path, monkeypatch):
    """`%APPDATA%\\HelloGames\\NMS` as a temp tree with two profiles.

    The real folder is never read by this test. `detect-saves` probes exactly
    this one root and nothing else (Rule 1).

    Windows only, and not because of the path separators: off Windows
    `platform.default_save_root()` is None by design, because
    `%APPDATA%\\HelloGames\\NMS` is not a path that exists there and a guess
    would sort the wrong account (GOAL.md §2.3, P4-1). The off-Windows
    behaviour has its own tests below.
    """
    if not platformmod.is_windows():
        pytest.skip("there is no %APPDATA% save root off Windows (P4-1)")
    root = tmp_path / "appdata" / "HelloGames" / "NMS"
    (root / "st_111").mkdir(parents=True)
    (root / "st_222").mkdir(parents=True)
    (root / "DefaultUser").mkdir(parents=True)
    (root / "not-a-profile").mkdir(parents=True)
    (root / "st_111" / "save.hg").write_bytes(b"x")
    (root / "st_111" / "mf_save.hg").write_bytes(b"x")
    (root / "st_222" / "save2.hg").write_bytes(b"x")
    (root / "st_222" / "save4.hg").write_bytes(b"x")
    os.utime(str(root / "st_111" / "save.hg"), (1_700_000_000, 1_700_000_000))
    os.utime(str(root / "st_222" / "save2.hg"), (1_800_000_000, 1_800_000_000))
    os.utime(str(root / "st_222" / "save4.hg"), (1_750_000_000, 1_750_000_000))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    return root


def test_detect_saves_probes_one_root_and_reports_what_it_found(srv, appdata):
    status, b = jget(srv, "/api/detect-saves")
    assert status == 200
    assert b["root"] == str(appdata)
    assert b["root_exists"] is True
    paths = [f["path"] for f in b["folders"]]
    assert paths[0] == str(appdata / "st_222")          # newest save first
    assert str(appdata / "st_111") in paths
    assert str(appdata / "DefaultUser") in paths
    assert str(appdata / "not-a-profile") not in paths  # no other strategy
    by_path = dict((f["path"], f) for f in b["folders"])
    assert by_path[str(appdata / "st_222")]["save_count"] == 2
    assert by_path[str(appdata / "st_222")]["newest_save_mtime"] == 1_800_000_000
    assert by_path[str(appdata / "st_111")]["save_count"] == 1
    assert by_path[str(appdata / "DefaultUser")]["save_count"] == 0
    assert by_path[str(appdata / "DefaultUser")]["newest_save_mtime"] is None


def test_detect_saves_with_no_root_at_all(srv, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "nowhere"))
    status, b = jget(srv, "/api/detect-saves")
    assert status == 200
    assert b["folders"] == [] and b["root_exists"] is False


@pytest.mark.skipif(platformmod.is_windows(),
                    reason="this is the off-Windows answer")
def test_detect_saves_off_windows_says_there_is_nowhere_to_look(srv):
    """No root, no folders, and a sentence that points at the two ways to say
    where the saves are. Not an empty `%APPDATA%`, which would read as "your
    save folder is missing" on a machine that never had one."""
    status, b = jget(srv, "/api/detect-saves")
    assert status == 200
    assert b["root"] is None
    assert b["root_exists"] is False
    assert b["folders"] == []
    assert "--folder" in b["error"]
    assert "Settings section" in b["error"]


@pytest.mark.skipif(platformmod.is_windows(),
                    reason="this is the off-Windows answer")
def test_the_first_run_page_off_windows_still_offers_the_type_in_field(
        first_run_srv):
    """Browsing works everywhere, so the folder picker has to render with no
    candidates at all rather than 500 on a root that is None (P4-1)."""
    status, ctype, raw = get(first_run_srv, "/")
    assert status == 200 and ctype.startswith("text/html")
    page = raw.decode("utf-8")
    assert "<h1>" in page
    assert "folder" in page.lower()


@pytest.fixture
def first_run_srv(tmp_path):
    """A server with no save folder chosen: the P2-3 state."""
    cfg_path = tmp_path / "config.json"
    st = settingsmod.Settings(backup_folder=str(tmp_path / "backups"))
    st.first_run = True
    app = App(config_path=str(cfg_path), settings=st,
              settings_path=str(tmp_path / "settings.json"),
              log_path=None)
    with running(app, tmp=tmp_path) as s:
        yield s


def test_the_first_run_page_is_served_when_no_folder_is_chosen(first_run_srv,
                                                               appdata):
    """"Save folder not found": no save
    folder is not an error page, it is the first-run page -- the candidates
    this build will talk about, rendered server-side, with a text field for a
    save folder it did not find. No auto-detect beyond
    `%APPDATA%\\HelloGames\\NMS`, because a wrong guess sorts the wrong
    account."""
    assert first_run_srv.app.first_run is True
    status, ctype, raw = get(first_run_srv, "/")
    assert status == 200 and ctype.startswith("text/html")
    page = raw.decode("utf-8")
    assert "<h1>" in page
    assert str(appdata / "st_222") in page          # rendered without any JS
    assert "Use this folder" in page
    assert 'action="/api/settings"' in page


def test_the_first_run_page_lists_the_three_paths_when_nothing_is_found(
        first_run_srv, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "nowhere"))
    status, _ctype, raw = get(first_run_srv, "/")
    page = raw.decode("utf-8")
    assert status == 200
    assert pages.NO_SAVES_H1.replace("'", "&#x27;") in page
    assert "DefaultUser" in page and "st_" in page
    assert "not supported" in page


def test_choosing_a_folder_turns_the_first_run_page_into_the_app(
        first_run_srv, tmp_path):
    folder = tmp_path / "chosen"
    folder.mkdir()
    legacy.build_save(str(folder / "save7.hg"))

    status, b = post(first_run_srv, "/api/settings",
                     {"settings": {"save_folder": str(folder)}})
    assert status == 200, b
    assert b["first_run"] is False

    status, ctype, raw = get(first_run_srv, "/")
    assert status == 200 and ctype.startswith("text/html")
    assert b"<html" in raw.lower() and b"app.js" in raw

    status, boot = jget(first_run_srv, "/api/bootstrap")
    assert [s["file"] for s in boot["saves"]] == ["save7.hg"]
    # and it is remembered
    with open(str(tmp_path / "settings.json"), encoding="utf-8") as fh:
        assert json.load(fh)["save_folder"] == str(folder)


def test_bootstrap_still_answers_200_with_issues_on_the_first_run(
        first_run_srv):
    """DECISIONS.md resolution 4: the page must render its folder picker and
    its settings even when no save can be loaded."""
    status, b = jget(first_run_srv, "/api/bootstrap")
    assert status == 200
    assert b["save"] is None
    assert b["issues"] and b["issues"][0]["level"] == "error"
    assert b["save_folder"] == ""


# ==========================================================================
# P2-2: the three degraded startups
# ==========================================================================

@pytest.fixture
def degraded(tmp_path, monkeypatch):
    """Build the `Degraded` the CLI would build, for each of (b), (c), (d)."""
    folder = tmp_path / "saves"
    folder.mkdir()
    legacy.build_save(str(folder / "save.hg"))
    cfg_path = tmp_path / "config.json"

    def build(kind):
        if kind == "config_unreadable":
            cfg_path.write_text("{ this is not json", encoding="utf-8")
        elif kind == "config_too_new":
            cfg_path.write_text(json.dumps({"config_version": 99}),
                                encoding="utf-8")
        elif kind == "data_missing":
            def boom(name):
                raise OSError(2, "No such file or directory", name)

            monkeypatch.setattr(itemdbmod, "_DB", None)
            monkeypatch.setattr(itemdbmod, "_read_data", boom)
        else:
            raise AssertionError(kind)
        st = settingsmod.Settings(save_folder=str(folder),
                                  backup_folder=str(tmp_path / "backups"))
        app = cli.build_app(st, str(cfg_path),
                            str(tmp_path / "settings.json"),
                            str(tmp_path / "sorter.log"))
        assert app.degraded is not None, "expected a degraded app for " + kind
        assert app.degraded.kind == kind, app.degraded.kind
        return app, cfg_path

    return build


DEGRADED_H1 = {
    "config_unreadable": pages.CONFIG_UNREADABLE_H1,
    "config_too_new": pages.CONFIG_TOO_NEW_H1,
    "data_missing": pages.DATA_MISSING_H1,
}


@pytest.mark.parametrize("kind", sorted(DEGRADED_H1))
def test_a_startup_failure_is_a_page_at_the_root(degraded, kind):
    app, cfg_path = degraded(kind)
    with running(app) as srv:
        status, ctype, raw = get(srv, "/")
        assert status == 200
        assert ctype.startswith("text/html")
        page = raw.decode("utf-8")
        assert "<h1>%s</h1>" % DEGRADED_H1[kind] in page
        assert "Traceback" not in page
        if kind.startswith("config"):
            assert str(cfg_path) in page


@pytest.mark.parametrize("kind", sorted(DEGRADED_H1))
def test_every_other_route_is_503_json_while_degraded(degraded, kind):
    app, _cfg = degraded(kind)
    with running(app) as srv:
        for path in ("/api/bootstrap", "/api/save", "/api/browse",
                     "/api/items", "/api/game", "/static/app.js"):
            status, b = jget(srv, path)
            assert status == 503, (path, status)
            assert is_error(b, "refused")
            assert "reduced mode" in b["error"]
        for path in ("/api/plan", "/api/apply", "/api/config", "/api/select"):
            status, b = post(srv, path, {})
            assert status == 503, (path, status)
            assert is_error(b, "refused")


@pytest.mark.parametrize("kind", sorted(DEGRADED_H1))
def test_the_page_can_still_read_health_and_settings(degraded, kind):
    app, _cfg = degraded(kind)
    with running(app) as srv:
        status, b = jget(srv, "/api/health")
        assert status == 200
        assert b["degraded"] == kind
        assert b["config"]["version"] is None
        assert b["roundtrip"]["state"] == "unknown"
        status, b = jget(srv, "/api/settings")
        assert status == 200 and b["settings"]["settings_version"] == 1
        status, b = jget(srv, "/api/detect-saves")
        assert status == 200 and "folders" in b
        status, b = post(srv, "/api/settings",
                         {"settings": {"backup_keep": 5}})
        assert status == 200 and b["settings"]["backup_keep"] == 5


def test_only_the_unreadable_config_offers_the_repair(degraded):
    app, cfg_path = degraded("config_too_new")
    with running(app) as srv:
        status, b = post(srv, "/api/config/reset", {})
        assert status == 503
        assert is_error(b, "refused")
    assert json.load(open(str(cfg_path), encoding="utf-8"))["config_version"] \
        == 99                                    # the file is untouched


def test_the_unreadable_config_can_be_moved_aside_and_the_app_recovers(
        degraded, tmp_path):
    app, cfg_path = degraded("config_unreadable")
    with running(app) as srv:
        status, b = post(srv, "/api/config/reset", {})
        assert status == 200, b
        assert b["recovered"] is True
        assert ".bad-" in b["moved_to"]
        assert os.path.exists(b["moved_to"])
        # the broken file is kept, not deleted
        with open(b["moved_to"], encoding="utf-8") as fh:
            assert fh.read().startswith("{ this is not json")
        # and the same URL now serves the application
        status, ctype, raw = get(srv, "/")
        assert status == 200 and b"app.js" in raw
        status, boot = jget(srv, "/api/bootstrap")
        assert status == 200 and boot["save"] is not None
    assert json.load(open(str(cfg_path), encoding="utf-8"))["config_version"] \
        == cfgmod.CONFIG_VERSION


def test_the_too_new_page_names_both_versions(degraded):
    app, _cfg = degraded("config_too_new")
    with running(app) as srv:
        _status, _ctype, raw = get(srv, "/")
        page = raw.decode("utf-8")
        assert ">99<" in page
        assert ">%d<" % cfgmod.CONFIG_VERSION in page


def test_the_data_page_names_the_file_that_could_not_be_read(degraded):
    app, _cfg = degraded("data_missing")
    with running(app) as srv:
        _status, _ctype, raw = get(srv, "/")
        page = raw.decode("utf-8")
        assert "nms_sorter/data/" in page
        assert "incomplete" in page


# ==========================================================================
# P2-1: both fingerprint forms (GOAL.md §3.9)
# ==========================================================================

def test_plan_mints_both_fingerprint_forms(srv):
    plan = _plan(srv)
    assert len(plan["fingerprint"]) == 8
    assert len(plan["fingerprint_full"]) == 64
    assert all(c in "0123456789abcdef" for c in plan["fingerprint_full"])
    sig = srv.app.plan_signature(plan["fingerprint"])
    assert sig and srv.app.plan_signature(plan["fingerprint_full"]) == sig


def _spy_on_apply(monkeypatch):
    """Record the fingerprint `do_apply` hands `safety.apply_plan`. -> [seen]."""
    seen = []

    def fake_apply(path, cfg, fingerprint, builder, root, **kw):
        seen.append(fingerprint)
        return safety.Report(), {"rows": 0}

    monkeypatch.setattr(server.safety, "apply_plan", fake_apply)
    return seen


def test_apply_forwards_the_full_digest_whichever_form_it_was_given(
        srv, monkeypatch):
    """R10, GOAL.md §3.9: "the first 8 hex characters are shown to the person,
    the full digest is compared". The route used to pass the client's token
    straight through, and `safety._fingerprint_matches` compares the prefix
    when a prefix is what arrived -- so the shipped page's short form silently
    narrowed the gate to 32 bits. The server minted both; the short one is
    looked up, never forwarded."""
    seen = _spy_on_apply(monkeypatch)
    for which in ("fingerprint", "fingerprint_full"):
        plan = _plan(srv)
        status, b = post(srv, "/api/apply",
                         {"file": str(srv.save), "fingerprint": plan[which]})
        assert status == 200, b
        assert seen[-1] == plan["fingerprint_full"]
    assert [len(f) for f in seen] == [64, 64]


def test_a_plan_with_no_full_digest_at_all_still_applies(srv, monkeypatch):
    """The fallback, and the only case where 8 characters reach the write path:
    a planner build that mints no `fingerprint_full` has no wider digest to
    compare, and refusing every apply would be worse than the 32-bit gate that
    was the whole behaviour until §3.9."""
    real = planner.build_plan

    def no_full(save, cfg, **kw):
        plan, commit = real(save, cfg, **kw)
        plan.fingerprint_full = None
        return plan, commit

    monkeypatch.setattr(server.planner, "build_plan", no_full)
    seen = _spy_on_apply(monkeypatch)
    plan = _plan(srv)
    assert "fingerprint_full" not in plan
    status, b = post(srv, "/api/apply",
                     {"file": str(srv.save), "fingerprint": plan["fingerprint"]})
    assert status == 200, b
    assert seen == [plan["fingerprint"]]


def test_apply_prunes_the_backups_and_says_what_it_removed(srv, monkeypatch):
    """R8: `backup_keep` had no reader anywhere -- the Settings tab stated a
    retention policy that did not exist and every apply left a full copy of the
    save for ever. The rules are lane B's; what this asserts is that the call
    happens after a successful write and that the answer reaches the operator,
    because a backup removed silently is indistinguishable from one lost."""
    _spy_on_apply(monkeypatch)
    called = []

    def fake_prune(root, keep):
        called.append((root, keep))
        return [os.path.join(root, "20260101-010101-save")]

    monkeypatch.setattr(safety, "prune_backups", fake_prune)
    post(srv, "/api/settings", {"settings": {"backup_keep": 3}})
    plan = _plan(srv)
    status, b = post(srv, "/api/apply",
                     {"file": str(srv.save), "fingerprint": plan["fingerprint"]})
    assert status == 200, b
    assert called == [(str(srv.tmp / "backups"), 3)]
    assert b["result"]["pruned"] == [
        os.path.join(str(srv.tmp / "backups"), "20260101-010101-save")]


def test_a_failing_prune_does_not_fail_the_apply(srv, monkeypatch):
    """The write is done and proved by the time retention runs. A backup folder
    that cannot be removed is a line in the log, not a 500 on a request that
    succeeded."""
    _spy_on_apply(monkeypatch)

    def boom(root, keep):
        raise OSError("the backup folder is held by something")

    monkeypatch.setattr(safety, "prune_backups", boom)
    plan = _plan(srv)
    status, b = post(srv, "/api/apply",
                     {"file": str(srv.save), "fingerprint": plan["fingerprint"]})
    assert status == 200, b
    assert b["result"]["pruned"] == []


def test_prune_is_not_called_without_a_keep_setting(srv, monkeypatch):
    """`backup_keep` is the policy. Absent or zero means "keep everything",
    which must not become `keep=0` and delete the lot."""
    _spy_on_apply(monkeypatch)
    called = []
    monkeypatch.setattr(safety, "prune_backups",
                        lambda root, keep: called.append(keep) or [])
    monkeypatch.setattr(srv.app.settings, "backup_keep", 0)
    plan = _plan(srv)
    status, _b = post(srv, "/api/apply",
                      {"file": str(srv.save), "fingerprint": plan["fingerprint"]})
    assert status == 200
    assert called == []


def test_a_64_character_token_is_passed_through_untouched(srv, monkeypatch):
    """It *is* the full digest, so there is nothing to look up -- and a token
    this server never minted is refused before any of this, by the signature
    check."""
    seen = _spy_on_apply(monkeypatch)
    plan = _plan(srv)
    full = plan["fingerprint_full"]
    assert len(full) == 64
    status, b = post(srv, "/api/apply",
                     {"file": str(srv.save), "fingerprint": full})
    assert status == 200, b
    assert seen == [full]


# ==========================================================================
# the main-menu flow: the four game states, for apply and for restore
# ==========================================================================
#
# The primary documented flow is: save in game, quit to the main menu, stay
# there, sort, load the save again. So a running game is not a refusal; a
# running game nobody has placed on the menu is. These go through the real
# write path rather than a spy, because the sentence a player reads is the
# point and it is `safety`'s.


def _game(monkeypatch, answer):
    monkeypatch.setattr(safety, "game_status", lambda *a, **kw: dict(answer))


def _applied(srv, **body):
    """One real apply through the route. -> the response body."""
    plan = _plan(srv)
    request = {"file": str(srv.save), "fingerprint": plan["fingerprint"]}
    request.update(body)
    status, b = post(srv, "/api/apply", request)
    assert status == 200, b
    return b


def _step(body, label="game"):
    rows = [s for s in body.get("steps", []) if s["label"] == label]
    assert rows, body.get("steps")
    return rows[0]


def test_apply_from_the_main_menu_writes_with_the_game_up(srv, monkeypatch):
    """The primary flow, end to end on the port."""
    _game(monkeypatch, GAME_RUNNING)
    before = srv.save.read_bytes()
    b = _applied(srv, at_main_menu=True)
    assert srv.save.read_bytes() != before, "the save was written"
    step = _step(b)
    assert step["state"] == "info"
    assert step["detail"] == ("running as pid 4242: at the main menu, as "
                             "confirmed"), step
    man = b["result"]["manifest"]
    assert man["game"] == "game running: at the main menu, as confirmed"


def test_apply_while_the_game_runs_without_the_tick_names_the_tick(
        srv, monkeypatch):
    _game(monkeypatch, GAME_RUNNING)
    before = srv.save.read_bytes()
    plan = _plan(srv)
    status, b = post(srv, "/api/apply",
                     {"file": str(srv.save), "fingerprint": plan["fingerprint"]})
    assert status == 409, b
    assert is_error(b, "refused")
    assert "NMS.exe is running as pid 4242" in b["error"], b
    assert "I am at the main menu, not in a loaded save" in b["error"], b
    assert srv.save.read_bytes() == before, "and nothing was written"


def test_apply_with_the_game_closed_asks_for_no_tick(srv):
    """The secondary path, and the fixture's default answer: no confirmation
    is sent and the apply runs."""
    before = srv.save.read_bytes()
    b = _applied(srv)
    assert srv.save.read_bytes() != before
    step = _step(b)
    assert step["state"] == "ok", step
    assert b["result"]["manifest"]["game"] == "game closed"


def test_apply_is_refused_when_the_process_list_will_not_read(srv, monkeypatch):
    """The tick says where the player is, not whether the game is up, so it
    cannot answer a question that could not be asked at all."""
    _game(monkeypatch, GAME_UNKNOWN)
    before = srv.save.read_bytes()
    plan = _plan(srv)
    for body in ({}, {"at_main_menu": True}):
        request = {"file": str(srv.save), "fingerprint": plan["fingerprint"]}
        request.update(body)
        status, b = post(srv, "/api/apply", request)
        assert status == 409, b
        assert "could not read the process list" in b["error"], b
        assert "blocking the process list" in b["error"], b
    assert srv.save.read_bytes() == before


def _backup_of_one_apply(srv):
    """Apply once with the game closed, and name the folder it backed up to."""
    b = _applied(srv)
    return os.path.basename(b["result"]["backup"])


def test_restore_from_the_main_menu_puts_the_backup_back(srv, monkeypatch):
    folder = _backup_of_one_apply(srv)
    written = srv.save.read_bytes()
    _game(monkeypatch, GAME_RUNNING)
    status, b = post(srv, "/api/restore", {"folder": folder,
                                           "at_main_menu": True})
    assert status == 200, b
    assert srv.save.read_bytes() != written, "the backup is back"
    step = _step(b)
    assert step["state"] == "info"
    assert step["detail"] == ("running as pid 4242: at the main menu, as "
                             "confirmed"), step


def test_restore_while_the_game_runs_without_the_tick_names_the_tick(
        srv, monkeypatch):
    folder = _backup_of_one_apply(srv)
    written = srv.save.read_bytes()
    _game(monkeypatch, GAME_RUNNING)
    status, b = post(srv, "/api/restore", {"folder": folder})
    assert status == 409, b
    assert is_error(b, "refused")
    assert "I am at the main menu, not in a loaded save" in b["error"], b
    assert srv.save.read_bytes() == written, "and nothing was written"


def test_restore_with_the_game_closed_asks_for_no_tick(srv):
    folder = _backup_of_one_apply(srv)
    written = srv.save.read_bytes()
    status, b = post(srv, "/api/restore", {"folder": folder})
    assert status == 200, b
    assert srv.save.read_bytes() != written
    assert _step(b)["state"] == "ok"


def test_restore_is_refused_when_the_process_list_will_not_read(srv,
                                                                monkeypatch):
    folder = _backup_of_one_apply(srv)
    written = srv.save.read_bytes()
    _game(monkeypatch, GAME_UNKNOWN)
    status, b = post(srv, "/api/restore", {"folder": folder,
                                           "at_main_menu": True})
    assert status == 409, b
    assert "could not read the process list" in b["error"], b
    assert srv.save.read_bytes() == written


# ==========================================================================
# P2-5 (server half): POST /api/restore
# ==========================================================================

RESTORE_SENTENCE = ("this server was started with --read-only, so it will not "
                    "write to a save. Restart without that flag if you meant "
                    "to apply.")


def _fake_restore(monkeypatch, srv, result=None):
    """Stand in for lane B's `safety.restore`. -> the calls it recorded.

    The write half of P2-5 is lane B's; the route is this ticket's. So the
    route is exercised against the signature the two lanes agreed --
    `restore(folder, save_dir, report=None, at_main_menu=False) ->
    (Report, dict)` -- rather than against whichever half landed first.
    """
    calls = []

    def fake(folder, save_dir, report=None, at_main_menu=False):
        calls.append({"folder": folder, "save_dir": save_dir,
                      "at_main_menu": at_main_menu})
        if report is not None:
            report.ok("restore", "%s put back into %s" % (folder, save_dir))
        return report, dict(result or {"restored": ["save.hg", "mf_save.hg"],
                                       "backup": folder,
                                       "manifest": {"save": {"file": "save.hg"}}})

    monkeypatch.setattr(safety, "restore", fake)
    return calls


def _a_backup_folder(srv, name="20260914-010203-save"):
    folder = srv.tmp / "backups" / name
    folder.mkdir(parents=True)
    (folder / "save.hg").write_bytes(srv.save.read_bytes())
    (folder / "manifest.json").write_text("{}", encoding="utf-8")
    return folder


def test_restore_puts_a_backup_back_and_answers_like_apply(srv, monkeypatch):
    """Same shape as apply -- `{steps, result, save}` -- because the page draws
    both cards from it, and Undo is the same button as Restore (P2-6)."""
    folder = _a_backup_folder(srv)
    calls = _fake_restore(monkeypatch, srv)
    plan = _plan(srv)
    assert srv.app.plan_signature(plan["fingerprint"])

    status, b = post(srv, "/api/restore", {"folder": folder.name})
    assert status == 200, b
    assert set(b) >= {"steps", "result", "save"}
    assert b["steps"] and b["steps"][0]["label"] == "restore"
    assert b["save"]["file"] == "save.hg"
    assert calls == [{"folder": str(folder), "save_dir": str(srv.folder),
                      "at_main_menu": False}]
    # every fingerprint that was approvable described the bytes this replaced
    assert srv.app.plan_signature(plan["fingerprint"]) is None


def test_restore_takes_a_full_path_as_well_as_a_name(srv, monkeypatch):
    folder = _a_backup_folder(srv, "20260914-020304-save")
    calls = _fake_restore(monkeypatch, srv)
    status, b = post(srv, "/api/restore", {"folder": str(folder)})
    assert status == 200, b
    assert calls[0]["folder"] == str(folder)


@pytest.mark.parametrize("folder", ["..", "../saves", "nested/deeper", "",
                                    "   "])
def test_a_folder_that_is_not_a_direct_child_of_the_backup_root_is_400(
        srv, monkeypatch, folder):
    """Restore is the one route that copies *from* a path the client names, so
    the path is checked by name against the backup root rather than by prefix:
    `..` and a nested folder both sit "under" the root as strings."""
    calls = _fake_restore(monkeypatch, srv)
    status, b = post(srv, "/api/restore", {"folder": folder})
    assert status == 400, b
    assert is_error(b, "invalid")
    assert b["where"] == "folder"
    assert calls == [], "nothing was read before the path was judged"


def test_a_backup_folder_that_is_not_there_is_400(srv, monkeypatch):
    _fake_restore(monkeypatch, srv)
    status, b = post(srv, "/api/restore", {"folder": "20991231-235959-save"})
    assert status == 400
    assert "not one of the backup folders" in b["error"]
    assert str(srv.tmp / "backups") in b["error"]


def test_restore_is_refused_in_read_only_mode_with_the_apply_sentence(
        srv, monkeypatch):
    """One wording for "this server does not write", whichever route asked."""
    folder = _a_backup_folder(srv)
    calls = _fake_restore(monkeypatch, srv)
    post(srv, "/api/settings", {"settings": {"read_only": True}})
    status, b = post(srv, "/api/restore", {"folder": folder.name})
    assert status == 409
    assert is_error(b, "refused")
    assert b["error"] == RESTORE_SENTENCE
    assert calls == []
    status, other = post(srv, "/api/apply", {"fingerprint": "deadbeef"})
    assert status == 409 and other["error"] == RESTORE_SENTENCE


def test_an_unknown_field_on_restore_is_400(srv, monkeypatch):
    _fake_restore(monkeypatch, srv)
    status, b = post(srv, "/api/restore", {"folder": "x", "force": True})
    assert status == 400 and b["where"] == "force"


def test_apply_passes_the_version_gate_setting_as_given(srv, monkeypatch):
    """`POST /api/apply` reads `strict_version_check` off the settings, so
    turning it on from the Settings tab changes the next write without a
    restart."""
    seen = []

    def fake(path, cfg, fingerprint, builder, root, **kw):
        seen.append(kw)
        return safety.Report(), {"rows": 0, "backup": str(srv.tmp)}

    monkeypatch.setattr(safety, "apply_plan", fake)
    plan = _plan(srv)
    body = {"file": str(srv.save), "fingerprint": plan["fingerprint"]}

    status, _b = post(srv, "/api/apply", body)
    assert status == 200
    assert seen[-1]["strict_version_check"] is False,         "off by default: a game update must not turn the tool off"

    post(srv, "/api/settings", {"settings": {"strict_version_check": True}})
    plan = _plan(srv)
    body["fingerprint"] = plan["fingerprint"]
    status, _b = post(srv, "/api/apply", body)
    assert status == 200
    assert seen[-1]["strict_version_check"] is True,         "and the setting is what the write sequence is given: %s" % seen[-1]


def test_apply_refuses_an_unverified_version_only_in_strict_mode(srv,
                                                                 monkeypatch):
    """End to end through the route, with `safety` left alone: the 409 the
    Settings tab turns on, and the 200 it turns off."""
    def boom(path, cfg, fingerprint, builder, root, **kw):
        if kw.get("strict_version_check"):
            raise safety.Refused(
                "this save reports version 4800; this build was verified on "
                "4670 to 4735, and strict_version_check is on, so apply is "
                "refused.")
        return safety.Report(), {"rows": 1, "backup": str(srv.tmp)}

    monkeypatch.setattr(safety, "apply_plan", boom)
    plan = _plan(srv)
    body = {"file": str(srv.save), "fingerprint": plan["fingerprint"]}
    status, _b = post(srv, "/api/apply", body)
    assert status == 200, "the default applies"

    post(srv, "/api/settings", {"settings": {"strict_version_check": True}})
    plan = _plan(srv)
    body["fingerprint"] = plan["fingerprint"]
    status, b = post(srv, "/api/apply", body)
    assert status == 409 and is_error(b, "refused")
    assert "4800" in b["error"], b


def test_health_names_which_way_the_version_gate_is_set(srv):
    """The health panel is what a bug report pastes, so the mode the write ran
    in has to be in it."""
    _status, b = jget(srv, "/api/health")
    assert b["strict_version_check"] is False
    post(srv, "/api/settings", {"settings": {"strict_version_check": True}})
    _status, b = jget(srv, "/api/health")
    assert b["strict_version_check"] is True
    _status, boot = jget(srv, "/api/bootstrap")
    assert boot["strict_version_check"] is True,         "and the page is told, because it draws the gate line from bootstrap"


def test_restore_passes_the_main_menu_confirmation_as_given(srv, monkeypatch):
    """The confirmation comes off the request, not off the settings: it is
    about one press of one button."""
    folder = _a_backup_folder(srv)
    calls = _fake_restore(monkeypatch, srv)
    status, _b = post(srv, "/api/restore", {"folder": folder.name,
                                           "at_main_menu": True})
    assert status == 200
    assert calls[0]["at_main_menu"] is True
    status, _b = post(srv, "/api/restore", {"folder": folder.name})
    assert status == 200
    assert calls[1]["at_main_menu"] is False, "absent is false, not missing"


#: `None` is deliberately not in here: it is how a JSON body says the field
#: was not sent, and absent is false.
@pytest.mark.parametrize("value", ["yes", 1, {}])
def test_a_main_menu_confirmation_that_is_not_a_boolean_is_400(srv, monkeypatch,
                                                               value):
    calls = _fake_restore(monkeypatch, srv)
    status, b = post(srv, "/api/restore", {"folder": "x",
                                           "at_main_menu": value})
    assert status == 400, b
    assert is_error(b, "invalid")
    assert b["where"] == "at_main_menu"
    assert calls == [], "and nothing was read first"


def test_restore_refuses_when_safety_says_so(srv, monkeypatch):
    """Lane B's gates -- the game is running, the manifest does not verify, the
    lock is held -- are `Refused`, and this route must not dress them as
    anything else."""
    folder = _a_backup_folder(srv)

    def refuse(folder, save_dir, report=None, at_main_menu=False):
        raise safety.Refused("NMS.exe is running as pid 4242")

    monkeypatch.setattr(safety, "restore", refuse)
    status, b = post(srv, "/api/restore", {"folder": folder.name})
    assert status == 409
    assert is_error(b, "refused")
    assert "4242" in b["error"]


def test_a_refusal_carrying_a_report_hands_its_steps_to_the_page(
        srv, monkeypatch):
    """Q1: a refusal that fired *after* the backup was taken -- or after the
    save was written and a check then failed -- is not one sentence's worth of
    news. `Refused.report` carries what did happen, and the 409 passes those
    steps through, because `app.js` renders `steps` on a failure exactly as it
    does on a success. A refusal with no report still answers the plain
    shape."""
    folder = _a_backup_folder(srv)
    rep = safety.Report()
    rep.ok("game", "NMS.exe is not in the process list (test)")
    rep.info("restore", "save10.hg put back")

    def refuse_late(folder, save_dir, report=None, at_main_menu=False):
        exc = safety.Refused("re-read from disk: the file on disk does not "
                             "hash to the backup's copy")
        exc.report = rep
        raise exc

    monkeypatch.setattr(safety, "restore", refuse_late)
    status, b = post(srv, "/api/restore", {"folder": folder.name})
    assert status == 409
    assert is_error(b, "refused")
    assert b["steps"] == rep.steps
    assert [s["label"] for s in b["steps"]] == ["game", "restore"]


def test_an_apply_refusal_carrying_a_report_does_the_same(srv, monkeypatch):
    def refuse_late(path, cfg, fingerprint, builder, root, **kw):
        rep = safety.Report()
        rep.ok("backup", "save.hg -> 20260914-010203-save")
        exc = safety.Refused("re-read from disk: re-decoding the written file "
                             "differs at BaseContext/PlayerStateData")
        exc.report = rep
        raise exc

    monkeypatch.setattr(server.safety, "apply_plan", refuse_late)
    plan = _plan(srv)
    status, b = post(srv, "/api/apply",
                     {"file": str(srv.save), "fingerprint": plan["fingerprint"]})
    assert status == 409
    assert is_error(b, "refused")
    assert [s["label"] for s in b["steps"]] == ["backup"]
    assert "re-decoding" in b["error"]


def test_a_refusal_with_no_report_carries_no_steps_key(srv):
    """"No steps" and "the run got nowhere" are different answers, so the key
    is absent rather than an empty list. Every refusal raised before there is
    a report -- read-only, an unknown fingerprint -- is one of these."""
    status, b = post(srv, "/api/apply", {"fingerprint": "deadbeef"})
    assert status == 409
    assert "steps" not in b
    assert is_error(b, "refused")


def test_a_restore_that_cannot_re_read_the_save_still_reports_success(
        srv, monkeypatch):
    """The files are back. Failing to re-render them is a warning, not a 500
    that implies the restore did not happen."""
    folder = _a_backup_folder(srv)
    _fake_restore(monkeypatch, srv, result={"restored": ["save99.hg"]})
    status, b = post(srv, "/api/restore", {"folder": folder.name})
    assert status == 200, b
    assert b["save"] is None
    assert "put back" in b["warning"]


@pytest.mark.parametrize("res,want", [
    ({"restored": ["save3.hg", "mf_save3.hg"]}, "save3.hg"),
    ({"restored": ["mf_save3.hg", "save3.hg"]}, "save3.hg"),
    ({"restored": ["save3.hg"], "manifest": {"save": {"file": "save7.hg"}}},
     "save7.hg"),
    ({"restored": []}, None),
    ({}, None),
    (None, None),
])
def test_which_save_a_restore_put_back(res, want):
    """`safety.restore` answers `restored` as the *list* of file names it
    wrote -- the save and its `mf_` -- so the one to re-read is the one that is
    not the metadata, and the manifest, which names the save outright, wins."""
    assert server.restored_save_name(res) == want


def test_a_real_restore_puts_the_bytes_back(srv, tmp_path):
    """End to end against lane B's own `safety.restore`, no stand-in: apply,
    then restore the backup that apply made, then compare the file with the
    copy taken before the write. This is the P2-6 Undo button's whole
    contract."""
    if getattr(safety, "restore", None) is None:
        pytest.skip("safety.restore has not landed yet (lane B, P2-5)")
    before = srv.save.read_bytes()
    (tmp_path / "pre-apply.hg").write_bytes(before)

    plan = _plan(srv)
    status, b = post(srv, "/api/apply",
                     {"file": str(srv.save), "fingerprint": plan["fingerprint"]})
    assert status == 200, b
    assert srv.save.read_bytes() != before, "the apply wrote something"
    backup = b["result"]["backup"]

    status, backups = jget(srv, "/api/backups")
    assert status == 200
    rows = [r for r in backups["backups"] if r["folder"] == backup]
    assert rows and rows[0]["has_manifest"] is True, backups

    status, b = post(srv, "/api/restore", {"folder": os.path.basename(backup)})
    assert status == 200, b
    assert srv.save.read_bytes() == before, "the bytes are the pre-apply bytes"
    assert b["save"]["file"] == "save.hg"
    assert any(s["label"] == "restore" for s in b["steps"]), b["steps"]


# ==========================================================================
# P2-5: GET /api/log
# ==========================================================================

@pytest.fixture
def logging_srv(srv):
    """`srv`, with the logger actually writing to the file the app names.

    The rest of this file leaves logging alone -- a suite that installs a file
    handler per test leaks handlers -- but this route reads the file the
    *running* process is writing, so there has to be one (Rule 4). Put back
    afterwards, whatever happens.
    """
    cli.setup_logging(str(srv.tmp / "sorter.log"))
    try:
        yield srv
    finally:
        cli.setup_logging("-")


def test_the_log_route_returns_the_tail_of_the_running_log(logging_srv):
    srv = logging_srv
    jget(srv, "/api/version")                    # something to log
    status, b = jget(srv, "/api/log?tail=20")
    assert status == 200
    assert set(b) == {"path", "total", "lines"}
    assert b["path"] == str(srv.tmp / "sorter.log")
    assert len(b["lines"]) <= 20
    assert b["total"] >= len(b["lines"])
    assert any("/api/version" in line for line in b["lines"]), b["lines"][-3:]


def test_the_log_tail_is_clamped(logging_srv):
    jget(logging_srv, "/api/version")
    status, b = jget(logging_srv, "/api/log?tail=1000000")
    assert status == 200
    assert len(b["lines"]) <= server.LOG_TAIL_MAX
    assert server.LOG_TAIL_MAX == 2000


def test_a_non_numeric_tail_is_400(logging_srv):
    status, b = jget(logging_srv, "/api/log?tail=all")
    assert status == 400 and is_error(b, "invalid")


def test_a_log_file_that_has_not_been_written_yet_is_404(srv):
    """The path is configured and the file is not there: a sorter started with
    `--log` pointing somewhere that has not been opened yet."""
    status, b = jget(srv, "/api/log")
    assert status == 404
    assert is_error(b, "not_found")
    assert "not been written yet" in b["error"]


def test_the_log_route_is_404_when_the_log_goes_to_stderr(tmp_path):
    """`--log -` means there is no file. "There is no log file" and "the log is
    empty" are different answers and a bug report has to tell them apart."""
    folder = tmp_path / "saves"
    folder.mkdir()
    legacy.build_save(str(folder / "save.hg"))
    app = App(str(folder), str(tmp_path / "config.json"),
              str(tmp_path / "backups"),
              settings_path=str(tmp_path / "settings.json"),
              log_path=None)
    with running(app) as s:
        status, b = jget(s, "/api/log")
        assert status == 404
        assert is_error(b, "not_found")
        assert "stderr" in b["error"]


# ==========================================================================
# P4-1: /api/game is platform.game_status(), unaltered
# ==========================================================================

def test_the_game_route_is_the_platform_answer(srv, monkeypatch):
    """One process check in the program. The route is a view over
    `platform.game_status()` and adds no field of its own: a second shape here
    is how a health panel and an apply refusal come to disagree about whether
    the game is running.

    The real answer, not the fixture's: `srv` arranges a closed game so the
    write path has something to read, and this is the one test that has to see
    what the machine says -- off Windows that answer carries an `error` the
    stub has no reason to invent."""
    monkeypatch.setattr(safety, "game_status", platformmod.game_status)
    status, b = jget(srv, "/api/game")
    assert status == 200
    expected = platformmod.game_status()
    assert set(b) == set(expected)
    assert set(b) >= {"running", "known", "method", "pids", "process"}
    assert b["process"] == expected["process"]
    assert b["known"] == expected["known"]
    assert set(safety.game_status()) == set(b)


# ==========================================================================
# P6 (server half): a refusing gate is 409, a warning gate is surfaced
# ==========================================================================

#: both gate classes, because the dispatcher maps them with one clause and
#: either one reaching the 500 handler is the bug (an expedition save reading
#: as "the server hit an error").
GATE_CLASSES = (savemodel.SaveGate, savemodel.UnsupportedSave)


@pytest.mark.parametrize("gate", GATE_CLASSES, ids=lambda g: g.__name__)
@pytest.mark.parametrize("route,body", [
    ("/api/plan", {}),
    ("/api/select", {"file": "save.hg"}),
])
def test_a_refusing_gate_is_409_refused(srv, monkeypatch, route, body, gate):
    """GOAL.md Rule 1: an expedition save, or a save version outside the tested
    range at apply, is understood and refused -- not bad input (400) and not an
    unexpected error (500, which is what it was)."""
    def boom(*a, **kw):
        raise gate("this save is an expedition save; the live inventory may be "
                   "the season copy")

    monkeypatch.setattr(srv.app, "save", boom)
    status, b = post(srv, route, body)
    assert status == 409, b
    assert is_error(b, "refused")
    assert "expedition" in b["error"]


def test_a_gate_that_appears_between_the_plan_and_the_apply_is_409(
        srv, monkeypatch):
    """The version gate refuses at apply, not at plan (GOAL.md §2.3: "plan
    works with a banner; apply refuses"), so the interesting order is plan
    first, gate second -- and the answer still has to be a refusal rather than
    the 500 an unmapped `ValueError` gives."""
    gate = savemodel.SaveGate
    plan = _plan(srv)

    def boom(*a, **kw):
        raise gate("this save reports version 9999; apply was verified on "
                   "4735 only")

    monkeypatch.setattr(srv.app, "save", boom)
    status, b = post(srv, "/api/apply",
                     {"file": str(srv.save), "fingerprint": plan["fingerprint"]})
    assert status == 409, b
    assert is_error(b, "refused")
    assert "9999" in b["error"]


def test_a_refusing_gate_leaves_bootstrap_at_200_with_issues(srv, monkeypatch):
    """DECISIONS.md resolution 4: the page must render its folder picker and
    its settings even when no save can be loaded."""
    gate = savemodel.SaveGate

    def boom(*a, **kw):
        raise gate("this save reports version 9999, which is outside the "
                   "tested range")

    monkeypatch.setattr(srv.app, "save", boom)
    status, b = jget(srv, "/api/bootstrap")
    assert status == 200
    assert b["save"] is None
    assert b["issues"] and b["issues"][0]["level"] == "error"
    assert "9999" in b["issues"][0]["message"]


def test_warning_gates_reach_the_save_view_and_the_bootstrap_issues(
        srv, monkeypatch):
    """A caveat nobody is shown is not a caveat. The route carries whatever
    `SaveFile.gates()` answers into `save.gates` and into the list the page
    already renders."""
    warned = [{"level": "warning", "where": "save",
               "message": "this save reports Version 4740; 4735 is the newest "
                          "one that was verified"}]
    monkeypatch.setattr(type(srv.app.save()), "gates",
                        lambda self, strict=False: list(warned))
    status, b = post(srv, "/api/select", {"file": "save.hg"})
    assert status == 200, b
    assert b["gates"] == warned

    status, boot = jget(srv, "/api/bootstrap")
    assert status == 200
    assert boot["save"]["gates"] == warned
    assert warned[0] in boot["issues"]


@pytest.mark.parametrize("strict,level,tail", [
    (False, "note", "Apply proceeds:"),
    (True, "warn", "and strict_version_check is on, so apply is refused."),
])
def test_the_version_gate_reaches_the_page_worded_for_the_mode(
        srv, monkeypatch, strict, level, tail):
    """The owner's report: the page carried the refusal's wording and then a
    second sentence saying the apply proceeds, joined with no full stop. The
    route carries one complete sentence, and `strict_version_check` decides
    which one; nothing downstream has to finish it."""
    monkeypatch.setattr(type(srv.app.save()), "version", lambda self: 4800)
    if strict:
        post(srv, "/api/settings",
             {"settings": {"strict_version_check": True}})
    status, b = post(srv, "/api/select", {"file": "save.hg"})
    assert status == 200, b
    rows = [g for g in b["gates"] if g["where"] == "version"]
    assert len(rows) == 1 and rows[0]["level"] == level, b["gates"]
    msg = rows[0]["message"]
    assert msg.startswith("this save reports version 4800; this build was "
                          "verified on 4670 to 4735"), msg
    assert tail in msg, msg
    assert msg.endswith("."), "every sentence in it is punctuated: %s" % msg
    assert ("Apply proceeds" in msg) is (not strict), \
        "one mode's wording never appears in the other: %s" % msg
    assert "fixture" not in msg, "not a word a player is owed: %s" % msg


def test_a_save_with_nothing_to_warn_about_reports_an_empty_list(srv):
    """The key is always there, so a page that reads `save.gates.length` never
    has to check first. The fixture is a supported version with no season
    word, which is the ordinary case and has to render as "no caveats" rather
    than as a missing field."""
    status, b = post(srv, "/api/select", {"file": "save.hg"})
    assert status == 200
    assert b["gates"] == []
    assert savemodel.SaveFile(str(srv.save)).gates() == []


def test_a_broken_gates_method_is_a_warning_not_a_500(srv, monkeypatch):
    def boom(self):
        raise RuntimeError("the gate itself is broken")

    monkeypatch.setattr(type(srv.app.save()), "gates", boom)
    status, b = post(srv, "/api/select", {"file": "save.hg"})
    assert status == 200, b
    assert b["gates"][0]["level"] == "warning"
    assert "could not be read" in b["gates"][0]["message"]


def test_the_save_list_carries_sizes_match(srv):
    """R3: a save whose `mf_` disagrees with the file's size is the state the
    review found unmarked. The page flags the row before it is selected, so the
    field has to be on the row and not only inside the metadata summary."""
    status, b = jget(srv, "/api/bootstrap")
    assert status == 200
    row = b["saves"][0]
    assert "sizes_match" in row
    assert row["sizes_match"] in (True, False, None)


# ==========================================================================
# lane D: POST /api/validate, and one spelling per path
# ==========================================================================

def test_validate_answers_issues_without_storing_anything(srv):
    """The page shows what is wrong with an edit before it is saved. The only
    route that answered that question also wrote the file."""
    before = open(str(srv.tmp / "config.json"), encoding="utf-8").read()
    cfg = legacy.synthetic_config()
    cfg["bucket_rules"] = [{"bucket": "not_a_bucket", "store": "nowhere"}]
    status, b = post(srv, "/api/validate", {"config": cfg})
    assert status == 200, b
    assert set(b) == {"issues"}
    assert any(i["level"] == "error" for i in b["issues"]), b["issues"]
    assert open(str(srv.tmp / "config.json"), encoding="utf-8").read() == before


def test_validate_of_a_good_config_is_no_errors(srv):
    status, b = post(srv, "/api/validate",
                     {"config": legacy.synthetic_config()})
    assert status == 200
    assert [i for i in b["issues"] if i["level"] == "error"] == []


def test_validate_does_not_touch_the_minted_plans(srv):
    plan = _plan(srv)
    status, _b = post(srv, "/api/validate",
                      {"config": legacy.synthetic_config()})
    assert status == 200
    assert srv.app.plan_signature(plan["fingerprint"])


def test_validate_wrong_shape_is_400(srv):
    status, b = post(srv, "/api/validate", {})
    assert status == 400 and b["where"] == "config"
    status, b = post(srv, "/api/validate", {"config": [1, 2]})
    assert status == 400 and b["where"] == "config"
    status, b = post(srv, "/api/validate",
                     {"config": legacy.synthetic_config(), "force": True})
    assert status == 400 and b["where"] == "force"


def test_every_path_the_api_emits_has_one_spelling(tmp_path):
    """Lane D: `bootstrap.saves[].path` was built from the save folder as the
    operator typed it (`--folder C:/Users/x/saves`) while `save.path` came back
    from `os.path.abspath`, so the row the page compared did not match the save
    that was loaded. Normalised once, in `App`."""
    folder = tmp_path / "saves"
    folder.mkdir()
    legacy.build_save(str(folder / "save.hg"))
    typed = str(folder).replace(os.sep, "/") + "/"
    app = App(typed, str(tmp_path / "config.json"), str(tmp_path / "backups"),
              settings_path=str(tmp_path / "settings.json"))
    assert app.save_folder == os.path.normpath(str(folder))
    with running(app) as s:
        status, b = jget(s, "/api/bootstrap")
        assert status == 200
        assert b["save_folder"] == os.path.normpath(str(folder))
        assert b["saves"][0]["path"] == b["save"]["path"]
        assert b["save"]["path"] == os.path.join(os.path.normpath(str(folder)),
                                                 "save.hg")
        assert b["backup_root"] == os.path.normpath(str(tmp_path / "backups"))


def test_an_empty_save_folder_is_not_the_working_directory(tmp_path):
    """`os.path.abspath("")` is the process working directory, which is how "no
    save folder chosen yet" would silently become "sort whatever is in the
    folder the sorter was started from"."""
    st = settingsmod.Settings(backup_folder=str(tmp_path / "backups"))
    st.first_run = True
    app = App(config_path=str(tmp_path / "config.json"), settings=st,
              settings_path=str(tmp_path / "settings.json"))
    assert app.save_folder == ""
    assert app.first_run is True


# ==========================================================================
# R6: the client's spelling of the file name
# ==========================================================================

def test_a_save_name_that_differs_only_in_case_is_the_same_save(srv):
    """R6: the folder was compared exactly, so `SAVE.HG` was accepted and
    `write_atomic` then replaced onto *that* spelling -- measurably renaming
    the operator's `save9.hg` to `SAVE9.HG`, its `mf_` with it, and splitting
    the plan signature, which is taken from the basename. The name on disk is
    the only one that reaches the write path."""
    status, b = post(srv, "/api/select", {"file": "SAVE.HG"})
    assert status == 200, b
    assert b["file"] == "save.hg"
    assert b["path"] == str(srv.save)
    assert srv.app._save_path == str(srv.save)
    shouty = os.path.join(str(srv.folder).upper(), "save.hg")
    status, b = post(srv, "/api/select", {"file": shouty})
    assert status == 200, b
    assert b["path"] == str(srv.save)


def test_a_name_with_no_match_at_all_is_still_400(srv):
    status, b = post(srv, "/api/select", {"file": "SAVE99.HG"})
    assert status == 400
    assert is_error(b, "invalid")
    assert "save folder" in b["error"]


def test_a_plan_minted_under_one_spelling_applies_under_the_other(
        srv, monkeypatch):
    """The signature is `name|size|mtime` off the basename, so two spellings
    used to mint two plans over one file and neither could apply the other."""
    seen = _spy_on_apply(monkeypatch)
    status, plan = post(srv, "/api/plan", {"file": "SAVE.HG"})
    assert status == 200, plan
    assert plan["file"] == "save.hg"
    status, b = post(srv, "/api/apply",
                     {"file": "save.hg", "fingerprint": plan["fingerprint"]})
    assert status == 200, b
    assert seen and len(seen[0]) == 64


# ==========================================================================
# R15: the cache follows the bytes, not the timestamp
# ==========================================================================

def test_a_same_size_same_mtime_change_is_not_served_from_the_cache(srv,
                                                                    tmp_path):
    """R15: a same-length edit written back with the original timestamp keeps
    the name, the size and `st_mtime`, so the cached `SaveFile` went on serving
    a document that was no longer on disk -- and the operator approved a plan
    drawn from bytes that were gone. It cannot cause a wrong write (the write
    path re-plans from the backup copy), which is exactly why it has to be
    caught here."""
    first = srv.app.save(reload=True)
    was = os.stat(first.path)
    units = first.d.get(first.d.player, "Units")

    edited = savemodel.SaveFile(first.path)
    changed = 12344 if units is None or len(str(units)) != 5 else units - 1
    edited.d.set(edited.d.player, "Units", changed)   # same number of digits
    framed = codec.frame_payload(codec.dumps(edited.doc))
    if len(framed) != was.st_size:
        pytest.skip("the edit did not come out the same length on this build")
    srv.save.write_bytes(framed)
    os.utime(str(srv.save), (was.st_atime, was.st_mtime))
    now = os.stat(str(srv.save))
    assert now.st_size == was.st_size and now.st_mtime == was.st_mtime

    again = srv.app.save()
    assert again is not first, "the cache answered for bytes that had changed"
    assert again.d.get(again.d.player, "Units") == changed


# ==========================================================================
# P5-2 D4: the documents, served from this machine
# ==========================================================================

def html_get(srv, path):
    status, ctype, raw = get(srv, path)
    return status, ctype, raw.decode("utf-8", "replace")


def test_a_shipped_document_is_rendered_from_this_machine(srv):
    """D4: the gate warnings' "what this means" links pointed at github.com. A
    tool that binds to loopback and promises to touch nothing must not send the
    operator to the internet to read its own safety copy -- and those links
    404 until the repository is public."""
    status, ctype, page = html_get(srv, "/docs/TROUBLESHOOTING.md")
    assert status == 200
    assert ctype.startswith("text/html")
    assert "<h1" in page and "<h3" in page
    assert "NMS.exe is running as pid" in page, "the refusal entries rendered"
    assert "<script" not in page, "a document page needs no script"
    # a table, from a document that has one: `DOC_STYLE` carries table rules
    # and a renderer that dropped them would go unnoticed otherwise.
    _status, _ctype, rules = html_get(srv, "/docs/RULES.md")
    assert "</table>" in rules


def test_a_heading_gets_the_anchor_its_links_use(srv):
    """The links are written against GitHub's rendering, so the anchors have to
    match GitHub's slugs or every `#...` link lands nowhere."""
    _status, _ctype, page = html_get(srv, "/docs/TROUBLESHOOTING.md")
    assert 'id="startup-pages"' in page


def test_the_docs_index_lists_what_this_build_ships(srv):
    status, _ctype, page = html_get(srv, "/docs/")
    assert status == 200
    for name in ("RULES.md", "GUIDE.md", "TROUBLESHOOTING.md"):
        assert "/docs/%s" % name in page, name
    status, _ctype, page = html_get(srv, "/docs")
    assert status == 200 and "/docs/GUIDE.md" in page


def test_every_internal_link_in_a_served_document_resolves(srv):
    """A served document with a dead link in it is the same complaint in
    miniature, so the links are followed rather than eyeballed."""
    _status, _ctype, page = html_get(srv, "/docs/GUIDE.md")
    targets = sorted(set(re.findall(r'href="(/docs/[^"#]+)', page)))
    assert len(targets) >= 3, targets
    for target in targets:
        status, ctype, _raw = get(srv, target)
        assert status == 200, "%s -> %s" % (target, status)
        assert ctype.startswith("text/html")


def test_an_unknown_document_is_json_404_naming_what_there_is(srv):
    status, b = jget(srv, "/docs/NOPE.md")
    assert status == 404
    assert is_error(b, "not_found")
    assert "NOPE.md" in b["error"]
    assert "GUIDE.md" in b["error"], "it says what it does ship"


@pytest.mark.parametrize("path", [
    "/docs/../pyproject.toml",
    "/docs/..%2fpyproject.toml",
    "/docs/%2e%2e/pyproject.toml",
    "/docs/static/app.js",
    "/docs/nms_sorter/server.py",
    "/docs/C:/Windows/win.ini",
    "/docs/GUIDE.md/../../pyproject.toml",
    "/docs/.env",
])
def test_nothing_outside_the_document_list_can_be_read(srv, path):
    """The listing is the allow-list, so there is no path to sanitise: a name
    that is not one of the documents is simply not a key."""
    status, ctype, raw = get(srv, path)
    assert status == 404, (path, status)
    assert "json" in ctype, path
    assert b"[project]" not in raw and b"def " not in raw


def test_the_documents_are_served_while_degraded_too(degraded):
    """Reduced mode is exactly when "what does this mean?" needs answering, and
    none of the three startup failures touches a file in `docs/`."""
    app, _cfg = degraded("config_unreadable")
    with running(app) as srv:
        status, ctype, page = html_get(srv, "/docs/TROUBLESHOOTING.md")
        assert status == 200 and ctype.startswith("text/html")
        assert "<h1" in page
        status, b = jget(srv, "/docs/NOPE.md")
        assert status == 404 and is_error(b, "not_found")


def test_the_gate_links_in_the_page_land_on_a_real_heading(srv):
    """The other half of D4. `GATE_DOC` (static/app.js) names six headings in
    `docs/TROUBLESHOOTING.md` and the page appends each to
    `/docs/TROUBLESHOOTING.md#...`; this route is what makes those links local,
    so the anchors have to exist in what it renders.

    The target used to be `docs/SAFETY.md`, and two of the six anchors were
    already stale when this route landed, which is the class of rot a link
    nobody follows accumulates. `docs/TROUBLESHOOTING.md` is the better target
    for a second reason: `tests/test_docs.py` requires every `###` heading in
    it to be a sentence the code can say, so an anchor here cannot outlive the
    refusal it explains without that test going red too.
    """
    js_path = os.path.join(os.path.dirname(HERE), "nms_sorter", "static",
                           "app.js")
    with open(js_path, encoding="utf-8") as fh:
        js = fh.read()
    m = re.search(r"const GATE_DOC = \[(.*?)\];", js, re.S)
    if not m:
        pytest.skip("static/app.js no longer declares GATE_DOC")
    anchors = re.findall(r'"([^"]+)"', m.group(1))
    assert anchors, m.group(1)

    _status, _ctype, page = html_get(srv, "/docs/TROUBLESHOOTING.md")
    ids = set(re.findall(r'id="([^"]+)"', page))
    missing = [a for a in anchors if a not in ids]
    assert not missing, (
        "these gate links land nowhere in the served document: %s" % missing)


def test_the_docs_route_has_one_resolution_order(srv):
    """A source run serves the repository's own `docs/`; the frozen build
    serves the copy the spec put beside the package. Same names, one order, and
    `README.md` comes from beside the folder in both layouts."""
    root = os.path.dirname(HERE)
    assert server.docs_dir() == os.path.join(root, "docs")
    assert server.DOC_DIRS[0].endswith(os.path.join("nms_sorter", "docs"))
    assert server.DOC_DIRS[1] == os.path.join(root, "docs")
    names = server.doc_names()
    assert names[0] == "README.md"
    assert "TROUBLESHOOTING.md" in names and "GUIDE.md" in names
    real, text = server.read_doc("readme.MD")
    assert real == "README.md" and text.strip()


# ==========================================================================
# P5-2 C2: POST /api/config/reload
# ==========================================================================

def _write_config(srv, mutate):
    """Edit `config.json` on disk behind the running server. -> the bytes."""
    path = str(srv.tmp / "config.json")
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    mutate(cfg)
    raw = json.dumps(cfg, indent=2)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(raw)
    return raw


def test_reload_picks_up_a_config_edited_on_disk(srv):
    """C2: the file was read once at startup, so the workflow every document
    describes -- "copy this JSON into your config file" -- did nothing until
    the sorter was restarted, and pressing save first overwrote the file that
    had just been pasted in."""
    before = srv.app.config["options"].get("max_moves")
    assert before != 7
    _write_config(srv, lambda cfg: cfg["options"].__setitem__("max_moves", 7))
    status, b = jget(srv, "/api/bootstrap")
    assert status == 200
    assert b["config"]["options"]["max_moves"] == before, "not yet reloaded"

    status, b = post(srv, "/api/config/reload", {})
    assert status == 200, b
    assert {"saved", "issues", "config"} <= set(b)
    assert b["saved"] is False, "reload writes nothing"
    assert b["config"]["options"]["max_moves"] == 7
    assert srv.app.config["options"]["max_moves"] == 7
    status, boot = jget(srv, "/api/bootstrap")
    assert boot["config"]["options"]["max_moves"] == 7


def test_reload_writes_nothing(srv):
    raw = _write_config(srv, lambda cfg: cfg["options"].__setitem__(
        "max_new_stacks", 3))
    status, _b = post(srv, "/api/config/reload", {})
    assert status == 200
    with open(str(srv.tmp / "config.json"), encoding="utf-8") as fh:
        assert fh.read() == raw, "the file the operator pasted is untouched"


def test_reload_clears_the_minted_plans(srv):
    """A plan printed under the old rules must not stay approvable -- the same
    reason /api/config and /api/config/reset clear them."""
    plan = _plan(srv)
    assert srv.app.plan_signature(plan["fingerprint"])
    status, _b = post(srv, "/api/config/reload", {})
    assert status == 200
    assert srv.app.plan_signature(plan["fingerprint"]) is None
    status, b = post(srv, "/api/apply",
                     {"file": str(srv.save), "fingerprint": plan["fingerprint"]})
    assert status == 409
    assert "has not printed a plan" in b["error"]


def test_reload_reports_the_issues_the_new_file_has(srv):
    def break_a_rule(cfg):
        cfg["bucket_rules"] = [{"bucket": "not_a_bucket", "store": "nowhere"}]

    _write_config(srv, break_a_rule)
    status, b = post(srv, "/api/config/reload", {})
    assert status == 200, b
    assert any(i["level"] == "error" for i in b["issues"]), b["issues"]


def test_an_unreadable_config_is_refused_and_the_old_rules_stay(srv):
    path = str(srv.tmp / "config.json")
    keep = json.loads(json.dumps(srv.app.config))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ this is not json")
    status, b = post(srv, "/api/config/reload", {})
    assert status == 409
    assert is_error(b, "refused")
    assert path in b["error"]
    assert "still the ones in use" in b["error"]
    assert srv.app.config == keep, "the rules in memory are untouched"
    # and it is still a working server, not one in reduced mode
    status, boot = jget(srv, "/api/bootstrap")
    assert status == 200 and boot["save"] is not None


def test_a_config_from_a_newer_build_is_refused_not_a_reduced_mode(srv):
    """`ConfigTooNew` at startup is a page; the same file arriving under a
    running server is one sentence, and the rules already loaded stay."""
    path = str(srv.tmp / "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"config_version": 99}, fh)
    status, b = post(srv, "/api/config/reload", {})
    assert status == 409
    assert is_error(b, "refused")
    assert path in b["error"]
    assert "99" in b["error"]
    assert srv.app.degraded is None


# ==========================================================================
# Q14 again: a broken backup folder is visible, not silent
# ==========================================================================

def test_a_failing_prune_says_so_in_the_log(srv, monkeypatch, caplog):
    """It answers `[]` so a proved write does not fail on a folder it could not
    delete -- and a retention that raises on every apply looks exactly like a
    retention nobody configured, so the difference has to be somewhere. That
    somewhere is one warning in `/api/log`."""
    def boom(root, keep):
        raise OSError(13, "the backup folder is held by something")

    monkeypatch.setattr(safety, "prune_backups", boom)
    with caplog.at_level(logging.WARNING, logger="nms_sorter.app"):
        assert srv.app.prune_backups() == []
    said = [r.getMessage() for r in caplog.records]
    assert any("retention did not run" in m for m in said), said
    hit = [m for m in said if "retention did not run" in m][0]
    assert str(srv.tmp / "backups") in hit, hit
    # CPython maps errno 13 onto PermissionError; the line names whatever the
    # class turned out to be, which is the half a bug report needs.
    assert "PermissionError" in hit and "held by something" in hit, hit


def test_a_backup_root_that_cannot_be_listed_says_so_in_the_log(srv,
                                                                monkeypatch,
                                                                caplog):
    def boom(root):
        raise OSError("the drive went away")

    monkeypatch.setattr(safety, "list_backups", boom)
    with caplog.at_level(logging.WARNING, logger="nms_sorter.app"):
        assert srv.app.list_backups() == []
    said = [r.getMessage() for r in caplog.records]
    assert any("could not be listed" in m for m in said), said
    assert any(str(srv.tmp / "backups") in m for m in said), said


def test_a_working_prune_logs_nothing_at_warning(srv, monkeypatch, caplog):
    """The quiet path stays quiet: a log that warns on every successful apply
    is a log nobody reads."""
    monkeypatch.setattr(safety, "prune_backups", lambda root, keep: [])
    with caplog.at_level(logging.WARNING, logger="nms_sorter.app"):
        assert srv.app.prune_backups() == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


# ==========================================================================
# P7-6: the version the release is tagged with
# ==========================================================================

def test_the_version_is_1_0_0_everywhere_it_is_reported(srv):
    """One string, three readers: `/api/version`, the health panel (which is
    what a bug report pastes) and the backup manifest's `app_version`. A
    release tagged `v1.0.0` against a build that reports `0.1.0.dev0` is a bug
    report nobody can place."""
    from nms_sorter import __version__
    assert __version__ == "1.0.0"
    status, b = jget(srv, "/api/version")
    assert status == 200 and b["app"] == "1.0.0"
    status, h = jget(srv, "/api/health")
    assert status == 200 and h["app_version"] == "1.0.0"
    assert safety._app_version() == "1.0.0"


# ==========================================================================
# POST /api/quit: stopping the sorter from the page
#
# The windowed executable has no console, so before this route the only way to
# stop `NMS-Sorter.exe` was Task Manager. Every test here talks to a running
# server, and the ones that expect the server to *survive* say so by asking it
# something afterwards -- a quit that refused and stopped anyway would
# otherwise pass.
# ==========================================================================

def test_quit_answers_the_documented_shape_and_stops(srv):
    status, b = post(srv, "/api/quit", {})
    assert status == 200, b
    assert b == {"stopping": True,
                 "message": "the sorter is stopping; you can close this tab"}
    assert b["message"] == server.QUIT_STOPPING


def test_quit_from_another_origin_is_403_and_the_server_stays_up(srv):
    """The drive-by case, and the one where the cost of getting it wrong is
    highest: a page on another origin must not be able to turn the sorter off
    while the operator is using it."""
    status, b = post(srv, "/api/quit", {}, origin="http://evil.example")
    assert status == 403 and is_error(b, "refused")
    status, _v = jget(srv, "/api/version")
    assert status == 200, "and it is still answering"


def test_quit_needs_json_like_every_other_post(srv):
    status, b = post(srv, "/api/quit", raw=b"stop", ctype="text/plain")
    assert status == 415 and is_error(b, "invalid")
    status, b = post(srv, "/api/quit", raw=b"stop=1",
                     ctype="application/x-www-form-urlencoded")
    assert status == 415 and is_error(b, "invalid")
    status, _v = jget(srv, "/api/version")
    assert status == 200, "and it is still answering"


def test_quit_with_a_field_it_does_not_know_is_400(srv):
    status, b = post(srv, "/api/quit", {"force": True})
    assert status == 400 and is_error(b, "invalid")
    assert b["where"] == "force"
    status, _v = jget(srv, "/api/version")
    assert status == 200


def test_quit_is_refused_while_a_write_is_running(srv):
    """`App.busy` held by hand, which is what `do_apply` does around the write.

    The refusal is a 409 with the sentence `docs/TROUBLESHOOTING.md` is keyed
    by, and the server is still there afterwards -- the whole point is that a
    stop arriving mid-apply changes nothing.
    """
    srv.app.begin_write()
    try:
        status, b = post(srv, "/api/quit", {})
        assert status == 409 and is_error(b, "refused")
        assert b["error"] == server.QUIT_BUSY
        assert "wait for it to finish" in b["error"]
        status, _v = jget(srv, "/api/version")
        assert status == 200, "and it did not stop anyway"
    finally:
        srv.app.end_write()
    # and once the write is done, the same request is granted
    status, b = post(srv, "/api/quit", {})
    assert status == 200 and b["stopping"] is True


def test_two_writes_at_once_both_have_to_finish_before_a_quit(srv):
    """The reason the flag is counted rather than a bare boolean: the first
    write to finish must not clear it under the second."""
    srv.app.begin_write()
    srv.app.begin_write()
    srv.app.end_write()
    status, b = post(srv, "/api/quit", {})
    assert status == 409 and b["error"] == server.QUIT_BUSY
    srv.app.end_write()
    assert srv.app.busy is False
    status, _b = post(srv, "/api/quit", {})
    assert status == 200


def test_a_quit_arriving_mid_apply_is_refused(srv, monkeypatch):
    """End to end, with the quit sent from inside the write sequence itself.

    `safety.apply_plan` is replaced by something that asks the server to stop
    while it is holding `App.lock`, which is exactly the race the flag exists
    for: the route cannot take the lock to find out whether a write is running,
    because the write is holding it.
    """
    seen = {}

    def fake(path, cfg, fingerprint, builder, root, **kw):
        seen["status"], seen["body"] = post(srv, "/api/quit", {})
        return safety.Report(), {"rows": 0, "backup": str(srv.tmp)}

    monkeypatch.setattr(safety, "apply_plan", fake)
    plan = _plan(srv)
    status, _b = post(srv, "/api/apply",
                      {"file": str(srv.save), "fingerprint": plan["fingerprint"]})
    assert status == 200, "the apply itself is unaffected"
    assert seen["status"] == 409, seen
    assert seen["body"]["error"] == server.QUIT_BUSY
    assert srv.app.busy is False, "and the flag is cleared when the write ends"
    status, _v = jget(srv, "/api/version")
    assert status == 200, "and the server is still up afterwards"


def test_quit_makes_serve_forever_return(tmp_path):
    """The property the whole feature is: `cli.main` gets to `return 0`.

    Not `running()`: that fixture shuts the server down itself, which would
    prove nothing. This one starts `serve_forever` in a thread, asks the server
    to stop over HTTP, and waits for the thread to end -- which is the same
    thread `cli.main` blocks on in the frozen build.
    """
    folder = tmp_path / "saves"
    folder.mkdir()
    legacy.build_save(str(folder / "save.hg"))
    app = App(str(folder), str(tmp_path / "config.json"),
              str(tmp_path / "backups"),
              settings_path=str(tmp_path / "settings.json"))
    httpd = server.serve(app, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        s = SimpleNamespace(port=httpd.server_address[1])
        status, b = jget(s, "/api/version")
        assert status == 200, b
        status, b = post(s, "/api/quit", {})
        assert status == 200 and b["stopping"] is True
        thread.join(timeout=5)
        assert not thread.is_alive(), \
            "serve_forever has to return within 5 s of the 200"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_quit_logs_one_line_saying_where_the_stop_came_from(srv, caplog):
    """"Why did the sorter stop?" is a question the log has to answer on its
    own: a stop from the page and a crash look identical without this."""
    with caplog.at_level(logging.INFO, logger="nms_sorter.server"):
        status, _b = post(srv, "/api/quit", {})
        assert status == 200
        # The answer goes out before the line is written -- that is the whole
        # point of the ordering -- so the line is waited for rather than
        # asserted on the instant the 200 arrives.
        said = []
        end = time.time() + 5
        while time.time() < end and not said:
            said = [r for r in caplog.records
                    if r.getMessage() == "stopped from the page"]
            if not said:
                time.sleep(0.02)
    assert said, [r.getMessage() for r in caplog.records]


def test_quit_still_works_while_degraded(degraded):
    """Reduced mode is one of the states most worth being able to stop from:
    the page is one paragraph about a broken config file, and in the windowed
    build there is no console to press ctrl-c in."""
    app, _cfg = degraded("config_unreadable")
    with running(app) as srv:
        status, b = post(srv, "/api/quit", {})
        assert status == 200, b
        assert b["stopping"] is True


def test_the_idle_clock_is_reset_by_any_request(srv):
    """`cli.idle_watchdog` reads `server.idle_seconds()`, and `GET /api/game`
    -- the poll the page makes every 15 seconds -- is what keeps an open tab
    from idling out. Every request counts, refusals included."""
    status, _b = jget(srv, "/api/game")
    assert status == 200
    assert server.idle_seconds() < 2.0
    server._LAST_REQUEST[0] = time.monotonic() - 3600.0
    assert server.idle_seconds() > 3000.0
    status, b = post(srv, "/api/quit", {"nope": 1})
    assert status == 400 and is_error(b, "invalid")
    assert server.idle_seconds() < 2.0, \
        "a request that was refused still proves the page is there"


# ==========================================================================
# the player-day review: W5, W6, W7, W8, W10, W14, W15, W16, W17, and the
# owner's two (the Technology section, the extractor cores' substances)
# ==========================================================================

@pytest.fixture
def gone_srv(tmp_path):
    """A server whose configured save folder has been renamed out from under
    it: the unplugged-drive state, with a `settings.json` that exists."""
    folder = tmp_path / "saves"
    folder.mkdir()
    legacy.build_save(str(folder / "save.hg"))
    st = settingsmod.Settings(save_folder=str(folder),
                              backup_folder=str(tmp_path / "backups"))
    settingsmod.store(str(tmp_path / "settings.json"), st)
    app = App(config_path=str(tmp_path / "config.json"), settings=st,
              settings_path=str(tmp_path / "settings.json"), log_path=None)
    folder.rename(tmp_path / "moved")
    with running(app, tmp=tmp_path, folder=folder) as s:
        yield s


def test_a_configured_folder_that_is_gone_is_its_own_page(gone_srv, appdata):
    """W5. A save folder on a drive that is not plugged in degraded to the
    first-run page: it said "That folder holds no save*.hg files" without ever
    naming the path, printed "first run" though `settings.json` existed, and
    offered a different `st_` profile behind a "Use this folder" button --
    which walks around 2.3's refusal to choose between accounts in one click.
    """
    status, ctype, raw = get(gone_srv, "/")
    assert status == 200 and ctype.startswith("text/html")
    page = raw.decode("utf-8")
    assert pages.MISSING_FOLDER_H1.split("%s")[0] in page
    assert str(gone_srv.folder) in page              # the path is named
    assert "not plugged in" in page
    assert "Look again" in page
    assert pages.CHOOSE_FOLDER_PATH in page
    # and no other account is offered from here
    assert "Use this folder" not in page
    assert str(appdata / "st_222") not in page
    assert "holds no" not in page


def test_the_missing_folder_page_is_not_the_first_run_page(gone_srv):
    """It is decided per request, so plugging the drive back in and pressing
    Look again is the whole of the fix: nothing has to be typed again."""
    _status, _ctype, raw = get(gone_srv, "/")
    assert pages.FIRST_RUN_H1 not in raw.decode("utf-8")
    (gone_srv.tmp / "moved").rename(gone_srv.folder)
    status, _ctype, raw = get(gone_srv, "/")
    assert status == 200 and b"app.js" in raw       # the application, in place


def test_bootstrap_stays_200_and_names_the_missing_folder(gone_srv):
    """W5 again, for a page that was already open when the drive went."""
    status, b = jget(gone_srv, "/api/bootstrap")
    assert status == 200
    assert b["save_folder_missing"] == gone_srv.app.save_folder
    assert b["save"] is None
    assert b["issues"] and any(i["level"] == "error" for i in b["issues"])


def test_choose_folder_serves_the_picker_with_nothing_pre_selected(gone_srv,
                                                                  appdata):
    """The link the missing-folder page offers: the picker, with the dead path
    neither pre-filled nor complained about, because the player came here to
    type a different one."""
    status, ctype, raw = get(gone_srv, pages.CHOOSE_FOLDER_PATH)
    assert status == 200 and ctype.startswith("text/html")
    page = raw.decode("utf-8")
    assert str(appdata / "st_222") in page          # candidates are listed
    assert "Use this folder" in page
    assert str(gone_srv.folder) not in page         # nothing pre-selected
    assert "holds no" not in page and "There is no folder at" not in page


def test_an_empty_folder_and_a_missing_one_get_different_sentences(
        first_run_srv, tmp_path):
    """W6, through the server: which sentence a player sees is decided by
    which mistake they made."""
    empty = tmp_path / "empty"
    empty.mkdir()
    status, b = post(first_run_srv, "/api/settings",
                     {"settings": {"save_folder": str(empty)}})
    assert status == 200, b
    _status, _ctype, raw = get(first_run_srv, "/")
    page = raw.decode("utf-8")
    assert "holds no" in page
    assert "There is no folder at" not in page

    status, b = post(first_run_srv, "/api/settings",
                     {"settings": {"save_folder": str(tmp_path / "nowhere")}})
    assert status == 200, b
    _status, _ctype, raw = get(first_run_srv, "/")
    assert pages.MISSING_FOLDER_H1.split("%s")[0] in raw.decode("utf-8")


# ----------------------------------------------------------------- W7: tabs

def _updated(srv):
    _status, b = jget(srv, "/api/bootstrap")
    return b["config_updated"]


def test_a_stale_based_on_is_refused_and_stores_nothing(srv):
    """W7. Tab B saved; tab A then saved its stale copy and both edits landed
    on disk with no warning in either tab, because there was no ETag, no
    If-Match and no mtime check on this write."""
    stale = _updated(srv)
    assert stale, "the config has to carry the stamp the page loaded"

    # `config.store` stamps to the second, so tab B's save has to land in a
    # later second than the one tab A loaded. `_check_based_on` documents that
    # resolution and what it costs.
    time.sleep(1.05)
    cfg = dict(srv.app.config)
    cfg["notes"] = "tab B was here"
    status, b = post(srv, "/api/config", {"config": cfg, "based_on": stale})
    assert status == 200 and b["saved"] is True
    fresh = _updated(srv)
    assert fresh != stale

    other = dict(srv.app.config)
    other["notes"] = "tab A, holding a stale copy"
    status, b = post(srv, "/api/config", {"config": other, "based_on": stale})
    assert status == 409, b
    assert is_error(b, "refused")
    assert b["where"] == "based_on"
    assert "saved by another tab (or another copy)" in b["error"]
    assert "reload from disk" in b["error"]
    # and nothing was stored
    with open(srv.app.config_path, encoding="utf-8") as fh:
        assert json.load(fh)["notes"] == "tab B was here"
    assert srv.app.config["notes"] == "tab B was here"


def test_renaming_a_container_stores_labels_and_survives_the_round_trip(srv):
    """The rename box is the only way to name an exocraft, and the path it
    uses is `POST /api/config` with `force` and `based_on`. It has to store
    without a validation error and come back on the next read: the validator
    used to warn that `labels` was deprecated and unread, which it never was.
    """
    cfg = dict(srv.app.config)
    cfg["labels"] = {"vehicle0": "Roamer, ores"}
    status, b = post(srv, "/api/config",
                     {"config": cfg, "force": True, "based_on": _updated(srv)})
    assert status == 200 and b["saved"] is True, b
    assert b["config"]["labels"] == {"vehicle0": "Roamer, ores"}
    # No *error*: an error blocks the save, and a name is not a mistake. This
    # save carries no exocraft, so the one thing the validator has to say about
    # it is the warning that the name is shown nowhere here.
    assert not [i for i in (b.get("issues") or [])
                if "labels" in (i.get("where") or "")
                and i["level"] == "error"], b.get("issues")
    with open(srv.app.config_path, encoding="utf-8") as fh:
        assert json.load(fh)["labels"] == {"vehicle0": "Roamer, ores"}
    _status, v = post(srv, "/api/validate", {"config": cfg})
    assert not [i for i in (v.get("issues") or [])
                if "deprecated" in i["message"]], v["issues"]


def test_a_write_with_no_based_on_is_unchecked(srv):
    """Every build before this one, and every non-browser client, sends none;
    an unchecked write is what they have always had."""
    cfg = dict(srv.app.config)
    cfg["notes"] = "no stamp sent"
    status, b = post(srv, "/api/config", {"config": cfg})
    assert status == 200 and b["saved"] is True


def test_force_bypasses_the_stale_check(srv):
    """The deliberate override: the page sends it for an undo, which is a
    return to a state this page has already had accepted."""
    stale = _updated(srv)
    cfg = dict(srv.app.config)
    cfg["notes"] = "somebody else"
    post(srv, "/api/config", {"config": cfg, "based_on": stale})
    forced = dict(srv.app.config)
    forced["notes"] = "forced over it"
    status, b = post(srv, "/api/config",
                     {"config": forced, "based_on": stale, "force": True})
    assert status == 200 and b["saved"] is True
    assert srv.app.config["notes"] == "forced over it"


def test_a_stamp_written_by_another_copy_of_the_sorter_is_caught(srv):
    """The stamp is read off the file, not out of memory: the other writer may
    be another copy of the sorter entirely, and that is the one that can be
    pointed at a different save folder."""
    stale = _updated(srv)
    with open(srv.app.config_path, encoding="utf-8") as fh:
        body = json.load(fh)
    body["updated"] = "2099-01-01T00:00:00"
    with open(srv.app.config_path, "w", encoding="utf-8") as fh:
        json.dump(body, fh)
    status, b = post(srv, "/api/config",
                     {"config": dict(srv.app.config), "based_on": stale})
    assert status == 409, b
    assert "2099-01-01 00:00:00" in b["error"]      # local, and readable


def test_based_on_has_to_be_a_string(srv):
    status, b = post(srv, "/api/config",
                     {"config": dict(srv.app.config), "based_on": 17})
    assert status == 400 and is_error(b, "invalid")
    assert b["where"] == "based_on"


def test_validate_is_unaffected_by_the_two_tab_check(srv):
    """`/api/validate` stores nothing, so it has nothing to race with. It
    takes a config and nothing else, and says so."""
    status, b = post(srv, "/api/validate", {"config": dict(srv.app.config)})
    assert status == 200 and "issues" in b
    status, b = post(srv, "/api/validate",
                     {"config": dict(srv.app.config), "based_on": "whenever"})
    assert status == 400 and is_error(b, "invalid")
    assert b["where"] == "based_on"


# ------------------------------------------- W8: our own apply, named as ours

def _fake_backup(srv, name="20260914-192329-save", written=True, sha=None,
                 save_name="save.hg", save_dir=None):
    """A backup folder whose manifest says what the apply left on disk."""
    folder = srv.tmp / "backups" / name
    folder.mkdir(parents=True)
    body = {
        "created": "2026-09-15T00:23:32Z",
        "outcome": "written", "completed": True,
        "save_dir": save_dir or str(srv.folder),
        "save": {"file": save_name, "sha256": "0" * 64, "size": 1},
        "meta": {"file": "mf_" + save_name, "sha256": "0" * 64, "size": 1},
        "plan": {"fingerprint": "abcd1234"},
    }
    if written:
        body["written"] = {
            "save": {"file": save_name,
                     "sha256": sha or safety.sha256(str(srv.save))},
            "meta": {"file": "mf_" + save_name, "sha256": "1" * 64},
        }
    with open(str(folder / safety.MANIFEST_NAME), "w", encoding="utf-8") as fh:
        json.dump(body, fh)
    return folder


def test_a_save_this_tool_wrote_is_not_reported_as_tampering(srv):
    """W8. The apply leaves the game's own stamp alone while the filesystem
    mtime becomes now, so the header's "something other than the game has
    written to it" was permanent after a player's first successful sort -- the
    tool accusing itself. The manifest records the hash the apply wrote; when
    the bytes on disk are still those, the page says so instead."""
    _status, b = jget(srv, "/api/save")
    assert b["last_apply"] is None, "nothing has been applied yet"

    _fake_backup(srv)
    _status, b = jget(srv, "/api/save")
    assert b["last_apply"], "the newest manifest's written hash is these bytes"
    assert b["last_apply"]["created"] == "2026-09-15T00:23:32Z"
    # and every route that renders a save agrees
    _status, boot = jget(srv, "/api/bootstrap")
    assert boot["save"]["last_apply"] == b["last_apply"]
    _status, sel = post(srv, "/api/select", {"file": "save.hg"})
    assert sel["last_apply"] == b["last_apply"]


def test_a_hash_that_is_not_the_bytes_on_disk_keeps_the_warning(srv):
    """The game saved again after the sort, or something else did: exactly the
    case the warning is for, so it stays."""
    _fake_backup(srv, sha="9" * 64)
    _status, b = jget(srv, "/api/save")
    assert b["last_apply"] is None


def test_a_manifest_with_no_written_block_keeps_the_warning(srv):
    """An older build's manifest, or an apply that stopped before its last
    step. `written` is read through `.get`, so its absence degrades to the
    sentence the header has always shown."""
    _fake_backup(srv, written=False)
    _status, b = jget(srv, "/api/save")
    assert b["last_apply"] is None


def test_a_backup_of_another_folders_save_is_not_credited(srv):
    """The manifest binds a backup to a place (Q4), so a backup of another
    profile's `save.hg` must not read as this save's last apply."""
    _fake_backup(srv, save_dir=str(srv.tmp / "somewhere-else"))
    _status, b = jget(srv, "/api/save")
    assert b["last_apply"] is None


def test_the_backups_route_carries_the_manifests_created_stamp(srv):
    """W17. The folder is named in local time and the manifest records UTC, so
    the card showed one of the two and nothing said which. Both reach the page
    now, and it renders the UTC one in local time."""
    _fake_backup(srv)
    _status, b = jget(srv, "/api/backups")
    assert len(b["backups"]) == 1
    assert b["backups"][0]["created"] == "2026-09-15T00:23:32Z"
    assert b["backups"][0]["stamp"] == "20260914-192329"


def test_a_backup_with_no_readable_manifest_gains_no_created_key(srv):
    """The route adds nothing it cannot read."""
    folder = srv.tmp / "backups" / "20260101-000000-save"
    folder.mkdir(parents=True)
    _status, b = jget(srv, "/api/backups")
    assert b["backups"] and "created" not in b["backups"][0]


# ------------------------------------------- W10: telling two profiles apart

def _stamp_meta(path, summary, play_time):
    """Write a save summary and a play time into an `mf_` fixture."""
    import struct
    slot = codec.meta_slot(str(path))
    with open(str(path), "rb") as fh:
        plain = bytearray(codec.meta_decode(fh.read(), slot))
    struct.pack_into("<Q", plain, 0x4C, play_time)
    raw = summary.encode("utf-8")
    plain[0xD8:0xD8 + len(raw)] = raw
    plain[0xD8 + len(raw)] = 0
    with open(str(path), "wb") as fh:
        fh.write(codec.meta_encode(bytes(plain), slot))


def test_detect_saves_carries_the_newest_saves_summary_and_play_time(srv,
                                                                     appdata):
    """W10. Two `st_` ids with one save each and the same timestamp are two ids
    a player has never seen; the `mf_` summary is what they recognise. Read
    from the metadata only -- no save file is opened."""
    prof = appdata / "st_333"
    prof.mkdir()
    out = legacy.write_fixture(str(prof))
    _stamp_meta(out["meta"], "Aboard Iigash Station Sigma", 86400)

    _status, b = jget(srv, "/api/detect-saves")
    row = [f for f in b["folders"] if f["path"] == str(prof)]
    assert row, [f["path"] for f in b["folders"]]
    assert row[0]["newest_save_summary"] == "Aboard Iigash Station Sigma"
    assert row[0]["newest_save_playtime"] == "24h played"
    # and it reaches the page a player reads
    _status, _ctype, raw = get(srv, pages.CHOOSE_FOLDER_PATH)
    page = raw.decode("utf-8")
    assert "Aboard Iigash Station Sigma" in page and "24h played" in page


def test_a_profile_whose_metadata_cannot_be_read_is_still_a_candidate(srv,
                                                                      appdata):
    """A folder of `save*.hg` files that are not saves at all, which is what
    the other profiles in this fixture hold."""
    _status, b = jget(srv, "/api/detect-saves")
    rows = [f for f in b["folders"] if f["path"] == str(appdata / "st_111")]
    assert rows and rows[0]["save_count"] == 1
    assert rows[0]["newest_save_summary"] is None
    assert rows[0]["newest_save_playtime"] is None


# ------------------------------------ W14: the shipped default and this save

def _fresh_app(tmp_path, variant="base", name="saves"):
    folder = tmp_path / name
    folder.mkdir()
    legacy.build_save(str(folder / "save.hg"), variant)
    return App(str(folder), str(tmp_path / (name + "-config.json")),
               str(tmp_path / "backups"),
               settings_path=str(tmp_path / "settings.json"), log_path=None)


def test_the_first_created_config_drops_rules_this_save_cannot_honour(
        tmp_path):
    """W14. The shipped default routes thirteen categories into
    `chest1`..`chest10`, and the page labels a container by the *player's* name
    for it -- so on a save with named chests the routing shelf rendered
    pairings like "Raw Resources -> Raw Resources (chest1)": rules nobody
    wrote, on the first screen a first-timer sees. A rule whose destination
    this save does not have can never fire and is not a starting point.
    """
    app = _fresh_app(tmp_path, "zero-chests")
    assert app.created is True
    stores = sorted(set(r["store"] for r in app.config["bucket_rules"]))
    assert "chest1" not in stores
    assert stores == ["cooking"], stores        # the one destination it has
    # and it is written back, not only held in memory
    with open(app.config_path, encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert on_disk["bucket_rules"] == app.config["bucket_rules"]
    assert app.config_notes and "chest1" in app.config_notes[0]
    assert "no such container" in app.config_notes[0]


def test_a_save_with_the_chests_keeps_every_shipped_rule(tmp_path):
    """A rule pointing at a container that is there is a rule this tool can
    honour, so nothing is dropped and nothing is said."""
    app = _fresh_app(tmp_path)
    assert app.created is True
    assert len(app.config["bucket_rules"]) == \
        len(cfgmod.default_config()["bucket_rules"])
    assert app.config_notes == []


def test_a_configuration_that_already_existed_is_never_stripped(tmp_path):
    """A file somebody has edited is a statement to respect -- the same rule
    `cfgmod.load` keeps about a key the file leaves out. Only a configuration
    this run created is fitted to the save."""
    folder = tmp_path / "saves"
    folder.mkdir()
    legacy.build_save(str(folder / "save.hg"), "zero-chests")
    cfg_path = tmp_path / "config.json"
    kept = cfgmod.default_config()
    cfgmod.store(str(cfg_path), kept)
    app = App(str(folder), str(cfg_path), str(tmp_path / "backups"),
              settings_path=str(tmp_path / "settings.json"), log_path=None)
    assert app.created is False
    assert len(app.config["bucket_rules"]) == len(kept["bucket_rules"])
    assert app.config_notes == []


def test_a_save_that_cannot_be_read_leaves_the_shipped_rules_alone(tmp_path):
    """Nothing to compare against. Stripping every rule because the save
    folder is on a drive that is not plugged in would be the destructive
    reading of "not present in this save"."""
    app = App(str(tmp_path / "nowhere"), str(tmp_path / "config.json"),
              str(tmp_path / "backups"),
              settings_path=str(tmp_path / "settings.json"), log_path=None)
    assert app.created is True
    assert len(app.config["bucket_rules"]) == \
        len(cfgmod.default_config()["bucket_rules"])


def test_the_shipped_default_itself_still_carries_its_rules():
    """`default_config()` is unchanged: it is the shipped layout, and the
    fitting happens in `App` where the save is known."""
    assert len(cfgmod.default_config()["bucket_rules"]) == 13


# ---------------------------------------------- W15: a corrupt settings file

def test_a_corrupt_settings_file_is_named_on_the_page(tmp_path):
    """W15. The file could not be parsed, so every value in force -- the port
    among them -- is a shipped default. That was reported only in the window
    the sorter started in, and `NMS-Sorter.exe` has no window."""
    folder = tmp_path / "saves"
    folder.mkdir()
    legacy.build_save(str(folder / "save.hg"))
    spath = tmp_path / "settings.json"
    spath.write_text("{ not json", encoding="utf-8")
    st, notes = settingsmod.load(str(spath))
    assert [n for n in notes if n["level"] == "error"]
    st.override(save_folder=str(folder))
    app = App(config_path=str(tmp_path / "config.json"), settings=st,
              settings_path=str(spath), log_path=None)
    with running(app, tmp=tmp_path) as s:
        status, b = jget(s, "/api/settings")
        assert status == 200
        assert b["load_error"] and str(spath) in b["load_error"]
        assert "defaults" in b["load_error"]
        # saving once rewrites the file, so the page stops saying it
        status, b = post(s, "/api/settings", {"settings": {"backup_keep": 4}})
        assert status == 200, b
        assert b["load_error"] is None
        status, b = jget(s, "/api/settings")
        assert b["load_error"] is None


# ------------------------------- the owner's first: the Technology section

def test_the_technology_grids_are_not_offered_to_the_page(srv):
    """Nothing is ever sorted into or out of one, so every picker already
    filtered them out and what was left was somebody else's machinery on the
    first tab a player sees -- 116 stacks whose Amount is a charge level
    rather than a count, under a heading that invited reading them as
    storage."""
    _status, b = jget(srv, "/api/save")
    assert "Technology" not in b["sections"]
    keys = [c["key"] for c in b["containers"]]
    for gone in ("suit_tech", "multitool", "freighter_tech", "fishbaitbox",
                 "foodunit"):
        assert gone not in keys, gone
    assert not [c for c in b["containers"] if c.get("is_tech")]
    assert not [c for c in b["containers"] if c["section"] == "Technology"]
    # the containers that can be sorted are all still there
    assert "suit" in keys and "chest1" in keys


def test_the_model_still_knows_the_technology_grids(srv):
    """Removed from the page, not from the program: `App.container_keys` still
    names them, which is what makes the refusal below the sentence it has
    always been."""
    keys, sortable, dead = srv.app.container_keys()
    assert "suit_tech" in keys and "multitool" in keys
    assert "suit_tech" not in sortable
    # and it is not in the third list either: a technology grid is a rule
    # somebody should fix, and that is the difference between it and a cargo
    # grid the game merged away.
    assert "suit_tech" not in dead and "multitool" not in dead
    assert "suit_cargo" in dead and "ship0_cargo" in dead


def test_a_rule_naming_a_technology_grid_is_still_refused(srv):
    """A hand-edited configuration is the only way to name one now, and it
    gets the sentence it always got -- not "not a container in this save",
    which would be untrue and would send somebody looking for the container.
    """
    cfg = dict(srv.app.config)
    cfg["bucket_rules"] = [{"bucket": "raw_resources", "store": "suit_tech"}]
    status, b = post(srv, "/api/validate", {"config": cfg})
    assert status == 200
    said = [i["message"] for i in b["issues"]
            if i["where"].startswith("bucket_rules")]
    assert any("not sortable right now" in m for m in said), said
    assert not any("not a container in this save" in m for m in said), said


# ------------------ the owner's second: the cores mine gas as well as metal

def test_the_extractor_summary_totals_every_substance(srv):
    """"mining Chromatic Metal into a buffer" was wrong about what the
    machinery does: a core holds the metal *and* the four gases, and the card
    totalled the metal alone, so a freighter banking 3,000 Oxygen was
    described by a figure that ignored it."""
    _status, b = jget(srv, "/api/save")
    x = b["extractors"]
    assert x["count"] >= 1
    subs = x["substances"]
    assert len(subs) == len(x["cores"][0]["substances"]), subs
    # the save's own slot order, and the per-substance totals
    assert [s["id"] for s in subs] == \
        [s["id"] for s in x["cores"][0]["substances"]]
    for i, s in enumerate(subs):
        want = sum(c["substances"][i]["amount"] for c in x["cores"])
        assert s["amount"] == want, s
        assert s["max"] == sum(c["substances"][i]["max"] for c in x["cores"])
    # and Chromatic Metal is still totalled on its own, for the header stat
    metal = [s for s in subs if str(s["id"]).endswith("STELLAR2")]
    assert metal and metal[0]["amount"] == x["chromatic"]


def test_the_extractor_cores_are_their_own_section_after_fleet(tmp_path):
    """The owner: "Stellar Extractors end up under 'fleet', these aren't ships
    so they probably need a better named section separate from fleet."

    Fleet is the freighter and the Corvette. A core is a read-only mining
    buffer in a freighter base room, and fourteen of them under that heading
    was the question. The section order is the server's answer, once, so the
    cards, the chips and every picker order them the same way.
    """
    app = _fresh_app(tmp_path, "base")
    view = server.save_view(app.save(), cfg=app.config)
    assert view["sections"] == ["Personal", "Storage", "Base", "Ships",
                               "Exocraft", "Fleet", "Stellar Extractors"]
    cores = [c for c in view["containers"] if c["key"].startswith("extractor")]
    assert cores, view["containers"]
    assert all(c["section"] == "Stellar Extractors" for c in cores), cores
    assert all(c["drain_only"] and not c["sortable"] for c in cores), cores
    # and nothing else moved: the freighter and the Corvette are still Fleet
    model = dict((c.key, c) for c in app.save().containers())
    assert model["freighter"].section == "Fleet"
    assert model["corvette"].section == "Fleet"


def test_a_category_nothing_can_be_routed_to_is_not_offered(srv):
    """It stays on the category list, because the Items table is a lookup and
    134 ids have that category, and it carries `routable: false` so no routing
    control on the page offers it. The page has one filter for that, used in
    every place a destination is chosen.
    """
    status, b = jget(srv, "/api/bootstrap")
    assert status == 200
    by = dict((x["key"], x) for x in b["buckets"])
    assert "system_meta" in by, "still a category the Items table can show"
    assert by["system_meta"]["routable"] is False
    assert all(by[k]["routable"] is True for k in by if k != "system_meta")
    js = _static_text_from_disk("app.js")
    assert "const routableBuckets = list =>" in js
    for place in ("routableBuckets(S.buckets).forEach(b => {",
                  "return routableBuckets(S.buckets)",
                  "routableBuckets(S.browse.buckets).forEach",
                  "routableBuckets(r.buckets).forEach"):
        assert place in js, place
    # and the shipped default no longer lists it as a never-route either
    assert "system_meta" not in (b["config"].get("never_buckets") or [])


def test_a_picker_never_offers_a_container_with_no_cells(tmp_path):
    """Every destination picker on the page is built from `sortableConts()`,
    and a container with no cells is not sortable -- it can never hold
    anything. Asserted on the server's answer and on the one list the browser
    filters, because there is no third place to get it wrong."""
    app = _fresh_app(tmp_path, "with-exocraft")
    _keys, sortable, dead = app.container_keys()
    assert dead and not (set(dead) & set(sortable)), (dead, sortable)
    js = _static_text_from_disk("app.js")
    assert "const sortableConts = () =>" in js
    assert "S.save.containers.filter(c => c.sortable)" in js, \
        "the one place a picker's options come from"


def test_a_core_card_lists_its_substances_and_not_the_machine(tmp_path):
    """Finding 2: `^MAINT_HOOVER` is the extractor itself and the card showed
    it as an item row called "Extractor Unit", with a stray tag counting it and
    `6 / 0 cells` underneath."""
    app = _fresh_app(tmp_path, "base")
    view = server.save_view(app.save(), cfg=app.config)
    core = [c for c in view["containers"] if c["key"] == "extractor1"][0]
    assert [i["stem"] for i in core["items"]] == ["STELLAR2"], core["items"]
    assert core["free"] >= 0 and core["used"] == core["valid"], core
    assert not any("MAINT" in (i["stem"] or "") for i in core["items"])
    assert all(c["free"] >= 0 for c in view["containers"]), \
        "no container reports negative free cells"


def test_the_container_view_names_the_vessel_in_use(tmp_path):
    """1d: for a vessel the save carries no name for, "in use" is the one true
    thing there is to say about which one it is. It is a field on the view, not
    a guess in the browser."""
    app = _fresh_app(tmp_path, "with-exocraft")
    conts = server.save_view(app.save())["containers"]
    by_key = dict((c["key"], c) for c in conts)
    assert by_key["ship0"]["in_use"] is True
    assert by_key["vehicle0"]["in_use"] is True
    assert by_key["suit"]["in_use"] is False
    assert all("in_use" in c for c in conts), "every row carries the field"


def test_the_view_says_which_vessel_and_which_grid_each_container_is(tmp_path):
    """3a: the page groups a vessel's grids into one box, and the pairing has
    to come from the model. Splitting `_cargo` off a key in the browser would
    be the same inference the model refuses to make, in the one place nothing
    could test it.

    The *word* is the game's own, from `data/gridnames.json`, and "General" is
    not one of them: the game has no such tab. It is only ever drawn when a
    vessel has two grids that can hold something, which is the freighter.
    """
    app = _fresh_app(tmp_path, "freighter-two-grids")
    view = server.save_view(app.save())
    by_key = dict((c["key"], c) for c in view["containers"])
    assert (by_key["suit"]["vessel"],
            by_key["suit"]["grid_role"]) == ("suit", "general")
    assert by_key["suit"]["grid"] == "Exosuit Inventory"
    assert (by_key["ship0"]["vessel"],
            by_key["ship0"]["grid_role"]) == ("ship0", "general")
    assert by_key["ship0"]["grid"] == "Starship Inventory"
    assert (by_key["freighter"]["vessel"],
            by_key["freighter"]["grid"]) == ("freighter", "Freighter Inventory")
    assert (by_key["freighter_cargo"]["vessel"],
            by_key["freighter_cargo"]["grid"]) == ("freighter", "Cargo")
    assert by_key["freighter_cargo"]["vessel_label"] == "Fixture基地", \
        "the heading is the vessel's own name, on both of its grids"
    # the dead grids are not on the view at all now, and a rule naming one is
    # answered by `container_keys`, not by a card
    for gone in ("suit_cargo", "ship0_cargo"):
        assert gone not in by_key, gone
    assert "General" not in [c["grid"] for c in view["containers"]]
    # and a container that is not a grid of anything says so. Read off the
    # model for the two the fixture leaves with no cells, because those are
    # not on the page at all.
    assert by_key["chest1"]["vessel"] is None and by_key["chest1"]["grid"] == ""
    by_model = dict((c.key, c) for c in app.save().containers())
    assert by_model["corvette"].vessel is None, \
        "the Corvette's storage is its own container, not a grid of a vessel"
    assert by_model["multitool"].vessel is None, \
        "and a multi-tool is not a grid of the exosuit"


def test_a_vessel_with_two_live_grids_keeps_both(tmp_path):
    """The freighter on the corpus's save3 and save4: a cargo bulkhead bought
    before Waypoint merged the other cargo grids away. Two cards in one box,
    each under the game's own word, and it is the only shape that draws a
    grid heading at all."""
    app = _fresh_app(tmp_path, "freighter-two-grids")
    conts = server.save_view(app.save())["containers"]
    rows = [c for c in conts if c["vessel"] == "freighter"]
    assert [c["grid"] for c in rows] == ["Freighter Inventory", "Cargo"], rows
    assert all(c["valid"] > 0 and not c["no_cells"] for c in rows), rows
    # every other vessel in the same save has exactly one
    others = {}
    for c in conts:
        if c["vessel"] and c["vessel"] != "freighter":
            others.setdefault(c["vessel"], []).append(c)
    assert others and all(len(v) == 1 for v in others.values()), others


def test_renaming_a_vessel_renames_the_heading_its_grids_sit_under(tmp_path):
    """The rename box writes the General grid's key, and the heading is that
    grid's label: a box headed "Starship 1" over a grid the player has named
    something else would be two answers to one question."""
    app = _fresh_app(tmp_path, "with-exocraft")
    conts = server.save_view(app.save(),
                             labels={"ship0": "The Kestrel"})["containers"]
    rows = [c for c in conts if c["vessel"] == "ship0"]
    assert rows, conts
    assert all(c["vessel_label"] == "The Kestrel" for c in rows), rows


def test_a_cargo_grid_the_game_merged_away_is_not_drawn(tmp_path):
    """The owner: "why is there any section besides cargo?" -- because the
    page was drawing a grid the game itself no longer shows.

    Waypoint (4.0) merged a starship's and the exosuit's cargo grid into the
    main one and left the node behind with no cells; on all seven readable
    corpus saves every one of them has none, and the game gives a player no
    way to add one. So it is not drawn: off the view, off the chips, off every
    picker and out of the plan, while its key stays valid.
    """
    app = _fresh_app(tmp_path, "with-exocraft")
    view = server.save_view(app.save())
    keys = [c["key"] for c in view["containers"]]
    assert "ship0_cargo" not in keys and "suit_cargo" not in keys, keys
    # the model still knows it, which is what answers a rule naming one
    row = [c for c in app.save().containers() if c.key == "ship0_cargo"][0]
    assert row.allocated is False and row.view("High")["no_cells"] is True
    _keys, sortable, dead = app.container_keys()
    assert "ship0_cargo" in dead and "ship0_cargo" not in sortable


def test_the_freighter_name_reaches_the_container_labels(tmp_path):
    """1b: `PlayerFreighterName` was parsed, served as `save.freighter`, and
    rendered by no line of the page."""
    app = _fresh_app(tmp_path, "freighter-two-grids")
    view = server.save_view(app.save())
    by_key = dict((c["key"], c) for c in view["containers"])
    assert view["freighter"] == "Fixture基地"
    assert by_key["freighter"]["label"] == "Fixture基地"
    assert by_key["freighter_cargo"]["label"] == "Fixture基地 Cargo"


def test_a_label_from_the_config_still_wins_over_the_derived_one(tmp_path):
    """1e: `labels` is blessed, so it must still be the last word. The
    validator used to warn that nothing read it; `save_view` always has."""
    app = _fresh_app(tmp_path, "with-exocraft")
    conts = server.save_view(app.save(),
                             labels={"vehicle0": "Roamer, ores"})["containers"]
    row = [c for c in conts if c["key"] == "vehicle0"][0]
    assert row["label"] == "Roamer, ores" and row["renamed"] is True
    assert row["default_label"] == "Roamer", \
        "and the name it replaced is still on the row: %s" % row["default_label"]


@pytest.mark.parametrize("bad", ["vehicle0", 42, ["vehicle0"], True,
                                 {"vehicle0": 42}, {"vehicle0": ["x"]},
                                 {"vehicle0": {"a": 1}}, {"vehicle0": None}])
def test_a_hand_edited_labels_value_cannot_take_the_save_section_down(
        tmp_path, bad):
    """Review 4, finding 4: `labels.get(c.key)` on a string raised
    `AttributeError` out of /api/select and /api/bootstrap, so a config that
    saved cleanly left the Save section unable to render and the only way out
    was editing the file by hand. A value of the wrong type is no name."""
    app = _fresh_app(tmp_path, "with-exocraft")
    conts = server.save_view(app.save(), labels=bad)["containers"]
    row = [c for c in conts if c["key"] == "vehicle0"][0]
    assert row["label"] == "Roamer", "the derived name stands: %r" % row["label"]
    assert not row.get("renamed"), row


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_whitespace_label_is_the_same_as_no_entry(tmp_path, blank):
    """What `docs/RULES.md` has always said. A bare truthiness test kept
    `"   "`, so the card rendered blank and claimed it was renamed."""
    app = _fresh_app(tmp_path, "with-exocraft")
    conts = server.save_view(app.save(),
                             labels={"vehicle0": blank})["containers"]
    row = [c for c in conts if c["key"] == "vehicle0"][0]
    assert row["label"] == "Roamer" and not row.get("renamed"), row


def test_a_label_is_cut_to_the_ceiling_the_validator_states(tmp_path):
    """A 300-character name reached the card, three `<option>` lists and the
    plan's container table verbatim. None of them can lay one out."""
    app = _fresh_app(tmp_path, "with-exocraft")
    conts = server.save_view(
        app.save(), labels={"vehicle0": "Ore " * 200})["containers"]
    row = [c for c in conts if c["key"] == "vehicle0"][0]
    assert len(row["label"]) <= cfgmod.LABEL_MAX, row["label"]
    assert row["renamed"] is True and row["label"].startswith("Ore"), row


def test_a_starship_slot_nobody_owns_is_the_hull_and_not_the_cells(tmp_path):
    """The owner, on save10: "my slot5 save10.hg has Starship 1 and Starship 2
    ... they are slots that used to have ships I think."

    They are. `ShipOwnership[2]` there carries no `Resource.Filename` and
    `[False, "0x0"]` for a seed, and 35 cells with nothing in them: the layout
    the ship had before it was traded away. Measured across the seven readable
    corpus saves, every row with a hull has a seed and every row without one
    has neither, so the hull is the whole test and the cells say nothing.

    The grids stay in `containers` -- a rule, a plan and the validator all
    name containers by key whether a card is drawn or not -- and carry
    `slot_empty`, which is what keeps them off the cards and out of the chips.
    """
    app = _fresh_app(tmp_path, "empty-ship-slots")
    view = server.save_view(app.save())
    conts = view["containers"]
    by = dict((c["key"], c) for c in conts)
    model = dict((c.key, c) for c in app.save().containers())
    assert model["ship0"].slot_empty is False, "it is holding something"
    assert model["ship1"].slot_empty is True, "all zeros"
    assert model["ship2"].slot_empty is True, \
        "cells but no hull: the leftover layout of a ship that is gone"
    assert model["ship3"].slot_empty is False, "a hull and no cells is a ship"
    # what reaches the page: the one ship, and nothing for the other three.
    # ship1 and ship3 have no cells at all, so they are not containers; ship2
    # has 35 of them and is kept off the cards by `slot_empty`.
    assert "ship0" in by and by["ship0"]["slot_empty"] is False
    assert "ship1" not in by and "ship3" not in by, sorted(by)
    assert by["ship2"]["slot_empty"] is True
    assert all("slot_empty" in c for c in conts), \
        "on every row, like `in_use` and `vessel`"
    # an exocraft cannot be decided that way: every row carries an empty hull
    # and a full grid on all seven corpus saves, owned or not, so the
    # all-zeros test is the only thing the save says about one
    veh = _fresh_app(tmp_path, "with-exocraft", name="exo")
    rows = [c for c in server.save_view(veh.save())["containers"]
            if c["vessel"] == "vehicle0"]
    assert rows and all(c["slot_empty"] is False for c in rows), rows
    # and the count the sentence under the chips uses is over every grid the
    # save carries, not over the ones that survived the filter: an all-zeros
    # slot has no cells either and is dropped twice over, so counting the
    # containers the page was sent said one on a save with nine
    assert view["unowned_slots"] == 2, view["unowned_slots"]
    js = _static_text_from_disk("app.js")
    assert "c => !c.slot_empty" in js, "no card for a slot nobody owns"
    assert "S.save.unowned_slots" in js, \
        "and the ones nobody owns are the sentence under the chips"


def test_an_item_id_is_a_tooltip_everywhere_but_the_items_table(tmp_path):
    """"Why do we show things like MAINT_HOOVER in the app" was answered at
    its one instance: `Carbon FUEL1`, `Faecium PLANT_POOP3` and
    `Life Support GelPRODFUEL2` were still printed on the Save, Rules and Plan
    sections, 34 in one chest and 59 on one plan, and in the Plan the id was
    glued to the name with no separator at all.

    The Items table keeps its visible `ID` column: it is labelled, it carries
    a `?`, it is what `Search name or id` searches and it is the identity the
    configuration stores.
    """
    js = _static_text_from_disk("app.js")
    assert "function idSpan(" not in js, "the id chip is gone"
    assert 'el("span", "id", ' not in js and 'idSpan("pid"' not in js
    assert "function withId(node, id)" in js
    assert "function idTitle(id)" in js
    # the Items table's own column, untouched
    assert '"it-id"' in js or "it-id" in js, "the ID column stays"
    css = _static_text_from_disk("app.css")
    assert ".pid{" not in css, "and its styling with it"


def test_the_extractor_block_shows_the_live_cores_and_counts_the_stale(
        tmp_path):
    """Three rooms, four buffers: three cards and one number.

    The block used to list every buffer the filter matched, which on the
    operator's save was seventeen cards -- three of them holding whatever the
    rooms they no longer belong to had mined, under a red "Mismatch: 17 cores
    against 14 rooms". The leftover's 99 units are in no card, in no total and
    in no header stat; the fact that it exists is one line under the cards.
    """
    app = _fresh_app(tmp_path, "extractor-stale")
    x = server.save_view(app.save())["extractors"]
    assert (x["count"], x["rooms"], x["stale"]) == (3, 3, 1), x
    assert (x["unplaced"], x["coreless"]) == (0, 0), x
    assert x["issue"] is None and x["consistent"] is True, x
    assert [c["key"] for c in x["cores"]] == \
        ["extractor1", "extractor2", "extractor3"], x["cores"]
    assert [c["room"] for c in x["cores"]] == [1, 2, 3], \
        "each card names the room it is in: %s" % x["cores"]
    held = [(c["key"], s["amount"]) for c in x["cores"]
            for s in c["substances"]]
    assert sorted(a for _k, a in held) == [10, 20, 30], held
    assert x["chromatic"] == 60, \
        "and the header stat totals the live cores only: %s" % x["chromatic"]
    assert all(s["amount"] != 99 for c in x["cores"] for s in c["substances"]), \
        "the leftover is nowhere on the page: %s" % held


def test_the_totals_are_per_substance_across_every_core():
    """The fixture's core holds one substance, so the summing is exercised
    here: two cores, the metal and two gases, in the save's slot order, with a
    core that is missing one of them."""
    cores = [
        {"index": 0, "substances": [
            {"id": "^STELLAR2", "name": "Chromatic Metal",
             "amount": 300, "max": 2975},
            {"id": "^OXYGEN", "name": "Oxygen", "amount": 0, "max": 2975},
            {"id": "^LAUNCHSUB", "name": "Di-hydrogen",
             "amount": 12, "max": 2975}]},
        {"index": 1, "substances": [
            {"id": "^STELLAR2", "name": "Chromatic Metal",
             "amount": 249, "max": 2975},
            {"id": "^OXYGEN", "name": "Oxygen", "amount": 0, "max": 2975}]},
    ]
    totals = server.extractor_totals(cores)
    assert [(t["name"], t["amount"], t["max"]) for t in totals] == [
        ("Chromatic Metal", 549, 5950),
        ("Oxygen", 0, 5950),
        ("Di-hydrogen", 12, 2975),
    ]


def test_the_totals_of_no_cores_at_all_are_empty():
    """V6: with no cores every figure is a zero the save never stated."""
    assert server.extractor_totals([]) == []
    assert server.extractor_totals(None) == []


# --------------------------------------------------- the page, as it ships

def _static_text_from_disk(name):
    """The shipped `static/` file, read directly.

    The `srv` fixture is not in scope for every test that wants to assert on
    the page, and `nms_sorter/static/` is what the server serves: the route
    itself is covered by `_static_text` below.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "nms_sorter", "static", name),
              encoding="utf-8") as fh:
        return fh.read()


def _static_text(srv, path):
    status, ctype, raw = get(srv, path)
    assert status == 200, path
    assert ctype.startswith("text/") or "javascript" in ctype, ctype
    return raw.decode("utf-8")


def test_the_no_destination_card_is_in_the_second_column(srv):
    """Finding 6a: it was the second card in the *first* column, and only the
    first card in a column was told it could shrink, so it sized itself to its
    content and took 269 px of 431 -- leaving the destinations list 84 px of a
    995 px table, one row of eleven.

    Asserted on the shipped markup rather than on a screenshot: the order of
    the two `.colstack`s is the fix."""
    html = _static_text(srv, "/")
    cat = html.split('id="tab-categories"', 1)[1].split('id="tab-rules"', 1)[0]
    stacks = cat.split('class="colstack"')
    assert len(stacks) == 3, "two columns in the Categories section"
    assert 'id="assign-scroll"' in stacks[1], "destinations in the first"
    assert 'id="bucket-shelf"' not in stacks[1], \
        "and the shelf is not sharing that column with it"
    assert 'id="bucket-shelf"' in stacks[2] and 'id="sources"' in stacks[2]
    assert stacks[2].index('id="sources"') < stacks[2].index('id="bucket-shelf"'), \
        "under Sources, not above it"


def test_the_rail_is_not_laid_out_by_the_apply_step_rule(srv):
    """The costliest measurement in the QA pass: every count in the rail --
    `29 containers`, `13 routed`, `12 moves`, `5,133` and the `tick to enable`
    hint under Apply -- had `clientWidth: 0` against `scrollWidth: 73`, at
    every window size.

    Cause: the apply card's own `.step` rule (`display:grid` over
    `22px 200px 1fr`) was written unscoped, 370 lines after the rail's, so it
    won on both and gave the third column -- the count -- 0 px in a 186 px
    box. Scoped to `.steps`, which is the only thing that holds one."""
    css = _static_text(srv, "/static/app.css")
    assert ".steps .step{display:grid" in css
    assert (chr(10) + ".step{display:grid") not in css, \
        "an unscoped `.step` grid also matches every step in the rail"
    for sel in (".steps .step .lbl", ".steps .step .det", ".steps .step .s"):
        assert sel in css, sel


def test_the_no_destination_shelf_shows_its_rows(srv):
    """Finding 6's regression: the shelf measured 35 px against 295 px of
    content at 1280x720 *and* at 1600x900, so not one of the four rows fitted
    and the `route to...` picker -- the only control that clears the page's own
    warning -- was below the fold of a box smaller than one row.

    Two measurements in the CSS: a row is one line, and the shelf opens to
    three of them.

    2026-09-17: the shelf is behind its own fold now. The column cannot hold
    both lists -- 468 px wanted of the 431 px there are at 1280x720 -- and the
    Sources list is the one worked down every session, so this is what folds.
    The card is sized by its content rather than capped: 48 px closed, 189
    open, of which the shelf is 106, three of its 33 px rows."""
    css = _static_text(srv, "/static/app.css")
    assert "#bucket-shelf{flex:none;min-height:34px;max-height:106px" in css
    row = css.split(chr(10) + ".unrouted{", 1)[1].split("}", 1)[0]
    assert "flex-wrap:nowrap" in row,         "a row is the category and the picker that fixes it, on one line: %s" % row
    assert "#tab-categories .colstack>.card.fixed{flex:none}" in css
    html = _static_text(srv, "/")
    assert 'class="unroute-fold"' in html,         "and the destructive control is behind a fold, not under the remedy"
    assert 'class="nd-fold" id="nd-fold">' in html,         "and the shelf itself is behind one, closed"


def test_no_compacted_control_is_under_the_sheets_own_24px_floor(srv):
    """`button,.btn,.iconbtn,.chip .x,.del,.linkbtn,.fchip{min-height:24px}`
    has been this sheet's floor for the whole project and WCAG 2.5.8 asks for
    24x24; 6b and 6c shipped a 20 px pill and two 22 px controls."""
    css = _static_text(srv, "/static/app.css")
    for rule in (".destmeta .pill{min-height:24px",
                 ".addbtn{min-height:24px",
                 ".addsel{min-height:24px"):
        assert rule in css, rule
    assert "min-height:22px" not in css and "min-height:20px" not in css,         "and nothing else went under it either"


def test_a_border_that_is_a_things_whole_shape_carries_3_to_1(srv):
    """WCAG 1.4.11. `--line-2` is 1.57:1 on `--surface`, which is right for a
    divider and was what the `in use` pill, the `+` button and the `route
    to...` picker were drawn with: 1.47 to 1.68 measured in the page, so the
    pill did not read as a pill. `--line-3` is 3.33:1 on `--surface` and
    3.14:1 on `--surface-2`."""
    css = _static_text(srv, "/static/app.css")
    assert "--line-3:#5C6B84;" in css
    # Every border whose job is to be the shape of a thing rather than the
    # line between two things. Matched on the declaration, not on the whole
    # rule, so reflowing the sheet does not fail this.
    tag = css.split(".tag{", 1)[1].split("}", 1)[0]
    assert "border:1px solid var(--line-3)" in tag, tag
    pill = css.split(chr(10) + ".pill{", 1)[1].split("}", 1)[0]
    assert "border:1px solid var(--line-3)" in pill, pill
    addbtn = css.split(".addbtn{", 1)[1].split("}", 1)[0]
    assert "border-color:var(--line-3)" in addbtn, addbtn
    addsel = css.split(".addsel{", 1)[1].split("}", 1)[0]
    assert "border-color:var(--line-3)" in addsel, addsel
    route = css.split("select.chip-route{", 1)[1].split("}", 1)[0]
    assert "border-color:var(--line-3)" in route, route


def test_a_control_only_in_the_dom_when_it_is_open_is_not_a_focus_successor(
        srv):
    """6c put the category select behind a `+`, so `add-to-<key>` exists only
    while that select is open -- and two call sites still named it after a
    re-render that had removed it. Focus fell to `<body>` on the primary
    keyboard path, which is the No-destination card's whole purpose. The
    fallback could not recover it either: `-[a-z-]+$` never matched a key
    ending in a digit, and every container key does."""
    js = _static_text(srv, "/static/app.js")
    assert "focusAfter(`add-to-${key}-open`);" in js
    assert "? `add-to-${storeKey}-open`" in js
    assert "`add-to-${storeKey}`" not in js and "focusAfter(`add-to-${key}`)" not in js
    assert 'tries.push(key.replace(/-[^-]+$/, ""));' in js
    assert '/-[a-z-]+$/' not in js
    assert 'if (!/-open$/.test(key)) tries.push(key + "-open");' in js


def test_a_name_that_does_not_fit_carries_its_whole_self(srv):
    """`Trade Goods, Curiosities and ...` and `Expedition and Se...` with no
    tooltip: unrecoverable without resizing the window. Measured after layout
    rather than guessed from the string, and swept again when a section is
    shown, because a panel that is `hidden` has no width and nothing in it is
    ever clipped."""
    js = _static_text(srv, "/static/app.js")
    assert "function titleIfClipped(node, full)" in js
    assert "function clipSweep()" in js
    assert "n.scrollWidth > n.clientWidth + 1" in js
    assert 'window.addEventListener("resize", clipSweepSoon);' in js
    assert "titleIfClipped(cn, c ? c.label : key);" in js


def test_the_help_note_goes_under_its_row_not_inside_it(srv):
    """The `?` beside Verify file sat on the line *above* the button it
    belongs to, because the note was inserted inside the `data-help` span --
    a flex item, which then grew to the note's 78ch and took the control row
    onto a second line. `.helpnote` is `flex:1 1 100%` and was written for a
    line of its own."""
    js = _static_text(srv, "/static/app.js")
    assert 'const inRow = row && row.classList && row.classList.contains("row");' in js
    assert "anchor.insertAdjacentElement(\"afterend\", note);" in js
    assert 'b.insertAdjacentElement("afterend", note)' not in js


def test_every_card_in_those_columns_may_shrink(srv):
    """The `:first-child` in the one-screen flex rules is what finding 6 is:
    the rule has to name every card in the column."""
    css = _static_text(srv, "/static/app.css")
    assert "#tab-categories .colstack>.card:first-child" not in css
    assert "#tab-categories .colstack>.card," in css
    assert "#tab-categories .colstack>.card.fixed{" in css, \
        "and a card that opts out says so by name"


def test_the_destinations_list_keeps_its_table_meaning(srv):
    """6d: the `<table>` is an auto-filling grid of blocks now, so a wide
    window is two columns. "Container" and "Categories it receives" is what the
    two halves of a row mean, and that is carried by roles rather than by the
    element.

    `grid`, not `table`, since the rows took the arrow keys: a grid is a table
    a keyboard can walk, and announcing a table while handling Up, Down, Home
    and End is the half-and-half that neither audience gets anything from.
    """
    js = _static_text(srv, "/static/app.js")
    assert 'el("table", "assign")' not in js, "the table element is gone"
    for role in ('"role", "grid"', '"role", "row"', '"role", "columnheader"',
                 '"role", "gridcell"'):
        assert role in js, role
    assert "Categories it receives" in js and '"Container"' in js


def test_the_destinations_grid_is_one_tab_stop_with_the_arrows_between_cells(
        srv):
    """The Items table has had this since P6 and the destinations rows never
    had it: 24 tab stops to cross eleven rows, and arrow keys that only
    scrolled the box. One roving `tabIndex`, kept by container key so a row
    appearing or going does not move it, and the same key handling."""
    js = _static_text(srv, "/static/app.js")
    assert "let DEST_ROVE = null;" in js
    assert "function roveDest()" in js and "function destGridKeys(tr)" in js
    assert 'tr.tabIndex = key === DEST_ROVE ? 0 : -1;' in js,         "one row carries the tab stop"
    assert 'tr.querySelectorAll("button,select,input").forEach' in js,         "and every control in the row is off the tab order"
    for k in ('"ArrowRight"', '"ArrowLeft"', '"ArrowDown"', '"Home"', '"End"'):
        assert k in js, k
    assert 'fk(tr, "destrow-" + key)' in js,         "and the row itself is focus-key preserved across a re-render"


def test_a_container_key_is_a_tooltip_unless_two_names_collide(srv):
    """3c. The key was printed beside the name in seven places, always: on all
    65 Save cards, on every destinations row, in every picker's option text, in
    the tidy list and in the plan's container table.

    The reason (U9) was real and is kept -- a player who renames a chest "Raw
    Resources" makes it read exactly like the category on the shelf beside it
    -- but it is applied to the collision rather than to every container. The
    key cannot simply go: `config.json` names it and `docs/RULES.md` documents
    it, so it is in the `title` and the accessible name always.
    """
    js = _static_text(srv, "/static/app.js")
    assert "function labelCollides(" in js
    assert "labelCollides(c.label) ? `${c.label} · ${c.key}` : c.label" in js
    # every keySpan call site has to cope with it returning nothing
    assert 'el("span", "ckey", c.key)' not in js, \
        "the Save card's always-on key span"
    assert "if (!labelCollides(contLabel(key))) return null;" in js
    assert 'toggle.title = `${c.label} (${c.key}) ' in js, \
        "and the card carries it where a person can still find it"
    assert "`as ${c.group}`" in js, \
        "along with the stack-size group, a CamelCase word that was visible"


def test_every_shipped_settings_key_has_a_label(srv):
    """`setLabel` falls back to the raw key with its underscores swapped,
    which is a field name on screen. A field `settings.py` adds must still
    render, so the fallback stays -- but no *shipped* key may reach it."""
    js = _static_text(srv, "/static/app.js")
    block = js.split("const SET_LABEL = {", 1)[1].split("};", 1)[0]
    for f in settingsmod.FIELDS:
        assert f["key"] + ":" in block, f["key"]


def test_every_config_key_has_a_name_a_player_reads(srv):
    """Same for `PART_NAME`, which names what changed in the revert
    confirmation, and for `whereLabel`, which names where a validation message
    points."""
    js = _static_text(srv, "/static/app.js")
    block = js.split("const PART_NAME = {", 1)[1].split("};", 1)[0]
    where = js.split("function whereLabel(", 1)[1].split("\n}", 1)[0]
    for key in cfgmod.default_config():
        if key in ("config_version", "name", "notes", "updated"):
            continue            # already English, and not a field name
        assert key + ":" in block, "PART_NAME is missing %s" % key
        assert '"%s"' % key in where, "whereLabel is missing %s" % key


def test_the_page_has_no_technology_filter_left(srv):
    """The checkbox went with the grids it filtered."""
    html = _static_text(srv, "/")
    js = _static_text(srv, "/static/app.js")
    assert "hide-tech" not in html and "hide-tech" not in js
    assert "hide technology grids" not in html
    assert "is_tech" not in js


def test_the_page_renders_the_second_launch_notice(srv):
    """W12. A second double-click finds the port taken, recognises the sorter
    that has it, opens this page and closes itself -- which is right, and
    happened in silence, so the second launch looked like one that did
    nothing."""
    js = _static_text(srv, "/static/app.js")
    assert "second-launch" in js
    # the sentence is written across two source lines, so it is matched in the
    # two halves the file actually holds
    assert "another copy of the sorter was started and closed" in js
    assert "itself; this is the running one" in js
    assert "history.replaceState" in js         # and taken out of the URL


def test_the_page_says_the_zone_its_times_are_in(srv):
    """W17. The manifest records UTC and the backup folder beside it is named
    in local time; the card showed one of the two, five hours apart, with
    nothing to say it was the same moment."""
    js = _static_text(srv, "/static/app.js")
    assert "resolvedOptions().timeZone" in js
    assert "local time" in js
    assert "function localTime" in js
    assert "backupCreated" in js


def test_the_page_names_every_repeated_button(srv):
    """W16. The duplicates found: two "show more" in the Items panel, one
    "rename" per container card, one "reset to <category>" per overridden item
    row, one "use this one" per folder the Settings tab found, and the
    first-run page's "Use this folder" offers (`tests/test_settings.py`).

    The two "show more" buttons went with the growing document: the Items
    panel is a fixed-height table body with a pager now (`design/PROPOSAL.md`
    C2), so the pair that has to stay distinct is the pager's two arrows,
    which are one glyph each and would otherwise read identically.
    """
    html = _static_text(srv, "/")
    js = _static_text(srv, "/static/app.js")
    assert 'aria-label="show more items' not in html
    assert html.count('aria-label="previous page of items"') == 1
    assert html.count('aria-label="next page of items"') == 1
    assert 'b.setAttribute("aria-label", "rename " + c.label)' in js
    assert 'b.setAttribute("aria-label", "use this one: " + f.path)' in js
    assert "reset to ${it.original_bucket_label}`)" in js


def test_the_page_offers_reload_from_disk_on_the_two_tab_refusal(srv):
    """W7, the page half: the sentence goes above the bar that holds the
    button that answers it, and the edits are kept as an undo so a reload does
    not throw them away."""
    html = _static_text(srv, "/")
    js = _static_text(srv, "/static/app.js")
    assert 'id="config-conflict"' in html
    assert 'id="config-reload"' in html
    assert "function configConflict" in js
    assert "isStaleConfig" in js
    assert 'where === "based_on"' in js
    assert "based_on: basedOn()" in js


def test_the_page_greys_out_a_restore_that_would_discard_play(srv):
    """`current_matches` is False: the save has been written since that backup
    was taken, `POST /api/restore` refuses it, and the button is disabled here
    rather than offered and then refused."""
    js = _static_text(srv, "/static/app.js")
    assert "function restorability" in js
    # two source lines, as the file holds it
    assert "the save has been written since this backup was taken" in js
    assert "restoring would discard that play" in js
    assert "whether the save changed since cannot be checked" in js
    assert "current_matches" in js
    assert "undo.disabled = !ableUndo.ok" in js


# ------------------------------------------------------- W3: one listener

def test_a_second_serve_on_one_port_is_refused(srv):
    """W3. `socketserver` sets `allow_reuse_address = 1`, and on Windows that
    means "bind even if somebody is already listening here": three
    double-clicks bound three servers to one port and the OS handed each
    connection to whichever accepted first, so the page a player was looking
    at belonged to a different process from one request to the next.

    `serve` binds exclusively, so the second bind raises and the caller moves
    to the next port."""
    with pytest.raises(OSError):
        server.serve(srv.app, "127.0.0.1", srv.port)
    # and the first server is still the one answering
    status, _b = jget(srv, "/api/version")
    assert status == 200



# ==========================================================================
# starting over, without losing the configuration that was there
# "we need a way to reset the config (don't lose my existing config)". The
# route existed and was reachable from exactly one place: the startup page
# that comes up when `config.json` cannot be read at all.
# ==========================================================================

KEPT_RE = re.compile(r"^config\.before-reset-\d{8}-\d{6}(-\d+)?\.json$")


def _kept_files(tmp):
    return sorted(p.name for p in tmp.iterdir() if KEPT_RE.match(p.name))


def _named_config(srv, name):
    """Put a named configuration on disk and in the server. -> its raw bytes."""
    cfg = legacy.synthetic_config()
    cfg["name"] = name
    status, b = post(srv, "/api/config", {"config": cfg, "force": True})
    assert status == 200 and b["saved"], b
    return (srv.tmp / "config.json").read_bytes()


def test_reset_keeps_the_configuration_it_replaces(srv):
    """The whole of "don't lose my existing config": the file that was there is
    beside the new one, byte for byte, under a name of its own."""
    before = _named_config(srv, "Mine")
    status, b = post(srv, "/api/config/reset", {})
    assert status == 200, b
    assert KEPT_RE.match(b["kept"]), b["kept"]
    kept = srv.tmp / b["kept"]
    assert kept.read_bytes() == before
    # and the shipped default is what is in use now
    assert b["config"]["name"] == cfgmod.default_config()["name"]
    on_disk = json.loads((srv.tmp / "config.json").read_text(encoding="utf-8"))
    assert on_disk["name"] == cfgmod.default_config()["name"]


def test_a_kept_configuration_is_not_in_the_bak_rotation(srv):
    """`.bak.1`..`.bak.5` shifts on every save, so five edits after a reset the
    copy would have fallen off the end. The kept file has its own name."""
    _named_config(srv, "Mine")
    status, b = post(srv, "/api/config/reset", {})
    assert status == 200, b
    for _i in range(cfgmod.BAK_KEEP + 2):
        status, _ = post(srv, "/api/config",
                         {"config": cfgmod.default_config(), "force": True})
        assert status == 200
    assert (srv.tmp / b["kept"]).exists()
    body = json.loads((srv.tmp / b["kept"]).read_text(encoding="utf-8"))
    assert body["name"] == "Mine"


def test_reset_still_clears_the_minted_plans_and_says_what_it_kept(srv):
    before = srv.save.read_bytes()
    plan = _plan(srv)
    status, b = post(srv, "/api/config/reset", {})
    assert status == 200 and b["kept"]
    status, b2 = post(srv, "/api/apply",
                      {"file": str(srv.save),
                       "fingerprint": plan["fingerprint"]})
    assert status == 409 and "has not printed a plan" in b2["error"]
    assert srv.save.read_bytes() == before


def test_the_kept_list_is_newest_first_and_reads_each_file(srv):
    _named_config(srv, "First")
    status, one = post(srv, "/api/config/reset", {})
    assert status == 200
    _named_config(srv, "Second")
    status, two = post(srv, "/api/config/reset", {})
    assert status == 200
    status, b = jget(srv, "/api/config/kept")
    assert status == 200
    assert b["dir"] == str(srv.tmp)
    assert b["config_path"] == str(srv.tmp / "config.json")
    files = [k["file"] for k in b["kept"]]
    assert files == [two["kept"], one["kept"]], files
    assert [k["name"] for k in b["kept"]] == ["Second", "First"]
    for k in b["kept"]:
        assert k["readable"] is True
        assert k["updated"], k
        assert k["size"] > 0
    assert _kept_files(srv.tmp) == sorted(files)


def test_a_kept_file_that_will_not_read_is_listed_as_such(srv):
    _named_config(srv, "Mine")
    status, one = post(srv, "/api/config/reset", {})
    assert status == 200
    (srv.tmp / one["kept"]).write_text("{ not json", encoding="utf-8")
    status, b = jget(srv, "/api/config/kept")
    assert status == 200
    row = [k for k in b["kept"] if k["file"] == one["kept"]][0]
    assert row["readable"] is False
    assert row["name"] is None and row["updated"] is None
    # and it is refused rather than swapped in
    status, b = post(srv, "/api/config/use", {"file": one["kept"]})
    assert status == 409
    assert is_error(b, "refused")
    assert "could not be read" in b["error"]
    assert "untouched" in b["error"]
    on_disk = json.loads((srv.tmp / "config.json").read_text(encoding="utf-8"))
    assert on_disk["name"] == cfgmod.default_config()["name"]


def test_use_puts_a_kept_configuration_back_and_keeps_the_current_one(srv):
    """Both directions, and the file that comes back is the file that was kept:
    the same bytes, not a re-serialisation with a new `updated` stamp."""
    mine = _named_config(srv, "Mine")
    status, one = post(srv, "/api/config/reset", {})
    assert status == 200
    shipped = (srv.tmp / "config.json").read_bytes()

    status, b = post(srv, "/api/config/use", {"file": one["kept"]})
    assert status == 200, b
    assert b["used"] == one["kept"]
    assert KEPT_RE.match(b["kept"]), b["kept"]
    assert b["config"]["name"] == "Mine"
    assert (srv.tmp / "config.json").read_bytes() == mine
    # the layout it replaced is kept too, and the file used is still there
    assert (srv.tmp / b["kept"]).read_bytes() == shipped
    assert (srv.tmp / one["kept"]).read_bytes() == mine
    status, boot = jget(srv, "/api/bootstrap")
    assert boot["config"]["name"] == "Mine"


def test_use_clears_the_minted_plans(srv):
    _named_config(srv, "Mine")
    status, one = post(srv, "/api/config/reset", {})
    assert status == 200
    plan = _plan(srv)
    status, _b = post(srv, "/api/config/use", {"file": one["kept"]})
    assert status == 200
    status, b = post(srv, "/api/apply",
                     {"file": str(srv.save),
                      "fingerprint": plan["fingerprint"]})
    assert status == 409 and "has not printed a plan" in b["error"]


@pytest.mark.parametrize("name", [
    "config.json",
    "config.json.bak.1",
    "settings.json",
    "config.before-reset-20260916-142233.txt",
    "config.before-reset-2026-142233.json",
    "..\\config.before-reset-20260916-142233.json",
    "../config.before-reset-20260916-142233.json",
    "sub/config.before-reset-20260916-142233.json",
    "C:\\Windows\\config.before-reset-20260916-142233.json",
    "/etc/config.before-reset-20260916-142233.json",
])
def test_use_refuses_anything_that_is_not_a_kept_basename(srv, name):
    """A basename matching the pattern cannot hold a separator, so the path is
    inside the configuration folder by construction. Everything else is a 400
    that names the folder, the way restore's is."""
    before = (srv.tmp / "config.json").read_bytes()
    status, b = post(srv, "/api/config/use", {"file": name})
    assert status == 400, b
    assert is_error(b, "invalid")
    assert b["where"] == "file"
    assert str(srv.tmp) in b["error"]
    assert (srv.tmp / "config.json").read_bytes() == before
    assert not _kept_files(srv.tmp)


def test_use_needs_a_file_at_all(srv):
    status, b = post(srv, "/api/config/use", {})
    assert status == 400 and is_error(b, "invalid") and b["where"] == "file"
    status, b = post(srv, "/api/config/use", {"file": "  "})
    assert status == 400 and is_error(b, "invalid")
    status, b = post(srv, "/api/config/use",
                     {"file": "config.before-reset-20260916-142233.json",
                      "nonsense": 1})
    assert status == 400 and is_error(b, "invalid") and b["where"] == "nonsense"


def test_use_refuses_a_kept_name_that_is_not_there(srv):
    before = (srv.tmp / "config.json").read_bytes()
    status, b = post(srv, "/api/config/use",
                     {"file": "config.before-reset-20260916-142233.json"})
    assert status == 400 and is_error(b, "invalid")
    assert "not one of the kept configurations" in b["error"]
    assert (srv.tmp / "config.json").read_bytes() == before


def test_neither_start_over_route_runs_while_a_write_does(srv):
    """The guard `POST /api/quit` uses, read the same way: `App.busy` without
    the lock, because a swap that queued on the lock would be granted the
    instant the apply released it."""
    _named_config(srv, "Mine")
    status, one = post(srv, "/api/config/reset", {})
    assert status == 200
    before = (srv.tmp / "config.json").read_bytes()
    srv.app.begin_write()
    try:
        status, b = post(srv, "/api/config/reset", {})
        assert status == 409 and is_error(b, "refused")
        assert b["error"] == server.CONFIG_BUSY
        status, b = post(srv, "/api/config/use", {"file": one["kept"]})
        assert status == 409 and is_error(b, "refused")
        assert b["error"] == server.CONFIG_BUSY
    finally:
        srv.app.end_write()
    assert (srv.tmp / "config.json").read_bytes() == before
    assert _kept_files(srv.tmp) == [one["kept"]]
    # and both work again the moment the write ends
    status, b = post(srv, "/api/config/use", {"file": one["kept"]})
    assert status == 200, b


def test_neither_start_over_route_runs_on_a_stale_based_on(srv):
    """W7, for the two new buttons: another tab saved after this page loaded,
    so the answer is a 409 carrying `where: based_on` and the page offers
    reload from disk."""
    _named_config(srv, "Mine")
    status, one = post(srv, "/api/config/reset", {})
    assert status == 200
    before = (srv.tmp / "config.json").read_bytes()
    stale = "2000-01-01T00:00:00"

    status, b = post(srv, "/api/config/reset", {"based_on": stale})
    assert status == 409 and is_error(b, "refused")
    assert b["where"] == "based_on"
    status, b = post(srv, "/api/config/use",
                     {"file": one["kept"], "based_on": stale})
    assert status == 409 and is_error(b, "refused")
    assert b["where"] == "based_on"
    assert (srv.tmp / "config.json").read_bytes() == before
    assert _kept_files(srv.tmp) == [one["kept"]]

    # `force` is the deliberate override, as on /api/config
    status, b = post(srv, "/api/config/use",
                     {"file": one["kept"], "based_on": stale, "force": True})
    assert status == 200, b
    assert b["config"]["name"] == "Mine"


def test_the_start_over_routes_check_the_origin_and_the_body_cap(srv):
    status, b = post(srv, "/api/config/reset", {},
                     origin="http://evil.example")
    assert status == 403 and is_error(b, "refused")
    status, b = post(srv, "/api/config/use", {"file": "x"},
                     origin="http://evil.example")
    assert status == 403 and is_error(b, "refused")
    status, b = post(srv, "/api/config/use", {"file": "x"},
                     fake_length=server.MAX_BODY + 1)
    assert status == 413 and is_error(b, "invalid")
    status, b = post(srv, "/api/config/use", raw=b"{}", ctype="text/plain")
    assert status == 415 and is_error(b, "invalid")


def test_the_start_over_card_is_on_the_settings_tab(srv):
    html = _static_text(srv, "/")
    js = _static_text(srv, "/static/app.js")
    assert 'id="startover-head"' in html
    assert 'id="start-over"' in html
    assert 'id="kept-list"' in html
    assert 'id="kept-dir"' in html
    assert "Start over from the shipped layout" in html
    # asked once, in the page, never window.confirm
    assert "START_OVER_QUESTION" in js
    assert 'confirmInline($("#start-over")' in js
    assert "/api/config/use" in js and "/api/config/kept" in js
    assert "based_on: basedOn()" in js
    # the two-tab refusal offers reload from disk from inside the card
    assert "function reloadConfigFromDisk" in js
    assert "function swapRefused" in js


def test_the_kept_routes_are_not_offered_while_degraded(degraded):
    app, _cfg = degraded("config_unreadable")
    with running(app) as srv:
        status, b = jget(srv, "/api/config/kept")
        assert status == 503 and is_error(b, "refused")
        status, b = post(srv, "/api/config/use", {"file": "x"})
        assert status == 503 and is_error(b, "refused")


def test_the_degraded_repair_still_uses_its_own_name(degraded):
    """Unchanged on purpose: a file this build cannot parse is not a
    configuration anybody can be offered back, so it keeps `.bad-<stamp>` and
    stays out of the kept list."""
    app, _cfg_path = degraded("config_unreadable")
    with running(app) as srv:
        status, b = post(srv, "/api/config/reset", {})
        assert status == 200, b
        assert b["recovered"] is True and ".bad-" in b["moved_to"]
        assert "kept" not in b
        status, kept = jget(srv, "/api/config/kept")
        assert status == 200 and kept["kept"] == []
    assert not KEPT_RE.match(os.path.basename(b["moved_to"]))
