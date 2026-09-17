"""`nms_sorter.savemodel` over the synthetic save, plus the fixture variants.

Converted from `selftest_legacy.py`, sections "synthetic save" and the
extractor-container part of the extractor block. The item table and the codec's
own self-test are not here; they live in `test_itemdb.py` and `test_codec.py`.
"""
import hashlib
import os
import re
import shutil

import pytest

from nms_sorter import planner
from nms_sorter.codec import dumps, frame_payload, loads, read_payload
from nms_sorter import savemodel
from nms_sorter.savemodel import SaveFile, UnsupportedSave, list_saves
from tools.make_fixture import PROC_ID, VARIANTS

#: every variant except the one whose whole point is that it cannot be loaded
LOADABLE = tuple(v for v in VARIANTS if v != "pre-waypoint")

# ------------------------------------------------------------------ framing


def test_synthetic_save_frames_and_reframes_byte_exactly(synthetic_path,
                                                         synthetic_payload):
    again, _info = read_payload(synthetic_path)
    assert again == synthetic_payload, \
        "synthetic save frames and reframes byte-exactly"


def test_synthetic_save_json_round_trip_is_byte_exact(synthetic_payload):
    assert dumps(loads(synthetic_payload)) == synthetic_payload, \
        "synthetic save json round trip is byte-exact"


def test_the_non_utf8_id_is_in_the_payload_as_raw_bytes(synthetic_payload):
    assert PROC_ID.encode("utf-8", "surrogateescape") in synthetic_payload, \
        "the non-UTF-8 id is in the payload as raw bytes"


# --------------------------------------------------------------- containers


def test_difficulty_is_read_from_difficulty_state(synthetic_save):
    assert synthetic_save.difficulty() == "High", \
        "difficulty read from DifficultyState.Settings"


def test_stack_size_group_is_read_from_the_container(synthetic_save):
    cm = synthetic_save.container_map()
    assert cm["suit"].group == "Personal" and cm["chest1"].group == "Chest", \
        "StackSizeGroup is read from the container, not assumed"


def test_a_containers_own_name_is_used(synthetic_save):
    cm = synthetic_save.container_map()
    assert cm["chest1"].display_name() == "Minerals", \
        "a container's own name is used when it has one"


def test_bld_storage_name_falls_back_to_the_default_label(synthetic_save):
    cm = synthetic_save.container_map()
    assert cm["chest2"].display_name() == "Storage Container 2", \
        "BLD_STORAGE_NAME falls back to the default label"


def test_empty_valid_slot_indices_reads_as_unallocated(synthetic_save):
    cm = synthetic_save.container_map()
    assert not cm["freighter"].allocated, \
        "an empty ValidSlotIndices reads as unallocated, not as absent"


# ------------------------------------------------------------ vessel names


@pytest.mark.parametrize("marker", sorted(savemodel.VESSEL_NAME_MARKERS))
def test_a_game_marker_never_becomes_a_vessel_name(marker):
    """`StarterShip` and `MainShip` are the game's own bookkeeping. Only the
    first used to be filtered, so `MainShip` was on screen as the name of the
    player's ship on two of the seven readable corpus saves."""
    assert savemodel.vessel_name(marker) == ""
    assert savemodel.ship_label(0, marker, "") == "Starship 1"
    assert savemodel.vehicle_label(3, marker) == "Pilgrim"


def test_a_real_player_name_that_looks_internal_is_kept():
    """`Sarco` is a real name on the corpus's save5, and is the same shape as
    a marker. The filter is a list for exactly this reason."""
    assert savemodel.vessel_name("Sarco") == "Sarco"
    assert savemodel.ship_label(1, "Sarco", "SHUTTLE_PROC.SCENE.MBIN") == "Sarco"
    assert savemodel.vessel_name("  spaced  ") == "spaced"
    assert savemodel.vessel_name(None) == ""
    assert savemodel.vessel_name("   ") == ""


@pytest.mark.parametrize("filename,expect", [
    ("MODELS/COMMON/SPACECRAFT/FIGHTERS/FIGHTER_PROC.SCENE.MBIN", "Fighter"),
    ("models/common/spacecraft/shuttle/shuttle_proc.scene.mbin", "Shuttle"),
    ("MODELS/COMMON/SPACECRAFT/SENTINELSHIP/SENTINELSHIP_PROC.SCENE.MBIN",
     "Sentinel Interceptor"),
    ("MODELS/COMMON/SPACECRAFT/S-CLASS/BIOPARTS/BIOSHIP_PROC.SCENE.MBIN", "Living"),
    ("MODELS/COMMON/SPACECRAFT/CORVETTE/CORVETTE.SCENE.MBIN", "Corvette"),
    # BIGGS is the Corvette: the game's own `BIGGS_*` localisation keys are
    # the Corvette build system ("MODIFY DOCKED CORVETTE", "Corvette
    # Autopilot", "Excavate Corvette Modules") and no other hull claims them.
    ("MODELS/COMMON/SPACECRAFT/BIGGS/BIGGS.SCENE.MBIN", "Corvette"),
    ("", ""),
    (None, ""),
])
def test_a_ship_class_comes_from_the_hull_the_save_names(filename, expect):
    assert savemodel.ship_class(filename) == expect


def test_a_ship_with_no_class_keeps_the_generic_label():
    """A hull the table does not know is "Starship N", not a guess. The number
    is the slot, so the label and the container key agree."""
    assert savemodel.ship_label(3, "", "SOMETHING_NEW.SCENE.MBIN") == "Starship 4"
    assert savemodel.ship_label(3, "", "SENTINELSHIP_PROC.SCENE.MBIN") == \
        "Sentinel Interceptor 4"


def test_a_corvette_is_named_from_the_field_the_game_names_it_in():
    """A Corvette's name is not in its `ShipOwnership` row: it is in
    `CorvetteEditShipName`, which is the same field the Corvette's storage
    container is already labelled from. One field, both places.

    `BIGGS` is the hull. That is the game's own name for it -- the `BIGGS_*`
    localisation keys are the Corvette build system, down to "MODIFY DOCKED
    CORVETTE" -- and the owner confirmed the ship in that slot is their
    Corvette.
    """
    biggs = "MODELS/COMMON/SPACECRAFT/BIGGS/BIGGS.SCENE.MBIN"
    assert savemodel.ship_label(1, "", biggs) == "Corvette 2"
    assert savemodel.ship_label(1, "", biggs, "Bunjiw RW2") == "Bunjiw RW2"
    # and no other hull takes that field
    assert savemodel.ship_label(1, "", "x/FIGHTER_PROC.SCENE.MBIN",
                                "Bunjiw RW2") == "Fighter 2"


def test_an_exocraft_is_named_from_the_games_own_vehicle_order():
    """`data/vehicles.json` is `GcVehicleType` in the order the game writes it,
    so slot 2 is the Colossus on the game's authority rather than on the
    authority of its grid being the biggest."""
    assert savemodel.vehicle_label(0, "") == "Roamer"
    assert savemodel.vehicle_label(2, "") == "Colossus"
    assert savemodel.vehicle_label(99, "") == "Exocraft 100"


def test_an_absent_primary_field_is_not_an_in_use_pill():
    """Index 0 is a valid primary and cannot be told from the field being
    unset -- three corpus saves read `0 / 0` -- and the owner's call is that 0
    is a primary, so the pill stays. An *absent* field is a different answer:
    no index, and therefore no pill on any vessel. `True` is not index 1 and a
    negative is not an index either."""
    assert savemodel._index_of(None) is None, "absent: no pill anywhere"
    assert savemodel._index_of(0) == 0, "and 0 is the first vessel"
    assert savemodel._index_of(True) is None
    assert savemodel._index_of(-1) is None


