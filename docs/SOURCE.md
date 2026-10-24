# Running from source

The same program as the release executable, without the executable. Use this on Linux or macOS,
or on Windows when you would rather run Python than an unsigned binary.

## Requirements

Python 3.9 or newer. Nothing else: the sorter uses the standard library only and never touches
the network.

## Run it

```
git clone https://github.com/altmank/NoMansSky-Inventory-Manager-Save-Editor
cd NoMansSky-Inventory-Manager-Save-Editor
packaging\start.bat
```

On Linux or macOS run `packaging/start.sh` instead. Either script runs `python -m nms_sorter`
from the repository root and opens the page in your browser; from there everything in
`GUIDE.md` applies.

Flags pass straight through, for example `packaging\start.bat --folder "D:\my saves"`.
`python -m nms_sorter --help` lists them.

## What differs from the executable

- Apply works on Windows, and from source on Linux. On Linux the game process is visible as
  `NMS.exe` in `/proc`, so the write gate can answer whether it is running. On macOS you can
  browse, plan and dry-run a save, and nothing is ever written, because there is no measured
  macOS process check.
- The first-run page finds the save folder on its own: `%APPDATA%\HelloGames\NMS` on Windows, the
  game's Steam Proton prefix in each Steam library on Linux, and
  `~/Library/Application Support/HelloGames/NMS` on macOS. `--folder` overrides the search.
- Stopping: ctrl-c in the window you started it from, or **Stop the sorter** at the foot of
  the Settings section.
- Settings, configuration, backups and the log live in the same place as for the executable; see
  "Where things live" in the README.
