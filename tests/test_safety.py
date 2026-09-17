"""`nms_sorter.safety`: the write sequence, end to end, on a throwaway copy.

Converted from `selftest_legacy.py`, section "apply, end to end, on a copy",
plus the two codec control-byte checks that were parked inside the extractor
block there (they are about what the write path must preserve, not about
extractors).

The apply runs once per module, in `apply_lab`, and the assertions read its
report and the file it left behind. Doing it once is deliberate: the point of
the ten-step sequence is that one run produces a whole report, and an assertion
per step reading that one report is a truer test than ten independent applies.

Nothing here touches a real save: `apply_lab` copies the synthetic fixture into
a pytest temp directory and writes an `mf_` beside it.
"""
import ctypes
import errno
import hashlib
import io
import json
import logging
import os
import shutil
import stat
import subprocess
import time

import pytest

from nms_sorter import codec
from nms_sorter import config as cfgmod
from nms_sorter import planner, safety
from nms_sorter import platform as platformmod
from nms_sorter.codec import dumps, frame_payload, loads
from nms_sorter.savemodel import SaveFile
from tools import make_fixture

STEPS = ("game", "backup", "identity round trip",
         "plan matches what you saw", "encode, decode, compare",
         "nothing else changed", "write", "metadata", "re-read from disk")


# ----------------------------------------- control bytes, at the text boundary
#
# 0x0C inside a procedural id: the game writes a `\f` escape, and writing
# `` instead is a byte difference that blocks every edit on the save. One
# id in the operator's live save carries exactly this byte.

WEIRD = {"a": "^\x80\x80)\x0c\xdc\xac#00000", "b": "\t\r\n\x08\x01"}


def test_a_0x0c_byte_is_written_as_backslash_f():
    raw = dumps(WEIRD)
    assert b"\\f" in raw and b"\\u000C" not in raw, \
        "a 0x0C byte is written as \\f, the way the game writes it: %r" % raw


def test_a_string_of_control_bytes_survives_a_round_trip():
    raw = dumps(WEIRD)
    assert dumps(loads(raw)) == raw, \
        "a string of control bytes survives a round trip byte-for-byte"


# ------------------------------------------------------------- the apply lab


@pytest.fixture(scope="module")
def apply_lab(synthetic_path, tmp_path_factory, no_game_running):
    """One full `apply_plan` on a copy, with a refused attempt in front of it.

    Yields a dict: the plan that was approved, the refusal evidence, the
    report, the result, and the target path afterwards.
    """
    lab = str(tmp_path_factory.mktemp("lab"))
    target = os.path.join(lab, make_fixture.SAVE_NAME)
    shutil.copy2(synthetic_path, target)

    # An mf_ the codec will accept, so step 9 is exercised too.
    payload = SaveFile(target).payload
    with open(os.path.join(lab, "mf_" + make_fixture.SAVE_NAME), "wb") as fh:
        fh.write(make_fixture.build_meta(len(payload), os.path.getsize(target),
                                         make_fixture.SAVE_NAME, "base"))

    cfg = make_fixture.synthetic_config()
    plan, _commit = planner.build_plan(SaveFile(target), cfg)
    before = open(target, "rb").read()
    backups = os.path.join(lab, "backups")

    refused = None
    try:
        safety.apply_plan(target, cfg, "00000000", planner.build_plan, backups)
    except safety.Refused as exc:
        refused = exc
    after_refusal = open(target, "rb").read()

    rep, res = safety.apply_plan(target, cfg, plan.fingerprint,
                                 planner.build_plan, backups)
    return {"lab": lab, "target": target, "plan": plan, "cfg": cfg,
            "before": before, "refused": refused,
            "after_refusal": after_refusal, "rep": rep, "res": res}


# ------------------------------------------------------------- the refusal


def test_a_plan_the_operator_did_not_approve_is_refused(apply_lab):
    assert apply_lab["refused"] is not None, \
        "a plan the operator did not approve is refused"


def test_a_refusal_writes_nothing_at_all(apply_lab):
    assert apply_lab["after_refusal"] == apply_lab["before"], \
        "a refusal writes nothing at all"


# --------------------------------------------------------------- the report


@pytest.mark.parametrize("want", STEPS)
def test_the_step_ran(apply_lab, want):
    labels = [s["label"] for s in apply_lab["rep"].steps]
    assert want in labels, "step ran: %s (got %s)" % (want, labels)


def test_every_step_passed(apply_lab):
    bad = [s for s in apply_lab["rep"].steps if s["state"] == "fail"]
    assert not bad, "every step passed: %s" % bad


def test_the_backup_folder_exists(apply_lab):
    assert os.path.isdir(apply_lab["res"]["backup"]), "the backup folder exists"


def test_the_mf_metadata_is_backed_up_too(apply_lab):
    mf = os.path.join(apply_lab["res"]["backup"], "mf_" + make_fixture.SAVE_NAME)
    assert os.path.exists(mf), "the mf_ metadata is backed up too"


# ----------------------------------------------------- what landed on disk


@pytest.fixture(scope="module")
def applied(apply_lab):
    return SaveFile(apply_lab["target"]).container_map()


def test_chest1_really_holds_250_sodium_after_the_write(applied):
    sodium = [s for s in applied["chest1"].slots() if s.id == "^CATALYST1"]
    assert sodium and sodium[0].amount == 250, \
        ("chest1 really holds 250 Sodium after the write: %s"
         % [s.amount for s in sodium])


def test_the_non_utf8_procedural_id_survived_the_whole_write_path(applied):
    assert make_fixture.PROC_ID in [s.id for s in applied["suit"].slots()], \
        "the non-UTF-8 procedural id survived the whole write path byte for byte"


def test_a_cjk_name_survived_the_whole_write_path(apply_lab):
    assert SaveFile(apply_lab["target"]).freighter_name() == "Fixture基地", \
        "a CJK name survived the whole write path"


def test_the_merged_trade_goods_landed_in_chest3(applied):
    """One stack of 16, not two, and not 11.

    The legacy check accepted either "a stack of exactly 11" or "a total of
    16", which hid which one actually happens. It is the second, and the
    arithmetic is forced: the suit holds 4 + 7 Albumen Pearl and ship0 holds 5,
    both sources are in `synthetic_config`, and trade_goods routes to chest3.
    The in-container merge makes 11, the ship's 5 merges onto it, and chest3
    ends with a single stack of 16. A stack of 11 would mean the ship was never
    drained.
    """
    pearls = [(s.id, s.amount) for s in applied["chest3"].slots()
              if s.id == "^ALBUMENPEARL"]
    assert pearls == [("^ALBUMENPEARL", 16)], \
        ("the merged trade goods landed in chest3 as one stack of 16: %s"
         % [(s.id, s.amount) for s in applied["chest3"].slots()])


def _census(sf):
    """Per-item totals across every sortable container."""
    out = {}
    for c in sf.containers():
        if not c.sortable:
            continue
        for s in c.slots():
            out[s.id] = out.get(s.id, 0) + s.amount
    return out


def test_per_item_totals_across_sortable_containers_are_unchanged(apply_lab):
    backup = os.path.join(apply_lab["res"]["backup"], make_fixture.SAVE_NAME)
    assert _census(SaveFile(backup)) == _census(SaveFile(apply_lab["target"])), \
        "per-item totals across sortable containers are unchanged"


def test_the_written_file_still_round_trips_byte_for_byte(apply_lab):
    rep = safety.Report()
    safety.verify_roundtrip(apply_lab["target"], rep)
    assert all(s["state"] == "ok" for s in rep.steps), \
        "the written file still round-trips byte-for-byte: %s" % rep.steps


# ===================================================== defect register P1-3


@pytest.fixture
def write_lab(synthetic_path, tmp_path):
    """A save, its mf_ and a backup root in a temp directory, plus the plan and
    the fingerprint an apply would need.

    Nothing is applied here: every test in this section drives `apply_plan`
    itself, because what is under test is what happens *instead* of a write.
    """
    target = str(tmp_path / make_fixture.SAVE_NAME)
    shutil.copy2(synthetic_path, target)
    payload = SaveFile(target).payload
    with open(str(tmp_path / ("mf_" + make_fixture.SAVE_NAME)), "wb") as fh:
        fh.write(make_fixture.build_meta(len(payload), os.path.getsize(target),
                                         make_fixture.SAVE_NAME, "base"))
    cfg = make_fixture.synthetic_config()
    plan, _commit = planner.build_plan(SaveFile(target), cfg)
    return {"target": target, "cfg": cfg, "plan": plan,
            "backups": str(tmp_path / "backups"),
            "before": open(target, "rb").read()}


def _apply(lab, fingerprint=None, **kw):
    return safety.apply_plan(lab["target"], lab["cfg"],
                             fingerprint or lab["plan"].fingerprint,
                             planner.build_plan, lab["backups"], **kw)


def _unchanged(lab):
    return open(lab["target"], "rb").read() == lab["before"]


# --- D9: the lock, the pid in the temp name, the cleanup -------------------

#: a pid nothing has, and one every platform this suite runs on can prove is
#: gone. `platform.pid_alive` branches on *capability* rather than on the
#: platform name, so under `NMS_SORTER_FAKE_PLATFORM=linux` on a Windows host
#: it still reads the answer from `OpenProcess` and still answers False here
#: (lane C, 218d07c). The tests below used to open with a skip for the case
#: where it could not; that case no longer exists and the skip hid two of the
#: stale-lock branches whenever the fake platform was in use.
DEAD_PID = 999999999


def test_the_lock_sits_beside_the_save_it_guards(write_lab):
    assert safety.lock_path(write_lab["target"]) == \
        write_lab["target"] + ".nms-sorter.lock", \
        ("one lock per save file, beside it: %s"
         % safety.lock_path(write_lab["target"]))


def test_a_live_lock_refuses_and_writes_nothing(write_lab, no_game_running):
    """D9. Two applies to one save used to collide on a fixed temp name."""
    lock = safety.lock_path(write_lab["target"])
    with open(lock, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "created": "2026-09-14T00:00:00Z"}, fh)
    try:
        with pytest.raises(safety.Refused) as exc:
            _apply(write_lab)
        assert "another sorter instance is writing this save" in str(exc.value), \
            "the refusal says what is happening: %s" % exc.value
        assert str(os.getpid()) in str(exc.value), \
            "and names the pid holding it: %s" % exc.value
        assert _unchanged(write_lab), "a refused apply writes nothing"
    finally:
        os.unlink(lock)


def test_a_stale_lock_is_removed_and_the_apply_proceeds(write_lab,
                                                        no_game_running,
                                                        caplog):
    """Dead pid and older than ten minutes: whoever took it is gone."""
    lock = safety.lock_path(write_lab["target"])
    with open(lock, "w", encoding="utf-8") as fh:
        json.dump({"pid": DEAD_PID, "created": "2020-01-01T00:00:00Z"}, fh)
    old = time.time() - (safety.STALE_LOCK_SECONDS + 60)
    os.utime(lock, (old, old))
    with caplog.at_level(logging.INFO, logger="nms_sorter.safety"):
        _rep, res = _apply(write_lab)
    assert res["rows"], "the apply ran"
    said = [r.getMessage() for r in caplog.records]
    assert any("stale lock" in m for m in said), \
        "the removal is on the record: %s" % said
    assert not os.path.exists(lock), "and the lock is gone afterwards"


def test_the_lock_is_released_when_the_apply_finishes(write_lab, no_game_running):
    _apply(write_lab)
    assert not os.path.exists(safety.lock_path(write_lab["target"])), \
        "the lock does not outlive the apply"


def test_the_lock_is_released_when_a_step_refuses(write_lab, no_game_running):
    with pytest.raises(safety.Refused):
        _apply(write_lab, fingerprint="00000000")
    assert not os.path.exists(safety.lock_path(write_lab["target"])), \
        "a refusal releases it too, or the next run is blocked forever"


def test_the_temp_file_carries_the_pid(write_lab):
    assert safety.temp_path(write_lab["target"]).endswith(
        ".nms-sorter-%d.tmp" % os.getpid()), \
        ("the temp name carries the pid: %s"
         % safety.temp_path(write_lab["target"]))


def test_a_failed_write_leaves_no_temp_file_and_no_damage(write_lab,
                                                          no_game_running,
                                                          monkeypatch):
    """D9. There was no `try/finally`: a failure between the temp write and the
    replace left a stray temp file in the operator's save folder.

    The failure arrives as a `Refused` rather than as the bare `OSError` it
    used to be (review one, R7): the sentence names the file and says nothing
    was changed, which is the difference between a refusal and an HTTP 500
    with a traceback in it.
    """
    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(safety.os, "replace", boom)
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert write_lab["target"] in str(exc.value) and "disk full" in str(exc.value), \
        "the refusal names the file and the reason: %s" % exc.value
    assert _unchanged(write_lab), "the original is intact"
    strays = [f for f in os.listdir(os.path.dirname(write_lab["target"]))
              if "tmp" in f.lower()]
    assert not strays, "no temp file is left behind: %s" % strays


# --- D10: a diff too large to enumerate is a refusal -----------------------


def test_a_diff_larger_than_the_guard_can_enumerate_refuses(write_lab,
                                                            no_game_running,
                                                            monkeypatch):
    """D10. The step-7 diff was truncated at 100,000 paths and truncation was
    not itself a refusal, so past that point the guard stopped guarding."""
    monkeypatch.setattr(safety, "DIFF_LIMIT", 1)
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert ("the change set is larger than the guard can enumerate; refusing "
            "rather than writing blind") in str(exc.value), \
        "the refusal is explicit: %s" % exc.value
    assert _unchanged(write_lab), "and nothing is written"


# --- D11: the metadata has to be one this build can update -----------------


def _meta_lab(fixture_variant, tmp_path, variant):
    """A copy of a variant's save and its mf_, ready to be applied to."""
    fx = fixture_variant(variant)
    target = str(tmp_path / make_fixture.SAVE_NAME)
    shutil.copy2(fx["save"], target)
    shutil.copy2(fx["meta"], str(tmp_path / ("mf_" + make_fixture.SAVE_NAME)))
    cfg = make_fixture.synthetic_config()
    plan, _ = planner.build_plan(SaveFile(target), cfg)
    return {"target": target, "cfg": cfg, "plan": plan,
            "backups": str(tmp_path / "backups"),
            "before": open(target, "rb").read()}


def test_a_metadata_format_this_build_does_not_know_refuses(fixture_variant,
                                                            tmp_path,
                                                            no_game_running):
    """D11. The field offsets were decoded for format 2004; 2003 is 384 bytes
    long and lays them out differently."""
    lab = _meta_lab(fixture_variant, tmp_path, "mf-format-2003")
    with pytest.raises(safety.Refused) as exc:
        _apply(lab)
    assert "this metadata format (2003) is not one this build knows how to " \
           "update" in str(exc.value), "the refusal names the format: %s" % exc.value
    assert _unchanged(lab), "the save is untouched"


def test_a_metadata_integrity_hash_refuses(fixture_variant, tmp_path,
                                           no_game_running):
    """D11. "The mf_ carries no integrity hash" was asserted in the self-test
    and nowhere in the write path. A build that fills those fields would have
    had a stale hash written over it."""
    lab = _meta_lab(fixture_variant, tmp_path, "mf-hash-set")
    with pytest.raises(safety.Refused) as exc:
        _apply(lab)
    assert ("this save's metadata carries an integrity hash this build cannot "
            "recompute; refusing to write a stale one") in str(exc.value), \
        "the refusal explains itself: %s" % exc.value
    assert _unchanged(lab), "the save is untouched"


