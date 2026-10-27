// The naming pattern of DESIGN §4, for the live preview while a pattern is
// typed. The server's dry run only knows the saved pattern, so the browser
// works out the example itself. This is a port of app/naming.py, rule for
// rule, so the preview and the real name agree.

const STOP = new Set(["a", "an", "the", "of", "for", "on", "in", "and", "to", "with", "towards", "via", "using"]);
const PLACEHOLDER = /\{(year|conference|paper|track|title)(\|(lower|upper))?\}/g;
const SPELLED_OUT = { "ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss", "ẞ": "SS" };
const EMPTY = "\u0000";
const COLLAPSE = /[-_.]*(?:\u0000[-_.]*)+/g;
const NAME_MAX = 120;

export function slug(value) {
  const text = String(value ?? "").normalize("NFC").replace(/[äöüÄÖÜßẞ]/g, (c) => SPELLED_OUT[c])
    .normalize("NFKD").replace(/[^\x00-\x7f]/g, "");
  return text.replace(/[^A-Za-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

export function paperFromTitle(title) {
  const words = [];
  for (const word of String(title || "").split(/\s+/)) {
    const s = slug(word).toLowerCase();
    if (s && !STOP.has(s)) words.push(s);
    if (words.length === 3) break;
  }
  return words.join("-");
}

function trim(name, limit = NAME_MAX) {
  return name.replace(/^[-_.]+/, "").slice(0, limit).replace(/[-_.]+$/, "");
}

export function hasPlaceholder(pattern) {
  return new RegExp(PLACEHOLDER.source).test(pattern);
}

// The server's reason a pattern is refused, or null when it is usable.
export function patternProblem(pattern) {
  if (!hasPlaceholder(pattern)) {
    return "The naming pattern needs at least one placeholder, for example {year} or {paper|lower}.";
  }
  const literal = pattern.replace(PLACEHOLDER, "");
  if (literal.includes("{") || literal.includes("}")) {
    return "The naming pattern knows only {year}, {conference}, {paper}, {track} and {title}, each with an optional |lower or |upper.";
  }
  if (!/^[A-Za-z0-9._-]*$/.test(literal)) {
    return "Text outside the placeholders may only use letters, digits, dot, underscore and dash.";
  }
  return null;
}

export function previewName(pattern, fields) {
  const value = (field) => {
    const raw = fields[field];
    if (field === "paper" && !raw) return paperFromTitle(fields.title || "");
    return raw === null || raw === undefined || raw === "" ? "" : slug(raw);
  };
  const filled = pattern.replace(PLACEHOLDER, (_, field, _f, filter) => {
    let v = value(field);
    if (filter === "lower") v = v.toLowerCase();
    if (filter === "upper") v = v.toUpperCase();
    return v || EMPTY;
  });
  // At either end the separator goes with the empty placeholder. In the
  // middle one separator stays, the one written before the placeholder.
  const collapsed = filled.replace(COLLAPSE, (match, offset, whole) => {
    if (offset === 0 || offset + match.length === whole.length) return "";
    return match.split(EMPTY).filter(Boolean)[0] || "";
  });
  return trim(collapsed.replaceAll(EMPTY, ""));
}
