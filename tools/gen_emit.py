"""Write `nms_sorter/data/*.json`: the item table, the taxonomy, the stack
caps, the save-key map, and the two files the tests assert against.

One input path:

  --tables DIR --lang DIR   the game's own decompiled tables, merged with the
                            table already shipped. A union, never a
                            replacement: the reality tables carry ~3,100 ids,
                            our table carries 5,133, and the difference is not
                            error on either side. Where the game has an opinion
                            about a row -- its English name, its category, its
                            stack multiplier -- the game wins, because it is
                            the build that is running.

The base of that merge is `--base`, which defaults to the shipped
`nms_sorter/data/`: a run is read-modify-write over the files it replaces.
Three of them are not in the game's tables at all and are carried forward
unchanged -- `stacks.json`, `savekeys.json` and the bucket list in
`buckets.json` -- so the only way they change is a hand edit to a generated
file, which is a thing this project does not do. A run against the same build
must therefore reproduce the shipped bytes exactly, and that identity is the
check on a regeneration; git history is what the previous state is recovered
from.

Outputs, all under `nms_sorter/data/`:

    items.json     {id: {name,kind,category,subtitle,multiplier,bucket,procedural}}
    buckets.json   [{key,label,note}] in the order the sorter walks containers
    stacks.json    {difficulty: {group: {product,substance}}}
    savekeys.json  {"forward": {obf: plain}, "reverse": {plain: obf}}
    vehicles.json  [{index,type,title_key,name}] in VehicleOwnership order
    gridnames.json [{vessel,grid,keys,name}] the game's word for each grid
    DATA_VERSION   key=value lines: game build, MBINCompiler, date, generator
    DATA_COUNTS    the counts test_data.py asserts the loaded table against

Usage:
    python tools/gen_emit.py --tables <nms-db>/tables --lang <nms-db>/lang
    python tools/gen_emit.py --tables ... --lang ... --out /tmp/compare
"""
import argparse
import collections
import datetime
import io
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gen_items as G                                            # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DATA = os.path.join(REPO, "nms_sorter", "data")

# The build these tables were read from. Everything that used to be a stale
# string inside a generated header is a value here instead, so the header is
# generated from the data and cannot drift from it.
#
# 25320008 is the installed Steam build (`appmanifest_275850.acf`, patched
# 2026-09-15). The tables were decompiled from 25233815, and this is not a
# guess that nothing moved: every input `items.json` is built from was
# extracted again from the new paks with `tools/hgpak.py` and compared byte
# for byte. `nms_reality_gcproducttable`, `gcsubstancetable`,
# `gctechnologytable`, `gcproceduraltechnologytable`, `legacyitemtable`,
# `legacybasebuildingtable` and `proceduralproducttable` are identical;
# `consumableitemtable` differs in 8 bytes, all of them inside the 0x60-byte
# MBIN header; `basebuildingobjectstable` differs in exactly one byte, a 0x01
# that became 0x00. The localisation the names come from is identical in
# `nms_loc1` and `nms_loc4` -- where every grid-name key lives -- and in
# `nms_loc6`; `nms_loc9` changed, and the Corvette keys this build reads are
# still in it.
#
# What that leaves open, and what a `--tables` run is for: whether that one
# base-building byte moves a row. `docs/POST-PATCH.md` is still the playbook,
# and it has not been run for this build.
GAME_BUILD = "25320008"
GAME_VERSION = "7.01 Cosmos"
MBINCOMPILER = "7.02.0.1"

OUT_FILES = ("items.json", "buckets.json", "stacks.json", "savekeys.json",
             "vehicles.json", "gridnames.json", "DATA_VERSION", "DATA_COUNTS")