def test_the_metadata_check_runs_before_the_save_is_written(fixture_variant,
                                                            tmp_path,
                                                            no_game_running):
    """Order matters: step 8 writes the save and step 9 rewrites the mf_. A
    metadata refusal raised after step 8 would leave a mismatched pair on
    disk -- a save whose sizes its own metadata denies."""
    lab = _meta_lab(fixture_variant, tmp_path, "mf-hash-set")
    meta = os.path.join(os.path.dirname(lab["target"]),
                        "mf_" + make_fixture.SAVE_NAME)
    meta_before = open(meta, "rb").read()
    with pytest.raises(safety.Refused):
        _apply(lab)
    assert _unchanged(lab), "the save bytes are the ones we started with"
    assert open(meta, "rb").read() == meta_before, \
        "and so are the metadata bytes: neither half of the pair moved"


def test_a_good_metadata_file_passes_the_new_step(write_lab, no_game_running):
    rep, _res = _apply(write_lab)
    steps = dict((s["label"], s) for s in rep.steps)
    assert "metadata is updatable" in steps, \
        "the check is a step of its own: %s" % [s["label"] for s in rep.steps]
    assert steps["metadata is updatable"]["state"] == "ok"
    labels = [s["label"] for s in rep.steps]
    assert labels.index("metadata is updatable") < labels.index("write"), \
        "and it runs before the write: %s" % labels


# --- 3.7: the backup manifest ---------------------------------------------


@pytest.fixture
def applied_lab(write_lab, no_game_running):
    rep, res = _apply(write_lab)
    return dict(write_lab, rep=rep, res=res)


def test_the_backup_folder_carries_a_manifest(applied_lab):
    path = os.path.join(applied_lab["res"]["backup"], "manifest.json")
    assert os.path.exists(path), "manifest.json is written after step 10"
    with open(path, encoding="utf-8") as fh:
        man = json.load(fh)
    assert man["save"]["file"] == make_fixture.SAVE_NAME
    assert len(man["save"]["sha256"]) == 64 and man["save"]["size"] > 0
    assert man["meta"] and man["meta"]["file"] == "mf_" + make_fixture.SAVE_NAME
    assert man["created"].endswith("Z"), "created is UTC: %r" % man["created"]
    assert man["app_version"], "the app version that wrote it"


def test_the_manifest_pins_the_plan_and_the_config(applied_lab):
    man = safety.read_manifest(applied_lab["res"]["backup"])
    assert man["plan"]["fingerprint"] == applied_lab["plan"].fingerprint
    assert man["plan"]["fingerprint_full"] == applied_lab["plan"].fingerprint_full
    assert man["plan"]["rows"] == len(applied_lab["plan"].rows)
    assert man["plan"]["containers"] == applied_lab["res"]["containers"]
    assert man["config_sha256"] == cfgmod.config_hash(applied_lab["cfg"]), \
        "the config that produced the plan is hashed into the manifest"


def test_the_manifest_hash_is_of_the_backup_not_of_the_new_file(applied_lab):
    man = safety.read_manifest(applied_lab["res"]["backup"])
    backup_save = os.path.join(applied_lab["res"]["backup"],
                               make_fixture.SAVE_NAME)
    assert man["save"]["sha256"] == safety.sha256(backup_save), \
        "the manifest describes the copy it sits next to"
    assert man["save"]["sha256"] != safety.sha256(applied_lab["target"]), \
        "which is not the file that was just written"


def test_list_backups_verifies_each_folder(applied_lab):
    rows = safety.list_backups(applied_lab["backups"])
    assert len(rows) == 1, "one backup, one row: %s" % rows
    row = rows[0]
    assert row["has_manifest"] and row["sha256_ok"] is True, \
        "the hash in the manifest matches the file beside it: %s" % row
    assert row["save"] == make_fixture.SAVE_NAME and row["size"] > 0
    assert row["folder"] == applied_lab["res"]["backup"]
    assert row["stamp"], "the folder's timestamp is parsed out: %r" % row["stamp"]


def test_list_backups_reports_a_folder_with_no_manifest(applied_lab):
    """An unknown folder is left alone rather than pruned or trusted."""
    stray = os.path.join(applied_lab["backups"], "20200101-000000-save9")
    os.makedirs(stray)
    rows = dict((r["folder"], r) for r in safety.list_backups(applied_lab["backups"]))
    assert rows[stray]["has_manifest"] is False \
        and rows[stray]["sha256_ok"] is None, \
        "no manifest is not the same as a bad hash: %s" % rows[stray]


def test_list_backups_notices_a_corrupted_backup(applied_lab):
    backup_save = os.path.join(applied_lab["res"]["backup"],
                               make_fixture.SAVE_NAME)
    with open(backup_save, "r+b") as fh:
        fh.seek(0)
        fh.write(b"\x00")
    rows = safety.list_backups(applied_lab["backups"])
    assert rows[0]["sha256_ok"] is False, \
        "a backup whose bytes moved is reported, not silently restorable"


def test_list_backups_of_a_folder_that_does_not_exist_is_empty(tmp_path):
    assert safety.list_backups(str(tmp_path / "nope")) == []


# --- 3.9: which fingerprint is compared ------------------------------------


def test_the_full_fingerprint_is_accepted(write_lab, no_game_running):
    _rep, res = _apply(write_lab, fingerprint=write_lab["plan"].fingerprint_full)
    assert res["fingerprint"] == write_lab["plan"].fingerprint, \
        "the short form stays in the result, for the page"
    assert res["fingerprint_full"] == write_lab["plan"].fingerprint_full, \
        "and the full digest is there too, for the manifest and the log"


def test_a_wrong_full_fingerprint_is_refused(write_lab, no_game_running):
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab, fingerprint="0" * 64)
    assert "plan matches what you saw" in str(exc.value)
    assert _unchanged(write_lab), "and nothing is written"


def test_the_eight_character_prefix_is_still_accepted(write_lab,
                                                      no_game_running):
    """The server mints and passes the short form; the UI shows it. Both have
    to keep working until the client is switched over."""
    _rep, res = _apply(write_lab, fingerprint=write_lab["plan"].fingerprint)
    assert res["rows"], "the short form still applies"


# --- step 1 off Windows -----------------------------------------------------


def test_a_process_check_that_cannot_run_refuses_and_writes_nothing(
        write_lab, monkeypatch):
    """A check that cannot run is a refusal. Off Windows there is no
    `ctypes.windll` and no `tasklist`, so this is the branch Ubuntu takes and
    the reason apply is Windows-only."""
    monkeypatch.setattr(safety, "game_status", lambda: {
        "running": None, "pids": [], "method": "failed", "known": False,
        "process": "NMS.exe", "error": "no windll / no tasklist"})
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert "could not read the process list" in str(exc.value), \
        "the refusal says the check could not run: %s" % exc.value
    assert _unchanged(write_lab), "and nothing is written"


def test_an_unrunnable_check_says_what_to_do_about_it(write_lab, monkeypatch):
    """The Windows half of the unknown case: the error is quoted and the
    sentence says what is wrong on the machine rather than telling somebody to
    quit a game that may not even be running."""
    monkeypatch.setattr(safety, "game_status", lambda: {
        "running": None, "pids": [], "method": "failed", "known": False,
        "process": "NMS.exe", "error": "no windll / no tasklist"})
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab, at_main_menu=True)
    assert "no windll / no tasklist" in str(exc.value), \
        "the reason is quoted: %s" % exc.value
    assert "blocking the process list" in str(exc.value), \
        "and the sentence says what to do about it: %s" % exc.value


# ===========================================================================
# P1-8 review one, section 4: the refusal sentences nothing reached
# ===========================================================================
#
# Sixteen of the twenty-three refusal sites in `safety.py` were never reached by
# the suite, measured by wrapping `Report.fail`. Seven of those are "the codec
# broke" sentences, reachable only by making `dumps`, `frame_payload`,
# `meta_encode` or `meta_read` lie -- which is a legitimate test and the only
# way anyone will ever know those branches work. The rest are producible from a
# fixture.
#
# Each test below names the sentence it reaches. The point is not the assertion;
# it is that the branch executes at all, and that what comes out is a sentence
# rather than a traceback.


def _plan_for(target, cfg=None):
    """(cfg, full fingerprint) for a save, the way an apply wants them."""
    cfg = cfg or make_fixture.synthetic_config()
    plan, _commit = planner.build_plan(SaveFile(target), cfg)
    return cfg, plan.fingerprint_full


def _lab_bytes(lab):
    return open(lab["target"], "rb").read()


def _only_backup_folder(lab):
    folders = [os.path.join(lab["backups"], f)
               for f in sorted(os.listdir(lab["backups"]))]
    assert len(folders) == 1, "one apply, one backup folder: %s" % folders
    return folders[0]


# --- step 1: the refusal the whole design exists for ------------------------


def test_a_running_game_with_no_confirmation_names_the_tick(write_lab,
                                                            monkeypatch):
    """A running game on its own is not a refusal any more; a running game
    nobody has placed on the main menu is, and the refusal has to name the box
    that turns it into an apply."""
    monkeypatch.setattr(safety, "GAME_PROCESS", "NMS.exe")
    monkeypatch.setattr(safety, "game_status", lambda: {
        "running": True, "pids": [4242], "method": "test", "known": True,
        "process": "NMS.exe"})
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert "NMS.exe is running as pid 4242" in str(exc.value), \
        "the refusal names the process and the pid: %s" % exc.value
    assert "I am at the main menu, not in a loaded save" in str(exc.value), \
        "and names the tick, verbatim: %s" % exc.value
    assert _unchanged(write_lab), "and nothing is written"
    assert not os.path.isdir(write_lab["backups"]), \
        "the refusal comes before the backup, so there is not even a folder"


def test_a_running_game_applies_when_the_main_menu_is_confirmed(
        write_lab, monkeypatch):
    """The primary flow: save, quit to the menu, sort. The step is an `info`
    that says which of the two runs this was, and the write goes ahead."""
    monkeypatch.setattr(safety, "GAME_PROCESS", "NMS.exe")
    monkeypatch.setattr(safety, "game_status", lambda: {
        "running": True, "pids": [4242], "method": "test", "known": True,
        "process": "NMS.exe"})
    rep, res = _apply(write_lab, at_main_menu=True)
    step = [s for s in rep.steps if s["label"] == "game"][0]
    assert step["state"] == "info", step
    assert step["detail"] == ("running as pid 4242: at the main menu, as "
                             "confirmed"), step["detail"]
    assert res["rows"], "and the apply ran"
    assert res["manifest"]["game"] == ("game running: at the main menu, as "
                                       "confirmed"), \
        "and the manifest records which kind of run it was"
    assert "applied" not in res["manifest"]["game"], \
        ("review 5, finding 2: the same note is written to the in-progress "
         "and the refusal manifest, so it may not claim an outcome")


def test_a_closed_game_is_recorded_in_the_manifest_too(write_lab,
                                                       no_game_running):
    """The other half of the same record: a run made with the game closed
    says so, rather than leaving the field absent and unreadable."""
    rep, res = _apply(write_lab)
    step = [s for s in rep.steps if s["label"] == "game"][0]
    assert step["state"] == "ok", step
    assert res["manifest"]["game"] == "game closed", res["manifest"]


#: `CreateFileW` with `GENERIC_READ` and `FILE_SHARE_READ`: other readers are
#: let in and everything else is denied, which is what a process with the file
#: open for its own use looks like from outside. Steps 3 to 8 read the save and
#: still work; step 9's rename is refused with `[WinError 5] Access is denied`,
#: measured rather than assumed.
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80


def _hold_open_against_rename(path):
    """A Windows handle that denies the rename, or None. Windows only."""
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = ctypes.c_void_p
    handle = k32.CreateFileW(ctypes.c_wchar_p(path), _GENERIC_READ,
                             _FILE_SHARE_READ, None, _OPEN_EXISTING,
                             _FILE_ATTRIBUTE_NORMAL, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        return None
    return handle


def test_a_save_something_else_holds_open_names_the_game(
        write_lab, no_game_running, monkeypatch):
    """The failure a loaded game produces at step 9, and the sentence for it.

    On Windows the file really is held open with a share mode that denies the
    rename. Off Windows there is no share mode to deny, so the same
    `PermissionError` comes out of `os.replace` directly; the sentence is
    chosen from the exception and from whether the file is writable at all,
    and both are the same either way. No mtime guessing is involved in
    reaching it.
    """
    handle = None
    if os.name == "nt":
        handle = _hold_open_against_rename(write_lab["target"])
        assert handle, "the save could not be opened for this test"
    else:
        def denied(*_a, **_kw):
            raise PermissionError(errno.EACCES, "Permission denied")

        monkeypatch.setattr(safety.os, "replace", denied)
    try:
        with pytest.raises(safety.Refused) as exc:
            _apply(write_lab)
    finally:
        if handle:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
    assert "the game has the save open" in str(exc.value), exc.value
    assert "go to the main menu, or close the game, and try again" \
        in str(exc.value), exc.value
    assert _unchanged(write_lab), "and the original bytes are still on disk"


def test_a_save_the_operator_cannot_write_is_not_blamed_on_the_game(
        write_lab, no_game_running):
    """The other half of the same branch. A read-only attribute is the same
    errno and a different problem, so it keeps the sentence that names the
    folder: telling somebody to quit a game they have not started is worse
    than saying nothing."""
    os.chmod(write_lab["target"], stat.S_IREAD)
    try:
        try:
            with open(write_lab["target"], "ab"):
                pass
        except OSError:
            pass
        else:
            pytest.skip("this filesystem does not enforce the read-only bit")
        with pytest.raises(safety.Refused) as exc:
            _apply(write_lab)
        assert "the save folder refused the write" in str(exc.value), exc.value
        assert "the game has the save open" not in str(exc.value), exc.value
    finally:
        os.chmod(write_lab["target"], stat.S_IWRITE)


def test_the_confirmation_does_not_cover_an_unreadable_process_list(
        write_lab, monkeypatch):
    """The tick says *where* the player is, not whether the game is up, so it
    cannot stand in for an answer that could not be got at all. Off Windows
    there is no process list and no error string, so the sentence is the
    platform one rather than "could not read the process list (None)"."""
    monkeypatch.setattr(safety, "game_status", lambda: {
        "running": None, "pids": [], "method": "unsupported: linux",
        "known": False, "process": "NMS.exe"})
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab, at_main_menu=True)
    assert str(exc.value) == ("game: %s" % platformmod.UNSUPPORTED_REFUSAL), \
        "one wording, from `platform.refusal_sentence`: %s" % exc.value
    assert _unchanged(write_lab), "and nothing is written"


# --- step 2: the backup's own two refusals ----------------------------------


def test_a_save_that_is_not_there_is_named(tmp_path):
    """Review line 409: **"backup: ... does not exist"**. It was reachable only
    through R4's `endswith` defect, and then it named the wrong file."""
    r = safety.Report()
    missing = str(tmp_path / "save9.hg")
    with pytest.raises(safety.Refused) as exc:
        safety.backup(missing, str(tmp_path / "backups"), r)
    assert "%s does not exist" % missing in str(exc.value), \
        "the refusal names the file it looked for: %s" % exc.value


def test_a_backup_copy_that_does_not_match_is_refused(write_lab,
                                                      no_game_running,
                                                      monkeypatch):
    """Review line 416: **"backup: copy of ... does not match the original"**.
    Step 2 hashes both ends, and everything downstream works from the copy, so
    a copy that is not the original is the one thing that must never be trusted.
    """
    real_copy = shutil.copy2

    def bad_copy(src, dst, *a, **kw):
        real_copy(src, dst, *a, **kw)
        with open(dst, "r+b") as fh:      # one byte, after the hash source
            fh.seek(0)
            fh.write(b"\x00")

    monkeypatch.setattr(safety.shutil, "copy2", bad_copy)
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert "copy of save9.hg does not match the original" in str(exc.value), \
        "the refusal names the file: %s" % exc.value
    assert _unchanged(write_lab), "and nothing was written"


