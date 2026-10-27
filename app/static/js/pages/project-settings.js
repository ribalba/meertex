// The Settings tab, like a GitHub repository's settings: a menu on the left,
// General (details, outcome, labels, rename, archive, delete), Access
// (members) and Resubmit (fork to another venue).

import { api, enc, projectUrl } from "../api.js";
import { h, append, clear, debounce, nextId, plural } from "../dom.js";
import { icon } from "../icons.js";
import { navigate } from "../router.js";
import { dateList } from "../dates.js";
import { labelEditor, venueFields, VENUE_KEYS } from "../forms.js";
import { timeAgo } from "../time.js";
import {
  avatar, busy, confirmDialog, field, flash, flashBox, flashError, openDialog, OUTCOMES, queueFlash,
  setTitle, textInput,
} from "../ui.js";

const SECTIONS = [
  { key: "", text: "General", icon: "gear" },
  { key: "access", text: "Access", icon: "people" },
  { key: "resubmit", text: "Resubmit", icon: "fork" },
];

export async function render(ctx) {
  const section = ctx.sub[1] || "";
  const known = SECTIONS.find((s) => s.key === section);
  if (!known) {
    const err = new Error("There is no such settings page.");
    err.status = 404;
    throw err;
  }
  setTitle(`${known.text} settings`, ctx.project.name);

  const menu = h("nav", { class: "side-menu", "aria-label": "Settings" },
    SECTIONS.map((s) => h("a", {
      href: `${ctx.base}/settings${s.key ? `/${s.key}` : ""}`,
      "aria-current": s.key === section ? "page" : null,
    }, icon(s.icon), s.text)));
  const content = h("div", { class: "settings-content" });
  if (section === "") general(ctx, content);
  else if (section === "access") await access(ctx, content);
  else await resubmit(ctx, content);
  if (!ctx.alive()) return;
  clear(ctx.main).append(h("div", { class: "layout-settings" }, h("div", {}, menu), content));
}

// --- General ----------------------------------------------------------------

