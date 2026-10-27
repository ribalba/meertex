// The Versions tab, like GitHub's Releases. Each version is a card with its
// tag, commit, note, attached PDF, compiled PDF and source zip.
// /p/<name>/compare shows the diff between two states, by lines, by words or
// as a PDF made by latexdiff.

import { api, enc, projectUrl } from "../api.js";
import { h, clear, nextId } from "../dom.js";
import { icon } from "../icons.js";
import { navigate, onLeave } from "../router.js";
import { checkView } from "../pagelimit.js";
import { createPdfView } from "../pdfview.js";
import { renderMarkdown } from "../markdown.js";
import { renderDiff } from "../diff.js";
import { uploadPdf } from "../questions.js";
import { dateLabel } from "../dates.js";
import { formatDay, formatDue, formatInMyZone, timeAgo } from "../time.js";
import { refName, refOptions } from "./files.js";
import {
  avatar, blankslate, busy, confirmDialog, counter, dropdown, field, flash, flashError, loading,
  personName, setTitle, spinner, textInput,
} from "../ui.js";

export const VERSION_KINDS = [
  ["submission", "Submission"],
  ["rebuttal", "Rebuttal"],
  ["revision", "Revision"],
  ["camera_ready", "Camera-ready"],
  ["arxiv", "arXiv"],
  ["snapshot", "Snapshot"],
  ["other", "Other"],
];
const KIND_TEXT = Object.fromEntries(VERSION_KINDS);

export async function render(ctx) {
  if (ctx.sub[0] === "compare") return renderCompare(ctx);
  const p = ctx.project;
  setTitle("Versions", p.name);
  const versions = [...(p.versions || [])].sort((a, b) => (b.number || 0) - (a.number || 0));

  const formHost = h("div");
  const newBtn = ctx.canEdit ? h("button", { type: "button", class: "btn btn-primary" }, icon("tag"), "New version") : null;
  const head = h("div", { class: "subhead" },
    h("h2", {}, "Versions ", counter(versions.length)),
    h("a", { class: "btn", href: `${ctx.base}/compare` }, icon("arrow-switch"), "Compare"),
    newBtn);
  newBtn?.addEventListener("click", () => {
    if (formHost.firstChild) { clear(formHost); return; }
    formHost.append(newVersionForm(ctx, () => clear(formHost)));
    formHost.querySelector("select")?.focus();
  });

  const list = versions.length
    ? h("div", {}, versions.map((v, i) => versionCard(v, i === 0, ctx)))
    : h("div", { class: "box" }, blankslate({
      iconName: "tag",
      title: "No versions yet",
      text: "A version keeps the files exactly as they were handed in. meertex offers one when you answer that a deadline is done, and you can save one any time.",
      action: newBtn ? h("button", { type: "button", class: "btn btn-primary", text: "Save the first version", onclick: () => newBtn.click() }) : null,
    }));

  clear(ctx.main).append(head, formHost, list);
  if (ctx.query.get("new") === "1" && newBtn) newBtn.click();
}

