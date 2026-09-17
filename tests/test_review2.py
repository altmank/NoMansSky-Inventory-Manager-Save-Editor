"""Write-path review two (P6-4): one test per confirmed finding.

Written as twelve `xfail(strict=True)` reproductions of
`docs/reviews/2026-09-14-write-path-review-2.md`, one per finding, so the suite
stayed green while the findings stood and turned red the moment one was fixed
without the report being updated. P6-5 fixed all twelve and removed the
markers, which is why they are ordinary tests now: each one is the regression
test for its finding, and its docstring is still the review's account of what
was wrong. Q10 and Q14 had no test here -- Q10's is
`test_safety.py::test_the_newest_is_exempt_even_when_the_arithmetic_says_take_it`
and Q14 removed code rather than adding behaviour.

Nothing here reads the operator's save folder. The save is the synthetic
fixture, copied into a pytest temp directory, and the one test that needs a
real 2.8 MB document is in the report as a measurement rather than an
assertion.
"""
import hashlib
import io
import json
import os
import shutil
import time

import pytest

from nms_sorter import codec
from nms_sorter import config as cfgmod
from nms_sorter import planner, safety
from nms_sorter.savemodel import Doc, SaveFile
from tools import make_fixture


# --------------------------------------------------------------- the lab


@pytest.fixture
def lab(synthetic_path, tmp_path):
    """A save, its `mf_`, a backup root, and the plan an apply would need.

    The save's modification time is set a month back, because two of the
    findings below are about what happens to `int(st_mtime)` between the
    original and the backup copy, and a fixture written seconds ago hides a
    whole-second difference behind the second it was written in.
    """
    target = str(tmp_path / make_fixture.SAVE_NAME)
    meta = str(tmp_path / ("mf_" + make_fixture.SAVE_NAME))
    shutil.copy2(synthetic_path, target)
    payload = SaveFile(target).payload
    with open(meta, "wb") as fh:
        fh.write(make_fixture.build_meta(len(payload), os.path.getsize(target),
                                         make_fixture.SAVE_NAME, "base"))
    old = time.time() - 86400 * 30
    for p in (target, meta):
        os.utime(p, (old, old))
    cfg = make_fixture.synthetic_config()
    plan, _commit = planner.build_plan(SaveFile(target), cfg)
    return {"dir": str(tmp_path), "target": target, "meta": meta, "cfg": cfg,
            "plan": plan, "backups": str(tmp_path / "backups"),
            "before": open(target, "rb").read()}


def _apply(lab, fingerprint=None, **kw):
    return safety.apply_plan(lab["target"], lab["cfg"],
                             fingerprint or lab["plan"].fingerprint,
                             planner.build_plan, lab["backups"], **kw)


def _changed(lab):
    return open(lab["target"], "rb").read() != lab["before"]


def _only_backup(lab):
    names = sorted(os.listdir(lab["backups"]))
    assert len(names) == 1, "one apply, one backup folder: %s" % names
    return os.path.join(lab["backups"], names[0])


def _backup_folder(root, name, body=b"pretend this is a save",
                   save="save9.hg"):
    """A backup folder good enough for `prune_backups` and `list_backups`."""
    folder = os.path.join(root, name)
    os.makedirs(folder)
    with open(os.path.join(folder, save), "wb") as fh:
        fh.write(body)
    with io.open(os.path.join(folder, safety.MANIFEST_NAME), "w",
                 encoding="utf-8") as fh:
        json.dump({"created": safety.utc_now(),
                   "outcome": safety.OUTCOME_WRITTEN, "completed": True,
                   "save": {"file": save,
                            "sha256": hashlib.sha256(body).hexdigest(),
                            "size": len(body)},
                   "meta": None, "plan": {"rows": 1, "containers": []},
                   "config_sha256": "0" * 64}, fh)
    return folder


# ===========================================================================
# Q1 -- a refusal after the save has been replaced is recorded as "refused"
# ===========================================================================


