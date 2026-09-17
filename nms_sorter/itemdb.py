"""The item table, the bucket taxonomy and the stack-cap formula.

Five read-only JSON files, all shipped inside the package as `nms_sorter/data/`
and all loaded with `importlib.resources`, so a wheel, a zipapp and a
PyInstaller bundle read them the same way a source checkout does:

  data/items.json     id -> {name, kind, category, subtitle, multiplier,
                            bucket, procedural}
  data/buckets.json   the ordered bucket list, [{key, label, note}]
  data/stacks.json    stack size per (difficulty, group, product|substance)
  data/vehicles.json  the exocraft types, in `VehicleOwnership` order
  data/gridnames.json the game's own word for each of a vessel's grids
  data/DATA_COUNTS    the counts a regeneration is expected to produce

They are written by `tools/gen_emit.py` from a decompiled game install
(`--tables`/`--lang`), which is a maintainer's job after a game patch and
never happens at run time. There is no CSV reader and no Lua parser here: the
generator owns every input format and this module owns exactly one.

The exact counts live in `data/DATA_COUNTS` and are asserted by
`tests/test_data.py`, not here. What this module enforces is a floor -- fewer
than 17 buckets or 5,000 ids means the data did not load, which is a different
failure from "the table grew". A hard equality check here would have to be
edited in lockstep with every regeneration, and it would refuse to start the
app over a number that a test is the right place to notice.

An id the table does not know is the normal case after a game patch, not an
error. `lookup()` returns None and every caller treats that as "leave it where
it is".
"""
import json

from . import text

from importlib.resources import files as _res_files


def _read_data(name):
    """A file shipped inside `nms_sorter/data/`, read as package data.

    Not `os.path.join(dirname(__file__), "data", name)`: that spelling works in
    a source checkout and fails in a wheel, a zipapp and a PyInstaller bundle,
    which are three of the four ways this is meant to run.

    One `/` per segment, not `joinpath("data", name)`: multi-argument
    `joinpath` on a Traversable is 3.11 and later, and the declared floor is
    3.9.
    """
    return (_res_files("nms_sorter") / "data" / name).read_text(encoding="utf-8")

UNSORTED = "unsorted"
PACKED_TECH = "packed_tech"
SYSTEM_META = "system_meta"

#: Buckets nothing can be routed to, because nothing in them is ever in a
#: container. The owner: "Question about 'System and Non-inventory', if its
#: stuff that doesn't occupy a slot, then why are we able to assign it to
#: storage?"
#:
#: It cannot. `system_meta` is currencies, reputation deltas, settlement stat
#: tokens, world-object repair sockets and cooking category placeholders --
#: the game's own bookkeeping ids -- and not one of the 134 of them sits in a
#: slot on any of the seven readable corpus saves. A destination for it is a
#: row that can never fire, so it is refused rather than offered.
#:
#: `UNSORTED` is a different thing and is deliberately not here: an unsorted
#: id is a real item in a real cell that this build's table does not know, and
#: it is left alone rather than being unroutable. It keeps the handling it has
#: always had.
UNROUTABLE = (SYSTEM_META,)

# Floors, not counts. See the module docstring.
MIN_BUCKETS = 17
MIN_IDS = 5000

#: What to tell somebody whose data files will not load. Not a command: the
#: tables are generated from a decompiled game install by a maintainer, so a
#: player cannot rebuild them and being handed a command they cannot run is
#: worse than being told the truth. A missing data file is a broken install.
_REINSTALL = ("The install is incomplete: unzip the release again into an "
              "empty folder, or re-clone the repository.")

_UNSORTED_ROW = {
    "key": UNSORTED, "label": "Unsorted",
    "note": "Not in the item table. The table lags the game build, so this is "
            "a normal condition: these are reported and left exactly where they are."}


def _load(name):
    try:
        return json.loads(_read_data(name))
    except Exception as exc:
        raise RuntimeError(
            "nms_sorter/data/%s could not be read as JSON (%s: %s). %s"
            % (name, type(exc).__name__, exc, _REINSTALL))


