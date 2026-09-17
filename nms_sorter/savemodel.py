"""The save, read as containers.

Navigation is done on the *obfuscated* document, never on a deobfuscated copy.
`data/savekeys.json` maps `vLc -> BaseContext`; this module keeps the document
exactly as the codec produced it and resolves plain names to whichever key is
actually present. Editing the real keys in place is what makes the byte-exact
round trip cheap: no remap in, no remap out, no chance of an unmapped key
colliding with a deobfuscated one.

Container discrimination follows `save-edit/containers.md`:

    a real, player-visible container has len(ValidSlotIndices) > 0
    Width/Height lie -- sold ship slots read 1x1, machinery reads 16x1

and the sort targets are the rows that document marks `yes`.

Storage containers are *discovered* rather than listed: any key matching
`CHEST_RE` below -- Chest, a number, Inventory -- is one. A save with twelve of
them is a save with twelve of them, and the two the model used not to know
about would have been refused at write-path step 7 as containers it could not
name a path for.
"""
import hashlib
import math
import os
import re

from . import text
from .codec import (load_keymap, load as codec_load,
                    read_payload_bytes, loads)
from .itemdb import db, grid_names, vehicles as vehicle_table

# ---------------------------------------------------------------------------
# document navigation
# ---------------------------------------------------------------------------


#: `load_keymap()` reads and parses a file. It was doing so once per
#: `SaveFile`, and the page builds one per request. The map is derived from
#: package data that cannot change while the process runs, so it is read once.
#: A test that needs another map sets `savemodel._KEYMAP` directly.
_KEYMAP = None


def keymap():
    global _KEYMAP
    if _KEYMAP is None:
        _KEYMAP = load_keymap()
    return _KEYMAP


class Doc(object):
    """A decoded save plus the key map, with plain-name access."""

    def __init__(self, doc):
        self.doc = doc
        self.fwd, self.rev = keymap()

    def key(self, node, name):
        """The key actually present in `node` for the plain name `name`."""
        o = self.rev.get(name)
        if o is not None and o in node:
            return o
        if name in node:
            return name
        return None

    def get(self, node, name, default=None):
        if not isinstance(node, dict):
            return default
        k = self.key(node, name)
        return default if k is None else node[k]

    def set(self, node, name, value):
        k = self.key(node, name)
        if k is None:
            raise KeyError("no key %r in this object" % name)
        node[k] = value

    def at(self, path, root=None):
        """path is a list of plain names and integer array indices."""
        node = self.doc if root is None else root
        for step in path:
            if node is None:
                return None
            if isinstance(step, int):
                if not isinstance(node, list) or step >= len(node):
                    return None
                node = node[step]
            else:
                node = self.get(node, step)
        return node

    @property
    def player(self):
        return self.at(["BaseContext", "PlayerStateData"])


# ---------------------------------------------------------------------------
# container registry
# ---------------------------------------------------------------------------

PERSONAL, STORAGE, BASE, SHIPS, EXOCRAFT, FLEET, TECH = (
    "Personal", "Storage", "Base", "Ships", "Exocraft", "Fleet", "Technology")

#: The section the Stellar Extractor Cores sit in, after Fleet. They were in
#: Fleet, which is the freighter and the Corvette, and a core is neither: it
#: is a mining buffer in a freighter base room, read-only, refilled by the
#: game itself. Fourteen of them under "Fleet" is what prompted "these aren't
#: ships".
#:
#: The game has no plural word for the group. Its own strings for the thing
#: are `UI_HOOVER_TECH_SUB` = "Stellar Extractor Core", which is what the
#: cards are named, and `BLD_FRE_ROOM_EXTR_NAME_L` = "Stellar Extractor Room".
#: This is that noun in the plural and nothing else.
EXTRACTORS = "Stellar Extractors"

# key, path under PlayerStateData, display label, section, sortable
STATIC_CONTAINERS = [
    ("suit",          ["Inventory"],                    "Exosuit",                 PERSONAL, True),
    ("suit_cargo",    ["Inventory_Cargo"],              "Exosuit Cargo",           PERSONAL, True),
    ("suit_tech",     ["Inventory_TechOnly"],           "Exosuit Technology",      TECH,     False),
    ("multitool",     ["WeaponInventory"],              "Multi-Tool",              TECH,     False),
    ("freighter",     ["FreighterInventory"],           "Freighter",               FLEET,    True),
    ("freighter_cargo", ["FreighterInventory_Cargo"],   "Freighter Cargo",         FLEET,    True),
    ("freighter_tech", ["FreighterInventory_TechOnly"], "Freighter Technology",    TECH,     False),
    ("corvette",      ["CorvetteStorageInventory"],     "Corvette Storage",        FLEET,    True),
    ("cooking",       ["CookingIngredientsInventory"],  "Nutrient Processor",      BASE,     True),
    ("rocketlocker",  ["RocketLockerInventory"],        "Rocket Locker",           BASE,     True),
    ("fishplatform",  ["FishPlatformInventory"],        "Fishing Platform",        BASE,     True),
    ("chestmagic",    ["ChestMagicInventory"],          "Base Capsule 1",          BASE,     True),
    ("chestmagic2",   ["ChestMagic2Inventory"],         "Base Capsule 2",          BASE,     True),
    ("fishbaitbox",   ["FishBaitBoxInventory"],         "Bait Box",                BASE,     False),
    ("foodunit",      ["FoodUnitInventory"],            "Food Unit",               BASE,     False),
]
# Storage containers are not in that list: see `discover_chests`.
CHEST_RE = re.compile(r"^Chest(\d+)Inventory$")


def discover_chests(d, ps):
    """-> [(key, plain key, label)] for every storage container in this save.

    Sorted numerically, so `chest2` precedes `chest10` and the page reads in
    the order the game numbers them. Each key is translated through the key map
    first and taken as written if the map does not know it: `Chest11Inventory`
    and `Chest12Inventory` have no entry in `savekeys.json` and the game
    therefore writes them in the clear.
    """
    found = []
    for raw in (ps or {}):
        m = CHEST_RE.match(d.fwd.get(raw, raw))
        if m:
            found.append((int(m.group(1)), d.fwd.get(raw, raw)))
    found.sort()
    return [("chest%d" % n, plain, "Storage Container %d" % n)
            for n, plain in found]


# Single-cell containers and technology grids are listed so the page can show
# them, but they are never sort targets and never sort sources.
NEVER_SORT = {"suit_tech", "multitool", "freighter_tech", "fishbaitbox", "foodunit"}

#: The three freighter grids, and the word that follows the freighter's name on
#: each, so `PlayerFreighterName` can replace the literal "Freighter" without
#: any of the three labels being assembled by string surgery.
FREIGHTER_GRIDS = {"freighter": "", "freighter_cargo": " Cargo",
                   "freighter_tech": " Technology"}

#: Which of a vessel's three sibling inventory nodes a container is. A role,
#: never a display word: the word on screen comes from the game's own
#: localisation through `grid_title`, and "General" was never one of them --
#: the game has no such tab.
GENERAL, CARGO, TECHNOLOGY = "general", "cargo", "technology"

#: A vessel key -> the kind of vessel it is, which is what `gridnames.json` is
#: keyed by. `ship3` and `ship11` are both a `ship`.
VESSEL_KINDS = (("suit", "suit"), ("freighter", "freighter"),
                ("ship", "ship"), ("vehicle", "exocraft"))

#: The last resort if `data/gridnames.json` cannot be read: a **fallback**,
#: not the game's word. `grid_title` prefers the generated table on every
#: call, and `tests/test_data.py` is what keeps the table present.
FALLBACK_GRID_TITLES = {
    ("suit", GENERAL): "Inventory",
    ("ship", GENERAL): "Cargo",
    ("freighter", GENERAL): "Cargo",
    ("exocraft", GENERAL): "Cargo",
    ("suit", CARGO): "Cargo",
    ("ship", CARGO): "Cargo",
    ("freighter", CARGO): "Cargo",
}


def is_dead_container(c):
    """Can nothing ever be in this container?

    Zero valid cells, and not one of the three kinds that have their own
    answer already:

      * an extractor core, whose `ValidSlotIndices` is empty by design and
        whose capacity is implied by its slots;
      * a technology grid, which a rule must still be refused for naming --
        that refusal is the only reason the model keeps them;
      * a bait box or a food unit, single cells that hold the machine's own
        consumable and are refused the same way.

    Everything left is a grid the game merged away: `Inventory_Cargo` on
    every starship and on the exosuit, on all seven readable corpus saves.
    A rule naming one is valid and silently does nothing, because the key was
    right before Waypoint (4.0) and nobody made a mistake.
    """
    return bool(not c.valid
                and not getattr(c, "drain_only", False)
                and not c.is_tech
                and c.key not in NEVER_SORT)


def vessel_kind(vessel):
    """-> "suit" / "ship" / "freighter" / "exocraft", or "" for no vessel."""
    v = vessel or ""
    for prefix, kind in VESSEL_KINDS:
        if v == prefix or v.startswith(prefix):
            return kind
    return ""