function newVersionForm(ctx, close) {
  const p = ctx.project;
  const dates = [...(p.dates || [])].sort((a, b) => Date.parse(a.due_at) - Date.parse(b.due_at));
  const kind = h("select", { class: "form-select input-block", value: "snapshot" }, VERSION_KINDS.map(([v, t]) => h("option", { value: v }, t)));
  const label = textInput({ placeholder: "Snapshot N", maxlength: "200" });
  const dateSel = h("select", { class: "form-select input-block" },
    h("option", { value: "" }, "None"),
    dates.map((d) => h("option", { value: d.id }, `${dateLabel(d)} · ${formatDue(d)}`)));
  const name = nextId("at");
  const atNow = h("input", { type: "radio", name, value: "now", checked: true });
  const atDeadline = h("input", { type: "radio", name, value: "deadline", disabled: true });
  const note = h("textarea", { class: "form-control input-block", rows: "3", placeholder: "What is in this version, and where it went" });
  const pdf = h("input", { type: "file", accept: "application/pdf,.pdf", class: "input-block" });
  const error = h("div", { class: "form-error mb-2", role: "alert", hidden: true });

  const syncKind = () => {
    label.placeholder = kind.value === "other" ? "Required for Other" : `${KIND_TEXT[kind.value]}${kind.value === "revision" || kind.value === "snapshot" ? " N" : ""}`;
    // Pick the matching date for a hand-in, which is what people mean.
    const match = { submission: "submission", rebuttal: "rebuttal_end", revision: "revision", camera_ready: "camera_ready" }[kind.value];
    const d = match && dates.filter((x) => x.kind === match).pop();
    if (d && !dateSel.value) { dateSel.value = d.id; syncDate(); }
  };
  const syncDate = () => {
    atDeadline.disabled = !dateSel.value;
    if (!dateSel.value) atNow.checked = true;
  };
  kind.addEventListener("change", syncKind);
  dateSel.addEventListener("change", syncDate);
  syncKind();

  const save = h("button", { type: "submit", class: "btn btn-primary", text: "Save version" });
  const form = h("form", { class: "box mb-4" },
    h("div", { class: "box-header box-header-sm" }, h("h3", { class: "box-title", text: "New version" })),
    h("div", { class: "box-body" },
      h("div", { class: "form-row" },
        field("Kind", kind),
        field("Label", label, { optional: true })),
      field("For the date", dateSel, { optional: true }),
      h("fieldset", { class: "form-group" },
        h("legend", { text: "Which state should it keep?" }),
        h("label", { class: "choice" }, atNow, h("div", {}, h("strong", { text: "Now" }), h("span", { text: "A fresh snapshot of the live files." }))),
        h("label", { class: "choice" }, atDeadline, h("div", {}, h("strong", { text: "At the date's deadline" }),
          h("span", { text: "The last snapshot at or before the deadline, for a version saved after the fact." })))),
      field("Note", note, { optional: true }),
      field("PDF", pdf, { optional: true, hint: "The PDF you handed in, exactly as uploaded." }),
      error,
      h("div", { class: "form-actions" }, save, h("button", { type: "button", class: "btn", text: "Cancel", onclick: close }))),
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.hidden = true;
    if (kind.value === "other" && !label.value.trim()) {
      error.textContent = "A version of kind Other needs a label.";
      error.hidden = false;
      label.focus();
      return;
    }
    const body = { kind: kind.value, at: atDeadline.checked ? "deadline" : "now" };
    if (label.value.trim()) body.label = label.value.trim();
    if (note.value.trim()) body.note = note.value.trim();
    if (dateSel.value) body.date_id = dateSel.value;
    try {
      const v = await busy(save, () => api.post(projectUrl(ctx.ref, "/versions"), body));
      const file = pdf.files[0];
      if (file && v?.id) {
        try {
          await busy(save, () => uploadPdf(ctx.ref, v.id, file));
        } catch (err) {
          flash(`The version was saved, but the PDF could not be attached. ${err.message}`, "warn");
        }
      }
      flash(`Saved the version ${v?.label || ""} (tag ${v?.tag || "?"}).`, "success");
      navigate(`${ctx.base}/versions`, { replace: true });
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    }
  });
  return form;
}

function pdfUrl(ctx, v) {
  return projectUrl(ctx.ref, `/versions/${enc(v.id)}/pdf`);
}

