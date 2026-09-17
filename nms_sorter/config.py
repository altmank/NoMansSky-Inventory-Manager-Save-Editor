"""The sorter configuration: load, validate, migrate, save, version.

One versioned JSON file. `config_version` is read on load: a file from an
older version is migrated in memory and written back only when something else
saves it, and a file from a *newer* version is refused, because this build
cannot know what it would be throwing away. Every save rotates five numbered
`.bak` revisions, so an edit made in the browser is always one file copy away
from being undone -- and so is the one before that.

The model is the one fixed during development and left unchanged; it is
written up in `docs/RULES.md`:

  sources       ordered container keys that get drained
  bucket_rules  ordered [{bucket, store}], first match wins
  item_rules    ordered [{item, store, keep, fill, move, priority, pin}],
                consulted before the bucket rules, lower priority first
  never_move    ids pinned wherever they are
  never_buckets buckets a catch-all `*` rule may not sweep up

The one thing that is *not* here is a container-to-bucket map keyed the other
way round. A bucket has one destination; a container may receive several
buckets. The page draws it as containers-with-buckets because that is how a
person thinks about storage, and writes it back as this ordered list, because
that is what decides.
"""
import copy
import datetime
import hashlib
import json
import logging
import os
import shutil

from .itemdb import db, UNROUTABLE, UNSORTED

import re

log = logging.getLogger("nms_sorter.config")

CONFIG_VERSION = 2

#: how many previous revisions `store()` keeps, newest as `.bak.1`
BAK_KEEP = 5

#: the word in the middle of a kept configuration's name: `config.json` is
#: kept as `config.before-reset-20260916-142233.json`. A name of its own
#: rather than a slot in the `.bak.1`..`.bak.5` rotation, because that
#: rotation shifts on every save and would push the configuration somebody
#: started over from off the end after five edits.
KEPT_INFIX = "before-reset"

#: the keys `enrich()` stamps on an item rule for the browser, and the only
#: keys `_strip_ui` may remove. It used to remove any `_`-prefixed key at any
#: depth, and an item id is a key in `item_buckets`.
UI_KEYS = ("_name", "_bucket", "_bucket_label")

#: `never_buckets` blocks the `*` catch-all. `system_meta` used to be in this
#: list; it is unroutable now -- nothing in it is ever in a container, so no
#: rule may name it at all -- and a list entry for it would be a second,
#: weaker answer to the same question. `migrate()` drops it.
DEFAULT_NEVER_BUCKETS = [UNSORTED]

#: the longest container name `labels` accepts. The game's own field limit is
#: 40 (`naming.MAX_NAME`) and that one is written into a save; this one is only
#: ever drawn on a card, in three pickers and in the plan's container table, so
#: it is wider -- but it is bounded, because none of those places can lay out a
#: 300-character name and a hand-edited file is the only way to get one.
LABEL_MAX = 64


class ConfigTooNew(ValueError):
    """The file was written by a build that knew more than this one.

    Refused rather than read, because a field this build does not know is a
    field it would drop on the next save. `path` carries the file; the message
    is the sentence, so `docs/TROUBLESHOOTING.md` can be keyed on it.
    """

    def __init__(self, message, path=None, file_version=None):
        ValueError.__init__(self, message)
        self.path = path
        self.file_version = file_version


def stores_of(rule):
    """A rule's destination containers, in order.

    The one reader of "where does this rule send things". `store` is a
    container or a list of them (config v2); `stores` is v1's spelling of the
    same chain and is still read, because a file on disk is not migrated until
    something saves it. The planner, the namer, the validator and the migrator
    all call this: a second implementation is a second answer.
    """
    store = rule.get("store")
    first = list(store) if isinstance(store, (list, tuple)) else [store]
    out = []
    for v in (first + list(rule.get("stores") or [])):
        if v and v not in out:
            out.append(v)
    return out


def _generated_buckets():
    """Bucket keys that come from the generated taxonomy, never from a config."""
    idb = db()
    base = idb._base_buckets if idb._base_buckets is not None else idb.buckets
    return set([b["key"] for b in base] + [UNSORTED])


def _now():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def canonical_json(cfg):
    """The configuration as one deterministic string.

    Sorted keys and no whitespace, so two configurations that differ only in
    key order or indentation hash the same and two that differ in any value do
    not. The plan fingerprint and the backup manifest both hash this, and they
    have to agree on what "the same configuration" means.
    """
    return json.dumps(cfg or {}, sort_keys=True, separators=(",", ":"),
                      default=str)


#: keys `config_hash` leaves out, because they cannot change what a plan does.
#: `updated` is the one that mattered: `store` stamps it on every write, so a
#: configuration saved with no rule touched used to mint a different plan
#: fingerprint *and* a different `config_sha256` in the backup manifest --
#: which is the only field that can answer "was this backup taken under these
#: rules?" (write-path review two, Q11). Measured: two `default_config()` calls
#: 1.2 s apart hashed differently.
HASH_EXCLUDED = ("updated",)


