"""Build the synthetic save the test suite runs against, plus its variants.

Everything here goes through the real codec, so a fixture is a real save file:
LZ4-framed, obfuscated key names, `RawFloat` number spellings preserved, and a
procedural item id that is not valid UTF-8. That last detail is the reason the
codec exists and the reason the fixture is built rather than checked in.

    python tools/make_fixture.py OUT_DIR [--variant NAME]
    python tools/make_fixture.py --list

Each run writes `save9.hg` and a matching `mf_save9.hg` into OUT_DIR. The
metadata is genuine: XXTEA-encrypted with the slot key `save9.hg` derives, with
the two size fields at 0x38/0x3C filled in from the file that was just written,
so `safety.apply_plan` step 9 is exercised against it.

The variants are deltas on the base builder, no more. They exist so a later
ticket can point a refusal test at one file instead of hand-rolling a save:

    base              the save `selftest.py` always built
    no-freighter      `FreighterInventory` absent entirely
    no-ships          `ShipOwnership: []`
    twelve-chests     Chest1..Chest12Inventory (the model knows only ten)
    zero-chests       no Chest*Inventory at all
    expedition        a non-empty CommonStateData.SeasonData.Inventory and a
                      non-zero season word in the mf_
    version-4800      root `Version: 4800`
    low-limits        InventoryStackLimitsDifficulty: "Low"
    mf-format-2003    a 384-byte mf_ whose format word is 2003 (real saves do
                      carry this: mf_save6.hg in the operator's corpus is one)
    mf-hash-set       a 432-byte mf_ with the spooky/sha256 fields non-zero
    pre-waypoint      `PlayerStateData` at the document root with no
                      `BaseContext` above it, the way save.hg and save2.hg in
                      the operator's corpus (Version 4645) are really written
    empty-ship-slots  four `ShipOwnership` entries: the starter ship, a slot
                      that is zeros all the way down (no name, no hull, no
                      cells, nothing in it -- ten of the twelve on the
                      operator's save3 are this), a slot with cells but no
                      hull and nothing in it, and a slot with a hull and no
                      cells. Only the first is unremarkable; the second is the
                      one nothing is drawn for
    with-exocraft     one entry in `VehicleOwnership`, so `vehicle0` exists
    no-exocraft       `VehicleOwnership: []` said out loud, so the corpus has a
                      row for it instead of it being implied by `base`
    no-corvette       `CorvetteStorageInventory` absent entirely
    all-containers    every static container carries one stack, including the
                      technology grids and the two single cells
    brand-new         hour one: a 12-cell suit holding two starting substances,
                      no ships, no chests, no freighter, no extractors
    extractor-stale   a freighter base with three `^FRE_ROOM_EXTR` rooms and
                      four core buffers, the fourth a leftover sitting 0.14 m
                      from room 2 with an older `LastUpdateTimestamp` -- the
                      shape of `save10.hg` (17 buffers against 14 rooms) small
                      enough to assert on. The buffer frame is yawed 180
                      degrees and translated away from the room frame, the way
                      the real save writes it
    normal            `DifficultyState.Preset` spelled out, at "Normal"
    relaxed           preset "Relaxed", stack limits "High"
    survival          preset "Survival", stack limits "Low"
    permadeath        preset "Permadeath", stack limits "Low"
    creative          preset "Creative", stack limits "High"

The five preset variants are the P6-2 corpus targets that can be synthesised:
GOAL.md section 4 lists Normal, Survival, Permadeath, Creative and Relaxed, and
they "differ only in `DifficultyState` preset strings and stack limits". Only
the stack-limit string is read by anything -- `itemdb.cap()` picks the stack
table from it -- so the preset words are carried for the record and for a later
banner, not because the planner branches on them.

`base` deliberately does *not* carry the `Preset` node. The preset variants are
a superset of it, so adding them here cannot move the base fixture's bytes, and
therefore cannot move a plan fingerprint anywhere else in the suite.
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if __name__ == "__main__":                      # importable without installing
    sys.path.insert(0, os.path.dirname(HERE))

from nms_sorter import codec as codecmod                             # noqa: E402
from nms_sorter import config as cfgmod                              # noqa: E402
from nms_sorter.codec import (dumps, frame_payload, load_keymap,     # noqa: E402
                              meta_encode, meta_slot)

# A real id out of the operator's ship inventory. Not valid UTF-8.
PROC_ID = b"^\x80\x80\xfd62\x95#03535".decode("utf-8", "surrogateescape")

# The fixture is always slot 9, so `meta_slot` and the sizes line up.
SAVE_NAME = "save9.hg"

#: preset name -> (DifficultyPresetType, InventoryStackLimitsDifficulty).
#: The stack-limit column is the shipped default for that preset, and it is the
#: only half anything reads. A player who moves one slider gets preset
#: "Custom"; that permutation is not synthesised, because nothing branches on
#: the word.
PRESETS = {
    "normal": ("Normal", "High"),
    "relaxed": ("Relaxed", "High"),
    "creative": ("Creative", "High"),
    "survival": ("Survival", "Low"),
    "permadeath": ("Permadeath", "Low"),
}

VARIANTS = ("base", "no-freighter", "no-ships", "twelve-chests", "zero-chests",
            "expedition", "version-4800", "low-limits", "mf-format-2003",
            "mf-hash-set", "pre-waypoint", "with-exocraft", "no-exocraft",
            "no-corvette", "all-containers", "brand-new", "empty-ship-slots",
            "extractor-stale", "freighter-two-grids") + tuple(sorted(PRESETS))

#: `extractor-stale`: where the three extractor rooms sit in the freighter
#: base's own frame, and the rigid motion that carries a room onto its core
#: buffer. Both halves are measured off the operator's `save10.hg`: rooms on an
#: 8-metre grid at y 4.0, buffers yawed 180 degrees and offset by (0, +35.3,
#: -1641.3), which is why a plain distance test between the two `Position`
#: fields matches nothing at all. The fixture carries the real geometry so a
#: registration that only works by accident fails here.
STALE_ROOMS = ((-8.0, 4.0, 35.5), (-16.0, 4.0, 35.5), (-16.0, 4.0, 27.5))
STALE_SHIFT = (0.0, 35.3, -1641.3)

#: how far the leftover buffer sits from the live one on the same room, and by
#: how much its `LastUpdateTimestamp` trails. Both are measured: on
#: `save10.hg` the three stale buffers sit 0.1414 m away -- 0.1 down in y and
#: 0.1 along z -- and were last updated 16,948 to 16,957 seconds earlier.
STALE_DRIFT = (0.0, -0.1, 0.1)
STALE_AGE = 16953


def buffer_position(room):
    """An extractor room's position in the frame `RefinerBufferKeys` uses."""
    x, y, z = room
    return (-x + STALE_SHIFT[0], y + STALE_SHIFT[1], -z + STALE_SHIFT[2])