function versionCard(v, latest, ctx) {
  const creator = v.created_by;
  const zip = `${projectUrl(ctx.ref, "/zip")}?ref=${enc(v.tag)}`;
  const noteHost = h("div", { class: "release-note" });
  const paintNote = () => {
    clear(noteHost);
    if (v.note && v.note.trim()) noteHost.append(h("div", { class: "markdown", html: renderMarkdown(v.note) }));
    else noteHost.append(h("p", { class: "color-muted mb-0", text: "No note." }));
  };
  paintNote();
  const title = h("h2", { text: v.label });

  const actions = h("div", { class: "actions" });
  if (ctx.canEdit) {
    const edit = h("button", { type: "button", class: "btn-octicon", "aria-label": `Edit ${v.label}`, title: "Edit label and note" }, icon("pencil"));
    edit.addEventListener("click", () => editVersion(v, ctx, { title, noteHost, paintNote, edit }));
    actions.append(edit);
  }
  if (ctx.isOwner) {
    const del = h("button", { type: "button", class: "btn-octicon danger", "aria-label": `Delete ${v.label}`, title: "Delete version" }, icon("trash"));
    del.addEventListener("click", async () => {
      const ok = await confirmDialog({
        title: `Delete ${v.label}?`,
        message: `The tag ${v.tag} goes. The commit stays in the history, so the files are not lost.`,
        confirmText: "Delete version",
        danger: true,
      });
      if (!ok) return;
      try {
        await api.del(projectUrl(ctx.ref, `/versions/${enc(v.id)}`));
        flash(`Deleted the version ${v.label}.`, "success");
        ctx.reload();
      } catch (err) {
        flashError(err);
      }
    });
    actions.append(del);
  }

  const assets = h("div", { class: "box" });
  if (v.pdf_name) {
    assets.append(h("div", { class: "asset-row" }, icon("pdf"),
      h("a", { href: pdfUrl(ctx, v), target: "_blank", rel: "noopener", text: v.pdf_name }),
      h("span", { class: "size", text: "Handed-in PDF" })));
  }
  const preview = h("div", { class: "release-pdf", hidden: true });
  assets.append(compiledRow(v, ctx, preview));
  assets.append(h("div", { class: "asset-row" }, icon("zip"),
    h("a", { href: zip, download: `${ctx.project.name}-${v.tag}.zip`, text: "Source code (zip)" })));
  if (ctx.canEdit) {
    const input = h("input", { type: "file", accept: "application/pdf,.pdf", hidden: true });
    const attach = h("button", { type: "button", class: "btn-link text-small" }, icon("upload", { size: 14 }), v.pdf_name ? " Replace the PDF" : " Attach the handed-in PDF");
    attach.addEventListener("click", () => input.click());
    input.addEventListener("change", async () => {
      const file = input.files[0];
      if (!file) return;
      try {
        await busy(attach, () => uploadPdf(ctx.ref, v.id, file));
        flash(`Attached ${file.name} to ${v.label}.`, "success");
        ctx.reload();
      } catch (err) {
        flashError(err);
      }
    });
    assets.append(h("div", { class: "asset-row" }, attach, input));
  }

  const compareTargets = refOptions(ctx.project).filter((o) => o.ref !== v.tag);
  const compare = dropdown({
    summary: [icon("arrow-switch"), "Compare", icon("triangle-down")],
    summaryClass: "btn btn-sm",
    label: `Compare ${v.label} with`,
    items: [
      { header: `Compare ${v.tag} with` },
      ...compareTargets.map((o) => ({ text: `${o.text} · ${o.hint}`, href: `${ctx.base}/compare?from=${enc(v.tag)}&to=${enc(o.ref)}` })),
    ],
  });

  return h("section", { class: "release", id: `v-${v.tag}`, "aria-label": v.label },
    h("div", { class: "release-meta" },
      h("div", { class: "when", title: formatInMyZone(v.created_at), text: formatDay(v.created_at) }),
      creator ? h("div", { class: "meta-line" }, avatar(creator), h("span", { class: "truncate", text: personName(creator) })) : null,
      h("div", { class: "meta-line" }, h("a", { class: "tag-pill", href: `${ctx.base}/files?ref=${enc(v.tag)}`, title: "Browse the files of this version" }, icon("tag", { size: 12 }), v.tag)),
      v.commit ? h("div", { class: "meta-line" }, icon("commit", { size: 14 }),
        h("a", { class: "sha", href: `${ctx.base}/commit/${enc(v.commit)}`, text: v.commit.slice(0, 7) })) : null),
    h("div", { class: "box release-card" },
      h("div", { class: "box-body" },
        h("div", { class: "release-head" },
          title,
          (v.label || "").toLowerCase().startsWith((KIND_TEXT[v.kind] || v.kind).toLowerCase()) ? null
            : h("span", { class: "Label", text: KIND_TEXT[v.kind] || v.kind }),
          latest ? h("span", { class: "Label label-success", text: "Latest" }) : null,
          actions),
        h("div", { class: "text-small color-muted mt-2" },
          `Version ${v.number} · saved `, timeAgo(v.created_at), creator ? ` by ${personName(creator)}` : ""),
        noteHost,
        h("details", { class: "assets", open: true },
          h("summary", {}, icon("chevron-down"), "Assets ", counter(1 + (v.pdf_name ? 1 : 0) + (v.compiled?.pdf_url ? 1 : 0))),
          assets),
        preview,
        h("div", { class: "release-actions" },
          h("a", { class: "btn btn-sm", href: `${ctx.base}/files?ref=${enc(v.tag)}` }, icon("code"), "Browse files"),
          h("a", { class: "btn btn-sm", href: zip, download: `${ctx.project.name}-${v.tag}.zip` }, icon("download"), "Download ZIP"),
          compare))),
  );
}

