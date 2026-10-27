// The files of a live document (docs/DESIGN.md §8), read the way
// app/live/ydoc.py reads them. The server, the CLI and every browser must agree
// on which entry a path shows, so this is a line-by-line port of
// `path_problem`, `conflict_path`, `entries` and `materialize`. Change them
// together. tools/editor/tests/test_livefiles.py compares the two.

export const FILES = "files";
const MAX_PATH_BYTES = 1024;
const utf8 = new TextEncoder();

// Why `path` cannot name a file in a project, or null when it can.
export function pathProblem(path) {
  if (!path) return "The path is empty";
  if (utf8.encode(path).length > MAX_PATH_BYTES) return "The path is longer than 1024 bytes";
  if (path.includes("\\") || path.includes("\0")) return "The path contains a backslash or a NUL character";
  if (path.startsWith("/")) return "The path must be relative";
  const parts = path.split("/");
  if (parts.some((p) => p === "" || p === "." || p === "..")) return "The path has an empty, '.' or '..' segment";
  if (parts[0] === ".meertex" || parts[0] === ".git") return `${parts[0]}/ is reserved`;
  return null;
}

// Where a losing entry of a path clash is shown, like intro.conflict-1a2b3c4d.tex.
export function conflictPath(path, fileId) {
  const slash = path.lastIndexOf("/");
  const head = slash >= 0 ? path.slice(0, slash) : "";
  const name = path.slice(slash + 1);
  const dotAt = name.indexOf(".");
  let stem = dotAt >= 0 ? name.slice(0, dotAt) : name;
  let dot = dotAt >= 0 ? "." : "";
  let suffix = dotAt >= 0 ? name.slice(dotAt + 1) : "";
  if (!stem) { // ".gitignore": the whole name is the stem
    stem = name;
    dot = "";
    suffix = "";
  }
  const renamed = `${stem}.conflict-${fileId.slice(0, 8)}${dot}${suffix}`;
  return head ? `${head}/${renamed}` : renamed;
}

// Python sorts strings by code point, JavaScript by UTF-16 unit. They differ
// only above U+FFFF, but the winner of a clash must be the same everywhere.
function byCodePoint(a, b) {
  const x = Array.from(a);
  const y = Array.from(b);
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    const d = x[i].codePointAt(0) - y[i].codePointAt(0);
    if (d) return d;
  }
  return x.length - y.length;
}

// Every well-formed entry of the "files" map, in id order. `Y` is the yjs
// module (from the editor bundle). An entry is {id, path, kind, text | sha, size},
// with `text` the live Y.Text.
export function entries(files, Y) {
  const out = [];
  for (const id of [...files.keys()].sort(byCodePoint)) {
    const value = files.get(id);
    if (!(value instanceof Y.Map)) continue;
    const path = value.get("path");
    const kind = value.get("kind");
    if (typeof path !== "string" || (kind !== "text" && kind !== "blob")) continue;
    if (pathProblem(path) !== null) continue;
    if (kind === "text") {
      const text = value.get("text");
      out.push({ id, path, kind, text: text instanceof Y.Text ? text : null });
    } else {
      const size = value.get("size");
      out.push({ id, path, kind, sha: value.get("sha") ?? null, size: typeof size === "number" ? Math.trunc(size) : null });
    }
  }
  return out;
}

// The files as they are written to disk, as a Map from path to entry. On a
// clash the entry with the lowest id keeps the path and every other one goes
// to its conflictPath(). A conflict path that is itself taken is skipped.
// Every entry gets `shownAt` (its path here) and `conflict` (whether it lost).
export function materialize(files, Y) {
  const byPath = new Map();
  const losers = [];
  for (const e of entries(files, Y)) {
    if (byPath.has(e.path)) losers.push(e);
    else byPath.set(e.path, { ...e, shownAt: e.path, conflict: false });
  }
  for (const e of losers) {
    const target = conflictPath(e.path, e.id);
    if (!byPath.has(target)) byPath.set(target, { ...e, shownAt: target, conflict: true });
  }
  return byPath;
}