function general(ctx, out) {
  const p = ctx.project;

  // Rename, on its own like GitHub's repository name field.
  const nameInput = textInput({ value: p.name, spellcheck: "false", pattern: "[A-Za-z0-9][A-Za-z0-9._\\-]*", maxlength: "120" });
  const renameBtn = h("button", { type: "submit", class: "btn", text: "Rename" });
  const renameForm = h("form", { class: "form-group" },
    h("label", { class: "form-label", for: (nameInput.id = "settings-name") }, "Name"),
    h("div", { class: "flex flex-wrap" }, h("div", { class: "flex-1", style: { maxWidth: "440px" } }, nameInput), renameBtn),
    h("div", { class: "form-hint", text: "Checkouts on your coauthors' computers keep their folder name. meertex sync points the rename out." }));
  renameForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = nameInput.value.trim();
    if (!name || name === p.name) return;
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$/.test(name)) {
      flash("A name starts with a letter or digit and holds only letters, digits, dot, dash and underscore.", "error");
      return;
    }
    const ok = await confirmDialog({
      title: `Rename ${p.name}?`,
      message: h("div", {},
        h("p", {}, "The paper will be called ", h("strong", { text: name }), "."),
        flashBox("Local checkouts keep their folder name, and links with the old name stop working.", "warn")),
      confirmText: "Rename",
    });
    if (!ok) return;
    try {
      const updated = await busy(renameBtn, () => api.patch(projectUrl(ctx.ref), { name }));
      queueFlash(`Renamed to ${updated?.name || name}.`, "success");
      navigate(`/p/${enc(updated?.name || name)}/settings`, { replace: true });
    } catch (err) {
      flashError(err);
    }
  });

  // The details form holds every field of DESIGN §4 that a person edits.
  const inputs = {
    title: textInput({ value: p.title || "" }),
    paper: textInput({ value: p.paper || "", spellcheck: "false" }),
    conference: textInput({ value: p.conference || "" }),
    year: textInput({ type: "number", min: "1990", max: "2100", value: p.year ? String(p.year) : "" }),
    main_file: textInput({ value: p.main_file || "main.tex", spellcheck: "false" }),
    doi: textInput({ value: p.doi || "", placeholder: "10.1145/…", spellcheck: "false" }),
    publication_url: textInput({ type: "url", value: p.publication_url || "", placeholder: "https://…" }),
    notes: h("textarea", { class: "form-control input-block input-mono", rows: "8" }),
  };
  inputs.notes.value = p.notes || "";
  const venue = venueFields(p);
  const saveBtn = h("button", { type: "submit", class: "btn btn-primary", text: "Save changes" });
  const details = h("form", {},
    field("Title", inputs.title),
    h("div", { class: "form-row" },
      field("Conference", inputs.conference),
      field("Year", inputs.year),
      field("Short name", inputs.paper, { hint: "Only used when the name is built." })),
    venue.el,
    h("div", { class: "form-row" },
      field("Main file", inputs.main_file, { hint: "The file LaTeX compiles." }),
      field("DOI", inputs.doi, { optional: true })),
    field("Published version", inputs.publication_url, { optional: true }),
    field("Notes", inputs.notes, { optional: true, hint: "Markdown. Shown on the overview." }),
    h("div", { class: "form-actions" }, saveBtn));
  details.addEventListener("submit", async (e) => {
    e.preventDefault();
    const values = {
      ...venue.values(),
      title: inputs.title.value.trim(),
      paper: inputs.paper.value.trim(),
      conference: inputs.conference.value.trim(),
      main_file: inputs.main_file.value.trim() || "main.tex",
      doi: inputs.doi.value.trim(),
      publication_url: inputs.publication_url.value.trim(),
      notes: inputs.notes.value,
    };
    const year = inputs.year.value ? Number(inputs.year.value) : null;
    const changes = {};
    for (const [k, v] of Object.entries(values)) if ((p[k] ?? "") !== v) changes[k] = v;
    if ((p.year ?? null) !== year) changes.year = year;
    if (!Object.keys(changes).length) {
      flash("Nothing changed.", "info", { timeout: 3000 });
      return;
    }
    try {
      const updated = await busy(saveBtn, () => api.patch(projectUrl(ctx.ref), changes));
      Object.assign(p, updated || changes);
      flash("Saved.", "success");
      ctx.reload();
    } catch (err) {
      flashError(err);
    }
  });

  // Outcome.
  const outcome = h("select", { class: "form-select", value: p.outcome || "" },
    h("option", { value: "" }, "Undecided"),
    Object.entries(OUTCOMES).map(([v, o]) => h("option", { value: v }, o.text)));
  outcome.id = "settings-outcome";
  const outcomeBtn = h("button", { type: "submit", class: "btn", text: "Update outcome" });
  const outcomeForm = h("form", { class: "form-group" },
    h("label", { class: "form-label", for: outcome.id, text: "Outcome" }),
    h("div", { class: "flex flex-wrap" }, outcome, outcomeBtn,
      p.outcome && p.outcome_at ? h("span", { class: "text-small color-muted" }, "Set ", timeAgo(p.outcome_at)) : null),
    h("div", { class: "form-hint", text: "Usually set by answering the notification question. Rejected, withdrawn and not submitted stop the countdowns." }));
  outcomeForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if ((p.outcome || "") === outcome.value) return;
    try {
      await busy(outcomeBtn, () => api.patch(projectUrl(ctx.ref), { outcome: outcome.value || null }));
      flash(`Outcome set to ${OUTCOMES[outcome.value]?.text || "Undecided"}.`, "success");
      ctx.reload();
    } catch (err) {
      flashError(err);
    }
  });

  // Labels.
  const labels = labelEditor(p.labels || []);
  labels.input.id = "settings-labels";
  const labelsBtn = h("button", { type: "submit", class: "btn", text: "Save labels" });
  const labelsForm = h("form", { class: "form-group" },
    h("label", { class: "form-label", for: labels.input.id, text: "Labels" }),
    labels.el,
    h("div", { class: "form-hint", text: "Free labels to filter the list, like green-software or with-arne. Press Enter or comma after each." }),
    h("div", { class: "mt-2" }, labelsBtn));
  labelsForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await busy(labelsBtn, () => api.patch(projectUrl(ctx.ref), { labels: labels.values() }));
      flash("Labels saved.", "success");
      ctx.reload();
    } catch (err) {
      flashError(err);
    }
  });

  // Danger zone.
  const archiveBtn = h("button", { type: "button", class: "btn btn-danger", text: p.archived ? "Unarchive" : "Archive" });
  archiveBtn.addEventListener("click", async () => {
    const ok = await confirmDialog({
      title: p.archived ? `Unarchive ${p.name}?` : `Archive ${p.name}?`,
      message: p.archived ? "The paper shows up in the default list again." : "Archived papers are hidden from the default list. Nothing else changes, and you can unarchive it any time.",
      confirmText: p.archived ? "Unarchive" : "Archive",
      danger: !p.archived,
    });
    if (!ok) return;
    try {
      await busy(archiveBtn, () => api.patch(projectUrl(ctx.ref), { archived: !p.archived }));
      flash(p.archived ? "Unarchived." : "Archived. It is hidden from the default list now.", "success");
      ctx.reload();
    } catch (err) {
      flashError(err);
    }
  });
  const deleteBtn = h("button", { type: "button", class: "btn btn-danger", text: "Delete this paper", disabled: !ctx.isOwner });
  deleteBtn.addEventListener("click", () => deleteDialog(ctx));

  out.append(
    h("div", { class: "subhead" }, h("h2", { text: "General" })),
    renameForm,
    h("hr"),
    h("div", { class: "subhead" }, h("h3", { text: "Details" })),
    details,
    h("div", { class: "subhead mt-4" }, h("h3", { text: "Outcome and labels" })),
    outcomeForm,
    labelsForm,
    h("div", { class: "subhead mt-4 subhead-danger" }, h("h3", { text: "Danger zone" })),
    h("div", { class: "box box-danger" },
      h("div", { class: "box-row danger-row" },
        h("div", {}, h("h4", { text: p.archived ? "Unarchive this paper" : "Archive this paper" }),
          h("p", { text: p.archived ? "It is archived and hidden from the default list." : "Hide it from the default list. Nothing else changes." })),
        archiveBtn),
      h("div", { class: "box-row danger-row" },
        h("div", {}, h("h4", { text: "Delete this paper" }),
          h("p", { text: ctx.isOwner ? "Its files, history and versions go for every member. There is no going back." : "Only the owner can delete a paper." })),
        deleteBtn)),
  );
}