const COMPILED = {
  success: ["Compiled", "label-success"],
  errors: ["Compiled with errors", "label-attention"],
  failure: ["Did not compile", "label-danger"],
  timeout: ["Timed out", "label-danger"],
};
const FAILED = new Set(["failure", "timeout"]);

// The PDF the server compiled from the version's commit, with a preview in the page.
function compiledRow(v, ctx, preview) {
  const c = v.compiled;
  const label = c ? COMPILED[c.status] || ["Compiling…", "label-secondary"] : null;
  const row = h("div", { class: "asset-row" }, icon("pdf"));
  if (c?.pdf_url) {
    const show = h("button", { type: "button", class: "btn btn-sm", "aria-expanded": "false" }, icon("eye"), "Preview");
    let view = null;
    show.addEventListener("click", () => {
      const open = preview.hidden;
      preview.hidden = !open;
      show.setAttribute("aria-expanded", String(open));
      if (open && !view) {
        view = pdfBox(preview, {
          title: `${v.label}, compiled from ${v.tag}`, url: c.pdf_url, name: `${ctx.project.name}-${v.tag}.pdf`, pageLimit: ctx.project.page_limit,
        });
        onLeave(() => view.destroy());
      }
    });
    row.append(h("a", { href: c.pdf_url, target: "_blank", rel: "noopener", text: "Compiled PDF" }),
      h("span", { class: `Label ${label[1]}`, text: label[0] }), h("span", { class: "flex-1" }), show);
  } else {
    row.append(h("span", { class: "color-muted", text: "Compiled PDF" }),
      label ? h("span", { class: `Label ${label[1]}`, text: label[0] }) : h("span", { class: "text-small color-muted", text: "not compiled yet" }),
      c && FAILED.has(c.status) ? h("a", { class: "text-small", href: projectUrl(ctx.ref, `/compiles/${enc(c.id)}/log`), target: "_blank", rel: "noopener", text: "Log" }) : null);
  }
  return row;
}

// A box with a PDF in the viewer, its zoom, page count and a download link.
// With the paper's `pageLimit` a PDF that is too long gets a warning (pagelimit.js).
function pdfBox(host, { title, url, name, pageLimit = "" }) {
  const zoom = h("span", { class: "pdf-zoom-text", text: "" });
  const pages = h("span", { class: "pdf-pages" });
  const view = { current: null };
  const header = h("div", { class: "box-header box-header-sm" },
    icon("pdf", { cls: "color-muted" }),
    h("strong", { class: "flex-1 truncate", text: title }),
    h("button", { type: "button", class: "btn-octicon pdf-zoom-step", "aria-label": "Zoom out", text: "−", onclick: () => view.current.zoomOut() }),
    zoom,
    h("button", { type: "button", class: "btn-octicon pdf-zoom-step", "aria-label": "Zoom in", text: "+", onclick: () => view.current.zoomIn() }),
    h("button", { type: "button", class: "btn btn-sm", text: "Fit", onclick: () => view.current.setZoom("width") }),
    pages,
    h("a", { class: "btn-octicon", href: url, download: name, "aria-label": "Download the PDF", title: "Download the PDF" }, icon("download")));
  const body = h("div", { class: "pdf-box-body" });
  const overLimit = h("div", { class: "pdf-limit", hidden: true }, icon("alert", { size: 14 }), h("span"));
  clear(host).append(h("div", { class: "box pdf-box" }, header, overLimit, body));
  view.current = createPdfView(body, {
    onChange: ({ page, pages: n, zoom: z }) => {
      zoom.textContent = `${Math.round(z * 100)}%`;
      pages.textContent = n ? `${page} / ${n}` : "";
    },
  });
  view.current.load(url).then(() => checkView(view.current, pageLimit).then((result) => {
    overLimit.hidden = !result?.over;
    overLimit.lastChild.textContent = result?.message || "";
    pages.classList.toggle("is-over", Boolean(result?.over));
    pages.title = result?.summary || "";
  }, (err) => console.warn("The PDF's pages could not be checked", err)), (err) => {
    console.error(err);
    clear(body).append(blankslate({ iconName: "alert", title: "The PDF could not be shown", text: err.message || String(err) }));
  });
  return view.current;
}

