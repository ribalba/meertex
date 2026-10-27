// Spelling and grammar in the browser editor, the way meerail's composer has
// them: wavy underlines in the text, a box of suggestions under the one that
// was clicked, and Alt+Shift+G for the next one after the cursor.
//
// The checking is done by the LanguageTool server meertex runs next to it
// (app/grammar/). The page only ever talks to /api/grammar/ on meertex's own
// server, which is the one place that knows where the checker is. It turns the
// LaTeX into LanguageTool's annotated text, so commands, math and citations
// are skipped, and every offset it answers with is a UTF-16 offset into the
// file as it was sent. UTF-16 is also what CodeMirror counts in, so an offset
// is a position in the editor.
//
// The whole open file goes along with every check. The server keeps its
// answers per paragraph and asks LanguageTool about the paragraphs it has not
// seen yet, those nearest the cursor first. What it did not get to comes back
// as `pending` ranges. Underlines already in them stay, and the page asks
// again a moment later.
//
// The underlines are a StateField of mark decorations, so CodeMirror moves
// them along with every edit, a coauthor's too. A match an edit touches is
// dropped until the next check (carry() below). An answer that arrives after
// more typing is about the text as it was sent, so it is carried through the
// edits made since in the same way.
//
//   const grammar = editorGrammar({ cm: () => cm, view: () => view, path: () => openPath,
//     text: () => boundText, canEdit, onChange() });
//   extensions: grammar.extension()   in every text file's EditorState
//   grammar.opened()                  another file is open, or none
//   grammar.box                       the suggestions, a child of the editor's host
//   grammar.start(), grammar.toggle(), grammar.destroy()
//   grammar.active, enabled, count, busy, failed, title   what the Spellcheck button shows

import { api } from "./api.js";
import { h, plural } from "./dom.js";
import { flashError } from "./ui.js";

// A file that was just opened is checked almost at once, not after a pause
// nobody is making.
const OPEN_MS = 150;
// Quiet after the last change before the file is checked. Long enough not to
// check a word half typed, short enough that the underline turns up while the
// sentence is still in mind.
const QUIET_MS = 700;
// 429: the server is still busy with this person's previous check, often one
// this page gave up on a moment ago.
const RETRY_MS = 1000;
// The server did not get to every paragraph. The next part comes soon.
const MORE_MS = 250;
// The server refuses more (422), so a longer file is not sent at all.
const MAX_CHARS = 2000000;
const MAX_SUGGESTIONS = 6;
const KINDS = [[/\.(tex|ltx)$/i, "latex"], [/\.(md|txt)$/i, "text"]];
const TYPES = { spelling: "Spelling", grammar: "Grammar", style: "Style" };
const MODIFIERS = new Set(["Shift", "Alt", "Control", "Meta", "AltGraph", "CapsLock"]);

export function kindOf(path) {
  return KINDS.find(([re]) => re.test(path || ""))?.[1] || null;
}

// The changes `changes` made, applied to the matches before them. A match
// clear of every change slides along. A match a change touches is dropped,
// since its words changed and the verdict may have too, and the next check
// brings it back if it still stands. Touching includes typing right up against
// a word, so finishing "teh" into "tehe" takes the underline away at once.
// The exception is whitespace typed next to a word with nothing deleted. The
// word is exactly as it was, and dropping its underline for the length of a
// check would make every flagged word blink off as the sentence carries on.
function carry(list, changes) {
  if (changes.empty || !list.length) return list;
  const out = [];
  for (const m of list) {
    let keep = true;
    changes.iterChanges((fromA, toA, _fromB, _toB, inserted) => {
      if (!keep || toA < m.from || fromA > m.to) return;
      if (fromA === toA && (fromA === m.to || fromA === m.from)) {
        const typed = inserted.toString();
        if (fromA === m.to && /^\s/.test(typed)) return;
        if (fromA === m.from && /\s$/.test(typed)) return;
      }
      keep = false;
    });
    if (!keep) continue;
    // Whitespace typed at the start goes in front of the word, at the end behind it.
    const from = changes.mapPos(m.from, 1);
    const to = changes.mapPos(m.to, -1);
    if (to > from) out.push({ ...m, from, to });
  }
  return out;
}

const byStart = (a, b) => a.from - b.from || a.to - b.to;
const ignoreKey = (m) => `${m.rule}\u0000${m.text}`;

