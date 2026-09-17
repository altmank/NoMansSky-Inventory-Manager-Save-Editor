"""Guards on the shipped data files themselves.

`itemdb.py` only enforces a floor (17 buckets, 5,000 ids) because refusing to
start the application over a count is the wrong response to a table that grew.
The exact dimensions live in `nms_sorter/data/DATA_COUNTS`, written by the
generator, and this file is where they are held to. A regeneration updates the
data and `DATA_COUNTS` in one commit; a table that silently shrinks fails here
with a sentence naming which count moved.

What is *not* checked here is the generator. `tools/gen_emit.py` needs 128 MB
of decompiled game tables to run, so there is no regeneration this suite can
perform. The check on a run is that pointing it at the build the data is
already stamped with reproduces the committed bytes, and that is a maintainer's
step in `docs/POST-PATCH.md`. What this file holds is the output: the counts,
the shapes, and the canonical formatting that makes a regeneration reviewable
as a diff.
"""
import io
import json
import os

import pytest

from nms_sorter import itemdb
from nms_sorter.itemdb import PACKED_TECH, UNSORTED, db

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "nms_sorter", "data")

EXPECTED_FILES = ["items.json", "buckets.json", "stacks.json", "savekeys.json",
                  "vehicles.json", "gridnames.json", "DATA_VERSION",
                  "DATA_COUNTS"]


@pytest.fixture(scope="module")
def counts():
    return itemdb.data_counts()


# --------------------------------------------------------------------------
# the files exist, and only these files
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", EXPECTED_FILES)
def test_data_file_is_shipped(name):
    assert os.path.exists(os.path.join(DATA, name))


def test_no_generator_inputs_are_shipped():
    """The package reads JSON and nothing else, and the PyInstaller spec ships
    this whole directory. A generator's input or report landing here would go
    inside the executable and leave two answers to every question about an
    id: `added-in-<build>.txt` goes to `tools/` for exactly that reason."""
    stale = sorted(f for f in os.listdir(DATA)
                   if f.endswith((".csv", ".lua", ".xml", ".txt")))
    assert stale == []


def test_data_directory_is_within_budget():
    """The contract budgets `data/` at under 2 MB; measured input was under
    600 KB. Only the shipped files count, and only the shipped files are in
    here: a generator run writes its outputs in place and keeps no copy of
    what it replaced, because git holds that."""
    total = sum(os.path.getsize(os.path.join(DATA, f)) for f in EXPECTED_FILES)
    assert total < 2 * 1024 * 1024, "data/ is %d bytes" % total


def test_nothing_unexpected_is_shipped_in_data():
    """A `data/*` glob in pyproject.toml ships whatever is in this directory."""
    assert sorted(os.listdir(DATA)) == sorted(EXPECTED_FILES)


# --------------------------------------------------------------------------
# vehicles.json: a floor and a shape, never an exact list
# --------------------------------------------------------------------------

def test_vehicles_json_is_the_vehicle_ownership_order():
    """A floor, like MIN_IDS: the game has shipped six nameable exocraft and a
    seventh slot, and a patch adding an eighth must not fail here.

    What is asserted is the shape the model depends on: rows in index order
    starting at 0, so `vehicles()[i]` is `VehicleOwnership[i]` without a
    search, and a distinct non-empty name per row, because the name is the
    whole label for a slot the save carries no name for.
    """
    rows = itemdb.vehicles()
    assert len(rows) >= itemdb.MIN_VEHICLES, len(rows)
    assert [r["index"] for r in rows] == list(range(len(rows)))
    names = [r["name"] for r in rows]
    assert all(n and n.strip() for n in names), names
    assert len(set(names)) == len(names), "a name identifies a slot: %r" % names
    for r in rows:
        assert r["type"], r
        assert r["title_key"] == "VEHICLE_%s_TITLE_L" % r["type"].upper(), r


def test_vehicles_count_matches_data_counts(counts):
    assert counts["vehicles"] == len(itemdb.vehicles())


def test_a_missing_vehicle_table_falls_back_to_exocraft_n(monkeypatch):
    """The file is data, not a dependency: `savemodel` must still label an
    exocraft when it will not load."""
    from nms_sorter import savemodel
    monkeypatch.setattr(itemdb, "_VEHICLES", [])
    assert savemodel.vehicle_label(2, "") == "Exocraft 3"
    assert savemodel.vehicle_label(2, "Digger") == "Digger"


