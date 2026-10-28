// @-mentions in comments (docs/DESIGN.md §13, Mentions).
//
// Typing @ in a comment box lists the paper's members, narrowed by what is
// typed after it, like GitHub's. Up and Down move, Enter or Tab takes one,
// Escape closes the list. The comment then says "@Name", which the server
// reads as a mention and mails that person about. What counts as a mention is
// app/mentions.py's rule, and markdown.js marks mentions by the same rule.
//
// The list is a popover in the top layer, so neither a dialog nor the
// editor's scrolling comment panel clips it.

import { api, projectUrl } from "./api.js";
import { h, clear, nextId } from "./dom.js";
import { session } from "./session.js";
import { avatar } from "./ui.js";

const WORD = /[\p{L}\p{N}_]/u;
const MAX_SHOWN = 8;
// Characters between the @ and the caret. A name of a few words or an email fits.
const MAX_QUERY = 60;
const FRESH_MS = 60_000;

// The members of each project, read again after a minute.
const cache = new Map();

function membersOf(projectId) {
  const hit = cache.get(projectId);
  if (hit && Date.now() - hit.at < FRESH_MS) return hit.promise;
  const promise = api.get(projectUrl(projectId, "/members")).catch(() => []);
  cache.set(projectId, { at: Date.now(), promise });
  return promise;
}

// What goes after the @. That is the name, or the email where another member
// has the same name, so that the mention names one person.
export function mentionText(person, people) {
  const name = (person.name || "").trim();
  if (!name) return person.email;
  const same = people.filter((p) => (p.name || "").trim().toLowerCase() === name.toLowerCase());
  return same.length > 1 ? person.email : name;
}

// The @ the caret stands after, and what was typed since, or null. An @ right
// after a letter is part of an email address and opens nothing.
function typedMention(ta) {
  const caret = ta.selectionStart;
  if (caret !== ta.selectionEnd) return null;
  const from = Math.max(0, caret - MAX_QUERY - 1);
  for (let at = caret - 1; at >= from; at--) {
    const ch = ta.value[at];
    if (ch === "\n") return null;
    if (ch !== "@" || (at > 0 && WORD.test(ta.value[at - 1]))) continue;
    return { at, query: ta.value.slice(at + 1, caret) };
  }
  return null;
}

// How well a member fits what was typed: the start of the name, then of the
// email, then of a later word of the name ("@hoff" finds Didi Hoffmann). 0 is
// not at all.
function fit(person, typed) {
  const q = typed.toLowerCase();
  if (!q) return 1;
  const name = (person.name || "").toLowerCase();
  if (name.startsWith(q)) return 4;
  if (person.email.toLowerCase().startsWith(q)) return 3;
  for (const m of name.matchAll(/\s+(?=\S)/g)) {
    if (name.startsWith(q, m.index + m[0].length)) return 2;
  }
  return 0;
}

const MIRRORED = [
  "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth",
  "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
  "fontFamily", "fontSize", "fontStyle", "fontVariant", "fontWeight", "fontStretch",
  "lineHeight", "letterSpacing", "wordSpacing", "textIndent", "textTransform", "tabSize",
];

// Where a character of the text area is on the screen. The text is laid out
// again in a hidden copy of the box, up to that character.
function charRect(ta, index) {
  const style = getComputedStyle(ta);
  const copy = h("div", { "aria-hidden": "true" });
  for (const p of MIRRORED) copy.style[p] = style[p];
  const borders = parseFloat(style.borderLeftWidth) + parseFloat(style.borderRightWidth);
  Object.assign(copy.style, {
    position: "absolute", top: "0", left: "-9999px", visibility: "hidden", boxSizing: "border-box",
    width: `${ta.clientWidth + borders}px`, whiteSpace: "pre-wrap", overflowWrap: "break-word",
  });
  const mark = h("span", { text: ta.value[index] || "." });
  copy.append(ta.value.slice(0, index), mark);
  document.body.append(copy);
  const box = ta.getBoundingClientRect();
  const rect = {
    left: box.left + parseFloat(style.borderLeftWidth) + mark.offsetLeft - ta.scrollLeft,
    top: box.top + parseFloat(style.borderTopWidth) + mark.offsetTop - ta.scrollTop,
    height: mark.offsetHeight,
  };
  copy.remove();
  return rect;
}

