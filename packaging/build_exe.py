#!/usr/bin/env python3
"""Build the two Windows executables, `dist/SHA256SUMS` and the release zip.

    python packaging/build_exe.py
    python packaging/build_exe.py --dry-run      # print the command, build nothing

Produces, in `dist/`:

    NMS-Sorter.exe            the double-click one; no console window
    NMS-Sorter-console.exe    the same program with a console, for diagnostics
    SHA256SUMS                `sha256sum -c` format, one line per file
    NMS-Sorter-<v>-win64.zip  those three, flat, plus the four documents
    NMS-Sorter-<v>-win64.zip.sha256    that zip's own hash, one line

The checksum file is the point of this script rather than a plain
`pyinstaller` call. A one-file PyInstaller binary is flagged by antivirus
often enough that the published hash is how someone decides whether the thing
they downloaded is the thing that was built (GOAL.md §9.1, "Executable flagged
by antivirus"). It is written last, from the files actually on disk, and it
covers every file in `dist/` -- so a leftover from an earlier build shows up
in it and gets noticed rather than shipped unlisted.

**The zip, and why the documents are in it** (W11). The release page attached
three files and nothing else, so someone who downloaded the executable had the
program and none of its instructions: `GUIDE.md`, `TROUBLESHOOTING.md` and
`RULES.md` existed only on a web page they would have to still be on. The zip
ships them beside the binaries. It is **flat** -- no folder inside it --
because a nested folder turns "double-click the exe" into "extract this, find
that", and because the lines in `SHA256SUMS` are written against bare names
and have to keep resolving beside the files they name.

`SHA256SUMS` is *inside* the zip, so the zip cannot be a line in it: a file
cannot carry its own hash. The zip's hash is published as a separate one-line
`<zip>.sha256` instead, and the order in `main()` is what keeps the two
consistent -- the sums first, over the two binaries alone, then the zip, then
the zip's hash.

The four documents are copied under their repository names (`README.md`,
`GUIDE.md`, `RULES.md`, `TROUBLESHOOTING.md`) with their relative links
rewritten **for the copy only**: `docs/GUIDE.md` becomes `GUIDE.md`, a link to
something the zip does not carry becomes plain text rather than a dead link,
and the README's two screenshots are dropped because `docs/images/` is not in
the zip and will not be (6.8 MB of PNGs and a 3.4 MB GIF, beside a 10 MB
program). See `rewrite_for_zip`. Nothing in the repository is edited.

Nothing here is Windows-specific except what it produces: on another platform
PyInstaller would emit that platform's binaries, the names would still be
right, and `apply` in them would refuse for the reason `platform.py` gives.
There is no cross-compilation; the release is built on Windows, which is also
why `win64` is a constant below rather than read off the building machine.
"""
import argparse
import glob
import hashlib
import io
import os
import re
import subprocess
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SPEC = os.path.join(HERE, "NMS-Sorter.spec")
DIST = os.path.join(ROOT, "dist")
WORK = os.path.join(ROOT, "build")
SUMS = os.path.join(DIST, "SHA256SUMS")

#: what the spec builds. Named here too, so a missing output is reported as
#: "this was not built" rather than as an empty checksum file.
EXPECTED = ("NMS-Sorter.exe", "NMS-Sorter-console.exe")

#: what the zip carries besides `EXPECTED` and `SHA256SUMS`, as repository
#: paths. The name inside the zip is the basename, so `docs/GUIDE.md` is
#: `GUIDE.md` there -- which is what `rewrite_for_zip` rewrites the links to.
#: `POST-PATCH.md` is deliberately out: it is for the day the game updates,
#: not something the person who just unzipped this needs beside the exe.
ZIP_DOCS = ("README.md", "docs/GUIDE.md", "docs/RULES.md",
            "docs/TROUBLESHOOTING.md")

#: every name the zip contains, flat and in listing order. Asserted against
#: the zip's own namelist in `tests/test_packaging.py`, because a document
#: silently dropped is a release whose troubleshooting page is missing.
ZIP_NAMES = EXPECTED + ("SHA256SUMS",) + tuple(
    os.path.basename(p) for p in ZIP_DOCS)

#: the platform tag in the zip's name. Not `platform.machine()`: the release
#: is built on 64-bit Windows by the `release` job, and a zip named after
#: whatever machine happened to build it would have a different name every
#: time somebody built one by hand.
PLATFORM_TAG = "win64"

#: An image link is dropped from the copy in the zip: `docs/images/` is not in
#: it. Same list as `tests/test_docs.py`.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")