def rule_bearing(cfg):
    """`cfg` with the keys that cannot change a plan removed.

    `HASH_EXCLUDED` plus any `_`-prefixed top-level key: those are the page's
    own scratch space (`enrich()` adds them and `_strip_ui` takes the nested
    ones back out) and nothing downstream of the planner reads them. Top level
    only, deliberately -- an `item_buckets` override whose item id starts with
    an underscore is data, which is the trap `_strip_ui` already documents.
    """
    return dict((k, v) for k, v in (cfg or {}).items()
                if k not in HASH_EXCLUDED and not str(k).startswith("_"))


def config_hash(cfg):
    """SHA-256 of the rule-bearing part of a configuration.

    Not of the whole document: see `rule_bearing`. Two configurations that
    differ only in when they were saved are the same rules and hash the same,
    which is what both readers -- the plan fingerprint (`planner._fingerprint`)
    and the manifest's `config_sha256` -- mean by "the same configuration".
    """
    return hashlib.sha256(
        canonical_json(rule_bearing(cfg)).encode(
            "utf-8", "surrogateescape")).hexdigest()


def default_config():
    """A starting layout for the ten chests plus cooking.

    One rule per generated bucket that has a chest of its own, in the
    taxonomy's own order, read from `data/buckets.json` rather than counted in
    this docstring. The buckets left over are deliberately left without a
    destination rather than swept into a catch-all: a rule that lands every
    base-building part in one box is worse than no rule.
    """
    return {
        "config_version": CONFIG_VERSION,
        "name": "Default layout",
        "updated": _now(),
        "notes": "Ten chests plus the Nutrient Processor. Edit in the browser; every "
                 "save keeps the five previous revisions alongside, as "
                 "config.json.bak.1 to .bak.5.",
        "options": {
            "merge_duplicates": True,
            "create_stacks": True,
            "min_source": 1,
            "max_moves": 200,
            "max_new_stacks": 80,
            "tidy": [],
            "auto_name": False,
            "auto_name_overwrite": False,
        },
        "sources": ["suit", "ship0", "freighter", "corvette"],
        "bucket_rules": [
            {"bucket": "raw_resources", "store": "chest1"},
            {"bucket": "refined_crafted", "store": "chest2"},
            {"bucket": "trade_goods", "store": "chest3"},
            {"bucket": "food_ingredients", "store": "cooking"},
            {"bucket": "fish", "store": "chest4"},
            {"bucket": "tech_upgrades", "store": "chest5"},
            {"bucket": "tech_upgrades_elite", "store": "chest6"},
            {"bucket": "utility_consumables", "store": "chest7"},
            {"bucket": "curiosities_artifacts", "store": "chest8"},
            {"bucket": "base_structures", "store": "chest9"},
            {"bucket": "base_decor", "store": "chest9"},
            {"bucket": "ship_construction", "store": "chest10"},
            {"bucket": "salvage_junk", "store": "chest10"},
        ],
        "item_rules": [
            {"item": "CATALYST1", "keep": 250, "priority": 10,
             "note": "Sodium: always keep 250 in the source, then let the bucket "
                     "rules take the surplus"},
            {"item": "OXYGEN", "keep": 250, "priority": 10,
             "note": "Oxygen: life support and refining"},
            {"item": "FUEL1", "keep": 500, "priority": 10,
             "note": "Carbon: never leave the suit short"},
            {"item": "ROCKETSUB", "keep": 200, "priority": 10,
             "note": "Tritium: pulse-engine fuel"},
            {"item": "STELLAR2", "store": "chest2", "keep": 500, "priority": 20,
             "note": "Chromatic Metal: keep a working stock, bank the rest"},
        ],
        "custom_buckets": [],
        "item_buckets": {},
        "never_move": [],
        "never_buckets": list(DEFAULT_NEVER_BUCKETS),
        "labels": {},
    }


# ---------------------------------------------------------------- validation

_INT_FIELDS = ("keep", "fill", "move", "priority", "stock")


def _kind(v):
    """The word a refusal uses for a value of the wrong JSON type.

    A type name (`str`, `dict`) is the programmer's word for it; a player hand
    editing the file sees the JSON, so the refusal says what the JSON is.
    """
    if v is None:
        return "nothing"
    if isinstance(v, bool):
        return "true or false"
    if isinstance(v, str):
        return "text"
    if isinstance(v, (int, float)):
        return "a number"
    if isinstance(v, list):
        return "a list"
    if isinstance(v, dict):
        return "an object"
    return "a value of another kind"