# --------------------------------------------------------------------------
# gridnames.json: the game's own word for each grid of a vessel
# --------------------------------------------------------------------------

def test_gridnames_json_carries_the_games_own_words():
    """Seven rows: four vessel kinds and the three cargo grids that exist.

    Every name is composed by the generator from the localisation keys the row
    names, so what is asserted here is the shape `savemodel.grid_title`
    depends on, plus the one claim that matters -- the word "General" is not
    in this table, because the game does not have it.
    """
    rows = itemdb.grid_names()
    assert len(rows) >= itemdb.MIN_GRID_NAMES, rows
    for kind in ("suit", "ship", "freighter", "exocraft"):
        assert rows.get((kind, "general")), kind
    for kind in ("suit", "ship", "freighter"):
        assert rows[(kind, "cargo")] == "Cargo", kind
    assert "General" not in rows.values(), rows


def test_gridnames_count_matches_data_counts(counts):
    assert counts["gridnames"] == len(itemdb.grid_names())


def test_a_missing_grid_name_table_falls_back(monkeypatch):
    """The file is data, not a dependency: a vessel with two live grids must
    still head them with something when it will not load."""
    from nms_sorter import savemodel
    monkeypatch.setattr(itemdb, "_GRID_NAMES", {})
    assert savemodel.grid_title("suit", savemodel.GENERAL) == "Inventory"
    assert savemodel.grid_title("freighter", savemodel.CARGO) == "Cargo"
    assert savemodel.grid_title("ship3", savemodel.TECHNOLOGY) == ""


def test_data_version_names_the_build():
    v = itemdb.data_version()
    assert v["game_build"].isdigit()
    assert v["mbincompiler"]
    assert v["generated"]
    assert v["generator"] == "gen_emit.py"


# --------------------------------------------------------------------------
# DATA_COUNTS describes the table that actually loaded
# --------------------------------------------------------------------------

def test_bucket_count_matches_data_counts(counts):
    d = db()
    assert len(d.buckets) == counts["buckets"], (
        "buckets moved: DATA_COUNTS says %d, buckets.json loaded %d"
        % (counts["buckets"], len(d.buckets)))


def test_id_count_matches_data_counts(counts):
    d = db()
    assert len(d.items) == counts["ids"], (
        "ids moved: DATA_COUNTS says %d, items.json loaded %d"
        % (counts["ids"], len(d.items)))
    assert len(d.by_id) == counts["ids"]


def test_procedural_count_matches_data_counts(counts):
    assert len(db().procedural) == counts["procedural"]


def test_unsorted_count_matches_data_counts(counts):
    d = db()
    assert sum(1 for b in d.by_id.values() if b == UNSORTED) == counts["unsorted"]


def test_per_bucket_counts_match_data_counts(counts):
    got = db().bucket_counts()
    assert got == counts["by_bucket"], "\n".join(
        "%-24s DATA_COUNTS=%-5s loaded=%s" % (k, counts["by_bucket"].get(k),
                                              got.get(k))
        for k in sorted(set(counts["by_bucket"]) | set(got))
        if counts["by_bucket"].get(k) != got.get(k))


def test_counts_are_internally_consistent(counts):
    assert sum(counts["by_bucket"].values()) == counts["ids"]
    assert counts["by_bucket"].get(UNSORTED, 0) == counts["unsorted"]
    assert counts["buckets"] >= itemdb.MIN_BUCKETS
    assert counts["ids"] >= itemdb.MIN_IDS


# --------------------------------------------------------------------------
# shape of the taxonomy
# --------------------------------------------------------------------------

def test_every_bucket_has_a_label_and_a_note():
    for b in db().buckets:
        assert b["key"] and b["label"], b
        assert len(b["note"]) > 40, "%s: note is %d chars" % (b["key"], len(b["note"]))


def test_bucket_keys_are_unique_and_identifier_shaped():
    keys = [b["key"] for b in db().buckets]
    assert len(set(keys)) == len(keys)
    for k in keys:
        assert k.replace("_", "").isalnum() and k == k.lower(), k


def test_packed_tech_note_is_not_truncated():
    """It was assembled in the Lua with `..` and the old regex captured only
    the first segment, so the note stopped mid-sentence at "An "."""
    note = db().bucket_by_key[PACKED_TECH]["note"]
    assert note.startswith("Matched by SHAPE")
    assert note.rstrip().endswith("what a lookup cannot.")
    assert " .. " not in note and '"' not in note
    assert len(note) > 300, len(note)