function deleteDialog(ctx) {
  const p = ctx.project;
  const input = textInput({ spellcheck: "false", autocomplete: "off", "aria-label": `Type ${p.name} to confirm` });
  const del = h("button", { type: "button", class: "btn btn-danger btn-block", disabled: true, text: "Delete this paper" });
  input.addEventListener("input", () => { del.disabled = input.value !== p.name; });
  const d = openDialog({
    title: `Delete ${p.name}`,
    body: h("div", {},
      flashBox("This cannot be undone. The files, the history, every version and every attached PDF go, for all members.", "warn"),
      h("p", { class: "mt-3" }, "To confirm, type ", h("strong", { text: p.name }), " below."),
      input),
    footer: [del],
  });
  input.focus();
  del.addEventListener("click", async () => {
    try {
      await busy(del, () => api.del(projectUrl(ctx.ref), { query: { confirm: input.value } }));
      d.close();
      queueFlash(`Deleted ${p.name}.`, "success");
      navigate("/");
    } catch (err) {
      flashError(err);
    }
  });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !del.disabled) del.click(); });
}

// --- Access -----------------------------------------------------------------

const ROLE_TEXT = {
  owner: "Everything, including deleting the paper and managing people.",
  editor: "Edits files and details, answers questions, saves versions, invites reviewers and viewers.",
  reviewer: "Reads everything, and reviews (coming with reviews).",
  viewer: "Reads everything.",
};

