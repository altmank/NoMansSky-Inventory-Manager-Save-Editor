"""`nms_sorter.planner` over the synthetic save.

Converted from `selftest_legacy.py`, section "planner" and everything after it
that builds a plan: the overflow chain, packed technology, DamageFactor, stock,
a scoped keep floor, the extractor drain, the per-item bucket override, and
fingerprint stability. The `validate()` calls that were interleaved with those
cases moved to `test_config.py`; the item table's own shape is in
`test_itemdb.py`.

Every case builds its own `SaveFile`, as the legacy test did: `commit()`
mutates the document, so a shared one would make the suite order-dependent.
"""
import json

import pytest

from nms_sorter import planner
from nms_sorter import savemodel
from nms_sorter.itemdb import db
from nms_sorter.savemodel import SaveFile
from tools import make_fixture


def _plan(path, cfg):
    return planner.build_plan(SaveFile(path), cfg)


def _moves(plan, item_id):
    return [r for r in plan.rows if r["op"] == "move" and r["id"] == item_id]


# ============================================================== base case
#
# suit: 400 Sodium (keep 250), 300 Carbon (keep 500), 900 Chromatic Metal
# (keep 500 -> chest2), two stacks of Albumen Pearl, an unknown id, a substance
# carrying DamageFactor 1.0, a non-UTF-8 procedural id, one technology module.
# chest1 already holds 100 Sodium, parked in the last cell.


@pytest.fixture
def base_plan(synthetic_path, synthetic_config):
    plan, _commit = _plan(synthetic_path, synthetic_config)
    return plan


def test_an_id_the_table_does_not_know_is_reported_and_left_alone(base_plan):
    assert any(s["id"] == "^HULK1" for s in base_plan.skips), \
        "an id the table does not know is reported and left alone"


def test_the_non_utf8_id_is_skipped_not_guessed_at(base_plan):
    assert any(s["id"].startswith("^\\x80") for s in base_plan.skips), \
        "the non-UTF-8 id is skipped, not guessed at"


def test_technology_is_never_sorted(base_plan):
    assert any("technology" in s["reason"]
               for s in base_plan.skips if "UP_HYP1" in s["id"]), \
        "technology is never sorted"


def test_a_keep_floor_larger_than_the_holding_stops_the_move(base_plan):
    assert any(s["id"] == "^FUEL1" and "keep floor" in s["reason"]
               for s in base_plan.skips), \
        "a keep floor larger than the holding stops the move"


def test_keep_250_of_400_sodium_moves_exactly_the_surplus(base_plan):
    by_id = dict((r["id"], r) for r in base_plan.rows if r["op"] == "move")
    assert "^CATALYST1" in by_id and by_id["^CATALYST1"]["moved"] == 150, \
        ("keep 250 of 400 Sodium moves exactly the surplus of 150: %s"
         % by_id.get("^CATALYST1", {}).get("moved"))


def test_the_surplus_merges_into_the_stack_chest1_already_holds(base_plan):
    by_id = dict((r["id"], r) for r in base_plan.rows if r["op"] == "move")
    merges = by_id["^CATALYST1"]["merges"]
    assert merges and merges[0]["after"] == 250, \
        "the surplus merges into the stack chest1 already holds"


def test_keep_500_of_900_chromatic_metal_moves_400_into_chest2(base_plan):
    by_id = dict((r["id"], r) for r in base_plan.rows if r["op"] == "move")
    assert "^STELLAR2" in by_id and by_id["^STELLAR2"]["moved"] == 400, \
        ("keep 500 of 900 Chromatic Metal moves 400 into chest2: %s"
         % by_id.get("^STELLAR2", {}).get("moved"))


def test_two_stacks_of_one_item_in_one_container_are_merged_first(base_plan):
    merges = [r for r in base_plan.rows if r["op"] == "merge"]
    assert len(merges) == 1 and merges[0]["id"] == "^ALBUMENPEARL", \
        "two stacks of one item in one container are merged first"


def test_a_tidy_row_is_planned(base_plan):
    assert any(r["op"] == "tidy" for r in base_plan.rows), "a tidy row is planned"


# ======================================================== overflow chains


def test_a_bucket_on_two_containers_plans_against_the_chain_in_order(
        synthetic_path, synthetic_config):
    # chest1 is 4x2 with a Sodium stack already in it. With create_stacks off
    # the destination genuinely runs out of room, so the second container in
    # the chain has to be part of the plan.
    ovf = synthetic_config
    ovf["bucket_rules"] = [
        {"bucket": "raw_resources", "store": "chest1"},
        {"bucket": "raw_resources", "store": "chest2"},
    ]
    ovf["item_rules"] = [{"item": "CATALYST1", "keep": 0, "priority": 10}]
    ovf["options"]["tidy"] = []
    ovf["options"]["create_stacks"] = False   # merge-only: chest1 holds one
    #                                           stack, chest2 holds none
    plan, _ = _plan(synthetic_path, ovf)
    na = _moves(plan, "^CATALYST1")
    assert na and all(r.get("chain") == ["chest1", "chest2"] for r in na), \
        ("a bucket on two containers plans against the chain, in order: %s"
         % [r.get("chain") for r in na])


def test_a_dead_container_in_the_chain_is_stepped_over_not_fatal(
        synthetic_path, synthetic_config):
    ovf2 = synthetic_config
    ovf2["bucket_rules"] = [
        {"bucket": "raw_resources", "store": "chestNOPE"},
        {"bucket": "raw_resources", "store": "chest1"},
    ]
    ovf2["item_rules"] = [{"item": "CATALYST1", "keep": 250, "priority": 10}]
    ovf2["options"]["tidy"] = []
    plan, _ = _plan(synthetic_path, ovf2)
    landed = _moves(plan, "^CATALYST1")
    assert len(landed) == 1 and landed[0]["dst"] == "chest1", \
        ("a dead container in the chain is stepped over, not fatal: %s"
         % [(r["dst"], r["moved"]) for r in landed])
    assert sum(r["moved"] for r in landed) == 150, \
        ("stepping over it still moves the full surplus of 150: %s"
         % sum(r["moved"] for r in landed))


# ==================================================== packed technology
#
# The premise: an unnameable id carrying a #hash is bucketed by its shape, not
# by a lookup. `test_itemdb.py` owns the taxonomy; these three are here because
# the plan assertion below is meaningless without them.


def test_packed_technology_is_bucketed_by_shape_not_by_lookup():
    idb = db()
    packed = "^ý62#03535"
    assert idb.bucket_of(packed) == "packed_tech", \
        ("an unnameable id with a #hash is packed technology: %s"
         % idb.bucket_of(packed))
    assert idb.bucket_of("^UP_SGUN4#86554") == "tech_upgrades_elite", \
        ("a procedural id the table DOES know keeps its own bucket: %s"
         % idb.bucket_of("^UP_SGUN4#86554"))
    assert idb.bucket_of("^NO_SUCH_THING") == "unsorted", \
        ("an unknown id with no hash is still left alone: %s"
         % idb.bucket_of("^NO_SUCH_THING"))


def test_a_raw_byte_id_is_moved_once_a_bucket_claims_it(synthetic_path,
                                                        synthetic_config):
    pk = synthetic_config
    pk["sources"] = ["suit"]
    pk["bucket_rules"] = [{"bucket": "packed_tech", "store": "chest3"}]
    pk["item_rules"] = []
    pk["options"]["tidy"] = []
    plan, _ = _plan(synthetic_path, pk)
    got = [r for r in plan.rows if r["op"] == "move" and "x80" in r["id"]]
    assert len(got) == 1 and got[0]["dst"] == "chest3", \
        ("a raw-byte id is moved once a bucket claims it: %s"
         % [(r["id"], r["dst"]) for r in got])


# ======================================================= DamageFactor 1.0


@pytest.fixture
def damage_plan(synthetic_path, synthetic_config):
    dmg = synthetic_config
    dmg["sources"] = ["suit"]
    dmg["bucket_rules"] = [{"bucket": "salvage_junk", "store": "chest3"}]
    dmg["item_rules"] = []
    dmg["options"]["tidy"] = []
    plan, _ = _plan(synthetic_path, dmg)
    return plan


def test_a_substance_carrying_damage_factor_one_still_sorts(damage_plan):
    gunk = _moves(damage_plan, "^SPACEGUNK1")
    assert len(gunk) == 1 and gunk[0]["moved"] == 60, \
        ("a substance carrying DamageFactor 1.0 still sorts: %s"
         % [(r["moved"], r["dst"]) for r in gunk])


