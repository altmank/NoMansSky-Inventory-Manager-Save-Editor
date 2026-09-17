"""The rules engine: turn a save plus a config into a plan.

Structure, and the reason for it:

  * Planning runs over a *virtual* copy of every container that wraps the real
    JSON nodes. The virtual state is updated as the plan is built, so a plan
    that moves three things into one chest reports the third against the space
    the first two consumed. That is what makes the report arithmetically
    honest rather than three independent guesses.
  * The same pass produces a `commit()` that writes the result into the real
    document. Nothing else mutates a save.
  * A plan carries a fingerprint. `apply` re-plans from the exact bytes it is
    about to rewrite and refuses unless the fingerprint matches what the
    operator was shown. A plan is therefore never applied to a world it was
    not computed against.

Phases, in the only order that works (see docs/RULES.md): merge, then move,
then tidy. Merging first is what makes a duplicated item movable at all;
tidying last is what leaves the result readable.
"""
import copy
import hashlib

from . import text
from . import naming
from .config import config_hash
from .itemdb import db, UNROUTABLE, UNSORTED
from .savemodel import is_maintenance_slot

DEFAULT_PRIORITY = 100

#: how much of the digest a person is shown and asked to compare by eye
SHORT_FINGERPRINT = 8


def _stores_of(rule):
    """A rule's destinations, in order. `store` is one or a list; `stores` is
    the older spelling of the same chain.

    A bucket or an item may name more than one container. They are filled in the
    order given: the first takes what it can hold, the next takes the overflow.
    Config v2 writes `store: [a, b]`; v1 wrote `store: a, stores: [b]`. Both are
    read here, because this is the one function that decides what "where does it
    go" means and a second reader is a second answer.
    """
    store = rule.get("store")
    first = list(store) if isinstance(store, (list, tuple)) else [store]
    out = []
    for v in (first + list(rule.get("stores") or [])):
        if v and v not in out:
            out.append(v)
    return out


# ---------------------------------------------------------------------------
# virtual state
# ---------------------------------------------------------------------------


class VSlot(object):
    def __init__(self, cont, idx, slot):
        self.cont = cont
        self.idx = idx                    # position in the original Slots array
        self.node = slot.node
        self.raw = slot.id
        self.stem = text.stem(self.raw)
        self.amount = slot.amount
        self.max = slot.max_amount
        self.itype = slot.inv_type
        self.damage = slot.damage
        self.cell = slot.cell
        self._ident = slot.identity()
        self.removed = False
        self.created = False
        self.source_node = None           # for a created stack: the node copied

    @property
    def ident(self):
        return self._ident

    @property
    def ident_cross(self):
        """Identity for a merge *across* containers.

        MaxAmount is a property of the destination's inventory group, not of
        the item: the same product is capped at 10 in the exosuit and 20 in a
        chest and the game wrote both numbers. Inside one container it must
        match; across two it is expected to differ.
        """
        return tuple((k, v) for k, v in self._ident if k != "MaxAmount")

    def row(self, idb):
        r = idb.lookup(self.raw)
        return {"i": self.idx, "id": text.safe(self.raw), "stem": text.safe(self.stem),
                "name": r["name"] if r else None, "amount": self.amount,
                "max": self.max, "cell": list(self.cell), "type": self.itype,
                "bucket": idb.bucket_of(self.raw)}


class VCont(object):
    drain_only = False
    rename_to = None

    def __init__(self, cont, idb, difficulty):
        self.c = cont
        self.key = cont.key
        self.label = cont.display_name()
        self.group = cont.group
        self.valid = list(cont.valid)
        self.is_tech = cont.is_tech
        self.sortable = cont.sortable
        self.allocated = cont.allocated
        self.drain_only = getattr(cont, "drain_only", False)
        self.idb = idb
        self.difficulty = difficulty
        self.slots = [VSlot(self, i, s) for i, s in enumerate(cont.slots())]
        self.before = self.snapshot()
        self.touched = False

    # ------------------------------------------------------------- queries

    def live(self):
        return [s for s in self.slots if not s.removed]

    def occupied(self):
        return set(s.cell for s in self.live())

    def free_cells(self):
        taken = self.occupied()
        return [c for c in self.valid if c not in taken]

    def total(self, stem):
        return sum(s.amount for s in self.live() if s.stem == stem)

    def maintenance(self, s):
        """Is this virtual slot the machine itself rather than inventory?

        The same predicate `Container.view` and the source loop use. It was
        applied to both of those and not to this third consumer, so the Save
        card read "5 substances" while the plan's "What each container ends up
        holding" table read 6 stacks out of 0 valid cells for the same core,
        with `Extractor Unit` named in `before` and in `after` (review 4,
        finding 7).
        """
        return is_maintenance_slot(self.drain_only, self.group, s.itype)

    def valid_cells(self):
        """How many cells this container has, as a number to show a person.

        A drain-only core's `ValidSlotIndices` is empty and its slots are
        real, so the array is the thing that lies. The same fallback
        `Container.view` got, over the same `before` state, so the card and
        the plan's table cannot disagree.
        """
        if self.drain_only and not self.valid:
            return len(self.before)
        return len(self.valid)

    def snapshot(self):
        out = []
        for s in self.live():
            if self.maintenance(s):
                continue
            r = self.idb.lookup(s.raw)
            out.append({"id": text.safe(s.raw), "name": r["name"] if r else None,
                        "amount": s.amount, "max": s.max, "cell": list(s.cell),
                        "bucket": self.idb.bucket_of(s.raw)})
        out.sort(key=lambda x: (x["cell"][1], x["cell"][0]))
        return out

    def predicted_cap(self, raw, itype):
        return self.idb.cap(raw, itype, self.group, self.difficulty)

    # ------------------------------------------------------------ mutation

    def add_stack(self, src_slot, cell, amount, cap):
        v = VSlot.__new__(VSlot)
        v.cont = self
        v.idx = None
        v.node = None
        v.raw = src_slot.raw
        v.stem = src_slot.stem
        v.amount = amount
        v.max = cap
        v.itype = src_slot.itype
        v.damage = src_slot.damage
        v.cell = cell
        v._ident = src_slot.ident
        v.removed = False
        v.created = True
        v.source_node = src_slot.node
        self.slots.append(v)
        self.touched = True
        return v

    # -------------------------------------------------------------- commit

    def commit(self, d):
        """Write the virtual state back into the real Slots array."""
        if not self.touched:
            return
        if self.rename_to is not None:
            d.set(self.c.node, "Name", self.rename_to)
        out = []
        for s in self.slots:
            if s.removed:
                continue
            if s.created:
                node = copy.deepcopy(s.source_node)
                d.set(node, "Amount", int(s.amount))
                d.set(node, "MaxAmount", int(s.max))
                ix = d.get(node, "Index")
                d.set(ix, "X", int(s.cell[0]))
                d.set(ix, "Y", int(s.cell[1]))
            else:
                node = s.node
                d.set(node, "Amount", int(s.amount))
                ix = d.get(node, "Index")
                d.set(ix, "X", int(s.cell[0]))
                d.set(ix, "Y", int(s.cell[1]))
            out.append(node)
        slots = d.get(self.c.node, "Slots")
        slots[:] = out