def test_packed_tech_holds_no_ids():
    """It is a shape rule, not a lookup: no id is ever assigned to it, and an
    id carrying a #hash that no table names lands there at query time."""
    d = db()
    assert [i for i, b in d.by_id.items() if b == PACKED_TECH] == []
    assert d.bucket_of("NOT_A_REAL_STEM#12345") == PACKED_TECH


def test_unsorted_is_not_a_bucket_but_has_a_label():
    d = db()
    assert UNSORTED not in [b["key"] for b in d.buckets]
    assert d.label(UNSORTED) == "Unsorted"


def test_every_assigned_bucket_is_defined():
    d = db()
    defined = set(b["key"] for b in d.buckets) | {UNSORTED}
    assert set(d.by_id.values()) <= defined


def test_every_item_has_every_field():
    for iid, r in db().items.items():
        assert r["id"] == iid
        assert isinstance(r["name"], str) and r["name"]
        assert isinstance(r["kind"], str) and r["kind"]
        assert isinstance(r["category"], str)
        assert isinstance(r["subtitle"], str)
        assert r["multiplier"] is None or isinstance(r["multiplier"], int)


def test_stacks_cover_every_difficulty_and_group():
    s = db().stacks
    assert set(s) == {"High", "Normal", "Low"}
    groups = set(s["Normal"])
    assert "Default" in groups
    for diff, table in s.items():
        assert set(table) == groups, diff
        for group, row in table.items():
            assert row["product"] > 0 and row["substance"] > 0, (diff, group)


def test_savekeys_are_a_bijection():
    fwd = json.load(io.open(os.path.join(DATA, "savekeys.json"),
                            encoding="utf-8"))
    assert len(fwd["forward"]) == len(fwd["reverse"]) == len(set(fwd["forward"].values()))
    for obf, plain in fwd["forward"].items():
        assert fwd["reverse"][plain] == obf


# --------------------------------------------------------------------------
# bucket 18: corvette_parts (P0-4)
# --------------------------------------------------------------------------

CORVETTE = "corvette_parts"
FIXTURE_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "fixtures", "config_v1_real.json")


@pytest.fixture(scope="module")
def config_overrides():
    """`item_buckets` out of the first real user's configuration."""
    with io.open(FIXTURE_CONFIG, encoding="utf-8") as fh:
        return json.load(fh).get("item_buckets") or {}


def test_corvette_parts_is_the_eighteenth_bucket():
    d = db()
    keys = [b["key"] for b in d.buckets]
    assert keys[-1] == CORVETTE, "appended, so no existing bucket changed position"
    assert len(keys) == 18
    assert d.label(CORVETTE) == "Corvette Parts"


def test_corvette_parts_note_states_the_rule():
    note = db().bucket_by_key[CORVETTE]["note"]
    assert "Corvette" in note
    assert "CV_" in note, "the note has to say why the id prefix is not the rule"


def test_generated_corvette_assignment_equals_the_users_640_overrides(config_overrides):
    """The decision recorded on 2026-09-14: those 640 overrides are the default
    classification for those parts, so the override became the rule.

    Reported both ways round. A rule that claims an id the user left alone is
    as wrong as one that misses an id they moved -- the first silently moves
    somebody's items, the second leaves their config 640 lines long.
    """
    want = set(k for k, v in config_overrides.items() if v == CORVETTE)
    got = set(i for i, b in db().by_id.items() if b == CORVETTE)
    assert len(want) == 640, "the fixture should hold 640 corvette overrides"
    assert sorted(got - want) == [], "the rule claims ids the user did not move"
    assert sorted(want - got) == [], "the rule misses ids the user moved"


def test_the_one_non_corvette_override_is_personal_not_a_rule(config_overrides):
    """`TECH_COMP -> refined_crafted` is the user's own preference for Wiring
    Looms and must not be generated: it is the migration's job to keep it."""
    other = dict((k, v) for k, v in config_overrides.items() if v != CORVETTE)
    assert other == {"TECH_COMP": "refined_crafted"}
    assert db().bucket_of("TECH_COMP") != "refined_crafted"


