# After a No Man's Sky patch

Audience: whoever is keeping this project working. This is the playbook for the
day the game updates.

Most patches change the item table and nothing else, and the whole job is steps
1 to 7 below in about half an hour. Some patches move something in the save, and
then it is a code change, not a data change. The difference shows up at step 5,
which is why the corpus runs before the release and not after it.

**Nothing here is urgent.** A table that lags the game build is the normal state
of this tool: an id it does not know is reported and left exactly where it is.
Users are not at risk while you take your time.

## What you need

| For | Need |
|---|---|
| `tools/hgpak.py`, step 1 | `zstandard`. The `.pak` payloads are HGPAK v2 with raw Zstd frames, not PSARC. `python -m pip install zstandard`, or `pip install -e ".[data]"` |
| MBINCompiler, step 2 | the .NET 8 runtime, and an MBINCompiler matching the game build. The version that shipped with the current data is 7.02.0.1, decompiled against game build 25233815; the data is stamped 25320008 because every input was re-extracted from that build's paks and compared byte for byte |
| steps 3 to 7 | nothing beyond the standard library |

`nms-db/` is around 128 MB of Hello Games' content. It is not in this
repository and must not be added to it.

**There is no route without a game install.** `tools/gen_emit.py` has one
mode, `--tables` with `--lang`, and both are required. A rebuild that is not
chasing a patch is the same command against the build the data is already
stamped with: it must reproduce every committed file byte for byte, and a
`git diff` that says nothing is the check.

## The seven steps

### 1. Pull the tables and the language paks

```
python tools/hgpak.py "<Steam>/No Man's Sky/GAMEDATA/PCBANKS" reality       nms-db/mbin
python tools/hgpak.py "<Steam>/No Man's Sky/GAMEDATA/PCBANKS" itemtable     nms-db/mbin
python tools/hgpak.py "<Steam>/No Man's Sky/GAMEDATA/PCBANKS" basebuilding  nms-db/mbin
python tools/hgpak.py "<Steam>/No Man's Sky/GAMEDATA/PCBANKS" nms_loc       nms-db/mbin
python tools/hgpak.py "<Steam>/No Man's Sky/GAMEDATA/PCBANKS" gcvehicleglobals nms-db/mbin
```

The last one is the exocraft table: `gcvehicleglobals.global.mbin` in
`NMSARC.globals.pak` carries `GcVehicleType` in the order the game writes it,
which is the order `VehicleOwnership` is in.

### 2. Convert with the pinned MBINCompiler, and check the pin

```
MBINCompiler.exe nms-db/mbin/*.mbin
```

Then sort the output: the reality, item and base-building tables plus
`gcvehicleglobals.global.MXML` into `nms-db/tables/`, the `nms_loc*_english`
files into `nms-db/lang/`.

**MBIN files carry a format version, and MBINCompiler is version-locked to the
game.** A compiler older than the game refuses the file or, worse, decodes it
with the previous struct layout and gives you plausible XML with the wrong field
names. If the game's MBIN version moved, update the pin before anything else:
the current pin is recorded in `nms_sorter/data/DATA_VERSION` as
`mbincompiler`.

### 3. Generate

```
python tools/gen_emit.py --tables nms-db/tables --lang nms-db/lang --build <new build>
```

`--tables` and `--lang` are required and never default to a path outside the
repository, because a generator that guesses where the extract lives can read the
wrong build's tables and say nothing. The generator merges and never replaces:
every existing row survives, the game wins where it has an opinion, and new ids
are appended. The base it merges over is `--base`, which defaults to the
shipped `nms_sorter/data/`, so a run is read-modify-write over the files it
overwrites.

**Commit or stash before you run it.** Nothing is copied aside first: git
history is the backup, `git diff` is the review and `git checkout
nms_sorter/data` is the undo. That is deliberate. The previous design wrote
`.pre-<build>` copies beside the generator, and the genuine 4,805-row original
was lost anyway when a second run overwrote its own backup.

Then prove the result:

```
python -m pytest tests/test_data.py tests/test_itemdb.py -q
```

Those tests read their expected values out of `nms_sorter/data/DATA_COUNTS`,
which the generator writes: `buckets`, `ids`, `procedural`, `unsorted`,
`vehicles`, `gridnames` and `by_bucket`. **No exact count lives in application code**, and none is typed
into a document either. `by_bucket` is there so a failure names which bucket
moved rather than reporting that a total changed. The health panel and the plan
banner read the same numbers through `itemdb.data_version()` and the API, which
is why "sixteen", "fourteen cores", "5133" and "17" appear nowhere in the UI.
`DATA_VERSION` is the other generated file, four `key=value` lines:
`game_build`, `mbincompiler`, `generated` and `generator`.

`vehicles.json` is generated in the same run, from the two first-party sources
above: the type order out of `gcvehicleglobals.global.MXML` and the English
name out of the localisation, under `VEHICLE_<TYPE>_TITLE_L`. It is what lets
an exocraft card read "Colossus" instead of "Exocraft 3". A type the
localisation no longer names keeps the name the shipped file has for it, and a
new type with no name at all falls back to "Exocraft N" on the page rather than
being guessed at. If the property the order is read from is ever renamed the
generator stops and says so; it will not infer an order from a grid size.

