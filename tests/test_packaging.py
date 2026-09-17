"""P2-8: the executable's build inputs, without building it.

A PyInstaller run takes tens of seconds and produces a 9 MB binary; it belongs
in a release step, not in a suite that has to stay under thirty seconds
(GOAL.md §7.1). What is cheap and worth guarding is everything that decides
*what* gets built, because each of these has failed in practice:

* the spec no longer parses (a spec is Python, and nothing imports it, so a
  typo in it is invisible until release day);
* the spec stops shipping `data/` or `static/`, which is a binary that starts
  and then 500s on `/api/bootstrap`;
* `entry.py` stops importing, which is a binary that does nothing at all on a
  double click;
* `build_exe.py` stops naming both executables, which is a release with a
  windowed binary and no diagnostic one.

The build itself was run by hand and the transcript is in the ticket report:
both executables start, `/api/version` and `/api/bootstrap` answer, and the
windowed one opens no console.
"""
import ast
import hashlib
import io
import os
import subprocess
import sys
import zipfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PACKAGING = os.path.join(ROOT, "packaging")
SPEC = os.path.join(PACKAGING, "NMS-Sorter.spec")
ENTRY = os.path.join(PACKAGING, "entry.py")
BUILD = os.path.join(PACKAGING, "build_exe.py")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _read(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


def _package_data_block():
    """The `[tool.setuptools.package-data]` section's lines.

    Cut at the next *section header*, which is a `[` in the first column --
    not at the next `[` of any kind, which is the opening bracket of the list
    itself.
    """
    src = _read(os.path.join(ROOT, "pyproject.toml"))
    after = src.split("[tool.setuptools.package-data]", 1)[1]
    out = []
    for line in after.splitlines():
        if line.startswith("["):
            break
        out.append(line)
    return "\n".join(out)


# ------------------------------------------------------------------- spec

def test_the_spec_file_exists_and_parses():
    """A spec is executed by PyInstaller with `Analysis`, `PYZ`, `EXE` and
    `SPECPATH` injected into its globals, so it cannot be imported here --
    but it is Python, and `ast.parse` catches every syntax error a release
    would otherwise hit first."""
    assert os.path.isfile(SPEC)
    tree = ast.parse(_read(SPEC), filename=SPEC)
    assert isinstance(tree, ast.Module)


def test_the_spec_builds_two_one_file_executables_with_the_right_consoles():
    """The `--console` decision, as code. One binary cannot both have and not
    have a console: the subsystem is in the PE header."""
    src = _read(SPEC)
    tree = ast.parse(src)
    exes = [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "EXE"]
    assert len(exes) == 2, "one windowed, one console"
    by_name = {}
    for call in exes:
        kw = dict((k.arg, k.value) for k in call.keywords)
        name = kw["name"]
        name = name.id if isinstance(name, ast.Name) else name.value
        console = kw["console"].value
        by_name[name] = console
    assert by_name == {"NAME": False, "CONSOLE_NAME": True}
    assert 'NAME = "NMS-Sorter"' in src
    assert 'CONSOLE_NAME = "NMS-Sorter-console"' in src
    # one-file: no COLLECT, which is what makes a one-folder build
    assert "COLLECT(" not in src


def test_the_spec_ships_the_data_and_the_static_files():
    src = _read(SPEC)
    assert '"data"' in src and '"static"' in src
    assert '"nms_sorter", "data"' in src
    assert '"nms_sorter", "static"' in src
    # the paths the bundle must expose are the ones the code looks in
    from nms_sorter import platform as platformmod
    assert platformmod.bundle_dir().endswith("nms_sorter")


def test_the_spec_ships_the_documents_the_docs_route_serves():
    """`GET /docs/SAFETY.md` is what makes the page's "what this means" links
    local (P5-2, D4), and in the frozen build the only copy of those documents
    is the one the spec puts beside the package. `README.md` and `CHANGELOG.md`
    go one level up, because `server.DOC_SIBLINGS` looks beside the folder --
    which is the repository root in a source run."""
    src = _read(SPEC)
    assert '"nms_sorter", "docs"' in src
    assert 'name.endswith(".md")' in src, (
        "the documents are named file by file, not mapped as a folder")
    assert '(os.path.join(ROOT, "docs"), os.path.join' not in src, (
        "mapping the folder ships docs/images: 6.8 MB of screenshots and a "
        "3.4 MB GIF, in both executables")
    assert '"README.md"' in src and '"CHANGELOG.md"' in src
    from nms_sorter import server
    assert server.DOC_DIRS[0].endswith(os.path.join("nms_sorter", "docs"))
    assert server.DOC_SIBLINGS == ("README.md", "CHANGELOG.md")
    # every name the route serves in a source run is a file in the repository
    for name in server.doc_names():
        assert os.path.isfile(os.path.join(ROOT, "docs", name)) \
            or os.path.isfile(os.path.join(ROOT, name)), name


def test_the_spec_ships_no_screenshots():
    """`docs/images/` doubled both executables -- 9.5 MB to 16.3 MB -- for
    pictures the document viewer cannot show: it serves `.md` files, and an
    image in one renders as its alt text. The pictures live in the repository
    and on the release page."""
    from nms_sorter import server
    names = [os.path.basename(p) for p in _spec_doc_paths()]
    assert names, "the spec ships some documents"
    assert all(n.endswith(".md") for n in names), names
    assert "images" not in names
    # what the spec would ship and what the route serves are the same set
    served = [n for n in server.doc_names() if n not in server.DOC_SIBLINGS]
    assert sorted(names) == sorted(served)


def _spec_doc_paths():
    """Evaluate the spec's `DOCS` list without running PyInstaller."""
    src = _read(SPEC)
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(
                node.targets[0], "id", None) == "DOCS":
            code = compile(ast.Expression(node.value), SPEC, "eval")
            return [pair[0] for pair in eval(code, {"os": os, "ROOT": ROOT})]
    raise AssertionError("the spec no longer declares DOCS")


def test_the_spec_does_not_bundle_upx_or_the_test_tools():
    """UPX and a stdlib-only 40 MB binary are both antivirus magnets."""
    src = _read(SPEC)
    assert "upx=False" in src
    for unwanted in ("tkinter", "pytest", "hypothesis", "setuptools"):
        assert '"%s"' % unwanted in src, "%s should be excluded" % unwanted


# ------------------------------------------------------------------ entry

def test_the_entry_point_imports_and_exposes_main():
    """Imported for real, because a frozen build's first act is to import it
    and a failure there is the silent-double-click failure."""
    sys.path.insert(0, PACKAGING)
    try:
        for stale in ("entry",):
            sys.modules.pop(stale, None)
        import entry
        assert callable(entry.main)
    finally:
        sys.path.remove(PACKAGING)
        sys.modules.pop("entry", None)


def test_the_entry_point_goes_through_the_package_start_path():
    """One start path, not two: `python -m nms_sorter` and the executable both
    call `nms_sorter.__main__.main`, so the Python floor check and the import
    of `cli` happen in one place."""
    src = _read(ENTRY)
    assert "nms_sorter.__main__" in src
    assert "ensure_streams" in src, "a windowed build has no sys.stdout"
    from nms_sorter import __main__ as pkg_main
    assert callable(pkg_main.main)


def test_the_package_main_returns_the_exit_code_rather_than_raising():
    from nms_sorter import __main__ as pkg_main
    out = io.StringIO()
    assert pkg_main.check_python(version=(3, 8, 0), out=out) == 4
    assert "Python" in out.getvalue()
    assert pkg_main.check_python(version=(3, 9, 0), out=out) == 0


# -------------------------------------------------------------- build_exe

def test_build_exe_dry_run_prints_the_command_and_builds_nothing(tmp_path):
    out = subprocess.run([sys.executable, BUILD, "--dry-run"],
                         cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    said = out.stdout.strip()
    assert "PyInstaller" in said
    assert "--clean" in said and "--noconfirm" in said
    assert "NMS-Sorter.spec" in said
    assert "--distpath" in said and "--workpath" in said
    # W11: and the name of the zip, which is the one output whose name depends
    # on a version somebody had to remember to bump.
    from nms_sorter import __version__
    assert "NMS-Sorter-%s-win64.zip" % __version__ in said


def test_build_exe_names_both_executables_as_required_outputs():
    sys.path.insert(0, PACKAGING)
    try:
        sys.modules.pop("build_exe", None)
        import build_exe
        assert build_exe.EXPECTED == ("NMS-Sorter.exe", "NMS-Sorter-console.exe")
        cmd = build_exe.command(spec="S", dist="D", work="W")
        assert cmd[:5] == [sys.executable, "-m", "PyInstaller", "--clean",
                           "--noconfirm"]
        assert cmd[-1] == "S"
    finally:
        sys.path.remove(PACKAGING)
        sys.modules.pop("build_exe", None)


def test_the_checksum_file_is_sha256sum_format(tmp_path):
    """`sha256sum -c SHA256SUMS` has to work on the downloaded zip, because
    that is the instruction `docs/GUIDE.md` gives."""
    sys.path.insert(0, PACKAGING)
    try:
        sys.modules.pop("build_exe", None)
        import build_exe
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "NMS-Sorter.exe").write_bytes(b"not really an exe")
        (dist / "NMS-Sorter-console.exe").write_bytes(b"nor is this")
        out = dist / "SHA256SUMS"
        rows = build_exe.write_sums(dist=str(dist), out=str(out))
        assert [r[0] for r in rows] == ["NMS-Sorter-console.exe",
                                        "NMS-Sorter.exe"]
        text = out.read_text(encoding="utf-8")
        lines = text.strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            digest, name = line.split(" *", 1)
            assert len(digest) == 64 and int(digest, 16) >= 0
            assert name in ("NMS-Sorter.exe", "NMS-Sorter-console.exe")
        assert "SHA256SUMS" not in text, "the sums file does not hash itself"
        assert text.endswith("\n")
        # idempotent: hashing it twice must not pick the sums file up
        again = build_exe.write_sums(dist=str(dist), out=str(out))
        assert [r[0] for r in again] == [r[0] for r in rows]
    finally:
        sys.path.remove(PACKAGING)
        sys.modules.pop("build_exe", None)


# ------------------------------------------------- W11: the release zip

def _build_module():
    """`packaging/build_exe.py`, imported. -> the module.

    Left out of `sys.path` again afterwards but *not* out of `sys.modules`
    while the caller is using it, which is why this is a function returning a
    module rather than the `try/finally` the tests above use inline.
    """
    sys.path.insert(0, PACKAGING)
    try:
        sys.modules.pop("build_exe", None)
        import build_exe
        return build_exe
    finally:
        sys.path.remove(PACKAGING)


def _fake_dist(tmp_path, mod):
    """A `dist/` with stand-ins for the binaries and a real `SHA256SUMS`.

    Stand-ins, because the zip does not care what the bytes are and a
    PyInstaller run is tens of seconds (the reason this whole file exists).
    The sums are the genuine article: `write_sums` produced them, over exactly
    the two files that are there.
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "NMS-Sorter.exe").write_bytes(b"not really an exe")
    (dist / "NMS-Sorter-console.exe").write_bytes(b"nor is this")
    mod.write_sums(dist=str(dist), out=str(dist / "SHA256SUMS"))
    return dist


SEVEN = ("NMS-Sorter.exe", "NMS-Sorter-console.exe", "SHA256SUMS",
         "README.md", "GUIDE.md", "RULES.md", "TROUBLESHOOTING.md")


def test_the_zip_holds_exactly_the_seven_names_flat(tmp_path):
    """W11: the release attached three files and none of the instructions, so
    somebody who downloaded the exe had the program and no `GUIDE.md`.

    Flat is the other half of the ticket: a folder inside the archive turns
    "double-click the exe" into "extract this, find that", and every line in
    `SHA256SUMS` is written against a bare name that has to resolve beside the
    file it names.
    """
    mod = _build_module()
    dist = _fake_dist(tmp_path, mod)
    path, listed = mod.write_zip(dist=str(dist), root=ROOT, ver="9.9.9")
    assert os.path.basename(path) == "NMS-Sorter-9.9.9-win64.zip"
    assert sorted(listed) == sorted(SEVEN), listed
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        assert sorted(names) == sorted(SEVEN), names
        for name in names:
            assert "/" not in name and "\\" not in name, name
        assert not [i for i in zf.infolist() if i.is_dir()], "no folder entry"
    # and the module's own declared listing is the same set, since `main()`
    # checks the real zip against it
    assert sorted(mod.ZIP_NAMES) == sorted(SEVEN)


def test_the_readme_in_the_zip_has_no_docs_links_and_no_screenshots(tmp_path):
    """`docs/GUIDE.md` and `docs/images/save-tab.png` resolve in a checkout
    and nowhere inside a flat zip. The rewrite happens on the way into the
    archive; the repository's own README keeps its links, which the assertion
    at the end of this test is what proves."""
    mod = _build_module()
    dist = _fake_dist(tmp_path, mod)
    path, _listed = mod.write_zip(dist=str(dist), root=ROOT, ver="9.9.9")
    with zipfile.ZipFile(path) as zf:
        text = zf.read("README.md").decode("utf-8")
    assert "docs/" not in text, "a link no reader of the zip can follow"
    assert "![" not in text, "the screenshots are not in the zip"
    assert ".png" not in text and ".gif" not in text
    # the documents that *are* in the zip are still linked, by bare name
    for name in ("GUIDE.md", "RULES.md", "TROUBLESHOOTING.md"):
        assert "[%s](%s)" % (name, name) in text, name
    # the ones that are not are plain text, not a link that goes nowhere
    assert "POST-PATCH.md" in text and "](POST-PATCH.md)" not in text
    assert "](LICENSE)" not in text
    # the prose is untouched, and so is the file in the repository
    assert "# NMS Inventory Sorter" in text
    assert "no spawning, no stat editing, no unit editing" in text
    assert "](docs/GUIDE.md)" in _read(os.path.join(ROOT, "README.md"))


def test_the_other_three_documents_are_the_repository_text(tmp_path):
    """Only links are rewritten. `GUIDE.md` loses its `images/` links -- they
    are as dead in the zip as the README's -- and keeps every word."""
    mod = _build_module()
    dist = _fake_dist(tmp_path, mod)
    path, _listed = mod.write_zip(dist=str(dist), root=ROOT, ver="9.9.9")
    with zipfile.ZipFile(path) as zf:
        for rel in ("GUIDE.md", "RULES.md", "TROUBLESHOOTING.md"):
            inzip = zf.read(rel).decode("utf-8")
            repo = _read(os.path.join(ROOT, "docs", rel))
            assert "![" not in inzip, rel
            assert "\r\n" not in inzip, rel
            # every non-image line survives verbatim
            kept = [l for l in repo.splitlines() if not l.startswith("![")]
            missing = [l for l in kept if l and l not in inzip]
            assert not missing, "%s: %s" % (rel, missing[:3])


def test_the_sums_in_the_zip_are_the_sums_beside_the_binaries(tmp_path):
    """Not a second copy written for the archive: the same bytes, so
    `sha256sum -c SHA256SUMS` in an unzipped folder checks the two files that
    are in it."""
    mod = _build_module()
    dist = _fake_dist(tmp_path, mod)
    path, _listed = mod.write_zip(dist=str(dist), root=ROOT, ver="9.9.9")
    with zipfile.ZipFile(path) as zf:
        assert zf.read("SHA256SUMS") == (dist / "SHA256SUMS").read_bytes()
        for name in ("NMS-Sorter.exe", "NMS-Sorter-console.exe"):
            assert zf.read(name) == (dist / name).read_bytes()


def test_the_zip_hash_is_one_sha256sum_line_over_the_finished_zip(tmp_path):
    """`SHA256SUMS` is *inside* the zip, so the zip is not a line in it. Its
    hash is published beside it instead, in the same format, naming the zip
    without a directory so it resolves wherever the pair was downloaded to."""
    mod = _build_module()
    dist = _fake_dist(tmp_path, mod)
    path, _listed = mod.write_zip(dist=str(dist), root=ROOT, ver="9.9.9")
    out, digest = mod.write_zip_hash(path)
    assert out == path + ".sha256"
    text = _read(out)
    assert text.endswith("\n") and len(text.strip().splitlines()) == 1
    said, name = text.strip().split(" *", 1)
    assert name == "NMS-Sorter-9.9.9-win64.zip", name
    assert said == digest == mod.sha256(path)
    assert len(said) == 64 and int(said, 16) >= 0
    # the claim, checked the way a reader would check it
    h = hashlib.sha256()
    h.update(open(path, "rb").read())
    assert h.hexdigest() == said
    # and the zip does not list itself anywhere
    with zipfile.ZipFile(path) as zf:
        assert "NMS-Sorter-9.9.9-win64.zip" not in zf.read(
            "SHA256SUMS").decode("utf-8")


def test_the_zip_is_named_after_the_version_the_build_reports(tmp_path):
    from nms_sorter import __version__
    mod = _build_module()
    assert mod.zip_name() == "NMS-Sorter-%s-win64.zip" % __version__
    assert mod.PLATFORM_TAG == "win64", "built on Windows, not on the host"
    assert mod.version() == __version__


def test_a_previous_zip_is_cleared_before_the_sums_are_taken(tmp_path):
    """`write_sums` hashes every file in `dist/`, on purpose. A zip left there
    by an earlier build would become a line in the `SHA256SUMS` that goes into
    the new zip: a checksum file describing a build it is not part of.
    PyInstaller's `--noconfirm` does not remove it."""
    mod = _build_module()
    dist = _fake_dist(tmp_path, mod)
    stale = dist / "NMS-Sorter-0.0.1-win64.zip"
    stale.write_bytes(b"last release")
    (dist / "NMS-Sorter-0.0.1-win64.zip.sha256").write_text("x\n")
    gone = mod.clear_previous_zips(dist=str(dist))
    assert sorted(gone) == ["NMS-Sorter-0.0.1-win64.zip",
                            "NMS-Sorter-0.0.1-win64.zip.sha256"]
    assert not stale.exists()
    rows = mod.write_sums(dist=str(dist), out=str(dist / "SHA256SUMS"))
    assert [r[0] for r in rows] == ["NMS-Sorter-console.exe", "NMS-Sorter.exe"]


def test_the_zip_refuses_when_a_binary_or_the_sums_are_not_there(tmp_path):
    """The zip is assembled from what is on disk, and an archive missing the
    windowed executable is a release nobody can run. `main()` already fails on
    a missing binary before this point; this is the second door."""
    mod = _build_module()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "NMS-Sorter.exe").write_bytes(b"one of three")
    with pytest.raises(IOError) as err:
        mod.write_zip(dist=str(dist), root=ROOT, ver="9.9.9")
    assert "SHA256SUMS" in str(err.value)
    assert "NMS-Sorter-console.exe" in str(err.value)