# The global whose per-member struct carries `GcVehicleType` in the order the
# game writes it. One file, one property; if a patch renames either, the
# generator says so instead of guessing an order.
VEHICLE_GLOBALS = "gcvehicleglobals.global.MXML"
VEHICLE_ENUM_PROPERTY = "VehicleWeaponMuzzleFlash"
#: `VEHICLE_<TYPE>_TITLE_L` is the game's own long display name for a type.
VEHICLE_TITLE_KEY = "VEHICLE_%s_TITLE_L"

#: Which localisation keys name each of a vessel's inventory grids, in the
#: order `gridnames.json` ships them. `vessel` is the kind of vessel, not a
#: save key (`savemodel.vessel_kind` maps `ship3` to `ship`); `grid` is the
#: role of one of the vessel's sibling inventory nodes (`Inventory`,
#: `Inventory_Cargo`), not a display word.
#:
#: Two keys means composition: the first is the format string `INVENTORY` =
#: "%TYPE% INVENTORY", which the game titles every vessel inventory screen
#: with, and the second supplies the `%TYPE%` word (`SUIT` = "EXOSUIT", `SHIP`
#: = "STARSHIP", `FREIGHTER` = "FREIGHTER", `VEHICLE_TITLE` = "EXOCRAFT").
#: One key is a literal.
#:
#: Two absences, both searched for rather than assumed. The game has **no**
#: word "General": no localisation key resolves to "General" in an inventory
#: sense, which is why a vessel with one live grid is drawn with no
#: sub-heading at all rather than under a word the game does not use. And
#: there is no technology row, because the game ships no inventory-screen
#: title for one -- the nearest strings are `SUIT_TITLE` = "EQUIPMENT" and
#: `TECH` = "Tech", neither of which is a tab on that screen -- and no
#: technology grid is drawn anywhere in this program.
GRID_NAME_ROWS = (
    ("suit", "general", ("INVENTORY", "SUIT")),
    ("suit", "cargo", ("UI_CARGO",)),
    ("ship", "general", ("INVENTORY", "SHIP")),
    ("ship", "cargo", ("UI_CARGO",)),
    ("freighter", "general", ("INVENTORY", "FREIGHTER")),
    ("freighter", "cargo", ("UI_FREIGHTER_CARGO",)),
    ("exocraft", "general", ("INVENTORY", "VEHICLE_TITLE")),
)

#: The placeholder `INVENTORY` carries. Read off the string rather than
#: assumed: if a patch renames it the generator says so instead of writing
#: "%TYPE% Inventory" onto a card.
GRID_NAME_PLACEHOLDER = "%TYPE%"

UNSORTED = "unsorted"

# Ids whose placement was checked by hand against the game's own description,
# rather than inferred from a category. A hand-checked placement wins outright,
# including over one an earlier run of this generator produced: anything less
# and a bad automatic answer is sticky forever.
HAND = {
    "HULK1": "salvage_junk",        # Contaminated Metal, derelict salvage
    "SLIMEPOST1": "salvage_junk",   # Gelatinous Fibres, organic salvage
    "CV_TRACT1": "tech_upgrades",   # Corvette tractor beam, with the CV_* family
}

# ---- bucket 18, generated rather than overridden -------------------------
#
# Decided 2026-09-14. The Corvette hull sets shipped in 7.0 landed in
# `ship_construction` alongside freighter rooms and Starship Fabricator parts,
# and the first real user of this tool moved 640 of them out by hand -- 640 of
# the 848 lines in their configuration. A default that everybody overrides is
# the wrong default, so the override became the rule.
#
# The rule is read off the game's own text, not off the id: an id whose display
# name or subtitle says "Corvette" is a Corvette part. The id prefix `CV_` does
# not work, because the 31 `CV_*` entries are procedural *upgrade modules* that
# slot into a Corvette rather than parts a Corvette is built from -- 16 of them
# carry the game category CORVETTE and none of them says "Corvette" anywhere a
# player can read. Those stay in `tech_upgrades` with the rest of the module
# families, which is where somebody comparing rolls wants them.
CORVETTE_BUCKET = {
    "key": "corvette_parts",
    "label": "Corvette Parts",
    "note": "Corvette hull sections, bridges, engines, wings and interior "
            "fittings: the parts a Corvette is assembled from. Matched on the "
            "game's own display name or subtitle containing \"Corvette\", not "
            "on the CV_ id prefix -- the 31 procedural CV_* entries are "
            "upgrade modules that install into a Corvette, and they stay in "
            "Technology and Upgrade Modules. Split out of Ship and Freighter "
            "Construction because a Corvette build fills a container on its "
            "own and buried the freighter parts next to it.",
}