#: the static containers `all-containers` fills, as (plain key, w, h, group,
#: inventory type). The three technology grids and the two single cells are in
#: the list on purpose: a container the model calls unsortable is only proved
#: unsortable by a plan refusing to target it while it holds something.
ALL_CONTAINER_ROWS = (
    ("Inventory_Cargo", 2, 2, "Personal", "Substance"),
    ("Inventory_TechOnly", 2, 2, "Personal", "Technology"),
    ("WeaponInventory", 2, 2, "Personal", "Technology"),
    ("FreighterInventory_Cargo", 2, 2, "Freighter", "Substance"),
    ("FreighterInventory_TechOnly", 2, 2, "Freighter", "Technology"),
    ("CorvetteStorageInventory", 2, 2, "Chest", "Substance"),
    ("CookingIngredientsInventory", 2, 2, "Chest", "Substance"),
    ("RocketLockerInventory", 2, 2, "Chest", "Substance"),
    ("FishPlatformInventory", 2, 2, "Chest", "Substance"),
    ("ChestMagicInventory", 2, 2, "Chest", "Substance"),
    ("ChestMagic2Inventory", 2, 2, "Chest", "Substance"),
    ("FishBaitBoxInventory", 1, 1, "Chest", "Substance"),
    ("FoodUnitInventory", 1, 1, "Chest", "Substance"),
)

# Fixed so two runs of the builder produce byte-identical metadata.
TIMESTAMP = 1789260767


class _Keys(object):
    """The game's key obfuscation, as a callable.

    `o()` maps a dict written with the readable names onto the three-character
    names the game actually stores. A name the map does not know is left alone
    on purpose: `Chest11Inventory` and `Chest12Inventory` have no entry in
    `savekeys.json`, and the `twelve-chests` variant needs them anyway.
    """

    def __init__(self):
        self.fwd, self.rev = load_keymap()

    def __call__(self, d):
        return dict((self.rev.get(k, k), v) for k, v in d.items())

    def name(self, plain):
        return self.rev.get(plain, plain)


