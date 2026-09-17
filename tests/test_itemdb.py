"""`itemdb.py`'s public surface, which P0-3 rewrote underneath.

The file went from a CSV reader plus a Lua regex parser to four JSON loads.
Nothing outside it was supposed to notice, so these tests are written against
the surface rather than the storage: every name `config.py`, `planner.py`,
`server.py` and `savemodel.py` reach for, including the two private attributes
`config.py` reads directly.

`conftest.py` resets the singleton's overrides after every test, so a case here
may call `apply_overrides` without cleaning up.
"""
import pytest

from nms_sorter import itemdb
from nms_sorter.itemdb import PACKED_TECH, UNSORTED, ItemDB, db


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def test_db_is_a_singleton():
    assert db() is db()


def test_a_second_instance_loads_independently():
    """`db()` is shared and mutable; the class itself must not be."""
    fresh = ItemDB()
    assert fresh is not db()
    assert len(fresh.items) == len(db().items)
    assert fresh._base_by_id is None       # untouched by anyone's overrides


def test_public_names_exist():
    for name in ("db", "ItemDB", "UNSORTED", "PACKED_TECH"):
        assert hasattr(itemdb, name), name
    d = db()
    for name in ("items", "by_id", "buckets", "bucket_by_key", "procedural",
                 "custom", "overridden", "_base_by_id", "_base_buckets",
                 "lookup", "bucket_of", "original_bucket_of", "is_procedural",
                 "label", "cap", "apply_overrides", "is_custom_bucket",
                 "row_view", "search", "browse", "kinds", "bucket_counts",
                 "validate_item_id"):
        assert hasattr(d, name), name


def test_itemdb_parses_no_input_formats():
    """This module reads JSON and nothing else, and it raises rather than
    exits: a `SystemExit` out of a load is not something a web request handler
    can report. Every other format belongs to the generator."""
    import inspect
    src = inspect.getsource(itemdb)
    for gone in ("import csv", "ElementTree", "SystemExit"):
        assert gone not in src, gone


def test_a_short_table_raises_rather_than_exiting(monkeypatch):
    """It used to be `SystemExit`, which a web request handler cannot report.
    A floor, not an equality: a table that grew is not a failure."""
    one_row = ('{"A":{"name":"x","kind":"product","category":"","subtitle":"",'
               '"multiplier":1,"bucket":"raw_resources","procedural":false}}')
    monkeypatch.setattr(itemdb, "_read_data",
                        lambda name: one_row if name == "items.json"
                        else _real_read(name))
    with pytest.raises(RuntimeError) as e:
        ItemDB()
    assert "items.json holds 1 ids" in str(e.value)
    assert str(itemdb.MIN_IDS) in str(e.value)


def test_unreadable_data_names_the_file(monkeypatch):
    monkeypatch.setattr(itemdb, "_read_data", lambda name: "not json")
    with pytest.raises(RuntimeError) as e:
        ItemDB()
    assert "buckets.json" in str(e.value)
    # It must say how to fix it, and the fix is a reinstall: the tables come
    # out of a decompiled game install, so naming the generator would hand
    # somebody a command they cannot run.
    assert "re-clone" in str(e.value)
    assert "gen_emit" not in str(e.value)


_real_read = itemdb._read_data


def test_items_pointing_at_an_undefined_bucket_is_refused(monkeypatch):
    """items.json and buckets.json written by different runs is a real failure
    mode, and it is silent otherwise: the ids just render as Unsorted."""
    def fake(name):
        if name == "items.json":
            row = ('{"name":"x","kind":"product","category":"","subtitle":"",'
                   '"multiplier":1,"bucket":"no_such_bucket","procedural":false}')
            return "{%s}" % ",".join('"ID%04d":%s' % (i, row)
                                     for i in range(itemdb.MIN_IDS))
        return _real_read(name)
    monkeypatch.setattr(itemdb, "_read_data", fake)
    with pytest.raises(RuntimeError) as e:
        ItemDB()
    assert "no_such_bucket" in str(e.value)


# --------------------------------------------------------------------------
# lookup
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["TECH_COMP", "^TECH_COMP", "TECH_COMP#12345",
                                 "^TECH_COMP#12345"])
def test_lookup_tolerates_the_caret_and_the_hash(raw):
    row = db().lookup(raw)
    assert row and row["id"] == "TECH_COMP"


def test_lookup_of_an_unknown_id_is_none_not_an_error():
    """The normal case after a game patch. Every caller reads None as
    "leave it where it is"."""
    assert db().lookup("NO_SUCH_ITEM_XYZ") is None


def test_lookup_row_carries_the_game_fields():
    row = db().lookup("CATALYST1")
    assert set(row) == {"id", "name", "kind", "category", "subtitle", "multiplier"}
    assert row["name"] and row["kind"]


def test_is_procedural():
    d = db()
    assert d.is_procedural("UP_SGUN4") is True
    assert d.is_procedural("^UP_SGUN4#86554") is True
    assert d.is_procedural("CATALYST1") is False