def test_a_vessel_slot_the_player_does_not_own_says_so(fixture_variant):
    """The save carries twelve starship slots whether or not there is a ship
    in one, and for a starship the hull is the test: no `Resource.Filename`
    and no `Resource.Seed` means no ship, whatever the cells say.

    A slot that used to hold a ship keeps its layout -- save10 and save9 slot
    2 read 35 cells, 24 technology cells and nothing a player can move -- and
    was being drawn as "Starship 3". Measured across the seven readable corpus
    saves, every row with a hull has a seed and every row with neither holds
    nothing, so nothing visible is lost by believing the hull.

    Two shapes that must *not* be caught: a slot holding something, because
    invisible inventory is the one outcome worth ruling out, and a ship with a
    hull and no cells (save10 slot 1, the one wearing the in-use pill).
    """
    save = SaveFile(fixture_variant("empty-ship-slots")["save"])
    by = dict((c.key, c) for c in save.containers())
    assert by["ship1"].slot_empty is True, "no name, no hull, no cells, empty"
    assert by["ship1_cargo"].slot_empty is True, "every grid of the slot"
    assert by["ship0"].slot_empty is False, "it is holding something"
    assert by["ship2"].slot_empty is True, \
        "25 cells, no hull, nothing in them: a ship that is gone"
    assert by["ship3"].slot_empty is False, "a hull, and the game gave it one"
    assert by["suit"].slot_empty is False and by["freighter"].slot_empty is False, \
        "the exosuit and the freighter are not slots: you have one or none"


def test_a_core_lists_its_substances_in_one_order_on_every_core(synthetic_save):
    """Cores 1 to 11 on save10 listed Chromatic Metal first and cores 12 to 14
    listed it last, after Methane: the same five substances in two orders, in
    one grid whose whole purpose is comparing one core with the next. The
    save's own slot order differs between buffers, so the order is decided
    here."""
    core = synthetic_save.extractor_containers()
    assert core, "the fixture has one core"
    rows = synthetic_save.extractors()[0].substances()
    names = [r["name"] for r in rows]
    assert names == sorted(names, key=lambda n: (n != "Chromatic Metal", n)), names
    assert "Chromatic Metal" in names and not any(
        "MAINT" in (r["id"] or "") for r in rows)


def test_the_freighter_label_is_the_players_own_freighter_name(synthetic_save):
    """It was parsed, served as `save.freighter`, and rendered nowhere."""
    cm = synthetic_save.container_map()
    assert synthetic_save.freighter_name() == "Fixture基地"
    assert cm["freighter"].label == "Fixture基地"
    assert cm["freighter_cargo"].label == "Fixture基地 Cargo"
    assert cm["freighter_tech"].label == "Fixture基地 Technology"


def test_a_save_with_no_freighter_name_keeps_the_static_labels(tmp_path,
                                                               fixture_variant):
    save = str(tmp_path / "save9.hg")
    doc = savemodel.loads(read_payload(fixture_variant("base")["save"])[0])
    d = savemodel.Doc(doc)
    d.set(d.player, "PlayerFreighterName", "")
    with open(save, "wb") as fh:
        fh.write(frame_payload(dumps(doc)))
    cm = SaveFile(save).container_map()
    assert cm["freighter"].label == "Freighter"
    assert cm["freighter_cargo"].label == "Freighter Cargo"


def test_the_ship_in_use_is_the_one_primary_ship_names(synthetic_save):
    cm = synthetic_save.container_map()
    assert cm["ship0"].label == "Starship 1", "the marker is filtered"
    assert cm["ship0"].in_use is True
    assert cm["suit"].in_use is False, "only a vessel is ever in use"


def test_primary_ship_is_an_index_not_a_flag(tmp_path, fixture_variant):
    """`True` is not slot 1, and a missing field is not slot 0."""
    assert savemodel._index_of(True) is None
    assert savemodel._index_of(None) is None
    assert savemodel._index_of(-1) is None
    assert savemodel._index_of(0) == 0
    save = str(tmp_path / "save9.hg")
    doc = savemodel.loads(read_payload(fixture_variant("base")["save"])[0])
    d = savemodel.Doc(doc)
    d.set(d.player, "PrimaryShip", True)
    with open(save, "wb") as fh:
        fh.write(frame_payload(dumps(doc)))
    assert SaveFile(save).container_map()["ship0"].in_use is False


def test_the_container_view_carries_in_use(synthetic_save):
    v = synthetic_save.container_map()["ship0"].view("Normal")
    assert v["in_use"] is True and v["label"] == "Starship 1"


# --------------------------------------------------------------- extractors


def test_the_extractor_filter_is_location_plus_module(synthetic_save):
    assert len(synthetic_save.extractors()) == 1, \
        "the extractor filter is Location==2 plus ^MAINT_HOOVER, never an index"


def test_the_extractor_core_is_offered_as_a_container(synthetic_save):
    exc = synthetic_save.extractor_containers()
    assert len(exc) == 1 and exc[0].key == "extractor1", \
        "the extractor core is offered as a container: %s" % [c.key for c in exc]


def test_the_machines_own_row_is_not_inventory(synthetic_save):
    """`^MAINT_HOOVER` is the extractor itself, sitting in cell (0, 0) of its
    own buffer. It was rendered as an item row called "Extractor Unit", and it
    made a core read `valid: 0, used: 6, free: -6`."""
    core = synthetic_save.extractor_containers()[0]
    assert len(core.slots()) == 2, "the slot array is untouched"
    assert [s.id for s in core.holdings()] == ["^STELLAR2"]
    v = core.view("Normal")
    assert [i["stem"] for i in v["items"]] == ["STELLAR2"]
    assert (v["used"], v["valid"], v["free"]) == (1, 1, 0), v
    assert v["items"][0]["i"] == 1, \
        "the row keeps its real position in the slot array: %r" % v["items"][0]


def test_the_predicate_is_about_the_container_not_the_id():
    """Three facts about this container, so a machine the game adds later needs
    no id list edited. It is false for an ordinary technology grid, which is
    not drain-only, and for a core's substances, which are not Technology."""
    f = savemodel.is_maintenance_slot
    assert f(True, "MaintenanceObject", "Technology") is True
    assert f(False, "MaintenanceObject", "Technology") is False
    assert f(True, "Chest", "Technology") is False
    assert f(True, "MaintenanceObject", "Substance") is False


def test_free_cells_are_never_negative(synthetic_save):
    """A grid holding more stacks than it has valid cells is a real shape, and
    "-6 free cells" is not a number anybody can act on."""
    for c in synthetic_save.containers() + synthetic_save.extractor_containers():
        assert c.view("Normal")["free"] >= 0, c.key


def test_the_extractor_container_is_drain_only(synthetic_save):
    exc = synthetic_save.extractor_containers()[0]
    assert exc.drain_only and not exc.sortable and exc.allocated, \
        ("it is drain-only, allocated, and never sortable: %s %s %s"
         % (exc.drain_only, exc.sortable, exc.allocated))


# ------------------------------------------------------------------ variants
#
# The variants exist for later tickets (refusals over expedition saves, a save
# version we have not seen, an mf_ whose format is not 2004). Until those
# tickets land, the only thing asserted is that each one is a real save file:
# built through the real codec, framed, and byte-exact back out again. A
# fixture that does not round-trip would fail those tickets for the wrong
# reason.


@pytest.mark.parametrize("variant", VARIANTS)
def test_every_fixture_variant_round_trips_through_the_codec(fixture_variant,
                                                             variant):
    fx = fixture_variant(variant)
    again, info = read_payload(fx["save"])
    assert again == fx["payload"], \
        "variant %s reframes byte-exactly" % variant
    assert dumps(loads(fx["payload"])) == fx["payload"], \
        "variant %s json round trip is byte-exact" % variant
    assert info["framed"], "variant %s is framed, not plaintext" % variant


@pytest.mark.parametrize("variant", LOADABLE)
def test_every_fixture_variant_loads_as_a_save(fixture_variant, variant):
    fx = fixture_variant(variant)
    save = SaveFile(fx["save"])
    assert save.container_map() is not None, \
        "variant %s builds a container map" % variant
    assert save.version() in (4735, 4800), \
        "variant %s has a save version: %r" % (variant, save.version())