# ---------------------------------------------------------------------------
# rule resolution
# ---------------------------------------------------------------------------


class Rules(object):
    def __init__(self, cfg, idb):
        self.cfg = cfg
        self.idb = idb
        self.never_move = set(text.stem(x) for x in (cfg.get("never_move") or []))
        self.never_buckets = set(cfg.get("never_buckets") or [])
        items = list(enumerate(cfg.get("item_rules") or []))
        items.sort(key=lambda t: (t[1].get("priority", DEFAULT_PRIORITY), t[0]))
        self.item_rules = items
        self.bucket_rules = list(enumerate(cfg.get("bucket_rules") or []))
        self.fired = set()

    def match_item_rule(self, raw):
        """An exact id (with its #hash) beats the stem, so one roll can be
        pinned without pinning the family."""
        plain = text.strip_caret(raw)
        stem = text.stem(raw)
        for i, rule in self.item_rules:
            target = (rule.get("item") or "").strip()
            if not target:
                continue
            if target == plain or target == stem:
                return i, rule
        return None, None

    def decide(self, raw, src_key):
        """-> dict(dst, keep, fill, move, pin, why[], item_rule, bucket_rule)"""
        idb = self.idb
        stem = text.stem(raw)
        bucket = idb.bucket_of(raw)
        d = {"dst": None, "dsts": [], "keep": 0, "keep_in": None, "fill": None,
             "leg_fill": {}, "stock": None, "stock_in": None, "move": None,
             "pin": False, "why": [], "bucket": bucket, "item_rule": None,
             "bucket_rule": None}

        if stem in self.never_move:
            d["pin"] = True
            d["why"].append("pinned by never_move")
            return d

        ri, rule = self.match_item_rule(raw)
        if rule is not None:
            self.fired.add(ri)
            d["item_rule"] = ri
            prio = rule.get("priority", DEFAULT_PRIORITY)
            # The item's *name*, with the id only where the table has none
            # for it: this sentence is the "decided by" column, the one place
            # a player is asked to read carefully before pressing Apply, and
            # it read `item rule 1 (priority 10) on CATALYST1`.
            target = (rule.get("item") or "").strip()
            row = idb.lookup(target)
            desc = "item rule %d (priority %d) on %s" % (
                ri + 1, prio, (row["name"] if row and row.get("name")
                               else target))
            # `stock` is the two-sided one: top this container up to N from every
            # source, hold it there when draining it, and let the surplus carry on
            # to wherever the item would otherwise go. keep and fill are each one
            # half of that, and neither can express it alone. Tested before the
            # pin check, because a stock rule names no `store` and would
            # otherwise read as "no destination, therefore pinned".
            stocked = rule.get("stock") is not None and rule.get("stock_in")
            if stocked:
                n = int(rule["stock"])
                where = rule["stock_in"]
                d["stock"], d["stock_in"] = n, where
                d["keep"], d["keep_in"] = n, where
                d["leg_fill"][where] = n
                tail = [t for t in _stores_of(rule) if t != where]
                desc2 = "%s: stock %d in %s" % (desc, n, where)
                if tail:
                    d["dsts"] = [where] + tail
                    d["dst"] = d["dsts"][0]
                    d["why"].append(desc2 + ", surplus to " + ", ".join(tail))
                    return d
                # No explicit overflow target, so fall past this block and let the
                # bucket rules choose one; the stocked container is put in front
                # of whatever they pick.
                d["why"].append(desc2 + ", surplus follows the item's category")

            elif rule.get("pin") or (not _stores_of(rule)
                                     and rule.get("keep") is None):
                d["pin"] = True
                d["why"].append(desc + ": pinned, nothing moves it")
                return d

            if not stocked and rule.get("keep") is not None:
                d["keep"] = int(rule["keep"])
                # A floor with no `keep_in` guards every source it meets, which
                # is rarely what someone means by "keep 100 on me". Naming a
                # container scopes it to that one; the others drain in full.
                d["keep_in"] = rule.get("keep_in") or None
            if rule.get("move") is not None:
                d["move"] = int(rule["move"])
            if not stocked and _stores_of(rule):
                d["dsts"] = _stores_of(rule)
                d["dst"] = d["dsts"][0]
                if rule.get("fill") is not None:
                    d["fill"] = int(rule["fill"])
                bits = []
                if d["keep"]:
                    bits.append("keep %d in %s" % (d["keep"], d["keep_in"] or src_key))
                if d["fill"] is not None:
                    bits.append("fill %s to %d" % (d["dst"], d["fill"]))
                if d["move"] is not None:
                    bits.append("at most %d this run" % d["move"])
                if len(d["dsts"]) > 1:
                    bits.append("overflows to " + ", ".join(d["dsts"][1:]))
                d["why"].append(desc + " -> " + d["dst"] +
                                ((" (" + ", ".join(bits) + ")") if bits else ""))
                return d
            # keep with no store: reserve, then fall through to the buckets. A
            # stocked rule has already said where its floor is and that the
            # surplus follows the category, so saying it again here -- in the
            # vocabulary of bucket rules that may not even exist -- reads as a
            # second, contradictory decision.
            if not stocked:
                d["why"].append(desc + ": keep %d in %s, the surplus falls "
                                       "through to the bucket rules"
                                % (d["keep"], d["keep_in"] or src_key))

        # Every rule naming this bucket contributes, in list order. The first is
        # filled, then the next: a repeat is an overflow chain, not a shadow.
        # A stocked container always leads, whatever the buckets say.
        # Two lists and two rule indices, not one of each: whichever list ends
        # up supplying the chain has to be the one that names the rule. Sharing
        # `first_rule` meant a `*` catch-all listed before the exact rule named
        # the catch-all's number while the chain came from the exact rule --
        # sending the operator to read a rule that had nothing to do with it.
        chain, exact_rule = [], None
        catch_all, catch_rule = [], None
        for bi, brule in self.bucket_rules:
            b = brule.get("bucket")
            if b == bucket:
                for st in _stores_of(brule):
                    if st not in chain:
                        chain.append(st)
                        if exact_rule is None:
                            exact_rule = bi
            elif b == "*" and bucket not in self.never_buckets:
                for st in _stores_of(brule):
                    if st not in catch_all:
                        catch_all.append(st)
                        if catch_rule is None:
                            catch_rule = bi
        first_rule = exact_rule
        if not chain and catch_all:
            chain = catch_all
            first_rule = catch_rule
        if d["stock_in"]:
            chain = [d["stock_in"]] + [c for c in chain if c != d["stock_in"]]
        if chain:
            d["dsts"] = chain
            d["dst"] = chain[0]
            tail = (", overflowing to " + ", ".join(chain[1:])) if len(chain) > 1 else ""
            if first_rule is None:
                # The only way here is a stocked container leading an otherwise
                # empty chain: no bucket rule contributed anything. Numbering a
                # rule that does not exist ("bucket rule 1") was worse than
                # saying nothing, because it sent the operator looking for it.
                d["why"].append("stocked in %s; no category rule for the "
                                "surplus%s" % (d["stock_in"], tail))
            else:
                d["bucket_rule"] = first_rule
                d["why"].append("bucket rule %d: %s -> %s%s"
                                % (first_rule + 1, idb.label(bucket),
                                   d["dst"], tail))
            return d

        if bucket in self.never_buckets:
            d["why"].append("bucket %s is in never_buckets and no rule names it "
                            "explicitly" % bucket)
        else:
            d["why"].append("no bucket rule matches %s" % idb.label(bucket))
        return d


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


