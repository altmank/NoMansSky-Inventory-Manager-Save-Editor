"""`nms_sorter.config`: validation, enrichment, and stripping the UI fields.

Converted from `selftest_legacy.py`, section "configuration", plus the
validation checks that were interleaved with the stock, keep_in and override
planner cases. Every one of them is a pure `validate()` call, so they belong
together rather than next to the plan they were written beside.

`validate()` returns a list of `{"level","where","message"}`. The tests assert
on the message text because that text is what the operator reads; a refusal
whose sentence changes is a change the suite should notice.
"""
import datetime
import json
import os

import pytest

from nms_sorter import config as cfgmod
from nms_sorter import planner
from nms_sorter.itemdb import db
from nms_sorter.savemodel import SaveFile


def _messages(issues):
    return " ".join(i["message"] for i in issues)


def _errors(issues):
    return [i for i in issues if i["level"] == "error"]


def _validate(cfg, container_keys):
    ck, sk, dk = container_keys
    return cfgmod.validate(cfg, ck, sk, dk)


def test_a_rule_naming_a_grid_with_no_cells_is_valid_and_silent(container_keys):
    """`suit_cargo` and `ship0_cargo` are in every configuration written
    before Waypoint (4.0) merged those grids into the main one, and in both
    `docs/GUIDE.md` examples. They have no cells, so a rule naming one does
    nothing -- and nothing is what it should say about it. The keys never
    change, so the rule keeps working if a save ever has the grid again.
    """
    ck, sk, dk = container_keys
    assert "suit_cargo" in dk and "ship0_cargo" in dk, dk
    cfg = cfgmod.default_config()
    cfg["sources"] = ["suit", "suit_cargo", "ship0", "ship0_cargo"]
    cfg["bucket_rules"] = [{"bucket": "raw_resources",
                            "store": ["chest1", "suit_cargo"]},
                           {"bucket": "refined_crafted", "store": "ship0_cargo"}]
    cfg["item_rules"] = [{"item": "^CATALYST1", "store": "suit_cargo"}]
    cfg["options"]["tidy"] = ["suit_cargo"]
    issues = cfgmod.validate(cfg, ck, sk, dk)
    said = [i for i in issues
            if "cargo" in i["message"] or "_cargo" in str(i["where"])]
    assert said == [], said
    assert not [i for i in issues if i["level"] == "error"], issues
    # and a technology grid is still answered, which is the difference
    cfg["bucket_rules"] = [{"bucket": "refined_crafted", "store": "suit_tech"}]
    msgs = _messages(cfgmod.validate(cfg, ck, sk, dk))
    assert "not sortable right now" in msgs, msgs


def test_a_category_that_never_sits_in_a_slot_cannot_be_routed(container_keys):
    """The owner: "Question about 'System and Non-inventory', if its stuff
    that doesn't occupy a slot, then why are we able to assign it to storage?"

    It cannot be any more. None of the 134 `system_meta` ids sits in a slot on
    any of the seven readable corpus saves -- they are currencies, reputation
    deltas, settlement stat tokens and repair sockets -- so a destination for
    it is a rule that can never fire. Three shapes name a category, and all
    three are refused.
    """
    ck, sk, dk = container_keys
    cfg = cfgmod.default_config()
    cfg["bucket_rules"] = [{"bucket": "system_meta", "store": "chest1"}]
    msgs = _messages(cfgmod.validate(cfg, ck, sk, dk))
    assert "never occupies a container slot" in msgs, msgs
    assert _errors(cfgmod.validate(cfg, ck, sk, dk)), "an error, not a warning"

    cfg = cfgmod.default_config()
    cfg["item_buckets"] = {"^CATALYST1": "system_meta"}
    msgs = _messages(cfgmod.validate(cfg, ck, sk, dk))
    assert "never occupies a container slot" in msgs, msgs

    cfg = cfgmod.default_config()
    cfg["item_rules"] = [{"item": "^CATALYST1", "store": "chest1"}]
    cfg["bucket_rules"] = [{"bucket": "*", "store": "chest1"},
                           {"bucket": "system_meta", "store": ["chest2"]}]
    assert "never occupies a container slot" in \
        _messages(cfgmod.validate(cfg, ck, sk, dk))

    # and `unsorted` is untouched: it is a real item in a real cell that this
    # build's table does not know, not a non-inventory id
    assert "unsorted" in cfgmod.DEFAULT_NEVER_BUCKETS
    assert "system_meta" not in cfgmod.DEFAULT_NEVER_BUCKETS


def test_the_migration_drops_what_names_an_unroutable_category():
    """It runs at every version, because a config already at the current one
    would otherwise keep a rule its own validator refuses -- and the owner's
    file is one of those: `system_meta` was in the shipped `never_buckets`
    default. Existing configs load, they do not refuse.
    """
    cfg = {"config_version": cfgmod.CONFIG_VERSION,
           "never_buckets": ["unsorted", "system_meta"],
           "bucket_rules": [{"bucket": "system_meta", "store": "chest1"},
                            {"bucket": "fish", "store": "chest2"}],
           "item_buckets": {"^CATALYST1": "system_meta"}}
    out, notes = cfgmod.migrate(cfg)
    assert out["never_buckets"] == ["unsorted"]
    assert [r["bucket"] for r in out["bucket_rules"]] == ["fish"]
    assert out["item_buckets"] == {}
    said = " ".join(notes)
    assert "never-route list" in said and "rule(s) routing" in said, notes
    assert "per-item override(s) pointing" in said, notes
    # idempotent, and silent the second time
    again, more = cfgmod.migrate(out)
    assert again == out and more == [], more
    # and the file it produces validates
    assert _errors(cfgmod.validate(out, None, None)) == []