def test_the_variant_deltas_are_what_they_say(fixture_variant):
    """Each variant differs from `base` in the one way its name claims."""
    from nms_sorter.codec import meta_read

    base = SaveFile(fixture_variant("base")["save"])
    assert "freighter" in base.container_map()
    assert "ship0" in base.container_map()
    assert base.difficulty() == "High"
    assert base.version() == 4735

    nofr = SaveFile(fixture_variant("no-freighter")["save"]).container_map()
    assert "freighter" not in nofr, "no-freighter drops FreighterInventory"

    nosh = SaveFile(fixture_variant("no-ships")["save"]).container_map()
    assert "ship0" not in nosh, "no-ships empties ShipOwnership"

    twelve = SaveFile(fixture_variant("twelve-chests")["save"]).container_map()
    assert "chest10" in twelve, "twelve-chests keeps the ten the model knows"

    ex = SaveFile(fixture_variant("with-exocraft")["save"]).container_map()
    assert "vehicle0" in ex and "vehicle0_tech" in ex, \
        "with-exocraft adds a VehicleOwnership entry: %s" % sorted(ex)

    zero = SaveFile(fixture_variant("zero-chests")["save"]).container_map()
    # `chestmagic` and `chestmagic2` are the base capsules, not storage
    # containers, and the variant leaves them alone.
    numbered = [k for k in zero if re.match(r"^chest\d+$", k)]
    assert not numbered, \
        "zero-chests removes every ChestNInventory: %s" % sorted(numbered)

    assert SaveFile(fixture_variant("version-4800")["save"]).version() == 4800, \
        "version-4800 raises the root Version"
    assert SaveFile(fixture_variant("low-limits")["save"]).difficulty() == "Low", \
        "low-limits sets InventoryStackLimitsDifficulty"

    exp = meta_read(fixture_variant("expedition")["meta"])
    assert exp["season"] != 0, "expedition sets the mf_ season word"
    m03 = meta_read(fixture_variant("mf-format-2003")["meta"])
    assert m03["raw_len"] == 384 and m03["format"] == 2003, \
        "mf-format-2003 is a 384-byte mf_ with format 2003"
    mh = meta_read(fixture_variant("mf-hash-set")["meta"])
    assert any(mh["spooky"]) and any(mh["sha256"]), \
        "mf-hash-set fills 0x08..0x37, which the base fixture leaves zero"
    assert not any(meta_read(fixture_variant("base")["meta"])["sha256"]), \
        "...and the base fixture really does leave them zero"


# ---------------------------------------------------------------- real saves


def test_list_saves_reads_a_real_folder(real_saves_dir):
    rows = list_saves(real_saves_dir)
    assert len(rows) == 9, \
        "the corpus folder holds nine saveN.hg: %s" % [r["file"] for r in rows]
    assert all(r["error"] is None for r in rows), \
        "every mf_ decodes: %s" % [(r["file"], r["error"]) for r in rows if r["error"]]
    assert all(r["meta"] and r["meta"]["sizes_match"] for r in rows), \
        "every mf_ agrees with its save's size on disk"


def test_no_real_save_shows_a_game_marker_as_a_vessel_name(real_saves_dir):
    """Across the whole corpus: no container label is a marker, and every
    vessel with a hull the class table knows says what it is.

    save3 and save4 carry `MainShip`, which is what this closes; save5 and
    save6 carry `Sarco`, which must survive it.
    """
    seen = set()
    for row in list_saves(real_saves_dir):
        try:
            save = SaveFile(row["path"])
        except UnsupportedSave:
            continue                    # the two pre-Waypoint saves
        for c in save.containers():
            seen.add(c.label)
            for marker in savemodel.VESSEL_NAME_MARKERS:
                assert marker not in c.label, \
                    "%s: %s is on screen as %r" % (row["file"], marker, c.label)
    assert "Sarco" in seen, "a real player name survives the filter"
    assert "S-FreightXXX" in seen,         "and so does a generated freighter name, which is not a marker: the "         "freighter's label went through `text.safe` and not through "         "`vessel_name`, so it was the one name that skipped the marker filter"
    assert "Fighter 1" in seen, "a hull the class table knows says so"
    assert "Colossus" in seen, "the exocraft table names the slots"


def test_the_vessel_in_use_on_a_real_save_is_the_one_the_save_names(
        real_saves_dir):
    """save10: `PrimaryShip` is 1 and `PrimaryVehicle` is 2."""
    cm = SaveFile(os.path.join(real_saves_dir, "save10.hg")).container_map()
    in_use = sorted(k for k, c in cm.items() if c.in_use)
    assert in_use == ["ship1", "ship1_cargo", "ship1_tech", "vehicle2",
                      "vehicle2_tech"], in_use
    assert cm["vehicle2"].label == "Colossus"


@pytest.mark.parametrize("name", ["save.hg", "save2.hg"])
def test_a_pre_waypoint_real_save_is_refused_by_name(real_saves_dir, name):
    """A real layout this build cannot navigate is refused, not shown empty.

    Both of these are save version 4645, which keeps `PlayerStateData` at the
    document root with no `BaseContext` above it. They used to load cleanly and
    report zero containers -- no exception, no refusal, nothing to see, and a
    page that looked like an empty save rather than an unsupported one.
    """
    path = os.path.join(real_saves_dir, name)
    if not os.path.exists(path):
        pytest.skip("%s is not in the corpus" % name)
    with pytest.raises(UnsupportedSave) as exc:
        SaveFile(path)
    assert "saves older than Waypoint (4.0) are not supported" in str(exc.value), \
        "the refusal says what is wrong: %s" % exc.value


@pytest.mark.parametrize("name", ["save10.hg", "save8.hg"])
def test_a_supported_real_save_still_loads(real_saves_dir, name):
    path = os.path.join(real_saves_dir, name)
    if not os.path.exists(path):
        pytest.skip("%s is not in the corpus" % name)
    save = SaveFile(path)
    assert isinstance(save.version(), int), \
        "%s loads and reports a save version: %r" % (name, save.version())
    assert save.container_map(), "%s finds containers" % name


# ===================================================== defect register P1-2


# --- D8: storage containers are discovered, not listed ---------------------


def test_a_save_with_twelve_chests_reports_twelve(fixture_variant):
    """D8. `range(1, 11)` capped the model at ten. The live memory dump shows
    `chest11` and `chest12` exist, and a plan that named one would have been
    refused at write-path step 7 as a container with no path."""
    cm = SaveFile(fixture_variant("twelve-chests")["save"]).container_map()
    assert "chest11" in cm and "chest12" in cm, \
        "chest11 and chest12 are found: %s" % sorted(k for k in cm
                                                     if k.startswith("chest"))


def test_the_eleventh_chest_is_a_sortable_storage_container(fixture_variant):
    cm = SaveFile(fixture_variant("twelve-chests")["save"]).container_map()
    c = cm["chest11"]
    assert c.sortable and c.allocated and not c.is_tech, \
        "chest11 is sortable: %s %s %s" % (c.sortable, c.allocated, c.is_tech)
    assert c.section == "Storage" and c.label == "Storage Container 11", \
        "and labelled the way the game labels it: %r %r" % (c.section, c.label)


def test_the_eleventh_chest_has_a_path_the_write_path_can_name(fixture_variant):
    """The step-7 guard maps a plan's containers to JSON paths. `Chest11Inventory`
    has no entry in the key map, so the game writes it in the clear and the path
    has to be the plain name."""
    save = SaveFile(fixture_variant("twelve-chests")["save"])
    c = save.container_map()["chest11"]
    assert c.path == ["BaseContext", "PlayerStateData", "Chest11Inventory"], \
        "the path is the key the save really carries: %s" % c.path
    assert save.d.at(c.path) is c.node, "and it resolves back to the node"