#: `[label](target)`, with the leading `!` of an image link captured so the
#: two cases can be told apart.
_LINK = re.compile(r"(!?)\[([^\]]*)\]\(\s*([^)\s]+?)\s*\)")

#: what `rewrite_for_zip` leaves alone: a link that is not a path.
_ABSOLUTE = ("http://", "https://", "mailto:", "#", "<")


def command(spec=SPEC, dist=DIST, work=WORK):
    """The exact PyInstaller invocation. -> a list for `subprocess`.

    `--clean` because PyInstaller's cache survives a source change often
    enough to produce a binary of the previous commit, and a stale release
    binary is a bug you cannot reproduce. `--noconfirm` because the only
    interactive question it asks is whether to delete `dist/`, and the answer
    is always yes.
    """
    return [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
            "--distpath", dist, "--workpath", work, spec]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_sums(dist=DIST, out=SUMS):
    """Hash every file in `dist/` except the sums file itself. -> [(name, sha, size)]."""
    rows = []
    for name in sorted(os.listdir(dist)):
        path = os.path.join(dist, name)
        if not os.path.isfile(path) or os.path.abspath(path) == os.path.abspath(out):
            continue
        rows.append((name, sha256(path), os.path.getsize(path)))
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        for name, digest, _size in rows:
            # `sha256sum -c` format: digest, two spaces, binary marker, name.
            fh.write("%s *%s\n" % (digest, name))
    return rows


def version(root=ROOT):
    """-> `nms_sorter.__version__`, imported rather than parsed.

    The package the zip is named after is the package that goes in it, and
    `__version__` is the string the release job's tag has to match: cutting a
    release means bumping it in `nms_sorter/__init__.py` first, then tagging
    `v<that string>`. `sys.path` needs the repository root because `python
    packaging/build_exe.py` puts `packaging/` on it, not `ROOT`.
    """
    if root not in sys.path:
        sys.path.insert(0, root)
    import nms_sorter
    return nms_sorter.__version__


def zip_name(ver=None):
    """`NMS-Sorter-1.0.0-win64.zip`. -> a bare file name, no directory."""
    return "NMS-Sorter-%s-%s.zip" % (ver or version(), PLATFORM_TAG)


def rewrite_for_zip(text, names=ZIP_NAMES):
    """A document's markdown, with its links made to work inside a flat zip.

    Three rules, and nothing else is touched:

    * an image link is removed, because `docs/images/` is not in the zip. A
      line that held nothing but images collapses, so the blank run it leaves
      behind is squeezed back to one blank line.
    * a relative link to a file the zip *does* carry points at the bare name:
      `[docs/GUIDE.md](docs/GUIDE.md)` -> `[GUIDE.md](GUIDE.md)`. The `docs/`
      comes off the label too when the label is the path, which it is in the
      README's documentation table.
    * a relative link to anything else -- `docs/POST-PATCH.md`, `LICENSE` --
      becomes its own label as plain text. A dead relative link in an unzipped
      folder is worse than prose: markdown renderers and editors present it as
      something to click, and it goes nowhere.

    A URL, a `mailto:` and a bare `#anchor` are left exactly as they are: the
    first two resolve from anywhere and the third resolves within the file.

    The repository's own copy is never rewritten. This runs on the text on its
    way into the archive (`write_zip`), which is why it takes and returns a
    string rather than a path.
    """
    def one(m):
        bang, label, target = m.group(1), m.group(2), m.group(3)
        if bang or target.lower().endswith(IMAGE_SUFFIXES):
            return ""
        if target.startswith(_ABSOLUTE):
            return m.group(0)
        label = label.replace("docs/", "")
        bare = os.path.basename(target.split("#")[0])
        if bare in names and target.split("#")[0] in (bare, "docs/" + bare):
            anchor = ("#" + target.split("#", 1)[1]) if "#" in target else ""
            return "[%s](%s%s)" % (label, bare, anchor)
        return label

    out = _LINK.sub(one, text)
    # two image links on consecutive lines of their own leave two empty lines
    out = re.sub(r"\n[ \t]*\n([ \t]*\n)+", "\n\n", out)
    return out


