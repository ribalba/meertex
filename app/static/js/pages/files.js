// The Files tab, like GitHub's Code tab: a search over all files, a ref
// selector (live, HEAD and every version), a directory listing built from the
// flat file list, a file view with line numbers, and how to check the paper
// out locally.

import { api, enc, encPath, projectUrl, request } from "../api.js";
import { h, clear, formatBytes, plural } from "../dom.js";
import { icon } from "../icons.js";
import { decodePath, navigate } from "../router.js";
import { timeAgo } from "../time.js";
import { blankslate, commandLine, copyButton, dropdown, flashBox, setTitle } from "../ui.js";
import { editorHref } from "./editor.js";

// The types the server sends inline. SVG comes as a download, never rendered.
const IMAGE = /\.(png|jpe?g|gif|webp)$/i;

export function refName(ref) {
  if (ref === "live" || ref === "HEAD") return ref;
  return /^[0-9a-f]{40}$/i.test(ref) ? ref.slice(0, 7) : ref;
}

export function refOptions(project) {
  const versions = [...(project.versions || [])].sort((a, b) => (b.number || 0) - (a.number || 0));
  return [
    { ref: "live", text: "live", hint: "The live document, what everyone sees right now" },
    { ref: "HEAD", text: "HEAD", hint: "The last snapshot in the history" },
    ...versions.map((v) => ({ ref: v.tag, text: v.tag, hint: v.label, version: v })),
  ];
}

// A ref switcher like GitHub's branch button. `hrefFor(ref)` builds each link.
export function refSelector(project, current, hrefFor, { label = "Switch state" } = {}) {
  const opts = refOptions(project);
  return dropdown({
    summary: [icon(current === "live" ? "dot" : current === "HEAD" ? "history" : "tag"),
      h("span", { class: "ref-name", text: refName(current) }), icon("triangle-down")],
    summaryClass: "btn ref-button",
    label,
    wide: true,
    items: [
      { header: label },
      ...opts.slice(0, 2).map((o) => ({ text: `${o.text} · ${o.hint}`, checked: current === o.ref, href: hrefFor(o.ref) })),
      opts.length > 2 ? "divider" : null,
      opts.length > 2 ? { header: "Versions" } : null,
      ...opts.slice(2).map((o) => ({ text: `${o.text} · ${o.hint}`, checked: current === o.ref, href: hrefFor(o.ref) })),
    ],
  });
}

function filesHref(base, path, ref) {
  return `${base}/files${path ? `/${encPath(path)}` : ""}${ref && ref !== "live" ? `?ref=${enc(ref)}` : ""}`;
}

export async function render(ctx) {
  const p = ctx.project;
  const s = searchState(ctx.query);
  // With "all versions" the file types come from the live document.
  const ref = s.versions ? "live" : ctx.query.get("ref") || "live";
  const path = decodePath(ctx.sub.slice(1).join("/"));
  setTitle(s.q ? `Search ${s.q}` : path || "Files", p.name);

  const list = await api.get(projectUrl(ctx.ref, "/files"), { query: { ref } });
  if (!ctx.alive()) return;
  list.sort((a, b) => a.path.localeCompare(b.path));
  const bar = searchBar(ctx, list, s);

  if (s.q) {
    const results = await searchResults(ctx, s);
    if (!ctx.alive()) return;
    clear(ctx.main).append(bar, results);
    return;
  }

  const file = path ? list.find((f) => f.path === path) : null;
  const isDir = !path || list.some((f) => f.path.startsWith(`${path}/`));

  const toolbar = h("div", { class: "files-toolbar" },
    refSelector(p, ref, (r) => filesHref(ctx.base, path, r)),
    path ? null : h("a", { class: "btn-link text-small color-muted nowrap", href: `${ctx.base}/versions` },
      icon("tag", { size: 14 }), ` ${plural((p.versions || []).length, "version")}`),
    breadcrumb(p, ctx.base, path, ref),
    h("span", { class: "flex-1" }),
    h("a", { class: "btn", href: `${projectUrl(ctx.ref, "/zip")}?ref=${enc(ref)}`, download: `${p.name}-${refName(ref)}.zip` },
      icon("download"), "Download ZIP"),
  );

  let body;
  if (file) body = await fileView(ctx, file, ref);
  else if (isDir) body = h("div", {}, dirView(ctx, list, path, ref), path ? null : checkoutBox(p));
  else {
    body = h("div", { class: "box" }, blankslate({
      iconName: "file",
      title: "No such file here",
      text: `${path} does not exist in ${refName(ref)}.`,
      action: h("a", { class: "btn", href: filesHref(ctx.base, "", ref), text: "Go to the top" }),
    }));
  }
  if (!ctx.alive()) return;
  clear(ctx.main).append(bar, toolbar, body);
}