def test_a_save_with_no_chests_reports_none(fixture_variant):
    cm = SaveFile(fixture_variant("zero-chests")["save"]).container_map()
    assert not [k for k in cm if re.match(r"^chest\d+$", k)], \
        "zero-chests has no storage containers: %s" % sorted(cm)


def test_the_chests_come_out_in_numeric_order(fixture_variant):
    keys = [c.key for c in SaveFile(fixture_variant("twelve-chests")["save"]
                                    ).containers()
            if re.match(r"^chest\d+$", c.key)]
    assert keys == ["chest%d" % n for n in range(1, 13)], \
        "numeric order, so chest2 precedes chest10: %s" % keys


def test_discovery_reads_both_obfuscated_and_plain_keys(fixture_variant):
    """Chest1 is written under its three-character obfuscated name; Chest11 is
    not in the key map at all and is written in the clear. Both are storage."""
    save = SaveFile(fixture_variant("twelve-chests")["save"])
    ps = save.d.player
    rows = dict((k, plain) for k, plain, _ in savemodel.discover_chests(save.d, ps))
    assert rows.get("chest1") == "Chest1Inventory" \
        and rows.get("chest11") == "Chest11Inventory", rows
    assert save.d.rev.get("Chest1Inventory") in ps, \
        "chest1 really is stored under an obfuscated key"
    assert "Chest11Inventory" in ps, \
        "chest11 really is stored under its plain name"


# --- D23: the key map is read once per process -----------------------------