async function access(ctx, out) {
  const p = ctx.project;
  // People from the user's other papers are suggested in "Add someone". The
  // page works without them.
  const [members, people] = await Promise.all([
    api.get(projectUrl(ctx.ref, "/members")),
    ctx.canEdit ? api.get("/api/people").catch(() => []) : [],
  ]);
  if (!ctx.alive()) return;
  const order = { owner: 0, editor: 1, reviewer: 2, viewer: 3 };
  members.sort((a, b) => (order[a.role] ?? 9) - (order[b.role] ?? 9) || (a.email || "").localeCompare(b.email || ""));

  const rows = members.map((m) => {
    let role;
    if (m.role === "owner") role = h("span", { class: "Label", text: "Owner" });
    else if (ctx.isOwner) {
      role = h("select", { class: "form-select form-control-sm", "aria-label": `Role of ${m.email}`, value: m.role },
        ["editor", "reviewer", "viewer"].map((r) => h("option", { value: r }, r[0].toUpperCase() + r.slice(1))));
      role.addEventListener("change", async () => {
        try {
          await api.patch(projectUrl(ctx.ref, `/members/${enc(m.user_id)}`), { role: role.value });
          flash(`${m.email} is now ${role.value}.`, "success");
          m.role = role.value;
        } catch (err) {
          role.value = m.role;
          flashError(err);
        }
      });
    } else role = h("span", { class: "Label", text: m.role[0].toUpperCase() + m.role.slice(1) });

    const remove = ctx.isOwner && m.role !== "owner"
      ? h("button", { type: "button", class: "btn btn-sm btn-danger", text: "Remove" })
      : null;
    remove?.addEventListener("click", async () => {
      const ok = await confirmDialog({
        title: `Remove ${m.name || m.email}?`,
        message: `${m.email} loses access to ${p.name}. Their local checkout stays on their computer but stops syncing.`,
        confirmText: "Remove",
        danger: true,
      });
      if (!ok) return;
      try {
        await api.del(projectUrl(ctx.ref, `/members/${enc(m.user_id)}`));
        flash(`Removed ${m.email}.`, "success");
        ctx.reload();
      } catch (err) {
        flashError(err);
      }
    });

    return h("li", { class: "box-row member-row" },
      avatar(m, 32),
      h("div", { class: "who" },
        h("div", { class: "name", text: m.name || m.email }),
        h("div", { class: "email" }, m.name ? m.email : null, m.name && m.added_at ? " · " : null,
          m.added_at ? ["added ", timeAgo(m.added_at)] : null)),
      role,
      remove);
  });

  const onPaper = new Set(members.map((m) => m.email));
  const suggestions = h("datalist", { id: nextId("people") },
    people.filter((x) => !onPaper.has(x.email)).map((x) => h("option", { value: x.email, label: x.name || null })));
  const email = textInput({ type: "email", placeholder: "coauthor@example.com", autocomplete: "off", required: true, "aria-label": "Email", list: suggestions.id });
  const roleSel = h("select", { class: "form-select", "aria-label": "Role", value: ctx.isOwner ? "editor" : "reviewer" },
    ["editor", "reviewer", "viewer"].map((r) => h("option", { value: r, disabled: r === "editor" && !ctx.isOwner }, r[0].toUpperCase() + r.slice(1))));
  const add = h("button", { type: "submit", class: "btn btn-primary", text: "Add" });
  const invite = h("form", { class: "box mt-4" },
    h("div", { class: "box-header box-header-sm" }, h("h3", { class: "box-title", text: "Add someone" })),
    h("div", { class: "box-body" },
      h("div", { class: "flex flex-wrap" }, h("div", { class: "flex-1", style: { minWidth: "220px" } }, email, suggestions), roleSel, add),
      h("p", { class: "form-hint mb-0", text: "Someone without an account gets one right away. They receive an email with a sign-in link that opens this paper." }),
      ctx.isOwner ? null : h("p", { class: "form-hint mb-0", text: "Editors can add reviewers and viewers. Only the owner adds editors." })));
  invite.addEventListener("submit", async (e) => {
    e.preventDefault();
    const value = email.value.trim();
    if (!value || !email.checkValidity()) {
      flash("Enter an email address.", "error");
      email.focus();
      return;
    }
    try {
      await busy(add, () => api.post(projectUrl(ctx.ref, "/members"), { email: value, role: roleSel.value }));
      flash(`Added ${value} as ${roleSel.value}. They get an email with a sign-in link.`, "success");
      ctx.reload();
    } catch (err) {
      flashError(err);
    }
  });

  append(out, [
    h("div", { class: "subhead" }, h("h2", { text: "Who has access" })),
    h("ul", { class: "box box-list" },
      h("li", { class: "box-header box-header-sm" }, h("h3", { class: "box-title flex-1", text: plural(members.length, "person", "people") })),
      rows),
    ctx.canEdit ? invite : null,
    h("div", { class: "mt-4" },
      h("h3", { class: "mb-2", style: { fontSize: "14px" }, text: "Roles" }),
      h("ul", { class: "text-small color-muted", style: { paddingLeft: "20px", margin: "0" } },
        Object.entries(ROLE_TEXT).map(([r, t]) => h("li", {}, h("strong", { class: "color-fg", text: `${r[0].toUpperCase()}${r.slice(1)}: ` }), t)))),
  ]);
}

// --- Resubmit ---------------------------------------------------------------

