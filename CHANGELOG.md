# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
A data-only regeneration after a game patch bumps the patch version and
`DATA_VERSION`, and names the game build.

## [Unreleased]

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
