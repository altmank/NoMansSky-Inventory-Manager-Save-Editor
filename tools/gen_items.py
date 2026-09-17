"""Read the game's own tables, not a third-party jar.

Inputs, all extracted from GAMEDATA\\PCBANKS by `tools/hgpak.py` and decompiled
by MBINCompiler (7.02.0.1, decompiled against game build 25233815):

    <tables>/*.MXML   the reality tables: substance, product, technology,
                      procedural technology, consumable, legacy, base building
    <lang>/*.MXML     the English localisation tables

Both directories are named on the command line. They are 128 MB of extracted
game data and are deliberately *not* in this repository: `docs/POST-PATCH.md`
steps 1 and 2 are how to produce them.

This module produces rows; it writes nothing. `gen_emit.py` merges these rows
with the table already shipped -- `nms_sorter/data/items.json`, read back as
the base of the merge -- and writes `nms_sorter/data/*.json`.

Bucket assignment for an id we have never seen is learned, not invented: every
existing id already carries a bucket, so the (kind, game category) pair it sits
under votes for where a new id of the same pair belongs. An id whose pair has no
precedent stays `unsorted`, which the sorter reads as "leave it where it is".
That is deliberately conservative -- a wrong bucket moves someone's items.
"""
import argparse
import collections
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "nms_sorter", "data")

# table file -> (kind written to the row, entry struct name)
SOURCES = [
    ("nms_reality_gcsubstancetable.MXML", "substance", "GcRealitySubstanceData"),
    ("nms_reality_gcproducttable.MXML", "product", "GcProductData"),
    ("nms_reality_gctechnologytable.MXML", "technology", "GcTechnology"),
    ("nms_reality_gcproceduraltechnologytable.MXML", "procedural-technology",
     "GcProceduralTechnologyData"),
    ("consumableitemtable.MXML", "consumable", "GcConsumableItem"),
    ("legacyitemtable.MXML", "legacy", "GcLegacyItem"),
    # Base parts and decorations occupy inventory slots like anything else, so
    # leaving these out is what made 1,941 of our rows look unknown to the game.
    ("basebuildingobjectstable.MXML", "building", "GcBaseBuildingEntry"),
    ("legacybasebuildingtable.MXML", "building", "GcBaseBuildingEntry"),
]

# proceduralproducttable.MXML carries no per-entry ids at all: its entries are
# name-generator recipes keyed by slot (Loot, Fossil, ...), not inventory rows.
# Nothing in it belongs in an item table, so it is not read.

# Most tables hang their rows off a property called Table; the two base-building
# tables call theirs Objects. Both shapes carry the entry id as an _id attribute.
ENTRY_RE = re.compile(r'<Property name="(?:Table|Objects)" value="(\w+)" _id="([^"]*)"')
PROP_RE = re.compile(r'<Property name="(\w+)" value="([^"]*)"\s*/>')


def load_localisation(lang_dir):
    """key -> English string. Later files do not override earlier ones."""
    loc = {}
    names = sorted(fn for fn in os.listdir(lang_dir) if fn.endswith(".MXML"))
    if not names:
        print("MISSING %s" % os.path.join(lang_dir, "*.MXML"))
        raise SystemExit(2)
    for fn in names:
        src = io.open(os.path.join(lang_dir, fn), encoding="utf-8").read()
        for m in re.finditer(
                r'_id="([^"]*)"\s*>\s*<Property name="Id" value="[^"]*"\s*/>\s*'
                r'<Property name="English" value="([^"]*)"', src):
            loc.setdefault(m.group(1), m.group(2))
    return loc


def unescape(s):
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&"))


def title(s):
    """The game stores some names shouting. Match the old table's casing."""
    s = unescape(s).strip()
    if s and s == s.upper() and re.search(r"[A-Z]{2}", s):
        out = []
        for w in s.split(" "):
            out.append(w if (len(w) <= 1 or not w.isalpha()) else w.capitalize())
        s = " ".join(out)
    return s


def parse_table(path, kind, struct):
    """-> [ {id, name_key, alt_key, subtitle_key, category, multiplier, kind} ]"""
    src = io.open(path, encoding="utf-8").read()
    starts = [(m.start(), m.group(1), m.group(2)) for m in ENTRY_RE.finditer(src)
              if m.group(1) == struct]
    rows = []
    for n, (pos, _s, ident) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(src)
        blk = src[pos:end]
        # First occurrence wins. dict() over findall() would keep the LAST,
        # which on any entry carrying a Requirements list is the requirement's
        # ID rather than the entry's -- silently renaming 337 technologies.
        props = {}
        for k, v in PROP_RE.findall(blk):
            props.setdefault(k, v)
        # The _id attribute is MBINCompiler's own key for the entry; trust it
        # over anything scraped out of the body.
        iid = ident or props.get("ID") or props.get("Id")
        if not iid:
            continue
        cat = ""
        for key in ("ProductCategory", "SubstanceCategory", "TechnologyCategory",
                    "ConsumableCategory", "BaseBuildingDecorationType"):
            m = re.search(r'<Property name="%s" value="([^"]*)"' % key, blk)
            if m:
                cat = m.group(1).upper()
                break
        mult = props.get("StackMultiplier", "")
        rows.append({
            "id": iid,
            "name_key": (props.get("NameLower") or props.get("Name")
                         or props.get("DisplayName") or ""),
            "alt_key": props.get("Name") or "",
            "subtitle_key": props.get("Subtitle", ""),
            "category": cat,
            "multiplier": mult,
            "kind": kind,
        })
    return rows