def grid_title(vessel, role):
    """The game's own word for one grid of one vessel, or "" if it has none.

    "" for a technology grid on purpose: the game ships no inventory-screen
    title for one and no technology grid is drawn here, so there is nothing to
    invent. The heading is only ever used when a vessel has more than one
    grid the player can put something in.
    """
    kind = vessel_kind(vessel)
    if not kind or role not in (GENERAL, CARGO):
        return ""
    return (grid_names().get((kind, role))
            or FALLBACK_GRID_TITLES.get((kind, role), ""))

#: The stack-size group the game gives a machine's own buffer.
MAINTENANCE_GROUP = "MaintenanceObject"


def is_maintenance_slot(drain_only, group, inv_type):
    """Is this slot the machine itself rather than something it holds?

    A Stellar Extractor Core's buffer carries `^MAINT_HOOVER` -- item id
    "Extractor Unit" -- in cell (0, 0), typed `Technology`, alongside the five
    substances it mines. It is the extractor, in its own inventory: the same
    shape as the bait in a Bait Box and the cooker in a Food Unit.

    Three conditions, and all three are facts about this container rather than
    about the id, because an id list would have to be edited every time the
    game adds a machine. Measured across the seven readable corpus saves,
    every container plus every live core: `^MAINT_HOOVER` in the fourteen cores
    is the only `MAINT_*` id anywhere, and the only `Technology`-typed slot in
    a grid that is not a technology grid.

    The `^MAINT_HOOVER` test in `extractors()` is a different thing and is
    untouched: that is the *identification* of a core, and it stays raw.

    Tighter than the name suggests, and worth knowing before reusing it: the
    first conjunct is `drain_only`, which is set in exactly one place
    (`extractor_containers`). `fishbaitbox` carries `StackSizeGroup ==
    MaintenanceObject` on all seven corpus saves and is **not** matched here,
    because its `drain_only` is False. Nothing leaks -- `fishbaitbox` and
    `foodunit` are in `NEVER_SORT` and therefore in `server.HIDDEN_KEYS`, so
    the whole container is off the page and the planner refuses it as a
    source -- but this predicate is "an extractor core's Technology slot", not
    "any drain-only container's Technology slot", and it will not generalise
    to a bait box if one is ever un-hidden.
    """
    return (bool(drain_only) and group == MAINTENANCE_GROUP
            and inv_type == "Technology")

#: Which static containers are grids of one vessel, and which grid each is.
#: The exosuit, a starship and the freighter each have three: the save keeps
#: them as sibling nodes (`Inventory`, `Inventory_Cargo`, `Inventory_TechOnly`)
#: and the game presents them as tabs. Ships and exocraft are derived in
#: `containers()` from the same three suffixes.
#:
#: The multi-tool is not a grid of the exosuit: `WeaponInventory` is a separate
#: object with its own name in game, and it is one of several.
VESSEL_GRIDS = {
    "suit": ("suit", GENERAL),
    "suit_cargo": ("suit", CARGO),
    "suit_tech": ("suit", TECHNOLOGY),
    "freighter": ("freighter", GENERAL),
    "freighter_cargo": ("freighter", CARGO),
    "freighter_tech": ("freighter", TECHNOLOGY),
}

#: Names the *game* writes into a vessel's own `Name` field. They are markers
#: for its bookkeeping, not anything a player typed, and they are filtered for
#: ships and exocraft alike.
#:
#: A list, never a heuristic. `Sarco` (the corpus's save5 and save6) is a real
#: player name and is also a single ASCII token in the same shape as these, so
#: any "looks internal" rule eats it. A marker this set does not know reaches
#: the page as a vessel name, which is the direction to be wrong in: it is
#: visible, and it is one line to fix.
VESSEL_NAME_MARKERS = frozenset(("StarterShip", "MainShip"))

#: `Resource.Filename`'s leaf -> the class the game calls that hull.
#:
#: The leaves are the game's own scene names: the directories under
#: `MODELS/COMMON/SPACECRAFT/` that hold a hull a player can own. The English
#: class words are hand written, because the game has no table pairing a hull
#: with a class name -- the ship UI draws the class from the hull itself. So
#: this is a translation of nine file names and nothing is inferred from the
#: save; a leaf this table does not know yields no class rather than a guess,
#: and the label falls back to "Starship N".
#:
#: Leaves measured across the seven readable corpus saves: FIGHTER_PROC,
#: SHUTTLE_PROC, SENTINELSHIP_PROC, BIGGS and empty (a hull the save does not
#: name).
#:
#: `BIGGS` is the Corvette, and that is read off the game's own localisation
#: rather than guessed: the `BIGGS_*` and `UI_BIGGS_*` keys are the Corvette
#: build system, including `UI_BIGGS_EDIT_CONFIRM_TITLE` = "MODIFY DOCKED
#: CORVETTE", `UI_BIGGS_AUTOPILOT_TITLE` = "Corvette Autopilot" and
#: `UI_BIGGS_DIG_OBJ_ALT` = "Excavate Corvette Modules", and no other hull
#: name claims any of them. The owner confirmed they own a Corvette and that
#: this is the hull in the slot the game calls their primary ship. It was
#: previously commented here as "the Anomaly's own scene", which was wrong.
SHIP_CLASSES = {
    "FIGHTER_PROC": "Fighter",
    "DROPSHIP_PROC": "Hauler",
    "SCIENTIFIC_PROC": "Explorer",
    "SHUTTLE_PROC": "Shuttle",
    "S-CLASS_PROC": "Exotic",
    "BIOSHIP_PROC": "Living",
    "SENTINELSHIP_PROC": "Sentinel Interceptor",
    "SAILSHIP_PROC": "Solar",
    "CORVETTE": "Corvette",
    "BIGGS": "Corvette",
}

#: The hull leaf whose label comes from `CorvetteEditShipName` rather than
#: from the ship's own `Name`. A Corvette is named in the workshop, and the
#: save keeps that name in one place for both the hull and its storage.
CORVETTE_HULL = "BIGGS"


#: the substance every core card leads with, and the one the summary totals
CORE_LEAD = "STELLAR2"


def _substance_order(row):
    """Sort key for a core's substances: Chromatic Metal, then by name.

    The name rather than the id, because the name is what is on the card. An
    id the item table does not know sorts under its own spelling, after the
    named ones, rather than jumping about between saves.
    """
    stem = text.stem(row.get("id") or "")
    return (0 if stem == CORE_LEAD else 1,
            0 if row.get("name") else 1,
            (row.get("name") or stem or "").lower())


def _index_of(v):
    """An array index the save carries, or None. `True` is not index 1."""
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v if v >= 0 else None


def vessel_name(raw):
    """A vessel's own `Name`, laundered for display, or "" for a marker.

    "" rather than None so every caller can test it with `or`, and so a name
    made entirely of whitespace is the same answer as no name at all.
    """
    n = (raw or "").strip()
    if not n or n in VESSEL_NAME_MARKERS:
        return ""
    return text.safe(n)


def ship_class(filename):
    """The class of hull `Resource.Filename` names, or "" when we cannot say."""
    return SHIP_CLASSES.get(ship_hull(filename), "")


def ship_hull(filename):
    """The leaf of `Resource.Filename`, upper case and without its suffix."""
    leaf = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1].upper()
    for suffix in (".SCENE.MBIN", ".SCENE", ".MBIN"):
        if leaf.endswith(suffix):
            return leaf[:-len(suffix)]
    return leaf


def ship_label(index, name, filename, corvette_name=""):
    """What a starship's grids are headed by.

    Its own name if the player set one; else its class; else the generic word.
    The slot number stays on the last two, so `ship3` and
    "Sentinel Interceptor 4" are visibly the same vessel -- an unnamed ship's
    in-game name is generated from `Resource.Seed` at display time and is not
    in the save at all, so the number is the only identity there is.

    A Corvette is the exception, and `corvette_name` is why: its name is not
    in its `ShipOwnership` row at all, it is in `CorvetteEditShipName`, which
    is the same field the Corvette's storage container is already labelled
    from. One field, one name, both places.
    """
    if ship_hull(filename) == CORVETTE_HULL and corvette_name:
        return corvette_name
    return (vessel_name(name)
            or "%s %d" % (ship_class(filename) or "Starship", index + 1))


def vehicle_label(index, name):
    """The same for an exocraft, with `data/vehicles.json` doing the work the
    class table does for a ship.

    That file is the game's own `GcVehicleType` order and the game's own
    English names, so slot 2 is the Colossus on the game's authority. Slot
    order is the whole of it: the save carries no name, no `Resource.Filename`
    and no usable seed for an exocraft.
    """
    own = vessel_name(name)
    if own:
        return own
    rows = vehicle_table()
    if 0 <= index < len(rows):
        named = (rows[index].get("name") or "").strip()
        if named:
            return named
    return "Exocraft %d" % (index + 1)


