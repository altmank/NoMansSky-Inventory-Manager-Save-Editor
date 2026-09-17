# Troubleshooting

Audience: anyone who got a message they did not expect.

Find the message you saw. The headings are the exact first sentence of every
message the program can produce, in alphabetical order within each section.
`...` in a heading is where a real value appears: a file name, a number, a
container key.

Four sections, in the order you are likely to need them: the **startup pages**,
the **banners**, the **refusals** (with the ones that begin with a value listed
after them), and the **validation and plan messages** that appear beside a
control rather than on their own.

Messages only a script can provoke are not listed. If you are driving the HTTP
API yourself and a request comes back 400, the sentence names the field it did
not like.

**Almost nothing in this list means anything was written.** Every refusal but one
family happens before a byte reaches a save. The exceptions are the messages from
steps 10 and 11, which run after the write: they re-read what was already written
and tell you it does not match. Each of those entries says so, and in every one
of them the backup folder named on the Apply card holds the original.

Headings do not move: a CI test greps every one of them out of the source, so a
heading with no matching message in the code fails the build.

## Startup pages

Each of these is the first sentence of a page the sorter serves before the
application proper, or a line it prints and stops. Two of them are not errors and
say so.

### No No Man's Sky save folder was found.

cause: nothing under `%APPDATA%\HelloGames\NMS` looks like a save folder. This is
the **first-run page**, not an error: the sorter has not failed at anything, it
just does not know which folder you play. It probes that one root and no other
place on the machine, because a wrong guess sorts the wrong account.
fix: the page lists the three layouts it knows (Steam `st_<id>`, GOG
`DefaultUser`, and Microsoft Store / Game Pass, which is not supported) and has a
text field. Paste the folder that holds `save.hg` and `mf_save.hg`. "Look again"
re-runs the probe without a restart. Nothing was read and nothing was written.

### Port ... is already in use, and so are the ... ports after it -- another sorter, or something else, is listening there; stop it or pass --port with a free number.

cause: eleven ports in a row all answered. **One busy port is not an error**: the
sorter tries the next ten, and prints `port 8765 was busy; using port N instead`
in its startup block. You only see this sentence when the wanted port and all ten
after it are taken, which normally means several sorters are already running.
fix: look for the windows already running one and use the page one of them is
already serving, or start again with `--port` and a free number. The port is
probed rather than merely bound, because Windows lets a second process bind a
port another process is listening on, and two sorters on one port is a silent
wrong-answer machine. The process exits with code 3 and writes nothing.

### That folder holds no save*.hg files, so it is not a save folder. Nothing was changed.

cause: the folder you typed on the first-run page exists but holds no saves.
Almost always a parent folder (`...\HelloGames\NMS` rather than
`...\NMS\st_<id>`), or the path of a save **file** rather than the folder that
holds it.
fix: open the folder in Explorer, check you can see `save.hg` or `save2.hg` in
it, and paste that folder. Your setting was not changed.

### The configuration file could not be read.

cause: `config.json` is not valid JSON. A hand edit that lost a comma or a brace
is the usual reason; a file half-written by something that was interrupted is the
other one.
fix: either repair the JSON in a text editor and restart, or press **"Move it
aside and start fresh"** on the page. That renames the file to
`config.json.bad-<stamp>`, writes the shipped default, and drops you into the
application without a restart. Your rules live in that one file and nowhere else,
so starting fresh loses them; the `.bad-` copy beside it is the way back. While
this page is up, every other address answers 503 and no save has been touched.
The copy lands in the same folder the **Start over** card in the Settings
section keeps its configurations in, and that card is where the same action
lives once the sorter is running. It is not listed there: the card only offers
back files this build could read, and this one is the file it could not.

### The save folder ... is not there.

cause: the folder recorded in your settings does not exist any more, and the
heading names it. An unplugged external drive is the usual reason; a folder
that was moved or renamed is the other.
fix: plug the drive back in, or put the folder back, and press **Look again**.
Nothing has been read and nothing has been written, and the folder is still
recorded in your settings, so nothing needs re-choosing. This page
deliberately offers you nothing else to press: a folder that is not there is
not a reason to sort a different account, so **Choose another folder** is a
link one click further away and nothing is pre-selected there.

### the settings file ... could not be read (...: ...), so this run uses the defaults. Saving from the Settings section rewrites it.

cause: `settings.json` is not valid JSON, and the reason is quoted. A hand edit
or a half-written file after something was interrupted are the two causes. The
sorter starts anyway, on the shipped defaults, because the page that would let
you fix it is served by the process that just failed to read the file.
fix: press **Save settings** in the Settings section, which rewrites the file
from what is on screen, or repair the JSON yourself at the path in the message.
Note that this run's port is a default rather than a value you chose, so it
will not adopt a sorter already running on that port: you get your own, on the
next free port. Nothing was written to any save.

### There is no folder at ..., so there was nothing to look in. Check the path, or the drive it is on. Nothing was changed.

cause: the folder you typed on the first-run page does not exist. A typo, or a
drive letter that is not mounted.
fix: check the path and the drive, and paste it again. This is the separate
sentence for a path that is simply absent; "That folder holds no `save*.hg`
files" is the other one, for a folder that does exist and holds no saves. Your
setting was not changed.

### there is nothing this page can retry; restart the sorter

cause: you pressed retry on a reduced-mode startup page whose problem is not
one retrying can fix, missing data files being the usual one.
fix: do what the page itself says, then start the sorter again.

### This configuration file was written by a newer version of the sorter.

cause: a newer sorter has used this same config folder, and this build will not
guess what changed between the two formats.
fix: install the newer version again and use that, or point this one at a
different file with `--config <path>`. This page deliberately does **not** offer
to reset for you: a newer config is somebody's working configuration, and nothing
here can read it back once it is gone. Move it aside by hand if that is what you
want.

### This copy of the sorter is missing its data files.

cause: one of the files in `nms_sorter/data/` could not be read, so the install
is incomplete. A zip that only half extracted, an antivirus that quarantined a
file, and a checkout with a data file deleted all look like this.
fix: unzip the release again into an **empty** folder, or re-clone the
repository. The tables are not rebuilt at start-up and there is nothing you can
run to recreate them: they are generated from a decompiled game install by
whoever maintains the tool (`docs/POST-PATCH.md`), and they ship with it.

### This needs Python 3.9 or newer and this is Python ...

cause: the interpreter you started it with is older than the declared floor. This
line is printed before anything else loads, on purpose: the modules below use
syntax a 3.8 interpreter cannot even parse, and a `SyntaxError` pointing into
`server.py` would tell you nothing. It exits with code 4.
fix: install a current Python from https://www.python.org/downloads/ and tick
"Add python.exe to PATH" in the installer, then start it again. Or use the
packaged `NMS-Sorter.exe`, which carries its own Python.

### Which folder holds your No Man's Sky saves?

cause: nothing is wrong. This is the first-run page, shown whenever no save
folder has been chosen yet, and it is listed here only because it is a sentence
you can read on screen and search for.
fix: pick a folder, or type one. `docs/GUIDE.md` section 2 walks through it.

## Banners, and warnings that are not ours

Messages the page puts up about the state of a save, rather than refusals of
something you asked for, plus one warning Windows puts up about the download.

### another copy of the sorter was started and closed ...