def test_a_refusal_after_the_write_does_not_say_the_backup_changed_nothing(
        lab, no_game_running, monkeypatch):
    """`OUTCOME_REFUSED` is documented as "a run that stopped after the backup
    was taken and changed nothing", and `_apply_locked` writes it for *every*
    `Refused` after step 2 -- including the six that can only fire at steps 10
    and 11, after `os.replace` has already put the new save on disk. The
    `save written, metadata pending` outcome that recorded the truth is
    overwritten, `rows` and `containers` are reset to null, and the 409 the
    server answers with carries only the last sentence, so the manifest is the
    only machine-readable record of an apply and it says the opposite.
    """
    real = codec.meta_read

    def off_by_one(path):
        m = dict(real(path))
        m["size_disk"] += 1
        return m

    monkeypatch.setattr(safety, "meta_read", off_by_one)
    with pytest.raises(safety.Refused):
        _apply(lab)
    assert _changed(lab), "the save was replaced at step 9, or this proves nothing"
    man = safety.read_manifest(_only_backup(lab))
    assert man["outcome"] != safety.OUTCOME_REFUSED, (
        "the save on disk is the new one, so the backup that precedes it must "
        "not be described as a run that changed nothing: outcome %r, "
        "completed %r, rows %r"
        % (man["outcome"], man["completed"], man["plan"]["rows"]))


# ===========================================================================
# Q2 -- "nothing was changed" survives the first os.replace
# ===========================================================================


def test_a_write_that_fails_after_the_save_landed_does_not_claim_otherwise(
        lab, no_game_running, monkeypatch):
    """`refuse_oserror` ends every sentence with "nothing was changed", and its
    docstring justifies that by "both are also *before* the `os.replace`".
    True of step 9; not true of step 10, which calls `write_atomic` on the
    `mf_` after the save has been replaced, and not true of `restore`, which
    calls it twice.
    """
    real_replace = os.replace

    def fails_on_the_metadata(src, dst, *a, **kw):
        if os.path.basename(dst).startswith("mf_"):
            raise OSError(13, "Permission denied")
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(safety.os, "replace", fails_on_the_metadata)
    with pytest.raises(safety.Refused) as exc:
        _apply(lab)
    assert _changed(lab), "the save was replaced at step 9, or this proves nothing"
    assert "nothing was changed" not in str(exc.value), (
        "the save had already been written when this failed, so the refusal "
        "must not end with that promise: %s" % exc.value)


# ===========================================================================
# Q3 -- restore writes wherever the manifest points
# ===========================================================================


def test_restore_refuses_a_manifest_that_names_a_path_not_a_file(tmp_path,
                                                                 no_game_running):
    """`save["file"]` goes straight into `os.path.join` twice: once against the
    backup folder for the hash check and once against the save folder for the
    write. Neither is a basename, so a `..` in it reaches out of both, and all
    four steps report `ok` while a file that is not in the save folder at all
    is replaced with the backup's bytes.

    `server._backup_folder` does not help: it checks the folder the client
    names, and this path is inside the manifest.
    """
    root = str(tmp_path / "backups")
    folder = os.path.join(root, "20260101-000000-save9")
    os.makedirs(folder)
    saves = str(tmp_path / "saves")
    os.makedirs(saves)

    # one string, two meanings: `<backups>/x.hg` is what the hash check reads,
    # `<tmp>/x.hg` -- outside the save folder -- is what the write lands on.
    escape = os.path.join("..", "x.hg")
    victim = str(tmp_path / "x.hg")
    with open(victim, "wb") as fh:
        fh.write(b"a file of the operator's that is not a save")
    keep = open(victim, "rb").read()
    body = b"bytes from a crafted backup"
    with open(os.path.join(root, "x.hg"), "wb") as fh:
        fh.write(body)

    with io.open(os.path.join(folder, safety.MANIFEST_NAME), "w",
                 encoding="utf-8") as fh:
        json.dump({"created": safety.utc_now(),
                   "outcome": safety.OUTCOME_WRITTEN, "completed": True,
                   "save": {"file": escape,
                            "sha256": hashlib.sha256(body).hexdigest(),
                            "size": len(body)},
                   "meta": None, "plan": {}, "config_sha256": "0" * 64}, fh)

    try:
        safety.restore(folder, saves)
    except safety.Refused:
        pass
    assert open(victim, "rb").read() == keep, (
        "a restore writes into the save folder or it refuses; it does not "
        "write to %s because a manifest told it to" % victim)