def test_the_procedural_cv_modules_stay_in_tech_upgrades():
    """31 procedural `CV_*` entries, 16 of them with the game category
    CORVETTE. They install into a Corvette rather than build one, and a player
    comparing rolls wants them next to the other module families."""
    d = db()
    cv = [i for i in d.items if i.startswith("CV_") and d.is_procedural(i)]
    assert len(cv) == 31
    assert all(d.by_id[i] == "tech_upgrades" for i in cv), \
        [i for i in cv if d.by_id[i] != "tech_upgrades"]


def test_every_corvette_part_says_corvette():
    d = db()
    for i, b in d.by_id.items():
        if b != CORVETTE:
            continue
        r = d.items[i]
        assert "corvette" in ("%s %s" % (r["name"], r["subtitle"])).lower(), i


def test_the_rule_is_reapplied_by_the_generator_not_hand_maintained():
    """Re-derive the assignment from `items.json`'s own name/subtitle columns
    and require it to match what `items.json` says the bucket is. If the two
    ever disagree, somebody edited the data instead of the rule."""
    from tools import gen_emit
    d = db()
    rows = dict((i, {"name": r["name"], "subtitle": r["subtitle"],
                     "kind": r["kind"]}) for i, r in d.items.items())
    derived = gen_emit.corvette_ids(rows, d.procedural)
    assert derived == set(i for i, b in d.by_id.items() if b == CORVETTE)


def test_corvette_carve_out_survives_a_subtitle_change():
    """The exclusion is vacuous on this build -- no CV_* module mentions a
    Corvette in its text. It is here for the build where one does."""
    from tools import gen_emit
    rows = {
        "CV_HYP2": {"name": "Hyperdrive", "kind": "procedural-technology",
                    "subtitle": "Corvette Hyperdrive Upgrade"},
        "CV_WING_A": {"name": "Corvette Wing", "kind": "product", "subtitle": ""},
        "SHIPWING_A": {"name": "Corvette Wing", "kind": "product", "subtitle": ""},
    }
    got = gen_emit.corvette_ids(rows, {"CV_HYP2"})
    assert got == {"CV_WING_A", "SHIPWING_A"}


# --------------------------------------------------------------------------
# the sanity lookups the legacy self-test made
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,bucket,why", [
    ("^RED2", "raw_resources", "bucket_of strips the caret"),
    ("UP_SGUN4#86554", "tech_upgrades_elite",
     "a procedural id resolves through its stem"),
    ("UP_RAD3#28463", "tech_upgrades_elite",
     "UP_RAD3 is S-Class despite the 3: grade is read off the name, because "
     "the five hazard-protection families have no C-Class tier"),
    ("HULK1", "salvage_junk",
     "the game's own table names it Contaminated Metal"),
    ("NO_SUCH_ITEM_XYZ", UNSORTED, "an id no table knows is left alone"),
])
def test_known_bucket(raw, bucket, why):
    assert db().bucket_of(raw) == bucket, why


def test_an_id_inferred_from_a_display_name_is_refused():
    ok, msg, stem = db().validate_item_id("TECHMOD")
    assert ok is False and stem is None
    assert "display name" in msg


def test_the_real_wiring_loom_id_passes():
    assert db().validate_item_id("TECH_COMP") == (True, None, "TECH_COMP")


def test_stack_cap_comes_from_the_stacks_table():
    assert db().cap("^CATALYST1", "Substance", "Chest", "High") == 9999


# --------------------------------------------------------------------------
# the formatting a regeneration has to preserve to stay reviewable
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["items.json", "buckets.json", "stacks.json",
                                  "savekeys.json", "vehicles.json",
                                  "gridnames.json", "DATA_COUNTS"])
def test_shipped_json_is_canonically_formatted(name):
    """Sorted keys, one-space indent, LF, trailing newline. A regeneration has
    to be reviewable as a diff, and that only works if the formatting is a
    function of the data."""
    raw = io.open(os.path.join(DATA, name), encoding="utf-8", newline="").read()
    assert "\r" not in raw
    assert raw.endswith("\n")
    again = json.dumps(json.loads(raw), ensure_ascii=False, sort_keys=True,
                       indent=1, separators=(",", ":")) + "\n"
    assert raw == again


def test_items_json_keys_are_sorted():
    raw = io.open(os.path.join(DATA, "items.json"), encoding="utf-8").read()
    keys = json.loads(raw).keys()
    assert list(keys) == sorted(keys)