class Slot(object):
    __slots__ = ("node", "d")

    def __init__(self, d, node):
        self.d = d
        self.node = node

    @property
    def id(self):
        return self.d.get(self.node, "Id", "")

    @property
    def amount(self):
        return self.d.get(self.node, "Amount", 0)

    @amount.setter
    def amount(self, v):
        self.d.set(self.node, "Amount", int(v))

    @property
    def max_amount(self):
        return self.d.get(self.node, "MaxAmount", 0)

    @property
    def damage(self):
        return float(self.d.get(self.node, "DamageFactor", 0.0) or 0.0)

    @property
    def inv_type(self):
        t = self.d.get(self.node, "Type")
        return self.d.get(t, "InventoryType", "") if t else ""

    @property
    def cell(self):
        ix = self.d.get(self.node, "Index")
        return (self.d.get(ix, "X", 0), self.d.get(ix, "Y", 0))

    def set_cell(self, x, y):
        ix = self.d.get(self.node, "Index")
        self.d.set(ix, "X", int(x))
        self.d.set(ix, "Y", int(y))

    def identity(self):
        """Everything except Amount and Index, as a comparable tuple.

        The merge predicate, borrowed from nms-sort-o-matic and from
        docs/RULES.md: two stacks are the same item only when every field
        but the amount and the cell agrees. Keying on the id alone silently
        resolves a damaged module into an intact one.
        """
        out = []
        for k, v in sorted(self.node.items()):
            plain = self.d.fwd.get(k, k)
            if plain in ("Amount", "Index"):
                continue
            if isinstance(v, dict):
                v = tuple(sorted((self.d.fwd.get(a, a), b) for a, b in v.items()))
            elif isinstance(v, list):
                v = tuple(v)
            out.append((plain, v))
        return tuple(out)


class Container(object):
    def __init__(self, d, key, node, label, section, sortable, path):
        self.d = d
        self.key = key
        self.node = node
        self.label = label
        self.section = section
        self.path = path
        self.width = d.get(node, "Width", 0)
        self.height = d.get(node, "Height", 0)
        self.valid = [(d.get(c, "X", 0), d.get(c, "Y", 0))
                      for c in (d.get(node, "ValidSlotIndices") or [])]
        self.special = [(d.get(c, "X", 0), d.get(c, "Y", 0))
                        for c in (d.get(node, "SpecialSlots") or [])]
        ssg = d.get(node, "StackSizeGroup")
        self.group = d.get(ssg, "InventoryStackSizeGroup", "Default") if ssg else "Default"
        self.name = d.get(node, "Name", "") or ""
        self.allocated = len(self.valid) > 0
        self.is_tech = (key in NEVER_SORT and key != "fishbaitbox" and key != "foodunit")
        self.sortable = sortable and self.allocated
        # Drain-only: usable as a source, never as a destination.
        self.drain_only = False
        # Is this the ship or the exocraft the player is currently in? Said
        # rather than guessed: `PrimaryShip` and `PrimaryVehicle` are indices
        # the save carries, and for a vessel with no name of its own "in use"
        # is the only true thing there is to say about which one it is.
        #
        # Index 0 is a valid primary and is indistinguishable from the field
        # being unset: three corpus saves read `0 / 0`, which is either "the
        # first ship and the Roamer" or "nothing recorded". The owner's call
        # is that 0 is a primary, so the pill stays. An *absent* field is a
        # different answer and gives no pill at all: `_index_of` returns None
        # for it and None equals no index.
        self.in_use = False
        # Which vessel this is a grid of, and which of its grids. None for a
        # container that is not part of a vessel: a chest, the Corvette's
        # storage, a base machine, an extractor core. The page groups on these
        # two so it never has to split `_cargo` off a key in the browser, which
        # is the same guess the model refuses to make everywhere else.
        self.vessel = None
        self.vessel_label = ""
        # The role (`GENERAL`, `CARGO`, `TECHNOLOGY`) and the game's own word
        # for it. The word is set once, by `containers()`, from the generated
        # table; it is "" for a container that is not a vessel grid.
        self.grid_role = ""
        self.grid = ""
        # Is this a grid of a vessel slot the player does not own? The save
        # carries twelve starship slots and seven exocraft slots whether or
        # not there is a ship in them, and an unowned one is all zeros: no
        # name, no hull, no cells, nothing in it. Set by `containers()`,
        # which is the only place that can see a vessel's grids together.
        self.slot_empty = False

    # ------------------------------------------------------------ contents

    @property
    def slot_nodes(self):
        return self.d.get(self.node, "Slots") or []

    def slots(self):
        return [Slot(self.d, n) for n in self.slot_nodes]

    def is_maintenance_slot(self, slot):
        return is_maintenance_slot(self.drain_only, self.group, slot.inv_type)

    def holdings(self):
        """The slots that are inventory: everything the machine is not.

        `slots()` stays the whole array, because the write path addresses a
        stack by its position in it and a filtered view of it would be a
        different container.
        """
        return [s for s in self.slots() if not self.is_maintenance_slot(s)]

    def occupied_cells(self):
        return set(s.cell for s in self.slots())

    def free_cells(self):
        taken = self.occupied_cells()
        return [c for c in self.valid if c not in taken]

    def display_name(self):
        n = self.name.strip()
        if not n or n == "BLD_STORAGE_NAME" or n.endswith("_NAME") or n.endswith("_NAME_L"):
            return self.label
        return text.safe(n)

    # ------------------------------------------------------------- summary

    def view(self, difficulty):
        items = []
        idb = db()
        for i, s in enumerate(self.slots()):
            # The machine's own row is not inventory. Generic: the predicate is
            # false for every container that is not a machine's drain-only
            # buffer, so nothing else changes. `i` stays the slot's real
            # position in the array, because that is what a stray row and the
            # write path both address.
            if self.is_maintenance_slot(s):
                continue
            raw = s.id
            st = text.stem(raw)
            row = idb.lookup(raw)
            bucket = idb.bucket_of(raw)
            items.append({
                "i": i,
                "id": text.safe(raw),
                "stem": text.safe(st),
                "token": text.token(raw),
                "name": row["name"] if row else None,
                "kind": row["kind"] if row else None,
                "bucket": bucket,
                "bucket_label": idb.label(bucket),
                "amount": s.amount,
                "max": s.max_amount,
                "type": s.inv_type,
                "damage": s.damage,
                "cell": list(s.cell),
                "known": row is not None,
                "procedural": "#" in text.strip_caret(raw),
                "ascii": text.is_clean_ascii(st),
            })
        used = len(items)
        valid = len(self.valid)
        if self.drain_only and not valid:
            # A core's `ValidSlotIndices` is empty and its slots are real --
            # `extractor_containers` says exactly that in its own docstring.
            # Reading the empty array as the capacity gave `valid: 0, used: 6,
            # free: -6` and a card that said "6 / 0 cells".
            valid = used
        return {
            "key": self.key,
            "label": self.display_name(),
            "default_label": self.label,
            "section": self.section,
            "path": [p for p in self.path if isinstance(p, str)],
            "width": self.width,
            "height": self.height,
            "valid": valid,
            "used": used,
            # Never negative. A container holding more stacks than it has valid
            # cells is a real shape -- a grid the player shrank by removing a
            # cargo bulkhead -- and "-6 free cells" is not a number anybody can
            # act on.
            "free": max(0, valid - used),
            # Zero cells and not a machine's buffer: nothing can ever be in
            # this container, so the page draws none of them and no picker
            # offers one. Every starship and the exosuit read this way on
            # `Inventory_Cargo` on all seven readable corpus saves -- Waypoint
            # (4.0) merged the cargo grid into the main one and the game has
            # no way to put a cell back -- and the freighter is the one vessel
            # where the second grid is still real.
            #
            # A core is never dead: `ValidSlotIndices` is empty on every one
            # of them and its capacity is implied by its slots, which is what
            # the `drain_only` fallback above computes.
            "no_cells": bool(valid == 0 and not self.drain_only),
            "fill": (float(used) / valid) if valid else 0.0,
            "group": self.group,
            "special": len(self.special),
            "allocated": self.allocated,
            "slot_empty": self.slot_empty,
            "sortable": self.sortable,
            "is_tech": self.is_tech,
            "in_use": self.in_use,
            "vessel": self.vessel,
            "vessel_label": self.vessel_label,
            "grid": self.grid,
            "grid_role": self.grid_role,
            "items": items,
        }


