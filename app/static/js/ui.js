// Shared pieces of the interface: flashes, dialogs, menus, labels and the
// small Primer components every page uses.

import { h, clear, nextId } from "./dom.js";
import { icon } from "./icons.js";

// --- Flash messages ---------------------------------------------------------

const FLASH_ICONS = { info: "info", success: "check-circle", warn: "alert", error: "alert" };
let queued = [];

function flashRegion() {
  return document.getElementById("flash-region");
}

export function flashBox(content, kind = "info", { dismissible = false, onClose = null, iconName = null } = {}) {
  const box = h("div", { class: `flash${kind === "info" ? "" : ` flash-${kind}`}`, role: kind === "error" ? "alert" : "status" },
    icon(iconName || FLASH_ICONS[kind] || "info"),
    h("div", { class: "flash-body" }, content),
  );
  if (dismissible) {
    box.append(h("button", {
      type: "button", class: "btn-octicon flash-close", "aria-label": "Dismiss",
      onclick: () => { box.remove(); onClose?.(); },
    }, icon("x")));
  }
  return box;
}

export function flash(content, kind = "info", { timeout = kind === "success" ? 6000 : 0 } = {}) {
  const region = flashRegion();
  if (!region) return null;
  // A new confirmation replaces the last one, so saving three times does not
  // stack three green boxes. Errors stay until they are dismissed.
  if (kind === "success" || kind === "info") {
    region.querySelectorAll(".flash:not(.flash-error):not(.flash-warn)").forEach((f) => f.remove());
  }
  const box = flashBox(content, kind, { dismissible: true });
  region.append(box);
  if (timeout) setTimeout(() => box.remove(), timeout);
  return box;
}

export function flashError(err) {
  if (err?.name === "AbortError") return null;
  // A 401 is already on its way to the sign-in page.
  if (err?.status === 401) return null;
  return flash(err?.message || String(err), "error");
}

export function clearFlashes() {
  const region = flashRegion();
  if (region) clear(region);
}

// For a message that belongs to the page we are about to open ("Paper created").
export function queueFlash(content, kind = "success") {
  queued.push([content, kind]);
}

export function showQueuedFlashes() {
  const items = queued;
  queued = [];
  items.forEach(([c, k]) => flash(c, k, { timeout: 0 }));
}

// --- Dialogs ----------------------------------------------------------------

// A modal on the native <dialog>, which brings focus trapping and Escape.
export function openDialog({ title, body, footer = null, wide = false, onClose = null }) {
  const titleId = nextId("dlg");
  const dlg = h("dialog", { class: `dialog${wide ? " dialog-wide" : ""}`, "aria-labelledby": titleId },
    h("div", { class: "dialog-header" },
      h("h2", { class: "dialog-title", id: titleId, text: title }),
      h("button", { type: "button", class: "btn-octicon", "aria-label": "Close", onclick: () => close() }, icon("x")),
    ),
    h("div", { class: "dialog-body" }, body),
    footer ? h("div", { class: "dialog-footer" }, footer) : null,
  );
  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    dlg.close();
    dlg.remove();
    onClose?.();
  }
  dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(); });
  // A click on the backdrop lands on the dialog element itself.
  dlg.addEventListener("mousedown", (e) => { if (e.target === dlg) close(); });
  document.body.append(dlg);
  dlg.showModal();
  return { el: dlg, close };
}

export function confirmDialog({ title, message, confirmText = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    let result = false;
    const ok = h("button", { type: "button", class: `btn ${danger ? "btn-danger" : "btn-primary"}`, text: confirmText,
      onclick: () => { result = true; d.close(); } });
    const d = openDialog({
      title,
      body: typeof message === "string" ? h("p", { text: message }) : message,
      footer: [h("button", { type: "button", class: "btn", text: "Cancel", onclick: () => d.close() }), ok],
      onClose: () => resolve(result),
    });
    ok.focus();
  });
}

// --- Dropdown menus ---------------------------------------------------------

