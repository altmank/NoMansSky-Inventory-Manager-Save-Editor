"""`nms_sorter.naming`: container auto-naming, and the 40-character cap.

Converted from `selftest_legacy.py`, section "auto-naming". The cap is the
game's: a longer name is silently truncated in the game's own UI, so a proposal
that exceeds it is a bug, not a cosmetic issue.
"""
import pytest

from nms_sorter import naming, planner
from nms_sorter.savemodel import SaveFile


@pytest.fixture
def naming_config(synthetic_config):
    nm = synthetic_config
    nm["options"]["auto_name"] = True
    nm["options"]["tidy"] = []
    return nm


@pytest.fixture
def name_rows(synthetic_path, naming_config):
    return naming.plan_names(SaveFile(synthetic_path), naming_config)


def test_no_proposed_name_exceeds_the_games_40_character_limit(name_rows):
    assert all(r["length"] <= 40 for r in name_rows), \
        ("no proposed name exceeds the game's 40-character limit: %s"
         % [(r["key"], r["length"]) for r in name_rows])


def test_a_container_is_named_after_the_bucket_routed_to_it(name_rows):
    by = dict((r["key"], r) for r in name_rows)
    assert "chest2" in by and by["chest2"]["after"] == "Refined and Crafted Materials", \
        ("a container is named after the bucket routed to it: %s"
         % by.get("chest2", {}).get("after"))


def test_a_name_the_operator_typed_by_hand_is_left_alone(name_rows):
    by = dict((r["key"], r) for r in name_rows)
    assert "chest1" not in by, \
        "a name the operator typed by hand is left alone: %s" % by.get("chest1")


def test_unless_overwrite_is_asked_for_explicitly(synthetic_path, naming_config):
    over = dict(naming_config)
    over["options"] = dict(naming_config["options"], auto_name_overwrite=True)
    rows = naming.plan_names(SaveFile(synthetic_path), over, only_default=False)
    assert any(r["key"] == "chest1" for r in rows), \
        "a hand-typed name is renamed only when overwrite is asked for explicitly"


def test_three_buckets_on_one_container_collapse_to_a_counted_name():
    long_name = naming.proposed_name("chest9", {"bucket_rules": [
        {"bucket": "base_structures", "store": "chest9"},
        {"bucket": "base_decor", "store": "chest9"},
        {"bucket": "curiosities_artifacts", "store": "chest9"}]})
    assert len(long_name) <= 40 and "+2 more" in long_name, \
        "three buckets on one container collapse to a counted name: %s" % long_name


def test_an_overflow_chain_names_its_position():
    chained = naming.proposed_name("chest2", {"bucket_rules": [
        {"bucket": "raw_resources", "store": "chest1"},
        {"bucket": "raw_resources", "store": "chest2"}]}, 2, 2)
    assert chained.endswith("2/2") and len(chained) <= 40, \
        ("an overflow chain names its position, so the pair is tellable apart: %s"
         % chained)


# --------------------------------------------------- renames inside the plan


@pytest.fixture
def committed_names(synthetic_path, naming_config, name_rows):
    save = SaveFile(synthetic_path)
    plan, commit = planner.build_plan(save, naming_config)
    commit()
    return plan, dict((c.key, c.name) for c in save.containers())


def test_every_proposed_rename_becomes_a_plan_row(committed_names, name_rows):
    plan, _named = committed_names
    assert plan.totals["renames"] == len(name_rows), \
        ("every proposed rename becomes a plan row: %s vs %s"
         % (plan.totals["renames"], len(name_rows)))


def test_the_rename_is_committed_onto_the_container_node(committed_names):
    _plan, named = committed_names
    assert named.get("chest2") == "Refined and Crafted Materials", \
        ("the rename is committed onto the container node: %r"
         % named.get("chest2"))


def test_and_the_hand_typed_name_is_still_there_afterwards(committed_names):
    _plan, named = committed_names
    assert named.get("chest1") == "Minerals", \
        "the hand-typed name is still there afterwards: %r" % named.get("chest1")


# ------------------------------------------------- D22: chains spelled `stores`


def test_a_stores_chain_names_both_of_its_containers(synthetic_path,
                                                     synthetic_config):
    """D22. The chain position was derived from `store` alone, so a rule that
    spelled its chain `stores` (or, in v2, `store: [a, b]`) left both chests
    out of every chain and named them identically."""
    cfg = synthetic_config
    cfg["options"]["auto_name"] = True
    cfg["options"]["tidy"] = []
    cfg["bucket_rules"] = [{"bucket": "raw_resources",
                            "stores": ["chest2", "chest3"]}]
    cfg["item_rules"] = []
    rows = dict((r["key"], r["after"])
                for r in naming.plan_names(SaveFile(synthetic_path), cfg))
    assert rows.get("chest2", "").endswith("1/2") \
        and rows.get("chest3", "").endswith("2/2"), \
        "a stores chain names both of its containers, in order: %s" % rows


def test_a_list_valued_store_names_its_chain_too(synthetic_path,
                                                 synthetic_config):
    cfg = synthetic_config
    cfg["options"]["auto_name"] = True
    cfg["options"]["tidy"] = []
    cfg["bucket_rules"] = [{"bucket": "raw_resources",
                            "store": ["chest2", "chest3"]}]
    cfg["item_rules"] = []
    rows = dict((r["key"], r["after"])
                for r in naming.plan_names(SaveFile(synthetic_path), cfg))
    assert rows.get("chest2", "").endswith("1/2") \
        and rows.get("chest3", "").endswith("2/2"), \
        "a list-valued store names its chain too: %s" % rows


def test_an_item_rule_with_a_stores_chain_still_names_the_container(
        synthetic_path, synthetic_config):
    cfg = synthetic_config
    cfg["options"]["auto_name"] = True
    cfg["options"]["tidy"] = []
    cfg["bucket_rules"] = []
    cfg["item_rules"] = [{"item": "CATALYST1", "stores": ["chest2"]}]
    rows = dict((r["key"], r["after"])
                for r in naming.plan_names(SaveFile(synthetic_path), cfg))
    assert rows.get("chest2") == "Sodium", \
        "an item rule's stores chain still names the container: %s" % rows