# ----------------------------------------------------------- the shipped one


def test_the_shipped_default_config_has_no_errors(container_keys, default_config):
    issues = _validate(default_config, container_keys)
    assert not _errors(issues), \
        "the shipped default config has no errors: %s" % _messages(_errors(issues))


# ------------------------------------------------------------- item rules


def test_an_unknown_item_id_is_a_configuration_error(container_keys):
    bad = _validate({"config_version": 2, "item_rules": [
        {"item": "TECHMOD", "keep": 1}, {"item": "CATALYST1", "fill": 5}]},
        container_keys)
    assert "TECHMOD" in _messages(bad), "an unknown item id is a configuration error"


def test_a_fill_with_no_store_is_a_configuration_error(container_keys):
    bad = _validate({"config_version": 2, "item_rules": [
        {"item": "TECHMOD", "keep": 1}, {"item": "CATALYST1", "fill": 5}]},
        container_keys)
    assert "fill" in _messages(bad), "a fill with no store is a configuration error"


def test_two_rules_on_one_item_with_the_same_destination_is_an_error(container_keys):
    dup = _validate({"config_version": 2, "item_rules": [
        {"item": "CATALYST1", "keep": 1}, {"item": "CATALYST1", "keep": 2}]},
        container_keys)
    assert any("can never fire" in i["message"] for i in dup), \
        "two rules on the same item with the same destination is an error"


def test_a_stock_rule_raises_nothing_because_it_needs_no_store(container_keys):
    quiet = _validate({"config_version": 2, "sources": ["suit"], "item_rules": [
        {"item": "CATALYST1", "stock": 350, "stock_in": "suit"}]}, container_keys)
    said = [i["message"][:50] for i in quiet if "item_rules" in i["where"]]
    assert not said, \
        "a stock rule raises nothing: it needs no store of its own: %s" % said


def test_a_rule_with_nothing_set_at_all_is_called_out(container_keys):
    idle = _validate({"config_version": 2, "sources": ["suit"],
                      "item_rules": [{"item": "CATALYST1"}]}, container_keys)
    assert any("does nothing" in i["message"] for i in idle), \
        "a rule with nothing set at all is still called out"


def test_keep_and_stock_together_is_a_configuration_error(container_keys):
    both = _validate({"config_version": 2, "sources": ["suit"], "item_rules": [
        {"item": "CATALYST1", "stock": 10, "stock_in": "suit", "keep": 5}]},
        container_keys)
    assert any("both keep and stock" in i["message"] for i in both), \
        "keep and stock together is a configuration error"


def test_keep_in_with_no_keep_is_a_configuration_error(container_keys):
    bad = _validate({"config_version": 2, "sources": ["suit"], "item_rules": [
        {"item": "CATALYST1", "keep_in": "suit"}]}, container_keys)
    assert any("no keep to apply" in i["message"] for i in bad), \
        "keep_in with no keep is a configuration error"


# ------------------------------------------------------------ bucket rules


def test_the_same_bucket_on_two_containers_is_an_overflow_chain(container_keys):
    chain = _validate({"config_version": 2, "bucket_rules": [
        {"bucket": "raw_resources", "store": "chest1"},
        {"bucket": "raw_resources", "store": "chest2"}]}, container_keys)
    assert not _errors(chain), \
        ("the same bucket on two containers is an overflow chain, not an error: %s"
         % _messages(_errors(chain)))


def test_the_same_bucket_twice_on_the_same_container_is_an_error(container_keys):
    same = _validate({"config_version": 2, "bucket_rules": [
        {"bucket": "raw_resources", "store": "chest1"},
        {"bucket": "raw_resources", "store": "chest1"}]}, container_keys)
    assert any("can never fire" in i["message"] for i in same), \
        "the same bucket twice on the SAME container is an error"


# ------------------------------------------------- custom buckets, overrides


def test_an_override_pointing_at_a_bucket_that_does_not_exist_is_an_error(
        container_keys):
    bad = _validate({"config_version": 2,
                     "item_buckets": {"CATALYST1": "no_such_bucket"}}, container_keys)
    assert any("is not a bucket" in i["message"] for i in bad), \
        "an override pointing at a bucket that does not exist is an error"


def test_a_custom_bucket_key_with_spaces_or_capitals_is_an_error(container_keys):
    bad = _validate({"config_version": 2,
                     "custom_buckets": [{"key": "Ammo Fuel", "label": "x"}]},
                    container_keys)
    assert any("lower-case" in i["message"] for i in bad), \
        "a custom bucket key with spaces or capitals is an error"


def test_a_custom_bucket_cannot_shadow_a_generated_one(container_keys):
    clash = _validate({"config_version": 2,
                       "custom_buckets": [{"key": "raw_resources", "label": "x"}]},
                      container_keys)
    assert any("already a generated bucket" in i["message"] for i in clash), \
        "a custom bucket cannot shadow a generated one"


# ------------------------------------------------------ enrich and strip_ui


