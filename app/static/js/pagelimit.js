// The page limit (docs/DESIGN.md §4 and §14): what a paper's page_limit says,
// and whether a PDF keeps to it. The limit is free text as the call for papers
// words it. Its first number is the pages for the text. A second number after
// "+" or "plus", or one next to the word references, is how many pages the
// references may add. Words like "plus references" or "references not counted"
// let the references run on.
//
//   parseLimit("8")                                 {pages: 8, references: 0}
//   parseLimit("6 pages including references")      {pages: 6, references: 0}
//   parseLimit("8 pages + unlimited references")    {pages: 8, references: Infinity}
//   parseLimit("10 pages + 2 pages of references")  {pages: 10, references: 2}
//   parseLimit("no limit")                          null, nothing to check
//
// measure(pages) finds where the references start in the text of a PDF, and
// checkLimit(limit, measured) says whether the PDF is too long. Nothing here
// needs a browser, so tools/editor/tests/test_pagelimit.py runs it in node on
// PDFs that LaTeX made.

const NUMBER_WORDS = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
  "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty"];
const NUMBER_WORD = new RegExp(`\\b(?:${NUMBER_WORDS.join("|")})\\b`, "g");
const REFERENCES = /\b(?:references?|refs?|bibliograph\w*|citations?|literatur\w*)\b/;
// A number followed by one of these counts something other than pages.
const NOT_PAGES = /^\s*-?\s*(?:columns?|col\b|pt\b|points?|inch(?:es)?|in\b|cm\b|mm\b|px\b|%|words?|char\w*|figures?|tables?|authors?|days?|weeks?|months?|hours?|min\w*|keywords?|lines?)/;
// What the references get, read from the words around them. Checked in this
// order, so "references not included" lets them run on.
const RUN_ON = /\+|\b(?:plus|unlimited|excluding|excluded?|excludes|exclusive|without|extra|additional|except|apart|beyond|separate(?:ly)?|any number)\b|\bnot (?:be )?(?:counted|included|count)\b|\bdo(?:es)?(?: not|n't) count\b/;
const COUNTED = /\b(?:including|included?|includes|inclusive|incl|in total|total|with)\b/;
const MAX_PAGES = 200;

// The limit in `text`, or null when it has no page count. `pages` is for the
// text. `references` is 0 when the references count like the text, a number
// of extra pages for them, or Infinity when they do not count.
export function parseLimit(text) {
  const s = ` ${String(text || "").toLowerCase()} `.replace(NUMBER_WORD, (w) => String(NUMBER_WORDS.indexOf(w) + 1));
  let numbers = [];
  for (const m of s.matchAll(/\d+/g)) {
    const start = m.index;
    const end = start + m[0].length;
    const n = Number(m[0]);
    // Not a count of pages: a year, 8.5 x 11, A4, 2-column, 10 pt.
    if (n < 1 || n > MAX_PAGES || /[\p{L}]$|\d[.,]$/u.test(s.slice(0, start)) || /^[.,]\d/.test(s.slice(end)) || NOT_PAGES.test(s.slice(end))) continue;
    numbers.push({ n, start, end });
  }
  // "6-10 pages" and "6 to 10 pages" allow the larger one.
  numbers = numbers.reduce((out, num) => {
    const prev = out[out.length - 1];
    if (prev && /^\s*(?:-|–|—|to)\s*$/.test(s.slice(prev.end, num.start))) out[out.length - 1] = { ...num, n: Math.max(prev.n, num.n), start: prev.start };
    else out.push(num);
    return out;
  }, []);
  if (!numbers.length) return null;
  const [main, second] = numbers;
  if (second && (/\+|\bplus\b/.test(s.slice(main.end, second.start)) || REFERENCES.test(clauseAt(s, second.start)))) {
    return { pages: main.n, references: second.n };
  }
  const ref = REFERENCES.exec(s);
  if (!ref) return { pages: main.n, references: 0 };
  // The words next to "references" first, then the whole text.
  for (const part of [clauseAt(s, ref.index), s]) {
    if (RUN_ON.test(part)) return { pages: main.n, references: Infinity };
    if (COUNTED.test(part)) return { pages: main.n, references: 0 };
  }
  return { pages: main.n, references: 0 };
}

function clauseAt(s, at) {
  const stops = /[,;:()[\]]/g;
  let from = 0;
  let to = s.length;
  for (const m of s.matchAll(stops)) {
    if (m.index < at) from = m.index + 1;
    else {
      to = m.index;
      break;
    }
  }
  return s.slice(from, to);
}

function plural(n, one) {
  return `${n} ${n === 1 ? one : `${one}s`}`;
}

// "8 pages plus the references", for hints and warnings.
export function describeLimit({ pages, references }) {
  if (!references) return `${plural(pages, "page")}, references included`;
  if (references === Infinity) return `${plural(pages, "page")} plus the references`;
  return `${plural(pages, "page")} plus ${references} for the references`;
}

// The headings a bibliography gets, without accents, case and spaces.
const HEADINGS = new Set([
  "references", "reference", "referencescited", "citedreferences", "workscited", "bibliography",
  "bibliographie", "bibliografia", "bibliografie", "literature", "literatur", "literaturverzeichnis",
  "quellen", "quellenverzeichnis", "referencias", "riferimenti", "riferimentibibliografici", "referenties",
]);

// Whether a line of a page reads as the bibliography's heading. Small capitals
// come out as "R EFERENCES", a numbered section as "7 References" or "VII. References".
export function isHeading(text) {
  const plain = text.normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase();
  const bare = plain.replace(/^\s*(?:\d+(?:\.\d+)*|[ivxlc]+)\.?\s+/, "");
  return HEADINGS.has(plain.replace(/\s+/g, "")) || HEADINGS.has(bare.replace(/\s+/g, ""));
}

// The text of a page as segments, the runs of text on one baseline without a
// wide gap: a line of a column, a heading, a running head. `view` is the
// page's box and `items` PDF.js's text content. Positions are in points from
// the page's bottom left. Turned text, a stamp in the margin, is left out.
export function segments({ view, items }) {
  const [x0, y0] = view;
  const runs = [];
  for (const it of items || []) {
    if (typeof it.str !== "string" || !it.str.trim() || !Array.isArray(it.transform)) continue;
    const [a, b, c, d, e, f] = it.transform;
    if (Math.abs(b) > 1e-3 || Math.abs(c) > 1e-3 || a <= 0 || d <= 0) continue;
    runs.push({ str: it.str, x: e - x0, y: f - y0, end: e - x0 + (it.width || 0), size: d });
  }
  runs.sort((p, q) => q.y - p.y || p.x - q.x);
  const out = [];
  let line = [];
  const flush = () => {
    line.sort((p, q) => p.x - q.x);
    let seg = null;
    for (const r of line) {
      const gap = seg ? r.x - seg.end : 0;
      const size = seg ? Math.min(seg.size, r.size) : r.size;
      // A gap of most of a letter's height parts two columns, or a running
      // head's left and right part.
      if (!seg || gap > 0.8 * size) {
        seg = { text: r.str, x: r.x, end: r.end, y: r.y, size: r.size };
        out.push(seg);
        continue;
      }
      const space = gap > 0.15 * size && !/\s$/.test(seg.text) && !/^\s/.test(r.str);
      seg.text += (space ? " " : "") + r.str;
      seg.end = Math.max(seg.end, r.end);
      seg.size = Math.max(seg.size, r.size);
    }
    line = [];
  };
  for (const r of runs) {
    if (line.length && Math.abs(line[0].y - r.y) > 0.5) flush();
    line.push(r);
  }
  flush();
  return out;
}

// Running heads, footers, page and line numbers. Text without letters is one.
// So is a line of which a part with letters stands at the same height on
// another page, its numbers aside. Odd and even pages may have different
// heads, so one repeated part, the venue say, takes the title next to it along.
const marginKey = (text) => text.replace(/\d+/g, "#").replace(/\s+/g, " ").trim().toLowerCase();
const hasLetters = (text) => /\p{L}/u.test(text);

function marginLines(all, page) {
  const seen = new Map();
  all.forEach((segs, i) => {
    if (i === page) return;
    for (const seg of segs) {
      const key = marginKey(seg.text);
      if (!seen.has(key)) seen.set(key, []);
      seen.get(key).push(seg.y);
    }
  });
  return all[page]
    .filter((seg) => hasLetters(seg.text) && (seen.get(marginKey(seg.text)) || []).some((y) => Math.abs(y - seg.y) < 2))
    .map((seg) => seg.y);
}

// Where the references start in a PDF. `pages` is [{view, items}] per page,
// as pdfview.js's texts() gives it. Returns {pages, text, references}: the
// page count, the pages the text takes and the page the references start on.
// The last heading counts, so a table cell or a contents line that reads
// References does not. The page it is on belongs to the text as well when
// anything but running heads and page numbers comes before the heading. The
// last two are null without a heading.
export function measure(pages) {
  const all = pages.map(segments);
  let found = null;
  let foundKey = null;
  all.forEach((segs, page) => {
    const mid = (pages[page].view[2] - pages[page].view[0]) / 2;
    for (const seg of segs) {
      if (!isHeading(seg.text)) continue;
      // Later pages, then the right column, then lower down.
      const key = [page, seg.x > mid ? 1 : 0, -seg.y];
      if (!foundKey || key[0] > foundKey[0] || (key[0] === foundKey[0] && (key[1] > foundKey[1] || (key[1] === foundKey[1] && key[2] > foundKey[2])))) {
        found = { page, seg };
        foundKey = key;
      }
    }
  });
  if (!found) return { pages: pages.length, text: null, references: null };
  const { page, seg: head } = found;
  const margin = marginLines(all, page);
  const inMargin = (s) => !hasLetters(s.text) || margin.some((y) => Math.abs(y - s.y) < 0.5);
  const mid = (pages[page].view[2] - pages[page].view[0]) / 2;
  // A heading in the right column has the whole left column before it.
  const right = head.x > mid;
  const before = all[page].some((s) => s !== head && !inMargin(s)
    && (s.y > head.y + 1 ? right || s.x < mid : right && s.end <= mid));
  return { pages: pages.length, text: before ? page + 1 : page, references: page + 1 };
}

// Whether a PDF keeps to `limit`, from measure(). Returns {over, message,
// summary}. `message` says what is too long, or is null. `summary` says what
// was found and what the limit is, for a tooltip.
export function checkLimit(limit, m) {
  const want = describeLimit(limit);
  const found = m.references
    ? `The references start on page ${m.references}.`
    : limit.references ? "No references heading was found." : "";
  const summary = `${found} The limit is ${want}.`.trim();
  let message = null;
  if (m.pages > limit.pages + limit.references) {
    message = `The PDF has ${plural(m.pages, "page")}. The limit is ${want}.`;
  } else if (limit.references && m.references && m.text > limit.pages) {
    const takes = m.text === m.references
      ? `The text runs onto page ${m.text}, where the references start.`
      : `The text takes ${plural(m.text, "page")}.`;
    message = `${takes} The limit is ${want}.`;
  } else if (limit.references && !m.references && m.pages > limit.pages) {
    message = `The PDF has ${plural(m.pages, "page")} and no references heading was found in it. The limit is ${want}.`;
  }
  return { over: Boolean(message), message, summary };
}

// The PDF in a view of pdfview.js against a paper's page_limit, as
// checkLimit() says it, or null when the limit has no page count. The text is
// read only when the references have pages of their own.
export async function checkView(view, pageLimit) {
  const limit = parseLimit(pageLimit);
  if (!limit) return null;
  const m = limit.references ? measure(await view.texts()) : { pages: view.pages, text: null, references: null };
  return checkLimit(limit, m);
}
