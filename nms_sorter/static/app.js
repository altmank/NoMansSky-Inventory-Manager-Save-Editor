/* NMS inventory sorter -- the whole client. Vanilla, no build step. */
"use strict";

const S = {           // everything the page knows
  boot: null, save: null, config: null, saved: null, issues: [],
  plan: null, buckets: [], open: new Set(), dirty: false,
  game: null,        // the last /api/game answer, polled every 15 s
  browse: null       // the last /api/browse page: rows, buckets, counts
};

const BUCKET_HUE = {
  raw_resources: 28, refined_crafted: 46, trade_goods: 140, food_ingredients: 96,
  fish: 190, tech_upgrades: 210, tech_upgrades_elite: 265, utility_consumables: 168,
  curiosities_artifacts: 300, expedition_rewards: 330, base_structures: 15,
  base_decor: 350, ship_construction: 200, cosmetics_appearance: 285,
  salvage_junk: 0, system_meta: 220, unsorted: 220
};
const hue = k => BUCKET_HUE[k] === undefined ? 220 : BUCKET_HUE[k];
const bcol = (k, l) => `hsl(${hue(k)} ${k === "system_meta" || k === "unsorted" ? 8 : 62}% ${l || 58}%)`;

/* ------------------------------------------------------------- helpers */
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const el = (tag, cls, txt) => { const n = document.createElement(tag);
  if (cls) n.className = cls; if (txt !== undefined) n.textContent = txt; return n; };
const esc = s => String(s === null || s === undefined ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const num = n => (n === null || n === undefined) ? "-" : n.toLocaleString("en-US");

/* "1 rule" / "2 rules". The count is always in hand at the call site, so a
   printed "(s)" was never anything but a shrug (U29). */
const plural = (n, one, many) =>
  `${num(n)} ${n === 1 ? one : (many || one + "s")}`;

/* ------------------------------------------------------- focus (U2)
   Every list on this page is re-rendered from the config after any edit, which
   dropped focus to `<body>` and made a reorder button impossible to press
   twice. Each control carries a stable `data-focus-key`; the key is read before
   a re-render and re-focused after it. A handler that knows where focus should
   land -- a row that moved, a row that was removed -- names the key itself with
   `focusAfter()`, which wins over the remembered one. */
let WANT_FOCUS = null;
function focusAfter(key) { WANT_FOCUS = key; }
function fk(node, key) {
  if (node && key) node.setAttribute("data-focus-key", key);
  return node;
}
function focusMemo() {
  const a = document.activeElement;
  if (!a || a === document.body || !a.closest) return null;
  const n = a.closest("[data-focus-key]");
  return n ? n.getAttribute("data-focus-key") : null;
}
/* A key that no longer exists, or exists on something the browser will not
   focus, falls back along the row it belonged to rather than to `<body>`:
   `src-0-up` at the top of the list is inert, so focus goes to `src-0-down`. */
function focusRestore(key) {
  if (!key) return false;
  // A control inside a closed `details` has no `offsetParent`, so the shelf's
  // own keys open the fold before they are looked for: a category that has
  // just lost its last destination is shown where it went.
  if (/^unrouted-/.test(key)) openShelf();
  const tries = [key];
  if (/-up$/.test(key)) tries.push(key.replace(/-up$/, "-down"));
  if (/-down$/.test(key)) tries.push(key.replace(/-down$/, "-up"));
  // A control that is only in the DOM once its opener has been pressed falls
  // back to the opener: `add-to-chest1` to `add-to-chest1-open`.
  if (!/-open$/.test(key)) tries.push(key + "-open");
  // The last segment, not "every trailing lower-case run": `-[a-z-]+$` never
  // matched a key ending in a digit at all -- which is every key naming a
  // container, `add-to-chest1` and `cont-vehicle0-name` among them -- and
  // where it did match it was greedy from the left, so `add-to-suit` fell
  // back to `add`, which is nothing. One segment off the end is a container
  // row's own group, and it can carry a digit.
  tries.push(key.replace(/-[^-]+$/, ""));
  for (const k of tries) {
    // A key can also be an element's id: the controls that are never
    // re-rendered -- revert, save configuration, the add-rule box -- are named
    // by their ids rather than carrying a second attribute.
    const n = document.querySelector('[data-focus-key="' + CSS.escape(k) + '"]') ||
      document.getElementById(k);
    if (n && !n.disabled && n.offsetParent !== null) { n.focus(); return true; }
  }
  return false;
}
/* Run a re-render with focus carried across it. */
function withFocus(fn) {
  const before = focusMemo();
  const r = fn();
  const key = WANT_FOCUS || before;
  WANT_FOCUS = null;
  if (key) focusRestore(key);
  return r;
}

/* --------------------------------------------- one sentence for a dead server
   `Failed to fetch` is Chrome's `TypeError`, not a sentence, and Firefox says
   something else again (U12). Every route goes through `api()`, so the
   substitution happens once. */
const DEAD_SERVER = "the sorter is not answering; is it still running? " +
  "Check the window it started in, then reload this page.";

/* Where a validator `where` path points, in the words the page uses. The raw
   path stays in the `title` for anyone who edits the file by hand (U10). */
function whereLabel(where) {
  const w = String(where === null || where === undefined ? "" : where);
  if (w === "bucket_rules") return "category routing";
  if (w === "sources") return "sources";
  if (w === "labels") return "container names";
  if (w === "item_rules") return "per-item rules";
  if (w === "item_buckets") return "item overrides";
  if (w === "custom_buckets") return "your categories";
  if (w === "never_move") return "items never moved";
  if (w === "never_buckets") return "categories never moved";
  if (w === "options") return "options";
  if (w === "config_version") return "configuration file";
  if (w === "save") return "this save";
  if (w === "version") return "save version";
  if (w === "expedition") return "expedition save";
  if (w === "plan") return "plan";
  if (w === "restart") return "restart";
  const m = w.match(
    /^(item_rules|bucket_rules|sources|custom_buckets|never_move)\[(\d+)\]$/);
  if (m) {
    const n = +m[2] + 1;
    if (m[1] === "item_rules") return "per-item rule " + n;
    if (m[1] === "bucket_rules") return "category routing " + n;
    if (m[1] === "sources") return "source " + n;
    if (m[1] === "never_move") return "never-moved item " + n;
    return "your category " + n;
  }
  const t = w.match(/^options\.tidy\[(\d+)\]$/);
  if (t) return "tidy container " + (+t[1] + 1);
  const ib = w.match(/^item_buckets\[(.+)\]$/);
  if (ib) return "override on " + ib[1];
  // Any other `options.<key>`: the key is a tuning number's own name and
  // reads as English once the underscores go, but it is said to be an option
  // rather than left looking like a field the player has never seen.
  const op = w.match(/^options\.(.+)$/);
  if (op) return "the " + op[1].replace(/_/g, " ") + " option";
  return w.replace(/_/g, " ");
}

/* One word per concept for anything the user reads. The server speaks the
   config's own vocabulary -- `bucket` is the JSON field name -- so every
   message that reaches a person passes through here first. `where` strings are
   JSON paths and are left exactly as they are. */
/* Item ids the page can name. A server message that quotes an id gets the
   item's own name instead, because the row above it says "Sodium" (V3). */
function itemName(id) {
  const key = String(id || "").replace(/^\^/, "");
  const rule = ((S.config && S.config.item_rules) || [])
    .find(r => String(r.item || "").replace(/^\^/, "") === key && r._name);
  if (rule) return rule._name;
  const row = IB.rowOf.get(key) || IB.rowOf.get(id);
  return (row && row.name) ? row.name : null;
}

/* The config file's own key for a category, laundered to the label the rest of
   the page shows. X6 (review 3): the most prominent warning on Categories read
   "category 'system_meta' is also in never_buckets", where `system_meta` is
   displayed as "System and Non-Inventory" on every other surface and
   `never_buckets` is a JSON key. `words()` is the one place a server sentence
   passes through on its way to a person, and this is the same laundering
   `bucket` -> `category` has always had. */
function bucketLabelOf(key) {
  const k = String(key || "");
  const b = (S.buckets || []).find(x => x.key === k) ||
    (((S.config && S.config.custom_buckets) || []).find(x => x.key === k));
  if (b && b.label) return b.label;
  if (k === "unsorted") return "Unsorted";
  return null;
}

function words(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/ -- /g, ": ")
    // V3: a Python `None` where a destination belongs, and a quoted id where a
    // name belongs. Both come from the validator; neither is a word.
    .replace(/\brouted to None\b/g, "routed to no destination")
    .replace(/\bto None by rule\b/g, "to no destination by rule")
    .replace(/'([A-Z0-9_^#]{3,})'/g, (m, id) => {
      const n = itemName(id);
      return n ? `${n} (${id.replace(/^\^/, "")})` : m;
    })
    // X6: a quoted category key, in the words the page uses, with the key kept
    // in brackets for anyone who edits the file by hand.
    .replace(/'([a-z][a-z0-9_]{2,})'/g, (m, key) => {
      const n = bucketLabelOf(key);
      return n ? `${n} (${key})` : m;
    })
    // X6: and the two list names the validator quotes as bare identifiers.
    .replace(/\bnever_buckets\b/g, "the never-route list")
    .replace(/\bnever_move\b/g, "the never-move list")
    // X7: a bare 64-character digest in a sentence a person reads.
    // GOAL.md 3.9 shows the first 8 to the person and compares the full
    // one, and every other surface obeys that. `safety.py` now formats
    // step 5's own two digests; this catches any other server sentence
    // that quotes one whole. Nothing is ever compared against this: it is
    // the message, on its way to a person, and nothing else.
    .replace(/\b([0-9a-f]{64})\b/g, (m, d) => d.slice(0, 8) + "...")
    .replace(/\bbucket\(s\)/g, "categories")
    .replace(/\bbucket\b/g, "category").replace(/\bbuckets\b/g, "categories")
    .replace(/\bBucket\b/g, "Category").replace(/\bBuckets\b/g, "Categories");
}

/* The toast is never the only place something is said (§3.10), but a
   destructive edit gets its undo here, because that is where the hand already
   is. An action keeps the toast up for its full ten seconds. */
function toast(msg, kind, action) {
  const t = $("#toast"); t.innerHTML = "";
  // X7: the toast is a place a server sentence reaches a person, so it is
  // laundered like every other one. `words()` is idempotent on the page's own
  // labels, which is what the rest of these calls pass.
  t.appendChild(el("span", null, words(msg)));
  let life = 4200;
  if (action) {
    life = 10000;
    const b = el("button", "btn tiny toast-act", action.label);
    b.onclick = () => { t.classList.add("hidden"); action.fn(); };
    t.appendChild(b);
  }
  t.className = "toast " + (kind || ""); t.classList.remove("hidden");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), life);
}

/* ---------------------------------------------------- in-memory undo (D17)
   One snapshot deep, which is what "undo the click I just made" needs. The
   snapshot is of S.config only: nothing on disk has moved unless the edit
   persisted, and then the undo persists the snapshot back. */
let UNDO = null;
function snapshot() { return JSON.parse(JSON.stringify(S.config)); }
function undoable(label, before, persisted) {
  UNDO = { before: before, persisted: !!persisted, label: label };
  toast(label, "", { label: "undo", fn: doUndo });
  syncUndo();
}

/* The toast's undo was tab stop 107 of 108 and expired in ten seconds, so in
   practice it was mouse-only (U3). The same action is a button in the sticky
   save bar, which does not expire, and the panel says what happened. */
function syncUndo() {
  const b = $("#undo-last"); if (!b) return;
  b.disabled = !UNDO;
  b.title = UNDO ? "undo: " + UNDO.label
                 : "nothing to undo yet; this undoes the last edit you made here";
  b.setAttribute("aria-label", UNDO ? "undo last change: " + UNDO.label
                                    : "undo last change (nothing to undo)");
  const note = $("#undo-note");
  if (note) note.textContent = UNDO ? UNDO.label + ". It can still be undone." : "";
}
async function doUndo() {
  if (!UNDO) return;
  const u = UNDO; UNDO = null;
  syncUndo();
  S.config = u.before;
  if (u.persisted) {
    try {
      // `force`, and so past the two-tab check: an undo is a return to a
      // state this page has already had accepted, and the alternative is a
      // player who cannot take back the edit that lost the race.
      const r = await post("/api/config",
        { config: S.config, force: true, based_on: basedOn() });
      S.config = JSON.parse(JSON.stringify(r.config));
      S.saved = JSON.stringify(r.config);
      S.issues = r.issues || [];
      if (S.browse) await loadItems(true);
      if (S.save) await selectSave(S.save.path);
      renderSave();
    } catch (e) { toast(e.message, "bad"); return; }
  }
  S.plan = null;
  // The undo button disables itself once the undo is spent, so focus is handed
  // to its neighbour in the save bar rather than falling to the body (U2).
  focusAfter("revert");
  renderRules(); renderPlanTab();
  syncUndo();
  const note = $("#undo-note");
  if (note) note.textContent = "undone: " + u.label;
  toast("undone");
}

/* --------------------------------------------------- where a confirmation goes
   `insertAdjacentElement("afterend")` on the control is right for a control in
   a block, and wrong for one in a row: a `div` after a `td` is an anonymous
   cell 73 px wide, so the sources confirmation was 292 px tall inside a 63 px
   scroller with 239 px of it clipped away -- the owner's "confirm ui that you
   can't even read". The same insertion inside `.rule`, a nowrap flex row, laid
   the box over the rule's own controls.

   So the box goes *under the whole row*, full width, in the row's own flow: a
   `tr` with one spanning cell in a table, a block after the row everywhere
   else. It is still in place and still scrollable-to, and nothing about it is
   clipped. */
/* The pair every remove in Categories and Rules uses: the press is a removal,
   so the buttons say so. "yes, do it" and "no" stay the default for the rest
   (stop, revert, restore, put a kept configuration back). */
const REMOVE_YN = { yes: "yes, remove", no: "cancel" };
function confirmRowHost(anchor) {
  const tr = anchor.closest("tr");
  if (tr && tr.parentNode && (tr.parentNode.tagName === "TBODY"
                              || tr.parentNode.tagName === "TABLE")) {
    return { row: tr, cells: tr.cells.length };
  }
  // `.destrow` is deliberately not here: `#store-grid` is a `role="grid"`,
  // whose children have to be rows, and the chip confirmation measured
  // unclipped and full width where it already sits (372x71 in a chip that
  // wraps). The rule row is the other one that needed moving.
  const row = anchor.closest(".rule");
  return row ? { row: row, cells: 0 } : null;
}

/* ------------------------------------------------- inline confirmation (D17)
   Not `window.confirm`: a modal steals the page and says nothing about what is
   about to be lost. This sits under the row, names the damage, and puts the
   focus on the cancel button. `labels` renames the pair for a removal, where
   "yes, remove" and "cancel" say what the press does. */
function confirmInline(anchor, question, onYes, tick, whyOff, labels) {
  // one at a time in a section: the old rule looked at the control's own
  // parent, which the row host above is no longer inside.
  const scope = anchor.closest(".stage-panel, .savebar, .railfoot") || document;
  Array.from(scope.querySelectorAll(".confirm")).forEach(n => {
    const owner = n.closest("tr.confirm-row");
    (owner || n).remove();
  });
  const box = el("div", "confirm");
  box.setAttribute("role", "group");
  box.appendChild(el("span", "cq", question));
  const yes = el("button", "btn tiny confirm-yes",
                 (labels && labels.yes) || "yes, do it");
  const no = el("button", "btn tiny", (labels && labels.no) || "no");
  const host = confirmRowHost(anchor);
  const close = () => {
    const owner = box.closest("tr.confirm-row");
    (owner || box).remove();
    if (anchor.isConnected) anchor.focus();
  };
  no.onclick = close;
  // An optional second condition on the row: `tick` is a sentence the player
  // has to agree to before "yes, do it" turns on, and its answer is handed to
  // `onYes`. It lives on the confirmation rather than on the card because it
  // is per action -- a new press is a new box and a new unticked question.
  //
  // QA 3: `whyOff` is that sentence, under the row, the way `#apply-why`
  // carries it under Apply. A disabled destructive button with the reason
  // only in the operator's head is the same defect D2 was raised about.
  let cb = null;
  if (tick) {
    const lbl = el("label", "check");
    cb = el("input");
    cb.type = "checkbox";
    lbl.appendChild(cb);
    lbl.appendChild(el("span", null, tick));
    box.appendChild(lbl);
    yes.disabled = true;
    const why = whyOff ? el("p", "hint cwhy", whyOff) : null;
    if (why) box.appendChild(why);
    cb.onchange = () => {
      yes.disabled = !cb.checked;
      if (why) why.textContent = cb.checked ? "" : whyOff;
    };
  }
  yes.onclick = () => {
    const owner = box.closest("tr.confirm-row");
    (owner || box).remove();
    onYes(cb ? cb.checked : false);
  };
  box.appendChild(yes); box.appendChild(no);
  box.addEventListener("keydown", e => {
    if (e.key === "Escape") { e.stopPropagation(); close(); }
  });
  if (host && host.cells) {
    const tr = el("tr", "confirm-row");
    const td = el("td", "confirm-cell");
    td.colSpan = host.cells;
    td.appendChild(box);
    tr.appendChild(td);
    host.row.insertAdjacentElement("afterend", tr);
  } else if (host) {
    host.row.insertAdjacentElement("afterend", box);
  } else {
    anchor.insertAdjacentElement("afterend", box);
  }
  // the sentence first, then the focus: `focus()` scrolls the button it lands
  // on into view, which on a box taller than its scroller would put the
  // question off the top of it.
  if (box.scrollIntoView) box.scrollIntoView({ block: "nearest" });
  try { no.focus({ preventScroll: true }); } catch (e) { no.focus(); }
  return box;
}

/* ------------------------------------------------------ the `?` (§5 policy)
   One sentence, maximum, under a control; anything longer goes here. A `title`
   on a non-focusable element reaches neither a keyboard nor a touch user
   (A4), so this is a real button: the sentence is its `aria-label` and its
   `title`, and pressing it prints the sentence under the control it belongs
   to. Escape closes it and hands focus back.

   Nothing here paraphrases. Every sentence passed in is either the server's
   own, verbatim, or the page's own sentence moved out of a paragraph. */
function helpButton(sentence, what, key) {
  const b = el("button", "help", "?");
  b.type = "button";
  // U2: every panel holding one of these is rebuilt from the config after an
  // edit, so a `?` needs a focus key like every other control on the page.
  if (key) fk(b, key);
  b.title = sentence;
  b.setAttribute("aria-label", (what ? what + ": " : "") + sentence);
  b.setAttribute("aria-expanded", "false");
  let note = null;
  b.onclick = ev => {
    ev.preventDefault();
    ev.stopPropagation();
    if (note && note.isConnected) {
      note.remove();
      note = null;
      b.setAttribute("aria-expanded", "false");
      return;
    }
    note = el("div", "helpnote", sentence);
    note.setAttribute("role", "note");
    note.tabIndex = -1;
    note.addEventListener("keydown", e => {
      if (e.key === "Escape") {
        e.stopPropagation(); note.remove(); note = null;
        b.setAttribute("aria-expanded", "false"); b.focus();
      }
    });
    // Below the row, not inside it. Inserted after the button the note
    // landed inside the `data-help` span, and that span is a flex item of a
    // control row: it grew to the note's 78ch and took the row onto a second
    // line, which is what put the `?` on the line above the Verify file
    // button it belongs to in the Settings health fold. `.helpnote` is
    // `flex:1 1 100%`, so a full line is what it was written for -- but only
    // a *row* may hold it, because in one of the flex-column cards that
    // basis is a height and the note would take the whole card. Anything
    // else (a `?` written straight into a grid header cell, which has its
    // own absolute rule) keeps the old anchor.
    const host = b.parentElement;
    const row = host && host.parentElement;
    const inRow = row && row.classList && row.classList.contains("row");
    const anchor = (host && host.dataset && host.dataset.helpWired && inRow)
      ? row : b;
    anchor.insertAdjacentElement("afterend", note);
    b.setAttribute("aria-expanded", "true");
  };
  return b;
}

/* ------------------------------------------------------------------ icons
   Inline SVG, 1.6 stroke, `currentColor`, no asset and no font. Every one is
   either beside a text label or carries an `aria-label` from its caller. */
const ICON = {
  lock: "M4 11h16v10H4zM8 11V7a4 4 0 018 0v4",
  close: "M18 6L6 18M6 6l12 12",
  up: "M12 19V5M5 12l7-7 7 7",
  down: "M12 5v14M19 12l-7 7-7-7",
  grid: "M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z",
  gear: "M12 9a3 3 0 100 6 3 3 0 000-6zM19.4 15a1.7 1.7 0 00.3 1.9l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-2.9 1.2V21a2 2 0 11-4 0v-.1A1.7 1.7 0 007 19.4a1.7 1.7 0 00-1.9.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1A1.7 1.7 0 003 15a2 2 0 110-4 1.7 1.7 0 001.4-2.9l-.1-.1a2 2 0 112.8-2.8l.1.1A1.7 1.7 0 0011 3a2 2 0 114 0 1.7 1.7 0 002.9 1.4l.1-.1a2 2 0 112.8 2.8l-.1.1A1.7 1.7 0 0021 11a2 2 0 110 4z",
  dots: "M5 12h.01M12 12h.01M19 12h.01",
  tag: "M20.6 13.6l-7 7a2 2 0 01-2.8 0l-8.4-8.4V3h9.2l9 9a2 2 0 010 1.6zM7 7h.01",
  play: "M7 4l12 8-12 8z",
  shieldcheck: "M12 3l8 3v6c0 5-3.4 8.2-8 9-4.6-.8-8-4-8-9V6zM9 12l2 2 4-4",
  folder: "M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z",
  shield: "M12 3l8 3v6c0 5-3.4 8.2-8 9-4.6-.8-8-4-8-9V6z",
  plug: "M9 3v6M15 3v6M6 9h12v3a6 6 0 01-12 0zM12 18v3",
  warn: "M10.3 3.9L2.4 17.1A2 2 0 004.1 20h15.8a2 2 0 001.7-2.9L13.7 3.9a2 2 0 00-3.4 0z",
  tick: "M20 6L9 17l-5-5",
  power: "M18.4 6.6a9 9 0 11-12.8 0M12 2v10",
  chevron: "M9 6l6 6-6 6",
  search: "M11 19a8 8 0 100-16 8 8 0 000 16zM21 21l-4.3-4.3"
};
function icon(name, size) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("width", String(size || 16));
  s.setAttribute("height", String(size || 16));
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("fill", "none");
  s.setAttribute("stroke", "currentColor");
  s.setAttribute("stroke-width", "1.6");
  s.setAttribute("stroke-linecap", "round");
  s.setAttribute("stroke-linejoin", "round");
  s.setAttribute("aria-hidden", "true");
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
  p.setAttribute("d", ICON[name] || "");
  s.appendChild(p);
  return s;
}

/* The sentences the page's deleted paragraphs became. Keyed so `index.html`
   can name one with `data-help` and the markup stays free of prose. */
const HELP = {
  routing:
    "A category may name several containers: add a second one and it " +
    "overflows there when the first is full, in the order the numbered " +
    "badges show.",
  modes:
    "keep does not fetch. A keep of 800 while you carry 141 moves nothing; " +
    "it only stops those 141 leaving. To pull the missing 659 out of " +
    "storage, use stock. keep and cap are different ends: keep guards the " +
    "source an item leaves; cap guards the destination it arrives in.",
  verify:
    "The check decodes this save, writes it out again and compares the two " +
    "byte for byte. Nothing is saved by it. Worth running after a game " +
    "patch, after an apply refused at the round-trip step, or after another " +
    "tool has edited the save; an apply runs the same check itself and " +
    "refuses, so in normal use it is never needed.",
  zeros:
    "Shown at zero as well, because a plan that computed no refusals has to " +
    "look different from one that never computed them.",
  overrides:
    "The generated categories underneath are never edited: the override " +
    "lives in your configuration and can be undone."
};

/* The rail's icons, declared in `index.html` as `data-icon` so the markup
   stays free of 40-character path data. */
function wireIcons(root) {
  (root || document).querySelectorAll("[data-icon]").forEach(host => {
    if (host.dataset.iconWired) return;
    host.dataset.iconWired = "1";
    host.appendChild(icon(host.dataset.icon, 16));
  });
}

function wireHelp(root) {
  (root || document).querySelectorAll("[data-help]").forEach(host => {
    if (host.dataset.helpWired) return;
    const s = HELP[host.dataset.help];
    if (!s) return;
    host.dataset.helpWired = "1";
    host.appendChild(helpButton(s, host.dataset.helpWhat || "",
      "help-" + host.dataset.help));
  });
}

/* --------------------------------------------------- pulse, for a Fix button */
function pulse(nodes, ms) {
  const list = (Array.isArray(nodes) ? nodes : [nodes]).filter(Boolean);
  list.forEach(n => n.classList.add("pulse"));
  setTimeout(() => list.forEach(n => n.classList.remove("pulse")), ms || 2000);
}
/* `scroll-margin-top` is set from the measured sticky chrome (U21), so
   `block:"start"` lands under it rather than behind it. */
function reveal(node, block) {
  if (!node) return;
  node.scrollIntoView({ block: block || "center", behavior: "smooth" });
}

async function api(path, opts) {
  let r;
  try {
    r = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts));
  } catch (netErr) {
    // A rejected fetch is the server being gone, not an answer from it.
    const e = new Error(DEAD_SERVER);
    e.offline = true;
    throw e;
  }
  const body = await r.json().catch(() => ({ error: "the server did not answer with JSON" }));
  if (!r.ok) {
    const e = new Error(body.error || ("HTTP " + r.status));
    // The status is carried as well as the sentence, because one caller has to
    // tell "this build has no such route" (404) from "that route said no"
    // (409): the restore button is wired to a route two other lanes are still
    // landing, and a missing route is not an error to shout about.
    e.body = body; e.status = r.status;
    throw e;
  }
  return body;
}
const post = (p, o) => api(p, { method: "POST", body: JSON.stringify(o || {}) });

const cont = key => (S.save ? S.save.containers.find(c => c.key === key) : null);
const contLabel = key => { const c = cont(key); return c ? c.label : key; };
/* For prose. A key with no container in this save is named as such rather than
   printed bare, because a bare key reads like a container you have. */
const contName = key => {
  if (!key) return "";
  const c = cont(key);
  return c ? c.label : key + " (not in this save)";
};
/* The container's own key used to go after the name everywhere: in an option's
   text, where no markup is allowed, and in a muted span in six other places.
   The reason (U9) was real -- a player who renames a chest "Raw Resources"
   makes it read exactly like the category on the shelf beside it -- but the
   answer was applied to all 29 destinations whether they collided or not, and
   on a save with twelve ships it was the thing the name was truncated to make
   room for.

   So: the key is in the `title` and the accessible name always, and it comes
   back into the visible text only when this save really does have two
   containers reading the same name. It cannot simply go -- `config.json` names
   these keys and docs/RULES.md documents them. */
function labelCollides(label) {
  if (!S.save || !label) return false;
  let n = 0;
  for (const c of S.save.containers) if (c.label === label && ++n > 1) return true;
  return false;
}
const contOpt = c => (labelCollides(c.label) ? `${c.label} · ${c.key}` : c.label);
/* The same answer as an attribute, for a node that has a label of its own. */
function keyTitle(node, key, label) {
  if (!node) return node;
  const name = label === undefined ? contLabel(key) : label;
  node.title = `${name} (${key})`;
  return node;
}
/* A name that does not fit carries the whole of itself in a `title`.
   `text-overflow:ellipsis` is the right answer to a long name in a 190 px
   column, and it is only the right answer if the name is recoverable: at
   1280 the destinations list read `Trade Goods, Curiosities and ...` and
   `S-Class and Illegal Modules +...` with no tooltip at all, so the full
   name was unrecoverable without resizing the window.

   Measured after layout, not guessed from the string: the same name fits at
   1600 and does not at 1280, so the test is `scrollWidth > clientWidth`.
   Called again on every re-render, which is when a column can change width.
   A node that already has a title of its own keeps it -- the container name
   cell is titled `label (key)` by `keyTitle`, which carries the name too. */
function titleIfClipped(node, full) {
  if (!node) return node;
  node.dataset.full = full;
  clipSweepSoon();
  return node;
}
let CLIP_RAF = null;
function clipSweepSoon() {
  if (CLIP_RAF) return;
  CLIP_RAF = requestAnimationFrame(() => { CLIP_RAF = null; clipSweep(); });
}
/* One pass over every node that has asked, because a node cannot be measured
   when it is rendered: a section is built while its panel is `hidden`, where
   `clientWidth` is 0 and nothing is ever clipped. So the sweep also runs when
   a section is shown and when the window is resized, which are the two other
   moments the answer changes. */
function clipSweep() {
  $$("[data-full]").forEach(n => {
    if (!n.offsetParent) return;                 // hidden: ask again later
    if (n.scrollWidth > n.clientWidth + 1) {
      if (!n.title) n.title = n.dataset.full;
      n.dataset.clipped = "1";
    } else if (n.dataset.clipped) {
      n.removeAttribute("title");
      delete n.dataset.clipped;
    }
  });
}
window.addEventListener("resize", clipSweepSoon);
/* U36: the save writes an id with a leading caret; the item table's own id has
   none, and the caret is not part of anything a person types or reads. Shown
   without it, with the save's spelling in the title. */
const showId = id => String(id === null || id === undefined ? "" : id)
  .replace(/^\^/, "");
function idTitle(id) {
  return /^\^/.test(String(id || ""))
    ? `${showId(id)} -- the game's own id for this item. The save writes it ` +
      `as ${id}`
    : `${showId(id)} -- the game's own id for this item`;
}
/* The id on the name's own tooltip instead of printed beside it.

   "Why do we show things like MAINT_HOOVER in the app" was answered at its
   one instance and left standing everywhere else: `Carbon FUEL1`,
   `Faecium PLANT_POOP3`, `Life Support GelPRODFUEL2` glued together with no
   separator, 34 of them in one chest and 59 on one plan. The id is the
   identity the configuration stores and the Items table searches, so it does
   not go -- it goes where a container key went in 9d6, into the tooltip of
   the thing it names. The Items table keeps its visible `ID` column, which is
   labelled, carries a `?`, and is what that table is for. */
function withId(node, id) {
  if (node && id) node.title = idTitle(id);
  return node;
}
const contPhrase = key => {
  if (!key) return "";
  const c = cont(key);
  return c ? `${c.label} (${c.key})` : key + " (not in this save)";
};
/* `contPhrase` names a container for an attribute -- a `title` or an
   accessible name -- where the key is always welcome and costs no layout.
   Visible prose is the other rule, the one `contOpt` states: the name, and the
   key after it only where two containers in this save read the same name.
   QA pass 2 found the new chip-remove confirm asking "Stop routing Raw
   Resources to Raw Resources (chest1)?" -- the one sentence on the page where
   a player is asked to agree to something, printing an internal key. */
const contSay = key => {
  if (!key) return "";
  const c = cont(key);
  return c ? contOpt(c) : key + " (not in this save)";
};
/* Only where two containers in this save read the same name. Everywhere else
   the key is on the row's `title`; see `contOpt`. */
function keySpan(key) {
  if (!labelCollides(contLabel(key))) return null;
  const n = el("span", "ckey", key);
  n.title = "the container's own key in the save file";
  return n;
}
const sortableConts = () =>
  (S.save ? S.save.containers.filter(c => c.sortable) : []);
/* The categories a rule may name. The server says which: `system_meta` is
   currencies, reputation deltas and settlement stat tokens, none of which is
   ever in a container slot, so a destination for it is a row that can never
   fire and the validator refuses one. It stays on the list the *Items* table
   filters and displays by, because every item has a category and 134 of them
   have that one; it is only routing controls it is kept out of. */
const routableBuckets = list => (list || []).filter(b => b.routable !== false);
const bktLabel = k => (S.buckets.find(b => b.key === k) || {}).label ||
  (k === "unsorted" ? "Unsorted" : k);
const isCustomBucket = k =>
  ((S.config && S.config.custom_buckets) || []).some(b => b.key === k) ||
  (S.buckets.find(b => b.key === k) || {}).custom === true;

/* The highest per-stack cap this save shows for an item is the game's own
   number for it, so an amount field for a substance can carry a real `max`
   instead of a half-open range. Products cap per stack in the tens; you can
   hold any number of stacks, so their amount fields stay unbounded. */
function stackMaxOf(item) {
  if (!item || !S.save) return null;
  let m = null;
  S.save.containers.forEach(c => (c.items || []).forEach(i => {
    if ((i.stem === item || i.id === item) && i.max) m = Math.max(m || 0, i.max);
  }));
  return m && m >= 1000 ? m : null;
}

/* V4: a number the field itself rejects. `25012345678` in a field with
   `max="9999"` was kept, rendered in the generated sentence as if it were the
   rule, and refused at save time with "fix the errors above" pointing at three
   messages about something else. The bad text stays in the box, the model keeps
   the last value the validator would accept, and the complaint joins the issue
   list the Fix machinery already reads. */
const BAD_FIELDS = new Map();
function markBad(key, node, message, where) {
  BAD_FIELDS.set(key, { message: message, where: where || "", node: node });
  node.setAttribute("aria-invalid", "true");
  node.classList.add("bad-value");
}
function markGood(key, node) {
  BAD_FIELDS.delete(key);
  node.removeAttribute("aria-invalid");
  node.classList.remove("bad-value");
}
function badFieldIssues() {
  const out = [];
  BAD_FIELDS.forEach(v => out.push({
    level: "error", where: v.where, message: v.message
  }));
  return out;
}

/* Where a validator `where` string points on the page. Rebuilt on every render
   of the Configuration tab; read by the Fix buttons in #issues. */
const FIX = new Map();
function fixTarget(where, node, focus) {
  FIX.set(where, { node: node, focus: focus || null });
}

/* ================================================================ boot */
/* A second double-click on `NMS-Sorter.exe` finds the port taken, recognises
   the sorter that has it, opens this page and closes itself. That is the right
   behaviour and it happened in silence, so the player's second launch looked
   like a launch that did nothing. The copy that closed itself says so by
   opening the page with `?notice=second-launch`.

   The parameter is stripped from the address bar afterwards: it describes one
   arrival, and a reload -- or a bookmark -- must not repeat it. */
const NOTICES = {
  "second-launch": "another copy of the sorter was started and closed " +
    "itself; this is the running one"
};

function showNotice() {
  let q;
  try { q = new URLSearchParams(location.search); } catch (e) { return; }
  const key = q.get("notice");
  if (!key) return;
  q.delete("notice");
  const rest = q.toString();
  try {
    history.replaceState(null, "",
      location.pathname + (rest ? "?" + rest : "") + location.hash);
  } catch (e) { /* a browser that will not rewrite the address bar */ }
  const said = NOTICES[key];
  if (said) toast(said, "");
}

async function boot() {
  showNotice();
  // The `?` and icon placeholders `index.html` declares, filled once: they are
  // static markup, so nothing re-renders them.
  wireHelp();
  wireIcons();
  applyBoot(await api("/api/bootstrap"));
  pollGame();
  // Kept, because stopping the sorter has to stop the polling too: otherwise
  // the page goes on asking a server that has gone, and the operator is shown
  // "the sorter is not answering" on a page that just told them it stopped on
  // purpose. It is also what keeps the idle exit from firing under an open
  // tab -- every one of these is a request.
  GAME_POLL = setInterval(pollGame, 15000);
  // the backups card is on the Plan tab and is read far more often than it is
  // written, so it is filled at boot rather than on first sight
  loadBackups();
}

/* Re-read everything the page was started from. Called when the save folder
   changes on the Settings tab: the server drops its own save cache and its
   minted plans when that happens, so the page has to stop showing a plan for a
   file it may no longer be able to name. */
async function reboot() {
  applyBoot(await api("/api/bootstrap"));
  S.plan = null;
  renderAll();
}

function applyBoot(b) {
  S.boot = b; S.save = b.save; S.buckets = b.buckets;
  S.config = JSON.parse(JSON.stringify(b.config));
  S.saved = JSON.stringify(b.config);
  S.issues = b.issues || [];
  renderSavePicker();
  renderAll();
  syncUndo();
  // First run: load the newest save rather than leaving two headings over an
  // empty page (U1). The server selects nothing when its first candidate is a
  // save this build refuses, which is exactly the case that looked broken.
  if (!S.save && (b.saves || []).length) autoSelectNewest();
  else renderPickNote("");
}

/* ------------------------------------------------------- the save picker
   §3.5: an 850 px `<select>` whose option text truncated mid-word, above a
   line repeating the same facts. A button showing slot and filename -- short,
   so it cannot truncate -- and a listbox holding the long descriptor, where
   there is room for it.

   U1 is kept: with nothing selected the button says so, and the newest
   readable save is loaded for the player anyway, so the choice is rarely
   theirs to make. */
function saveRowText(sv) {
  const ts = sv.meta && sv.meta.timestamp;
  return [
    sv.file,
    `slot ${sv.pair}`,
    ts ? `${stamp(ts)} (${ago(ts)})` : `file ${stamp(sv.mtime)}`,
    sv.meta ? playtime(sv.meta.play_time) : "",
    sv.meta ? (sv.meta.summary || "(no summary)") : "no mf_ metadata"
  ].filter(Boolean).join("  ·  ");
}

