"""P1-8: the failing test behind every finding of the first write-path review.

One test per confirmed finding of `docs/reviews/2026-09-14-write-path-review-1.md`.
Each arrived `xfail(strict=True)` and each is now a plain test: the reviewer left a
failing test behind rather than an opinion, and the marks came off in the change
that made them pass. They stay here, under the finding numbers, as the regression
suite for the fixes -- a file of defects nobody can quietly re-introduce.

Nothing here touches a real save. Every case copies the synthetic fixture into a
pytest temp directory, or builds one from `tools/make_fixture`.

Five findings arrived with no test here, because a test would have had to invent
the API the fix chose: R8 `backup_keep` had no reader, R12 the dead 0x38..0x3F
assertion, R13 `codec.meta_update_sizes`, R16 the post-commit container map and
R18 the temp file nothing swept. All five are now covered in
`tests/test_safety.py`, under the headings that name them.
"""
import json
import os
import stat
import threading
import time

import pytest

from nms_sorter import codec
from nms_sorter import config as cfgmod
from nms_sorter import planner, safety, server
from nms_sorter.app import App
from nms_sorter.savemodel import SaveFile, keymap
from tools import make_fixture


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _lab(tmp_path, variant="base"):
    """A save, its mf_, a config and the plan an apply of it would need."""
    fx = make_fixture.write_fixture(str(tmp_path), variant)
    cfg = make_fixture.synthetic_config()
    plan, _commit = planner.build_plan(SaveFile(fx["save"]), cfg)
    return {"target": fx["save"], "cfg": cfg, "plan": plan,
            "meta": os.path.join(str(tmp_path), "mf_" + make_fixture.SAVE_NAME),
            "backups": os.path.join(str(tmp_path), "backups"),
            "before": open(fx["save"], "rb").read()}


def _apply(lab, fingerprint=None, **kw):
    return safety.apply_plan(lab["target"], lab["cfg"],
                             fingerprint or lab["plan"].fingerprint_full,
                             planner.build_plan, lab["backups"], **kw)


def _only_backup(lab):
    folders = [os.path.join(lab["backups"], f)
               for f in sorted(os.listdir(lab["backups"]))]
    assert len(folders) == 1, "one apply, one backup folder: %s" % folders
    return folders[0]


# --------------------------------------------------------------------------
# R1 the stale-lock removal is not atomic
# --------------------------------------------------------------------------

