// The spot a click in the PDF points at, shown in the editor.
//
// SyncTeX names a line only, and a LaTeX paragraph is often one long line. So
// the PDF view also says which word was clicked, with a few characters around
// it (pdfview.js). The word is looked for in that line and the lines next to
// it, where LaTeX may have moved it, in the text as a reader sees it: without
// commands and braces, with accents put together ("na\"ive" reads "naïve").
// The occurrence whose surroundings match best wins. It is selected, and the
// line and the word light up and fade out after three seconds.
//
//   extensions: spotExtension(cm)        in every text file's EditorState
//   showSpot(view, cm, {line, word, before, after})  returns the position to scroll to
//
// findPassage does the same for a passage selected in the PDF, for the Files
// view of a review (pages/review-files.js).

const FADE_MS = 3200;
const AROUND = 3;
const WORD = /[\p{L}\p{N}]/u;
// The shortest start or end of a passage that counts as found.
const ENOUGH = 6;

// Accent commands and the combining marks they stand for.
const ACCENTS = {
  '"': "\u0308", "'": "\u0301", "`": "\u0300", "^": "\u0302", "~": "\u0303", "=": "\u0304", ".": "\u0307",
  u: "\u0306", v: "\u030C", H: "\u030B", c: "\u0327", k: "\u0328", r: "\u030A",
};
const SYMBOLS = {
  ss: "\u00DF", o: "\u00F8", O: "\u00D8", ae: "\u00E6", AE: "\u00C6", oe: "\u0153", OE: "\u0152",
  aa: "\u00E5", AA: "\u00C5", l: "\u0142", L: "\u0141", i: "\u0131", j: "\u0237",
};
const ACCENT = /^\\(["'`^~=.]|[uvHckr](?=[\s{]))\s*\{?\s*(\\[ij](?![a-zA-Z])|[a-zA-Z])\s*\}?/;
const COMMAND = /^\\([a-zA-Z]+)\s*(\{\})?/;

// The text a reader sees in LaTeX source, and for each of its UTF-16 units the
// source range it comes from.
export function plainText(src) {
  let text = "";
  const from = [];
  const to = [];
  const push = (out, a, b) => {
    for (let k = 0; k < out.length; k++) {
      from.push(a);
      to.push(b);
    }
    text += out;
  };
  let i = 0;
  while (i < src.length) {
    const ch = src[i];
    if (ch === "\\") {
      const rest = src.slice(i, i + 48);
      const accent = ACCENT.exec(rest);
      if (accent) {
        const base = accent[2].length > 1 ? accent[2][1] : accent[2];
        push((base + ACCENTS[accent[1]]).normalize("NFC"), i, i + accent[0].length);
        i += accent[0].length;
        continue;
      }
      const command = COMMAND.exec(rest);
      if (command) {
        if (SYMBOLS[command[1]] !== undefined) push(SYMBOLS[command[1]], i, i + command[0].length);
        i += command[0].length;
        continue;
      }
      const next = src[i + 1] ?? "";
      // \- (a place to hyphenate), \/ and \@ print nothing.
      if (next && "-/@".includes(next)) {
        i += 2;
        continue;
      }
      push("%&$#_{}".includes(next) && next ? next : " ", i, i + 2);
      i += 2;
      continue;
    }
    if (ch === "%") {
      const end = src.indexOf("\n", i);
      if (end < 0) break;
      i = end;
      continue;
    }
    if (ch === "{" || ch === "}") {
      i += 1;
      continue;
    }
    if (ch === "~" || ch === "\n" || ch === "\t") {
      push(" ", i, i + 1);
      i += 1;
      continue;
    }
    if (src.startsWith("---", i)) {
      push("\u2014", i, i + 3);
      i += 3;
      continue;
    }
    if (src.startsWith("--", i)) {
      push("\u2013", i, i + 2);
      i += 2;
      continue;
    }
    if (src.startsWith("``", i) || src.startsWith("''", i)) {
      push(ch === "`" ? "\u201C" : "\u201D", i, i + 2);
      i += 2;
      continue;
    }
    push(ch, i, i + 1);
    i += 1;
  }
  return { text, from, to };
}

const squash = (s) => String(s || "").normalize("NFC").replace(/\u00AD/g, "").replace(/\s+/g, " ");

function commonSuffix(a, b) {
  let n = 0;
  while (n < a.length && n < b.length && a[a.length - 1 - n] === b[b.length - 1 - n]) n += 1;
  return n;
}

function commonPrefix(a, b) {
  let n = 0;
  while (n < a.length && n < b.length && a[n] === b[n]) n += 1;
  return n;
}

// Where `word` is in the lines around `line` (1-based) of a CodeMirror doc:
// {from, to} in the doc, or null.
export function findWord(doc, line, word, before = "", after = "") {
  const target = squash(word).trim();
  if (!target) return null;
  const first = Math.max(1, line - AROUND);
  const last = Math.min(doc.lines, line + AROUND);
  const start = doc.line(first).from;
  const src = doc.sliceString(start, doc.line(last).to);
  const plain = plainText(src);
  const { text } = plain;
  const wantBefore = squash(before);
  const wantAfter = squash(after);
  let best = null;
  for (const caseless of [false, true]) {
    const hay = caseless ? text.toLowerCase() : text;
    const needle = caseless ? target.toLowerCase() : target;
    for (let k = hay.indexOf(needle); k >= 0; k = hay.indexOf(needle, k + 1)) {
      const end = k + needle.length;
      if (WORD.test(hay[k - 1] || "") || WORD.test(hay[end] || "")) continue;
      const from = start + plain.from[k];
      const to = start + plain.to[end - 1];
      const at = doc.lineAt(from).number;
      const score = commonSuffix(wantBefore, squash(text.slice(Math.max(0, k - 60), k)))
        + commonPrefix(wantAfter, squash(text.slice(end, end + 60)))
        + (at === line ? 3 : 0) - Math.abs(at - line);
      if (!best || score > best.score) best = { from, to, score };
    }
    if (best) break;
  }
  return best ? { from: best.from, to: best.to } : null;
}

// White space and hyphens are left out of a passage, because the PDF breaks
// lines and words where the source does not. Quotes count as straight ones.
const SKIP = /[\s\u00AD-]/;
const QUOTES = { "\u2018": "'", "\u2019": "'", "`": "'", "\u201C": '"', "\u201D": '"' };

function fold(text, caseless) {
  let out = "";
  for (const ch of text) {
    if (SKIP.test(ch)) continue;
    const c = QUOTES[ch] ?? ch;
    const lower = caseless ? c.toLowerCase() : c;
    out += lower.length === c.length ? lower : c;
  }
  return out;
}

// plainText folded, keeping the source range of every UTF-16 unit.
function foldPlain(plain, caseless) {
  let text = "";
  const from = [];
  const to = [];
  for (let k = 0; k < plain.text.length; k++) {
    const c = fold(plain.text[k], caseless);
    if (c.length !== 1) continue;
    text += c;
    from.push(plain.from[k]);
    to.push(plain.to[k]);
  }
  return { text, from, to };
}

// The longest n for which has(n) holds, given that it holds for every smaller n.
function longest(max, has) {
  let lo = 0;
  let hi = max;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (has(mid)) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

// Where a passage selected in the PDF is in the lines around `first` to `last`
// (1-based, SyncTeX's lines of its start and its end): {from, to} in the doc,
// or null. Math, citations and references read differently in the PDF, so a
// passage that is not there as a whole is found by its start and its end. Each
// is the longest beginning (or ending) of the passage that is there, with
// words at the edge left out while it is not, and the place nearest to its
// SyncTeX line wins.
export function findPassage(doc, first, last, passage) {
  const words = squash(passage).normalize("NFKC").trim().split(" ");
  const lo = Math.max(1, Math.min(first, last) - AROUND);
  const hi = Math.min(doc.lines, Math.max(first, last) + AROUND);
  const start = doc.line(lo).from;
  const plain = plainText(doc.sliceString(start, doc.line(hi).to));
  for (const caseless of [false, true]) {
    const hay = foldPlain(plain, caseless);
    const parts = words.map((w) => fold(w, caseless)).filter(Boolean);
    if (!parts.length) return null;
    const lineAt = (k) => doc.lineAt(start + hay.from[k]).number;
    // The occurrence of `needle` at or after `after` nearest to `line`.
    const nearest = (needle, line, after = 0) => {
      let best = -1;
      let distance = Infinity;
      for (let k = hay.text.indexOf(needle, after); k >= 0; k = hay.text.indexOf(needle, k + 1)) {
        const d = Math.abs(lineAt(k) - line);
        if (d < distance) [best, distance] = [k, d];
      }
      return best;
    };
    const span = (a, b) => ({ from: start + hay.from[a], to: start + hay.to[b] });

    const whole = parts.join("");
    const at = nearest(whole, first);
    if (at >= 0) return span(at, at + whole.length - 1);

    let from = -1;
    for (let i = 0; i < parts.length && from < 0; i++) {
      const rest = parts.slice(i).join("");
      const n = longest(rest.length, (m) => hay.text.includes(rest.slice(0, m)));
      if (n >= Math.min(ENOUGH, rest.length) && n > 1) from = nearest(rest.slice(0, n), first);
    }
    if (from < 0) continue;
    let to = -1;
    for (let j = parts.length; j > 0 && to < 0; j--) {
      const head = parts.slice(0, j).join("");
      const n = longest(head.length, (m) => hay.text.indexOf(head.slice(head.length - m), from) >= 0);
      if (n >= Math.min(ENOUGH, head.length) && n > 1) to = nearest(head.slice(head.length - n), last, from) + n - 1;
    }
    if (to >= from) return span(from, to);
  }
  return null;
}

// --- The highlight -------------------------------------------------------------------

const fields = new WeakMap();
const timers = new WeakMap();

function parts(cm) {
  let p = fields.get(cm);
  if (!p) {
    const set = cm.StateEffect.define();
    const field = cm.StateField.define({
      create: () => cm.Decoration.none,
      update: (deco, tr) => {
        let next = deco.map(tr.changes);
        for (const e of tr.effects) {
          if (!e.is(set)) continue;
          const v = e.value;
          next = v ? cm.Decoration.set([
            cm.Decoration.line({ class: "cm-syncLine" }).range(v.line),
            ...(v.to > v.from ? [cm.Decoration.mark({ class: "cm-syncWord" }).range(v.from, v.to)] : []),
          ], true) : cm.Decoration.none;
        }
        return next;
      },
      provide: (f) => cm.EditorView.decorations.from(f),
    });
    p = { set, field };
    fields.set(cm, p);
  }
  return p;
}

export function spotExtension(cm) {
  return parts(cm).field;
}

export function showSpot(view, cm, { line, word = "", before = "", after = "" }) {
  const { set } = parts(cm);
  const doc = view.state.doc;
  const n = Math.max(1, Math.min(line || 1, doc.lines));
  const found = word ? findWord(doc, n, word, before, after) : null;
  const at = found ? doc.lineAt(found.from) : doc.line(n);
  view.dispatch({
    selection: found ? { anchor: found.from, head: found.to } : { anchor: at.from },
    effects: set.of({ line: at.from, from: found?.from ?? 0, to: found?.to ?? 0 }),
  });
  clearTimeout(timers.get(view));
  timers.set(view, setTimeout(() => {
    if (view.dom.isConnected) view.dispatch({ effects: set.of(null) });
  }, FADE_MS));
  return found ? found.from : at.from;
}