function renderSavePicker() {
  const btn = $("#save-picker"), menu = $("#save-menu");
  if (!btn || !menu) return;
  btn.innerHTML = "";
  if (S.save) {
    const row = saveMeta(S.save.path);
    btn.appendChild(el("span", "slot", "Slot " + (row ? row.pair : "?")));
    btn.appendChild(el("b", null, S.save.file));
  } else {
    btn.appendChild(el("b", null, "Choose a save"));
  }
  btn.appendChild(icon("chevron", 14));
  btn.setAttribute("aria-label", S.save
    ? `Choose save. Currently ${S.save.file}`
    : "Choose save. Nothing is loaded");

  menu.innerHTML = "";
  ((S.boot && S.boot.saves) || []).forEach((sv, i) => {
    const o = el("div", "saveopt");
    o.setAttribute("role", "option");
    o.id = "saveopt-" + i;
    o.tabIndex = -1;
    const here = !!(S.save && samePath(sv.path, S.save.path));
    o.setAttribute("aria-selected", here ? "true" : "false");
    if (here) o.classList.add("on");
    // sizes_match === false: the mf_ metadata disagrees with the file on disk,
    // which is the state the game may refuse to load. Flagged on the row
    // before the save is selected, not after (lane C's new field).
    if (sv.sizes_match === false) {
      const g = icon("warn", 13);
      o.appendChild(g);
    }
    o.appendChild(el("span", null, saveRowText(sv)));
    withTz(o, sv.sizes_match === false
      ? "this save's metadata disagrees with its size on disk; the game may " +
        "not load it. Restore the last backup."
      : "");
    o.onclick = () => choosePickerSave(sv.path);
    menu.appendChild(o);
  });
  if (!menu.childElementCount) {
    menu.appendChild(el("div", "hint saveopt-empty",
      "there is no save in this folder"));
  }
}

async function choosePickerSave(path) {
  closeSavePicker();
  // `/api/bootstrap` hands out two spellings of the same path: the save list
  // keeps the folder exactly as it was given on the command line, while the
  // save view has been through `os.path.abspath`.
  if (S.save && samePath(path, S.save.path)) return;
  try {
    await selectSave(path);
    S.plan = null; renderAll();
  } catch (e) { toast(e.message, "bad"); }
}

let PICKER_OPEN = false;
function openSavePicker() {
  const btn = $("#save-picker"), menu = $("#save-menu");
  if (!btn || !menu) return;
  PICKER_OPEN = true;
  menu.classList.remove("hidden");
  btn.setAttribute("aria-expanded", "true");
  document.addEventListener("mousedown", onPickerMenuOutside, true);
  const on = menu.querySelector(".saveopt.on") || menu.firstElementChild;
  if (on && on.focus) on.focus();
}
function closeSavePicker(keepFocus) {
  const btn = $("#save-picker"), menu = $("#save-menu");
  if (!menu) return;
  PICKER_OPEN = false;
  menu.classList.add("hidden");
  if (btn) {
    btn.setAttribute("aria-expanded", "false");
    // A menu that closes onto `<body>` loses a keyboard user's place (U2).
    if (!keepFocus && btn.isConnected) btn.focus();
  }
  document.removeEventListener("mousedown", onPickerMenuOutside, true);
}
function onPickerMenuOutside(e) {
  const wrap = e.target.closest && e.target.closest(".pickwrap");
  if (!wrap) closeSavePicker(true);
}
function movePickerFocus(step) {
  const menu = $("#save-menu");
  const opts = Array.from(menu.querySelectorAll(".saveopt"));
  if (!opts.length) return;
  const at = opts.indexOf(document.activeElement);
  const to = opts[Math.max(0, Math.min(opts.length - 1, (at < 0 ? 0 : at) + step))];
  if (to) to.focus();
}

/* The save with the newest metadata stamp, falling back to the file's own
   mtime. Loaded once, at boot, and only when nothing is selected. */
/* V9: it picks for you, so it says so. §2.3's instinct is that picking
   silently is how the wrong account gets sorted. */
function renderPickNote(text) {
  const box = $("#save-warn"); if (!box) return;
  const old = box.querySelector(".picknote");
  if (old) old.remove();
  if (!text) return;
  const d = el("span", "pill picknote", text);
  d.title = "nothing was selected, so the save with the newest metadata stamp " +
    "was opened. Use Choose save to switch.";
  box.insertBefore(d, box.firstChild);
}

async function autoSelectNewest() {
  const rows = ((S.boot && S.boot.saves) || []).slice();
  if (!rows.length) return;
  const when = r => ((r.meta && r.meta.timestamp) || r.mtime || 0);
  rows.sort((a, b) => when(b) - when(a));
  for (const r of rows) {
    try {
      await selectSave(r.path);
      S.plan = null;
      renderAll();
      // §5 cut 1: "opened save10.hg, the newest of 9 saves in this folder.
      // Use Choose save above to switch." is the picker's own statement --
      // it reads `Slot 5 · save10.hg` -- and "use the control above" is the
      // control's job. The fact that it chose is a three-word pill.
      renderPickNote("opened for you");
      return;
    } catch (e) {
      // A save this build will not read is not an error to shout about here:
      // the next newest is tried, and the issue list carries the reason.
    }
  }
  renderAll();
}

/* Select a save *and* refresh the issue list.
   `POST /api/select` answers with the save view and nothing else, so the
   warnings on the Configuration tab -- "this destination is not a container in
   this save" and every other one computed against the container keys -- went
   on describing the save that was showing a moment ago. There is no route that
   validates without planning, so the answer is `GET /api/bootstrap`, which
   carries `issues` for whatever save is now selected. */
async function selectSave(path) {
  S.save = await post("/api/select", { file: path });
  // U4: a tick made about one save must not arm apply for another.
  clearConfirm("the save changed");
  try {
    const b = await api("/api/bootstrap");
    S.boot = b;
    S.issues = b.issues || [];
  } catch (e) { /* the save view is already in hand; keep the old issues */ }
  return S.save;
}

async function pollGame() {
  try { renderBanner(await api("/api/game")); } catch (e) { /* server gone */ }
}

/* =================================================== stopping the sorter ===
   `NMS-Sorter.exe` is built with no console, so a player who double-clicked it
   has no window to press ctrl-c in: before this, the only way to stop it was
   Task Manager, which is a bad last impression for a tool that promises to
   touch nothing. Two ways in -- the button on the Settings tab and the small
   `stop` in the top bar -- both through the one confirmation and the one
   route, `POST /api/quit`.

   The interval handle lives here because the stop has to cancel it: a page
   that goes on polling a server it just asked to stop reports the deliberate
   shutdown as "the sorter is not answering". */
let GAME_POLL = null;

const STOP_QUESTION = "Stop the sorter? The page will stop answering and you can close this tab. Nothing is written.";
const STOPPED_SENTENCE = "The sorter has stopped. Close this tab. Double-click NMS-Sorter.exe to start it again.";

function stopPolling() {
  if (GAME_POLL !== null) { clearInterval(GAME_POLL); GAME_POLL = null; }
}

/* The whole document, replaced by the one sentence that is still true. Every
   control on this page talks to a server that has gone, so leaving them there
   would be offering actions that can only fail. */
function pageStopped() {
  stopPolling();
  document.title = "the sorter has stopped";
  document.body.innerHTML = "";
  const box = el("div", "stopped");
  box.appendChild(el("p", null, STOPPED_SENTENCE));
  document.body.appendChild(box);
}

/* The refusal, which is the 409 while an apply or a restore is running. Shown
   in the panel *and* as a toast: the top-bar link can be pressed from any tab,
   and a sentence rendered on a hidden tab is a sentence nobody read. */
function stopRefused(sentence) {
  toast(sentence, "bad");
  const box = $("#stop-out");
  if (!box) return;
  box.innerHTML = "";
  const d = el("div", "issue warning");
  d.appendChild(el("b", "where", "Not stopped. "));
  d.appendChild(el("span", "imsg", words(sentence)));
  box.appendChild(d);
}

function stopSorter(anchor) {
  const out = $("#stop-out");
  if (out) out.innerHTML = "";
  confirmInline(anchor, STOP_QUESTION, async () => {
    try {
      await post("/api/quit");
    } catch (e) {
      // A rejected fetch means the server is already gone -- someone pressed
      // ctrl-c, or the idle exit fired -- which is the outcome that was asked
      // for, not an error to report.
      if (e.offline) { pageStopped(); return; }
      stopRefused(e.message);
      return;
    }
    pageStopped();
  });
}

/* W17: every time on this page is this machine's local time, and the zone it
   is local to is said in the title of whatever carries it. The manifest
   records UTC (`2026-09-15T00:23:32Z`) while the backup folder beside it is
   named in local time (`20260914-192329-save10`), and the card showed one of
   the two without saying which: five hours apart, in the operator's own run,
   with nothing to tell them it was the same moment. */
const TZ_NAME = (() => {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; }
  catch (e) { return ""; }
})();
const TZ_NOTE = "times on this page are this machine's local time" +
  (TZ_NAME ? ` (${TZ_NAME})` : "");
/* Sets the zone note as the title, keeping whatever the element already said
   in front of it. Returns the node, so it can be used inline. */
function withTz(node, extra) {
  if (!node) return node;
  node.title = (extra ? extra + " · " : "") + TZ_NOTE;
  return node;
}
/* A UTC ISO stamp -- what `safety.utc_now()` writes into a manifest -- in
   local time. The string is handed back unchanged when it is not a date this
   browser can parse: a manifest is a file on disk and this page did not write
   it. */
function localTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  return d.toLocaleString();
}

/* The save's own timestamp comes out of the mf_ metadata, which is what the
   game itself writes -- not the file mtime, which any tool touching the file
   would move. Both are shown when they disagree, because a mismatch is worth
   seeing. */
function stamp(unix) {
  if (!unix) return "";
  const d = new Date(unix * 1000);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const hm = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  if (sameDay) return hm;
  const ymd = d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return `${ymd} ${hm}`;
}

function ago(unix) {
  if (!unix) return "";
  const secs = Math.max(0, Math.floor(Date.now() / 1000) - unix);
  if (secs < 90) return `${secs}s ago`;
  const mins = Math.round(secs / 60);
  if (mins < 90) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 36) return `${hrs} h ago`;
  return `${Math.round(hrs / 24)} d ago`;
}

function playtime(secs) {
  if (!secs) return "";
  const h = Math.floor(secs / 3600);
  return h >= 1 ? `${h}h played` : `${Math.round(secs / 60)}m played`;
}

/* Two spellings of one Windows path: separators either way and case that does
   not matter. Only ever used to match a path this server handed out against
   another path the same server handed out. */
const samePath = (a, b) =>
  String(a === null || a === undefined ? "" : a).replace(/\\/g, "/").toLowerCase() ===
  String(b === null || b === undefined ? "" : b).replace(/\\/g, "/").toLowerCase();

function saveMeta(path) {
  const row = ((S.boot && S.boot.saves) || []).find(s => samePath(s.path, path));
  return row || null;
}

function renderAll() {
  renderHeader(); renderSave(); revalidate(); renderPlanTab();
}

/* The apply acknowledgement is cleared whenever the thing it was about
   changes: a new plan, a different save, a saved configuration, a completed
   apply (U4). The reason is said beside the box, so the tick does not simply
   vanish. */
function clearConfirm(reason) {
  const c = $("#confirm"); if (!c) return;
  const m = $("#menu-confirm");
  const was = c.checked || !!(m && m.checked);
  c.checked = false;
  // The main-menu confirmation is per apply, like the acknowledgement beside
  // it: a plan or a save that changed is a different write, and nobody has
  // said where they are in the game for that one.
  if (m) m.checked = false;
  const note = $("#confirm-note");
  if (note) {
    // QA 3: in the running state two ticks are shown and two were cleared,
    // and the note said one. The menu tick's own row is the thing that knows
    // whether it was on screen.
    const box = $("#menu-check");
    const two = !!(m && box && !box.classList.contains("hidden"));
    note.textContent = !(was && reason) ? ""
      : two
        ? `the ticks were cleared because ${reason}. Read the plan above and tick both again.`
        : `the tick was cleared because ${reason}. Read the plan above and tick it again.`;
  }
  renderGates();
}

/* ============================================================== header ===
   3.5: the identity of the open save was spread across four places -- an
   850 px select, a `#save-sub` line repeating the same facts, a `.picknote`
   and a `savewarn` row -- across 198 px of stacked bars. One 60 px status
   strip: the picker (short, so it cannot truncate), the summary as the
   identity line, because "Aboard Iigash Station Sigma" is how a player knows
   which save this is; the second line for playtime, stamp, version and stack
   limits; then the state pills and the totals.

   A pill for a state, a banner for a refusal (5, policy 6). */
function renderHeader() {
  renderSavePicker();
  renderSaveBanner();
  const sum = $("#save-summary");
  if (!S.save) {
    if (sum) sum.textContent = "No save loaded";
    $("#save-sub").textContent = "pick one with the picker on the left";
    $("#save-sub").removeAttribute("title");
    $("#save-stats").innerHTML = "";
    renderStatePills();
    renderRail();
    return;
  }
  const s = S.save;
  const m = saveMeta(s.path);
  const ts = m && m.meta && m.meta.timestamp;
  // 5 cut 2: `#save-sub` repeated the picker's own option text. The summary is
  // the identity line; this is everything else, printed once.
  if (sum) sum.textContent = (m && m.meta && m.meta.summary) || s.file;
  const bits = [];
  if (m && m.meta && m.meta.play_time) bits.push(playtime(m.meta.play_time));
  if (ts) bits.push("saved " + ago(ts));
  else if (m) bits.push("file modified " + stamp(m.mtime) + " (no mf_ metadata)");
  bits.push("save version " + s.version);
  bits.push("stack limits " + s.difficulty);
  const line = bits.join(" · ");
  $("#save-sub").textContent = line;
  // W17: with the zone those times are in, and the full stamp for the width
  // where even this line runs out of room.
  withTz($("#save-sub"), line + (ts
    ? " · saved " + new Date(ts * 1000).toLocaleString() : ""));
  const sortable = s.containers.filter(c => c.sortable).length;
  const held = s.containers.reduce((a, c) => a + c.used, 0);
  $("#save-stats").innerHTML = "";
  [[num(s.units), "units"], [String(sortable), "containers"],
   [num(held), "stacks"]].forEach(([v, k]) => {
    const d = el("div", "hstat"); d.appendChild(el("b", null, v));
    d.appendChild(el("span", null, k)); $("#save-stats").appendChild(d);
  });
  renderStatePills();
  renderRail();
}

/* The pills. Each is a fact about the file or the machine rather than an
   instruction, which is exactly what 5's policy 6 says should not be 43 px of
   full-width colour. The sentences they replace are unchanged: each is the
   pill's accessible name and its title. */
function renderStatePills() {
  const box = $("#save-warn");
  if (!box) return;
  const keepNote = box.querySelector(".picknote");
  box.innerHTML = "";
  if (keepNote) box.appendChild(keepNote);

  const g = S.game || {};
  // 5 cut 3: the banner's ok state, the game being closed, is this pill. The
  // banner keeps the running, unreadable and read-only states, verbatim.
  if (S.game) {
    let pill;
    if (g.running) {
      pill = el("span", "pill warn");
      pill.appendChild(icon("warn", 14));
      pill.appendChild(el("span", null, "No Man's Sky is running"));
      pill.title = "apply from the main menu: confirm it on the Apply card";
    } else if (g.known) {
      pill = el("span", "pill ok");
      pill.appendChild(icon("tick", 14));
      pill.appendChild(el("span", null, "Game closed"));
      // QA 3: "Safe to apply a plan." was a verdict on the operator's
      // machine that a process check does not supply -- a read-only run, an
      // expedition save and a missing plan are all still refusals with the
      // game closed. The pill states the fact it has and nothing else.
      pill.title = "The game is not running.";
    } else {
      pill = el("span", "pill warn");
      pill.appendChild(icon("warn", 14));
      pill.appendChild(el("span", null, "Process list unreadable"));
      pill.title = g.error || "the process list could not be read";
    }
    box.appendChild(pill);
  }
  if (S.boot && S.boot.read_only) {
    const p = el("span", "pill warn");
    p.appendChild(el("span", null, "Read-only"));
    p.title = "started with --read-only: plans run, nothing can be written";
    box.appendChild(p);
  }
  if (!S.save) return;
  const m = saveMeta(S.save.path);
  const ts = m && m.meta && m.meta.timestamp;
  // U6: the game writes the metadata timestamp; the filesystem writes mtime.
  // More than a minute apart means something other than the game touched the
  // file. That is a safety signal, so it is a pill of its own.
  //
  // W8: and the sorter's own apply is one of the things that moves mtime while
  // deliberately leaving the game's stamp alone, so after a first successful
  // sort this warning was permanent -- the tool accusing itself of tampering.
  // The server compares the bytes on disk against the hash the last backup
  // manifest recorded writing (`save.last_apply`); when they match, this says
  // so instead, in the same place and without the warning glyph.
  if (ts && m && m.mtime && Math.abs(m.mtime - ts) > 60) {
    const applied = S.save.last_apply;
    if (applied) {
      const said = `sorted by this tool at ${localTime(applied.created)}; the game has ` +
        `not written since.`;
      const p = el("span", "pill ok");
      p.appendChild(icon("tick", 14));
      p.appendChild(el("span", null, "sorted by this tool"));
      withTz(p, said);
      p.setAttribute("aria-label", said);
      box.appendChild(p);
    } else {
      const said = `this file was modified ${stamp(m.mtime)}, later than the game's own ` +
        `stamp: something other than the game has written to it.`;
      const p = el("span", "pill warn");
      p.appendChild(icon("warn", 14));
      p.appendChild(el("span", null, "edited outside the game"));
      withTz(p, said);
      p.setAttribute("aria-label", said);
      box.appendChild(p);
    }
  }
}

/* The save list's `sizes_match` is the one metadata fact worth a banner: the
   `mf_` records a size the file on disk does not have, and the game may refuse
   to load it. Lane C put the field on every row of the save list. */
function renderSaveBanner() {
  const box = $("#save-banner"); if (!box) return;
  box.innerHTML = "";
  if (!S.save) return;
  const m = saveMeta(S.save.path);
  if (!m || m.sizes_match !== false) return;
  const d = el("div", "banner danger");
  d.innerHTML = `<b>This save's metadata disagrees with its size.</b> The ` +
    `<code>mf_</code> file records a different size from the save on disk, so ` +
    `the game may not load it. Restore the last backup from the ` +
    `Plan section before playing on.`;
  box.appendChild(d);
}

/* The banner's text for the game state in `S.game`, painted on its own so
   the main-menu tick can repaint it: `renderBanner` also stores the state and
   re-renders the pills and the gates, and `renderGates` calls this one. QA 3:
   before the tick the banner tells you to quit to the main menu, and after it
   the same paragraph was still telling you to, above a card that had just
   recorded you saying you were there. */
function paintBanner() {
  const b = $("#game-banner"); if (!b) return;
  const g = S.game || {};
  const menu = !!($("#menu-confirm") && $("#menu-confirm").checked);
  b.classList.remove("hidden");
  if (g.running) {
    b.className = "banner warn";
    const pids = (g.pids || []).join(", ") || "?";
    b.innerHTML = menu
      ? `<b>No Man's Sky is running</b> (pid ${pids}). You confirmed you are ` +
        `on the main menu. Load the save after you apply.`
      : `<b>No Man's Sky is running</b> (pid ${pids}). ` +
        `Save, then <b>quit to the main menu</b> and stay there: apply from the menu ` +
        `and load the save afterwards. In a loaded save the game holds its own copy in ` +
        `memory and whichever writes last wins, so that is refused.`;
  } else if (!g.known) {
    b.className = "banner warn";
    b.innerHTML = `<b>Cannot read the process list</b>, so it cannot be told whether ` +
      `NMS.exe is running. Applying is blocked rather than guessed at. (${esc(g.error || "")})`;
  } else if (S.boot && S.boot.read_only) {
    b.className = "banner warn";
    b.innerHTML = `<b>Read-only mode.</b> Started with <code>--read-only</code>: ` +
      `plans run, nothing can be written.`;
  } else {
    // 5 cut 3: nothing that is merely true gets 43 px of full-width colour.
    // The game being closed is the `Game closed` pill in the header.
    b.className = "banner hidden";
    b.innerHTML = "";
  }
}

function renderBanner(g) {
  S.game = g;
  paintBanner();
  renderStatePills();
  renderGates();
}

/* ================================================================ save */
function renderSave() {
  // U1: two headings over empty space was the first thing a first-timer saw
  // when the server could not read its first candidate. The newest readable
  // save is loaded at boot; when not one of them can be read, this says so
  // rather than showing nothing.
  if (!S.save) {
    const box = $("#containers");
    box.innerHTML = "";
    const d = el("div", "guide");
    d.appendChild(el("h2", null, "No save is loaded"));
    d.appendChild(el("p", "lede",
      "Pick one with the picker at the top of the page. Nothing is written " +
      "until you say so."));
    const bad = (S.issues || []).filter(i => i.where === "save");
    bad.forEach(i => d.appendChild(el("p", "hint", words(i.message))));
    box.appendChild(d);
    const fold = $("#extractor-fold");
    if (fold) { fold.open = false; }
    return;
  }
  renderExtractors();
  renderContainers();
}

/* A substance's name for the page: the item table's, or its bare id.
   `showId` drops the save's leading caret. */
const subName = s => s.name || showId(s.id);

/* "Chromatic Metal 549 of 5,950; Oxygen 0 of 5,950; ..." -- every substance
   the cores hold, in the save's own slot order.

   The summary used to total Chromatic Metal alone, under a lede that said the
   cores were "mining Chromatic Metal": a core holds the metal *and* four
   gases, so a freighter banking 3,000 Oxygen was described by a figure that
   ignored it. `extractors.substances` is the server's per-substance total;
   an older server sends none, and then the one figure it does send is all
   there is to say. */
function substanceTotals(x) {
  const rows = (x && x.substances) || [];
  if (rows.length) {
    return rows.map(s => `${subName(s)} ${num(s.amount)} of ${num(s.max)}`)
      .join("; ");
  }
  return `Chromatic Metal ${num(x.chromatic)} of ${num(x.chromatic_cap)}`;
}

function renderExtractors() {
  const x = S.save.extractors, box = $("#extractors");
  box.innerHTML = "";
  // V6: with no cores, every figure below is a zero the save never stated. One
  // sentence instead of a panel of fabricated numbers.
  if (!x || !x.count) {
    $("#extractor-lede").textContent =
      "No Stellar Extractor Cores in this save, so there is nothing to show here.";
    const sum0 = $("#extractor-summary-label");
    if (sum0) sum0.textContent = "Stellar Extractor Cores (none in this save)";
    return;
  }
  // The count and the buffer size are facts about this save, so they are read
  // off it rather than written into the page. What the cores *hold* is a fact
  // about this save too: they mine gas as well as metal, and the lede used to
  // say "mining Chromatic Metal" over a card that showed only that one figure.
  // §5 cuts 6 and 7: the 34-word lede and the 22-word read-only paragraph are
  // the fold's summary line plus these six words. The buffer size is still a
  // fact read off the save, so it goes in the summary rather than in prose.
  $("#extractor-lede").textContent = "The sorter never writes to one.";
  const sum = el("div", "extractor-summary");
  const pct = x.chromatic_cap ? Math.round(100 * x.chromatic / x.chromatic_cap) : 0;
  sum.innerHTML =
    `<div class="big">${num(x.chromatic)} <small>/ ${num(x.chromatic_cap)}</small></div>` +
    `<div class="hint" style="margin-top:6px">Chromatic Metal across ` +
    `${esc(plural(x.count, "core", "cores"))} · ${pct}% of buffer</div>` +
    `<div class="fill" style="margin-top:10px"><i style="width:${pct}%;background:${bcol("refined_crafted")}"></i></div>` +
    // Every substance, not only the metal: the totals line is the one place
    // the whole of what the cores are holding is stated.
    `<div class="hint" id="extractor-totals" style="margin-top:9px">` +
    `${esc(substanceTotals(x))}</div>` +
    // Each core is now matched to the room it sits in, not counted against
    // the total: the red "Mismatch: 17 cores against 14 rooms" this line used
    // to show was the whole of the old refusal, and three stale buffers from
    // rebuilt rooms were enough to produce it.
    `<div class="hint" style="margin-top:9px">${x.consistent
      ? `${esc(plural(x.count, "core", "cores"))} matched to ` +
        `${x.rooms === null ? "an unknown number of" : x.rooms} Stellar ` +
        `Extractor Rooms in the freighter base.`
      : `<b style="color:var(--danger)">${esc(x.issue || "")}</b>`}</div>`;
  box.appendChild(sum);
  const grid = el("div", "core-grid");
  x.cores.forEach((c, i) => {
    // One row per substance the core holds, in the save's own slot order.
    // The card used to show the Chromatic Metal slot alone, so four of the
    // five things in a core were only in a `title`.
    const subs = (c.substances || []);
    const hottest = subs.reduce((a, t) =>
      Math.max(a, t.max ? (100 * t.amount / t.max) : 0), 0);
    const d = el("div", "core" + (hottest >= 90 ? " hot" : ""));
    // `core 3` is `extractor3` in a rule, which is why the number comes from
    // the server's key order rather than from the buffer index.
    d.appendChild(el("div", "n", `core ${i + 1}`));
    if (!subs.length) {
      d.appendChild(el("div", "hint", "no substance slots in this core"));
    }
    subs.forEach(t => {
      const p = t.max ? Math.round(100 * t.amount / t.max) : 0;
      const row = el("div", "core-sub");
      row.appendChild(el("span", "core-subname", subName(t)));
      const v = el("span", "core-subval");
      v.appendChild(el("b", null, num(t.amount)));
      v.appendChild(el("span", "of", ` of ${num(t.max)}`));
      row.appendChild(v);
      const bar = el("div", "fill");
      const i2 = el("i");
      i2.style.width = p + "%";
      i2.style.background = p >= 90 ? "var(--red)" : bcol("refined_crafted");
      bar.appendChild(i2);
      row.appendChild(bar);
      row.title = `${subName(t)} ${num(t.amount)} of ${num(t.max)} (${p}%)`;
      d.appendChild(row);
    });
    // Which room in the freighter. The buffer's own place in the save --
    // `RefinerBufferData[78]` -- is a save-key path: it overflowed its 97 px
    // cell to 114 px and wrapped, fourteen times, one line under the
    // substance names that had just been cleaned up. It answers exactly one
    // question ("which buffer is this, for an issue"), so it is the tooltip
    // and the room is the line.
    const where = el("div", "hint core-where",
      c.room ? `room ${c.room}` : "no room matched");
    where.title = (c.room ? `extractor room ${c.room}; ` : "") +
      `the save keeps this buffer at RefinerBufferData[${c.index}]`;
    d.appendChild(where);
    grid.appendChild(d);
  });
  box.appendChild(grid);
  // One line, under the cards, for the buffers that are deliberately not
  // there. A rebuilt extractor room leaves its old buffer in the save; the
  // game stops reading it, so draining it would duplicate items rather than
  // move them. Said out loud because three cards vanishing without a word is
  // how the operator would learn to distrust the count.
  if (x.stale) {
    box.appendChild(el("p", "hint", x.stale === 1
      ? "1 stale core buffer from a rebuilt room is ignored: the game no " +
        "longer reads it, so what it holds is not inventory and draining it " +
        "would duplicate it."
      : `${plural(x.stale, "stale core buffer", "stale core buffers")} from ` +
        `rebuilt rooms are ignored: the game no longer reads them, so what ` +
        `they hold is not inventory and draining one would duplicate it.`));
  }
  // U35: 17 cards of 0/350 and a red mismatch was the first thing a first-timer
  // met. The block stays folded unless it holds something and agrees with the
  // freighter's rooms.
  // §5 cut 6: the summary line is the lede. "14 cores · 0 of 4,900 Chromatic
  // Metal · read-only" says the count, what they hold and that nothing is
  // written to one, which is the whole of the 56 words it replaced.
  const label = $("#extractor-summary-label");
  if (label) {
    label.textContent = "Stellar Extractor Cores";
    const n = el("span", "hint", x.count
      ? `${plural(x.count, "core", "cores")} · ${num(x.chromatic)} of ` +
        `${num(x.chromatic_cap)} Chromatic Metal · read-only`
      : "none in this save");
    label.appendChild(document.createTextNode(" "));
    label.appendChild(n);
  }
  const fold = $("#extractor-fold");
  if (fold && !fold.dataset.touched) {
    const worth = x.count > 0 && x.consistent && x.chromatic > 0;
    fold.open = !!worth;
    fold.ontoggle = () => { fold.dataset.touched = "1"; };
  }
}

function renderContainers() {
  const box = $("#containers"); box.innerHTML = "";
  // The "hide technology grids" checkbox went with the grids themselves: the
  // Technology section, the multi-tool, the bait box and the food unit are no
  // longer offered to this page at all, because nothing is ever sorted into or
  // out of one. The model still knows them and the validator still refuses a
  // rule that names one.
  // 5 cut 5: "A dashed outline is empty; a container with no free cell says
  // FULL" was a legend for a state the chips now name outright, and the two
  // hide-X checkboxes were a filter nobody could read off the page. The chips
  // are the filter and the legend at once.
  //
  // The third checkbox, "Show unallocated", went the same way once the server
  // stopped sending a grid with no cells at all: unallocated and "can never
  // hold anything" were the same set, so the box switched between showing six
  // cards of nothing and showing none. `savePool()` is what it used to
  // filter, and `#save-hidden` says what is not on screen.
  renderSaveFilters();
  S.save.sections.forEach(sec => {
    let list = savePool().filter(c => c.section === sec);
    list = list.filter(c => saveFilterKeeps(c));
    if (!list.length) return;
    box.appendChild(el("div", "section-title", sec));
    const grid = el("div", "cards");
    groupByVessel(list).forEach(g => grid.appendChild(
      g.vessel ? vesselBox(g) : card(g.grids[0])));
    box.appendChild(grid);
  });
  if (!box.children.length) {
    box.appendChild(el("p", "hint", "nothing matches these filters"));
  }
}

/* The game gives the exosuit, each starship and the freighter three inventory
   grids and shows them as tabs on one vessel: General, Cargo, Technology. The
   save keeps three sibling nodes, and this page showed every one as a peer
   card -- so one ship was two entries, 24 cards for 12 vessels, 16 of them
   reading 0 / 0 cells.

   The pairing comes off the container view as `vessel` + `grid`, which is the
   model's answer. Splitting `_cargo` off a key here would be the same
   inference the model refuses to make, in the one place nothing could test it.

   List order is kept: a vessel appears where its first grid did. */
function groupByVessel(list) {
  const out = [], at = {};
  list.forEach(c => {
    if (!c.vessel) { out.push({ vessel: null, grids: [c] }); return; }
    if (at[c.vessel] === undefined) {
      at[c.vessel] = out.length;
      out.push({ vessel: c.vessel, label: c.vessel_label || c.label, grids: [] });
    }
    out[at[c.vessel]].grids.push(c);
  });
  return out;
}

/* One vessel: its name once, and a card per grid it actually has.

   A grid sub-heading only when there are two of them. Every starship and the
   exosuit carry an `Inventory_Cargo` node with no cells on all seven readable
   corpus saves -- Waypoint (4.0) merged the cargo grid into the main one and
   the game has no way to add a cell back -- so those are not sent to this
   page at all, and a card that is the vessel's only grid is the vessel: a
   sub-heading over it is a tab nobody can switch away from. The freighter is
   the one vessel where a second grid is still real, and there both are headed
   with the game's own word for them (`c.grid`, from `data/gridnames.json`). */
function vesselBox(g) {
  const n = el("div", "vessel");
  const h = el("div", "vhead");
  h.appendChild(el("span", "vname", g.label));
  if (g.grids.some(c => c.in_use)) {
    const u = el("span", "tag", "in use");
    u.title = "the ship or exocraft you are currently in";
    h.appendChild(u);
  }
  n.appendChild(h);
  const gr = el("div", "vgrids");
  const many = g.grids.length > 1;
  g.grids.forEach(c => gr.appendChild(
    card(c, { title: many ? (c.grid || c.label) : "", inVessel: true })));
  n.appendChild(gr);
  return n;
}

/* The five container filters. Each one is a state a card can be in, so the
   chip bar is the legend the 30-word lede used to be. */
let SAVE_FILTER = "all";
const SAVE_FILTERS = [
  ["all", "All", () => true],
  ["items", "With items", c => c.used > 0],
  ["empty", "Empty", c => c.used === 0],
  // `&& !c.drain_only`, the same line `card()` carries: 2b gave a core
  // `valid = used` so that its card could stop saying "6 / 0 cells", which
  // also made every one of the 14 cores match "valid and nothing free". The
  // chip read 14 on a save with no full container in it, and clicking it
  // showed fourteen cards that say "read-only buffer" and carry no full tag.
  // The game refills a core itself; it is never full in the sense this chip
  // means, which is "nothing more will fit in your storage".
  ["full", "Full", c => c.valid > 0 && c.free === 0 && !c.drain_only],
  ["stray", "Out of place", c => (c.strays || []).length > 0]
];
function saveFilterKeeps(c) {
  const f = SAVE_FILTERS.find(x => x[0] === SAVE_FILTER);
  return f ? f[2](c) : true;
}
/* Every container this page draws, and the only list the chips may count.

   `!c.slot_empty`: a starship slot the player does not own is not a
   container, so it is not `empty`, it is not `full`, and counting it in a
   chip is counting a thing that is not there. The server has already dropped
   the technology grids and every grid with no cells at all, so there is no
   third term any more -- which is what retired the "Show unallocated"
   checkbox. */
function savePool() {
  return S.save ? S.save.containers.filter(c => !c.slot_empty) : [];
}

function renderSaveFilters() {
  const box = $("#save-filters");
  if (!box || !S.save) return;
  const pool = savePool();
  box.innerHTML = "";
  SAVE_FILTERS.forEach(([key, label, test]) => {
    const n = pool.filter(test).length;
    const c = el("button", "chip" + (SAVE_FILTER === key ? " on" : ""));
    c.type = "button";
    c.setAttribute("aria-pressed", SAVE_FILTER === key ? "true" : "false");
    if (key === "stray") c.appendChild(icon("warn", 14));
    c.appendChild(el("span", null, label));
    c.appendChild(el("span", "count", num(n)));
    c.title = (SAVE_FILTER === key ? "showing " : "show only ") +
      label.toLowerCase() + " (" + num(n) + ")";
    fk(c, "savefilter-" + key);
    c.onclick = () => {
      SAVE_FILTER = key;
      focusAfter("savefilter-" + key);
      withFocus(() => renderContainers());
    };
    box.appendChild(c);
  });
  renderSaveHidden();
}

/* What is not on this section, once, as a sentence.

   It replaced the "Show unallocated" checkbox, whose whole population was
   grids with no cells: the game merged a starship's and the exosuit's cargo
   grid into the main one in Waypoint (4.0) and gives no way to put a cell
   back, so those grids can never hold anything and are not drawn at any
   setting. The switch had nothing left to switch, and 0 / 0 cells was never
   an answer to "what is in my save". The slots nobody owns were already
   disclosed here rather than drawn, and still are. */
function renderSaveHidden() {
  const n = $("#save-hidden");
  if (!n || !S.save) return;
  // The server's count, over every grid the save carries: an unowned slot
  // usually has no cells either, so it is not on the list this page was sent
  // and counting what is here would have said "1" on a save with nine.
  const slots = S.save.unowned_slots || 0;
  const bits = [];
  if (slots) bits.push(`${num(slots)} vessel ` +
    `${slots === 1 ? "slot" : "slots"} with nothing in them`);
  bits.push("the cargo grids the game merged into the main one");
  bits.push("technology grids");
  n.textContent = "Not shown: " + bits.join(", ") + ".";
  n.title = "Nothing can be put in any of these: a slot with no ship in it " +
    "has no cells, a merged cargo grid has none either, and a technology " +
    "grid is never sorted. A rule naming one stays valid and does nothing.";
}

