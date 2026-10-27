// Home: your folder tree on the left, the project list on the right, and the
// questions that wait for an answer above it. /folders/<path> selects a folder.
// Filters live in the query string, so a filtered list can be bookmarked.

import { api, encPath, projectUrl, enc } from "../api.js";
import { h, clear, debounce, plural } from "../dom.js";
import { icon } from "../icons.js";
import { navigate, setQuery, decodePath, onLeave } from "../router.js";
import { questionControls } from "../questions.js";
import { countdown, formatDue, formatInMyZone, timeAgo } from "../time.js";
import {
  blankslate, busy, confirmDialog, dropdown, flash, flashError, loading, openDialog, outcomeLabel,
  setTitle, topicList,
} from "../ui.js";

const OUTCOME_FILTERS = [
  ["", "All"],
  ["none", "Under review"],
  ["accepted", "Accepted"],
  ["revision", "Revision"],
  ["rejected", "Rejected"],
  ["withdrawn", "Withdrawn"],
  ["not_submitted", "Not submitted"],
];

// Plans are venues without a paper yet (DESIGN §4, Planned papers).
const PLANNED_FILTERS = [
  ["", "All", "Papers and plans"],
  ["true", "Only plans", "Only plans"],
  ["false", "Only papers", "Only papers"],
];

const COLLAPSED_KEY = "meertex.folders.collapsed";
const DRAG_FOLDER = "application/x-meertex-folder";
const DRAG_PROJECT = "application/x-meertex-project";

function loadCollapsed() {
  try {
    return new Set(JSON.parse(localStorage.getItem(COLLAPSED_KEY) || "[]"));
  } catch {
    return new Set();
  }
}

function saveCollapsed(set) {
  try {
    localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...set]));
  } catch {
    // A private window may refuse storage. The tree then forgets, nothing else.
  }
}

export function folderHref(path) {
  return path ? `/folders/${encPath(path)}` : "/";
}

// The views of your papers, like a GitHub project's Table and Roadmap tabs.
export function viewTabs(active) {
  const tab = (key, href, iconName, text) => h("a", {
    class: "underline-nav-item", href, "aria-current": key === active ? "page" : null,
  }, icon(iconName), text);
  return h("div", { class: "view-tabs" },
    h("nav", { class: "underline-nav", "aria-label": "Views of your papers" },
      tab("list", "/", "book", "List"),
      tab("timeline", "/timeline", "calendar", "Timeline")));
}

// The badge of a planned project. Dashed, like its bars on the timeline.
export function plannedLabel() {
  return h("span", { class: "Label label-planned", title: "A venue without a paper yet", text: "Planned" });
}

