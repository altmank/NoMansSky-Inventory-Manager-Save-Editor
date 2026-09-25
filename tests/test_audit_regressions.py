"""Regression cases from the September 2026 audit, using synthetic saves only."""
import contextlib
import copy
import http.client
import json
import os
import threading

import pytest

from nms_sorter import app as appmod
from nms_sorter import codec, config, itemdb, planner, safety, server
from nms_sorter.savemodel import SaveFile
from tools import make_fixture


def write_save(path, save):
    payload = codec.dumps(save.doc)
    raw = codec.frame_payload(payload)
    path.write_bytes(raw)
    path.with_name("mf_" + path.name).write_bytes(
        make_fixture.build_meta(len(payload), len(raw), path.name, "base"))


@pytest.fixture
def lab(tmp_path, no_game_running):
    folder = tmp_path / "saves"
    make_fixture.write_fixture(str(folder), "base")
    path = folder / make_fixture.SAVE_NAME
    save = SaveFile(str(path))
    templates = {s.id: copy.deepcopy(s.node)
                 for s in save.container_map()["suit"].slots()}
    for c in save.container_map().values():
        if c.sortable:
            save.d.set(c.node, "Slots", [])

    def put(key, rows):
        c = save.container_map()[key]
        nodes = []
        for i, (raw, amount) in enumerate(rows):
            product = "#" in raw or (itemdb.db().lookup(raw) or {}).get("kind") == "product"
            template = "^ALBUMENPEARL" if product else "^FUEL1"
            node = copy.deepcopy(templates[template])
            save.d.set(node, "Id", raw)
            save.d.set(node, "Amount", amount)
            index = save.d.get(node, "Index")
            save.d.set(index, "X", c.valid[i][0])
            save.d.set(index, "Y", c.valid[i][1])
            nodes.append(node)
        save.d.set(c.node, "Slots", nodes)

    put("suit", [("^FUEL1", 300)])
    cfg = config.default_config()
    cfg.update(sources=["suit"], bucket_rules=[{"bucket": "raw_resources", "store": "chest1"}],
               item_rules=[], never_move=[])
    cfg["options"].update(merge_duplicates=False, tidy=[], auto_name=False)
    write_save(path, save)
    cfgpath = tmp_path / "config.json"
    cfgpath.write_text(json.dumps(cfg), encoding="utf-8")
    app = appmod.App(str(folder), str(cfgpath), str(tmp_path / "backups"),
                     settings_path=str(tmp_path / "settings.json"))
    app.config = cfg
    return {"path": path, "save": save, "put": put, "cfg": cfg, "app": app}