function card(c, opts) {
  opts = opts || {};
  // `title: ""` means draw no name line: the box around it already carries
  // the name, and repeating "Starship 2" inside a box headed "Starship 2" is
  // what the sub-heading looked like once there was only ever one grid.
  const title = opts.title === undefined ? c.label : opts.title;
  const open = S.open.has(c.key);
  const pct = c.valid ? c.used / c.valid : 0;
  const full = c.valid > 0 && c.free === 0 && !c.drain_only;
  const n = el("div", "card" + (c.used === 0 ? " empty" : "") + (full ? " full" : "") +
    (open ? " open" : "") + (opts.inVessel ? " ingrid" : ""));
  // The head, the bar and the numbers are one button: a div with a click
  // handler is not reachable by keyboard and says nothing to a screen reader.
  // The body is outside it, because it holds a text field of its own.
  const toggle = el("button", "ctoggle");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", open ? "true" : "false");
  toggle.setAttribute("aria-label",
    `${c.label} (${c.key}): ${c.used} of ${c.valid} cells used. ` +
    `${open ? "Hide" : "Show"} contents`);
  toggle.title = `${c.label} (${c.key}) · ${c.width}x${c.height} · stacks ` +
    `as ${c.group}`;
  const head = el("div", "chead");
  const left = el("div");
  if (title) left.appendChild(el("div", "cname", title));
  const meta = el("div", "cmeta");
  // The key is on the card's own toggle, in the title and the accessible
  // name, and in the text only when this save has two containers reading the
  // same name. It was a peer of the name on all 65 cards.
  const ks = keySpan(c.key);
  if (ks) meta.appendChild(ks);
  meta.appendChild(el("span", null, `${c.width}×${c.height}`));
  // `MaintenanceObject` fourteen times on the Save section, `PersonalCargo`,
  // `ShipCargo`, `BaseCapsule`: the game's own stack-size group, in
  // CamelCase, under the names that had just been cleaned up -- the literal
  // thing the complaint named. It decides how big a stack of a given item may
  // be here, which is worth having, and it is one word of jargon, which is
  // not worth a line. On the card's own tooltip with the key.
  left.appendChild(meta);
  head.appendChild(left);
  const tags = el("div", "row");
  tags.style.gap = "5px";
  // "full" and "empty" are answers about cells, and a machine's buffer has
  // none the player can use.
  const plainTags = !c.drain_only;
  // An unnamed ship or exocraft has no name in the save at all: the game makes
  // one up from a seed at display time. Rather than make one up here, say the
  // one true thing there is to say about which vessel it is. Inside a vessel
  // box the pill is on the vessel's own head, where it is said once.
  if (c.in_use && !opts.inVessel) {
    const u = el("span", "tag", "in use");
    u.title = "the ship or exocraft you are currently in";
    tags.appendChild(u);
  }
  // No `unallocated` tag: a container with no cells is not sent to this page
  // any more, so the tag could only ever have been drawn on nothing.
  if (!plainTags) { /* nothing: see plainTags */ }
  else if (full) tags.appendChild(el("span", "tag full", "full"));
  else if (c.used === 0) tags.appendChild(el("span", "tag", "empty"));
  const sc = c.stray_counts || {};
  const strayN = (c.strays || []).length;
  if (strayN) {
    // Stuck strays are the ones worth a red tag: nothing in the current config
    // will ever move them. Ones merely waiting for a run are not a problem.
    const stuck = (sc.unrouted || 0) + (sc.not_source || 0);
    const t = el("span", "tag " + (stuck ? "stray" : ""), `${strayN} out of place`);
    t.title = stuck
      ? `${stuck} of these will never move under this configuration. Open the card for why.`
      : `all ${strayN} have somewhere to go; the next run moves them.`;
    tags.appendChild(t);
  }
  head.appendChild(tags);
  toggle.appendChild(head);

  // stacked composition, one segment per category. A machine's buffer is
  // measured in units, not stacks: its cells are all occupied all the time --
  // the game owns them -- so counting stacks drew a 100 % bar on an empty
  // core. How full the buffer is, is the one thing worth reading off it.
  const bar = el("div", "fill");
  const by = {};
  c.items.forEach(i => by[i.bucket] = (by[i.bucket] || 0) +
    (c.drain_only ? (i.amount || 0) : 1));
  const room = c.drain_only
    ? Math.max(1, c.items.reduce((t, i) => t + (i.max || 0), 0))
    : Math.max(c.valid, 1);
  Object.keys(by).sort().forEach(k => {
    const seg = el("i");
    seg.style.width = (100 * by[k] / room) + "%";
    seg.style.background = bcol(k);
    seg.title = c.drain_only
      ? `${bktLabel(k)} · ${num(by[k])} of ${num(room)} units`
      : `${bktLabel(k)} · ` + plural(by[k], "stack", "stacks");
    bar.appendChild(seg);
  });
  toggle.appendChild(bar);
  const fn = el("div", "fillnum");
  // A machine's buffer has no cells to fill: the game owns every slot in it
  // and refills them itself, so "5 / 5 cells 100%" would read as a container
  // that needs emptying. It holds substances, and that is what it says.
  if (c.drain_only) {
    fn.appendChild(el("span", null, plural(c.used, "substance", "substances")));
    fn.appendChild(el("span", null, "read-only buffer"));
  } else {
    fn.appendChild(el("span", null, `${c.used} / ${c.valid} cells`));
    fn.appendChild(el("span", null, c.valid ? Math.round(100 * pct) + "%" : "no cells"));
  }
  toggle.appendChild(fn);
  fk(toggle, `cont-${c.key}-toggle`);
  toggle.onclick = () => {
    S.open.has(c.key) ? S.open.delete(c.key) : S.open.add(c.key);
    focusAfter(`cont-${c.key}-toggle`);
    withFocus(() => renderContainers());
  };
  n.dataset.ckey = c.key;
  n.appendChild(toggle);

  if (open) {
    const body = el("div", "card-body");
    if ((c.own_buckets || []).length || strayN) {
      const remit = el("div", "remit");
      remit.appendChild(el("div", "hint", (c.own_buckets || []).length
        ? `Holds: ${c.own_buckets.join(", ")}${c.is_source ? "" : " · not a source, so nothing is ever taken out"}`
        : `No category is routed here${c.is_source ? "" : ", and it is not a source"}.`));
      body.appendChild(remit);
    }
    const strayBy = {};
    (c.strays || []).forEach(x => { strayBy[x.i] = x; });
    const groups = {};
    c.items.forEach(i => (groups[i.bucket] = groups[i.bucket] || []).push(i));
    const order = S.buckets.map(b => b.key).concat(["unsorted"]);
    order.filter(k => groups[k]).forEach(k => {
      const g = el("div", "bkt-group");
      const h = el("div", "bkt-name");
      const sw = el("span", "swatch"); sw.style.background = bcol(k);
      h.appendChild(sw);
      h.appendChild(el("span", null, bktLabel(k)));
      // \u00a75 cut 8: the same 9-word reason was printed on all six rows of a
      // group. It is stated once, here, with the count in front of it; every
      // row still carries it verbatim in the glyph's `aria-label`.
      const reasons = {};
      groups[k].forEach(i => {
        const sx = strayBy[i.i];
        if (sx) reasons[sx.reason] = (reasons[sx.reason] || 0) + 1;
      });
      Object.keys(reasons).forEach(r => {
        const d = el("span", "hint stray-why");
        d.style.marginLeft = "auto";
        d.textContent = `${reasons[r]} \u00d7 ${words(r)}`;
        h.appendChild(d);
      });
      g.appendChild(h);
      groups[k].sort((a, b) => (b.amount - a.amount)).forEach(i => {
        const sx = strayBy[i.i];
        const r = el("div", "item" + (i.known ? "" : " unknown") +
          (sx ? " stray-" + sx.why : ""));
        if (sx) r.title = sx.reason;
        const nm = el("div", "nm");
        // Out of place is carried by a glyph with a title, not by the left edge
        // alone: a coloured 2px rule is not a message (§3.10).
        if (sx) {
          const gl = el("span", "glyph", sx.why === "elsewhere" ? "↷" : "⚠");
          gl.title = sx.reason;
          gl.setAttribute("aria-label", "out of place: " + sx.reason);
          nm.appendChild(gl);
        }
        nm.appendChild(el("span", null, i.name || i.id));
        if (!i.known) nm.title = "not in the item table: left exactly where it is";
        else withId(nm, i.id);
        r.appendChild(nm);
        r.appendChild(el("div", "am", `${num(i.amount)}${i.max ? " / " + num(i.max) : ""}`));
        g.appendChild(r);
      });
      body.appendChild(g);
    });
    if (!c.items.length) body.appendChild(el("p", "hint", "empty"));
    // The save names chests but not exocraft or ships. Naming one is a config
    // edit, not a guess made from its grid size.
    const rn = el("div", "row gap");
    const inp = el("input");
    inp.id = "rename-" + c.key;
    inp.name = inp.id;
    inp.placeholder = "name this container"; inp.value =
      (S.config.labels && S.config.labels[c.key]) || "";
    inp.setAttribute("aria-label", "your name for " + c.label);
    // The ceiling `config.LABEL_MAX` refuses, applied where the name is typed:
    // a box that takes 300 characters and a validator that refuses them is one
    // control disagreeing with itself.
    inp.maxLength = 64;
    inp.style.flex = "1";
    fk(inp, `cont-${c.key}-name`);
    const b = el("button", "btn tiny", "rename");
    // W16: one of these per card, and every one of them said "rename". The
    // word stays -- the card is the context on screen -- and the accessible
    // name says which container it renames.
    b.setAttribute("aria-label", "rename " + c.label);
    b.title = "save your name for " + c.label;
    fk(b, `cont-${c.key}-rename`);
    b.onclick = async () => {
      S.config.labels = S.config.labels || {};
      if (inp.value.trim()) S.config.labels[c.key] = inp.value.trim();
      else delete S.config.labels[c.key];
      const r = await post("/api/config",
        { config: S.config, force: true, based_on: basedOn() });
      S.config = JSON.parse(JSON.stringify(r.config)); S.saved = JSON.stringify(r.config);
      await selectSave(S.save.path);
      renderSave(); revalidate();
    };
    rn.appendChild(inp); rn.appendChild(b);
    body.appendChild(rn);
    n.appendChild(body);
  }
  return n;
}

/* =============================================================== rules */
/* W7: the `updated` stamp of the configuration this page loaded, sent back as
   `based_on` on every save. The server compares it with the stamp in the file
   and refuses a write based on a configuration that is no longer there --
   another tab of this sorter, or another copy of it entirely. Two tabs used to
   overwrite each other in silence.

   Read from `S.config` rather than kept in a second variable, because every
   successful save replaces `S.config` with what the server stored, stamp
   included, and a copy would be one more thing to remember to move. */
const basedOn = () => (S.config && S.config.updated) || null;

/* Is this the two-tab refusal? `where` rather than the sentence: the sentence
   is going to be reworded and a page that matched on it would quietly stop
   offering the fix. */
const isStaleConfig = e =>
  e && e.status === 409 && (e.body || {}).where === "based_on";

/* The sentence, above the bar that holds the button that answers it, plus the
   edits kept where "undo last change" can bring them back. */
function configConflict(message) {
  const box = $("#config-conflict");
  if (box) {
    box.innerHTML = "";
    const d = el("div", "issue error");
    d.appendChild(el("b", "where", "Not saved. "));
    d.appendChild(el("span", "imsg", words(message)));
    box.appendChild(d);
    const p = el("p", "hint",
      "Your edits are still on this page and are kept as an undo: press " +
      "reload from disk to take the other copy's configuration, then undo " +
      "last change to get yours back and redo the part you meant.");
    box.appendChild(p);
  }
  // The snapshot is of the edits as they stand, so a reload-from-disk that
  // replaces them is undoable. `persisted: false`: nothing was written.
  undoable("your unsaved changes, kept from before the reload", snapshot(),
           false);
  const b = $("#config-reload");
  if (b) { reveal(b, "center"); pulse(b, 2500); b.focus(); }
}

function clearConfigConflict() {
  const box = $("#config-conflict");
  if (box) box.innerHTML = "";
}

function markDirty() {
  S.dirty = JSON.stringify(S.config) !== S.saved;
  $("#dirty-note").textContent = S.dirty ? "unsaved changes" : "";
}

function renderRules() {
  if (!S.config) return;
  // U2: every panel below is rebuilt from scratch, so focus is remembered here
  // once rather than in each of them.
  withFocus(() => {
    FIX.clear();
    renderRouting();
    renderSources();
    renderOptions();
    renderItemRules();
    renderSaveGates();     // the save's own caveats, above the config's
    renderIssues();        // after the panels, so the Fix targets are registered
    renderGuide();
    markDirty();
    syncUndo();
  });
}

/* `save_view.gates` (lane C) are the save's own reasons to stop: an expedition
   save, a `Version` this build was not verified on. They are facts about the
   file, not the configuration, so they sit above the validator's list on both
   tabs that act on a save. A `refuse` gate also disables apply, with its own
   sentence beside the button (renderGates). */
function saveGates() {
  return ((S.save && S.save.gates) || []).filter(g => g && g.message);
}
function gateRow(g) {
  const lvl = g.level === "refuse" ? "error" : "warning";
  const d = el("div", "issue " + lvl);
  const wh = el("b", "where",
    (lvl === "error" ? "Error in " : "Warning in ") +
    whereLabel(g.where || "save") + ". ");
  wh.title = "the save's own check this is about (" + (g.where || "save") + ")";
  d.appendChild(wh);
  d.appendChild(issueText(g.message + gateTail(g)));
  return d;
}
/* What the gate means for the apply about to be pressed, which the server's
   sentence cannot say on its own: the version gate's wording is the refusal,
   and the refusal only happens in strict mode. Off, which is the default, the
   sentence is a caveat and this says so rather than leaving the banner
   promising a refusal that will not come. */
function gateTail(g) {
  if (g.level === "refuse") return " Apply is refused on this save.";
  if (g.level === "warn" && g.where === "version") {
    return versionStrict()
      ? " Apply is refused on this save, because strict_version_check is on."
      : " Apply proceeds: strict_version_check is off, and the round-trip and " +
        "nothing-else-changed checks still run on this file.";
  }
  return "";
}
const versionStrict = () => !!(S.boot && S.boot.strict_version_check);
function renderSaveGates() {
  const box = $("#rules-gates"); if (!box) return;
  box.innerHTML = "";
  saveGates().forEach(g => box.appendChild(gateRow(g)));
}

/* ------------------------------------------------------------- P3-2 guided
   An empty routing table is the one state where the page cannot be read by
   poking at it: nothing on the shelf looks different, and a save with no
   category routed anywhere silently moves nothing. Three steps, each a link to
   the control it names, gone the moment one rule exists. */
function renderGuide() {
  const box = $("#guide"); box.innerHTML = "";
  const rules = S.config.bucket_rules || [];
  // With no save loaded there is nothing to compare the destinations against,
  // so only the empty-configuration case can be stated.
  const here = S.save ? rules.filter(r => cont(r.store)) : rules;
  if (rules.length && here.length) return;
  const card = el("div", "guide");
  card.appendChild(el("h2", null, rules.length
    ? "Nothing is routed to a container in this save"
    : "Nothing is routed yet"));
  card.appendChild(el("p", "lede", rules.length
    ? `This configuration routes ${rules.length} categor${rules.length === 1 ? "y" : "ies"}, ` +
      "but none of the containers named are in the save you have open, so a run " +
      "would move nothing. Three steps:"
    : "A category with no destination is never moved, so a dry run would come " +
      "back empty. Three steps:"));
  const ol = el("ol", "guide-steps");
  const step = (n, text, linkText, go) => {
    const li = el("li");
    li.appendChild(el("span", "gn", String(n)));
    li.appendChild(el("span", null, text + " "));
    const a = el("button", "linkbtn", linkText);
    a.onclick = go;
    li.appendChild(a);
    ol.appendChild(li);
  };
  step(1, "Confirm the save you are editing is the one you mean.",
    "check the save picker", () => {
      pulse($("#save-picker"), 2000); $("#save-picker").focus();
    });
  step(2, "Give each category a destination with its route to… picker.",
    "go to No destination", () => {
      showTab("categories");
      openShelf();
      const shelf = $("#bucket-shelf");
      reveal(shelf, "start");
      pulse(unroutedChips(), 2000);
      const sel = shelf && shelf.querySelector(".chip-route");
      if (sel) sel.focus();
    });
  step(3, "Run a dry run. Nothing is written by it.",
    "go to Plan", () => { showTab("plan"); $("#run-plan").focus(); });
  card.appendChild(ol);
  card.appendChild(el("p", "hint",
    "The layout this build ships with is the only preset. Anything else is the " +
    "one you build here, and it is saved to your own configuration file."));
  box.appendChild(card);
}

/* ------------------------------------------------- P3-4 warnings with a Fix
   A warning that names a control and does not offer it is a riddle. Each
   pattern below maps to the thing that would satisfy it; anything else falls
   through to "scroll to whatever `where` points at". */
function fixFor(issue) {
  const w = issue.where || "", m = issue.message || "";
  const at = w.match(/^(item_rules|bucket_rules|sources|options\.tidy|custom_buckets)\[(\d+)\]$/);
  const idx = at ? +at[2] : -1;

  if (w === "bucket_rules" && /no destination/.test(m)) {
    return ["show me the categories with no destination", () => {
      showTab(sectionFor(w));
      openShelf();
      // U11: the count in the sentence and the set highlighted come from one
      // predicate (`unroutedChips`), the scroll lands on the first of them
      // rather than centring a 1,500 px panel, and focus goes to the control
      // that answers the warning.
      const unrouted = unroutedChips();
      const first = unrouted[0] || $("#bucket-shelf").firstElementChild;
      reveal(first, "start");
      pulse(unrouted.length ? unrouted : Array.from($("#bucket-shelf").children), 4000);
      const sel = first && first.querySelector(".chip-route");
      if (sel) sel.focus();
    }];
  }
  if (at && at[1] === "bucket_rules" && /destination .* is not (a container|sortable)/.test(m)) {
    const t = FIX.get(w);
    return ["pick another destination", () => {
      showTab(sectionFor(w)); reveal(t && t.node); pulse(t && t.node, 2000);
      if (t && t.focus) t.focus();
    }];
  }
  if (at && at[1] === "sources" && /is not a container/.test(m)) {
    const t = FIX.get(w);
    return ["show the source", () => {
      showTab(sectionFor(w)); reveal(t && t.node); pulse(t && t.node, 2000);
      if (t && t.focus) t.focus();
    }];
  }
  if (at && at[1] === "item_rules" && /this rule does nothing/.test(m)) {
    const t = FIX.get(w + ".mode");
    return ["choose what it does", () => {
      showTab(sectionFor(w)); reveal(t && t.node); if (t && t.focus) t.focus();
    }];
  }
  if (at && at[1] === "item_rules" &&
      /(destination|stock container|keep_in) .* is not|cannot receive/.test(m)) {
    const t = FIX.get(w + ".dest") || FIX.get(w);
    return ["pick another container", () => {
      showTab(sectionFor(w)); reveal(t && t.node); if (t && t.focus) t.focus();
    }];
  }
  const generic = FIX.get(w);
  if (generic) {
    return ["show me", () => {
      showTab(sectionFor(w)); reveal(generic.node); pulse(generic.node, 2000);
      if (generic.focus) generic.focus();
    }];
  }
  return null;
}

/* One message, split at the colon when what follows it is a list of names, so
   no sentence on the page runs past 30 words (§3.10). */
function issueText(message) {
  const box = el("span", "imsg");
  const txt = words(message);
  const at = txt.lastIndexOf(": ");
  const tail = at > 0 ? txt.slice(at + 2) : "";
  if (at > 0 && tail.split(", ").length > 1 && !/[.!?]/.test(tail)) {
    box.appendChild(el("span", null, txt.slice(0, at) + ":"));
    box.appendChild(el("div", "ilist", tail));
  } else {
    box.textContent = txt;
  }
  return box;
}

/* E1: every issue row used to lead with a monospace `span.where` --
   `category routing 11`, `plan` -- which is a JSON path rendered into English
   and reads as an error code. The location is kept, demoted into the sentence:
   "**Warning in category routing 11.**" in bold, then the server's sentence
   verbatim. */
function whereLead(level, where) {
  const wh = el("b", "where",
    (level === "error" ? "Error in " : "Warning in ") + whereLabel(where) + ". ");
  wh.title = "the place in your configuration file this is about: " +
    (where || "the whole file");
  return wh;
}

function renderIssues() {
  const box = $("#issues"); box.innerHTML = "";
  // F4: Configuration is two sections now, so a warning is rendered in the one
  // whose controls answer it. A per-item rule also states its own warning
  // under its own row, which is where its Fix button lands.
  const rbox = $("#rules-issues");
  if (rbox) {
    Array.from(rbox.querySelectorAll(".issue")).forEach(n => n.remove());
  }
  const all = badFieldIssues().concat(S.issues || []);
  all.forEach(i => {
    const host = (sectionFor(i.where) === "rules" && rbox) ? rbox : box;
    const d = el("div", "issue " + i.level);
    // U10: the label of the page's most prominent warning was a JSON key. The
    // page's own words go here; the raw path stays in the title.
    d.appendChild(whereLead(i.level, i.where));
    d.appendChild(issueText(i.message));
    const f = fixFor(i);
    if (f) {
      const b = el("button", "btn tiny fixbtn", "Fix");
      fk(b, "fix-" + (i.where || "all"));
      b.title = f[0];
      b.setAttribute("aria-label", "Fix: " + f[0]);
      b.onclick = f[1];
      d.appendChild(b);
    }
    host.appendChild(d);
  });
}

/* 3.1: a per-container assignment table.

   Before: a 300 px sticky shelf of 110 px chips, each with a full-width
   `route to...` select, plus a grid of container tiles that were drop targets,
   plus a `move to...` select and an `x` on every chip already placed, plus a
   numbered badge for overflow order, plus drag-and-drop between the two. Two
   representations of one relation, four controls for one verb, and one
   drag-only path (A2).

   After: one row per container that receives something, stating the relation
   once and in the direction the player thinks in -- "what goes in this box".
   Capacity and drain state are printed in the row rather than hung off a
   `title` on a `<span>` (A4), because the drain state is the thing `GUIDE.md`
   calls the most common cause of a short dry run and this is the moment it is
   being decided.

   Drag-and-drop stays as an accelerator. It is no longer the only path to
   anything: add is the row's select, remove one link is the chip's `x`, and
   remove from everywhere is the select at the foot of the No destination
   panel. */
function bucketChip(b, storeKey, ord) {
  const c = el("span", "chip" + (isCustomBucket(b.key) ? " custom" : ""));
  c.draggable = true;
  c.dataset.bucket = b.key;
  const sw = el("span", "sw"); sw.style.background = bcol(b.key);
  c.appendChild(sw);
  const nm = el("span", "chip-name", b.label);
  if (isCustomBucket(b.key)) nm.title = "a category you added";
  // On the No destination shelf a chip shares its line with the picker, so a
  // long category name clips: `Expedition and Se...`. Nothing else on that
  // row names it.
  else titleIfClipped(nm, b.label);
  c.appendChild(nm);
  const cnt = el("span", "count", num(b.items));
  cnt.title = plural(b.items, "item", "items") + " in this category";
  c.appendChild(cnt);
  if (b.note) c.title = b.note;
  if (storeKey) {
    // Overflow order is inline: a category naming two containers carries a
    // 1/2 ordinal in each row it appears in. No badge when the chain has one
    // leg -- the ordinal only means something when there is something to
    // order.
    if (ord) {
      const o = el("span", "ord", String(ord));
      o.title = "overflow order: filled " +
        (ord === 1 ? "first" : "after the one before");
      o.setAttribute("aria-label", "overflow order " + ord + ": filled " +
        (ord === 1 ? "first" : "after the one before"));
      c.appendChild(o);
    }
    const x = el("button", "x");
    x.type = "button";
    x.appendChild(icon("close", 13));
    x.title = `stop routing ${b.label} to ${contPhrase(storeKey)}`;
    x.setAttribute("aria-label", x.title);
    fk(x, `chip-${b.key}-${storeKey}-remove`);
    // U14: unrouting was three lines with no guard: no confirm, no toast, no
    // undo. It now asks on the chip and leaves an undo behind, like every
    // other destructive click on this page.
    x.onclick = ev => {
      ev.stopPropagation();
      confirmInline(x,
        `Stop routing ${b.label} to ${contSay(storeKey)}?`,
        () => {
          const before = snapshot();
          S.config.bucket_rules = S.config.bucket_rules.filter(
            r => !(r.bucket === b.key && r.store === storeKey));
          // X2 (review 3): this named the "route to..." select the category
          // gets when it has no destinations left, and that control is only
          // rendered in that one case -- so removing one link of several, or
          // emptying a container's row, looked for something that was not
          // there and focus fell to `<body>`. Three outcomes, three
          // successors, each chosen from the configuration *after* the
          // mutation so the key is one that will exist.
          const left = S.config.bucket_rules;
          const rowLives = left.some(r => r.store === storeKey);
          const stillRouted = left.filter(r => r.bucket === b.key);
          // 6c put the select behind a `+`, so `add-to-<key>` is only in the
          // DOM while that select is open and this key was stale from the
          // moment it shipped -- the same defect X2 above fixed once, back
          // again one commit later. The `+` is what the row always has.
          focusAfter(rowLives
            ? `add-to-${storeKey}-open`
            : (stillRouted.length
                ? `chip-${b.key}-${stillRouted[0].store}-remove`
                : `unrouted-${b.key}-route`));
          revalidate();
          undoable(`${b.label} no longer goes to ${contLabel(storeKey)}`, before);
        }, null, null, REMOVE_YN);
    };
    c.appendChild(x);
  }
  c.dataset.chip = b.key;
  c.ondragstart = e => {
    e.dataTransfer.setData("text/bucket", b.key);
    // Where the drag started decides what the drop means: off the unrouted
    // panel adds a container to the chain, off a row moves that one.
    e.dataTransfer.setData("text/from", storeKey || "");
    e.dataTransfer.effectAllowed = storeKey ? "move" : "copy";
    c.classList.add("dragging");
  };
  c.ondragend = () => c.classList.remove("dragging");
  return c;
}

/* B1: `route to...` and `move to...` were the option text of an unlabelled
   `<select>` -- two verbs for one operation, and neither of them a label. The
   control is a cell in a table whose column header says "Categories it
   receives", so the head option is a command: `+ add category`. */
function routeSelect(b, from) {
  const sel = el("select", "isel chip-route");
  sel.id = `route-${b.key}-${from || "shelf"}`;
  sel.name = sel.id;
  fk(sel, `chip-${b.key}-${from || "shelf"}-route`);
  const head = el("option");
  head.value = "";
  head.textContent = from ? "move to…" : "route to…";
  sel.appendChild(head);
  const rules = S.config.bucket_rules || [];
  sortableConts().forEach(c => {
    if (c.key === from) return;
    if (rules.some(r => r.bucket === b.key && r.store === c.key)) return;
    const o = el("option"); o.value = c.key; o.textContent = contOpt(c);
    sel.appendChild(o);
  });
  sel.disabled = sel.options.length < 2;
  sel.title = from
    ? `move ${b.label} from ${contPhrase(from)} to another container`
    : `route ${b.label} to a container; choose again to add an overflow container`;
  sel.setAttribute("aria-label", sel.title);
  // B8: the project fixed this for #add-rule, #add-all-extractors and #apply
  // -- a sentence beside the button, because a title on a disabled control
  // reaches nobody -- and this one select was left switching itself off in
  // silence. Its own reason, in words, beside it.
  if (sel.disabled) {
    const why = el("span", "hint", from
      ? `no other container left to move ${b.label} to`
      : `every container in this save already receives ${b.label}`);
    why.id = `route-${b.key}-${from || "shelf"}-why`;
    sel.setAttribute("aria-describedby", why.id);
    sel.setAttribute("aria-label", sel.title + ". " + why.textContent);
    sel._why = why;
    sel.dataset.why = why.id;
  }
  sel.onchange = () => {
    const key = sel.value;
    if (!key) return;
    const kept = from
      ? (S.config.bucket_rules || []).filter(r => !(r.bucket === b.key && r.store === from))
      : (S.config.bucket_rules || []);
    S.config.bucket_rules = kept.concat([{ bucket: b.key, store: key }]);
    // The `+` on the destination row this category just landed on -- not the
    // select behind it, which 6c made conditional on that `+` having been
    // pressed. Routing a category from the No destination card is the primary
    // keyboard path and the card's whole reason to exist, and it was dropping
    // focus to `<body>` on every use.
    focusAfter(`add-to-${key}-open`);
    revalidate();
  };
  return sel;
}

/* The `+ add category` select in a container's row. */
function addCategorySelect(key) {
  const c = cont(key);
  const name = c ? c.label : key;
  const sel = el("select", "addsel");
  sel.id = "add-to-" + key; sel.name = sel.id;
  fk(sel, "add-to-" + key);
  const head = el("option");
  head.value = ""; head.textContent = "+ add category";
  sel.appendChild(head);
  const rules = S.config.bucket_rules || [];
  routableBuckets(S.buckets).forEach(b => {
    if (rules.some(r => r.bucket === b.key && r.store === key)) return;
    const o = el("option"); o.value = b.key; o.textContent = b.label;
    sel.appendChild(o);
  });
  sel.setAttribute("aria-label", "add a category to " + name);
  sel.disabled = sel.options.length < 2;
  if (sel.disabled) {
    // B8 again: this container has nothing left to receive, and it says so.
    const why = el("span", "hint", name + " already receives every category");
    why.id = "add-to-" + key + "-why";
    sel.setAttribute("aria-describedby", why.id);
    sel.setAttribute("aria-label",
      "add a category to " + name + ". " + why.textContent);
    sel._why = why;
  }
  sel.onchange = () => {
    if (!sel.value) return;
    S.config.bucket_rules = (S.config.bucket_rules || [])
      .concat([{ bucket: sel.value, store: key }]);
    focusAfter("add-to-" + key + "-open");
    revalidate();
  };
  return sel;
}

/* 6c: the select above was on every row, all the time, at 26 px -- including
   the eight rows of eleven with a single chip on them. A 22 px button that
   swaps the select in when it is pressed: the select, its `aria-label` and its
   disabled-with-a-reason branch are all the ones above, unchanged. */
function addCategoryControl(key) {
  const sel = addCategorySelect(key);
  const wrap = el("span", "addwrap");
  const b = el("button", "btn tiny addbtn");
  b.type = "button";
  b.appendChild(el("span", null, "+"));
  // Not the select's own name: this button does not add a category, it
  // reveals the control that does, and a screen reader was told otherwise.
  const c = cont(key);
  const label = "choose a category to add to " + (c ? c.label : key);
  b.setAttribute("aria-label", label);
  b.setAttribute("aria-expanded", "false");
  b.title = label;
  b.disabled = sel.disabled;
  fk(b, "add-to-" + key + "-open");
  if (sel._why) b.setAttribute("aria-describedby", sel._why.id);
  b.onclick = () => {
    b.setAttribute("aria-expanded", "true");
    sel.tabIndex = -1;            // the row is the grid's tab stop
    wrap.replaceChild(sel, b);
    sel.focus();
    // And open it, so adding a category from a row is one interaction again
    // rather than two. `showPicker` is a user-gesture call and this is one;
    // where the browser has not got it, the select is focused and open to
    // the keyboard anyway.
    try { sel.showPicker(); } catch (e) { /* focused is enough */ }
  };
  wrap.appendChild(b);
  wrap._why = sel._why;
  return wrap;
}

/* The No-destination shelf is behind a fold (the column cannot hold both
   lists), so anything that sends the player to a chip in it opens the fold
   first: a `details` that is closed has no layout and nothing in it can take
   the focus. */
function openShelf() {
  const f = $("#nd-fold");
  if (f && !f.open) f.open = true;
  return f;
}
function dropzone(node, onDrop) {
  node.ondragover = e => {
    if (!e.dataTransfer.types.includes("text/bucket")) return;
    e.preventDefault(); node.classList.add("over");
  };
  node.ondragleave = () => node.classList.remove("over");
  node.ondrop = e => {
    node.classList.remove("over");
    const b = e.dataTransfer.getData("text/bucket");
    if (!b) return;
    e.preventDefault();
    onDrop(b, e.dataTransfer.getData("text/from") || "");
  };
}

/* Which categories have no destination, by the same test the validator's
   count uses: routed nowhere, and not in `never_buckets` (U11). */
function unroutedBuckets() {
  const rules = (S.config && S.config.bucket_rules) || [];
  const never = (S.config && S.config.never_buckets) || [];
  const routed = new Set(rules.map(r => r.bucket));
  if (routed.has("*")) return [];
  return routableBuckets(S.buckets)
    .filter(b => !routed.has(b.key) && !never.includes(b.key));
}
/* The rows the Fix button highlights. Kept as a DOM query, because that is
   what `pulse()` and `scrollIntoView` take. */
function unroutedChips() {
  const shelf = $("#bucket-shelf");
  if (!shelf) return [];
  return Array.from(shelf.querySelectorAll("[data-chip]"));
}

/* The destinations grid's one tab stop, by container key. */
let DEST_ROVE = null;

/* Put `tabIndex` where `DEST_ROVE` says, without re-rendering anything. */
function roveDest() {
  $$("#store-grid .destrow").forEach(r => {
    r.tabIndex = r.dataset.store === DEST_ROVE ? 0 : -1;
  });
}

/* The grid's keys on one destinations row: Left and Right walk the row's own
   controls, Up and Down walk the rows, Home and End jump to the ends. The
   same handler shape as the Items grid, and for the same reason -- a row of
   chips, a remove button each, a `+` and sometimes a select is up to five tab
   stops, and eleven of those rows is 24. */
function destGridKeys(tr) {
  tr.addEventListener("keydown", e => {
    const rows = $$("#store-grid .destrow");
    const inner = Array.from(tr.querySelectorAll("button,select,input"))
      .filter(n => !n.disabled && n.offsetParent !== null);
    const onRow = document.activeElement === tr;
    if (e.key === "ArrowRight") {
      e.preventDefault();
      const at = inner.indexOf(document.activeElement);
      const next = inner[at + 1] || inner[inner.length - 1];
      if (next) next.focus();
      return;
    }
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      const at = inner.indexOf(document.activeElement);
      if (at <= 0) tr.focus(); else inner[at - 1].focus();
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (!onRow) return;                   // a select owns its own arrows
      e.preventDefault();
      const at = rows.indexOf(tr);
      const to = rows[at + (e.key === "ArrowDown" ? 1 : -1)];
      if (to) to.focus();
      return;
    }
    if (e.key === "Home" || e.key === "End") {
      // A caret in a text field owns these two keys and always will.
      const a = document.activeElement;
      if (a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA") &&
          a.type !== "checkbox" && a.type !== "radio") return;
      e.preventDefault();
      // QA 2, item 6: with focus on a cell these did nothing at all, so the
      // scroller moved under a focus ring that stayed where it was. ARIA's
      // grid pattern is the answer and it is the one the rest of the page
      // already implements for rows: unmodified, Home and End are the ends of
      // *this row*; with Control, the ends of the grid. With focus on the row
      // itself there is no cell to travel to, so both forms are the grid --
      // which is what this handler already did, unchanged.
      if (!onRow && !e.ctrlKey && inner.length) {
        (e.key === "Home" ? inner[0] : inner[inner.length - 1]).focus();
        return;
      }
      const to = e.key === "Home" ? rows[0] : rows[rows.length - 1];
      if (to) to.focus();
    }
  });
}

