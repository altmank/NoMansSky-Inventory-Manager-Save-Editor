# Rules reference

Audience: the tinkerer. This is the reference for the configuration file, field
by field. `docs/GUIDE.md` teaches; this file is looked up.

Messages are quoted verbatim; `docs/TROUBLESHOOTING.md` is keyed by them. Every
example is a complete configuration and is validated by the test suite.

The section you probably want is **Precedence**. Most surprises are not a field
behaving oddly, they are two fields resolving in an order you did not expect.

## The file

One JSON object, at `%LOCALAPPDATA%\NMS-Sorter\config.json`, with the five
previous revisions kept beside it. It is written by the browser and read by the
planner; nothing else touches it.

A config from a **newer** build is refused on load, and the file is not touched:

> this config is version \<v\>; this build reads up to 2. Update the sorter or
> move the file aside

A config from an older build is migrated on read. See **Config version 2**.

## Config keys

Thirteen top-level keys. The defaults are `config.default_config()`.

| Key | Type | Default | Notes |
|---|---|---|---|
| `config_version` | int | `2` | read on load, rewritten on every store; a higher number is refused |
| `name` | string | `"Default layout"` | yours to change; nothing reads it |
| `updated` | string | an ISO timestamp | rewritten by `store()` on every save |
| `notes` | string | a sentence about the ten chests | yours; nothing reads it |
| `options` | object | see **Options** | eight keys, each with its own default |
| `sources` | list of container keys | `["suit", "ship0", "freighter", "corvette"]` | drained in this order |
| `bucket_rules` | list of `{bucket, store}` | 13 rules, raw resources through salvage | consulted in list order |
| `item_rules` | list of objects | 5 rules: Sodium, Oxygen, Carbon, Tritium, Chromatic Metal | consulted before the bucket rules |
| `custom_buckets` | list of `{key, label, note}` | `[]` | your own categories |
| `item_buckets` | object, id to bucket key | `{}` | re-categorise one item |
| `never_move` | list of item ids | `[]` | pinned wherever they are |
| `never_buckets` | list of bucket keys | `["unsorted"]` | blocks the `*` catch-all only |
| `labels` | object, container key to your name | `{}` | renames a container on screen, up to 64 characters; see **Renaming a container** |

**A key you leave out is empty, not the shipped default.** An absent
`item_rules`, `never_buckets`, `sources`, `item_buckets`, `never_move` or
`custom_buckets` becomes empty, and the load reports each one as a note: "...
absent; treated as empty".

`options` is the exception, and is still merged key by key, so an old file
missing a newer option still starts.

Only a file that **does not exist at all** gets the whole shipped default, and
that one is written out, so what is on disk is what is in force. If you want the
shipped rules, copy that file: `config.json` as the sorter first wrote it, or the
Ten chests example in `docs/GUIDE.md`.

**The configuration hash covers the rules only.** `config_hash` is SHA-256 over
everything except `updated` and any top-level key beginning with `_`. Re-saving
a config without touching a rule leaves every minted plan approvable.

## Options

| Option | Type | Default | What it does |
|---|---|---|---|
| `merge_duplicates` | bool | `true` | run the merge phase: combine identical stacks inside one container |
| `create_stacks` | bool | `true` | allow a move to occupy an empty cell. With it off, a move can only top up a stack that is already there |
| `min_source` | int | `1` | how much stays behind in the source stack. **Only read when `create_stacks` is off** |
| `max_moves` | int | `200` | stop after this many move rows |
| `max_new_stacks` | int | `80` | stop creating new stacks after this many, across the whole run |
| `tidy` | list of container keys | `[]` | containers to repack after the moves |
| `auto_name` | bool | `false` | rename storage containers after what the config sends them |
| `auto_name_overwrite` | bool | `false` | also rename a container you named by hand |

Three interactions to know about.

**`create_stacks` off changes what a move can do at all.** A move may then only
merge into an existing stack of the same item in the destination. If the
destination holds none, nothing happens and the plan says so:

> \<key\> holds no stack of this item to top up and merge-only never creates one

and the refusal for the whole row carries the fix:

> nothing could be placed. \<reasons\>. Put a single unit in the destination by
> hand once, or turn on create new stacks.

**`min_source` is inert while `create_stacks` is on.** It is applied in that one
branch and nowhere else. With it at 1 and `create_stacks` off, a stack of 1
cannot move at all:

> merge-only mode keeps 1 behind and this stack holds 1

**`max_moves` and `max_new_stacks` are different kinds of limit.** `max_moves`
counts move rows and stops the phase with a warning:

> stopped at max_moves = 200; raise it in options or run twice

`max_new_stacks` is a quiet budget: when it runs out a leg simply places less
than it wanted, and the row's own arithmetic shows it.

## Sources

`sources` is an ordered list of container keys. It is the only thing that decides
what gets drained; a container that is not a source is only ever a destination.

Order matters: the keep floor and the move cap are both spent as the list is
walked, and the first source to meet a `keep` floor is the one that keeps the
items.

A source that is not in the save is an error at validation time and a note at
plan time. A source that exists but is not allocated, a ship you sold, is a
warning and is skipped. A technology grid or a single cell is skipped too:

> source \<key\> is a technology grid or a single cell; skipped

Extractor cores are asymmetric: they can be a source and are never a
destination, and an emptied slot in one is left at zero rather than deleted.

## Category routing

`bucket_rules` is an ordered list of `{bucket, store}`. Each rule names one
category and one destination.

Several rules may name the same category. That is an **overflow chain**, not a
shadowed rule: the destinations are collected in list order, each is filled as
far as it will go, then the next is tried. Only an exact repeat of the same pair
can never fire, and that is an error:

> bucket \<b\> is already routed to \<store\> by rule \<n\>; this exact pair can
> never fire

A duplicate of a rule that names no destination -- one that only pins or only
keeps -- reads:

> bucket \<b\> is already covered by rule \<n\>; this exact pair can never fire

`bucket: "*"` is a catch-all. It applies **only** to a category that no exact
rule names, and never to a category listed in `never_buckets`.

A category no rule names is **never routed**, which is a real choice. Its items
stay exactly where they are. Validation says so once, for all of them:

> \<n\> bucket(s) have no destination, so anything in them stays put wherever it
> already is. That includes a container meant for something else: \<labels\>

## Item rules

`item_rules` is a list of objects, consulted before the bucket rules.

| Field | Type | Meaning |
|---|---|---|
| `item` | string | the id this rule is about. Matched against the id with its `^` stripped, or against its stem, the part before `#`. An exact id therefore beats the family, so one procedural roll can be pinned without pinning the rest |
| `store` | string or list | the destination container, or a chain of them in order. A chain of one is a plain string |
| `stores` | list of strings | version 1's spelling of a chain. Still read, and folded into `store` by the migration. Both go through `stores_of()`, which drops blanks and repeats |
| `keep` | int | hold this many back in the source and let the surplus go on |
| `keep_in` | string | scope the floor to one container. Without it the floor guards **every** source it meets |
| `fill` | int | a ceiling on the destination: stop once it holds this many |
| `move` | int | a cap on how much of this item may move in one run, across every source |
| `priority` | int | rules are consulted in ascending priority, then in config order. Default 100 |
| `stock` | int | keep one container topped up to this number. Needs `stock_in` |
| `stock_in` | string | the container `stock` applies to. Needs `stock` |
| `pin` | bool | never move this item |
| `note` | string | yours. Nothing reads it |

`keep`, `fill`, `move`, `priority` and `stock` must each be a non-negative whole
number:

> \<field\> must be a non-negative whole number, not \<value\>

A rule that names none of `store`, `keep`, `stock` or `pin` does nothing, and
says so as a warning rather than an error:

> no store, no keep, no stock and no pin: this rule does nothing

## The five modes

The UI names five modes. They are combinations of the fields above, not separate
mechanisms.

| Mode | Fields |
|---|---|
| pin | `pin: true`, or a rule with neither `store` nor `keep` |
| keep | `keep`, optionally `keep_in`, and no `store` |
| send all | `store`, plus `stores` for a chain |
| keep + send | `keep` and `store` |
| stock | `stock` and `stock_in` together |

Each example below is a complete configuration file. Each one validates with
no errors; the one warning they each raise is the unrouted-category warning,
which every minimal example raises by being minimal.

### pin

Never moves, whatever any category rule says.

```json
{
  "config_version": 2,
  "name": "pin",
  "options": {"tidy": []},
  "sources": ["suit"],
  "bucket_rules": [{"bucket": "raw_resources", "store": "chest1"}],
  "item_rules": [
    {"item": "CATALYST1", "pin": true, "note": "Sodium stays on me"}
  ],
  "never_buckets": ["unsorted"]
}
```