def test_and_is_not_reported_as_damaged(damage_plan):
    assert not any("damaged" in (x.get("reason") or "")
                   for x in damage_plan.skips if x.get("id") == "^SPACEGUNK1"), \
        "a substance carrying DamageFactor 1.0 is not reported as damaged"


def test_while_technology_is_still_refused_on_its_own_grounds(damage_plan):
    tech = [x for x in damage_plan.skips if "UP_HYP1" in (x.get("id") or "")]
    assert tech and "technology is never sorted" in tech[0]["reason"], \
        ("technology is still refused, on its own grounds: %s" % tech[:1])


# ================================================================== stock
#
# The synthetic suit holds 400 Sodium and chest1 holds 100. Stocking chest1 to
# 250 must pull 150 out of the suit and send the rest onward.


def _stock_config(synthetic_config):
    stk = synthetic_config
    stk["sources"] = ["suit", "chest1"]
    stk["bucket_rules"] = [{"bucket": "raw_resources", "store": "chest2"}]
    stk["item_rules"] = [{"item": "CATALYST1", "stock": 250,
                          "stock_in": "chest1", "priority": 10}]
    stk["options"]["tidy"] = []
    return stk


def _sodium(plan):
    return [(r["key"], r["dst"], r["moved"]) for r in _moves(plan, "^CATALYST1")]


def test_stock_pulls_the_shortfall_out_of_the_other_sources(synthetic_path,
                                                            synthetic_config):
    plan, _ = _plan(synthetic_path, _stock_config(synthetic_config))
    moves = _sodium(plan)
    assert ("suit", "chest1", 150) in moves, \
        "stock pulls the shortfall out of the other sources: %s" % moves


def test_and_the_surplus_carries_on_to_the_items_category(synthetic_path,
                                                          synthetic_config):
    plan, _ = _plan(synthetic_path, _stock_config(synthetic_config))
    moves = _sodium(plan)
    assert ("suit", "chest2", 250) in moves, \
        "the surplus carries on to the item's category: %s" % moves


def test_the_stocked_container_is_never_drained(synthetic_path, synthetic_config):
    plan, _ = _plan(synthetic_path, _stock_config(synthetic_config))
    moves = _sodium(plan)
    assert not any(m[0] == "chest1" for m in moves), \
        "the stocked container is never drained: %s" % moves


def test_stock_with_an_explicit_overflow_container(synthetic_path,
                                                   synthetic_config):
    stk2 = _stock_config(synthetic_config)
    stk2["bucket_rules"] = []
    stk2["item_rules"] = [{"item": "CATALYST1", "stock": 250,
                           "stock_in": "chest1", "store": "chest3",
                           "priority": 10}]
    plan, _ = _plan(synthetic_path, stk2)
    moves = _sodium(plan)
    assert ("suit", "chest1", 150) in moves and ("suit", "chest3", 250) in moves, \
        "stock with an explicit overflow container: %s" % moves


def test_a_container_already_at_its_stock_level_takes_nothing_more(
        synthetic_path, synthetic_config):
    stk3 = _stock_config(synthetic_config)
    stk3["item_rules"] = [{"item": "CATALYST1", "stock": 100,
                           "stock_in": "chest1", "priority": 10}]
    plan, _ = _plan(synthetic_path, stk3)
    moves = _sodium(plan)
    assert not any(m[1] == "chest1" for m in moves), \
        "a container already at its stock level takes nothing more: %s" % moves


# ====================================================== a scoped keep floor
#
# With the floor unscoped both the suit and chest1 are guarded; scoped to the
# suit, chest1 drains in full.


def _keep_config(synthetic_config):
    sc = synthetic_config
    sc["sources"] = ["suit", "chest1"]
    sc["bucket_rules"] = [{"bucket": "raw_resources", "store": "chest2"}]
    sc["item_rules"] = [{"item": "CATALYST1", "keep": 50, "priority": 10}]
    sc["options"]["tidy"] = []
    return sc


def test_an_unscoped_floor_is_held_back_in_every_source_separately(
        synthetic_path, synthetic_config):
    plan, _ = _plan(synthetic_path, _keep_config(synthetic_config))
    moved = dict((r["key"], r["moved"]) for r in _moves(plan, "^CATALYST1"))
    assert moved.get("suit") == 350 and moved.get("chest1") == 50, \
        ("an unscoped floor is held back in every source separately: %s" % moved)


@pytest.fixture
def scoped_keep_plan(synthetic_path, synthetic_config):
    sc2 = _keep_config(synthetic_config)
    sc2["item_rules"] = [{"item": "CATALYST1", "keep": 50, "keep_in": "suit",
                          "priority": 10}]
    plan, _ = _plan(synthetic_path, sc2)
    return plan


def test_a_floor_scoped_to_one_container_lets_the_others_drain_in_full(
        scoped_keep_plan):
    moved = dict((r["key"], r["moved"]) for r in _moves(scoped_keep_plan,
                                                        "^CATALYST1"))
    assert moved.get("suit") == 350 and moved.get("chest1") == 100, \
        ("a floor scoped to one container lets the others drain in full: %s"
         % moved)


def test_and_the_plan_says_why_the_floor_did_not_apply_there(scoped_keep_plan):
    why = " ".join(r.get("limits", [""])[0] for r in scoped_keep_plan.rows
                   if r["op"] == "move" and r["key"] == "chest1")
    assert "scoped to suit" in why, \
        "the plan says why the floor did not apply there: %s" % why


# ======================================================== extractor drain


@pytest.fixture
def drain_config(synthetic_config):
    drain = synthetic_config
    drain["sources"] = ["extractor1"]
    drain["bucket_rules"] = [{"bucket": "refined_crafted", "store": "chest2"}]
    drain["item_rules"] = []
    drain["options"]["tidy"] = []
    return drain


def test_a_core_drains_its_chromatic_metal_into_the_routed_chest(
        synthetic_path, drain_config):
    plan, _ = _plan(synthetic_path, drain_config)
    got = _moves(plan, "^STELLAR2")
    assert len(got) == 1 and got[0]["moved"] == 40 and got[0]["dst"] == "chest2", \
        ("a core drains its Chromatic Metal into the routed chest: %s"
         % [(r["moved"], r["dst"]) for r in got])


def test_the_extractor_module_itself_is_never_moved(synthetic_path, drain_config):
    plan, _ = _plan(synthetic_path, drain_config)
    assert not any(r["id"].startswith("^MAINT") for r in plan.rows), \
        "the extractor module itself is never moved"


def test_the_extractor_module_is_not_even_a_skip_row(synthetic_path, drain_config):
    """It was never moved -- "technology is never sorted" caught it -- but it
    said so once per core. On the operator's save, adding all fourteen cores as
    sources gave 12 moves and 119 skips, 14 of them the extractors themselves.
    The skip list is what a player reads to check nothing was missed."""
    plan, _ = _plan(synthetic_path, drain_config)
    assert not any("MAINT" in r["stem"] for r in plan.skips), \
        "no skip row about the machinery: %s" % [r["stem"] for r in plan.skips]
    assert not any("MAINT" in r["stem"] for r in plan.unknown), plan.unknown


def test_the_module_is_dropped_before_anything_is_said_about_it(
        synthetic_path, drain_config):
    """Silently, and nowhere near the fingerprint: the preimage is taken over
    `plan.rows` and never over `skips`, so this changes no byte an apply
    writes. That claim is the point of this test."""
    plan, _ = _plan(synthetic_path, drain_config)
    assert "MAINT" not in planner.fingerprint_preimage(plan)
    assert plan.fingerprint_full, "and it is still fingerprinted"


def test_the_plan_table_does_not_name_the_machine_either(synthetic_path,
                                                         drain_config):
    """`plan.diff` is the second consumer of the predicate and did not have
    it: the Save card read "substances" while "What each container ends up
    holding" read one stack more, out of 0 valid cells, with `Extractor Unit`
    listed in `before` and in `after` (review 4, finding 7)."""
    plan, _ = _plan(synthetic_path, drain_config)
    rows = [d for d in plan.diff if d["key"] == "extractor1"]
    assert rows, "the core is in the table: %s" % [d["key"] for d in plan.diff]
    row = rows[0]
    named = [r["id"] for r in row["before"] + row["after"]]
    assert not any("MAINT" in i for i in named), named
    assert not any("MAINT" in c["id"] for c in row["changes"]), row["changes"]
    assert row["valid"] == row["before_used"] and row["valid"] > 0, \
        ("a core's cells are its slots, not its empty ValidSlotIndices: "
         "valid %r, before_used %r" % (row["valid"], row["before_used"]))