function renderRouting() {
  const rules = S.config.bucket_rules || [];

  // ---- the destinations table -------------------------------------------
  const host = $("#store-grid"); host.innerHTML = "";
  const keys = [];
  rules.forEach(r => { if (r.store && !keys.includes(r.store)) keys.push(r.store); });
  // A container with nothing routed to it is not a row: the relation is
  // stated once, and an empty row states nothing. Routing to a fresh
  // container is the No destination panel's select, which lists them all.
  // 6d: blocks in an auto-filling grid rather than a `<table>`, so a wide
  // window is two columns and all eleven rows fit with no scrolling at all.
  // The roles are kept: "Container" and "Categories it receives" are what the
  // two halves of a row mean, and a screen reader loses that if the table
  // element goes and nothing replaces it.
  //
  // `grid`, not `table`: the rows carry controls and now carry the arrow keys
  // that walk them, which is exactly the difference between the two roles. It
  // is the same pattern the Items table has had since P6 -- one tab stop for
  // the whole list, arrows between cells and rows, Home and End to the ends
  // -- and eleven rows used to cost 24 tab stops to cross.
  host.setAttribute("role", "grid");
  host.setAttribute("aria-label", "Containers and the categories they receive");
  host.setAttribute("aria-rowcount", String(keys.length + 1));
  if (keys.length) {
    const hd = el("div", "desthead");
    hd.setAttribute("role", "row");
    ["Container", "Categories it receives"].forEach(t => {
      const h = el("span", "dh", t);
      h.setAttribute("role", "columnheader");
      hd.appendChild(h);
    });
    host.appendChild(hd);
  }
  // Which row holds the grid's one tab stop. Kept across a re-render by
  // container key rather than by index, because a row can appear or go.
  if (!keys.includes(DEST_ROVE)) DEST_ROVE = keys[0] || null;
  keys.forEach((key, ri) => {
    const c = cont(key);
    const tr = el("div", "destrow");
    tr.setAttribute("role", "row");
    tr.setAttribute("aria-rowindex", String(ri + 2));   // 1 is the header
    tr.tabIndex = key === DEST_ROVE ? 0 : -1;
    tr.dataset.store = key;
    fk(tr, "destrow-" + key);
    tr.addEventListener("focus", () => { DEST_ROVE = key; roveDest(); });
    destGridKeys(tr);
    const left = el("div", "destcell");
    left.setAttribute("role", "gridcell");
    const nm = el("div", "cname");
    const cn = el("span", "cn", c ? c.label : key);
    // `Trade Goods, Curiosities and ...` and `S-Class and Illegal Modules
    // +...` at 1280: the cell around it is titled `name (key)`, which does
    // carry the name, but the name on its own is what a player hovering the
    // name is asking for.
    titleIfClipped(cn, c ? c.label : key);
    nm.appendChild(cn);
    const rk = c ? keySpan(key) : el("span", "ckey", key);
    if (rk) nm.appendChild(rk);
    left.appendChild(nm);
    keyTitle(left, key, c ? c.label : key);
    const meta = el("div", "row destmeta");
    if (!c) {
      const p = el("span", "pill bad");
      p.appendChild(icon("warn", 13));
      p.appendChild(el("span", null, "not in this save"));
      p.title = key + " is not a container in this save, so nothing is ever moved there";
      meta.appendChild(p);
    } else {
      const bar = el("span", "bar");
      const i2 = el("i");
      const pct = c.valid ? Math.round(100 * c.used / c.valid) : 0;
      i2.style.width = pct + "%";
      i2.style.background = (c.valid > 0 && c.free === 0)
        ? "var(--danger)" : "var(--ink-3)";
      bar.appendChild(i2);
      meta.appendChild(bar);
      meta.appendChild(el("span", "tnum", `${c.used}/${c.valid}`));
      if (c.valid > 0 && c.free === 0) {
        // FULL is carried by the word, in a box: colour alone is not a
        // message, and the sentence says what to do about it.
        const f = el("span", "pill bad");
        f.appendChild(el("span", null, "full"));
        f.title = "every cell in this container is taken; give this category a second container";
        meta.appendChild(f);
      }
      // The pill that answers the question GUIDE.md says is the most common
      // cause of a short dry run, at the moment you are routing to it.
      if (c.is_source) {
        const p = el("span", "pill ok");
        p.appendChild(icon("tick", 13));
        p.appendChild(el("span", null, "source"));
        p.title = "this container is drained, so what it holds can move out again";
        meta.appendChild(p);
      } else {
        const p = el("span", "pill warn");
        p.appendChild(icon("warn", 13));
        p.appendChild(el("span", null, "not drained"));
        p.title = "this container is not a source, so nothing is ever taken out of it";
        meta.appendChild(p);
      }
      if (c.in_use) {
        const u = el("span", "pill");
        u.appendChild(el("span", null, "in use"));
        u.title = "the ship or exocraft you are currently in";
        meta.appendChild(u);
      }
    }
    left.appendChild(meta);
    tr.appendChild(left);

    const right = el("div", "destchips");
    right.setAttribute("role", "gridcell");
    const wrap = el("div", "chipwrap");
    rules.forEach((r, ri) => {
      if (r.store !== key) return;
      const b = S.buckets.find(x => x.key === r.bucket) ||
        { key: r.bucket, label: r.bucket, items: 0, note: "" };
      const chain = rules.filter(x => x.bucket === r.bucket);
      const ord = chain.length > 1 ? chain.findIndex(x => x.store === key) + 1 : 0;
      const chip = bucketChip(b, key, ord);
      wrap.appendChild(chip);
      // Where a "bucket_rules[N]" warning points, and the control that
      // answers it.
      fixTarget("bucket_rules[" + ri + "]", tr,
        () => { const x = chip.querySelector(".x"); if (x) x.focus(); });
    });
    const add = addCategoryControl(key);
    wrap.appendChild(add);
    if (add._why) wrap.appendChild(add._why);
    right.appendChild(wrap);
    // Drag stays as an accelerator on the row.
    dropzone(right, (bkt, from) => {
      if (from === key) return;
      if (rules.some(r => r.bucket === bkt && r.store === key)) return;
      const kept = from ? rules.filter(r => !(r.bucket === bkt && r.store === from))
                        : rules;
      S.config.bucket_rules = kept.concat([{ bucket: bkt, store: key }]);
      revalidate();
    });
    // One tab stop for the grid, which is the row: every control in it is
    // reached with the arrow keys instead, which is what `role="grid"`
    // promises and what takes crossing eleven rows from 24 tab stops to one.
    // `focusRestore` still reaches any of them by key, because `.focus()`
    // does not care about `tabIndex`.
    tr.appendChild(right);
    tr.querySelectorAll("button,select,input").forEach(n => { n.tabIndex = -1; });
    host.appendChild(tr);
  });
  if (!keys.length) {
    // 5 policy 3: an empty state carries the instruction.
    const e = el("div", "empty");
    e.appendChild(el("b", null, "No category has a destination"));
    e.appendChild(el("p", null,
      "Give one a container below and it appears here. A dry run moves nothing " +
      "until at least one category has somewhere to go."));
    host.appendChild(e);
  }
  const dc = $("#dest-count");
  if (dc) {
    dc.textContent = keys.length
      ? plural(keys.length, "container receives something",
               "containers receive something")
      : "";
  }

  // ---- the No destination panel ----------------------------------------
  const shelf = $("#bucket-shelf"); shelf.innerHTML = "";
  const none = unroutedBuckets();
  none.forEach(b => {
    const row = el("div", "unrouted");
    row.dataset.chip = b.key;
    const chip = bucketChip(b);
    row.appendChild(chip);
    const sel = routeSelect(b, "");
    fk(sel, `unrouted-${b.key}-route`);
    row.appendChild(sel);
    if (sel._why) row.appendChild(sel._why);
    shelf.appendChild(row);
  });
  if (!none.length) {
    // 5 cut 11: two 29-word hints became this, once, and only in the state it
    // is true of.
    const e = el("div", "empty");
    e.appendChild(icon("tick", 16));
    e.appendChild(el("b", null, "Every category has a destination"));
    e.appendChild(el("p", null,
      "One with none is never moved by a category rule: a real choice, but " +
      "nothing here is making it."));
    shelf.appendChild(e);
  }
  const uc = $("#unrouted-count");
  if (uc) {
    uc.textContent = none.length
      ? plural(none.length, "category", "categories")
      : "all " + routableBuckets(S.buckets).length + " categories have one";
  }
  // Dropping a chip back on the panel unroutes that category everywhere,
  // which is destructive and silent, so it asks first (D17). The zone is the
  // fold rather than the shelf inside it, so the gesture still has a target
  // when the fold is closed.
  const fold = $("#nd-fold");
  dropzone(fold || shelf, b => unrouteEverywhere(b, fold || shelf));

  // ---- A2: the drag-free "remove from everywhere" -----------------------
  const all = $("#unroute-all");
  if (all) {
    const routed = [];
    rules.forEach(r => {
      if (!routed.includes(r.bucket)) routed.push(r.bucket);
    });
    const was = all.value;
    all.innerHTML = "";
    routed.forEach(k => {
      const o = el("option"); o.value = k; o.textContent = bktLabel(k);
      all.appendChild(o);
    });
    if (was && routed.includes(was)) all.value = was;
    const btn = $("#unroute-all-btn");
    const why = $("#unroute-all-why");
    all.disabled = !routed.length;
    if (btn) btn.disabled = !routed.length;
    if (why) {
      why.textContent = routed.length ? ""
        : "nothing is routed, so there is nothing to remove";
    }
    all.setAttribute("aria-label", routed.length
      ? "a category to stop routing anywhere"
      : "a category to stop routing anywhere. Nothing is routed, so there is nothing to remove");
  }
}

/* The keyboard and pointer path that used to be a drag onto the shelf and
   nothing else (A2, WCAG 2.2 `dragging-alternative`). */
function unrouteEverywhere(bucket, anchor) {
  const rules = (S.config && S.config.bucket_rules) || [];
  const where = rules.filter(r => r.bucket === bucket).map(r => contLabel(r.store));
  if (!where.length) return;
  const label = bktLabel(bucket);
  confirmInline(anchor,
    `Unroute ${label} from ` +
    (where.length === 1 ? where[0]
                        : `all ${where.length} of its containers (${where.join(", ")})`) +
    `? Nothing in it would ever be moved again.`,
    () => {
      const before = snapshot();
      S.config.bucket_rules = (S.config.bucket_rules || [])
        .filter(r => r.bucket !== bucket);
      focusAfter(`unrouted-${bucket}-route`);
      revalidate();
      undoable(`${label} unrouted from ` +
        plural(where.length, "container", "containers"), before);
    }, null, null, REMOVE_YN);
}

/* Up and down buttons, so an order that can be dragged can also be typed at.
   `move` is the mutation; the button re-focuses itself at the new index, which
   is what lets a keyboard user press it twice. */
/* Up and down buttons, so an order that can be dragged can also be typed at.
   An arrow with nowhere to go is `aria-disabled` rather than `disabled`, so it
   keeps its place in the Tab order and focus can land on it after the move
   that got the row to the end of the list (U2). `keyBase` is the focus key
   prefix: the button names its own new index, which is what lets the same key
   be pressed three times. */
function orderButtons(list, i, onMove, what, keyBase) {
  const wrap = el("div", "ord-btns");
  const mk = (dir, glyph, label) => {
    const inert = dir < 0 ? i === 0 : i === list.length - 1;
    const b = el("button", "iconbtn ordbtn" + (inert ? " inert" : ""), glyph);
    b.type = "button";
    b.title = inert
      ? `${label}: it is already ${dir < 0 ? "first" : "last"}`
      : label;
    b.setAttribute("aria-label", b.title);
    if (inert) b.setAttribute("aria-disabled", "true");
    fk(b, `${keyBase}-${i}-${dir < 0 ? "up" : "down"}`);
    b.onclick = () => {
      if (inert) return;
      const j = i + dir;
      if (j < 0 || j >= list.length) return;
      const [m] = list.splice(i, 1); list.splice(j, 0, m);
      focusAfter(`${keyBase}-${j}-${dir < 0 ? "up" : "down"}`);
      onMove();
    };
    return b;
  };
  wrap.appendChild(mk(-1, "↑", `move ${what} up`));
  wrap.appendChild(mk(1, "↓", `move ${what} down`));
  return wrap;
}

/* B3: `.chip .count{margin-left:auto}` was applied to the *first* child of a
   source row, so the `#N` ordinal started at x = 196, 192, 194, 192, 175, 136
   inside a 579 px row: an ordered list of nine sources sharing no left edge.
   A real table with a 34 px ordinal column, and the used/valid figures in a
   column of their own, right-aligned, in tabular figures. */
function renderSources() {
  const box = $("#sources"); box.innerHTML = "";
  const srcs = S.config.sources || [];
  const tb = el("table", "srctable");
  const body = el("tbody");
  srcs.forEach((key, i) => {
    const c = cont(key);
    const name = c ? c.label : key;
    const tr = el("tr");
    tr.draggable = true;
    tr.dataset.src = String(i);
    const ord = el("td", "c hint tnum", String(i + 1));
    tr.appendChild(ord);
    const nm = el("td", "srcname");
    nm.appendChild(el("span", null, name));
    // The key was printed beside the name here, inside a 340 px column that
    // also holds an ordinal, a used/valid figure and four buttons, so the
    // thing it was competing with was the name: "Starship 1 shi...",
    // "Corvette Stora...". The row already carries both in its title.
    nm.title = c ? `${name} (${key})` : name;
    tr.appendChild(nm);
    const st = el("td", "r hint tnum", c ? `${c.used}/${c.valid}` : "not in this save");
    st.title = c ? `${c.used} of ${c.valid} cells used`
                 : "this save has no container with that key, so it is never drained";
    tr.appendChild(st);
    const tools = el("td", "r srctools");
    // U20: named, like the rule buttons are. Four rows of "move this source
    // up" told a screen-reader user nothing about which row they were on.
    tools.appendChild(orderButtons(srcs, i, () => revalidate(), name, "src"));
    const x = el("button", "iconbtn del");
    x.type = "button";
    x.appendChild(icon("close", 13));
    x.title = "remove " + name + " from the sources";
    x.setAttribute("aria-label", x.title);
    fk(x, `src-${i}-remove`);
    // One sentence, the container's own label in it, under the row it is
    // about. The anchor is the remove control, not its cell: cancel puts the
    // focus back on a button rather than on an unfocusable `td`.
    x.onclick = () => confirmInline(x,
      `Stop taking items from ${contSay(key)}?`,
      () => {
        const before = snapshot();
        S.config.sources.splice(i, 1);
        focusAfter(`src-${Math.max(0, i - 1)}-remove`);
        revalidate();
        undoable(`${name} is no longer a source`, before);
      }, null, null, REMOVE_YN);
    tools.appendChild(x);
    tr.appendChild(tools);
    fixTarget("sources[" + i + "]", tr, () => x.focus());
    tr.ondragstart = e => {
      e.dataTransfer.setData("text/src", String(i));
      tr.classList.add("dragging");
    };
    tr.ondragend = () => tr.classList.remove("dragging");
    tr.ondragover = e => {
      if (e.dataTransfer.types.includes("text/src")) e.preventDefault();
    };
    tr.ondrop = e => {
      e.preventDefault();
      const from = parseInt(e.dataTransfer.getData("text/src"), 10);
      if (isNaN(from) || from === i) return;
      const a = S.config.sources; const [m] = a.splice(from, 1); a.splice(i, 0, m);
      revalidate();
    };
    body.appendChild(tr);
  });
  tb.appendChild(body);
  if (srcs.length) box.appendChild(tb);
  else {
    const e = el("div", "empty");
    e.appendChild(el("b", null, "No sources"));
    e.appendChild(el("p", null,
      "Nothing is ever taken out of a container that is not a source, so a dry " +
      "run would move nothing. Add one below."));
    box.appendChild(e);
  }
  const add = $("#add-source"); add.innerHTML = "";
  // A Stellar Extractor Core is not sortable -- nothing is ever put into one --
  // but it can be drained, so it belongs in this list.
  (S.save ? S.save.containers.filter(c => c.sortable || c.drain_only) : [])
    .filter(c => !(S.config.sources || []).includes(c.key))
    .forEach(c => {
      const o = el("option"); o.value = c.key;
      // `contOpt`, so two containers reading the same name are told apart
      // here too: this list had the label alone and nothing else.
      // One space before the parenthesis: this join carried two, so all
      // fourteen cores read `Stellar Extractor 1  (drain only)` (QA 2, 6).
      o.textContent = contOpt(c) + (c.drain_only ? " (drain only)" : "");
      add.appendChild(o);
    });
  add.disabled = !add.options.length;
  const addBtn = $("#add-source-btn");
  if (addBtn) addBtn.disabled = !add.options.length;
  const all = $("#add-all-extractors");
  if (all) {
    const cores = (S.save ? S.save.containers.filter(c => c.drain_only) : [])
      .map(c => c.key).filter(k => !(S.config.sources || []).includes(k));
    all.disabled = !cores.length;
    // U30: a disabled button is not the place for a status. The button keeps a
    // verb; the state is the sentence beside it. 5 cut 15 keeps that sentence,
    // because it is the only way to know why the add-select is short.
    all.textContent = cores.length
      ? "Add all " + plural(cores.length, "extractor core", "extractor cores")
      : "Add all extractor cores";
    all.title = cores.length
      ? "adds every extractor core in this save to the sources"
      : "every extractor core in this save is already a source";
    let why = all.nextElementSibling;
    if (!why || !why.classList.contains("extractor-note")) {
      why = el("span", "hint extractor-note");
      all.insertAdjacentElement("afterend", why);
    }
    const already = (S.save ? S.save.containers.filter(c => c.drain_only) : [])
      .filter(c => (S.config.sources || []).includes(c.key)).length;
    why.textContent = cores.length ? ""
      : (already
          ? plural(already, "extractor core is already a source",
                   "extractor cores are already sources") + "."
          : "this save has no extractor cores.");
    all.onclick = () => {
      S.config.sources = (S.config.sources || []).concat(cores);
      focusAfter("add-all-extractors");
      revalidate();
    };
  }
}

function renderOptions() {
  const o = S.config.options, box = $("#options"); box.innerHTML = "";
  // §5 cut 24: seven hints, 137 words, printed under seven controls. The
  // sentence is verbatim in a `?` now; what five of them were actually for --
  // "whole number of ..." -- is a unit suffix on the field.
  let host = box;
  const group = title => {
    const g = el("div", "optgroup");
    g.appendChild(el("h3", null, title));
    box.appendChild(g);
    host = g;
  };
  // The label sits beside the control, not parked at the far right edge of the
  // card (B2), and it is wired up by id: a <label> with no `for` is a label of
  // nothing.
  const bool = (k, label, hint) => {
    const d = el("div", "opt");
    const l = el("label", "optlabel", label); l.htmlFor = "opt-" + k;
    d.appendChild(l);
    d.appendChild(helpButton(hint, label, "opt-" + k + "-help"));
    d.appendChild(el("span", "tablespacer"));
    const i = el("input"); i.type = "checkbox"; i.checked = !!o[k];
    i.id = l.htmlFor; i.name = i.id;
    fk(i, "opt-" + k);
    i.onchange = () => { o[k] = i.checked; markDirty(); scheduleValidate(); };
    d.appendChild(i);
    host.appendChild(d);
  };
  // U16: three of these announced "minimum 0, maximum 0", because
  // `input[type=number]` with no `min`/`max` is what Chrome maps to 0..0 in the
  // accessibility tree. None of them has a real ceiling -- the planner does not
  // impose one -- so rather than invent a bound, the control drops the
  // spinbutton role entirely and says what it accepts in the unit suffix.
  //
  // B4: and it now takes the same `markBad` path as the rule rows' `numField`.
  // These fields used to coerce silently with `Math.max(0, parseInt || 0)` and
  // rewrite the box, so two identical-looking number fields on one page had
  // two different validation contracts: one named its complaint, one said
  // nothing at all.
  const int = (k, label, hint, unit) => {
    const d = el("div", "opt");
    const l = el("label", "optlabel", label); l.htmlFor = "opt-" + k;
    d.appendChild(l);
    d.appendChild(helpButton(hint, label, "opt-" + k + "-help"));
    d.appendChild(el("span", "tablespacer"));
    const unitBox = el("span", "unit");
    const i = el("input", "inum");
    i.type = "text";
    i.inputMode = "numeric";
    i.autocomplete = "off";
    i.spellcheck = false;
    i.value = o[k];
    i.id = l.htmlFor; i.name = i.id;
    i.setAttribute("aria-label", label + ", a whole number of " + unit);
    fk(i, "opt-" + k);
    i.onfocus = () => i.select();          // same retype-not-edit behaviour
    i.onclick = () => i.select();
    const fkey = label + "|options." + k;
    const read = () => {
      const raw = i.value.trim();
      if (/^\d+$/.test(raw)) { markGood(fkey, i); o[k] = parseInt(raw, 10); return; }
      // The model keeps the value it had: a number the field rejects must not
      // reach the planner or the file.
      markBad(fkey, i,
        `${label} must be a whole number from 0 upwards, so "${raw}" was not taken.`,
        "options");
    };
    i.oninput = () => { read(); markDirty(); };
    i.onchange = () => { read(); revalidate(); };
    unitBox.appendChild(i);
    unitBox.appendChild(el("span", "suf", unit));
    d.appendChild(unitBox);
    host.appendChild(d);
  };
  group("Stacks");
  bool("merge_duplicates", "Merge duplicate stacks",
    "combines two stacks of one item inside one container; frees cells and is what makes an item movable");
  bool("create_stacks", "Create new stacks in the destination",
    "off = merge-only: a target with no stack of the item is reported, not filled");
  int("min_source", "Leave behind in the source",
    "whole number of units left behind so the source stack survives a reload",
    "units");
  group("Limits per run");
  int("max_moves", "Move rows",
    "whole number; a runaway plan is still a runaway plan", "max");
  // U31: the one field with no sentence of its own.
  int("max_new_stacks", "New stacks",
    "whole number; once this many new stacks have been created the rest is reported, not done",
    "max");
  group("Container names");
  bool("auto_name", "Name containers from this configuration",
    "renames each storage container after the categories routed to it, capped at " +
    "the game's 40-character limit. Only containers this config routes something to.");
  bool("auto_name_overwrite", "Rename containers you named yourself as well",
    "off = a name you typed by hand is left alone; only the game's own default " +
    "name and blanks are replaced");

  group("Tidy");
  // U17: a 29-option `<select multiple>` whose arrow keys silently threw the
  // previous pick away. A checkbox list, like the rest of the page.
  // The label row sits *outside* the fold: a `<summary>` is itself a button,
  // so an interactive element inside one is a control a keyboard cannot reach
  // predictably -- Chrome reports it, and it is the same class of defect as a
  // `title` on a non-focusable element (A4).
  const wrap = el("div", "opt tidy-opt");
  const l = el("div", "tidy-head row");
  const lbl = el("span", "optlabel", "Repack the grid");
  l.appendChild(lbl);
  const h = el("span", "offscreen",
    "repack the grid into reading order by category, then name. Writes only " +
    "the two grid coordinates. Technology grids are always refused.");
  h.id = "opt-tidy-hint";
  l.appendChild(helpButton(h.textContent, "Repack the grid", "opt-tidy-help"));
  l.appendChild(h);
  l.appendChild(el("span", "tablespacer"));
  const chosenNote = el("span", "hint");
  chosenNote.id = "tidy-count";
  l.appendChild(chosenNote);
  wrap.appendChild(l);
  const d = el("details", "tidy-fold");
  if (TIDY_OPEN) d.open = true;
  d.ontoggle = () => { TIDY_OPEN = d.open; };
  const sum = el("summary", null, "Choose containers");
  sum.setAttribute("aria-label",
    "choose which containers are repacked, " + tidyNote(o.tidy));
  fk(sum, "opt-tidy-fold");
  d.appendChild(sum);
  const list = el("div", "tidy-list");
  list.setAttribute("role", "group");
  list.setAttribute("aria-label", "Tidy these containers");
  list.setAttribute("aria-describedby", h.id);
  const chosen = (o.tidy || []);
  sortableConts().forEach(c => {
    const lab = el("label", "check");
    lab.htmlFor = "tidy-" + c.key;
    const cb = el("input"); cb.type = "checkbox";
    cb.id = lab.htmlFor; cb.name = cb.id;
    cb.checked = chosen.includes(c.key);
    cb.setAttribute("aria-label", "tidy " + contPhrase(c.key));
    fk(cb, "tidy-" + c.key);
    cb.onchange = () => {
      const set = new Set(o.tidy || []);
      if (cb.checked) set.add(c.key); else set.delete(c.key);
      o.tidy = sortableConts().map(x => x.key).filter(k => set.has(k));
      markDirty(); scheduleValidate();
      const note = $("#tidy-count");
      if (note) note.textContent = tidyNote(o.tidy);
    };
    lab.appendChild(cb);
    lab.appendChild(el("span", null, c.label));
    const tk = keySpan(c.key);
    if (tk) lab.appendChild(tk);
    keyTitle(lab, c.key, c.label);
    list.appendChild(lab);
  });
  d.appendChild(list);
  wrap.appendChild(d);
  chosenNote.textContent = tidyNote(o.tidy);
  host.appendChild(wrap);
}

/* Whether the tidy fold was open, so a re-render does not close it under the
   hand that opened it. */
let TIDY_OPEN = false;

function tidyNote(tidy) {
  const n = (tidy || []).length;
  return n ? plural(n, "container", "containers") + " will be repacked"
           : "no container is repacked";
}

/* Per-item rules: one row each, columns labelled once at the top.

   The columns are what the old table had, but they are no longer four bare
   numbers in a line. keep and destination sit where the sentence would put
   them, the cap only appears where it can apply, and the last column says what
   the rule does to this save right now. */

const RULE_MODES = [
  { key: "pin",        label: "pin" },
  { key: "keep",       label: "keep" },
  { key: "route",      label: "send all" },
  { key: "keep_route", label: "keep + send" },
  { key: "stock",      label: "stock" },
];

function ruleMode(r) {
  if (r.stock !== undefined && r.stock !== null && r.stock_in) return "stock";
  if (r.pin) return "pin";
  if (r.store && r.keep !== undefined && r.keep !== null) return "keep_route";
  if (r.store) return "route";
  return "keep";
}

/* A number typed into a field the next mode has no use for is not thrown away
   (the D17 complaint); it is parked here, keyed by the rule object itself, and
   put back if the mode comes back. A side table rather than `_kept` on the
   rule, because the rule is serialised and posted and the config file must not
   grow UI state. Saving replaces every rule object, which empties this. */
const KEPT = new WeakMap();
const MODE_FIELDS = {
  pin: ["pin"],
  keep: ["keep", "keep_in"],
  route: ["store", "fill"],
  keep_route: ["keep", "keep_in", "store", "fill"],
  stock: ["stock", "stock_in", "store"],
};
const ALL_MODE_FIELDS = ["pin", "keep", "keep_in", "store", "fill", "stock", "stock_in"];

function keptOf(r) { return KEPT.get(r) || {}; }

/* keep and stock are both "an amount of this item", so a number typed for one
   is the number meant for the other: switching keep 250 to stock used to zero
   the field and describe the rule as "Stock 0 ... at its 0" (U15). The value is
   carried, and because it is now live it is not also shown struck through. */
const AMOUNT_ALIAS = { keep: "stock", stock: "keep" };
function amountBack(k, field) {
  if (k[field] !== undefined && k[field] !== null) return k[field];
  const alt = AMOUNT_ALIAS[field];
  if (alt && k[alt] !== undefined && k[alt] !== null) {
    const v = k[alt];
    delete k[alt];
    k._carried = alt;
    return v;
  }
  return undefined;
}

function setRuleMode(r, mode) {
  // Every mode starts from nothing: a keep floor of 250 silently becoming a
  // stock level of 250 is a different instruction wearing the same digits. What
  // was typed is remembered beside the rule, shown struck through where it no
  // longer applies, and restored if the mode is switched back.
  const k = keptOf(r);
  ALL_MODE_FIELDS.forEach(f => {
    if (r[f] !== undefined && r[f] !== null) k[f] = r[f];
    delete r[f];
  });
  KEPT.set(r, k);
  const want = MODE_FIELDS[mode] || [];
  const back = f => (k[f] !== undefined && k[f] !== null) ? k[f] : undefined;
  const amount = f => amountBack(k, f);
  if (mode === "pin") r.pin = true;
  else if (mode === "keep") { const v = amount("keep");
                              r.keep = v !== undefined ? v : 0;
                              if (back("keep_in") !== undefined) r.keep_in = back("keep_in"); }
  else if (mode === "route") { r.store = back("store") || firstSortableKey();
                               if (back("fill") !== undefined) r.fill = back("fill"); }
  else if (mode === "stock") {
    const v = amount("stock");
    r.stock = v !== undefined ? v : 0;
    r.stock_in = back("stock_in") || (S.config.sources || [])[0] || firstSortableKey();
    if (back("store") !== undefined) r.store = back("store");
  } else {
    const v = amount("keep");
    r.keep = v !== undefined ? v : 0;
    if (back("keep_in") !== undefined) r.keep_in = back("keep_in");
    r.store = back("store") || firstSortableKey();
    if (back("fill") !== undefined) r.fill = back("fill");
  }
  // Anything the new mode now carries is no longer "kept": it is live again.
  want.forEach(f => { if (r[f] !== undefined) delete k[f]; });
}

/* 5 cut 23: what a number the new verb has no slot for used to look like --
   struck through, with a hint explaining the strikethrough -- is nothing at
   all. `KEPT` still remembers it and `setRuleMode` still puts it back when the
   verb comes back; there is simply no visible artefact, so there is nothing to
   explain. */

function firstSortableKey() {
  const c = sortableConts()[0];
  return c ? c.key : "";
}

/* A number cell. A substance caps per stack at a number the save itself
   states, so that field can carry a real `max`; a product does not cap in any
   way this field could express. U16: rather than a `min` with no `max`, which
   Chrome announces as "minimum 0, maximum 0", an unbounded field is a numeric
   text box and says what it accepts in its description. `onLive` re-renders
   the generated sentence while the caret is still in the box (U25). */
function numField(r, field, ph, cls, label, onLive, fixWhere) {
  const i = el("input", "inum " + (cls || ""));
  i.placeholder = ph;
  // X5: the ceiling the save states for this item is still enforced -- `read()`
  // below refuses anything above it by name -- but it no longer changes the
  // *type* of the control. Two visually identical fields in one list behaved
  // differently under the arrow keys because one item had a per-stack cap in
  // this save and the next did not.
  const sm = stackMaxOf(r.item);
  const bounded = (sm && ["keep", "stock", "fill", "move"].includes(field))
    ? sm : (field === "priority" ? 9999 : null);
  i.type = "text";
  i.inputMode = "numeric";
  i.autocomplete = "off";
  i.spellcheck = false;
  if (label) {
    i.setAttribute("aria-label", label + ", a whole number" +
      (bounded ? " from 0 to " + num(bounded) : " of units"));
    // C3: every keep field was `name="rule-keep"`, so three rows carried the
    // same name. Indexed, like `rule-5-priority` beside it.
    const at = (S.config.item_rules || []).indexOf(r);
    i.name = at >= 0 ? `rule-${at}-${field}` : "rule-" + field;
  }
  i.value = (r[field] === undefined || r[field] === null) ? "" : r[field];
  // Clicking in selects the whole value: these cells are retyped, not edited a
  // digit at a time. focus fires for tabbing, click for the mouse, where focus
  // has usually already happened and the browser has placed a caret.
  i.onfocus = () => i.select();
  i.onclick = () => i.select();
  // The key a complaint about this field is filed under, and the words it uses.
  const fkey = (label || field) + "|" + field;
  const lo = 0, hi = bounded || null;
  const read = () => {
    const raw = i.value.trim();
    if (raw === "") { markGood(fkey, i); r[field] = undefined; return; }
    const n = parseInt(raw, 10);
    const ok = /^\d+$/.test(raw) && !isNaN(n) && n >= lo && (hi === null || n <= hi);
    if (!ok) {
      // The model keeps the value it had: a number the field rejects must not
      // reach the generated sentence, the planner or the file.
      markBad(fkey, i,
        `${label || field} must be a whole number ` +
        (hi === null ? `from ${lo} upwards` : `from ${lo} to ${num(hi)}`) +
        `, so "${raw}" was not taken.`,
        fixWhere || "");
      return;
    }
    markGood(fkey, i);
    r[field] = Math.max(0, n);
  };
  i.oninput = () => { read(); if (onLive) onLive(); markDirty(); };
  i.onchange = () => { read(); revalidate(); };
  return i;
}

function containerSelect(value, onChange) {
  const sel = el("select", "isel");
  sortableConts().forEach(c => {
    const o = el("option"); o.value = c.key; o.textContent = contOpt(c);
    sel.appendChild(o);
  });
  if (value && !Array.from(sel.options).some(o => o.value === value)) {
    const o = el("option"); o.value = value;
    o.textContent = value + " (not in this save)";
    sel.appendChild(o);
  }
  sel.value = value || "";
  sel.onchange = () => { onChange(sel.value); revalidate(); };
  return sel;
}

function ruleEffect(r, mode) {
  if (!r.item) return ["", ""];
  if (!S.save) return ["", ""];
  const srcs = (S.config.sources || []);
  if (mode === "stock") {
    let held = 0, elsewhere = 0, where = [];
    (S.config.sources || []).forEach(key => {
      const c = cont(key); if (!c) return;
      let n = 0;
      (c.items || []).forEach(i => { if (i.stem === r.item || i.id === r.item) n += i.amount; });
      if (!n) return;
      where.push(`${num(n)} in ${c.label}`);
      if (key === r.stock_in) held += n; else elsewhere += n;
    });
    const want = r.stock || 0;
    const short = Math.max(0, want - held);
    const pull = Math.min(short, elsewhere);
    const tip = where.join(", ");
    if (!held && !elsewhere) return ["nothing held", ""];
    if (short <= 0) return [`${num(held)} stocked, at its ${num(want)}`, tip];
    if (!pull) return [`${num(held)} of ${num(want)}, none to pull`, tip];
    return [`${num(held)} of ${num(want)}, pulls ${num(pull)}`, tip];
  }
  // A rule with no amount typed yet is what the planner treats as a pin, so
  // the live figure says so rather than reporting the whole holding as moving
  // (U8).
  const unset = (mode === "keep" || mode === "keep_route") &&
    (r.keep === undefined || r.keep === null);
  const keep = (mode === "keep" || mode === "keep_route") ? (r.keep || 0) : 0;
  let total = 0, surplus = 0, where = [];
  srcs.forEach(key => {
    const c = cont(key);
    if (!c) return;
    let n = 0;
    (c.items || []).forEach(i => { if (i.stem === r.item || i.id === r.item) n += i.amount; });
    if (!n) return;
    total += n;
    where.push(`${num(n)} in ${c.label}`);
    if (mode === "pin") return;
    // The floor only bites in the container it names; every other source drains
    // in full, which is exactly the thing the flat number used to hide.
    const floorHere = (!r.keep_in || r.keep_in === key) ? keep : 0;
    surplus += Math.max(0, n - floorHere);
  });
  const tip = where.join(", ");
  if (!total) return ["nothing held", ""];
  if (mode === "pin" || unset) return [`${num(total)} held, all stays`, tip];
  if (!surplus) return [`${num(total)} held, none moves`, tip];
  return [`${num(total)} held, ${num(surplus)} moves`, tip];
}

/* ------------------------------------------------- P3-3 the rule, in a line

   Built from the same fields, in the same order of precedence, that
   `planner.match_item_rule` reads: `stock` first, then the pin case, then keep
   and the destination chain. Nothing here is inferred from the mode select, so
   the sentence cannot drift from what a run would do. */
function ruleStores(r) {
  const out = [];
  const push = v => {
    if (Array.isArray(v)) v.forEach(push);
    else if (v && !out.includes(v)) out.push(v);
  };
  push(r.store); push(r.stores);
  return out;
}

/* `short` leaves off the priority clause: the sentence is reused inside the
   delete confirmation, where two thirds of it was about a number irrelevant to
   a deletion (U29). */
function ruleSentence(r, short) {
  if (!r.item) return "No item chosen yet, so this rule does nothing.";
  const name = r._name || r.item;
  const dsts = ruleStores(r);
  const cat = r._bucket_label ? ` (${r._bucket_label})` : "";
  const per = (r.move !== undefined && r.move !== null)
    ? `, at most ${num(r.move)} per run` : "";
  const overflow = dsts.length > 1
    ? `, overflowing to ${dsts.slice(1).map(contName).join(", ")}` : "";
  const capOf = k => (r.fill !== undefined && r.fill !== null)
    ? `, and stop when ${contName(k)} holds ${num(r.fill)}` : "";
  const stocked = r.stock !== undefined && r.stock !== null && !!r.stock_in;

  let s;
  if (stocked) {
    const tail = dsts.filter(k => k !== r.stock_in);
    s = `Stock ${num(r.stock)} ${name} in ${contName(r.stock_in)} from every source; ` +
      (tail.length
        ? `surplus goes to ${contName(tail[0])}` +
          (tail.length > 1 ? `, overflowing to ${tail.slice(1).map(contName).join(", ")}` : "")
        : `surplus follows its category${cat}`) + per + ".";
  } else if (r.pin) {
    s = `Pin ${name}: nothing moves it, whatever a category rule says.`;
  } else if (!dsts.length && (r.keep === undefined || r.keep === null)) {
    // U8: a rule with no number typed yet. The planner pins it, the select
    // says keep, and the sentence has to say both without contradicting
    // either.
    s = `Keep (amount not set): type a number, or ${name} is left where it is.`;
  } else if (dsts.length && (r.keep === undefined || r.keep === null)) {
    s = `Send all ${name} to ${contName(dsts[0])}${overflow}${capOf(dsts[0])}${per}.`;
  } else if (dsts.length) {
    s = `Keep ${num(r.keep)} ${name} in ` +
      `${r.keep_in ? contName(r.keep_in) : "every source"}; send the rest to ` +
      `${contName(dsts[0])}${overflow}${capOf(dsts[0])}${per}.`;
  } else {
    s = `Keep ${num(r.keep)} ${name} in ` +
      `${r.keep_in ? contName(r.keep_in) : "every source"}; the rest follows its ` +
      `category${cat}, wherever that is routed${per}.`;
  }
  if (!short && r.priority !== undefined && r.priority !== null &&
      r.priority !== 100) {
    s += ` Weighed at priority ${r.priority}, so it is tried ` +
      (r.priority < 100 ? "before" : "after") + " the rules left at 100.";
  }
  return s;
}

/* 3.2: the generated sentence becomes the control.

   Before: an 8-column grid (`24px 1.3fr 104px 1.25fr 1.2fr 62px 1.35fr 44px`)
   whose middle four cells changed meaning with the mode select; cells that
   were "not used" rendered the words *not used*; the parked value of the
   previous mode rendered struck through with a hint explaining the
   strikethrough; and underneath it all the correct answer already existed --
   `ruleSentence()`.

   After: that sentence, with its variables as controls. The decisive property
   is kept: the shape is chosen by the same fields, in the same order of
   precedence, that `planner.match_item_rule` reads -- `stock` first, then the
   pin case, then keep and the destination chain -- and *not* by the value of
   the mode select. `ruleShape()` below and `ruleSentence()` above read the
   same four predicates off the rule, so the row and the sentence cannot
   disagree with the planner or with each other.

   What this deletes: the 8-column header and its eight tooltips (cut 20), the
   `not used` / `stays put` / `its category` dash cells and their six tooltips
   (cut 22), the struck-through parked value and its explanatory hint (cut 23),
   and the `.ir-say` line itself (cut 21) -- it is the row. */