def validate(cfg, container_keys=None, sortable_keys=None, dead_keys=None):
    """Return a list of {level, where, message}. `level` is error or warning.

    Errors are configuration mistakes -- a typo, a rule that can never fire, a
    ceiling with no target. Warnings are facts about this particular save, like
    a rule pointing at a container that is not allocated right now: a sold ship
    is not a mistake.

    `dead_keys` are the containers with no cells at all, which nothing can
    ever be in. A rule naming one is valid and does nothing, and it is not
    worth a word: every existing configuration and both `docs/GUIDE.md`
    examples name `suit_cargo`, because the grid was real before Waypoint
    merged it away, and there is nothing for anybody to fix. The keys never
    change, so such a rule keeps working if a save ever has the grid again.
    """
    idb = db()
    out = []
    ck = set(container_keys or [])
    sk = set(sortable_keys or [])
    dk = set(dead_keys or [])

    def err(where, msg):
        out.append({"level": "error", "where": where, "message": msg})

    def warn(where, msg):
        out.append({"level": "warning", "where": where, "message": msg})

    if cfg.get("config_version") != CONFIG_VERSION:
        err("config_version", "this file is version %r; this build writes version %d"
            % (cfg.get("config_version"), CONFIG_VERSION))

    for i, key in enumerate(cfg.get("sources") or []):
        if ck and key not in ck:
            err("sources[%d]" % i, "%r is not a container in this save" % key)
        elif key.startswith("extractor"):
            pass        # drain-only, and never in the sortable list by design
        elif key in dk:
            pass        # no cells: a no-op, and not a mistake anybody made
        elif sk and key not in sk:
            warn("sources[%d]" % i,
                 "%r is not sortable right now (unallocated, or a technology grid)" % key)

    seen_buckets = {}
    for i, rule in enumerate(cfg.get("bucket_rules") or []):
        where = "bucket_rules[%d]" % i
        b = rule.get("bucket")
        chain = stores_of(rule)
        shown = chain[0] if len(chain) == 1 else chain
        if b != "*" and b not in idb.bucket_by_key:
            err(where, "no bucket named %r; a rule that can never match is a typo" % b)
        elif b in UNROUTABLE:
            err(where, "%r never occupies a container slot, so it cannot be "
                       "routed to one" % b)
        # The same bucket may name several containers. The rules are consulted in
        # list order and each one is filled before the next is tried, so a repeat
        # is an overflow chain, not a shadowed rule. Only an exact repeat of the
        # same (bucket, store) pair can never fire.
        fired = (b, tuple(chain))
        if fired in seen_buckets:
            # Two spellings, because a rule with no destination has nothing to
            # name: "already routed to None by rule 1" is what a `keep`-only
            # or `stock`-only rule used to render, and `None` is not a
            # container anybody typed.
            if chain:
                err(where, "bucket %r is already routed to %r by rule %d; this exact "
                           "pair can never fire" % (b, shown, seen_buckets[fired] + 1))
            else:
                err(where, "bucket %r is already covered by rule %d; this exact "
                           "pair can never fire" % (b, seen_buckets[fired] + 1))
        else:
            seen_buckets[fired] = i
        if not chain:
            err(where, "no destination container")
        for store in chain:
            if ck and store not in ck:
                err(where, "destination %r is not a container in this save" % store)
            elif store in dk:
                pass    # no cells: see `dead_keys` above
            elif sk and store not in sk:
                warn(where, "destination %r is not sortable right now" % store)
        if b in (cfg.get("never_buckets") or []):
            warn(where, "bucket %r is also in never_buckets; the explicit rule wins, "
                        "which is probably what you meant" % b)

    routed = set(r.get("bucket") for r in (cfg.get("bucket_rules") or []))
    if "*" not in routed:
        never = set(cfg.get("never_buckets") or [])
        homeless = [b["key"] for b in idb.buckets
                    if b["key"] not in routed and b["key"] not in never
                    and b["key"] not in UNROUTABLE]
        if homeless:
            # Two sentences rather than one 34-word one: the first says what
            # happens, the second says why that is worth knowing. The opening
            # fragment is unchanged so the documentation entry keyed on it
            # still matches.
            warn("bucket_rules",
                 "%d bucket(s) have no destination, so anything in them stays put "
                 "wherever it already is. That includes a container meant for "
                 "something else: %s"
                 % (len(homeless), ", ".join(idb.label(b) for b in homeless)))

    seen_items = {}
    for i, rule in enumerate(cfg.get("item_rules") or []):
        where = "item_rules[%d]" % i
        item = (rule.get("item") or "").strip()
        ok, msg, _ = idb.validate_item_id(item)
        if not ok:
            err(where, "%s: %s" % (item or "(blank)", msg))
        chain = stores_of(rule)
        store_now = chain[0] if len(chain) == 1 else (chain or None)
        fired = (item, tuple(chain))
        if fired in seen_items:
            # See the bucket half above: a rule that only pins or keeps has no
            # destination to name, and `None` read as one.
            if chain:
                err(where, "%r is already routed to %r by rule %d; this exact pair can "
                           "never fire" % (item, store_now, seen_items[fired] + 1))
            else:
                err(where, "%r is already covered by rule %d; this exact pair can "
                           "never fire" % (item, seen_items[fired] + 1))
        else:
            seen_items[fired] = i
        for f in _INT_FIELDS:
            v = rule.get(f)
            if v is None:
                continue
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                err(where, "%s must be a non-negative whole number, not %r" % (f, v))
        store = bool(chain)
        for dest in chain:
            if ck and dest not in ck:
                err(where, "destination %r is not a container in this save" % dest)
            elif dest in dk:
                pass    # no cells: see `dead_keys` above
            elif sk and dest not in sk:
                warn(where, "destination %r is not sortable right now" % dest)
        st, sin = rule.get("stock"), rule.get("stock_in")
        if st is not None or sin:
            if st is None or not sin:
                err(where, "stock needs both an amount and a container")
            if sin and ck and sin not in ck:
                err(where, "stock container %r is not in this save" % sin)
            elif sin and sin in dk:
                pass    # no cells: see `dead_keys` above
            elif sin and sk and sin not in sk:
                warn(where, "stock container %r cannot receive items right now" % sin)
            if rule.get("keep") is not None:
                err(where, "a rule cannot have both keep and stock; stock already "
                           "holds that many back in its own container")

        kin = rule.get("keep_in")
        if kin:
            if rule.get("keep") is None:
                err(where, "keep_in names %r but there is no keep to apply there" % kin)
            if ck and kin not in ck:
                err(where, "keep_in %r is not a container in this save" % kin)
            elif kin not in (cfg.get("sources") or []):
                warn(where, "keep_in %r is not one of your sources, so nothing is "
                            "ever drained from it and the floor can never bind" % kin)
        if rule.get("fill") is not None and not store and st is None:
            err(where, "a `fill` ceiling needs a `store` to apply it to")
        # `stock` counts as doing something: it is a destination and a floor at
        # once, and needs no store of its own because the surplus falls through
        # to the category rules.
        if (not store and st is None and rule.get("keep") is None
                and not rule.get("pin")):
            warn(where, "no store, no keep, no stock and no pin: this rule does nothing")

    seen_bucket_keys = set()
    for i, b in enumerate(cfg.get("custom_buckets") or []):
        where = "custom_buckets[%d]" % i
        key = (b.get("key") or "").strip()
        if not key:
            err(where, "a bucket needs a key")
        elif not re.match(r"^[a-z0-9_]+$", key):
            err(where, "key %r must be lower-case letters, digits and underscores; "
                       "it is written into the config and compared exactly" % key)
        elif key in seen_bucket_keys:
            err(where, "duplicate bucket key %r" % key)
        elif key in _generated_buckets():
            err(where, "%r is already a generated bucket; pick another key" % key)
        else:
            seen_bucket_keys.add(key)
        if not (b.get("label") or "").strip():
            warn(where, "no label; the key will be shown instead")

    known_buckets = set(_generated_buckets()) | seen_bucket_keys
    for raw, bucket in (cfg.get("item_buckets") or {}).items():
        where = "item_buckets[%s]" % raw
        ok, msg, _ = idb.validate_item_id(raw)
        if not ok:
            err(where, "%s: %s" % (raw, msg))
        if bucket not in known_buckets:
            err(where, "%r is not a bucket. An override pointing at a bucket that "
                       "does not exist would silently do nothing." % bucket)
        elif bucket in UNROUTABLE:
            err(where, "%r never occupies a container slot, so it cannot be "
                       "routed to one" % bucket)

    # `labels` is read, by `server.save_view`, and written, by the per-card
    # rename box. It used to carry a deprecation warning here saying nothing
    # read it, which was false and told a player their only way of naming an
    # exocraft did nothing. Blessed 2026-09-15; documented in docs/RULES.md.
    #
    # Blessed is not unchecked. Blessing it put a `json` fence in RULES.md
    # inviting a player to hand edit the map, and it is the only
    # container-key-bearing field in the file that had no check at all: a
    # string where the object belongs saved without a word and then took the
    # whole Save section down with a 500 (review 4, finding 4). So the three
    # checks every neighbouring field gets -- the shape, the values, and the
    # keys against this save.
    labels = cfg.get("labels")
    if labels is not None and not isinstance(labels, dict):
        err("labels", "labels has to be an object mapping a container key to a "
                      "name, not %s" % _kind(labels))
    elif labels:
        for key in sorted(labels, key=str):
            name = labels[key]
            where = "labels[%s]" % key
            if not isinstance(name, str):
                err(where, "a container name has to be text, not %s" % _kind(name))
            elif not name.strip():
                err(where, "a container name cannot be blank; delete the entry "
                           "to go back to the name the save carries")
            elif len(name.strip()) > LABEL_MAX:
                err(where, "a container name is at most %d characters and this "
                           "one is %d" % (LABEL_MAX, len(name.strip())))
            # A warning, not an error: one configuration is used against every
            # save in the folder, and a name for a ship slot this save does not
            # have is not a mistake -- it is the other save's ship.
            if ck and key not in ck:
                warn(where, "%r is not a container in this save, so this name is "
                            "shown nowhere" % key)

    for i, item in enumerate(cfg.get("never_move") or []):
        ok, msg, _ = db().validate_item_id(item)
        if not ok:
            err("never_move[%d]" % i, "%s: %s" % (item, msg))

    for i, key in enumerate((cfg.get("options") or {}).get("tidy") or []):
        if ck and key not in ck:
            err("options.tidy[%d]" % i, "%r is not a container in this save" % key)
        elif key in dk:
            pass        # no cells: see `dead_keys` above
        elif sk and key not in sk:
            warn("options.tidy[%d]" % i, "%r is not sortable right now" % key)
    return out