def test_the_config_sent_to_the_browser_carries_display_names(synthetic_config):
    enr = cfgmod.enrich(synthetic_config)
    assert enr["item_rules"][0].get("_name") == "Sodium", \
        ("the config sent to the browser carries display names: %s"
         % enr["item_rules"][0].get("_name"))


def test_display_names_are_stripped_again_before_the_config_is_stored(
        synthetic_config):
    enr = cfgmod.enrich(synthetic_config)
    stripped = cfgmod._strip_ui(enr)
    assert not any(k.startswith("_") for k in stripped["item_rules"][0]), \
        "display names are stripped again before the config is stored"


# ===================================================== defect register P1-4
#
# Config version 2 and its migration, the `.bak` rotation, `_strip_ui`, and
# the validator readers a list-valued `store` needs.

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _real_v1():
    """The operator's own 848-line configuration, ids intact.

    It carries no personal data: container keys, item ids and numbers. It is
    the only input that proves the migration against what a real config looks
    like rather than against what one was imagined to look like.
    """
    with open(os.path.join(FIXTURES, "config_v1_real.json"), encoding="utf-8") as fh:
        return json.load(fh)


# --- the version itself -----------------------------------------------------


def test_this_build_writes_version_two():
    assert cfgmod.CONFIG_VERSION == 2, \
        "a schema that gained six fields without a version bump gets one"
    assert cfgmod.default_config()["config_version"] == 2, \
        "and the shipped default is written at it"


def test_config_too_new_is_a_value_error():
    assert issubclass(cfgmod.ConfigTooNew, ValueError), \
        "so a caller that only knows ValueError still gets the sentence"


# --- the migration ----------------------------------------------------------


def test_the_real_v1_config_migrates_to_version_two():
    cfg, notes = cfgmod.migrate(_real_v1())
    assert cfg["config_version"] == 2, "version bumped: %r" % cfg["config_version"]
    assert notes, "and the migration says what it did"


def test_the_corvette_overrides_are_dropped_because_they_are_the_default():
    """640 of the 641 overrides route Corvette parts to a bucket the shipped
    taxonomy now generates. An override that agrees with the default is 76% of
    a file saying nothing."""
    before = _real_v1()
    cfg, _notes = cfgmod.migrate(before)
    idb = db()
    expected = dict((k, v) for k, v in before["item_buckets"].items()
                    if idb.original_bucket_of(k) != v)
    assert cfg["item_buckets"] == expected, \
        ("only the overrides that still say something survive: %d of %d"
         % (len(cfg["item_buckets"]), len(before["item_buckets"])))
    assert len(cfg["item_buckets"]) < len(before["item_buckets"]), \
        "the file really does shrink"


def test_a_custom_bucket_the_taxonomy_now_generates_is_dropped():
    """The same config declares `corvette_parts` as a custom bucket. Leaving it
    would make the migrated file fail its own validator."""
    cfg, _notes = cfgmod.migrate(_real_v1())
    keys = [b.get("key") for b in cfg.get("custom_buckets") or []]
    assert "corvette_parts" not in keys, \
        "the custom declaration goes with the overrides: %s" % keys


def test_the_migrated_config_validates_clean(container_keys):
    cfg, _notes = cfgmod.migrate(_real_v1())
    ck, sk, dk = container_keys
    # This save has neither the operator's chests nor their extractors, so
    # container errors are expected and beside the point. What must not be
    # there is a *schema* error: a version, a bucket or an id the validator
    # cannot make sense of.
    bad = [i for i in cfgmod.validate(cfg, None, None) if i["level"] == "error"]
    assert not bad, "a migrated config has no schema errors: %s" % bad


def test_the_migration_is_idempotent():
    once, _n1 = cfgmod.migrate(_real_v1())
    twice, n2 = cfgmod.migrate(once)
    assert twice == once, "migrating twice changes nothing further"
    assert n2 == [], "and has nothing to say about it: %s" % n2


def test_the_migration_does_not_touch_its_input():
    before = _real_v1()
    cfgmod.migrate(before)
    assert before["config_version"] == 1 and len(before["item_buckets"]) == 641, \
        "migrate() is pure: the caller's object is untouched"


def test_the_same_save_plans_the_same_moves_before_and_after_migration(
        synthetic_path):
    """The migration is only allowed to drop what says nothing. If a move row
    moves, it dropped something that mattered."""
    def rows(cfg):
        plan, _ = planner.build_plan(SaveFile(synthetic_path), cfg)
        return [(r["key"], r["dst"], r["id"], r["moved"])
                for r in plan.rows if r["op"] == "move"]

    before = _real_v1()
    after, _notes = cfgmod.migrate(before)
    assert rows(after) == rows(before), "the same moves, before and after"


def test_stores_is_folded_into_store_as_a_list():
    cfg, notes = cfgmod.migrate({
        "config_version": 1,
        "item_rules": [{"item": "CATALYST1", "store": "chest1",
                        "stores": ["chest2", "chest3"]}],
        "bucket_rules": [{"bucket": "fish", "stores": ["chest4", "chest5"]}]})
    rule = cfg["item_rules"][0]
    assert rule["store"] == ["chest1", "chest2", "chest3"], \
        "one field, in chain order: %r" % rule.get("store")
    assert "stores" not in rule, "and the old spelling is gone"
    assert cfg["bucket_rules"][0]["store"] == ["chest4", "chest5"], \
        "bucket rules fold the same way: %r" % cfg["bucket_rules"][0]
    assert any("stores" in n for n in notes), "the fold is reported: %s" % notes


