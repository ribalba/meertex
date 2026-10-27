// The Overview tab: waiting questions as banners, the dates as a timeline
// with countdowns and moves, the notes, and a sidebar like a GitHub
// repository's About, Releases and Contributors boxes, and your own reminder
// mails for the paper like an issue's Notifications box.

import { api, enc, projectUrl } from "../api.js";
import { h, clear, plural } from "../dom.js";
import { icon } from "../icons.js";
import { renderMarkdown } from "../markdown.js";
import { questionControls } from "../questions.js";
import { DATE_ICONS, dateLabel, dateRow } from "../dates.js";
import { countdown, formatDue, formatInMyZone, formatShort, timeAgo } from "../time.js";
import { avatar, busy, commandLine, confirmDialog, counter, flash, flashBox, flashError, personName, setTitle } from "../ui.js";
import { editorHref } from "./editor.js";
import { planBox, writingLine } from "./timeline.js";
import { openConversationsSection } from "../threads.js";
import { session } from "../session.js";

const ANSWERED = {
  yes: ["Done", "label-success"],
  no: ["Did not happen", "label-secondary"],
  accepted: ["Accepted", "label-success"],
  rejected: ["Rejected", "label-danger"],
  revision: ["Revision requested", "label-attention"],
};

export function safeUrl(u) {
  try {
    const url = new URL(u);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

export async function render(ctx) {
  const p = ctx.project;
  setTitle(p.title || p.name, p.name);
  const dates = [...(p.dates || [])].sort((a, b) => Date.parse(a.due_at) - Date.parse(b.due_at));
  const byId = new Map(dates.map((d) => [d.id, d]));

  const banners = (p.questions || []).map((q) => flashBox(h("div", {},
    h("p", { class: "mb-2" }, h("strong", { text: q.label }), " ", h("span", { text: q.text })),
    // Viewers and reviewers see what is open but cannot answer (the API says 403).
    ctx.canEdit
      ? questionControls(q, { date: byId.get(q.date_id) || null, onAnswered: () => ctx.reload() })
      : h("p", { class: "text-small color-muted mb-0", text: "The owner or an editor can answer this." })),
  "warn", { iconName: "question" }));

  const mainCol = h("div", { class: "repo-main" },
    banners.length ? h("div", { class: "flex", style: { flexDirection: "column", alignItems: "stretch", gap: "8px" } }, banners) : null,
    banners.length ? h("div", { class: "mb-3" }) : null,
    p.planned ? planBox(p, ctx) : p.file_count === 0 ? setupBox(p, ctx) : null,
    writingLine(p),
    datesBox(p, dates, ctx),
    notesBox(p, ctx),
  );
  const side = h("aside", { "aria-label": "About this paper" },
    aboutSection(p, ctx),
    versionsSection(p, ctx),
    openConversationsSection(p, ctx.base),
    peopleSection(p, ctx),
    ctx.canEdit ? remindersSection(p) : null,
  );
  clear(ctx.main).append(h("div", { class: "layout-repo" }, mainCol, side));
}

function setupBox(p, ctx) {
  return h("div", { class: "box setup-box mb-4" },
    h("div", { class: "box-body" },
      h("h3", {}, icon("pencil", { cls: "color-muted" }), " Start writing"),
      h("p", { text: "This paper has no files yet. Write in the browser together with your coauthors, or check it out on your computer and write in your own editor. meertex keeps both in step." }),
      h("p", { class: "mb-3" }, h("a", { class: "btn btn-primary", href: editorHref(ctx.base) }, icon("pencil"), ctx.canEdit ? "Write in the browser" : "Open the editor")),
      h("h4", { class: "mb-2", text: "Or on your computer" }),
      h("div", { class: "cmd-list" },
        commandLine(`meertex checkout ${p.name}`, "1. Create the folder ~/papers/" + p.name),
        commandLine("meertex sync", "2. Keep it in sync while you write"),
        commandLine(`meertex link ~/papers/my-draft --project ${p.name}`, "Or turn an existing folder into this paper")),
    ));
}

// --- Dates ------------------------------------------------------------------

function datesBox(p, dates, ctx) {
  const nextId = p.next_date?.id || null;
  const list = h("ul", { class: "timeline" });
  const addBtn = ctx.canEdit
    ? h("button", { type: "button", class: "btn btn-sm" }, icon("plus"), "Add date")
    : null;
  const box = h("section", { class: "box mb-4", "aria-labelledby": "dates-title" },
    h("div", { class: "box-header box-header-sm" },
      icon("calendar", { cls: "color-muted" }),
      h("h2", { class: "box-title flex-1", id: "dates-title" }, "Dates ", counter(dates.length)),
      addBtn),
    list);

  const paintList = () => {
    clear(list);
    if (!dates.length) {
      list.append(h("li", { class: "timeline-item" }, h("div", { class: "color-muted" },
        "No dates yet.", ctx.canEdit ? " Add the deadlines from the call for papers." : "")));
    }
    for (const d of dates) list.append(dateItem(d, d.id === nextId, ctx));
  };
  paintList();

  addBtn?.addEventListener("click", () => {
    if (list.querySelector(".timeline-edit.is-new")) return;
    const row = dateRow({ kind: dates.length ? "other" : "submission", tz: dates[dates.length - 1]?.tz || "AoE" });
    const save = h("button", { type: "submit", class: "btn btn-primary btn-sm", text: "Add date" });
    const form = h("form", { class: "timeline-edit is-new" }, row.el,
      h("div", { class: "form-actions mt-2" }, save,
        h("button", { type: "button", class: "btn btn-sm", text: "Cancel", onclick: () => form.remove() })));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const v = row.value();
      if (!v) return;
      try {
        await busy(save, () => api.post(projectUrl(p.id, "/dates"), v));
        flash(`Added ${dateLabel(v)}.`, "success");
        ctx.reload();
      } catch (err) {
        row.showError(err.message);
      }
    });
    list.prepend(form);
    row.focus();
  });
  return box;
}