# ===========================================================================
# Q4 -- restore is not bound to the folder the backup was taken from
# ===========================================================================


def test_restore_refuses_a_save_folder_the_backup_did_not_come_from(
        lab, no_game_running, tmp_path):
    """The manifest records the save's *name* and nothing about where it lived,
    and `/api/restore` hands `restore` whatever folder is selected now. So a
    backup of one profile's `save9.hg` is put over another profile's
    `save9.hg`, with `verify the backup` and `re-read from disk` both reporting
    that every byte matches -- which is true, and of the wrong file.
    """
    _apply(lab)
    backup = _only_backup(lab)
    other = str(tmp_path / "another-profile")
    os.makedirs(other)
    make_fixture.write_fixture(other, "twelve-chests")
    victim = os.path.join(other, make_fixture.SAVE_NAME)
    keep = open(victim, "rb").read()

    try:
        safety.restore(backup, other)
    except safety.Refused:
        pass
    assert open(victim, "rb").read() == keep, (
        "this backup was taken from %s; putting it into %s overwrote a "
        "different save of the same name" % (lab["dir"], other))


# ===========================================================================
# Q5 -- restore reads its source with a bare open
# ===========================================================================


def test_a_backup_copy_that_cannot_be_read_is_a_sentence_not_a_traceback(
        lab, no_game_running, monkeypatch):
    """Nothing locks the backup root: `prune_backups` runs outside the save
    lock (`server.do_apply`) and `rmtree`s folders a restore in another process
    may be halfway through. The verify and the read are two separate passes
    over the same files, and the read is `open(src, "rb")` with no handler, so
    the second file vanishing is a `FileNotFoundError` -- a 500 with a
    reference number -- after the first file has already been written back.
    """
    _apply(lab)
    backup = _only_backup(lab)
    real = safety.write_atomic

    def prune_the_rest(path, data, *a, **kw):
        # `*a, **kw`: the seam takes `what` and `after_write` now (Q2).
        real(path, data, *a, **kw)
        if not os.path.basename(path).startswith("mf_"):
            os.unlink(os.path.join(backup, "mf_" + make_fixture.SAVE_NAME))

    monkeypatch.setattr(safety, "write_atomic", prune_the_rest)
    raised = None
    try:
        safety.restore(backup, lab["dir"])
    except Exception as exc:                                # noqa: BLE001
        raised = exc
    assert isinstance(raised, safety.Refused), (
        "a backup copy that cannot be read is an ordinary filesystem failure "
        "and every other one in this module is a sentence: got %r"
        % (raised,))


# ===========================================================================
# Q6 -- the step-5 gate depends on the backup preserving st_mtime
# ===========================================================================


def test_an_apply_survives_a_backup_root_that_does_not_keep_timestamps(
        lab, no_game_running, monkeypatch):
    """Step 5 re-plans from the backup copy, and the fingerprint preimage
    carries `save.signature()`, which is
    `name|size|int(st_mtime)|sha256`. `shutil.copy2` is what puts the
    original's mtime on the copy -- so a backup folder on exFAT (two-second
    granularity), on a network share, or under a sync client that stamps its
    own time makes the recomputed fingerprint differ from the approved one on
    every single run. The save is untouched, which is the safe direction, but
    it is unsortable for good and the sentence says "Re-run the plan", which
    can never help.
    """
    monkeypatch.setattr(safety.shutil, "copy2",
                        lambda src, dst: shutil.copyfile(src, dst))
    _apply(lab)
    assert _changed(lab), "the apply ran"


# ===========================================================================
# Q7 -- retention orders backups by the folder name, not by time
# ===========================================================================