def test_a_single_destination_stays_a_plain_string():
    cfg, _notes = cfgmod.migrate({
        "config_version": 1,
        "item_rules": [{"item": "CATALYST1", "stores": ["chest1"]}]})
    assert cfg["item_rules"][0]["store"] == "chest1", \
        "a chain of one is a string, not a list of one"


def test_an_empty_labels_map_is_dropped():
    cfg, notes = cfgmod.migrate({"config_version": 1, "labels": {}})
    assert "labels" not in cfg, "an empty deprecated field is just noise"
    assert any("labels" in n for n in notes), notes


def test_a_labels_map_someone_typed_is_kept():
    """A non-empty `labels` is a thing a person typed, and a migration that
    deletes those is a migration nobody trusts."""
    cfg, _notes = cfgmod.migrate({"config_version": 1,
                                  "labels": {"chest1": "Ores"}})
    assert cfg["labels"] == {"chest1": "Ores"}, \
        "it is kept: %r" % cfg.get("labels")


def test_a_labels_map_is_not_warned_about(container_keys):
    """`labels` is read by `server.save_view` and written by the rename box on
    every Save card. The validator used to warn that nothing read it, which
    told a player their only way of naming an exocraft did nothing."""
    issues = _validate(dict(cfgmod.default_config(),
                            labels={"chest1": "Dragonfly"}), container_keys)
    assert not any("labels" in (i.get("where") or "") for i in issues), \
        "nothing to say about a field that works: %s" % _messages(issues)
    assert not any("deprecated" in i["message"] for i in issues), \
        _messages(issues)


def test_an_empty_labels_map_is_not_warned_about(container_keys):
    issues = _validate(cfgmod.default_config(), container_keys)
    assert not any("labels" in (i.get("where") or "") for i in issues), \
        "nothing to say about a field nobody filled in"


# --- labels, validated ------------------------------------------------------
# Blessing `labels` in c88e93d put a `json` fence in docs/RULES.md inviting a
# player to hand edit the map and left it the only container-key-bearing field
# with no check at all. `"labels": "vehicle4"` -- the shape a person most
# plausibly types -- saved without a word and then raised out of /api/select,
# so the Save section could not render at all (review 4, finding 4).


@pytest.mark.parametrize("bad", ["vehicle4", ["vehicle4"], 42, True])
def test_a_labels_field_that_is_not_an_object_is_an_error(container_keys, bad):
    issues = _validate(dict(cfgmod.default_config(), labels=bad), container_keys)
    errs = [i for i in _errors(issues) if i["where"] == "labels"]
    assert errs, "the whole field is the wrong shape: %s" % _messages(issues)
    assert "has to be an object" in errs[0]["message"], errs[0]["message"]


@pytest.mark.parametrize("bad", [42, ["x"], {"a": 1}, None, True])
def test_a_label_that_is_not_text_is_an_error(container_keys, bad):
    """It reached the card verbatim: `42`, `['x']`, `{'a': 1}`."""
    issues = _validate(dict(cfgmod.default_config(), labels={"vehicle4": bad}),
                       container_keys)
    errs = [i for i in _errors(issues) if i["where"] == "labels[vehicle4]"]
    assert errs, "a name has to be text: %s" % _messages(issues)
    assert "has to be text" in errs[0]["message"], errs[0]["message"]


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n "])
def test_a_blank_or_whitespace_label_is_an_error(container_keys, blank):
    """`docs/RULES.md` said whitespace was the same as no entry and the bare
    truthiness test in `server.save_view` kept it, so the card rendered blank
    and said it was renamed."""
    issues = _validate(dict(cfgmod.default_config(), labels={"chest1": blank}),
                       container_keys)
    errs = [i for i in _errors(issues) if i["where"] == "labels[chest1]"]
    assert errs, "blank is not a name: %r -> %s" % (blank, _messages(issues))
    assert "cannot be blank" in errs[0]["message"], errs[0]["message"]


def test_a_label_longer_than_the_ceiling_is_an_error(container_keys):
    """A 300-character label reached the card, three `<option>` lists and the
    plan's container table verbatim."""
    long = "x" * (cfgmod.LABEL_MAX + 1)
    issues = _validate(dict(cfgmod.default_config(), labels={"chest1": long}),
                       container_keys)
    errs = [i for i in _errors(issues) if i["where"] == "labels[chest1]"]
    assert errs, "a ceiling, and it is stated: %s" % _messages(issues)
    assert str(cfgmod.LABEL_MAX) in errs[0]["message"], errs[0]["message"]
    ok = _validate(dict(cfgmod.default_config(),
                        labels={"chest1": "x" * cfgmod.LABEL_MAX}),
                   container_keys)
    assert not [i for i in _errors(ok) if i["where"] == "labels[chest1]"], \
        "the ceiling itself is allowed: %s" % _messages(ok)


def test_a_label_for_a_container_this_save_lacks_is_a_warning(container_keys):
    """A warning, not an error: one configuration serves every save in the
    folder, so a name for a ship slot this save has not got is the other
    save's ship, not a mistake -- and an error would block every later config
    save until it was deleted."""
    issues = _validate(dict(cfgmod.default_config(),
                            labels={"nosuchkey": "Ores"}), container_keys)
    mine = [i for i in issues if i["where"] == "labels[nosuchkey]"]
    assert mine, "it used to do nothing, silently, forever: %s" % _messages(issues)
    assert mine[0]["level"] == "warning", mine[0]
    assert "is not a container in this save" in mine[0]["message"], mine[0]