@contextlib.contextmanager
def serving(app):
    httpd = server.serve(app, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def post(route, body=None):
        port = httpd.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        try:
            conn.request("GET" if body is None else "POST", route,
                         None if body is None else json.dumps(body),
                         {"Content-Type": "application/json",
                          "Origin": "http://127.0.0.1:%d" % port})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()
    try:
        yield post
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def apply(lab, builder=planner.build_plan):
    plan, _ = planner.build_plan(SaveFile(str(lab["path"])), lab["cfg"])
    return safety.apply_plan(str(lab["path"]), lab["cfg"], plan.fingerprint_full,
                             builder, lab["app"].backup_root)


def slots(lab, key):
    return SaveFile(str(lab["path"])).container_map()[key].slots()


@pytest.mark.parametrize("sources", [["suit", "chest1"], ["chest1", "suit"]])
def test_overlapping_source_and_destination_applies_once_and_is_stable(lab, sources):
    lab["cfg"]["sources"] = sources
    lab["cfg"]["bucket_rules"].append({"bucket": "raw_resources", "store": "chest2"})
    with serving(lab["app"]) as post:
        status, plan = post("/api/plan", {})
        assert status == 200 and not any(i["level"] == "error" for i in plan["issues"])
        status, result = post("/api/apply", {"fingerprint": plan["fingerprint"]})
        assert status == 200, result
        assert not slots(lab, "suit") and not slots(lab, "chest2")
        assert sum(s.amount for s in slots(lab, "chest1")) == 300
        status, again = post("/api/plan", {})
        assert status == 200 and not again["rows"]


def test_overflow_source_can_top_up_an_earlier_destination(lab):
    lab["put"]("suit", [])
    lab["put"]("chest1", [("^FUEL1", 9998)] * 8)
    lab["put"]("chest2", [("^FUEL1", 20)])
    lab["cfg"]["sources"] = ["chest2", "chest1"]
    lab["cfg"]["bucket_rules"].append({"bucket": "raw_resources", "store": "chest2"})
    write_save(lab["path"], lab["save"])
    apply(lab)
    assert sum(s.amount for s in slots(lab, "chest1")) == 9999 * 8
    assert sum(s.amount for s in slots(lab, "chest2")) == 12
    plan, _ = planner.build_plan(SaveFile(str(lab["path"])), lab["cfg"])
    assert not plan.rows


@pytest.mark.parametrize("selected", [False, True])
def test_unknown_duplicate_slots_survive_apply_byte_for_byte(lab, selected):
    raw = "^AUDIT_UNKNOWN_ITEM"
    assert itemdb.db().lookup(raw) is None
    lab["put"]("chest3", [(raw, 1), (raw, 1)])
    lab["cfg"]["options"].update(merge_duplicates=True, tidy=["chest3"])
    if selected:
        lab["cfg"]["sources"].append("chest3")
    write_save(lab["path"], lab["save"])
    before = codec.dumps([s.node for s in slots(lab, "chest3")])
    apply(lab)
    assert codec.dumps([s.node for s in slots(lab, "chest3")]) == before
    assert sum(s.amount for s in slots(lab, "chest1")) == 300


@pytest.mark.parametrize("exact_first", [False, True])
@pytest.mark.parametrize("exact_priority", [10, 100])
def test_exact_pin_beats_family_in_both_orders(lab, exact_first, exact_priority):
    raw = "^UP_SGUN4#86554"
    lab["put"]("suit", [("^FUEL1", 300), (raw, 1)])
    family = {"item": "UP_SGUN4", "store": "chest2", "priority": 10}
    exact = {"item": raw[1:], "pin": True, "priority": exact_priority}
    lab["cfg"]["item_rules"] = [exact, family] if exact_first else [family, exact]
    write_save(lab["path"], lab["save"])
    apply(lab)
    assert [(s.id, s.amount) for s in slots(lab, "suit")] == [(raw, 1)]
    assert not slots(lab, "chest2")


def test_change_during_decode_invalidates_next_cache_read(lab, monkeypatch):
    original = appmod.SaveFile
    reads = []
    alternate = original(str(lab["path"]))
    alternate.d.set(alternate.d.player, "Units", 7777777)

    def load_then_edit(path):
        obj = original(path)
        if not reads:
            write_save(lab["path"], alternate)
        reads.append(obj)
        return obj
    monkeypatch.setattr(appmod, "SaveFile", load_then_edit)
    first = lab["app"].save(reload=True)
    assert first.d.get(first.d.player, "Units") == 12345
    second = lab["app"].save()
    assert second.d.get(second.d.player, "Units") == 7777777
    assert lab["app"].save() is second
    assert len(reads) == 2


@pytest.mark.parametrize("phase", ["planning", "save_fsync"])
@pytest.mark.parametrize("change", ["save", "metadata", "delete_metadata", "add_metadata"])
def test_apply_refuses_external_change_before_replacing_either_file(lab, monkeypatch, phase, change):
    path = lab["path"]
    meta = path.with_name("mf_" + path.name)
    meta_before = meta.read_bytes()
    if change == "add_metadata":
        meta.unlink()
    before = path.read_bytes()
    external = {}

    def edit():
        if external:
            return
        if change == "save":
            newer = SaveFile(str(path))
            newer.d.set(newer.d.player, "Units", 123456789)
            path.write_bytes(codec.frame_payload(codec.dumps(newer.doc)))
        elif change == "metadata":
            meta.write_bytes(meta_before[:-1] + bytes([meta_before[-1] ^ 1]))
        elif change == "delete_metadata":
            meta.unlink()
        else:
            meta.write_bytes(meta_before)
        external.update(save=path.read_bytes(), meta=meta.read_bytes() if meta.exists() else None)

    with serving(lab["app"]) as post:
        status, plan = post("/api/plan", {})
        assert status == 200
        if phase == "planning":
            real = planner.build_plan
            def injected(*args, **kwargs):
                edit()
                return real(*args, **kwargs)
            monkeypatch.setattr(planner, "build_plan", injected)
        else:
            real = safety.os.fsync
            def injected(fd):
                out = real(fd)
                if os.path.exists(safety.temp_path(str(path))):
                    edit()
                return out
            monkeypatch.setattr(safety.os, "fsync", injected)
        status, result = post("/api/apply", {"fingerprint": plan["fingerprint"]})
    assert status == 409, result
    assert "changed" in result["error"]
    assert external and path.read_bytes() == external["save"]
    assert (meta.read_bytes() if meta.exists() else None) == external["meta"]
    manifests = list((path.parent.parent / "backups").glob("*/manifest.json"))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text())["outcome"] == safety.OUTCOME_REFUSED
    assert not os.path.exists(safety.temp_path(str(path)))
    if change != "save":
        assert path.read_bytes() == before