# ----------------------------------------------------------------- migration

def _file_version(cfg):
    """The version a file claims, as an int, defaulting to 1.

    `config_version` has been 1 through six schema additions, so a file with no
    version at all is a version 1 file, not a broken one.
    """
    v = cfg.get("config_version")
    if isinstance(v, bool) or v is None:
        return 1
    try:
        return int(v)
    except (TypeError, ValueError):
        return 1


def _drop_unroutable(cfg):
    """Take `system_meta` out of a config, in place. -> notes.

    Three places name a category: a rule's `bucket`, a per-item override's
    value, and the never-route list. Nothing in `itemdb.UNROUTABLE` is ever in
    a container slot, so a rule for it could never fire, an override into it
    would hide an item from every rule, and a never-route entry is a weaker
    second answer to a question that is now settled -- `system_meta` was in
    the shipped `never_buckets` default and comes out of it here.

    Dropped rather than reported, because there is nothing for a player to
    decide: the destination they picked cannot receive any of it.
    """
    notes = []
    never = cfg.get("never_buckets")
    if isinstance(never, list):
        kept = [b for b in never if b not in UNROUTABLE]
        if len(kept) != len(never):
            cfg["never_buckets"] = kept
            notes.append("dropped %s from the never-route list; nothing in it "
                         "ever occupies a container slot, so no rule can name "
                         "it at all now"
                         % ", ".join(repr(b) for b in never if b in UNROUTABLE))
    rules = cfg.get("bucket_rules")
    if isinstance(rules, list):
        kept = [r for r in rules
                if not (isinstance(r, dict) and r.get("bucket") in UNROUTABLE)]
        if len(kept) != len(rules):
            cfg["bucket_rules"] = kept
            notes.append("dropped %d rule(s) routing a category that never "
                         "occupies a container slot" % (len(rules) - len(kept)))
    overrides = cfg.get("item_buckets")
    if isinstance(overrides, dict):
        kept = dict((k, b) for k, b in overrides.items() if b not in UNROUTABLE)
        if len(kept) != len(overrides):
            cfg["item_buckets"] = kept
            notes.append("dropped %d per-item override(s) pointing at a "
                         "category that never occupies a container slot"
                         % (len(overrides) - len(kept)))
    return notes