export async function render(ctx) {
  const folderPath = ctx.params?.[0] ? decodePath(ctx.params[0]) : "";
  const filters = {
    q: ctx.query.get("q") || "",
    outcome: ctx.query.get("outcome") || "",
    label: ctx.query.get("label") || "",
    archived: ctx.query.get("archived") === "1",
    planned: ["true", "false"].includes(ctx.query.get("planned")) ? ctx.query.get("planned") : "",
  };
  const state = { folderPath, filters, folders: [], all: [], list: [], questions: [] };

  if (!ctx.main.querySelector(".home-page")) clear(ctx.main).append(loading());

  const [folders, all, questions, list] = await Promise.all([
    api.get("/api/folders"),
    api.get("/api/projects", { query: { archived: "all" } }),
    api.get("/api/questions"),
    fetchList(state),
  ]);
  if (!ctx.alive()) return;
  Object.assign(state, { folders, all, questions, list });

  const folder = folders.find((f) => f.path.toLowerCase() === folderPath.toLowerCase());
  if (folderPath && !folder) {
    const err = new Error(`There is no folder "${folderPath}".`);
    err.status = 404;
    throw err;
  }
  setTitle(folder ? folder.name : "Your papers");

  const sidebar = h("aside", { class: "home-sidebar", "aria-label": "Folders" });
  const waiting = h("div");
  const head = h("div");
  const listHost = h("div");
  const page = h("div", { class: "container page home-page" },
    viewTabs("list"),
    h("div", { class: "layout-sidebar" },
      sidebar,
      h("div", { class: "home-main" }, waiting, head, filterBar(state, () => reloadList()), listHost)));
  clear(ctx.main).append(page);

  async function reloadAll() {
    const [f, a, q, l] = await Promise.all([
      api.get("/api/folders"),
      api.get("/api/projects", { query: { archived: "all" } }),
      api.get("/api/questions"),
      fetchList(state),
    ]);
    if (!ctx.alive()) return;
    Object.assign(state, { folders: f, all: a, questions: q, list: l });
    paint();
  }

  let listToken = 0;
  async function reloadList() {
    const mine = ++listToken;
    try {
      const l = await fetchList(state);
      if (mine !== listToken || !ctx.alive()) return;
      state.list = l;
      paintList();
    } catch (err) {
      flashError(err);
    }
  }

  function paintList() {
    clear(listHost).append(projectList(state, { reloadAll }));
  }

  const wide = window.matchMedia("(min-width: 768px)");
  let foldersOpen = wide.matches;
  const onWide = () => { if (wide.matches) sidebar.querySelector("details")?.setAttribute("open", ""); };
  wide.addEventListener("change", onWide);
  onLeave(() => wide.removeEventListener("change", onWide));

  function paint() {
    const details = h("details", { class: "folders-details", open: foldersOpen || wide.matches },
      h("summary", {}, icon("chevron-down", { cls: "chev" }), icon("folder"), "Folders",
        h("span", { class: "current truncate", text: `· ${folder ? folder.path : "All papers"}` })),
      folderTree(state, { reloadAll }));
    details.addEventListener("toggle", () => { foldersOpen = details.open; });
    clear(sidebar).append(details);
    clear(waiting).append(waitingBox(state.questions, { reloadAll }) || "");
    clear(head).append(pageHead(state));
    paintList();
  }
  paint();
}

function fetchList(state) {
  const f = state.filters;
  return api.get("/api/projects", {
    query: {
      folder: state.folderPath || null,
      q: f.q || null,
      outcome: f.outcome || null,
      label: f.label || null,
      archived: f.archived ? "true" : null,
      planned: f.planned || null,
    },
  });
}

// --- Page head and filters --------------------------------------------------

function pageHead(state) {
  const crumbs = [];
  if (state.folderPath) {
    const parts = state.folderPath.split("/");
    crumbs.push(h("a", { href: "/", text: "Your papers" }));
    parts.forEach((p, i) => {
      crumbs.push(h("span", { class: "color-muted", text: " / " }));
      const path = parts.slice(0, i + 1).join("/");
      crumbs.push(i === parts.length - 1 ? h("strong", { text: p }) : h("a", { href: folderHref(path), text: p }));
    });
  }
  return h("div", { class: "flex flex-wrap mb-3" },
    h("h1", { class: "text-normal flex-1", style: { fontSize: "20px" } },
      state.folderPath ? crumbs : "Your papers"),
  );
}

