// The web app starts here. It loads who is signed in, draws the header and
// hands the URL to the router.

import { api, ApiError } from "./api.js";
import { h, clear } from "./dom.js";
import { icon } from "./icons.js";
import { session } from "./session.js";
import { route, start, navigate, decodePath } from "./router.js";
import { avatar, dropdown, flashBox, flashError } from "./ui.js";

import * as home from "./pages/home.js";
import * as newPaper from "./pages/new.js";
import * as account from "./pages/account.js";
import * as project from "./pages/project.js";
import * as timeline from "./pages/timeline.js";

function renderHeader() {
  const me = session.me;
  const actions = clear(document.getElementById("header-actions"));

  const search = h("form", { class: "header-search", role: "search" },
    h("input", { class: "form-control", type: "search", name: "q", placeholder: "Search papers", "aria-label": "Search papers" }));
  search.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = search.q.value.trim();
    navigate(q ? `/?q=${encodeURIComponent(q)}` : "/");
  });

  const userMenu = dropdown({
    summary: [avatar(me, 32), icon("triangle-down", { cls: "color-muted" })],
    summaryClass: "avatar-btn",
    label: "Your account",
    align: "right",
    items: [
      { node: h("div", { class: "dropdown-text" }, "Signed in as ", h("strong", { text: me.email })) },
      "divider",
      { text: "Your papers", icon: "book", href: "/" },
      { text: "Timeline", icon: "calendar", href: "/timeline" },
      { text: "New paper", icon: "plus", href: "/new" },
      { text: "Settings", icon: "gear", href: "/settings" },
      "divider",
      { text: "Sign out", icon: "sign-out", onClick: signOut },
    ],
  });

  actions.append(
    search,
    h("a", { class: "btn btn-sm", href: "/new", "aria-label": "New paper", title: "New paper" },
      icon("plus"), h("span", { class: "hide-sm", text: "New paper" })),
    userMenu,
  );
}

async function signOut() {
  try {
    await api.post("/api/auth/logout");
  } catch (err) {
    flashError(err);
  }
  location.assign("/login");
}

async function boot() {
  // A shared timeline is public. It shows without a session and without the
  // rest of the app, not even the header's search and menu (DESIGN §3).
  const shared = /^\/timeline\/([^/]+)\/?$/.exec(location.pathname);
  if (shared) {
    timeline.renderShared({ main: document.getElementById("main"), params: [decodePath(shared[1])], alive: () => true });
    return;
  }
  try {
    session.me = await api.get("/api/auth/me");
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return; // on its way to /login
    clear(document.getElementById("main")).append(
      h("div", { class: "container page" }, flashBox(`meertex could not start. ${err.message}`, "error")));
    return;
  }
  renderHeader();
  api.get("/api/version").then((v) => {
    document.getElementById("app-version").textContent = v?.version || "";
  }).catch(() => {});

  route(/^\/$/, home.render);
  route(/^\/folders\/(.+)$/, home.render);
  route(/^\/timeline$/, timeline.render);
  route(/^\/new$/, newPaper.render);
  route(/^\/settings$/, account.render);
  route(/^\/p\/([^/]+)(?:\/(.*))?$/, project.render);
  start();
}

boot();