function ruleShape(r) {
  const dsts = ruleStores(r);
  return {
    dsts: dsts,
    stocked: r.stock !== undefined && r.stock !== null && !!r.stock_in,
    pinned: !!r.pin,
    hasKeep: r.keep !== undefined && r.keep !== null
  };
}

/* A container select, bounded so a long name cannot push the sentence off the
   row, with an ellipsis rather than a clip (A5). */
function sayContainer(opts) {
  const sel = el("select", "isel says");
  // `head` is one [value, text] pair, the option that is not a container:
  // "every source" on a keep floor, "its category" on a stock's surplus.
  (opts.head ? [opts.head] : []).forEach(pair => {
    const v = pair[0], t = pair[1];
    const o = el("option"); o.value = v; o.textContent = t; sel.appendChild(o);
  });
  (opts.list || sortableConts()).forEach(c => {
    const o = el("option"); o.value = c.key; o.textContent = contOpt(c);
    sel.appendChild(o);
  });
  if (opts.value && !Array.from(sel.options).some(o => o.value === opts.value)) {
    const o = el("option");
    o.value = opts.value;
    o.textContent = contLabel(opts.value) + (opts.strayNote || " (not in this save)");
    sel.appendChild(o);
  }
  sel.value = opts.value || "";
  sel.id = opts.id; sel.name = opts.id;
  sel.setAttribute("aria-label", opts.label);
  sel.title = opts.title || opts.label;
  fk(sel, opts.key);
  sel.onchange = () => { opts.onChange(sel.value); revalidate(); };
  return sel;
}

const sayWord = t => el("span", "w", t);

function renderItemRules() {
  const box = $("#item-rules"); box.innerHTML = "";
  const rules = S.config.item_rules || [];
  const issues = (S.issues || []).filter(i => /^item_rules\[/.test(i.where));
  const issueFor = i => issues.filter(x => +x.where.match(/\[(\d+)\]/)[1] === i);

  if (!rules.length) {
    // 5 policy 3: the empty state carries the instruction.
    const e = el("div", "empty");
    e.appendChild(el("b", null, "No per-item rules"));
    e.appendChild(el("p", null,
      "Everything is routed by its category alone. Name an item below to make " +
      "one item an exception."));
    box.appendChild(e);
    return;
  }

  rules.forEach((r, i) => box.appendChild(ruleRow(rules, r, i, issueFor(i))));
}

function ruleRow(rules, r, i, mine) {
  const mode = ruleMode(r);
  const sh = ruleShape(r);
  const bad = mine.some(x => x.level === "error");
  const wrap = el("div", "rule" + (bad ? " bad" : ""));
  wrap.dataset.rule = String(i);

  // ---- the ordinal and the two reorder buttons --------------------------
  const ord = el("div", "ord2");
  ord.appendChild(el("span", "n", String(i + 1)));
  ord.appendChild(orderButtons(rules, i, () => revalidate(),
    "rule " + (i + 1) + (r._name ? ", on " + r._name : ""), "rule"));
  ord.title = "read in this order";
  wrap.appendChild(ord);

  const body = el("div", "body");
  const say = el("div", "say");
  body.appendChild(say);
  const meta = el("div", "rulemeta");

  // ---- the verb ---------------------------------------------------------
  const modeSel = el("select", "isel verb");
  modeSel.setAttribute("aria-label", `what rule ${i + 1} does`);
  modeSel.id = `rule-${i}-mode`; modeSel.name = modeSel.id;
  fk(modeSel, `rule-${i}-mode`);
  RULE_MODES.forEach(m => {
    const o = el("option"); o.value = m.key; o.textContent = m.label;
    modeSel.appendChild(o);
  });
  modeSel.value = mode;
  modeSel.title = {
    pin: "never moved, whatever any category rule says",
    keep: "hold this many back; the surplus follows the item's category",
    route: "send all of it to one container, ignoring its category",
    keep_route: "hold this many back and send the surplus to one container",
    stock: "keep one container topped up to this many from every source, never "
         + "drain it below that, and send anything above it onward",
  }[mode];
  modeSel.onchange = () => {
    setRuleMode(r, modeSel.value);
    focusAfter(`rule-${i}-mode`);
    revalidate();
  };
  fixTarget(`item_rules[${i}].mode`, wrap, () => modeSel.focus());
  say.appendChild(modeSel);

  // ---- the item ---------------------------------------------------------
  const pick = el("button", "tok" + (r._name ? "" : " unknown"));
  pick.type = "button";
  pick.appendChild(el("span", "nm2", r._name || showId(r.item) || "choose…"));
  pick.title = (r._name ? `${r._name}, which the save stores as ` +
    `${showId(r.item)}. ` : "") + "Choose the item this rule is about.";
  pick.setAttribute("aria-label",
    `item for rule ${i + 1}: ${r._name || r.item || "not chosen"}. Change it`);
  fk(pick, `rule-${i}-item`);
  pick.onclick = () => openPicker(pick, it => {
    r.item = it.id; r._name = it.name; r._bucket_label = it.bucket_label;
    focusAfter(`rule-${i}-item`);
    revalidate();
  });

  const liveCell = el("span", "live");
  const live = () => {
    const [eff, tip] = ruleEffect(r, ruleMode(r));
    liveCell.textContent = eff || "";
    liveCell.title = "what this rule would do to the save loaded right now" +
      (tip ? ". Held in: " + tip
           : ". Nothing in your sources matches this item");
    liveCell.classList.toggle("act", /\d moves|pulls/.test(eff));
  };

  const amount = field => {
    const f = numField(r, field, "0",
      "amt", `${field === "stock" ? "stock level" : "keep floor"} for rule ${i + 1}`,
      () => live(), `item_rules[${i}]`);
    fk(f, `rule-${i}-${field}`);
    f.title = field === "stock"
      ? "how many to hold in that container. Every source tops it up to this " +
        "number, and it is never drained below it."
      : `floor: this many ${r._name || "of it"} stay put. Only the excess moves.`;
    return f;
  };

  const keepIn = () => {
    const list = (S.config.sources || []).map(k => cont(k) || { key: k, label: contLabel(k) });
    return sayContainer({
      head: ["", "every source"], list: list, value: r.keep_in || "",
      id: `rule-${i}-keep-in`, key: `rule-${i}-keepin`,
      label: `which source the floor guards, rule ${i + 1}`,
      strayNote: " (not a source)",
      title: "which source the floor guards. \"every source\" holds this many " +
             "back in each one separately, which is rarely what is meant.",
      onChange: v => { r.keep_in = v || undefined; focusAfter(`rule-${i}-keepin`); }
    });
  };
  const store = (head) => sayContainer({
    head: head, value: r.store || "", id: `rule-${i}-store`,
    key: `rule-${i}-store`, label: `where the surplus goes, rule ${i + 1}`,
    title: "where the surplus goes. Only containers that can receive items are listed.",
    onChange: v => { r.store = v || undefined; focusAfter(`rule-${i}-store`); }
  });
  const cap = () => {
    const f = numField(r, "fill", "none", "amt",
      `cap in the destination, rule ${i + 1}`, () => live(), `item_rules[${i}]`);
    fk(f, `rule-${i}-cap`);
    f.title = "ceiling: stop once the destination holds this many of the item. " +
      "Blank means no ceiling.";
    return f;
  };

  // ---- the sentence, in the planner's own order of precedence -----------
  if (sh.stocked) {
    say.appendChild(amount("stock"));
    say.appendChild(pick);
    say.appendChild(sayWord("in"));
    // D5: a stock aimed at a container that is not a source has nothing to
    // pull from and is silently inert, so only sources are offered.
    const stockable = sortableConts()
      .filter(c => (S.config.sources || []).includes(c.key));
    say.appendChild(sayContainer({
      list: stockable.length ? stockable : sortableConts(),
      value: r.stock_in || "", id: `rule-${i}-stock-in`,
      key: `rule-${i}-stockin`,
      label: `container to keep stocked, rule ${i + 1}`,
      strayNote: cont(r.stock_in) ? " (not a source)" : " (not in this save)",
      title: "the container to keep stocked. Only your sources are listed: a " +
             "stock in a container nothing is drained from can never be topped up.",
      onChange: v => { r.stock_in = v || undefined; focusAfter(`rule-${i}-stockin`); }
    }));
    say.appendChild(sayWord("; surplus to"));
    const st = store(["", "its category"]);
    say.appendChild(st);
    fixTarget(`item_rules[${i}].dest`, wrap, () => st.focus());
    // U15: a stock of nothing moves nothing, and nothing said so.
    if (!r.stock) {
      say.appendChild(el("span", "cellwarn",
        "a stock of 0 moves nothing: type a number"));
    }
  } else if (sh.pinned) {
    say.appendChild(pick);
    say.appendChild(sayWord("is never moved, whatever a category rule says."));
  } else if (!sh.dsts.length && !sh.hasKeep) {
    // U8: a rule with no number typed yet. The planner pins it, the verb says
    // keep, and the row has to say both without contradicting either.
    say.appendChild(amount("keep"));
    say.appendChild(pick);
    say.appendChild(sayWord("in"));
    say.appendChild(keepIn());
    say.appendChild(el("span", "cellwarn",
      "type a number, or it is left where it is"));
  } else if (sh.dsts.length && !sh.hasKeep) {
    say.appendChild(pick);
    say.appendChild(sayWord("to"));
    const st = store();
    say.appendChild(st);
    fixTarget(`item_rules[${i}].dest`, wrap, () => st.focus());
    say.appendChild(sayWord("and stop at"));
    say.appendChild(cap());
  } else if (sh.dsts.length) {
    say.appendChild(amount("keep"));
    say.appendChild(pick);
    say.appendChild(sayWord("in"));
    say.appendChild(keepIn());
    say.appendChild(sayWord("and send the rest to"));
    const st = store();
    say.appendChild(st);
    fixTarget(`item_rules[${i}].dest`, wrap, () => st.focus());
    say.appendChild(sayWord("stopping at"));
    say.appendChild(cap());
  } else {
    say.appendChild(amount("keep"));
    say.appendChild(pick);
    say.appendChild(sayWord("in"));
    say.appendChild(keepIn());
    say.appendChild(sayWord("and the rest follows its category"));
  }
  // An overflow chain the composer has no slot for is still stated, so a
  // configuration written by hand is never silently misdescribed.
  if (sh.dsts.length > 1) {
    say.appendChild(sayWord(", overflowing to " +
      sh.dsts.slice(1).map(contName).join(", ")));
  }

  // ---- the meta line: the read-only facts that used to be columns -------
  // X4: and the one clause that is constant for a whole verb. Every `stock`
  // rule pulls "from every source" -- it is never anything else -- so it
  // belongs here rather than taking a fifth of the sentence's width.
  // The id used to be printed here as well. It is in the item button's own
  // title -- "Sodium, which the save stores as CATALYST1" -- which is the
  // thing it names, so the meta line carried it for no one.
  if (sh.stocked) {
    meta.appendChild(el("span", null, "from every source"));
  }
  if (r._bucket_label) {
    const c = el("span", null, "category ");
    c.appendChild(el("b", null, r._bucket_label));
    meta.appendChild(c);
  }
  if (r.priority !== undefined && r.priority !== null) {
    meta.appendChild(el("span", null, "priority " + r.priority));
  }
  meta.appendChild(liveCell);
  body.appendChild(meta);

  // ---- the extras, behind the dots -------------------------------------
  const extra = el("div", "extra hidden");
  const numLabel = (name, key, ph, hint, unit) => {
    const lab = el("label", "lbl");
    lab.htmlFor = `rule-${i}-${key}`;
    lab.appendChild(el("span", null, name));
    const f = numField(r, key, ph, "num", name + " for rule " + (i + 1),
      () => live());
    f.id = lab.htmlFor; f.name = f.id;
    fk(f, `rule-${i}-${key}`);
    if (unit) {
      const u = el("span", "unit");
      u.appendChild(f);
      u.appendChild(el("span", "suf", unit));
      lab.appendChild(u);
    } else {
      lab.appendChild(f);
    }
    lab.appendChild(el("span", "under", hint));
    return lab;
  };
  // 5 cut 19: two tooltips and a 28-word `dl` entry became six words.
  extra.appendChild(numLabel("Priority", "priority", "100", "Lower runs first."));
  extra.appendChild(numLabel("Max per run", "move", "none", "A safety valve.",
    "units"));
  const p3 = el("label", "lbl grow");
  p3.htmlFor = `rule-${i}-note`;
  p3.appendChild(el("span", null, "Note"));
  const note = el("input", "inote");
  note.id = p3.htmlFor; note.name = note.id;
  note.value = r.note || ""; note.placeholder = "why this rule exists";
  note.setAttribute("aria-label", "note on rule " + (i + 1));
  fk(note, `rule-${i}-note`);
  note.onchange = () => { r.note = note.value.trim() || undefined; markDirty(); };
  p3.appendChild(note);
  extra.appendChild(p3);
  body.appendChild(extra);

  mine.forEach(x => {
    const d = el("div", "ir-issue " + x.level);
    d.appendChild(issueText(x.message));
    body.appendChild(d);
  });
  fixTarget(`item_rules[${i}]`, wrap, () => modeSel.focus());
  wrap.appendChild(body);

  // ---- the tools -------------------------------------------------------
  const tools = el("div", "tools");
  const more = el("button", "iconbtn");
  more.type = "button";
  more.appendChild(icon("dots", 16));
  more.title = "priority, max per run and note";
  fk(more, `rule-${i}-more`);
  more.setAttribute("aria-label", `priority, max per run and note for rule ${i + 1}`);
  more.setAttribute("aria-expanded", "false");
  more.onclick = () => {
    const now = extra.classList.toggle("hidden");
    more.setAttribute("aria-expanded", now ? "false" : "true");
  };
  tools.appendChild(more);
  const del = el("button", "iconbtn del");
  del.type = "button";
  del.appendChild(icon("close", 16));
  del.title = "remove this rule";
  fk(del, `rule-${i}-remove`);
  del.setAttribute("aria-label", `remove rule ${i + 1}` +
    (r._name ? ", on " + r._name : ""));
  // Removing a rule was one click with no way back (D17). It asks on the row,
  // names the rule in the words the sentence uses, and leaves an undo behind.
  // The anchor is the remove button, not the row's body: `afterend` on the
  // body put the box inside `.rule`, a nowrap flex row, over the controls it
  // was asking about. It goes under the whole rule now.
  del.onclick = () => confirmInline(del,
    `Remove rule ${i + 1}? ${ruleSentence(r, true)}`,
    () => {
      const before = snapshot();
      rules.splice(i, 1);
      focusAfter(rules.length
        ? `rule-${Math.min(i, rules.length - 1)}-remove` : "new-item");
      revalidate();
      undoable(`rule ${i + 1} removed`, before);
      panelTell(`rule ${i + 1} was removed. ` +
        `The undo button in the save bar puts it back.`);
    }, null, null, REMOVE_YN);
  tools.appendChild(del);
  wrap.appendChild(tools);

  live();

  // Drag reorder stays as an accelerator on the ordinal.
  ord.draggable = true;
  ord.ondragstart = e => {
    e.dataTransfer.setData("text/rule", String(i));
    wrap.classList.add("dragging");
  };
  ord.ondragend = () => wrap.classList.remove("dragging");
  wrap.ondragover = e => {
    if (e.dataTransfer.types.includes("text/rule")) {
      e.preventDefault(); wrap.classList.add("over");
    }
  };
  wrap.ondragleave = () => wrap.classList.remove("over");
  wrap.ondrop = e => {
    wrap.classList.remove("over");
    const from = parseInt(e.dataTransfer.getData("text/rule"), 10);
    if (isNaN(from) || from === i) return;
    e.preventDefault();
    const [m] = rules.splice(from, 1);
    rules.splice(i, 0, m);
    revalidate();
  };
  return wrap;
}

/* The one sentence a panel says about what just happened, so nothing is
   announced only by a toast. It used to be appended to `#item-rules-head`,
   which the split into two sections took away. */
function panelTell(text) {
  const host = $("#rules-issues");
  if (!host) return;
  let note = host.querySelector(".paneltell");
  if (!note) { note = el("p", "hint paneltell"); host.appendChild(note); }
  note.textContent = text;
}

/* a name-first item picker, openable from any rule row -------------- */
let pickerBox = null, pickerAnchor = null;
function closePicker(keepFocus) {
  if (pickerBox) { pickerBox.remove(); pickerBox = null; }
  // The popup took the focus when it opened, so it hands it back: closing a
  // dialog onto `document.body` loses a keyboard user's place entirely.
  const a = pickerAnchor;
  pickerAnchor = null;
  document.removeEventListener("mousedown", onPickerOutside, true);
  window.removeEventListener("scroll", placePicker, true);
  window.removeEventListener("resize", placePicker);
  if (!keepFocus && a && a.isConnected) a.focus();
}

/* Anchored under the row's item button, clamped into the viewport, flipped
   above it when there is no room below. Position only -- everything else the
   popup looks like is in .picker-pop. */
function placePicker() {
  if (!pickerBox || !pickerAnchor) return;
  if (!pickerAnchor.isConnected) { closePicker(); return; }
  const r = pickerAnchor.getBoundingClientRect();
  const w = Math.max(340, Math.min(r.width, window.innerWidth - 16));
  pickerBox.style.width = w + "px";
  pickerBox.style.left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8)) + "px";
  const h = pickerBox.offsetHeight || 300;
  const room = window.innerHeight - r.bottom - 8;
  pickerBox.style.top = (room >= h || r.top < h + 8 ? r.bottom + 4
                                                    : r.top - h - 4) + "px";
}
function onPickerOutside(e) {
  if (pickerBox && !pickerBox.contains(e.target)) closePicker();
}
function openPicker(anchor, onChoose) {
  closePicker(true);
  const box = el("div", "suggest picker-pop");
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-label", "choose an item");
  const input = el("input");
  input.placeholder = "type a name, e.g. Sodium";
  input.id = "picker-q"; input.name = "picker-q";
  input.setAttribute("aria-label", "search the item table by name or id");
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-controls", "picker-list");
  input.setAttribute("aria-autocomplete", "list");
  input.spellcheck = false;
  const list = el("div", "picker-list");
  list.id = "picker-list";
  list.setAttribute("role", "listbox");
  list.setAttribute("aria-label", "matching items");
  box.appendChild(input); box.appendChild(list);
  document.body.appendChild(box);
  pickerBox = box; pickerAnchor = anchor;
  placePicker();
  document.addEventListener("mousedown", onPickerOutside, true);
  // The popup is fixed to the viewport, so it has to be told when the viewport
  // moves under it. Capture, because the row it hangs off may sit in a
  // scrollable box of its own.
  window.addEventListener("scroll", placePicker, true);
  window.addEventListener("resize", placePicker);

  let sel = -1, items = [];
  async function refresh() {
    const res = await api("/api/items?q=" + encodeURIComponent(input.value.trim()) + "&n=60");
    items = res.items; sel = -1;
    list.innerHTML = "";
    if (!items.length) {
      list.innerHTML = `<div class="hint" style="padding:9px 11px">nothing in the ` +
        `item table matches that name or id.</div>`;
      placePicker();
      return;
    }
    items.forEach((it, i) => {
      const d = el("div");
      d.id = "pick-opt-" + i;
      d.setAttribute("role", "option");
      d.setAttribute("aria-selected", "false");
      d.innerHTML = `<span>${esc(it.name)}</span>` +
        `<span class="sid">${esc(it.id)}</span>` +
        `<span class="sb">${esc(it.bucket_label)}</span>`;
      d.onmousedown = ev => { ev.preventDefault(); closePicker(true); onChoose(it); };
      list.appendChild(d);
    });
    placePicker();
  }
  let t = null;
  input.addEventListener("input", () => { clearTimeout(t); t = setTimeout(refresh, 130); });
  input.addEventListener("keydown", e => {
    if (e.key === "Escape") { closePicker(); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      sel = Math.max(0, Math.min(items.length - 1, sel + (e.key === "ArrowDown" ? 1 : -1)));
      Array.from(list.children).forEach((c, i) => {
        c.classList.toggle("sel", i === sel);
        if (c.setAttribute) c.setAttribute("aria-selected", i === sel ? "true" : "false");
      });
      const n = list.children[sel];
      if (n) {
        input.setAttribute("aria-activedescendant", n.id || "");
        if (n.scrollIntoView) n.scrollIntoView({ block: "nearest" });
      }
    } else if (e.key === "Enter" && sel >= 0) {
      e.preventDefault(); closePicker(true); onChoose(items[sel]);
    }
  });
  input.focus();
  refresh();
}

/* item id autocomplete ---------------------------------------------- */
let sugTimer = null, sugItems = [], sugSel = -1;
/* The last row the picker handed back, kept so `add a rule` can put the name
   and the category on the new rule without a second round trip. */
let PICKED = null;
$("#new-item").addEventListener("focus", () => $("#new-item").dispatchEvent(new Event("input")));
$("#new-item").addEventListener("input", e => {
  clearTimeout(sugTimer);
  // Editing the box throws the pick away: the id belonged to the old text.
  delete e.target.dataset.id;
  if ($("#new-item-note").classList.contains("bad")) setAddNote(ADD_NOTE_IDLE);
  syncAddRule();
  const q = e.target.value.trim();
  sugTimer = setTimeout(async () => {
    const r = await api("/api/items?q=" + encodeURIComponent(q));
    sugItems = r.items; sugSel = -1;
    const box = $("#suggest"); box.innerHTML = "";
    if (!sugItems.length) {
      box.innerHTML = `<div class="hint" style="padding:9px 11px">no item in the table ` +
        `has this id or name. Ids come from the table or from your own inventory, ` +
        `never from a display name.</div>`;
    }
    sugItems.forEach((it, i) => {
      const d = el("div");
      d.id = "sug-opt-" + i;
      d.setAttribute("role", "option");
      d.setAttribute("aria-selected", "false");
      d.innerHTML = `<span>${esc(it.name)}</span><span class="sid">${esc(it.id)}</span>` +
        `<span class="sb">${esc(it.bucket_label)}</span>`;
      d.onclick = () => pick(it);
      box.appendChild(d);
    });
    box.classList.remove("hidden");
    $("#new-item").setAttribute("aria-expanded", "true");
    syncAddRule();          // D3: the exact-match test needs this answer
  }, 130);
});
$("#new-item").addEventListener("keydown", e => {
  const box = $("#suggest");
  if (box.classList.contains("hidden")) return;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    sugSel = Math.max(0, Math.min(sugItems.length - 1, sugSel + (e.key === "ArrowDown" ? 1 : -1)));
    Array.from(box.children).forEach((c, i) => {
      c.classList.toggle("sel", i === sugSel);
      if (c.setAttribute) c.setAttribute("aria-selected", i === sugSel ? "true" : "false");
    });
    const n = box.children[sugSel];
    if (n) e.target.setAttribute("aria-activedescendant", n.id || "");
  } else if (e.key === "Enter" && sugSel >= 0) { e.preventDefault(); pick(sugItems[sugSel]); }
  else if (e.key === "Escape") {
    closeSuggest();
  }
});

/* U30: `aria-activedescendant` pointing at an option in a listbox that is no
   longer open is a dangling reference, and a reader reads it. */
function closeSuggest() {
  const box = $("#suggest");
  box.classList.add("hidden");
  const inp = $("#new-item");
  inp.setAttribute("aria-expanded", "false");
  inp.removeAttribute("aria-activedescendant");
  sugSel = -1;
}
function pick(it) {
  PICKED = it;
  $("#new-item").value = it.name;
  $("#new-item").dataset.id = it.id;
  closeSuggest();
  setAddNote(`${it.name} · the game's id is ${it.id} · ${it.bucket_label}`);
  syncAddRule();
}

/* The id a rule stores is never invented here. It comes from a pick, or from
   the item table agreeing exactly with what was typed. An id the table does not
   know is not refused by the game: it is dropped, silently, on the next load,
   and the rule then looks fine while doing nothing. */
const ADD_NOTE_IDLE = "Pick a name from the list. The game's own id for it is " +
  "what gets stored.";
const ADD_NOTE_NO_MATCH = "no item in the table has this name or id. The " +
  "game's ids come from the table or from your own inventory, never from a " +
  "display name.";

function setAddNote(text, bad) {
  const n = $("#new-item-note");
  n.textContent = text;
  n.classList.toggle("bad", !!bad);
}

/* D3: the button's own tooltip said a typed name is only accepted when it
   matches an item exactly, and then the button enabled itself on any text at
   all. It now enables on a pick, or on text that the last suggestion list
   matched exactly, case for case ignored. */
function exactMatch(text) {
  const t = String(text || "").trim().toLowerCase();
  if (!t) return null;
  const hit = (sugItems || []).find(x =>
    String(x.name || "").toLowerCase() === t ||
    String(x.id || "").toLowerCase() === t ||
    String(x.id || "").replace(/^\^/, "").toLowerCase() === t);
  return hit || null;
}

function syncAddRule() {
  const box = $("#new-item");
  // Enabled on a pick, or on text the item table matches exactly.
  const hit = box.dataset.id ? null : exactMatch(box.value);
  if (hit) { PICKED = hit; box.dataset.id = hit.id; }
  const off = !box.dataset.id;
  $("#add-rule").disabled = off;
  $("#add-rule").title = box.dataset.id
    ? "add a rule for " + box.dataset.id
    : "pick an item from the list first; a typed name is only accepted when it " +
      "matches an item exactly";
  // U28: a `title` on a disabled button reaches nobody, because a disabled
  // button cannot be focused or hovered usefully. The reason is a sentence.
  const why = $("#add-rule-why");
  if (why) {
    why.textContent = off
      ? (box.value.trim()
          ? "add a rule is off until the name above matches an item exactly. " +
            "Pick one from the list."
          : "add a rule is off until an item is named above.")
      : "";
  }
}

$("#add-rule").onclick = async () => {
  const box = $("#new-item");
  let id = (box.dataset.id || "").trim();
  const typed = box.value.trim();
  // What the picker or the table said this id is, so the row can show the name
  // straight away rather than the id twice over (U8).
  let resolved = PICKED && PICKED.id === id ? PICKED : null;
  if (!id) {
    if (!typed) return;
    // Typed, never picked. One question to the table, and only an exact answer
    // counts: a near miss is a different item, and a miss is not an id at all.
    let res;
    try {
      res = await api("/api/items?q=" + encodeURIComponent(typed) + "&n=1");
    } catch (e) { toast(e.message, "bad"); return; }
    const hit = (res.items || [])[0];
    const same = v => String(v || "").toLowerCase() === typed.toLowerCase();
    if (!hit || !(same(hit.name) || same(hit.id))) {
      // focus first: the box re-runs its search on focus, and that search is
      // what would wipe the note if it landed afterwards.
      box.focus();
      setAddNote("not added: " + ADD_NOTE_NO_MATCH, true);
      return;
    }
    id = hit.id;
    resolved = hit;
  }
  // U8: the row renders the name, not the raw id, and the new rule is a keep
  // with no amount: the select says keep, the sentence says "Keep (amount not
  // set)" and the validator's own line appears under the row within 300 ms.
  const rule = { item: id, priority: 100 };
  if (resolved) {
    rule._name = resolved.name;
    rule._bucket_label = resolved.bucket_label;
  }
  (S.config.item_rules = S.config.item_rules || []).push(rule);
  const at = S.config.item_rules.length - 1;
  box.value = ""; delete box.dataset.id;
  setAddNote(ADD_NOTE_IDLE);
  syncAddRule();
  focusAfter(`rule-${at}-mode`);
  revalidate();
};
/* A2: the drag-free "remove from everywhere". The one gesture on this page
   that had no pointer or keyboard alternative was dragging a category chip
   back onto the shelf; this is a select and a button, in the panel the
   proposal names, and it asks first like every other destructive click. */
$("#unroute-all-btn").onclick = () => {
  const sel = $("#unroute-all");
  if (!sel || !sel.value) return;
  focusAfter("unroute-all-btn");
  unrouteEverywhere(sel.value, $("#unroute-all-btn"));
};

$("#add-source-btn").onclick = () => {
  const v = $("#add-source").value;
  if (v) { (S.config.sources = S.config.sources || []).push(v); revalidate(); }
};

/* save / revert ------------------------------------------------------ */

/* U8: the issue list used to come only from the last `/api/bootstrap` or the
   last save, so a rule added a moment ago carried no message at all and the
   messages that were showing described a configuration that no longer existed.
   `POST /api/validate` (lane C) is the same validator with no side effect, so
   the page can ask it after every edit. Debounced at 300 ms, and an answer for
   a configuration that has since changed again is dropped. */
let VALIDATE_T = null;
let VALIDATE_SEQ = 0;
function scheduleValidate() {
  clearTimeout(VALIDATE_T);
  VALIDATE_T = setTimeout(runValidate, 300);
}
async function runValidate() {
  if (!S.config) return;
  const seq = ++VALIDATE_SEQ;
  const sent = JSON.stringify(S.config);
  let issues;
  try {
    const r = await post("/api/validate", { config: JSON.parse(sent) });
    issues = r.issues || [];
  } catch (e) {
    // The page keeps the issues it has rather than claiming the configuration
    // is clean because the server is gone (U12).
    if (!e.offline) toast(e.message, "bad");
    return;
  }
  if (seq !== VALIDATE_SEQ) return;            // a later edit is already out
  S.issues = issues;
  renderRules();
}

async function revalidate() { markDirty(); renderRules(); scheduleValidate(); }

/* The configuration file is read once at startup, so a file edited by hand
   outside the page is invisible until a restart, and pressing save first
   overwrites it (walkthrough C2). This asks the server to re-read it. The route
   is lane C's and may not exist yet, so a 404 says what to do instead. */
/* Lifted out of the click handler because the two-tab refusal on the Settings
   tab has to offer the same action: the save bar that holds this button is
   hidden there, so the Start over card renders its own "reload from disk" and
   both have to be the one code path. */
async function reloadConfigFromDisk() {
  try {
    const r = await post("/api/config/reload", {});
    const cfg = r.config || (r.bootstrap && r.bootstrap.config);
    if (cfg) {
      S.config = JSON.parse(JSON.stringify(cfg));
      S.saved = JSON.stringify(cfg);
      S.issues = r.issues || [];
    } else {
      await reboot();
    }
    S.plan = null;
    BAD_FIELDS.clear();
    clearConfigConflict();
    renderRules(); renderPlanTab();
    toast("the configuration file was re-read from disk", "good");
  } catch (e) {
    if (e.status === 404 || e.status === 405 || e.status === 501) {
      toast("this build cannot re-read the file yet; restart the sorter to " +
        "load a configuration you edited by hand", "");
    } else {
      toast(e.message, "bad");
    }
  }
}

$("#config-reload").onclick = async () => {
  const go = reloadConfigFromDisk;
  if (S.dirty) {
    confirmInline($("#config-reload"),
      "Throw away the unsaved changes on this page and re-read the file on " +
      "disk?", go);
    return;
  }
  go();
};

$("#save-config").onclick = async () => {
  // V4: a value the field rejected never reached the model, so the server would
  // have accepted the old one silently. Refused here, by name.
  if (BAD_FIELDS.size) {
    renderIssues();
    const first = BAD_FIELDS.values().next().value;
    reveal(first.node, "center");
    if (first.node.isConnected) first.node.focus();
    toast("not saved: " + first.message, "bad");
    return;
  }
  try {
    const r = await post("/api/config",
      { config: S.config, force: false, based_on: basedOn() });
    S.issues = r.issues;
    if (!r.saved) {
      renderIssues(); renderItemRules();
      toast("not saved: fix the errors above", "bad");
      return;
    }
    S.config = JSON.parse(JSON.stringify(r.config));
    S.saved = JSON.stringify(r.config);
    S.plan = null;
    // U4: the plan a tick was made about is retired by a configuration change.
    clearConfirm("the configuration changed");
    renderRules(); renderPlanTab();
    panelTell("configuration saved. The previous revision is kept as a .bak " +
      "beside it.");
    clearConfigConflict();
    toast("configuration saved; the previous revision is kept as .bak", "good");
  } catch (e) {
    if (isStaleConfig(e)) {
      configConflict(e.message);
      toast("not saved: the file changed under this page", "bad");
      return;
    }
    toast(e.message, "bad");
  }
};
/* How many top-level parts of the configuration differ from the saved copy.
   "discard 3 unsaved changes" is a fact; "discard unsaved changes" is a
   shrug, and this button used to be one click with neither (D17). */
function dirtyCount() {
  let saved;
  try { saved = JSON.parse(S.saved); } catch (e) { return 0; }
  const keys = new Set(Object.keys(saved).concat(Object.keys(S.config)));
  let n = 0;
  keys.forEach(k => {
    if (k === "updated") return;          // stamped by the server on every save
    if (JSON.stringify(saved[k]) !== JSON.stringify(S.config[k])) n++;
  });
  return n;
}

/* U33: "1 unsaved change(s)" is a count, and §3.10 asks that revert name what
   it discards. The top-level parts of the configuration have page names. */
const PART_NAME = {
  bucket_rules: "category routing", item_rules: "per-item rules",
  sources: "sources", options: "options", item_buckets: "item overrides",
  custom_buckets: "your categories", labels: "container names",
  never_move: "never move", never_buckets: "categories never moved"
};
function dirtyNames() {
  let saved;
  try { saved = JSON.parse(S.saved); } catch (e) { return []; }
  const keys = new Set(Object.keys(saved).concat(Object.keys(S.config)));
  const out = [];
  keys.forEach(k => {
    if (k === "updated") return;
    if (JSON.stringify(saved[k]) !== JSON.stringify(S.config[k])) {
      out.push(PART_NAME[k] || String(k).replace(/_/g, " "));
    }
  });
  return out;
}

$("#revert").onclick = () => {
  if (!S.dirty) { toast("nothing to revert: no unsaved changes"); return; }
  const names = dirtyNames();
  const what = names.length
    ? "the unsaved changes to " +
      (names.length === 1 ? names[0]
        : names.slice(0, -1).join(", ") + " and " + names[names.length - 1])
    : "the unsaved changes";
  confirmInline($("#revert"),
    `Discard ${what} and go back to the configuration on disk?`,
    () => {
      const before = snapshot();
      S.config = JSON.parse(S.saved);
      focusAfter("revert");
      revalidate();
      panelTell(names.length
        ? "reverted " + what.replace(/^the /, "") + "."
        : "reverted to the configuration on disk.");
      undoable("reverted to the last saved configuration", before);
    });
};

/* ================================================================ plan */
/* The planner names a container by its display name, which on a save whose
   chests are renamed reads exactly like a category, and prints `->` where the
   line above it prints an arrow (U24). One pass over any server sentence
   before it is shown. */
function contByLabel(name) {
  const c = (S.save ? S.save.containers : []).find(x => x.label === name);
  return c ? c.key : null;
}
/* The server's own sentence with its punctuation fixed and its keys left in
   place. This is what goes on the `title` of a "decided by" line: a key is
   always welcome in an attribute, which is the split `contPhrase` already
   makes against `contOpt`. */
function whySentence(text) {
  return words(String(text === null || text === undefined ? "" : text))
    .replace(/\s->\s/g, " → ");
}
function prettyWhy(text) {
  let out = whySentence(text);
  // A bare container key in a why-string becomes the container's *name*.
  // It used to become `name (key)`, and the "decided by" column is prose a
  // player is asked to read carefully before pressing Apply: `... -> System
  // and Non-Inventory +6 more (chest7)`, `keep 250 in Exosuit (suit)`. The
  // key stays where two containers in this save read the same name, which is
  // the one case it disambiguates anything -- the same rule `contOpt` uses
  // for every picker on the page.
  //
  // QA 3: the boundary was `(^|[\s→])key(?=$|[\s,.;])`, which cannot see a key
  // against a bracket -- and the planner brackets every qualifier it writes:
  // `... → Raw Resources (keep 500 in suit)`, `(overflows to chest2, chest7)`.
  // Two of the three places the column prints a key sat outside the list of
  // separators somebody remembered, so `suit` reached the screen. A word
  // boundary is the rule instead. `_` is in the class because container keys
  // carry one (`suit_cargo`, `ship1_cargo`), so `suit` must not match inside
  // `suit_cargo`.
  (S.save ? S.save.containers : []).forEach(c => {
    out = out.replace(
      new RegExp("(^|[^A-Za-z0-9_])" + c.key + "(?![A-Za-z0-9_])", "g"),
      (m, pre) => pre + contOpt(c));
  });
  return out;
}
function contLine(name) {
  const k = contByLabel(name);
  return k ? `${name} (${k})` : name;
}