# --- load() -----------------------------------------------------------------


def test_load_migrates_a_v1_file_on_read(tmp_path):
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(_real_v1(), fh)
    before = open(path, encoding="utf-8").read()
    notes = []
    cfg, created = cfgmod.load(path, notes)
    assert created is False, "an existing file was not created"
    assert cfg["config_version"] == 2, "the object handed back is v2"
    assert notes, "and the caller is told what changed: %s" % notes
    assert open(path, encoding="utf-8").read() == before, \
        "but nothing is written until the next store()"


def test_load_refuses_a_file_from_a_newer_build(tmp_path):
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"config_version": 99}, fh)
    with pytest.raises(cfgmod.ConfigTooNew) as exc:
        cfgmod.load(path)
    assert "this config is version 99" in str(exc.value), exc.value
    assert getattr(exc.value, "path", None) == path, \
        "the path is on the exception, not in the sentence"


# --- D19: five rotating revisions ------------------------------------------


def test_six_saves_leave_five_numbered_backups(tmp_path):
    """D19. There was one `.bak`, rewritten every save. The operator kept three
    hand-made `.pre-*` copies because that was not a backup."""
    path = str(tmp_path / "config.json")
    for n in range(1, 7):
        cfgmod.store(path, dict(cfgmod.default_config(), name="v%d" % n))
    names = sorted(f for f in os.listdir(str(tmp_path)) if ".bak" in f)
    assert names == ["config.json.bak.%d" % n for n in range(1, 6)], \
        "five numbered revisions and no more: %s" % names

    def name_in(fn):
        with open(str(tmp_path / fn), encoding="utf-8") as fh:
            return json.load(fh)["name"]

    assert name_in("config.json") == "v6", "the current file is the newest"
    assert [name_in("config.json.bak.%d" % n) for n in range(1, 6)] == \
        ["v5", "v4", "v3", "v2", "v1"], \
        "and .bak.1 is the one just replaced, .bak.5 the oldest kept"


def test_a_legacy_single_bak_is_taken_into_the_rotation(tmp_path):
    """Anyone upgrading has a `config.json.bak` from the old single slot. It is
    the only copy they have; the rotation adopts it rather than overwrites it."""
    path = str(tmp_path / "config.json")
    cfgmod.store(path, dict(cfgmod.default_config(), name="current"),
                 backup=False)
    with open(path + ".bak", "w", encoding="utf-8") as fh:
        json.dump(dict(cfgmod.default_config(), name="legacy"), fh)
    cfgmod.store(path, dict(cfgmod.default_config(), name="second"))
    assert not os.path.exists(path + ".bak"), \
        "the old single slot is not left behind to confuse anyone"
    kept = []
    for n in range(1, 6):
        p = path + ".bak.%d" % n
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                kept.append(json.load(fh)["name"])
    assert "legacy" in kept, \
        "and what was in it is still on disk: %s" % kept


# --- D18: _strip_ui strips the UI fields and nothing else -------------------


def test_an_underscore_key_in_item_buckets_survives_a_store(tmp_path):
    """D18. `_strip_ui` deleted any `_`-prefixed key at any depth, and an item
    id is a key in `item_buckets`. One that starts with an underscore was
    silently dropped every time the config was saved."""
    path = str(tmp_path / "config.json")
    cfg = cfgmod.default_config()
    cfg["item_buckets"] = {"_ODD_ID": "raw_resources",
                           "CATALYST1": "raw_resources"}
    cfgmod.store(path, cfg)
    with open(path, encoding="utf-8") as fh:
        back = json.load(fh)
    assert back["item_buckets"] == {"_ODD_ID": "raw_resources",
                                    "CATALYST1": "raw_resources"}, \
        "an override key is data, not a display field: %s" % back["item_buckets"]


def test_the_browsers_display_fields_are_still_stripped(synthetic_config):
    enr = cfgmod.enrich(synthetic_config)
    stripped = cfgmod._strip_ui(enr)
    assert not any(k.startswith("_") for k in stripped["item_rules"][0]), \
        "the three keys the page stamps on are removed"


def test_an_unknown_underscore_key_on_a_rule_is_left_alone():
    """Only the three keys `enrich` adds are display fields. Anything else a
    person put there is theirs."""
    cfg = {"item_rules": [{"item": "CATALYST1", "_mine": 1, "_name": "Sodium"}]}
    out = cfgmod._strip_ui(cfg)
    assert out["item_rules"][0] == {"item": "CATALYST1", "_mine": 1}, \
        "only the known UI keys go: %s" % out["item_rules"][0]


# --- the validator learns the list form -------------------------------------


def test_a_list_form_store_is_checked_entry_by_entry(container_keys):
    bad = _validate({"config_version": 2, "bucket_rules": [
        {"bucket": "raw_resources", "store": ["chest1", "chestNOPE"]}]},
        container_keys)
    assert any("chestNOPE" in i["message"] for i in bad), \
        "every container in the chain is checked: %s" % _messages(bad)
    assert not any("chest1" in i["message"] for i in bad), \
        "and the one that exists is not complained about: %s" % _messages(bad)


