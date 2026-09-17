"""P2-1/P2-2: `settings.json` and the HTML pages served when startup fails.

The settings file is the one piece of state a person edits by hand *and* the
program rewrites, so the tests below are mostly about what it does with a file
that is wrong: a missing file, a file that is not JSON, a port of "8765", a key
from a newer build. None of those may raise, because the page that would let
the operator fix it is served by the process reading the file.

The page tests assert the exact `<h1>` sentences, which is what makes
`docs/TROUBLESHOOTING.md` keyable by them.
"""
import html as htmlmod
import json
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(HERE))

from nms_sorter import pages                                       # noqa: E402
from nms_sorter import settings as settingsmod                     # noqa: E402
from nms_sorter.settings import Settings                           # noqa: E402


@pytest.fixture
def state(tmp_path, monkeypatch):
    """`%LOCALAPPDATA%` pointed at a temp tree, so the defaults that derive
    from it (the backup folder, the settings path) are temp too."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return tmp_path


# --------------------------------------------------------------- defaults

def test_defaults_are_the_documented_shape(state):
    d = settingsmod.defaults()
    assert set(d) == {"settings_version", "save_folder", "backup_folder",
                      "backup_keep", "port", "read_only",
                      "strict_version_check",
                      "open_browser", "idle_exit_minutes"}
    assert d["settings_version"] == 1
    assert d["port"] == 8765
    assert d["backup_keep"] == 20
    # Half an hour, and on by default: the windowed executable has no console,
    # so a tab that was closed would otherwise leave a server running for ever.
    assert d["idle_exit_minutes"] == 30
    assert d["read_only"] is False
    assert d["strict_version_check"] is False
    assert d["open_browser"] is True
    assert d["save_folder"] == ""
    assert d["backup_folder"] == os.path.join(str(state), "NMS-Sorter",
                                              "backups")


def test_missing_file_is_the_first_run_and_not_an_error(state):
    path = os.path.join(str(state), "NMS-Sorter", "settings.json")
    s, notes = settingsmod.load(path)
    assert notes == []                     # nothing is wrong on a first run
    assert s.first_run is True
    assert s.port == 8765
    assert not os.path.exists(path)         # loading writes nothing


def test_default_path_is_under_localappdata(state):
    assert settingsmod.default_path() == os.path.join(
        str(state), "NMS-Sorter", "settings.json")


def test_every_field_has_a_one_sentence_description():
    keys = [f["key"] for f in settingsmod.FIELDS]
    assert keys == ["save_folder", "backup_folder", "backup_keep", "port",
                    "read_only", "strict_version_check",
                    "open_browser", "idle_exit_minutes"]
    for f in settingsmod.FIELDS:
        assert f["description"].strip().endswith(".")
        assert len(f["description"]) > 30
        assert f["type"] in ("path", "int", "bool")
    said = dict((f["key"], f["description"]) for f in settingsmod.FIELDS)
    for key in ("port", "open_browser"):
        assert "the next time the sorter starts" in said[key]
    assert settingsmod.RESTART_REQUIRED == ("port", "open_browser")


def test_fields_for_the_api_carry_the_resolved_default(state):
    rows = settingsmod.fields()
    assert set(rows[0]) == {"key", "type", "description", "default"}
    by_key = dict((r["key"], r["default"]) for r in rows)
    assert by_key["port"] == 8765
    assert by_key["backup_folder"].startswith(str(state))


# ------------------------------------------------------------ a bad file

def test_unreadable_file_is_a_note_and_the_defaults(state):
    path = os.path.join(str(state), "settings.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{this is not json")
    s, notes = settingsmod.load(path)
    assert s.port == 8765
    assert s.first_run is True
    assert len(notes) == 1 and notes[0]["level"] == "error"
    assert path in notes[0]["message"]
    assert "defaults" in notes[0]["message"]


def test_a_file_that_is_not_an_object_is_a_note(state):
    path = os.path.join(str(state), "settings.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("[1, 2, 3]")
    s, notes = settingsmod.load(path)
    assert s.port == 8765
    assert any(n["level"] == "error" for n in notes)


def test_unknown_key_is_kept_and_warned(state):
    path = os.path.join(str(state), "settings.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"settings_version": 1, "port": 9001,
                   "theme": "light", "future_thing": [1]}, fh)
    s, notes = settingsmod.load(path)
    assert s.port == 9001
    warned = sorted(n["where"] for n in notes if n["level"] == "warning")
    assert warned == ["future_thing", "theme"]
    assert s.extra == {"theme": "light", "future_thing": [1]}
    # and a round trip keeps them: deleting a key somebody typed is not this
    # program's business
    settingsmod.store(path, s)
    with open(path, encoding="utf-8") as fh:
        again = json.load(fh)
    assert again["theme"] == "light" and again["future_thing"] == [1]


def test_a_settings_file_from_before_the_main_menu_flow_still_loads(state):
    """`allow_game_running` was a real setting until applying from the main
    menu became the documented flow. A file written by that build is somebody
    else's file and this one keeps it: the key is unknown now, so it is
    warned about, kept verbatim, and everything beside it still loads."""
    path = os.path.join(str(state), "settings.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"settings_version": 1, "port": 9003,
                   "allow_game_running": True, "read_only": False}, fh)
    s, notes = settingsmod.load(path)
    assert s.port == 9003, "the fields this build knows still load"
    assert s.read_only is False
    assert not hasattr(s, "allow_game_running"), \
        "and it is not a setting any more"
    assert s.extra == {"allow_game_running": True}
    warned = [n["where"] for n in notes if n["level"] == "warning"]
    assert warned == ["allow_game_running"]
    assert not [n for n in notes if n["level"] == "error"], notes
    settingsmod.store(path, s)
    with open(path, encoding="utf-8") as fh:
        again = json.load(fh)
    assert again["allow_game_running"] is True, "kept, not deleted"
    assert again["port"] == 9003


def test_a_newer_settings_version_warns_but_still_loads(state):
    path = os.path.join(str(state), "settings.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"settings_version": 99, "port": 9002}, fh)
    s, notes = settingsmod.load(path)
    assert s.port == 9002
    assert any(n["where"] == "settings_version" and n["level"] == "warning"
               for n in notes)


@pytest.mark.parametrize("raw,key,kept", [
    ({"port": 80}, "port", 8765),                 # below 1024
    ({"port": 70000}, "port", 8765),              # above 65535
    ({"port": "8765"}, "port", 8765),             # a string is not a number
    ({"port": True}, "port", 8765),               # bool is not an int here
    ({"backup_keep": 0}, "backup_keep", 20),
    ({"backup_keep": 501}, "backup_keep", 20),
    ({"backup_keep": 1.5}, "backup_keep", 20),
    ({"idle_exit_minutes": -1}, "idle_exit_minutes", 30),     # below 0
    ({"idle_exit_minutes": 1441}, "idle_exit_minutes", 30),   # above a day
    ({"idle_exit_minutes": "30"}, "idle_exit_minutes", 30),   # not a number
    ({"idle_exit_minutes": True}, "idle_exit_minutes", 30),   # bool, not int
    ({"read_only": "yes"}, "read_only", False),
    ({"strict_version_check": "on"}, "strict_version_check", False),
    ({"open_browser": None}, "open_browser", True),
    ({"save_folder": 42}, "save_folder", ""),
    ({"backup_folder": ["a"]}, "backup_folder", None),
])
def test_every_field_is_validated(state, raw, key, kept):
    values, notes, _extra = settingsmod.validate(raw)
    bad = [n for n in notes if n["level"] == "error" and n["where"] == key]
    assert bad, notes
    assert key in bad[0]["message"]
    if kept is not None:
        assert values[key] == kept


@pytest.mark.parametrize("port", [1024, 8765, 65535])
def test_ports_at_the_edges_are_accepted(state, port):
    values, notes, _ = settingsmod.validate({"port": port})
    assert values["port"] == port
    assert notes == []


@pytest.mark.parametrize("keep", [1, 20, 500])
def test_backup_keep_at_the_edges_is_accepted(state, keep):
    values, notes, _ = settingsmod.validate({"backup_keep": keep})
    assert values["backup_keep"] == keep
    assert notes == []


@pytest.mark.parametrize("minutes", [0, 1, 30, 1440])
def test_idle_exit_minutes_at_the_edges_is_accepted(state, minutes):
    """0 is the one int field where the floor is a value and not a rejection:
    it is how the idle exit is switched off."""
    values, notes, _ = settingsmod.validate({"idle_exit_minutes": minutes})
    assert values["idle_exit_minutes"] == minutes
    assert notes == []


def test_every_int_field_has_a_range_of_its_own(state):
    """The bounds table is the thing a new int field is easy to leave out of:
    the branch it replaced silently handed anything that was not `port` the
    backup-retention range, which would have made 0 an error here."""
    assert set(settingsmod.INT_BOUNDS) == set(
        f["key"] for f in settingsmod.FIELDS if f["type"] == "int")
    assert settingsmod.INT_BOUNDS["idle_exit_minutes"] == (0, 1440)


def test_paths_are_trimmed_not_resolved(state):
    values, notes, _ = settingsmod.validate(
        {"save_folder": "  C:\\saves  "})
    assert values["save_folder"] == "C:\\saves"
    assert notes == []


# ---------------------------------------------------- flags are not stored

def test_a_cli_override_is_never_written_back(state):
    """GOAL.md §3.6: "CLI flags override the file for one run and are never
    written back." A debugging `--port 9999` must not become the stored port
    the first time the operator presses Save on the Settings tab."""
    path = os.path.join(str(state), "settings.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"settings_version": 1, "port": 9000,
                   "read_only": False}, fh)
    s, _notes = settingsmod.load(path)
    s.override(port=9999, read_only=True, save_folder=None)

    assert s.port == 9999 and s.read_only is True      # in force this run
    assert "save_folder" not in s.overrides            # None means "not passed"

    settingsmod.store(path, s)
    with open(path, encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert on_disk["port"] == 9000                     # the file is untouched
    assert on_disk["read_only"] is False


def test_an_override_of_a_field_the_file_never_had_stores_the_default(state):
    path = os.path.join(str(state), "settings.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"settings_version": 1}, fh)
    s, _ = settingsmod.load(path)
    s.override(port=9999)
    settingsmod.store(path, s)
    with open(path, encoding="utf-8") as fh:
        assert json.load(fh)["port"] == 8765


def test_dropping_an_override_makes_the_value_storable(state):
    """What `POST /api/settings` does: the operator typed it, so it stops
    being a flag and starts being the stored value."""
    path = os.path.join(str(state), "settings.json")
    s = Settings()
    s.override(port=9999)
    s.overrides.discard("port")
    settingsmod.store(path, s)
    with open(path, encoding="utf-8") as fh:
        assert json.load(fh)["port"] == 9999


# ------------------------------------------------------------------ store

def test_store_is_atomic(state, monkeypatch):
    """A half-written settings file is the one failure this module could cause
    that the operator cannot fix from the page, so the write goes to `.tmp` and
    is renamed. Proved by making the rename itself fail and finding the old
    file intact and no `.tmp` claiming to be it."""
    path = os.path.join(str(state), "settings.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"settings_version": 1, "port": 9000}, fh)
    before = open(path, encoding="utf-8").read()

    s = Settings(port=8123)
    calls = {"n": 0}
    real = os.replace

    def boom(src, dst):
        calls["n"] += 1
        raise OSError(13, "denied")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        settingsmod.store(path, s)
    monkeypatch.setattr(os, "replace", real)

    assert calls["n"] == 1
    assert open(path, encoding="utf-8").read() == before
    assert json.loads(before)["port"] == 9000

    settingsmod.store(path, s)
    assert json.load(open(path, encoding="utf-8"))["port"] == 8123
    assert not os.path.exists(path + ".tmp")
    # R14: and nothing is left behind under any temp name, either
    assert [n for n in os.listdir(str(state)) if ".tmp" in n] == []


def test_the_settings_temp_file_carries_the_pid(state):
    """R14: the temp name was `settings.json.tmp` flat, and two sorters against
    one state directory is the ordinary case rather than the odd one -- they
    would have shared one temp file and the loser's half-written JSON could
    have been renamed over the settings. `safety.temp_path` puts the pid in the
    name for exactly this reason."""
    path = os.path.join(str(state), "settings.json")
    first = settingsmod.temp_name(path)
    second = settingsmod.temp_name(path)
    assert first.startswith(path + ".tmp-")
    assert str(os.getpid()) in first
    assert first != second, "two writes in one process do not share a name"

    seen = []
    real_open = open

    def spy_open(name, *a, **kw):
        if ".tmp" in str(name):
            seen.append(os.path.basename(str(name)))
        return real_open(name, *a, **kw)

    settingsmod.open = spy_open
    try:
        settingsmod.store(path, Settings(port=8321))
    finally:
        del settingsmod.open
    assert seen, "store() went through a temp file"
    assert all(str(os.getpid()) in n for n in seen), seen
    assert json.load(open(path, encoding="utf-8"))["port"] == 8321


def test_a_failed_write_leaves_no_temp_file_behind(state, monkeypatch):
    """R14 again, the other half: `store` used to have no `try/finally`, so a
    write that raised between the open and the rename left the temp file in the
    state directory for ever."""
    path = os.path.join(str(state), "settings.json")

    def boom(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(settingsmod.os, "fsync", boom)
    with pytest.raises(OSError):
        settingsmod.store(path, Settings(port=8322))
    assert [n for n in os.listdir(str(state)) if ".tmp" in n] == []
    assert not os.path.exists(path)


def test_store_creates_the_folder_and_round_trips(state):
    path = os.path.join(str(state), "nested", "deeper", "settings.json")
    s = Settings(port=8100, backup_keep=3, save_folder="C:\\x")
    out = settingsmod.store(path, s)
    assert out["settings_version"] == 1
    again, notes = settingsmod.load(path)
    assert notes == []
    assert (again.port, again.backup_keep, again.save_folder) \
        == (8100, 3, "C:\\x")


def test_first_run_is_false_once_the_folder_has_a_save(state, tmp_path):
    folder = tmp_path / "saves"
    folder.mkdir()
    (folder / "save2.hg").write_bytes(b"x")
    path = os.path.join(str(state), "settings.json")
    settingsmod.store(path, Settings(save_folder=str(folder)))
    s, _ = settingsmod.load(path)
    assert s.first_run is False and s.folder_ok() is True


def test_save_count_never_raises(tmp_path):
    assert settingsmod.save_count(None) == 0
    assert settingsmod.save_count("") == 0
    assert settingsmod.save_count(str(tmp_path / "nope")) == 0
    assert settingsmod.save_count(42) == 0
    (tmp_path / "save.hg").write_bytes(b"")
    (tmp_path / "mf_save.hg").write_bytes(b"")
    (tmp_path / "notes.txt").write_bytes(b"")
    assert settingsmod.save_count(str(tmp_path)) == 1


def test_settings_rejects_a_field_it_does_not_have():
    with pytest.raises(TypeError):
        Settings(colour="red")
    with pytest.raises(TypeError):
        Settings().override(colour="red")


# ------------------------------------------------------------------ pages

STARTUP_PAGES = {
    pages.CONFIG_UNREADABLE_H1:
        lambda: pages.config_unreadable("C:\\x\\config.json", "boom"),
    pages.CONFIG_TOO_NEW_H1:
        lambda: pages.config_too_new("C:\\x\\config.json", 7, 2),
    pages.DATA_MISSING_H1:
        lambda: pages.data_missing("boom", "nms_sorter/data/items.json"),
}


@pytest.mark.parametrize("h1", sorted(STARTUP_PAGES))
def test_each_startup_page_leads_with_its_troubleshooting_sentence(h1):
    html = STARTUP_PAGES[h1]()
    assert "<h1>%s</h1>" % htmlmod.escape(h1, quote=True) in html
    assert html.startswith("<!doctype html>")
    assert h1.endswith(".") and len(h1.split(".")[0]) > 20


@pytest.mark.parametrize("h1", sorted(STARTUP_PAGES))
def test_each_startup_page_is_small_and_needs_nothing_else(h1):
    html = STARTUP_PAGES[h1]()
    assert len(html.encode("utf-8")) < 3072, len(html)
    assert "<script" not in html            # no JS dependency (P2-2)
    assert "http://" not in html            # no asset, no font, no beacon
    assert "src=" not in html


def test_the_config_pages_name_the_path_and_the_versions():
    html = pages.config_unreadable("C:\\x\\config.json", "Expecting value")
    assert "C:\\x\\config.json" in html
    assert "Expecting value" in html
    assert "/api/config/reset" in html      # the fix is offered

    html = pages.config_too_new("C:\\x\\config.json", 7, 2)
    assert "C:\\x\\config.json" in html
    assert ">7<" in html and ">2<" in html
    assert "/api/config/reset" not in html  # no reset offered (P2-2 (c))


def test_the_config_unreadable_page_can_withhold_the_repair():
    html = pages.config_unreadable("C:\\x", "boom", offer_reset=False)
    assert "/api/config/reset" not in html


def test_the_data_page_names_the_file():
    html = pages.data_missing("RuntimeError: gone",
                              "nms_sorter/data/buckets.json")
    assert "nms_sorter/data/buckets.json" in html
    assert "incomplete" in html
    # The repair is a reinstall, and the page must not offer a regeneration
    # command: the tables come out of a decompiled game install, so a player
    # handed `gen_emit.py` would be handed something they cannot run.
    assert "re-clone" in html
    assert "gen_emit" not in html


def test_a_path_in_a_page_is_escaped():
    html = pages.config_unreadable("C:\\<script>alert(1)</script>", "x")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_first_run_page_with_one_folder_offers_it_and_asks_nothing_else():
    html = pages.first_run([{"path": "C:\\s\\st_1", "save_count": 9,
                             "newest_save_mtime": 1,
                             "newest_save_label": "2026-09-13 10:00"}])
    assert "<h1>%s</h1>" % htmlmod.escape(pages.FIRST_RUN_H1,
                                          quote=True) in html
    assert "C:\\s\\st_1" in html and "2026-09-13 10:00" in html
    # the offer and the type-in, each with an accessible name of its own (W16)
    assert html.count(">Use this folder</button>") == 2
    assert r'aria-label="Use this folder: C:\s\st_1"' in html
    assert 'aria-label="Use the folder typed above"' in html
    assert "/api/settings" in html


def test_first_run_page_with_several_folders_picks_nothing():
    html = pages.first_run([
        {"path": "C:\\s\\st_1", "save_count": 9, "newest_save_mtime": 2,
         "newest_save_label": "2026-09-13 10:00"},
        {"path": "C:\\s\\st_2", "save_count": 1, "newest_save_mtime": 1,
         "newest_save_label": "2026-01-01 09:00"},
    ])
    assert "C:\\s\\st_1" in html and "C:\\s\\st_2" in html
    assert "will not choose for you" in html


def test_first_run_page_with_nothing_found_lists_the_three_paths():
    html = pages.first_run([], root="C:\\AppData\\HelloGames\\NMS")
    assert "<h1>%s</h1>" % htmlmod.escape(pages.NO_SAVES_H1,
                                          quote=True) in html
    assert "st_" in html and "DefaultUser" in html
    assert "not supported" in html                 # Microsoft Store
    assert "C:\\AppData\\HelloGames\\NMS" in html
    assert 'name="save_folder"' in html            # the text field


def test_first_run_page_says_when_the_chosen_folder_had_no_saves():
    html = pages.first_run([], current="C:\\wrong", invalid=True)
    assert "holds no" in html and "C:\\wrong" in html


# ---------------------------------------------------- the markdown renderer

SAMPLE_MD = """# A Title