def test_label_falls_back_to_unsorted():
    d = db()
    assert d.label("raw_resources") == "Raw Resources"
    assert d.label("no_such_bucket") == "Unsorted"
    assert d.label(None) == "Unsorted"


def test_bucket_of_a_hashed_unknown_stem_is_packed_tech():
    d = db()
    assert d.bucket_of("NOT_A_STEM#00001") == PACKED_TECH
    assert d.bucket_of("NOT_A_STEM") == UNSORTED


def test_bucket_of_accepts_a_non_string():
    """`bucket_of` is called with whatever came out of a save. `text.stem`
    handles a str; the `#` branch guards the rest."""
    assert db().bucket_of("") == UNSORTED


# --------------------------------------------------------------------------
# cap
# --------------------------------------------------------------------------

@pytest.mark.parametrize("difficulty,group,inv,expect", [
    ("High", "Chest", "Substance", 9999),
    ("Normal", "Chest", "Substance", 1000),
    ("Low", "Chest", "Substance", 1000),
    ("Normal", "Freighter", "Substance", 2000),
])
def test_cap_reads_the_stacks_table(difficulty, group, inv, expect):
    assert db().cap("^CATALYST1", inv, group, difficulty) == expect


def test_cap_multiplies_a_product_by_its_stack_multiplier():
    d = db()
    row = d.lookup("TECH_COMP")
    base = d.stacks["Normal"]["Chest"]["product"]
    assert d.cap("TECH_COMP", "Product", "Chest", "Normal") == base * row["multiplier"]


def test_cap_of_an_unknown_id_assumes_a_multiplier_of_one():
    d = db()
    base = d.stacks["Normal"]["Chest"]["product"]
    assert d.cap("NO_SUCH_ITEM_XYZ", "Product", "Chest", "Normal") == base


def test_cap_falls_back_for_an_unknown_difficulty_and_group():
    d = db()
    assert (d.cap("^CATALYST1", "Substance", "Chest", "Relaxed")
            == d.cap("^CATALYST1", "Substance", "Chest", "Normal"))
    assert (d.cap("^CATALYST1", "Substance", "NoSuchGroup", "Normal")
            == d.stacks["Normal"]["Default"]["substance"])


# --------------------------------------------------------------------------
# the configuration overlay
# --------------------------------------------------------------------------

def test_apply_overrides_moves_an_item_and_records_it():
    d = db().apply_overrides({"item_buckets": {"CATALYST1": "trade_goods"}})
    assert d.bucket_of("CATALYST1") == "trade_goods"
    assert d.overridden == {"CATALYST1": "trade_goods"}
    assert d.original_bucket_of("CATALYST1") == "raw_resources"


def test_apply_overrides_is_idempotent_and_reverts():
    d = db()
    before = dict(d.by_id)
    d.apply_overrides({"item_buckets": {"CATALYST1": "trade_goods"}})
    d.apply_overrides({})
    assert d.by_id == before
    assert d.overridden == {} and d.custom == set()


def test_an_override_equal_to_the_default_is_not_recorded():
    """Config v2's migration drops these; the overlay must agree, or the UI
    shows an override the user cannot remove."""
    d = db()
    default = d.bucket_of("CATALYST1")
    d.apply_overrides({"item_buckets": {"CATALYST1": default}})
    assert d.overridden == {}


def test_an_override_naming_an_unknown_bucket_is_ignored():
    d = db().apply_overrides({"item_buckets": {"CATALYST1": "no_such_bucket"}})
    assert d.bucket_of("CATALYST1") == "raw_resources"
    assert d.overridden == {}


def test_an_override_key_is_stemmed():
    d = db().apply_overrides({"item_buckets": {"^UP_SGUN4#86554": "trade_goods"}})
    assert d.bucket_of("UP_SGUN4") == "trade_goods"
    assert "UP_SGUN4" in d.overridden


def test_custom_buckets_are_appended_and_flagged():
    d = db().apply_overrides({
        "custom_buckets": [{"key": "mine", "label": "Mine", "note": "n"}],
        "item_buckets": {"CATALYST1": "mine"}})
    assert d.buckets[-1]["key"] == "mine"
    assert d.is_custom_bucket("mine") and not d.is_custom_bucket("raw_resources")
    assert d.bucket_of("CATALYST1") == "mine"
    assert d.label("mine") == "Mine"


def test_a_custom_bucket_cannot_shadow_a_generated_one():
    d = db().apply_overrides({
        "custom_buckets": [{"key": "raw_resources", "label": "Hijacked"}]})
    assert d.label("raw_resources") == "Raw Resources"
    assert not d.is_custom_bucket("raw_resources")


def test_a_custom_bucket_disappears_when_the_config_drops_it():
    d = db()
    d.apply_overrides({"custom_buckets": [{"key": "mine", "label": "Mine"}]})
    d.apply_overrides({})
    assert "mine" not in d.bucket_by_key
    assert "mine" not in [b["key"] for b in d.buckets]