The plan reports each pinned stack as a skip, naming the rule:

> \<id\>: pinned, nothing moves it

### keep

Holds a number back where the item already is and hands the surplus to the
category rules, so the destination is wherever that category is routed. It never
fetches: a floor of 250 in a container holding 10 does not top it up to 250.

```json
{
  "config_version": 2,
  "name": "keep",
  "options": {"tidy": []},
  "sources": ["suit", "ship0"],
  "bucket_rules": [{"bucket": "raw_resources", "store": "chest1"}],
  "item_rules": [
    {"item": "CATALYST1", "keep": 250, "keep_in": "suit", "priority": 10,
     "note": "250 Sodium on me, the rest to raw resources"}
  ],
  "never_buckets": ["unsorted"]
}
```

Without `keep_in` that floor holds 250 back in **every** source it meets. With
it, the other sources drain in full, and the plan says so:

> the keep floor of 250 is scoped to suit, so it does not apply here

`keep_in` naming a container that is not one of your sources is a warning:

> keep_in \<key\> is not one of your sources, so nothing is ever drained from it
> and the floor can never bind

### send all

Ignores the category and moves everything to one named container, or to a chain.

```json
{
  "config_version": 2,
  "name": "send all",
  "options": {"tidy": []},
  "sources": ["suit", "ship0"],
  "bucket_rules": [{"bucket": "raw_resources", "store": "chest1"}],
  "item_rules": [
    {"item": "STELLAR2", "store": "chest2", "stores": ["chest3"],
     "note": "Chromatic Metal to chest2, overflow to chest3"}
  ],
  "never_buckets": ["unsorted"]
}
```

### keep + send

A floor in the source and a named destination for the surplus.

```json
{
  "config_version": 2,
  "name": "keep + send",
  "options": {"tidy": []},
  "sources": ["suit", "ship0"],
  "bucket_rules": [{"bucket": "raw_resources", "store": "chest1"}],
  "item_rules": [
    {"item": "STELLAR2", "store": "chest2", "keep": 500, "keep_in": "suit",
     "fill": 2000, "priority": 20,
     "note": "keep a working stock, bank the rest, stop at 2000 banked"}
  ],
  "never_buckets": ["unsorted"]
}
```

`fill` is a ceiling on the destination and `keep` is a floor in the source. A
`fill` with nothing to apply it to is an error:

> a \`fill\` ceiling needs a \`store\` to apply it to

### stock

The two-sided one. It keeps one container topped up to a number by pulling from
every source, never drains it below that number, and sends anything above it
onward.

```json
{
  "config_version": 2,
  "name": "stock",
  "options": {"tidy": []},
  "sources": ["suit", "ship0", "chest1"],
  "bucket_rules": [{"bucket": "raw_resources", "store": "chest2"}],
  "item_rules": [
    {"item": "LAUNCHSUB", "stock": 1000, "stock_in": "chest1",
     "note": "1000 Di-hydrogen in chest1 always, surplus to raw resources"}
  ],
  "never_buckets": ["unsorted"]
}
```

Four things to know about it.

- **The stocked container must also be one of your sources**, or nothing is ever
  drained from it and only the top-up half works.
- `stock` and `stock_in` are required together. One without the other is an
  error: "stock needs both an amount and a container".
- It cannot be combined with `keep`: "a rule cannot have both keep and stock;
  stock already holds that many back in its own container".
- The surplus goes where you say. Name a `store` and the rule reads "stock 1000
  in chest1, surplus to chest2". Name none and the surplus follows the item's
  category, with the stocked container pushed to the front of that chain so it
  fills first.

## What the sentence under a rule means

The Rules section prints one sentence under every per-item rule, in the
planner's words rather than the config's. Each phrase maps to a field.

| Phrase | Field |
|---|---|
| Pin X: nothing moves it | `pin: true`, or no destination and no `keep`. An item routed nowhere is pinned, not merely unhandled |
| Keep N X in \<container\> | `keep: N` with `keep_in` |
| Keep (amount not set): type a number, or \<item\> is left where it is. | the `keep` mode chosen with no amount yet. Until a number is typed the rule pins the item, because a rule with neither a destination nor a keep is a pin, and the validator says the rule does nothing yet |
| in every source | `keep` with no `keep_in`: the floor is held in each source separately |
| Stock N X in \<container\> from every source | `stock: N` with `stock_in` |
| send the rest to \<container\> | `store`, alongside a `keep` |
| Send all X to \<container\> | `store`, with no `keep` |
| surplus follows its category (X) | a stock rule with no `store` |
| surplus goes to \<container\> | `store` on a stock rule |
| overflowing to B, C | the second and later entries of the destination chain |
| and stop when \<container\> holds N | `fill: N` |
| at most N per run | `move: N` |
| Weighed at priority N, so it is tried before the rules left at 100 | a `priority` below 100 |
| Weighed at priority N, so it is tried after the rules left at 100 | a `priority` above 100 |
| \<key\> (not in this save) | a container this save does not have: a sold ship, or a chest not built yet |
| \<key\> (no cells) | a grid the game merged away, such as `suit_cargo`: the rule is valid, does nothing, and says nothing |

A rule can carry both a `keep` and a `fill`, and then it drains the source down
to the floor and tops the destination up to the ceiling, whichever binds first.
`docs/GUIDE.md` section 3 settles the two confusions this wording exists for.

## Precedence

This is the order `Rules.decide()` applies, for one stack in one source. The
first step that returns a decision wins.

1. **`never_move`.** If the stack's stem is in the list it is pinned, with the
   reason "pinned by never_move".
2. **Item rules.** Sorted by `priority` ascending, default 100, then by config
   order. The first rule whose `item` equals the caret-stripped id or its stem
   wins. Only one item rule ever applies.
3. **`stock`**, if that rule has both `stock` and `stock_in`. The stocked
   container becomes a destination and a floor at once. If the rule also names a
   `store`, that is the surplus tail and resolution ends here. If it does not,
   resolution continues at step 8 to find a tail.
4. **`pin`**, either explicit or implied: a rule with no `store` and no `keep` is
   a pin. This step is skipped entirely for a stock rule, which is why a stock
   rule naming no destination does not read as "pinned".
5. **`keep`**, recorded with its `keep_in` scope.
6. **`move`**, recorded as a per-run cap.
7. **`store` and `stores`**, which end resolution: the chain is the rule's own.
8. **The bucket chain.** Every `bucket_rules` entry naming this category
   contributes its destinations, in config order, without repeats. A `*` rule
   contributes only if no exact rule matched, and never for a category in
   `never_buckets`.
9. **The stocked container is prepended** to whatever chain step 8 produced.
10. **No destination.** The stack is skipped, with either "bucket \<b\> is in
    never_buckets and no rule names it explicitly" or "no bucket rule matches
    \<label\>".

A rule that reaches step 5 or 6 but names no destination falls through to the
bucket chain. That is the `keep` mode: "keep 250 in suit, the surplus falls
through to the bucket rules".

Step 4 asks whether the rule names any destination at all, through
`stores_of()`, so a rule written with either spelling of a chain routes rather
than pinning.

## Overflow chains

A chain is a list of destinations for one category or one item. Each is filled as
far as it will go, then the next is tried. The row names its position:

> overflow 2 of 2 in the chain chest2 -> chest3

Before a leg is used it is checked, and an unusable leg is described rather than
silently dropped:

| Condition | What the plan says |
|---|---|
| the leg is the source itself | \<key\> is the source itself, skipped |
| not in this save | \<key\> is not a container in this save |
| not allocated | \<key\> is not allocated in this save |
| an extractor core | \<key\> is a Stellar Extractor Core; the game refills those itself, so nothing is ever sorted into one |
| a technology grid | \<key\> is a technology grid; nothing is ever sorted into one, whatever a rule says |

If no leg survives, the row is a refusal listing every reason: "no usable
destination: ...". If the only destination was the source itself it is a skip
instead: "already in \<key\>, which is where the rule sends it".

**Per-leg ceilings.** A leg's ceiling is the stocked amount if that leg is the
stocked container, and the rule's `fill` otherwise. The two read differently:

> stock ceiling 1000 (chest1 holds 400)

> fill ceiling 2000 (chest2 holds 1950)

A leg already at its ceiling is not an error, it is the reason the next leg was
used: "\<key\> already holds 400 of its 1000". If every leg is at its ceiling or
full, the run leaves the remainder alone and warns:

> \<id\>: \<n\> left behind, the whole chain (chest2 -> chest3) is full

## How much moves

For one stack, in this order:

1. **The source total.** `keep` is measured against everything of that item in
   the source, not against the one stack. A floor of 250 across two stacks of 200
   holds both.
2. **The keep floor**, but only if it is unscoped or scoped to this container.
   The amount wanted is the smaller of this stack and the source total minus the
   floor. If that is zero or less the row is a skip: "the keep floor of 250 in
   suit holds all 300 of these back".
3. **The move cap**, spent per stem per run across every source. Used up, the
   next stack of the same item is skipped: "the move cap of 100 for this item is
   already used up this run".
4. **Merge-only**, when `create_stacks` is off: `min_source` units stay behind.
5. **The leg ceiling**, per destination, as above.

Only then is anything placed.

## Merging and new stacks

Two phases touch stacks, and they are not the same thing.

**Phase 1, merge duplicates**, works inside one container. It groups stacks by
full identity, which **includes** `MaxAmount`, pours them into the first members
up to each cap, and frees the cells that end at zero. It runs first, because it
is what makes a duplicated item movable at all. It refuses in three cases:

> technology: Amount is a charge level, not a count, so combining two stacks is
> meaningless

> one of these stacks is damaged; merging would hide the damage

> \<n\> units would not fit back into these stacks

The last one rolls the group back untouched. A group already as consolidated as
pouring can make it is left alone and writes no byte.

**Phase 2, moves**, merges **into existing stacks before creating any**. Across
two containers the identity match ignores `MaxAmount`, because the same product
is capped at 10 in the exosuit and 20 in a chest.

Only when every matching stack in the destination is at its cap does a move
create a new one, in a free cell from `ValidSlotIndices`, and only while
`max_new_stacks` is unspent. The cap for a created stack is predicted from the
stack table, **unless a stack of that item is already there**, in which case the
`MaxAmount` the game wrote wins.

An emptied source stack is removed, with one exception: in an extractor core it
is left at zero, because that slot is where the next yield lands.

## Tidy

`options.tidy` is a list of container keys, repacked after the moves. Tidy writes
two integers per item, the grid cell, and nothing else: no stack is created,
removed, merged or resized.

The order is reading order, row by row: category first, in the taxonomy's own
order, then item name, then stem.

- Items the table does not know, and ids carrying raw bytes, **stay in their
  cells**, and everything else is packed around them.
- A technology grid is refused outright: "a technology grid is never repacked:
  adjacent modules of the same family grant adjacency bonuses and supercharged
  slots are fixed cells. A repacked grid looks perfectly healthy while the ship
  quietly flies slower."
- More items than cells is a refusal: "more items (\<n\>) than usable cells
  (\<n\>)".
- A container already in order is a skip, not a rewrite: "already in order;
  nothing written".

## Auto-naming

With `auto_name` on, a storage container is renamed after what the config sends
it. Only the Storage section is named: a ship or the exosuit carries a name you
chose for another reason.

- The name is the labels of the categories routed there, joined with commas. If
  no category is routed there, it is the names of the items an item rule pins
  there. If neither, the container is not named.
- The ceiling is **40 characters**, which is where the in-game rename field
  stops. A name is cut on a word boundary when one is available late enough in
  the budget.
- Too many categories to list becomes the first plus a count: `Raw Resources +3
  more`.
- A container in an overflow chain is numbered: `Raw Resources 2/2`.
- An empty name and the literal `BLD_STORAGE_NAME` mean the same thing, "never
  named", and both are safe to replace.
- A name you typed is left alone unless `auto_name_overwrite` is on.

Renames are part of the plan and are in the fingerprint, so a rename-only
difference between the dry run and the apply cannot pass the apply gate.

## Renaming a container

`labels` maps a container key to the name you want on screen:

```json
{
  "config_version": 2,
  "labels": {"vehicle4": "Dragonfly, spare parts", "chest7": "Nanite stock"}
}
```

It is display only. Nothing is written into the save by it, the container keys
and the rules that name them are untouched, and no row of the plan changes. The
rename box on each card in the Save section writes this map, so you never have
to edit it by hand.

It exists because the save does not always carry a name. A storage container
you named in game carries yours; a ship you never renamed carries nothing at
all, and the sorter falls back to the ship's class and slot number. A name here
wins over both.

What the validator asks of it:

| It has to be | Or |
|---|---|
| an object, container key to name | `"labels": "vehicle4"` is an error. It used to save without a word and then leave the Save section unable to draw at all |
| a name that is text | a number, a list or an object is an error; one used to be drawn on the card as `42` or `['x']` |
| a name with something in it | blank or whitespace is an error. Delete the entry instead, and the card goes back to the name the save and the sorter work out between them |
| a name of at most 64 characters | longer is an error. A card, three destination pickers and the plan's container table all draw this name, and none of them can lay out 300 characters |
| a container key this save has | a key it has not got is a **warning**, not an error: one configuration serves every save in the folder, so a name for a ship slot this save has not got is the other save's ship |

A rename does change the plan's **fingerprint**, because `labels` is part of the
configuration and the fingerprint covers it. So renaming a card after a dry run
means running the dry run again: Apply only ever runs a plan it has printed.

## `never_move` and `never_buckets`

They sound alike and do different things.

`never_move` is a list of **item ids**, compared by stem. Anything in it is
pinned wherever it is, before any rule is consulted.

`never_buckets` is a list of **category keys**, and it blocks exactly one thing:
the `*` catch-all. It does not stop an explicit rule. Routing a category that is
also in `never_buckets` is a warning that says as much:

> bucket \<b\> is also in never_buckets; the explicit rule wins, which is
> probably what you meant

The default is `["unsorted"]`, so a catch-all cannot sweep up the items the
table does not know.

**System and Non-Inventory is not a destination at all.** Nothing in it ever
occupies a container slot -- it is currencies, reputation deltas, settlement
stat tokens and repair sockets -- so no rule may name it, an override may not
point an item at it, and it is not offered in any picker. It was in this
default list until that was settled; a config that still names it loads, and
the migration takes it out and says so. The category itself stays in the Items
table, because every item has one.

## Custom categories and per-item overrides

**`custom_buckets`** adds your own category, as `{key, label, note}`. The key is
written into the config and compared exactly, so it is checked hard:

> key \<k\> must be lower-case letters, digits and underscores; it is written
> into the config and compared exactly

A key that duplicates another custom key is an error, and so is one that collides
with a generated category: "\<k\> is already a generated bucket; pick another
key". A missing label is a warning; the key is shown instead.

**`item_buckets`** moves one item to another category, as `{"<id>": "<bucket>"}`.
It is an overlay on the generated table, re-applied from the base every time, so
removing an override really removes it. An override pointing at a category that
does not exist is an error:

> \<b\> is not a bucket. An override pointing at a bucket that does not exist
> would silently do nothing.

Reach for `item_buckets` when the item is in the wrong category for everyone.
Reach for an item rule when you want this one item treated specially.

## `unsorted` and `packed_tech`

`unsorted` is not a category you can route. It means the item table does not know
this id, so the item is left exactly where it is:

> the item table does not know this id. That is normal after a game patch, not an
> error: it is left exactly where it is.

`packed_tech` is matched by **shape**, not by a lookup: an id carrying a `#hash`
that no table names is an uninstalled module. It has no entries in `items.json`.