function filterBar(state, reload) {
  const f = state.filters;
  const search = h("input", {
    type: "search", class: "form-control", placeholder: "Find a paper…", value: f.q,
    "aria-label": "Find a paper by name, title, conference or notes",
  });
  const run = debounce(() => {
    f.q = search.value.trim();
    setQuery({ q: f.q });
    reload();
  }, 250);
  search.addEventListener("input", run);

  const outcomeText = () => OUTCOME_FILTERS.find(([v]) => v === f.outcome)?.[1] || "All";
  const outcomeSummary = h("span", {}, "Outcome: ", h("strong", { text: outcomeText() }));
  const outcomeMenu = dropdown({
    summary: [outcomeSummary, icon("triangle-down")],
    label: "Filter by outcome",
    items: () => [
      { header: "Filter by outcome" },
      ...OUTCOME_FILTERS.map(([v, t]) => ({
        text: t, checked: f.outcome === v,
        onClick: () => {
          f.outcome = v;
          outcomeSummary.lastChild.textContent = outcomeText();
          setQuery({ outcome: v });
          reload();
        },
      })),
    ],
  });

  const labelSummary = h("span", {}, "Label: ", h("strong", { text: f.label || "All" }));
  const labelMenu = dropdown({
    summary: [labelSummary, icon("triangle-down")],
    label: "Filter by label",
    items: () => {
      const labels = [...new Set(state.all.flatMap((p) => p.labels || []))].sort((a, b) => a.localeCompare(b));
      const pick = (v) => () => {
        f.label = v;
        labelSummary.lastChild.textContent = v || "All";
        setQuery({ label: v });
        reload();
      };
      return [
        { header: "Filter by label" },
        { text: "All labels", checked: !f.label, onClick: pick("") },
        ...(labels.length ? labels.map((l) => ({ text: l, checked: f.label === l, onClick: pick(l) }))
          : [{ node: h("div", { class: "dropdown-text", text: "No labels yet. Add them in a paper's settings." }) }]),
      ];
    },
  });

  const plannedText = () => PLANNED_FILTERS.find(([v]) => v === f.planned)?.[1] || "All";
  const plannedSummary = h("span", {}, "Planned: ", h("strong", { text: plannedText() }));
  const plannedMenu = dropdown({
    summary: [plannedSummary, icon("triangle-down")],
    label: "Filter by planned or not",
    items: () => [
      { header: "Papers and plans" },
      ...PLANNED_FILTERS.map(([v, , t]) => ({
        text: t, checked: f.planned === v,
        onClick: () => {
          f.planned = v;
          plannedSummary.lastChild.textContent = plannedText();
          setQuery({ planned: v });
          reload();
        },
      })),
    ],
  });

  const archived = h("button", { type: "button", class: "btn", "aria-pressed": String(f.archived) },
    icon("archive"), "Archived");
  archived.addEventListener("click", () => {
    f.archived = !f.archived;
    archived.setAttribute("aria-pressed", String(f.archived));
    archived.classList.toggle("is-on", f.archived);
    setQuery({ archived: f.archived });
    reload();
  });
  archived.classList.toggle("is-on", f.archived);

  const newHref = state.folderPath ? `/new?folder=${enc(state.folderPath)}` : "/new";
  return h("div", { class: "filter-bar" },
    h("div", { class: "search" }, icon("search"), search),
    outcomeMenu,
    labelMenu,
    plannedMenu,
    archived,
    h("a", { class: "btn btn-primary", href: newHref }, icon("book"), "New"),
  );
}

function filtersActive(f) {
  return Boolean(f.q || f.outcome || f.label || f.archived || f.planned);
}

// --- The project list -------------------------------------------------------

function projectList(state, { reloadAll }) {
  const f = state.filters;
  const items = state.list;
  const out = h("div");
  if (filtersActive(f)) {
    const parts = [];
    if (f.archived) parts.push("archived");
    if (f.outcome) parts.push(OUTCOME_FILTERS.find(([v]) => v === f.outcome)?.[1].toLowerCase());
    if (f.label) parts.push(`labelled ${f.label}`);
    if (f.planned) parts.push(f.planned === "true" ? "planned" : "not planned");
    out.append(h("div", { class: "filter-summary" },
      h("span", { class: "flex-1" },
        h("strong", { text: plural(items.length, "paper") }),
        parts.length ? ` ${parts.join(", ")}` : "",
        f.q ? [" matching ", h("strong", { text: f.q })] : ""),
      h("a", { class: "btn-link", href: location.pathname }, icon("x"), " Clear filters")));
  }
  if (!items.length) {
    if (filtersActive(f)) {
      out.append(blankslate({ iconName: "search", title: "No papers match", text: "Try another search, or clear the filters." }));
    } else if (state.folderPath) {
      out.append(blankslate({
        iconName: "folder",
        title: "This folder is empty",
        text: "Drag papers onto it in the folder list, or use Move to folder… in a paper's menu.",
      }));
    } else {
      out.append(blankslate({
        iconName: "book",
        title: "Start your first paper",
        text: "A paper keeps its LaTeX files, its conference and deadlines, and the versions you handed in. A plan keeps a venue you want to hand something in to before the topic is clear.",
        action: h("div", { class: "flex flex-wrap", style: { justifyContent: "center" } },
          h("a", { class: "btn btn-primary", href: "/new" }, "New paper"),
          h("a", { class: "btn", href: "/new?plan=1" }, "Plan a venue")),
      }));
    }
    return out;
  }
  out.append(h("ul", { class: "project-list" }, items.map((p) => projectRow(p, state, { reloadAll }))));
  return out;
}