cause: nothing is wrong. You double-clicked `NMS-Sorter.exe` while one was
already running. The second copy found the port taken, recognised the sorter
that had it, opened this page and closed itself, and this toast is it saying so.
Before it existed, a second launch looked like a launch that did nothing.
fix: nothing. The page in front of you is the sorter that is running. The
notice is stripped out of the address bar afterwards, so a reload or a bookmark
does not repeat it. If you really want a second instance for a different save
folder, start it with `--port` and a free number.

### sorted by this tool at ...

cause: nothing is wrong, and this replaces a warning. The save file's
modification time is later than the game's own timestamp inside it, which
normally means something other than the game wrote to it; here the file still
hashes to what the last apply's manifest recorded writing, so the something was
this tool. The line names when, and it carries a tick rather than a warning
glyph.
fix: nothing. Load the save in game when you are ready. If the game had written
since, the line would be the warning instead.

### This save's metadata disagrees with its size.

cause: the `mf_` metadata file records a size the save on disk does not have.
The page shows this as a red banner, and puts a warning glyph beside the save
in the picker so you see it before you select it. The game reads that size to
decide how much of the save to load, so it may refuse to load this one.
fix: go to the main menu in game, or close the game, restore the most
recent backup from the Plan section, and load the save once before playing
on. Every backup holds both files, so a
restore puts the pair back into agreement. Nothing the sorter does next makes
it worse: an apply refuses at step 8 on a save whose metadata it cannot account
for.

### The sorter has stopped. Close this tab. Double-click NMS-Sorter.exe to start it again.

cause: nothing is wrong. The sorter was stopped on purpose -- by **Stop the
sorter** in the Settings section, by **Stop** at the foot of the rail, by
ctrl-c in the window it was started in, or by the idle exit after
`idle_exit_minutes` minutes with no request from the page. The whole page is
replaced by that one sentence because every control on it talked to a server
that is no longer there.
fix: close the tab. Start it again by double-clicking `NMS-Sorter.exe`, or with
`python -m nms_sorter` from the repository, or `packaging\start.bat`. Nothing
was written to your save, and your settings, configuration and backups are
where they were. If it stopped without you asking, it was the idle exit: set
`idle_exit_minutes` to 0 in the Settings section to switch that off, or leave
the page open, which is what keeps it alive.

#### Windows protected your PC (SmartScreen), or your antivirus quarantines NMS-Sorter.exe

cause: the executable is an unsigned one-file PyInstaller bundle, which is the
exact shape both SmartScreen and most antivirus heuristics are suspicious of: a
single binary that unpacks itself and runs an interpreter. Nothing is wrong
with your machine and nothing is necessarily wrong with the download.
fix: check the hash first, before deciding anything. `Get-FileHash
NMS-Sorter.exe -Algorithm SHA256` in PowerShell, compared with the line in
`SHA256SUMS` on the release page, tells you whether you have the file that was
built. If it matches, SmartScreen's "More info" then "Run anyway" is safe in
the sense that the bytes are the published ones; if it does not match, delete
it and say so in an issue. Add an antivirus exclusion only with that check
behind you, or skip the binary and run it from source (`docs/SOURCE.md`),
which is the same program. A report naming a specific antivirus engine is
worth filing: the documented fallback is a one-folder build instead
of one-file.

## Refusals

### a container name cannot be blank; delete the entry to go back to the name the save carries

cause: a `labels` entry in your configuration file has an empty name, or one
made only of spaces or tabs. It cannot be reached through the rename box on a
card, which treats a blank box as "delete this name"; it comes from editing
`config.json` by hand.
fix: delete the entry. That is what a blank one was trying to say, and it puts
the card back to the name the save and the sorter work out between them. The
config was not saved.

### a container name has to be text, not ...

cause: a `labels` entry in your configuration file has a number, a list or an
object where the name belongs, so there is nothing to draw. It used to be drawn
anyway, as `42` or `['x']`.
fix: put the name in quotes: `"labels": {"chest7": "Nanite stock"}`. Names are
text, one per container key. `docs/RULES.md`, section **Renaming a container**,
has the whole field. The config was not saved.

### a container name is at most 64 characters and this one is ...

cause: a `labels` entry in your configuration file is longer than any of the
places that draw it can lay out: the card in the Save section, three
destination pickers, and the plan's container table.
fix: shorten it. 64 characters is the ceiling here; a name written into the save
itself by `auto_name` has a tighter one of 40, which is the game's own field
limit. The config was not saved.

### a rule cannot have both keep and stock ...

cause: one item rule carries both a `keep` and a `stock`. They are the same
idea said twice: `stock` already holds its number back in its own container,
and a second floor somewhere else would fight it.
fix: decide which one you meant. `stock` if you want one container kept topped
up and drained no lower; `keep` if you want a floor where the item already is
and the surplus sent on. `docs/RULES.md` has both. The config was not saved.

### a sorter is already running at http://127.0.0.1:... ; opening that instead

cause: not an error. You started a second sorter while one was already
listening on that port, usually by double-clicking the exe twice, and it handed
you the window the first one is serving rather than starting a second copy.
fix: use the page that opened; it is the running sorter. Two sorters on one
save folder is the thing this avoids: they would mint plans against each
other's writes. If you actually want a second instance for a different save
folder, start it with `--port` and a free number. The first process is
untouched and nothing was written.

### a technology grid is never repacked: adjacent modules of the same family grant adjacency bonuses and supercharged slots are fixed cells ...

cause: a technology grid is in your `tidy` list. Cell positions in a grid are
not cosmetic: modules of the same family boost each other when they are
adjacent, and supercharged slots are specific cells.
fix: take that container out of `tidy`. This refusal exists because a repacked
grid looks perfectly healthy while the ship quietly flies slower, which is the
worst kind of bug to ship. Nothing was written.

### already in ..., which is where the rule sends it

cause: the rule for this item names the very container it is already in, so
there is nothing to do. Common when a chest is both a source and the
destination for its own category.
fix: nothing. This is a skip, not a problem. If you expected a move, check
which container the item is actually in; the plan row names it.

### an apply or restore is running; wait for it to finish, then stop the sorter

cause: you pressed **Stop the sorter** (or **Stop** at the foot of the rail,
or sent `POST /api/quit`) while an apply or a restore was part way through. The
sorter will not stop underneath a write. Nothing was stopped, and the refusal
itself changed nothing.
fix: wait. An apply takes seconds and the Plan section shows its steps as
they happen; press stop again once it has finished. Nothing is lost if you
forget to: the write completes either way, and the backup folder named on the
Apply card holds the original.

### an apply or restore is running; wait for it to finish, then try again

cause: **Start over from the shipped layout** or **Use this one** was pressed
while an apply or a restore was part way through. Neither will swap the
configuration out from under a write: the plan being applied was computed
against the configuration in use, and the report you are about to read
describes that run.
fix: wait, then press again. Nothing was changed and no file was kept, so the
card is exactly as it was.

### another sorter instance is writing this save

cause: a lock file beside the save says another sorter is part way through
writing it, and that the process holding the lock is still alive. Two writers
on one save is how a save gets torn.
fix: find the other sorter and let it finish, or close it. A lock left behind
by a crash is removed automatically once it is ten minutes old and its process
is gone. Nothing was written.

