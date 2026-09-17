"""P6-2: the same seven checks over every save we can lay hands on.

A *corpus case* is one save file plus its `mf_`. Two things produce them:

* `--real-saves PATH` -- a **copy** of a real save folder. The operator's own
  nine saves are run this way. No save is committed to this repository: a save
  names a player, their bases and their coordinates, so the real-save leg is
  something a maintainer runs locally and never something the clone carries.
* `tools/make_fixture.py` -- every variant it can build, which is the part of
  the GOAL.md section 4 corpus target list that can be synthesised.

The seven checks, in the order GOAL.md lists them:

    a  the payload re-serialises byte for byte
    b  it reframes back to itself, compressed and uncompressed
    c  `SaveFile` loads it, or refuses it with `UnsupportedSave` -- and only
       for the one documented reason, a save with no `BaseContext`
    d  the container, chest and extractor counts match `<file>.expect.json`
       if there is one, and are printed if there is not
    e  a plan under the shipped default config completes with no error note
       beyond a source this save does not have
    f  `safety.apply_plan` on a throwaway copy either writes, with every step
       `ok` and per-item totals conserved, or refuses with a sentence
    g  the file it wrote still round-trips byte for byte

Checks (a) to (e) read. Only (f) writes, and only to a pytest temp directory:
the folder given to `--real-saves` is opened read-only and is documented as a
copy. Nothing here ever prints a real player, a base or a coordinate, so a
failure can be pasted into an issue.

Each case runs once, module-scoped, and the seven tests read the result. Doing
it the other way -- seven applies per save -- would multiply the slowest part of
the suite by seven for no extra coverage.
"""
import io
import json
import os
import re
import shutil

import pytest

from nms_sorter import codec, config as cfgmod, planner, safety
from nms_sorter.codec import dumps, frame_payload, loads, read_payload
from nms_sorter.savemodel import SaveFile, SaveGate, UnsupportedSave
from tools import make_fixture

HERE = os.path.dirname(os.path.abspath(__file__))

try:                                    # the census is defined there, once
    from test_safety import _census as census
except ImportError:                     # pragma: no cover - import-mode only
    def census(sf):
        """Per-item totals across every sortable container."""
        out = {}
        for c in sf.containers():
            if not c.sortable:
                continue
            for s in c.slots():
                out[s.id] = out.get(s.id, 0) + s.amount
        return out

#: the one plan note that is an error and is still expected: the shipped
#: default config names four sources, and a save without a freighter or a
#: corvette has two of them missing. That is a fact about the save, not a
#: mistake in the configuration, and GOAL.md P6-2 names it as the exception.
ABSENT_SOURCE = "is not a container in this save"


# ---------------------------------------------------------------------------
# collecting the cases
# ---------------------------------------------------------------------------

#: `save.hg`, `save10.hg`, and the labelled form `normal-12chests-save9.hg`.
#: `mf_` sidecars are excluded by requiring the name to *end* in the save part.
SAVE_RE = re.compile(r"(?:^|-)save\d*\.hg$", re.I)


def _saves_in(folder):
    """[path] for every save file in a folder, `mf_` sidecars excluded.

    Flat: `--real-saves` is a save folder, which is how the game lays one out.
    """
    if not folder or not os.path.isdir(folder):
        return []
    out = []
    for fn in sorted(os.listdir(folder)):
        if SAVE_RE.search(fn) and not fn.lower().startswith("mf_"):
            out.append(os.path.join(folder, fn))
    return out


def _specs(config):
    """[(id, spec)] for everything to run, in a stable order."""
    specs = []
    real = config.getoption("--real-saves")
    for path in _saves_in(real):
        specs.append(("real-" + os.path.basename(path),
                      {"kind": "file", "path": path}))
    for variant in make_fixture.VARIANTS:
        specs.append(("variant-" + variant,
                      {"kind": "variant", "variant": variant}))
    return specs


def pytest_generate_tests(metafunc):
    if "case" not in metafunc.fixturenames:
        return
    specs = _specs(metafunc.config)
    metafunc.parametrize("case", [s for _i, s in specs],
                         ids=[i for i, _s in specs],
                         indirect=True, scope="module")