Two more things are never sorted. Technology is skipped outright: "technology is
never sorted". An id that is not printable ASCII and that no rule claims is left
alone.

## Every validation message

`config.validate()` returns a list of `{level, where, message}`. An **error**
blocks a save; a **warning** does not.

| Level | `where` | Message |
|---|---|---|
| error | `config_version` | this file is version \<v\>; this build writes version 2 |
| error | `sources[i]` | \<key\> is not a container in this save |
| warning | `sources[i]` | \<key\> is not sortable right now (unallocated, or a technology grid) |
| error | `bucket_rules[i]` | no bucket named \<b\>; a rule that can never match is a typo |
| error | `bucket_rules[i]` | bucket \<b\> is already routed to \<store\> by rule \<n\>; this exact pair can never fire |
| error | `bucket_rules[i]` | bucket \<b\> is already covered by rule \<n\>; this exact pair can never fire, for a duplicate that names no destination |
| error | `bucket_rules[i]` | no destination container |
| error | `bucket_rules[i]` | destination \<key\> is not a container in this save |
| warning | `bucket_rules[i]` | destination \<key\> is not sortable right now |
| warning | `bucket_rules[i]` | bucket \<b\> is also in never_buckets; the explicit rule wins, which is probably what you meant |
| warning | `bucket_rules` | \<n\> bucket(s) have no destination, so anything in them stays put wherever it already is. That includes a container meant for something else: \<labels\> |
| error | `item_rules[i]` | \<id\>: empty id |
| error | `item_rules[i]` | \<id\>: not printable ASCII; rules are written by hand and ids with raw bytes cannot be typed reliably |
| error | `item_rules[i]` | \<id\>: no item in the table has this id -- a typo, or content newer than the item table. Ids come from the table or from your own inventory, never from a display name. |
| error | `item_rules[i]` | \<id\> is already routed to \<store\> by rule \<n\>; this exact pair can never fire |
| error | `item_rules[i]` | \<id\> is already covered by rule \<n\>; this exact pair can never fire, for a duplicate that names no destination |
| error | `item_rules[i]` | \<field\> must be a non-negative whole number, not \<value\> |
| error | `item_rules[i]` | destination \<key\> is not a container in this save |
| warning | `item_rules[i]` | destination \<key\> is not sortable right now |
| error | `item_rules[i]` | stock needs both an amount and a container |
| error | `item_rules[i]` | stock container \<key\> is not in this save |
| warning | `item_rules[i]` | stock container \<key\> cannot receive items right now |
| error | `item_rules[i]` | a rule cannot have both keep and stock; stock already holds that many back in its own container |
| error | `item_rules[i]` | keep_in names \<key\> but there is no keep to apply there |
| error | `item_rules[i]` | keep_in \<key\> is not a container in this save |
| warning | `item_rules[i]` | keep_in \<key\> is not one of your sources, so nothing is ever drained from it and the floor can never bind |
| error | `item_rules[i]` | a \`fill\` ceiling needs a \`store\` to apply it to |
| warning | `item_rules[i]` | no store, no keep, no stock and no pin: this rule does nothing |
| error | `custom_buckets[i]` | a bucket needs a key |
| error | `custom_buckets[i]` | key \<k\> must be lower-case letters, digits and underscores; it is written into the config and compared exactly |
| error | `custom_buckets[i]` | duplicate bucket key \<k\> |
| error | `custom_buckets[i]` | \<k\> is already a generated bucket; pick another key |
| warning | `custom_buckets[i]` | no label; the key will be shown instead |
| error | `item_buckets[id]` | \<id\>: one of the three id messages above |
| error | `item_buckets[id]` | \<b\> is not a bucket. An override pointing at a bucket that does not exist would silently do nothing. |
| error | `labels` | labels has to be an object mapping a container key to a name, not \<what it is\> |
| error | `labels[key]` | a container name has to be text, not \<what it is\> |
| error | `labels[key]` | a container name cannot be blank; delete the entry to go back to the name the save carries |
| error | `labels[key]` | a container name is at most 64 characters and this one is \<n\> |
| warning | `labels[key]` | \<key\> is not a container in this save, so this name is shown nowhere |
| error | `never_move[i]` | \<id\>: one of the three id messages above |
| error | `options.tidy[i]` | \<key\> is not a container in this save |
| warning | `options.tidy[i]` | \<key\> is not sortable right now |

## Config version 2

Version 2 is what this build reads and writes. A file claiming a higher version
is refused at startup, with a page naming both numbers:

> this config is version \<v\>; this build reads up to 2. Update the sorter or
> move the file aside

A version 1 file is migrated **in memory** when it is loaded. Nothing is written
to disk until something else saves the configuration, so opening the sorter on an
old config does not change it. `migrate(cfg)` is pure and idempotent: migrating
an already-current config returns a copy and says nothing.

Version 2 changes nothing a person typed. Four things happen on migration:

| Change | Effect |
|---|---|
| `stores` folds into `store`, which is now a container or a list of them | a chain of one stays a plain string; both spellings go through `stores_of()` |
| an `item_buckets` override equal to the shipped category is dropped | an override that agrees with the default cannot be told from one nobody has looked at |
| a `custom_buckets` entry whose key the taxonomy now generates is dropped | the validator refuses a custom category that shadows a generated one |
| `labels` is dropped only when it is empty | a non-empty one is your own container names and is kept as written |

Every change is reported: the migration returns a note per change, and those
notes are surfaced on load rather than applied silently.

## Revisions of your configuration

Every save rotates the previous revisions down one:

```
config.json         what you just saved
config.json.bak.1   the revision you just replaced
config.json.bak.2
config.json.bak.3
config.json.bak.4
config.json.bak.5   the oldest kept; the next rotation drops it
```

A single `config.json.bak` left by an older build is **adopted** into the
rotation on the first save, becoming `.bak.1` if that slot is free, rather than
being overwritten.