def test_a_backup_folder_the_operator_renamed_is_not_the_first_one_deleted(
        tmp_path):
    """`prune_backups` sorts on `STAMP_RE.match(name)` and substitutes `""`
    when a folder name does not start with a stamp, so the empty string sorts
    before every real stamp and a folder somebody renamed to say what it was
    for is the *first* thing retention removes. The same ordering runs
    `list_backups`, whose "newest first" is `sorted(..., reverse=True)` over
    the same names.

    The rule the docstring states -- "oldest first, by the stamp in the folder
    name -- not by mtime, which a copy or a sync client moves" -- has a third
    option it does not take: `created` in the manifest, which is UTC, is
    written by this code and cannot be renamed.
    """
    root = str(tmp_path / "backups")
    os.makedirs(root)
    keep = _backup_folder(root, "KEEP-before-the-big-refactor-save9")
    _backup_folder(root, "20260910-120000-save9")
    _backup_folder(root, "20260911-120000-save9")
    removed = safety.prune_backups(root, 2)
    assert os.path.isdir(keep), (
        "the folder with no stamp is the one folder whose age nothing knows, "
        "so it is not the one to delete first: removed %s" % removed)


# ===========================================================================
# Q8 -- the lock reclaim leaves files nothing sweeps
# ===========================================================================


def test_the_stale_lock_reclaim_leaves_nothing_behind(lab, no_game_running):
    """`_reclaim_stale_lock` creates `<lock>.reclaim` and moves the stale lock
    to `<lock>.stale-<pid>`. Both are removed on the way out, and neither is
    removed by a kill -- which is R18's argument, one release later, about two
    more files. `TEMP_RE` is `\\.nms-sorter-\\d+\\.tmp$`, so the sweep at step
    0b does not match either name and they stay in the operator's save folder
    for good.
    """
    d = lab["dir"]
    stale = os.path.join(d, os.path.basename(safety.lock_path(lab["target"]))
                         + ".stale-4242")
    guard = os.path.join(d, os.path.basename(safety.lock_path(lab["target"]))
                         + safety.RECLAIM_SUFFIX)
    long_ago = time.time() - 86400
    for p in (stale, guard):
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("{}")
        os.utime(p, (long_ago, long_ago))
    _apply(lab)
    left = sorted(f for f in os.listdir(d) if "nms-sorter" in f)
    assert left == [], (
        "a day-old reclaim leftover is not a live writer's file and nothing "
        "else will ever remove it: %s" % left)


# ===========================================================================
# Q9 -- the mf_ path is concatenated, so the apply renames it
# ===========================================================================


def test_an_apply_does_not_rename_the_metadata_file(lab, no_game_running):
    """R6 was "`os.replace` onto the client's spelling renames the operator's
    files", and its own docstring names `mf_SAVE9.HG` as one of the two files
    that moved. `on_disk_path` is applied to the save; the `mf_` is
    `"mf_" + os.path.basename(save_path)` at three sites (`backup`, step 8/10,
    `SaveFile.meta_path`), so on a case-insensitive filesystem a metadata file
    whose spelling differs is found, backed up under the wrong name, and
    renamed by step 10's `os.replace`.
    """
    shouty = os.path.join(lab["dir"], "mf_SAVE9.HG")
    os.rename(lab["meta"], shouty)
    if not os.path.exists(lab["meta"]):
        pytest.skip("a case-sensitive filesystem: mf_save9.hg is simply absent "
                    "here and the apply takes its no-metadata path")
    _apply(lab)
    left = sorted(f for f in os.listdir(lab["dir"]) if f.lower().startswith("mf_"))
    assert left == ["mf_SAVE9.HG"], (
        "the metadata file is written back under the name the filesystem has, "
        "not under the one this code spelled: %s" % left)


# ===========================================================================
# Q11 -- the config hash in the fingerprint moves with a timestamp
# ===========================================================================


