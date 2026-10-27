// The History tab, like GitHub's commit list. Snapshots are grouped by day,
// each with who edited and the version tags on it. /p/<name>/commit/<sha> shows a
// snapshot's diff against the one before it.

import { api, enc, projectUrl } from "../api.js";
import { h, append, clear, plural } from "../dom.js";
import { icon } from "../icons.js";
import { navigate } from "../router.js";
import { renderDiff } from "../diff.js";
import { formatInMyZone, MONTHS, partsIn, timeAgo } from "../time.js";
import { myTimezone } from "../session.js";
import { avatar, blankslate, copyButton, loading, setTitle } from "../ui.js";

const PAGE = 50;

function firstLine(message) {
  return String(message || "Snapshot").split("\n")[0];
}

function editorsText(editors) {
  const list = (editors || []).filter(Boolean);
  if (!list.length) return "meertex";
  if (list.length === 1) return list[0];
  if (list.length === 2) return `${list[0]} and ${list[1]}`;
  return `${list[0]} and ${list.length - 1} others`;
}

function avatars(editors) {
  return (editors || []).slice(0, 3).map((e) => avatar({ email: e, name: e === "API" ? "API" : null }));
}

function tagsAt(project, commit) {
  return (project.versions || []).filter((v) => v.commit && commit && (v.commit === commit || v.commit.startsWith(commit) || commit.startsWith(v.commit)));
}

export async function render(ctx) {
  if (ctx.sub[0] === "commit" && ctx.sub[1]) return renderCommit(ctx, decodeURIComponent(ctx.sub[1]));
  const p = ctx.project;
  setTitle("History", p.name);
  const limit = Math.max(PAGE, Number(ctx.query.get("limit")) || PAGE);
  const commits = await api.get(projectUrl(ctx.ref, "/history"), { query: { limit } });
  if (!ctx.alive()) return;

  const head = h("div", { class: "subhead" },
    h("h2", { text: "History" }),
    h("span", { class: "text-small color-muted", text: "A snapshot is taken a minute after the last edit, and at least every ten minutes while people write." }));

  if (!commits.length) {
    clear(ctx.main).append(head, h("div", { class: "box" }, blankslate({
      iconName: "history",
      title: "No history yet",
      text: "The first snapshot is taken shortly after the first files arrive.",
    })));
    return;
  }

  // Grouped by the reader's own calendar day.
  const tz = myTimezone();
  const groups = [];
  for (const c of commits) {
    const d = partsIn(c.at, tz);
    const key = `${d.y}-${d.mo}-${d.d}`;
    let g = groups[groups.length - 1];
    if (!g || g.key !== key) {
      g = { key, title: `${MONTHS[d.mo]} ${d.d}, ${d.y}`, items: [] };
      groups.push(g);
    }
    g.items.push(c);
  }

  const body = groups.map((g) => h("div", { class: "commit-group" },
    h("h3", { class: "commit-group-title text-normal" }, icon("commit"), `Snapshots on ${g.title}`),
    h("ol", { class: "box box-list" }, g.items.map((c) => commitRow(c, ctx)))));

  const more = commits.length >= limit
    ? h("div", { class: "flex", style: { justifyContent: "center" } },
      h("a", { class: "btn", href: `${ctx.base}/history?limit=${limit + PAGE}`, onclick: (e) => {
        e.preventDefault();
        navigate(`${ctx.base}/history?limit=${limit + PAGE}`, { replace: true, keepScroll: true });
      } }, "Show older snapshots"))
    : null;
  append(clear(ctx.main), [head, body, more]);
}

function commitRow(c, ctx) {
  const href = `${ctx.base}/commit/${enc(c.commit)}`;
  const tags = tagsAt(ctx.project, c.commit);
  return h("li", { class: "box-row commit-row hoverable" },
    h("div", { class: "commit-main" },
      h("a", { class: "commit-message", href, text: firstLine(c.message) }),
      tags.length ? h("span", { class: "flex flex-wrap gap-4", style: { display: "inline-flex", marginLeft: "8px" } },
        tags.map((v) => h("a", { class: "tag-pill", href: `${ctx.base}/versions#v-${enc(v.tag)}` }, icon("tag", { size: 12 }), v.tag))) : null,
      h("div", { class: "commit-meta" },
        avatars(c.editors),
        h("strong", { text: editorsText(c.editors) }),
        "edited ", timeAgo(c.at),
        typeof c.files_changed === "number" ? ` · ${plural(c.files_changed, "file")} changed` : "")),
    h("div", { class: "commit-side" },
      h("a", { class: "btn btn-sm sha-btn", href, title: c.commit, text: c.commit.slice(0, 7) }),
      copyButton(c.commit, { label: "Copy the full commit id" }),
      h("a", { class: "btn-octicon", href: `${ctx.base}/files?ref=${enc(c.commit)}`, "aria-label": "Browse the files at this snapshot", title: "Browse files" }, icon("code"))),
  );
}