def build_doc(variant="base"):
    """The decoded save document, with the game's own obfuscated key names."""
    if variant not in VARIANTS:
        raise ValueError("unknown variant %r; known: %s"
                         % (variant, ", ".join(VARIANTS)))
    o = _Keys()

    def slot(iid, amount, mx, x, y, itype="Substance", dmg=0.0):
        return o({"Type": o({"InventoryType": itype}), "Id": iid, "Amount": amount,
                  "MaxAmount": mx, "DamageFactor": codecmod.RawFloat(repr(dmg)),
                  "FullyInstalled": False, "AddedAutomatically": False,
                  "Index": o({"X": x, "Y": y})})

    def cells(w, h):
        return [o({"X": x, "Y": y}) for y in range(h) for x in range(w)]

    def inv(w, h, group, slots, name=""):
        return o({"Slots": slots, "ValidSlotIndices": cells(w, h),
                  "Class": o({"InventoryClass": "C"}),
                  "StackSizeGroup": o({"InventoryStackSizeGroup": group}),
                  "BaseStatValues": [], "SpecialSlots": [], "Width": w, "Height": h,
                  "IsCool": False, "Name": name, "Version": 1, "NumSlotsFromTech": 0})

    empty = o({"Slots": [], "ValidSlotIndices": [], "Class": o({"InventoryClass": "C"}),
               "StackSizeGroup": o({"InventoryStackSizeGroup": "Ship"}),
               "BaseStatValues": [], "SpecialSlots": [], "Width": 1, "Height": 1,
               "IsCool": False, "Name": "", "Version": 1, "NumSlotsFromTech": 0})

    suit = inv(4, 2, "Personal", [
        slot("^CATALYST1", 400, 9999, 0, 0),                       # Sodium, keep 250
        slot("^FUEL1", 300, 9999, 1, 0),                           # Carbon, keep 500
        slot("^STELLAR2", 900, 9999, 2, 0),                        # keep 500 -> chest2
        slot("^ALBUMENPEARL", 4, 20, 3, 0, "Product"),             # trade_goods
        slot("^ALBUMENPEARL", 7, 20, 0, 1, "Product"),             # duplicate: merge
        slot("^HULK1", 3, 20, 1, 1, "Product"),                    # unknown id
        # DamageFactor 1.0 on an ordinary substance: the real save carries this
        # on Viscous Fluids, Residual Goop, Faecium and Living Slime, and it does
        # not mean the item is damaged.
        slot("^SPACEGUNK1", 60, 9999, 1, 2, "Substance", 1.0),
        slot(PROC_ID, 1, 1, 2, 1, "Product"),                      # not valid UTF-8
        slot("^UP_HYP1#12345", 1, 1, 3, 1, "Technology"),          # technology
    ], name="")
    # deliberately parked in the last cell, so the tidy phase has work to do
    chest1 = inv(4, 2, "Chest", [slot("^CATALYST1", 100, 9999, 3, 1)], name="Minerals")
    chest2 = inv(4, 2, "Chest", [], name="BLD_STORAGE_NAME")
    chest3 = inv(4, 2, "Chest", [], name="")
    ship = o({"Name": "StarterShip", "Resource": o({"Filename": ""}),
              "Inventory": inv(2, 2, "Ship", [slot("^ALBUMENPEARL", 5, 10, 0, 0, "Product")]),
              "Inventory_Cargo": empty, "Inventory_TechOnly": empty,
              "Location": 0})
    def extractor_core(amount, stamp=None):
        """One `RefinerBufferData` entry: the module, and one substance slot.

        `stamp` writes `LastUpdateTimestamp`, which is how two buffers on one
        room are told apart. Omitted on the base fixture, whose core has never
        carried one -- a buffer without the field is the case
        `Extractor.last_update` answers None for.
        """
        node = {"InventoryContainer": o({
            "Slots": [slot("^MAINT_HOOVER", 1, 1, 0, 0, "Technology"),
                      slot("^STELLAR2", amount, 350, 0, 0)],
            "ValidSlotIndices": [], "Class": o({"InventoryClass": "C"}),
            "StackSizeGroup": o({"InventoryStackSizeGroup": "MaintenanceObject"}),
            "BaseStatValues": [], "SpecialSlots": [], "Width": 16, "Height": 1,
            "IsCool": False, "Name": "", "Version": 1, "NumSlotsFromTech": 0}),
            "AmountAccumulators": []}
        if stamp is not None:
            node["LastUpdateTimestamp"] = stamp
        return o(node)

    def xyz(p):
        """A position, spelled the way the game spells one."""
        return [codecmod.RawFloat(repr(round(float(v), 4))) for v in p]

    core = extractor_core(40)

    if variant == "low-limits":
        difficulty = "Low"
    elif variant in PRESETS:
        difficulty = PRESETS[variant][1]
    else:
        difficulty = "High"

    # `Preset` only appears on the preset variants: see the module docstring for
    # why `base` must keep the document it always had.
    dstate = {"Settings": o({"InventoryStackLimits": o({
        "InventoryStackLimitsDifficulty": difficulty})})}
    if variant in PRESETS:
        preset = o({"DifficultyPresetType": PRESETS[variant][0]})
        # A real save carries all three, and the two "used" ones are how the
        # game remembers that a Permadeath run was once played on Normal.
        dstate["Preset"] = preset
        dstate["EasiestUsedPreset"] = preset
        dstate["HardestUsedPreset"] = preset

    ps = o({
        "Units": 12345, "PlayerFreighterName": "Fixture基地",
        # Which ship and which exocraft the player is currently in. Real saves
        # carry both, and they are the only true thing the save says about a
        # vessel the player never named.
        "PrimaryShip": 0, "PrimaryVehicle": 0,
        "Inventory": suit,
        "Inventory_Cargo": empty, "Inventory_TechOnly": empty, "WeaponInventory": empty,
        "FreighterInventory": empty, "FreighterInventory_Cargo": empty,
        "FreighterInventory_TechOnly": empty, "CorvetteStorageInventory": empty,
        "CookingIngredientsInventory": empty, "RocketLockerInventory": empty,
        "FishPlatformInventory": empty, "ChestMagicInventory": empty,
        "ChestMagic2Inventory": empty, "FishBaitBoxInventory": empty,
        "FoodUnitInventory": empty,
        "Chest1Inventory": chest1, "Chest2Inventory": chest2, "Chest3Inventory": chest3,
        "ShipOwnership": [ship], "VehicleOwnership": [],
        "RefinerBufferData": [core],
        "RefinerBufferKeys": [o({"Position": [codecmod.RawFloat("1.0")] * 3,
                                 "Location": 2})],
        "PersistentPlayerBases": [],
        "DifficultyState": o(dstate),
    })
    for i in range(4, 11):
        ps[o.name("Chest%dInventory" % i)] = empty

    # ---- variant deltas -------------------------------------------------
    if variant == "no-freighter":
        del ps[o.name("FreighterInventory")]
    elif variant == "no-ships":
        ps[o.name("ShipOwnership")] = []
    elif variant == "twelve-chests":
        for i in (11, 12):
            ps[o.name("Chest%dInventory" % i)] = inv(4, 2, "Chest", [], name="")
    elif variant == "zero-chests":
        for i in range(1, 11):
            ps.pop(o.name("Chest%dInventory" % i), None)
    elif variant == "empty-ship-slots":
        # A slot the player does not own is all zeros; a ship with cells and
        # nothing in them, and a ship with a hull and no cells, are both
        # ships. save6 slot 0 and save10 slot 2 are the second kind and save10
        # slot 1 is the third, so all three shapes are real.
        blank = o({"Name": "", "Resource": o({"Filename": ""}),
                   "Inventory": empty, "Inventory_Cargo": empty,
                   "Inventory_TechOnly": empty, "Location": 0})
        cells_no_hull = o({"Name": "", "Resource": o({"Filename": ""}),
                           "Inventory": inv(5, 5, "Ship", []),
                           "Inventory_Cargo": empty,
                           "Inventory_TechOnly": empty, "Location": 0})
        hull_no_cells = o({
            "Name": "",
            "Resource": o({"Filename":
                           "MODELS/COMMON/SPACECRAFT/SHUTTLE/"
                           "SHUTTLE_PROC.SCENE.MBIN"}),
            "Inventory": empty, "Inventory_Cargo": empty,
            "Inventory_TechOnly": empty, "Location": 0})
        ps[o.name("ShipOwnership")] = [ship, blank, cells_no_hull,
                                       hull_no_cells]
    elif variant == "with-exocraft":
        ps[o.name("VehicleOwnership")] = [o({
            "Name": "",
            "Inventory": inv(3, 2, "Vehicle",
                             [slot("^CATALYST1", 30, 9999, 0, 0)]),
            "Inventory_TechOnly": inv(2, 1, "Vehicle",
                                      [slot("^UP_HYP1#12345", 1, 1, 0, 0,
                                            "Technology")])})]
    elif variant == "no-exocraft":
        # Already the base state. Spelled out so the corpus row exists on its
        # own terms: "no exocraft" is a supported shape, not an omission, and a
        # test that asserts it has to name the file it asserted it against.
        ps[o.name("VehicleOwnership")] = []
    elif variant == "no-corvette":
        del ps[o.name("CorvetteStorageInventory")]
    elif variant == "all-containers":
        for plain, w, h, group, itype in ALL_CONTAINER_ROWS:
            iid = "^UP_HYP1#12345" if itype == "Technology" else "^CATALYST1"
            amount = 1 if itype == "Technology" else 30
            ps[o.name(plain)] = inv(w, h, group,
                                    [slot(iid, amount, 9999, 0, 0, itype)])
    elif variant == "brand-new":
        # Hour one. The suit is the only allocated container in the save, and
        # it is the shape that breaks a planner that assumes a destination
        # exists: every category is routed nowhere, so nothing can move and the
        # plan has to say so rather than fail.
        keep = ("Units", "Inventory", "Inventory_Cargo", "Inventory_TechOnly",
                "WeaponInventory", "ShipOwnership", "VehicleOwnership",
                "RefinerBufferData", "RefinerBufferKeys",
                "PersistentPlayerBases", "DifficultyState")
        kept = set(o.name(k) for k in keep)
        for key in list(ps):
            if key not in kept:
                del ps[key]
        ps[o.name("Inventory")] = inv(4, 3, "Personal", [
            slot("^CATALYST1", 50, 9999, 0, 0),
            slot("^FUEL1", 100, 9999, 1, 0),
        ], name="")
        ps[o.name("ShipOwnership")] = []
        ps[o.name("RefinerBufferData")] = []
        ps[o.name("RefinerBufferKeys")] = []
    elif variant == "freighter-two-grids":
        # The one vessel that still has two grids a player can put something
        # in. Every starship and the exosuit carry an `Inventory_Cargo` with
        # no cells on all seven readable corpus saves, but save3 and save4
        # both have a real `FreighterInventory_Cargo` -- a cargo bulkhead
        # bought before Waypoint merged the other ones away -- and that is the
        # only shape where a grid sub-heading is drawn at all.
        ps[o.name("FreighterInventory")] = inv(
            4, 2, "Freighter", [slot("^CATALYST1", 40, 9999, 0, 0)])
        ps[o.name("FreighterInventory_Cargo")] = inv(
            2, 2, "Freighter", [slot("^FUEL1", 60, 9999, 0, 0)])
    elif variant == "extractor-stale":
        # Three rooms, four buffers: room 2 was rebuilt and its old buffer is
        # still in the array, 0.14 m from the live one. The leftover is written
        # *last* on purpose -- it has the higher `RefinerBufferData` index and
        # the older timestamp, so a tie-break that fell back to the index
        # instead of reading the stamp would pick exactly the wrong one.
        data, keys = [], []
        for n, room in enumerate(STALE_ROOMS):
            data.append(extractor_core(10 * (n + 1), TIMESTAMP))
            keys.append(o({"Position": xyz(buffer_position(room)),
                           "Location": 2}))
        leftover = tuple(v + STALE_DRIFT[i] for i, v
                         in enumerate(buffer_position(STALE_ROOMS[1])))
        data.append(extractor_core(99, TIMESTAMP - STALE_AGE))
        keys.append(o({"Position": xyz(leftover), "Location": 2}))
        ps[o.name("RefinerBufferData")] = data
        ps[o.name("RefinerBufferKeys")] = keys
        ps[o.name("PersistentPlayerBases")] = [o({
            "BaseType": o({"PersistentBaseTypes": "FreighterBase"}),
            "Objects": [o({"ObjectID": "^FRE_ROOM_EXTR", "Position": xyz(r)})
                        for r in STALE_ROOMS]})]

    doc = o({"Version": 4800 if variant == "version-4800" else 4735,
             "Platform": "Win|Final",
             "BaseContext": o({"PlayerStateData": ps})})
    if variant == "pre-waypoint":
        # Waypoint (4.0) wrapped the player state in BaseContext. Before that
        # the same object hung off the document root, which is why two of the
        # operator's nine saves read as a save with no containers at all.
        doc = o({"Version": 4645, "Platform": "Win|Final",
                 "PlayerStateData": ps})

    if variant == "expedition":
        # An expedition save carries its own inventory outside PlayerStateData.
        # Non-empty is the whole point: an empty SeasonData proves nothing.
        doc[o.name("CommonStateData")] = o({"SeasonData": o({
            "Inventory": inv(2, 2, "Personal",
                             [slot("^CATALYST1", 50, 9999, 0, 0)])})})
    return doc