function renderPlanTab() {
  const p = S.plan;
  const out = $("#plan-out"); out.innerHTML = "";
  const sum = $("#plan-summary"); sum.innerHTML = "";
  const tables = $("#plan-tables"); if (tables) tables.innerHTML = "";
  const det = $("#plan-detail");
  // The apply card also stays up while it is showing the result of an apply:
  // the plan is spent by then, and hiding the card would take the step list,
  // the backup path and the Undo button with it.
  const showing = $("#apply-out").childElementCount > 0;
  $("#apply-zone").classList.toggle("hidden", (!p || !p.rows.length) && !showing);
  det.classList.toggle("hidden", !p);
  // QA 3: applying consumes the plan, so the left column of this split has
  // nothing in it while the right one is showing the write report. Empty and
  // 650 px wide it read as a broken page; hidden, the report gets the width
  // and the step details stop wrapping.
  const left = $("#plan-left");
  const cols = $("#plan-cols");
  if (left) left.classList.toggle("hidden", !p);
  if (cols) cols.classList.toggle("solo", !p);
  renderBackupLine();
  if (!p) { $("#plan-meta").textContent = ""; return; }
  // The short form is the first 8 characters of the full SHA-256 (§3.9), and
  // it is the full digest that apply compares. Shown from `fingerprint_full`
  // when the server sends one, so what is on screen is provably a prefix of
  // what is checked rather than a second number that happens to agree.
  const short = (p.fingerprint_full || "").slice(0, 8) || p.fingerprint;
  const meta = $("#plan-meta");
  meta.textContent =
    `${p.file} · fingerprint ${short} · ` + plural(p.rows.length, "row", "rows");
  meta.title = p.fingerprint_full
    ? "the full digest apply compares: " + p.fingerprint_full
    : "this plan carries only the short fingerprint";

  // The summary sits above Apply; the per-row detail is folded away below it.
  // Reading 60 move rows should be a choice, not the price of reaching the
  // button.
  // E3: eight tiles at equal weight, two of them zero, gave "0 MERGES" the
  // same 20 px number and the same box as "10 MOVES". The facts that happened
  // are tiles, moves in amber as the lead and refused in red; the facts that
  // did not are one grey line, still printed, because a plan that computed no
  // refusals has to look different from one that never computed them (P3-9).
  const t = p.totals;
  const renames = p.rows.filter(r => r.op === "rename").length;
  const FACTS = [
    ["moves", t.moves, "lead"],
    ["units moved", t.units_moved, ""],
    ["containers touched", t.containers_touched, ""],
    ["new stacks", t.new_stacks, ""],
    ["containers repacked", t.tidies, ""],
    ["merges", t.merges, ""],
    ["renames", renames, ""],
    ["unknown ids", t.unknown, "warn"],
    ["refused", t.refusals, "bad"]
  ];
  const tally = el("div", "plan-tally");
  FACTS.filter(f => f[1]).slice(0, 6).forEach(([k, v, cls]) => {
    const d = el("div", "t " + (cls || ""));
    d.appendChild(el("b", null, num(v)));
    d.appendChild(el("span", null, k));
    tally.appendChild(d);
  });
  if (tally.childElementCount) sum.appendChild(tally);
  const zeros = FACTS.filter(f => !f[1]).map(f => `0 ${f[0]}`)
    .concat([plural(p.skips.length, "stack left alone", "stacks left alone")]);
  const zline = el("div", "zeros", zeros.join(" · ") + " ");
  zline.appendChild(helpButton(HELP.zeros, "the zeros", "plan-zeros-help"));
  sum.appendChild(zline);

  (p.notes || []).forEach(n => {
    const d = el("div", "issue " + (n.level === "error" ? "error" : "warning"));
    // E2: `^ASTEROID2: 367 left behind, the whole chain (suit) is full` is a
    // refusal identified by an id the player has never seen. The item table
    // knows it is Gold, so the resolved name leads, in bold, and the server's
    // sentence follows it word for word.
    const who = planNoteItem(n.message);
    if (who) d.appendChild(el("b", "where", who + " · "));
    else d.appendChild(el("b", "where", "Plan. "));
    // A save gate arrives here as a plan note too, and the version gate's
    // sentence is the *refusal's* wording -- which is only what happens with
    // strict_version_check on. `gateTail` is the one place that decides, so
    // the note reads the same as the gate line beside the Apply button
    // instead of promising a refusal that is not coming.
    const gate = saveGates().find(g => g.message === n.message);
    d.appendChild(el("span", "imsg",
      words(n.message + (gate ? gateTail(gate) : ""))));
    sum.appendChild(d);
  });

  // E5: five `.hint` lines at 12 px grey -- `Raw Resources (chest1): 29 -> 30
  // of 50 cells (+1)` -- were tabular data doing a bad impression of a
  // sentence. One table, tabular figures, and the change column is signed as
  // well as coloured so colour is not the only carrier.
  if (p.diff.length) tables.appendChild(diffTable(p.diff));
  // 3.3: every row this plan would write, with the `why` string in a column
  // of its own rather than in a rule under each of 13 cards.
  const writes = p.rows.filter(r => r.op === "move" || r.op === "merge");
  if (writes.length) tables.appendChild(movesTable(writes, p));

  const moves = p.rows.filter(r => r.op === "move").length;
  const overflow = p.rows.filter(r => (r.chain_index || 0) > 0).length;
  $("#plan-detail-summary").textContent =
    "the detail: " + plural(moves, "move row", "move rows") +
    (overflow ? `, ${overflow} of them overflow into a second container` : "") +
    (renames ? ", " + plural(renames, "container", "containers") + " renamed" : "") +
    `, ${p.skips.length} left alone`;

  if (!p.rows.length) {
    tables.appendChild(el("p", "lede",
      "Nothing to do. Either everything is already where the rules want it, or no " +
      "rule matches what the sources hold. Open the detail to see which."));
  }

  group(out, "Moves", p.rows.filter(r => r.op === "move"), moveRow);
  group(out, "Merges", p.rows.filter(r => r.op === "merge"), mergeRow);
  group(out, "Tidy", p.rows.filter(r => r.op === "tidy"), tidyRow);
  group(out, "Renamed", p.rows.filter(r => r.op === "rename"), renameRow);

  const refusals = p.skips.filter(s => s.op === "refuse");
  const skips = p.skips.filter(s => s.op !== "refuse");
  if (refusals.length) group(out, "Refused", refusals, skipRow);
  if (skips.length) {
    const d = el("details", "fold");
    d.appendChild(fk(el("summary", null,
      plural(skips.length, "item", "items") + " left alone, and why"),
      "plan-skips-fold"));
    skips.forEach(s => d.appendChild(skipRow(s)));
    out.appendChild(d);
  }
  if (p.idle_rules.length) {
    const d = el("details", "fold");
    d.appendChild(fk(el("summary", null,
      plural(p.idle_rules.length, "item rule", "item rules") +
      " matched nothing this run"), "plan-idle-fold"));
    p.idle_rules.forEach(r => d.appendChild(el("p", "hint",
      `rule ${r.i + 1} on ${itemName(r.item) || showId(r.item)}: ${r.note}. ` +
      `A typo and an item you simply do not ` +
      `hold look identical until you see them side by side.`)));
    out.appendChild(d);
  }

  if (p.diff.length) {
    // The per-container totals are the table in the summary above; these cards
    // are the per-item detail behind it, so the 24-word lede they shared with
    // it is gone from here.
    out.appendChild(Object.assign(el("h2"),
      { textContent: "What changes inside each container" }));
    const g = el("div", "diffs");
    p.diff.forEach(d => g.appendChild(diffCard(d)));
    out.appendChild(g);
  }
  renderGates();
  renderRail();
}

/* E2: the display name for the first id a plan note quotes, so the sentence
   can be led by the word the table above it uses. Nothing is rewritten: the
   name goes *in front of* the server's sentence, which follows verbatim. */
function planNoteItem(message) {
  const m = String(message || "").match(/\^?[A-Z0-9_]{3,}(?:#\d+)?/);
  if (!m) return null;
  return itemName(m[0]);
}

/* E5: container, before, arrow, after, change, fill after, capacity. */
function diffTable(diff) {
  const card = el("div", "card");
  const hd = el("div", "hd");
  hd.appendChild(el("h2", null, "What each container ends up holding"));
  hd.appendChild(el("span", "hint",
    "computed from the transformed document, not predicted"));
  card.appendChild(hd);
  const bd = el("div", "bd flush");
  const tb = el("table", "plan-diff");
  const head = el("tr");
  ["container", "before", "", "after", "change", "fill after"].forEach((h, i) => {
    const th = el("th", i >= 1 && i !== 5 ? "r" : null, h);
    if (!h) th.setAttribute("aria-label", "becomes");
    head.appendChild(th);
  });
  const thead = el("thead"); thead.appendChild(head); tb.appendChild(thead);
  const body = el("tbody");
  diff.forEach(d => {
    const tr = el("tr");
    const nameCell = el("td");
    nameCell.appendChild(el("span", null, d.container));
    const k = contByLabel(d.container);
    if (k) {
      const ks2 = keySpan(k);
      if (ks2) nameCell.appendChild(ks2);
      keyTitle(nameCell, k, d.container);
    }
    tr.appendChild(nameCell);
    tr.appendChild(el("td", "r tnum", num(d.before_used)));
    const ar = el("td", "c"); ar.textContent = "→";
    ar.setAttribute("aria-hidden", "true");
    tr.appendChild(ar);
    const after = el("td", "r tnum");
    after.appendChild(el("b", null, num(d.after_used)));
    tr.appendChild(after);
    const delta = d.after_used - d.before_used;
    // `color-not-only`: the sign is in the text, the colour only repeats it.
    const ch = el("td", "r tnum " + (delta > 0 ? "up" : delta < 0 ? "down" : ""),
      (delta > 0 ? "+" : "") + num(delta));
    tr.appendChild(ch);
    const fillCell = el("td");
    const wrap = el("div", "row");
    const bar = el("div", "fill wide");
    const i2 = el("i");
    const pct = d.valid ? Math.min(100, Math.round(100 * d.after_used / d.valid)) : 0;
    i2.style.width = pct + "%";
    i2.style.background = d.after_used >= d.valid ? "var(--danger)" : "var(--ok)";
    bar.appendChild(i2);
    wrap.appendChild(bar);
    wrap.appendChild(el("span", "hint tnum",
      `of ${num(d.valid)}` + (d.after_used >= d.valid ? " · full" : "")));
    fillCell.appendChild(wrap);
    tr.appendChild(fillCell);
    body.appendChild(tr);
  });
  tb.appendChild(body);
  bd.appendChild(tb);
  card.appendChild(bd);
  return card;
}

/* One row per write, the quantity first because that is the number a player
   checks, and `Decided by` carrying the planner's own `why` string. */
function movesTable(rows, p) {
  const card = el("div", "card");
  const hd = el("div", "hd");
  hd.appendChild(el("h2", null, "Every row this plan would write"));
  hd.appendChild(el("span", "hint", plural(p.rows.length, "row", "rows")));
  card.appendChild(hd);
  const bd = el("div", "bd flush");
  const tb = el("table", "plan-diff moves");
  const head = el("tr");
  [["qty", "r"], ["item", ""], ["from", ""], ["to", ""],
   ["decided by", ""]].forEach(([h, cls]) => {
    head.appendChild(el("th", cls, h));
  });
  const thead = el("thead"); thead.appendChild(head); tb.appendChild(thead);
  const body = el("tbody");
  rows.forEach(r => {
    const tr = el("tr");
    tr.appendChild(el("td", "r tnum", num(r.moved === undefined ? r.total : r.moved)));
    const it = el("td");
    it.appendChild(r.name
      ? withId(el("b", null, r.name), r.id)
      : withId(el("code", null, showId(r.id)), r.id));
    if (r.partial) {
      const pt = el("span", "pill warn");
      pt.appendChild(el("span", null, `partial ${r.moved} of ${r.wanted}`));
      pt.title = "the destination filled before the whole amount could move";
      it.appendChild(pt);
    }
    tr.appendChild(it);
    tr.appendChild(el("td", "hint", r.container || ""));
    tr.appendChild(el("td", null, r.dst_container || r.container || ""));
    const why = el("td", "hint whycell");
    // The visible line names containers the way every picker does; the line's
    // own `title` is the planner's sentence with its keys, for anyone who
    // wants the key that the rule file is written in.
    (r.why || []).forEach(x => {
      const d = el("div", null, prettyWhy(x));
      d.title = whySentence(x);
      why.appendChild(d);
    });
    (r.limits || []).forEach(x => {
      const d = el("div", null, "limit: " + prettyWhy(x));
      d.title = "limit: " + whySentence(x);
      why.appendChild(d);
    });
    tr.appendChild(why);
    body.appendChild(tr);
  });
  tb.appendChild(body);
  bd.appendChild(tb);
  card.appendChild(bd);
  return card;
}

function renameRow(r) {
  const d = el("div", "prow");
  const head = el("div", "ph");
  head.appendChild(el("b", null, r.before || "(unnamed)"));
  head.appendChild(el("span", "arrow", "\u2192"));
  head.appendChild(el("b", null, r.after));
  head.appendChild(el("span", "hint", `${r.length}/40 characters`));
  d.appendChild(head);
  d.appendChild(el("div", "why",
    r.was_default ? "the game had never been given a name for this one"
                  : "replacing a name that was typed by hand"));
  return d;
}

function group(parent, title, rows, fn) {
  if (!rows.length) return;
  const box = el("div", "plan-group");
  box.appendChild(Object.assign(el("h2"), { textContent: `${title} (${rows.length})` }));
  rows.forEach(r => box.appendChild(fn(r)));
  parent.appendChild(box);
}

function whyBlock(r) {
  const w = el("div", "why");
  (r.why || []).forEach(x => w.appendChild(el("div", null, prettyWhy(x))));
  (r.limits || []).forEach(x => w.appendChild(el("div", null,
    "limit: " + prettyWhy(x))));
  return w;
}

function moveRow(r) {
  const n = el("div", "prow move");
  const l = el("div", "pline");
  l.appendChild(el("span", "pop", "move"));
  l.appendChild(el("span", "pqty", num(r.moved)));
  l.appendChild(withId(el("span", "pitem", r.name || showId(r.id)), r.id));
  l.appendChild(el("span", "parrow", "from"));
  l.appendChild(el("span", null, r.container));
  l.appendChild(el("span", "parrow", "→"));
  l.appendChild(el("span", null, r.dst_container));
  if (r.partial) {
    const p = el("span", "tag full", `partial ${r.moved} of ${r.wanted}`);
    l.appendChild(p);
  }
  n.appendChild(l);
  const d = el("div", "pdetail");
  r.merges.forEach(m => d.appendChild(el("div", null,
    `merge ${m.add} into the stack at cell ${m.cell[0]},${m.cell[1]} → ${m.after} / ${m.max}`)));
  r.new_stacks.forEach(s => d.appendChild(el("div", null,
    `new stack of ${s.amount} at cell ${s.cell[0]},${s.cell[1]} (cap ${s.max})`)));
  d.appendChild(el("div", null, r.source_removed
    ? `the source stack at cell ${r.cell[0]},${r.cell[1]} is emptied and removed`
    : `the source stack at cell ${r.cell[0]},${r.cell[1]} drops ${r.amount} → ${r.source_after}`));
  n.appendChild(d);
  n.appendChild(whyBlock(r));
  return n;
}

function mergeRow(r) {
  const n = el("div", "prow merge");
  const l = el("div", "pline");
  l.appendChild(el("span", "pop", "merge"));
  l.appendChild(withId(el("span", "pitem", r.name || showId(r.id)), r.id));
  l.appendChild(el("span", "parrow", "in"));
  l.appendChild(el("span", null, r.container));
  l.appendChild(el("span", "pqty", num(r.total) + " total"));
  n.appendChild(l);
  const d = el("div", "pdetail");
  r.stacks.forEach(s => d.appendChild(el("div", null,
    `cell ${s.cell[0]},${s.cell[1]}: ${s.before} → ${s.after} / ${s.max}` +
    (s.after === 0 ? "  (removed)" : ""))));
  if (r.removed.length) d.appendChild(el("div", null,
    plural(r.removed.length, "stack", "stacks") + " removed, freeing " +
    plural(r.removed.length, "cell", "cells")));
  n.appendChild(d);
  n.appendChild(whyBlock(r));
  return n;
}

function tidyRow(r) {
  const n = el("div", "prow tidy");
  const l = el("div", "pline");
  l.appendChild(el("span", "pop", "tidy"));
  l.appendChild(el("span", "pitem", r.container));
  l.appendChild(el("span", "pqty", `${r.placements.length} of ${r.count} repositioned`));
  if (r.pinned) l.appendChild(el("span", "tag", `${r.pinned} pinned`));
  n.appendChild(l);
  const d = el("details", "fold");
  const sm = el("summary", null, "cell by cell");
  sm.setAttribute("aria-label", "cell by cell, for " + (r.container || "this container"));
  fk(sm, "tidy-" + (r.container || "") + "-cells");
  d.appendChild(sm);
  // U29: the cut used to be silent, unlike the one on the diff card below.
  if (r.placements.length > 400) {
    d.appendChild(el("div", "pdetail",
      `showing the first 400 of ${r.placements.length} placements`));
  }
  r.placements.slice(0, 400).forEach(p => d.appendChild(el("div", "pdetail",
    `${p.name || p.id}: ${p.from[0]},${p.from[1]} → ${p.to[0]},${p.to[1]}`)));
  n.appendChild(d);
  n.appendChild(whyBlock(r));
  return n;
}

function skipRow(s) {
  const n = el("div", "prow " + (s.op === "refuse" ? "refuse" : "skip"));
  const l = el("div", "pline");
  l.appendChild(el("span", "pop", s.op));
  if (s.name || s.id) {
    // The name, and the id only where the item table has no name for it --
    // "35 items left alone, and why" was 59 raw ids, `B_STR_U_NETB` and
    // `FRIGATE_FUEL_1` among them, which is the complaint restated. An id
    // that *is* the label is in a code span, because then it is a spelling
    // rather than a word.
    l.appendChild(s.name
      ? withId(el("span", "pitem", s.name), s.id)
      : withId(el("code", "pitem", showId(s.id)), s.id));
  }
  if (s.container) { l.appendChild(el("span", "parrow", "in")); l.appendChild(el("span", null, s.container)); }
  if (s.amount) l.appendChild(el("span", "pqty", num(s.amount)));
  n.appendChild(l);
  n.appendChild(el("div", "pdetail", prettyWhy(s.reason)));
  if (s.why) n.appendChild(whyBlock(s));
  return n;
}

function diffCard(d) {
  const n = el("div", "diff");
  n.appendChild(el("h4", null, d.container));
  n.appendChild(el("div", "sub",
    `${d.before_used} → ${d.after_used} of ${d.valid} cells · ` +
    plural(d.changes.length, "item", "items") + " changed"));
  d.changes.slice(0, 60).forEach(c => {
    const up = c.after > c.before;
    const r = el("div", "dline " + (up ? "up" : "down"));
    r.appendChild(el("div", "n", c.name || showId(c.id)));
    r.appendChild(el("div", "a", num(c.before)));
    r.appendChild(el("div", "ar", "→"));
    r.appendChild(el("div", "b", num(c.after)));
    r.title = plural(c.before_stacks, "stack", "stacks") + " → " +
      plural(c.after_stacks, "stack", "stacks");
    n.appendChild(r);
  });
  if (d.changes.length > 60) n.appendChild(el("p", "hint", `+${d.changes.length - 60} more`));
  return n;
}

/* gates + apply ------------------------------------------------------ */

/* Every gate line gets a "what this means" link into the troubleshooting
   document. Each of the six anchors below is a `###` entry there, and every
   heading in that file is a sentence the code can actually say -- which
   `tests/test_docs.py` enforces on every run -- so a gate link cannot outlive
   the refusal it explains. `docs/SAFETY.md` used to be the target and its six
   anchors went when it was deleted, which is the rot this arrangement avoids:
   an anchor here is now the same string a person searches for. */
/* D4: a tool that binds to loopback should not send a player to the internet
   for its own safety copy, and those links 404 while the repository is private.
   The local route is preferred and probed once; GitHub is the fallback. */
const SAFETY_GITHUB =
  "https://github.com/altmank/NoMansSky-Inventory-Manager-Save-Editor/blob/main/docs/TROUBLESHOOTING.md";
const SAFETY_LOCAL = "/docs/TROUBLESHOOTING.md";
let SAFETY_URL = SAFETY_LOCAL;          // the local copy is the one to prefer
/* Whether this build serves the document itself. Resolved on the first click
   rather than at load: a probe at load time would put a failed request in the
   console of every session that does not have the route yet, and the answer is
   only needed when somebody follows a link. */
let DOCS_LOCAL = null;
async function openSafetyDoc(anchor) {
  if (DOCS_LOCAL === null) {
    try {
      const r = await fetch(SAFETY_LOCAL, { method: "GET" });
      DOCS_LOCAL = r.ok;
    } catch (e) { DOCS_LOCAL = false; }
    SAFETY_URL = DOCS_LOCAL ? SAFETY_LOCAL : SAFETY_GITHUB;
    renderGates();
  }
  window.open((DOCS_LOCAL ? SAFETY_LOCAL : SAFETY_GITHUB) + "#" + anchor,
              "_blank", "noreferrer");
}
/* One slug per gate, in gate order. Each is the anchor of a ### entry in
   docs/TROUBLESHOOTING.md, so the entry names the refusal the gate would
   raise: the game-running refusal, the read-only refusal, the fingerprint
   mismatch, a backup copy that does not match, the byte-for-byte round trip,
   and the eight metadata bytes. No quoted text in here: a test reads the
   string literals of this array straight out of the file. */
const GATE_DOC = [
  "game-nmsexe-is-running-as-pid--go-to-the-main-menu-in-the-game-then-tick-i-am-at-the-main-menu-not-in-a-loaded-save-and-try-again",
  "this-server-was-started-with---read-only-so-it-will-not-write-to-a-save-restart-without-that-flag-if-you-meant-to-apply",
  "plan-matches-what-you-saw-the-plan-computed-from-these-bytes-fingerprints--but-you-approved-",
  "backup-copy-of--does-not-match-the-original",
  "identity-round-trip-this-save-does-not-re-serialise-byte-for-byte-first-difference-at-offset-",
  "metadata-the-size-patch-would-touch-a-byte-outside-0x380x3f"
];
/* U19: six links all called "what this means" are six identical lines in a
   screen reader's link list. Each says which gate it explains. */
const GATE_NAME = [
  "what the game step means",
  "what read-only mode means",
  "what the plan fingerprint means",
  "what the backup taken before a write means",
  "what the byte-for-byte check means",
  "what the changed metadata bytes mean"
];

/* The Apply card says where the backup goes and that putting it back is one
   click, with the click being a jump to the Backups card below. */
function renderBackupLine() {
  const line = $("#backup-line"); if (!line) return;
  const file = S.save ? S.save.file : "this save";
  // The apply card's two ledes were 44 words saying the same thing as the
  // "Backup taken first" gate below them. One sentence, and where the click
  // that puts it back is.
  //
  // Finding 5: that used to be a button, and it was wrong twice. It scrolled
  // to `#backups-head`, which is in no document -- the only occurrence in the
  // repo was a stale CSS selector -- and it focused
  // `#backups-list button.btn.tiny`, which exists only once a backup has been
  // taken. Measured on a fresh backups folder: `scrollTop` 0 before the click
  // and 0 after. A no-op on the first apply, which is exactly when a player
  // reads this sentence. The card is in the same column and the section does
  // not scroll, so the sentence is worth keeping and the control is not.
  line.innerHTML =
    `Rewrites <code>${esc(file)}</code> and its <code>mf_</code> metadata. ` +
    `Putting it back is one click, in the Backups card below.`;
}

/* Whether a *gate* refuses, as opposed to the tick simply not being made.
   `renderGates` computes the same thing for the button; this records it so the
   rail can name the two states apart (X8). */
let APPLY_BLOCKED = false;
const applyBlocked = () => APPLY_BLOCKED;

function renderGates() {
  const box = $("#gates"); if (!box) return;
  box.innerHTML = "";
  const g = S.game || {};
  const menu = !!($("#menu-confirm") && $("#menu-confirm").checked);
  // E4: 120 words of rationale printed inline between the plan and the tick,
  // in the register of a commit message. Each gate is one affirmation now,
  // with its own sentence verbatim in the `?` beside it and the existing
  // "what this means" link into docs/TROUBLESHOOTING.md kept as it was. The
  // `GATE_DOC` anchors are untouched: this changes the visible label only.
  const rows = [
    [g.running ? menu : !!g.known,
      // QA 3 / review 5: the program knows one thing here -- whether NMS.exe
      // is in the process list -- and both labels claimed to know where the
      // player was in the game. "Game is running, in a loaded save" was the
      // label shown *before* the tick, so a player already on the main menu
      // was told they were in a loaded save, and after a successful apply it
      // read as a red cross above "Written." Both labels are now statements
      // about what the program has: a process, and a confirmation or not.
      g.running
        ? (menu
            ? `NMS.exe is running (pid ${(g.pids || []).join(", ")}), at the main menu as you confirmed.`
            : `NMS.exe is running (pid ${(g.pids || []).join(", ")}). If you are not on the main menu, go there, then tick the box below. Applying from a loaded save is refused.`)
        : (g.known ? "NMS.exe is not running."
                   : "The process list could not be read. Refused."),
      g.running ? (menu ? "Game is running, main menu confirmed"
                        : "Game is running, main menu not confirmed")
                : (g.known ? "Game is closed" : "Process list unreadable")],
    [!(S.boot && S.boot.read_only), (S.boot && S.boot.read_only)
      ? "Server is in --read-only mode. Refused."
      : "Server can write.",
      (S.boot && S.boot.read_only) ? "Read-only mode" : "Server can write"],
    // QA 3: this gate's label read `Plan 67abcb3f is fixed`. A digest is not a
    // player's word for anything and "is fixed" reads like a bug was repaired,
    // where what the gate asserts is that the printed plan belongs to the save
    // as it was read: apply re-plans from the exact bytes and refuses on any
    // difference. The fingerprint is a `title`, where the plan meta line
    // already shows it in full.
    [!!(S.plan && S.plan.rows.length),
      S.plan ? "Plan printed, " +
        plural(S.plan.rows.length, "row", "rows") + ". Apply re-computes it " +
        "from the exact bytes it is about to rewrite, and refuses on any difference."
             : "No plan yet.",
      S.plan ? "Plan pinned to the save as it was read" : "No plan yet",
      S.plan ? "the fingerprint apply compares: " +
        ((S.plan.fingerprint_full || S.plan.fingerprint) || "none") : ""],
    [true, `The save and its mf_ metadata are copied to a timestamped folder under ${S.boot ? S.boot.backup_root : ""} and hashed before anything is written.`,
      "Backup taken first"],
    [true, "Round trip is proved on this specific file first: decode, re-encode, compare byte for byte. Then encode, decode again, and compare the document.",
      "Round trip proved"],
    [true, "mf_ metadata is updated at 0x38 and 0x3C only. The timestamp, slot id, save name and summary are left exactly as the game wrote them.",
      "Only 8 metadata bytes change"]
  ];
  rows.forEach(([ok, txt, label, title], i) => {
    const d = el("div", "gate " + (ok ? "ok" : "bad"));
    const dot = el("span", "dot", ok ? "✓" : "✕");
    dot.title = ok ? "this gate is satisfied" : "this gate refuses right now";
    d.appendChild(dot);
    const t = el("span", "txt", label || txt);
    if (title) t.title = title;
    d.appendChild(t);
    d.appendChild(helpButton(txt, label || "", "gate-" + i + "-help"));
    if (GATE_DOC[i]) {
      const a = el("a", "gatedoc", "what this means");
      a.href = SAFETY_URL + "#" + GATE_DOC[i];
      a.target = "_blank"; a.rel = "noreferrer";
      a.setAttribute("aria-label", GATE_NAME[i]);
      a.title = GATE_NAME[i] + ", in docs/TROUBLESHOOTING.md" +
        (DOCS_LOCAL === false ? " on GitHub, which needs a connection"
                              : " served by the sorter itself");
      const anchor = GATE_DOC[i];
      a.onclick = ev => { ev.preventDefault(); openSafetyDoc(anchor); };
      fk(a, "gate-" + i + "-doc");
      d.appendChild(a);
    }
    box.appendChild(d);
  });
  // A `refuse` gate on the save itself -- an expedition, a version this build
  // was not verified on: a line here and a block on apply (lane C's
  // `save_view.gates`).
  const refusals = saveGates().filter(x => x.level === "refuse");
  refusals.forEach(x => {
    const d = el("div", "gate bad");
    const dot = el("span", "dot", "✕");
    dot.title = "this gate refuses right now";
    d.appendChild(dot);
    d.appendChild(el("span", "txt", words(x.message) + " Apply is refused."));
    box.appendChild(d);
  });
  // The version gate reads like the game-running one: a line either way, and
  // whether it refuses is the operator's `strict_version_check`, not the save.
  const strict = versionStrict();
  const versions = saveGates().filter(x => x.level === "warn" && x.where === "version");
  versions.forEach(x => {
    const d = el("div", "gate " + (strict ? "bad" : "ok"));
    const dot = el("span", "dot", strict ? "✕" : "✓");
    dot.title = strict ? "this gate refuses right now" : "this gate is satisfied";
    d.appendChild(dot);
    d.appendChild(el("span", "txt", words(x.message) + (strict
      ? " Refused."
      : " Check disabled by strict_version_check being off: the round-trip and " +
        "nothing-else-changed checks still run on this file.")));
    box.appendChild(d);
  });
  // The main-menu tick is only asked for while the game is up, and it is only
  // shown then: a box about where you are in a game that is not running is a
  // question with no answer.
  const menuBox = $("#menu-check");
  if (menuBox) menuBox.classList.toggle("hidden", !g.running);
  // The banner's running-game paragraph reads differently once the tick is
  // made, and the tick's `onchange` is this function.
  paintBanner();
  const blocked = !g.known || (S.boot && S.boot.read_only) ||
    !(S.plan && S.plan.rows.length) || refusals.length > 0 ||
    (strict && versions.length > 0);
  APPLY_BLOCKED = !!blocked;
  $("#apply").disabled = blocked || !$("#confirm").checked ||
    (!!g.running && !menu);
  // D2: `add a rule` says why it is off; this button, which is the only write
  // path in the product, did not.
  const why = $("#apply-why");
  if (why) {
    let sentence = "";
    if (refusals.length) sentence = "apply is off: " + words(refusals[0].message);
    else if (strict && versions.length) sentence = "apply is off: " + words(versions[0].message);
    else if (!g.known) sentence = "apply is off: the process list could not be read, so it cannot be told whether the game is running.";
    else if (S.boot && S.boot.read_only) sentence = "apply is off: this copy was started with --read-only.";
    else if (!(S.plan && S.plan.rows.length)) sentence = S.plan
      ? "apply is off: this plan has no rows to write."
      : "apply is off until a dry run has been read. Press run dry run above.";
    // QA 3: "the box above" named nothing while two boxes sat above the
    // button and the first of them was already ticked, and "until you are at
    // the main menu" told a player to be somewhere the process check cannot
    // see. One sentence for the running state, counting the boxes, which is
    // the thing that is actually missing.
    else if (g.running && (!$("#confirm").checked || !menu))
      sentence = "apply is off until both boxes are ticked.";
    else if (!$("#confirm").checked) sentence = "apply is off until the box above is ticked.";
    why.textContent = sentence;
  }
  const pg = $("#plan-gates");
  if (pg) {
    pg.innerHTML = "";
    saveGates().forEach(x => pg.appendChild(gateRow(x)));
  }
  renderRail();
}
$("#confirm").onchange = renderGates;
$("#menu-confirm").onchange = renderGates;

$("#run-plan").onclick = async () => {
  // X3 (review 3): disabling the element that has focus is what dropped it to
  // `<body>`; the button is re-enabled at the end but focus was already gone.
  // The successor is the tick, which is the next thing to do with a plan, and
  // the button itself when there is no plan to tick for.
  const runHadFocus = document.activeElement === $("#run-plan");
  $("#run-plan").disabled = true;
  // a fresh plan retires the last apply's result card
  $("#apply-out").innerHTML = "";
  // U4: and the acknowledgement, which was about the plan before this one.
  clearConfirm("this is a new plan");
  try {
    S.plan = await post("/api/plan", { file: S.save ? S.save.path : null });
    S.issues = S.plan.issues || S.issues;
    renderBanner(S.plan.game);
    renderPlanTab();
    // QA 3: this read `plan 67abcb3f: 12 rows, nothing written`. A toast has no
    // `title` to hide a digest in, and the fingerprint is on the plan's meta
    // line a few pixels away, so the toast says what happened instead.
    toast("plan printed: " +
      plural(S.plan.rows.length, "row", "rows") + ", nothing written", "good");
  } catch (e) {
    // V6: a toast that expires was the only statement that the dry run failed,
    // and the panel it concerns went blank. The sentence lands in the panel.
    const sum = $("#plan-summary");
    const d = el("div", "issue error");
    d.appendChild(el("b", "where", "Dry run failed. "));
    d.appendChild(el("span", "imsg", words(e.message) + " Nothing was written."));
    sum.insertBefore(d, sum.firstChild);
    toast(e.message, "bad");
  }
  $("#run-plan").disabled = false;
  if (runHadFocus) {
    const tick = $("#confirm");
    const to = (S.plan && S.plan.rows.length && tick &&
                !$("#apply-zone").classList.contains("hidden")) ? tick : $("#run-plan");
    if (to && to.isConnected) to.focus();
  }
};

$("#run-verify").onclick = async () => {
  try {
    const r = await post("/api/verify", { file: S.save ? S.save.path : null });
    $("#verify-out").innerHTML = "";
    $("#verify-out").appendChild(stepList(r.steps));
  } catch (e) {
    $("#verify-out").innerHTML =
      `<div class="issue error"><span class="imsg">${esc(words(e.message))}</span></div>`;
  }
};

/* The apply and restore steps come from `safety.py` verbatim, and they still
   carry `N row(s)` and an ASCII arrow (V7). The words are the server's; the
   plural and the glyph are punctuation, so they are fixed here, once. */
function stepText(t) {
  return words(String(t === null || t === undefined ? "" : t))
    .replace(/ -> /g, " → ")
    // "2 row(s)" and "9 changed path(s)" alike: the count, any words between
    // them, and the one word the "(s)" belongs to.
    .replace(/\b(\d[\d,]*)\s+((?:[A-Za-z_]+\s+)*)([A-Za-z_]+)\(s\)/g,
      (m, n, mid, word) => plural(parseInt(String(n).replace(/,/g, ""), 10),
        (mid || "") + word, (mid || "") + word + "s"));
}

function stepList(steps) {
  const box = el("div", "steps");
  steps.forEach(s => {
    const d = el("div", "step " + s.state);
    d.appendChild(el("span", "s", s.state === "ok" ? "✓" : s.state === "fail" ? "✕" : "·"));
    d.appendChild(el("span", "lbl", stepText(s.label)));
    // The detail is its own row under the label now, so an empty one would
    // be an empty line rather than an empty column.
    const det = stepText(s.detail);
    if (det) d.appendChild(el("span", "det", det));
    box.appendChild(d);
  });
  return box;
}

$("#apply").onclick = async () => {
  if (!S.plan) return;
  // X3: same cause as the dry run. A successful apply already moves focus to
  // "Undo this apply" (review two's V5); a refusal left it on `<body>` while
  // the panel filled with the refusal nobody's focus was near.
  const applyHadFocus = document.activeElement === $("#apply");
  $("#apply").disabled = true;
  const out = $("#apply-out"); out.innerHTML = "";
  try {
    // The full fingerprint when the plan carries one, the short one otherwise:
    // apply compares what it is about to write against the token it was given,
    // and the wider token is the one that also covers the rename rows.
    const p = S.plan;
    const r = await post("/api/apply",
      { file: S.save.path, fingerprint: p.fingerprint_full || p.fingerprint,
        // Sent every time, and false is a real answer: the server refuses a
        // running game without it, in the sentence that names the tick.
        at_main_menu: !!($("#menu-confirm") && $("#menu-confirm").checked) });
    out.appendChild(stepList(r.steps));
    const res = r.result;
    const d = el("div", "issue warning");
    d.innerHTML = `<span class="imsg">Written. Backup of the save and its ` +
      `<code>mf_</code> is at <code>${esc(res.backup)}</code>. The change takes ` +
      `effect when the game next loads this save.</span>`;
    out.appendChild(d);

    // Undo is the same restore action, aimed at the folder this apply just
    // made (P2-6). It is offered here because this is where the hand already
    // is; the Backups card below offers it for every other backup.
    const undoOut = el("div", "undo-out");
    const row = el("div", "row gap");
    const undo = el("button", "btn tiny", "Undo this apply");
    const man = res.manifest || {};
    const bk = {
      folder: res.backup,
      stamp: stampOfFolder(res.backup),
      save: (man.save && man.save.file) ||
            (S.save && S.save.file) || null,
      has_manifest: !!res.manifest, sha256_ok: true, size: null,
      created: man.created || null,
      // The save was re-read as the last step of the apply, and the server
      // says whether what is on disk is what this run wrote (`last_apply`).
      // Null rather than false when it cannot tell, because "cannot be
      // checked" must not grey out the Undo of an apply that just succeeded.
      current_matches: (r.save && r.save.last_apply) ? true : null
    };
    const ableUndo = restorability(bk);
    undo.disabled = !ableUndo.ok;
    undo.title = ableUndo.why;
    undo.onclick = () => confirmRestore(undo, bk, undoOut);
    fk(undo, "undo-this-apply");
    row.appendChild(undo);
    row.appendChild(el("span", "hint", ableUndo.ok
      ? "copies both files back out of that folder; from the main menu, or with the game closed"
      : ableUndo.why + ", so this is not offered"));
    out.appendChild(row);
    out.appendChild(undoOut);

    S.save = r.save; S.plan = null;
    clearConfirm("this plan has been applied");
    renderHeader(); renderSave(); renderPlanTab();
    toast("applied; backup at " + res.backup, "good");
    await loadBackups();
    await loadHealth();
    // V5: the button that was pressed is gone, and the one that matters now is
    // the way back. A keyboard user should not have to hunt for it.
    if (undo.isConnected) { reveal(undo, "center"); undo.focus(); }
  } catch (e) {
    const b = e.body || {};
    out.innerHTML = "";
    const d = el("div", "issue error");
    d.innerHTML = `<span><b>Refused.</b> ${esc(words(e.message))}<br>Nothing was written.</span>`;
    out.appendChild(d);
    if (b.steps) out.appendChild(stepList(b.steps));
    toast("refused: " + e.message, "bad");
    if (applyHadFocus) {
      // The refusal is the thing to read, so focus goes to it: it is the only
      // new content on the screen, and from there Tab reaches the steps and
      // the button again.
      d.tabIndex = -1;
      fk(d, "apply-refusal");
      reveal(d, "center");
      d.focus();
    }
  }
  renderGates();
};

/* ============================================================== items */
/* `rowOf` remembers the row behind every id that has ever been selected, so a
   selection made on page 1 still carries its original category after "show
   more" has loaded page 2 or the filters have moved on. Without it a bulk
   re-categorise silently skipped anything no longer on screen. */
/* C2: `limit` used to be 200 and "show 200 more" *appended*, so three presses
   took the document past 48,000 px with every row still in the DOM. The page
   is a window now -- the pager replaces it -- so the row count stops
   mattering and the table body is a fixed height whatever the filter says. */
const IB = { offset: 0, limit: 50, total: 0, rows: [], sel: new Set(),
             rowOf: new Map() };

function itemFilters() {
  return {
    q: $("#ib-q").value.trim(),
    bucket: $("#ib-bucket").value,
    kind: $("#ib-kind").value,
    only: $("#ib-only").value,
  };
}

async function loadItems(reset) {
  if (reset) { IB.offset = 0; IB.rows = []; }
  const f = itemFilters();
  // The window is what the server is asked for, so a page is a page: the old
  // `IB.rows.concat(...)` accumulation is what grew the document.
  const qs = new URLSearchParams({
    q: f.q, bucket: f.bucket, kind: f.kind, only: f.only,
    offset: IB.offset, limit: IB.limit,
  });
  const r = await api("/api/browse?" + qs.toString());
  S.browse = r;
  IB.total = r.total;
  IB.rows = r.items;
  S.buckets = r.buckets;          // keep the rest of the app in step
  renderItemFilters(r);
  renderItemTable();
  renderCustomBuckets();
}

/* A filter chip is a button, not a div with a handler: it toggles state, it is
   reachable by Tab, and `aria-pressed` says which way it is set. */
function chip(label, count, active, onclick, colour, custom, key) {
  const c = el("button", "fchip" + (active ? " on" : "") + (custom ? " custom" : ""));
  c.type = "button";
  // U2: the chip bar is rebuilt on every answer from /api/browse, so a chip
  // that was just pressed has to be findable again by key.
  if (key) fk(c, key);
  c.setAttribute("aria-pressed", active ? "true" : "false");
  if (colour) {
    const sw = el("span", "swatch"); sw.style.background = colour;
    c.appendChild(sw);
  }
  c.appendChild(el("span", null, label));
  if (count !== null && count !== undefined) c.appendChild(el("span", "count", num(count)));
  c.title = (active ? "showing " : "show only ") + label +
    (count === null || count === undefined ? "" : ` (${num(count)})`) +
    (custom ? ": a category you added" : "");
  c.onclick = onclick;
  return c;
}

function renderItemChips(r) {
  const f = itemFilters();
  const bb = $("#ib-bucket-chips"); bb.innerHTML = "";
  // "All" is a chip too, so clearing a filter is the same gesture as setting one.
  bb.appendChild(chip("All categories", Object.values(r.counts).reduce((a, b) => a + b, 0),
    !f.bucket, () => { $("#ib-bucket").value = ""; IB.offset = 0;
      focusAfter("fchip-bucket-all"); loadItems(true); },
    null, false, "fchip-bucket-all"));
  // Twenty category chips are three rows of chip bar, which is most of the
  // space the section has. Eight, then a button that says how many are
  // behind it -- and the one that is *set* is always among the eight, so the
  // current filter is never hidden behind the fold.
  const CHIP_SHOW = 8;
  const order = r.buckets.slice().sort((x, y) => {
    if ((f.bucket === x.key) !== (f.bucket === y.key)) return f.bucket === x.key ? -1 : 1;
    return (r.counts[y.key] || 0) - (r.counts[x.key] || 0);
  });
  const shown = IB.allChips ? order : order.slice(0, CHIP_SHOW);
  shown.forEach(b => bb.appendChild(chip(
    b.label, r.counts[b.key] || 0, f.bucket === b.key,
    () => { $("#ib-bucket").value = b.key === f.bucket ? "" : b.key; IB.offset = 0;
      focusAfter("fchip-bucket-" + b.key); loadItems(true); },
    bcol(b.key), isCustomBucket(b.key), "fchip-bucket-" + b.key)));
  if (order.length > CHIP_SHOW) {
    const more = el("button", "fchip");
    more.type = "button";
    more.setAttribute("aria-expanded", IB.allChips ? "true" : "false");
    more.textContent = IB.allChips
      ? "fewer" : "+" + (order.length - CHIP_SHOW) + " more";
    more.title = IB.allChips
      ? "show the eight biggest categories only"
      : "show every category, including the ones with nothing in them";
    fk(more, "fchip-more");
    more.onclick = () => {
      IB.allChips = !IB.allChips;
      focusAfter("fchip-more");
      withFocus(() => renderItemChips(r));
    };
    bb.appendChild(more);
  }

  const kb = $("#ib-kind-chips"); kb.innerHTML = "";
  kb.appendChild(chip("Any kind", null, !f.kind,
    () => { $("#ib-kind").value = ""; IB.offset = 0;
      focusAfter("fchip-kind-any"); loadItems(true); },
    null, false, "fchip-kind-any"));
  Object.keys(r.kinds).sort().forEach(k => kb.appendChild(chip(
    k || "(none)", r.kinds[k], f.kind === k,
    () => { $("#ib-kind").value = k === f.kind ? "" : k; IB.offset = 0;
      focusAfter("fchip-kind-" + k); loadItems(true); },
    null, false, "fchip-kind-" + (k || "none"))));
}

function renderItemFilters(r) {
  renderItemChips(r);
  const bsel = $("#ib-bucket");
  if (bsel.options.length !== r.buckets.length + 1) {
    bsel.innerHTML = "";
    const all = el("option"); all.value = ""; all.textContent = "all categories";
    bsel.appendChild(all);
    r.buckets.forEach(b => {
      const o = el("option"); o.value = b.key;
      o.textContent = `${b.label} (${r.counts[b.key] || 0})`;
      bsel.appendChild(o);
    });
  }
  const ksel = $("#ib-kind");
  if (!ksel.options.length) {
    const all = el("option"); all.value = ""; all.textContent = "all kinds";
    ksel.appendChild(all);
    Object.keys(r.kinds).sort().forEach(k => {
      const o = el("option"); o.value = k;
      o.textContent = `${k || "(none)"} (${r.kinds[k]})`;
      ksel.appendChild(o);
    });
  }
  const bulk = $("#ib-bulk-bucket");
  bulk.innerHTML = "";
  routableBuckets(r.buckets).forEach(b => {
    const o = el("option"); o.value = b.key; o.textContent = b.label;
    bulk.appendChild(o);
  });
  // C2: `1-50 of 5,133`, and two buttons that move the window. "N shown of M
  // matching" was a count of how much of the document had been grown.
  const from = IB.total ? IB.offset + 1 : 0;
  const to = Math.min(IB.offset + IB.rows.length, IB.total);
  $("#ib-count").textContent = IB.total
    ? `${num(from)}–${num(to)} of ${num(IB.total)}`
    : "nothing matches these filters";
  const prev = $("#ib-prev"), next = $("#ib-next");
  if (prev) {
    prev.disabled = IB.offset <= 0;
    prev.title = prev.disabled ? "this is the first page"
      : `the previous ${num(Math.min(IB.limit, IB.offset))} rows`;
  }
  if (next) {
    next.disabled = to >= IB.total;
    next.title = next.disabled ? "this is the last page"
      : `the next ${num(Math.min(IB.limit, IB.total - to))} rows`;
  }
  // §5 cut 31: the count of your own categories and of your overrides is the
  // fold's summary line, not a 28-word lede above an empty list.
  const sm = $("#nb-summary");
  if (sm) {
    const mine = (S.config.custom_buckets || []).length;
    sm.textContent = (mine
      ? plural(mine, "category of your own", "categories of your own")
      : "none yet · they behave exactly like the generated ones") +
      " · " + plural(Object.keys(S.config.item_buckets || {}).length,
                     "item override", "item overrides");
  }
}

function renderItemTable() {
  withFocus(() => renderItemGrid());
}

/* U5/U18: the grid is a `role="grid"` with a sticky header row, and each row is
   one tab stop rather than three. 200 rows were 400 stops between the filters
   and the two buttons under the table; they are 200 now, the checkbox and the
   category select are reached with the arrow keys from the row (the grid
   pattern), and Space on the row toggles its selection. Two skip links and a
   pager below the filters replaces the window rather than growing it. */
function renderItemGrid() {
  const box = $("#item-table"); box.innerHTML = "";
  box.setAttribute("role", "grid");
  box.setAttribute("aria-label", "items and their categories");
  box.setAttribute("aria-rowcount", String(IB.total || IB.rows.length));
  const head = el("div", "it-head");
  head.setAttribute("role", "row");
  // Every column that is not self-evident says what it is, in a legend under
  // the table as well as in a `title`, because a `title` on a div is
  // mouse-only.
  const IT_COLS = [
    ["", ""],
    ["item", "the name the game shows. A second line is the subtitle from the " +
             "item table, where there is one."],
    ["id", "the game's own id for it, stored in the save, and the only thing a " +
           "rule ever holds."],
    ["kind", "which of the game's own tables the item comes from: a substance, " +
             "a product or a piece of technology. It decides how it stacks."],
    ["category", "the category this item sorts into. Changing it here is your " +
                 "override; the generated categories underneath are never edited."],
    ["stack", "the stack-size multiplier for this item on this save's " +
              "difficulty, shown as ×N. Blank means the table has no multiplier " +
              "for it."],
    // D1: this cell prints the game's own classification token (BUILDINGPART,
    // CONSUMABLE), not a category of ours. The label and the sentence now say
    // what is in it; the category you can change is the one three columns left.
    ["game category", "the game's own classification string for this item, out " +
                      "of its own tables. The category it sorts into is the " +
                      "picker in this row."],
  ];
  // §5 cut 28: `#items-legend` printed the three column glosses as a 55-word
  // paragraph under the table *and* as a `title` on a `role="columnheader"`
  // div, which is mouse-only (A4). The three that need one carry a focusable
  // `?` holding the same sentence verbatim; the other four say what they are.
  const HELPED = ["kind", "stack", "game category"];
  IT_COLS.forEach(([h, tip]) => {
    const d = el("div", null, h);
    d.setAttribute("role", "columnheader");
    if (tip && HELPED.includes(h)) {
      d.appendChild(document.createTextNode(" "));
      d.appendChild(helpButton(tip, h, "itemcol-" + h.replace(/\s+/g, "-") + "-help"));
    } else if (tip) {
      d.title = tip;
      d.classList.add("has-tip");
    }
    head.appendChild(d);
  });
  box.appendChild(head);

  IB.rows.forEach((it, ri) => {
    IB.rowOf.set(it.id, it);
    const row = el("div", "it-row" + (it.overridden ? " overridden" : ""));
    row.setAttribute("role", "row");
    row.setAttribute("aria-rowindex", String(ri + 2));   // 1 is the header
    row.tabIndex = 0;
    row.dataset.item = it.id;
    fk(row, "item-" + it.id);
    const cell = (node, label) => {
      const c = el("div", "it-cell");
      c.setAttribute("role", "gridcell");
      if (label) c.setAttribute("aria-label", label);
      c.appendChild(node);
      return c;
    };

    const cb = el("input"); cb.type = "checkbox"; cb.checked = IB.sel.has(it.id);
    cb.id = "sel-" + it.id; cb.name = cb.id;
    cb.tabIndex = -1;
    cb.setAttribute("aria-label", "select " + (it.name || it.id));
    cb.onchange = () => {
      if (cb.checked) { IB.sel.add(it.id); IB.rowOf.set(it.id, it); }
      else IB.sel.delete(it.id);
      row.setAttribute("aria-selected", cb.checked ? "true" : "false");
      renderSelCount();
    };
    row.setAttribute("aria-selected", cb.checked ? "true" : "false");
    // SC 2.5.8: a 16x16 checkbox is a 16x16 pointer target. The label around
    // it is the target, and it is 24x24.
    const cbwrap = el("label", "cbhit");
    cbwrap.htmlFor = cb.id;
    cbwrap.appendChild(cb);
    row.appendChild(cell(cbwrap));

    const nm = el("div", "it-name");
    nm.appendChild(el("b", null, it.name || it.id));
    if (it.subtitle) nm.appendChild(el("div", "hint", it.subtitle));
    row.appendChild(cell(nm));

    const idc = el("div", "it-id"); idc.textContent = it.id;
    idc.title = "the game's own id for this item";
    if (it.procedural) {
      const pr = el("span", "tag", "proc");
      pr.title = "procedurally generated: the real id carries a #hash suffix";
      idc.appendChild(pr);
    }
    row.appendChild(cell(idc));

    row.appendChild(cell(el("div", "hint", it.kind || "")));

    // 4: a generated category hue is data, so it appears in an 8 px dot and a
    // chip swatch and nowhere else. It used to be this select's border colour,
    // where it competed with the semantic set -- a red border on a row meant
    // "Base Building: Decoration", not "wrong".
    const catcell = el("div", "catcell");
    const dot = el("span", "swatch"); dot.style.background = bcol(it.bucket);
    catcell.appendChild(dot);
    const sel = el("select", "it-bucket" + (isCustomBucket(it.bucket) ? " custom" : ""));
    routableBuckets(S.browse.buckets).forEach(b => {
      const o = el("option"); o.value = b.key; o.textContent = b.label;
      sel.appendChild(o);
    });
    // A bucket the browse list does not offer -- Unsorted, or one this config
    // has since removed -- left the control blank, which reads as "no
    // category" rather than the category it actually has.
    if (it.bucket && !Array.from(sel.options).some(o => o.value === it.bucket)) {
      const o = el("option");
      o.value = it.bucket;
      o.textContent = bktLabel(it.bucket);
      sel.appendChild(o);
    }
    sel.value = it.bucket;
    sel.setAttribute("aria-label", "category for " + (it.name || it.id));
    sel.title = bktLabel(it.bucket);
    sel.id = "bkt-" + it.id; sel.name = sel.id;
    sel.tabIndex = -1;
    fk(sel, "item-" + it.id + "-cat");
    // V1: an ArrowDown with focus in this select used to change the value and
    // write config.json, because a <select> owns the arrow keys and the change
    // goes straight to disk. The grid convention: while the cell is only
    // *focused* the control is inert to keys, and Enter or F2 puts it into edit
    // mode. Escape leaves edit mode and puts the value back.
    sel.dataset.edit = "off";
    sel.setAttribute("aria-keyshortcuts", "Enter F2");
    const editTitle = () => {
      sel.title = sel.dataset.edit === "on"
        ? bktLabel(sel.value) + ". Editing: the up and down arrows choose, " +
          "Enter keeps it, Escape puts it back."
        : bktLabel(sel.value) + ". Press Enter to change it.";
    };
    editTitle();
    const startEdit = () => {
      sel.dataset.edit = "on";
      sel.dataset.was = sel.value;
      sel.classList.add("editing");
      editTitle();
    };
    const endEdit = restore => {
      if (sel.dataset.edit !== "on") return;
      sel.dataset.edit = "off";
      sel.classList.remove("editing");
      if (restore && sel.dataset.was !== undefined) sel.value = sel.dataset.was;
      editTitle();
    };
    sel.onmousedown = startEdit;       // a mouse pick is an edit, and says so
    sel.onblur = () => endEdit(false);
    sel.addEventListener("keydown", e => {
      if (sel.dataset.edit === "on") {
        if (e.key === "Escape") { e.stopPropagation(); endEdit(true); sel.focus(); }
        else if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); endEdit(false); }
        return;                        // the select owns every other key now
      }
      if (e.key === "Enter" || e.key === "F2") {
        e.preventDefault(); e.stopPropagation(); startEdit(); return;
      }
      // Not editing: no key may reach the control, because every arrow, Home,
      // End, PageUp, PageDown and letter changes a <select>'s value. The grid's
      // own navigation keys are left to bubble to the row.
      const nav = ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown",
                   "Home", "End", "Tab"];
      if (!nav.includes(e.key)) e.preventDefault();
      if (["ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(e.key)) {
        e.preventDefault();            // stop the select; the row moves focus
      }
    });
    sel.onchange = () => {
      // Only an edit changes the category. A change that arrives without one is
      // a browser-initiated value shift, and it is put back.
      if (sel.dataset.edit !== "on" && sel.dataset.was !== undefined &&
          sel.value !== sel.dataset.was) {
        sel.value = sel.dataset.was;
        return;
      }
      focusAfter("item-" + it.id + "-cat");
      setOverride(it, sel.value, it.name || it.id);
    };
    catcell.appendChild(sel);
    row.appendChild(cell(catcell));

    const mult = el("div", "hint", it.multiplier === null || it.multiplier === undefined
      ? "" : "×" + it.multiplier);
    if (it.multiplier !== null && it.multiplier !== undefined) {
      mult.title = `stack-size multiplier ×${it.multiplier} on this save's difficulty`;
    }
    row.appendChild(cell(mult));

    const orig = el("div", "hint it-orig");
    orig.appendChild(el("span", null, it.category || ""));
    if (it.overridden) {
      const u = el("button", "linkbtn", `reset to ${it.original_bucket_label}`);
      u.title = `drop your override and let ${it.name || it.id} sort into ` +
        `${it.original_bucket_label} again`;
      // W16: every overridden row in the grid offers this, and two items
      // overridden out of the same category read as the same button.
      u.setAttribute("aria-label",
        `${it.name || showId(it.id)}: reset to ${it.original_bucket_label}`);
      u.tabIndex = -1;
      fk(u, "item-" + it.id + "-reset");
      u.onclick = () => {
        focusAfter("item-" + it.id);
        setOverride(it, null, it.name || it.id);
      };
      orig.appendChild(u);
    }
    row.appendChild(cell(orig));

    // The grid's keys: Space selects, Left and Right walk the row's controls,
    // Up and Down walk the rows, Home and End jump to the ends.
    row.addEventListener("keydown", e => {
      // While a cell is in edit mode the grid's keys belong to that control.
      if (document.activeElement &&
          document.activeElement.dataset &&
          document.activeElement.dataset.edit === "on") return;
      const inner = Array.from(row.querySelectorAll("input,select,button"));
      if (e.key === " " || e.key === "Spacebar") {
        if (e.target !== row) return;
        e.preventDefault();
        cb.checked = !cb.checked;
        cb.dispatchEvent(new Event("change"));
        return;
      }
      if (e.key === "ArrowRight") {
        e.preventDefault();
        const at = inner.indexOf(document.activeElement);
        const next = inner[at + 1] || inner[inner.length - 1];
        if (next) next.focus();
        return;
      }
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        const at = inner.indexOf(document.activeElement);
        if (at <= 0) row.focus(); else inner[at - 1].focus();
        return;
      }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (document.activeElement !== row) return;   // a select owns its arrows
        e.preventDefault();
        const rows = $$("#item-table .it-row");
        const at = rows.indexOf(row);
        const to = rows[at + (e.key === "ArrowDown" ? 1 : -1)];
        if (to) to.focus();
        return;
      }
      if (e.key === "Home" || e.key === "End") {
        if (document.activeElement !== row) return;
        e.preventDefault();
        const rows = $$("#item-table .it-row");
        const to = e.key === "Home" ? rows[0] : rows[rows.length - 1];
        if (to) to.focus();
      }
    });
    box.appendChild(row);
  });
  // §5 cut 29: the 53-word paragraph teaching this page's own keyboard model.
  // The behaviour is unchanged: `aria-keyshortcuts="Enter F2"` announces the
  // edit gesture and the `editing` outline shows it, so the prose was read by
  // the people who needed it least.
  if (!IB.rows.length) {
    box.appendChild(el("p", "hint", "nothing matches these filters"));
  }
  renderSelCount();
}

