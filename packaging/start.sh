#!/bin/sh
# ---------------------------------------------------------------------------
#  Start the No Man's Sky inventory sorter from a source checkout (GOAL.md
#  P2-9). Same flags as `python -m nms_sorter`:
#
#      packaging/start.sh --help
#      packaging/start.sh --read-only
#
#  Linux runs the browser, the planner, the codec and apply: the game shows
#  up as NMS.exe in /proc, so the write gate can answer whether it is
#  running. macOS runs the browser, the planner and the codec, and apply
#  refuses there: there is no measured macOS process check, and a wrong "No
#  Man's Sky is not running" answer loses play (GOAL.md 2.3).
#
#  Exit codes match the application: 3 no free port, 4 Python too old or
#  absent.
# ---------------------------------------------------------------------------
set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CHECK='import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'

PY=
for candidate in python3 python python3.13 python3.12 python3.11 python3.10 python3.9; do
    if command -v "$candidate" >/dev/null 2>&1 \
       && "$candidate" -c "$CHECK" >/dev/null 2>&1; then
        PY=$candidate
        break
    fi
done

if [ -z "$PY" ]; then
    cat <<'MSG'

Python 3.9 or newer was not found.

The sorter is a Python program and needs nothing else: it uses only the
standard library.

  Debian/Ubuntu   sudo apt install python3
  Fedora          sudo dnf install python3
  macOS           brew install python3
  Any platform    https://www.python.org/downloads/

Then run this script again.

MSG
    exit 4
fi

cd "$ROOT" || exit 1
exec "$PY" -m nms_sorter "$@"