# ---------------------------------------------------------------------------
# running one case
# ---------------------------------------------------------------------------

def _expectations(path):
    """`<file>.expect.json` beside the save, or None.

    `{"containers": N, "chests": N, "extractors": N, "version": N}`, counted
    in the game and written beside the save by hand. It is the only
    independent check the corpus has: the code counting its own containers and
    agreeing with itself proves nothing (GOAL.md rule 5.7.10).
    """
    sidecar = path + ".expect.json"
    if not os.path.exists(sidecar):
        return None
    with io.open(sidecar, encoding="utf-8") as fh:
        return json.load(fh)


def _counts(save):
    return {"containers": len(save.containers()),
            "chests": len([c for c in save.containers()
                           if c.key.startswith("chest")]),
            "extractors": len(save.extractor_containers()),
            "version": save.version()}


def _run(spec, workdir, fixture_variant):
    """Every check for one save. Never raises: a failure becomes a field.

    A raising runner would lose the rest of the row, and the row is the point:
    GOAL.md P6-2 asks for "version, containers, chests, extractors, plan rows,
    apply result" per file, which is only useful when a save that refuses at
    step 1 still reports its container count.
    """
    if spec["kind"] == "variant":
        built = fixture_variant(spec["variant"])
        src, meta = built["save"], built["meta"]
        label = "variant:" + spec["variant"]
    else:
        src = spec["path"]
        sib = os.path.join(os.path.dirname(src), "mf_" + os.path.basename(src))
        meta = sib if os.path.exists(sib) else None
        label = spec.get("label") or os.path.basename(src)

    out = {"label": label, "source": src, "meta": meta, "errors": [],
           "gated": None}

    # ---- a: the payload re-serialises byte for byte
    payload, info = read_payload(src)
    escape = b"\\/" in payload
    out["payload_len"] = len(payload)
    out["blocks"] = info["blocks"]
    out["escape_slash"] = escape
    doc = loads(payload)
    out["identity"] = dumps(doc, escape_slash=escape) == payload

    # ---- b: it reframes back to itself, both ways
    out["reframe_compressed"] = codec.read_payload_bytes(
        frame_payload(payload, compress=True))[0] == payload
    out["reframe_plain"] = codec.read_payload_bytes(
        frame_payload(payload, compress=False))[0] == payload

    # ---- c: it loads, or refuses for the one documented reason
    out["unsupported"] = None
    save = None
    try:
        save = SaveFile(src)
    except UnsupportedSave as exc:
        out["unsupported"] = str(exc)
    if save is None:
        out["counts"] = None
        out["expect"] = _expectations(src) if spec["kind"] == "file" else None
        out["plan_rows"] = None
        out["apply"] = "not attempted: %s" % out["unsupported"]
        return out

    # ---- d: the counts, against a hand count if one sits beside the save
    out["counts"] = _counts(save)
    out["expect"] = _expectations(src) if spec["kind"] == "file" else None
    out["difficulty"] = save.difficulty()

    # ---- e: a plan under the shipped default
    #
    # A `refuse`-level save gate stops the plan rather than the apply: an
    # expedition save is not planned at all (DECISIONS.md, 2026-09-14), so the
    # row records the sentence and stops here. A `warn` gate -- a version
    # outside the tested range -- is a plan note and the apply refuses, which
    # check (f) already accepts.
    cfg = cfgmod.default_config()
    try:
        plan, _commit = planner.build_plan(save, cfg)
    except SaveGate as exc:
        out["gated"] = str(exc)
        out["plan_rows"] = None
        out["plan_skips"] = 0
        out["plan_unknown"] = 0
        out["notes"] = []
        out["plan_errors"] = []
        out["fingerprint"] = None
        out["apply"] = "refused"
        out["refusal"] = str(exc)
        out["roundtrip_after"] = None
        return out
    out["plan_rows"] = len(plan.rows)
    out["plan_skips"] = len(plan.skips)
    out["plan_unknown"] = len(getattr(plan, "unknown", []) or [])
    out["notes"] = list(plan.notes)
    out["plan_errors"] = [n["message"] for n in plan.notes
                          if n.get("level") == "error"
                          and ABSENT_SOURCE not in n["message"]]
    out["fingerprint"] = plan.fingerprint

    # ---- f: apply on a copy, or the sentence it refused with
    lab = os.path.join(workdir, "apply")
    if os.path.isdir(lab):
        shutil.rmtree(lab)
    os.makedirs(lab)
    target = os.path.join(lab, os.path.basename(src))
    shutil.copy2(src, target)
    if meta:
        shutil.copy2(meta, os.path.join(lab, "mf_" + os.path.basename(src)))
    out["target"] = target

    before = SaveFile(target)
    out["census_before"] = census(before)
    tplan, _c = planner.build_plan(SaveFile(target), cfg)
    out["target_fingerprint"] = tplan.fingerprint
    try:
        rep, res = safety.apply_plan(target, cfg, tplan.fingerprint,
                                     planner.build_plan,
                                     os.path.join(lab, "backups"))
        out["apply"] = "written"
        out["report"] = rep.steps
        out["result"] = res
        out["census_after"] = census(SaveFile(target))
        # ---- g: the file it wrote still round-trips
        vrep = safety.Report()
        safety.verify_roundtrip(target, vrep)
        out["roundtrip_after"] = all(s["state"] in ("ok", "info")
                                     for s in vrep.steps)
    except safety.Refused as exc:
        out["apply"] = "refused"
        out["refusal"] = str(exc)
        out["roundtrip_after"] = None
    except Exception as exc:                              # noqa: BLE001
        # Named and carried, not swallowed: GOAL.md P6-2 asks for "if lane B's
        # savemodel/safety are mid-edit and something fails, name it and
        # continue", and a corpus row that says which save raised what is the
        # only way to tell a broken save from a broken build.
        out["apply"] = "error"
        out["refusal"] = "%s: %s" % (type(exc).__name__, exc)
        out["roundtrip_after"] = None
    return out