### another sorter instance is writing this save (pid ..., since ...)

cause: the lock file beside this save says another sorter is part way through
writing it, and names the process and the time it started. This is the same
refusal as the one above, with the pid and the timestamp added so you can tell
a live writer from a leftover.
fix: find that process and let it finish, or close it. A lock is only removed
automatically when it is over ten minutes old **and** its process is gone, so a
live one always means a real writer. Nothing was written.

### backup: ... does not exist

cause: step 2 went to copy the save and the file named is not there. Between
choosing the save and pressing Apply, it was moved, renamed or deleted.
fix: reload the page so the save list is re-read, pick the save again and re-run the dry run. Nothing was written, and no backup was taken.

### backup: copy of ... does not match the original

cause: step 2 copied the save, hashed both files with SHA-256, and got two
different answers. A failing disk, a full disk and antivirus rewriting a file
mid-copy all look like this.
fix: check free space on the backup drive, then try again. If it repeats, point
`--backups` at a different disk. Nothing was written to your save: this is the
backup step refusing to continue without a copy it can prove.

### bucket ... is already routed to ... by rule ...; this exact pair can never fire

cause: two rules name the same category and the same destination. The second
can never do anything, because the first already sends everything there. Note
that the same category named with *different* destinations is not this error:
that is an overflow chain and it is allowed.
fix: delete the duplicate rule, or change its destination to make it the second
leg of a chain. The config was not saved.

### bucket ... is already covered by rule ...; this exact pair can never fire

cause: two category rules name the same category and neither names a
destination, so the second says nothing the first did not. The wording differs
from "already routed to" because there is no destination to put in the message.
fix: delete the duplicate, or give one of them the destination that makes it
different. The config was not saved.

### container round trip: reframing the payload does not decode back

cause: still step 4. The payload survived decoding, but re-packing it into the
game's block framing and reading it back produced something else. That is the
compression or block layer disagreeing, not the JSON.
fix: nothing you can configure. Nothing was written and the backup is intact.
Open an issue with the health panel text and the save `Version`.

### encode, decode, compare: the framed file does not decode back to the payload we built

cause: still step 6, one layer down: the bytes that would have gone to disk do
not decode back to the payload they were built from.
fix: nothing you can configure. Nothing was written and the backup is intact.
Open an issue with the health panel text.

### encode, decode, compare: the transformed document does not survive its own serialisation ...

cause: step 6. The edited document was written out and read back, and the two
do not agree, with the first differing path named. The edit itself is fine; the
round trip over it is not.
fix: nothing you can configure. Nothing was written to your save and the backup
is intact. Open an issue with the health panel text and the path from the
message.

### game: NMS.exe is running as pid ... Go to the main menu in the game, then tick "I am at the main menu, not in a loaded save" and try again.

cause: step 1. No Man's Sky is running and nobody has said where in it you
are. On the main menu the game is not writing your save, and applying from
there is the normal way to use this tool; in a loaded save the game holds its
own copy in memory and autosaves about every 60 seconds, so an edit written now
would either overwrite play or be overwritten by it. No process check can tell
those two apart, so you say which.
fix: in the game, save; open Options and **Quit to main menu**; stay on the
menu. Then tick **"I am at the main menu, not in a loaded save"** on the Apply
card and apply again. Load the save afterwards. Closing the game entirely
works too, and then the tick is not shown at all. Nothing was written.

### game: could not read the process list (...), so it cannot be told whether NMS.exe is running. Refusing rather than guessing: something on this machine is blocking the process list, and apply needs it.

cause: step 1 could not answer the question at all. Reading the process list
failed and the reason is quoted in the message. A check that cannot run counts
as a refusal, and the main-menu tick does not cover it: that tick says where
you are in the game, not whether the game is up.
fix: on Windows the usual causes are a security product blocking process
enumeration or an unusually locked-down account. Allow the sorter to enumerate
processes, or run it as the account that plays, and try again. Off Windows this
is expected, because the process check needs Windows. Nothing was written.

### identity round trip: this save does not re-serialise byte-for-byte; first difference at offset ...

cause: step 4, and the one that makes everything after it safe. The sorter
decoded the save and re-encoded it without changing anything, and got different
bytes back. Something in this file is outside what the codec has been proved
against, so the tool does not know enough about it to edit it.
fix: nothing you can configure. Nothing was written, and the backup folder
named on the Apply card holds a verified copy. Please open an issue with the
health panel text and the save `Version`: the offset in the message is what
makes it diagnosable.

### keep_in ... is not one of your sources, so nothing is ever drained from it and the floor can never bind

cause: a `keep_in` names a container that is not in your sources list. A keep
floor only ever applies while a container is being drained, so a floor on a
container nothing drains can never do anything.
fix: add that container to your sources, or move the floor to a container you
do drain. This is a warning, not an error: the config saves, the floor simply
has no effect.

### keep_in names ... but there is no keep to apply there

cause: a rule names a `keep_in` but has no `keep`. The container is the scope
of a floor, and there is no floor for it to scope.
fix: add the `keep` you meant, or drop the `keep_in`. If you wanted the
container topped up rather than a floor held, that is `stock` and `stock_in`.
The config was not saved.

### key ... must be lower-case letters, digits and underscores ...

cause: a custom category key has a space, a capital or punctuation in it. The
key is written into the config file and compared exactly, so a key that is easy
to mistype is a rule that silently stops matching.
fix: use lower-case letters, digits and underscores: `my_junk`, not `My Junk`.
The label is the part you see on the page, and that one can say anything. The
config was not saved.

### labels has to be an object mapping a container key to a name, not ...

cause: the `labels` field in your configuration file is not an object. The
shape it wants is one container key to one name, `{"chest7": "Nanite stock"}`;
what is most often typed instead is the bare key, `"labels": "chest7"`.
fix: wrap it: a `{` , the container key in quotes, a `:`, your name in quotes, a
`}`. Or delete the field and use the rename box on the card, which writes it for
you. Until this build, a file like this saved without a word and then left the
Save section unable to draw at all. The config was not saved.

### merge-only mode keeps ... behind and this stack holds ...

cause: `create_stacks` is off, so `min_source` units have to stay in the source
stack, and this stack does not hold more than that. With `min_source` at 1 a
stack of 1 cannot move at all.
fix: turn on "create new stacks" in options if you want the stack moved whole,
or lower `min_source`. Nothing was written.

### metadata is updatable: this metadata format (...) is not one this build knows how to update; only ... was decoded field by field

cause: step 8, checked before anything is written. The `mf_` file beside this
save uses a metadata format this build has not decoded field by field, so it
cannot patch the two size numbers afterwards.
fix: nothing you can configure. Nothing was written and the backup folder named
on the Apply card holds a verified copy. Open an issue with the health panel
text and the format number from the message: a new format is a real change in
the game, not a fault on your machine.

### metadata is updatable: this save's metadata carries an integrity hash this build cannot recompute; refusing to write a stale one

cause: still step 8. The bytes from 0x08 to 0x38 of the `mf_` are meant to be
zero on every save seen so far. On this one they are not, which means the file
carries a checksum this build cannot recompute.
fix: nothing you can configure. Writing the save and leaving a stale hash
beside it is how a pair gets rejected, so the run stops instead. Nothing was
written, the backup is intact, and this is worth an issue with the health panel
text.