def corvette_ids(rows, procedural):
    """The ids bucket 18 claims.

    A `procedural-technology`, or a procedural `CV_*` stem, is an upgrade
    module and is excluded even if its text mentions a Corvette: the exclusion
    is stated rather than relied upon to be vacuous, because it is vacuous only
    on this build. A patch that gives `CV_HYP2` the subtitle "Corvette
    Hyperdrive Upgrade" would otherwise move sixteen module families out of the
    container the player compares rolls in.
    """
    out = set()
    for iid, r in rows.items():
        text = "%s %s" % (r.get("name") or "", r.get("subtitle") or "")
        if "corvette" not in text.lower():
            continue
        if r.get("kind") == "procedural-technology":
            continue
        if iid.startswith("CV_") and iid in procedural:
            continue
        out.add(iid)
    return out


# --------------------------------------------------------------------------
# reading the base: the files this run is about to replace
# --------------------------------------------------------------------------

def read_shipped(base_dir, name):
    """One shipped JSON file, read as the base of this run.

    A generated file is the input to the next generation of itself. That is
    what makes a run a merge rather than a replacement, and it is why there is
    no separate copy of the previous table kept beside the generator: the
    previous table is the one in `nms_sorter/data/`, and the one before that is
    in git.
    """
    path = os.path.join(base_dir, name)
    if not os.path.exists(path):
        print("MISSING %s" % path)
        raise SystemExit(2)
    return json.load(io.open(path, encoding="utf-8"))


def read_vehicles_from_tables(tables_dir, lang_dir, shipped):
    """[{index,type,title_key,name}] in `VehicleOwnership` order.

    The order comes from the game's own `GcVehicleType`, read off the
    per-member struct named by `VEHICLE_ENUM_PROPERTY`; the names from its own
    localisation. A type the localisation does not name keeps the name
    `shipped` has for it, so a refresh can add a vehicle without losing the
    six that are already right; a type nothing names at all is written with an
    empty name and `savemodel` falls back to "Exocraft N" for it.

    Corroborated independently by the grid the game allocates each slot: on
    every readable corpus save slots 1, 3 and 4 are 7x5 and slots 0, 2, 5 and
    6 are 10x5, which is exactly the three bike-class types against the four
    larger ones. Nothing here is derived from a slot count, which is the
    inference `server.save_view` has refused since the exocraft cards first
    shipped.
    """
    path = os.path.join(tables_dir, VEHICLE_GLOBALS)
    if not os.path.exists(path):
        raise SystemExit(
            "%s is not in %s.\nExtract it with\n"
            "  python tools/hgpak.py <PCBANKS> gcvehicleglobals <dir>\n"
            "and decompile it with MBINCompiler; see docs/POST-PATCH.md."
            % (VEHICLE_GLOBALS, tables_dir))
    root = ET.parse(path).getroot()
    types = []
    for p in root.iter("Property"):
        if p.get("name") == VEHICLE_ENUM_PROPERTY:
            types = [c.get("name") for c in p.findall("Property") if c.get("name")]
            break
    if not types:
        raise SystemExit("%s has no <Property name=\"%s\"> with members; the "
                         "vehicle order cannot be read and will not be guessed"
                         % (VEHICLE_GLOBALS, VEHICLE_ENUM_PROPERTY))
    loc = G.load_localisation(lang_dir)
    by_type = dict((r["type"], r["name"]) for r in shipped)
    out = []
    for i, vtype in enumerate(types):
        key = VEHICLE_TITLE_KEY % vtype.upper()
        name = G.title(loc[key]) if loc.get(key) else by_type.get(vtype, "")
        out.append({"index": i, "type": vtype, "title_key": key, "name": name})
    print("vehicles: %d types, %d named (%s)"
          % (len(out), sum(1 for r in out if r["name"]),
             ", ".join(r["name"] or r["type"] for r in out)))
    return out