def test_the_written_bytes_are_the_same_whatever_is_said_about_the_machine(
        synthetic_path, synthetic_config):
    """The pinning test for findings 2 and 7: hiding the marker from a display
    is only safe if no configuration can make it move.

    Four, deliberately hostile: the core as a source; the core as a
    *destination* and in `tidy` at once; an `item_rules` entry naming
    `MAINT_HOOVER` by id; and an `item_buckets` override moving it into a
    bucket that is routed. In every one of them the slot is still at cell
    (0, 0) with amount 1 after `commit()`, the fingerprint preimage never
    names it, and no row of the plan or of the diff does either.
    """
    def cfg_for(which):
        cfg = json.loads(json.dumps(synthetic_config))
        cfg["item_rules"] = []
        cfg["options"]["tidy"] = []
        cfg["bucket_rules"] = [{"bucket": "refined_crafted", "store": "chest2"}]
        if which == "source":
            cfg["sources"] = ["extractor1"]
        elif which == "destination":
            cfg["sources"] = ["suit"]
            cfg["bucket_rules"] = [{"bucket": "raw_resources",
                                    "store": "extractor1"},
                                   {"bucket": "*", "store": "extractor1"}]
            cfg["options"]["tidy"] = ["extractor1"]
        elif which == "item_rule":
            cfg["sources"] = ["extractor1"]
            cfg["item_rules"] = [{"item": "MAINT_HOOVER", "store": "chest1"}]
        elif which == "override":
            cfg["sources"] = ["extractor1"]
            cfg["item_buckets"] = {"MAINT_HOOVER": "raw_resources"}
            cfg["bucket_rules"] = [{"bucket": "raw_resources",
                                    "store": "chest1"}]
        return cfg

    for which in ("source", "destination", "item_rule", "override"):
        cfg = cfg_for(which)
        save = SaveFile(synthetic_path)
        db().apply_overrides(cfg)
        try:
            plan, commit = planner.build_plan(save, cfg)
            assert "MAINT" not in planner.fingerprint_preimage(plan), which
            said = [r.get("stem") or "" for r in plan.rows + plan.skips]
            assert not any("MAINT" in s for s in said), (which, said)
            for d in plan.diff:
                ids = [r["id"] for r in d["before"] + d["after"]]
                assert not any("MAINT" in i for i in ids), (which, d["key"], ids)
            commit()
        finally:
            db().apply_overrides(synthetic_config)
        core = save.extractor_containers()[0]
        marker = [s for s in core.slots() if "MAINT" in s.id]
        assert len(marker) == 1, (which, [s.id for s in core.slots()])
        assert marker[0].cell == (0, 0) and marker[0].amount == 1, \
            (which, marker[0].cell, marker[0].amount)


def test_the_drained_slot_stays_in_place_at_zero(synthetic_path, drain_config):
    save = SaveFile(synthetic_path)
    _plan_obj, commit = planner.build_plan(save, drain_config)
    commit()
    after = save.extractor_containers()[0]
    amt = dict((sl.id, sl.amount) for sl in after.slots())
    assert "^STELLAR2" in amt and amt["^STELLAR2"] == 0, \
        ("the drained slot stays in place at zero, so the core can refill it: %s"
         % amt)
    assert "^MAINT_HOOVER" in amt, "the extractor module survives the drain"


def test_a_stale_buffer_is_never_drained(fixture_variant, synthetic_config):
    """The leftover from a rebuilt room holds 99 units nobody may have.

    The game stopped reading that buffer when the room went, so its contents
    are not in the world: moving them into a chest would *create* 99 units of
    Chromatic Metal rather than move them. The three live cores drain, the
    leftover does not, and its 99 is still sitting there afterwards.
    """
    path = fixture_variant("extractor-stale")["save"]
    cfg = synthetic_config
    cfg["sources"] = ["extractor1", "extractor2", "extractor3", "extractor4"]
    cfg["bucket_rules"] = [{"bucket": "refined_crafted", "store": "chest2"}]
    cfg["item_rules"] = []
    cfg["options"]["tidy"] = []
    save = SaveFile(path)
    plan, commit = planner.build_plan(save, cfg)
    moved = sorted(r["moved"] for r in _moves(plan, "^STELLAR2"))
    assert moved == [10, 20, 30], \
        "the three live cores drain and the leftover does not: %s" % moved
    assert any("'extractor4' is not a container in this save" in n["message"]
               for n in plan.notes), \
        "and a rule naming a fourth gets the ordinary note: %s" \
        % [n["message"] for n in plan.notes]
    commit()
    stale = save.extractor_match()["stale"][0]["core"]
    held = dict((savemodel.Slot(save.d, n).id, savemodel.Slot(save.d, n).amount)
                for n in (save.d.get(stale.container, "Slots") or []))
    assert held.get("^STELLAR2") == 99, \
        "the leftover is untouched by the apply: %s" % held


def test_the_owners_own_config_resolves_its_fourteen_extractors(
        real_saves_dir):
    """The bug this was raised for, as one assertion.

    `save10.hg` plus the operator's own configuration: every `extractorN` in
    `sources` used to come back as "not a container in this save", fourteen
    times, because three stale buffers made the core count disagree with the
    room count. There is nothing in those cores on that save right now --
    every substance reads 0 of 350 -- so what this asserts is the bug itself:
    the sources resolve, and the plan has nothing to say about them.
    """
    import json
    import os
    from nms_sorter import config as cfgmod
    path = os.path.join(real_saves_dir, "save10.hg")
    if not os.path.exists(path):
        pytest.skip("save10.hg is not in the corpus")
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "fixtures", "config_v1_real.json"),
              encoding="utf-8") as fh:
        cfg, _notes = cfgmod.migrate(json.load(fh))
    assert "extractor14" in cfg["sources"], "the config names all fourteen"
    save = SaveFile(path)
    plan, _commit = planner.build_plan(save, cfg)
    said = [n["message"] for n in plan.notes]
    assert not [m for m in said if "is not a container" in m], \
        "no source is missing any more: %s" % said
    assert not [m for m in said if "extractor" in m.lower()], \
        "and there is nothing to say about the extractors: %s" % said


def test_sorting_into_an_extractor_core_is_refused(synthetic_path,
                                                   synthetic_config):
    into = synthetic_config
    into["sources"] = ["suit"]
    into["bucket_rules"] = [{"bucket": "refined_crafted", "store": "extractor1"}]
    into["item_rules"] = []
    into["options"]["tidy"] = []
    plan, _ = _plan(synthetic_path, into)
    refusals = [x["reason"] for x in plan.skips if x.get("op") == "refuse"]
    assert any("Stellar Extractor Core" in r for r in refusals), \
        ("sorting INTO an extractor core is refused: %s"
         % [r[:60] for r in refusals])


# ================================================= a per-item bucket override


def test_an_override_changes_where_the_planner_actually_sends_the_item(
        synthetic_path, synthetic_config):
    # The itemdb side of this -- that the custom bucket exists, that the
    # generated taxonomy underneath is not edited, that clearing the config
    # restores it -- is `test_itemdb.py`. What is asserted here is the only
    # part the planner owns: the override moves the destination.
    ov = synthetic_config
    ov["custom_buckets"] = [{"key": "ammo_fuel", "label": "Ammo and Fuel"}]
    ov["item_buckets"] = {"CATALYST1": "ammo_fuel"}
    ov["bucket_rules"] = [{"bucket": "ammo_fuel", "store": "chest3"}]
    ov["item_rules"] = []
    ov["options"]["tidy"] = []
    plan, _ = _plan(synthetic_path, ov)
    moved = _moves(plan, "^CATALYST1")
    assert len(moved) == 1 and moved[0]["dst"] == "chest3", \
        ("the override changes where the planner actually sends the item: %s"
         % [(r["dst"], r["moved"]) for r in moved])


# ============================================================ fingerprint