def test_the_link_rewrite_leaves_urls_and_anchors_alone():
    """Three rules and nothing else, stated one at a time."""
    mod = _build_module()
    r = mod.rewrite_for_zip
    assert r("see [docs/GUIDE.md](docs/GUIDE.md) now") == "see [GUIDE.md](GUIDE.md) now"
    assert r("[the guide](docs/GUIDE.md)") == "[the guide](GUIDE.md)"
    assert r("[a section](docs/GUIDE.md#the-items-tab)") == \
        "[a section](GUIDE.md#the-items-tab)"
    assert r("[POST-PATCH](docs/POST-PATCH.md)") == "POST-PATCH"
    assert r("[LICENSE](LICENSE)") == "LICENSE"
    assert r("[the repo](https://example.invalid/x)") == \
        "[the repo](https://example.invalid/x)"
    assert r("[back](#install)") == "[back](#install)"
    assert r("![alt](docs/images/x.png)\n") == "\n"
    assert r("a\n\n![one](i/a.png)\n![two](i/b.gif)\n\nb\n") == "a\n\nb\n"


# ------------------------------------------------- P7-1: the release notes

CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")
SECTION = os.path.join(PACKAGING, "changelog_section.py")

SAMPLE = """# Changelog

Some preamble nobody wants in a release body.

## [Unreleased]

### Added

- something not released yet

## [1.2.0] - 2026-09-14

### Added

- the thing this release is about
- a second line

### Fixed

- a bug

## [1.1.0] - 2026-08-01

### Added

- older news

## [1.0.1]

## [1.0.0] - 2026-07-01

- the first one
"""