### metadata is updatable: this save's metadata could not be decoded (...), so it cannot be updated after the write; refusing rather than leaving the pair disagreeing

cause: still step 8. The `mf_` file beside this save could not be decrypted or
parsed, and the reason is quoted. A truncated, hand-edited or copied `mf_` is
the usual cause, since its key is derived from its own file name.
fix: check that the `mf_` has not been renamed, and that both files came out of
the same save slot. Nothing was written and the backup is intact.

### metadata: the rewritten mf_ does not decrypt back to what we wrote

cause: also step 10. The `mf_` was rewritten, read back and decrypted, and it
does not match what was meant to go in. The slot key is derived from the file
name, so a renamed or copied `mf_` is the usual cause.
fix: restore both the save and its `mf_` from the backup folder named on the
Apply card, and check that neither file has been renamed. Then open an issue
with the health panel text.

### metadata: the size patch would touch a byte outside 0x38..0x3F

cause: step 10. Updating the `mf_` file should change exactly two 32-bit
numbers, the decompressed size at 0x38 and the on-disk size at 0x3C. The patch
this build computed would have changed a byte outside those eight, so it was
abandoned.
fix: the save itself was written and verified; the `mf_` was not touched. The
pair now disagree about sizes, so restore both files from the backup folder
named on the Apply card and open an issue.

### more items (...) than usable cells (...)

cause: tidy was asked to repack a container holding more items than it has
cells it may use. Cells holding a pinned item, one the table does not know or
one with an unprintable id, are not available, which is usually where the
shortfall comes from.
fix: run the sort first so the container is drained, or take it out of `tidy`.
Tidy refuses rather than dropping an item. Nothing was written.

### no bucket named ...; a rule that can never match is a typo

cause: a rule names a category that does not exist. Usually a typo, or a custom
category that has since been deleted or renamed.
fix: pick the category from the list in the Categories section rather than
typing it, or re-add the custom category with that exact key. The config was
not saved.

### no metadata file beside this save, so whether it is an expedition cannot be confirmed from the mf_; the season inventory is empty, so it is treated as a normal save

cause: a plan note, and the mildest of the three levels a save gate has. The
save carries expedition data but its season inventory is empty, and there is no
`mf_` beside it to read the season word out of, so the expedition test cannot
be answered either way. It is treated as a normal save and said out loud rather
than assumed.
fix: nothing, and planning and applying both work. If this save really is an
expedition, put its `mf_` back beside it and the gate will refuse properly; a
save with no `mf_` is usually a hand-copied one, and copying the pair rather
than the `.hg` alone is the fix for more than this.

### no plan fingerprint; run a dry run first

cause: apply was called without a plan fingerprint. Apply only ever runs a plan
you have been shown, and the fingerprint is how it knows which one.
fix: run the dry run in the Plan section, read it, then apply from that same
page. If you are driving the API yourself, pass the `fingerprint` the plan
returned. Nothing was written.

### no store, no keep, no stock and no pin: this rule does nothing

cause: an item rule names an item and then nothing to do with it. That is
usually a half-finished edit: you added the row and have not said where the
item should go yet.
fix: give it a destination, a keep, a stock or a pin, or delete the row. This
is a warning rather than an error, so the config still saves; the rule just has
no effect on any plan.

### no usable destination: ...

cause: every container the rule named was rejected, and the message lists why
for each one: not in this save, no cells in this save, an extractor core, or a
technology grid. A sold ship lands here, and so does a rule whose only
destination is a cargo grid the game merged into the main one -- those have no
cells, so nothing can be put in one.
fix: point the rule at a container this save actually has. The Save section
lists every container it can see, and a rule that also names a live container
alongside a merged one is fine as it is: the live one takes the items. Nothing
was written.

### nothing could be placed: ...

cause: the destinations exist and are usable, but there was no room in any of
them: every matching stack is at its cap and there is no free cell, or the
chain is at its `fill` ceiling.
fix: add a second container to the chain, raise the `fill` ceiling, or empty
the destination. If `create_stacks` is off, the refusal says so and the fix is
to put one unit in the destination by hand or turn that option on. Nothing was
written.

### nothing else changed: ... path(s) outside the containers this plan names differ ...

cause: step 7, the guard that compares the whole document before and after.
Something changed outside the containers your plan named, and the message gives
the count and an example path.
fix: nothing you can configure, and do not retry blindly. Nothing was written
and the backup is intact. Open an issue with the health panel text and the
example path: a change the plan did not name is exactly what this guard exists
to catch.

### nothing else changed: the change set is larger than the guard can enumerate; refusing rather than writing blind

cause: step 7 lists every path that differs before and after, and stops at a
limit. Past that limit the list is no longer "every difference", so the
comparison it feeds stops meaning anything.
fix: plan a smaller run: lower `max_moves` in options, or narrow your sources,
and apply in stages. Nothing was written and the backup is intact. A guard that
cannot see the whole change set has to say so rather than wave it through.

### nothing else changed: the plan touched '...', which is not a container this build can name a path for. Refusing rather than writing blind.

cause: still step 7. The plan changed a container whose location in the save
document this build cannot state. Without a path for it, the guard cannot check
whether the change was in bounds, so it cannot vouch for the write at all.
fix: take that container out of your sources and your rules for now, and report
it. Nothing was written and the backup is intact. This normally means a
container family the build does not know yet, and a save that shows it is worth
attaching to the issue.

### one of these stacks is damaged; merging would hide the damage

cause: two stacks of the same item in one container, and at least one carries a
non-zero `DamageFactor`. Pouring them together would average the damage away
and lose the fact that one was damaged.
fix: nothing, if you want the damage kept visible. Deal with the damaged stack
in game and the two will merge on the next run. Nothing was written.

### pinned by never_move

cause: this item id is in `never_move`, which pins it wherever it is before any
other rule is looked at. It is the strongest statement in the config.
fix: nothing, if that was the intent. If you want it sorted again, remove the
id from `never_move` in your configuration file. A rule cannot override it.

### plan matches what you saw: the plan computed from these bytes fingerprints ..., but you approved ...

cause: step 5. Apply re-plans from the exact bytes it is about to rewrite and
compares the fingerprint with the one you approved. They differ, so something
changed between the dry run and now: the save was played, or the configuration
was edited in another tab.
fix: run the dry run again, read it, and apply that. Nothing was written. This
gate is the whole reason apply cannot surprise you.

### plan matches what you saw: the plan is empty; there is nothing to do

cause: step 5 re-planned and the plan came out with no rows. Usually the work
was already applied, in this window or another one.
fix: nothing. Re-run the dry run to see the current state. Nothing was written.

### re-read from disk: mf_ size_decompressed ... does not match the payload's ...

cause: still step 11, the other size field: the decompressed size recorded in
the `mf_` does not match the payload that was written.
fix: restore both files from the backup folder named on the Apply card, then
open an issue with the health panel text.

### re-read from disk: mf_ size_disk ... does not match the file's ...

cause: still step 11. The `mf_` file's on-disk size field and the actual size
of the save disagree. The game reads that number to decide how much to load.
fix: restore both files from the backup folder named on the Apply card. Do not
load the save first: a size field that disagrees with the file is exactly the
shape of a save the game may reject.