def test_the_plan_has_a_fingerprint(base_plan):
    assert base_plan.fingerprint and len(base_plan.fingerprint) == 8, \
        "the plan has a fingerprint: %r" % base_plan.fingerprint


def test_planning_the_same_save_twice_fingerprints_identically(
        synthetic_path, synthetic_config):
    first, _ = _plan(synthetic_path, synthetic_config)
    second, _ = _plan(synthetic_path, synthetic_config)
    assert second.fingerprint == first.fingerprint, \
        "planning the same save twice fingerprints identically"


# ===================================================== defect register P1-1
#
# One section per row of GOAL.md 1.7 that lands in the planner. Each test was
# written against the pre-fix code and shown failing first.


# --- D1: a rule that names only `stores` must route, not be pinned ---------


def test_a_rule_with_only_stores_routes_along_the_whole_chain(synthetic_path,
                                                              synthetic_config):
    """D1. The pin test read `store` alone, so a `stores`-only rule looked
    destinationless and was pinned -- which made its own `stores` branch
    unreachable."""
    cfg = synthetic_config
    cfg["sources"] = ["suit"]
    cfg["bucket_rules"] = []
    cfg["item_rules"] = [{"item": "CATALYST1", "stores": ["chest1", "chest2"],
                          "priority": 10}]
    cfg["options"]["tidy"] = []
    plan, _ = _plan(synthetic_path, cfg)
    moves = _moves(plan, "^CATALYST1")
    assert moves, ("a stores-only rule routes: %s"
                   % [s["reason"] for s in plan.skips if s["id"] == "^CATALYST1"])
    assert all(r["chain"] == ["chest1", "chest2"] for r in moves), \
        "the whole chain is planned: %s" % [r["chain"] for r in moves]


def test_a_stores_only_rule_is_not_reported_as_pinned(synthetic_path,
                                                      synthetic_config):
    cfg = synthetic_config
    cfg["sources"] = ["suit"]
    cfg["bucket_rules"] = []
    cfg["item_rules"] = [{"item": "CATALYST1", "stores": ["chest1"]}]
    cfg["options"]["tidy"] = []
    plan, _ = _plan(synthetic_path, cfg)
    pinned = [s for s in plan.skips
              if s.get("id") == "^CATALYST1" and "pinned" in s["reason"]]
    assert not pinned, "a stores-only rule is not pinned: %s" % pinned


def test_a_list_valued_store_reads_as_a_chain(synthetic_path, synthetic_config):
    """Config v2 spells a chain as `store: [a, b]`; the planner must read it."""
    cfg = synthetic_config
    cfg["sources"] = ["suit"]
    cfg["bucket_rules"] = []
    cfg["item_rules"] = [{"item": "CATALYST1", "store": ["chest1", "chest2"],
                          "priority": 10}]
    cfg["options"]["tidy"] = []
    plan, _ = _plan(synthetic_path, cfg)
    moves = _moves(plan, "^CATALYST1")
    assert moves and all(r["chain"] == ["chest1", "chest2"] for r in moves), \
        "a list-valued store is a chain: %s" % [r.get("chain") for r in moves]


# --- D2: the fingerprint covers renames, the config and the save ------------


def _row(op="move", **kw):
    base = {"op": op, "key": "chest1", "dst": "chest2", "id": "^X",
            "cell": [0, 0], "moved": 5, "merges": [], "new_stacks": []}
    base.update(kw)
    return base


def test_two_plans_differing_only_in_a_rename_fingerprint_differently():
    """D2. A rename-only difference used to pass the step-5 gate."""
    a, b = planner.Plan(), planner.Plan()
    a.rows = [_row()]
    b.rows = [_row(), {"op": "rename", "key": "chest2", "before": "",
                       "after": "Refined and Crafted Materials"}]
    fa = planner._fingerprint(a, {}, "save9.hg|1|2")
    fb = planner._fingerprint(b, {}, "save9.hg|1|2")
    assert fa != fb, "a rename row changes the fingerprint: %s vs %s" % (fa, fb)


def test_a_config_change_alone_changes_the_fingerprint():
    """D2 and 3.9. The plan is pinned to the config it was computed from."""
    p = planner.Plan()
    p.rows = [_row()]
    one = planner._fingerprint(p, {"notes": "before"}, "save9.hg|1|2")
    two = planner._fingerprint(p, {"notes": "after"}, "save9.hg|1|2")
    assert one != two, "a config change alone changes the fingerprint"


def test_the_save_signature_is_part_of_the_fingerprint():
    p = planner.Plan()
    p.rows = [_row()]
    assert planner._fingerprint(p, {}, "save9.hg|1|2") \
        != planner._fingerprint(p, {}, "save9.hg|1|3"), \
        "the save signature is part of the fingerprint"


def test_the_full_fingerprint_is_sha256_and_the_short_one_its_prefix(base_plan):
    assert len(base_plan.fingerprint_full) == 64, \
        "the compared fingerprint is a full sha256: %r" % base_plan.fingerprint_full
    assert base_plan.fingerprint == base_plan.fingerprint_full[:8], \
        "the one shown to a person is its first 8 characters"
    d = base_plan.as_dict()
    assert d["fingerprint"] == base_plan.fingerprint \
        and d["fingerprint_full"] == base_plan.fingerprint_full, \
        "as_dict() carries both"


# --- D3: renames are computed before the diff -------------------------------


@pytest.fixture
def rename_only_plan(synthetic_path, synthetic_config):
    """chest3 is routed a bucket this save holds nothing of, so the only thing
    that happens to it is the rename."""
    cfg = synthetic_config
    cfg["sources"] = ["suit"]
    cfg["bucket_rules"] = [{"bucket": "fish", "store": "chest3"}]
    cfg["item_rules"] = []
    cfg["options"]["tidy"] = []
    cfg["options"]["auto_name"] = True
    plan, _ = _plan(synthetic_path, cfg)
    return plan


def test_a_rename_only_container_is_in_the_diff(rename_only_plan):
    """D3. The diff ran before the rename phase, so a container that was only
    renamed was missing from the report the operator reads."""
    assert any(r["op"] == "rename" and r["key"] == "chest3"
               for r in rename_only_plan.rows), "chest3 is renamed"
    assert "chest3" in [d["key"] for d in rename_only_plan.diff], \
        ("a rename-only container is in the diff: %s"
         % [d["key"] for d in rename_only_plan.diff])


def test_and_is_counted_in_containers_touched(rename_only_plan):
    assert rename_only_plan.totals["containers_touched"] == \
        len(rename_only_plan.diff), "containers_touched counts the diff rows"
    assert rename_only_plan.totals["containers_touched"] >= 1, \
        "a rename-only run still touches a container"


# --- D4: the new-stack budget says so when it runs out ----------------------


@pytest.fixture
def budget_plan(fixture_variant, synthetic_config):
    """`low-limits` caps a product stack at 10, so the suit's 11 Albumen Pearl
    need two new cells in chest3 and a budget of one cannot cover them."""
    cfg = synthetic_config
    cfg["sources"] = ["suit"]
    cfg["bucket_rules"] = [{"bucket": "trade_goods", "store": "chest3"}]
    cfg["item_rules"] = []
    cfg["options"]["tidy"] = []
    cfg["options"]["max_new_stacks"] = 1
    plan, _ = _plan(fixture_variant("low-limits")["save"], cfg)
    return plan


def test_the_exhausted_new_stack_budget_is_named_on_the_row(budget_plan):
    """D4. Exhaustion was silent: no note, no limit, only a `partial` flag."""
    rows = _moves(budget_plan, "^ALBUMENPEARL")
    assert rows, "the pearls move as far as the budget allows"
    assert any("new-stack budget of 1 used up this run" in lim
               for r in rows for lim in r["limits"]), \
        "the row says the budget ran out: %s" % [r["limits"] for r in rows]


def test_and_warned_about_exactly_once(budget_plan):
    said = [n["message"] for n in budget_plan.notes
            if "new-stack budget" in n["message"]]
    assert len(said) == 1, "warned about exactly once: %s" % said


# --- D5: max_moves stops the whole move phase, once -------------------------


@pytest.fixture
def capped_plan(synthetic_path, synthetic_config):
    cfg = synthetic_config
    cfg["sources"] = ["suit", "ship0"]
    cfg["item_rules"] = []
    cfg["options"]["tidy"] = []
    cfg["options"]["max_moves"] = 1
    plan, _ = _plan(synthetic_path, cfg)
    return plan