class Extractor(object):
    """A Stellar Extractor Core: RefinerBufferData[i], addressed by filter."""

    def __init__(self, d, index, node, key_node):
        self.d = d
        self.index = index
        self.node = node
        self.container = d.get(node, "InventoryContainer")
        self.position = d.get(key_node, "Position") or []
        self.location = d.get(key_node, "Location")

    @property
    def last_update(self):
        """`LastUpdateTimestamp`, or None when the buffer does not carry one.

        The tie-breaker when two buffers claim the same extractor room: the
        game stops ticking a buffer whose room was demolished, so the stale one
        is the one whose last update is older. Measured on the operator's
        `save10.hg`: the fourteen live buffers last updated at 1789364553 or
        1789364588, the three stale ones at 1789347631, 1789347635 and
        1789347640 -- about four and a half hours earlier.

        None rather than 0 for a buffer without the field, so
        `extractor_match` can fall back to the buffer index instead of
        treating "no timestamp" as "the oldest possible timestamp". The
        synthetic fixture's core has no timestamp at all.
        """
        v = self.d.get(self.node, "LastUpdateTimestamp")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        return v

    def substances(self):
        """What this core holds, in one fixed order on every core.

        Not the save's own slot order: it differs between buffers, so cores 1
        to 11 listed Chromatic Metal first and cores 12 to 14 listed it last,
        after Methane -- the same five substances in two orders, in one grid
        whose whole purpose is comparing one core with the next.

        Chromatic Metal first, because it is the one the summary line totals
        and the one a player is looking for, then the gases by name. Order is
        a display concern and the write path addresses a stack by its position
        in `Slots`, which this does not touch.
        """
        out = []
        for n in (self.d.get(self.container, "Slots") or []):
            s = Slot(self.d, n)
            raw = s.id
            if text.stem(raw).startswith("MAINT_"):
                continue
            row = db().lookup(raw)
            out.append({"id": text.safe(raw), "name": row["name"] if row else None,
                        "amount": s.amount, "max": s.max_amount})
        out.sort(key=_substance_order)
        return out


# ---------------------------------------------------------------------------
# matching core buffers to extractor rooms
# ---------------------------------------------------------------------------
#
# `containers.md` asked for a *count* cross-check -- fourteen buffers against
# fourteen `^FRE_ROOM_EXTR` objects -- and the operator's own save falsifies
# the premise it rests on: `save10.hg` matches 17 buffers against 14 rooms,
# because a rebuilt room leaves its old buffer behind. Counting cannot tell
# which three of the seventeen are the leftovers, so the whole feature was
# switched off on the save it was written for.
#
# Identifying them can. Each buffer carries a `Position` and each room carries
# one too -- but *not in the same frame*, which is the measurement that decides
# the shape of this code. On `save10.hg`:
#
#     rooms   x -8 .. -80    y 4.0     z 27.5 / 35.5
#     buffers x  8 ..  80    y 39.3    z -1668.8 / -1676.8
#
# so the nearest room to any buffer is 1,697 m away and a plain distance test
# matches nothing. The two sets are *congruent*, though: both lie on the same
# 8-metre grid, ten in one row and four in another, and one rigid motion --
# a 180-degree yaw plus (0, +35.3, -1641.3) -- carries every room onto a
# buffer with a worst residual of 0.002 m. `register_rooms` recovers that
# motion from the data instead of assuming it.

#: How far a core buffer may sit from a room, once the frames are registered,
#: and still be that room's core. Measured on `save9.hg` and `save10.hg`: the
#: live buffers land within 0.0023 m of a room, the three stale ones within
#: 0.1414 m of the room they used to serve, and the rooms sit 8.0 m apart.
#: Any value from 0.2 to 4.0 separates "this room's buffer" from "the next
#: room's buffer"; 1.0 is 400x the worst live residual and 8x inside the room
#: pitch, so it is wrong only if the game changes both numbers at once.
#:
#: Note what the tolerance does *not* do: it does not distinguish live from
#: stale. A stale buffer is 0.14 m from its room, well inside any workable
#: tolerance, and is meant to be -- it is the *same* extractor, built twice.
#: Two buffers on one room is the stale test; see `SaveFile.extractor_match`.
ROOM_MATCH_TOLERANCE = 1.0

#: The yaws a registration may try, as (cos, sin). Freighter rooms snap to an
#: 8-metre grid aligned with the freighter, so the only rotation between the
#: two frames is a multiple of 90 degrees -- 180 on the operator's save. The
#: translation is not quantised and is taken from the data, so nothing here
#: assumes the +35.3 / -1641.3 that save happens to have.
_YAWS = ((1, 0), (0, 1), (-1, 0), (0, -1))


def _vec3(p):
    """`p` as a 3-tuple of floats, or None when it is not a position.

    `RawFloat` is a `float` subclass, so a position out of the document
    arrives as numbers already; this is about the shapes that are not
    positions at all -- a missing `Position` (`d.get` answers None), a
    truncated one, or a string. None propagates: a core with no position is
    unplaced and a room with no position has no core, which is the truth
    rather than a zero-vector that would match everything at the origin.
    """
    if not isinstance(p, (list, tuple)) or len(p) != 3:
        return None
    out = []
    for v in p:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        out.append(float(v))
    return tuple(out)


def _dist(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2
                     + (a[2] - b[2]) ** 2)


def register_rooms(cores, rooms, tolerance=ROOM_MATCH_TOLERANCE):
    """-> `rooms` moved into `cores`' frame, in the same order, or None.

    Both arguments are lists of 3-tuples. The answer is a list as long as
    `rooms`, positionally aligned with it, holding each room's position in the
    frame the buffers are written in -- so the caller can compare a buffer to a
    room with `_dist` and nothing else.

    The motion is found by vote rather than by fitting, and that is deliberate.
    The obvious fit is "align the centroids": it is three lines, and it is
    exactly as fragile as the count check this replaces, because one unplaced
    buffer or one room without a buffer moves the centroid and then *every*
    pairing is wrong. Instead: for each of the four yaws, and for each
    (buffer, room) pair, take the translation that would carry that room onto
    that buffer, and score the whole candidate by how many buffers land within
    `tolerance` of some room. A correct motion is voted for by every matched
    pair at once, so it wins by a margin that does not care how many extras
    either side has.

    Scored on (matches, then smallest total residual). Measured on
    `save10.hg`: yaw 180 matches 17 of 17 buffers onto 14 distinct rooms with
    a total residual of 0.43 m, while the other three yaws match 10, 3 and 3
    -- not a close call. 17 buffers against 14 rooms is 4 x 17 x 14 = 952
    candidate motions, deduplicated to the distinct translations, and the
    whole registration measures 17 ms.

    None when either side is empty, or when no motion matches anything at all.
    """
    if not cores or not rooms:
        return None
    best = None
    for ca, sa in _YAWS:
        rot = [(ca * x + sa * z, y, -sa * x + ca * z) for x, y, z in rooms]
        tried = set()
        for c in cores:
            for r in rot:
                shift = (round(c[0] - r[0], 3), round(c[1] - r[1], 3),
                         round(c[2] - r[2], 3))
                if shift in tried:
                    continue
                tried.add(shift)
                moved = [(p[0] + shift[0], p[1] + shift[1], p[2] + shift[2])
                         for p in rot]
                hits, residual = 0, 0.0
                for cc in cores:
                    near = min(_dist(cc, m) for m in moved)
                    if near <= tolerance:
                        hits += 1
                        residual += near
                if hits and (best is None
                             or (hits, -residual) > (best[0], -best[1])):
                    best = (hits, residual, moved)
    return None if best is None else best[2]


# ---------------------------------------------------------------------------
# the save file
# ---------------------------------------------------------------------------

SAVE_RE = re.compile(r"^save(\d*)\.hg$", re.I)


def folded(name):
    """`name` as it is compared when case is not allowed to matter.

    `os.path.normcase` and a `lower()`: the first is what Windows itself does
    to a path, the second is what makes the comparison the same one on Linux,
    where `normcase` returns its argument untouched.
    """
    return os.path.normcase(name).lower()


def match_name(names, want):
    """The name in `names` that answers to `want`. -> that name, or None.

    One rule, on every filesystem: the exact spelling if it is there, and
    otherwise the *only* name that differs from it by case alone. Two names
    that differ by case alone and no exact match is an ambiguity, and this
    program does not pick (GOAL.md Rule 1) -- the answer is None, which every
    caller reads as "there is no such file".

    Written this way rather than with `os.path.normcase` because `normcase`
    answers for the *interpreter's* platform, not for the filesystem in front
    of it, and on Linux it does nothing at all: `readme.MD` was a 404 on the
    documents route and `SAVE.HG` was "there is no SAVE.HG in the selected
    save folder" for a file the same page had just listed. Windows never
    reaches the second half -- a directory cannot hold two names that differ
    by case -- so its behaviour is the exact match it always was.
    """
    if want in names:
        return want
    wanted = folded(want)
    hits = [name for name in names if folded(name) == wanted]
    return hits[0] if len(hits) == 1 else None