def write_zip(dist=DIST, root=ROOT, ver=None, names=ZIP_NAMES):
    """Build `dist/NMS-Sorter-<version>-win64.zip`. -> (path, [name, ...]).

    Flat: every `writestr`/`write` is given a bare name, so there is no
    directory entry in the archive at all. Deflated, because a 20 MB pair of
    one-file binaries compresses to about half that and the release page is
    the one place the size is a person's problem.

    The binaries and `SHA256SUMS` are read from `dist` and must already be
    there -- `main()` calls `write_sums()` first, which is also what keeps the
    zip out of its own listing. The documents are read from `root` and
    rewritten by `rewrite_for_zip` on the way in.
    """
    path = os.path.join(dist, zip_name(ver))
    missing = [n for n in EXPECTED + ("SHA256SUMS",)
               if not os.path.isfile(os.path.join(dist, n))]
    if missing:
        raise IOError("not in %s: %s" % (dist, ", ".join(missing)))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in EXPECTED + ("SHA256SUMS",):
            zf.write(os.path.join(dist, name), name)
        for rel in ZIP_DOCS:
            src = os.path.join(root, rel.replace("/", os.sep))
            with io.open(src, encoding="utf-8") as fh:
                body = rewrite_for_zip(fh.read(), names=names)
            # `newline="\n"` is not a `writestr` option, so it is done here:
            # one line ending in the archive whatever built it, which is the
            # rule `tests/test_packaging.py` already holds the release notes to
            zf.writestr(os.path.basename(rel), body.replace("\r\n", "\n"))
        listed = zf.namelist()
    return path, listed


def write_zip_hash(zip_path):
    """Write `<zip>.sha256`, one `sha256sum -c` line. -> (path, digest).

    Separate from `SHA256SUMS` because `SHA256SUMS` is inside the zip. Same
    format, so the same `sha256sum -c` verifies either one, and the name in it
    is bare so it resolves beside the zip wherever the two were downloaded to.
    """
    digest = sha256(zip_path)
    out = zip_path + ".sha256"
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("%s *%s\n" % (digest, os.path.basename(zip_path)))
    return out, digest


def clear_previous_zips(dist=DIST):
    """Delete any zip and `.sha256` a previous build left in `dist/`.

    `write_sums` hashes *every* file in `dist/`, on purpose, so a zip left over
    from an earlier run would become a line in the `SHA256SUMS` that goes into
    the new one -- a checksum file listing a file that is not beside it, for a
    build it does not describe. PyInstaller's `--noconfirm` does not remove it:
    a one-file build overwrites its own outputs and leaves everything else.
    Globbed rather than named, so a version bump takes the old version's zip
    with it.
    """
    gone = []
    for pattern in ("NMS-Sorter-*-%s.zip" % PLATFORM_TAG,
                    "NMS-Sorter-*-%s.zip.sha256" % PLATFORM_TAG):
        for path in sorted(glob.glob(os.path.join(dist, pattern))):
            os.remove(path)
            gone.append(os.path.basename(path))
    return gone


def main(argv=None):
    ap = argparse.ArgumentParser(prog="build_exe",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="print the PyInstaller command and exit")
    args = ap.parse_args(argv)

    cmd = command()
    print(" ".join(cmd))
    # Printed on a dry run too: "what would this produce" includes the name
    # the release page will carry, and that name depends on a version somebody
    # has to have bumped.
    print("zip: %s" % zip_name())
    if args.dry_run:
        return 0

    if not os.path.isfile(SPEC):
        print("no spec file at %s" % SPEC)
        return 2
    started = time.time()
    proc = subprocess.run(cmd, cwd=ROOT)
    if proc.returncode != 0:
        print("PyInstaller failed with %d" % proc.returncode)
        return proc.returncode
    took = time.time() - started

    missing = [n for n in EXPECTED if not os.path.isfile(os.path.join(DIST, n))]
    if missing:
        print("built, but these are not in dist/: %s" % ", ".join(missing))
        return 1

    # Order matters, and it is the order these three lines are in: the stale
    # zip goes before the sums are taken (it would be hashed into them), the
    # sums go before the zip is built (they go into it), and the zip's hash
    # goes last (it is taken over the finished archive).
    for name in clear_previous_zips():
        print("removed a previous %s" % name)
    rows = write_sums()
    archive, listed = write_zip()
    hash_path, zip_digest = write_zip_hash(archive)

    print("")
    print("built in %.0f s" % took)
    for name, digest, size in rows:
        print("  %-26s %8.1f MB  %s" % (name, size / 1048576.0, digest[:16]))
    print("  %-26s %s" % ("SHA256SUMS", SUMS))
    print("  %-26s %8.1f MB  %s" % (os.path.basename(archive),
                                    os.path.getsize(archive) / 1048576.0,
                                    zip_digest[:16]))
    print("  %-26s %s" % (os.path.basename(hash_path), hash_path))
    print("  in the zip, flat: %s" % ", ".join(listed))

    # The listing is asserted in `tests/test_packaging.py`; it is checked here
    # too because the test builds a zip out of stand-ins in a temp directory,
    # and this is the real one going to the release page.
    if sorted(listed) != sorted(ZIP_NAMES):
        print("the zip does not hold the expected names: %s" % sorted(listed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