def test_the_key_map_is_read_once_per_process(synthetic_path, monkeypatch):
    """D23. `jsonmap` was re-read and re-parsed on every `SaveFile`, and the
    page builds one per request."""
    calls = []
    real = savemodel.load_keymap

    def counted(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(savemodel, "load_keymap", counted)
    monkeypatch.setattr(savemodel, "_KEYMAP", None)
    SaveFile(synthetic_path)
    SaveFile(synthetic_path)
    savemodel.Doc({})
    assert len(calls) == 1, "one read for three documents: %d" % len(calls)


def test_the_cached_map_is_the_real_one(synthetic_save):
    assert synthetic_save.d.rev.get("BaseContext"), \
        "the cached map still maps plain names to obfuscated ones"


# --- extractor cores against extractor rooms -------------------------------
#
# The cores are matched to the rooms they stand in, one object at a time, and
# not counted against them. The count check these tests used to assert was the
# only thing wrong with the operator's own save -- 17 buffers against 14 rooms,
# three of them left behind by rebuilt rooms -- and it switched the whole
# drain feature off on the one save it was written for.


def _freighter_base(save, rooms, placed=True):
    """Give the save a freighter base with `rooms` extractor rooms in it.

    The rooms go on the 8-metre grid the game uses, in the freighter base's own
    frame. `placed=False` leaves the `Position` off entirely, which is the
    shape of a room this code cannot locate -- and the shape this helper used
    to build unconditionally, back when only the count was read.
    """
    from tools.make_fixture import _Keys
    o = _Keys()

    def room(n):
        node = {"ObjectID": "^FRE_ROOM_EXTR"}
        if placed:
            node["Position"] = [-8.0 * (n + 1), 4.0, 35.5]
        return o(node)

    save.d.player[o.name("PersistentPlayerBases")] = [o({
        "BaseType": o({"PersistentBaseTypes": "FreighterBase"}),
        "Objects": [room(n) for n in range(rooms)]})]
    return save


def test_one_room_one_core_matches_and_says_nothing(synthetic_path):
    save = _freighter_base(SaveFile(synthetic_path), 1)
    assert save.extractor_rooms() == 1, "the matching reads the base"
    assert [c.key for c in save.extractor_containers()] == ["extractor1"], \
        "one core, one room, offered by name"
    assert save.extractor_issue is None, "and nothing to report"
    assert save.extractor_containers()[0].room_index == 1, \
        "the container knows which room it is in"


def test_a_room_with_no_buffer_is_named_and_the_rest_still_offered(
        synthetic_path):
    """The replacement for "the counts disagree, so you get none of them".

    Three rooms and one buffer is a real mismatch -- two rooms genuinely have
    no core in this save -- and the answer is now two rooms named and one core
    offered, rather than the whole feature switched off. Rule 1 refuses the
    thing that cannot be named; a room without a buffer is exactly that, and
    the buffer that *did* match a room is not.
    """
    save = _freighter_base(SaveFile(synthetic_path), 3)
    said = save.extractor_issue
    assert said == "2 of 3 extractor rooms have no core buffer", \
        "the sentence counts the rooms it could not account for: %s" % said
    assert save.extractor_room_mismatch() == said, \
        "the planner's name for it is the same sentence"
    assert [c.key for c in save.extractor_containers()] == ["extractor1"], \
        "and the one core that did match a room is still offered"
    assert len(save.containers()) > 10, \
        "while every other container is still there"
    with pytest.raises(ValueError) as exc:
        save.extractor_containers(strict=True)
    assert said in str(exc.value), "strict mode raises it: %s" % exc.value


def test_rooms_with_no_position_can_match_nothing_and_say_so(synthetic_path):
    """A room whose `Position` will not read is not quietly dropped.

    It is the shape this file's own helper used to build, and under the old
    count check it counted as a room like any other. There is no way to match
    it, so it reports as a room with no buffer and the buffer reports as
    matching no room -- both sentences at once, which is the only honest
    answer.
    """
    save = _freighter_base(SaveFile(synthetic_path), 1, placed=False)
    rep = save.extractor_report()
    assert rep["rooms"] == 1 and rep["live"] == 0, \
        "the room is counted and nothing is matched to it: %s" % rep
    assert rep["registered"] is False, "there was nothing to register"
    assert len(rep["unplaced"]) == 1 and rep["coreless"] == [1], rep
    assert save.extractor_issue == (
        "1 of 1 extractor rooms have no core buffer; "
        "1 core buffers matched no room and are not offered"), \
        save.extractor_issue
    assert save.extractor_containers() == [], "and nothing is offered"


def test_rooms_are_counted_across_every_freighter_base(synthetic_path):
    """A save carries eleven bases; nothing promises the freighter is the
    first, or that there is only one."""
    from tools.make_fixture import _Keys
    o = _Keys()
    save = SaveFile(synthetic_path)
    def base(kind, rooms):
        return o({"BaseType": o({"PersistentBaseTypes": kind}),
                  "Objects": [o({"ObjectID": "^FRE_ROOM_EXTR"})
                              for _ in range(rooms)]})
    save.d.player[o.name("PersistentPlayerBases")] = [
        base("HomePlanetBase", 5), base("FreighterBase", 1),
        base("FreighterBase", 2)]
    assert save.extractor_rooms() == 3,         "both freighter bases count, the home planet base does not: %s"         % save.extractor_rooms()


def test_a_configured_extractor_source_beyond_the_cores_is_missing(
        synthetic_path, synthetic_config):
    """A key past the last core is still the ordinary missing-container note.

    The numbering is dense over the cores on offer, so a save with one
    identified core has an `extractor1` and no `extractor2`, whatever its room
    count is. The plan says both things: the key it could not find, and the
    two rooms that explain why there are not more.
    """
    from nms_sorter import planner
    save = _freighter_base(SaveFile(synthetic_path), 3)
    cfg = dict(synthetic_config, sources=["extractor1", "extractor2"])
    plan, _commit = planner.build_plan(save, cfg)
    said = [n["message"] for n in plan.notes]
    assert any("'extractor2' is not a container in this save" in m
               for m in said), "%s" % said
    assert not any("'extractor1' is not a container" in m for m in said), \
        "while the core that was identified is a source: %s" % said
    assert any("extractor rooms have no core buffer" in m for m in said), \
        "and the plan carries the reason there is only one: %s" % said


def test_no_freighter_base_means_nothing_to_match_against(synthetic_save):
    """Extractors deployed on a planet rather than in a freighter.

    There is no room to match a buffer to, which is not a failure to match: the
    buffers are offered in position order with no room number, because that is
    the most that can be said.
    """
    assert synthetic_save.extractor_rooms() is None, \
        "a save with no freighter base has nothing to match against"
    cs = synthetic_save.extractor_containers()
    assert [c.key for c in cs] == ["extractor1"], \
        "and the cores are still offered"
    assert cs[0].room_index is None, "with no room number to give them"
    assert synthetic_save.extractor_issue is None, "and nothing to report"


# --- the stale buffer a rebuilt room leaves behind -------------------------


def _stale_save(fixture_variant):
    return SaveFile(fixture_variant("extractor-stale")["save"])


def test_the_two_position_fields_are_in_different_frames(fixture_variant):
    """The measurement the whole matching is shaped by, asserted.

    `RefinerBufferKeys[i].Position` and a base object's `Position` are not in
    the same frame: on the operator's `save10.hg` the nearest room to any
    buffer is 1,697 m away, so "find the nearest room" over the raw numbers
    matches nothing at all. The fixture carries the same offset, and this test
    exists so that a reader who reaches for a plain distance test finds out
    here rather than on the real save.
    """
    save = _stale_save(fixture_variant)
    cores = [savemodel._vec3(c.position) for c in save.extractors()]
    rooms = [r["position"] for r in save.extractor_room_objects()]
    raw = min(savemodel._dist(c, r) for c in cores for r in rooms)
    assert raw > 1000, \
        "the two frames are far apart, so a raw distance test is hopeless: %s" % raw
    moved = savemodel.register_rooms(cores, rooms)
    assert moved, "registering the frames is what makes a distance mean anything"
    best = min(savemodel._dist(c, m) for c in cores for m in moved)
    assert best < 0.01, "and then a buffer lands on its room: %s" % best


def test_a_stale_buffer_is_matched_to_its_room_and_not_offered(fixture_variant):
    """Three rooms, four buffers, and the fourth is not inventory.

    The leftover buffer holds 99 units, which the live one on that room never
    does, so "the stale buffer was offered" cannot pass by accident.
    """
    save = _stale_save(fixture_variant)
    rep = save.extractor_report()
    assert (rep["rooms"], rep["live"]) == (3, 3), rep
    assert len(rep["stale"]) == 1 and not rep["unplaced"], rep
    assert rep["coreless"] == [], "every room has a live buffer: %s" % rep
    assert save.extractor_issue is None, \
        "a rebuilt room is not something to report: %s" % save.extractor_issue
    stale = rep["stale"][0]
    assert stale["room"] == 2 and stale["index"] == 3, \
        "the leftover is matched to the room it used to serve: %s" % stale
    assert round(stale["distance"], 3) == 0.141, \
        "0.14 m from the live one, as measured on save10: %s" % stale
    held = [[(s.id, s.amount) for s in c.slots()]
            for c in save.extractor_containers()]
    assert [k for k in (c.key for c in save.extractor_containers())] == \
        ["extractor1", "extractor2", "extractor3"], held
    assert not any(a == 99 for rows in held for _id, a in rows), \
        "and what it holds is never offered: %s" % held


def test_the_newest_timestamp_wins_not_the_buffer_index(fixture_variant):
    """Which of two buffers on one room is live is read, not guessed.

    The fixture writes the leftover *last*, so it has the higher
    `RefinerBufferData` index: a tie-break that fell back to the index would
    pick exactly the wrong one, and the 99 above would be on offer.
    """
    save = _stale_save(fixture_variant)
    live = dict((c.room_index, c) for c in save.extractor_containers())
    room2 = live[2]
    assert room2.path[3] == 1, \
        "the live buffer on room 2 is the earlier array entry: %s" % room2.path
    amounts = dict((s.id, s.amount) for s in room2.slots())
    assert amounts.get("^STELLAR2") == 20, \
        "and it is the one the game last updated: %s" % amounts
    match = save.extractor_match()
    live2 = [w for w in match["live"] if w["room"] == 2][0]
    stale = match["stale"][0]
    assert stale["core"].index > live2["core"].index, \
        "the leftover is the later array entry"
    assert stale["core"].last_update < live2["core"].last_update, \
        "and the older timestamp, which is the half that decides it"


def test_the_numbering_does_not_move_when_the_buffers_are_shuffled(
        fixture_variant):
    """`extractor2` is the second room, not the second array entry.

    The array order is the one thing a rebuilt room changes, and it was what
    the keys used to be derived from. Every rotation of the four buffers is
    tried, in lockstep across `RefinerBufferData` and `RefinerBufferKeys`, and
    each key has to keep naming the same room and the same contents.
    """
    save = _stale_save(fixture_variant)
    d = save.d

    def shot():
        return [(c.key, c.room_index,
                 tuple(sorted((s.id, s.amount) for s in c.slots())))
                for c in save.extractor_containers()]

    first = shot()
    assert len(first) == 3 and [k for k, _r, _h in first] == \
        ["extractor1", "extractor2", "extractor3"], first
    data = list(d.get(d.player, "RefinerBufferData"))
    keys = list(d.get(d.player, "RefinerBufferKeys"))
    for turn in range(1, len(data)):
        d.set(d.player, "RefinerBufferData", data[turn:] + data[:turn])
        d.set(d.player, "RefinerBufferKeys", keys[turn:] + keys[:turn])
        assert shot() == first, \
            "rotating the buffer array by %d moved the numbering" % turn


def test_a_buffer_that_matches_no_room_is_reported_and_dropped(fixture_variant):
    """A fifth buffer, nowhere near the freighter's three rooms.

    The three live buffers and the leftover vote the registration in between
    them, so the odd one out is genuinely unmatched rather than able to drag
    the whole fit towards itself -- which is what a centroid alignment would
    have let it do.
    """
    save = _stale_save(fixture_variant)
    d = save.d
    data = d.get(d.player, "RefinerBufferData")
    keys = d.get(d.player, "RefinerBufferKeys")
    import copy
    data.append(copy.deepcopy(data[0]))
    lost = copy.deepcopy(keys[0])
    d.set(lost, "Position", [1000.0, 2000.0, 3000.0])
    keys.append(lost)
    rep = save.extractor_report()
    assert (rep["rooms"], rep["live"]) == (3, 3), \
        "the three rooms still have their own buffers: %s" % rep
    assert len(rep["stale"]) == 1, rep
    assert [w["index"] for w in rep["unplaced"]] == [4], \
        "and the fifth buffer is the one nothing can be said about: %s" % rep
    assert save.extractor_issue == \
        "1 core buffers matched no room and are not offered", \
        save.extractor_issue
    assert len(save.extractor_containers()) == 3, \
        "it is not offered, and it does not cost the others their names"


# --- an unsupported document shape -----------------------------------------


def test_a_pre_waypoint_layout_is_refused(fixture_variant):
    with pytest.raises(UnsupportedSave) as exc:
        SaveFile(fixture_variant("pre-waypoint")["save"])
    assert "this save has no BaseContext" in str(exc.value), \
        "the refusal names the missing node: %s" % exc.value
    assert "4645" in str(exc.value), \
        "and the version it did manage to read: %s" % exc.value


def test_an_unsupported_save_is_a_value_error(fixture_variant):
    """So a caller that only knows `ValueError` still gets a sentence rather
    than a traceback about a NoneType."""
    with pytest.raises(ValueError):
        SaveFile(fixture_variant("pre-waypoint")["save"])


def test_listing_a_folder_of_unsupported_saves_does_not_raise(fixture_variant):
    """`list_saves` reads the mf_ and the directory entry, never the document,
    which is what keeps it fast and what keeps the save browser renderable
    when one of the saves cannot be loaded."""
    folder = os.path.dirname(fixture_variant("pre-waypoint")["save"])
    rows = list_saves(folder)
    assert len(rows) == 1 and rows[0]["error"] is None, \
        "the row is listed with its metadata: %s" % rows


# --- save_version -----------------------------------------------------------


def test_save_version_reads_the_root_version_whatever_the_layout(
        fixture_variant, synthetic_payload):
    from nms_sorter.codec import loads as codec_loads, read_payload as rp
    assert savemodel.save_version(codec_loads(synthetic_payload)) == 4735, \
        "the supported layout"
    pre, _ = rp(fixture_variant("pre-waypoint")["save"])
    assert savemodel.save_version(codec_loads(pre)) == 4645, \
        "and the one that has no BaseContext to hang it off"
    assert savemodel.save_version(None) is None \
        and savemodel.save_version({}) is None, \
        "nothing to read is None, not an exception"


# --- the whole corpus, by name ---------------------------------------------


def test_every_real_save_either_loads_with_containers_or_is_refused_by_name(
        real_save_paths):
    """The gate keys on `BaseContext/PlayerStateData` and nothing else.

    Not on the save `Version` and never on the mf_'s `base_version`, which
    reads 4223 on a current 7.01 save and is not the save format version at
    all. Only the two pre-Waypoint saves may be refused; every other save in
    the corpus has to load and find containers, because that is what the
    operator browses.
    """
    loaded, refused = {}, {}
    for path in real_save_paths:
        name = os.path.basename(path)
        try:
            save = SaveFile(path)
        except UnsupportedSave as exc:
            refused[name] = str(exc)
            continue
        loaded[name] = len(save.containers())
    assert sorted(refused) == ["save.hg", "save2.hg"], \
        "only the two pre-Waypoint saves are refused: %s" % sorted(refused)
    assert loaded, "and the rest load: %s" % loaded
    assert all(n > 0 for n in loaded.values()), \
        "each with containers in it: %s" % loaded
    assert loaded.get("save10.hg", 0) > 50, \
        "save10.hg is the current one and is full of them: %s" % loaded


def test_the_real_current_save_resolves_all_fourteen_extractor_rooms(
        real_saves_dir):
    """save10.hg is the save the operator plays, and the reason for all of this.

    Seventeen buffers, fourteen rooms, and the three extra are leftovers from
    rooms that were rebuilt: `RefinerBufferData[84]`, `[85]` and `[86]`, each
    0.14 m from a live buffer and each last updated about four and a half hours
    earlier. Under the count check every `extractorN` in the operator's own
    config read as "not a container in this save"; identified one at a time,
    all fourteen rooms come back.
    """
    path = os.path.join(real_saves_dir, "save10.hg")
    if not os.path.exists(path):
        pytest.skip("save10.hg is not in the corpus")
    save = SaveFile(path)
    assert len(save.containers()) > 50, \
        "the save loads with its containers: %d" % len(save.containers())
    assert len(save.extractors()) == 17, "seventeen buffers match the filter"
    rep = save.extractor_report()
    assert (rep["rooms"], rep["live"]) == (14, 14), \
        "and every one of the fourteen rooms has a live buffer: %s" \
        % {"rooms": rep["rooms"], "live": rep["live"]}
    assert sorted(w["index"] for w in rep["stale"]) == [84, 85, 86], \
        "the three leftovers are named: %s" % rep["stale"]
    assert not rep["unplaced"] and not rep["coreless"], rep
    assert max(w["distance"] for w in rep["stale"]) < 0.2, \
        "each sits a hair off the live one: %s" % rep["stale"]
    live = save.extractor_match()["live"]
    assert all(w["distance"] < 0.01 for w in live), \
        "while a live buffer lands on its room: %s" \
        % [w["distance"] for w in live]
    assert min(w["core"].last_update for w in live) > \
        max(w["last_update"] for w in rep["stale"]), \
        "and every live buffer was updated after every stale one"
    assert save.extractor_issue is None, \
        "so there is nothing left to report: %s" % save.extractor_issue
    assert [c.key for c in save.extractor_containers()] == \
        ["extractor%d" % n for n in range(1, 15)], \
        "extractor1..14 are back"
    assert [c.room_index for c in save.extractor_containers()] == \
        list(range(1, 15)), "one per room, in room order"


# ===========================================================================
# P4-2: the save gates
# ===========================================================================
#
# Two gates, two levels. An expedition is a `refuse` and stops the plan; a
# version outside the range anything was measured on is a `warn`, so the plan
# is printed with a banner and `safety.apply_plan` step 1b refuses. The
# reasoning is in DECISIONS.md (2026-09-14, expedition saves) and in
# `SaveFile.gates`.


def test_the_base_fixture_has_no_gates(synthetic_save):
    assert synthetic_save.gates() == [], \
        "a save inside the tested range and not an expedition passes both gates"


def test_the_supported_version_range_is_the_corpus():
    """4670 to 4735 inclusive, and the floor is 4670 because that is the oldest
    save in the operator's corpus that carries `BaseContext`: `save6.hg` is
    4670, `save3/4/5/7` are 4734 and `save9/10` are 4735. 4645 (`save.hg`,
    `save2.hg`) has no `BaseContext` and is refused before a version gate could
    speak, which is why the floor is not 4645."""
    assert savemodel.SUPPORTED_VERSIONS == (4670, 4735), \
        "the range is the corpus, not a guess: %s" % (
            savemodel.SUPPORTED_VERSIONS,)
    lo, hi = savemodel.SUPPORTED_VERSIONS
    assert lo <= 4670 <= hi and lo <= 4734 <= hi and lo <= 4735 <= hi, \
        "every version the corpus proved loads is inside it"
    assert not lo <= 4645 <= hi, \
        "and 4645 is outside it: that save has no BaseContext at all"


def test_an_expedition_save_is_refused_at_plan_time(fixture_variant,
                                                    synthetic_config):
    """The `mf_` season word is non-zero *and* `SeasonData.Inventory` holds a
    stack. Either alone is the detection; the fixture carries both."""
    save = SaveFile(fixture_variant("expedition")["save"])
    assert save.season_word() != 0, "the mf_ says season 1"
    assert save.season_slots() == 1, "and the document carries a season stack"
    gates = save.gates()
    assert [g["where"] for g in gates] == ["expedition"], \
        "one gate, and it is the expedition one: %s" % gates
    assert gates[0]["level"] == "refuse"
    assert "expedition save" in gates[0]["message"]

    with pytest.raises(savemodel.SaveGate) as exc:
        planner.build_plan(save, synthetic_config)
    assert "expedition" in str(exc.value), \
        "and the planner raises it rather than printing a plan: %s" % exc.value


def test_the_season_word_alone_is_an_expedition(fixture_variant, tmp_path):
    """A save whose season word is set and whose `SeasonData` is empty is still
    an expedition. Half the detection has to stand on its own, or a save that
    carries only one of the two signals is planned."""
    target = str(tmp_path / "save9.hg")
    shutil.copy2(fixture_variant("base")["save"], target)
    shutil.copy2(fixture_variant("expedition")["meta"],
                 str(tmp_path / "mf_save9.hg"))
    save = SaveFile(target)
    assert save.season_slots() == 0, "the document half says nothing"
    assert [g["where"] for g in save.gates()] == ["expedition"], \
        "the mf_ half is enough on its own"


def test_a_save_with_no_metadata_has_no_season_word(tmp_path, fixture_variant):
    """`season_word()` is None, not 0, when there is nothing to read -- and
    None must not read as "an expedition"."""
    target = str(tmp_path / "save9.hg")
    shutil.copy2(fixture_variant("base")["save"], target)
    save = SaveFile(target)
    assert save.meta_path() is None and save.season_word() is None
    assert save.gates() == [], "no metadata is not an expedition"


def test_a_version_outside_the_range_is_a_note_not_a_refusal(
        fixture_variant, synthetic_config):
    """Planning a save from a build we have not seen is how the corpus that
    would widen the range gets collected. Writing to one is not.

    `note`, not `warn`: the level is what the page and step 1b act on, and
    with `strict_version_check` off the apply *runs*, which is what a note
    means here. The sentence is the whole sentence, and it does not quote the
    refusal's wording."""
    save = SaveFile(fixture_variant("version-4800")["save"])
    gates = save.gates()
    assert [(g["where"], g["level"]) for g in gates] == [("version", "note")], \
        "one gate, at note level: %s" % gates
    assert gates[0]["message"] == (
        "this save reports version 4800; this build was verified on 4670 to "
        "4735. Apply proceeds: the round-trip and nothing-else-changed checks "
        "run on this exact file. Settings can turn on strict_version_check to "
        "refuse instead."), gates[0]["message"]
    assert "refused" not in gates[0]["message"], \
        ("the advisory path never says the apply is refused: %s"
         % gates[0]["message"])

    plan, _commit = planner.build_plan(save, synthetic_config)
    assert plan.rows, "the plan is still printed"
    assert any("version 4800" in n["message"] for n in plan.notes), \
        "with the banner on it: %s" % plan.notes


def test_strict_version_check_is_the_other_sentence(fixture_variant,
                                                    synthetic_config):
    """One gate, two sentences, and the mode decides which. The owner's report
    was both of them at once: the refusal's wording, then a second sentence
    contradicting it with no full stop between them."""
    save = SaveFile(fixture_variant("version-4800")["save"])
    gates = save.gates(True)
    assert [(g["where"], g["level"]) for g in gates] == [("version", "warn")], \
        "strict makes it a warning, which is the level step 1b refuses: %s" % gates
    assert gates[0]["message"] == (
        "this save reports version 4800; this build was verified on 4670 to "
        "4735, and strict_version_check is on, so apply is refused."), \
        gates[0]["message"]
    assert "fixture" not in gates[0]["message"], \
        "a fixture is a maintainer's word, not a player's"

    plan, _commit = planner.build_plan(save, synthetic_config, strict=True)
    assert any(n["message"] == gates[0]["message"] for n in plan.notes), \
        ("the plan's banner is the sentence the apply gate shows: %s"
         % plan.notes)


def test_a_version_that_is_not_a_number_is_gated(synthetic_save, monkeypatch):
    """A document whose `Version` is missing, or spelled as a string, is not
    inside any range. "I could not read the version" and "the version is fine"
    must not be the same answer."""
    monkeypatch.setattr(SaveFile, "version", lambda self: None)
    assert [g["where"] for g in synthetic_save.gates()] == ["version"]
    monkeypatch.setattr(SaveFile, "version", lambda self: "4735")
    assert [g["where"] for g in synthetic_save.gates()] == ["version"], \
        "a version spelled as a string is not a version this build checked"


# ===========================================================================
# R15: the signature carries a content hash
# ===========================================================================


def test_the_signature_carries_a_content_hash(synthetic_save):
    parts = synthetic_save.signature().split("|")
    assert len(parts) == 4, "name|size|mtime|sha256: %s" % parts
    assert parts[0] == os.path.basename(synthetic_save.path)
    assert parts[3] == synthetic_save.digest() and len(parts[3]) == 64, \
        "the fourth field is the file's SHA-256: %s" % parts[3]


def test_the_signature_describes_the_bytes_that_were_parsed(tmp_path,
                                                            fixture_variant):
    """The hash is taken in `__init__`, from the bytes `read_payload` read.

    Lazily, it described whatever was on disk when somebody first asked, so a
    cached `SaveFile` whose file had changed underneath it would answer with
    the *new* hash and compare equal to a disk it no longer matched -- which is
    R15 again, one layer down. Eager, `signature()` means one thing: the
    document in this object came from these bytes.
    """
    target = str(tmp_path / "save9.hg")
    shutil.copy2(fixture_variant("base")["save"], target)
    save = SaveFile(target)
    was = save.signature()
    digest = save.digest()
    assert digest == hashlib.sha256(open(target, "rb").read()).hexdigest()

    # A different, still perfectly valid save lands on the same path.
    shutil.copy2(fixture_variant("low-limits")["save"], target)
    assert save.digest() == digest, \
        "the object still describes the bytes it parsed"
    assert save.signature() == was, \
        "and so does its signature, whenever it is asked"
    assert SaveFile(target).digest() != digest, \
        "while a fresh read sees the new bytes"


def test_a_same_size_same_second_edit_moves_the_signature(tmp_path,
                                                          fixture_variant):
    """The measured hole (review one, R15): a same-length edit written back
    with the original timestamp kept the name, the size and the integer second,
    so the signature did not move and a plan pinned to it stayed approvable."""
    target = str(tmp_path / "save9.hg")
    shutil.copy2(fixture_variant("base")["save"], target)
    was = os.stat(target)
    before = SaveFile(target).signature()

    edited = SaveFile(target)
    edited.d.set(edited.d.player, "Units", 12344)      # same number of digits
    framed = frame_payload(dumps(edited.doc))
    assert len(framed) == was.st_size, "the same size, or this proves nothing"
    with open(target, "wb") as fh:
        fh.write(framed)
    os.utime(target, (was.st_atime, was.st_mtime))

    after = SaveFile(target)
    assert int(after.stat.st_mtime) == int(was.st_mtime) and \
        after.stat.st_size == was.st_size, \
        "name, size and second are all unmoved, which is the point"
    assert after.signature() != before, \
        "and the signature moved anyway, because the bytes did"


# ===========================================================================
# R3: list_saves surfaces a pair that disagrees
# ===========================================================================


def test_list_saves_reports_whether_the_pair_agrees(fixture_variant):
    rows = dict((r["file"], r) for r in
                list_saves(os.path.dirname(fixture_variant("base")["save"])))
    row = rows["save9.hg"]
    assert row["sizes_match"] is True, \
        "the fixture's mf_ records the size of the file beside it: %s" % row


def test_a_save_whose_metadata_denies_its_size_says_so(tmp_path,
                                                       fixture_variant):
    """What a write killed between step 9 and step 10 leaves behind: the save
    replaced, the mf_ still carrying the old two sizes. `sizes_match` was
    computed and read by nobody (review one, R3)."""
    shutil.copy2(fixture_variant("base")["save"], str(tmp_path / "save9.hg"))
    shutil.copy2(fixture_variant("base")["meta"], str(tmp_path / "mf_save9.hg"))
    with open(str(tmp_path / "save9.hg"), "ab") as fh:
        fh.write(b"\x00" * 16)              # the save grew; the mf_ did not
    row = [r for r in list_saves(str(tmp_path)) if r["file"] == "save9.hg"][0]
    assert row["sizes_match"] is False, \
        "a pair that disagrees is reported as one: %s" % row
    assert row["meta"]["sizes_match"] is False, \
        "and the nested copy still says it too, for callers that read that"


def test_a_save_with_no_metadata_has_no_opinion_on_the_pair(tmp_path,
                                                            fixture_variant):
    shutil.copy2(fixture_variant("base")["save"], str(tmp_path / "save9.hg"))
    row = list_saves(str(tmp_path))[0]
    assert row["sizes_match"] is None and row["meta"] is None, \
        "no metadata is not a disagreement: %s" % row


# ===========================================================================
# P6-5 (write-path review two): Q6, Q9, Q12
# ===========================================================================


def test_the_content_signature_carries_no_timestamp(synthetic_save):
    """Q6. `signature()` is what the save cache compares and keeps its mtime;
    the *plan* is pinned to this one, because an apply re-plans from the
    backup copy and only `shutil.copy2` made the copy's mtime match."""
    parts = synthetic_save.content_signature().split("|")
    assert len(parts) == 3, "name|size|sha256: %s" % parts
    assert parts[0] == os.path.basename(synthetic_save.path)
    assert parts[1] == str(os.path.getsize(synthetic_save.path))
    assert parts[2] == synthetic_save.digest() and len(parts[2]) == 64
    assert str(int(synthetic_save.stat.st_mtime)) not in parts, \
        "nothing a copy onto exFAT or a sync client can move: %s" % parts


def test_the_plan_fingerprint_does_not_move_with_the_timestamp(tmp_path,
                                                               synthetic_path,
                                                               synthetic_config):
    """Q6, end to end: the same bytes under two different mtimes fingerprint
    the same, which is what makes step 5 survive a backup root that does not
    preserve times."""
    a = str(tmp_path / "save9.hg")
    b = str(tmp_path / "copy" / "save9.hg")
    os.makedirs(os.path.dirname(b))
    shutil.copyfile(synthetic_path, a)
    shutil.copyfile(synthetic_path, b)
    os.utime(b, (0, 0))
    pa, _ca = planner.build_plan(SaveFile(a), synthetic_config)
    pb, _cb = planner.build_plan(SaveFile(b), synthetic_config)
    assert pa.fingerprint_full == pb.fingerprint_full, \
        "%s vs %s" % (pa.fingerprint, pb.fingerprint)


def test_the_metadata_path_is_the_spelling_on_disk(tmp_path, fixture_variant):
    """Q9. `"mf_" + basename` is a name this code invents; on a
    case-insensitive filesystem it finds a differently spelled file and used
    to hand back the invented spelling, which step 10 then renamed."""
    save = str(tmp_path / "save9.hg")
    shutil.copy2(fixture_variant("base")["save"], save)
    shouty = str(tmp_path / "mf_SAVE9.HG")
    shutil.copy2(fixture_variant("base")["meta"], shouty)
    if not os.path.exists(str(tmp_path / "mf_save9.hg")):
        pytest.skip("a case-sensitive filesystem: there is no pair to confuse")
    assert SaveFile(save).meta_path() == shouty, \
        "the directory listing decides: %s" % SaveFile(save).meta_path()


def test_list_saves_finds_a_metadata_file_spelled_differently(tmp_path,
                                                              fixture_variant):
    """Q9, on the read path: the row's `meta` comes from the file that is
    there, under the name that is there."""
    shutil.copy2(fixture_variant("base")["save"], str(tmp_path / "save9.hg"))
    shutil.copy2(fixture_variant("base")["meta"], str(tmp_path / "mf_SAVE9.HG"))
    if not os.path.exists(str(tmp_path / "mf_save9.hg")):
        pytest.skip("a case-sensitive filesystem: there is no pair to confuse")
    row = [r for r in list_saves(str(tmp_path)) if r["file"] == "save9.hg"][0]
    assert row["meta"] is not None and row["error"] is None, \
        "the pair is read, not reported as an error: %s" % row


def test_an_expedition_with_an_empty_season_inventory_and_no_metadata_is_noted(
        tmp_path, fixture_variant, synthetic_config):
    """Q12. `season_word()` answers None both for "no mf_" and for "there is
    an mf_ and this is not an expedition", so with the season inventory empty
    the gate rested on nothing and said nothing. It is a `note` now: the plan
    is printed, the apply runs, and the sentence is on the record."""
    save = str(tmp_path / "save9.hg")
    doc = savemodel.loads(read_payload(fixture_variant("expedition")["save"])[0])
    d = savemodel.Doc(doc)
    inv = d.at(["CommonStateData", "SeasonData", "Inventory"])
    assert inv is not None, "the expedition variant carries a season inventory"
    d.set(inv, "Slots", [])
    with open(save, "wb") as fh:
        fh.write(frame_payload(dumps(doc)))
    sf = SaveFile(save)
    assert sf.has_season_data() is True and sf.meta_path() is None
    gates = sf.gates()
    assert [(g["where"], g["level"]) for g in gates] == \
        [("expedition", "note")], "one gate, at note level: %s" % gates
    assert "cannot be confirmed from the mf_" in gates[0]["message"]

    plan, _commit = planner.build_plan(sf, synthetic_config)
    assert any("cannot be confirmed" in n["message"] for n in plan.notes), \
        "the plan carries the banner: %s" % plan.notes


def test_an_expedition_whose_season_inventory_is_full_is_still_refused(
        fixture_variant, tmp_path):
    """The note never weakens the refusal: with stacks in the season
    inventory, a save with no `mf_` is an expedition and is refused."""
    save = str(tmp_path / "save9.hg")
    shutil.copy2(fixture_variant("expedition")["save"], save)
    sf = SaveFile(save)
    assert sf.meta_path() is None, "no metadata beside it"
    assert [(g["where"], g["level"]) for g in sf.gates()] == \
        [("expedition", "refuse")], sf.gates()


def test_a_save_with_no_season_data_and_no_metadata_has_nothing_to_note(
        tmp_path, fixture_variant):
    """The gate is the presence of the node, not the absence of an `mf_`: an
    ordinary save with no metadata beside it is not an expedition and must not
    grow a caveat, or every save the operator has copied out of the game
    without its `mf_` would carry one."""
    save = str(tmp_path / "save9.hg")
    shutil.copy2(fixture_variant("base")["save"], save)
    sf = SaveFile(save)
    assert sf.has_season_data() is False and sf.meta_path() is None
    assert sf.gates() == [], sf.gates()


# ------------------------------- two spellings of one name (review 5, note 7)


def test_two_spellings_of_one_name_are_refused_not_picked(tmp_path,
                                                          monkeypatch):
    """`on_disk_path` used to hand back the literal path it was given whenever
    `match_name` refused, and on an ambiguous listing that path does not
    exist. Measured in review 5 against a simulated listing: a request for
    `Save9.hg` came back as the folder plus `Save9.hg`, a file the callers
    would then have written to.

    The listing is faked because Windows -- the platform apply runs on --
    cannot hold two names that differ only by case, so this branch is
    reachable in code and not on an NTFS disk.
    """
    folder = str(tmp_path)
    real = os.listdir

    def listdir(path):
        if os.path.abspath(path) == os.path.abspath(folder):
            return ["save9.hg", "SAVE9.HG", "mf_save9.hg"]
        return real(path)

    monkeypatch.setattr(savemodel.os, "listdir", listdir)
    # an exact spelling still answers
    assert savemodel.on_disk_path(os.path.join(folder, "save9.hg")) == \
        os.path.join(folder, "save9.hg")
    # a name that is not in the listing at all is still the invented path: a
    # save with no mf_ beside it needs one to ask os.path.exists about
    assert savemodel.on_disk_path(os.path.join(folder, "mf_save8.hg")) == \
        os.path.join(folder, "mf_save8.hg")
    with pytest.raises(savemodel.SaveGate) as exc:
        savemodel.on_disk_path(os.path.join(folder, "Save9.hg"))
    assert "more than one file named Save9.hg" in str(exc.value), exc.value
    assert "save9.hg" in str(exc.value) and "SAVE9.HG" in str(exc.value), \
        "and the refusal lists both spellings: %s" % exc.value


def test_two_spellings_of_one_save_do_not_share_one_mf(tmp_path,
                                                       fixture_variant):
    """The `list_saves` half, on a filesystem that can really hold both.

    `SAVE_RE` is case-insensitive, so both spellings are rows -- and the
    folded lookup used to hand the one `mf_save9.hg` to both of them, which
    is two distinct saves paired to one metadata file, silently. Both rows
    stay; the metadata goes to the exact spelling only.
    """
    folder = str(tmp_path)
    shutil.copy2(fixture_variant("base")["save"],
                 os.path.join(folder, "save9.hg"))
    shutil.copy2(fixture_variant("base")["meta"],
                 os.path.join(folder, "mf_save9.hg"))
    if os.path.exists(os.path.join(folder, "SAVE9.HG")):
        pytest.skip("this filesystem resolves names case-insensitively, so "
                    "two spellings cannot be two files here")
    shutil.copy2(fixture_variant("base")["save"],
                 os.path.join(folder, "SAVE9.HG"))
    rows = list_saves(folder)
    assert sorted(r["file"] for r in rows) == ["SAVE9.HG", "save9.hg"], \
        "both spellings are listed, because both are real files: %s" % rows
    paired = {r["file"]: bool(r["meta"]) for r in rows}
    assert paired == {"save9.hg": True, "SAVE9.HG": False}, \
        ("the mf_ goes to the exact spelling only; the other row shows none "
         "rather than somebody else's metadata: %s" % paired)
