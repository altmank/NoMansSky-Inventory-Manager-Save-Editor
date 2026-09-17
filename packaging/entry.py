"""The script PyInstaller freezes. Not imported by anything else.

Five things happen here that cannot happen inside the package, because they
are about the *bundle* rather than about the program:

1. **`sys.path`.** The spec sets `pathex` to the repository root, so
   `nms_sorter` is collected into the bundle and imported from it. Nothing is
   added to `sys.path` at runtime.
2. **The streams.** A windowed build (`console=False`) has `sys.stdout is
   None`, and the first `print()` in `cli.main` would raise `AttributeError`
   in a process with no console -- a completely silent exit on a double click.
   `platform.ensure_streams()` substitutes a writer that forwards the startup
   banner to the log file instead.
3. **The owner stamp.** `platform.write_runtime_owner()` writes this pid, and
   the time it started, into the one-file bundle's own extraction folder. It
   happens here, before anything else can go wrong, because the folder has to
   be claimed *before* any other copy of this program can look at it: a second
   launch that could not tell a live extraction folder from an abandoned one
   emptied a running server's bundle out from under it (see
   `platform.OWNER_FILE`). Every frozen run stamps its own folder, the ones
   that go on to exit included.
4. **The working directory.** A double-clicked executable inherits the folder
   it sits in as its working directory, and Windows will not delete a folder
   that is some process's cwd -- so "delete the exe and that folder", which is
   the whole of the uninstall procedure, failed on the folder and left a
   server answering 8765 with its own executable already gone (player-day
   review, W2). `platform.release_cwd()` moves to
   `%LOCALAPPDATA%\\NMS-Sorter` before anything else is imported. Nothing in
   the package resolves a relative path and nothing anywhere calls
   `os.chdir`, so there is nothing else to change: every path comes from
   `state_dir()`, `bundle_dir()`, `exe_dir()` or a flag.
5. **The exit code.** Returned, never raised past this frame, so the bundle's
   own atexit handlers run.

The Python floor check in `nms_sorter.__main__` is moot here: the interpreter
is inside the executable. It is still called, through `__main__.main()`, so
that the frozen build and `python -m nms_sorter` share one start path rather
than two that can drift.
"""
import sys


def main():
    from nms_sorter import platform as platformmod
    platformmod.ensure_streams()
    # First of all, and unconditionally: this claims the extraction folder
    # this process is running out of, so that no other launch can mistake it
    # for litter. It is a `write` into a directory that was created moments
    # ago by the bootloader; if it fails there is nothing to do about it and
    # the sweep's other conditions are what stand between a live folder and a
    # delete.
    platformmod.write_runtime_owner()
    # Before the package is imported, let alone the server started: the point
    # is to be out of the exe's folder for the whole life of the process, and
    # a run that fails early should not be the one that still holds it.
    platformmod.release_cwd()
    from nms_sorter.__main__ import main as start
    return start()


if __name__ == "__main__":
    sys.exit(main())