def test_max_moves_stops_at_the_cap(capped_plan):
    assert capped_plan.totals["moves"] == 1, \
        "max_moves = 1 plans one move: %s" % capped_plan.totals["moves"]


def test_max_moves_warns_exactly_once_for_the_whole_run(capped_plan):
    """D5. The `break` left the source loop running, so every remaining source
    re-tripped the cap and repeated the warning."""
    said = [n["message"] for n in capped_plan.notes if "max_moves" in n["message"]]
    assert len(said) == 1, \
        "one warning for the whole run, not one per source: %s" % said


# --- D6: no fabricated "bucket rule 1" --------------------------------------


def _stock_only_rules(synthetic_config):
    cfg = synthetic_config
    cfg["sources"] = ["suit"]
    cfg["bucket_rules"] = []
    cfg["item_rules"] = [{"item": "CATALYST1", "stock": 250,
                          "stock_in": "chest1", "priority": 10}]
    cfg["options"]["tidy"] = []
    return cfg


def test_a_stocked_container_with_no_bucket_rule_says_so(synthetic_path,
                                                         synthetic_config):
    """D6. `(first_rule or 0) + 1` printed "bucket rule 1" when there was no
    bucket rule at all."""
    plan, _ = _plan(synthetic_path, _stock_only_rules(synthetic_config))
    why = " | ".join(w for r in _moves(plan, "^CATALYST1") for w in r["why"])
    assert "stocked in chest1; no category rule for the surplus" in why, \
        "the why names the stock, not an invented rule: %s" % why
    assert "bucket rule" not in why, "no bucket rule is fabricated: %s" % why


def test_and_the_decision_carries_no_bucket_rule_index(synthetic_config):
    rules = planner.Rules(_stock_only_rules(synthetic_config), db())
    dec = rules.decide("^CATALYST1", "suit")
    assert dec["dsts"] == ["chest1"] and dec["bucket_rule"] is None, \
        "bucket_rule stays None: %r" % dec["bucket_rule"]


# --- D7: the reachable set --------------------------------------------------


def test_damaged_technology_is_skipped_as_technology(synthetic_path,
                                                     synthetic_config):
    """D7. The damage guard sat after the Technology skip, so it could only
    ever fire on a substance -- where the field does not mean damage. Damaged
    technology is refused by the line above it, and this is that line."""
    save = SaveFile(synthetic_path)
    cfg = synthetic_config
    cfg["sources"] = ["suit"]
    cfg["bucket_rules"] = [{"bucket": "tech_upgrades_elite", "store": "chest3"}]
    cfg["item_rules"] = []
    cfg["options"]["tidy"] = []
    tech = [s for s in save.container_map()["suit"].slots() if "UP_HYP1" in s.id]
    assert tech, "the fixture carries a technology module"
    save.d.set(tech[0].node, "DamageFactor", 1.0)
    plan, _ = planner.build_plan(save, cfg)
    said = [s["reason"] for s in plan.skips if "UP_HYP1" in (s.get("id") or "")]
    assert said == ["technology is never sorted"], \
        "damaged technology is skipped as technology, once: %s" % said


def test_a_non_ascii_id_a_rule_claims_is_moved(synthetic_path, synthetic_config):
    """D7. The non-ASCII guard required the id to be unsorted, which the skip
    above it had already caught. A claimed raw-byte id moves."""
    cfg = synthetic_config
    cfg["sources"] = ["suit"]
    cfg["bucket_rules"] = [{"bucket": "packed_tech", "store": "chest3"}]
    cfg["item_rules"] = []
    cfg["options"]["tidy"] = []
    plan, _ = _plan(synthetic_path, cfg)
    moved = [r for r in plan.rows if r["op"] == "move" and "x80" in r["id"]]
    assert len(moved) == 1 and moved[0]["dst"] == "chest3", \
        "a non-ASCII id with a rule moves: %s" % [(r["id"], r["dst"]) for r in moved]


def test_a_non_ascii_id_no_rule_claims_is_left_alone(synthetic_path,
                                                     synthetic_config):
    cfg = synthetic_config
    cfg["sources"] = ["suit"]
    cfg["bucket_rules"] = []
    cfg["item_rules"] = []
    cfg["options"]["tidy"] = []
    plan, _ = _plan(synthetic_path, cfg)
    said = [s["reason"] for s in plan.skips if "x80" in (s.get("id") or "")]
    assert said and "Packed Technology" in said[0], \
        "an unclaimed raw-byte id is left where it is, by category: %s" % said


# ============================================== P1-7: the untested fields
#
# GOAL.md 1.5 lists what `selftest.py` never covered. Everything in that list
# that the planner owns is below: the modes (`pin`, `move`, `fill`), the
# guards (`never_move`, `never_buckets`, `min_source`), the options
# (`merge_duplicates`), the tidy layout, both stack tables, the exocraft, and
# the thirteen static containers that were only ever seen empty.


def _bare(cfg, **opts):
    """`cfg` with no bucket rules, no item rules and no tidy: a blank sheet."""
    cfg["sources"] = ["suit"]
    cfg["bucket_rules"] = []
    cfg["item_rules"] = []
    cfg["options"]["tidy"] = []
    cfg["options"].update(opts)
    return cfg


# --- fill: a ceiling in the destination, per leg ---------------------------


@pytest.fixture
def fill_plan(synthetic_path, synthetic_config):
    """400 Sodium, a chain of two empty chests, a ceiling of 50 in each."""
    cfg = _bare(synthetic_config)
    cfg["item_rules"] = [{"item": "CATALYST1", "keep": 0, "fill": 50,
                          "store": ["chest2", "chest3"], "priority": 10}]
    plan, _ = _plan(synthetic_path, cfg)
    return plan


def test_a_fill_ceiling_stops_at_the_ceiling(fill_plan):
    moved = dict((r["dst"], r["moved"]) for r in _moves(fill_plan, "^CATALYST1"))
    assert moved == {"chest2": 50, "chest3": 50}, \
        "the ceiling applies to each leg of the chain: %s" % moved


def test_the_row_says_which_ceiling_it_hit(fill_plan):
    lim = [l for r in _moves(fill_plan, "^CATALYST1") for l in r["limits"]]
    assert any("fill ceiling 50" in l for l in lim), \
        "the row names the ceiling and what the destination held: %s" % lim


def test_what_the_ceiling_held_back_is_reported(fill_plan):
    assert any("left behind" in n["message"] and "chest2 -> chest3" in n["message"]
               for n in fill_plan.notes), \
        "the leftover is a plan-level warning, not silence: %s" % fill_plan.notes


def test_a_destination_already_over_its_ceiling_takes_nothing(synthetic_path,
                                                              synthetic_config):
    """chest1 already holds 100 Sodium, which is over a ceiling of 50."""
    cfg = _bare(synthetic_config)
    cfg["item_rules"] = [{"item": "CATALYST1", "keep": 0, "fill": 50,
                          "store": ["chest1", "chest2"], "priority": 10}]
    plan, _ = _plan(synthetic_path, cfg)
    moved = dict((r["dst"], r["moved"]) for r in _moves(plan, "^CATALYST1"))
    assert "chest1" not in moved and moved.get("chest2") == 50, \
        "the full leg is stepped over, the next one fills: %s" % moved


# --- move: a cap on how much of one item moves this run --------------------


@pytest.fixture
def move_cap_plan(synthetic_path, synthetic_config):
    cfg = _bare(synthetic_config)
    cfg["sources"] = ["suit", "chest1"]
    cfg["item_rules"] = [{"item": "CATALYST1", "keep": 0, "move": 25,
                          "store": "chest2", "priority": 10}]
    plan, _ = _plan(synthetic_path, cfg)
    return plan


def test_a_move_cap_moves_exactly_that_much(move_cap_plan):
    moved = [(r["key"], r["moved"]) for r in _moves(move_cap_plan, "^CATALYST1")]
    assert moved == [("suit", 25)], \
        "25 of the 400 move, and nothing else does: %s" % moved


def test_the_move_cap_is_per_run_not_per_source(move_cap_plan):
    said = [s["reason"] for s in move_cap_plan.skips
            if s.get("key") == "chest1" and s.get("id") == "^CATALYST1"]
    assert said == ["the move cap of 25 for this item is already used up this "
                    "run"], \
        "the second source is told why it moved nothing: %s" % said