# --- step 4: the codec-broke sentences --------------------------------------


def test_a_save_that_does_not_re_serialise_is_refused(write_lab,
                                                      no_game_running,
                                                      monkeypatch):
    """Review line 375: **"identity round trip: this save does not re-serialise
    byte-for-byte"**. Step 4 is the step that makes every other step safe, and
    the only way to see it work is to make `dumps` lie."""
    real = safety.dumps

    def liar(doc, **kw):
        return real(doc, **kw) + b" "

    monkeypatch.setattr(safety, "dumps", liar)
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert "does not re-serialise byte-for-byte" in str(exc.value)
    assert "first difference at offset" in str(exc.value), \
        "and says where: %s" % exc.value
    assert _unchanged(write_lab)


def test_a_payload_that_does_not_reframe_is_refused(write_lab,
                                                    no_game_running,
                                                    monkeypatch):
    """Review line 385: **"container round trip: reframing the payload does not
    decode back"**."""
    real = safety.frame_payload

    def liar(payload, **kw):
        return real(payload + b" ", **kw)

    monkeypatch.setattr(safety, "frame_payload", liar)
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert "reframing the payload does not decode back" in str(exc.value)
    assert _unchanged(write_lab)


# --- step 6: the transformed document has to survive its own serialisation --


def _lying_loads(monkeypatch, original_payload, on_call=1):
    """Make `safety.loads` inject a key the `n`th time it sees a payload that
    is *not* the file's original.

    The seam has to be this precise: step 4 loads the original twice and must
    get the truth, step 6 loads the new payload once (that is `on_call=1`) and
    step 11 loads it again (`on_call=2`). Counting per-payload is what lets one
    patch reach either of the two "the codec broke" sentences downstream of it.
    """
    real = safety.loads
    seen = {}

    def spy(payload):
        doc = real(payload)
        if payload == original_payload:
            return doc
        seen[payload] = seen.get(payload, 0) + 1
        if seen[payload] == on_call and isinstance(doc, dict):
            doc = dict(doc)
            doc["ZZ_injected_by_the_test"] = 1
        return doc

    monkeypatch.setattr(safety, "loads", spy)


def test_a_transform_that_does_not_survive_serialisation_is_refused(
        write_lab, no_game_running, monkeypatch):
    """Review line 712: **"encode, decode, compare: ... does not survive its own
    serialisation"**."""
    _lying_loads(monkeypatch, SaveFile(write_lab["target"]).payload, on_call=1)
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert "does not survive its own serialisation" in str(exc.value)
    assert "ZZ_injected_by_the_test" in str(exc.value), \
        "and names the path it differs at: %s" % exc.value
    assert _unchanged(write_lab)


def test_a_framed_payload_that_does_not_decode_back_is_refused(
        write_lab, no_game_running, monkeypatch):
    """Review line 718: **"encode, decode, compare: the framed file does not
    decode back to the payload we built"**. `frame_payload` lies only for the
    post-transform payload, so step 4 still passes and step 6 is the step that
    catches it."""
    original = SaveFile(write_lab["target"]).payload
    real = safety.frame_payload

    def liar(payload, **kw):
        if payload != original:
            return real(payload + b" ", **kw)
        return real(payload, **kw)

    monkeypatch.setattr(safety, "frame_payload", liar)
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert "the framed file does not decode back" in str(exc.value)
    assert _unchanged(write_lab)


# --- step 7: the two guards on "nothing else changed" -----------------------


def _plan_builder_that_also(extra_keys=(), mutate=None):
    """`build_plan`, with a `commit()` that claims extra containers or changes
    something outside the ones it claims."""
    def builder(save, cfg):
        plan, commit = planner.build_plan(save, cfg)

        def wrapped():
            touched = commit()
            if mutate is not None:
                mutate(save)
            return list(touched) + list(extra_keys)

        return plan, wrapped
    return builder


def test_a_container_the_model_cannot_path_is_refused(write_lab,
                                                      no_game_running):
    """Review line 737: **"nothing else changed: ... not a container this build
    can name a path for"**. The reviewer could not reach it; a `commit()` that
    names a key the container map does not have does."""
    with pytest.raises(safety.Refused) as exc:
        safety.apply_plan(write_lab["target"], write_lab["cfg"],
                          write_lab["plan"].fingerprint_full,
                          _plan_builder_that_also(extra_keys=["chest99"]),
                          write_lab["backups"])
    assert "the plan touched 'chest99'" in str(exc.value), \
        "the refusal names the key: %s" % exc.value
    assert "Refusing rather than writing blind" in str(exc.value)
    assert _unchanged(write_lab)


def test_a_change_outside_the_named_containers_is_refused(write_lab,
                                                          no_game_running):
    """Review line 757: **"nothing else changed: N path(s) outside the
    containers this plan names differ"**. A `commit()` that edits `Units` --
    which is not in any container -- is the smallest possible version of the
    thing this guard exists for."""
    def mutate(save):
        save.d.set(save.d.player, "Units", 999999)

    with pytest.raises(safety.Refused) as exc:
        safety.apply_plan(write_lab["target"], write_lab["cfg"],
                          write_lab["plan"].fingerprint_full,
                          _plan_builder_that_also(mutate=mutate),
                          write_lab["backups"])
    assert "path(s) outside the containers this plan names differ" in str(exc.value)
    assert _unchanged(write_lab), "and the save is untouched"


# --- step 8 and step 10: the metadata sentences -----------------------------


def test_metadata_that_will_not_decode_is_refused(write_lab, no_game_running):
    """Review line 465: **"metadata is updatable: this save's metadata could not
    be decoded"**. A truncated `mf_` is the real-world version: a sync client or
    a full disk leaves one."""
    meta = os.path.join(os.path.dirname(write_lab["target"]), "mf_save9.hg")
    with open(meta, "wb") as fh:
        fh.write(b"\x00" * 16)
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert "this save's metadata could not be decoded" in str(exc.value)
    assert "refusing rather than leaving the pair disagreeing" in str(exc.value)
    assert _unchanged(write_lab), "and the save is untouched"


def test_metadata_that_will_not_decode_because_the_reader_raised_is_refused(
        write_lab, no_game_running, monkeypatch):
    """The same sentence from the other direction: `meta_read` itself raising.
    A future metadata layout is a thing that happens after a game patch, and it
    must be a sentence rather than a traceback."""
    def boom(_path):
        raise ValueError("format 2005 is not one this build has seen")

    monkeypatch.setattr(safety, "meta_read", boom)
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert "format 2005" in str(exc.value), \
        "the reader's own words are carried into the sentence: %s" % exc.value
    assert _unchanged(write_lab)


def test_a_metadata_patch_that_moved_another_byte_is_refused(write_lab,
                                                             no_game_running,
                                                             monkeypatch):
    """Review line 498 and **R12**: the guard used to be an assertion over the
    two `pack_into` calls this function had just made, so it could not fire.
    Now the written file is decrypted and compared against the plaintext as it
    was, and a `meta_encode` that scribbles at `0x100` reaches the sentence."""
    real = safety.meta_encode

    def scribble(plain, slot):
        bad = bytearray(plain)
        bad[0x100] ^= 0xFF
        return real(bytes(bad), slot)

    monkeypatch.setattr(safety, "meta_encode", scribble)
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert "the size patch would touch a byte outside 0x38..0x3F" in str(exc.value)


def test_metadata_that_does_not_decrypt_back_is_refused(write_lab,
                                                        no_game_running,
                                                        monkeypatch):
    """Review line 503: **"metadata: the rewritten mf_ does not decrypt back to
    what we wrote"**. The scribble is *inside* `0x38..0x40` this time, so the
    byte-range check passes and this is the sentence left to catch it."""
    real = safety.meta_encode

    def scribble(plain, slot):
        bad = bytearray(plain)
        bad[0x3E] ^= 0xFF
        return real(bytes(bad), slot)

    monkeypatch.setattr(safety, "meta_encode", scribble)
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert "does not decrypt back to what we wrote" in str(exc.value)


# --- step 11: the four re-read sentences ------------------------------------


def test_a_written_file_that_decodes_to_something_else_is_refused(
        write_lab, no_game_running, monkeypatch):
    """Review line 783: **"re-read from disk: the file on disk does not decode
    to what we wrote"**. Something puts the old bytes back between the write and
    the re-read -- a sync client, a restore racing an apply -- and step 11 is
    the only thing that would ever notice."""
    real = safety.update_metadata
    real_backup = safety.backup
    backup_copy = []

    def spy(meta_path, a, b, report):
        out = real(meta_path, a, b, report)
        shutil.copy2(backup_copy[0], write_lab["target"])
        return out

    def remember(save_path, backup_root, report):
        folder, copied = real_backup(save_path, backup_root, report)
        backup_copy.append(os.path.join(folder, os.path.basename(save_path)))
        return folder, copied

    monkeypatch.setattr(safety, "update_metadata", spy)
    monkeypatch.setattr(safety, "backup", remember)
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert "the file on disk does not decode to what we wrote" in str(exc.value)


def test_a_written_file_that_decodes_differently_is_refused(
        write_lab, no_game_running, monkeypatch):
    """Review line 787: **"re-read from disk: re-decoding the written file
    differs at ..."**. Byte-identical and decoding to a different document is
    the shape of a decoder bug, and it is the one thing step 11 checks that
    step 6 cannot."""
    _lying_loads(monkeypatch, SaveFile(write_lab["target"]).payload, on_call=2)
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert "re-decoding the written file differs at" in str(exc.value)
    assert "ZZ_injected_by_the_test" in str(exc.value)


def test_a_metadata_size_disk_that_does_not_match_is_refused(write_lab,
                                                            no_game_running,
                                                            monkeypatch):
    """Review line 791: **"re-read from disk: mf_ size_disk ... does not match
    the file's ..."**. This is the pair-disagrees check, and the save is already
    written when it fires -- which is why the sentence exists at all."""
    real = safety.update_metadata

    def off_by_one(meta_path, size_decompressed, size_disk, report):
        return real(meta_path, size_decompressed, size_disk + 1, report)

    monkeypatch.setattr(safety, "update_metadata", off_by_one)
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert "mf_ size_disk" in str(exc.value) and "does not match" in str(exc.value)


def test_a_metadata_size_decompressed_that_does_not_match_is_refused(
        write_lab, no_game_running, monkeypatch):
    """Review line 794: **"re-read from disk: mf_ size_decompressed ... does not
    match the payload's ..."**."""
    real = safety.update_metadata

    def off_by_one(meta_path, size_decompressed, size_disk, report):
        return real(meta_path, size_decompressed + 1, size_disk, report)

    monkeypatch.setattr(safety, "update_metadata", off_by_one)
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert "mf_ size_decompressed" in str(exc.value)


# ===========================================================================
# R10 (safety half): the full digest is compared when a full digest arrives
# ===========================================================================


def test_a_full_digest_whose_prefix_matches_is_still_refused(write_lab,
                                                            no_game_running):
    """The server's half of R10 is to forward the digest it minted. This is the
    half that has to be worth forwarding: a 64-character token whose first 8
    characters are right and whose remaining 56 are not must refuse, or the
    gate is 32 bits wide whatever the client sends."""
    real = write_lab["plan"].fingerprint_full
    forged = real[:8] + ("0" * 56 if real[8:] != "0" * 56 else "1" * 56)
    assert len(forged) == 64 and forged != real
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab, fingerprint=forged)
    assert "plan matches what you saw" in str(exc.value)
    assert _unchanged(write_lab)


# ===========================================================================
# R16: the step-7 container map is built before commit()
# ===========================================================================


def test_the_step_seven_map_is_built_before_the_transform_runs(
        write_lab, no_game_running, monkeypatch):
    """The guard's idea of which containers can be named must not depend on
    state the transform may have changed. `extractor_containers()` returns `[]`
    on a core/room mismatch, so a commit that drained the `^MAINT_HOOVER` slot
    would have made a container the plan openly named unnameable (review one,
    R16). Unreachable through the planner today, which is a fact about the
    planner and not about the guard."""
    order = []
    real_map = SaveFile.container_map

    def spy_map(self):
        order.append("container_map")
        return real_map(self)

    monkeypatch.setattr(SaveFile, "container_map", spy_map)

    def builder(save, cfg):
        plan, commit = planner.build_plan(save, cfg)

        def wrapped():
            order.append("commit")
            return commit()

        return plan, wrapped

    _rep, res = safety.apply_plan(write_lab["target"], write_lab["cfg"],
                                  write_lab["plan"].fingerprint_full, builder,
                                  write_lab["backups"])
    assert res["rows"], "the apply ran"
    assert "commit" in order, "and commit() was reached"
    # Not `index(...) < index("commit")`: `build_plan` calls
    # `container_map()` itself, before the transform, so the *first*
    # call is pre-commit whatever step 7 does. What matters is that no
    # call happens after it.
    after = order[order.index("commit"):]
    assert "container_map" not in after, \
        ("nothing asks the post-commit document which containers it has: %s"
         % order)


# ===========================================================================
# R13: the two unguarded writers in `codec` have no caller in the package
# ===========================================================================


def test_nothing_in_the_package_writes_a_save_except_the_write_path():
    """`codec.save` writes a whole save with none of the sequence -- no backup,
    no round trip, no fsync, no replace -- and `codec.meta_update_sizes`
    rewrites an `mf_` in place with no format check and no hash check. They are
    the shape of the mistake step 8 exists to prevent, sitting in the module the
    write path imports (review one, R13).

    Deleting them is lane A's call; this is the guard that says nobody in the
    package may call them. The write path's own writers are
    `safety.write_atomic` and `safety.update_metadata`, and `safety.py` is the
    only file allowed to hold either.
    """
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "nms_sorter")
    banned = ("meta_update_sizes", "codec.save(")
    offenders = {}
    for fn in sorted(os.listdir(root)):
        if not fn.endswith(".py") or fn == "codec.py":
            continue
        with io.open(os.path.join(root, fn), encoding="utf-8") as fh:
            body = fh.read()
        hits = [b for b in banned if b in body]
        if hits:
            offenders[fn] = hits
    assert offenders == {}, \
        ("only `codec.py` may mention its own unguarded writers; found %s"
         % offenders)


# ===========================================================================
# R18: a killed write leaves a temp file, and the next one sweeps it
# ===========================================================================


def test_a_stale_temp_file_is_swept_at_the_start_of_an_apply(write_lab,
                                                             no_game_running,
                                                             caplog):
    """`SIGKILL` runs no `finally`, so `save9.hg.nms-sorter-<pid>.tmp` stays in
    the operator's save folder: correctly invisible to `list_saves`, impossible
    to mistake for a save, and never cleaned up, so they accumulate a save's
    worth of bytes each (review one, R18)."""
    folder = os.path.dirname(write_lab["target"])
    old = os.path.join(folder, "save9.hg.nms-sorter-999999.tmp")
    fresh = os.path.join(folder, "save9.hg.nms-sorter-999998.tmp")
    for p in (old, fresh):
        with open(p, "wb") as fh:
            fh.write(b"leftover")
    long_ago = time.time() - (safety.TEMP_SWEEP_SECONDS + 60)
    os.utime(old, (long_ago, long_ago))

    with caplog.at_level(logging.INFO, logger="nms_sorter.safety"):
        rep, _res = _apply(write_lab)
    assert not os.path.exists(old), "the hour-old temp file is gone"
    assert os.path.exists(fresh), \
        "and a fresh one is left alone: it could belong to a live write"
    said = [r.getMessage() for r in caplog.records]
    assert any("swept" in m for m in said), \
        "the sweep is on the record: %s" % said
    step = [s for s in rep.steps if s["label"] == "temp files"]
    assert step and "999999" in step[0]["detail"], \
        "and the report names what it removed: %s" % step