`gridnames.json` is generated in the same run, out of the localisation alone:
the game titles every vessel inventory screen with `INVENTORY` = "%TYPE%
INVENTORY" and substitutes the vessel word (`SUIT`, `SHIP`, `FREIGHTER`,
`VEHICLE_TITLE`), and a cargo tab is `UI_CARGO`, or `UI_FREIGHTER_CARGO` on the
freighter. It is what heads the freighter's two grids. If `INVENTORY` stops
carrying its `%TYPE%` placeholder the generator stops and says so rather than
writing the placeholder onto a card; a key the localisation no longer has keeps
the name the shipped file holds.

A half-finished run cannot pass quietly: `DATA_COUNTS` is written by step 3 and
these tests assert the loaded table against it.

### 4. Read `added-in-<build>.txt`

`tools/added-in-<build>.txt` lists only what the new build introduced, with each
id's kind, bucket and name. Read it. It is a few dozen lines, not a 5,000-row
diff. It is written beside the outputs, or into `tools/` when the outputs are
the shipped ones, and it is gitignored: it is a record of one run for you to
read, not part of the release.

**Hand-check any bucket that gained more than 20 ids.** A new bucket assignment
is learned from the `(kind, game category)` pair, and a patch that introduces a
whole new content type can land a lot of ids somewhere plausible and wrong. A
bucket that gained three needs a glance; one that gained two hundred needs the
game's own description of a few of them.

### 5. Diff the data, and run the corpus

Diff `nms_sorter/data/*.json` against the previous release. **Every changed
bucket assignment gets a changelog line**, because a changed assignment moves
somebody's items to a different chest than last week.

```
python -m pytest tests/test_corpus.py --real-saves <a copy of a save folder>
```

A **copy**, never the live folder. This is the step that tells you whether the
patch was a data change or a code change: if a container path moved, the corpus
fails here, and that is a change in `savemodel.py` and not something a
regeneration can fix.

### 6. Bump the versions and release

Bump `DATA_VERSION` (the generator writes it) and the patch version, write the
changelog section naming the game build, and release. A data-only regeneration
is a patch release.

### 7. If the save `Version` moved, add a fixture first

The accepted save `Version` range is the set of versions this build has been run
against, and **it is not a wall**. A save outside it plans with a banner and
**applies**, recording the version as an `info` step at step 1b:

> this save reports version ..., outside the range this build was verified on
> (... to ...); continuing, because the round-trip and nothing-else-changed
> checks run on this file regardless

The gate is advisory because the alternative costs more than it buys: the game
updates, the version rises the day it does, and a refusal keyed to a number
turns the tool off on every patch. Only an operator who turned
`strict_version_check` on, or passed `--strict-version-check`, sees a refusal
instead:

> this save reports version ...; this build was verified on ... to ..., so
> apply is refused until a fixture for ... exists

The expedition gate and step 8's `mf_` format check are unchanged in both
modes: those are measured structural differences, not a version number.

Widening the range is still evidence-driven, in this order:

1. get a save on the new version into the corpus as a fixture,
2. see it round-trip byte-exact and its containers discovered against a hand
   count -- **Verify file**, in the health panel on the Settings section,
   does the round trip on demand and prints both steps,
3. then widen the range, with a changelog line naming the fixture that justified
   it.

Never the other way round. What stands between this tool and a save shape nobody
has measured is steps 4, 6 and 7 of the write sequence, on the file itself; the
range is the record of what has been checked by hand, and widening it is how that
record stays true.

## What usually breaks

In rough order of how often it has happened.

**Container paths.** A key that moved in the save document is a **code change**
in `savemodel.py`, not a data change. The symptom is the corpus at step 5, or
step 7 of the write sequence refusing with "nothing else changed: the plan
touched '...', which is not a container this build can name a path for". Do not
work around it in a config.

**A new bucket.** A patch that adds a content type may want a new category. That
touches the floor in `itemdb.py`, which refuses to load below 17 buckets or
5,000 ids, and `DATA_COUNTS`, which the tests assert against. Note that the
floor is a floor: the table growing is fine, the table shrinking is what it is
there to catch.

**A new inventory kind.** The planner branches on `inv_type` for stack caps and
for the technology skip. A kind nobody has seen will fall through those branches
and be treated as an ordinary product, which is the wrong answer for anything
whose `Amount` is not a count. Check `stacks.json` grew a group if the game added
a container family.

**An `mf_` format bump.** The metadata codec decodes format 2004 field by field.
A new format means step 8 of the write sequence refuses, before anything is
written, with "metadata is updatable: this metadata format (...) is not one this
build knows how to update". That refusal is correct and it is not a bug: the fix
is to decode the new format, not to widen the check. An integrity hash appearing
in `0x08..0x37` is the same class of problem and the same answer.

## What does not need doing

- **The shipped data is not regenerated on first run**, and that is deliberate:
  it needs a .NET tool and 128 MB of extract. A product that asks a player for
  that is a different product.
- **An unknown id is not a bug report.** It is reported as `unsorted` and left
  alone. That is the designed behaviour between a patch and a regeneration.
- **Ids are never inferred from a display name.** Localisation keys look like
  `<ID>_NAME_L`, which invites it, and the game silently discards an id it does
  not recognise, so a wrong id looks like it worked and vanishes at the next
  load. It has been wrong three times here. Read ids out of the tables.