async function renderCommit(ctx, sha) {
  const p = ctx.project;
  setTitle(`Snapshot ${sha.slice(0, 7)}`, p.name);
  const word = ctx.query.get("word") === "1";
  const host = h("div", {}, loading());
  clear(ctx.main).append(host);

  // Find the commit in the history list. Look further back when it is older
  // than the first page.
  let commits = await api.get(projectUrl(ctx.ref, "/history"), { query: { limit: 200 } });
  let i = commits.findIndex((c) => c.commit === sha || c.commit.startsWith(sha));
  if (i < 0 && commits.length >= 200) {
    // 1000 is the most the history route returns.
    commits = await api.get(projectUrl(ctx.ref, "/history"), { query: { limit: 1000 } });
    i = commits.findIndex((c) => c.commit === sha || c.commit.startsWith(sha));
  }
  if (!ctx.alive()) return;
  if (i < 0) {
    const err = new Error(`There is no snapshot ${sha.slice(0, 7)} in this paper's history.`);
    err.status = 404;
    throw err;
  }
  const c = commits[i];
  const parent = c.parent ? { commit: c.parent } : null;
  const tags = tagsAt(p, c.commit);

  const lines = h("button", { type: "button", "aria-pressed": String(!word), text: "Lines",
    onclick: () => navigate(`${ctx.base}/commit/${enc(c.commit)}`, { replace: true, keepScroll: true }) });
  const words = h("button", { type: "button", "aria-pressed": String(word), text: "Words",
    onclick: () => navigate(`${ctx.base}/commit/${enc(c.commit)}?word=1`, { replace: true, keepScroll: true }) });

  const header = h("div", { class: "box mb-4" },
    h("div", { class: "box-header" },
      h("div", { class: "flex-1", style: { minWidth: "0" } },
        h("h2", { style: { fontSize: "16px" }, text: firstLine(c.message) }),
        String(c.message || "").includes("\n") ? h("pre", { class: "text-small color-muted mt-2", style: { whiteSpace: "pre-wrap" }, text: c.message.split("\n").slice(1).join("\n").trim() }) : null),
      h("a", { class: "btn btn-sm", href: `${ctx.base}/files?ref=${enc(c.commit)}` }, icon("code"), "Browse files")),
    h("div", { class: "box-body flex flex-wrap" },
      avatars(c.editors),
      h("strong", { text: editorsText(c.editors) }),
      h("span", { class: "color-muted" }, "edited ", h("span", { title: formatInMyZone(c.at) }, timeAgo(c.at))),
      tags.map((v) => h("a", { class: "tag-pill", href: `${ctx.base}/versions#v-${enc(v.tag)}` }, icon("tag", { size: 12 }), v.tag)),
      h("span", { class: "flex-1" }),
      parent ? h("span", { class: "text-small color-muted" }, "before: ",
        h("a", { class: "sha", href: `${ctx.base}/commit/${enc(parent.commit)}`, text: parent.commit.slice(0, 7) })) : null,
      h("span", { class: "text-small color-muted" }, "snapshot ", h("span", { class: "sha", text: c.commit.slice(0, 7) })),
      copyButton(c.commit, { label: "Copy the full commit id" })),
  );

  const diffHost = h("div", {}, loading());
  clear(host).append(
    h("div", { class: "flex mb-3" },
      h("a", { class: "btn btn-sm", href: `${ctx.base}/history` }, icon("arrow-left"), "History"),
      h("span", { class: "flex-1" }),
      parent ? h("div", { class: "segmented", role: "group", "aria-label": "Diff mode" }, lines, words) : null),
    header,
    diffHost);

  // Not awaited, so the header shows while git computes the diff.
  const load = parent
    ? api.get(projectUrl(ctx.ref, "/diff"), { query: { from: parent.commit, to: c.commit, word: word ? "true" : "false" } })
      .then((diff) => renderDiff(diff, { word }))
    : api.get(projectUrl(ctx.ref, "/files"), { query: { ref: c.commit } }).then((files) => firstSnapshot(files));
  load.then((node) => {
    if (ctx.alive()) clear(diffHost).append(node);
  }).catch((err) => {
    if (ctx.alive()) clear(diffHost).append(h("div", { class: "box" }, blankslate({ iconName: "alert", title: "Could not load the changes", text: err.message })));
  });
}

// The oldest snapshot has nothing before it, so it lists what it added.
function firstSnapshot(files) {
  if (!files.length) return h("p", { class: "color-muted", text: "The first snapshot of this paper. It holds no files yet." });
  return h("div", {},
    h("p", { class: "color-muted" }, "The first snapshot of this paper. It added ", h("strong", { text: plural(files.length, "file") }), "."),
    h("ul", { class: "box box-list diff-filelist" }, files.map((f) => h("li", { class: "box-row box-row-sm flex" },
      h("span", { class: "Label label-success", text: "Added" }),
      icon(f.kind === "blob" ? "file-binary" : "file-text", { cls: "color-muted" }),
      h("span", { class: "text-mono", text: f.path })))));
}