// A menu on <details>, like GitHub's details-menu. Items are
// {text, icon, onClick, href, danger, checked, disabled} or "divider" or
// {header: "..."}.
export function dropdown({ summary, summaryClass = "btn", label = null, items, align = "left", wide = false, cls = "" }) {
  const menu = h("div", { class: `dropdown-menu${align === "right" ? " dropdown-menu-right" : ""}${wide ? " dropdown-wide" : ""}`, role: "menu" });
  const details = h("details", { class: `dropdown ${cls}`.trim() },
    h("summary", { class: summaryClass, "aria-haspopup": "menu", "aria-label": label }, summary),
    menu,
  );
  const fill = () => {
    clear(menu);
    const list = typeof items === "function" ? items() : items;
    for (const item of list) {
      if (!item) continue;
      if (item === "divider") { menu.append(h("div", { class: "dropdown-divider", role: "separator" })); continue; }
      if (item.header) { menu.append(h("div", { class: "dropdown-header", text: item.header })); continue; }
      if (item.node) { menu.append(item.node); continue; }
      const checkable = item.checked !== undefined;
      const content = [
        checkable ? icon("check", { cls: "check" }) : null,
        item.icon ? icon(item.icon) : null,
        h("span", { class: "flex-1 truncate", text: item.text }),
        item.trailing || null,
      ];
      const attrs = {
        class: `dropdown-item${item.danger ? " danger" : ""}`,
        role: checkable ? "menuitemradio" : "menuitem",
        "aria-checked": checkable ? String(Boolean(item.checked)) : null,
      };
      const node = item.href
        ? h("a", { ...attrs, href: item.href, download: item.download || null, onclick: () => { details.open = false; } }, content)
        : h("button", { ...attrs, type: "button", disabled: item.disabled, onclick: () => { details.open = false; item.onClick?.(); } }, content);
      menu.append(node);
    }
  };
  details.addEventListener("toggle", () => {
    if (!details.open) return;
    fill();
    // Close other open menus, one menu at a time like GitHub.
    document.querySelectorAll("details.dropdown[open]").forEach((d) => { if (d !== details) d.open = false; });
    menu.querySelector(".dropdown-item, input")?.focus();
  });
  menu.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { details.open = false; details.querySelector("summary").focus(); }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const all = [...menu.querySelectorAll(".dropdown-item:not(:disabled)")];
      const i = all.indexOf(document.activeElement);
      const next = all[(i + (e.key === "ArrowDown" ? 1 : -1) + all.length) % all.length];
      next?.focus();
      e.preventDefault();
    }
  });
  return details;
}

// One listener closes any open menu on an outside click.
document.addEventListener("click", (e) => {
  document.querySelectorAll("details.dropdown[open]").forEach((d) => {
    if (!d.contains(e.target)) d.open = false;
  });
});

// --- Small components -------------------------------------------------------

export const OUTCOMES = {
  accepted: { text: "Accepted", cls: "label-success" },
  rejected: { text: "Rejected", cls: "label-danger" },
  revision: { text: "Revision", cls: "label-attention" },
  withdrawn: { text: "Withdrawn", cls: "label-secondary" },
  not_submitted: { text: "Not submitted", cls: "label-secondary" },
};

export function outcomeLabel(outcome) {
  const o = OUTCOMES[outcome];
  if (!o) return null;
  return h("span", { class: `Label ${o.cls}`, text: o.text });
}

export function counter(n, cls = "") {
  return h("span", { class: `counter ${cls}`.trim(), text: String(n ?? 0) });
}

export function topic(label, href = null) {
  return href ? h("a", { class: "topic", href, text: label }) : h("span", { class: "topic", text: label });
}

export function topicList(labels, hrefFor = null) {
  if (!labels?.length) return null;
  return h("div", { class: "topic-list" }, labels.map((l) => topic(l, hrefFor ? hrefFor(l) : null)));
}

const AVATAR_COLORS = ["#8a6a3b", "#2f6f9f", "#6b7d2a", "#8250df", "#bf5a2a", "#1a7f73", "#b35900", "#4b5fa8", "#9a3c6e", "#3c7d4f"];

