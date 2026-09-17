"""Auto-naming: a container's name derived from what the sort config sends it.

The game stores a storage container's name on the inventory node itself, in
`Name`. An unnamed one carries the literal `BLD_STORAGE_NAME`, which the game
renders as "Storage Container N" -- so an empty string and that placeholder mean
the same thing and both are safe to overwrite.

The in-game rename field stops at **40 characters**, so that is the ceiling here.
Names are truncated on a word boundary where one is available, because a name cut
mid-word reads like corruption rather than like a limit.

Only containers the config actually routes something to are named, and only in
the Storage section. A ship or the exosuit carries a name the operator chose for
other reasons; overwriting those from a sort rule would be destroying information
to display information.
"""
from .itemdb import db

MAX_NAME = 40


def _stores_of(rule):
    """A rule's destinations, in order.

    The same two lines as `planner._stores_of`, spelled out here rather than
    imported because `planner` imports this module. Deriving a chain position
    from `store` alone put the second chest of a pair in no chain at all, so a
    `stores` chain produced two containers named identically.
    """
    store = rule.get("store")
    first = list(store) if isinstance(store, (list, tuple)) else [store]
    out = []
    for v in (first + list(rule.get("stores") or [])):
        if v and v not in out:
            out.append(v)
    return out

# The game shows this for a container that has never been named.
PLACEHOLDER = "BLD_STORAGE_NAME"


def _fit(s, limit=MAX_NAME):
    """Truncate to `limit`, preferring a word boundary over a hard cut."""
    s = " ".join((s or "").split())
    if len(s) <= limit:
        return s
    cut = s[:limit]
    sp = cut.rfind(" ")
    # Only honour the space if it leaves most of the budget used; otherwise a
    # single long word would collapse the name to almost nothing.
    if sp >= limit * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,+&")


def is_default_name(name):
    n = (name or "").strip()
    return n == "" or n == PLACEHOLDER


def buckets_for(key, cfg):
    """Every bucket routed to this container, in rule order, without repeats."""
    out = []
    for rule in (cfg.get("bucket_rules") or []):
        if key in _stores_of(rule) and rule.get("bucket") not in out:
            out.append(rule.get("bucket"))
    return out


def items_for(key, cfg):
    """Items explicitly pinned to this container by an item rule."""
    out = []
    for rule in (cfg.get("item_rules") or []):
        if key in _stores_of(rule):
            row = db().lookup(rule.get("item") or "")
            nm = row["name"] if row else (rule.get("item") or "")
            if nm and nm not in out:
                out.append(nm)
    return out


def proposed_name(key, cfg, chain_position=None, chain_length=None):
    """The name this config implies for `key`, or None if it implies nothing.

    `chain_position` / `chain_length` describe where this container sits in an
    overflow chain, so the second chest of a pair reads "Raw Resources 2 of 2"
    rather than being indistinguishable from the first.
    """
    idb = db()
    parts = [idb.label(b) for b in buckets_for(key, cfg) if b]
    if not parts:
        parts = items_for(key, cfg)
    if not parts:
        return None

    if chain_length and chain_length > 1 and chain_position:
        base = parts[0]
        suffix = " %d/%d" % (chain_position, chain_length)
        if len(parts) > 1:
            extra = " +%d" % (len(parts) - 1)
            return _fit(base, MAX_NAME - len(suffix) - len(extra)) + extra + suffix
        return _fit(base, MAX_NAME - len(suffix)) + suffix

    joined = ", ".join(parts)
    if len(joined) <= MAX_NAME:
        return joined
    # Too long to list: lead with the first and count the rest, which stays
    # honest about how many buckets land here.
    extra = " +%d more" % (len(parts) - 1)
    return _fit(parts[0], MAX_NAME - len(extra)) + extra


def plan_names(save, cfg, only_default=True):
    """-> [{key, label, before, after, was_default}] for every rename proposed.

    `only_default` leaves a name the operator typed themselves alone. That is the
    default because a hand-written name is a decision, and a config is not
    grounds to overwrite one silently.
    """
    rows = []
    # Where each container sits in each bucket's overflow chain.
    chains = {}
    for rule in (cfg.get("bucket_rules") or []):
        chain = chains.setdefault(rule.get("bucket"), [])
        for st in _stores_of(rule):
            if st not in chain:
                chain.append(st)

    for c in save.containers():
        if c.section != "Storage" or not c.allocated:
            continue
        pos = length = None
        bl = buckets_for(c.key, cfg)
        if bl:
            chain = chains.get(bl[0]) or []
            if len(chain) > 1 and c.key in chain:
                pos, length = chain.index(c.key) + 1, len(chain)
        want = proposed_name(c.key, cfg, pos, length)
        if not want:
            continue
        before = c.name or ""
        was_default = is_default_name(before)
        if only_default and not was_default:
            continue
        if before == want:
            continue
        rows.append({"key": c.key, "label": c.display_name(), "before": before,
                     "after": want, "was_default": was_default,
                     "length": len(want)})
    return rows