class ItemDB(object):
    def __init__(self):
        self.buckets = []          # [{key,label,note}] in the taxonomy's own order
        self.bucket_by_key = {}
        self.by_id = {}            # stem -> bucket key
        self.procedural = set()    # stems that only ever appear hashed
        self.items = {}            # stem -> {id,name,kind,category,subtitle,multiplier}
        self.stacks = {}           # difficulty -> group -> {product,substance}
        self.custom = set()        # bucket keys added by the configuration
        self.overridden = {}       # stem -> bucket, from the configuration
        self._base_by_id = None    # the generated mapping, kept to reset against
        self._base_buckets = None
        self._load_buckets()
        self._load_items()
        self._load_stacks()

    # ---------------------------------------------------------------- load

    def _load_buckets(self):
        for b in _load("buckets.json"):
            row = {"key": b["key"], "label": b["label"], "note": b.get("note", "")}
            self.buckets.append(row)
            self.bucket_by_key[row["key"]] = row
        if len(self.buckets) < MIN_BUCKETS:
            raise RuntimeError(
                "nms_sorter/data/buckets.json holds %d buckets, fewer than the "
                "%d the taxonomy has always had: the file is truncated, or was "
                "written by a generator run that failed half way. %s"
                % (len(self.buckets), MIN_BUCKETS, _REINSTALL))
        # Not a bucket in the taxonomy: the answer for an id no table names. It
        # has a label because the UI shows it, and no entry in `buckets` because
        # nothing is ever sorted *into* it.
        self.bucket_by_key[UNSORTED] = dict(_UNSORTED_ROW)

    def _load_items(self):
        for iid, r in _load("items.json").items():
            self.items[iid] = {
                "id": iid, "name": r["name"], "kind": r["kind"],
                "category": r["category"], "subtitle": r["subtitle"],
                "multiplier": r["multiplier"],
            }
            self.by_id[iid] = r["bucket"]
            if r["procedural"]:
                self.procedural.add(iid)
        if len(self.items) < MIN_IDS:
            raise RuntimeError(
                "nms_sorter/data/items.json holds %d ids, under the floor of "
                "%d: the file is truncated, or was written by a generator run "
                "that failed half way. %s"
                % (len(self.items), MIN_IDS, _REINSTALL))
        unknown = sorted(set(self.by_id.values()) - set(self.bucket_by_key))
        if unknown:
            raise RuntimeError(
                "nms_sorter/data/items.json assigns ids to bucket(s) "
                "buckets.json does not define: %s. The two files came from "
                "different runs of the generator. %s"
                % (", ".join(unknown), _REINSTALL))

    def _load_stacks(self):
        self.stacks = _load("stacks.json")
        if "Normal" not in self.stacks or "Default" not in self.stacks["Normal"]:
            raise RuntimeError(
                "nms_sorter/data/stacks.json has no Normal/Default row, which "
                "is what every cap() call falls back to. %s" % _REINSTALL)

    # -------------------------------------------------------------- lookup

    def lookup(self, item_id):
        """Item row for a raw save id (caret and #hash tolerated). None if unknown."""
        return self.items.get(text.stem(item_id))

    def bucket_of(self, item_id):
        b = self.by_id.get(text.stem(item_id))
        if b:
            return b
        # Shape rule, not a lookup: an id with a #hash that no table names is
        # packed technology. Every other bucket answers "what is this"; this one
        # answers "what shape is this", which is all there is to go on.
        if isinstance(item_id, str) and "#" in item_id:
            return PACKED_TECH
        return UNSORTED

    # ------------------------------------------------- configuration overlay

    def apply_overrides(self, cfg):
        """Overlay a config's custom buckets and per-item category overrides.

        The generated taxonomy stays the base and is restored first every time,
        so this is idempotent and a removed override really disappears rather
        than lingering in a long-lived process.

        Applied here rather than threaded through every caller because the app
        works against exactly one configuration at a time, and an overlay that
        only half the code knows about is worse than no overlay at all.
        """
        cfg = cfg or {}
        if self._base_by_id is None:
            self._base_by_id = dict(self.by_id)
            self._base_buckets = list(self.buckets)
        self.by_id = dict(self._base_by_id)
        self.buckets = list(self._base_buckets)
        for k in list(self.custom):
            self.bucket_by_key.pop(k, None)
        self.custom = set()
        self.overridden = {}

        for b in (cfg.get("custom_buckets") or []):
            key = (b.get("key") or "").strip()
            if not key or key in self.bucket_by_key:
                continue
            row = {"key": key, "label": (b.get("label") or key).strip(),
                   "note": (b.get("note") or "").strip(), "custom": True}
            self.buckets.append(row)
            self.bucket_by_key[key] = row
            self.custom.add(key)

        for raw, bucket in (cfg.get("item_buckets") or {}).items():
            stem = text.stem(raw)
            if not stem or bucket not in self.bucket_by_key:
                continue
            if self.by_id.get(stem) == bucket:
                continue
            self.by_id[stem] = bucket
            self.overridden[stem] = bucket
        return self

    def is_custom_bucket(self, key):
        return key in self.custom

    def original_bucket_of(self, item_id):
        base = self._base_by_id if self._base_by_id is not None else self.by_id
        return base.get(text.stem(item_id), UNSORTED)

    def row_view(self, stem_id, row=None):
        """One item, with everything the items page shows about it."""
        r = row or self.items.get(stem_id) or {}
        b = self.bucket_of(stem_id)
        orig = self.original_bucket_of(stem_id)
        return {
            "id": stem_id,
            "name": r.get("name"),
            "kind": r.get("kind"),
            "category": r.get("category"),
            "subtitle": r.get("subtitle"),
            "multiplier": r.get("multiplier"),
            "bucket": b,
            "bucket_label": self.label(b),
            "original_bucket": orig,
            "original_bucket_label": self.label(orig),
            "overridden": b != orig,
            "procedural": stem_id in self.procedural,
        }

    def is_procedural(self, item_id):
        return text.stem(item_id) in self.procedural

    def label(self, bucket_key):
        b = self.bucket_by_key.get(bucket_key)
        return b["label"] if b else "Unsorted"

    def cap(self, item_id, inv_type, group, difficulty):
        """Predicted stack cap. Only a fallback: a live MaxAmount the game wrote
        beats this every time, because the game wrote it and this is a formula."""
        table = self.stacks.get(difficulty) or self.stacks.get("Normal")
        g = table.get(group) or table.get("Default")
        base = g["substance"] if inv_type == "Substance" else g["product"]
        row = self.lookup(item_id)
        mult = row["multiplier"] if row else None
        if not mult:
            # items.xml writes 0 for one-off and blueprint rows; 1 is the
            # documented assumption and it is never written without the caller
            # preferring a live MaxAmount.
            mult = 1
        return base * mult

    def search(self, query, limit=40):
        q = (query or "").strip().lower()
        if not q:
            # An empty box browses rather than refusing: the operator is picking
            # by name and may not know any id at all.
            rows = sorted(self.items.items(), key=lambda kv: kv[1]["name"].lower())
            return [self.row_view(s, r) for s, r in rows[:limit]]
        out = []
        for stem_id, row in self.items.items():
            score = None
            low_id = stem_id.lower()
            low_name = row["name"].lower()
            if low_id == q:
                score = 0
            elif low_id.startswith(q):
                score = 1
            elif low_name.startswith(q):
                score = 2
            elif q in low_id:
                score = 3
            elif q in low_name:
                score = 4
            elif q in (row.get("subtitle") or "").lower():
                score = 5
            elif q in (row.get("category") or "").lower():
                score = 6
            if score is not None:
                out.append((score, low_name, stem_id, row))
        out.sort(key=lambda t: (t[0], t[1]))
        return [self.row_view(s, r) for _, _, s, r in out[:limit]]

    def browse(self, q="", bucket=None, kind=None, only=None, offset=0, limit=200):
        """The item table, filtered and paged. Returns (rows, total).

        `only` is one of: overridden, procedural, unsorted.
        """
        ql = (q or "").strip().lower()
        rows = []
        for stem_id, r in self.items.items():
            if ql and (ql not in stem_id.lower()
                       and ql not in (r["name"] or "").lower()
                       and ql not in (r["subtitle"] or "").lower()
                       and ql not in (r["category"] or "").lower()):
                continue
            b = self.bucket_of(stem_id)
            if bucket and b != bucket:
                continue
            if kind and (r.get("kind") or "") != kind:
                continue
            if only == "overridden" and stem_id not in self.overridden:
                continue
            if only == "procedural" and stem_id not in self.procedural:
                continue
            if only == "unsorted" and b != UNSORTED:
                continue
            rows.append((r["name"] or stem_id, stem_id, r))
        rows.sort(key=lambda t: t[0].lower())
        total = len(rows)
        page = rows[offset:offset + limit]
        return [self.row_view(sid, r) for _, sid, r in page], total

    def kinds(self):
        out = {}
        for r in self.items.values():
            k = r.get("kind") or ""
            out[k] = out.get(k, 0) + 1
        return out

    def bucket_counts(self):
        out = {}
        for stem_id in self.items:
            b = self.bucket_of(stem_id)
            out[b] = out.get(b, 0) + 1
        return out

    def validate_item_id(self, raw):
        """Is this an id a rule may name? Returns (ok, message, resolved_stem)."""
        s = text.stem(raw or "")
        if not s:
            return False, "empty id", None
        if not text.is_clean_ascii(s):
            return False, "not printable ASCII; rules are written by hand and ids "\
                          "with raw bytes cannot be typed reliably", None
        if s in self.items:
            return True, None, s
        return (False,
                "no item in the table has this id -- a typo, or content newer than "
                "the item table. Ids come from the table or from your own inventory, "
                "never from a display name.", None)