export function editorGrammar({ cm, view, path, text, canEdit, onChange }) {
  let lib = null;
  let field = null;
  let setMatches = null;
  let setActive = null;
  let config = null; // GET /api/grammar/config, null until it answers or without the routes
  let saving = false;
  let timer = 0;
  let flight = null; // {ac, changes}: the check on the wire and the edits made since it left
  let failed = "";   // the last error's sentence, until a check succeeds
  let language = null; // {code, name, auto} the last answer was checked in
  let shown = null;  // {id} of the match whose suggestions are up
  let painted = "";
  let dead = false;
  let ids = 0;
  let sugs = []; // the replacements on the box's buttons
  // The language detected for each file, sent along while the setting is
  // Auto, so the server can use what it cached under that language.
  const detected = new WeakMap();
  // Ignore holds for this page only: rule and words of what was ignored.
  const ignored = new Set();

  // The checker is set up and this person may edit, so the button is its switch.
  const active = () => Boolean(config?.available && canEdit);
  const on = () => active() && Boolean(config.enabled);
  const listOf = (state) => state?.field(field, false)?.list || [];

  // --- The underlines ---------------------------------------------------------------

  function makeField() {
    setMatches = lib.StateEffect.define();
    setActive = lib.StateEffect.define();
    const build = (list, active) => lib.Decoration.set(list.map((m) => lib.Decoration.mark({
      class: `cm-grammar cm-grammar-${m.type}${m.id === active ? " is-active" : ""}`,
    }).range(m.from, m.to)), true);
    field = lib.StateField.define({
      create: () => ({ list: [], active: null, deco: lib.Decoration.none }),
      update(value, tr) {
        let { list, active } = value;
        let changed = false;
        if (tr.docChanged) {
          list = carry(list, tr.changes);
          changed = list !== value.list;
        }
        for (const e of tr.effects) {
          if (e.is(setMatches)) [list, changed] = [e.value, true];
          else if (e.is(setActive)) [active, changed] = [e.value, true];
        }
        if (!changed) return value;
        if (active !== null && !list.some((m) => m.id === active)) active = null;
        return { list, active, deco: build(list, active) };
      },
      provide: (f) => lib.EditorView.decorations.from(f, (v) => v.deco),
    });
  }

  function extension() {
    lib = cm();
    if (!field) makeField();
    return [
      field,
      lib.EditorView.domEventHandlers({
        click: onClick,
        keydown: onKey,
        scroll: () => {
          if (shown) place();
        },
      }),
      lib.keymap.of([
        { key: "Alt-Shift-g", run: () => next() },
        // Escape with the suggestions up takes them away and nothing else.
        { key: "Escape", run: () => Boolean(shown && (close(), true)) },
      ]),
      lib.EditorView.updateListener.of(updated),
    ];
  }

  function setList(list) {
    const v = view();
    if (v && field && v.state.field(field, false)) v.dispatch({ effects: setMatches.of(list) });
  }

  function updated(u) {
    if (u.docChanged) {
      if (flight) {
        try {
          flight.changes = flight.changes.compose(u.changes);
        } catch {
          // Not the document the check was sent for. Its answer is of no use.
          abort();
        }
      }
      if (on() && kindOf(path())) schedule(QUIET_MS);
    }
    if (shown) {
      if (!current(u.state)) close();
      else if (u.docChanged || u.geometryChanged || u.viewportChanged) place();
    }
    report();
  }

  // --- Checking ---------------------------------------------------------------------

  function schedule(ms) {
    clearTimeout(timer);
    timer = setTimeout(check, ms);
  }

  function abort() {
    if (flight) flight.ac.abort();
    flight = null;
  }

  function stop() {
    clearTimeout(timer);
    timer = 0;
    abort();
  }

  async function check() {
    timer = 0;
    const v = view();
    const kind = kindOf(path());
    if (dead || !v || !kind || !on()) {
      report();
      return;
    }
    abort();
    const key = text();
    const sent = v.state.doc.toString();
    if (sent.length > MAX_CHARS) {
      failed = "This file is too long to check for spelling and grammar.";
      setList([]);
      report();
      return;
    }
    // The edits from here on are collected in updated(), to carry the answer through.
    const mine = { ac: new AbortController(), changes: v.state.changes() };
    flight = mine;
    report();
    let res;
    try {
      res = await api.post("/api/grammar/check", {
        text: sent,
        kind,
        language: config.language === "auto" ? (key && detected.get(key)) || null : null,
        focus: v.state.selection.main.head,
      }, { signal: mine.ac.signal });
    } catch (err) {
      if (flight !== mine) return; // overtaken by a newer check, or the file changed
      flight = null;
      if (err.status === 429) {
        schedule(RETRY_MS);
        report();
        return;
      }
      failed = err.message || "The spelling and grammar checker did not answer.";
      report();
      // Turned off since this page asked, in another tab or on the server.
      // Ask again rather than keep checking into a refusal.
      if (err.status === 409) load();
      return;
    }
    if (flight !== mine || dead) return;
    flight = null;
    const now = view();
    if (!now || text() !== key || !on()) {
      report();
      return;
    }
    failed = "";
    language = res.language || null;
    if (config.language === "auto" && key && language?.code) detected.set(key, language.code);
    const words = new Set((config.words || []).map((w) => w.toLowerCase()));
    const rules = new Set(config.disabled_rules || []);
    const fresh = [];
    for (const m of res.matches || []) {
      const from = m.offset;
      const to = m.offset + m.length;
      if (!(m.length > 0) || from < 0 || to > sent.length) continue;
      const match = { ...m, id: 0, from, to, text: sent.slice(from, to) };
      // The server filters the dictionary and the rules too. A word added
      // while this check was out is not in its answer's filter yet.
      if (rules.has(match.rule) || ignored.has(ignoreKey(match))) continue;
      if (match.type === "spelling" && words.has(match.text.toLowerCase())) continue;
      fresh.push(match);
    }
    const changes = mine.changes;
    let list = carry(fresh, changes);
    const up = current(now.state);
    for (const m of list) {
      // The box stays up when the answer has the same words for it.
      m.id = up && m.from === up.from && m.to === up.to && m.rule === up.rule ? up.id : ++ids;
    }
    // Paragraphs the server did not get to keep the underlines they have.
    const pending = (res.pending || []).map(([a, b]) => [changes.mapPos(a, -1), changes.mapPos(b, 1)]);
    if (pending.length) {
      list = list.concat(listOf(now.state).filter((m) => pending.some(([a, b]) => m.from >= a && m.to <= b)));
    }
    setList(list.sort(byStart));
    report();
    if (res.complete === false) schedule(MORE_MS);
  }

  // --- The suggestions --------------------------------------------------------------

  const box = h("div", { class: "grammar-box", role: "dialog", "aria-label": "Spelling and grammar", hidden: true });
  // Clicking a suggestion must not take the focus and the cursor out of the
  // text first. Focus moves on mousedown, and the click still arrives.
  box.addEventListener("mousedown", (e) => e.preventDefault());
  box.addEventListener("click", onBoxClick);
  box.addEventListener("keydown", onBoxKey);

  function current(state) {
    if (!shown || !state) return null;
    return listOf(state).find((m) => m.id === shown.id) || null;
  }

  // The shortest underline at `pos`, for a word inside a longer one.
  function matchAt(state, pos) {
    let best = null;
    for (const m of listOf(state)) {
      if (m.from <= pos && pos <= m.to && (!best || m.to - m.from < best.to - best.from)) best = m;
    }
    return best;
  }

  // A click on an underline brings its suggestions up, the way every word
  // processor does it. A drag that selected text is not that click.
  function onClick(e, v) {
    const mark = e.target.closest?.(".cm-grammar");
    if (!mark || !on() || !v.state.selection.main.empty) {
      close();
      return false;
    }
    const pos = v.posAtCoords({ x: e.clientX, y: e.clientY });
    const m = pos === null ? null : matchAt(v.state, pos);
    if (m) open(m, false);
    else close();
    return false;
  }

  // Typing on with the suggestions up takes them away. Escape does only that.
  function onKey(e) {
    if (!shown || MODIFIERS.has(e.key) || (e.altKey && e.shiftKey && e.code === "KeyG")) return false;
    close();
    if (e.key !== "Escape") return false;
    e.preventDefault();
    return true;
  }

  function open(m, withFocus) {
    const v = view();
    if (!v) return;
    shown = { id: m.id };
    fill(m);
    box.hidden = false;
    v.dispatch({ effects: setActive.of(m.id) });
    place();
    if (withFocus) box.querySelector("button")?.focus();
  }

  function fill(m) {
    sugs = (m.replacements || []).slice(0, MAX_SUGGESTIONS);
    const rule = m.rule_description || m.rule;
    box.replaceChildren(
      h("div", { class: "grammar-box-body" },
        h("div", { class: `grammar-box-kind is-${TYPES[m.type] ? m.type : "grammar"}`, text: m.short || TYPES[m.type] || "Grammar" }),
        h("p", { class: "grammar-box-msg", text: m.message || "Possible mistake." }),
        // An empty replacement means "take these words out", which is what
        // LanguageTool suggests for a word typed twice.
        sugs.length ? h("div", { class: "grammar-box-sugs" }, sugs.map((s, i) => h("button", {
          type: "button", class: "grammar-sug", dataset: { i: String(i) }, title: s ? `Replace with ${s}` : "Remove these words",
        }, s || h("em", { text: "Remove" })))) : null),
      h("div", { class: "grammar-box-acts" },
        h("button", { type: "button", class: "grammar-act", dataset: { act: "ignore" }, text: "Ignore",
          title: "Stop flagging this until the editor is opened again" }),
        m.type === "spelling" ? h("button", { type: "button", class: "grammar-act", dataset: { act: "word" }, text: "Add to dictionary",
          title: `Never flag “${m.text}” as misspelled again, in any file. Settings lists your dictionary.` }) : null,
        h("button", { type: "button", class: "grammar-act", dataset: { act: "rule" }, text: "Turn off this rule",
          title: `Turn off “${rule}” in every file. Settings lists the rules you turned off.` })));
  }

  // Under the words, or above them near the bottom of the editor. Hidden, not
  // closed, while the words are scrolled out of sight.
  function place() {
    const v = view();
    if (!v || !shown) return;
    v.requestMeasure({
      key: box,
      read: () => {
        const m = current(v.state);
        const host = box.parentElement?.getBoundingClientRect();
        const at = m && v.coordsAtPos(m.from, 1);
        if (!at || !host) return null;
        return { at, host, scroller: v.scrollDOM.getBoundingClientRect(), width: box.offsetWidth, height: box.offsetHeight };
      },
      write: (r) => {
        if (!r || r.at.bottom < r.scroller.top || r.at.top > r.scroller.bottom) {
          box.style.visibility = "hidden";
          return;
        }
        box.style.visibility = "";
        let top = r.at.bottom - r.host.top + 4;
        if (top + r.height > r.host.height - 8 && r.at.top - r.host.top - r.height - 4 >= 8) top = r.at.top - r.host.top - r.height - 4;
        const left = Math.max(8, Math.min(r.at.left - r.host.left - 4, r.host.width - r.width - 8));
        box.style.top = `${Math.round(top)}px`;
        box.style.left = `${Math.round(left)}px`;
      },
    });
  }

  function close() {
    if (!shown) return;
    shown = null;
    box.hidden = true;
    box.replaceChildren();
    const v = view();
    if (v && field && v.state.field(field, false)?.active != null) v.dispatch({ effects: setActive.of(null) });
  }

  // Back to the text, where the cursor was when the suggestions came up.
  function back() {
    const v = view();
    close();
    v?.focus();
  }

  function onBoxClick(e) {
    const b = e.target.closest("button");
    const v = view();
    const m = v && current(v.state);
    if (!b || !m) return;
    if (b.dataset.i !== undefined) apply(v, m, sugs[Number(b.dataset.i)] ?? "");
    else act(b.dataset.act, m);
  }

  function onBoxKey(e) {
    if (e.ctrlKey || e.metaKey) return;
    if (e.altKey && e.shiftKey && e.code === "KeyG") {
      e.preventDefault();
      next();
      return;
    }
    const buttons = [...box.querySelectorAll("button")];
    const i = buttons.indexOf(document.activeElement);
    const step = (d) => buttons[(i + d + buttons.length) % buttons.length]?.focus();
    if (e.key === "Escape") {
      e.preventDefault();
      back();
    } else if (e.key === "ArrowRight" || e.key === "ArrowDown" || (e.key === "Tab" && !e.shiftKey)) {
      e.preventDefault();
      step(1);
    } else if (e.key === "ArrowLeft" || e.key === "ArrowUp" || (e.key === "Tab" && e.shiftKey)) {
      e.preventDefault();
      step(-1);
    } else if (e.key.length === 1 && e.key !== " " && !e.altKey) {
      // Carried on typing instead of choosing. Focus moves during the
      // keydown, so the character lands in the text, where it was going.
      back();
    }
  }

  // A normal edit through yCollab, so it reaches everybody and Ctrl+Z takes
  // it back like anything typed.
  function apply(v, m, value) {
    close();
    // The words changed since the box came up (a coauthor's edit, an undo).
    // Nothing here is about them any more.
    if (v.state.doc.sliceString(m.from, m.to) !== m.text) {
      schedule(0);
      return;
    }
    v.dispatch({
      changes: { from: m.from, to: m.to, insert: value },
      selection: { anchor: m.from + value.length },
      userEvent: "input.grammar",
    });
    v.focus();
  }

  // Ignore is this page's alone and costs nothing. The other two are kept on
  // the server, in the dictionary and the list of rules turned off on the
  // Settings page, so they hold in every file and every browser. The
  // underlines go at once rather than when the server answers.
  async function act(what, m) {
    back();
    const word = m.text.toLowerCase();
    let gone;
    if (what === "word") {
      config.words = [...(config.words || []), m.text];
      gone = (x) => x.type === "spelling" && x.text.toLowerCase() === word;
    } else if (what === "rule") {
      config.disabled_rules = [...(config.disabled_rules || []), m.rule];
      gone = (x) => x.rule === m.rule;
    } else {
      ignored.add(ignoreKey(m));
      gone = (x) => ignoreKey(x) === ignoreKey(m);
    }
    setList(listOf(view()?.state).filter((x) => !gone(x)));
    report();
    if (what === "ignore") return;
    try {
      const saved = await api.post("/api/grammar/ignore", what === "word" ? { word: m.text } : { rule: m.rule });
      if (saved && !dead) config = saved;
    } catch (err) {
      flashError(err);
    }
  }

  // Alt+Shift+G: the next underline after the cursor, or after the one whose
  // suggestions are up, round to the first after the last.
  function next() {
    const v = view();
    const list = listOf(v?.state);
    if (!v || !on() || !list.length) return false;
    const up = current(v.state);
    const from = up ? up.from : v.state.selection.main.head;
    const m = list.find((x) => x.from > from) || list[0];
    close();
    v.focus();
    v.dispatch({
      selection: { anchor: m.to },
      effects: lib.EditorView.scrollIntoView(m.from, { y: "nearest", yMargin: 80 }),
    });
    open(m, true);
    return true;
  }

  // --- Settings ---------------------------------------------------------------------

  async function load() {
    try {
      config = await api.get("/api/grammar/config");
    } catch (err) {
      // No checker routes on this server (404), or none reachable. The
      // button stays the browser's spellchecker.
      if (err.status !== 401) config = null;
    }
    if (!dead) configured();
  }

  function configured() {
    if (on()) {
      if (kindOf(path()) && view()) schedule(0);
    } else {
      stop();
      close();
      failed = "";
      language = null;
      setList([]);
    }
    report();
  }

  // The Spellcheck button. Stored with the account, so it holds in every
  // browser, like meerail's setting.
  async function toggle() {
    if (!config?.available || saving) return;
    saving = true;
    try {
      const saved = await api.put("/api/grammar/config", { enabled: !config.enabled });
      if (!dead) config = saved;
    } catch (err) {
      flashError(err);
    } finally {
      saving = false;
    }
    if (!dead) configured();
  }

  // --- For the button ---------------------------------------------------------------

  const count = () => (on() && kindOf(path()) ? listOf(view()?.state).length : 0);

  function title() {
    if (!active()) return "";
    if (!config.enabled) return "Spelling and grammar are off. Click to turn them on.";
    if (!kindOf(path())) return "Spelling and grammar are checked in .tex, .md and .txt files. Click to turn them off.";
    if (failed) return `${failed} Click to turn spelling and grammar off.`;
    if (!language) return "Checking spelling and grammar… Click to turn them off.";
    const n = count();
    const lang = `${language.name || language.code}${config.language === "auto" ? ", detected from the text" : ""}`;
    if (!n) return `No issues in ${lang}. Click to turn spelling and grammar off.`;
    return `${plural(n, "issue")} in ${lang}. Alt+Shift+G goes to the next underline. Click to turn spelling and grammar off.`;
  }

  // Tells the page when something on the button changed.
  function report() {
    if (dead) return;
    const sig = JSON.stringify([active(), on(), count(), Boolean(flight), failed, title()]);
    if (sig === painted) return;
    painted = sig;
    onChange?.();
  }

  const onDown = (e) => {
    if (!shown || box.contains(e.target) || view()?.contentDOM.contains(e.target)) return;
    close();
  };
  const onResize = () => close();
  document.addEventListener("mousedown", onDown, true);
  window.addEventListener("resize", onResize);

  return {
    box,
    extension,
    start: load,
    toggle,
    // The file on screen changed, or there is none.
    opened() {
      stop();
      close();
      failed = "";
      language = null;
      if (on() && kindOf(path()) && view()) schedule(OPEN_MS);
      report();
    },
    get active() {
      return active();
    },
    get enabled() {
      return on();
    },
    get count() {
      return count();
    },
    get busy() {
      return Boolean(flight);
    },
    get failed() {
      return Boolean(failed) && on();
    },
    get title() {
      return title();
    },
    destroy() {
      dead = true;
      stop();
      document.removeEventListener("mousedown", onDown, true);
      window.removeEventListener("resize", onResize);
    },
  };
}