### re-read from disk: ... was written but does not hash to the backup's copy; the file on disk is ...

cause: a restore wrote a file back, read it again and hashed it, and got an
answer the backup's manifest does not record. The bytes that landed are not the
bytes that were sent. A failing disk, a full disk, and a sync client or
antivirus rewriting the file between the write and the read all look like this.
fix: do not load the save. Run the same restore again with the game closed and
any sync client stopped; if it repeats, copy both files out of the backup folder
by hand and check the drive. The first eight characters of what is on disk are
in the message, which is what makes it worth quoting in an issue.

### re-read from disk: re-decoding the written file differs at ...

cause: also step 11. The file decoded, but comparing it path by path with what
was written found a difference, and the message names the first one.
fix: restore from the backup folder named on the Apply card and open an issue
with the health panel text and that path. Do not keep playing on the written
save.

### re-read from disk: the file on disk does not decode to what we wrote

cause: step 11. The write completed, the file was read back from disk, and it
does not decode to the document that was written. This is the step that catches
a lying filesystem.
fix: restore from the backup folder named on the Apply card, then check the
drive. The save on disk is not what this tool intended to write, so treat it as
suspect until it is restored.

### restore: ... has changed since this backup was taken; the game (or something else) has written it since, and restoring would discard that. Copy the backup out by hand if you are sure

cause: the apply this backup belongs to recorded the hashes it left on disk,
and the save in your folder no longer matches them. Something has written to it
since: almost always the game, because you played. Putting the backup back
would throw that play away, so the restore stops before writing anything. This
is the check behind a greyed-out **Undo this apply**: the Backups card asks the
same question before it offers the button, and says "the save has been written
since this backup was taken; restoring would discard that play" beside it.
fix: decide which you want. To keep playing, do nothing; the backup stays on
disk and nothing was changed. To go back anyway, go to the main menu or
close the game, and copy the save and its `mf_` out of the backup folder by
hand. A backup whose manifest
predates this check cannot be compared, and then the report says so rather than
guessing: "the manifest does not record what the apply wrote, so whether the
save changed since cannot be checked".

### restore: the manifest names a file outside its folder (...); refusing

cause: a backup's `manifest.json` names a file with a path in it rather than a
plain file name. A restore only ever copies the two files sitting in the folder
it was given, so a name pointing anywhere else is refused rather than followed.
fix: nothing to configure, and no backup the sorter wrote can look like this.
Copy the two files out of the folder by hand if you trust it, and open an issue
with the manifest: a hand-edited or hand-assembled folder is the likely cause.
Nothing was written.

### restore: this backup has no manifest, so its contents cannot be verified; copy the files back by hand if you are sure

cause: the backup folder carries no `manifest.json`, so there is nothing
recording which save its files came from or what they hashed to when they were
copied. Folders written by a version before manifests existed look like this,
and so does one somebody assembled by hand.
fix: the files are probably fine, but nothing here can say so. From the main
menu, or with the game closed, copy the save and its `mf_` out of that folder
back over the save folder yourself. The Backups card marks these rows
"no manifest", and retention never prunes them, so the folder will still be
there. Nothing was written.

### restore: this backup was taken from ...; refusing to write it into ...

cause: the backup records which save folder it was taken from, and that is not
the folder you are restoring into. Restoring one profile's save over another's
is the mistake this catches, and it is not one a message after the fact can
undo.
fix: select the save folder the backup came from, the one named in the message,
and restore there. The route does take a `force` flag for the case where you
mean it; the page does not offer one, because a cross-profile restore is not
something to make easy. Nothing was written.

### restore: this backup's manifest does not name a save file, so there is nothing to put back

cause: the folder has a manifest, but its `save` entry is empty. That means the
apply it belongs to failed at the backup step itself, before a save was ever
copied in.
fix: use a different backup; this one holds nothing to restore. If every row
looks like this, the backup folder is the thing to look at, and the log will
name why the copy failed at the time. Nothing was written.

### verify the backup: ..., so this backup is not one to restore from

cause: a file in the backup no longer hashes to what its own manifest records,
or is the wrong size, or is missing. The message leads with which. Every copy
is proved **before any of them is written**, so this refusal means nothing has
been put back.
fix: use a different backup. The Backups card shows the same fact as "hash
differs" on the row, so you can pick one that still verifies before pressing
anything. A folder whose contents changed under it is worth an issue: nothing
in this tool writes there after the copy.

### verify the backup: ... could not be read from this backup (...), so this backup is not one to restore from

cause: one of the two files in the backup folder could not be read at all, so
it cannot be checked against the manifest. Distinct from the hash and size
wordings beside it: this is a read that failed, not a comparison that
disagreed.
fix: check the backup folder is readable, then try again, or pick a different
backup. Every copy is proved before any of them is written, so nothing was put
back.

### source ... is a technology grid or a single cell; skipped

cause: a plan note. One of your sources is a technology grid, or a container
with a single fixed cell, so there is nothing in it this tool will move.
fix: nothing, and nothing is wrong. Take it out of your sources to stop the
note. Technology is never sorted whatever a rule says, because cell positions
carry adjacency bonuses.

### stock container ... is not in this save

cause: a `stock_in` names a container key this save does not have at all.
Distinct from "cannot receive items right now", which is a container that
exists and is unavailable: this one is a key that is not in the save.
fix: pick the container from the list in the Categories section rather than
typing the key, and check you are on the profile you meant. The config was not
saved.

### stock needs both an amount and a container

cause: a rule has one half of a stock: an amount with no container, or a
container with no amount. Stock is two-sided by nature, "keep this many in this
one place", and neither half means anything alone.
fix: fill in both, and make sure the stocked container is also one of your
sources, or there is nothing to pull from. The config was not saved.

### stocked in ...; no category rule for the surplus

cause: a plan reason, not an error. A stock rule names a container to keep
topped up but no destination for the surplus, and the item's category has no
rule either, so the stocked container is the only place anything can go.
fix: nothing, if that is what you meant: the container is still stocked. Give
the rule a `store`, or route the item's category, if you want the surplus to go
somewhere. This line used to read "bucket rule 1", naming a rule that did not
exist and sending people looking for it.

### stopped at max_moves = ...; raise it in options or run twice

cause: the plan reached the `max_moves` limit and stopped there. The limit
exists so a first run on a full save is reviewable rather than being two
thousand rows.
fix: apply this plan and run again, which is safe and picks up where it left
off, or raise `max_moves` in options. Everything the plan does show is real;
the run was cut short, not corrupted.

### stopped creating stacks: the new-stack budget of ... is used up. Raise max_new_stacks in options or run again.

cause: the run used up `max_new_stacks`, the limit on how many empty cells one
plan may fill, so later rows moved less than they wanted or nothing at all.
fix: apply this plan and run again, which picks up where it left off, or raise
`max_new_stacks` in options. The warning exists because exhaustion used to be
reported only as a partial row, which reads as "the chest is full", a fact
about your save rather than about a limit you set.

### technology is never sorted

cause: the stack is installed technology. Its `Amount` is a charge level, not a
count, and its cell position can matter for adjacency bonuses.
fix: nothing. Technology is skipped by design, whatever a rule says. Move it in
game if you want it moved.

### technology: Amount is a charge level, not a count, so combining two stacks is meaningless