def test_a_temp_file_is_never_mistaken_for_a_save(write_lab):
    """The sweep is a tidy-up, not a safety property: the reason a stray temp
    file was never dangerous is that `SAVE_RE` does not match it."""
    folder = os.path.dirname(write_lab["target"])
    with open(os.path.join(folder, "save9.hg.nms-sorter-1.tmp"), "wb") as fh:
        fh.write(b"leftover")
    from nms_sorter.savemodel import list_saves
    assert [r["file"] for r in list_saves(folder)] == ["save9.hg"], \
        "a temp file is not a save"


# ===========================================================================
# R2 / R3: the manifest records the outcome, at every stage
# ===========================================================================


def test_the_manifest_is_written_before_the_transform_starts(write_lab,
                                                             no_game_running,
                                                             monkeypatch):
    """The manifest used to be step 12, so every failure after step 2 left a
    backup of bare bytes -- and restore verifies against the manifest while
    retention refuses to prune a folder that has none, so the recovery artifact
    for the worst failure was exactly the one the recovery path could not use,
    and it accumulated forever (review one, R2)."""
    seen = {}

    def boom(save, cfg):
        folder = _only_backup_folder(write_lab)
        seen["manifest"] = safety.read_manifest(folder)
        raise KeyboardInterrupt("the machine lost power here")

    with pytest.raises(KeyboardInterrupt):
        safety.apply_plan(write_lab["target"], write_lab["cfg"],
                          write_lab["plan"].fingerprint_full, boom,
                          write_lab["backups"])
    man = seen["manifest"]
    assert man is not None, "there is a manifest before step 5 runs at all"
    assert man["outcome"] == safety.OUTCOME_IN_PROGRESS
    assert man["completed"] is False
    assert man["plan"]["fingerprint"] == write_lab["plan"].fingerprint, \
        "and it records the digest the operator approved: %s" % man["plan"]
    assert man["save"]["sha256"] == safety.sha256(
        os.path.join(_only_backup_folder(write_lab), "save9.hg"))


def test_a_refused_apply_leaves_the_reason_in_the_manifest(write_lab,
                                                           no_game_running):
    """A refusal after step 2 changed nothing, and the folder it leaves behind
    should say so rather than look like an abandoned write."""
    meta = os.path.join(os.path.dirname(write_lab["target"]), "mf_save9.hg")
    with open(meta, "wb") as fh:
        fh.write(b"\x00" * 16)              # refuses at step 8
    with pytest.raises(safety.Refused):
        _apply(write_lab)
    man = safety.read_manifest(_only_backup_folder(write_lab))
    assert man["outcome"] == safety.OUTCOME_REFUSED
    assert man["completed"] is False
    assert "could not be decoded" in man["refusal"], \
        "the sentence is in the file: %s" % man.get("refusal")
    assert _unchanged(write_lab), "and nothing was written"


def test_the_manifest_marks_the_window_in_which_the_pair_disagrees(
        write_lab, no_game_running, monkeypatch):
    """Between step 9 and step 10 the save has been replaced and its `mf_` still
    records the old sizes. That window is the one an operator most needs
    described, and the manifest is the only thing on disk that can describe it
    (review one, R3)."""
    def boom(*_a, **_kw):
        raise KeyboardInterrupt("the machine lost power here")

    monkeypatch.setattr(safety, "update_metadata", boom)
    with pytest.raises(KeyboardInterrupt):
        _apply(write_lab)
    folder = _only_backup_folder(write_lab)
    man = safety.read_manifest(folder)
    assert man["outcome"] == safety.OUTCOME_META_PENDING
    assert man["completed"] is False
    row = safety.list_backups(write_lab["backups"])[0]
    assert row["outcome"] == safety.OUTCOME_META_PENDING, \
        "and `list_backups` surfaces it, for the page: %s" % row
    meta = codec.meta_read(os.path.join(os.path.dirname(write_lab["target"]),
                                        "mf_save9.hg"))
    assert meta["size_disk"] != os.path.getsize(write_lab["target"]), \
        "the pair really does disagree, or this test proves nothing"


def test_a_finished_apply_says_written(applied_lab):
    man = safety.read_manifest(applied_lab["res"]["backup"])
    assert man["outcome"] == safety.OUTCOME_WRITTEN and man["completed"] is True
    assert "refusal" not in man, \
        "a finished apply carries no refusal: %s" % man.get("refusal")
    row = safety.list_backups(applied_lab["backups"])[0]
    assert row["outcome"] == safety.OUTCOME_WRITTEN and row["completed"] is True


# ===========================================================================
# R9: `list_backups` verifies both halves of the pair
# ===========================================================================


def test_list_backups_says_which_half_did_not_verify(applied_lab):
    mf = os.path.join(applied_lab["res"]["backup"], "mf_save9.hg")
    with open(mf, "r+b") as fh:
        fh.seek(0)
        fh.write(b"\x00\x00\x00\x00")
    row = safety.list_backups(applied_lab["backups"])[0]
    assert row["sha256_ok"] is False, "the folder is not verified: %s" % row
    assert row["files"]["save"]["sha256_ok"] is True, "the save half is fine"
    assert row["files"]["meta"]["sha256_ok"] is False, "the mf_ half is not"
    assert any("mf_save9.hg" in p for p in row["problems"]), \
        "and the sentence names the file: %s" % row["problems"]


def test_list_backups_tells_a_missing_copy_from_a_rotted_one(applied_lab):
    """"The mf_ is missing from this backup" and "it does not hash" are
    different problems with different fixes."""
    os.unlink(os.path.join(applied_lab["res"]["backup"], "mf_save9.hg"))
    row = safety.list_backups(applied_lab["backups"])[0]
    assert row["sha256_ok"] is False
    assert "is missing from this backup" in row["files"]["meta"]["detail"], \
        "the detail says missing, not corrupt: %s" % row["files"]["meta"]


# ===========================================================================
# R8 / P2-5: retention
# ===========================================================================


def _stamped_backup(root, stamp, manifest=True, name="save9.hg"):
    folder = os.path.join(root, "%s-save9" % stamp)
    os.makedirs(folder)
    body = b"pretend this is a save"
    with open(os.path.join(folder, name), "wb") as fh:
        fh.write(body)
    if manifest:
        with io.open(os.path.join(folder, "manifest.json"), "w",
                     encoding="utf-8") as fh:
            json.dump({"created": stamp, "outcome": "written",
                       "save": {"file": name,
                                "sha256": hashlib.sha256(body).hexdigest(),
                                "size": len(body)},
                       "meta": None, "plan": {"rows": 1, "containers": []},
                       "config_sha256": "0" * 64}, fh)
    return folder


def test_seven_backups_and_keep_five_removes_the_two_oldest(tmp_path):
    """`backup_keep` was a setting with a label on the page, a default in
    `settings.py` and no reader anywhere (review one, R8)."""
    root = str(tmp_path / "backups")
    os.makedirs(root)
    folders = [_stamped_backup(root, "2026091%d-120000" % n) for n in range(1, 8)]
    removed = safety.prune_backups(root, 5)
    assert sorted(removed) == sorted(folders[:2]), \
        "the two oldest, by stamp: %s" % removed
    left = sorted(os.listdir(root))
    assert len(left) == 5 and left[0] == os.path.basename(folders[2])
    assert os.path.isdir(folders[-1]), "and the newest is still there"


def test_keep_zero_prunes_nothing(tmp_path):
    """"Keep none" is not a policy anybody means, and a zero from a mistyped
    setting must not delete every backup."""
    root = str(tmp_path / "backups")
    os.makedirs(root)
    folders = [_stamped_backup(root, "20260911-12000%d" % n) for n in range(3)]
    assert safety.prune_backups(root, 0) == []
    assert safety.prune_backups(root, -1) == []
    assert len(os.listdir(root)) == len(folders)


def test_the_newest_backup_is_never_pruned(tmp_path):
    root = str(tmp_path / "backups")
    os.makedirs(root)
    folders = [_stamped_backup(root, "20260911-12000%d" % n) for n in range(3)]
    removed = safety.prune_backups(root, 1)
    assert sorted(removed) == sorted(folders[:2])
    assert os.path.isdir(folders[-1]), "the newest survives any arithmetic"


def test_a_folder_with_no_manifest_is_never_pruned(tmp_path, caplog):
    """An unknown folder in the backup root is somebody's. GOAL 3.7: retention
    "never [prunes] one whose manifest is missing"."""
    root = str(tmp_path / "backups")
    os.makedirs(root)
    known = [_stamped_backup(root, "2026091%d-120000" % n) for n in (5, 6, 7)]
    stray = _stamped_backup(root, "20260101-000000", manifest=False)
    with caplog.at_level(logging.INFO, logger="nms_sorter.safety"):
        removed = safety.prune_backups(root, 2)
    assert stray not in removed and os.path.isdir(stray), \
        "the folder nobody described is left alone"
    assert removed == [known[0]], \
        "and it does not count towards `keep` either: %s" % removed
    assert any("manifest is missing" in m for m in
               [r.getMessage() for r in caplog.records]), \
        "with a line saying why: %s" % [r.getMessage() for r in caplog.records]


def test_a_folder_whose_manifest_will_not_parse_is_never_pruned(tmp_path):
    """A truncated manifest is indistinguishable from none as far as
    `read_manifest` is concerned, and both mean "do not touch this"."""
    root = str(tmp_path / "backups")
    os.makedirs(root)
    _stamped_backup(root, "20260915-120000")
    broken = _stamped_backup(root, "20260101-000000")
    with open(os.path.join(broken, "manifest.json"), "w", encoding="utf-8") as fh:
        fh.write('{"created": "2026-01-01T00:00:00Z", "save"')
    assert safety.manifest_state(broken) == "unreadable"
    assert safety.prune_backups(root, 1) == []
    assert os.path.isdir(broken)


def test_pruning_an_empty_or_missing_root_is_not_an_error(tmp_path):
    assert safety.prune_backups(str(tmp_path / "nope"), 5) == []
    os.makedirs(str(tmp_path / "empty"))
    assert safety.prune_backups(str(tmp_path / "empty"), 5) == []


# ===========================================================================
# P2-5: restore
# ===========================================================================


@pytest.fixture
def restored_lab(applied_lab):
    """One apply, then one restore of the backup it made."""
    rep, res = safety.restore(applied_lab["res"]["backup"],
                              os.path.dirname(applied_lab["target"]))
    return dict(applied_lab, restore_rep=rep, restore_res=res)


def test_a_restore_puts_the_original_bytes_back(restored_lab):
    assert open(restored_lab["target"], "rb").read() == restored_lab["before"], \
        "the save is byte-for-byte the one that was backed up"


def test_a_restore_puts_the_metadata_back_too(restored_lab):
    """Both halves or neither: a good save beside metadata that denies it is
    worse than a pair nobody touched."""
    man = safety.read_manifest(restored_lab["res"]["backup"])
    meta = os.path.join(os.path.dirname(restored_lab["target"]), "mf_save9.hg")
    assert safety.sha256(meta) == man["meta"]["sha256"]
    assert sorted(restored_lab["restore_res"]["restored"]) == \
        ["mf_save9.hg", "save9.hg"]


def test_a_restore_reports_every_step_it_took(restored_lab):
    labels = [s["label"] for s in restored_lab["restore_rep"].steps]
    assert labels == ["game", "verify the backup", "restore",
                      "re-read from disk"], \
        "four steps, in this order: %s" % labels
    assert all(s["state"] in ("ok", "info")
               for s in restored_lab["restore_rep"].steps)


def test_a_restore_names_the_backup_and_the_manifest_it_used(restored_lab):
    res = restored_lab["restore_res"]
    assert res["backup"] == restored_lab["res"]["backup"]
    assert res["manifest"]["save"]["file"] == "save9.hg"


def test_a_restore_leaves_the_plan_signature_where_it_was(applied_lab):
    """The signature is `name|size|int(mtime)|sha256`, so a restore that left
    `now` in the modification time would hand back the right bytes under a
    signature the pre-apply plan cannot be re-approved against. `shutil.copy2`
    put the original's timestamps into the backup; the restore puts them back.
    """
    before = applied_lab["plan"].fingerprint_full
    safety.restore(applied_lab["res"]["backup"],
                   os.path.dirname(applied_lab["target"]))
    after, _commit = planner.build_plan(SaveFile(applied_lab["target"]),
                                        applied_lab["cfg"])
    assert after.fingerprint_full == before, \
        ("the plan printed before the apply is the plan printed after the "
         "restore: %s vs %s" % (before, after.fingerprint_full))


def test_a_backup_with_no_manifest_is_not_restored(tmp_path):
    """GOAL 3.7: restore "verifies both hashes before copying". A folder with no
    manifest has nothing to verify against, and guessing which of its files is
    the save is how the wrong bytes get written."""
    folder = str(tmp_path / "20260101-000000-save9")
    os.makedirs(folder)
    with open(os.path.join(folder, "save9.hg"), "wb") as fh:
        fh.write(b"bytes nobody described")
    with pytest.raises(safety.Refused) as exc:
        safety.restore(folder, str(tmp_path))
    assert ("this backup has no manifest, so its contents cannot be verified; "
            "copy the files back by hand if you are sure") in str(exc.value), \
        "the refusal says what to do instead: %s" % exc.value


def test_a_backup_whose_save_copy_has_rotted_is_not_restored(applied_lab):
    backup_save = os.path.join(applied_lab["res"]["backup"], "save9.hg")
    with open(backup_save, "r+b") as fh:
        fh.seek(0)
        fh.write(b"\x00")
    written = open(applied_lab["target"], "rb").read()
    with pytest.raises(safety.Refused) as exc:
        safety.restore(applied_lab["res"]["backup"],
                       os.path.dirname(applied_lab["target"]))
    assert "save9.hg does not hash to what the manifest says" in str(exc.value), \
        "the refusal names the file: %s" % exc.value
    assert open(applied_lab["target"], "rb").read() == written, \
        "and nothing was written"


def test_a_backup_whose_metadata_copy_is_missing_is_not_restored(applied_lab):
    """Half a pair is not a restore. This is the failure R9 made invisible."""
    os.unlink(os.path.join(applied_lab["res"]["backup"], "mf_save9.hg"))
    written = open(applied_lab["target"], "rb").read()
    with pytest.raises(safety.Refused) as exc:
        safety.restore(applied_lab["res"]["backup"],
                       os.path.dirname(applied_lab["target"]))
    assert "mf_save9.hg is missing from this backup" in str(exc.value)
    assert open(applied_lab["target"], "rb").read() == written


def test_a_restore_works_when_the_save_has_been_deleted(applied_lab):
    """The case a restore is actually for. Nothing in the sequence needs the
    file it is about to write to exist: the lock is named after the save in the
    manifest, and the bytes come out of the backup."""
    os.unlink(applied_lab["target"])
    os.unlink(os.path.join(os.path.dirname(applied_lab["target"]), "mf_save9.hg"))
    _rep, res = safety.restore(applied_lab["res"]["backup"],
                               os.path.dirname(applied_lab["target"]))
    assert sorted(res["restored"]) == ["mf_save9.hg", "save9.hg"]
    assert open(applied_lab["target"], "rb").read() == applied_lab["before"]


