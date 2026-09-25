# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
A data-only regeneration after a game patch bumps the patch version and
`DATA_VERSION`, and names the game build.

## [Unreleased]

### Fixed

- Apply checks the backed-up save and metadata again immediately before each
  file replacement. Detected concurrent changes, including added or deleted metadata,
  stop the write and preserve the other writer's bytes. Failures after the save
  was written remain marked as such; unexpected transform errors no longer
  leave their backup marked as an unfinished run.
- A container selected as both source and destination no longer passes items
  onward into a later overflow container. Earlier destinations can still be
  topped up, and explicit stock rules still send their surplus onward.
- Unknown items retain every slot and amount during duplicate merging as well
  as routing and tidying.
- Exact procedural-item rules take precedence over family rules, including
  pins. Priority and list order resolve competing rules of the same specificity.
- A file changed while being decoded invalidates the cached document on the
  next read instead of attaching the newer file's signature to old contents.
- The data generator can write its results to another Windows drive.

### Changed

- Save refresh and health read backup metadata without hashing every backup.
  The Backups screen and Restore still perform full content verification.
- Release publishing depends on the main test matrix and documentation tests.
- Regenerated item data from Steam build 25442159 with MBINCompiler 7.04.0-pre1.
  Added `BANNER_MAINFAM` and `BANNER_NMSA` to Cosmetics & Appearance. Existing
  item names, bucket assignments and stack multipliers are unchanged. English
  names for the two new IDs are absent from the localization inputs, so the
  existing ID fallback is retained.

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