function dateItem(d, isNext, ctx) {
  const answered = ANSWERED[d.answer];
  const classes = ["timeline-item"];
  if (d.passed) classes.push("is-past");
  if (isNext) classes.push("is-next");
  if (d.answer === "yes" || d.answer === "accepted") classes.push("is-done");
  const badgeIcon = d.answer === "yes" || d.answer === "accepted" ? "check" : d.answer === "rejected" || d.answer === "no" ? "x" : DATE_ICONS[d.kind] || "calendar";

  const when = h("div", { class: "timeline-when" },
    h("span", { title: `In your time zone this is ${formatInMyZone(d.due_at)}.`, text: formatDue(d) }));
  const moves = d.changes || [];
  if (moves.length) {
    const last = moves[moves.length - 1];
    const history = moves.map((c) => {
      const from = c.from ? formatShort(c.from, d.tz, { dateOnly: d.date_only }) : "?";
      const to = c.to ? formatShort(c.to, d.tz, { dateOnly: d.date_only }) : "?";
      return `${from} to ${to}${c.at ? `, on ${formatInMyZone(c.at, { time: false })}` : ""}${c.by ? ` by ${typeof c.by === "string" ? c.by : personName(c.by)}` : ""}`;
    }).join("\n");
    when.append(h("span", { class: "moved", title: `Moved ${plural(moves.length, "time")}:\n${history}` },
      icon("history", { size: 12 }), ` moved from ${formatShort(last.from, d.tz, { compareTo: d.due_at, dateOnly: d.date_only })}`,
      moves.length > 1 ? ` (${moves.length} moves)` : ""));
  }

  const side = h("div", { class: "timeline-side" }, countdown(d.due_at));
  const li = h("li", { class: classes.join(" ") },
    h("div", { class: "timeline-badge" }, icon(badgeIcon)),
    h("div", { class: "timeline-body" },
      h("div", { class: "timeline-title" },
        h("strong", { text: dateLabel(d) }),
        isNext ? h("span", { class: "Label label-accent", text: "Next" }) : null,
        answered ? h("span", { class: `Label ${answered[1]}`, text: answered[0] }) : null),
      when),
    side);

  if (ctx.canEdit) {
    const edit = h("button", { type: "button", class: "btn-octicon", "aria-label": `Edit ${dateLabel(d)}`, title: "Edit" }, icon("pencil"));
    const del = h("button", { type: "button", class: "btn-octicon danger", "aria-label": `Delete ${dateLabel(d)}`, title: "Delete" }, icon("trash"));
    side.append(edit, del);
    edit.addEventListener("click", () => {
      const row = dateRow(d);
      const save = h("button", { type: "submit", class: "btn btn-primary btn-sm", text: "Save" });
      const form = h("form", { class: "timeline-edit" }, row.el,
        h("p", { class: "text-small color-muted mt-2 mb-2", text: "A new day or time is recorded as a move and clears the answer." }),
        h("div", { class: "form-actions" }, save,
          h("button", { type: "button", class: "btn btn-sm", text: "Cancel", onclick: () => form.replaceWith(li) })));
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const changes = row.changes();
        if (!changes) return;
        if (!Object.keys(changes).length) { form.replaceWith(li); return; }
        try {
          await busy(save, () => api.patch(projectUrl(ctx.ref, `/dates/${enc(d.id)}`), changes));
          flash(`Saved ${dateLabel({ ...d, ...changes })}.`, "success");
          ctx.reload();
        } catch (err) {
          row.showError(err.message);
        }
      });
      li.replaceWith(form);
      row.focus();
    });
    del.addEventListener("click", async () => {
      const ok = await confirmDialog({
        title: `Delete ${dateLabel(d)}?`,
        message: `${formatDue(d)}. Versions made for this date stay.`,
        confirmText: "Delete date",
        danger: true,
      });
      if (!ok) return;
      try {
        await api.del(projectUrl(ctx.ref, `/dates/${enc(d.id)}`));
        flash(`Deleted ${dateLabel(d)}.`, "success");
        ctx.reload();
      } catch (err) {
        flashError(err);
      }
    });
  }
  return li;
}

