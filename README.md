# NMS Inventory Sorter

Sorts your No Man's Sky inventories for you. Tell it which category of item
belongs in which container, see exactly what would move, and let it move the
lot in one go. It only moves what you already own:
no spawning, no stat editing, no unit editing.

![Screenshot of the Save section](docs/images/save-tab.png)

## What it does

- **Every container on one page.** Exosuit, starships by name, exocraft,
  freighter, Corvette, storage containers, Nutrient Processor and Stellar
  Extractor cores, with what is in each and how full it is.
- **Sort by category, not by item.** Route Raw Resources to one chest,
  Corvette Parts to another, food to the freighter. Eighteen categories out
  of the box, every item reassignable, custom categories if you want them.
- **Overflow chains.** A category can have a first container and a second for
  when the first fills up.
- **Keep, stock and cap rules.** Keep 500 Carbon in the suit, stock the
  freighter with Sodium, cap a chest, all in one sentence per rule.
- **Merges stacks and tidies** on the way, so you end up with fewer, fuller
  stacks.
- **Dry run first.** Nothing is written until you have read the plan and
  ticked it.
- **Backup and Undo.** Every apply backs up the save first. The Backups card
  puts it back in one click.
- **Works with the game open**, from the main menu.

## Install

Download `NMS-Sorter-<version>-win64.zip` from the release page, unzip it
anywhere, double-click `NMS-Sorter.exe`. It opens in your browser. Nothing is
installed.

Windows SmartScreen will say "Windows protected your PC" because the build is
not code-signed: click "More info", then "Run anyway". `SHA256SUMS` in the
zip lets you check the files if you want to.

Linux, macOS, or Python on Windows: [docs/SOURCE.md](docs/SOURCE.md).

## How to use it

1. **Save in game**, then **Options > Quit to main menu**. Stay on the menu.
2. **Pick the save** at the top of the page. It finds your Steam or GOG folder
   on its own.
3. **Route categories** on the Categories screen: a container for each, a
   second for overflow, and which containers count as sources.
4. **Dry run** on the Plan screen and read what would move.
5. **Apply.** Tick the two boxes and press Apply.
6. **Load the save** in game.

With the game closed, skip step 1 and there is one box to tick.

## Safety

**No guarantees. A save editor can corrupt a save.** Before anything is
written, the save and its metadata file are copied to a timestamped backup
folder and verified by hash; the Backups card restores one in a click, and
refuses if you have played since. Keep your own copies too.

It also proves it can rewrite your save byte for byte before touching it,
changes nothing outside the containers you named, and never applies from a
loaded save. Expedition, Game Pass, console and pre-4.0 saves are refused.

## Stopping and updating

**Stop** at the foot of the page, or it stops itself after 30 minutes idle.
Double-click the exe again to get the page back. To update, unzip the new
build over the old one; your rules and backups live in
`%LOCALAPPDATA%\NMS-Sorter` and are kept. To uninstall, stop it, delete the
exes and that folder.

## More

| Document | For |
|---|---|
| [docs/GUIDE.md](docs/GUIDE.md) | first run, your first sort, every screen |
| [docs/RULES.md](docs/RULES.md) | every rule and setting |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | look up the message you saw |
| [docs/SOURCE.md](docs/SOURCE.md) | running from source |
| [docs/POST-PATCH.md](docs/POST-PATCH.md) | when the game updates |

MIT licence for the code; the item tables are derived from the game's own
data, owned by Hello Games. Not affiliated with Hello Games.
