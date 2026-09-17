#!/usr/bin/env python3
"""Cut one version's section out of `CHANGELOG.md`, for a release body.

    python packaging/changelog_section.py 1.0.0
    python packaging/changelog_section.py v1.0.0 -o dist/RELEASE_BODY.md
    python packaging/changelog_section.py --list

The release job (`.github/workflows/ci.yml`) runs this on a `v*` tag and hands
the file to `softprops/action-gh-release@v2` as `body_path`, so the notes a
release shows are the notes that were reviewed in the pull request rather than
a second copy somebody retyped on release day. Keep a Changelog's own shape is
the contract: `## [X.Y.Z] - YYYY-MM-DD` opens a section and the next `## ` line
closes it.

Exit codes: 0 wrote the section, 2 the tag names no section (which is a
deliberate release-blocking failure: a version with no changelog entry is a
release nobody can read), 3 the changelog could not be read.
"""
import argparse
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")

#: `## [1.0.0] - 2026-09-14`, `## [1.0.0]`, `## [Unreleased]`. The link-style
#: heading Keep a Changelog also allows (`## [1.0.0](compare/...)`) matches too,
#: because the version is read out of the brackets and the rest of the line is
#: not looked at.
HEADING = re.compile(r"^##\s*\[([^\]]+)\]")


def normalise(version):
    """`v1.0.0` and `1.0.0` are the same section; a tag carries the `v`."""
    v = (version or "").strip()
    if v[:1] in ("v", "V"):
        v = v[1:]
    return v


def sections(text):
    """-> [(version, [lines])] in file order, headings excluded.

    The heading is dropped on purpose: a GitHub release already shows the tag
    and the date above the body, and repeating them is the kind of duplication
    that goes stale on one side only.
    """
    out, current = [], None
    for line in text.splitlines():
        m = HEADING.match(line)
        if m:
            current = (m.group(1).strip(), [])
            out.append(current)
            continue
        if current is not None:
            current[1].append(line)
    return out


def section(text, version):
    """-> the body of `## [version]`, stripped of blank edges, or None."""
    want = normalise(version).lower()
    for name, lines in sections(text):
        if normalise(name).lower() == want:
            body = "\n".join(lines).strip("\n")
            return body.rstrip() + "\n" if body.strip() else ""
    return None


def read(path=CHANGELOG):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="changelog_section",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("version", nargs="?",
                    help="the version or tag to cut out, e.g. 1.0.0 or v1.0.0")
    ap.add_argument("-c", "--changelog", default=CHANGELOG)
    ap.add_argument("-o", "--out", default=None,
                    help="write here instead of standard output")
    ap.add_argument("--list", action="store_true",
                    help="print the versions the changelog has and exit")
    args = ap.parse_args(argv)

    try:
        text = read(args.changelog)
    except OSError as exc:
        print("%s could not be read (%s)" % (args.changelog, exc),
              file=sys.stderr)
        return 3

    if args.list:
        for name, _lines in sections(text):
            print(name)
        return 0
    if not args.version:
        ap.error("name a version, or pass --list")

    body = section(text, args.version)
    if body is None:
        have = ", ".join(n for n, _l in sections(text)) or "(none)"
        print("%s has no section for %s; it has: %s"
              % (args.changelog, args.version, have), file=sys.stderr)
        return 2
    if not body.strip():
        # An empty section is a heading somebody added and never filled in.
        # Releasing it would publish a blank body, so say so instead.
        print("the section for %s in %s is empty"
              % (args.version, args.changelog), file=sys.stderr)
        return 2

    if args.out:
        folder = os.path.dirname(os.path.abspath(args.out))
        if folder:
            os.makedirs(folder, exist_ok=True)
        with io.open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        print("wrote %d bytes of %s notes to %s"
              % (len(body), normalise(args.version), args.out))
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