def build_save(path, variant="base"):
    """Write the framed save to `path`. Returns the uncompressed payload."""
    payload = dumps(build_doc(variant))
    with open(path, "wb") as fh:
        fh.write(frame_payload(payload))
    return payload


def build_meta(payload_len, disk_size, filename=SAVE_NAME, variant="base"):
    """The encrypted `mf_` bytes for a save of the given two sizes.

    Everything not named here stays zero, which is what the real files look
    like: `mf_save9.hg` in the operator's corpus has both the spooky hash and
    the sha256 field all-zero, and that is why the write path may leave them
    alone. `mf-hash-set` is the fixture that stops us relying on it.
    """
    size = 384 if variant == "mf-format-2003" else 432
    fmt = 2003 if variant == "mf-format-2003" else 2004
    plain = bytearray(size)
    struct.pack_into("<II", plain, 0x00, 0xEEEEEEBE, fmt)
    if variant == "mf-hash-set":
        # 0x08..0x18 spooky, 0x18..0x38 sha256. Non-zero, deterministic.
        for i in range(0x08, 0x38):
            plain[i] = (i * 7 + 1) & 0xFF
    struct.pack_into("<II", plain, 0x38, payload_len, disk_size)
    if variant == "expedition":
        # game_mode in the low 16 bits, season in the high 16.
        struct.pack_into("<I", plain, 0x48, (1 << 16) | 1)
    struct.pack_into("<I", plain, 0x164, TIMESTAMP)
    return meta_encode(bytes(plain), meta_slot(filename))