def test_a_restore_refuses_a_loaded_save(applied_lab, monkeypatch):
    """The same rule as an apply, in the same direction: a running game with
    nobody saying they are on the menu is the 60-second autosave race."""
    monkeypatch.setattr(safety, "GAME_PROCESS", "NMS.exe")
    monkeypatch.setattr(safety, "game_status", lambda: {
        "running": True, "pids": [77], "method": "test", "known": True,
        "process": "NMS.exe"})
    written = open(applied_lab["target"], "rb").read()
    with pytest.raises(safety.Refused) as exc:
        safety.restore(applied_lab["res"]["backup"],
                       os.path.dirname(applied_lab["target"]))
    assert "NMS.exe is running as pid 77" in str(exc.value)
    assert "I am at the main menu, not in a loaded save" in str(exc.value), \
        "and the refusal names the tick: %s" % exc.value
    assert open(applied_lab["target"], "rb").read() == written


def test_a_restore_runs_from_the_main_menu_when_it_is_confirmed(applied_lab,
                                                                monkeypatch):
    monkeypatch.setattr(safety, "GAME_PROCESS", "NMS.exe")
    monkeypatch.setattr(safety, "game_status", lambda: {
        "running": True, "pids": [77], "method": "test", "known": True,
        "process": "NMS.exe"})
    rep, _res = safety.restore(applied_lab["res"]["backup"],
                               os.path.dirname(applied_lab["target"]),
                               at_main_menu=True)
    step = [s for s in rep.steps if s["label"] == "game"][0]
    assert step["state"] == "info", step
    assert step["detail"] == ("running as pid 77: at the main menu, as "
                             "confirmed"), step["detail"]
    assert open(applied_lab["target"], "rb").read() == applied_lab["before"]


def test_a_restore_takes_the_same_lock_an_apply_does(applied_lab):
    """One writer per save file, whichever direction it is writing in."""
    lock = safety.lock_path(applied_lab["target"])
    with io.open(lock, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "created": "2026-09-14T00:00:00Z"}, fh)
    try:
        with pytest.raises(safety.Refused) as exc:
            safety.restore(applied_lab["res"]["backup"],
                           os.path.dirname(applied_lab["target"]))
        assert "another sorter instance is writing this save" in str(exc.value)
    finally:
        os.unlink(lock)
    assert not os.path.exists(lock + ".reclaim"), "and no guard is left behind"


def test_a_restore_releases_the_lock(restored_lab):
    assert not os.path.exists(safety.lock_path(restored_lab["target"])), \
        "the lock does not outlive the restore"


def test_a_restore_that_cannot_write_is_refused_in_words(applied_lab,
                                                         monkeypatch):
    """The generic filesystem sentence: a full disk, a read-only volume,
    anything that is not a permission failure on a writable file."""
    def boom(*_a, **_kw):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(safety.os, "replace", boom)
    with pytest.raises(safety.Refused) as exc:
        safety.restore(applied_lab["res"]["backup"],
                       os.path.dirname(applied_lab["target"]))
    assert "the save folder refused the write" in str(exc.value)
    assert "No space left on device" in str(exc.value)