class Plan(object):
    def __init__(self):
        self.rows = []
        self.skips = []
        self.notes = []
        self.unknown = []
        self.diff = []
        self.totals = {}
        self.fingerprint = None         # the 8 hex characters a person reads
        self.fingerprint_full = None    # the 64 the write path compares
        self.idle_rules = []
        # The container keys this plan changes, in diff order. The backup
        # manifest reads it (`safety.write_manifest`), and it used to be set
        # by the write path on its way past, which meant a plan that was never
        # applied could not say what it would have touched.
        self.containers = []

    def as_dict(self):
        out = {
            "rows": self.rows, "skips": self.skips, "notes": self.notes,
            "unknown": self.unknown, "diff": self.diff, "totals": self.totals,
            "fingerprint": self.fingerprint,
            "idle_rules": self.idle_rules,
        }
        if self.fingerprint_full:
            # Absent rather than null on a plan nobody fingerprinted: a client
            # that sees the key can rely on it being a digest.
            out["fingerprint_full"] = self.fingerprint_full
        return out


def _note(plan, level, msg):
    plan.notes.append({"level": level, "message": msg})


def build_plan(save, cfg):
    """Plan the run. Returns (plan, commit) -- `commit()` writes it into `save`.

    Raises `savemodel.SaveGate` before doing any work when the save carries a
    `refuse`-level gate. An expedition is the one that exists: planning it
    would print a plan over an inventory that may not be the live one, and a
    plan the operator can read and approve is the thing an apply gate is
    supposed to protect. A `warn`-level gate -- a save version outside the
    range this build was verified on -- becomes a plan note instead, because
    reading a save is how the corpus that would widen the range gets collected.
    A `note`-level gate -- no `mf_` to confirm that this is not an expedition
    -- becomes the same plan note and does not stop the apply either.
    """
    from .savemodel import SaveGate
    gates = save.gates() if hasattr(save, "gates") else []
    for g in gates:
        if g["level"] == "refuse":
            raise SaveGate(g["message"])
    idb = db().apply_overrides(cfg)
    difficulty = save.difficulty()
    conts = save.container_map()
    # The extractor cores join the map as drain-only containers, so a rule can
    # name one as a source. They are never a destination; see savemodel.
    for c in save.extractor_containers():
        conts.setdefault(c.key, c)
    # The cores are numbered by the room each one sits in, so "extractor7" is
    # the seventh room whatever the buffer array does. What is left to say at
    # plan level is only what could *not* be identified -- a room whose buffer
    # was not found, or a buffer belonging to no room -- because that is the
    # case where a core the save holds is not on offer. A stale buffer from a
    # rebuilt room is not that: it is excluded on purpose and the live one is
    # right here. Said once rather than refused: the save is still sortable.
    mismatch = save.extractor_issue
    opts = cfg.get("options") or {}
    merge_dupes = bool(opts.get("merge_duplicates", True))
    create_stacks = bool(opts.get("create_stacks", True))
    min_source = int(opts.get("min_source", 1))
    max_moves = int(opts.get("max_moves", 200))
    max_new = int(opts.get("max_new_stacks", 80))

    V = dict((k, VCont(c, idb, difficulty)) for k, c in conts.items())
    rules = Rules(cfg, idb)
    plan = Plan()
    new_stacks_used = [0]
    if mismatch:
        _note(plan, "warning", mismatch)
    # A `warn` gate is a banner, not a refusal: the plan is printed, and the
    # sentence says that apply will not run. `safety.apply_plan` step 1b is
    # the half that refuses, so the promise this note makes is kept somewhere
    # other than in the note. A `note` gate is a banner whose apply *does*
    # run -- a caveat that is worth stating and is not a reason to stop -- and
    # it reads the same to the page, which renders anything that is not
    # `refuse` as a warning.
    for g in gates:
        if g["level"] in ("warn", "note"):
            _note(plan, "warning", g["message"])

    # ------------------------------------------------------------- phase 1
    if merge_dupes:
        for key in sorted(V):
            v = V[key]
            if not v.sortable:
                continue
            groups = {}
            for s in v.live():
                groups.setdefault(s.ident, []).append(s)
            for ident, members in groups.items():
                if len(members) < 2:
                    continue
                first = members[0]
                name = (idb.lookup(first.raw) or {}).get("name")
                base = {"op": "merge", "key": key, "container": v.label,
                        "id": text.safe(first.raw), "name": name}
                if first.itype == "Technology":
                    plan.skips.append(dict(base, op="refuse", reason=(
                        "technology: Amount is a charge level, not a count, so "
                        "combining two stacks is meaningless")))
                    continue
                if any(m.damage > 0 for m in members):
                    plan.skips.append(dict(base, op="refuse", reason=(
                        "one of these stacks is damaged; merging would hide the damage")))
                    continue
                if all(m.amount >= m.max for m in members):
                    continue
                total = sum(m.amount for m in members)
                before = [{"i": m.idx, "cell": list(m.cell), "before": m.amount,
                           "max": m.max} for m in members]
                left = total
                removed = []
                for m in members:
                    take = min(m.max, left)
                    m.amount = take
                    left -= take
                    if take == 0:
                        m.removed = True
                        removed.append({"i": m.idx, "cell": list(m.cell)})
                if left:
                    plan.skips.append(dict(base, op="refuse", reason=(
                        "%d units would not fit back into these stacks" % left)))
                    for m, b in zip(members, before):
                        m.amount = b["before"]
                        m.removed = False
                    continue
                for m, b in zip(members, before):
                    b["after"] = m.amount
                if not removed and all(b["after"] == b["before"] for b in before):
                    # Already as consolidated as pouring can make it -- the first
                    # stack is full and the remainder is where it would land
                    # anyway. Writing this would change no byte.
                    continue
                v.touched = True
                plan.rows.append(dict(base, stacks=before, removed=removed,
                                      total=total, frees=len(removed),
                                      why=["%d stacks of the same item in one container; "
                                           "combining them frees %d cell(s) and is what "
                                           "makes the item movable at all"
                                           % (len(members), len(removed))]))

    # ------------------------------------------------------------- phase 2
    moved_per_item = {}
    move_rows = 0
    # `max_moves` is a cap on the run, not on a source. Breaking out of one
    # source's loop left every later source to trip the same cap and repeat the
    # same warning, which read as several separate problems.
    move_cap_hit = False
    new_budget_warned = False
    for src_key in (cfg.get("sources") or []):
        if move_cap_hit:
            break
        sv = V.get(src_key)
        if sv is None:
            _note(plan, "error", "source %r is not a container in this save" % src_key)
            continue
        if not sv.allocated:
            # No cells, so there is nothing in it and nothing to say. It used
            # to be a plan warning, which meant every configuration naming
            # `suit_cargo` or `shipN_cargo` -- and every one does, because the
            # grid was real before Waypoint (4.0) merged it away -- opened its
            # dry run with warnings about grids the game itself no longer
            # shows. The keys stay valid and this stays a no-op.
            continue
        if not sv.sortable and not getattr(sv, "drain_only", False):
            _note(plan, "warning", "source %s is a technology grid or a single cell; "
                                   "skipped" % src_key)
            continue
        for s in list(sv.live()):
            if move_cap_hit:
                break
            if s.removed:
                continue
            # The machine's own row, before anything is said about it. It is
            # already never moved -- the Technology skip below catches it --
            # but it was saying so, once per core: "Add all 14 extractor cores"
            # produced 12 move rows and 119 skips, 14 of them the extractors
            # themselves. The skip list is what a player reads to check nothing
            # was missed, and fourteen rows about the machinery is exactly the
            # noise that hides a real skip. Silent here rather than filtered in
            # the plan view, because the view is not the only consumer; the
            # fingerprint is taken over `plan.rows` and never over `skips`, so
            # this changes no byte an apply writes.
            if is_maintenance_slot(sv.drain_only, sv.group, s.itype):
                continue
            base = {"key": src_key, "container": sv.label, "id": text.safe(s.raw),
                    "stem": text.safe(s.stem), "amount": s.amount,
                    "cell": list(s.cell)}
            row = idb.lookup(s.raw)
            base["name"] = row["name"] if row else None
            bucket = idb.bucket_of(s.raw)
            base["bucket"] = bucket
            base["bucket_label"] = idb.label(bucket)

            if bucket in UNROUTABLE:
                # Defensive, and a refusal rather than a skip. Nothing in
                # `system_meta` sits in a slot on any of the seven readable
                # corpus saves -- it is currencies, reputation deltas and
                # settlement stat tokens -- so reaching this line means either
                # the game now keeps one of those in a container or a
                # hand-edited config named the category. Either way it is not
                # moved, and the plan says so out loud instead of quietly
                # leaving a row out.
                plan.skips.append(dict(base, op="refuse", reason=(
                    "%s never occupies a container slot, so it cannot be "
                    "routed to one" % idb.label(bucket))))
                continue
            if bucket == UNSORTED:
                plan.unknown.append(dict(base))
                plan.skips.append(dict(base, op="skip", reason=(
                    "the item table does not know this id. That is normal after a "
                    "game patch, not an error: it is left exactly where it is.")))
                continue
            # Technology covers damaged technology too, and that is the whole
            # of the DamageFactor question. A separate damage guard used to sit
            # below this line, where it could only ever fire on a substance or
            # a product -- the two kinds where the field does not mean damage.
            # In the operator's save all 116 technology stacks read 0.0 while
            # six ordinary items read 1.0 (Viscous Fluids, Residual Goop,
            # Faecium, Living Slime, a Hyaline Brain), and the guard was pinning
            # those in place forever. It is gone; this line is what catches
            # damaged gear.
            if s.itype == "Technology":
                plan.skips.append(dict(base, op="skip", reason="technology is never sorted"))
                continue
            # An id with raw bytes cannot be typed into a rule, but it can still
            # be moved: the plan addresses a stack by position, never by name. A
            # guard that refused one used to sit here, qualified by "and the
            # table does not know it" -- which the UNSORTED skip above has
            # already dealt with. There is nothing left for it to catch.

            dec = rules.decide(s.raw, src_key)
            if dec["pin"]:
                plan.skips.append(dict(base, op="skip", reason="; ".join(dec["why"])))
                continue
            if not dec["dsts"]:
                plan.skips.append(dict(base, op="skip", reason="; ".join(dec["why"])))
                continue

            # Walk the destination chain. Each container is filled as far as it
            # will go before the next is tried, so one overflowing chest can be
            # backed by a second without changing anything but the routing.
            chain, chain_notes = [], []
            for key in dec["dsts"]:
                if key == src_key:
                    chain_notes.append("%s is the source itself, skipped" % key)
                    continue
                cv = V.get(key)
                if cv is None:
                    chain_notes.append("%s is not a container in this save" % key)
                    continue
                if not cv.allocated:
                    # No cells, so nothing can be put here. Not a plan
                    # warning -- see the source half above -- but the chain
                    # notes are what the refusal below is spelled out of, so
                    # it says the true thing rather than nothing at all. It
                    # only ever reaches a player when *every* destination a
                    # rule names is a grid like this; a chain of a live grid
                    # and a dead one just uses the live one.
                    chain_notes.append("%s has no cells in this save, so "
                                       "nothing can be put in it" % key)
                    continue
                if getattr(cv, "drain_only", False):
                    chain_notes.append("%s is a Stellar Extractor Core; the game "
                                       "refills those itself, so nothing is ever "
                                       "sorted into one" % key)
                    continue
                if cv.is_tech or not cv.sortable:
                    chain_notes.append("%s is a technology grid; nothing is ever "
                                       "sorted into one, whatever a rule says" % key)
                    continue
                chain.append((key, cv))
            if not chain:
                only_self = (len(dec["dsts"]) == 1 and dec["dsts"][0] == src_key)
                if only_self:
                    plan.skips.append(dict(base, op="skip", reason=(
                        "already in %s, which is where the rule sends it" % src_key),
                        why=dec["why"]))
                else:
                    plan.skips.append(dict(base, op="refuse", reason=(
                        "no usable destination: " + "; ".join(chain_notes)),
                        why=dec["why"]))
                continue

            if move_rows >= max_moves:
                _note(plan, "warning",
                      "stopped at max_moves = %d; raise it in options or run twice"
                      % max_moves)
                move_cap_hit = True
                break

            src_total = sv.total(s.stem)
            # The floor applies here only if it is unscoped, or scoped to this
            # very container. A floor naming the exosuit must not quietly hold
            # stock back in the freighter as well.
            keep_here = dec["keep"] if (not dec["keep_in"] or dec["keep_in"] == src_key) else 0
            want = min(s.amount, max(0, src_total - keep_here))
            limits = []
            if keep_here:
                limits.append("keep floor %d in %s (it holds %d)"
                              % (keep_here, src_key, src_total))
            elif dec["keep"] and dec["keep_in"]:
                limits.append("the keep floor of %d is scoped to %s, so it does not "
                              "apply here" % (dec["keep"], dec["keep_in"]))
            if want <= 0:
                plan.skips.append(dict(base, op="skip", reason=(
                    "the keep floor of %d in %s holds all %d of these back"
                    % (keep_here, src_key, src_total)), why=dec["why"]))
                continue
            if dec["move"] is not None:
                already = moved_per_item.get(s.stem, 0)
                room = dec["move"] - already
                limits.append("move cap %d this run" % dec["move"])
                if room <= 0:
                    plan.skips.append(dict(base, op="skip", reason=(
                        "the move cap of %d for this item is already used up this run"
                        % dec["move"]), why=dec["why"]))
                    continue
                want = min(want, room)
            if not create_stacks:
                want = min(want, max(0, s.amount - min_source))
                limits.append("merge-only: %d stays behind so the source stack "
                              "survives" % min_source)
                if want <= 0:
                    plan.skips.append(dict(base, op="skip", reason=(
                        "merge-only mode keeps %d behind and this stack holds %d"
                        % (min_source, s.amount)), why=dec["why"]))
                    continue

            remaining = want
            placed_any = False
            full_notes = []
            for ci, (dst_key, dv) in enumerate(chain):
                if remaining <= 0:
                    break
                if move_rows >= max_moves:
                    _note(plan, "warning",
                          "stopped at max_moves = %d; raise it in options or run twice"
                          % max_moves)
                    move_cap_hit = True
                    break
                here = remaining
                here_limits = list(limits)
                # A stocked leg carries its own ceiling; every other leg uses the
                # rule's fill, if it has one.
                ceiling = dec["leg_fill"].get(dst_key, dec["fill"])
                if ceiling is not None:
                    held = dv.total(s.stem)
                    room = ceiling - held
                    here_limits.append(
                        ("stock ceiling %d (%s holds %d)" if dst_key == dec["stock_in"]
                         else "fill ceiling %d (%s holds %d)")
                        % (ceiling, dst_key, held))
                    if room <= 0:
                        full_notes.append("%s already holds %d of its %d"
                                          % (dst_key, held, ceiling))
                        continue
                    here = min(here, room)
                if ci > 0:
                    here_limits.append("overflow %d of %d in the chain %s"
                                       % (ci + 1, len(chain),
                                          " -> ".join(k for k, _ in chain)))

                left = here
                merges = []
                for ds in dv.live():
                    if left <= 0:
                        break
                    if ds.ident_cross != s.ident_cross:
                        continue
                    room = ds.max - ds.amount
                    if room <= 0:
                        continue
                    take = min(room, left)
                    ds.amount += take
                    left -= take
                    merges.append({"i": ds.idx, "cell": list(ds.cell), "add": take,
                                   "after": ds.amount, "max": ds.max,
                                   "created": ds.created})
                news = []
                if create_stacks and left > 0:
                    cap = dv.predicted_cap(s.raw, s.itype)
                    known_cap = next((x["max"] for x in merges), None)
                    if known_cap:
                        cap = known_cap
                    if cap <= 0:
                        cap = 1
                    for cell in dv.free_cells():
                        if left <= 0 or new_stacks_used[0] >= max_new:
                            break
                        take = min(cap, left)
                        dv.add_stack(s, cell, take, cap)
                        left -= take
                        new_stacks_used[0] += 1
                        news.append({"cell": list(cell), "amount": take, "max": cap})
                    if left > 0 and new_stacks_used[0] >= max_new:
                        # Exhaustion used to be reported only as `partial`,
                        # which reads as "the chest is full" -- a fact about the
                        # save rather than about an option the operator set.
                        here_limits.append("new-stack budget of %d used up this "
                                           "run" % max_new)
                        if not new_budget_warned:
                            _note(plan, "warning",
                                  "stopped creating stacks: the new-stack budget "
                                  "of %d is used up. Raise max_new_stacks in "
                                  "options or run again." % max_new)
                            new_budget_warned = True
                moved = here - left
                if moved <= 0:
                    if not create_stacks:
                        full_notes.append("%s holds no stack of this item to top up "
                                          "and merge-only never creates one" % dst_key)
                    else:
                        full_notes.append("%s is full: every matching stack is at its "
                                          "cap and there is no free cell" % dst_key)
                    continue

                remaining -= moved
                placed_any = True
                s.amount -= moved
                sv.touched = True
                dv.touched = True
                removed_src = False
                if s.amount <= 0:
                    # An emptied stack is normally deleted. Not in an extractor
                    # core: the game keeps a zeroed slot for each resource it
                    # mines -- the four gas slots sit at 0 in every core -- and
                    # that slot is where the next yield lands. Delete it and the
                    # core has nowhere to mine into.
                    if sv.drain_only:
                        s.amount = 0
                    else:
                        s.removed = True
                        removed_src = True
                moved_per_item[s.stem] = moved_per_item.get(s.stem, 0) + moved
                move_rows += 1
                why = list(dec["why"])
                if ci > 0 and full_notes:
                    why.append("overflowed here because " + "; ".join(full_notes))
                plan.rows.append(dict(
                    base, op="move", dst=dst_key, dst_container=dv.label,
                    moved=moved, wanted=here,
                    partial=(remaining > 0 and ci == len(chain) - 1),
                    chain=[k for k, _ in chain], chain_index=ci,
                    merges=merges, new_stacks=news,
                    source_after=(0 if removed_src else s.amount),
                    source_removed=removed_src,
                    why=why, limits=here_limits))

            if not placed_any:
                tail = "; ".join(full_notes) or "no room anywhere in the chain"
                if not create_stacks:
                    reason = ("nothing could be placed. " + tail + ". Put a single "
                              "unit in the destination by hand once, or turn on "
                              "create new stacks.")
                else:
                    reason = "nothing could be placed: " + tail
                plan.skips.append(dict(base, op="refuse", reason=reason, why=dec["why"]))
            elif remaining > 0:
                _note(plan, "warning",
                      "%s: %d left behind, the whole chain (%s) is full"
                      % (text.safe(s.raw), remaining,
                         " -> ".join(k for k, _ in chain)))

    # ------------------------------------------------------------- phase 3
    order = dict((b["key"], i) for i, b in enumerate(idb.buckets))
    for key in (opts.get("tidy") or []):
        v = V.get(key)
        if v is not None and not v.allocated:
            continue                    # no cells: nothing to tidy, and no news
        if v is None:
            _note(plan, "warning", "tidy target %r is not an allocated container" % key)
            continue
        if v.is_tech or not v.sortable:
            plan.skips.append({"op": "refuse", "key": key, "container": v.label,
                               "reason": (
                "a technology grid is never repacked: adjacent modules of the same "
                "family grant adjacency bonuses and supercharged slots are fixed "
                "cells. A repacked grid looks perfectly healthy while the ship "
                "quietly flies slower.")})
            continue
        live = v.live()
        pinned = [s for s in live if idb.bucket_of(s.raw) == UNSORTED
                  or not text.is_clean_ascii(s.stem)]
        pinned_cells = set(s.cell for s in pinned)
        movers = [s for s in live if s not in pinned]
        cells = [c for c in sorted(v.valid, key=lambda c: (c[1], c[0]))
                 if c not in pinned_cells]
        if len(movers) > len(cells):
            plan.skips.append({"op": "refuse", "key": key, "container": v.label,
                               "reason": "more items (%d) than usable cells (%d)"
                                         % (len(movers), len(cells))})
            continue

        def sort_key(s):
            r = idb.lookup(s.raw)
            return (order.get(idb.bucket_of(s.raw), 99),
                    (r["name"] if r else s.stem).lower(), s.stem)
        movers.sort(key=sort_key)
        placements = []
        for s, cell in zip(movers, cells):
            if s.cell != cell:
                r = idb.lookup(s.raw)
                placements.append({"i": s.idx, "id": text.safe(s.raw),
                                   "name": r["name"] if r else None,
                                   "from": list(s.cell), "to": list(cell),
                                   "bucket": idb.bucket_of(s.raw)})
            s.cell = cell
        if not placements:
            plan.skips.append({"op": "skip", "key": key, "container": v.label,
                               "reason": "already in order; nothing written"})
            continue
        v.touched = True
        plan.rows.append({"op": "tidy", "key": key, "container": v.label,
                          "placements": placements, "pinned": len(pinned),
                          "count": len(live),
                          "why": ["repack into reading order by bucket, then name. "
                                  "Writes two integers per item -- the grid cell -- "
                                  "and nothing else: no stack is created, removed, "
                                  "merged or resized."]})

    # --------------------------------------------------------- idle rules
    for i, rule in rules.item_rules:
        if i not in rules.fired:
            plan.idle_rules.append({"i": i, "item": rule.get("item"),
                                    "note": "matched nothing in this run"})

    # ---------------------------------------------------------- phase 4
    # Naming describes where things ended up, so it runs after the moves. It
    # also runs *before* the diff: a container whose only change is its name is
    # still a container this run touched, and computing the diff first left it
    # out of both `diff` and `containers_touched` while the write path went on
    # to change it.
    renames = []
    if opts.get("auto_name"):
        for row in naming.plan_names(save, cfg,
                                     only_default=not opts.get("auto_name_overwrite")):
            renames.append(row)
            plan.rows.append(dict(row, op="rename", key=row["key"],
                                  container=row["label"]))
            v = V.get(row["key"])
            if v is not None:
                v.touched = True
                v.rename_to = row["after"]

    # -------------------------------------------------------------- diff
    for key in sorted(V):
        v = V[key]
        if not v.touched:
            continue
        after = v.snapshot()
        plan.diff.append({"key": key, "container": v.label,
                          "before": v.before, "after": after,
                          "before_used": len(v.before), "after_used": len(after),
                          "valid": v.valid_cells(),
                          "changes": _cell_diff(v.before, after)})
    plan.containers = [row["key"] for row in plan.diff]

    plan.totals = {
        "merges": sum(1 for r in plan.rows if r["op"] == "merge"),
        "moves": sum(1 for r in plan.rows if r["op"] == "move"),
        "tidies": sum(1 for r in plan.rows if r["op"] == "tidy"),
        "units_moved": sum(r.get("moved", 0) for r in plan.rows if r["op"] == "move"),
        "new_stacks": new_stacks_used[0],
        "renames": len(renames),
        "containers_touched": len(plan.diff),
        "skips": len(plan.skips),
        "refusals": sum(1 for s in plan.skips if s.get("op") == "refuse"),
        "unknown": len(plan.unknown),
    }
    # `content_signature()`, not `signature()`: `safety.apply_plan` re-plans
    # from the *backup copy* and compares the digest, so a preimage carrying
    # `int(st_mtime)` made the step-5 gate depend on the backup root
    # preserving timestamps -- exFAT, a network share or a sync client made
    # the save unsortable for good (write-path review two, Q6).
    plan.fingerprint_full = _fingerprint(plan, cfg, save.content_signature())
    plan.fingerprint = plan.fingerprint_full[:SHORT_FINGERPRINT]

    def commit():
        for vc in V.values():
            vc.commit(save.d)
        return [vc.key for vc in V.values() if vc.touched]

    return plan, commit


