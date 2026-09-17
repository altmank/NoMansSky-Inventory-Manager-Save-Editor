# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
A data-only regeneration after a game patch bumps the patch version and
`DATA_VERSION`, and names the game build.

## [Unreleased]

### Changed

- An X acts. Removing a rule, a source, a destination from a category or a
  category you added no longer asks first: the press does it, the toast names
  what went and carries an undo for ten seconds, and "Undo last change" in the
  save bar keeps it after that. Delete or Backspace on a chip or a row is the
  same removal. What an undo cannot take back still asks: apply, restore from
  Backups, start over or putting a kept configuration back, throwing away
  unsaved changes, a bulk change to the selected rows in Items, and stopping
  the sorter.

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