cause: the merge phase found two technology stacks that look identical and
refused to combine them. Adding two charge levels together does not produce a
fuller anything.
fix: nothing. This is the merge phase declining to do something meaningless.
Nothing was written.

### the configuration was saved by another tab (or another copy) at ... after this page loaded it; reload from disk, then redo your change

cause: two tabs. This page loaded the configuration, another tab (or another
copy of the sorter) saved it after that, and this page then tried to save on
top. The stamp is read off the file rather than out of memory, so the other
writer may be a different process entirely. Nothing was stored, and the
configuration on disk is the other tab's.
fix: press **Reload from disk** to pick up the stored configuration, then make
your change again. Working in one tab at a time is the way not to meet this.
The stamp has a resolution of one second, so two saves inside the same second
cannot be told apart; that direction is the safe one, because a conflict can be
missed but never invented.

### the default save folder is a Windows path, so there is nothing to look in here; pass --folder, or set the save folder on the Settings section

cause: the sorter looks for saves in the HelloGames NMS folder under %APPDATA%,
which means nothing on Linux or macOS, so there is no folder to probe and it
says so rather than reporting an empty result.
fix: start it with `--folder <path>`, or set the save folder in the Settings
section. Under Proton the saves are inside the prefix, typically under
`steamapps/compatdata/275850/pfx/`. Browsing and planning work from there; only
apply refuses.

### the item table does not know this id. That is normal after a game patch, not an error: it is left exactly where it is.

cause: you are holding an item whose id is not in the shipped table. This is
the normal state of the tool after a game patch: the table lags the game build,
so there is always some content it has never heard of.
fix: nothing, and nothing is at risk: an unknown item is left exactly where it
is rather than guessed at. If you want it sorted, give it a category with a
per-item override, or wait for a data regeneration. `docs/RULES.md` explains
`unsorted`.

### the keep floor of ... in ... holds all ... of these back

cause: a `keep` floor covers everything of this item in that container, so
there is no surplus to move. The floor is measured against the container's
whole holding of the item, not against one stack.
fix: nothing, if the floor is what you wanted. Lower the `keep`, or scope it to
one container with `keep_in`, if you expected some of it to move. An unscoped
floor applies in every source it meets.

### the move cap of ... for this item is already used up this run

cause: the rule has a `move` cap and this run has already moved that much of
this item. The cap is spent across every source, not per container.
fix: apply and run again if you want more moved, or raise the cap. Nothing was
written.

### the process check needs Windows, so it cannot be told whether NMS.exe is running; plan and browse work here, apply does not

cause: you are on Linux or macOS. Apply's first step reads whether No Man's Sky
is running, and the only process check this build trusts is the Windows one;
under Proton the game is invisible to a native process list, so an answer of
"not running" here would be a guess about the one thing that loses play. This
is a refusal by design, at step 1, before any backup is taken.
fix: everything except apply works here: the page, the item table, the planner,
the dry run. To write, run the sorter on Windows against the same folder; the
configuration file is portable and nothing in it is platform-specific. Under
Proton the save folder is typically `<steam library>/steamapps/compatdata/27585
0/pfx/drive_c/users/steamuser/AppData/Roaming/HelloGames/NMS`, and `--folder
<path>` points the sorter at it, because the automatic probe is a Windows path
and says so.

### the save file has changed on disk since that plan was printed. Re-run the dry run.

cause: the save's signature no longer matches the one recorded when the plan
was minted. The game saved, or something else wrote to the file, between the
dry run and the apply.
fix: re-run the dry run so the plan is computed against the file as it is now,
read it again, and apply. Nothing was written.

### the save folder holds more than one file named ..., differing only in upper and lower case (...), so it cannot be told which one is meant; rename or move one of them and try again

cause: the folder holds two files whose names differ only in case, such as
`save.hg` and `SAVE.HG`, and both of them answer to the name that was asked
for. Picking one would be picking which of your saves gets written to, so
nothing is picked. Windows cannot produce this; a Linux or macOS folder, or a
copy taken from one, can.
fix: rename or move one of the two so that one name answers. The message lists
both spellings. Nothing was read and nothing was written.

### the save folder ... could not be read (...)

cause: the save folder could not be listed, and the reason is quoted. A folder
renamed or unplugged since it was chosen is the usual one.
fix: check the folder still exists, then reload the page; change it on the
Settings section if it has moved. A folder that is missing outright gets its
own page instead, "The save folder ... is not there." Nothing was read and
nothing was written.

### there are no saveN.hg files in ...

cause: the selected folder holds no save files, so there is nothing to read.
This is the API's wording; the first-run page says the same thing in its own
words when you pick the folder by hand.
fix: check the folder in the Settings section, and use its **Find my save
folder** button to list the folders that do hold saves. A parent folder rather
than the profile folder is the usual cause. Nothing was read and nothing was
written.

### there is no ... in the selected save folder

cause: a request named a save file that is inside the chosen folder but does
not exist. Usually a stale page, after the file was deleted or the folder was
changed in another tab.
fix: reload the page so the save list is re-read, then pick the save again.
Nothing was read and nothing was written.

### there is no document called ... in this build; it ships ...

cause: a `/docs/` address named a file this build does not carry, and the
message lists what it does ship. The list of documents **is** the allow-list,
so an unknown name never becomes a path to open.
fix: use a link from `/docs/`, which lists everything available. A source
checkout serves the repository's `docs/` folder and the packaged executable
serves the copy inside the bundle; a wheel install may carry none, and then
`/docs/` says so rather than pretending they are missing.

### this config is version ...; this build reads up to 2. Update the sorter or move the file aside

cause: the configuration file was written by a newer sorter than this one. This
build reads up to version 2 and will not guess at what a later version changed.
fix: install the newer sorter again and use that, or point this one at a
different file with `--config <path>`. The file was not touched, and this build
will not offer to reset it for you: a newer config is somebody's working
configuration. An older file is not this message, it is migrated on read.

### this save has no BaseContext; saves older than Waypoint (4.0) are not supported (this one reports version ...)

cause: the save has no `BaseContext`, which every save since Waypoint (4.0)
has. Container paths, slot fields and the whole container model were checked
against post-Waypoint saves only, so there is nothing here that can read an
older one honestly.
fix: nothing, and this is a refusal by name rather than a crash halfway
through: the version it did find is in the message. A pre-Waypoint save needs a
container model nobody has measured. Nothing was read beyond the header and
nothing was written.

### this save reports version ..., outside the range this build was verified on (... to ...); continuing, because the round-trip and nothing-else-changed checks run on this file regardless

cause: step 1b, and **this is not a refusal**: nothing stopped. Your save's
root `Version` is outside 4670 to 4735, which is the set of versions this build
has been run against, and the apply went ahead anyway. The line is on the
record so a bug report says which version the write ran on.
fix: nothing. The version is a number nobody here has measured, not a known
problem: 4670, 4734 and 4735 have identical container layouts, and every break
anybody can name is structural and detected by shape somewhere else -- no
`BaseContext`, a compression change, an `mf_` format this build cannot update.
What proves the write is safe is steps 4, 6 and 7 on your actual file: it
re-serialises byte for byte, the edited document survives its own
serialisation, and nothing outside the containers your plan named differs. If
you would rather the old refusal, turn `strict_version_check` on on the
Settings section, or start the sorter with `--strict-version-check`. Either way
the backup was taken first. `docs/POST-PATCH.md`, step 7, has the full
reasoning.