@pytest.fixture(scope="module")
def case(request, tmp_path_factory, fixture_variant, no_game_running):
    """One corpus case, run once. `no_game_running` makes step 1 pass."""
    spec = request.param
    work = tmp_path_factory.mktemp("corpus")
    return _run(spec, str(work), fixture_variant)


# ---------------------------------------------------------------------------
# the seven checks
# ---------------------------------------------------------------------------

def test_a_the_payload_re_serialises_byte_for_byte(case):
    assert case["identity"], \
        ("%s: dumps(loads(payload)) == payload (%d bytes, %d block(s), "
         "pre-Frontiers escaping %s)"
         % (case["label"], case["payload_len"], case["blocks"],
            case["escape_slash"]))


def test_b_it_reframes_in_both_modes(case):
    assert case["reframe_compressed"] and case["reframe_plain"], \
        ("%s: reframes back to the same payload compressed (%s) and "
         "uncompressed (%s)"
         % (case["label"], case["reframe_compressed"], case["reframe_plain"]))


def test_c_it_loads_or_refuses_for_the_one_documented_reason(case):
    """`UnsupportedSave` is allowed, but only for a pre-Waypoint document.

    The refusal has to name Waypoint, because "not supported" with no reason
    is indistinguishable from a bug, and two of the operator's own saves are
    this case: version 4645, written before the player state moved under
    `BaseContext`.
    """
    if case["unsupported"] is None:
        assert case["counts"] is not None, "%s loaded" % case["label"]
        return
    msg = case["unsupported"]
    assert "BaseContext" in msg and "Waypoint" in msg, \
        ("%s: an unsupported save says which shape and which release: %r"
         % (case["label"], msg))


def test_d_the_counts_match_the_hand_count(case):
    """Against `<file>.expect.json` when one sits beside the save, else printed."""
    if case["counts"] is None:
        pytest.skip("%s: %s" % (case["label"], case["apply"]))
    counts = case["counts"]
    print("%s: %s" % (case["label"], counts))
    expect = case["expect"]
    if not expect:
        pytest.skip("%s has no .expect.json; counts printed above"
                    % case["label"])
    wrong = dict((k, (v, counts.get(k))) for k, v in expect.items()
                 if counts.get(k) != v)
    assert not wrong, \
        ("%s: discovery matches the hand count; expected vs got %s"
         % (case["label"], wrong))