export function avatar(person, size = 20) {
  const who = person?.name || person?.email || "?";
  const parts = who.replace(/@.*/, "").split(/[\s._-]+/).filter(Boolean);
  const initials = ((parts[0]?.[0] || "?") + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase();
  let hash = 0;
  for (const c of person?.email || who) hash = (hash * 31 + c.charCodeAt(0)) >>> 0;
  const cls = size >= 40 ? " avatar-40" : size >= 32 ? " avatar-32" : "";
  return h("span", {
    class: `avatar${cls}`,
    style: { background: AVATAR_COLORS[hash % AVATAR_COLORS.length] },
    "aria-hidden": "true",
    text: initials,
  });
}

export function personName(person) {
  if (!person) return "someone";
  return person.name || person.email || "someone";
}

export function spinner(large = false) {
  return h("span", { class: `spinner${large ? " spinner-lg" : ""}`, role: "status", "aria-label": "Loading" });
}

export function loading() {
  return h("div", { class: "loading" }, spinner(true));
}

export function blankslate({ iconName = "info", title, text = null, action = null }) {
  return h("div", { class: "blankslate" },
    icon(iconName, { size: 24 }),
    h("h3", { text: title }),
    text ? h("p", { text }) : null,
    action,
  );
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Without the clipboard API (http on a LAN address) a hidden textarea does it.
    const ta = h("textarea", { style: { position: "fixed", opacity: "0" } });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    ta.remove();
    return ok;
  }
}

export function copyButton(getText, { label = "Copy", cls = "btn-octicon", showText = false } = {}) {
  const btn = h("button", { type: "button", class: cls, "aria-label": label, title: label },
    icon("copy"), showText ? h("span", { text: label }) : null);
  btn.addEventListener("click", async () => {
    const ok = await copyText(typeof getText === "function" ? getText() : getText);
    if (!ok) { flash("Could not copy. Select the text and copy it by hand.", "warn"); return; }
    btn.replaceChildren(icon("check", { cls: "color-success" }), showText ? h("span", { text: "Copied" }) : "");
    btn.setAttribute("aria-label", "Copied");
    setTimeout(() => {
      btn.replaceChildren(icon("copy"), showText ? h("span", { text: label }) : "");
      btn.setAttribute("aria-label", label);
    }, 1600);
  });
  return btn;
}

// A command with a copy button, like the clone URL field on GitHub.
export function commandLine(cmd, caption = null) {
  return h("div", {},
    caption ? h("div", { class: "cmd-caption", text: caption }) : null,
    h("div", { class: "copy-field" },
      h("code", { class: "cmd", text: cmd }),
      copyButton(cmd, { label: "Copy command", cls: "btn" }),
    ),
  );
}

// A labelled form field. `input` gets an id when it has none.
export function field(labelText, input, { hint = null, optional = false, cls = "" } = {}) {
  if (!input.id) input.id = nextId("in");
  return h("div", { class: `form-group ${cls}`.trim() },
    h("label", { for: input.id }, labelText, optional ? h("span", { class: "optional", text: " (optional)" }) : null),
    input,
    hint ? h("div", { class: "form-hint" }, hint) : null,
  );
}

export function textInput(attrs = {}) {
  return h("input", { type: "text", class: "form-control input-block", ...attrs });
}

export function select(options, value, attrs = {}) {
  return h("select", { class: "form-select input-block", ...attrs, value },
    options.map(([v, t, extra]) => h("option", { value: v, ...(extra || {}) }, t)));
}

// Disable a button while an action runs, and always enable it again.
export async function busy(button, fn) {
  const was = button?.disabled;
  if (button) button.disabled = true;
  try {
    return await fn();
  } finally {
    if (button) button.disabled = was || false;
  }
}

export function setTitle(...parts) {
  document.title = [...parts.filter(Boolean), "meertex"].join(" · ");
}

// The breadcrumb next to the logo ("meertex / 2027-icse-green-llm").
export function setCrumbs(items = []) {
  const nav = document.getElementById("header-crumbs");
  if (!nav) return;
  nav.replaceChildren();
  for (const item of items) {
    nav.append(h("span", { class: "sep", text: "/", "aria-hidden": "true" }));
    nav.append(item.href ? h("a", { href: item.href, class: "truncate", text: item.text }) : h("span", { class: "truncate", text: item.text }));
  }
}