function nextDateBlock(nd) {
  if (!nd) return h("div", { class: "next-date-day", text: "No upcoming date" });
  return h("div", { class: "next-date", title: `In your time zone this is ${formatInMyZone(nd.due_at)}.` },
    h("div", { class: "next-date-what" }, icon("calendar"), h("span", { text: nd.label })),
    countdown(nd.due_at, { cls: "next-date-when" }),
    h("div", { class: "next-date-day", text: formatDue(nd) }),
  );
}

function projectRow(p, state, { reloadAll }) {
  const href = `/p/${enc(p.name)}`;
  const venue = [p.conference, p.year].filter(Boolean).join(" ");
  const meta = h("div", { class: "project-meta" });
  if (venue) meta.append(h("span", {}, icon("globe", { size: 14 }), venue, p.track ? ` · ${p.track}` : ""));
  if (p.folder_path && p.folder_path !== state.folderPath) {
    meta.append(h("a", { href: folderHref(p.folder_path) }, icon("folder", { size: 14 }), p.folder_path));
  }
  if (p.role && p.role !== "owner") meta.append(h("span", {}, icon("people", { size: 14 }), `Shared with you as ${p.role}`));
  if (p.updated_at) meta.append(h("span", {}, "Updated ", timeAgo(p.updated_at)));

  const menu = dropdown({
    summary: icon("kebab"),
    summaryClass: "btn-octicon",
    label: `Actions for ${p.name}`,
    align: "right",
    items: [
      { text: "Move to folder…", icon: "move-to", onClick: () => moveProjectDialog(p, state, reloadAll) },
      { text: "Files", icon: "code", href: `${href}/files` },
      p.role === "owner" || p.role === "editor" ? { text: "Settings", icon: "gear", href: `${href}/settings` } : null,
    ],
  });

  const side = h("div", { class: "project-side" },
    nextDateBlock(p.next_date),
    p.question_count > 0
      ? h("a", { class: "question-marker", href, title: "Open the paper to answer" },
        h("span", { class: "dot dot-attention" }), p.question_count === 1 ? "1 question" : `${p.question_count} questions`)
      : null,
  );

  const li = h("li", { class: "project-row", draggable: "true" },
    h("div", { class: "project-main" },
      h("div", { class: "project-name-line" },
        h("a", { class: "project-name", href, text: p.name }),
        outcomeLabel(p.outcome),
        p.planned ? plannedLabel() : null,
        p.archived ? h("span", { class: "Label label-attention", text: "Archived" }) : null),
      p.title ? h("p", { class: "project-title", text: p.title }) : null,
      topicList(p.labels, (l) => `/?label=${enc(l)}`),
      meta,
    ),
    side,
    h("div", { class: "project-menu" }, menu),
  );
  li.addEventListener("dragstart", (e) => {
    e.dataTransfer.setData(DRAG_PROJECT, p.id);
    e.dataTransfer.effectAllowed = "move";
    li.classList.add("dragging");
  });
  li.addEventListener("dragend", () => li.classList.remove("dragging"));
  return li;
}

// --- Waiting for you --------------------------------------------------------

function waitingBox(questions, { reloadAll }) {
  if (!questions?.length) return null;
  return h("section", { class: "box waiting", "aria-label": "Questions waiting for you" },
    h("div", { class: "box-header box-header-sm" },
      icon("inbox"),
      h("h2", { class: "box-title flex-1", text: "Waiting for you" }),
      h("span", { class: "counter counter-attention", text: String(questions.length) })),
    questions.map((q) => h("div", { class: "box-row" },
      h("div", { class: "question-row" },
        h("div", { class: "question-text" },
          h("a", { class: "question-project", href: `/p/${enc(q.project_name)}`, text: q.project_name }),
          h("span", { class: "color-muted", text: ` · ${q.label}` }),
          h("p", { class: "mt-0 mb-2", text: q.text }),
          questionControls(q, { onAnswered: () => reloadAll() }),
        )))),
  );
}

// --- Folders ----------------------------------------------------------------

