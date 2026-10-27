// What the browser editor (app/static/js/pages/editor.js) takes from npm, in
// one module, because the web app has no build step. Exports are listed one by
// one, so the bundle holds only what the page uses. yjs is re-exported whole.
// The page needs Y.Doc, Y.Map, Y.Text and Y.UndoManager, and y-websocket and
// y-codemirror.next pull in most of it anyway. StateField, StateEffect and
// Decoration draw the comments' highlights (app/static/js/editor-comments.js),
// and @codemirror/lint the compile's problems (app/static/js/editor-problems.js).
// The Files view of a review (app/static/js/pages/review-files.js) colours its
// LaTeX with the same mode, through highlightTree and tagHighlighter.

export * as Y from "yjs";
export { WebsocketProvider } from "y-websocket";
export { yCollab, yUndoManagerKeymap } from "y-codemirror.next";

export { Compartment, EditorSelection, EditorState, StateEffect, StateField } from "@codemirror/state";
export {
  Decoration,
  EditorView,
  drawSelection,
  dropCursor,
  highlightActiveLine,
  highlightActiveLineGutter,
  highlightSpecialChars,
  keymap,
  lineNumbers,
} from "@codemirror/view";
export { defaultKeymap, indentWithTab } from "@codemirror/commands";
export { highlightSelectionMatches, search, searchKeymap } from "@codemirror/search";
export {
  HighlightStyle,
  StreamLanguage,
  bracketMatching,
  indentOnInput,
  indentUnit,
  syntaxHighlighting,
} from "@codemirror/language";
export { acceptCompletion, autocompletion, snippet, startCompletion } from "@codemirror/autocomplete";
export { forEachDiagnostic, lintGutter, setDiagnostics } from "@codemirror/lint";
export { highlightTree, tagHighlighter, tags } from "@lezer/highlight";
export { stex } from "@codemirror/legacy-modes/mode/stex";