def test_the_move_cap_is_named_on_the_row(move_cap_plan):
    lim = [l for r in _moves(move_cap_plan, "^CATALYST1") for l in r["limits"]]
    assert "move cap 25 this run" in lim, "%s" % lim


# --- pin: never move this item ---------------------------------------------


def test_an_explicitly_pinned_item_does_not_move(synthetic_path,
                                                 synthetic_config):
    cfg = _bare(synthetic_config)
    cfg["bucket_rules"] = [{"bucket": "raw_resources", "store": "chest1"}]
    cfg["item_rules"] = [{"item": "CATALYST1", "pin": True, "priority": 10}]
    plan, _ = _plan(synthetic_path, cfg)
    assert not _moves(plan, "^CATALYST1"), "a pinned item does not move"
    said = [s["reason"] for s in plan.skips if s["id"] == "^CATALYST1"]
    assert said and "pinned, nothing moves it" in said[0], \
        "and says so in the vocabulary of the rule that pinned it: %s" % said


def test_pinning_one_item_does_not_pin_the_rest(synthetic_path,
                                                synthetic_config):
    cfg = _bare(synthetic_config)
    cfg["bucket_rules"] = [{"bucket": "raw_resources", "store": "chest1"},
                           {"bucket": "refined_crafted", "store": "chest2"}]
    cfg["item_rules"] = [{"item": "CATALYST1", "pin": True, "priority": 10}]
    plan, _ = _plan(synthetic_path, cfg)
    assert _moves(plan, "^STELLAR2"), "the Chromatic Metal still sorts"


def test_a_rule_with_a_store_and_pin_is_still_a_pin(synthetic_path,
                                                    synthetic_config):
    """`pin` wins over a destination on the same rule: the page can offer both
    and the explicit flag has to be the one that decides."""
    cfg = _bare(synthetic_config)
    cfg["item_rules"] = [{"item": "CATALYST1", "pin": True, "store": "chest2",
                          "priority": 10}]
    plan, _ = _plan(synthetic_path, cfg)
    assert not _moves(plan, "^CATALYST1"), "pin wins"


# --- never_move: pinned wherever it is, whatever the rules say -------------


def test_never_move_beats_every_rule(synthetic_path, synthetic_config):
    cfg = _bare(synthetic_config)
    cfg["never_move"] = ["CATALYST1"]
    cfg["bucket_rules"] = [{"bucket": "raw_resources", "store": "chest1"}]
    cfg["item_rules"] = [{"item": "CATALYST1", "keep": 0, "store": "chest2",
                          "priority": 10}]
    plan, _ = _plan(synthetic_path, cfg)
    assert not _moves(plan, "^CATALYST1"), \
        "never_move is checked before any rule"
    said = [s["reason"] for s in plan.skips if s["id"] == "^CATALYST1"]
    assert said == ["pinned by never_move"], \
        "and it says which list did it: %s" % said


def test_never_move_matches_the_stem_not_the_caret(synthetic_path,
                                                   synthetic_config):
    """The save writes `^CATALYST1`; a person types `CATALYST1`."""
    cfg = _bare(synthetic_config)
    cfg["never_move"] = ["^CATALYST1"]
    cfg["bucket_rules"] = [{"bucket": "raw_resources", "store": "chest1"}]
    plan, _ = _plan(synthetic_path, cfg)
    assert not _moves(plan, "^CATALYST1"), "either spelling matches"


# --- never_buckets: what a catch-all may not sweep up ----------------------


def test_a_catch_all_skips_a_never_bucket(synthetic_path, synthetic_config):
    cfg = _bare(synthetic_config)
    cfg["never_buckets"] = ["raw_resources"]
    cfg["bucket_rules"] = [{"bucket": "*", "store": "chest3"}]
    plan, _ = _plan(synthetic_path, cfg)
    assert not _moves(plan, "^CATALYST1"), \
        "the catch-all does not take a never_bucket"
    said = [s["reason"] for s in plan.skips if s["id"] == "^CATALYST1"]
    assert said and "never_buckets" in said[0], \
        "and the reason names the list: %s" % said


def test_the_catch_all_still_takes_everything_else(synthetic_path,
                                                   synthetic_config):
    cfg = _bare(synthetic_config)
    cfg["never_buckets"] = ["raw_resources"]
    cfg["bucket_rules"] = [{"bucket": "*", "store": "chest3"}]
    plan, _ = _plan(synthetic_path, cfg)
    assert _moves(plan, "^ALBUMENPEARL"), "trade goods are swept up as normal"


def test_an_explicit_rule_beats_never_buckets(synthetic_path, synthetic_config):
    """"The explicit rule wins, which is probably what you meant" is what the
    validator says about this; it has to be what the planner does."""
    cfg = _bare(synthetic_config)
    cfg["never_buckets"] = ["raw_resources"]
    cfg["bucket_rules"] = [{"bucket": "raw_resources", "store": "chest2"},
                           {"bucket": "*", "store": "chest3"}]
    plan, _ = _plan(synthetic_path, cfg)
    moved = [(r["dst"], r["moved"]) for r in _moves(plan, "^CATALYST1")]
    assert moved == [("chest2", 400)], \
        "the bucket named explicitly is routed: %s" % moved


# --- min_source with create_stacks off ------------------------------------


@pytest.fixture
def merge_only_plan(synthetic_path, synthetic_config):
    """Merge-only: nothing is created, so a source stack has to survive."""
    cfg = _bare(synthetic_config, create_stacks=False, min_source=10)
    cfg["bucket_rules"] = [{"bucket": "raw_resources", "store": "chest1"}]
    plan, _ = _plan(synthetic_path, cfg)
    return plan


def test_min_source_leaves_that_many_behind(merge_only_plan):
    moved = [(r["dst"], r["moved"], r["source_after"])
             for r in _moves(merge_only_plan, "^CATALYST1")]
    assert moved == [("chest1", 390, 10)], \
        "390 of 400 move and 10 stay so the stack survives: %s" % moved


def test_the_row_says_min_source_held_it_back(merge_only_plan):
    lim = [l for r in _moves(merge_only_plan, "^CATALYST1") for l in r["limits"]]
    assert any("merge-only: 10 stays behind" in l for l in lim), "%s" % lim


def test_merge_only_never_creates_a_stack(merge_only_plan):
    assert merge_only_plan.totals["new_stacks"] == 0, \
        "no cell is ever filled that was empty"
    assert all(not r["new_stacks"] for r in merge_only_plan.rows
               if r["op"] == "move"), "and no row claims one"


def test_merge_only_refuses_where_there_is_nothing_to_top_up(synthetic_path,
                                                             synthetic_config):
    cfg = _bare(synthetic_config, create_stacks=False, min_source=1)
    cfg["bucket_rules"] = [{"bucket": "refined_crafted", "store": "chest2"}]
    plan, _ = _plan(synthetic_path, cfg)
    said = [s["reason"] for s in plan.skips if s["id"] == "^STELLAR2"]
    assert said and "merge-only never creates one" in said[0], \
        ("chest2 holds no Chromatic Metal to top up, and the refusal says what "
         "to do about it: %s" % said)


def test_a_stack_smaller_than_min_source_is_left_alone(synthetic_path,
                                                       synthetic_config):
    cfg = _bare(synthetic_config, create_stacks=False, min_source=500)
    cfg["bucket_rules"] = [{"bucket": "raw_resources", "store": "chest1"}]
    plan, _ = _plan(synthetic_path, cfg)
    said = [s["reason"] for s in plan.skips if s["id"] == "^CATALYST1"]
    assert said == ["merge-only mode keeps 500 behind and this stack holds 400"], \
        "%s" % said


# --- merge_duplicates off --------------------------------------------------


def test_merge_duplicates_off_plans_no_merge(synthetic_path, synthetic_config):
    cfg = _bare(synthetic_config, merge_duplicates=False)
    plan, _ = _plan(synthetic_path, cfg)
    assert plan.totals["merges"] == 0, "no merge rows at all"
    assert not any(r["op"] == "merge" for r in plan.rows)


def test_merge_duplicates_off_still_moves_both_stacks(synthetic_path,
                                                      synthetic_config):
    """The suit holds two Albumen Pearl stacks. Unmerged, each one moves on its
    own, and the total that arrives is the same."""
    cfg = _bare(synthetic_config, merge_duplicates=False)
    cfg["bucket_rules"] = [{"bucket": "trade_goods", "store": "chest3"}]
    plan, _ = _plan(synthetic_path, cfg)
    rows = _moves(plan, "^ALBUMENPEARL")
    assert len(rows) == 2 and sum(r["moved"] for r in rows) == 11, \
        "two rows, eleven units: %s" % [(r["cell"], r["moved"]) for r in rows]