def test_a_list_form_store_is_a_destination(container_keys):
    ok = _validate({"config_version": 2, "bucket_rules": [
        {"bucket": "raw_resources", "store": ["chest1", "chest2"]}]},
        container_keys)
    assert not any("no destination container" in i["message"] for i in ok), \
        "a chain is a destination: %s" % _messages(ok)


def test_an_item_rule_with_a_list_store_does_something(container_keys):
    issues = _validate({"config_version": 2, "sources": ["suit"], "item_rules": [
        {"item": "CATALYST1", "store": ["chest1", "chest2"]}]}, container_keys)
    assert not any("does nothing" in i["message"] for i in issues), \
        "a rule with a chain is not idle: %s" % _messages(issues)


def test_an_item_rule_with_only_stores_does_something(container_keys):
    """The `stores` spelling stays readable after the migration, because a
    config on disk is not migrated until it is next saved."""
    issues = _validate({"config_version": 2, "sources": ["suit"], "item_rules": [
        {"item": "CATALYST1", "stores": ["chest1"]}]}, container_keys)
    assert not any("does nothing" in i["message"] for i in issues), \
        "a stores-only rule is not idle either: %s" % _messages(issues)


def test_the_does_nothing_warning_names_every_mode(container_keys):
    idle = _validate({"config_version": 2, "sources": ["suit"],
                      "item_rules": [{"item": "CATALYST1"}]}, container_keys)
    said = [i["message"] for i in idle if "does nothing" in i["message"]]
    assert said == ["no store, no keep, no stock and no pin: this rule does "
                    "nothing"], "the sentence lists what it looked for: %s" % said


def test_a_fill_on_a_list_store_is_not_an_error(container_keys):
    issues = _validate({"config_version": 2, "sources": ["suit"], "item_rules": [
        {"item": "CATALYST1", "store": ["chest1"], "fill": 100}]}, container_keys)
    assert not any("needs a `store`" in i["message"] for i in issues), \
        "a ceiling has something to apply to: %s" % _messages(issues)


def test_the_same_chain_twice_can_never_fire(container_keys):
    same = _validate({"config_version": 2, "bucket_rules": [
        {"bucket": "raw_resources", "store": ["chest1", "chest2"]},
        {"bucket": "raw_resources", "store": ["chest1", "chest2"]}]},
        container_keys)
    assert any("can never fire" in i["message"] for i in same), \
        "an exact repeat is still a typo: %s" % _messages(same)


# --- the config hash the fingerprint and the manifest share -----------------


def test_the_config_hash_ignores_key_order_and_nothing_else():
    a = {"x": 1, "y": [1, 2]}
    b = {"y": [1, 2], "x": 1}
    assert cfgmod.config_hash(a) == cfgmod.config_hash(b), \
        "key order is not a change"
    assert cfgmod.config_hash(a) != cfgmod.config_hash({"x": 1, "y": [2, 1]}), \
        "list order is"
    assert len(cfgmod.config_hash({})) == 64, "it is a sha256"


# --- P6-5 (write-path review two): Q11 and Q13 -----------------------------


def test_the_config_hash_ignores_when_it_was_saved():
    """Q11. `store` stamps `updated` on every write, and the hash goes into
    the plan fingerprint and into the backup manifest's `config_sha256` --
    the one field that can answer "was this backup taken under these rules?".
    A config saved with nothing touched used to mint a new value for both."""
    a = dict(cfgmod.default_config())
    b = dict(a)
    a["updated"] = "2026-09-14T06:01:10"
    b["updated"] = "2026-09-14T06:01:11"
    assert cfgmod.config_hash(a) == cfgmod.config_hash(b), \
        "the same rules at two different moments: %s vs %s" \
        % (cfgmod.config_hash(a)[:8], cfgmod.config_hash(b)[:8])


def test_the_config_hash_ignores_a_top_level_underscore_key():
    a = dict(cfgmod.default_config())
    b = dict(a)
    b["_scratch"] = {"the page": "put this here"}
    assert cfgmod.config_hash(a) == cfgmod.config_hash(b), \
        "the page's own scratch space is not a rule"


def test_the_config_hash_still_moves_when_a_rule_moves():
    a = cfgmod.default_config()
    b = cfgmod.default_config()
    b["options"] = dict(b["options"], max_moves=1)
    assert cfgmod.config_hash(a) != cfgmod.config_hash(b), \
        "and a rule that changed is still a different configuration"


def test_an_underscore_key_inside_item_buckets_is_still_hashed():
    """Top level only: an item id that begins with an underscore is data, not
    decoration, which is the trap `_strip_ui` documents."""
    a = {"item_buckets": {"_ODD1": "chest1"}}
    b = {"item_buckets": {}}
    assert cfgmod.config_hash(a) != cfgmod.config_hash(b)


def test_rule_bearing_keeps_everything_a_plan_reads():
    cfg = cfgmod.default_config()
    kept = cfgmod.rule_bearing(cfg)
    assert "updated" not in kept
    for key in ("bucket_rules", "item_rules", "options", "never_buckets",
                "config_version"):
        assert key in kept, "%s is a rule-bearing key: %s" % (key, sorted(kept))