def migrate(cfg):
    """-> (a new config at `CONFIG_VERSION`, notes about what changed).

    Pure and idempotent: the argument is never touched, and migrating an
    already-current config returns a copy and says nothing. Version 2 changes
    nothing a person typed. It exists so that a migration function exists, and
    so the three things below can be dropped now rather than argued about at
    every later version:

      * `stores` folds into `store`, which is a container or a list of them.
        One field with one reader (`stores_of`) instead of two fields with
        four.
      * an `item_buckets` override equal to the taxonomy's own answer is
        deleted. 640 of the operator's 641 overrides became the default when
        `corvette_parts` was generated, and an override that agrees with the
        default cannot be told from one that has been forgotten about.
      * a `custom_buckets` entry whose key the taxonomy now generates is
        deleted, because the validator refuses one that shadows a generated
        bucket and a migration must not produce a file its own validator
        rejects.

    `labels` is dropped only when it is empty, because an empty map says
    nothing. A non-empty one is the player's own names for their containers and
    is kept as written.

    One pass runs at **every** version, before the early return: dropping what
    names an unroutable category. That is not a version 2 conversion, it is a
    statement that stopped being true of the program, and a config already at
    the current version would otherwise keep a rule its own validator now
    refuses. It is idempotent -- a second run finds nothing and says nothing --
    which is what lets it sit outside the version gate.
    """
    notes = []
    cfg = copy.deepcopy(cfg or {})
    v = _file_version(cfg)
    notes.extend(_drop_unroutable(cfg))
    if v >= CONFIG_VERSION:
        return cfg, notes

    folded = 0
    for section in ("item_rules", "bucket_rules"):
        for rule in (cfg.get(section) or []):
            if not isinstance(rule, dict) or not rule.get("stores"):
                continue
            chain = stores_of(rule)
            rule.pop("stores", None)
            rule["store"] = chain[0] if len(chain) == 1 else chain
            folded += 1
    if folded:
        notes.append("folded `stores` into `store` on %d rule(s); a chain is "
                     "now one field" % folded)

    idb = db()
    overrides = cfg.get("item_buckets") or {}
    kept = dict((k, b) for k, b in overrides.items()
                if idb.original_bucket_of(k) != b)
    if len(kept) != len(overrides):
        notes.append("dropped %d per-item override(s) that now match the "
                     "shipped category" % (len(overrides) - len(kept)))
    if overrides or "item_buckets" in cfg:
        cfg["item_buckets"] = kept

    generated = _generated_buckets()
    customs = cfg.get("custom_buckets") or []
    keep_customs = [b for b in customs
                    if (b.get("key") or "").strip() not in generated]
    if len(keep_customs) != len(customs):
        gone = [b.get("key") for b in customs if b not in keep_customs]
        notes.append("dropped custom categor%s %s: the shipped taxonomy "
                     "generates %s now"
                     % ("y" if len(gone) == 1 else "ies",
                        ", ".join(repr(g) for g in gone),
                        "it" if len(gone) == 1 else "them"))
    if customs or "custom_buckets" in cfg:
        cfg["custom_buckets"] = keep_customs

    if "labels" in cfg and not cfg["labels"]:
        cfg.pop("labels")
        notes.append("dropped the empty `labels` map; it names no container")

    cfg["config_version"] = CONFIG_VERSION
    notes.append("config_version %d -> %d" % (v, CONFIG_VERSION))
    return cfg, notes