def test_merge_duplicates_on_is_the_default(synthetic_path, synthetic_config):
    plan, _ = _plan(synthetic_path, _bare(synthetic_config))
    assert plan.totals["merges"] == 1, "the duplicate is merged unless told not to"


# --- tidy: the resulting cell layout --------------------------------------


@pytest.fixture
def tidy_state(synthetic_path, synthetic_config):
    """Fill chest1 from the suit, then tidy it, and read the real container."""
    cfg = _bare(synthetic_config)
    cfg["bucket_rules"] = [{"bucket": b, "store": "chest1"} for b in (
        "raw_resources", "refined_crafted", "trade_goods", "salvage_junk",
        "packed_tech")]
    cfg["options"]["tidy"] = ["chest1"]
    save = SaveFile(synthetic_path)
    before = dict((s.id, s.cell) for s in save.container_map()["chest1"].slots())
    plan, commit = planner.build_plan(save, cfg)
    commit()
    after = save.container_map()["chest1"]
    return {"plan": plan, "before": before,
            "slots": [(s.cell, s.id) for s in after.slots()],
            "valid": after.valid}


def test_tidy_packs_into_reading_order_with_no_gaps(tidy_state):
    from nms_sorter import text as textmod
    cells = sorted(c for c, _id in tidy_state["slots"])
    pinned = set(c for c, i in tidy_state["slots"]
                 if not textmod.is_clean_ascii(textmod.stem(i)))
    movers = [c for c in cells if c not in pinned]
    want = [c for c in sorted(tidy_state["valid"], key=lambda c: (c[1], c[0]))
            if c not in pinned][:len(movers)]
    assert sorted(movers, key=lambda c: (c[1], c[0])) == want, \
        "row-major from the first free cell, no holes: %s" % movers


def test_tidy_orders_by_category_then_name(tidy_state):
    idb = db()
    order = dict((b["key"], i) for i, b in enumerate(idb.buckets))
    rows = []
    for cell, iid in sorted(tidy_state["slots"], key=lambda t: (t[0][1], t[0][0])):
        row = idb.lookup(iid)
        if not row:
            continue                      # the pinned unknowns keep their cell
        rows.append((order.get(idb.bucket_of(iid), 99), row["name"].lower()))
    assert rows == sorted(rows), \
        "reading order follows the taxonomy, then the name: %s" % rows


def test_tidy_leaves_a_pinned_cell_exactly_where_it_was(tidy_state):
    """A raw-byte id cannot be sorted by name, so the tidy phase treats its
    cell as fixed and packs the rest around it."""
    from nms_sorter import text as textmod
    row = [r for r in tidy_state["plan"].rows if r["op"] == "tidy"][0]
    pinned = [(c, i) for c, i in tidy_state["slots"]
              if not textmod.is_clean_ascii(textmod.stem(i))]
    assert pinned, "the fixture's raw-byte id landed in chest1"
    assert row["pinned"] == len(pinned),         "the row counts them: %s vs %s" % (row["pinned"], pinned)
    placed = set(p["id"] for p in row["placements"])
    assert not any(textmod.safe(i) in placed for _c, i in pinned),         "and none of them is in the placements: %s" % sorted(placed)
    cells = [c for c, _i in tidy_state["slots"]]
    assert len(set(cells)) == len(cells), "no two stacks share a cell"


def test_tidy_writes_only_cells(tidy_state):
    row = [r for r in tidy_state["plan"].rows if r["op"] == "tidy"][0]
    assert row["placements"] and all(
        set(p) == {"i", "id", "name", "from", "to", "bucket"}
        for p in row["placements"]), \
        "a placement is a cell move and nothing else: %s" % row["placements"][:1]


def test_tidying_an_ordered_container_writes_nothing(synthetic_path,
                                                     synthetic_config):
    cfg = _bare(synthetic_config)
    cfg["options"]["tidy"] = ["chest2"]        # empty, therefore already tidy
    plan, _ = _plan(synthetic_path, cfg)
    said = [s["reason"] for s in plan.skips if s.get("key") == "chest2"]
    assert said == ["already in order; nothing written"], "%s" % said


# --- the two stack tables -------------------------------------------------


def test_the_stack_tables_disagree_and_both_are_read(idb):
    normal = idb.cap("ALBUMENPEARL", "Product", "Chest", "Normal")
    low = idb.cap("ALBUMENPEARL", "Product", "Chest", "Low")
    assert normal > low > 0, \
        "Low is a lower ceiling, not a missing one: %s vs %s" % (normal, low)


def test_a_new_stack_is_capped_by_the_saves_difficulty(fixture_variant,
                                                       synthetic_config, idb):
    """The difficulty is read from the save, not assumed: the same plan against
    the same items caps a created stack differently."""
    def cap_of(variant, difficulty):
        cfg = _bare(make_fixture.synthetic_config())
        cfg["bucket_rules"] = [{"bucket": "trade_goods", "store": "chest3"}]
        plan, _ = _plan(fixture_variant(variant)["save"], cfg)
        news = [n for r in _moves(plan, "^ALBUMENPEARL") for n in r["new_stacks"]]
        assert news, "a stack is created in the empty chest"
        return news[0]["max"], idb.cap("ALBUMENPEARL", "Product", "Chest",
                                       difficulty)

    base_max, base_want = cap_of("base", "High")
    low_max, low_want = cap_of("low-limits", "Low")
    assert base_max == base_want and low_max == low_want, \
        "each run uses its own save's table: %s %s" % (base_max, low_max)
    assert base_max != low_max, "and the two tables really do differ"


# --- the exocraft ---------------------------------------------------------


def test_an_exocraft_is_a_container_when_the_save_has_one(fixture_variant):
    cm = SaveFile(fixture_variant("with-exocraft")["save"]).container_map()
    assert cm["vehicle0"].sortable and not cm["vehicle0"].is_tech, \
        "the exocraft's own inventory is sortable"
    assert cm["vehicle0_tech"].is_tech and not cm["vehicle0_tech"].sortable, \
        "its technology grid is not"
    assert cm["vehicle0"].section == "Exocraft"


def test_an_exocraft_can_be_drained(fixture_variant, synthetic_config):
    cfg = _bare(synthetic_config)
    cfg["sources"] = ["vehicle0"]
    cfg["bucket_rules"] = [{"bucket": "raw_resources", "store": "chest1"}]
    plan, _ = _plan(fixture_variant("with-exocraft")["save"], cfg)
    moved = [(r["key"], r["dst"], r["moved"]) for r in _moves(plan, "^CATALYST1")]
    assert moved == [("vehicle0", "chest1", 30)], \
        "the exocraft is a source like any other: %s" % moved


def test_nothing_is_ever_sorted_into_an_exocraft_tech_grid(fixture_variant,
                                                           synthetic_config):
    cfg = _bare(synthetic_config)
    cfg["bucket_rules"] = [{"bucket": "raw_resources", "store": "vehicle0_tech"}]
    plan, _ = _plan(fixture_variant("with-exocraft")["save"], cfg)
    said = [s["reason"] for s in plan.skips if s.get("id") == "^CATALYST1"]
    assert said and "technology grid" in said[0], "%s" % said


# --- the thirteen static containers, with something in them ---------------

#: key -> (sortable, is_tech). The three grids and the two single cells are
#: listed here because a container the model calls unsortable is only proved
#: unsortable by a plan refusing to target it while it holds something.
STATIC_EXPECTATIONS = {
    "suit_cargo": (True, False),
    "freighter_cargo": (True, False),
    "corvette": (True, False),
    "cooking": (True, False),
    "rocketlocker": (True, False),
    "fishplatform": (True, False),
    "chestmagic": (True, False),
    "chestmagic2": (True, False),
    "fishbaitbox": (False, False),
    "foodunit": (False, False),
    "multitool": (False, True),
    "suit_tech": (False, True),
    "freighter_tech": (False, True),
}


@pytest.fixture(scope="module")
def all_containers(fixture_variant):
    return SaveFile(fixture_variant("all-containers")["save"]).container_map()