function descendants(folders, id) {
  const out = new Set([id]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const f of folders) {
      if (f.parent_id && out.has(f.parent_id) && !out.has(f.id)) {
        out.add(f.id);
        grew = true;
      }
    }
  }
  return out;
}

function folderOptions(folders, exclude = new Set()) {
  return [["", "Top level (no folder)"], ...folders.filter((f) => !exclude.has(f.id)).map((f) => [f.id, f.path])];
}

function countIn(all, path) {
  return all.filter((p) => !p.archived && (p.folder_path === path || (p.folder_path || "").startsWith(`${path}/`))).length;
}

function folderTree(state, { reloadAll }) {
  const { folders, all } = state;
  const collapsed = loadCollapsed();
  const current = folders.find((f) => f.path.toLowerCase() === state.folderPath.toLowerCase());
  // The selected folder's ancestors stay open, so the selection is visible.
  if (current) {
    let p = current.parent_id;
    while (p) {
      collapsed.delete(p);
      p = folders.find((f) => f.id === p)?.parent_id || null;
    }
  }
  const children = new Map();
  for (const f of folders) {
    const key = f.parent_id || "";
    if (!children.has(key)) children.set(key, []);
    children.get(key).push(f);
  }

  const list = h("ul", { class: "tree" });
  const wrap = h("div", {});

  const newFolderBtn = h("button", { type: "button", class: "btn btn-sm", "aria-label": "New folder", title: "New folder" }, icon("plus"), "New");
  newFolderBtn.addEventListener("click", () => startCreate(null));

  const header = h("div", { class: "folders-head" }, h("h2", { text: "Folders" }), newFolderBtn);

  const rootRow = h("div", { class: "tree-row", "aria-current": state.folderPath ? null : "page" },
    h("span", { class: "tree-toggle spacer" }),
    h("a", { class: "tree-link", href: "/" }, icon("book"), h("span", { class: "flex-1 truncate", text: "All papers" }),
      h("span", { class: "tree-count", text: String(all.filter((p) => !p.archived).length) })));
  makeDropTarget(rootRow, null, state, reloadAll);
  list.append(h("li", {}, rootRow));

  let createHost = null;

  function addLevel(parentId, depth) {
    for (const f of children.get(parentId || "") || []) {
      const kids = children.get(f.id) || [];
      const open = !collapsed.has(f.id);
      const li = h("li");
      const row = folderRow(f, depth, kids.length > 0, open);
      li.append(row);
      list.append(li);
      if (open) addLevel(f.id, depth + 1);
    }
  }

  function folderRow(f, depth, hasKids, open) {
    const isCurrent = current?.id === f.id;
    const toggle = h("button", {
      type: "button", class: `tree-toggle${hasKids ? "" : " spacer"}`,
      "aria-label": open ? `Collapse ${f.name}` : `Expand ${f.name}`, "aria-expanded": hasKids ? String(open) : null,
      tabindex: hasKids ? null : "-1",
    }, icon(open ? "chevron-down" : "chevron-right", { size: 12 }));
    if (hasKids) {
      toggle.addEventListener("click", () => {
        if (open) collapsed.add(f.id);
        else collapsed.delete(f.id);
        saveCollapsed(collapsed);
        rerender();
      });
    }
    const link = h("a", { class: "tree-link", href: folderHref(f.path) },
      icon(isCurrent ? "folder-fill" : "folder"),
      h("span", { class: "flex-1 truncate", text: f.name }),
      h("span", { class: "tree-count", text: String(countIn(all, f.path) || "") }));
    const actions = dropdown({
      summary: icon("kebab"),
      summaryClass: "btn-octicon",
      label: `Actions for folder ${f.name}`,
      align: "right",
      cls: "tree-actions",
      items: [
        { text: "New subfolder", icon: "plus", onClick: () => startCreate(f) },
        { text: "Rename", icon: "pencil", onClick: () => startRename(f, row) },
        { text: "Move to…", icon: "move-to", onClick: () => moveFolderDialog(f, state, reloadAll) },
        "divider",
        { text: "Delete", icon: "trash", danger: true, onClick: () => deleteFolder(f, state, reloadAll) },
      ],
    });
    const row = h("div", {
      class: "tree-row", draggable: "true", "aria-current": isCurrent ? "page" : null,
      style: { paddingLeft: `${depth * 16}px` }, dataset: { folderId: f.id },
    }, toggle, link, actions);
    row.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData(DRAG_FOLDER, f.id);
      e.dataTransfer.effectAllowed = "move";
      row.classList.add("dragging");
    });
    row.addEventListener("dragend", () => row.classList.remove("dragging"));
    makeDropTarget(row, f, state, reloadAll);
    return row;
  }

  function inlineEdit({ value = "", placeholder, onSave, onCancel }) {
    const input = h("input", { type: "text", class: "form-control form-control-sm", value, placeholder, "aria-label": placeholder, maxlength: "200" });
    const form = h("form", { class: "tree-edit" }, input,
      h("button", { type: "submit", class: "btn-octicon", "aria-label": "Save" }, icon("check")),
      h("button", { type: "button", class: "btn-octicon", "aria-label": "Cancel", onclick: () => onCancel() }, icon("x")));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const name = input.value.trim();
      if (!name) { onCancel(); return; }
      try {
        await busy(form.querySelector("button[type=submit]"), () => onSave(name));
      } catch (err) {
        flashError(err);
        input.focus();
      }
    });
    input.addEventListener("keydown", (e) => { if (e.key === "Escape") onCancel(); });
    setTimeout(() => { input.focus(); input.select(); }, 0);
    return form;
  }

  function startCreate(parent) {
    createHost?.remove();
    const depth = parent ? parent.path.split("/").length : 0;
    const row = h("li", {}, h("div", { class: "tree-row", style: { paddingLeft: `${depth * 16}px` } },
      h("span", { class: "tree-toggle spacer" }), icon("folder", { cls: "color-muted" }),
      inlineEdit({
        placeholder: parent ? `New folder in ${parent.name}` : "New folder name",
        onCancel: () => row.remove(),
        onSave: async (name) => {
          await api.post("/api/folders", { name, parent_id: parent ? parent.id : null });
          if (parent) {
            collapsed.delete(parent.id);
            saveCollapsed(collapsed);
          }
          await reloadAll();
        },
      })));
    createHost = row;
    if (parent) {
      const parentLi = [...list.children].find((li) => li.firstChild?.dataset?.folderId === parent.id);
      parentLi ? parentLi.after(row) : list.append(row);
    } else {
      list.append(row);
    }
  }

  function startRename(f, row) {
    const original = [...row.childNodes];
    const restore = () => row.replaceChildren(...original);
    row.draggable = false;
    row.replaceChildren(h("span", { class: "tree-toggle spacer" }), inlineEdit({
      value: f.name,
      placeholder: "Folder name",
      onCancel: () => { row.draggable = true; restore(); },
      onSave: async (name) => {
        const updated = await api.patch(`/api/folders/${enc(f.id)}`, { name });
        // The open folder, or one above it, has a new path now.
        const here = state.folderPath.toLowerCase();
        const old = f.path.toLowerCase();
        if (here === old || here.startsWith(`${old}/`)) {
          const newPath = (updated?.path || f.path.replace(/[^/]+$/, name)) + state.folderPath.slice(f.path.length);
          navigate(folderHref(newPath), { replace: true, keepScroll: true });
          return;
        }
        await reloadAll();
      },
    }));
  }

  function rerender() {
    wrap.replaceWith(folderTree(state, { reloadAll }));
  }

  addLevel(null, 0);
  wrap.append(header, list);
  if (!folders.length) {
    wrap.append(h("p", { class: "text-small color-muted mt-2", text: "Folders are yours alone. Sort shared papers however you like, your coauthors keep their own order." }));
  }
  return wrap;
}