/* A selection that survives a page turn has to say where it lives, or the
   count reads as wrong the moment some of it leaves the window. */
function renderSelCount() {
  const n = IB.sel.size;
  const node = $("#ib-sel-count");
  // B6: the bar appears on a selection. It used to be permanently on, reading
  // "nothing selected" beside a disabled button, which is the whole of what
  // `progressive-disclosure` asks a bulk bar not to do.
  const bar = $("#ib-bulk");
  if (bar) bar.classList.toggle("hidden", !n);
  if (!n) {
    node.textContent = "nothing selected";
    node.title = "tick a row to select it; the selection survives a page turn";
  } else {
    const pages = new Set();
    let visible = 0;
    IB.rows.forEach((it, idx) => {
      if (IB.sel.has(it.id)) { visible++; pages.add(Math.floor(idx / IB.limit) + 1); }
    });
    const elsewhere = n - visible;
    node.textContent = `${num(n)} selected` +
      (elsewhere ? `, ${num(elsewhere)} not on this page` : "");
    node.title = elsewhere
      ? "the ones outside the current filter are still selected and still move"
      : "";
  }
  $("#ib-bulk-apply").disabled = !n;
}

function setOverride(it, bucket, name) {
  const before = snapshot();
  const ov = (S.config.item_buckets = S.config.item_buckets || {});
  if (bucket === null || bucket === it.original_bucket) delete ov[it.id];
  else ov[it.id] = bucket;
  // V1: this path writes to disk immediately, which is what the Items tab has
  // always done, so it leaves an undo behind rather than nothing at all.
  const who = name || it.name || it.id;
  saveAndReloadItems();
  undoable(bucket === null || bucket === it.original_bucket
    ? `${who} sorts into ${bktLabel(it.original_bucket)} again`
    : `${who} now sorts into ${bktLabel(bucket)}`, before, true);
}

async function saveAndReloadItems() {
  try {
    const r = await post("/api/config",
      { config: S.config, based_on: basedOn() });
    if (!r.saved) {
      toast((r.issues.find(i => i.level === "error") || {}).message || "refused", "bad");
      return;
    }
    S.config = JSON.parse(JSON.stringify(r.config));
    S.saved = JSON.stringify(r.config);
    S.issues = r.issues || [];
    S.plan = null;
    await loadItems(true);
    await selectSave(S.save.path);
    renderSave(); revalidate(); renderPlanTab();
  } catch (e) {
    // The Items tab writes to disk on every change, so it meets the two-tab
    // refusal as well -- and it has no save button to point at. The sentence
    // goes to the Configuration tab's conflict box, where "reload from disk"
    // is, and the toast says where to look.
    if (isStaleConfig(e)) {
      configConflict(e.message);
      toast("not saved: the configuration file changed under this page; the " +
        "Configuration tab says what to do", "bad");
      return;
    }
    toast(e.message, "bad");
  }
}

function renderCustomBuckets() {
  const box = $("#custom-buckets"); box.innerHTML = "";
  const list = (S.config.custom_buckets || []);
  const counts = (S.browse && S.browse.counts) || {};
  // X15: the fold's own summary line says "none yet", so this said it twice.
  if (!list.length) return;
  list.forEach((b, i) => {
    const row = el("div", "cb-row custom");
    const sw = el("span", "swatch"); sw.style.background = bcol(b.key);
    row.appendChild(sw);
    const lab = el("input");
    lab.id = "cb-label-" + b.key; lab.name = lab.id;
    lab.setAttribute("aria-label", "name of your category " + b.key);
    lab.value = b.label || b.key;
    lab.onchange = () => { b.label = lab.value.trim() || b.key; saveAndReloadItems(); };
    row.appendChild(lab);
    const ck = el("span", "ckey", b.key);
    ck.title = "the key written into your configuration file";
    row.appendChild(ck);
    row.appendChild(el("span", "hint",
      plural(counts[b.key] || 0, "item", "items")));
    const note = el("input");
    note.id = "cb-note-" + b.key; note.name = note.id;
    note.setAttribute("aria-label", "note on your category " + (b.label || b.key));
    note.value = b.note || ""; note.placeholder = "what goes in here";
    note.onchange = () => { b.note = note.value.trim(); saveAndReloadItems(); };
    row.appendChild(note);
    const del = el("button", "del", "\u00d7");
    del.type = "button";
    del.title = "remove this category; every item overridden into it goes back to " +
      "the category the game gives it";
    del.setAttribute("aria-label", "remove the category " + (b.label || b.key));
    // This one click used to take the routing rules and every override with it,
    // silently (D17). It now states the count first and leaves an undo behind.
    del.onclick = () => {
      const ov = S.config.item_buckets || {};
      const overrides = Object.keys(ov).filter(k => ov[k] === b.key);
      const routes = (S.config.bucket_rules || []).filter(r => r.bucket === b.key);
      confirmInline(row,
        `Remove the category ${b.label || b.key}? It takes ` +
        plural(overrides.length, "item override", "item overrides") + " and " +
        plural(routes.length, "routing rule", "routing rules") +
        ` with it. Those items go back to their generated category.`,
        () => {
          const before = snapshot();
          overrides.forEach(k => delete ov[k]);
          S.config.custom_buckets.splice(i, 1);
          S.config.bucket_rules = (S.config.bucket_rules || [])
            .filter(r => r.bucket !== b.key);
          saveAndReloadItems();
          undoable(`${b.label || b.key} removed with ` +
            plural(overrides.length, "override", "overrides") + " and " +
            plural(routes.length, "routing rule", "routing rules"), before, true);
        });
    };
    row.appendChild(del);
    box.appendChild(row);
  });
}

$("#nb-add").onclick = () => {
  const label = $("#nb-label").value.trim();
  if (!label) return;
  const key = label.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  if (!key) { toast("that name has no letters or digits in it", "bad"); return; }
  const all = (S.browse ? S.browse.buckets : []).map(b => b.key);
  if (all.includes(key)) { toast(`there is already a category keyed ${key}`, "bad"); return; }
  (S.config.custom_buckets = S.config.custom_buckets || []).push({ key, label });
  $("#nb-label").value = "";
  $("#nb-note").textContent = `added as ${key}`;
  saveAndReloadItems();
};

$("#ib-bulk-apply").onclick = () => {
  const bucket = $("#ib-bulk-bucket").value;
  const label = bktLabel(bucket);
  const ids = Array.from(IB.sel).filter(id => IB.rowOf.has(id));
  const unknown = IB.sel.size - ids.length;
  if (!ids.length) { toast("nothing in the selection can be moved", "bad"); return; }
  // Re-categorising a hundred items at a time was one click with no statement
  // of how many and no way back (D17).
  confirmInline($("#ib-bulk-apply"),
    `Change the category of ` + plural(ids.length, "item", "items") +
    ` to ${label}?` +
    (unknown ? " " + plural(unknown, "selected id", "selected ids") +
      " are not in the table and are skipped." : "") +
    ` Each becomes an override in your configuration. Nothing in the save moves.`,
    () => {
      const before = snapshot();
      const ov = (S.config.item_buckets = S.config.item_buckets || {});
      ids.forEach(id => {
        const it = IB.rowOf.get(id);
        if (bucket === it.original_bucket) delete ov[id];
        else ov[id] = bucket;
      });
      IB.sel.clear();
      saveAndReloadItems();
      undoable(plural(ids.length, "item", "items") +
        ` now sort into ${label}`, before, true);
    });
};
$("#ib-bulk-clear").onclick = () => { IB.sel.clear(); renderItemTable(); };
/* C2: the pager moves the window and re-renders in place. Focus stays on the
   button that was pressed, which is what lets it be pressed again; when the
   button disables itself at the end of the list, `focusRestore`'s fallback
   chain hands focus to its neighbour rather than to `<body>`.
   §5 cut 30: the two skip links went with the growing document -- there is
   nothing to skip over a fixed-height table body. */
function pageItems(dir) {
  const to = IB.offset + dir * IB.limit;
  IB.offset = Math.max(0, Math.min(to, Math.max(0, IB.total - 1)));
  focusAfter(dir < 0 ? "ib-prev" : "ib-next");
  withFocus(() => {});
  loadItems(false).then(() => {
    const want = dir < 0 ? "ib-prev" : "ib-next";
    if (!focusRestore(want)) focusRestore(dir < 0 ? "ib-next" : "ib-prev");
    const box = $("#item-table");
    if (box) box.scrollTop = 0;
  }).catch(e => toast(e.message, "bad"));
}
$("#ib-prev").onclick = () => pageItems(-1);
$("#ib-next").onclick = () => pageItems(1);
$("#ib-clear").onclick = () => {
  ["ib-q", "ib-bucket", "ib-kind", "ib-only"].forEach(id => { $("#" + id).value = ""; });
  IB.sel.clear();
  loadItems(true);
};
["ib-q", "ib-bucket", "ib-kind", "ib-only"].forEach(id => {
  const n = $("#" + id);
  const ev = n.tagName === "SELECT" ? "change" : "input";
  let t = null;
  n.addEventListener(ev, () => { clearTimeout(t); t = setTimeout(() => loadItems(true), 160); });
});

/* ===================================================== settings (P2-4) ===
   The tab is a view over `GET /api/settings`. The field list, the type of each
   control, the sentence under it and the default all come from `FIELDS` in
   `settings.py`; not one of those sentences is written here, so the text on the
   page and the text in the API cannot drift apart (§3.6). A field a
   command-line flag is holding for this run is shown and disabled, because a
   flag is never written back and a control that silently did nothing would be
   worse than no control. */
const SET = { data: null, edits: {}, loaded: false };

/* A label is not a sentence. These read better than the key does, and anything
   not in the map falls back to the key itself, so a field added to `FIELDS`
   still renders without an edit here. */
const SET_LABEL = {
  save_folder: "Save folder", backup_folder: "Backup folder",
  backup_keep: "Backups to keep", port: "Port",
  read_only: "Read-only",
  strict_version_check: "Strict save-version check",
  open_browser: "Open a browser at startup",
  // §3.4: the unit is a suffix on the field, so the label stops being a
  // sentence with a parenthesis in it.
  idle_exit_minutes: "Stop by itself after"
};
const setLabel = k => SET_LABEL[k] || String(k).replace(/_/g, " ");
/* The unit a number field carries, printed after the box rather than inside
   the label. */
const SET_UNIT = { backup_keep: "folders", idle_exit_minutes: "minutes" };
/* §5 cut 35: one sentence under each control. The server's own description is
   verbatim in that field's `?`, never reworded; these are the page's own short
   forms of the same fact. A key with no entry here prints the server's
   sentence instead, so a field `settings.py` adds still renders. */
const SET_UNDER = {
  save_folder: "The only game folder the sorter reads.",
  backup_folder: "One timestamped folder per apply.",
  backup_keep: "Older ones are pruned; the most recent never is.",
  port: "A change takes effect the next time the sorter starts.",
  read_only: "This copy can plan and browse but can never write.",
  strict_version_check:
    "Refuse a save whose version is outside the verified range.",
  open_browser: "A change takes effect the next time the sorter starts.",
  idle_exit_minutes: "0 keeps it running until you stop it."
};
/* §3.4 and `field-grouping`: three switches that can cost you play were
   scattered between a path and a port number. Any field `settings.py` adds
   that is not named here falls through to a fourth group, so the tab still
   renders without an edit. */
const SET_GROUPS = [
  ["Folders", "folder", ["save_folder", "backup_folder", "backup_keep"]],
  ["Safety", "shield", ["read_only", "strict_version_check"]],
  ["Server", "plug", ["port", "open_browser", "idle_exit_minutes"]]
];
/* The one sentence the seven "set by --X for this run, ..." repetitions
   became, printed once at the top of the tab. */
const HELD_NOTICE = "fields are held by a command-line flag for this run. A " +
  "flag wins over the file and is never written back.";
/* Which flag holds which field, so "set by a flag" can name it (U26). */
const SET_FLAG = {
  save_folder: "--folder", backup_folder: "--backups", port: "--port",
  read_only: "--read-only",
  strict_version_check: "--strict-version-check",
  open_browser: "--no-browser",
  idle_exit_minutes: "--idle-exit-minutes"
};
const setOverridden = k => ((SET.data && SET.data.overridden) || []).includes(k);
const setValue = k => (k in SET.edits ? SET.edits[k]
  : (SET.data && SET.data.settings ? SET.data.settings[k] : ""));

async function loadSettings() {
  try {
    SET.data = await api("/api/settings");
    SET.edits = {}; SET.loaded = true;
  } catch (e) {
    // U12: the seven controls used to be replaced wholesale by the string
    // "Failed to fetch". The sentence goes in the banner; whatever is on the
    // page stays on the page.
    settingsBanner([{ level: "error", where: "settings", message: e.message }], []);
    if (!$("#settings-fields").childElementCount) {
      $("#settings-fields").innerHTML =
        `<p class="hint">the settings could not be read yet.</p>`;
    }
    return;
  }
  renderSettings();
}

function renderSettings() {
  const d = SET.data;
  const box = $("#settings-fields"); box.innerHTML = "";
  if (!d) { box.appendChild(el("p", "hint", "loading…")); return; }
  $("#settings-path").textContent = d.path;
  // E6: the two paths that used to sit in the tab strip, beside the fields
  // that set them.
  const paths = $("#paths");
  if (paths && S.boot) {
    paths.innerHTML =
      `<div>config <code>${esc(S.boot.config_path)}</code></div>` +
      `<div>backups <code>${esc(S.boot.backup_root)}</code></div>`;
  }
  // §5 cut 35 / cut 36: one notice, once, instead of a 53-word lede and the
  // same 13-word sentence seven times.
  const held = $("#settings-held");
  if (held) {
    held.innerHTML = "";
    const over = ((d.overridden) || []).length;
    if (over) {
      const m = el("div", "msg warn");
      const g = icon("warn", 14);
      m.appendChild(g);
      m.appendChild(el("span", null, `${over} ${HELD_NOTICE}`));
      held.appendChild(m);
    }
  }
  // Grouped, with anything settings.py adds later falling into "Other".
  const named = new Set();
  SET_GROUPS.forEach(g => g[2].forEach(k => named.add(k)));
  const byKey = {};
  (d.fields || []).forEach(f => { byKey[f.key] = f; });
  const groups = SET_GROUPS.map(([title, ic, keys]) =>
    [title, ic, keys.filter(k => byKey[k])]);
  const rest = (d.fields || []).filter(f => !named.has(f.key)).map(f => f.key);
  if (rest.length) groups.push(["Other", "plug", rest]);
  groups.filter(g => g[2].length).forEach(([title, ic, keys]) => {
    const h = el("h3");
    h.appendChild(icon(ic, 16));
    h.appendChild(el("span", null, title));
    box.appendChild(h);
    const grid = el("div", "setgrid");
    keys.forEach(k => grid.appendChild(settingsField(byKey[k])));
    box.appendChild(grid);
  });
  // W15: the settings file exists and could not be parsed, so every value
  // above is the shipped default for this run -- including the port. It was
  // only ever said in the window the sorter started in, and the windowed
  // executable has no window.
  if (d.load_error) {
    settingsBanner([{ level: "error", where: "settings file",
                      message: d.load_error }], d.restart_required || []);
  }
  syncSettings();
}

