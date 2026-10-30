# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
A data-only regeneration after a game patch bumps the patch version and
`DATA_VERSION`, and names the game build.

## [Unreleased]

### Changed

- The item table is regenerated from game build 26295378 (7.10 Forsaken).
  225 ids are new: 216 corvette construction modules (the cockpits, habs, hull
  and deco parts the Forsaken traders sell) join ship construction alongside
  the existing structural parts, the Larva Tank helm and two banners join
  cosmetics, and two names the game moved (`HULK1`, `STA_79`) are taken as
  written. No existing item changed bucket.
- The six haunting mementos the Forsaken traders take (`SPOOK_PACKAGE1-6`)
  report as unsorted: their game category has no precedent in the table, and
  an unknown id is left where it is until a hand-check places it.
- A save that reports a version up to 4743 no longer carries the "outside the
  range this build was verified on" note. 4743 is what 7.10 writes now, and
  three saves re-serialise byte for byte and apply on it in the corpus.

### Fixed

- The table pull from a game install no longer stops on an archive whose chunk
  was stored rather than compressed. The texture archives store theirs
  verbatim, and the pull used to abort with "error determining content size
  from frame header".

## [1.0.2] - 2026-09-19

### Changed

- An X acts. Removing a rule, a source, a destination from a category or a
  category you added no longer asks first: the press does it, the toast names
  what went and carries an undo for ten seconds, and "Undo last change" in the
  save bar keeps it after that. Delete or Backspace on a chip or a row is the
  same removal. What an undo cannot take back still asks: apply, restore from
  Backups, start over or putting a kept configuration back, throwing away
  unsaved changes, a bulk change to the selected rows in Items, and stopping
  the sorter.

### Fixed

- The list of matching items under "Add a rule for" is whole. It used to open
  downward inside the Rules card, which cut it off 53 px under the box: the
  first row was readable and the other thirty-nine were not. It opens above
  the box now whenever there is more room there, follows the box when the
  section scrolls, and shortens to fit rather than run off an edge.

## [1.0.1] - 2026-09-17

### Fixed

- The save-version caveat is one complete sentence, written for the mode it is
  in: with `strict_version_check` off it says the apply proceeds and which
  checks run on the file, and with it on it says the apply is refused. It used
  to show the refusal's wording and then contradict it in a second sentence
  with no full stop between them, and it sent the player looking for a fixture.
- The Sources list on the Categories screen shows seven containers at
  1280x720 where it showed one, and removing a source asks in a box you can
  read: one line under the row, naming the container, with "yes, remove" and
  "cancel". Escape cancels. The No destination panel below it is a fold now,
  closed, opening to three rows.

## [1.0.0] - 2026-09-16

Initial release.

A local web page that reads one No Man's Sky save, shows every container, lets you say which
category of item goes where, and writes the save only after proving it can do so safely. Windows
executables, plus `python -m nms_sorter` on any platform for browsing and dry runs.