def test_two_writers_cannot_both_hold_the_lock(tmp_path):
    """`O_EXCL` decides who holds the lock, but the stale branch is a check,
    then an unlink, then a create. Two writers that both read the same stale
    lock can both end up inside `held_lock`: the second one to unlink removes
    the *live* lock the first one had just created.

    The interleaving below is the one a scheduler produces on its own; the
    events only make it deterministic.
    """
    dead = 999999999
    if safety._pid_alive(dead):
        pytest.skip("this host cannot prove pid %d is gone, so no lock is ever "
                    "stale here and there is no stale branch to race (a faked "
                    "platform on a Windows box)" % dead)
    target = str(tmp_path / make_fixture.SAVE_NAME)
    with open(target, "wb") as fh:
        fh.write(b"not a real save; held_lock never reads it")
    lock = safety.lock_path(target)
    with open(lock, "w", encoding="utf-8") as fh:
        json.dump({"pid": dead, "created": "2020-01-01T00:00:00Z"}, fh)
    old = time.time() - (safety.STALE_LOCK_SECONDS + 60)
    os.utime(lock, (old, old))

    decided = threading.Event()       # A has decided the lock is stale
    b_in = threading.Event()          # B is inside, holding its own lock
    held, errors = [], []
    seen = []
    real_alive = safety._pid_alive
    guard = threading.Lock()

    def pausing_pid_alive(pid):
        answer = real_alive(pid)
        with guard:
            first = not seen
            seen.append(pid)
        if first:                     # A: descheduled before it unlinks
            decided.set()
            b_in.wait(10)
        return answer

    def run(tag):
        if tag == "B":
            decided.wait(10)
        try:
            with safety.held_lock(target):
                held.append(tag)
                if tag == "B":
                    b_in.set()
                time.sleep(0.3)
        except safety.Refused as exc:
            errors.append((tag, str(exc)))
            if tag == "B":
                b_in.set()

    safety._pid_alive = pausing_pid_alive
    try:
        threads = [threading.Thread(target=run, args=(t,)) for t in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
    finally:
        safety._pid_alive = real_alive

    assert len(held) == 1, (
        "one writer per save: %s both got inside held_lock (refusals: %s)"
        % (held, errors))


# --------------------------------------------------------------------------
# R2 / R3 the manifest is written last, so a broken write is undescribed
# --------------------------------------------------------------------------

def _die_between_the_two_writes(monkeypatch):
    """Kill the apply after the save has been replaced and before the mf_ is."""
    def boom(*_a, **_kw):
        raise KeyboardInterrupt("the machine lost power here")
    monkeypatch.setattr(safety, "update_metadata", boom)


def test_a_backup_of_a_write_that_died_still_carries_a_manifest(
        tmp_path, no_game_running, monkeypatch):
    """Step 12 writes the manifest, so any failure after step 2 leaves the
    backup folder undescribed -- and the failure that needs a restore most is
    the one between the save write and the mf_ write. Nothing in the manifest
    depends on the write having succeeded."""
    lab = _lab(tmp_path)
    _die_between_the_two_writes(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        _apply(lab)
    folder = _only_backup(lab)
    man = safety.read_manifest(folder)
    assert man is not None, (
        "the backup that could undo this half-written pair has no manifest, "
        "so the restore path cannot verify it: %s" % sorted(os.listdir(folder)))
    assert man["save"]["sha256"] == safety.sha256(
        os.path.join(folder, make_fixture.SAVE_NAME))


def test_a_backup_whose_write_did_not_finish_says_so(
        tmp_path, no_game_running, monkeypatch):
    """A save written beside an mf_ that still carries the old sizes is a pair
    that disagrees. Something on disk has to say the write was interrupted, or
    the next run cannot tell a finished apply from an abandoned one."""
    lab = _lab(tmp_path)
    _die_between_the_two_writes(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        _apply(lab)
    meta = codec.meta_read(lab["meta"])
    assert meta["size_disk"] != os.path.getsize(lab["target"]), \
        "the pair really does disagree, or this test proves nothing"
    folder = _only_backup(lab)
    names = set(os.listdir(folder))
    man = safety.read_manifest(folder) or {}
    said = (man.get("completed") is False
            or "pending.json" in names
            or any(n.endswith(".unfinished") for n in names))
    assert said, (
        "nothing in %s marks the write as unfinished: %s"
        % (folder, sorted(names)))


# --------------------------------------------------------------------------
# R4 a save with no mf_ is refused by an endswith() that matches too much
# --------------------------------------------------------------------------

def test_a_save_with_no_metadata_file_can_still_be_sorted(
        tmp_path, no_game_running):
    """`src.endswith(name)` is true for `mf_save9.hg` as well as `save9.hg`, so
    a missing mf_ is reported as a missing *save* and refused. Step 8 and step
    10 both carry a branch for the no-mf_ case; neither can be reached."""
    lab = _lab(tmp_path)
    os.unlink(lab["meta"])
    rep, res = _apply(lab)
    labels = dict((s["label"], s) for s in rep.steps)
    assert labels["backup"]["state"] == "ok"
    assert res["rows"], "the save sorts with no metadata beside it"


# --------------------------------------------------------------------------
# R5 step 7 translates container paths through `rev` alone
# --------------------------------------------------------------------------

def test_a_save_whose_keys_are_in_the_clear_is_not_all_stray_changes(
        tmp_path, no_game_running):
    """`Doc.key` resolves a plain name to whichever key is present, which is
    why `Chest11Inventory` works. Step 7 does not: it maps every step of a
    container path through `rev`, so on a document written with plain keys the
    guard compares obfuscated paths against plain ones and calls every change
    a stray."""
    fwd, _rev = keymap()
    plain = codec.remap(make_fixture.build_doc("base"), fwd)
    payload = codec.dumps(plain)
    target = str(tmp_path / make_fixture.SAVE_NAME)
    with open(target, "wb") as fh:
        fh.write(codec.frame_payload(payload))
    with open(str(tmp_path / ("mf_" + make_fixture.SAVE_NAME)), "wb") as fh:
        fh.write(make_fixture.build_meta(len(payload), os.path.getsize(target),
                                         make_fixture.SAVE_NAME, "base"))
    cfg = make_fixture.synthetic_config()
    plan, _commit = planner.build_plan(SaveFile(target), cfg)
    assert plan.rows, "the planner reads a plain-key save fine"
    rep, res = safety.apply_plan(target, cfg, plan.fingerprint_full,
                                 planner.build_plan,
                                 str(tmp_path / "backups"))
    assert res["rows"], "and the write path has to agree with it"


# --------------------------------------------------------------------------
# R6 the client's spelling of the filename becomes the file's name
# --------------------------------------------------------------------------

def test_the_save_folder_is_matched_case_insensitively(tmp_path):
    """Windows is case-insensitive, so `SAVE9.HG` names the same file as
    `save9.hg` -- and `os.replace` onto the first spelling renames the
    operator's save and its mf_. `app.save` has to answer with the name the
    filesystem actually has, and must not refuse a folder whose case differs
    from the one in the settings file."""
    folder = tmp_path / "saves"
    folder.mkdir()
    make_fixture.write_fixture(str(folder), "base")
    app = App(str(folder), str(tmp_path / "config.json"),
              str(tmp_path / "backups"),
              settings_path=str(tmp_path / "settings.json"))
    shouty = app.save(make_fixture.SAVE_NAME.upper())
    assert os.path.basename(shouty.path) == make_fixture.SAVE_NAME, \
        ("the write path is handed the spelling on disk, not the one the "
         "client typed: %s" % shouty.path)
    same = app.save(os.path.join(str(folder).upper(), make_fixture.SAVE_NAME))
    assert os.path.normcase(same.path) == os.path.normcase(
        os.path.join(str(folder), make_fixture.SAVE_NAME))


# --------------------------------------------------------------------------
# R7 an ordinary filesystem failure is not a sentence
# --------------------------------------------------------------------------

def test_a_read_only_save_is_refused_in_words(tmp_path, no_game_running):
    """"Every refusal explains itself in words" (GOAL 1.9). A save the operator
    cannot write -- read-only attribute, a folder a sync client holds -- comes
    back as a bare PermissionError, which is an HTTP 500 and a traceback."""
    lab = _lab(tmp_path)
    os.chmod(lab["target"], stat.S_IREAD)
    try:
        try:
            with open(lab["target"], "ab"):
                pass
        except OSError:
            pass
        else:
            pytest.skip("this filesystem does not enforce the read-only bit")
        with pytest.raises(safety.Refused) as exc:
            _apply(lab)
        assert lab["target"] in str(exc.value) or "read-only" in str(exc.value)
    finally:
        # Both bits: `S_IWRITE` alone is "clear the read-only attribute" on
        # Windows and "owner may write and may *not* read" on Linux, where it
        # made the check below a PermissionError of its own.
        os.chmod(lab["target"], stat.S_IWRITE | stat.S_IREAD)
    assert open(lab["target"], "rb").read() == lab["before"]


def test_a_full_disk_is_refused_in_words(tmp_path, no_game_running,
                                         monkeypatch):
    lab = _lab(tmp_path)

    def no_space(_fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(safety.os, "fsync", no_space)
    with pytest.raises(safety.Refused) as exc:
        _apply(lab)
    assert "space" in str(exc.value).lower()
    assert open(lab["target"], "rb").read() == lab["before"]


# --------------------------------------------------------------------------
# R9 list_backups verifies half of the pair
# --------------------------------------------------------------------------

def test_list_backups_checks_the_metadata_copy_too(tmp_path, no_game_running):
    """`sha256_ok` covers the save and nothing else, so a folder whose mf_ copy
    has rotted reports itself as verified. A restore driven off that field
    would put a good save beside metadata that denies it."""
    lab = _lab(tmp_path)
    _rep, res = _apply(lab)
    mf = os.path.join(res["backup"], "mf_" + make_fixture.SAVE_NAME)
    with open(mf, "r+b") as fh:
        fh.seek(0)
        fh.write(b"\x00\x00\x00\x00")
    row = safety.list_backups(lab["backups"])[0]
    assert row["sha256_ok"] is not True, (
        "a backup whose metadata copy has moved is not a verified backup: %s"
        % row)


# --------------------------------------------------------------------------
# R10 the server forwards the client's choice of check strength
# --------------------------------------------------------------------------

def test_do_apply_compares_the_full_digest_it_minted(tmp_path, monkeypatch):
    """GOAL 3.9: "the first 8 hex characters are shown to the person, the full
    digest is compared". The server passes back whatever token the client sent,
    and `_fingerprint_matches` compares the prefix when that is what arrived,
    so a client that sends the short form silently downgrades the gate to 32
    bits. The server already knows the full digest -- it minted it."""
    folder = tmp_path / "saves"
    folder.mkdir()
    make_fixture.write_fixture(str(folder), "base")
    app = App(str(folder), str(tmp_path / "config.json"),
              str(tmp_path / "backups"),
              settings_path=str(tmp_path / "settings.json"))
    app.config = make_fixture.synthetic_config()
    app.sync_taxonomy()

    handler = server.Handler.__new__(server.Handler)
    handler.app = app
    plan = handler.make_plan({"file": make_fixture.SAVE_NAME})
    assert len(plan["fingerprint"]) == 8 and len(plan["fingerprint_full"]) == 64

    seen = {}

    def spy(save_path, cfg, expected, *a, **kw):
        seen["expected"] = expected
        return safety.Report(), {"rows": 0}

    monkeypatch.setattr(server.safety, "apply_plan", spy)
    handler.do_apply({"file": make_fixture.SAVE_NAME,
                      "fingerprint": plan["fingerprint"]})
    assert seen["expected"] == plan["fingerprint_full"], (
        "the write path was handed %r, so the gate it applied was 32 bits wide"
        % seen["expected"])


# --------------------------------------------------------------------------
# R11 two absent fingerprints compare equal
# --------------------------------------------------------------------------

def test_an_unfingerprinted_plan_is_never_a_match():
    """`expected or ""` against `plan.fingerprint or ""` makes "nobody
    approved anything" a match for "this plan has no fingerprint"."""
    class Unfingerprinted(object):
        fingerprint = None
        fingerprint_full = None

    matched, _compared = safety._fingerprint_matches(Unfingerprinted(), None)
    assert not matched, "no fingerprint on either side is not a match"


# --------------------------------------------------------------------------
# R14 the config writer shares one temp name between processes
# --------------------------------------------------------------------------

def test_the_config_temp_file_carries_the_pid(tmp_path, monkeypatch):
    """`safety.temp_path` puts the pid in the name so two writers cannot share
    a temp file. `config.store` writes `config.json.tmp` flat, and two sorters
    started against one state directory is the ordinary case, not the odd
    one."""
    path = str(tmp_path / "config.json")
    seen = []
    real_open = cfgmod.open if hasattr(cfgmod, "open") else open

    def spy_open(name, *a, **kw):
        if str(name).endswith(".tmp"):
            seen.append(os.path.basename(str(name)))
        return real_open(name, *a, **kw)

    monkeypatch.setitem(cfgmod.__dict__, "open", spy_open)
    cfgmod.store(path, cfgmod.default_config())
    assert seen, "store() went through a temp file: %s" % seen
    assert all(str(os.getpid()) in n for n in seen), \
        "the temp name has to carry the pid: %s" % seen


# --------------------------------------------------------------------------
# R15 the save cache turns on mtime alone
# --------------------------------------------------------------------------

def test_a_same_second_same_size_change_reloads_the_save(tmp_path):
    """The cache is refreshed when `st_mtime` moves. The signature a plan is
    pinned to is `name|size|int(mtime)`, and a change that keeps all three --
    a same-length edit written back with the original timestamp -- leaves the
    page and the plan describing bytes that are no longer on disk."""
    folder = tmp_path / "saves"
    folder.mkdir()
    make_fixture.write_fixture(str(folder), "base")
    app = App(str(folder), str(tmp_path / "config.json"),
              str(tmp_path / "backups"),
              settings_path=str(tmp_path / "settings.json"))
    first = app.save(make_fixture.SAVE_NAME, reload=True)
    target = first.path
    was = os.stat(target)
    edited = SaveFile(target)
    edited.d.set(edited.d.player, "Units", 12344)      # same number of digits
    payload = codec.dumps(edited.doc)
    framed = codec.frame_payload(payload)
    assert len(framed) == was.st_size, "the same size, or this proves nothing"
    with open(target, "wb") as fh:
        fh.write(framed)
    os.utime(target, (was.st_atime, was.st_mtime))
    again = app.save(make_fixture.SAVE_NAME)
    assert again.d.get(again.d.player, "Units") == 12344, \
        "the cached save is the one on disk, not the one that used to be"


# --------------------------------------------------------------------------
# R17 structural_diff compares values, and the guard claims bytes
# --------------------------------------------------------------------------

def test_structural_diff_sees_a_bool_that_became_an_int():
    """`true` and `1` are different bytes in the payload and `True == 1` in
    Python, and the int/float escape in the type check lets the pair through to
    a plain `!=`."""
    assert safety.structural_diff({"IsCool": True}, {"IsCool": 1}) == ["IsCool"]


def test_structural_diff_sees_an_int_that_became_a_float():
    """`0` and `0.0` likewise. `Slot.amount`'s setter coerces with `int()`, so
    a save that spelled an amount as a float would be rewritten with a
    different byte at a path step 7 reports as unchanged."""
    assert safety.structural_diff({"Amount": 0}, {"Amount": 0.0}) == ["Amount"]