def test_a_successful_store_still_rotates_the_revision_it_retired(tmp_path):
    """Q13's fix moves the rotation after the replace, so the thing that gets
    rotated in has to be the *retired* content -- not the file that is now on
    disk, which is the new revision."""
    path = str(tmp_path / "config.json")
    cfg = cfgmod.default_config()
    cfg["name"] = "first"
    cfgmod.store(path, cfg, backup=False)
    cfg["name"] = "second"
    cfgmod.store(path, cfg)
    with open(path, encoding="utf-8") as fh:
        assert json.load(fh)["name"] == "second"
    with open(path + ".bak.1", encoding="utf-8") as fh:
        assert json.load(fh)["name"] == "first", \
            "`.bak.1` is the revision the replace retired, not a copy of the "\
            "one that just landed"
    assert not [f for f in os.listdir(str(tmp_path)) if f.endswith(".prev")], \
        "and the scratch copy is consumed: %s" % os.listdir(str(tmp_path))


def test_a_rotation_that_fails_does_not_fail_the_write(tmp_path, monkeypatch,
                                                       caplog):
    """The write is what the caller asked for. A revision slot that could not
    be shifted afterwards is a log line, not a lost configuration."""
    path = str(tmp_path / "config.json")
    cfg = cfgmod.default_config()
    cfgmod.store(path, cfg, backup=False)

    def no_rotation(*_a, **_kw):
        raise OSError(13, "the backup slot is held open")

    monkeypatch.setattr(cfgmod, "rotate_backups", no_rotation)
    cfg["name"] = "the one that matters"
    cfgmod.store(path, cfg)
    with open(path, encoding="utf-8") as fh:
        assert json.load(fh)["name"] == "the one that matters"


# --- the two sentences lane D found (P6-5) ---------------------------------


def test_a_repeated_rule_with_no_destination_does_not_name_one(container_keys):
    """"'CATALYST1' is already routed to None by rule 1" is what a repeated
    `keep`-only or `stock`-only rule rendered, and `None` is not a container
    anybody typed."""
    ck, sk, dk = container_keys
    cfg = cfgmod.default_config()
    cfg["item_rules"] = [{"item": "^CATALYST1", "keep": 250},
                         {"item": "^CATALYST1", "keep": 250}]
    cfg["bucket_rules"] = [{"bucket": "fuel", "keep": 1},
                           {"bucket": "fuel", "keep": 1}]
    msgs = _messages(cfgmod.validate(cfg, ck, sk, dk))
    assert "already covered by rule 1" in msgs, msgs
    assert "routed to None" not in msgs and "routed to []" not in msgs, msgs
    assert "this exact pair can never fire" in msgs, msgs


def test_a_repeated_rule_with_a_destination_still_names_it(container_keys):
    ck, sk, dk = container_keys
    cfg = cfgmod.default_config()
    cfg["item_rules"] = [{"item": "^CATALYST1", "store": "chest1"},
                         {"item": "^CATALYST1", "store": "chest1"}]
    msgs = _messages(cfgmod.validate(cfg, ck, sk, dk))
    assert "is already routed to 'chest1' by rule 1" in msgs, msgs


def test_the_homeless_bucket_warning_is_two_short_sentences(container_keys):
    """Lane D: one 34-word sentence, split. The opening fragment is unchanged
    so the documentation entry keyed on it still matches."""
    ck, sk, dk = container_keys
    cfg = cfgmod.default_config()
    cfg["bucket_rules"] = []
    said = [i["message"] for i in cfgmod.validate(cfg, ck, sk, dk)
            if "have no destination" in i["message"]]
    assert said, "the warning still fires with no rules at all"
    msg = said[0]
    assert msg.startswith(msg.split()[0] + " bucket(s) have no destination, "
                          "so anything in them stays put wherever it already "
                          "is."), msg
    head, tail = msg.split(". ", 1)
    for part in (head, tail.split(":")[0]):
        assert len(part.split()) < 30, \
            "%d words: %s" % (len(part.split()), part)


# --- C4: a key the file leaves out is empty, not the shipped default -------

#: `docs/GUIDE.md`, "Minimal: raw materials and technology". Two chests, four
#: categories, and no `item_rules` key at all -- which used to mean "the five
#: shipped item rules", one of which sends Chromatic Metal to `chest2`.
MINIMAL = {
    "config_version": 2,
    "name": "Minimal",
    "sources": ["suit", "ship0"],
    "bucket_rules": [
        {"bucket": "raw_resources", "store": "chest1"},
        {"bucket": "refined_crafted", "store": "chest1"},
        {"bucket": "tech_upgrades", "store": "chest2"},
        {"bucket": "tech_upgrades_elite", "store": "chest2"},
    ],
}


