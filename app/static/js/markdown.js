// A small, safe Markdown for project notes: headings, paragraphs with line
// breaks, lists, quotes, code, emphasis and links. The text is escaped first
// and only then given markup, so nothing in a note can become a tag or a
// script. Links are kept only for http(s) and mailto.

import { escapeHtml } from "./dom.js";

function inline(escaped) {
  const codes = [];
  let s = escaped.replace(/`([^`]+)`/g, (_, c) => {
    codes.push(c);
    return `\u0000${codes.length - 1}\u0000`;
  });
  // Finished links are set aside like code, so the emphasis rules below never
  // reach into a URL (an "_" or "**" in it would otherwise become markup).
  const links = [];
  const keep = (html) => {
    links.push(html);
    return `\u0001${links.length - 1}\u0001`;
  };
  s = s.replace(/\[([^\]]+)\]\(((?:https?:\/\/|mailto:)[^\s)]+)\)/g,
    (_, text, url) => keep(`<a href="${url}" rel="noopener noreferrer" target="_blank">${text}</a>`));
  // Bare URLs that are not already inside a link.
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)\u0001]+)/g,
    (_, pre, url) => pre + keep(`<a href="${url}" rel="noopener noreferrer" target="_blank">${url}</a>`));
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*\w])\*([^*\s][^*]*)\*(?!\w)/g, "$1<em>$2</em>");
  s = s.replace(/(^|[^_\w])_([^_\s][^_]*)_(?!\w)/g, "$1<em>$2</em>");
  s = s.replace(/\u0001(\d+)\u0001/g, (_, i) => links[Number(i)]);
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[Number(i)]}</code>`);
}

export function renderMarkdown(text) {
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const buf = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i += 1; }
      i += 1;
      out.push(`<pre><code>${escapeHtml(buf.join("\n"))}</code></pre>`);
      continue;
    }
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      const n = heading[1].length;
      out.push(`<h${n}>${inline(escapeHtml(heading[2]))}</h${n}>`);
      i += 1;
      continue;
    }
    if (/^\s*[-*+]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items = [];
      const re = ordered ? /^\s*\d+[.)]\s+(.*)$/ : /^\s*[-*+]\s+(.*)$/;
      while (i < lines.length && re.test(lines[i])) {
        items.push(`<li>${inline(escapeHtml(re.exec(lines[i])[1]))}</li>`);
        i += 1;
      }
      out.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
      continue;
    }
    if (/^>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^>\s?/, "")); i += 1; }
      out.push(`<blockquote><p>${buf.map((l) => inline(escapeHtml(l))).join("<br>")}</p></blockquote>`);
      continue;
    }
    if (!line.trim()) { i += 1; continue; }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(```|#{1,4}\s|\s*[-*+]\s+|\s*\d+[.)]\s+|>)/.test(lines[i])) {
      buf.push(lines[i]);
      i += 1;
    }
    out.push(`<p>${buf.map((l) => inline(escapeHtml(l))).join("<br>")}</p>`);
  }
  return out.join("\n");
}