def _cell_diff(before, after):
    """A per-item before/after, keyed by id, so the page can show a real diff."""
    def fold(rows):
        out = {}
        for r in rows:
            e = out.setdefault(r["id"], {"id": r["id"], "name": r["name"],
                                         "amount": 0, "stacks": 0})
            e["amount"] += r["amount"]
            e["stacks"] += 1
        return out
    b, a = fold(before), fold(after)
    out = []
    for k in sorted(set(b) | set(a)):
        bb = b.get(k)
        aa = a.get(k)
        if bb and aa and bb["amount"] == aa["amount"] and bb["stacks"] == aa["stacks"]:
            continue
        out.append({"id": k, "name": (aa or bb)["name"],
                    "before": bb["amount"] if bb else 0,
                    "after": aa["amount"] if aa else 0,
                    "before_stacks": bb["stacks"] if bb else 0,
                    "after_stacks": aa["stacks"] if aa else 0})
    return out


def fingerprint_preimage(plan):
    """The text the fingerprint is taken over: one line per row that changes a
    byte, in plan order. Exposed because a fingerprint that cannot be explained
    cannot be debugged when a gate fires."""
    parts = []
    for r in plan.rows:
        if r["op"] == "move":
            parts.append("move|%s|%s|%s|%d|%d|%s" % (
                r["key"], r["dst"], r["id"], r["cell"][0] * 1000 + r["cell"][1],
                r["moved"], ",".join("%d,%d+%d" % (m["cell"][0], m["cell"][1], m["add"])
                                     for m in r["merges"])))
            parts.append("new|" + ",".join("%d,%d=%d" % (n["cell"][0], n["cell"][1],
                                                         n["amount"])
                                           for n in r["new_stacks"]))
        elif r["op"] == "merge":
            parts.append("merge|%s|%s|%s|%d" % (
                r["key"], r["id"],
                ",".join("%d:%d" % (s["i"], s["after"]) for s in r["stacks"]),
                r["total"]))
        elif r["op"] == "tidy":
            parts.append("tidy|%s|%s" % (r["key"], ",".join(
                "%s:%d,%d>%d,%d" % (p["id"], p["from"][0], p["from"][1],
                                    p["to"][0], p["to"][1])
                for p in r["placements"])))
        elif r["op"] == "rename":
            # A rename writes a byte like any other row. Leaving it out meant a
            # dry run and an apply that differed only in a container's name
            # fingerprinted identically, and the step-5 gate -- the one that
            # says "this is the plan you saw" -- let it through.
            parts.append("rename|%s|%s" % (r["key"], r["after"]))
    return "\n".join(parts)


def _fingerprint(plan, cfg, signature):
    """SHA-256 over the rows, the configuration and the save's signature.

    Three inputs, because there are three ways the plan in front of the
    operator can stop describing what an apply would do: a row changed, the
    rules changed, or the file changed. The first 8 hex characters are what a
    person is shown; the full digest is what `safety.apply_plan` compares.
    """
    h = hashlib.sha256()
    h.update(fingerprint_preimage(plan).encode("utf-8", "surrogateescape"))
    h.update(b"\ncfg:")
    h.update(config_hash(cfg).encode("ascii"))
    h.update(b"\nsave:")
    h.update((signature or "").encode("utf-8", "surrogateescape"))
    return h.hexdigest()
