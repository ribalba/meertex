// A small, safe Markdown for project notes: headings, paragraphs with line
// breaks, lists, quotes, code, emphasis and links. The text is escaped first
// and only then given markup, so nothing in a note can become a tag or a
// script. Links are kept only for http(s) and mailto. @-mentions of the
// people given are marked (docs/DESIGN.md §13, Mentions).

import { escapeHtml } from "./dom.js";

const WORD = /[\p{L}\p{N}_]/u;

// The rule of app/mentions.py. An @ that does not follow a letter, a digit or
// "_", then one of the labels (longest first), not running on into a word. The
// text and the labels are escaped alike, so they compare as they are.
function markMentions(s, labels, keep) {
  if (!labels.length || !s.includes("@")) return s;
  let out = "";
  let done = 0;
  for (let at = s.indexOf("@"); at >= 0; at = s.indexOf("@", at + 1)) {
    if (at < done || (at > 0 && WORD.test(s[at - 1]))) continue;
    const label = labels.find((l) => s.slice(at + 1, at + 1 + l.length).toLowerCase() === l.toLowerCase()
      && !WORD.test(s[at + 1 + l.length] || ""));
    if (!label) continue;
    const end = at + 1 + label.length;
    out += s.slice(done, at) + keep(`<span class="mention">${s.slice(at, end)}</span>`);
    done = end;
  }
  return out + s.slice(done);
}

function inline(escaped, labels = []) {
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
  s = markMentions(s, labels, keep);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*\w])\*([^*\s][^*]*)\*(?!\w)/g, "$1<em>$2</em>");
  s = s.replace(/(^|[^_\w])_([^_\s][^_]*)_(?!\w)/g, "$1<em>$2</em>");
  s = s.replace(/\u0001(\d+)\u0001/g, (_, i) => links[Number(i)]);
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[Number(i)]}</code>`);
}

// mentions are the people the text mentions, as the server sent them.
export function renderMarkdown(text, { mentions = [] } = {}) {
  const labels = [...new Set(mentions.flatMap((p) => [p.name, p.email]).filter(Boolean).map((l) => escapeHtml(l.trim())))]
    .sort((a, b) => b.length - a.length);
  const span = (t) => inline(escapeHtml(t), labels);
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
      out.push(`<h${n}>${span(heading[2])}</h${n}>`);
      i += 1;
      continue;
    }
    if (/^\s*[-*+]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items = [];
      const re = ordered ? /^\s*\d+[.)]\s+(.*)$/ : /^\s*[-*+]\s+(.*)$/;
      while (i < lines.length && re.test(lines[i])) {
        items.push(`<li>${span(re.exec(lines[i])[1])}</li>`);
        i += 1;
      }
      out.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
      continue;
    }
    if (/^>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^>\s?/, "")); i += 1; }
      out.push(`<blockquote><p>${buf.map(span).join("<br>")}</p></blockquote>`);
      continue;
    }
    if (!line.trim()) { i += 1; continue; }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(```|#{1,4}\s|\s*[-*+]\s+|\s*\d+[.)]\s+|>)/.test(lines[i])) {
      buf.push(lines[i]);
      i += 1;
    }
    out.push(`<p>${buf.map(span).join("<br>")}</p>`);
  }
  return out.join("\n");
}