// --- Search -----------------------------------------------------------------
//
// A regular expression over every text file, at one state (live, HEAD, a
// version) or at all versions at once. Its state lives in the URL, so Back
// returns to the last search and a search can be linked.

function searchState(query) {
  return {
    q: query.get("q") || "",
    ref: query.get("ref") || "live",
    versions: query.get("versions") === "1",
    ext: (query.get("ext") || "").split(",").map((e) => e.trim().toLowerCase()).filter(Boolean),
    case: query.get("case") === "1",
    regex: query.get("regex") !== "0",
  };
}

function searchHref(base, s) {
  const qs = new URLSearchParams({ q: s.q });
  if (s.versions) qs.set("versions", "1");
  else if (s.ref !== "live") qs.set("ref", s.ref);
  if (s.ext.length) qs.set("ext", s.ext.join(","));
  if (s.case) qs.set("case", "1");
  if (!s.regex) qs.set("regex", "0");
  return `${base}/files?${qs}`;
}

function extensionOf(path) {
  const name = path.split("/").pop();
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

function scopeText(s) {
  return s.versions ? "All versions" : refName(s.ref);
}

function searchBar(ctx, list, initial) {
  const p = ctx.project;
  const s = { ...initial, ext: [...initial.ext] };
  const input = h("input", {
    type: "search", class: "form-control files-search-input", value: s.q, placeholder: "Search all files with a regular expression",
    "aria-label": "Search the files", autocomplete: "off", spellcheck: "false",
  });
  // A change of the scope, the file types or a toggle searches again right
  // away when there is a search on the page.
  const submit = () => {
    if (!input.value) {
      if (initial.q) navigate(filesHref(ctx.base, "", s.versions ? "live" : s.ref));
      else input.focus();
      return;
    }
    navigate(searchHref(ctx.base, { ...s, q: input.value }));
  };
  const again = () => { if (initial.q) submit(); };
  const toggle = (key, text, title) => {
    const b = h("button", {
      type: "button", class: "btn-octicon search-toggle", title, "aria-label": title, "aria-pressed": String(s[key]), text,
      onclick: () => { s[key] = !s[key]; b.setAttribute("aria-pressed", String(s[key])); again(); },
    });
    return b;
  };

  return h("form", { class: "files-search", role: "search", onsubmit: (e) => { e.preventDefault(); submit(); } },
    h("div", { class: "files-search-field" },
      icon("search", { cls: "files-search-icon" }),
      input,
      toggle("case", "Aa", "Match case"),
      toggle("regex", ".*", "Regular expression")),
    scopeMenu(p, s, again),
    typeMenu(list, s, again),
    h("button", { type: "submit", class: "btn", text: "Search" }),
    initial.q ? h("a", { class: "btn btn-invisible", href: filesHref(ctx.base, "", initial.versions ? "live" : initial.ref) }, icon("x"), "Clear") : null,
  );
}

function scopeMenu(p, s, onChange) {
  const name = h("span", { class: "ref-name", text: scopeText(s) });
  const versions = refOptions(p).slice(2);
  const pick = (ref, all) => {
    s.ref = ref;
    s.versions = all;
    name.textContent = scopeText(s);
    onChange();
  };
  const known = ["live", "HEAD", ...versions.map((o) => o.ref)];
  return dropdown({
    summary: [icon("history"), name, icon("triangle-down")],
    summaryClass: "btn ref-button",
    label: "Where to search",
    wide: true,
    items: () => [
      { header: "Search in" },
      { text: "live · the current document", checked: !s.versions && s.ref === "live", onClick: () => pick("live", false) },
      { text: "HEAD · the last snapshot", checked: !s.versions && s.ref === "HEAD", onClick: () => pick("HEAD", false) },
      known.includes(s.ref) ? null : { text: `${refName(s.ref)} · this commit`, checked: !s.versions, onClick: () => pick(s.ref, false) },
      {
        text: versions.length ? `All versions · ${plural(versions.length, "version")}` : "All versions · none yet",
        checked: s.versions, disabled: !versions.length, onClick: () => pick("live", true),
      },
      versions.length ? "divider" : null,
      versions.length ? { header: "One version" } : null,
      ...versions.map((o) => ({ text: `${o.text} · ${o.hint}`, checked: !s.versions && s.ref === o.ref, onClick: () => pick(o.ref, false) })),
    ],
  });
}

function typeMenu(list, s, onChange) {
  const counts = new Map();
  for (const f of list) {
    if (f.kind !== "text") continue;
    const ext = extensionOf(f.path);
    if (ext) counts.set(ext, (counts.get(ext) || 0) + 1);
  }
  for (const ext of s.ext) if (!counts.has(ext)) counts.set(ext, 0);
  const exts = [...counts.keys()].sort((a, b) => (counts.get(b) - counts.get(a)) || a.localeCompare(b));
  const name = h("span", {});
  const label = () => { name.textContent = s.ext.length ? s.ext.map((e) => `.${e}`).join(", ") : "All file types"; };
  label();
  let before = s.ext.join(",");
  const menu = dropdown({
    summary: [icon("file"), name, icon("triangle-down")],
    summaryClass: "btn files-type-button",
    label: "File types to search",
    items: () => [
      { header: "File types" },
      exts.length ? null : { text: "No text files here", disabled: true },
      ...exts.map((ext) => {
        const box = h("input", {
          type: "checkbox", checked: s.ext.includes(ext),
          onchange: () => {
            s.ext = box.checked ? [...s.ext, ext] : s.ext.filter((e) => e !== ext);
            label();
          },
        });
        return {
          node: h("label", { class: "dropdown-check" }, box,
            h("span", { class: "flex-1 text-mono", text: `.${ext}` }),
            h("span", { class: "color-muted text-small", text: counts.get(ext) ? plural(counts.get(ext), "file") : "" })),
        };
      }),
      s.ext.length ? "divider" : null,
      s.ext.length ? { text: "All file types", onClick: () => { s.ext = []; label(); } } : null,
    ],
  });
  // The ticks take effect when the menu closes, so ticking three types runs one search.
  menu.addEventListener("toggle", () => {
    if (menu.open) { before = s.ext.join(","); return; }
    if (s.ext.join(",") !== before) onChange();
  });
  return menu;
}

async function searchResults(ctx, s) {
  let res;
  try {
    res = await api.get(projectUrl(ctx.ref, "/search"), {
      query: {
        q: s.q, ref: s.versions ? null : s.ref, versions: s.versions || null,
        ext: s.ext.join(","), case: s.case || null, regex: s.regex ? null : false,
      },
    });
  } catch (err) {
    if (err?.status !== 422) throw err;
    return flashBox(err.message, "error");
  }
  const where = s.versions ? `all ${plural(res.refs.length, "version")}` : refName(s.ref);
  const out = h("div", { class: "search-results" });
  if (s.versions && !res.refs.length) {
    out.append(h("div", { class: "box" }, blankslate({
      iconName: "tag",
      title: "No versions yet",
      text: "A version is a fixed state of the files, such as the submission. Once there is one, its files can be searched here.",
      action: h("a", { class: "btn", href: `${ctx.base}/versions`, text: "Go to the versions" }),
    })));
    return out;
  }
  const shown = res.files.reduce((n, f) => n + f.lines.length, 0);
  out.append(h("div", { class: "search-summary" },
    h("strong", { text: plural(res.lines, "matching line") }),
    ` in ${plural(res.files.length, "file")} · ${where}`,
    s.ext.length ? ` · ${s.ext.map((e) => `.${e}`).join(", ")}` : ""));
  if (res.timed_out) {
    out.append(flashBox("The search stopped after 5 seconds, so some files were not searched. A simpler pattern is faster.", "warn"));
  }
  if (res.truncated && shown < res.lines) {
    out.append(flashBox(`Showing the first ${shown} matching lines. Narrow the search to see the rest.`));
  }
  if (!res.files.length) {
    out.append(h("div", { class: "box" }, blankslate({
      iconName: "search",
      title: "No matches",
      text: `Nothing in ${where} matches ${s.q}.`,
    })));
    return out;
  }
  const labels = new Map((ctx.project.versions || []).map((v) => [v.tag, v.label]));
  const chips = s.versions || s.ref !== "live";
  for (const f of res.files) out.append(searchFile(ctx, f, labels, chips));
  return out;
}

function searchFile(ctx, f, labels, chips) {
  const first = f.refs[0];
  let prev = null;
  const rows = f.lines.map((l) => {
    const gap = prev !== null && l.line !== prev + 1;
    prev = l.line;
    return h("tr", { class: gap ? "is-gap" : null },
      h("td", { class: "ln" }, h("a", { href: `${filesHref(ctx.base, f.path, first)}#L${l.line}`, text: String(l.line) })),
      h("td", { class: "lc" }, highlighted(l)));
  });
  const more = f.count - f.lines.length;
  return h("div", { class: "box mb-3 search-file" },
    h("div", { class: "box-header box-header-sm search-file-head" },
      icon("file-text", { size: 14 }),
      h("a", { class: "search-file-path", href: filesHref(ctx.base, f.path, first), text: f.path }),
      chips ? f.refs.map((r) => h("a", { class: "Label", href: filesHref(ctx.base, f.path, r), title: labels.get(r) || r, text: refName(r) })) : null,
      h("span", { class: "flex-1" }),
      h("span", { class: "color-muted text-small nowrap", text: plural(f.count, "line") })),
    rows.length ? h("div", { class: "code-scroll" }, h("table", { class: "code-table search-lines" }, h("tbody", {}, rows))) : null,
    more > 0 ? h("div", { class: "search-more", text: `${plural(more, "more matching line")} not shown` }) : null,
  );
}

// The line with its matches marked. The ranges are UTF-16 offsets, which is
// what JavaScript strings slice by.
function highlighted(l) {
  const parts = [];
  if (l.cut_before) parts.push(h("span", { class: "color-muted", text: "… " }));
  let at = 0;
  for (const [a, b] of l.ranges) {
    if (a < at) continue;
    parts.push(l.text.slice(at, a), h("mark", { class: "search-hit", text: l.text.slice(a, b) }));
    at = b;
  }
  parts.push(l.text.slice(at));
  if (l.cut_after) parts.push(h("span", { class: "color-muted", text: " …" }));
  return parts;
}

function breadcrumb(p, base, path, ref) {
  if (!path) return null;
  const parts = path.split("/");
  const crumbs = [h("a", { href: filesHref(base, "", ref), text: p.name })];
  parts.forEach((part, i) => {
    crumbs.push(h("span", { class: "sep", text: "/" }));
    const sub = parts.slice(0, i + 1).join("/");
    crumbs.push(i === parts.length - 1 ? h("span", { class: "current", text: part }) : h("a", { href: filesHref(base, sub, ref), text: part }));
  });
  return h("nav", { class: "breadcrumb", "aria-label": "File path" }, crumbs);
}

function refHeader(ctx, ref, count) {
  const v = (ctx.project.versions || []).find((x) => x.tag === ref);
  let what;
  if (ref === "live") what = [h("strong", { text: "Live document" }), h("span", { class: "color-muted", text: " · what everyone sees right now" })];
  else if (ref === "HEAD") what = [h("strong", { text: "Last snapshot" }), h("span", { class: "color-muted", text: " · the newest commit in the history" })];
  else if (v) {
    what = [h("strong", { text: v.label }),
      h("span", { class: "color-muted" }, " · ",
        h("a", { class: "sha", href: `${ctx.base}/commit/${enc(v.commit)}`, text: (v.commit || "").slice(0, 7) }), " · ", timeAgo(v.created_at))];
  } else what = [h("strong", { text: "Commit " }), h("a", { class: "sha", href: `${ctx.base}/commit/${enc(ref)}`, text: refName(ref) })];
  return h("div", { class: "box-header box-header-sm" },
    h("div", { class: "flex-1 truncate" }, what),
    h("span", { class: "color-muted text-small nowrap" }, icon("file", { size: 14 }), " ", plural(count, "file")));
}

function dirView(ctx, list, dir, ref) {
  const prefix = dir ? `${dir}/` : "";
  const dirs = new Map();
  const files = [];
  for (const f of list) {
    if (!f.path.startsWith(prefix)) continue;
    const rest = f.path.slice(prefix.length);
    const slash = rest.indexOf("/");
    if (slash >= 0) {
      const name = rest.slice(0, slash);
      dirs.set(name, (dirs.get(name) || 0) + 1);
    } else {
      files.push({ ...f, name: rest });
    }
  }
  const rows = [];
  if (dir) {
    const up = dir.includes("/") ? dir.slice(0, dir.lastIndexOf("/")) : "";
    rows.push(h("tr", {},
      h("td", { class: "icon-cell" }, icon("folder", { cls: "dir" })),
      h("td", { class: "name" }, h("a", { href: filesHref(ctx.base, up, ref), "aria-label": "Parent folder", text: ".." })),
      h("td", { class: "size" })));
  }
  [...dirs.keys()].sort((a, b) => a.localeCompare(b)).forEach((name) => {
    rows.push(h("tr", {},
      h("td", { class: "icon-cell" }, icon("folder-fill", { cls: "dir" })),
      h("td", { class: "name" }, h("a", { href: filesHref(ctx.base, prefix + name, ref), text: name })),
      h("td", { class: "size", text: plural(dirs.get(name), "file") })));
  });
  files.sort((a, b) => a.name.localeCompare(b.name)).forEach((f) => {
    rows.push(h("tr", {},
      h("td", { class: "icon-cell" }, icon(f.kind === "blob" ? "file-binary" : "file-text")),
      h("td", { class: "name" }, h("a", { href: filesHref(ctx.base, f.path, ref), text: f.name })),
      h("td", { class: "size", text: formatBytes(f.size) })));
  });

  const box = h("div", { class: "box mb-4" }, refHeader(ctx, ref, list.length));
  if (!list.length) {
    box.append(blankslate({
      iconName: "file",
      title: ref === "live" ? "No files yet" : "No files in this state",
      text: ref === "live" ? "Write in the browser, or check the paper out and add your LaTeX files. They show up here within a second." : "This snapshot holds no files.",
      action: ref === "live" ? h("a", { class: "btn", href: editorHref(ctx.base) }, icon("pencil"), "Open the editor") : null,
    }));
  } else {
    box.append(h("table", { class: "file-table" }, h("tbody", {}, rows)));
  }
  return box;
}

async function fileView(ctx, file, ref) {
  const url = `${projectUrl(ctx.ref, `/files/${encPath(file.path)}`)}?ref=${enc(ref)}`;
  const name = file.path.split("/").pop();
  const header = h("div", { class: "box-header" },
    h("div", { class: "flex-1 text-small color-muted" }),
    ref === "live" && file.kind === "text"
      ? h("a", { class: "btn btn-sm", href: editorHref(ctx.base, file.path) }, icon("pencil"), "Open in editor") : null,
    h("a", { class: "btn btn-sm", href: url, target: "_blank", rel: "noopener" }, "Raw"),
    h("a", { class: "btn-octicon", href: url, download: name, "aria-label": "Download", title: "Download" }, icon("download")),
  );
  const box = h("div", { class: "box code-box" }, header);
  const info = header.firstChild;

  if (file.kind === "blob") {
    info.textContent = `${formatBytes(file.size)} · binary file`;
    if (IMAGE.test(file.path)) {
      box.append(h("div", { class: "image-preview" }, h("img", { src: url, alt: file.path })));
    } else {
      box.append(blankslate({
        iconName: /\.pdf$/i.test(file.path) ? "pdf" : "file-binary",
        title: "Binary file",
        text: `${name} is ${formatBytes(file.size)} and cannot be shown here.`,
        action: h("a", { class: "btn", href: url, download: name }, icon("download"), "Download"),
      }));
    }
    return box;
  }

  const res = await request("GET", projectUrl(ctx.ref, `/files/${encPath(file.path)}`), { query: { ref }, raw: true });
  const text = await res.text();
  const lines = text.split("\n");
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  const loc = lines.filter((l) => l.trim()).length;
  info.textContent = `${plural(lines.length, "line")} (${loc} loc) · ${formatBytes(file.size ?? new Blob([text]).size)}`;
  header.insertBefore(copyButton(text, { label: "Copy file contents" }), header.lastChild);

  // :target does not follow pushState, so a line a search result links to is marked here.
  const target = Number(/^#L(\d+)$/.exec(location.hash)?.[1] || 0);
  const rows = lines.map((l, i) => h("tr", { id: `L${i + 1}`, class: i + 1 === target ? "is-target" : null },
    h("td", { class: "ln", dataset: { ln: i + 1 } }),
    h("td", { class: "lc", text: l })));
  box.append(h("div", { class: "code-scroll" }, h("table", { class: "code-table" }, h("tbody", {}, rows))));
  return box;
}

export function checkoutBox(p) {
  return h("section", { class: "box readme-box", "aria-labelledby": "checkout-title" },
    h("div", { class: "box-header" }, h("h2", { id: "checkout-title" }, icon("terminal"), "Work on this paper locally")),
    h("div", { class: "box-body" },
      h("p", { text: "meertex keeps a folder on your computer in step with this paper. Edit in VS Code or any editor, and your coauthors see the changes within a second." }),
      h("div", { class: "cmd-list" },
        commandLine(`meertex checkout ${p.name}`, `Copy the paper to ~/papers/${p.name}`),
        commandLine("meertex sync", "Keep every checkout in sync while you write"),
        commandLine("meertex service install", "Or run the sync in the background, started with your session")),
      h("p", { class: "text-small color-muted mt-3 mb-0" },
        "No meertex command yet? ", h("a", { href: "/settings#cli", text: "Set up the command line" }), ".")),
  );
}