def on_disk_path(path):
    """`path` with the spelling the filesystem actually has.

    Windows matches names case-insensitively and stores them case-preservingly,
    so `SAVE9.HG` names the same file as `save9.hg` -- and `os.replace` onto the
    first spelling *renames* the operator's save (review one, R6). Measured: an
    apply through `SAVE9.HG` left `SAVE9.HG` and `mf_SAVE9.HG` behind, named the
    backup folder `<stamp>-SAVE9` and split the plan signature, which is taken
    over the basename.

    The fix is not to refuse the other spelling and not to normalise it either:
    it is to write to the name that is there. The directory listing is the only
    thing that knows it, and `match_name` is the one rule for reading it.

    It lives here rather than in `safety`, which is where it was written,
    because the `mf_` half of the pair is named here -- `meta_path`, `gates`
    and `list_saves` all build `"mf_" + <save name>` -- and those three sites
    were the last route left to the damage R6 was raised about (write-path
    review two, Q9). `safety.on_disk_path` is this function.

    `match_name` answers None to two different questions, and this used to
    give one answer to both: the literal path it was given. That is right for
    "there is no such name" -- a save with no `mf_` beside it needs the
    invented name so that `os.path.exists` can say no -- and wrong for "there
    is more than one spelling of it", where the spelling asked for is a file
    that is not there and the callers write to whatever comes back (review 5,
    note 7). Measured against a simulated `['save.hg', 'SAVE.HG',
    'mf_save.hg']` listing: `on_disk_path("Save.hg")` -> `<folder>\\Save.hg`.
    So ambiguity is a refusal here too, in the exception the dispatcher
    already turns into a sentence. Windows cannot reach it: a directory there
    cannot hold two names that differ only by case.
    """
    folder = os.path.dirname(path) or "."
    try:
        entries = os.listdir(folder)
    except OSError:
        return path
    base = os.path.basename(path)
    real = match_name(entries, base)
    if real is not None:
        return os.path.join(folder, real)
    wanted = folded(base)
    spellings = sorted(n for n in entries if folded(n) == wanted)
    if len(spellings) > 1:
        raise SaveGate(
            "the save folder holds more than one file named %s, differing "
            "only in upper and lower case (%s), so it cannot be told which "
            "one is meant; rename or move one of them and try again"
            % (base, ", ".join(spellings)))
    return path


#: the closed range of root `Version` values this build has actually been run
#: against, read from the operator's own corpus: 4670 (`save6.hg`), 4734
#: (`save3`, `save4`, `save5`, `save7`) and 4735 (`save9`, `save10`) all load
#: and plan; 4734 and 4735 also apply. 4670's `mf_` is a format 2003
#: (`mf_save6.hg` is 384 bytes, not 432), which `safety.META_FORMAT` refuses at
#: step 8, so that save reads and plans and its apply stops before the write.
#: 4670 is the floor because it is the oldest save in the corpus that carries
#: `BaseContext`; 4645 (`save.hg`, `save2.hg`) does not and is refused by
#: `UnsupportedSave` before a version gate could speak. Widened only by a
#: fixture, never by a guess (GOAL.md Rule 1).
SUPPORTED_VERSIONS = (4670, 4735)


class SaveGate(ValueError):
    """A save this build refuses to plan for, or to apply to.

    Distinct from `UnsupportedSave`, which is "the document shape is one
    nothing here was written against". A gate is about a save this code could
    navigate and deliberately will not: an expedition, whose live inventory may
    be the season copy, or a build whose container layout nobody has measured.
    The message is the operator's sentence; `SaveFile.gates()` is where the
    wording lives.
    """


class UnsupportedSave(ValueError):
    """A save whose document shape this build cannot navigate.

    Not "a save we failed to read": the bytes decoded and round-trip fine. The
    layout is one nothing here was written against, and the alternative to
    saying so is a save that reports zero containers and looks empty. Two of
    the nine saves in the operator's own folder are this: Version 4645, written
    before Waypoint moved the player state under `BaseContext`.
    """


def save_version(doc):
    """The root `Version` of a decoded document, whatever its layout.

    Separate from `SaveFile.version()` because the version is the one field
    worth reading from a document this build has already refused: a refusal
    that can say *which* build the save came from is a better refusal.
    """
    fwd, rev = keymap()
    if not isinstance(doc, dict):
        return None
    for k in (rev.get("Version"), "Version"):
        if k and k in doc:
            return doc[k]
    return None