// --- Notes ------------------------------------------------------------------

function notesBox(p, ctx) {
  const body = h("div", { class: "box-body" });
  const paint = () => {
    clear(body);
    if (p.notes && p.notes.trim()) body.append(h("div", { class: "markdown", html: renderMarkdown(p.notes) }));
    else body.append(h("p", { class: "color-muted mb-0", text: ctx.canEdit ? "No notes yet. Keep what the CFP says and anything else worth remembering here." : "No notes yet." }));
  };
  paint();
  const edit = ctx.canEdit ? h("button", { type: "button", class: "btn-octicon", "aria-label": "Edit notes", title: "Edit notes" }, icon("pencil")) : null;
  edit?.addEventListener("click", () => {
    const ta = h("textarea", { class: "form-control input-block input-mono", rows: "12", "aria-label": "Notes" });
    ta.value = p.notes || "";
    const save = h("button", { type: "submit", class: "btn btn-primary btn-sm", text: "Save notes" });
    const form = h("form", {}, ta,
      h("div", { class: "form-actions mt-2" }, save,
        h("button", { type: "button", class: "btn btn-sm", text: "Cancel", onclick: () => { paint(); edit.hidden = false; } }),
        h("span", { class: "text-small color-muted", text: "Markdown: **bold**, *italic*, lists, links and `code`." })));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const updated = await busy(save, () => api.patch(projectUrl(ctx.ref), { notes: ta.value }));
        p.notes = updated?.notes ?? ta.value;
        paint();
        edit.hidden = false;
        flash("Notes saved.", "success");
      } catch (err) {
        flashError(err);
      }
    });
    clear(body).append(form);
    edit.hidden = true;
    ta.focus();
  });
  return h("section", { class: "box readme-box", "aria-labelledby": "notes-title" },
    h("div", { class: "box-header" }, h("h2", { id: "notes-title" }, icon("file-text"), "Notes"), edit),
    body);
}

// --- Sidebar ----------------------------------------------------------------

function linkItem(iconName, url, text) {
  const href = safeUrl(url);
  return h("li", {}, icon(iconName),
    href ? h("a", { class: "link", href, target: "_blank", rel: "noopener noreferrer", text: text || url }) : h("span", { class: "value", text: url }));
}