def _section_module():
    sys.path.insert(0, PACKAGING)
    try:
        sys.modules.pop("changelog_section", None)
        import changelog_section
        return changelog_section
    finally:
        sys.path.remove(PACKAGING)


def test_the_changelog_section_script_exists_and_parses():
    assert os.path.isfile(SECTION)
    ast.parse(_read(SECTION), filename=SECTION)


def test_a_version_cuts_out_exactly_its_own_section():
    """The release body is the section that was reviewed in the pull request,
    not a second copy retyped on release day. Everything above the next `## `
    heading and nothing below it."""
    mod = _section_module()
    body = mod.section(SAMPLE, "1.2.0")
    assert "the thing this release is about" in body
    assert "a second line" in body
    assert "a bug" in body
    assert "older news" not in body
    assert "not released yet" not in body
    assert "# Changelog" not in body
    assert "## [1.2.0]" not in body, "the heading is the tag; GitHub shows it"
    assert body.endswith("\n")


def test_a_tag_and_a_version_name_the_same_section():
    """A git tag is `v1.2.0` and a changelog heading is `[1.2.0]`; the `v` is
    the only difference and the caller should not have to strip it."""
    mod = _section_module()
    assert mod.section(SAMPLE, "v1.2.0") == mod.section(SAMPLE, "1.2.0")
    assert mod.normalise("v1.2.0") == "1.2.0"
    assert mod.normalise("1.2.0") == "1.2.0"