### this save reports version ...; this build was verified on ... to .... Apply proceeds: the round-trip and nothing-else-changed checks run on this exact file. Settings can turn on `strict_version_check` to refuse instead.

cause: the save's own version caveat, on the page and on the plan, with
`strict_version_check` **off**, which is the default. Your save's root
`Version` is outside 4670 to 4735, the set of versions this build has been run
against. Nothing is stopping: this is a note, not a refusal, and it says so in
full rather than quoting the refusal's wording and then taking it back.
fix: nothing. What proves the write is safe is steps 4, 6 and 7 on your actual
file, not the number in it: it re-serialises byte for byte, the edited document
survives its own serialisation, and nothing outside the containers your plan
named differs. If you would rather be refused, turn `strict_version_check` on
in the Settings section, or start the sorter with `--strict-version-check`, and
the sentence below is what you get instead. `docs/POST-PATCH.md`, step 7, has
the full reasoning and the playbook for widening the range.

### this save reports version ...; this build was verified on ... to ..., and `strict_version_check` is on, so apply is refused.

cause: step 1b with `strict_version_check` **on**. That setting is off by
default, so if you did not turn it on -- or pass `--strict-version-check` -- you
will not see this sentence; you will see the note above instead. With it on, a
root `Version` outside 4670 to 4735 is refused.
fix: turn `strict_version_check` off in the Settings section and apply again;
it applies at once and needs no restart. If it is set by the flag, the field
says so and the flag has to go from the command line. Nothing was written, and the
refusal comes before the backup, so there is not even a backup folder. If you
want the range widened instead, `docs/POST-PATCH.md` is the playbook: it takes
a save at the new version, not a guess.

### this server has not printed a plan with fingerprint .... Apply only runs a plan you have seen.

cause: the fingerprint is not one this server has issued. Either the server was
restarted since the dry run, which clears the plan cache, or the configuration
was saved, which also clears it, or more than sixteen plans have been minted
since.
fix: run the dry run again and apply that plan. Nothing was written.

### this server was started with --read-only, so it will not write to a save. Restart without that flag if you meant to apply.

cause: this copy was started with `--read-only`, or `read_only` is on in the
settings. Everything except writing works: browsing, planning, the dry run.
fix: restart without the flag, or turn `read_only` off in the Settings section.
That field applies at once and needs no restart. Nothing was written.

## Refusals that begin with a value

These read as a container key, an item id or a number first, so they are listed
last, alphabetically by the word after the value.
Restore refuses with this same sentence, and deliberately: there is one wording
for "this server does not write", whichever route asked for it.

### ... bucket(s) have no destination, so anything in them stays put wherever it already is ...

cause: not a mistake, a fact. Some categories have no rule routing them, so
items in them are never moved. They stay wherever they are, which includes
sitting in a container meant for something else.
fix: nothing, if that is what you meant. An unrouted category is a real choice.
If you expected those items to be tidied, give the category a destination, or
add a `*` catch-all rule. The message lists which categories, by name.

### ... could not be read (...), so it was not put back and the configuration in use is untouched.

cause: **Use this one** was pressed on a kept configuration in the **Start
over** card and that file would not load: either it is not valid JSON, or it
was kept by a newer sorter. The file name and the reason are both in the
message.
fix: nothing has to be done. The configuration you were using is still the one
in use and still on disk, and the kept file is left exactly where it is; the
card lists it as "could not be read" with its button greyed. To get that
layout back, repair the JSON in a text editor and press the button again. A
file kept by a newer sorter needs that sorter, not this one.

### ... could not be read (...), so the rules already in memory are still the ones in use. Fix the file and reload again.

cause: **reload from disk** was pressed and the configuration file would not
load: either it is not valid JSON, or it was written by a newer sorter. The
path and the reason are both in the message.
fix: fix the file and press reload again. The rules already in memory are
untouched and still in force, which is why this is a refusal rather than a
restart into reduced mode: the sorter keeps working while you sort the file
out. Nothing was written.

### ... is a Stellar Extractor Core; the game refills those itself, so nothing is ever sorted into one

cause: a rule names an extractor core as a destination. Cores are drain-only:
the machine owns their slots and refills them, so anything put in one is in the
way of the next yield.
fix: point the rule at a chest. A core can be a *source*, and draining one is
the normal reason to list it. Nothing was written.

### ... is a technology grid; nothing is ever sorted into one, whatever a rule says

cause: a rule names a technology grid as a destination. A grid's cells are
fixed by the machine and its contents are installed technology.
fix: point the rule at a container that holds items. The grid variants are
never drawn in the Save section, and none of them can receive a sorted item.
Nothing was written.

### ... is already covered by rule ...; this exact pair can never fire

cause: two item rules name the same item and neither names a destination, so
they say the same thing about it. A rule that only pins or only keeps has no
destination to name, which is why this wording exists alongside the "already
routed to" one: there is nothing to name in the message.
fix: delete the duplicate. If you meant the two to differ, give one of them the
destination, the keep or the stock that makes it different. The config was not
saved.

### ... is not a bucket. An override pointing at a bucket that does not exist would silently do nothing.

cause: a per-item category override names a category that does not exist. Most
often a custom category was deleted while an override still pointed at it.
fix: point the override at a category that exists, re-create the custom one, or
delete the override. The config was not saved.

### ... never occupies a container slot, so it cannot be routed to one

cause: a rule, or a per-item override, names **System and Non-Inventory**.
Nothing in that category is ever in a container: it is currencies, reputation
deltas, settlement stat tokens, repair sockets and cooking placeholders, and a
destination for it is a row that could never move anything.
fix: delete the rule or the override. The category is not offered in any picker,
so this only reaches a hand-edited file; a file that still lists it in
`never_buckets` loads and is cleaned up on read, with a note saying so. The
config was not saved.

### ... is not a container in this save

cause: a source, a destination or a `tidy` target names a container key this save
does not have. A typo in a hand-edited config is one cause; a key from another
profile, or a chest number you have not built, is the other.
fix: pick the container from the list in the Categories section rather than typing
the key. The Save section shows every container this save exposes. As an error
this blocks the save of the config; the same condition met at plan time is a
note on the plan, and nothing was written either way.

### ... is not in the selected save folder

cause: a request named a save file that is not directly inside the folder you
chose. The sorter reads one folder and refuses anything outside it, including a
path that walks out and back in again.
fix: pick the save from the list on the page. If you meant a different profile,
change the save folder in the Settings section. Nothing was read and nothing
was written.

### ...: pinned, nothing moves it

cause: an item rule pins this item, either with `pin: true` or by naming
neither a destination nor a keep, which reads as a pin.
fix: nothing, if that was the intent. Otherwise give the rule a destination or
a keep, and note the plan row names which rule did it, by number.

### ...: the backup copy could not be read (...: ...); nothing was changed

cause: a file in the backup folder could not be read during a restore, with the
path and the reason in the message. A second sorter pruning while this one
restores will do it, and so will a sync client holding the file.
fix: try again, or pick a different backup. The save was not touched, which is
what the sentence promises: this refusal happens before anything is written.