function hostOf(url) {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function aboutSection(p, ctx) {
  const items = [];
  const venue = [p.conference, p.year].filter(Boolean).join(" ");
  if (venue || p.track) items.push(h("li", {}, icon("globe"), h("span", { class: "value", text: [venue, p.track].filter(Boolean).join(" · ") })));
  if (p.location) items.push(h("li", {}, icon("location"), h("span", { class: "value", text: p.location })));
  if (p.cfp_url) items.push(linkItem("link", p.cfp_url, "Call for papers"));
  if (p.conference_url) items.push(linkItem("link", p.conference_url, hostOf(p.conference_url)));
  if (p.submission_url) items.push(linkItem("upload", p.submission_url, "Submission site"));
  if (p.publisher) items.push(h("li", {}, icon("book"), h("span", { class: "value", text: p.publisher })));
  if (p.page_limit) items.push(h("li", {}, icon("file-text"), h("span", { class: "value", text: p.page_limit })));
  if (p.template) items.push(h("li", {}, icon("code"), h("span", { class: "value", text: p.template })));
  if (p.review_model) items.push(h("li", {}, icon("eye"), h("span", { class: "value", text: `${p.review_model[0].toUpperCase()}${p.review_model.slice(1)} review` })));
  if (p.doi) items.push(linkItem("link", /^https?:/i.test(p.doi) ? p.doi : `https://doi.org/${p.doi}`, `doi:${p.doi.replace(/^https?:\/\/(dx\.)?doi\.org\//i, "")}`));
  if (p.publication_url) items.push(linkItem("file", p.publication_url, "Published paper"));
  if (p.main_file && p.main_file !== "main.tex") {
    items.push(h("li", {}, icon("file"), h("a", { class: "text-mono", href: editorHref(ctx.base, p.main_file), title: "Open the main file in the editor", text: p.main_file })));
  }
  if (p.forked_from) {
    items.push(h("li", {}, icon("fork"), h("span", {}, "Resubmitted from ",
      h("a", { href: `/p/${enc(p.forked_from.name || p.forked_from.id)}`, text: p.forked_from.name || "an earlier paper" }))));
  }

  return h("div", { class: "sidebar-section about" },
    h("h2", {}, "About",
      ctx.canEdit ? h("a", { class: "btn-octicon", href: `${ctx.base}/settings`, "aria-label": "Edit details", title: "Edit details" }, icon("gear")) : null),
    p.conference_name ? h("p", { class: "about-lede", text: p.conference_name }) : null,
    items.length ? h("ul", { class: "about-list" }, items)
      : h("p", { class: "color-muted text-small", text: "No venue details yet." }),
  );
}

function versionsSection(p, ctx) {
  const versions = [...(p.versions || [])].sort((a, b) => (b.number || 0) - (a.number || 0));
  const latest = versions[0];
  return h("div", { class: "sidebar-section" },
    h("h2", {}, h("a", { href: `${ctx.base}/versions` }, "Versions"), counter(versions.length)),
    latest
      ? h("div", {},
        h("div", { class: "sidebar-version" }, icon("tag"),
          h("div", { class: "flex-1", style: { minWidth: "0" } },
            h("div", { class: "flex flex-wrap gap-4" },
              h("a", { class: "text-bold", href: `${ctx.base}/versions#v-${enc(latest.tag)}`, text: latest.label }),
              h("span", { class: "Label label-success", text: "Latest" })),
            h("div", { class: "text-small color-muted" }, timeAgo(latest.created_at)))),
        versions.length > 1 ? h("a", { class: "text-small", href: `${ctx.base}/versions`, text: `+ ${plural(versions.length - 1, "more version")}` }) : null)
      : h("div", { class: "text-small color-muted" },
        "No versions yet. ",
        ctx.canEdit ? h("a", { href: `${ctx.base}/versions?new=1`, text: "Save one" }) : null,
        ctx.canEdit ? " when you hand something in." : ""),
  );
}

// Owners and editors get reminder mails before the paper's dates (DESIGN §5,
// Reminders). Muting is per person, so it only changes your own mails.
function remindersSection(p) {
  let muted = Boolean(p.reminders_muted);
  const button = h("button", { type: "button", class: "btn btn-sm btn-block" });
  const note = h("p", { class: "text-small color-muted mt-2 mb-0" });
  const paint = () => {
    button.replaceChildren(icon(muted ? "bell" : "bell-slash"), muted ? "Unmute reminders" : "Mute reminders");
    const allOff = session.me?.reminders === false;
    const text = allOff ? "Your reminder mails are off for every paper."
      : muted ? "You get no reminder mails about this paper."
        : "You get a mail before this paper's deadlines.";
    note.replaceChildren(text, " ", h("a", { href: "/settings#reminders", text: "Reminder settings" }));
  };
  button.addEventListener("click", async () => {
    try {
      const summary = await busy(button, () => api.put(projectUrl(p.id, "/reminders"), { muted: !muted }));
      muted = Boolean(summary?.reminders_muted);
      p.reminders_muted = muted;
      paint();
      flash(muted ? `Reminders for ${p.name} are muted.` : `Reminders for ${p.name} are on again.`, "success");
    } catch (err) {
      flashError(err);
    }
  });
  paint();
  return h("div", { class: "sidebar-section" }, h("h2", {}, "Reminders"), button, note);
}

function peopleSection(p, ctx) {
  const members = p.members || [];
  return h("div", { class: "sidebar-section" },
    h("h2", {}, ctx.canEdit ? h("a", { href: `${ctx.base}/settings/access` }, "People") : "People", counter(members.length)),
    h("ul", { class: "people-list" }, members.map((m) => h("li", {},
      avatar(m, 32),
      h("div", { class: "flex-1", style: { minWidth: "0" } },
        h("div", { class: "truncate text-bold", text: m.name || m.email }),
        h("div", { class: "text-small color-muted truncate", text: m.name ? `${m.email} · ${m.role}` : m.role })))),
  ));
}
