// Client-side routing on history.pushState. Same-site links are intercepted
// so moving between pages and tabs does not reload the app, and Back and
// Forward re-render the page the URL names.

import { clearFlashes, showQueuedFlashes, flashBox, setTitle, setCrumbs } from "./ui.js";
import { h, clear } from "./dom.js";

const routes = [];
let token = 0;
let cleanups = [];
let lastPath = null;
let popHandler = null;

export function route(pattern, handler) {
  routes.push({ pattern, handler });
}

export function navigate(url, { replace = false, keepScroll = false } = {}) {
  const target = new URL(url, location.href);
  if (target.origin !== location.origin) {
    location.assign(target.href);
    return;
  }
  // Remember where the reader was, so Back returns to the same spot.
  history.replaceState({ ...(history.state || {}), scroll: window.scrollY }, "");
  const href = target.pathname + target.search + target.hash;
  if (replace) history.replaceState({}, "", href);
  else history.pushState({}, "", href);
  render({ scroll: keepScroll ? null : 0 });
}

// Update the query string without rendering again (filters as they are typed).
export function setQuery(params, { push = false } = {}) {
  const url = new URL(location.href);
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined || v === "" || v === false) url.searchParams.delete(k);
    else url.searchParams.set(k, v === true ? "1" : v);
  }
  const href = url.pathname + url.search;
  if (push) history.pushState({}, "", href);
  else history.replaceState(history.state, "", href);
}

// "2027/Sub%20mitted" -> "2027/Sub mitted", tolerant of a bad escape.
export function decodePath(raw) {
  return String(raw || "")
    .split("/")
    .map((s) => { try { return decodeURIComponent(s); } catch { return s; } })
    .join("/");
}

export function onLeave(fn) {
  cleanups.push(fn);
}

// A page that changes the URL itself (the editor, when another file opens) can
// take Back and Forward between its own URLs. `fn(location)` returns true when
// it did, and the page is not rendered again. It is dropped on the next render.
export function onPop(fn) {
  popHandler = fn;
}

export async function render({ scroll = null } = {}) {
  const mine = ++token;
  cleanups.forEach((fn) => { try { fn(); } catch (e) { console.error(e); } });
  cleanups = [];
  popHandler = null;
  const main = document.getElementById("main");
  // Matched still encoded. Handlers decode segment by segment, so a file named
  // "a%2Fb" or "x#1" survives the trip.
  const path = location.pathname.replace(/\/+$/, "") || "/";
  if (path !== lastPath) {
    clearFlashes();
    setCrumbs([]);
  }
  lastPath = path;
  const ctx = {
    path,
    query: new URLSearchParams(location.search),
    main,
    alive: () => mine === token,
  };
  const match = routes.map((r) => ({ r, m: r.pattern.exec(path) })).find((x) => x.m);
  try {
    if (!match) {
      notFound(main);
    } else {
      ctx.params = match.m.slice(1);
      await match.r.handler(ctx);
    }
  } catch (err) {
    if (!ctx.alive()) return;
    if (err?.status === 401) return;
    if (err?.status !== 404) console.error(err);
    if (err?.status === 404) notFound(main, err.message);
    else {
      clear(main).append(h("div", { class: "container page" }, flashBox(err?.message || String(err), "error")));
    }
  }
  if (!ctx.alive()) return;
  showQueuedFlashes();
  const target = location.hash ? document.getElementById(decodeURIComponent(location.hash.slice(1))) : null;
  if (target) target.scrollIntoView({ block: "start" });
  else if (scroll !== null) window.scrollTo(0, scroll);
}

function notFound(main, message = null) {
  setTitle("Not found");
  clear(main).append(
    h("div", { class: "container container-sm page" },
      h("div", { class: "blankslate" },
        h("h3", { text: "This page does not exist" }),
        h("p", { text: message && message !== "Not found" ? message : "Or you do not have access to it. Papers are only visible to their members." }),
        h("a", { class: "btn", href: "/", text: "Go to your papers" }),
      )),
  );
}

export function start() {
  history.scrollRestoration = "manual";
  window.addEventListener("popstate", (e) => {
    try {
      if (popHandler?.(location)) return;
    } catch (err) {
      console.error(err);
    }
    render({ scroll: e.state?.scroll ?? 0 });
  });
  document.addEventListener("click", (e) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest("a[href]");
    if (!a || a.target || a.hasAttribute("download") || a.dataset.external !== undefined) return;
    const url = new URL(a.href, location.href);
    if (url.origin !== location.origin) return;
    if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/static/") || url.pathname === "/login") return;
    // A link to the same page with only a #fragment is left to the browser.
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return;
    e.preventDefault();
    navigate(url.href);
  });
  render({ scroll: history.state?.scroll ?? 0 });
}