function editVersion(v, ctx, { title, noteHost, paintNote, edit }) {
  const label = textInput({ value: v.label, maxlength: "200", "aria-label": "Label" });
  const note = h("textarea", { class: "form-control input-block", rows: "4", "aria-label": "Note" });
  note.value = v.note || "";
  const save = h("button", { type: "submit", class: "btn btn-primary btn-sm", text: "Save" });
  const form = h("form", { class: "mt-2" },
    field("Label", label),
    field("Note", note, { optional: true }),
    h("div", { class: "form-actions" }, save,
      h("button", { type: "button", class: "btn btn-sm", text: "Cancel", onclick: () => { paintNote(); edit.hidden = false; } })));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const updated = await busy(save, () => api.patch(projectUrl(ctx.ref, `/versions/${enc(v.id)}`), { label: label.value.trim(), note: note.value }));
      Object.assign(v, updated || { label: label.value.trim(), note: note.value });
      title.textContent = v.label;
      paintNote();
      edit.hidden = false;
      flash("Version saved.", "success");
    } catch (err) {
      flashError(err);
    }
  });
  edit.hidden = true;
  clear(noteHost).append(form);
  label.focus();
}

// --- Compare ----------------------------------------------------------------

async function renderCompare(ctx) {
  const p = ctx.project;
  setTitle("Compare", p.name);
  const versions = [...(p.versions || [])].sort((a, b) => (b.number || 0) - (a.number || 0));
  const from = ctx.query.get("from") || versions[0]?.tag || "HEAD";
  const to = ctx.query.get("to") || "live";
  const word = ctx.query.get("word") === "1";
  const pdf = ctx.query.get("pdf") === "1";

  const opts = refOptions(p);
  const optionList = (current) => {
    const list = [...opts];
    if (!list.some((o) => o.ref === current)) list.push({ ref: current, text: refName(current), hint: "commit" });
    return list.map((o) => h("option", { value: o.ref }, `${o.text} · ${o.hint}`));
  };
  const fromSel = h("select", { class: "form-select", "aria-label": "Base", value: from }, optionList(from));
  const toSel = h("select", { class: "form-select", "aria-label": "Compare", value: to }, optionList(to));
  const go = (w = word, asPdf = pdf) => navigate(
    `${ctx.base}/compare?from=${enc(fromSel.value)}&to=${enc(toSel.value)}${w ? "&word=1" : ""}${asPdf ? "&pdf=1" : ""}`,
    { replace: true, keepScroll: true });
  fromSel.addEventListener("change", () => go());
  toSel.addEventListener("change", () => go());

  const lines = h("button", { type: "button", "aria-pressed": String(!word && !pdf), text: "Lines", onclick: () => go(false, false) });
  const words = h("button", { type: "button", "aria-pressed": String(word && !pdf), text: "Words", onclick: () => go(true, false) });
  const pdfButton = h("button", { type: "button", class: "btn btn-sm", "aria-pressed": String(pdf), title: "Compile both states with latexdiff marks" },
    icon("pdf"), "PDF diff");
  pdfButton.addEventListener("click", () => go(word, !pdf));
  const swap = h("button", { type: "button", class: "btn-octicon", "aria-label": "Swap base and compare", title: "Swap" }, icon("arrow-switch"));
  swap.addEventListener("click", () => {
    const a = fromSel.value;
    fromSel.value = toSel.value;
    toSel.value = a;
    go();
  });

  const head = h("div", { class: "subhead" },
    h("h2", { text: "Compare" }),
    h("a", { class: "btn btn-sm", href: `${ctx.base}/versions` }, icon("tag"), "All versions"));
  const bar = h("div", { class: "range-bar" },
    h("label", { class: "text-small color-muted", text: "base" }), fromSel,
    swap,
    h("label", { class: "text-small color-muted", text: "compare" }), toSel,
    h("span", { class: "flex-1" }),
    h("div", { class: "segmented", role: "group", "aria-label": "Diff mode" }, lines, words),
    pdfButton);
  const hint = h("p", { class: "text-small color-muted", text: pdf
    ? "latexdiff marks what was added and removed, and the result is compiled. Big papers take a while."
    : word
      ? "Changed words are marked inside each line, which suits LaTeX paragraphs."
      : "Whole lines are compared. Switch to Words for long LaTeX paragraphs." });
  const host = h("div", {}, loading());
  clear(ctx.main).append(head, bar, hint, host);

  if (from === to) {
    clear(host).append(h("div", { class: "box" }, blankslate({ iconName: "arrow-switch", title: "Pick two different states", text: "Choose a base and a state to compare it with." })));
    return;
  }
  if (pdf) {
    pdfDiff(ctx, host, from, to);
    return;
  }
  // Not awaited, so the page shows its controls and a spinner while git works.
  api.get(projectUrl(ctx.ref, "/diff"), { query: { from, to, word: word ? "true" : "false" } })
    .then((diff) => {
      if (ctx.alive()) clear(host).append(renderDiff(diff, { word }));
    })
    .catch((err) => {
      if (!ctx.alive()) return;
      clear(host).append(h("div", { class: "box" }, blankslate({ iconName: "alert", title: "Could not compare", text: err.message })));
    });
}