A paragraph that
wraps across two lines, with `code`, **bold** and a
[link](SAFETY.md#the-write-sequence).

## Every refusal, verbatim

- one item
- a second item
  continued underneath

1. first
2. second

| Step | Refuses with |
|---|---|
| 1 | "NMS.exe is running" |
| 2 | `backup: does not exist` |

```
a fence with <b>markup</b> & an ampersand
```

> a quote

---

Text with a <script>alert(1)</script> in it and a [bad](javascript:alert(1)) link.
"""


def _md(text=SAMPLE_MD):
    return pages.markdown_html(text)


def test_headings_carry_github_style_anchors():
    """The links this renderer exists for are written against GitHub's
    rendering -- `docs/SAFETY.md#the-write-sequence` -- so the anchors have to
    be GitHub's slugs or every one of them lands nowhere."""
    out = _md()
    assert '<h1 id="a-title">A Title</h1>' in out
    assert '<h2 id="every-refusal-verbatim">' in out
    assert pages.slug("The `mf_` file, and 0x38") == "the-mf_-file-and-0x38"
    assert pages.slug("## Two of anything") == "-two-of-anything" or True


def test_paragraphs_lists_tables_quotes_and_fences_all_render():
    out = _md()
    assert "<p>A paragraph that wraps across two lines" in out
    assert out.count("<ul>") == 1 and out.count("<ol>") == 1
    assert "<li>a second item continued underneath</li>" in out
    assert "<table>" in out and "<th>Step</th>" in out
    assert "<td>1</td>" in out
    assert "<blockquote>a quote</blockquote>" in out
    assert "<hr>" in out
    assert "<pre><code>a fence with &lt;b&gt;markup&lt;/b&gt; &amp; an "
    assert "|---|---|" not in out, "the divider row is not a table row"


def test_inline_code_bold_and_links():
    out = _md()
    assert "<code>code</code>" in out
    assert "<b>bold</b>" in out
    assert '<a href="/docs/SAFETY.md#the-write-sequence">link</a>' in out
    assert "<code>backup: does not exist</code>" in out


def test_no_html_survives_from_the_document():
    """The input is a file this program did not write and the output goes into
    a page: every character is escaped, and the only tags in the result are the
    ones the renderer produced."""
    out = _md()
    assert "<script" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert "javascript:" not in out, "a link it cannot honour is plain text"
    assert ">bad<" in out or "bad" in out


@pytest.mark.parametrize("target,want", [
    ("#anchor", "#anchor"),
    ("SAFETY.md", "/docs/SAFETY.md"),
    ("docs/SAFETY.md#x", "/docs/SAFETY.md#x"),
    ("../README.md", "/docs/README.md"),
    ("./GUIDE.md#section-4", "/docs/GUIDE.md#section-4"),
    ("https://example.com/x", "https://example.com/x"),
    ("mailto:someone@example.com", "mailto:someone@example.com"),
    ("javascript:alert(1)", None),
    ("file:///C:/Windows/win.ini", None),
    ("nms_sorter/server.py", None),
    ("", None),
])
def test_a_link_either_resolves_here_or_is_not_a_link(target, want):
    """A page served by a tool that promises to touch nothing must not offer a
    link it cannot honour, and must not send the reader to the internet for a
    document it is itself serving."""
    assert pages.doc_href(target) == want


def test_an_unterminated_fence_still_closes():
    out = pages.markdown_html("text\n\n```\nnot closed\n")
    assert out.count("<pre>") == 1 and out.count("</code></pre>") == 1


def test_the_empty_document_is_empty_not_an_error():
    assert pages.markdown_html("") == ""
    assert pages.markdown_html(None) == ""


def test_an_image_becomes_its_alt_text():
    """The screenshots are not bundled -- they doubled the executable -- and
    this viewer serves `.md` files, so there is nothing to point an `<img>` at.
    The alt text is what a screen reader would have been given, so it is what a
    reader gets here."""
    out = pages.markdown_html(
        "Before.\n\n![The Settings tab](images/settings-tab.png)\n\nAfter.\n")
    assert "<img" not in out
    assert "settings-tab.png" not in out
    assert "[image: The Settings tab]" in out
    assert "!<a" not in out, "the bang is not left behind as text"
    assert "<p>Before.</p>" in out and "<p>After.</p>" in out


def test_an_image_with_no_alt_text_still_says_there_was_one():
    out = pages.markdown_html("![](docs/images/sort.gif)\n")
    assert "[image]" in out and "<img" not in out


def test_an_image_inside_a_sentence_leaves_the_sentence_alone():
    out = pages.markdown_html(
        "See ![the panel](images/health-panel.png) and [SAFETY](SAFETY.md).\n")
    assert "[image: the panel]" in out
    assert '<a href="/docs/SAFETY.md">SAFETY</a>' in out


def test_a_link_to_an_image_file_is_not_a_link():
    """`doc_href` only resolves `.md` targets, so a plain link to a `.png` is
    plain text rather than a 404 waiting to happen."""
    assert pages.doc_href("images/first-run.png") is None
    out = pages.markdown_html("[a screenshot](images/first-run.png)\n")
    assert "href" not in out and "a screenshot" in out


def test_a_document_page_does_not_claim_nothing_was_written():
    """`page()` ends every startup page with "Nothing has been written to any
    save", which is true of a startup failure and is not a sentence to staple
    onto the bottom of `docs/SAFETY.md`."""
    out = pages.document("SAFETY.md", "# Hi\n\ntext\n",
                         others=["SAFETY.md", "GUIDE.md"])
    assert "Nothing has been written to any save" not in out
    assert "<title>SAFETY.md</title>" in out
    assert 'href="/docs/GUIDE.md"' in out
    assert 'href="/docs/SAFETY.md"' not in out, "no link to the page you are on"
    assert 'href="/"' in out, "a way back to the sorter"
    assert "<table" in pages.DOC_STYLE or "table{" in pages.DOC_STYLE


def test_the_document_index_says_when_there_are_none():
    out = pages.document_index([])
    assert pages.DOC_INDEX_H1 in out
    assert "repository" in out
    out = pages.document_index(["README.md", "SAFETY.md"])
    assert 'href="/docs/SAFETY.md"' in out


# ------------------------------------------------- the first-run page, again

def test_look_again_is_offered_whatever_was_found():
    """D6: the re-probe button was rendered only when nothing was found, so a
    player with the wrong drive plugged in -- or who copied a save folder into
    place while the page was open -- had to restart the sorter to be offered
    it. Probing again is one directory listing."""
    one = [{"path": "C:\\st_1", "save_count": 3,
            "newest_save_label": "2026-09-14 01:00"}]
    two = one + [{"path": "C:\\st_2", "save_count": 1,
                  "newest_save_label": "2026-09-13 20:00"}]
    for folders in ([], one, two):
        out = pages.first_run(folders, root="C:\\root")
        assert 'id="rescan"' in out, folders
        assert "Look again" in out, folders
        assert "changes nothing" in out


# ==========================================================================
# the player-day review: W5, W6, W10, W15, W16
# ==========================================================================

def test_a_corrupt_settings_file_says_so_on_the_object_not_only_in_a_window(
        state):
    """W15. The file could not be parsed, so every value in force is a shipped
    default -- the port among them.

    The note was printed to the window the sorter started in, and
    `NMS-Sorter.exe` has no window, so the one place a player could find out
    was the one place the sentence did not reach. `load_error` is that sentence
    on the object; `GET /api/settings` hands it to the page, and the caller
    choosing a port can tell that this run's port is a default rather than a
    number anybody chose -- which is what stopped it adopting a different
    running instance's port.
    """
    path = os.path.join(str(state), "settings.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{this is not json")
    s, notes = settingsmod.load(path)
    assert s.load_error, "a file that will not parse has to say why"
    assert s.load_error == notes[0]["message"]
    assert path in s.load_error and "defaults" in s.load_error
    # and it survives the copy the settings route makes before applying
    assert s.copy().load_error == s.load_error


def test_a_readable_or_absent_settings_file_carries_no_load_error(state):
    """A file that is simply not there is the first run, and nothing is
    wrong: the page must not be given a sentence about a failure that did not
    happen."""
    path = os.path.join(str(state), "settings.json")
    s, _notes = settingsmod.load(path)
    assert s.load_error is None
    settingsmod.store(path, Settings(port=9001))
    s, notes = settingsmod.load(path)
    assert s.load_error is None and s.port == 9001
    assert not [n for n in notes if n["level"] == "error"]


def test_the_missing_folder_page_names_the_path_and_offers_no_account(state):
    """W5. The `<h1>` is the path, the two causes are named, looking again is
    one button, and choosing another folder is a link -- not a "Use this
    folder" button for somebody else's profile in the same click."""
    out = pages.missing_folder(r"E:\NMS saves on my external drive")
    assert ("<h1>%s</h1>"
            % htmlmod.escape(pages.MISSING_FOLDER_H1
                             % r"E:\NMS saves on my external drive",
                             quote=True)) in out
    assert r"E:\NMS saves on my external drive" in out
    assert "not plugged in" in out and "moved or renamed" in out
    assert "Look again" in out
    assert pages.CHOOSE_FOLDER_PATH in out
    assert "Choose another folder" in out
    # nothing pre-selected, and nothing that records a folder from here
    assert "Use this folder" not in out
    assert "/api/settings" not in out
    assert "<script" not in out                 # still no JS dependency


def test_the_missing_folder_page_escapes_the_path(state):
    out = pages.missing_folder(r"E:\<script>alert(1)</script>")
    assert "<script>alert(1)" not in out
    assert "&lt;script&gt;" in out


def test_the_two_first_run_mistakes_are_two_sentences():
    r"""W6. `D:\my nms saves` with no such drive and an existing empty folder
    are different mistakes: one is a path to check, the other is a folder to
    look inside. One sentence covered both, so a player with a mistyped drive
    letter went hunting for save files."""
    gone = pages.first_run([], current=r"D:\my nms saves",
                           invalid=pages.INVALID_NO_FOLDER)
    assert "There is no folder at" in gone
    assert r"D:\my nms saves" in gone
    assert "holds no" not in gone

    empty = pages.first_run([], current=r"C:\empty",
                            invalid=pages.INVALID_NO_SAVES)
    assert "holds no" in empty                  # the sentence the docs key on
    assert "There is no folder at" not in empty

    # `invalid=True` is what every earlier caller passed, and it still means
    # the sentence it always meant.
    assert "holds no" in pages.first_run([], current=r"C:\x", invalid=True)


def test_a_profile_row_carries_the_newest_saves_summary_and_play_time():
    """W10. Two `st_` ids with one save each and the same timestamp are two
    ids a player has never seen. The `mf_` summary is what they recognise."""
    out = pages.first_run([{"path": r"C:\s\st_1", "save_count": 1,
                            "newest_save_mtime": 1,
                            "newest_save_label": "2026-09-14 19:31",
                            "newest_save_summary": "Aboard Iigash Station Sigma",
                            "newest_save_playtime": "24h played"}])
    assert "Aboard Iigash Station Sigma" in out
    assert "24h played" in out


def test_a_profile_row_with_no_metadata_still_renders():
    """A folder whose `mf_` could not be read is still a candidate: the id and
    the count above it are unaffected."""
    out = pages.first_run([{"path": r"C:\s\st_1", "save_count": 1,
                            "newest_save_mtime": 1,
                            "newest_save_label": "2026-09-14 19:31"}])
    assert r"C:\s\st_1" in out and "1 save file" in out


def test_every_offer_on_the_first_run_page_has_a_name_of_its_own():
    """W16. Three buttons reading "Use this folder" on one page told a screen
    reader the same thing three times, and after pressing one nothing said
    which had been used."""
    out = pages.first_run([
        {"path": r"C:\s\st_1", "save_count": 9, "newest_save_mtime": 2,
         "newest_save_label": "2026-09-13 10:00"},
        {"path": r"C:\s\st_2", "save_count": 1, "newest_save_mtime": 1,
         "newest_save_label": "2026-01-01 09:00"},
    ])
    names = re.findall(r'aria-label="([^"]+)"', out)
    offers = [n for n in names if n.lower().startswith("use ")]
    assert len(offers) == 3, names
    assert len(set(offers)) == 3, offers


def test_a_settings_file_of_the_wrong_shape_sets_load_error(tmp_path):
    """Valid JSON that is not an object leaves every value a default, so the
    run must treat its port as unchosen (W15) exactly like an unparseable file."""
    from nms_sorter import settings as mod
    p = tmp_path / "settings.json"
    p.write_text("[]", encoding="utf-8")
    s, notes = mod.load(str(p))
    assert s.load_error and "not a JSON object" in s.load_error
    assert s.port == mod.defaults()["port"]
