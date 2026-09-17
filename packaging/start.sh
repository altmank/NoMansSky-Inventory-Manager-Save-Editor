#!/bin/sh
# ---------------------------------------------------------------------------
#  Start the No Man's Sky inventory sorter from a source checkout (GOAL.md
#  P2-9). Same flags as `python -m nms_sorter`:
#
#      packaging/start.sh --help
#      packaging/start.sh --read-only
#
#  Linux and macOS run the browser, the planner and the codec perfectly well.
#  Apply refuses there and says why: the process check needs Windows, and a
#  wrong "No Man's Sky is not running" answer loses play (GOAL.md 2.3).
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