def _write(tmp_path, body, name="config.json"):
    path = str(tmp_path / name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(body, fh)
    return path


def test_the_minimal_example_inherits_no_item_rules(tmp_path):
    """The file names four bucket rules and nothing else. Everything it does
    not name is empty, because the file is the operator's statement about
    their save and the shipped rules are not in it."""
    notes = []
    cfg, created = cfgmod.load(_write(tmp_path, MINIMAL), notes)
    assert created is False
    assert cfg["item_rules"] == [], \
        "five shipped rules used to arrive uninvited: %s" % cfg["item_rules"]
    assert cfg["never_buckets"] == [], cfg["never_buckets"]
    assert cfg["item_buckets"] == {} and cfg["never_move"] == []
    assert cfg["bucket_rules"] == MINIMAL["bucket_rules"], \
        "and what the file does say is untouched"
    assert cfg["sources"] == ["suit", "ship0"]


def test_the_merge_says_which_keys_it_filled_in(tmp_path):
    """A config that means less than it appears to is what this exists to
    stop, so every key the merge had to answer for is in the load notes."""
    notes = []
    cfgmod.load(_write(tmp_path, MINIMAL), notes)
    assert "item_rules absent; treated as empty" in notes, notes
    assert "never_buckets absent; treated as empty" in notes, notes
    assert not any("bucket_rules absent" in n for n in notes), \
        "and not the ones the file carries: %s" % notes


def test_an_option_the_file_omits_still_takes_its_default(tmp_path):
    """`options` is tuning numbers rather than rules: a file that does not
    mention `max_moves` is not asking for zero moves."""
    body = dict(MINIMAL, options={"max_moves": 3})
    cfg, _created = cfgmod.load(_write(tmp_path, body))
    assert cfg["options"]["max_moves"] == 3, "what the file says wins"
    assert cfg["options"]["merge_duplicates"] is \
        cfgmod.default_config()["options"]["merge_duplicates"], \
        "and the rest are the shipped defaults: %s" % cfg["options"]


def test_a_config_that_is_not_there_still_gets_the_shipped_default(tmp_path):
    """The one case with no statement to respect -- and it is written out, so
    what it says is visible rather than implied."""
    path = str(tmp_path / "config.json")
    cfg, created = cfgmod.load(path)
    assert created is True
    assert len(cfg["item_rules"]) == len(
        cfgmod.default_config()["item_rules"]) > 0
    assert os.path.exists(path), "and it is on disk to be edited"


def test_the_default_config_round_trips_through_load_unchanged(tmp_path):
    """The merge must not empty a key the shipped default fills: a file that
    carries every key is unaffected by the rule above."""
    shipped = cfgmod.default_config()
    notes = []
    cfg, _created = cfgmod.load(_write(tmp_path, shipped), notes)
    for key in ("item_rules", "bucket_rules", "sources", "never_buckets",
                "item_buckets", "never_move", "custom_buckets"):
        assert cfg[key] == shipped[key], key
    assert notes == [], "nothing was filled in: %s" % notes


# --- the player-day review: W7 (the stamp the two-tab check compares) and
# --- W14 (the shipped rules, and which of them a save can honour) ----------


def test_every_store_stamps_when_it_was_saved(tmp_path):
    """W7 rests on this field. `POST /api/config` carries `based_on`, the
    `updated` the page loaded, and refuses a write whose stamp is not the one
    in the file -- so `store` has to put a stamp there, `load` has to hand it
    back, and it has to move when the file is rewritten."""
    path = str(tmp_path / "config.json")
    first = cfgmod.store(path, cfgmod.default_config())
    assert first["updated"], "a stored configuration says when it was stored"
    back, _created = cfgmod.load(path)
    assert back["updated"] == first["updated"], \
        "the page is handed the stamp that is in the file"
    later = dict(first)
    later["updated"] = "1999-01-01T00:00:00"
    again = cfgmod.store(path, later)
    assert again["updated"] != "1999-01-01T00:00:00", \
        "the stamp is the server's, not the client's: a page cannot pin it"


def test_the_stamp_is_local_time_a_person_can_read(tmp_path):
    """W17: every time this program shows a player is local. The stamp is
    rendered into the two-tab refusal, so it is the same clock as the rest."""
    stored = cfgmod.store(str(tmp_path / "config.json"),
                          cfgmod.default_config())
    when = datetime.datetime.fromisoformat(stored["updated"])
    assert when.tzinfo is None, "a local stamp carries no offset"
    assert abs((datetime.datetime.now() - when).total_seconds()) < 120


def test_the_shipped_default_names_the_ten_chests_and_the_processor():
    """W14. The shipped layout is unchanged -- it is what a save with ten
    chests should start with. What changed is that `App` drops the rules the
    *selected save* has no container for, because the page labels a container
    by the player's own name for it and a rule that can never fire read as
    advice: "Raw Resources -> Raw Resources (chest1)"."""
    rules = cfgmod.default_config()["bucket_rules"]
    dests = sorted(set(r["store"] for r in rules))
    assert dests == ["chest1", "chest10", "chest2", "chest3", "chest4",
                     "chest5", "chest6", "chest7", "chest8", "chest9",
                     "cooking"], dests


def test_a_destination_a_save_does_not_have_is_named_by_stores_of():
    """The one reader of "where does this rule send things", and what `App`
    filters on. A chain counts as honourable while any of its containers is
    in the save: overflow into a chest you do have is still a route."""
    assert cfgmod.stores_of({"bucket": "fish", "store": "chest4"}) == \
        ["chest4"]
    assert cfgmod.stores_of({"bucket": "fish",
                             "store": ["chest4", "chest5"]}) == \
        ["chest4", "chest5"]
    assert cfgmod.stores_of({"bucket": "fish", "keep": 10}) == []


def test_a_rule_naming_a_container_this_save_lacks_is_an_error(
        container_keys):
    """And the reason dropping it beats keeping it: the validator calls it an
    error, so a shipped default nobody wrote put a red row on the
    Configuration tab of a save with fewer chests."""
    cfg = cfgmod.default_config()
    cfg["bucket_rules"] = [{"bucket": "fish", "store": "chest99"}]
    issues = _validate(cfg, container_keys)
    assert any("chest99" in i["message"] for i in _errors(issues)), \
        _messages(issues)
