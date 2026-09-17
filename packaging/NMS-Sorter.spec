# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: two one-file executables from one analysis.

    python packaging/build_exe.py

**Why two executables.** What was wanted is a one-file build with no console
window, and a `--console` flag for diagnostics.
Those two cannot both be true of one one-file binary: the Windows subsystem is
baked into the PE header at link time, so a `console=False` executable has no
console to show and `AllocConsole` at runtime gives an empty black window with
no Python streams attached to it. Pretending otherwise is the kind of thing
that works on the machine that built it.

So `--console` is a *different executable*:

  NMS-Sorter.exe          console=False  the double-click one. Every failure
                                         goes to the log and a message box
                                         (`cli.report_silent_failure`).
  NMS-Sorter-console.exe  console=True   the same program with a console. This
                                         is what a bug report is asked to run,
                                         and what the exit codes 3 and 4 are
                                         readable from.

Both are built from one `Analysis`, so they cannot contain different code.

**What goes in.** `nms_sorter/data/*` and `nms_sorter/static/*` as data, at
the same relative paths they have in the package, so that
`importlib.resources.files("nms_sorter") / "data" / name` resolves inside the
bundle (PyInstaller's importer implements the resource reader) and
`platform.bundle_dir()` finds `static/` on disk under `sys._MEIPASS`. Plus the
repository's `docs/*.md`, mapped to `nms_sorter/docs`, which is what makes
`GET /docs/TROUBLESHOOTING.md` answer with no network (`server.DOC_DIRS`).

No hidden imports: the package is standard library only, every import is a
plain `import` at module scope, and the analysis finds them all. If that ever
stops being true the symptom is a `ModuleNotFoundError` in the log, not a
mystery.

**Where it unpacks.** `runtime_tmpdir` moves the one-file extraction out of
`%TEMP%` and under `%LOCALAPPDATA%\\NMS-Sorter\\runtime`, which is the sentence
`docs/GUIDE.md` already makes about this program. See `RUNTIME_TMPDIR` below
for why the environment variable is left unexpanded and why it is Windows
only.
"""
import os

#: Where the one-file bundle unpacks itself.
#:
#: The default is `%TEMP%\_MEIxxxxxx`, which the bootloader removes only on a
#: graceful exit -- and before the stop button existed there was no graceful
#: exit, so a player's `%TEMP%` held 15 folders and 344 MB while `GUIDE.md`
#: promised that "nothing is written outside `%LOCALAPPDATA%\NMS-Sorter\`"
#: (player-day review, W4).
#:
#: The environment variable is left **unexpanded on purpose**: the Windows
#: bootloader calls `ExpandEnvironmentStringsW` on this string itself, which
#: was measured on a throwaway one-file build before this line was written --
#: `sys._MEIPASS` came out as
#: `C:\Users\<user>\AppData\Local\NMS-Sorter\runtime\_MEI0000c17c2`, and the
#: folder was removed again on exit. Expanding it here instead would bake the
#: building machine's user name into the published executable.
#:
#: Windows only. PyInstaller documents that the POSIX bootloader performs no
#: expansion at all, so on Linux or macOS this would create a literal
#: `%LOCALAPPDATA%` directory wherever the build was run; there the default is
#: correct and `platform.sweep_stale_runtime_dirs()` is the whole answer.
#: `platform.runtime_dir()` names this same path for that sweep.
RUNTIME_TMPDIR = (r"%LOCALAPPDATA%\NMS-Sorter\runtime"
                  if os.name == "nt" else None)

# SPECPATH is injected by PyInstaller and is the directory holding this file.
ROOT = os.path.dirname(os.path.abspath(SPECPATH))          # noqa: F821
PKG = os.path.join(ROOT, "nms_sorter")

NAME = "NMS-Sorter"
CONSOLE_NAME = "NMS-Sorter-console"

#: `docs/*.md`, named file by file, **not** the folder. Mapping the directory
#: also shipped `docs/images/`: 6.8 MB of screenshots and a 3.4 MB GIF turned
#: a 9.5 MB executable into a 16.3 MB one, twice, for pictures nothing in the
#: program renders -- the document viewer turns an image into its alt text
#: (`pages.markdown_html`). The pictures stay in the repository and on the
#: release page, which is where a reader who wants them is.
DOCS = [(os.path.join(ROOT, "docs", name), os.path.join("nms_sorter", "docs"))
        for name in sorted(os.listdir(os.path.join(ROOT, "docs")))
        if name.endswith(".md")]

a = Analysis(                                              # noqa: F821
    [os.path.join(ROOT, "packaging", "entry.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(PKG, "data"), os.path.join("nms_sorter", "data")),
        (os.path.join(PKG, "static"), os.path.join("nms_sorter", "static")),
        # Beside the folder, not in it: `server.DOC_SIBLINGS` looks one level
        # up from `docs/`, which is the repository root in a source run and
        # this directory in the bundle, so `GUIDE.md`'s `../README.md` link
        # resolves the same way in both.
        (os.path.join(ROOT, "README.md"), "nms_sorter"),
        (os.path.join(ROOT, "CHANGELOG.md"), "nms_sorter"),
        # The documents themselves, so `GET /docs/<name>.md` answers offline.
        # Read from the repository's own `docs/` rather than copied into the
        # package by a build step: one place to edit them, and the exe carries
        # the copy it was built from. `server.DOC_DIRS` reads this path first
        # and the repository's `docs/` second, which is what makes a source run
        # serve the working tree.
    ] + DOCS,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Nothing in this program draws, plots or parses XML schemas. Excluding
    # them is not premature: tkinter alone is several megabytes and is the
    # single most common reason a stdlib-only tool ships a 40 MB binary.
    excludes=["tkinter", "unittest", "pydoc", "doctest", "lib2to3",
              "pytest", "hypothesis", "setuptools", "pip", "distutils"],
    noarchive=False,
)
pyz = PYZ(a.pure)                                          # noqa: F821

exe = EXE(                                                 # noqa: F821
    pyz, a.scripts, a.binaries, a.datas, [],
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    runtime_tmpdir=RUNTIME_TMPDIR,
    strip=False,
    upx=False,                  # UPX is the other half of every antivirus
    console=False,              # false positive; not worth the megabytes
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

exe_console = EXE(                                         # noqa: F821
    pyz, a.scripts, a.binaries, a.datas, [],
    name=CONSOLE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    runtime_tmpdir=RUNTIME_TMPDIR,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