@pytest.mark.parametrize("key", sorted(STATIC_EXPECTATIONS))
def test_every_static_container_reads_the_way_the_model_says(all_containers, key):
    want_sortable, want_tech = STATIC_EXPECTATIONS[key]
    c = all_containers[key]
    assert c.allocated and c.slots(), \
        "%s is allocated and holds something in this fixture" % key
    assert (c.sortable, c.is_tech) == (want_sortable, want_tech), \
        ("%s: sortable/is_tech is %s, expected %s"
         % (key, (c.sortable, c.is_tech), (want_sortable, want_tech)))


@pytest.mark.parametrize("key", sorted(k for k, v in STATIC_EXPECTATIONS.items()
                                       if v[0]))
def test_a_sortable_static_container_can_be_a_destination(fixture_variant,
                                                          synthetic_config, key):
    cfg = _bare(synthetic_config)
    cfg["bucket_rules"] = [{"bucket": "trade_goods", "store": key}]
    plan, _ = _plan(fixture_variant("all-containers")["save"], cfg)
    moved = [r["dst"] for r in _moves(plan, "^ALBUMENPEARL")]
    assert moved == [key], "the pearls land in %s: %s" % (key, moved)


@pytest.mark.parametrize("key", sorted(k for k, v in STATIC_EXPECTATIONS.items()
                                       if not v[0]))
def test_a_plan_never_targets_a_tech_grid_or_a_single_cell(fixture_variant,
                                                           synthetic_config, key):
    cfg = _bare(synthetic_config)
    cfg["bucket_rules"] = [{"bucket": "trade_goods", "store": key}]
    plan, _ = _plan(fixture_variant("all-containers")["save"], cfg)
    assert not _moves(plan, "^ALBUMENPEARL"), \
        "nothing is sorted into %s, whatever a rule says" % key
    said = [s["reason"] for s in plan.skips if s.get("id") == "^ALBUMENPEARL"]
    assert said and "technology grid" in said[0], \
        "%s is refused as a destination: %s" % (key, said)


@pytest.mark.parametrize("key", sorted(k for k, v in STATIC_EXPECTATIONS.items()
                                       if not v[0]))
def test_an_unsortable_static_container_is_not_drained_either(fixture_variant,
                                                              synthetic_config,
                                                              key):
    cfg = _bare(synthetic_config)
    cfg["sources"] = [key]
    cfg["bucket_rules"] = [{"bucket": "raw_resources", "store": "chest1"},
                           {"bucket": "tech_upgrades_elite", "store": "chest1"}]
    plan, _ = _plan(fixture_variant("all-containers")["save"], cfg)
    assert not [r for r in plan.rows if r["op"] == "move"], \
        "%s is not a source either: %s" % (key, plan.rows)
    assert any("technology grid or a single cell" in n["message"]
               for n in plan.notes), "and the plan says why: %s" % plan.notes


def test_a_sortable_static_container_can_be_drained(fixture_variant,
                                                    synthetic_config):
    cfg = _bare(synthetic_config)
    cfg["sources"] = ["corvette", "cooking", "chestmagic"]
    cfg["bucket_rules"] = [{"bucket": "raw_resources", "store": "chest1"}]
    plan, _ = _plan(fixture_variant("all-containers")["save"], cfg)
    keys = sorted(r["key"] for r in _moves(plan, "^CATALYST1"))
    assert keys == ["chestmagic", "cooking", "corvette"], \
        "each of them drains: %s" % keys


def test_the_why_names_the_rule_that_supplied_the_chain(synthetic_path,
                                                        synthetic_config):
    """D6's neighbour: `first_rule` was shared between the exact list and the
    catch-all list, so a `*` rule written *before* the matching rule named the
    catch-all's number while the chain came from the exact rule."""
    cfg = _bare(synthetic_config)
    cfg["bucket_rules"] = [{"bucket": "*", "store": "chest9"},
                           {"bucket": "raw_resources", "store": "chest1"}]
    plan, _ = _plan(synthetic_path, cfg)
    rows = _moves(plan, "^CATALYST1")
    assert [r["dst"] for r in rows] == ["chest1"], \
        "the exact rule supplies the chain: %s" % [r["dst"] for r in rows]
    why = " | ".join(w for r in rows for w in r["why"])
    assert "bucket rule 2: Raw Resources -> chest1" in why, \
        "and the why names rule 2, the one that did it: %s" % why


def test_the_why_names_the_catch_all_when_the_catch_all_is_what_fired(
        synthetic_path, synthetic_config):
    cfg = _bare(synthetic_config)
    cfg["bucket_rules"] = [{"bucket": "fish", "store": "chest2"},
                           {"bucket": "*", "store": "chest3"}]
    plan, _ = _plan(synthetic_path, cfg)
    why = " | ".join(w for r in _moves(plan, "^CATALYST1") for w in r["why"])
    assert "bucket rule 2:" in why, \
        "the catch-all is rule 2 and says so: %s" % why


def test_the_decision_reports_the_same_index(synthetic_config):
    cfg = _bare(synthetic_config)
    cfg["bucket_rules"] = [{"bucket": "*", "store": "chest9"},
                           {"bucket": "raw_resources", "store": "chest1"}]
    dec = planner.Rules(cfg, db()).decide("^CATALYST1", "suit")
    assert dec["bucket_rule"] == 1 and dec["dsts"] == ["chest1"], \
        "bucket_rule is the index of the rule that supplied chain[0]: %r" \
        % dec["bucket_rule"]


def test_a_rule_naming_a_grid_with_no_cells_plans_to_nothing(
        synthetic_path, synthetic_config):
    """No rows, no skips about it, and no plan note: the grid cannot hold
    anything, the key is not a mistake, and a dry run that opens with warnings
    about the exosuit's merged cargo grid is warning about the game.

    The one thing it does say is when a rule has *no other* destination: then
    the item does not move and the refusal names why, which is the difference
    between quiet and silent.
    """
    cfg = dict(synthetic_config)
    cfg["sources"] = ["suit", "suit_cargo"]
    cfg["bucket_rules"] = [{"bucket": "*", "store": ["chest1", "suit_cargo"]}]
    cfg["item_rules"] = []
    cfg["options"] = dict(cfg.get("options") or {}, tidy=["suit_cargo"])
    plan, _c = planner.build_plan(SaveFile(synthetic_path), cfg)
    assert not [n for n in plan.notes if "cargo" in n["message"]], plan.notes
    assert not [r for r in plan.rows if "cargo" in r["key"]], plan.rows
    assert not [r for r in plan.skips if "cargo" in str(r.get("key"))], \
        plan.skips
    assert plan.rows, "and the live half of the chain still moved things"
    # every destination merged away: the item stays, and the refusal says why
    cfg["bucket_rules"] = [{"bucket": "*", "store": "suit_cargo"}]
    plan2, _c2 = planner.build_plan(SaveFile(synthetic_path), cfg)
    assert not [r for r in plan2.rows if r["op"] == "move"], plan2.rows
    bad = [r for r in plan2.skips if r["op"] == "refuse"]
    assert bad and all("has no cells in this save" in r["reason"] for r in bad), bad


def test_an_unroutable_category_is_refused_at_plan_time(
        synthetic_path, synthetic_config):
    """A hand-edited config cannot get a `system_meta` item moved, and the
    plan refuses it out loud rather than quietly leaving the row out.

    The file this guards is the one the validator already refuses twice over:
    an override putting an ordinary item into the category, and a `*`
    catch-all to carry it. `build_plan` applies the config's own overrides, so
    this is the real path and not a monkeypatch.
    """
    cfg = dict(synthetic_config)
    cfg["sources"] = ["suit"]
    cfg["item_rules"] = []
    cfg["bucket_rules"] = [{"bucket": "*", "store": "chest1"}]
    cfg["never_buckets"] = []
    cfg["item_buckets"] = {"^CATALYST1": "system_meta"}
    plan, _c = planner.build_plan(SaveFile(synthetic_path), cfg)
    assert not [r for r in plan.rows if r.get("bucket") == "system_meta"], \
        plan.rows
    bad = [r for r in plan.skips if r.get("bucket") == "system_meta"]
    assert bad, plan.skips
    assert all(r["op"] == "refuse" for r in bad), bad
    assert "never occupies a container slot" in bad[0]["reason"], bad[0]
    # and the rest of the plan is unaffected
    assert plan.rows, plan.notes