# ------------------------------------------------------------------- storage

#: keys the merge in `load` handles itself. `options` is merged key by key
#: (a scalar option the file omits takes the shipped default, because those
#: are tuning numbers rather than rules); `labels` is the player's own names
#: for containers and is put back only if the file carries it, so an absent map
#: stays absent instead of becoming an empty one.
_MERGE_SPECIAL = ("options", "labels")


def _empty_like(value):
    """What an absent key becomes when the file exists.

    A list becomes `[]` and a dict becomes `{}`; a scalar keeps the shipped
    default, because `name`, `notes` and `updated` describe the file rather
    than the sort. See `load` for why the containers do not.
    """
    if isinstance(value, list):
        return []
    if isinstance(value, dict):
        return {}
    return value


def load(path, notes=None):
    """-> (config, created). Migrates on read; writes nothing.

    `notes` is an optional list the migration's notes are appended to, rather
    than a third return value, because `App` unpacks two and a config file
    that migrated silently is a config file nobody knows migrated.

    A key the file leaves out is **empty**, not the shipped default. The merge
    used to be `default_config()` updated with the file, so a hand-written or
    trimmed config that omitted `item_rules` silently inherited all five
    shipped item rules -- Chromatic Metal to `chest2` among them -- and a file
    that omitted `never_buckets` inherited the two defaults. Both are rules
    nobody wrote, applied to somebody's save, and the operator's own file was
    the evidence that they did not want them. The "Minimal" example in
    `docs/GUIDE.md` is exactly that file.

    Only a file that does not exist at all gets the shipped default, which is
    the one case where there is no statement to respect -- and it is written
    out, so what it says is visible and editable rather than implied.

    Every key filled in this way is reported in `notes`, because a config that
    means less than it appears to is the thing this change exists to stop.
    """
    if not os.path.exists(path):
        cfg = default_config()
        store(path, cfg, backup=False)
        return cfg, True
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        raise ValueError("config %s is not a JSON object" % path)
    v = _file_version(cfg)
    if v > CONFIG_VERSION:
        raise ConfigTooNew(
            "this config is version %d; this build reads up to %d. Update the "
            "sorter or move the file aside" % (v, CONFIG_VERSION),
            path=path, file_version=v)
    cfg, moved = migrate(cfg)
    if notes is not None:
        notes.extend(moved)
    shipped = default_config()
    merged = dict(cfg)
    for key, value in shipped.items():
        if key in cfg or key in _MERGE_SPECIAL:
            continue
        merged[key] = _empty_like(value)
        if isinstance(value, (list, dict)) and notes is not None:
            notes.append("%s absent; treated as empty" % key)
    opts = dict(shipped["options"])
    opts.update(cfg.get("options") or {})
    merged["options"] = opts
    # `default_config()` carries an empty `labels` for the shape; a migrated
    # file that dropped it must not have it put back by the merge.
    if "labels" not in cfg:
        merged.pop("labels", None)
    return merged, False


def _strip_ui(cfg):
    """Remove the display fields `enrich()` adds, and nothing else.

    Scoped to the three known keys on `item_rules` entries. It used to delete
    any `_`-prefixed key at any depth, which silently ate an `item_buckets`
    override whose item id happens to start with an underscore -- data, not
    decoration.
    """
    if not isinstance(cfg, dict):
        return cfg
    cfg = copy.deepcopy(cfg)
    for rule in (cfg.get("item_rules") or []):
        if isinstance(rule, dict):
            for k in UI_KEYS:
                rule.pop(k, None)
    return cfg