def read_gridnames_from_tables(lang_dir, shipped):
    """[{vessel,grid,keys,name}] composed out of the game's own localisation.

    A key the localisation does not carry keeps the name `shipped` has for that
    row, so a patch that renames one string cannot blank a heading; the row is
    printed either way, so a refresh says which came from where.
    """
    loc = G.load_localisation(lang_dir)
    by_row = dict(((r["vessel"], r["grid"]), r["name"]) for r in shipped)
    out = []
    for vessel, grid, keys in GRID_NAME_ROWS:
        name, source = by_row.get((vessel, grid), ""), "shipped"
        raw = loc.get(keys[0])
        if raw and len(keys) == 1:
            name, source = G.title(raw), keys[0]
        elif raw and len(keys) == 2 and loc.get(keys[1]):
            if GRID_NAME_PLACEHOLDER not in raw:
                raise SystemExit(
                    "%s is %r and no longer carries %s; the grid titles "
                    "cannot be composed and will not be guessed"
                    % (keys[0], raw, GRID_NAME_PLACEHOLDER))
            name = G.title(raw.replace(GRID_NAME_PLACEHOLDER, loc[keys[1]]))
            source = "+".join(keys)
        out.append({"vessel": vessel, "grid": grid, "keys": list(keys),
                    "name": name})
        print("gridnames: %-9s %-7s %-22s from %s"
              % (vessel, grid, name, source))
    return out


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# the merge
# --------------------------------------------------------------------------