def test_saving_the_config_without_changing_a_rule_keeps_the_fingerprint(
        lab):
    """GOAL 3.9 puts "the config SHA-256" in the preimage so that *the rules*
    changing invalidates a printed plan. `config_hash` hashes the whole
    document, and `store` stamps `updated` on every write, so a config saved
    with no rule touched mints a different fingerprint -- and writes a
    different `config_sha256` into the backup manifest, which is the field a
    later "was this backup taken under these rules?" has to read.
    """
    one = dict(lab["cfg"])
    two = dict(lab["cfg"])
    one["updated"] = "2026-09-14T06:01:10"
    two["updated"] = "2026-09-14T06:01:11"
    save = SaveFile(lab["target"])
    a, _c = planner.build_plan(save, one)
    b, _c = planner.build_plan(SaveFile(lab["target"]), two)
    assert a.fingerprint_full == b.fingerprint_full, (
        "the same rules over the same bytes: %s vs %s (config hashes %s, %s)"
        % (a.fingerprint, b.fingerprint,
           cfgmod.config_hash(one)[:8], cfgmod.config_hash(two)[:8]))


# ===========================================================================
# Q12 -- the expedition gate rests on a file the save does not need
# ===========================================================================


def test_an_expedition_with_an_empty_season_inventory_is_still_gated(tmp_path):
    """`gates()` refuses an expedition on two pieces of evidence: the `mf_`'s
    season word, and stacks in `CommonStateData/SeasonData/Inventory`. R4 made
    a save with no `mf_` a supported case, and for such a save the first piece
    is `None` -- `season_word` cannot tell "not an expedition" from "there is
    no metadata here" and answers the same for both. What is left is a slot
    count, so an expedition whose season inventory is empty reads as an
    ordinary save and is written to.
    """
    d = str(tmp_path / "exp")
    os.makedirs(d)
    doc = make_fixture.build_doc("expedition")
    dd = Doc(doc)
    inv = dd.at(["CommonStateData", "SeasonData", "Inventory"])
    assert inv is not None, "the expedition variant carries a season inventory"
    dd.set(inv, "Slots", [])
    path = os.path.join(d, make_fixture.SAVE_NAME)
    with open(path, "wb") as fh:
        fh.write(codec.frame_payload(codec.dumps(doc)))
    # no mf_ beside it: the season word is unavailable, which R4 made legal
    gates = SaveFile(path).gates()
    assert gates, (
        "this save carries CommonStateData/SeasonData and no metadata to ask "
        "about the season word, and the answer was: %r" % (gates,))


# ===========================================================================
# Q13 -- the config rotation still runs before the replace that can fail
# ===========================================================================


def test_a_config_write_that_fails_at_the_replace_costs_no_revision(
        tmp_path, monkeypatch):
    """R14's fix reordered `store` to temp file, rotation, replace, and its
    docstring says "Now a failed write costs nothing: the temp file is removed
    in the `finally` and no slot has moved yet". One operation still runs after
    the rotation -- the `os.replace` -- and it is the one that fails when the
    config is held open by another process or a virus scanner. Measured: every
    slot shifts, `.bak.1` becomes a duplicate of the file that was never
    replaced, and the oldest revision is unlinked, which is R14's damage in a
    narrower window.
    """
    path = str(tmp_path / "config.json")
    cfg = cfgmod.default_config()
    cfgmod.store(path, cfg, backup=False)
    for i in range(cfgmod.BAK_KEEP + 2):
        cfg["name"] = "rev%d" % i
        cfgmod.store(path, cfg)

    def name_of(slot):
        with io.open("%s.bak.%d" % (path, slot), encoding="utf-8") as fh:
            return json.load(fh)["name"]

    before = [name_of(n) for n in range(1, cfgmod.BAK_KEEP + 1)]
    real = os.replace

    def held(src, dst, *a, **kw):
        if os.path.normcase(dst) == os.path.normcase(path):
            raise PermissionError(13, "the file is open in another process")
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(cfgmod.os, "replace", held)
    cfg["name"] = "the revision that was never written"
    with pytest.raises(OSError):
        cfgmod.store(path, cfg)
    monkeypatch.undo()
    after = [name_of(n) for n in range(1, cfgmod.BAK_KEEP + 1)]
    assert after == before, (
        "nothing was written, so no revision moved: %s -> %s" % (before, after))
