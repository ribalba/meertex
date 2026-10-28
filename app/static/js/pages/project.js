// /p/<name>[/<tab>...]: one paper. A header like a GitHub repository's, a row
// of tabs with counters, and the tab's content below. Each tab has its own
// URL, so Back and Forward move between them.

import { api, enc, projectUrl } from "../api.js";
import { h, clear } from "../dom.js";
import { icon } from "../icons.js";
import { render as rerender, decodePath } from "../router.js";
import { commandLine, counter, dropdown, flashBox, loading, personName, setCrumbs, setHeaderActions, setTitle } from "../ui.js";

import * as overview from "./overview.js";
import * as files from "./files.js";
import * as editor from "./editor.js";
import * as versions from "./versions.js";
import * as historyTab from "./history.js";
import * as settings from "./project-settings.js";
import * as reviews from "./reviews.js";
import * as references from "./references.js";
import { openThreadCounter } from "../threads.js";

const TABS = [
  { key: "overview", text: "Overview", icon: "book", path: "" },
  { key: "editor", text: "Editor", icon: "pencil", path: "/edit" },
  { key: "files", text: "Files", icon: "code", path: "/files" },
  { key: "versions", text: "Versions", icon: "tag", path: "/versions" },
  { key: "history", text: "History", icon: "history", path: "/history" },
  { key: "reviews", text: "Reviews", icon: "comment", path: "/reviews" },
  { key: "references", text: "References", icon: "quote", path: "/references" },
  { key: "settings", text: "Settings", icon: "gear", path: "/settings" },
];

function tabFor(sub) {
  const first = sub[0] || "";
  if (first === "files") return "files";
  if (first === "edit") return "editor";
  if (first === "versions" || first === "compare") return "versions";
  if (first === "history" || first === "commit") return "history";
  if (first === "settings") return "settings";
  if (first === "reviews") return "reviews";
  if (first === "references") return "references";
  return "overview";
}

export function canEdit(project) {
  return project.role === "owner" || project.role === "editor";
}

export async function render(ctx) {
  const ref = decodePath(ctx.params[0]);
  const sub = (ctx.params[1] || "").split("/").filter(Boolean);
  const tab = tabFor(sub);

  // Between tabs of the same paper the old page stays up while the new data
  // loads, instead of flashing a spinner.
  const sameProject = ctx.main.dataset.project === ref && ctx.main.querySelector(".repo-head");
  if (!sameProject) clear(ctx.main).append(loading());
  // The router empties the header on a new URL. The paper's name and Check
  // out go back at once, so they do not blink between its tabs.
  else if (shown) setHeader(shown);

  let project;
  try {
    project = await api.get(projectUrl(ref));
  } catch (err) {
    if (err.status === 409) {
      ambiguous(ctx, ref, err.detail);
      return;
    }
    throw err;
  }
  if (!ctx.alive()) return;

  const base = `/p/${enc(project.name)}`;
  // The URL may use the id or an old name. Show the current name.
  if (ref !== project.name && ref !== project.id) {
    history.replaceState(history.state, "", base + (ctx.params[1] ? `/${ctx.params[1]}` : "") + location.search);
  }

  const editable = canEdit(project);
  if (tab === "settings" && !editable) {
    const err = new Error("Only the owner and editors can change a paper's settings.");
    err.status = 404;
    throw err;
  }

  setHeader(project);
  const tabCtx = {
    ...ctx,
    project,
    ref: project.id,
    base,
    sub,
    canEdit: editable,
    isOwner: project.role === "owner",
    reload: () => rerender({ scroll: null }),
  };

  // The editor takes the full width below the header.
  const content = h("div", { class: tab === "editor" ? "editor-page" : "container page" });
  const page = h("div", {}, repoHead(project, tab, tabCtx), content);
  // Tabs render into `content`. Anything they show before their data is in
  // goes there too, so the header never blinks.
  tabCtx.main = content;

  const renderTab = {
    overview: overview.render,
    files: files.render,
    editor: editor.render,
    versions: versions.render,
    history: historyTab.render,
    settings: settings.render,
    reviews: reviews.render,
    references: references.render,
  }[tab];

  // Render the tab off-screen first, then swap in one go. A failing tab keeps
  // the header, so the other tabs stay one click away.
  try {
    await renderTab(tabCtx);
  } catch (err) {
    if (!ctx.alive() || err?.status === 401) return;
    console.error(err);
    content.replaceChildren(flashBox(err.message || String(err), "error"));
  }
  if (!ctx.alive()) return;
  ctx.main.dataset.project = ref;
  shown = project;
  clear(ctx.main).append(page);
}

// The paper on screen, for putting its header back between tabs.
let shown = null;

// The app header shows the paper's name after the logo and its Check out
// menu next to New paper.
function setHeader(p) {
  setCrumbs([{ text: p.name, href: `/p/${enc(p.name)}` }]);
  setHeaderActions([dropdown({
    summary: [icon("terminal"), h("span", { class: "hide-sm", text: "Check out" }), icon("triangle-down")],
    summaryClass: "btn btn-sm",
    label: "Check out this paper",
    align: "right",
    wide: true,
    cls: "checkout-menu",
    items: [
      { header: "Work on it locally" },
      { node: h("div", { class: "dropdown-body cmd-list" },
        commandLine(`meertex checkout ${p.name}`, "Copy the files to ~/papers and register the folder"),
        commandLine("meertex sync", "Keep every checkout in step with the server")) },
      "divider",
      { text: "Download ZIP", icon: "download", href: projectUrl(p.id, "/zip?ref=live"), download: `${p.name}.zip` },
    ],
  })]);
}

function repoHead(p, active, ctx) {
  const tabs = TABS.filter((t) => t.key !== "settings" || ctx.canEdit).map((t) => {
    let count = null;
    if (t.key === "files" && typeof p.file_count === "number") count = counter(p.file_count);
    if (t.key === "versions") count = counter((p.versions || []).length);
    if (t.key === "overview" && (p.questions || []).length) count = counter(p.questions.length, "counter-attention");
    if (t.key === "reviews") count = openThreadCounter(p.id);
    if (t.key === "references") count = references.problemsCounter(p.id);
    return h("a", {
      class: "underline-nav-item", href: ctx.base + t.path, "aria-current": t.key === active ? "page" : null,
    }, icon(t.icon), t.text, count);
  });

  // The name is in the app header already, so only the tabs show here.
  setTitle(p.name);
  return h("div", { class: "repo-head" },
    h("div", { class: "container" },
      h("nav", { class: "underline-nav", "aria-label": "Paper" }, tabs),
    ),
  );
}

function ambiguous(ctx, ref, detail) {
  const candidates = Array.isArray(detail?.candidates) ? detail.candidates : Array.isArray(detail) ? detail : [];
  setTitle(ref);
  clear(ctx.main).append(h("div", { class: "container container-sm page" },
    h("div", { class: "subhead" }, h("h2", { text: `More than one paper is called ${ref}` })),
    h("p", { class: "color-muted", text: "Papers shared with you can have the same name as one of yours. Pick the one you mean." }),
    candidates.length
      ? h("ul", { class: "box box-list" }, candidates.map((c) => h("li", { class: "box-row" },
        h("a", { href: `/p/${enc(c.id)}`, class: "text-bold", text: c.name || c.id }),
        c.owner ? h("span", { class: "color-muted", text: ` · ${personName(c.owner)}` }) : c.owner_email ? h("span", { class: "color-muted", text: ` · ${c.owner_email}` }) : null)))
      : h("p", { text: typeof detail === "string" ? detail : "Open it from your list of papers instead." }),
  ));
}