def merge_from_tables(tables_dir, lang_dir, base_dir):
    """-> (rows, by_id, procedural, added). Union with the shipped table."""
    rows, _assigned, old, old_bucket = G.collect(tables_dir, lang_dir, base_dir)
    game = dict((r["id"], r) for r in rows)

    merged, updates = {}, collections.Counter()
    for iid, o in old.items():
        row = {"name": o["name"], "kind": o["kind"], "category": o["category"],
               "subtitle": o["subtitle"],
               "multiplier": _int_or_none(o["multiplier"])}
        g = game.get(iid)
        if g:
            for field in ("name", "subtitle", "category"):
                if g[field] and g[field] != row[field]:
                    row[field] = g[field]
                    updates[field] += 1
            gm = _int_or_none(g["multiplier"])
            if gm and gm != row["multiplier"]:
                row["multiplier"] = gm
                updates["multiplier"] += 1
        merged[iid] = row

    added = []
    for iid, g in sorted(game.items()):
        if iid in merged:
            continue
        merged[iid] = {"name": g["name"] or iid, "kind": g["kind"],
                       "category": g["category"], "subtitle": g["subtitle"],
                       "multiplier": _int_or_none(g["multiplier"])}
        added.append(iid)

    print("\nMERGE")
    print("  existing rows kept : %d" % len(old))
    print("  new ids added      : %d" % len(added))
    print("  fields refreshed   : %s" % dict(updates))
    print("  total              : %d" % len(merged))

    # ---- buckets: keep every existing assignment, place the new ids ----
    # Learn the vote map from the MERGED rows, not the old ones. Over a thousand
    # category values were just refreshed from the game, so the old vocabulary
    # no longer describes the table; keying on it left 175 of 199 new ids
    # unplaced.
    #
    # `corvette_parts` casts no vote. It is not a precedent anybody set: it is
    # applied below by reading the game's own display text, and the base this
    # run merges over already carries the result of the last run applying it.
    # Letting it vote would place a new id there on a category majority, which
    # the rule would then not claim, so the id would sit in a Corvette
    # container without saying "Corvette" anywhere a player can read.
    votes = collections.defaultdict(collections.Counter)
    for iid, r in merged.items():
        b = old_bucket.get(iid)
        if b and b != UNSORTED and b != CORVETTE_BUCKET["key"]:
            votes[(r["kind"], r["category"])][b] += 1
            votes[(None, r["category"])][b] += 1     # fallback: category alone

    def vote(counter):
        """A majority only counts when the category is actually homogeneous.

        `substance/SPECIAL` holds 41 reputation pseudo-items alongside 12 raw
        resources and 6 bits of salvage. A plain most_common() there sends real
        salvage into system_meta, which the sorter never touches -- a confident
        wrong answer. Below the threshold, say nothing.
        """
        if not counter:
            return None
        (best, n), total = counter.most_common(1)[0], sum(counter.values())
        return best if total >= 3 and n / total >= 0.7 else None

    by_id = dict(old_bucket)
    placed = collections.Counter()
    for iid in added:
        r = merged[iid]
        b = (HAND.get(iid)
             or vote(votes.get((r["kind"], r["category"])))
             or vote(votes.get((None, r["category"])))
             or UNSORTED)
        by_id[iid] = b
        placed[b] += 1
    for iid, b in HAND.items():
        if iid in by_id:
            by_id[iid] = b
    for iid in merged:
        by_id.setdefault(iid, UNSORTED)
    print("  new ids by bucket  : %s" % dict(placed))

    _old, _old_bucket, proc = G.read_shipped_base(base_dir)
    procedural = set(proc)
    for iid, r in merged.items():
        if r["kind"].startswith("procedural-"):
            procedural.add(iid)
    return merged, by_id, procedural, added


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def data_version_text(build=GAME_BUILD, mbin=MBINCOMPILER, date=None,
                      generator="gen_emit.py"):
    """Generated from values, not patched into stale prose.

    Every count and every build number in the old generated Lua header was
    wrong, because the header was prose and the generator string-replaced
    fragments of it that no longer existed -- a silent no-op. Nothing here is a
    replacement.
    """
    date = date or datetime.date.today().isoformat()
    return ("game_build=%s\nmbincompiler=%s\ngenerated=%s\ngenerator=%s\n"
            % (build, mbin, date, generator))


def _dump(path, obj):
    """Deterministic JSON: sorted keys, LF endings, one indent level.

    Byte-stable output is what makes a regeneration reviewable as a diff, which
    is the whole reason the data ships as JSON and not as a pickle.
    """
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, sort_keys=True,
                  indent=1, separators=(",", ":"))
        fh.write("\n")


def counts_of(items, bucket_defs, vehicles=None, gridnames=None):
    by_bucket = collections.Counter(r["bucket"] for r in items.values())
    out = {
        "buckets": len(bucket_defs),
        "ids": len(items),
        "procedural": sum(1 for r in items.values() if r["procedural"]),
        "unsorted": by_bucket.get(UNSORTED, 0),
        "by_bucket": dict(sorted(by_bucket.items())),
    }
    # Only when there is a list to count, so an out-of-date caller sees the
    # same dict it always saw rather than a key whose value is a lie.
    if vehicles is not None:
        out["vehicles"] = len(vehicles)
    if gridnames is not None:
        out["gridnames"] = len(gridnames)
    return out