def enrich(cfg):
    """Stamp display names onto the rules, for the browser only. Never stored."""
    cfg = copy.deepcopy(cfg)
    idb = db()
    for rule in cfg.get("item_rules") or []:
        row = idb.lookup(rule.get("item") or "")
        rule["_name"] = row["name"] if row else None
        rule["_bucket"] = idb.bucket_of(rule.get("item") or "")
        rule["_bucket_label"] = idb.label(rule["_bucket"])
    return cfg


def _unlink_quietly(path):
    """Remove a scratch file if it is there. A scratch file that will not go
    away is not a reason to fail a write that succeeded."""
    if not path:
        return
    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass


def rotate_backups(path, keep=BAK_KEEP, previous=None):
    """Shift `config.json.bak.1..keep` down one and put the retired revision
    into `.bak.1`. The oldest falls off the end.

    One slot, rewritten on every save, is not a backup: the revision you want
    is usually the one before the one you just made. A single `.bak` left from
    an older build is adopted into the rotation rather than overwritten,
    because it may be the only copy anyone has.

    `previous` is the file holding the content being retired, set aside before
    `store` replaced `path`. Without it the rotation has to copy `path`, which
    is only the retired revision if the rotation runs *before* the replace --
    and running it before the replace is what made a failed write cost a
    revision slot (review one, R14, and write-path review two, Q13). `store`
    always passes it; a direct caller that has not replaced anything yet may
    leave it None and get the old copy-the-file behaviour.
    """
    if not os.path.exists(path) and previous is None:
        return []
    legacy = path + ".bak"
    if os.path.exists(legacy):
        slot = path + ".bak.1"
        if os.path.exists(slot):
            os.unlink(legacy)
        else:
            os.replace(legacy, slot)
    oldest = "%s.bak.%d" % (path, keep)
    if os.path.exists(oldest):
        os.unlink(oldest)
    for n in range(keep - 1, 0, -1):
        src = "%s.bak.%d" % (path, n)
        if os.path.exists(src):
            os.replace(src, "%s.bak.%d" % (path, n + 1))
    if previous is not None:
        # `os.replace` rather than a copy: the file is already a byte-for-byte
        # `copy2` of the revision being retired, timestamps included, so
        # moving it into the slot both preserves its mtime and consumes it.
        os.replace(previous, path + ".bak.1")
    else:
        shutil.copy2(path, path + ".bak.1")
    return [("%s.bak.%d" % (path, n)) for n in range(1, keep + 1)
            if os.path.exists("%s.bak.%d" % (path, n))]


def config_temp_path(path):
    """The temp file `store` writes through. The pid is in the name.

    `safety.temp_path` has carried the pid since the write path grew a lock,
    for the same reason: two sorters started against one state directory is
    the ordinary case, and a shared `config.json.tmp` means whichever process
    replaces last wins a file the other one was halfway through writing.
    """
    return "%s.%d.tmp" % (path, os.getpid())


def config_aside_path(path):
    """Where `store` parks the revision it is about to retire. Pid in the name
    for the same reason `config_temp_path` carries one."""
    return "%s.%d.prev" % (path, os.getpid())


# -------------------------------------------------- keeping a configuration
# "we need a way to reset the config (don't lose my existing config)". Two
# halves, and the second is the one that makes the first safe: starting over
# keeps what was there under a name of its own, and every name kept that way
# can be put back. Nothing in here deletes a file.


def kept_dir(path):
    """The folder the configuration and everything kept beside it lives in."""
    return os.path.dirname(os.path.abspath(path))


def _kept_parts(path):
    """(folder, stem, extension) of the configuration file's own name.

    Derived from the name rather than assumed to be `config.json`: `--config`
    takes any path, and a kept file has to sit beside whatever it was kept
    from and be recognisable as belonging to it.
    """
    stem, ext = os.path.splitext(os.path.basename(path))
    return kept_dir(path), stem, (ext or ".json")


def kept_re(path):
    """The basenames a kept configuration may have, as a compiled pattern.

    Anchored at both ends and built out of the configuration's own name, so
    the only thing it can ever match is a file `keep_aside` wrote beside it.
    This is what `POST /api/config/use` validates its `file` against: a
    basename that matches cannot contain a separator, so the path it names is
    inside `kept_dir` by construction rather than by a check.

    The optional `-<n>` is a second keep inside the same second.
    """
    _d, stem, ext = _kept_parts(path)
    return re.compile(r"^%s\.%s-(\d{8}-\d{6})(?:-(\d+))?%s$"
                      % (re.escape(stem), re.escape(KEPT_INFIX),
                         re.escape(ext)))


def kept_path(path, stamp=None):
    """A free name to keep the current configuration under.

    Never a name that already exists: two resets inside one second would
    otherwise overwrite the first one's copy, which is the one thing this
    whole path is here to prevent.
    """
    d, stem, ext = _kept_parts(path)
    stamp = stamp or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(d, "%s.%s-%s%s" % (stem, KEPT_INFIX, stamp, ext))
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(d, "%s.%s-%s-%d%s"
                            % (stem, KEPT_INFIX, stamp, n, ext))
    return dest