// Completes @-mentions in a text area from the members of the project.
export function attachMentions(ta, projectId) {
  if (!projectId) return;
  const pop = h("div", { class: "mention-pop", popover: "manual", role: "listbox", id: nextId("mention"), "aria-label": "People on this paper" });
  ta.setAttribute("aria-autocomplete", "list");
  ta.setAttribute("aria-controls", pop.id);
  ta.setAttribute("aria-expanded", "false");
  let people = [];
  let shown = [];
  let active = 0;
  let typed = null;
  // Keeps an answer that comes after more typing from opening an old list.
  let asked = 0;

  const isOpen = () => pop.matches(":popover-open");

  // The list stays at the @ while the page or a panel scrolls.
  const follow = () => { if (typed) place(); };

  function close() {
    typed = null;
    ta.setAttribute("aria-expanded", "false");
    ta.removeAttribute("aria-activedescendant");
    window.removeEventListener("scroll", follow, true);
    if (isOpen()) pop.hidePopover();
  }

  function paint() {
    clear(pop);
    shown.forEach((p, i) => {
      const row = h("div", { class: "mention-item", role: "option", id: `${pop.id}-${i}`, "aria-selected": String(i === active) },
        avatar(p, 20),
        h("span", { class: "mention-name", text: p.name || p.email }),
        p.name ? h("span", { class: "mention-email", text: p.email }) : null);
      // mousedown, so the text area keeps the focus and the caret.
      row.addEventListener("mousedown", (e) => {
        e.preventDefault();
        take(i);
      });
      row.addEventListener("mousemove", () => {
        if (active !== i) {
          active = i;
          mark();
        }
      });
      pop.append(row);
    });
    mark();
  }

  function mark() {
    pop.querySelectorAll(".mention-item").forEach((row, i) => row.setAttribute("aria-selected", String(i === active)));
    ta.setAttribute("aria-activedescendant", `${pop.id}-${active}`);
    pop.children[active]?.scrollIntoView({ block: "nearest" });
  }

  function place() {
    const at = charRect(ta, typed.at);
    const w = pop.offsetWidth;
    const ht = pop.offsetHeight;
    const left = Math.min(Math.max(8, at.left), window.innerWidth - w - 8);
    let top = at.top + at.height + 4;
    if (top + ht > window.innerHeight - 8) top = Math.max(8, at.top - ht - 4);
    pop.style.left = `${left}px`;
    pop.style.top = `${top}px`;
  }

  async function update() {
    const found = typedMention(ta);
    if (!found) return close();
    const n = ++asked;
    const members = await membersOf(projectId);
    if (n !== asked) return;
    people = members.filter((m) => m.user_id !== session.me?.id);
    shown = people
      .map((p) => [p, fit(p, found.query)])
      .filter(([, score]) => score > 0)
      .sort((a, b) => b[1] - a[1] || (a[0].name || a[0].email).localeCompare(b[0].name || b[0].email))
      .slice(0, MAX_SHOWN)
      .map(([p]) => p);
    if (!shown.length) return close();
    typed = found;
    active = Math.min(active, shown.length - 1);
    if (!pop.isConnected) ta.after(pop);
    paint();
    if (!isOpen()) {
      pop.showPopover();
      window.addEventListener("scroll", follow, true);
    }
    ta.setAttribute("aria-expanded", "true");
    place();
  }

  function take(i) {
    const person = shown[i];
    if (!person || !typed) return;
    const text = `@${mentionText(person, people)} `;
    const end = ta.selectionStart;
    ta.focus();
    ta.setSelectionRange(typed.at, end);
    // insertText keeps the browser's undo. setRangeText where it is missing.
    if (!document.execCommand("insertText", false, text)) {
      ta.setRangeText(text, typed.at, end, "end");
      ta.dispatchEvent(new Event("input", { bubbles: true }));
    }
    close();
  }

  ta.addEventListener("input", () => {
    active = 0;
    update();
  });
  ta.addEventListener("click", () => update());
  ta.addEventListener("blur", close);
  // In the capture phase, so the keys reach the list before the comment box's
  // own Enter and Escape.
  ta.addEventListener("keydown", (e) => {
    if (!isOpen() || e.isComposing) return;
    const own = () => {
      e.preventDefault();
      e.stopImmediatePropagation();
    };
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      own();
      active = (active + (e.key === "ArrowDown" ? 1 : shown.length - 1)) % shown.length;
      mark();
    } else if ((e.key === "Enter" || e.key === "Tab") && !e.ctrlKey && !e.metaKey && !e.shiftKey && !e.altKey) {
      own();
      take(active);
    } else if (e.key === "Escape") {
      own();
      close();
    } else if (e.key === "ArrowLeft" || e.key === "ArrowRight" || e.key === "Home" || e.key === "End") {
      // The caret leaves the name, and the click handler is not called for keys.
      requestAnimationFrame(update);
    } else if (e.key === "Enter") {
      close();
    }
  }, { capture: true });
}
