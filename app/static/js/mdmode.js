// Markdown colours for the editor and the review's Files view: a mode for
// CodeMirror's StreamLanguage, like the stex mode of LaTeX files. It marks
// what a writer looks for (headings, emphasis, code, links, math, lists and
// quotes) one line at a time, and does not parse Markdown fully. Its token
// names are tags of @lezer/highlight, which editor.js and review-files.js
// give their colours.
//
// The PDF of a Markdown file is pandoc's (compile/service.py), so this only
// has to look right, not decide anything.

const FENCE = /^\s{0,3}(`{3,}|~{3,})/;
const HEADING = /^\s{0,3}#{1,6}(?=\s|$)/;
const SETEXT = /^\s{0,3}(=+|-+)\s*$/;
const RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
const TABLE_RULE = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
const LIST = /^\s*(?:[-*+]|\d{1,9}[.)])\s+(?:\[[ xX]\]\s+)?/;
const QUOTE = /^\s*>\s?/;

export const markdownMode = {
  name: "markdown",
  startState: () => ({ first: true, front: false, fence: null, math: false, quote: false }),
  blankLine(state) {
    state.first = false;
    state.quote = false;
  },
  token(stream, state) {
    if (stream.sol()) {
      const first = state.first;
      state.first = false;
      state.quote = false;
      // The front matter: title, author and the other settings of the PDF.
      if (first && stream.match(/^---\s*$/)) {
        state.front = true;
        return "meta";
      }
      if (state.front) {
        if (stream.match(/^(---|\.\.\.)\s*$/)) state.front = false;
        else stream.skipToEnd();
        return "meta";
      }
      if (state.fence) {
        const close = stream.match(FENCE, false);
        if (close && close[1][0] === state.fence[0] && close[1].length >= state.fence.length) {
          state.fence = null;
          stream.skipToEnd();
          return "meta";
        }
        stream.skipToEnd();
        return "monospace";
      }
      const open = stream.match(FENCE);
      if (open) {
        state.fence = open[1];
        stream.skipToEnd();
        return "meta";
      }
      // A display formula between lines of $$.
      if (state.math) {
        if (stream.string.includes("$$")) state.math = false;
        stream.skipToEnd();
        return "string";
      }
      if (stream.match(/^\s*\$\$/, false)) {
        state.math = !stream.string.slice(stream.string.indexOf("$$") + 2).includes("$$");
        stream.skipToEnd();
        return "string";
      }
      if (stream.match(HEADING)) {
        stream.skipToEnd();
        return "heading";
      }
      if (stream.match(RULE) || stream.match(SETEXT) || stream.match(TABLE_RULE)) return "contentSeparator";
      if (stream.match(QUOTE)) {
        state.quote = true;
        return "quote";
      }
      if (stream.match(LIST)) return "list";
    }
    const plain = state.quote ? "quote" : null;
    if (stream.match(/^\\./)) return plain;
    if (stream.match(/^(`+)[\s\S]*?\1(\{=[a-z]+\})?/)) return "monospace";
    if (stream.match(/^\$[^$\s][^$]*?\$/) || stream.match(/^\$\$.*?\$\$/)) return "string";
    if (stream.match(/^(\*\*|__)(?=\S).*?\S\1/)) return "strong";
    if (stream.match(/^\*(?=[^\s*]).*?[^\s\\]\*/) || stream.match(/^_(?=[^\s_]).*?[^\s\\]_(?!\w)/)) return "emphasis";
    if (stream.match(/^~~(?=\S).*?\S~~/)) return "strikethrough";
    if (stream.match(/^\[\^[^\]\s]+\]/)) return "link";
    if (stream.match(/^!?\[[^\]\n]*\](\([^)\n]*\)|\[[^\]\n]*\])/)) return "link";
    if (stream.match(/^<(https?|mailto):[^>\s]+>/) || stream.match(/^https?:\/\/[^\s<>)]+/)) return "url";
    if (stream.match(/^<!--.*?-->/)) return "comment";
    if (stream.match(/^\{[#.=][^}\n]*\}/)) return "meta";
    stream.next();
    stream.eatWhile(/[^\\`$*_~[!<{h]/);
    return plain;
  },
};