def write_all(out_dir, items, bucket_defs, stacks, savekeys, vehicles,
              gridnames, added, build=GAME_BUILD, date=None):
    """Overwrite the eight outputs in place.

    Nothing is copied aside first. The files this replaces are committed, so
    `git diff` is the review and `git checkout` is the undo; a `.pre-<build>`
    copy on disk was a second, unversioned answer to the same question, and
    the one time it mattered a re-run had already overwritten it with its own
    output.
    """
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    _dump(os.path.join(out_dir, "items.json"), items)
    _dump(os.path.join(out_dir, "buckets.json"), bucket_defs)
    _dump(os.path.join(out_dir, "stacks.json"), stacks)
    _dump(os.path.join(out_dir, "savekeys.json"), savekeys)
    _dump(os.path.join(out_dir, "vehicles.json"), vehicles)
    _dump(os.path.join(out_dir, "gridnames.json"), gridnames)

    counts = counts_of(items, bucket_defs, vehicles, gridnames)
    _dump(os.path.join(out_dir, "DATA_COUNTS"), counts)
    io.open(os.path.join(out_dir, "DATA_VERSION"), "w",
            encoding="utf-8", newline="\n").write(
        data_version_text(build=build, date=date))

    for name in OUT_FILES:
        print("wrote %-14s %8d bytes"
              % (name, os.path.getsize(os.path.join(out_dir, name))))
    print("  buckets=%d ids=%d procedural=%d unsorted=%d vehicles=%d gridnames=%d"
          % (counts["buckets"], counts["ids"], counts["procedural"],
             counts["unsorted"], counts["vehicles"], counts["gridnames"]))

    # One file per build listing what this build introduced, so a post-patch
    # review has something to read instead of a 5,000-row diff.
    #
    # Beside the other outputs, unless the outputs are the shipped ones. The
    # one place it may not go is `nms_sorter/data/`, which holds no `.txt` by
    # design: the PyInstaller spec ships that whole directory, so a report
    # written into it lands inside the executable, and
    # `test_no_generator_inputs_are_shipped` is the check. The shipped run
    # therefore writes it to `tools/`, which is where docs/POST-PATCH.md step
    # 4 tells a maintainer to read it. It is gitignored either way: it is a
    # record of one run, not an artefact of the release.
    added_dir = (HERE if os.path.abspath(out_dir) == os.path.abspath(DATA)
                 else out_dir)
    added_path = os.path.join(added_dir, "added-in-%s.txt" % build)
    io.open(added_path, "w", encoding="utf-8", newline="\n").write(
        "# ids new in game build %s (%s), written by gen_emit.py --tables\n"
        "# id\tkind\tbucket\tname\n" % (build, GAME_VERSION)
        + "".join("%s\t%s\t%s\t%s\n"
                  % (i, items[i]["kind"], items[i]["bucket"], items[i]["name"])
                  for i in added))
    print("wrote %s (%d ids)"
          % (os.path.relpath(added_path, REPO).replace("\\", "/"), len(added)))
    return counts


# --------------------------------------------------------------------------

#: the game's own runtime placeholder. Its localisation strings carry it where
#: the game substitutes something at display time: `%NAME% Exhibit` takes the
#: species of the fossil in the box, `%NAME%'s Genetic Material` takes the
#: creature's name, `B-Class %NAME% Upgrade` takes the technology's. All three
#: are generated per item and are in nobody's table. Shipped as-is it put
#: `%NAME% Exhibit` and `%NAME% Implant` on the Items table as item names,
#: where they read as a broken program.
PLACEHOLDER = "%NAME%"

#: the placeholder, and a possessive that belongs to it rather than to the
#: words being kept: `%NAME%'s Genetic Material` is "Genetic Material", not
#: "'s Genetic Material".
_PLACEHOLDER_RE = re.compile(re.escape(PLACEHOLDER) + r"(?:'s|s')?")


