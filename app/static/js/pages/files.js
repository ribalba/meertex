// The Files tab, like GitHub's Code tab: a ref selector (live, HEAD and every
// version), a directory listing built from the flat file list, a file view
// with line numbers, and how to check the paper out locally.

import { api, enc, encPath, projectUrl, request } from "../api.js";
import { h, clear, formatBytes, plural } from "../dom.js";
import { icon } from "../icons.js";
import { decodePath } from "../router.js";
import { timeAgo } from "../time.js";
import { blankslate, commandLine, copyButton, dropdown, setTitle } from "../ui.js";
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
  const ref = ctx.query.get("ref") || "live";
  const path = decodePath(ctx.sub.slice(1).join("/"));
  setTitle(path || "Files", p.name);

  const list = await api.get(projectUrl(ctx.ref, "/files"), { query: { ref } });
  if (!ctx.alive()) return;
  list.sort((a, b) => a.path.localeCompare(b.path));

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
  clear(ctx.main).append(toolbar, body);
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

  const rows = lines.map((l, i) => h("tr", { id: `L${i + 1}` },
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