def test_a_version_with_no_section_is_exit_2_and_says_what_there_is(tmp_path):
    """A release with no changelog entry is a release nobody can read, so this
    fails the job rather than publishing an empty body."""
    path = tmp_path / "CHANGELOG.md"
    path.write_text(SAMPLE, encoding="utf-8")
    out = subprocess.run([sys.executable, SECTION, "v9.9.9",
                          "-c", str(path)],
                         cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 2
    assert "9.9.9" in out.stderr
    assert "1.2.0" in out.stderr, "it names the versions there are"


def test_an_empty_section_is_also_exit_2(tmp_path):
    """`## [1.0.1]` with nothing under it is a heading somebody added and never
    filled in."""
    path = tmp_path / "CHANGELOG.md"
    path.write_text(SAMPLE, encoding="utf-8")
    out = subprocess.run([sys.executable, SECTION, "1.0.1", "-c", str(path)],
                         cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 2
    assert "empty" in out.stderr


def test_the_script_writes_the_file_the_release_job_attaches(tmp_path):
    """`-o`, because the workflow hands the path to
    `softprops/action-gh-release@v2` as `body_path`."""
    path = tmp_path / "CHANGELOG.md"
    path.write_text(SAMPLE, encoding="utf-8")
    out_path = tmp_path / "notes" / "RELEASE_BODY.md"
    out = subprocess.run([sys.executable, SECTION, "v1.2.0", "-c", str(path),
                          "-o", str(out_path)],
                         cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    written = out_path.read_text(encoding="utf-8")
    assert "the thing this release is about" in written
    assert "older news" not in written
    assert "\r\n" not in written, "one line ending, whatever built it"


def test_the_script_prints_to_stdout_without_o(tmp_path):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(SAMPLE, encoding="utf-8")
    out = subprocess.run([sys.executable, SECTION, "1.1.0", "-c", str(path)],
                         cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "older news" in out.stdout
    assert "the thing this release is about" not in out.stdout


def test_it_lists_the_versions_the_real_changelog_has():
    """Run against the repository's own `CHANGELOG.md`, because the shape the
    parser assumes is the shape that file actually has."""
    mod = _section_module()
    names = [n for n, _lines in mod.sections(mod.read(CHANGELOG))]
    assert names, "the changelog has at least one section"
    assert "Unreleased" in names, "Keep a Changelog's own heading is there"


def test_the_version_this_build_reports_has_a_changelog_section():
    """P7-1 and P7-6 together: the release job cuts the section whose heading
    matches the tag, and the tag matches `nms_sorter.__version__`. A release
    tagged `v1.0.0` against a changelog with nothing under `[1.0.0]` fails the
    job -- better here, where the fix is a paragraph, than there, where the fix
    is a second tag.

    `[Unreleased]` being empty is fine and is the normal state right after a
    release; what must not be empty is the section named by the version this
    build reports."""
    from nms_sorter import __version__
    mod = _section_module()
    body = mod.section(mod.read(CHANGELOG), __version__)
    assert body is not None, (
        "CHANGELOG.md has no [%s] section; the release job would exit 2"
        % __version__)
    assert body.strip(), "the [%s] section is empty" % __version__


# --------------------------------------------------- P7-1: the release job

WORKFLOW = os.path.join(ROOT, ".github", "workflows", "ci.yml")


def _release_job():
    """The `release:` block of the workflow, as text.

    Read as text rather than parsed: PyYAML is not a dependency of this
    project and adding one to assert on a 40-line file would be the more
    expensive mistake. The assertions below are about strings a release
    depends on, each of which has exactly one spelling.
    """
    src = _read(WORKFLOW)
    after = src.split("\n  release:", 1)
    assert len(after) == 2, "the workflow has a release job"
    out = []
    for line in after[1].splitlines():
        if line and not line.startswith("    ") and not line.startswith("\t") \
                and line.strip():
            break
        out.append(line)
    return "\n".join(out)


def test_the_release_job_runs_on_a_version_tag_only():
    src = _read(WORKFLOW)
    assert 'tags: ["v*"]' in src
    job = _release_job()
    assert "startsWith(github.ref, 'refs/tags/v')" in job
    assert "github.event_name == 'push'" in job


def test_the_release_job_builds_on_windows_and_needs_write_permission():
    """There is no cross-compilation: the executable is built on the platform
    it runs on. And the token every other job holds is read-only, so the one
    job that creates a release asks for `contents: write` itself."""
    job = _release_job()
    assert "runs-on: windows-latest" in job
    assert "permissions:" in job and "contents: write" in job
    assert 'python-version: "3.13"' in job
    assert 'pip install -e ".[dev]" pyinstaller' in job


def test_the_release_job_runs_the_build_and_the_packaging_guards():
    job = _release_job()
    assert "python packaging/build_exe.py" in job
    assert "pytest tests/test_packaging.py -q" in job
    assert "packaging/changelog_section.py" in job


def test_the_release_job_attaches_the_two_binaries_and_the_sums():
    """The three files `docs/GUIDE.md` tells someone to download and verify,
    attached loose as well as inside the zip -- so a player who wants one
    executable can take one executable. `fail_on_unmatched_files` because a
    release missing a binary is worse than no release."""
    job = _release_job()
    assert "softprops/action-gh-release@v2" in job
    for name in ("dist/NMS-Sorter.exe", "dist/NMS-Sorter-console.exe",
                 "dist/SHA256SUMS"):
        assert name in job, name
    assert "body_path:" in job
    assert "RELEASE_BODY.md" in job
    assert "fail_on_unmatched_files: true" in job


def test_the_release_job_attaches_the_zip_and_its_hash():
    """W11. Globbed on the version, because the name carries
    `nms_sorter.__version__` and the workflow does not read Python; with
    `fail_on_unmatched_files: true` a glob that matches nothing fails the job,
    which is the behaviour wanted if the zip was not built.

    The two patterns cannot collide: one ends `.zip` and the other
    `.zip.sha256`, and `*` does not cross a suffix.
    """
    job = _release_job()
    for pattern in ("dist/NMS-Sorter-*-win64.zip",
                    "dist/NMS-Sorter-*-win64.zip.sha256"):
        assert pattern in job, pattern
    assert "fail_on_unmatched_files: true" in job
    # the name the glob has to match, for the version this build reports
    from nms_sorter import __version__
    import fnmatch
    for pattern, name in (
            ("dist/NMS-Sorter-*-win64.zip",
             "dist/NMS-Sorter-%s-win64.zip" % __version__),
            ("dist/NMS-Sorter-*-win64.zip.sha256",
             "dist/NMS-Sorter-%s-win64.zip.sha256" % __version__)):
        assert fnmatch.fnmatchcase(name, pattern), name
    assert not fnmatch.fnmatchcase(
        "dist/NMS-Sorter-%s-win64.zip.sha256" % __version__,
        "dist/NMS-Sorter-*-win64.zip"), "the hash is not attached twice"


def test_the_release_notes_are_not_hashed_into_the_sums():
    """`build_exe.write_sums` hashes every file in `dist/`, and the release
    notes are not an artefact anybody verifies, so they are written
    elsewhere."""
    job = _release_job()
    assert "dist/RELEASE_BODY.md" not in job
    assert "runner.temp" in job


def test_the_other_jobs_still_exist_and_are_untouched_by_the_tag():
    """A tag also runs the matrix (P7-1: "runs the matrix"), and the nightly
    fuzz job is still nightly."""
    src = _read(WORKFLOW)
    for job in ("  tests:", "  fuzz:", "  docs:", "  release:"):
        assert job in src, job
    assert "github.event_name == 'schedule'" in src


# --------------------------------------------------------- the .gitignore

@pytest.mark.parametrize("pattern", ["dist/", "build/", "*.spec.bak"])
def test_build_output_is_not_tracked(pattern):
    lines = [l.strip() for l in _read(os.path.join(ROOT, ".gitignore")).splitlines()]
    assert pattern in lines, pattern


# ----------------------------------------------- the wheel's package data

def test_package_data_names_files_not_a_directory_listing():
    """`data/*` would also ship whatever a generator run left behind."""
    block = _package_data_block()
    assert '"data/*.json"' in block
    assert '"data/DATA_VERSION"' in block
    assert '"data/DATA_COUNTS"' in block
    assert '"static/*"' in block
    assert '"data/*"' not in block


def test_every_shipped_data_file_matches_a_package_data_pattern():
    """The narrowing must not have dropped a file the program reads."""
    import fnmatch
    block = _package_data_block()
    patterns = [p.strip().strip(",").strip('"') for p in block.splitlines()
                if p.strip().startswith('"')]
    assert patterns, block
    for sub in ("data", "static"):
        folder = os.path.join(ROOT, "nms_sorter", sub)
        for name in os.listdir(folder):
            if not os.path.isfile(os.path.join(folder, name)):
                continue
            rel = "%s/%s" % (sub, name)
            assert any(fnmatch.fnmatch(rel, p) for p in patterns), rel


# ------------------------------------------- W4: where the bundle unpacks

def _exe_kwargs(src=None):
    """{name: {kwarg: source text}} for every `EXE(...)` call in the spec."""
    tree = ast.parse(src if src is not None else _read(SPEC))
    out = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "EXE"):
            continue
        kw = {k.arg: k.value for k in node.keywords}
        name = kw.get("name")
        key = getattr(name, "id", None) or getattr(name, "value", None)
        out[key] = kw
    return out


def test_the_spec_moves_the_extraction_out_of_temp():
    """W4: each one-file run unpacked ~23 MB to `%TEMP%\\_MEIxxxxx` and the
    bootloader removes it only on a graceful exit, so a player's `%TEMP%` held
    15 folders and 344 MB -- while `GUIDE.md` promised that nothing is written
    outside `%LOCALAPPDATA%\\NMS-Sorter\\`. Both executables, not just the
    windowed one: the console build is what a bug report is asked to run and
    it litters the same way."""
    kwargs = _exe_kwargs()
    assert set(kwargs) == {"NAME", "CONSOLE_NAME"}, sorted(kwargs)
    for key, kw in kwargs.items():
        assert "runtime_tmpdir" in kw, key
        assert getattr(kw["runtime_tmpdir"], "id", None) == "RUNTIME_TMPDIR", \
            key


def test_the_runtime_tmpdir_is_an_unexpanded_env_var_on_windows_only():
    """The Windows bootloader calls `ExpandEnvironmentStringsW` on this string
    itself -- measured on a throwaway one-file build, which is the only way to
    know -- so the variable is left in it. Expanding it at build time would
    bake the building machine's user name into the published executable, and
    PyInstaller documents that the POSIX bootloader expands nothing at all,
    which is why the value is `None` off Windows."""
    src = _read(SPEC)
    assert 'RUNTIME_TMPDIR = (r"%LOCALAPPDATA%\\NMS-Sorter\\runtime"' in src
    assert 'if os.name == "nt" else None)' in src
    # And it is the same directory `platform.sweep_stale_runtime_dirs()` looks
    # in, which is the whole point of naming it twice.
    ns = {"os": os}
    exec(src.split("# SPECPATH", 1)[0].split('"""', 2)[2], ns)
    value = ns["RUNTIME_TMPDIR"]
    if os.name != "nt":
        assert value is None
        pytest.skip("the expansion is the Windows bootloader's")
    from nms_sorter import platform as platformmod
    assert os.path.expandvars(value) == platformmod.runtime_dir()
    assert "%" in value, "the variable must reach the bootloader unexpanded"


def test_the_spec_still_parses_with_the_runtime_tmpdir_docstring():
    """A backslash in a non-raw docstring is a syntax error waiting for
    release day: `\\N` is not an escape Python accepts, and this spec's
    docstring now names `%LOCALAPPDATA%\\NMS-Sorter\\runtime`. Nothing imports
    a spec, so `ast.parse` is the only thing that would have caught it."""
    ast.parse(_read(SPEC), filename=SPEC)
    assert "NMS-Sorter" in _read(SPEC)


# --------------------------------------- W2: the working directory

def test_the_entry_point_releases_the_exe_folder_before_starting():
    """W2: a double-clicked executable inherits its own folder as the working
    directory, Windows will not delete a directory that is a process's cwd,
    and "delete the exe and that folder" is the whole uninstall procedure --
    so it failed on the folder and left a server answering 8765 with its
    executable already gone.

    Order matters: the move happens before `nms_sorter.__main__` is imported,
    so a run that fails early is not the one that still holds the folder.
    """
    tree = ast.parse(_read(ENTRY), filename=ENTRY)
    fn = [n for n in tree.body
          if isinstance(n, ast.FunctionDef) and n.name == "main"][0]
    # By line number, not by `ast.walk` order, which is breadth first and
    # therefore not the order the statements run in.
    where = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            attr = getattr(node.func, "attr", None)
            if attr:
                where.setdefault(attr, node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.module:
            where.setdefault("import " + node.module, node.lineno)
    assert "release_cwd" in where, sorted(where)
    assert where["release_cwd"] < where["import nms_sorter.__main__"], \
        "the folder is released before the package is even imported"
    assert where["ensure_streams"] < where["release_cwd"], \
        "the streams come first: a failure in the move must be loggable"


def test_the_entry_point_still_runs_and_moves_nothing_in_a_source_run(
        tmp_path, monkeypatch):
    """`release_cwd()` is frozen-only, and `entry.main` is importable and
    callable from a checkout -- which is what `test_the_entry_point_imports_
    and_exposes_main` above relies on."""
    sys.path.insert(0, PACKAGING)
    try:
        import entry                                       # noqa: F401
    finally:
        sys.path.remove(PACKAGING)
    from nms_sorter import platform as platformmod
    before = os.getcwd()
    monkeypatch.setattr(platformmod, "is_frozen", lambda: False)
    assert platformmod.release_cwd() is None
    assert os.getcwd() == before


def test_nothing_in_packaging_changes_the_working_directory():
    """The one `chdir` in this program is `platform.release_cwd`. A second one
    anywhere -- a helper that pushed into `ROOT`, say -- would make "where
    does this process's cwd come from?" a question with two answers."""
    for name in sorted(os.listdir(PACKAGING)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(PACKAGING, name)
        tree = ast.parse(_read(path), filename=path)
        called = [n.lineno for n in ast.walk(tree)
                  if isinstance(n, ast.Attribute) and n.attr == "chdir"]
        assert called == [], "%s calls chdir at line %s" % (name, called)