def test_metadata_change_during_its_temp_write_is_preserved(lab, monkeypatch):
    path = lab["path"]
    meta = path.with_name("mf_" + path.name)
    before = path.read_bytes()
    newer = meta.read_bytes()[:-1] + b"\x79"
    original = safety.os.fsync
    edits = []

    def fsync_then_edit(fd):
        out = original(fd)
        if os.path.exists(safety.temp_path(str(meta))) and not edits:
            meta.write_bytes(newer)
            edits.append(True)
        return out
    monkeypatch.setattr(safety.os, "fsync", fsync_then_edit)
    with pytest.raises(safety.Refused, match="changed"):
        apply(lab)
    assert edits and meta.read_bytes() == newer
    assert path.read_bytes() != before
    man = json.loads(next((path.parent.parent / "backups").glob("*/manifest.json")).read_text())
    assert man["outcome"] == safety.OUTCOME_REFUSED_AFTER_WRITE


def test_unexpected_prewrite_failure_settles_backup_manifest(lab):
    before = lab["path"].read_bytes()
    def broken(save, cfg):
        plan, _ = planner.build_plan(save, cfg)
        def commit():
            raise TypeError("synthetic failed transform")
        return plan, commit
    with pytest.raises(TypeError, match="synthetic failed transform"):
        apply(lab, broken)
    assert lab["path"].read_bytes() == before
    man = json.loads(next((lab["path"].parent.parent / "backups").glob("*/manifest.json")).read_text())
    assert man["outcome"] == safety.OUTCOME_REFUSED


def test_save_and_health_skip_backup_hashing_but_backups_verify(lab, monkeypatch):
    apply(lab)
    backup = next((lab["path"].parent.parent / "backups").glob("*/save9.hg"))
    backup.write_bytes(b"corrupted backup")
    hashed = []
    real = safety.sha256
    def count(path):
        hashed.append(os.path.abspath(path))
        return real(path)
    monkeypatch.setattr(safety, "sha256", count)
    with serving(lab["app"]) as post:
        status, body = post("/api/select", {"file": "save9.hg"})
        assert status == 200 and body["last_apply"]
        assert str(backup) not in hashed
        status, body = post("/api/health")
        assert status == 200 and body["backups"]["count"] == 1
        assert str(backup) not in hashed
    assert lab["app"].list_backups(verify=False)[0]["sha256_ok"] is None
    assert str(backup) not in hashed
    assert lab["app"].list_backups()[0]["sha256_ok"] is False
    assert str(backup) in hashed
    with pytest.raises(safety.Refused):
        safety.restore(str(backup.parent), str(lab["path"].parent))


@pytest.mark.parametrize("raw", ["^BANNER_MAINFAM", "^BANNER_NMSA"])
def test_current_banner_items_follow_the_cosmetics_rule(lab, raw):
    lab["put"]("suit", [(raw, 1)])
    lab["cfg"]["bucket_rules"] = [{"bucket": "cosmetics_appearance", "store": "chest2"}]
    write_save(lab["path"], lab["save"])
    apply(lab)
    assert not slots(lab, "suit")
    assert [(s.id, s.amount, s.inv_type) for s in slots(lab, "chest2")] == [(raw, 1, "Product")]