def data_version():
    """`data/DATA_VERSION` as a dict of key=value lines.

    The game build belongs on screen: a table that lags the installed build is
    the normal state of this tool, and the health panel showing which build the
    table came from is the difference between "unsorted" being explainable and
    being a mystery.
    """
    out = {}
    for line in _read_data("DATA_VERSION").splitlines():
        k, _, v = line.strip().partition("=")
        if k:
            out[k] = v
    return out


#: Fewer than six exocraft types means the file did not load: the game has
#: shipped six nameable ones since Living Ship, and a seventh slot. A floor,
#: like MIN_IDS, rather than an equality -- a patch adding a vehicle must not
#: stop the app.
MIN_VEHICLES = 6

_VEHICLES = None


def vehicles():
    """`data/vehicles.json` as `[{index, type, title_key, name}]`.

    The exocraft types in the order `PlayerStateData/VehicleOwnership` writes
    them, so slot 2 can be called a Colossus on the authority of the game's own
    `GcVehicleType` rather than on the authority of its grid being the biggest.
    `savemodel.vehicle_name` is the only caller.

    A bad or short file is not fatal: the caller falls back to "Exocraft N",
    which is what the page said before this file existed.
    """
    global _VEHICLES
    if _VEHICLES is None:
        try:
            rows = _load("vehicles.json")
        except Exception:
            rows = []
        if not isinstance(rows, list) or len(rows) < MIN_VEHICLES:
            rows = []
        _VEHICLES = [r for r in rows if isinstance(r, dict)]
    return _VEHICLES