def strip_placeholder(s):
    """`B-Class %NAME% Upgrade` -> `B-Class Upgrade`.

    Dropped rather than filled in. The obvious fill is the row's own name,
    and it is wrong twice over: `%NAME%'s Genetic Material` on a Companion Egg
    is the *creature's* name, not the egg's, and `CV_SROC3` is already named
    "A-Class Rocket Launcher Upgrade", which would have made its subtitle
    "A-Class A-Class Rocket Launcher Upgrade Upgrade". What the game wrote
    around the placeholder is true; what it puts in the hole is not ours to
    guess.
    """
    out = " ".join(_PLACEHOLDER_RE.sub(" ", s or "").split())
    return out.strip(" -,:;")


def build_items(rows, by_id, procedural):
    """The one shape `itemdb.py` reads: every field for an id in one place."""
    out = {}
    for iid in sorted(rows):
        r = rows[iid]
        # An item whose name is nothing *but* the placeholder falls back to
        # its subtitle, and then to its id, which is the same last resort the
        # merge uses for an id the game names not at all.
        name = strip_placeholder(r["name"])
        subtitle = strip_placeholder(r["subtitle"])
        if not name:
            name = subtitle or iid
        out[iid] = {
            "name": name, "kind": r["kind"], "category": r["category"],
            "subtitle": subtitle, "multiplier": r["multiplier"],
            "bucket": by_id.get(iid, UNSORTED),
            "procedural": iid in procedural,
        }
    return out


def generate(tables, lang, base_dir=DATA, out_dir=DATA, build=GAME_BUILD,
             date=None, write=True):
    """Produce every output. `write=False` returns them without touching disk."""
    rows, by_id, procedural, added = merge_from_tables(tables, lang, base_dir)
    bucket_defs = read_shipped(base_dir, "buckets.json")
    vehicles = read_vehicles_from_tables(
        tables, lang, read_shipped(base_dir, "vehicles.json"))
    gridnames = read_gridnames_from_tables(
        lang, read_shipped(base_dir, "gridnames.json"))

    # Bucket 18. Appended rather than inserted, so every existing bucket keeps
    # the position the sorter walks it in.
    if CORVETTE_BUCKET["key"] not in [b["key"] for b in bucket_defs]:
        bucket_defs = list(bucket_defs) + [dict(CORVETTE_BUCKET)]
    cv = corvette_ids(rows, procedural)
    moved = collections.Counter(by_id.get(i, UNSORTED) for i in cv)
    for iid in cv:
        by_id[iid] = CORVETTE_BUCKET["key"]
    print("corvette_parts: %d ids, taken from %s"
          % (len(cv), dict(sorted(moved.items()))))

    items = build_items(rows, by_id, procedural)
    # Carried forward, not derived: the game's tables say nothing about how
    # many of a thing fits in a cell of a chest, and nothing about the
    # obfuscated save keys. Both were read out of the save format and out of
    # the game's own inventory limits by hand, once.
    stacks = read_shipped(base_dir, "stacks.json")
    savekeys = read_shipped(base_dir, "savekeys.json")
    if write:
        write_all(out_dir, items, bucket_defs, stacks, savekeys, vehicles,
                  gridnames, added, build=build, date=date)
    return items, bucket_defs, stacks, savekeys, vehicles, gridnames, added


def _cli(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="see docs/POST-PATCH.md for the whole pipeline")
    ap.add_argument("--tables", required=True, metavar="DIR",
                    help="directory of decompiled reality tables (*.MXML)")
    ap.add_argument("--lang", required=True, metavar="DIR",
                    help="directory of decompiled English localisation (*.MXML)")
    ap.add_argument("--base", default=DATA, metavar="DIR",
                    help="the shipped table to merge with "
                         "(default: nms_sorter/data)")
    ap.add_argument("--out", default=DATA, metavar="DIR",
                    help="where the JSON goes (default: nms_sorter/data)")
    ap.add_argument("--build", default=GAME_BUILD, help="game build number")
    ap.add_argument("--date", default=None,
                    help="the generated= date (default: today)")
    a = ap.parse_args(argv)
    generate(tables=a.tables, lang=a.lang, base_dir=a.base, out_dir=a.out,
             build=a.build, date=a.date)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