function makeDropTarget(row, folder, state, reloadAll) {
  const accepts = (e) => e.dataTransfer.types.includes(DRAG_FOLDER) || e.dataTransfer.types.includes(DRAG_PROJECT);
  row.addEventListener("dragover", (e) => {
    if (!accepts(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    row.classList.add("drop-target");
  });
  row.addEventListener("dragleave", (e) => {
    if (!row.contains(e.relatedTarget)) row.classList.remove("drop-target");
  });
  row.addEventListener("drop", async (e) => {
    row.classList.remove("drop-target");
    const folderId = e.dataTransfer.getData(DRAG_FOLDER);
    const projectId = e.dataTransfer.getData(DRAG_PROJECT);
    if (!folderId && !projectId) return;
    e.preventDefault();
    try {
      if (folderId) {
        if (folder && descendants(state.folders, folderId).has(folder.id)) {
          flash("A folder cannot move into itself or one of its subfolders.", "warn");
          return;
        }
        await api.patch(`/api/folders/${enc(folderId)}`, { parent_id: folder ? folder.id : null });
      } else {
        await api.put(projectUrl(projectId, "/placement"), { folder_id: folder ? folder.id : null });
        const p = state.all.find((x) => x.id === projectId);
        flash(`Moved ${p ? p.name : "the paper"} to ${folder ? folder.path : "the top level"}.`, "success");
      }
      await reloadAll();
    } catch (err) {
      flashError(err);
    }
  });
}

function folderPicker(state, current, exclude = new Set()) {
  const opts = folderOptions(state.folders, exclude);
  return h("select", { class: "form-select input-block", value: current || "" },
    opts.map(([v, t]) => h("option", { value: v }, t)));
}

function moveProjectDialog(p, state, reloadAll) {
  const sel = folderPicker(state, p.folder_id);
  const save = h("button", { type: "button", class: "btn btn-primary", text: "Move" });
  const d = openDialog({
    title: `Move ${p.name}`,
    body: h("div", {},
      h("p", { class: "color-muted", text: "Folders are yours alone. Your coauthors keep the paper wherever they put it." }),
      h("label", { class: "form-label", for: "move-target", text: "Folder" }),
      Object.assign(sel, { id: "move-target" })),
    footer: [h("button", { type: "button", class: "btn", text: "Cancel", onclick: () => d.close() }), save],
  });
  save.addEventListener("click", async () => {
    try {
      await busy(save, () => api.put(projectUrl(p.id, "/placement"), { folder_id: sel.value || null }));
      d.close();
      const target = state.folders.find((f) => f.id === sel.value);
      flash(`Moved ${p.name} to ${target ? target.path : "the top level"}.`, "success");
      await reloadAll();
    } catch (err) {
      flashError(err);
    }
  });
}

function moveFolderDialog(f, state, reloadAll) {
  const sel = folderPicker(state, f.parent_id, descendants(state.folders, f.id));
  sel.id = "move-folder-target";
  const save = h("button", { type: "button", class: "btn btn-primary", text: "Move" });
  const d = openDialog({
    title: `Move folder ${f.name}`,
    body: h("div", {}, h("label", { class: "form-label", for: sel.id, text: "Move into" }), sel),
    footer: [h("button", { type: "button", class: "btn", text: "Cancel", onclick: () => d.close() }), save],
  });
  save.addEventListener("click", async () => {
    try {
      const updated = await busy(save, () => api.patch(`/api/folders/${enc(f.id)}`, { parent_id: sel.value || null }));
      d.close();
      const here = state.folderPath.toLowerCase();
      if (updated?.path && (here === f.path.toLowerCase() || here.startsWith(`${f.path.toLowerCase()}/`))) {
        navigate(folderHref(updated.path + state.folderPath.slice(f.path.length)), { replace: true, keepScroll: true });
        return;
      }
      await reloadAll();
    } catch (err) {
      flashError(err);
    }
  });
}

async function deleteFolder(f, state, reloadAll) {
  const ok = await confirmDialog({
    title: `Delete folder ${f.name}?`,
    message: "Only the folder goes. A folder that still holds folders or papers cannot be deleted.",
    confirmText: "Delete folder",
    danger: true,
  });
  if (!ok) return;
  try {
    await api.del(`/api/folders/${enc(f.id)}`);
    flash(`Deleted the folder ${f.path}.`, "success");
    const here = state.folderPath.toLowerCase();
    if (here === f.path.toLowerCase() || here.startsWith(`${f.path.toLowerCase()}/`)) {
      navigate("/", { replace: true });
      return;
    }
    await reloadAll();
  } catch (err) {
    if (err.status === 409) {
      flash(`The folder ${f.path} still holds folders or papers. Move them out first, then delete it.`, "error");
    } else {
      flashError(err);
    }
  }
}