def keep_aside(path):
    """Keep a copy of the configuration beside it. -> the basename, or None.

    A `copy2` rather than a rename, for the reason `store` sets its aside copy
    aside rather than moving it: a write that fails after this has to leave
    the configuration exactly where it was. The copy is byte for byte,
    timestamps included, so the file put back later is the file that was kept.

    None when there is nothing to keep, which is the first run: `load` writes
    the shipped default before anything can reset it, so in practice this
    only happens to a caller that deleted the file by hand.
    """
    if not path or not os.path.exists(path):
        return None
    dest = kept_path(path)
    shutil.copy2(path, dest)
    return os.path.basename(dest)


def read_kept(full):
    """What a kept file says about itself: (name, updated, readable)."""
    try:
        with open(full, encoding="utf-8") as fh:
            body = json.load(fh)
    except (OSError, ValueError):
        return None, None, False
    if not isinstance(body, dict):
        return None, None, False
    name = body.get("name")
    updated = body.get("updated")
    return (name if isinstance(name, str) else None,
            updated if isinstance(updated, str) else None, True)


def list_kept(path):
    """Every configuration kept beside `path`, newest first.

    The `name` and `updated` fields are read out of each file, because the
    file name carries only the moment it was kept and "Ten chests, saved
    yesterday" is what tells two of them apart.

    A file that will not read is listed with `readable: false` rather than
    left out: it is still somebody's configuration, and a list that silently
    drops it is a list that says "you have nothing kept".

    Sorted on the stamp in the name rather than on mtime: the name records
    when the configuration was kept, and a copy tool can rewrite an mtime.
    """
    d = kept_dir(path)
    pat = kept_re(path)
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for name in names:
        m = pat.match(name)
        if not m:
            continue
        full = os.path.join(d, name)
        try:
            size = os.path.getsize(full)
        except OSError:
            size = None
        label, updated, readable = read_kept(full)
        out.append({"file": name, "kept": m.group(1),
                    "seq": int(m.group(2) or 1), "name": label,
                    "updated": updated, "readable": readable, "size": size})
    out.sort(key=lambda r: (r["kept"], r["seq"]), reverse=True)
    return out


def install(path, src):
    """Make `src`'s exact bytes the configuration at `path`. Atomically.

    A byte copy rather than a `store`: the file being put back is the one
    that was kept, and `store` restamps `updated` and reformats, so "the
    configuration I had" would come back as a file that no longer matches the
    copy sitting beside it. It is the caller's job to have read `src` with
    `load` first, so a file this build cannot read never reaches here.

    No rotation either. The `.bak` series is the record of edits made in the
    browser; putting a kept configuration back is not one, and the
    configuration it replaces has just been kept under its own name.
    """
    tmp = config_temp_path(path)
    try:
        with open(src, "rb") as fh:
            data = fh.read()
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        _unlink_quietly(tmp)


def store(path, cfg, backup=True):
    """Write the config, rotating the retired revision into `.bak.1`.

    The order is: temp file, set the current content aside, replace, *then*
    rotate. Every earlier order cost a revision for a write that never
    happened. Rotation used to run first with no `try/finally`, so a failed
    write shifted every slot, made `.bak.1` a duplicate of the file that was
    never replaced and dropped the oldest revision (review one, R14). Moving
    the rotation to just before the `os.replace` narrowed that window to one
    operation and left the damage identical, because the replace is exactly
    the operation that fails when a scanner or another process holds the file
    open (write-path review two, Q13). Measured with five slots full: every
    slot shifted, `.bak.1` duplicated the unreplaced file and `rev1` was
    unlinked.

    So the rotation now runs only after the replace has returned, over the
    content the replace retired -- which is why it needs `previous`: after the
    replace, `path` holds the *new* revision, and rotating that in would put a
    duplicate of the current file in `.bak.1`.

    The window this leaves is a crash between the replace and the rotation,
    which costs a revision slot that has not been used yet. That is the
    harmless direction: the file on disk is the one asked for, and no existing
    revision has moved.
    """
    cfg = _strip_ui(copy.deepcopy(cfg))
    cfg["config_version"] = CONFIG_VERSION
    cfg["updated"] = _now()
    tmp = config_temp_path(path)
    aside = None
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        if backup and os.path.exists(path):
            aside = config_aside_path(path)
            shutil.copy2(path, aside)
        os.replace(tmp, path)
    except BaseException:
        # Nothing has been retired, so nothing may be rotated. Both scratch
        # files go, and the slots are exactly as they were.
        _unlink_quietly(aside)
        raise
    finally:
        _unlink_quietly(tmp)
    if aside is not None:
        try:
            rotate_backups(path, previous=aside)
        except OSError as exc:
            # The configuration the caller asked for is on disk. A rotation
            # that failed after that is a lost revision slot, not a lost
            # write, and saying so beats raising over a file nobody asked for.
            log.warning("saved %s but could not rotate its revisions (%s)",
                        path, exc)
            _unlink_quietly(aside)
    return cfg