#: The seven rows `gridnames.json` ships: four vessel kinds and their cargo
#: grids. A floor rather than an equality, like `MIN_VEHICLES`.
MIN_GRID_NAMES = 7

_GRID_NAMES = None


def grid_names():
    """`data/gridnames.json` as `{(vessel kind, grid role): name}`.

    The game's own word for each of a vessel's inventory grids, composed by
    the generator out of the game's own localisation keys. `savemodel
    .grid_title` is the only caller and it carries the fallback for a file
    that did not load.
    """
    global _GRID_NAMES
    if _GRID_NAMES is None:
        try:
            rows = _load("gridnames.json")
        except Exception:
            rows = []
        if not isinstance(rows, list) or len(rows) < MIN_GRID_NAMES:
            rows = []
        out = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            name = r.get("name")
            if isinstance(name, str) and name.strip():
                out[(r.get("vessel"), r.get("grid"))] = name.strip()
        _GRID_NAMES = out
    return _GRID_NAMES


def data_counts():
    """`data/DATA_COUNTS` as the generator wrote it.

    Nothing at runtime depends on these numbers; `tests/test_data.py` asserts
    the loaded table against them, which is what makes a silent shrink in the
    table a test failure rather than a smaller sorted inventory.
    """
    return _load("DATA_COUNTS")


_DB = None


def db():
    global _DB
    if _DB is None:
        _DB = ItemDB()
    return _DB