def test_e_a_plan_under_the_default_config_completes(case):
    if case["counts"] is None:
        pytest.skip("%s: %s" % (case["label"], case["apply"]))
    if case["gated"]:
        pytest.skip("%s: refused at plan time -- %s"
                    % (case["label"], case["gated"]))
    assert not case["plan_errors"], \
        ("%s: the only error-level plan note allowed is a source this save "
         "does not have; got %s" % (case["label"], case["plan_errors"]))
    print("%s: %d row(s), %d skip(s), %d unknown id(s), fingerprint %s"
          % (case["label"], case["plan_rows"], case["plan_skips"],
             case["plan_unknown"], case["fingerprint"]))


def test_f_apply_either_writes_cleanly_or_refuses_with_a_sentence(case):
    """Two acceptable outcomes, and nothing in between.

    A refusal is a pass: an empty plan, a save version outside the tested
    range and an `mf_` this build will not patch are all refusals by design,
    and GOAL.md P6-2 asks for the sentence to be printed rather than for the
    corpus to be narrowed until every save writes.

    What is not acceptable is an exception that is not a `Refused`: that is a
    build defect, and it fails here naming the save.
    """
    if case["counts"] is None:
        pytest.skip("%s: %s" % (case["label"], case["apply"]))
    if case["apply"] == "refused":
        print("%s: refused -- %s" % (case["label"], case["refusal"]))
        assert len(case["refusal"]) > 10, \
            "%s refused with a sentence, not a code" % case["label"]
        return
    assert case["apply"] == "written", \
        ("%s: apply either writes or refuses; got %s (%s)"
         % (case["label"], case["apply"], case.get("refusal")))
    bad = [s for s in case["report"] if s["state"] not in ("ok", "info")]
    assert not bad, "%s: every step ok; %s" % (case["label"], bad)
    assert case["census_before"] == case["census_after"], \
        ("%s: per-item totals across every sortable container are conserved"
         % case["label"])
    print("%s: written, %d row(s), backup %s"
          % (case["label"], case["result"]["rows"],
             os.path.basename(case["result"]["backup"])))


def test_g_the_written_file_still_round_trips(case):
    if case["counts"] is None or case["apply"] != "written":
        pytest.skip("%s: nothing was written (%s)"
                    % (case["label"], case["apply"]))
    assert case["roundtrip_after"], \
        "%s: the file apply wrote still round-trips byte for byte" % case["label"]


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------

def test_the_corpus_table(case, record_property, capsys):
    """One row per save, printed. This is the deliverable of the ticket.

    `docs/SAFETY.md`'s "Tested on" table is these rows, so the wording here is
    chosen to be pasteable: no paths, no player names, no coordinates.
    """
    c = case["counts"] or {}
    row = ("%-26s ver %-6s containers %-4s chests %-3s extractors %-3s "
           "stacks %-7s rows %-5s %s"
           % (case["label"], c.get("version", "-"),
              c.get("containers", "-"), c.get("chests", "-"),
              c.get("extractors", "-"), case.get("difficulty", "-"),
              case["plan_rows"] if case["plan_rows"] is not None else "-",
              case["apply"] if case["apply"] != "refused"
              else "refused: " + case["refusal"][:70]))
    record_property("corpus_row", row)
    with capsys.disabled():
        print("  " + row)


def test_the_corpus_has_cases_at_all(request):
    """The variants alone are a corpus; a real save is a bonus.

    Asserted so that a broken `make_fixture` shows up here as "no cases" rather
    than as a suite that silently runs nothing.
    """
    specs = _specs(request.config)
    files = [i for i, s in specs if s["kind"] == "file"]
    variants = [i for i, s in specs if s["kind"] == "variant"]
    print("corpus: %d real save(s) %s, %d synthesised variant(s)"
          % (len(files), files, len(variants)))
    assert len(variants) >= 15, \
        "every make_fixture variant is a corpus case: %s" % variants
