// The last compile's problems in the editor's text, like Overleaf (docs/DESIGN.md
// §14, In the editor). Errors and warnings with a line become diagnostics of
// @codemirror/lint on their whole line: an icon in the gutter with the message
// on hover, the same message when hovering the text, and a light background
// on the line (red for errors, amber for warnings). Bad boxes are too many for
// the text and stay in the PDF pane's list only.
//
// A problem is kept as two Yjs relative positions in its file's text, taken
// when the compile is in. They move with everybody's edits, so a file opened
// again later shows its problems on the right lines. While a file is open,
// CodeMirror moves them along. The next compile replaces all of them.
//
//   const problems = editorProblems({ cm: () => cm, doc: () => ydoc, files: () => byPath,
//     view: () => view, text: () => boundText });
//   extensions: problems.extension()    in every text file's EditorState
//   problems.update(compile.problems)   a compile is in
//   problems.opened()                   a text file was bound to the view
//   problems.ready()                    the document arrived, for problems that came before it

const LEVELS = new Set(["error", "warning"]);

export function editorProblems({ cm, doc, files, view, text }) {
  let last = null; // the newest compile's problems, kept until the files are there
  let byText = new Map(); // Y.Text -> [{from, to, severity, message}] in relative positions
  let lineField = null;

  // The line of a diagnostic gets a class, so its background is coloured. An
  // error wins over a warning on the same line.
  function makeField(lib) {
    const build = (state) => {
      const lines = new Map();
      lib.forEachDiagnostic(state, (d, from) => {
        const at = state.doc.lineAt(Math.min(from, state.doc.length)).from;
        if (lines.get(at) !== "error") lines.set(at, d.severity);
      });
      return lib.Decoration.set([...lines].map(([at, severity]) =>
        lib.Decoration.line({ class: `cm-problemLine is-${severity}` }).range(at)), true);
    };
    lineField = lib.StateField.define({
      create: build,
      update: (deco, tr) => (tr.docChanged || tr.effects.length ? build(tr.state) : deco),
      provide: (f) => lib.EditorView.decorations.from(f),
    });
  }

  function extension() {
    const lib = cm();
    if (!lineField) makeField(lib);
    return [lib.lintGutter(), lineField];
  }

  function lineRange(content, n) {
    let from = 0;
    for (let i = 1; i < n; i++) {
      const next = content.indexOf("\n", from);
      if (next < 0) return null;
      from = next + 1;
    }
    let to = content.indexOf("\n", from);
    if (to < 0) to = content.length;
    if (to > from && content[to - 1] === "\r") to -= 1;
    return { from, to };
  }

  function update(problems) {
    last = problems || [];
    const entries = files();
    if (!entries?.size) return;
    const { Y } = cm();
    byText = new Map();
    for (const p of last) {
      if (!LEVELS.has(p.level) || !p.path || !p.line) continue;
      const entry = entries.get(p.path);
      if (entry?.kind !== "text" || !entry.text) continue;
      const range = lineRange(entry.text.toString(), p.line);
      if (!range) continue; // a line past the end
      const list = byText.get(entry.text) || [];
      list.push({
        from: Y.createRelativePositionFromTypeIndex(entry.text, range.from),
        // The line's end stays in front of text typed after it.
        to: Y.createRelativePositionFromTypeIndex(entry.text, range.to, -1),
        severity: p.level,
        message: p.message,
        source: p.source || "LaTeX",
      });
      byText.set(entry.text, list);
    }
    last = null;
    opened();
  }

  // The open file's problems as diagnostics.
  function opened() {
    const v = view();
    const t = text();
    if (!v || !t) return;
    const lib = cm();
    const length = v.state.doc.length;
    const diagnostics = [];
    for (const d of byText.get(t) || []) {
      const a = lib.Y.createAbsolutePositionFromRelativePosition(d.from, doc());
      const b = lib.Y.createAbsolutePositionFromRelativePosition(d.to, doc());
      if (!a || !b || a.type !== t || b.type !== t) continue;
      const from = Math.min(a.index, length);
      diagnostics.push({ from, to: Math.min(Math.max(b.index, from), length), severity: d.severity, message: d.message, source: d.source });
    }
    v.dispatch(lib.setDiagnostics(v.state, diagnostics));
  }

  return {
    extension,
    update,
    opened,
    ready() {
      if (last) update(last);
    },
  };
}