// The latexdiff PDF of two states, compiled by the server (docs/DESIGN.md §14).
async function pdfDiff(ctx, host, from, to) {
  clear(host).append(h("div", { class: "box" }, h("div", { class: "blankslate" },
    spinner(true), h("h3", { text: "Making the PDF diff" }),
    h("p", { text: `latexdiff compares ${refName(from)} with ${refName(to)}, and the result is compiled.` }))));
  let c;
  try {
    c = await api.post(projectUrl(ctx.ref, "/pdfdiff"), { from, to });
    while (c && !["success", "errors", "failure", "timeout"].includes(c.status)) {
      await new Promise((r) => setTimeout(r, 1000));
      if (!ctx.alive()) return;
      c = await api.get(projectUrl(ctx.ref, `/compiles/${enc(c.id)}`));
    }
  } catch (err) {
    if (!ctx.alive()) return;
    clear(host).append(h("div", { class: "box" }, blankslate({
      iconName: err.status === 503 ? "info" : "alert",
      title: err.status === 503 ? "No compiling here" : "The PDF diff could not be made",
      text: err.status === 503 ? "This server has no compile service. Ask whoever runs it to set COMPILE_URL." : err.message,
    })));
    return;
  }
  if (!ctx.alive()) return;
  const problems = (c.problems || []).filter((p) => p.level === "error");
  const note = c.status === "errors"
    ? h("p", { class: "text-small color-attention", text: `LaTeX reported ${problems.length || "some"} ${problems.length === 1 ? "error" : "errors"}, so parts of the PDF may be off.` })
    : null;
  if (!c.pdf_url) {
    clear(host).append(h("div", { class: "box" }, blankslate({
      iconName: "alert",
      title: c.status === "timeout" ? "The PDF diff took too long" : "latexdiff made no PDF",
      text: problems.length ? problems.slice(0, 3).map((p) => `${p.path ? `${p.path}:${p.line} ` : ""}${p.message}`).join(" ") : "The log says why.",
      action: c.log_url ? h("a", { class: "btn", href: c.log_url, target: "_blank", rel: "noopener" }, "Open the log") : null,
    })));
    return;
  }
  const box = h("div");
  clear(host).append(note || "", box);
  const view = pdfBox(box, { title: `PDF diff of ${refName(from)} and ${refName(to)}`, url: c.pdf_url, name: `${ctx.project.name}-${refName(from)}-${refName(to)}-diff.pdf` });
  onLeave(() => view.destroy());
}