def test_base_attributes_config_reads_directly():
    """`config.py:41` reads `_base_buckets` to render the bucket list without
    the user's own additions. It is None until the first overlay."""
    d = db()
    assert d._base_by_id is None or isinstance(d._base_by_id, dict)
    d.apply_overrides({"item_buckets": {"CATALYST1": "trade_goods"}})
    assert isinstance(d._base_buckets, list) and d._base_buckets
    assert d._base_by_id["CATALYST1"] == "raw_resources"
    assert "trade_goods" in [b["key"] for b in d._base_buckets]


def test_original_bucket_of_before_any_overlay():
    fresh = ItemDB()
    assert fresh.original_bucket_of("^CATALYST1") == "raw_resources"
    assert fresh.original_bucket_of("NO_SUCH_ITEM_XYZ") == UNSORTED


# --------------------------------------------------------------------------
# views
# --------------------------------------------------------------------------

def test_row_view_shape():
    v = db().row_view("CATALYST1")
    assert set(v) == {"id", "name", "kind", "category", "subtitle", "multiplier",
                      "bucket", "bucket_label", "original_bucket",
                      "original_bucket_label", "overridden", "procedural"}
    assert v["overridden"] is False


def test_row_view_flags_an_override():
    d = db().apply_overrides({"item_buckets": {"CATALYST1": "trade_goods"}})
    v = d.row_view("CATALYST1")
    assert v["overridden"] is True
    assert v["bucket"] == "trade_goods" and v["original_bucket"] == "raw_resources"
    assert v["bucket_label"] == "Trade Goods"


def test_row_view_of_an_unknown_id_is_still_a_row():
    v = db().row_view("NO_SUCH_ITEM_XYZ")
    assert v["id"] == "NO_SUCH_ITEM_XYZ" and v["name"] is None
    assert v["bucket"] == UNSORTED


def test_search_exact_id_wins():
    rows = db().search("CATALYST1", limit=5)
    assert rows[0]["id"] == "CATALYST1"


def test_search_by_name():
    rows = db().search("wiring loom", limit=5)
    assert "TECH_COMP" in [r["id"] for r in rows]


def test_search_empty_query_browses_by_name():
    rows = db().search("", limit=7)
    assert len(rows) == 7
    names = [r["name"].lower() for r in rows]
    assert names == sorted(names)


def test_search_respects_the_limit():
    assert len(db().search("a", limit=3)) == 3


def test_browse_returns_rows_and_a_total():
    rows, total = db().browse(limit=10)
    assert len(rows) == 10 and total == len(db().items)


def test_browse_pages():
    first, total = db().browse(offset=0, limit=5)
    second, total2 = db().browse(offset=5, limit=5)
    assert total == total2
    assert [r["id"] for r in first] != [r["id"] for r in second]


def test_browse_filters_by_bucket_and_kind():
    rows, total = db().browse(bucket="fish", limit=1000)
    assert total and all(r["bucket"] == "fish" for r in rows)
    rows, total = db().browse(kind="substance", limit=1000)
    assert total and all(r["kind"] == "substance" for r in rows)


@pytest.mark.parametrize("only,check", [
    ("procedural", lambda r: r["procedural"] is True),
    ("unsorted", lambda r: r["bucket"] == UNSORTED),
])
def test_browse_only_filters(only, check):
    rows, total = db().browse(only=only, limit=2000)
    assert total and all(check(r) for r in rows)


def test_browse_only_overridden_needs_an_overlay():
    d = db()
    assert d.browse(only="overridden")[1] == 0
    d.apply_overrides({"item_buckets": {"CATALYST1": "trade_goods"}})
    rows, total = d.browse(only="overridden")
    assert total == 1 and rows[0]["id"] == "CATALYST1"


def test_browse_query_matches_id_name_subtitle_and_category():
    d = db()
    for q in ("catalyst1", "wiring loom"):
        assert d.browse(q=q, limit=5)[1] >= 1, q


def test_kinds_sums_to_the_table():
    k = db().kinds()
    assert sum(k.values()) == len(db().items)
    assert k["product"] > 0 and k["substance"] > 0


def test_bucket_counts_sums_to_the_table():
    c = db().bucket_counts()
    assert sum(c.values()) == len(db().items)


def test_bucket_counts_follows_an_override():
    d = db()
    before = d.bucket_counts()
    d.apply_overrides({"item_buckets": {"CATALYST1": "trade_goods"}})
    after = d.bucket_counts()
    assert after["trade_goods"] == before["trade_goods"] + 1
    assert after["raw_resources"] == before["raw_resources"] - 1


# --------------------------------------------------------------------------
# validate_item_id
# --------------------------------------------------------------------------

def test_validate_rejects_empty():
    assert db().validate_item_id("")[:2] == (False, "empty id")
    assert db().validate_item_id(None)[:2] == (False, "empty id")


def test_validate_rejects_raw_bytes():
    raw = b"^\x80\x80\xfd62\x95#03535".decode("utf-8", "surrogateescape")
    ok, msg, stem = db().validate_item_id(raw)
    assert ok is False and stem is None
    assert "printable ASCII" in msg


def test_validate_returns_the_stem():
    assert db().validate_item_id("^TECH_COMP#99")[2] == "TECH_COMP"