def write_fixture(out_dir, variant="base"):
    """Write `save9.hg` + `mf_save9.hg` into `out_dir`.

    Returns {"variant", "save", "meta", "payload"}.
    """
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    save_path = os.path.join(out_dir, SAVE_NAME)
    payload = build_save(save_path, variant)
    meta_path = os.path.join(out_dir, "mf_" + SAVE_NAME)
    with open(meta_path, "wb") as fh:
        fh.write(build_meta(len(payload), os.path.getsize(save_path),
                            SAVE_NAME, variant))
    return {"variant": variant, "save": save_path, "meta": meta_path,
            "payload": payload}


def synthetic_config():
    """The config the planner tests plan against: three buckets, three keeps."""
    cfg = cfgmod.default_config()
    cfg["sources"] = ["suit", "ship0"]
    cfg["bucket_rules"] = [
        {"bucket": "raw_resources", "store": "chest1"},
        {"bucket": "refined_crafted", "store": "chest2"},
        {"bucket": "trade_goods", "store": "chest3"},
    ]
    cfg["item_rules"] = [
        {"item": "CATALYST1", "keep": 250, "priority": 10},
        {"item": "FUEL1", "keep": 500, "priority": 10},
        {"item": "STELLAR2", "store": "chest2", "keep": 500, "priority": 20},
    ]
    cfg["options"]["tidy"] = ["chest1"]
    return cfg


def main(argv):
    if "--list" in argv:
        for v in VARIANTS:
            print(v)
        return 0
    args = [a for a in argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    variant = "base"
    if "--variant" in argv:
        variant = argv[argv.index("--variant") + 1]
    if variant not in VARIANTS:
        sys.stderr.write("unknown variant %r; known: %s\n"
                         % (variant, ", ".join(VARIANTS)))
        return 2
    out = write_fixture(args[0], variant)
    # Prove it reframes before anyone builds a test on it.
    again, info = codecmod.read_payload(out["save"])
    meta = codecmod.meta_read(out["meta"])
    print("variant           %s" % out["variant"])
    print("save              %s (%d bytes on disk, %d payload)"
          % (out["save"], os.path.getsize(out["save"]), len(out["payload"])))
    print("reframes exactly  %s" % (again == out["payload"]))
    print("json byte-exact   %s" % (dumps(codecmod.loads(out["payload"]))
                                    == out["payload"]))
    print("metadata          %s (%d bytes, format %d, season %d, sizes %d/%d)"
          % (out["meta"], meta["raw_len"], meta["format"], meta["season"],
             meta["size_decompressed"], meta["size_disk"]))
    print("blocks            %d" % info["blocks"])
    return 0 if again == out["payload"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