def test_a_restore_onto_a_file_something_holds_open_names_the_game(
        applied_lab, monkeypatch):
    """The same sentence an apply gets, from the other direction: a restore
    writes into the save folder too, so a `PermissionError` there is something
    holding the file rather than the operator's own permissions."""
    def boom(*_a, **_kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(safety.os, "replace", boom)
    with pytest.raises(safety.Refused) as exc:
        safety.restore(applied_lab["res"]["backup"],
                       os.path.dirname(applied_lab["target"]))
    assert "the game has the save open" in str(exc.value), exc.value
    assert "go to the main menu, or close the game, and try again" \
        in str(exc.value), exc.value


# ===========================================================================
# has the save moved since the apply? (the manifest's `written` block)
# ===========================================================================
#
# Everything the restore sequence checked before this was a property of the
# *backup*: its two copies still hash to the manifest, and the manifest came
# from the folder being written into. Both stay true for as long as the folder
# sits on disk. Neither says anything about the save, so sorting, playing for
# four hours and then pressing Undo put the pre-sort save back over the four
# hours with all four steps reporting `ok`.
#
# `written` is the missing fact: the hashes of the files as the apply left
# them. The refusal below is the new one, and it is reached by
# `test_a_restore_refuses_when_the_game_has_written_the_save_since` -- the
# same measurement the review applied to the other twenty-three (wrap
# `Report.fail`, run the suite, write a test for anything that never fired).


def _the_game_writes_the_save(path, hours=4):
    """Rewrite `path` the way the game would: same file, different bytes.

    Through the codec rather than by poking a byte, because a corrupt file
    would be caught by a different check and would prove nothing about this
    one: what has to be on disk is a save that decodes perfectly well and is
    simply not the one the apply wrote. A new top-level key rather than an
    edit to an existing one, so the helper does not depend on whether this
    document's keys are obfuscated.

    Returns the new bytes.
    """
    payload = SaveFile(path).payload
    doc = loads(payload)
    doc["PlayTime"] = int(hours * 3600)
    framed = frame_payload(dumps(doc))
    with open(path, "wb") as fh:
        fh.write(framed)
    assert SaveFile(path).payload != payload, \
        "the file on disk is a different save, and still a save"
    return framed


THE_SENTENCE = ("save9.hg has changed since this backup was taken; the game "
                "(or something else) has written it since, and restoring "
                "would discard that. Copy the backup out by hand if you are "
                "sure")


def test_the_manifest_records_what_the_apply_wrote(applied_lab):
    """Step 12's half of the pair. `save`/`meta` describe the backup copies;
    `written` describes what is in the save folder afterwards, which is the
    only thing a later restore can compare the save against."""
    man = safety.read_manifest(applied_lab["res"]["backup"])
    target = applied_lab["target"]
    meta = os.path.join(os.path.dirname(target), "mf_save9.hg")
    assert man["written"]["save"] == {
        "file": "save9.hg", "sha256": safety.sha256(target),
        "size": os.path.getsize(target)}, \
        "the save's row is the file on disk, not the backup copy: %s" % man["written"]
    assert man["written"]["meta"] == {
        "file": "mf_save9.hg", "sha256": safety.sha256(meta),
        "size": os.path.getsize(meta)}
    assert man["written"]["save"]["sha256"] != man["save"]["sha256"], \
        "which is the whole point: the two halves describe different bytes"


def test_a_manifest_records_no_written_metadata_when_there_is_none(
        write_lab, no_game_running):
    """A save with no `mf_` beside it. `null`, the same answer the backup half
    records, rather than a row for a file that does not exist."""
    os.unlink(os.path.join(os.path.dirname(write_lab["target"]), "mf_save9.hg"))
    _rep, res = _apply(write_lab)
    man = safety.read_manifest(res["backup"])
    assert man["written"]["meta"] is None
    assert man["written"]["save"]["sha256"] == safety.sha256(write_lab["target"])


def test_a_restore_is_allowed_when_nothing_has_touched_the_save(applied_lab):
    """The ordinary Undo: the save in the folder is still the one the apply
    wrote, so there is nothing to discard and the check is silent."""
    rep, _res = safety.restore(applied_lab["res"]["backup"],
                               os.path.dirname(applied_lab["target"]))
    assert open(applied_lab["target"], "rb").read() == applied_lab["before"]
    assert [s["label"] for s in rep.steps] == \
        ["game", "verify the backup", "restore", "re-read from disk"], \
        "a check that passes adds no step: %s" % [s["label"] for s in rep.steps]


def test_a_restore_refuses_when_the_game_has_written_the_save_since(
        applied_lab):
    """The four hours. Sort, play, press Undo: every check the sequence had
    still passes, because every one of them is about the backup.
    """
    played = _the_game_writes_the_save(applied_lab["target"])
    meta = os.path.join(os.path.dirname(applied_lab["target"]), "mf_save9.hg")
    meta_before = open(meta, "rb").read()

    with pytest.raises(safety.Refused) as exc:
        safety.restore(applied_lab["res"]["backup"],
                       os.path.dirname(applied_lab["target"]))

    assert str(exc.value) == "restore: " + THE_SENTENCE, \
        "the refusal names the file and says what to do instead: %s" % exc.value
    assert open(applied_lab["target"], "rb").read() == played, \
        "and the session on disk is untouched: a refusal writes nothing"
    assert open(meta, "rb").read() == meta_before, \
        "including the metadata, which is written second and so never at all"


def test_the_refusal_comes_after_the_checks_that_passed(applied_lab):
    """The 409 carries the steps, so the operator can see that the backup is
    fine and it is the save that has moved -- otherwise the sentence reads as
    if the backup were the problem."""
    _the_game_writes_the_save(applied_lab["target"])
    with pytest.raises(safety.Refused) as exc:
        safety.restore(applied_lab["res"]["backup"],
                       os.path.dirname(applied_lab["target"]))
    steps = exc.value.report.steps
    assert [s["label"] for s in steps] == \
        ["game", "verify the backup", "restore"], \
        "three steps: two passed, the third refused: %s" % steps
    assert steps[1]["state"] == "ok", "the backup itself verified"
    assert steps[-1]["state"] == "fail"


def test_a_restore_can_be_forced_past_a_save_that_has_moved(applied_lab):
    """`force` is for an operator who means it. Not silent: the one check that
    would have stopped this is the one the report has to name."""
    _the_game_writes_the_save(applied_lab["target"])
    rep, res = safety.restore(applied_lab["res"]["backup"],
                              os.path.dirname(applied_lab["target"]),
                              force=True)
    assert sorted(res["restored"]) == ["mf_save9.hg", "save9.hg"]
    assert open(applied_lab["target"], "rb").read() == applied_lab["before"], \
        "forced means forced: the pre-sort save is back"
    forced = [s for s in rep.steps
              if s["state"] == "info" and s["detail"] == THE_SENTENCE]
    assert len(forced) == 1, \
        "the refusal became an info step, with the same sentence: %s" % rep.steps


def test_a_manifest_that_does_not_say_what_the_apply_wrote_says_so(
        applied_lab):
    """An older build's backup, or an apply that stopped before step 12. It is
    still the operator's own backup and still worth restoring; what cannot be
    established is whether anything has been played since."""
    path = os.path.join(applied_lab["res"]["backup"], safety.MANIFEST_NAME)
    man = safety.read_manifest(applied_lab["res"]["backup"])
    del man["written"]
    with io.open(path, "w", encoding="utf-8") as fh:
        json.dump(man, fh)
    _the_game_writes_the_save(applied_lab["target"])

    rep, _res = safety.restore(applied_lab["res"]["backup"],
                               os.path.dirname(applied_lab["target"]))
    assert open(applied_lab["target"], "rb").read() == applied_lab["before"], \
        "the restore goes ahead: an unknown answer is not a refusal"
    said = [s for s in rep.steps
            if s["detail"] == ("the manifest does not record what the apply "
                               "wrote, so whether the save changed since "
                               "cannot be checked")]
    assert len(said) == 1 and said[0]["state"] == "info", \
        "and the report says the check could not be made: %s" % rep.steps


def test_restoring_the_same_backup_twice_is_not_a_discarded_session(
        applied_lab):
    """After the first restore the save on disk is the backup's own copy,
    which is exactly what a second restore would write. Refusing there would
    say a session had been thrown away when nothing had changed at all -- and
    it is the state the page is in the moment after an Undo."""
    folder = applied_lab["res"]["backup"]
    save_dir = os.path.dirname(applied_lab["target"])
    safety.restore(folder, save_dir)
    rep, res = safety.restore(folder, save_dir)
    assert sorted(res["restored"]) == ["mf_save9.hg", "save9.hg"]
    assert all(s["state"] == "ok" for s in rep.steps), \
        "an idempotent restore is not a refusal: %s" % rep.steps


# --- what the page needs to grey the button out ----------------------------


def test_list_backups_says_the_save_still_matches(applied_lab):
    row = safety.list_backups(applied_lab["backups"])[0]
    assert row["current_matches"] is True, \
        "nothing has touched the save, so an Undo discards nothing: %s" % row


def test_list_backups_says_the_save_has_moved(applied_lab):
    _the_game_writes_the_save(applied_lab["target"])
    row = safety.list_backups(applied_lab["backups"])[0]
    assert row["current_matches"] is False, \
        "the page greys out an Undo that would discard the play: %s" % row
    assert row["sha256_ok"] is True, \
        "and it is not the backup that is wrong; the two answers are separate"


def test_list_backups_says_it_cannot_tell(applied_lab):
    """Two ways: a manifest that records nothing to compare against, and a
    save folder that no longer holds the file. Neither is "would discard
    play", and neither is "safe"."""
    path = os.path.join(applied_lab["res"]["backup"], safety.MANIFEST_NAME)
    man = safety.read_manifest(applied_lab["res"]["backup"])
    written = man.pop("written")
    with io.open(path, "w", encoding="utf-8") as fh:
        json.dump(man, fh)
    row = safety.list_backups(applied_lab["backups"])[0]
    assert row["current_matches"] is None, \
        "an older build's manifest: unknown, not False: %s" % row

    man["written"] = written
    with io.open(path, "w", encoding="utf-8") as fh:
        json.dump(man, fh)
    os.unlink(applied_lab["target"])
    os.unlink(os.path.join(os.path.dirname(applied_lab["target"]),
                           "mf_save9.hg"))
    row = safety.list_backups(applied_lab["backups"])[0]
    assert row["current_matches"] is None, \
        "and restoring into an empty slot discards nothing either: %s" % row


def test_list_backups_hashes_the_save_folder_once_per_call(applied_lab,
                                                           monkeypatch):
    """Two manifests naming the same two files is still two files to hash. The
    save is a 2.8 MB file on the operator's machine and the page polls this
    route, so the cost has to be per call and not per backup."""
    second = os.path.join(applied_lab["backups"], "20200101-000000-save9")
    shutil.copytree(applied_lab["res"]["backup"], second)
    assert len(safety.list_backups(applied_lab["backups"])) == 2

    hashed = []
    real = safety.sha256

    def counting(path):
        hashed.append(os.path.normcase(os.path.abspath(path)))
        return real(path)

    monkeypatch.setattr(safety, "sha256", counting)
    rows = safety.list_backups(applied_lab["backups"])
    save_dir = os.path.normcase(os.path.abspath(
        os.path.dirname(applied_lab["target"])))
    in_save_dir = [p for p in hashed if os.path.dirname(p) == save_dir]
    assert sorted(in_save_dir) == sorted(set(in_save_dir)), \
        ("the save folder is hashed once per file per call, not once per "
         "backup: %s" % in_save_dir)
    assert len(in_save_dir) == 2, \
        "the save and its mf_, once each: %s" % in_save_dir
    assert all(r["current_matches"] is True for r in rows), \
        "and both backups get the answer: %s" % rows


# ===========================================================================
# P4-4: the write path under six kinds of interruption
# ===========================================================================
#
# One test per row of GOAL.md P4-4. Every one of them ends with the original
# bytes on disk and a sentence, which is the ticket's exit condition.


def test_p4_4_a_kill_between_the_temp_write_and_the_replace(write_lab,
                                                            no_game_running,
                                                            monkeypatch):
    """Row 1: the process dies at the worst moment there is. The temp file is
    written and `os.replace` never happens."""
    def boom(*_a, **_kw):
        raise KeyboardInterrupt("the machine lost power here")

    monkeypatch.setattr(safety.os, "replace", boom)
    with pytest.raises(KeyboardInterrupt):
        _apply(write_lab)
    assert _unchanged(write_lab), "the original bytes are on disk"
    strays = [f for f in os.listdir(os.path.dirname(write_lab["target"]))
              if f.endswith(".tmp")]
    assert not strays, "and no temp file is left: %s" % strays
    man = safety.read_manifest(_only_backup_folder(write_lab))
    assert man["outcome"] == safety.OUTCOME_IN_PROGRESS, \
        "the backup describes itself as a run that did not finish"
    assert not os.path.exists(safety.lock_path(write_lab["target"])), \
        "and the lock is released"


def test_p4_4_a_full_disk(write_lab, no_game_running, monkeypatch):
    """Row 2: `write` raising `ENOSPC`, which is what a full disk does to the
    temp file before anything has been replaced."""
    real_open = open

    class FullDisk(object):
        def __init__(self, *a, **kw):
            self.fh = real_open(*a, **kw)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.fh.close()
            return False

        def write(self, _data):
            raise OSError(errno.ENOSPC, "No space left on device")

        def __getattr__(self, name):
            return getattr(self.fh, name)

    def fake_open(name, mode="r", *a, **kw):
        if str(name).endswith(".tmp") and "b" in mode and "w" in mode:
            return FullDisk(name, mode, *a, **kw)
        return real_open(name, mode, *a, **kw)

    monkeypatch.setattr(safety, "open", fake_open, raising=False)
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert "No space left on device" in str(exc.value)
    assert "nothing was changed" in str(exc.value)
    assert _unchanged(write_lab), "the original bytes are on disk"
    strays = [f for f in os.listdir(os.path.dirname(write_lab["target"]))
              if f.endswith(".tmp")]
    assert not strays, "and no temp file is left: %s" % strays


def test_p4_4_a_read_only_save_file(write_lab, no_game_running):
    """Row 3: the read-only attribute, or a sync client holding the file. The
    replace is what fails, and the original is still the original."""
    os.chmod(write_lab["target"], stat.S_IREAD)
    try:
        try:
            with open(write_lab["target"], "ab"):
                pass
        except OSError:
            pass
        else:
            pytest.skip("this filesystem does not enforce the read-only bit")
        with pytest.raises(safety.Refused) as exc:
            _apply(write_lab)
        assert "the save folder refused the write" in str(exc.value)
        assert write_lab["target"] in str(exc.value), \
            "and names the file: %s" % exc.value
    finally:
        # Both bits: `S_IWRITE` alone is "clear the read-only attribute" on
        # Windows and "owner may write and may *not* read" on Linux, where it
        # made `_unchanged` a PermissionError of its own.
        os.chmod(write_lab["target"], stat.S_IWRITE | stat.S_IREAD)
    assert _unchanged(write_lab)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_posix_mode_that_forbids_writing_is_the_folder_sentence(
        write_lab, no_game_running):
    """The same row on POSIX, where the answer comes from the mode bits and
    the error carries no `winerror` at all: `_write_denied` has to refuse on
    the errno alone (review 5, finding 4)."""
    os.chmod(write_lab["target"], 0o444)
    try:
        with pytest.raises(safety.Refused) as exc:
            _apply(write_lab)
        assert "the save folder refused the write" in str(exc.value)
        assert "the game has the save open" not in str(exc.value), \
            "a mode the operator set is not the game: %s" % exc.value
    finally:
        os.chmod(write_lab["target"], 0o644)
    assert _unchanged(write_lab)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACLs")
def test_an_acl_that_denies_writing_is_refused_not_replaced(
        write_lab, no_game_running):
    """Review 5, finding 4, measured: an `icacls` deny-write entry left
    `os.access(W_OK)` True, `open(path, "r+b")` raising `PermissionError 13`,
    and `write_atomic` replacing the file anyway. An ACL is the other half of
    "the operator said not this file", and it now reaches the same refusal the
    read-only attribute does.

    The four specific rights and not the simple `W`: `W` denies reading the
    file's attributes too, so the apply cannot get as far as the write and the
    test would prove something else. The deny entry is removed in the
    `finally`, and the file it is set on is a copy of the synthetic fixture in
    a pytest temp directory."""
    target = write_lab["target"]
    user = os.environ.get("USERNAME") or ""
    if not user:
        pytest.skip("no USERNAME to name in an ACL")
    grant = subprocess.run(
        ["icacls", target, "/deny", "%s:(WD,AD,WEA,WA)" % user],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if grant.returncode != 0:
        pytest.skip("icacls refused to set the deny entry: %s"
                    % grant.stdout.decode("utf-8", "replace").strip())
    try:
        try:
            with open(target, "r+b"):
                pass
        except OSError:
            pass
        else:
            pytest.skip("this filesystem does not enforce the deny entry")
        # The defect this test exists for: the old precheck asked `os.access`,
        # which reads the read-only attribute and not the security
        # descriptor, so it answered "writable" here.
        assert os.access(target, os.W_OK), \
            "os.access still says writable, which is the whole point"
        with pytest.raises(safety.Refused) as exc:
            _apply(write_lab)
        assert "the save folder refused the write" in str(exc.value), exc.value
        assert "the game has the save open" not in str(exc.value), \
            "an ACL the operator set is not the game: %s" % exc.value
    finally:
        subprocess.run(["icacls", target, "/remove:d", user],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert _unchanged(write_lab), "and the original bytes are still on disk"


@pytest.mark.skipif(os.name != "nt", reason="Windows share modes")
def test_a_share_locked_save_still_reaches_the_game_sentence(
        write_lab, no_game_running):
    """The other side of the same precheck. A handle that denies the write is
    not a permission, and `os.open` reports it as `EACCES` with no `winerror`
    -- identical to an ACL denial -- so the probe must not refuse on it and
    steal the one sentence the main menu fixes."""
    handle = _hold_open_against_rename(write_lab["target"])
    assert handle, "the save could not be opened for this test"
    try:
        assert safety._write_denied(write_lab["target"]) is None, \
            "a share lock is not the file's own permissions"
        with pytest.raises(safety.Refused) as exc:
            _apply(write_lab)
    finally:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
    assert "the game has the save open" in str(exc.value), exc.value
    assert _unchanged(write_lab)


def test_an_eperm_is_not_reported_as_the_game_holding_the_save(write_lab):
    """Review 5, note 5. `refuse_oserror` gated on `PermissionError`, which
    covers `EPERM` as well as `EACCES`: a Linux immutable file (`chattr +i`)
    or an NFS export saying no was reported as the game holding the save, and
    "go to the main menu, or close the game" cannot fix either."""
    exc = PermissionError(errno.EPERM, "Operation not permitted")
    said = safety.refuse_oserror(exc, write_lab["target"], "write",
                                 held_open=True)
    assert "the game has the save open" not in str(said), str(said)
    assert "the save folder refused the write" in str(said), str(said)
    ok = PermissionError(errno.EACCES, "Access is denied")
    said = safety.refuse_oserror(ok, write_lab["target"], "write",
                                 held_open=True)
    assert "the game has the save open" in str(said), \
        "and EACCES on a writable file keeps the sentence: %s" % said


def test_the_written_bytes_do_not_depend_on_the_game_state(
        synthetic_path, tmp_path, monkeypatch):
    """`CLAUDE.md`'s "a test pins this", for the input the main-menu flow
    added. Review 5, note 8: nothing applied the same plan under the two game
    states and compared the bytes, which is exactly where a new step 1 could
    have leaked into the write.

    Two labs, same fixture, same config: one apply with the game faked closed
    and one with it faked running plus the tick. The save and its `mf_` have
    to hash the same, and the plan fingerprint with them."""
    def lab(name):
        folder = tmp_path / name
        folder.mkdir()
        target = str(folder / make_fixture.SAVE_NAME)
        shutil.copy2(synthetic_path, target)
        payload = SaveFile(target).payload
        with open(str(folder / ("mf_" + make_fixture.SAVE_NAME)), "wb") as fh:
            fh.write(make_fixture.build_meta(
                len(payload), os.path.getsize(target),
                make_fixture.SAVE_NAME, "base"))
        cfg = make_fixture.synthetic_config()
        plan, _commit = planner.build_plan(SaveFile(target), cfg)
        return {"target": target, "cfg": cfg, "plan": plan,
                "meta": str(folder / ("mf_" + make_fixture.SAVE_NAME)),
                "backups": str(folder / "backups")}

    def run(one, status):
        monkeypatch.setattr(safety, "GAME_PROCESS", "NMS.exe")
        monkeypatch.setattr(safety, "game_status", lambda: dict(status))
        _rep, res = safety.apply_plan(
            one["target"], one["cfg"], one["plan"].fingerprint_full,
            planner.build_plan, one["backups"],
            at_main_menu=bool(status["running"]))
        assert res["rows"], "the apply has to write something to prove this"
        return (hashlib.sha256(open(one["target"], "rb").read()).hexdigest(),
                hashlib.sha256(open(one["meta"], "rb").read()).hexdigest(),
                one["plan"].fingerprint_full)

    closed = run(lab("closed"), {"running": False, "pids": [], "known": True,
                                 "method": "test", "process": "NMS.exe"})
    menu = run(lab("menu"), {"running": True, "pids": [4242], "known": True,
                             "method": "test", "process": "NMS.exe"})
    assert closed == menu, \
        ("the bytes and the fingerprint are the same with the game closed and "
         "with it running plus the tick: %s vs %s" % (closed, menu))


def test_p4_4_the_save_was_replaced_between_the_plan_and_the_apply(
        write_lab, no_game_running):
    """Row 4: the same-size, same-second replacement the old signature could
    not see (review one, R15). The content hash is what refuses it now."""
    was = os.stat(write_lab["target"])
    edited = SaveFile(write_lab["target"])
    edited.d.set(edited.d.player, "Units", 12344)      # same number of digits
    framed = frame_payload(dumps(edited.doc))
    assert len(framed) == was.st_size, "the same size, or this proves nothing"
    with open(write_lab["target"], "wb") as fh:
        fh.write(framed)
    os.utime(write_lab["target"], (was.st_atime, was.st_mtime))
    now = open(write_lab["target"], "rb").read()

    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab, fingerprint=write_lab["plan"].fingerprint_full)
    assert "Something changed between the dry run and now" in str(exc.value), \
        "the refusal says what happened: %s" % exc.value
    assert open(write_lab["target"], "rb").read() == now, \
        "and the bytes that were there are still there"


def test_p4_4_the_metadata_file_is_missing(write_lab, no_game_running):
    """Row 5: a save with no `mf_` beside it sorts, and says so twice -- once at
    the backup and once at the metadata step (review one, R4)."""
    meta = os.path.join(os.path.dirname(write_lab["target"]), "mf_save9.hg")
    os.unlink(meta)
    rep, res = _apply(write_lab)
    assert res["rows"], "the save sorts with no metadata beside it"
    said = [s for s in rep.steps if s["state"] == "info"]
    assert any("no metadata file beside save9.hg" in s["detail"] for s in said), \
        "step 2 says there was nothing to copy: %s" % said
    assert any("nothing to update" in s["detail"] for s in said), \
        "and step 10 says there was nothing to patch: %s" % said
    assert not os.path.exists(meta), "and none was invented"


def test_p4_4_the_metadata_file_belongs_to_another_slot(write_lab,
                                                        no_game_running):
    """Row 6: an `mf_` encrypted with another slot's key -- somebody copied
    `mf_save3.hg` over `mf_save9.hg`. The decryption key is derived from the
    file name, so the bytes decrypt to rubbish; the codec notices the magic
    word is wrong and says which it suspects, and step 8 refuses before the
    save is written rather than after."""
    meta = os.path.join(os.path.dirname(write_lab["target"]), "mf_save9.hg")
    payload = SaveFile(write_lab["target"]).payload
    with open(meta, "wb") as fh:
        fh.write(make_fixture.build_meta(
            len(payload), os.path.getsize(write_lab["target"]), "save3.hg"))
    wrong = open(meta, "rb").read()
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert "this save's metadata could not be decoded" in str(exc.value), \
        "the refusal is a sentence, not a traceback: %s" % exc.value
    assert "wrong slot key" in str(exc.value), \
        "and the codec's own guess is carried into it: %s" % exc.value
    assert _unchanged(write_lab), "the save is untouched"
    assert open(meta, "rb").read() == wrong, "and so is the metadata"


# ===========================================================================
# the sentences the fixes themselves added
# ===========================================================================
#
# A fix that introduces a refusal nothing reaches has repeated the mistake the
# review measured. Same method: wrap `Report.fail`, run the suite, and write a
# test for anything that never fired.


def test_a_lock_that_turned_out_to_be_somebody_elses_is_refused(write_lab,
                                                                monkeypatch):
    """The pid is written and read back, and a lock that comes back naming
    another process is a refusal -- *without* removing it, because whoever owns
    it is entitled to it. It is the only way this process can find out that
    something replaced the file underneath it between the `O_EXCL` and the
    write."""
    real = safety.read_lock

    def foreign(path):
        out = real(path)
        out["pid"] = 4242
        return out

    monkeypatch.setattr(safety, "read_lock", foreign)
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert "was taken by another writer (pid 4242)" in str(exc.value), \
        "the refusal names the pid that has it: %s" % exc.value
    assert "nothing was changed" in str(exc.value)
    assert _unchanged(write_lab)
    lock = safety.lock_path(write_lab["target"])
    assert os.path.exists(lock), \
        "and it is left where it is: it is not ours to remove"
    os.unlink(lock)

    # The same check with no `Report` to write into: `held_lock` is called
    # directly by the two-writer test, and there it is `raise Refused`
    # rather than `Report.fail` that has to do the refusing.
    with pytest.raises(safety.Refused) as bare:
        with safety.held_lock(write_lab["target"]):
            pass
    assert "was taken by another writer (pid 4242)" in str(bare.value)
    if os.path.exists(lock):
        os.unlink(lock)


def test_a_lock_that_can_never_be_reclaimed_is_refused_not_spun_on(
        write_lab, monkeypatch):
    """`LOCK_ATTEMPTS` bounds the reclaim loop. A stale lock that is taken over
    by somebody else on every round is a save too contended to write to, and
    the answer is the ordinary live-lock sentence rather than a spin."""
    lock = safety.lock_path(write_lab["target"])
    with open(lock, "w", encoding="utf-8") as fh:
        json.dump({"pid": DEAD_PID, "created": "2020-01-01T00:00:00Z"}, fh)
    old = time.time() - (safety.STALE_LOCK_SECONDS + 60)
    os.utime(lock, (old, old))
    tries = []

    def always_loses(*_a, **_kw):
        tries.append(1)
        return None

    monkeypatch.setattr(safety, "_reclaim_stale_lock", always_loses)
    try:
        with pytest.raises(safety.Refused) as exc:
            _apply(write_lab)
        assert "another sorter instance is writing this save" in str(exc.value)
        assert len(tries) == safety.LOCK_ATTEMPTS, \
            "it gives up after a bounded number of rounds: %d" % len(tries)
    finally:
        os.unlink(lock)
    assert _unchanged(write_lab)


def test_a_manifest_that_names_no_save_is_not_restored(tmp_path):
    """A manifest is only a manifest if it says which save it describes.
    `write_manifest` records `null` when step 2 copied no save at all, which
    cannot happen today and is exactly the sort of thing a restore must not
    guess its way past."""
    folder = str(tmp_path / "20260101-000000-save9")
    os.makedirs(folder)
    with io.open(os.path.join(folder, "manifest.json"), "w",
                 encoding="utf-8") as fh:
        json.dump({"created": "2026-01-01T00:00:00Z", "save": None,
                   "meta": None, "outcome": "written"}, fh)
    with pytest.raises(safety.Refused) as exc:
        safety.restore(folder, str(tmp_path))
    assert "does not name a save file" in str(exc.value), \
        "the refusal says what is wrong with the manifest: %s" % exc.value


def test_a_restore_refuses_when_the_process_check_cannot_run(applied_lab,
                                                             monkeypatch):
    """Off Windows, and on a Windows box where the process list will not read.
    A check that cannot run is a refusal, in a restore exactly as in an
    apply."""
    monkeypatch.setattr(safety, "game_status", lambda: {
        "running": None, "pids": [], "method": "unsupported: linux",
        "known": False, "process": "NMS.exe"})
    written = open(applied_lab["target"], "rb").read()
    with pytest.raises(safety.Refused) as exc:
        safety.restore(applied_lab["res"]["backup"],
                       os.path.dirname(applied_lab["target"]))
    assert platformmod.UNSUPPORTED_REFUSAL in str(exc.value), \
        "one wording, from `platform.refusal_sentence`: %s" % exc.value
    assert open(applied_lab["target"], "rb").read() == written


def test_a_restore_that_wrote_the_wrong_bytes_is_caught_on_the_re_read(
        applied_lab, monkeypatch):
    """The restore's own step 4. Verifying the backup before writing is not the
    same claim as the file on disk being what the backup holds, and the second
    one is the one the operator cares about."""
    real = safety.write_atomic
    # `*a, **kw`: `write_atomic` takes `what` and `after_write` now, because
    # the sentence a failure produces has to say whether anything had already
    # been written (write-path review two, Q2). A stub that pinned the old
    # arity would fail on an argument instead of on the byte it corrupts.
    monkeypatch.setattr(
        safety, "write_atomic",
        lambda path, data, *a, **kw: real(path, data + b"\x00", *a, **kw))
    with pytest.raises(safety.Refused) as exc:
        safety.restore(applied_lab["res"]["backup"],
                       os.path.dirname(applied_lab["target"]))
    assert "was written but does not hash to the backup's copy" in str(exc.value), \
        "the refusal names the file and what it hashes to: %s" % exc.value


# ===========================================================================
# P6-5 (write-path review two): the fixes, and the two sentences review two
# found unreached
# ===========================================================================


# --- Q1: a refusal after the save was replaced --------------------------


def test_a_refusal_after_the_write_keeps_the_plan_row_and_its_own_outcome(
        write_lab, no_game_running, monkeypatch):
    """The manifest is the only machine-readable record that an apply
    happened. A refusal at step 11 used to rewrite it as `refused` -- defined
    as "changed nothing" -- with `rows` and `containers` reset to null, at
    exactly the moment the operator needs the backup (Q1)."""
    real = codec.meta_read

    def off_by_one(path):
        m = dict(real(path))
        m["size_disk"] += 1
        return m

    monkeypatch.setattr(safety, "meta_read", off_by_one)
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    man = safety.read_manifest(_only_backup_folder(write_lab))
    assert man["outcome"] == safety.OUTCOME_REFUSED_AFTER_WRITE, \
        "the save on disk is the new one and the outcome says so: %s" % man
    assert man["completed"] is False, "it is still not a finished apply"
    assert man["plan"]["rows"] == len(write_lab["plan"].rows), \
        "and the plan row it had recorded is not thrown away: %s" % man["plan"]
    assert man["plan"]["containers"], \
        "including which containers it touched: %s" % man["plan"]
    assert "mf_ size_disk" in man["refusal"], man["refusal"]
    said = [s["detail"] for s in exc.value.report.steps
            if s["state"] == "info"]
    assert any("the save on disk is the new one" in d for d in said), \
        "and the report says what is on disk: %s" % said


def test_a_refusal_after_the_write_is_never_pruned(write_lab, no_game_running,
                                                   monkeypatch):
    """A backup that precedes a save the operator may have to put back is not
    a retention candidate, whatever `backup_keep` says (Q1)."""
    def refuse(*_a, **_kw):
        raise safety.Refused("metadata: pretend step 10 failed")

    monkeypatch.setattr(safety, "update_metadata", refuse)
    with pytest.raises(safety.Refused):
        _apply(write_lab)
    folder = _only_backup_folder(write_lab)
    assert safety.read_manifest(folder)["outcome"] == \
        safety.OUTCOME_REFUSED_AFTER_WRITE
    assert safety.prune_backups(write_lab["backups"], 1) == [], \
        "retention leaves it alone even at keep=1"
    assert os.path.isdir(folder), "and it is still there"


def test_a_refusal_before_the_write_still_says_refused(write_lab,
                                                       no_game_running):
    """The other half of the same rule: a run that really did change nothing
    keeps the word that means that, and stays prunable."""
    meta = os.path.join(os.path.dirname(write_lab["target"]), "mf_save9.hg")
    with open(meta, "wb") as fh:
        fh.write(b"\x00" * 16)              # refuses at step 8
    with pytest.raises(safety.Refused):
        _apply(write_lab)
    man = safety.read_manifest(_only_backup_folder(write_lab))
    assert man["outcome"] == safety.OUTCOME_REFUSED
    assert _unchanged(write_lab)


def test_every_refusal_carries_the_report_it_refused_with(write_lab,
                                                          no_game_running):
    """`Refused.report` is what lets the server's 409 list the steps that
    passed. A refusal raised outside `Report.fail` -- this one comes out of
    `write_atomic` -- is given one by `apply_plan` (Q1)."""
    os.chmod(write_lab["target"], stat.S_IREAD)
    try:
        with pytest.raises(safety.Refused) as exc:
            _apply(write_lab)
    finally:
        os.chmod(write_lab["target"], stat.S_IWRITE | stat.S_IREAD)
    rep = exc.value.report
    assert rep is not None and rep.steps, \
        "the refusal carries the report: %r" % (rep,)
    assert any(s["label"] == "backup" and s["state"] == "ok"
               for s in rep.steps), \
        "including the steps that had passed: %s" % rep.steps


# --- Q2: what the sentence may promise ----------------------------------


def test_a_write_that_fails_before_anything_moved_promises_nothing_moved():
    exc = safety.refuse_oserror(OSError(13, "Permission denied"), "x.hg")
    assert str(exc).endswith("nothing was changed"), str(exc)


def test_a_write_that_fails_after_the_save_moved_says_what_is_on_disk():
    exc = safety.refuse_oserror(OSError(13, "Permission denied"), "mf_x.hg",
                                after_write=True)
    assert "nothing was changed" not in str(exc), str(exc)
    assert "the save was written but this file was not" in str(exc), str(exc)


# --- Q3: the manifest names files, not paths ----------------------------


@pytest.mark.parametrize("name", [
    os.path.join("..", "x.hg"), "sub/x.hg", "sub\\x.hg", "..", ".",
    "C:\\x.hg" if os.name == "nt" else "/x.hg", "", None])
def test_a_manifest_file_name_that_is_a_path_is_not_a_file_name(name):
    assert safety.is_plain_file_name(name) is False, \
        "%r names a place, not a file in the backup folder" % (name,)


def test_an_ordinary_save_name_is_a_file_name():
    assert safety.is_plain_file_name("save9.hg")
    assert safety.is_plain_file_name("mf_SAVE9.HG")


def test_list_backups_does_not_hash_a_file_outside_the_backup_folder(tmp_path):
    """`_verify_copy` joined the manifest's name against the folder too, so
    the listing hashed whatever the manifest pointed at (Q3)."""
    root = str(tmp_path / "backups")
    folder = os.path.join(root, "20260101-000000-save9")
    os.makedirs(folder)
    body = b"bytes beside the backup root"
    with open(os.path.join(root, "x.hg"), "wb") as fh:
        fh.write(body)
    with io.open(os.path.join(folder, safety.MANIFEST_NAME), "w",
                 encoding="utf-8") as fh:
        json.dump({"created": safety.utc_now(),
                   "outcome": safety.OUTCOME_WRITTEN, "completed": True,
                   "save": {"file": os.path.join("..", "x.hg"),
                            "sha256": hashlib.sha256(body).hexdigest(),
                            "size": len(body)},
                   "meta": None, "plan": {}, "config_sha256": "0" * 64}, fh)
    row = safety.list_backups(root)[0]
    assert row["sha256_ok"] is False, \
        "a manifest that points outside its folder does not verify: %s" % row
    assert any("outside this backup folder" in p for p in row["problems"]), \
        "and the listing says why: %s" % row["problems"]


# --- Q4: a backup is bound to the folder it came from -------------------


def test_the_manifest_records_the_folder_the_save_came_from(applied_lab):
    man = safety.read_manifest(applied_lab["res"]["backup"])
    assert man["save_dir"] == safety.normalised_folder(
        os.path.dirname(applied_lab["target"])), \
        "absolute and normalised: %r" % man["save_dir"]
    row = safety.list_backups(applied_lab["backups"])[0]
    assert row["save_dir"] == man["save_dir"], \
        "and the listing carries it, so the page can say so first: %s" % row


def test_a_restore_into_the_folder_it_came_from_is_not_refused(applied_lab):
    rep, _res = safety.restore(applied_lab["res"]["backup"],
                               os.path.dirname(applied_lab["target"]))
    assert all(s["state"] in ("ok", "info") for s in rep.steps), rep.steps


def test_a_restore_into_another_folder_can_be_forced_by_name(applied_lab,
                                                             tmp_path):
    """`force` exists for an operator who means it -- moving a save set
    between profiles is a real thing to want. The server does not expose it,
    so the only way here is to say so in code (Q4)."""
    other = str(tmp_path / "another-profile")
    os.makedirs(other)
    rep, res = safety.restore(applied_lab["res"]["backup"], other,
                              force=True)
    assert any("because force was asked for" in s["detail"]
               for s in rep.steps if s["state"] == "info"),         "and it is not silent about it: %s" % rep.steps
    assert res["restored"] == [make_fixture.SAVE_NAME,
                               "mf_" + make_fixture.SAVE_NAME]
    assert safety.sha256(os.path.join(other, make_fixture.SAVE_NAME)) == \
        safety.read_manifest(applied_lab["res"]["backup"])["save"]["sha256"]


def test_a_backup_with_no_recorded_folder_is_restored_with_a_line(applied_lab):
    """Backups written before the field existed are still the operator's own
    backups. The check cannot be made, so it says so rather than passing in
    silence (Q4)."""
    folder = applied_lab["res"]["backup"]
    man = safety.read_manifest(folder)
    del man["save_dir"]
    with io.open(os.path.join(folder, safety.MANIFEST_NAME), "w",
                 encoding="utf-8") as fh:
        json.dump(man, fh)
    rep, _res = safety.restore(folder, os.path.dirname(applied_lab["target"]))
    assert any("does not record which folder" in s["detail"]
               for s in rep.steps if s["state"] == "info"), rep.steps


# --- Q5: nothing prunes the folder a restore is reading -----------------


def test_a_backup_being_restored_from_is_not_pruned(tmp_path):
    root = str(tmp_path / "backups")
    os.makedirs(root)
    folders = [_stamped_backup(root, "20260911-12000%d" % n) for n in range(3)]
    with open(os.path.join(folders[0], safety.RESTORING_MARKER), "w",
              encoding="utf-8") as fh:
        fh.write("{}")
    assert safety.is_being_restored(folders[0])
    assert safety.prune_backups(root, 1) == [folders[1]], \
        "the folder a restore is reading from is left alone"
    assert os.path.isdir(folders[0])


def test_a_restore_marker_a_killed_restore_left_stops_being_believed(tmp_path):
    """Otherwise one crash makes a folder unprunable for ever."""
    root = str(tmp_path / "backups")
    os.makedirs(root)
    folder = _stamped_backup(root, "20260911-120000")
    marker = os.path.join(folder, safety.RESTORING_MARKER)
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write("{}")
    old = time.time() - (safety.RESTORING_MARKER_SECONDS + 60)
    os.utime(marker, (old, old))
    assert safety.is_being_restored(folder) is False


def test_a_restore_removes_its_marker(applied_lab):
    folder = applied_lab["res"]["backup"]
    safety.restore(folder, os.path.dirname(applied_lab["target"]))
    assert not os.path.exists(os.path.join(folder, safety.RESTORING_MARKER)), \
        "the marker is removed in the `finally`: %s" % os.listdir(folder)


def test_a_backup_copy_that_cannot_be_hashed_is_not_restored_from(applied_lab,
                                                                  monkeypatch):
    """Every read on the restore path is a sentence, including the hash in
    step 2 (Q5)."""
    real = safety.sha256

    def unreadable(path):
        if path.startswith(applied_lab["res"]["backup"]):
            raise OSError(errno.EACCES, "Permission denied")
        return real(path)

    monkeypatch.setattr(safety, "sha256", unreadable)
    with pytest.raises(safety.Refused) as exc:
        safety.restore(applied_lab["res"]["backup"],
                       os.path.dirname(applied_lab["target"]))
    assert "could not be read from this backup" in str(exc.value), exc.value


# --- Q6: the plan is pinned to the bytes, not to the timestamp ----------


def test_a_backup_copy_whose_mtime_is_zero_still_passes_step_five(
        write_lab, no_game_running, monkeypatch):
    """The most hostile version of a backup root that does not preserve times:
    the copy's mtime is the epoch. The fingerprint is over the bytes, so step
    5 does not notice (Q6)."""
    real = shutil.copy2

    def copy_and_forget_the_time(src, dst, *a, **kw):
        real(src, dst, *a, **kw)
        os.utime(dst, (0, 0))

    monkeypatch.setattr(safety.shutil, "copy2", copy_and_forget_the_time)
    _rep, res = _apply(write_lab)
    assert res["rows"], "the apply ran"
    assert os.path.getmtime(os.path.join(res["backup"],
                                         make_fixture.SAVE_NAME)) == 0, \
        "the backup copy really did lose its timestamp"


def test_the_content_signature_is_the_same_for_a_copy(write_lab, tmp_path):
    a = SaveFile(write_lab["target"])
    copy = str(tmp_path / "elsewhere.hg")
    shutil.copyfile(write_lab["target"], copy)
    os.utime(copy, (0, 0))
    b = SaveFile(copy)
    assert a.content_signature().split("|")[1:] == \
        b.content_signature().split("|")[1:], \
        "size and hash, and nothing that a copy moves: %s vs %s" \
        % (a.content_signature(), b.content_signature())
    assert a.signature() != b.signature(), \
        "while `signature()` still notices, which is what the cache wants"


# --- Q7 / Q10: retention's ordering -------------------------------------


def test_retention_orders_by_the_parsed_stamp_not_by_mtime(tmp_path):
    """2026-11-01 in New York: the clock goes back at 02:00, so 01:00 to 02:00
    happens twice. The folder named `010000` was taken an hour *after* the one
    named `015900`, and this machine's mtimes say so -- and retention still
    goes by the stamp in the name, because mtime is what a copy or a sync
    client moves (Q7)."""
    root = str(tmp_path / "backups")
    os.makedirs(root)
    first = _stamped_backup(root, "20261101-015900")      # 01:59 EDT
    second = _stamped_backup(root, "20261101-010000")     # 01:00 EST, later
    now = time.time()
    os.utime(first, (now - 7200, now - 7200))
    os.utime(second, (now, now))
    removed = safety.prune_backups(root, 1)
    assert removed == [second], \
        "oldest by parsed stamp, whatever the mtimes say: %s" % removed
    assert os.path.isdir(first), "and the newest by parsed stamp is exempt"


def test_a_folder_whose_name_is_not_a_date_is_never_pruned(tmp_path, caplog):
    """A name that matches the shape and is not a time is not a time
    (`20261301-000000` is month 13), so its age cannot be established (Q7)."""
    root = str(tmp_path / "backups")
    os.makedirs(root)
    real = [_stamped_backup(root, "2026091%d-120000" % n) for n in (1, 2, 3)]
    odd = _stamped_backup(root, "20261301-000000")
    assert safety.parsed_stamp("20261301-000000") is None
    with caplog.at_level(logging.INFO, logger="nms_sorter.safety"):
        removed = safety.prune_backups(root, 2)
    assert odd not in removed and os.path.isdir(odd), removed
    assert removed == [real[0]], \
        "and it does not count towards keep: %s" % removed
    assert any("does not begin with a stamp" in m
               for m in [r.getMessage() for r in caplog.records]), \
        "with a line saying why: %s" % [r.getMessage() for r in caplog.records]


def test_list_backups_puts_a_renamed_folder_last_not_in_name_order(tmp_path):
    """The same ordering from the other side: "newest first" was a reverse
    sort of the names (Q7)."""
    root = str(tmp_path / "backups")
    os.makedirs(root)
    _stamped_backup(root, "20260911-120000")
    _stamped_backup(root, "20260912-120000")
    keep = os.path.join(root, "ZZ-keep-this-one")
    os.makedirs(keep)
    for fn in ("manifest.json", "save9.hg"):
        shutil.copy2(os.path.join(root, "20260911-120000-save9", fn),
                     os.path.join(keep, fn))
    names = [os.path.basename(r["folder"]) for r in safety.list_backups(root)]
    assert names == ["20260912-120000-save9", "20260911-120000-save9",
                     "ZZ-keep-this-one"], \
        "dated folders newest first, undated last: %s" % names


def test_the_newest_is_exempt_even_when_the_arithmetic_says_take_it():
    """Q10. The guard used to be a `continue` that could not run, because the
    slice above it already excluded `rows[-1]`. Now the exemption *is* the
    slice, and this is the case that proves it: a `keep` of zero, which the
    arithmetic would satisfy by deleting every folder."""
    rows = [(1, "oldest"), (2, "middle"), (3, "newest")]
    assert safety.prunable_folders(rows, 0) == ["oldest", "middle"], \
        "everything but the newest, even at keep=0"
    assert safety.prunable_folders(rows, 1) == ["oldest", "middle"]
    assert safety.prunable_folders(rows, 2) == ["oldest"]
    assert safety.prunable_folders(rows, 3) == []
    assert safety.prunable_folders([(1, "only")], 0) == [], \
        "one folder is the newest folder"


def test_a_folder_whose_run_never_settled_is_never_pruned(tmp_path):
    """`in progress` and `save written, metadata pending` both mean a save
    somebody may still have to recover (Q1)."""
    root = str(tmp_path / "backups")
    os.makedirs(root)
    keepers = []
    for i, outcome in enumerate((safety.OUTCOME_IN_PROGRESS,
                                 safety.OUTCOME_META_PENDING,
                                 safety.OUTCOME_REFUSED_AFTER_WRITE)):
        folder = _stamped_backup(root, "2026091%d-120000" % (i + 1))
        man = safety.read_manifest(folder)
        man["outcome"] = outcome
        with io.open(os.path.join(folder, "manifest.json"), "w",
                     encoding="utf-8") as fh:
            json.dump(man, fh)
        keepers.append(folder)
    _stamped_backup(root, "20260920-120000")
    _stamped_backup(root, "20260921-120000")
    removed = safety.prune_backups(root, 1)
    assert removed == [os.path.join(root, "20260920-120000-save9")], \
        "only the settled ones are candidates: %s" % removed
    assert all(os.path.isdir(f) for f in keepers)


# --- Q8: the reclaim leaves nothing behind ------------------------------


def test_the_sweep_takes_the_reclaim_leftovers_and_not_the_lock(tmp_path):
    save = str(tmp_path / "save9.hg")
    with open(save, "wb") as fh:
        fh.write(b"x")
    lp = safety.lock_path(save)
    names = [lp + safety.RECLAIM_SUFFIX, lp + ".stale-4242",
             save + ".nms-sorter-77.tmp", lp]
    long_ago = time.time() - 86400
    for p in names:
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("{}")
        os.utime(p, (long_ago, long_ago))
    removed = sorted(safety.sweep_temp_files(str(tmp_path)))
    assert removed == sorted(os.path.basename(p) for p in names[:3]), \
        "the two reclaim leftovers and the temp file: %s" % removed
    assert os.path.exists(lp), \
        "and never the lock itself, which is reclaimed by age and pid"


def test_a_young_reclaim_leftover_is_left_alone(tmp_path):
    save = str(tmp_path / "save9.hg")
    guard = safety.lock_path(save) + safety.RECLAIM_SUFFIX
    with open(guard, "w", encoding="utf-8") as fh:
        fh.write("{}")
    assert safety.sweep_temp_files(str(tmp_path)) == [], \
        "a guard written a moment ago may belong to a live recovery"


def test_a_reclaim_guard_from_a_dead_process_is_itself_reclaimable(tmp_path):
    """The ten-minute window Q8 named: a guard left by a kill made a stale
    lock unreclaimable, with a refusal that blamed a writer that was gone."""
    save = str(tmp_path / "save9.hg")
    guard = safety.lock_path(save) + safety.RECLAIM_SUFFIX
    with open(guard, "w", encoding="utf-8") as fh:
        json.dump({"pid": DEAD_PID, "created": "2026-09-14T00:00:00Z"}, fh)
    assert safety._guard_is_abandoned(guard) is True, \
        "a recovery whose process does not exist cannot finish"
    with open(guard, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "created": "2026-09-14T00:00:00Z"}, fh)
    assert safety._guard_is_abandoned(guard) is False, \
        "and a live one is left to finish"
    old = time.time() - (safety.STALE_LOCK_SECONDS + 60)
    os.utime(guard, (old, old))
    assert safety._guard_is_abandoned(guard) is True, \
        "with age as the fallback for a pid that cannot be trusted"


def test_a_reclaim_guard_with_no_pid_falls_back_to_age(tmp_path):
    save = str(tmp_path / "save9.hg")
    guard = safety.lock_path(save) + safety.RECLAIM_SUFFIX
    with open(guard, "w", encoding="utf-8") as fh:
        fh.write("not json at all")
    assert safety._guard_is_abandoned(guard) is False
    old = time.time() - (safety.STALE_LOCK_SECONDS + 60)
    os.utime(guard, (old, old))
    assert safety._guard_is_abandoned(guard) is True


def test_a_stale_lock_reclaim_leaves_no_leftovers(write_lab, no_game_running):
    """The happy path: a reclaim that runs to completion takes both of its own
    files with it."""
    lock = safety.lock_path(write_lab["target"])
    with open(lock, "w", encoding="utf-8") as fh:
        json.dump({"pid": DEAD_PID, "created": "2020-01-01T00:00:00Z"}, fh)
    old = time.time() - (safety.STALE_LOCK_SECONDS + 60)
    os.utime(lock, (old, old))
    _rep, res = _apply(write_lab)
    assert res["rows"], "the apply ran"
    left = sorted(f for f in os.listdir(os.path.dirname(write_lab["target"]))
                  if "nms-sorter" in f)
    assert left == [], "nothing of ours is left in the save folder: %s" % left


# --- Q9: the mf_ keeps the name the filesystem has ----------------------


def test_the_metadata_path_is_the_one_on_disk(tmp_path):
    save = str(tmp_path / "save9.hg")
    with open(save, "wb") as fh:
        fh.write(b"x")
    shouty = str(tmp_path / "mf_SAVE9.HG")
    with open(shouty, "wb") as fh:
        fh.write(b"y")
    assert safety.meta_path_for(save) == shouty, \
        "the spelling comes from the directory listing: %s" \
        % safety.meta_path_for(save)


def test_the_metadata_path_of_a_pair_that_is_not_there_is_the_plain_one(
        tmp_path):
    save = str(tmp_path / "save9.hg")
    assert safety.meta_path_for(save) == str(tmp_path / "mf_save9.hg"), \
        "nothing to resolve against is not an error: %s" \
        % safety.meta_path_for(save)


def test_the_backup_copies_the_metadata_under_the_name_it_has(write_lab,
                                                              no_game_running):
    folder = os.path.dirname(write_lab["target"])
    shouty = os.path.join(folder, "mf_SAVE9.HG")
    os.rename(os.path.join(folder, "mf_save9.hg"), shouty)
    if not os.path.exists(os.path.join(folder, "mf_save9.hg")):
        pytest.skip("a case-sensitive filesystem: there is no pair to confuse")
    _rep, res = _apply(write_lab)
    man = safety.read_manifest(res["backup"])
    assert man["meta"]["file"] == "mf_SAVE9.HG", \
        "the manifest records the real name, so a restore writes it back: %s" \
        % man["meta"]
    assert sorted(os.listdir(res["backup"])) == \
        ["manifest.json", "mf_SAVE9.HG", "save9.hg"], os.listdir(res["backup"])


# --- the two refusal sentences review two found unreached ---------------


def test_a_backup_root_that_cannot_be_created_is_a_sentence(write_lab,
                                                            no_game_running,
                                                            tmp_path):
    """`safety.py:585` in review two's list: `os.makedirs` of the backup
    folder. A backup root whose parent is a *file* cannot hold a folder."""
    blocker = str(tmp_path / "not-a-directory")
    with open(blocker, "wb") as fh:
        fh.write(b"this is a file, not a folder")
    write_lab["backups"] = os.path.join(blocker, "backups")
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert "backup: the save folder refused the write" in str(exc.value), \
        "the backup step names itself: %s" % exc.value
    assert str(exc.value).endswith("nothing was changed"), str(exc.value)
    assert _unchanged(write_lab), "and the save is untouched"


def test_a_save_that_cannot_be_copied_into_the_backup_is_a_sentence(
        write_lab, no_game_running, monkeypatch):
    """`safety.py:597`: `shutil.copy2` of either half. The failure a sync
    client holding the save actually produces, and it happens after the
    folder has been created."""
    def held_open(src, dst, *_a, **_kw):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(safety.shutil, "copy2", held_open)
    with pytest.raises(safety.Refused) as exc:
        _apply(write_lab)
    assert "backup: the save folder refused the write" in str(exc.value), \
        exc.value
    assert "save9.hg" in str(exc.value), \
        "and names the file it could not copy: %s" % exc.value
    assert _unchanged(write_lab)


# ===========================================================================
# step 1b: the version gate is advisory by default
# ===========================================================================
#
# DECISIONS.md, "A game update must not turn the tool off". The gate was a
# refusal and the refusal had no evidence under it: 4670, 4734 and 4735 have
# identical container layouts, and every break anybody can name -- Waypoint
# moving the player state under `BaseContext`, the 3.60 compression change, the
# `mf_` format going 2001 to 2004 -- is detected by shape somewhere else in
# this file. So the default records the version and carries on, and steps 4, 6
# and 7 are what actually protect the write. `strict_version_check` puts the
# refusal back for anyone who wants it.
#
# The expedition gate is not covered by the flag in either mode: it is a
# measured structural difference, not a number.

VERSION_STEP = "save is one this build was verified on"


def _variant_lab(variant, fixture_variant, tmp_path):
    """A `write_lab` over one of `tools/make_fixture.py`'s variants.

    Built by copying, not by pointing at the session-scoped fixture directory:
    an apply writes, and the variant is shared with every other test that asks
    for it.
    """
    built = fixture_variant(variant)
    target = str(tmp_path / os.path.basename(built["save"]))
    shutil.copy2(built["save"], target)
    shutil.copy2(built["meta"],
                 str(tmp_path / os.path.basename(built["meta"])))
    cfg = make_fixture.synthetic_config()
    lab = {"target": target, "cfg": cfg, "plan": None,
           "backups": str(tmp_path / "backups"),
           "before": open(target, "rb").read()}
    try:
        lab["plan"], _commit = planner.build_plan(SaveFile(target), cfg)
    except Exception:
        # An expedition never gets a plan; `build_plan` raises `SaveGate`
        # first. The apply is still driven, because step 1b runs before the
        # fingerprint is compared and the sentence it refuses with is the
        # thing under test.
        lab["plan"] = None
    return lab


@pytest.fixture
def version_lab(fixture_variant, tmp_path):
    return _variant_lab("version-4800", fixture_variant, tmp_path)


@pytest.fixture
def expedition_lab(fixture_variant, tmp_path):
    return _variant_lab("expedition", fixture_variant, tmp_path)


def _version_step(rep):
    steps = [s for s in rep.steps if s["label"] == VERSION_STEP]
    assert steps, "step 1b ran: %s" % [s["label"] for s in rep.steps]
    return steps[0]


def test_a_version_outside_the_range_applies_and_says_so(version_lab,
                                                         no_game_running):
    """The default. The step is `info`, not `ok`: the apply happened on a
    version nothing was measured on and the report has to say which."""
    rep, res = _apply(version_lab,
                      fingerprint=version_lab["plan"].fingerprint_full)
    step = _version_step(rep)
    assert step["state"] == "info", \
        "the version is recorded rather than passed over: %s" % step
    assert step["detail"] == (
        "this save reports version 4800, outside the range this build was "
        "verified on (4670 to 4735); continuing, because the round-trip and "
        "nothing-else-changed checks run on this file regardless"), step
    assert res["rows"], "and the apply ran: %s" % res


def test_an_unverified_version_still_conserves_every_item(version_lab,
                                                          no_game_running):
    """The claim the default rests on: steps 4, 6 and 7 ran on this file, so
    the write is the same write any other version gets."""
    _rep, res = _apply(version_lab,
                       fingerprint=version_lab["plan"].fingerprint_full)
    backup = os.path.join(res["backup"], os.path.basename(version_lab["target"]))
    assert _census(SaveFile(backup)) == _census(SaveFile(version_lab["target"])), \
        "per-item totals across sortable containers are unchanged"


def test_strict_version_check_refuses_with_the_documented_sentence(
        version_lab, no_game_running):
    with pytest.raises(safety.Refused) as exc:
        _apply(version_lab, fingerprint=version_lab["plan"].fingerprint_full,
               strict_version_check=True)
    assert "this save reports version 4800; this build was verified on 4670 " \
           "to 4735, so apply is refused until a fixture for 4800 exists" \
           in str(exc.value), \
        "the strict refusal is the sentence the documents quote: %s" % exc.value
    assert _unchanged(version_lab), "and nothing is written"


def test_the_strict_refusal_comes_before_the_backup(version_lab,
                                                    no_game_running):
    """Step 1b is before step 2 on purpose: a save this build will not write to
    is a save nothing should be copied for."""
    with pytest.raises(safety.Refused):
        _apply(version_lab, fingerprint=version_lab["plan"].fingerprint_full,
               strict_version_check=True)
    assert not os.path.isdir(version_lab["backups"]), \
        "there is not even a backup folder"


@pytest.mark.parametrize("strict", [False, True])
def test_an_expedition_is_refused_in_both_modes(expedition_lab,
                                                no_game_running, strict):
    """`strict_version_check` is about a version number. The expedition gate is
    a measured structural difference -- the live inventory may be the season
    copy -- and no flag here touches it."""
    assert expedition_lab["plan"] is None, \
        "an expedition never gets a plan in the first place"
    with pytest.raises(safety.Refused) as exc:
        safety.apply_plan(expedition_lab["target"], expedition_lab["cfg"],
                          "0" * 64, planner.build_plan,
                          expedition_lab["backups"],
                          strict_version_check=strict)
    assert "this is an expedition save" in str(exc.value), \
        "refused as an expedition, not as a fingerprint: %s" % exc.value
    assert open(expedition_lab["target"], "rb").read() == \
        expedition_lab["before"], "and nothing is written"