async function resubmit(ctx, out) {
  const p = ctx.project;
  const folders = await api.get("/api/folders");
  if (!ctx.alive()) return;

  const title = textInput({ value: p.title || "", required: true });
  const paper = textInput({ value: p.paper || "", spellcheck: "false" });
  const conference = textInput({ placeholder: "EuroSys", autocomplete: "off" });
  const year = textInput({ type: "number", min: "1990", max: "2100", value: String((p.year || new Date().getFullYear()) + 1) });
  const venue = venueFields({ template: p.template, review_model: "" }, { split: true });
  const dates = dateList([{ kind: "abstract" }, { kind: "submission" }]);
  const labels = labelEditor(p.labels || []);
  labels.input.id = "fork-labels";
  const folder = h("select", { class: "form-select input-block", value: p.folder_id || "" },
    h("option", { value: "" }, "Top level"),
    folders.map((f) => h("option", { value: f.id }, f.path)));

  const nameOut = h("span", { class: "name", text: "…" });
  const nameNote = h("span", { class: "text-small color-muted" });
  const fields = () => {
    const body = {
      title: title.value.trim(),
      paper: paper.value.trim(),
      conference: conference.value.trim(),
      ...venue.values(),
      labels: labels.values(),
      folder: folder.value || null,
    };
    if (year.value) body.year = Number(year.value);
    for (const k of VENUE_KEYS) if (!body[k]) delete body[k];
    return body;
  };
  let token = 0;
  const preview = debounce(async () => {
    const mine = ++token;
    try {
      const res = await api.post("/api/projects", { ...fields(), dates: [], dry_run: true });
      if (mine === token) { nameOut.textContent = res?.name || "…"; nameNote.textContent = ""; }
    } catch (err) {
      if (mine === token) { nameOut.textContent = "…"; nameNote.textContent = err.message; }
    }
  }, 300);
  [title, paper, conference, year, venue.inputs.track].forEach((el) => el.addEventListener("input", preview));
  preview();

  const submit = h("button", { type: "submit", class: "btn btn-primary" }, icon("fork"), "Create the resubmission");
  const error = h("div");
  const form = h("form", { novalidate: true },
    field("Title", title),
    h("div", { class: "form-row" },
      field("New conference", conference),
      field("Year", year),
      field("Short name", paper, { optional: true })),
    h("div", { class: "name-preview form-group", "aria-live": "polite" },
      icon("book", { cls: "color-muted" }), h("span", { class: "color-muted", text: "The new paper will be called" }), nameOut, nameNote),
    venue.main,
    h("details", { class: "form-group" },
      h("summary", { class: "text-bold", style: { cursor: "pointer" }, text: "More about the new venue" }),
      h("div", { class: "mt-3" }, venue.more)),
    h("h3", { class: "mb-2", style: { fontSize: "16px" }, text: "Dates of the new venue" }),
    dates.el,
    h("hr"),
    h("div", { class: "form-row" },
      field("Folder", folder),
      h("div", { class: "form-group" }, h("label", { class: "form-label", for: labels.input.id, text: "Labels" }), labels.el)),
    error,
    h("div", { class: "form-actions" }, submit));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    clear(error);
    if (!title.value.trim() || !conference.value.trim()) {
      error.append(flashBox("The resubmission needs a title and the new conference.", "error"), h("div", { class: "mb-3" }));
      return;
    }
    const ds = dates.values();
    if (ds === null) return;
    try {
      const created = await busy(submit, () => api.post(projectUrl(ctx.ref, "/fork"), { ...fields(), dates: ds }));
      queueFlash(h("span", {}, "Created ", h("strong", { text: created.name }), ` from ${p.name}. Check it out with meertex checkout ${created.name}.`), "success");
      navigate(`/p/${enc(created.name)}`);
    } catch (err) {
      error.append(flashBox(err.message, "error"), h("div", { class: "mb-3" }));
    }
  });

  out.append(
    h("div", { class: "subhead" }, h("h2", { text: "Resubmit to another venue" })),
    h("p", { class: "color-muted" },
      "Creates a new paper for the next venue. It starts from the live files of ", h("strong", { text: p.name }),
      " and gets its own name, dates and history.",
      p.outcome ? ` This paper keeps its outcome (${OUTCOMES[p.outcome]?.text || p.outcome}), so your lists stay true.` : " This paper stays as it is."),
    form,
  );
}