def read_shipped_base(data_dir=DATA):
    """The table already shipped -> (old, old_bucket, procedural).

    `nms_sorter/data/items.json` is the base of the merge: it is the table this
    build ships, so it is the only honest answer to "what did we have before
    this run". The previous base was a CSV and a Lua file kept beside the
    generator, which had to be maintained in parallel with the JSON that was
    generated from it -- two answers to every question about an id, and the
    stale one was the input.

    A run is therefore read-modify-write over the shipped files, and the
    previous state is in git rather than in a copy on disk.
    """
    path = os.path.join(data_dir, "items.json")
    if not os.path.exists(path):
        print("MISSING %s" % path)
        raise SystemExit(2)
    shipped = json.load(io.open(path, encoding="utf-8"))
    old = dict((iid, {"name": r["name"], "kind": r["kind"],
                      "category": r["category"], "subtitle": r["subtitle"],
                      "multiplier": r["multiplier"]})
               for iid, r in shipped.items())
    old_bucket = dict((iid, r["bucket"]) for iid, r in shipped.items())
    procedural = set(iid for iid, r in shipped.items() if r["procedural"])
    return old, old_bucket, procedural


def collect(tables_dir, lang_dir, data_dir=DATA, quiet=False):
    """-> (rows, assigned, old, old_bucket). Exits non-zero on a missing input.

    A missing table used to print `MISSING x` and carry on, which produced a
    complete-looking run that had silently dropped every base part in the game.
    A generator that cannot see its inputs must not produce output.
    """
    def say(*a):
        if not quiet:
            print(*a)

    if not os.path.isdir(tables_dir):
        print("MISSING %s (pass --tables DIR)" % tables_dir)
        raise SystemExit(2)
    if not os.path.isdir(lang_dir):
        print("MISSING %s (pass --lang DIR)" % lang_dir)
        raise SystemExit(2)

    missing = [fn for fn, _k, _s in SOURCES
               if not os.path.exists(os.path.join(tables_dir, fn))]
    if missing:
        for fn in missing:
            print("MISSING %s" % os.path.join(tables_dir, fn))
        print("refusing to write a table with %d of %d sources missing"
              % (len(missing), len(SOURCES)))
        raise SystemExit(2)

    loc = load_localisation(lang_dir)
    say("localisation keys: %d" % len(loc))

    rows, seen = [], set()
    for fn, kind, struct in SOURCES:
        got = parse_table(os.path.join(tables_dir, fn), kind, struct)
        new = [r for r in got if r["id"] not in seen]
        for r in new:
            seen.add(r["id"])
        rows.extend(new)
        say("  %-46s %5d entries, %5d new" % (fn, len(got), len(new)))

    KEYISH = re.compile(r"^[A-Z0-9_]+$")
    for r in rows:
        nm = loc.get(r["name_key"]) or loc.get(r["alt_key"]) or ""
        # A name that is still an all-caps token is a localisation key that did
        # not resolve, not a display name. Try the key itself against the table
        # before giving up; several rows in the old CSV carry raw keys because
        # the jar had no localisation to resolve them with.
        if nm and KEYISH.match(nm):
            nm = loc.get(nm, nm)
        if not nm or KEYISH.match(nm):
            # Base-building entries carry no Name property at all, so the id is
            # all we have. The game's own keys follow a small set of shapes;
            # try them before writing an id into a name column.
            for shape in ("UI_%s_NAME_L", "%s_NAME_L", "UI_%s_NAME", "%s_NAME",
                          "BLD_%s_NAME_L", "BLD_%s_NAME"):
                hit = loc.get(shape % r["id"])
                if hit:
                    nm = hit
                    break
        r["name"] = title(nm)
        r["subtitle"] = unescape(loc.get(r["subtitle_key"], "")).strip()

    named = sum(1 for r in rows if r["name"])
    say("\n%d ids, %d with an English name (%d unresolved)"
        % (len(rows), named, len(rows) - named))

    # ---- carry the existing bucket assignments over ----
    old, old_bucket, _procedural = read_shipped_base(data_dir)

    # learn (kind, category) -> bucket from what is already assigned
    votes = collections.defaultdict(collections.Counter)
    for iid, b in old_bucket.items():
        o = old.get(iid)
        if o and b != "unsorted":
            votes[(o["kind"], o["category"])][b] += 1
    learned = dict((k, c.most_common(1)[0][0]) for k, c in votes.items())
    say("learned %d (kind, category) -> bucket rules" % len(learned))

    assigned, kept, guessed, unsorted = {}, 0, 0, 0
    for r in rows:
        b = old_bucket.get(r["id"])
        if b and b != "unsorted":
            kept += 1
        else:
            b = learned.get((r["kind"], r["category"]))
            if b:
                guessed += 1
            else:
                b = "unsorted"
                unsorted += 1
        assigned[r["id"]] = b
    say("buckets: %d kept, %d learned from their category, %d left unsorted"
        % (kept, guessed, unsorted))
    return rows, assigned, old, old_bucket


# `gen_emit` calls this for the data it returns, not for a side effect.
main = collect


def _cli(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tables", required=True, metavar="DIR",
                    help="directory of decompiled reality tables (*.MXML)")
    ap.add_argument("--lang", required=True, metavar="DIR",
                    help="directory of decompiled English localisation (*.MXML)")
    ap.add_argument("--base", default=DATA, metavar="DIR",
                    help="the shipped table to carry buckets over from "
                         "(default: nms_sorter/data)")
    a = ap.parse_args(argv)
    collect(a.tables, a.lang, a.base)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
