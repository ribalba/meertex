// DOM helpers. Everything a user or the server wrote reaches the page as a text
// node or an attribute value, so it is escaped by the browser. The one escape
// hatch, `html`, is for markup this app assembles itself (markdown.js output).

export function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  let value;
  for (const [key, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (key === "class") node.className = Array.isArray(v) ? v.filter(Boolean).join(" ") : v;
    else if (key === "text") node.textContent = v;
    else if (key === "html") node.innerHTML = v;
    else if (key === "value") value = v;
    else if (key === "dataset") Object.assign(node.dataset, v);
    else if (key === "style" && typeof v === "object") Object.assign(node.style, v);
    else if (key.startsWith("on") && typeof v === "function") node.addEventListener(key.slice(2).toLowerCase(), v);
    else if (key === "checked" || key === "selected" || key === "disabled" || key === "hidden") node[key] = Boolean(v);
    else node.setAttribute(key, v === true ? "" : v);
  }
  append(node, children);
  // After the children, so a <select> already has the option it should show.
  if (value !== undefined) node.value = value;
  return node;
}

export function append(node, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false || c === "") continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

export function frag(...children) {
  return append(document.createDocumentFragment(), children);
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

let uid = 0;
export function nextId(prefix = "f") {
  uid += 1;
  return `${prefix}-${uid}`;
}

export function debounce(fn, ms) {
  let t = null;
  const wrapped = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
  wrapped.cancel = () => clearTimeout(t);
  return wrapped;
}

export function formatBytes(n) {
  if (n === null || n === undefined) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function plural(n, one, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}