class SaveFile(object):
    def __init__(self, path):
        self.path = os.path.abspath(path)
        # Read once, hash the bytes that were read, decode those same bytes.
        # `read_payload` would do the open itself and throw the raw file away,
        # which would make the hash either a second read of a file that may
        # have moved in between, or -- worse, and what it was -- a lazy read
        # taken whenever somebody first asked for the signature. See `digest`.
        with open(self.path, "rb") as fh:
            raw = fh.read()
        self._digest = hashlib.sha256(raw).hexdigest()
        self.payload, self.info = read_payload_bytes(raw)
        self.doc = loads(self.payload)
        self.d = Doc(self.doc)
        if self.d.player is None:
            raise UnsupportedSave(
                "this save has no BaseContext; saves older than Waypoint (4.0) "
                "are not supported (this one reports version %r)"
                % (save_version(self.doc),))
        self.stat = os.stat(self.path)

    # ------------------------------------------------------------ identity

    def digest(self):
        """SHA-256 of the bytes this `SaveFile` was parsed from.

        The name, the size and the modification time are all things a tool can
        preserve while changing the contents: a same-length edit written back
        with the original timestamp used to be invisible to both the save cache
        and the plan signature, so the page and the plan described bytes that
        were no longer on disk (review one, R15). A hash cannot be preserved by
        accident.

        Taken in `__init__`, from the bytes that were read, and never
        recomputed. That is the whole point: a lazily hashed `SaveFile`
        describes whatever is on disk at the moment somebody first asks, so a
        file that changed under a cached save would answer with the *new*
        hash and compare equal to the disk it no longer matches. Eager, it
        means one thing only -- "the document in this object came from these
        bytes" -- and it costs one hash of a file that was being read anyway.
        """
        return self._digest

    def signature(self):
        """Has the file on disk moved under a cached read? -> name|size|mtime|hash

        What `App.save()` compares to decide whether to re-read, against
        `app.disk_signature`, which builds the same string from a `stat` and a
        hash without decoding anything. The mtime belongs here: it is the
        cheap half of the comparison and this string is only ever compared
        against another one taken from the same file moments earlier.

        It is *not* what a plan is pinned to any more. See
        `content_signature`.
        """
        return "%s|%d|%d|%s" % (os.path.basename(self.path),
                                self.stat.st_size, int(self.stat.st_mtime),
                                self.digest())

    def content_signature(self):
        """What a plan is pinned to: name|size|sha256. No timestamp.

        The plan fingerprint (`planner._fingerprint`) carried `signature()`,
        and `safety.apply_plan` re-plans from the *backup copy* and compares
        the result against the digest the operator approved. The only thing
        that made the copy's mtime equal the original's was `shutil.copy2`
        succeeding at it -- and `backup_folder` is operator-settable, so the
        backup root can be exFAT (two-second mtime granularity), a network
        share, or a folder a sync client restamps. On any of those the
        recomputed fingerprint differed on every single run and the save could
        not be sorted at all, with a sentence that said "Re-run the plan",
        which could never help (write-path review two, Q6).

        Dropping the mtime costs nothing a hash does not already say: R15 put
        the SHA-256 in precisely because a name, a size and a timestamp are all
        things a tool can preserve while changing the contents. The hash cannot
        be preserved by accident, and it is the same for the original and for a
        copy of it, which is what step 5 needs.
        """
        return "%s|%d|%s" % (os.path.basename(self.path),
                             self.stat.st_size, self.digest())

    # ------------------------------------------------------------- the gates

    def season_word(self):
        """The `mf_`'s season number (`0x48` high 16 bits), or None.

        None means "there is no metadata beside this save, or it would not
        decode" -- which is not "not an expedition". The document half of the
        test below stands on its own for exactly that reason.
        """
        mp = self.meta_path()
        if not mp:
            return None
        try:
            from .codec import meta_read
            return meta_read(mp)["season"]
        except Exception:
            return None

    def season_slots(self):
        """How many stacks the expedition's own inventory holds."""
        inv = self.d.at(["CommonStateData", "SeasonData", "Inventory"])
        if inv is None:
            return 0
        return len(self.d.get(inv, "Slots") or [])

    def has_season_data(self):
        """Does this document carry `CommonStateData/SeasonData` at all?

        The presence of the node, not a count inside it: an expedition save
        carries its own inventory outside `PlayerStateData`, and whether that
        inventory happens to be empty right now is a fact about the run, not
        about what kind of save this is (write-path review two, Q12).
        """
        return self.d.at(["CommonStateData", "SeasonData"]) is not None

    def gates(self, strict=False):
        """-> [{level, where, message}] for every reason to stop on this save.

        `strict` is the operator's `strict_version_check`. It is a parameter
        rather than something a caller bolts on afterwards because the version
        gate's *sentence* differs between the two modes, and a sentence
        composed in two places is how the page came to say "apply is refused"
        and "apply proceeds" in one breath (owner's report). One sentence, from
        here, complete.

        `level` is one of three, and the difference is which stage acts on it:

        * `"refuse"` -- `planner.build_plan` raises `SaveGate`; no plan exists.
        * `"warn"` -- the plan is printed with the sentence as a banner and
          `safety.apply_plan` step 1b refuses.
        * `"note"` -- the plan is printed, the sentence is said out loud at
          plan level and at step 1b, and the apply runs. For a caveat that is
          worth stating and is not a reason to stop.

        Three gates exist, and each is a Rule 1 refusal rather than a feature
        postponed:

        **expedition** -- the `mf_` season word is non-zero, or the save
        carries stacks in `CommonStateData/SeasonData/Inventory`. In an
        expedition the live inventory may be the season copy, and the
        relationship between it and the containers this build paths to has
        never been measured. `refuse`.

        **expedition, unconfirmed** -- the document carries a `SeasonData` node
        and there is no `mf_` to read a season word out of. R4 made "a save
        with no `mf_`" a supported case, and `season_word()` answers None both
        for that and for "there is metadata and this is not an expedition", so
        what was left holding the gate up was a slot count -- and an expedition
        whose season inventory is empty read as an ordinary save with nothing
        said (write-path review two, Q12). `note`: the strength of the
        remaining evidence is now on the record instead of assumed. No corpus
        save is an expedition, so whether the game ever writes an empty
        `SeasonData/Inventory` is unmeasured, which is exactly why this says so
        rather than refusing or passing in silence.

        **version** -- the root `Version` is outside `SUPPORTED_VERSIONS`.
        Planning always works, because reading is how a corpus gets collected.
        Whether writing works is the operator's `strict_version_check` and not
        a fact about the save, so this gate answers at the level that describes
        what will actually happen: `note` with the flag off, because the apply
        runs under steps 4, 6 and 7 on this exact file, and `warn` with it on,
        because step 1b then refuses. Each level carries the sentence for that
        outcome and nothing else.
        """
        out = []
        season = self.season_word()
        slots = self.season_slots()
        if (season or 0) != 0 or slots > 0:
            out.append({
                "level": "refuse", "where": "expedition",
                "message": ("this is an expedition save; the live inventory "
                            "may be the season copy and this build does not "
                            "know which, so it is not planned")})
        elif self.has_season_data() and self.meta_path() is None:
            out.append({
                "level": "note", "where": "expedition",
                "message": ("no metadata file beside this save, so whether it "
                            "is an expedition cannot be confirmed from the "
                            "mf_; the season inventory is empty, so it is "
                            "treated as a normal save")})
        v = self.version()
        lo, hi = SUPPORTED_VERSIONS
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            if strict:
                out.append({
                    "level": "warn", "where": "version",
                    "message": ("this save reports version %s; this build was "
                                "verified on %d to %d, and strict_version_check "
                                "is on, so apply is refused." % (v, lo, hi))})
            else:
                out.append({
                    "level": "note", "where": "version",
                    "message": ("this save reports version %s; this build was "
                                "verified on %d to %d. Apply proceeds: the "
                                "round-trip and nothing-else-changed checks "
                                "run on this exact file. Settings can turn on "
                                "strict_version_check to refuse instead."
                                % (v, lo, hi))})
        return out

    def meta_path(self):
        """The `mf_` beside this save, spelled the way the filesystem spells
        it, or None.

        Through `on_disk_path`, because `"mf_" + basename` is a name this code
        invented: a metadata file called `mf_SAVE9.HG` beside a `save9.hg` is
        found by `os.path.exists` under the invented spelling and was then
        *renamed* by the metadata write at step 10 (write-path review two, Q9).
        """
        p = on_disk_path(os.path.join(os.path.dirname(self.path),
                                      "mf_" + os.path.basename(self.path)))
        return p if os.path.exists(p) else None

    # ---------------------------------------------------------- properties

    def difficulty(self):
        st = self.d.at(["BaseContext", "PlayerStateData", "DifficultyState", "Settings"])
        node = self.d.get(st, "InventoryStackLimits") if st else None
        val = self.d.get(node, "InventoryStackLimitsDifficulty") if node else None
        return val or "Normal"

    def version(self):
        return self.d.get(self.doc, "Version")

    def freighter_name(self):
        n = self.d.get(self.d.player, "PlayerFreighterName", "") or ""
        return text.safe(n)

    def units(self):
        return self.d.get(self.d.player, "Units")

    # ---------------------------------------------------------- containers

    def containers(self):
        """Ordered list of Container. Ships and exocraft are discovered, not
        hardcoded: a sold ship is a slot the game keeps, so it is skipped by the
        ValidSlotIndices test rather than by index."""
        d = self.d
        ps = d.player
        out = []
        # Two static labels the save can answer for, and both were parsed and
        # thrown away: the freighter's name reached the browser as
        # `save.freighter` and nothing rendered it, and the Corvette's was
        # never read at all.
        # Through `vessel_name` like every other vessel: it applies
        # `VESSEL_NAME_MARKERS` as well as `text.safe`, and the freighter was
        # the one name that skipped the marker half (review 4, finding 11).
        # `S-FreightXXX`, which two corpus saves carry, is a real generated
        # freighter name in this game and is not a marker, so it stays.
        fleet_name = vessel_name(self.freighter_name())
        corvette_name = vessel_name(d.get(ps, "CorvetteEditShipName"))
        for key, rel, label, section, sortable in STATIC_CONTAINERS:
            node = d.at(rel, ps)
            if node is None:
                continue
            if fleet_name and key in FREIGHTER_GRIDS:
                label = fleet_name + FREIGHTER_GRIDS[key]
            elif corvette_name and key == "corvette":
                label = corvette_name + " Storage"
            c = Container(d, key, node, label, section, sortable,
                          ["BaseContext", "PlayerStateData"] + rel)
            c.vessel, c.grid_role = VESSEL_GRIDS.get(key, (None, ""))
            out.append(c)

        for key, plain, label in discover_chests(d, ps):
            node = d.get(ps, plain)
            if node is None:
                continue
            out.append(Container(d, key, node, label, STORAGE, True,
                                 ["BaseContext", "PlayerStateData", plain]))

        primary_ship = _index_of(d.get(ps, "PrimaryShip"))
        primary_vehicle = _index_of(d.get(ps, "PrimaryVehicle"))

        # Which vessels carry a hull in the save, for the unowned-slot test
        # below: a ship the game has given a model is a ship the player owns,
        # whatever its cells say.
        vessel_hulls = {}
        # And which carry a procedural seed, which is the same fact said
        # twice: measured across the seven readable corpus saves, every
        # `ShipOwnership` row with a hull has `Resource.Seed` `[True, <hex>]`
        # and every row without one has `[False, "0x0"]`. Read as a
        # corroboration rather than a second test: a row with a seed and no
        # hull is drawn, because "visible and wrong" is the direction to be
        # wrong in.
        vessel_seeds = {}
        # The vessels that are *slots*: the save carries all twelve starship
        # and all seven exocraft slots whether or not there is anything in
        # them. The exosuit and the freighter are not slots -- you have one of
        # each or the node is absent -- so they are never unowned.
        slot_vessels = set()
        # Which of those slots the hull test decides, which is the starships.
        # An exocraft cannot be decided that way: on all seven corpus saves
        # every one of the seven `VehicleOwnership` rows carries an empty
        # `Resource.Filename`, `[False, "0x0"]` and a fully allocated grid,
        # whether the player has built that exocraft or not, so the hull test
        # would hide all seven. They keep the all-zeros test below.
        hull_slots = set()
        ships = d.get(ps, "ShipOwnership") or []
        for i, ship in enumerate(ships):
            res = d.get(ship, "Resource")
            filename = d.get(res, "Filename") if res else ""
            seed = d.get(res, "Seed") if res else None
            vessel_hulls["ship%d" % i] = str(filename or "").strip()
            vessel_seeds["ship%d" % i] = bool(
                isinstance(seed, list) and seed and seed[0])
            slot_vessels.add("ship%d" % i)
            hull_slots.add("ship%d" % i)
            base = ship_label(i, d.get(ship, "Name"), filename, corvette_name)
            for suffix, lab, sec, sortable, grid in (
                    ("Inventory", "", SHIPS, True, GENERAL),
                    ("Inventory_Cargo", " Cargo", SHIPS, True, CARGO),
                    ("Inventory_TechOnly", " Technology", TECH, False, TECHNOLOGY)):
                node = d.get(ship, suffix)
                if node is None:
                    continue
                k = "ship%d%s" % (i, {"Inventory": "", "Inventory_Cargo": "_cargo",
                                      "Inventory_TechOnly": "_tech"}[suffix])
                c = Container(d, k, node, base + lab, sec, sortable,
                              ["BaseContext", "PlayerStateData", "ShipOwnership", i, suffix])
                c.is_tech = suffix == "Inventory_TechOnly"
                c.in_use = i == primary_ship
                c.vessel, c.grid_role = "ship%d" % i, grid
                out.append(c)

        vehicles = d.get(ps, "VehicleOwnership") or []
        for i, veh in enumerate(vehicles):
            slot_vessels.add("vehicle%d" % i)
            base = vehicle_label(i, d.get(veh, "Name"))
            for suffix, lab, sec, sortable, grid in (
                    ("Inventory", "", EXOCRAFT, True, GENERAL),
                    ("Inventory_TechOnly", " Technology", TECH, False, TECHNOLOGY)):
                node = d.get(veh, suffix)
                if node is None:
                    continue
                k = "vehicle%d%s" % (i, "" if suffix == "Inventory" else "_tech")
                c = Container(d, k, node, base + lab, sec, sortable,
                              ["BaseContext", "PlayerStateData", "VehicleOwnership", i, suffix])
                c.is_tech = suffix == "Inventory_TechOnly"
                c.in_use = i == primary_vehicle
                c.vessel, c.grid_role = "vehicle%d" % i, grid
                out.append(c)

        # A vessel slot the player does not own. The save carries twelve
        # starship slots and seven exocraft slots always, whether there is
        # anything in them or not, and a card per empty slot was twenty cards
        # of nothing on save3 and the reason "Starship N" was the commonest
        # label on the page.
        #
        # For a **starship** the hull is the whole test: no `Resource
        # .Filename` means no ship, whatever the cells say. A slot the player
        # traded or scrapped keeps the layout the ship had -- save10 and save9
        # slot 2 read 35 cells, 24 technology cells and nothing in either,
        # with an empty hull and `[False, "0x0"]` for a seed -- and those were
        # being drawn as "Starship 3", which is what the owner asked about.
        # The cells are the leftover, not a vessel. save6 slot 0 (25 cells)
        # and save7 slot 1 (16 cells) are the same shape.
        #
        # Two further ways to be a ship, both of them "fail visible": a name
        # the player typed, and anything at all in one of the grids. Neither
        # can be the test on its own -- the game writes `StarterShip` and
        # `MainShip` into the name itself, and a leftover keeps its cells --
        # but a slot holding items that this page did not draw would be
        # invisible inventory, which is the one outcome worth ruling out.
        # Every leftover slot in the corpus (save10 and save9 slot 2, save6
        # slot 0, save7 slot 1) holds nothing a player can move.
        #
        # The technology grid does not count towards that: a scrapped slot
        # keeps its modules -- save10 slot 2 has 24 technology cells -- and no
        # technology grid is drawn or sorted anywhere in this program, so
        # there is no inventory to lose sight of.
        #
        # For an **exocraft** it cannot be: every one of the seven rows
        # carries an empty hull and a full grid on all seven corpus saves, so
        # the hull test would hide the lot. They keep the all-zeros test,
        # which is the only thing the save says.
        #
        # Decided here rather than in the page because it is a fact about the
        # save, and it needs every grid of the slot at once.
        by_vessel = {}
        for c in out:
            if c.vessel:
                by_vessel.setdefault(c.vessel, []).append(c)
        for vessel, grids in by_vessel.items():
            if vessel not in slot_vessels:
                continue                    # the exosuit and the freighter
            if vessel in hull_slots:
                if (not vessel_hulls.get(vessel, "")
                        and not vessel_seeds.get(vessel)
                        and not any((g.slot_nodes and not g.is_tech)
                                    or vessel_name(g.name)
                                    for g in grids)):
                    for g in grids:
                        g.slot_empty = True
                continue
            if any(g.valid or g.slot_nodes or g.name.strip() for g in grids):
                continue
            for g in grids:
                g.slot_empty = True

        # The heading a vessel's grids sit under is the General grid's own
        # label, resolved here rather than assembled a second time: it is the
        # name the player set, or the hull's class, or the exocraft table's
        # word, and there must not be two answers to that.
        heads = dict((c.key, c.label) for c in out)
        for c in out:
            if c.vessel:
                c.vessel_label = heads.get(c.vessel, c.label)
                # And the word the game itself puts on that grid's tab, which
                # the page only draws when a vessel has two grids that can
                # hold something. One place, so a card and a picker cannot
                # disagree about it.
                c.grid = grid_title(c.vessel, c.grid_role)
        return out

    def container_map(self):
        return dict((c.key, c) for c in self.containers())

    def extractor_match(self):
        """Which core buffer belongs to which extractor room.

        The answer, and the single place the identification happens:

            {"rooms":      how many rooms, or None for no freighter base,
             "live":       [{room, position, core, distance}] in room order,
             "stale":      [{room, position, core, distance}],
             "unplaced":   [{room: None, position, core}],
             "coreless":   [room numbers with no buffer at all],
             "registered": was a frame registration found at all}

        Three outcomes, and each is a fact about one object rather than about a
        pair of totals:

        **live** -- exactly one buffer sits within `ROOM_MATCH_TOLERANCE` of
        this room, or it is the newest of several. Offered by name.

        **stale** -- a second buffer on a room that already has a newer one.
        The game rebuilt the room and left the old buffer behind; it does not
        read it any more, so its contents are not real inventory and moving
        them would *duplicate* items. Never offered, never drained.

        **unplaced** -- a buffer that matched no room. Nothing can be said
        about it, so nothing is: it is reported and dropped.

        Which of several buffers on one room is the live one is decided by
        `LastUpdateTimestamp`, highest wins, and by the buffer index when the
        field is absent or equal -- the later array entry being the more
        recently created one. On `save10.hg` the timestamps decide it
        outright: the three stale buffers are four and a half hours older than
        every live one.

        No freighter base at all (`rooms` is None) is not a failure to match:
        there is nothing to match against, so every buffer is live, in
        position order, with no room number. That is the shape of a save whose
        extractors are deployed on a planet rather than in a freighter.

        Memoised on the positions it was computed from, not on the `SaveFile`:
        the document is mutable -- the planner's `commit()` rewrites amounts
        and the tests bolt a freighter base on after loading -- and a cache
        that outlived the thing it described is the bug this whole method
        exists to fix. Positions are what the answer depends on, so positions
        are the key.
        """
        cores = self.extractors()
        rooms = self.extractor_room_objects()
        core_pos = [_vec3(c.position) for c in cores]
        key = (tuple(c.index for c in cores), tuple(core_pos),
               None if rooms is None else tuple(r["position"] for r in rooms))
        cached = getattr(self, "_match_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]

        out = {"rooms": None if rooms is None else len(rooms), "live": [],
               "stale": [], "unplaced": [], "coreless": [], "registered": False}
        if rooms is None:
            order = sorted(range(len(cores)),
                           key=lambda i: (core_pos[i] is None,
                                          core_pos[i] or (), cores[i].index))
            out["live"] = [{"room": None, "position": core_pos[i],
                            "core": cores[i], "distance": None} for i in order]
            out["registered"] = None    # not attempted; there was no frame
            self._match_cache = (key, out)
            return out

        placed = [(r["room"], r["position"]) for r in rooms
                  if r["position"] is not None]
        moved = register_rooms([p for p in core_pos if p is not None],
                               [p for _n, p in placed])
        out["registered"] = moved is not None
        by_room = {}
        for i, ex in enumerate(cores):
            near, room = None, None
            if moved is not None and core_pos[i] is not None:
                for (n, _p), m in zip(placed, moved):
                    dd = _dist(core_pos[i], m)
                    if near is None or dd < near:
                        near, room = dd, n
                if near > ROOM_MATCH_TOLERANCE:
                    near, room = None, None
            row = {"room": room, "position": core_pos[i], "core": ex,
                   "distance": near}
            if room is None:
                out["unplaced"].append(row)
            else:
                by_room.setdefault(room, []).append(row)
        for r in rooms:
            claims = by_room.get(r["room"])
            if not claims:
                out["coreless"].append(r["room"])
                continue
            # Newest first, then the later buffer index. `-1` for a buffer with
            # no timestamp loses to any buffer that has one, which is the right
            # way round: a buffer the game has never ticked is not the live one.
            claims.sort(key=lambda w: (w["core"].last_update
                                       if w["core"].last_update is not None
                                       else -1, w["core"].index),
                        reverse=True)
            out["live"].append(claims[0])
            out["stale"].extend(claims[1:])
        self._match_cache = (key, out)
        return out

    def extractor_report(self):
        """What `extractor_match` found, as plain data for the page and a test.

        `{live: N, stale: [...], unplaced: [...], rooms: M, coreless: [...]}`,
        where each `stale` and `unplaced` row names the buffer by its
        `RefinerBufferData` index and its position, so an operator looking at
        a save editor can find the object the sorter decided to ignore.
        """
        m = self.extractor_match()
        def row(w):
            return {"index": w["core"].index, "position": w["position"],
                    "room": w["room"], "last_update": w["core"].last_update,
                    "distance": w["distance"]}
        return {"live": len(m["live"]), "rooms": m["rooms"],
                "stale": [row(w) for w in m["stale"]],
                "unplaced": [row(w) for w in m["unplaced"]],
                "coreless": list(m["coreless"]),
                "registered": m["registered"]}

    def extractor_room_mismatch(self):
        """Kept as a name because the planner reports it at plan level.

        It used to be the count cross-check itself and said "found 17 Stellar
        Extractor cores but 14 extractor rooms; refusing to name them" -- which
        was the only thing wrong with the operator's save and switched the
        whole feature off on it. It is now `extractor_issue`: a sentence about
        the objects that could not be identified, and nothing at all about the
        ones that could.
        """
        return self.extractor_issue

    @property
    def extractor_issue(self):
        """What could not be identified, or None. Not "the counts disagree".

        Two things are worth saying, and a stale buffer is neither of them:
        a rebuilt room leaving its old buffer behind is the normal state of a
        played save, it is fully explained, and the live buffer is offered, so
        there is nothing for the operator to do. It is reported in
        `extractor_report()` and said once on the page as a count.

        What is worth saying is a room whose buffer this code could not find,
        and a buffer that belongs to no room -- because in both cases
        something in this save is not being offered and the reason is not
        "you rebuilt a room".
        """
        r = self.extractor_report()
        said = []
        if r["coreless"]:
            said.append("%d of %d extractor rooms have no core buffer"
                        % (len(r["coreless"]), r["rooms"]))
        if r["unplaced"]:
            said.append("%d core buffers matched no room and are not offered"
                        % len(r["unplaced"]))
        return "; ".join(said) if said else None

    def extractor_containers(self, strict=False):
        """The live extractor cores, presented as containers you may drain.

        Keyed `extractor1` upwards **in room order** -- the rooms sorted by
        position -- so a config that names `extractor7` means the same
        extractor on the next reload. It used to be buffer-array order, which
        is the one thing about this that a rebuilt room changes.

        Two things make them unlike every other container, and both are why
        they are drain-only rather than ordinary storage:

        `ValidSlotIndices` is empty on every one of them. The normal allocated
        test reads that as "this container does not exist", which is wrong
        here: the slots are real and carry real cells. Validity is implied by
        the slots.

        The game refills them on its own from `AmountAccumulators`, a parallel
        float array of part-mined fractions. Writing into one would be racing
        the simulation for a cell it owns, so nothing is ever sorted *into* an
        extractor: `sortable` stays False and the planner refuses it as a
        destination.

        Stale and unplaced buffers are not here. A stale buffer is a real
        container holding real-looking items that the game no longer reads, so
        draining it would create items rather than move them.

        `strict=True` raises rather than returning a list when something could
        not be identified (`extractor_issue`). The planner asks for the
        sentence and reports it; nothing browses strictly, because a save the
        game runs must still be readable here.
        """
        if strict:
            bad = self.extractor_issue
            if bad:
                raise ValueError(bad)
        out = []
        for n, w in enumerate(self.extractor_match()["live"]):
            ex = w["core"]
            c = Container(self.d, "extractor%d" % (n + 1), ex.container,
                          "Stellar Extractor %d" % (n + 1), EXTRACTORS, False,
                          ["BaseContext", "PlayerStateData", "RefinerBufferData",
                           ex.index, "InventoryContainer"])
            c.allocated = True          # the slots are real; ValidSlotIndices is not
            c.drain_only = True
            c.is_tech = False
            c.room_index = w["room"]
            c.position = w["position"]
            c.stale = False
            out.append(c)
        return out

    # ----------------------------------------------------------- extractors

    def extractors(self):
        """Every Stellar Extractor Core buffer, by the two-condition filter
        from containers.md: RefinerBufferKeys[i].Location == 2 and the
        container holds ^MAINT_HOOVER. Never by index.

        Not "the fourteen", and not "the live ones" either: on the operator's
        save this matches 17 buffers for 14 rooms, three of them left behind by
        rebuilt rooms. `extractor_match` is what sorts them out; this is the
        raw filter and it stays raw, because a stale buffer is a real object
        in the save and a function that hid it could not report it.
        """
        d = self.d
        ps = d.player
        data = d.get(ps, "RefinerBufferData") or []
        keys = d.get(ps, "RefinerBufferKeys") or []
        out = []
        for i, node in enumerate(data):
            kn = keys[i] if i < len(keys) else {}
            if d.get(kn, "Location") != 2:
                continue
            cont = d.get(node, "InventoryContainer")
            ids = [text.stem(d.get(s, "Id", "")) for s in (d.get(cont, "Slots") or [])]
            if "MAINT_HOOVER" not in ids:
                continue
            out.append(Extractor(d, i, node, kn))
        return out

    def extractor_room_objects(self):
        """Every ^FRE_ROOM_EXTR in the freighter base, with its position.

        `[{"room": 1-based number, "position": (x, y, z) or None}]`, sorted by
        position. The sort is what makes `extractor1` stable: the buffer array
        reorders when a room is rebuilt, and where the room sits in the
        freighter does not. A room whose `Position` will not read sorts last
        and can never be matched, which is reported rather than guessed at.

        Collected across every `FreighterBase` entry, not just the first: a
        save carries eleven bases and nothing promises the freighter is one
        particular element of that list, nor that there is only one. Returns
        None when there is no freighter base at all, which is "nothing to
        match against" rather than "zero rooms".
        """
        d = self.d
        bases = d.get(d.player, "PersistentPlayerBases") or []
        out, seen = [], False
        for b in bases:
            bt = d.get(b, "BaseType")
            if d.get(bt, "PersistentBaseTypes") != "FreighterBase":
                continue
            seen = True
            for o in (d.get(b, "Objects") or []):
                if text.stem(d.get(o, "ObjectID", "") or "") != "FRE_ROOM_EXTR":
                    continue
                out.append({"position": _vec3(d.get(o, "Position"))})
        if not seen:
            return None
        out.sort(key=lambda r: (r["position"] is None, r["position"] or ()))
        for n, r in enumerate(out):
            r["room"] = n + 1
        return out

    def extractor_rooms(self):
        """How many ^FRE_ROOM_EXTR objects the freighter base holds, or None.

        The count, for the page's summary line. `extractor_room_objects` is
        the same walk with the positions kept, and is what the matching uses:
        a count is exactly the thing that could not tell 17 buffers from 14
        rooms.
        """
        rooms = self.extractor_room_objects()
        return None if rooms is None else len(rooms)


def list_saves(folder):
    """Every saveN.hg in a folder, newest first, with its metadata summary."""
    from .codec import meta_read
    out = []
    if not os.path.isdir(folder):
        return out
    names = sorted(os.listdir(folder))
    # The listing is the only thing that knows how the `mf_` half of each pair
    # is spelled, and it has already been read: `"mf_" + fn` is a name this
    # code invents, and inventing it is what renamed `mf_SAVE9.HG` (Q9). One
    # map rather than an `on_disk_path` call per row, which would re-list the
    # folder once per save on every request. Keyed on `folded` and read
    # through `match_name`, so this row agrees with `safety.meta_path_for`
    # about which file is a save's metadata on a case-sensitive filesystem.
    by_folded = {}
    for n in names:
        by_folded.setdefault(folded(n), []).append(n)
    for fn in names:
        m = SAVE_RE.match(fn)
        if not m:
            continue
        p = os.path.join(folder, fn)
        st = os.stat(p)
        row = {"file": fn, "path": p, "size": st.st_size, "mtime": int(st.st_mtime),
               "slot": (int(m.group(1)) if m.group(1) else 1), "meta": None,
               "error": None,
               # Lifted out of `meta` so a caller cannot miss it. A save whose
               # mf_ records a different size is a *pair that disagrees*, which
               # is what an apply interrupted between the two writes leaves
               # behind (review one, R3); the field was computed and read by
               # nobody. None means there is no metadata to compare against.
               "sizes_match": None}
        row["pair"] = (row["slot"] + 1) // 2
        # Review 5, note 7: `SAVE_RE` is case-insensitive, so `save.hg` and
        # `SAVE.HG` are two rows -- and both of them used to pair to the one
        # `mf_save.hg`, because the folded lookup for either spelling holds
        # exactly one name and `match_name` returns it. Two distinct saves
        # sharing one `mf_` is the Q9 failure class, silently. Both rows stay
        # (they are two real files and the page lists what is there), and the
        # metadata is paired by the exact name only: a save whose own name has
        # more than one spelling in this folder is shown with no `mf_` rather
        # than with somebody else's. Windows cannot produce the case.
        ambiguous = len(by_folded.get(folded(fn), [])) > 1
        mf_name = ("mf_" + fn if ambiguous
                   else match_name(by_folded.get(folded("mf_" + fn), []),
                                   "mf_" + fn))
        mf = os.path.join(folder, mf_name) if mf_name else None
        if mf and os.path.exists(mf):
            try:
                meta = meta_read(mf)
                row["meta"] = {
                    "summary": text.safe(meta["save_summary"]),
                    "name": text.safe(meta["save_name"]),
                    "play_time": meta["play_time"],
                    "timestamp": meta["timestamp"],
                    "size_disk": meta["size_disk"],
                    "size_decompressed": meta["size_decompressed"],
                    "slot_id": meta["slot_id"],
                    "base_version": meta["base_version"],
                    "sizes_match": meta["size_disk"] == st.st_size,
                }
                row["sizes_match"] = row["meta"]["sizes_match"]
            except Exception as exc:
                row["error"] = str(exc)
        out.append(row)
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out