function settingsField(f) {
  const over = setOverridden(f.key);
  const wrap = el("div", "set" +
    (over ? " locked" : ""));
  const id = "set-" + f.key;

  // §3.4: label, `?` and the lock pill on one line; the control beside them;
  // one sentence underneath. The server's full description is the `?`, never
  // reworded (§5, "not cut, deliberately").
  const head = el("div", "top");
  const lab = el("label", "name", setLabel(f.key));
  lab.setAttribute("for", id);
  head.appendChild(lab);
  head.appendChild(helpButton(words(f.description), setLabel(f.key),
    "set-" + f.key + "-help"));
  if (over) {
    // U26: five fields said "a command-line flag" and none of them said which,
    // so the flag could not be found and removed. The pill names it, and the
    // sentence it replaced is its title, verbatim.
    const flag = SET_FLAG[f.key];
    const pill = el("span", "pill flag");
    pill.appendChild(icon("lock", 13));
    pill.appendChild(el("span", null, flag || "a flag"));
    pill.title = flag
      ? `set by ${flag} for this run, given on the command line that started ` +
        `the sorter`
      : "set by a command-line flag for this run";
    pill.id = id + "-over";
    head.appendChild(pill);
  }

  let input;
  if (f.type === "bool") {
    input = el("input"); input.type = "checkbox";
    input.checked = !!setValue(f.key);
    input.onchange = () => setEdit(f.key, input.checked);
  } else if (f.type === "int") {
    // V2: `type="number"` with no `min`/`max` is what Chrome announces as
    // "minimum 0, maximum 0" -- the defect U16 named, in a new place. The real
    // bounds live in `settings.py` and are not in `/api/settings`, so the field
    // uses them when the API starts sending them and otherwise drops the
    // spinbutton role rather than announcing a range that is not true.
    const lo = (f.min === undefined || f.min === null) ? null : Number(f.min);
    const hi = (f.max === undefined || f.max === null) ? null : Number(f.max);
    input = el("input", "inum");
    if (lo !== null && hi !== null) {
      input.type = "number"; input.min = lo; input.max = hi;
    } else {
      input.type = "text"; input.inputMode = "numeric";
      input.autocomplete = "off"; input.spellcheck = false;
    }
    input.value = setValue(f.key);
    input.oninput = () => setEdit(f.key,
      input.value === "" ? "" : Number(input.value));
  } else {
    input = el("input", "setpath"); input.type = "text";
    input.spellcheck = false;
    const v = setValue(f.key);
    input.value = (v === null || v === undefined) ? "" : String(v);
    input.oninput = () => setEdit(f.key, input.value);
  }
  input.id = id; input.name = id;
  input.disabled = over;
  if (over) input.setAttribute("aria-describedby", id + "-over");
  // A path is wide, so it takes the line under the head; a checkbox and a
  // number sit beside the label, which is what B2 asked for.
  if (f.type === "path") {
    wrap.appendChild(head);
    wrap.appendChild(input);
  } else {
    head.appendChild(el("span", "tablespacer"));
    if (SET_UNIT[f.key]) {
      // §3.4: the unit is a suffix on the field (`20 folders`, `30 minutes`)
      // rather than a parenthesis in the label.
      const u = el("span", "unit");
      u.appendChild(input);
      u.appendChild(el("span", "suf", SET_UNIT[f.key]));
      input.setAttribute("aria-label",
        setLabel(f.key) + ", in " + SET_UNIT[f.key]);
      head.appendChild(u);
    } else {
      head.appendChild(input);
    }
    wrap.appendChild(head);
  }

  // §5 cut 35: one sentence under the control. The server's own description is
  // verbatim in the `?` above, so the page and `settings.py` still cannot
  // drift (§3.6); a field `settings.py` adds that has no short form here falls
  // back to printing the server's sentence, exactly as before.
  const under = el("p", "under", SET_UNDER[f.key] || words(f.description));
  wrap.appendChild(under);

  // The only path this build can check is the save folder, and the only place
  // it can check it is the game's usual folder: there is no route that probes
  // an arbitrary directory, and inventing one would be a second detection
  // strategy (Rule 1).
  if (f.key === "save_folder") {
    const b = el("button", "btn sm", "");
    b.id = "set-check-" + f.key;
    b.appendChild(icon("search", 16));
    b.appendChild(el("span", null, "Find my save folder"));
    b.title = "list the save folders found in the usual place";
    b.onclick = () => checkSaveFolders(f.key);
    wrap.appendChild(b);
  }

  if (!over && f.default !== undefined && f.default !== "" &&
      String(f.default) !== String(setValue(f.key))) {
    wrap.appendChild(el("p", "hint", "default: " + f.default));
  }

  const found = el("div", "setfound"); found.id = "setfound-" + f.key;
  wrap.appendChild(found);
  return wrap;
}

function setEdit(key, value) {
  const cur = SET.data.settings[key];
  if (value === cur) delete SET.edits[key]; else SET.edits[key] = value;
  syncSettings();
}

function syncSettings() {
  const n = Object.keys(SET.edits).length;
  $("#settings-save").disabled = !n;
  $("#settings-revert").disabled = !n;
  $("#settings-note").textContent = n
    ? plural(n, "change", "changes") + " not saved: " +
      Object.keys(SET.edits).map(setLabel).join(", ")
    : "";
}

async function checkSaveFolders(key) {
  const box = $("#setfound-" + key); box.innerHTML = "";
  try {
    const d = await api("/api/detect-saves");
    box.appendChild(el("p", "hint", d.root_exists
      ? `looked in ${d.root}`
      : `${d.root} does not exist on this machine`));
    if (!(d.folders || []).length) {
      box.appendChild(el("p", "hint",
        "no save folder found there. Type the folder holding save.hg yourself; " +
        "there is deliberately no search of the whole machine, because a wrong " +
        "guess sorts the wrong account."));
      return;
    }
    (d.folders || []).forEach(f => {
      const r = el("div", "foundrow");
      r.appendChild(el("code", null, f.path));
      r.appendChild(el("span", "hint",
        plural(f.save_count, "save", "saves") +
        (f.newest_save_label ? ` · newest ${f.newest_save_label}` : "")));
      // Nothing to press when a flag is holding the field: it would look like
      // an edit and the value would never be written back.
      if (setOverridden(key)) {
        r.appendChild(el("span", "hint",
          "a command-line flag is holding this field for this run"));
      } else {
        const b = el("button", "btn tiny", "use this one");
        // W16: one per candidate folder, all reading "use this one".
        b.setAttribute("aria-label", "use this one: " + f.path);
        b.title = "put " + f.path + " in the field above";
        b.onclick = () => {
          const inp = $("#set-" + key);
          inp.value = f.path; setEdit(key, f.path); inp.focus();
        };
        r.appendChild(b);
      }
      box.appendChild(r);
    });
  } catch (e) {
    box.innerHTML = `<div class="issue error"><span class="imsg">${esc(words(e.message))}</span></div>`;
  }
}

function settingsBanner(notes, restart) {
  const box = $("#settings-banner"); box.innerHTML = "";
  if ((restart || []).length) {
    const d = el("div", "issue warning");
    d.appendChild(el("b", "where", "On restart. "));
    d.appendChild(el("span", "imsg",
      `${restart.map(setLabel).join(" and ")} ` +
      `${restart.length === 1 ? "takes" : "take"} effect on restart. ` +
      `This run keeps the value it started with.`));
    box.appendChild(d);
  }
  (notes || []).forEach(n => {
    const d = el("div", "issue " + (n.level === "error" ? "error" : "warning"));
    d.appendChild(el("b", "where", (n.level === "error" ? "Error in " : "Warning in ") + whereLabel(n.where || "settings") + ". "));
    d.appendChild(el("span", "imsg", words(n.message)));
    box.appendChild(d);
  });
}

async function saveSettings() {
  const changed = Object.assign({}, SET.edits);
  const keys = Object.keys(changed);
  if (!keys.length) return;
  const folderMoved = keys.includes("save_folder");
  $("#settings-save").disabled = true;
  try {
    const r = await post("/api/settings", { settings: changed });
    SET.data = r; SET.edits = {};
    renderSettings();
    settingsBanner(r.notes, r.restart_required);
    toast("settings saved", "good");
    // A save-folder change drops the server's save cache and its minted plans,
    // so the page has to re-read the save list and stop offering a plan for a
    // file that may not be there any more.
    if (folderMoved) {
      if (r.first_run) {
        settingsBanner((r.notes || []).concat([{
          level: "error", where: "save_folder",
          message: "there is no save to sort in that folder. Reload this page " +
                   "to get the folder picker back." }]), r.restart_required);
      } else {
        await reboot();
      }
    }
    await loadBackups();
    await loadHealth();
  } catch (e) {
    const b = e.body || {};
    settingsBanner([{ level: "error", where: b.where || "settings",
                      message: e.message }], []);
    toast(e.message, "bad");
  }
  syncSettings();
}

$("#settings-save").onclick = saveSettings;
$("#settings-revert").onclick = () => {
  SET.edits = {}; renderSettings();
  $("#settings-banner").innerHTML = "";
};

/* Both anchors, one flow. The confirmation lands on whichever control was
   pressed, which is the point of `confirmInline`: the question sits where the
   hand is.

   X9 (review 3): they announced with the same name, so a screen-reader user
   listing buttons heard one destructive label twice with no way to choose.
   The Settings one keeps the plain name -- it is the control E6 says is the
   real one -- and the rail's quiet entry says where it is. This is review
   one's U19 (six identical "what this means" links) in a new place. */
$("#stop-sorter").onclick = () => stopSorter($("#stop-sorter"));
$("#stop-top").onclick = () => stopSorter($("#stop-top"));

/* ======================================================= health (P2-7) ===
   One definition list and one copy button. The issue template asks for this
   block, so the text form is the deliverable: `key: value`, one per line, no
   markup, and the same text is kept in a text box on the page for anyone whose
   browser will not hand out the clipboard. */
let HEALTH = null;

async function loadHealth() {
  try { HEALTH = await api("/api/health"); }
  catch (e) {
    $("#health").innerHTML =
      `<div class="issue error"><span class="imsg">${esc(words(e.message))}</span></div>`;
    if (HEALTH) renderHealth();          // the last block read stays readable
    return;
  }
  renderHealth();
}

function yesno(v) { return v ? "yes" : "no"; }

function gameLine(g) {
  g = g || {};
  if (g.running) return `running (pid ${(g.pids || []).join(", ") || "?"}) via ${g.method}`;
  if (g.known) return `not running, via ${g.method}`;
  return `unknown: ${g.error || "the process list could not be read"} (${g.method || "no method"})`;
}

function healthRows(h) {
  const rt = h.roundtrip || {};
  const c = h.data_counts || {};
  return [
    ["save folder", h.save_folder || "(none)"],
    ["saves", num(h.saves)],
    ["game", gameLine(h.game)],
    // the codec's own detail, with its ASCII arrow in the page's glyph (U24)
    ["round trip", `${rt.state || "unknown"}: ` +
      String(rt.detail || "").replace(/ -> /g, " → ")],
    ["data version", `${h.data_version || "(none)"} (game build)`],
    ["data counts", c.buckets === undefined ? "(not loaded)"
      : `${plural(c.buckets, "category", "categories")}, ${plural(c.ids, "id", "ids")}`],
    // U27: the issue template asks for the save's own Version, and the only
    // place it appeared was the header line the ellipsis ate.
    ["save version", S.save ? String(S.save.version) : "(no save loaded)"],
    // U27: the issue template asks for the save's own platform word, which is
    // not the host OS below it.
    ["save platform", h.save_platform || (S.save && S.save.platform) ||
      "(not reported for this save)"],
    ["config", `${h.config ? h.config.path : "?"} (version ${h.config ? h.config.version : "?"})`],
    ["backups", `${h.backups ? h.backups.root : "?"} (${h.backups ? h.backups.count : 0})`],
    ["log", h.log_path ||
      "not written to a file; it goes to the window the sorter started in " +
      "(the --log - flag)"],
    ["app version", h.app_version],
    ["python", h.python],
    ["platform", h.platform],
    ["settings", h.settings_path],
    ["first run", yesno(h.first_run)],
    ["degraded", h.degraded || "no"],
    ["read-only", yesno(h.read_only)],
    ["save-version check", h.strict_version_check
      ? "strict: a version outside the verified range is refused at apply"
      : "advisory: an unverified version is recorded and the apply continues"],
    // The live setting, so a bug report says whether the sorter that vanished
    // stopped itself. `undefined` is an older build with no such field.
    ["idle exit", h.idle_exit_minutes === undefined ? "(not reported)"
      : (h.idle_exit_minutes > 0
        ? `${plural(h.idle_exit_minutes, "minute", "minutes")}`
        : "off; it runs until you stop it")]
  ];
}

function renderHealth() {
  const box = $("#health"); box.innerHTML = "";
  if (!HEALTH) return;
  const rows = healthRows(HEALTH);
  const dl = el("dl", "hdl");
  rows.forEach(([k, v]) => {
    dl.appendChild(el("dt", null, k));
    dl.appendChild(el("dd", null, String(v === null || v === undefined ? "" : v)));
  });
  box.appendChild(dl);
  $("#health-text").value = healthText(rows);
}

function healthText(rows) {
  return (rows || healthRows(HEALTH))
    .map(([k, v]) => `${k}: ${v === null || v === undefined ? "" : v}`)
    .join("\n") + "\n";
}

$("#health-refresh").onclick = async () => {
  $("#health-note").textContent = "";
  await loadHealth();
  $("#health-note").textContent = "refreshed";
};

$("#health-copy").onclick = async () => {
  const ta = $("#health-text");
  if (!ta.value) return;
  let ok = true;
  try { await navigator.clipboard.writeText(ta.value); }
  catch (e) {
    // no clipboard permission, or no clipboard at all
    try { ta.focus(); ta.select(); ok = document.execCommand("copy"); }
    catch (e2) { ok = false; }
  }
  // Not announced only by a toast (§3.10): the panel it concerns says it too.
  $("#health-note").textContent = ok
    ? "copied as text; paste it into the issue"
    : "could not reach the clipboard; select the text box below and copy by hand";
  toast(ok ? "copied" : "could not copy", ok ? "good" : "bad");
};

/* ====================================================== backups (P2-6) ===
   The list, a restore button per row with the confirmation on the row, and the
   same action offered as Undo on the apply result. Restore is
   `POST /api/restore`, which is lane B and lane C's; while it is missing the
   button says so in one sentence rather than showing a 404. */
const BK = { root: null, list: null, error: null };

async function loadBackups() {
  try {
    const r = await api("/api/backups");
    BK.root = r.root; BK.list = r.backups || []; BK.error = null;
  } catch (e) { BK.list = null; BK.error = e.message; }
  withFocus(() => renderBackups());
}

function backupStamp(s) {
  const m = /^(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)(\d\d)$/.exec(String(s || ""));
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}`
           : "no timestamp in the folder name";
}

/* W17: the folder is named in local time and the manifest records UTC, so the
   card showed `2026-09-14 19:23` next to a file saying `2026-09-15T00:23:32Z`
   and nothing said they were the same moment. The manifest's own stamp, in
   this machine's time.

   None when the manifest has no `created`, which is an older backup or one
   whose manifest could not be read: the route adds the field only when it has
   one to add. */
const backupCreated = b =>
  (b && b.created) ? localTime(b.created) : null;

/* True / false / null, from `safety.list_backups`: does the save folder still
   hold what that apply wrote? False means restoring would discard whatever has
   been played since, and `POST /api/restore` refuses it -- so the button is
   disabled here rather than offered and then refused. Null is "cannot be
   told": an older manifest, or a save that is not there now. */
function restorability(b) {
  const m = b ? b.current_matches : undefined;
  if (m === false) {
    return { ok: false,
             why: "the save has been written since this backup was taken; " +
                  "restoring would discard that play" };
  }
  if (m === true) {
    return { ok: true,
             why: "the save folder still holds exactly what this apply wrote" };
  }
  return { ok: true,
           why: "whether the save changed since cannot be checked" };
}
function stampOfFolder(folder) {
  const base = String(folder || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop();
  const m = /^(\d{8}-\d{6})/.exec(base);
  return m ? m[1] : null;
}
function bytes(n) {
  if (n === null || n === undefined) return "size unknown";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(2)} MB`;
}

function renderBackups() {
  const root = $("#backups-root");
  root.textContent = BK.root || (S.boot ? S.boot.backup_root : "");
  root.title = "every copy the sorter has taken lives in this folder";
  const box = $("#backups-list"); box.innerHTML = "";
  if (BK.list === null) {
    box.innerHTML =
      `<div class="issue error"><span class="imsg">the backup list could not ` +
      `be read: ${esc(BK.error || "")}</span></div>`;
    return;
  }
  if (!BK.list.length) {
    box.appendChild(el("p", "lede",
      "No backups yet. One is taken automatically before every apply, and " +
      "nothing else ever writes here."));
    return;
  }
  BK.list.forEach(b => box.appendChild(backupRow(b)));
}

function backupRow(b) {
  const n = el("div", "prow backup");
  const l = el("div", "pline");
  l.appendChild(el("span", "pop", "backup"));
  l.appendChild(el("b", null, backupStamp(b.stamp)));
  l.appendChild(el("span", "pitem", b.save || "(no save file in this folder)"));
  l.appendChild(el("span", "pqty", bytes(b.size)));

  // A tick is never the only carrier of the meaning: the word is beside it,
  // and "unknown" is a third answer, not a failure.
  const ok = b.sha256_ok;
  const tag = el("span", "tag " + (ok === true ? "hashok" : ok === false ? "full" : ""),
    ok === true ? "✓ hash matches" : ok === false ? "✗ hash differs" : "hash unknown");
  tag.title = ok === true
    ? "the file in the backup still hashes to what its manifest recorded"
    : ok === false
      ? "the file in the backup no longer hashes to what its manifest recorded; restoring it would be refused"
      : "there is nothing to check this copy against";
  l.appendChild(tag);
  if (!b.has_manifest) {
    const m = el("span", "tag", "no manifest");
    m.title = "this folder was not written by a version that keeps a manifest; " +
              "retention leaves it alone";
    l.appendChild(m);
  }

  const btn = el("button", "btn tiny", "restore");
  btn.setAttribute("aria-label",
    `restore the backup taken ${backupStamp(b.stamp)}`);
  fk(btn, `backup-${b.stamp || b.folder}-restore`);
  const able = restorability(b);
  btn.disabled = !able.ok;
  btn.title = able.why;
  btn.onclick = () => confirmRestore(btn, b, $("#restore-out"));
  l.appendChild(btn);
  n.appendChild(l);
  const detail = el("div", "pdetail", b.folder);
  const created = backupCreated(b);
  if (created) {
    detail.appendChild(document.createTextNode(" · "));
    detail.appendChild(withTz(el("span", null, `taken ${created}`),
                              "the apply's own record of when it ran"));
  }
  if (!able.ok) {
    // Beside the greyed button, in words: a disabled control with the reason
    // only in a `title` reaches nobody (U30, D2).
    n.appendChild(el("div", "pdetail", able.why + "."));
  }
  n.appendChild(detail);
  return n;
}

/* The confirmation names the two files and the stamp, because "restore" on its
   own does not say what is about to be overwritten. */
function confirmRestore(anchor, b, out) {
  const save = b.save || "the save";
  const mf = b.save ? "mf_" + b.save : "its mf_ metadata";
  // The same rule as apply: a running game is fine from the main menu and
  // refused from a loaded save, and only the player can say which.
  const running = !!(S.game && S.game.running);
  confirmInline(anchor,
    `Copy ${save} and ${mf} from the backup taken ${backupStamp(b.stamp)} ` +
    `back over the save folder? From the main menu, or with the game closed.`,
    menu => doRestore(b, out, menu),
    running ? "I am at the main menu, not in a loaded save." : null,
    running ? "restore is off until the box above is ticked." : null);
}

async function doRestore(b, out, atMainMenu) {
  out = out || $("#restore-out");
  out.innerHTML = "";
  try {
    const r = await post("/api/restore",
      { folder: b.folder, at_main_menu: !!atMainMenu });
    if (r.steps) out.appendChild(stepList(r.steps));
    const d = el("div", "issue warning");
    d.appendChild(el("b", "where", "Restored. "));
    d.appendChild(el("span", "imsg",
      `${b.save || "the save"} and its mf_ metadata were copied back from the ` +
      `backup taken ${backupStamp(b.stamp)}. The change takes effect when the ` +
      `game next loads this save.`));
    out.appendChild(d);
    toast("restored from " + backupStamp(b.stamp), "good");
    S.plan = null;
    try { if (S.save) await selectSave(S.save.path); } catch (e) { /* shown below */ }
    renderHeader(); renderSave(); renderPlanTab();
    await loadBackups();
    await loadHealth();
    // V5: the backups card is where the next decision is, so focus lands on
    // its first row rather than on the body.
    const first = $("#backups-list button.btn.tiny");
    if (first) { reveal(first, "center"); first.focus(); }
  } catch (e) {
    const d = el("div", "issue " + (e.status === 404 ? "warning" : "error"));
    if (e.status === 404) {
      d.appendChild(el("b", "where", "Not available. "));
      d.appendChild(el("span", "imsg",
        "restore is not available in this build yet. The backup itself is " +
        "complete: copying the two files out of " + b.folder + " back over the " +
        "save folder by hand does exactly what this button will do."));
    } else {
      d.appendChild(el("b", "where", "Refused. "));
      d.appendChild(el("span", "imsg", words(e.message) + " Nothing was written."));
    }
    out.appendChild(d);
    if ((e.body || {}).steps) out.appendChild(stepList(e.body.steps));
    toast(e.status === 404 ? "restore is not in this build yet"
                           : "refused: " + e.message,
          e.status === 404 ? "" : "bad");
  }
}

$("#backups-refresh").onclick = () => loadBackups();

/* ================================================== start over (config) ===
   "we need a way to reset the config (don't lose my existing config)". The
   route existed and only the unreadable-config startup page reached it, so
   the one state it could not be reached from was the working one.

   Two controls, one card, and the second is what makes the first safe:
   starting over keeps the configuration under a name of its own, and every
   name kept that way is a row with a way back. Both go through
   `applyConfigSwap`, because a configuration that changed under the page
   retires the plan, the undo and the tick on the Apply card whichever button
   did it. */
const STARTOVER = { dir: null, list: null, error: null };

const START_OVER_QUESTION =
  "Start over from the shipped Ten chests layout? The configuration you have " +
  "now is kept as a named file beside config.json, the shipped layout " +
  "replaces it, and any dry run is discarded.";

async function loadKept() {
  try {
    const r = await api("/api/config/kept");
    STARTOVER.dir = r.dir; STARTOVER.list = r.kept || []; STARTOVER.error = null;
  } catch (e) { STARTOVER.list = null; STARTOVER.error = e.message; }
  withFocus(() => renderKept());
}

/* `20260916-142233` -> `2026-09-16 14:22`, the same reading `backupStamp`
   gives a backup folder: one spelling for a stamp in a file name. */
function keptStamp(s) {
  const m = /^(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)(\d\d)$/.exec(String(s || ""));
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}`
           : "no timestamp in the file name";
}

function renderKept() {
  const dir = $("#kept-dir");
  if (dir) {
    dir.textContent = STARTOVER.dir ||
      (S.boot ? S.boot.config_path.replace(/[\\/][^\\/]*$/, "") : "");
    dir.title = "the same folder config.json is in; nothing here is deleted";
  }
  const box = $("#kept-list");
  if (!box) return;
  box.innerHTML = "";
  if (STARTOVER.list === null) {
    const d = el("div", "issue error");
    d.appendChild(el("span", "imsg",
      "the kept configurations could not be listed: " + (STARTOVER.error || "")));
    box.appendChild(d);
    return;
  }
  if (!STARTOVER.list.length) {
    box.appendChild(el("p", "lede",
      "Nothing kept yet. Starting over keeps the configuration you have, and " +
      "so does putting a kept one back."));
    return;
  }
  STARTOVER.list.forEach(k => box.appendChild(keptRow(k)));
}

function keptRow(k) {
  const n = el("div", "prow");
  const l = el("div", "pline");
  l.appendChild(el("span", "pop", "kept"));
  l.appendChild(withTz(el("b", null, keptStamp(k.kept)),
                       "when this configuration was kept"));
  l.appendChild(el("span", "pitem", k.readable
    ? (k.name || "(the file names no layout)")
    : "could not be read"));
  l.appendChild(el("span", "pqty", k.updated
    ? "saved " + localTime(k.updated) : bytes(k.size)));

  const btn = el("button", "btn tiny", "Use this one");
  btn.setAttribute("aria-label",
    `use the configuration kept ${keptStamp(k.kept)}`);
  fk(btn, `kept-${k.file}-use`);
  if (!k.readable) {
    btn.disabled = true;
    btn.title = "this file is not valid JSON, so it cannot be put back";
  } else {
    btn.title = "make this the configuration in use";
    btn.onclick = () => confirmInline(btn,
      `Put the configuration kept ${keptStamp(k.kept)} back as config.json? ` +
      `The one you have now is kept beside it, and any dry run is discarded.`,
      () => useKept(k, btn));
  }
  l.appendChild(btn);
  n.appendChild(l);
  if (!k.readable) {
    // U30 / D2: the reason a control is greyed goes in words beside it, not
    // only in a `title` nobody can reach from a keyboard.
    n.appendChild(el("div", "pdetail",
      "This file is not valid JSON, so it cannot be put back. It is left " +
      "exactly where it is."));
  }
  n.appendChild(el("div", "pdetail", k.file));
  return n;
}

/* Everything a configuration arriving from the server retires. The same list
   `#save-config` and `#config-reload` keep, in one place, because a swap does
   all of it at once: the plan, the tick on the Apply card, the field-level
   errors, and the one-deep undo -- which cannot survive this, because what it
   would put back is a configuration that is no longer on disk. */
async function applyConfigSwap(r) {
  S.config = JSON.parse(JSON.stringify(r.config));
  S.saved = JSON.stringify(r.config);
  S.issues = r.issues || [];
  S.plan = null;
  BAD_FIELDS.clear();
  UNDO = null;
  clearConfigConflict();
  clearConfirm("the configuration changed");
  // Container names come out of `labels`, so a swap changes what the Save
  // section calls a chest. Re-read rather than re-rendered from stale rows.
  try { if (S.save) await selectSave(S.save.path); } catch (e) { /* below */ }
  renderRules(); renderSave(); renderPlanTab();
  syncUndo();
}

/* The refusal, in the card, with the control that answers it. `#config-conflict`
   and `#config-reload` both live in the save bar, which is hidden while the
   Settings tab is showing, so a two-tab 409 answered only there would be a
   sentence nobody read. */
function swapRefused(e, anchor) {
  const out = $("#start-over-out");
  if (out) {
    out.innerHTML = "";
    const d = el("div", "issue " + (isStaleConfig(e) ? "error" : "warning"));
    d.appendChild(el("b", "where", "Not done. "));
    d.appendChild(el("span", "imsg", words(e.message)));
    out.appendChild(d);
    if (isStaleConfig(e)) {
      const p = el("p", "hint");
      p.appendChild(el("span", null,
        "Nothing was written. Take the other copy's configuration and try " +
        "again: "));
      const b = el("button", "btn tiny", "reload from disk");
      fk(b, "start-over-reload");
      b.onclick = async () => {
        await reloadConfigFromDisk();
        await loadKept();
      };
      p.appendChild(b);
      out.appendChild(p);
    }
  }
  toast(e.message, "bad");
  if (anchor && anchor.isConnected) anchor.focus();
}

$("#start-over").onclick = () => {
  const out = $("#start-over-out");
  if (out) out.innerHTML = "";
  confirmInline($("#start-over"), START_OVER_QUESTION, async () => {
    try {
      const r = await post("/api/config/reset", { based_on: basedOn() });
      await applyConfigSwap(r);
      $("#start-over-note").textContent = r.kept
        ? "kept as " + r.kept : "";
      toast(r.kept
        ? "started over from the shipped layout; what you had is kept as " +
          r.kept
        : "started over from the shipped layout", "good");
    } catch (e) { swapRefused(e, $("#start-over")); }
    await loadKept();
  });
};

async function useKept(k, anchor) {
  const out = $("#start-over-out");
  if (out) out.innerHTML = "";
  try {
    const r = await post("/api/config/use",
      { file: k.file, based_on: basedOn() });
    await applyConfigSwap(r);
    $("#start-over-note").textContent = r.kept ? "kept as " + r.kept : "";
    toast(`now using the configuration kept ${keptStamp(k.kept)}` +
      (r.kept ? "; what you had is kept as " + r.kept : ""), "good");
  } catch (e) { swapRefused(e, anchor); }
  await loadKept();
}

/* the rail ---------------------------------------------------------------
   F1: the docs teach an ordered sequence ("The job, in six steps" heads
   `README.md`'s numbered list; `GUIDE.md` 2 is a taught sequence) and
   `showTab` had no ordering, no prerequisite, no next and arrow keys that
   wrapped around. The rail is numbered and ordered and carries the state of
   each step beside it, which is the `multi-step-progress` requirement that
   was missing. Nothing is gated: every step is always reachable.

   It is a real vertical tablist. Two of them, in fact -- the job and the
   reference set -- because a `tablist` holds tabs and the Apply sub-step
   between them is a button, not a destination. The roving tabindex spans
   both, so the whole rail is one stop in the Tab order and the arrow keys
   reach all six, which is what a screen-reader user will try. */
const TAB_NAMES = ["save", "categories", "rules", "plan", "items", "settings"];
/* Which section a validator `where` path belongs to, now that Configuration
   is two sections (F4). A per-item rule warning points at Rules; everything
   else about the configuration points at Categories. */
function sectionFor(where) {
  return /^item_rules/.test(String(where || "")) ? "rules" : "categories";
}

function showTab(name) {
  if (!TAB_NAMES.includes(name)) return;
  $$(".tab").forEach(x => {
    const on = x.dataset.tab === name;
    x.classList.toggle("active", on);
    x.setAttribute("aria-selected", on ? "true" : "false");
    x.tabIndex = on ? 0 : -1;
  });
  // Fire and forget, but never unhandled: with the server gone these reject,
  // and an unhandled rejection is a console error with no sentence in it.
  if (name === "items" && !S.browse) loadItems(true).catch(e => toast(e.message, "bad"));
  if (name === "settings") {
    if (!SET.loaded) loadSettings().catch(e => toast(e.message, "bad"));
    if (!HEALTH) loadHealth().catch(e => toast(e.message, "bad"));
    // Every time, not only the first: another copy of the sorter may have
    // kept one since, and the list is one small read.
    loadKept().catch(e => toast(e.message, "bad"));
  }
  TAB_NAMES.forEach(n => $("#tab-" + n).classList.toggle("hidden", n !== name));
  // A1: the configuration save bar is the footer of the two sections it
  // belongs to, and nothing else. It is in the flow of the stage, below the
  // scroll area, so it cannot sit over a control the way the old
  // `position:fixed` bar did for 5,933 px of scroll.
  const bar = $("#savebar");
  if (bar) bar.classList.toggle("hidden", name !== "categories" && name !== "rules");
  renderRail();
  // A name is only clipped once it has a width, and a panel that is `hidden`
  // has none.
  clipSweepSoon();
}

/* The state beside each step. This is what F2's self-destructing three-step
   card was for, and it cannot vanish: every value here is a count of
   something, so "nothing routed yet" is a state of the rail rather than a
   card that deletes itself the moment one rule exists. */
function renderRail() {
  const cfg = S.config || {};
  const routed = new Set((cfg.bucket_rules || []).map(r => r.bucket)).size;
  const ruleN = (cfg.item_rules || []).length;
  const moves = S.plan ? S.plan.rows.filter(r => r.op === "move").length : null;
  const state = {
    save: S.save
      ? plural(S.save.containers.filter(c => c.sortable).length,
               "container", "containers")
      : "no save",
    categories: routed ? routed + " routed" : "none routed",
    rules: ruleN ? String(ruleN) : "none",
    plan: S.plan ? plural(moves, "move", "moves") : "not run",
    items: S.browse ? num(IB.total) : (S.boot ? "" : ""),
    settings: ""
  };
  const done = {
    save: !!S.save, categories: routed > 0, rules: true,
    plan: !!(S.plan && S.plan.rows.length), items: false, settings: false
  };
  TAB_NAMES.forEach(n => {
    const b = $("#tabbtn-" + n);
    if (!b) return;
    const meta = b.querySelector(".meta");
    if (meta) meta.textContent = state[n] || "";
    b.classList.toggle("done", !!done[n] && !b.classList.contains("active"));
  });
  // Apply is the one step whose state is a readiness rather than a count.
  const ap = $("#apply-state");
  if (ap) {
    // X8 (review 3): the rail said "blocked" when all six gates passed and
    // only the tick was missing -- the one state that is entirely the user's
    // to clear. "Blocked" is reserved for a gate that refuses; the tick gets
    // its own words, and the rail's tooltip carries the reason either way.
    if (!(S.plan && S.plan.rows.length)) {
      ap.textContent = "no plan";
      ap.title = "run a dry run first";
    } else if (!applyBlocked() && $("#apply") && $("#apply").disabled) {
      // Either box: the acknowledgement, or the main-menu confirmation the
      // card asks for while the game is running.
      ap.textContent = "tick to enable";
      ap.title = ($("#apply-why") && $("#apply-why").textContent) ||
        "apply is off until the box beside it is ticked";
    } else if (applyBlocked()) {
      ap.textContent = "blocked";
      ap.title = ($("#apply-why") && $("#apply-why").textContent) ||
        "a gate refuses right now";
    } else {
      ap.textContent = "ready";
      ap.title = "every gate passes and the box is ticked";
    }
  }
}

/* One roving tabindex over both rail tablists, so the arrows walk all six
   destinations. Up and Down because the rail is vertical; Left and Right are
   kept because a returning user may still reach for them. */
function railStep(from, step) {
  const i = TAB_NAMES.indexOf(from);
  if (i < 0) return;
  const j = (i + step + TAB_NAMES.length) % TAB_NAMES.length;
  showTab(TAB_NAMES[j]);
  $("#tabbtn-" + TAB_NAMES[j]).focus();
}

$$(".tab").forEach(t => {
  t.onclick = () => showTab(t.dataset.tab);
  t.addEventListener("keydown", e => {
    const step = (e.key === "ArrowDown" || e.key === "ArrowRight") ? 1
      : (e.key === "ArrowUp" || e.key === "ArrowLeft") ? -1 : 0;
    if (step) { e.preventDefault(); railStep(t.dataset.tab, step); return; }
    if (e.key === "Home" || e.key === "End") {
      e.preventDefault();
      const j = e.key === "Home" ? 0 : TAB_NAMES.length - 1;
      showTab(TAB_NAMES[j]);
      $("#tabbtn-" + TAB_NAMES[j]).focus();
    }
  });
});

/* Apply is a sub-step of Plan and shares its screen, so this is a jump, not a
   destination: it shows the plan, brings the apply card into view and puts
   focus on the tick, which is the thing it is there to do. */
$("#goto-apply").onclick = () => {
  showTab("plan");
  const card = $("#apply-anchor");
  if (card && !$("#apply-zone").classList.contains("hidden")) {
    reveal(card, "start");
    const c = $("#confirm");
    if (c) c.focus();
  } else {
    const b = $("#run-plan");
    if (b) { reveal(b, "center"); b.focus(); }
  }
};

/* The "Next: ..." button in each section header. The rail is the navigation;
   these make the intended path obvious without gating anything (F1). */
$$(".btn.next").forEach(b => {
  b.onclick = () => {
    const to = b.dataset.next;
    showTab(to);
    const head = $("#tab-" + to);
    if (head && head.focus) head.focus();
  };
});

/* The save picker's keyboard model: Enter, Space or Down opens the listbox,
   the arrows walk it, Enter chooses, Escape closes and hands focus back to
   the button (U2 -- a dialog that closes onto `<body>` loses a keyboard
   user's place entirely). */
$("#save-picker").onclick = () => { PICKER_OPEN ? closeSavePicker() : openSavePicker(); };
$("#save-picker").addEventListener("keydown", e => {
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    if (!PICKER_OPEN) openSavePicker(); else movePickerFocus(e.key === "ArrowDown" ? 1 : -1);
  } else if (e.key === "Escape" && PICKER_OPEN) {
    e.preventDefault(); closeSavePicker();
  }
});
$("#save-menu").addEventListener("keydown", e => {
  if (e.key === "Escape") { e.preventDefault(); closeSavePicker(); return; }
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault(); movePickerFocus(e.key === "ArrowDown" ? 1 : -1); return;
  }
  if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
    e.preventDefault();
    const a = document.activeElement;
    if (a && a.classList.contains("saveopt")) a.click();
  }
});

/* The header is measured into `--head` so the shell below it is exactly the
   rest of the viewport whatever the banners do. `--tabs-h` is kept at 0: the
   rail is vertical, so nothing sticky sits above the content any more and the
   scroll offsets the old two-bar layout needed are all zero. */
function stickyOffsets() {
  const top = $("#topbar");
  if (!top) return;
  const th = Math.round(top.getBoundingClientRect().height);
  document.documentElement.style.setProperty("--topbar-h", th + "px");
  document.documentElement.style.setProperty("--tabs-h", "0px");
}
if (window.ResizeObserver) {
  const ro = new ResizeObserver(stickyOffsets);
  ro.observe($("#topbar"));
}
window.addEventListener("resize", stickyOffsets);
stickyOffsets();

document.addEventListener("click", e => {
  if (!e.target.closest(".combo")) closeSuggest();
});

syncAddRule();
$("#undo-last").onclick = () => {
  if (!UNDO) { toast("nothing to undo yet"); return; }
  doUndo();
};
syncUndo();

boot().catch(e => {
  document.body.insertAdjacentHTML("afterbegin",
    `<div class="banner danger"><b>Failed to start:</b> ${esc(e.message)}</div>`);
});