### ...: the backup copy could not be read (...: ...); the save was written but this file was not, so the two files on disk disagree

cause: the same read failure, later: the save had already been put back and its
`mf_` had not. The sentence says what is on disk instead of making a promise it
can no longer keep.
fix: the pair now disagree, so do not load the save. Restore the same backup
again once whatever blocked the read has gone, or copy both files out of the
backup folder by hand with the game closed.

### ...: the file could not be read back (...: ...); nothing was changed

cause: a restore could not re-read a file it had written, so it cannot prove
the bytes arrived. Distinct from the backup-copy sentence above: this is the
file in your save folder, not the one in the backup.
fix: check the save folder is readable and run the restore again. This wording
is the one that reports nothing changed, so the write had not started.

### ...: the file could not be read back (...: ...); the save was written but this file was not, so the two files on disk disagree

cause: the same failure after the save had been written, so one of the pair is
the restored file and the other is not.
fix: do not load the save. Run the restore again, or copy both files back by
hand from the main menu or with the game closed, then load once.

### ...: the game has the save open (...: ...); go to the main menu, or close the game, and try again

cause: the write itself was refused by Windows because something else has that
file open, and in a loaded save that something is the game. This is the one
failure the main-menu flow prevents: on the menu the game is not holding the
save.
fix: go to the main menu in game, or close the game, and apply again. Nothing
was changed. If the game is not running at all, a backup tool or a sync client
is holding the file; close it and try again.

### ...: the game has the save open (...: ...); go to the main menu, or close the game, and try again. The save was written but this file was not, so the two files on disk disagree

cause: the same refusal, one file later: the save was replaced and its `mf_`
metadata could not be, so the pair on disk disagrees.
fix: **do not load the game yet.** Go to the main menu or close the game, then
restore the backup named on the Apply card, which puts both files back as they
were. Load the save once afterwards.

### ...: the save folder refused the write (...: ...); nothing was changed

cause: the filesystem refused a write, and the message names the path and the
reason. A read-only attribute on the save, a sync client holding the file and a
full disk are the three that happen.
fix: clear whatever refused it, close any sync client, check free space, and
try again. Nothing was changed, which is what the sentence promises: this
wording is only used before the save has been replaced. A file the *game* is
holding open has its own sentence, above.

### ...: the save folder refused the write (...: ...); the save was written but this file was not, so the two files on disk disagree

cause: the same refusal after the save had already been replaced, so the `mf_`
write or a restore's second file was the one that failed. The sentence says
what is on disk rather than promising something it can no longer promise.
fix: **do not load the game yet**; the save and its metadata disagree. Clear
whatever refused the write, run the same restore again, and load the save once
afterwards. The backup named on the Apply card holds both original files.

### ... units would not fit back into these stacks

cause: the merge phase added up two or more stacks and the total does not fit
back into them at their own caps. That should not happen and it means the
arithmetic disagrees with the save.
fix: nothing to do at your end. The group was rolled back untouched and nothing
was written. Please open an issue with the health panel text and the plan row:
this one is a bug, not a configuration mistake.

## Validation and plan messages

These appear next to the control that produced them, or on the plan, rather than
as a refusal on their own. They are listed here so a search finds them; the full
table of configuration messages, by level and field, is in `docs/RULES.md`.

### ... core buffers matched no room and are not offered

cause: the sorter matches each Stellar Extractor core buffer to the extractor
room it sits in, and one or more buffers matched no room at all. This is not
the ordinary case of a rebuilt room, which is matched and reported as a stale
buffer; it means a buffer sits somewhere in the freighter that has no room in
it, or the freighter's layout is one this has not been measured against.
fix: nothing is broken and nothing was written. Every buffer that *did* match
a room is offered as `extractorN` as usual; the unmatched ones are left out,
because nothing true can be said about which extractor they belong to. Worth
an issue with the sentence and the number of rooms shown in the Save section.

### ... is already a generated bucket; pick another key

cause: a custom category uses a key the shipped taxonomy now generates.
`corvette_parts` is the one that catches people out, because it used to be
something everyone added by hand.
fix: use the generated category and delete yours, or pick a different key. The
migration drops a custom category that has become generated.

### ... is not sortable right now

cause: `options.tidy` names a container that cannot be repacked at the moment.
fix: nothing; it is skipped. The same sentence appears for a source and for a
destination, with the field named beside it.

### ... must be a non-negative whole number, not ...

cause: `keep`, `fill`, `move`, `priority` or `stock` was given something that
is not a whole number of zero or more.
fix: whole numbers only. The field name is in the message, and a negative
amount is refused for the same reason.

### ... of ... extractor rooms have no core buffer

cause: your freighter base has more Stellar Extractor Rooms than the save has
core buffers to match them to. A room that has never produced anything may not
have a buffer yet, which is the harmless version; the other version is a room
whose buffer this build could not identify.
fix: nothing is broken and nothing was written. The rooms that do have a buffer
are offered as `extractorN` as usual, so a drain still works; the numbering
counts only the cores on offer. If the rooms have all been producing, it is
worth an issue with the sentence and the core and room counts from the Save
section.

### ...: ... left behind, the whole chain (...) is full

cause: a plan warning. Everything in the destination chain is at its ceiling or
has no room, so part of a stack stays where it is.
fix: add another container to the chain, raise the `fill`, or empty the
destination. What the plan does show is real.

### a `fill` ceiling needs a `store` to apply it to

cause: a rule sets `fill` but names no destination, so there is nothing for the
ceiling to be a ceiling on.
fix: give the rule a `store`, or drop the `fill`. `docs/RULES.md` explains
which end each guards.

### bucket ... is also in never_buckets; the explicit rule wins, which is probably what you meant

cause: a category you routed is also listed in `never_buckets`, which only ever
blocks the `*` catch-all.
fix: nothing, if you meant to route it: the explicit rule wins. Remove it from
`never_buckets` to stop the warning.

### config ... is not a JSON object

cause: the configuration file parsed as valid JSON that is not an object, a
list at the top level being the usual way.
fix: the outermost thing in the file has to be `{...}`. The startup page for an
unreadable config offers to move it aside.

### destination ... is not sortable right now

cause: a rule sends items to a container this save cannot receive into at the
moment: a ship you sold, or a chest you have not built.
fix: nothing, if you expect to have it back. It is a warning about this save
and not a mistake in the file.

### no destination container

cause: a category rule has no `store` at all.
fix: give it a destination, or delete the rule. A category with no rule is
simply never routed, which is a real choice.

### no label; the key will be shown instead

cause: a custom category has a key but no label.
fix: give it a label, which is the part you see on the page, or leave it and
read the key.

### stock container ... cannot receive items right now

cause: a `stock_in` names a container that is not sortable at the moment.
fix: nothing, if you expect it back. While it is unavailable the rule cannot
stock anything.

### this file is version ...; this build writes version ...

cause: the configuration in hand claims a version this build does not write. An
older one is migrated on read, so seeing this means something handed the
validator an unmigrated config.
fix: load it through the sorter rather than validating it directly. A newer
file has its own startup page.

### tidy target ... is not an allocated container

cause: `options.tidy` names a container this save does not have allocated, so
there is nothing to repack.
fix: nothing. It is skipped with this note. Take it out of `tidy` if it has
gone for good.
