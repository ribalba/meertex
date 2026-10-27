// Answering the questions a passed date asks (DESIGN §6), from the home
// page's "Waiting for you" box and from the banners on a project.
//
// "It moved" opens a small inline form for the new date. "Yes" on a hand-in
// (submission, rebuttal, revision, camera-ready) creates a version, so it asks
// which state to freeze and offers to attach the PDF that was handed in.

import { api, enc, projectUrl } from "./api.js";
import { h } from "./dom.js";
import { icon } from "./icons.js";
import { dateRow, dateLabel } from "./dates.js";
import { formatDue, formatInMyZone } from "./time.js";
import { busy, confirmDialog, field, flash, flashError, openDialog } from "./ui.js";

export const ANSWER_TEXT = {
  yes: "Yes",
  no: "No",
  moved: "It moved",
  accepted: "Accepted",
  rejected: "Rejected",
  revision: "Revision",
};

const VERSION_FOR = { submission: "submission", rebuttal_end: "rebuttal", revision: "revision", camera_ready: "camera_ready" };

const YES_TITLE = {
  submission: "You handed in the paper",
  rebuttal_end: "You sent a rebuttal",
  revision: "You handed in the revision",
  camera_ready: "You handed in the camera-ready version",
};

export function uploadPdf(projectId, versionId, file) {
  return api.put(projectUrl(projectId, `/versions/${enc(versionId)}/pdf`), file, {
    query: { name: file.name },
    contentType: file.type || "application/pdf",
  });
}

function answer(q, body) {
  return api.post(projectUrl(q.project_id, `/dates/${enc(q.date_id)}/answer`), body);
}

// The buttons for one question plus room for the inline "moved" form.
// `date` is the full date object when the caller has it (the project page),
// which lets the forms show the venue's time zone.
export function questionControls(q, { onAnswered, date = null }) {
  const actions = h("div", { class: "question-actions" });
  const formHost = h("div");
  const el = h("div", {}, actions, formHost);

  const done = async (project, given) => {
    await onAnswered?.(project, given);
    if (q.kind === "notification" && given === "revision") {
      addDateDialog(q.project_id, {
        kind: "revision",
        title: "When is the revision due?",
        text: "The outcome is now Revision. Add the deadline for the revised paper so meertex can count down to it.",
        onDone: onAnswered,
      });
    }
    if (q.kind === "revision" && given === "yes") {
      addDateDialog(q.project_id, {
        kind: "notification",
        title: "When is the next notification?",
        text: "The paper is under review again. Add the date the venue will decide.",
        onDone: onAnswered,
      });
    }
  };

  for (const a of q.answers || []) {
    const primary = a === "yes" || a === "accepted";
    const btn = h("button", { type: "button", class: `btn btn-sm${primary ? " btn-primary" : ""}` },
      a === "moved" ? icon("calendar") : null,
      ANSWER_TEXT[a] || a);
    btn.addEventListener("click", () => handle(a, btn));
    actions.append(btn);
  }

  async function handle(a, btn) {
    if (a === "moved") {
      toggleMoveForm();
      return;
    }
    if (a === "yes" && VERSION_FOR[q.kind]) {
      versionDialog(q, { date, onSaved: (project) => done(project, "yes") });
      return;
    }
    if (a === "no" && (q.kind === "submission" || q.kind === "abstract")) {
      const ok = await confirmDialog({
        title: q.kind === "submission" ? "Not handed in?" : "Abstract not registered?",
        message: `This sets the outcome of ${q.project_name} to Not submitted, and meertex stops asking about its later dates. You can change the outcome again in the settings.`,
        confirmText: "Mark as not submitted",
      });
      if (!ok) return;
    }
    try {
      const project = await busy(btn, () => answer(q, { answer: a }));
      flash(`Answered "${ANSWER_TEXT[a] || a}" for ${q.label} of ${q.project_name}.`, "success");
      await done(project, a);
    } catch (err) {
      flashError(err);
    }
  }

  function toggleMoveForm() {
    if (formHost.firstChild) {
      formHost.replaceChildren();
      return;
    }
    const row = dateRow(date ? { ...date, id: null } : { kind: q.kind, tz: "AoE" }, { fixedKind: q.kind });
    // Kind and label do not change when a date moves.
    row.el.querySelector(".date-kind").hidden = true;
    row.el.querySelector(".date-label").hidden = true;
    row.el.classList.add("compact");
    const save = h("button", { type: "submit", class: "btn btn-primary btn-sm" }, "Move date");
    const form = h("form", { class: "inline-form" },
      h("div", { class: "flex-1" },
        h("div", { class: "text-small color-muted mb-2", text: `When is ${q.label} now?` }),
        row.el),
      h("div", { class: "form-actions" },
        save,
        h("button", { type: "button", class: "btn btn-sm", text: "Cancel", onclick: () => formHost.replaceChildren() })),
    );
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const v = row.value();
      if (!v) return;
      const body = { answer: "moved", tz: v.tz };
      if (v.due_at) body.due_at = v.due_at;
      else body.date = v.date;
      try {
        const project = await busy(save, () => answer(q, body));
        flash(`Moved ${q.label} of ${q.project_name}.`, "success");
        formHost.replaceChildren();
        await done(project, "moved");
      } catch (err) {
        row.showError(err.message);
      }
    });
    formHost.append(form);
    row.focus();
  }

  return el;
}

function versionDialog(q, { date, onSaved }) {
  const kind = VERSION_FOR[q.kind];
  const when = date ? formatDue(date) : formatInMyZone(q.due_at);
  const radio = (value, checked) => h("input", { type: "radio", name: "at", value, checked });
  const atDeadline = radio("deadline", true);
  const atNow = radio("now", false);
  const note = h("textarea", { class: "form-control input-block", rows: "3", placeholder: "Anything worth remembering about this version" });
  const pdf = h("input", { type: "file", accept: "application/pdf,.pdf", class: "input-block" });
  const error = h("div", { class: "form-error", role: "alert", hidden: true });

  const body = h("div", {},
    h("p", { text: "meertex keeps the files as a fixed version, so you can always see what was handed in." }),
    h("fieldset", { class: "form-group" },
      h("legend", { text: "Which state should the version keep?" }),
      h("label", { class: "choice" }, atDeadline,
        h("div", {}, h("strong", { text: "The state at the deadline" }),
          h("span", { text: `The last snapshot at or before ${when}. Right when you answer after the fact.` }))),
      h("label", { class: "choice" }, atNow,
        h("div", {}, h("strong", { text: "The current state" }),
          h("span", { text: "A fresh snapshot of the files as they are now." })))),
    field("Note", note, { optional: true }),
    field("The PDF you handed in", pdf, {
      optional: true,
      hint: "Attach it exactly as you uploaded it to the venue. You can also add it later on the Versions tab.",
    }),
    error,
  );
  const save = h("button", { type: "button", class: "btn btn-primary" }, "Save version");
  const d = openDialog({
    title: YES_TITLE[q.kind] || "Save a version",
    body,
    footer: [h("button", { type: "button", class: "btn", text: "Cancel", onclick: () => d.close() }), save],
  });

  save.addEventListener("click", async () => {
    error.hidden = true;
    const file = pdf.files[0] || null;
    if (file && file.size === 0) {
      error.textContent = "That PDF is empty.";
      error.hidden = false;
      return;
    }
    try {
      const at = atNow.checked ? "now" : "deadline";
      const project = await busy(save, () => answer(q, { answer: "yes", version: { at, note: note.value.trim() } }));
      const version = (project?.versions || [])
        .filter((v) => v.date_id === q.date_id || v.kind === kind)
        .sort((a, b) => (b.number || 0) - (a.number || 0))[0];
      if (file) {
        if (!version) {
          flash("The version was saved, but meertex could not find it to attach the PDF. Attach it on the Versions tab.", "warn");
        } else {
          try {
            await busy(save, () => uploadPdf(q.project_id, version.id, file));
          } catch (err) {
            flash(`The version was saved, but the PDF could not be attached. ${err.message}`, "warn");
          }
        }
      }
      d.close();
      flash(version
        ? `Saved the version ${version.label} (tag ${version.tag}) for ${q.project_name}.`
        : `Answered "Yes" for ${q.label} of ${q.project_name}.`, "success");
      await onSaved(project);
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    }
  });
}

// "When is the revision due?" after a decision. Skipping is fine.
export function addDateDialog(projectId, { kind, title, text, onDone }) {
  const row = dateRow({ kind, tz: "AoE" }, { fixedKind: kind });
  const save = h("button", { type: "button", class: "btn btn-primary" }, "Add date");
  const d = openDialog({
    title,
    wide: true,
    body: h("div", {}, h("p", { text }), row.el),
    footer: [h("button", { type: "button", class: "btn", text: "Skip", onclick: () => d.close() }), save],
  });
  save.addEventListener("click", async () => {
    const v = row.value();
    if (!v) return;
    try {
      await busy(save, () => api.post(projectUrl(projectId, "/dates"), v));
      d.close();
      flash(`Added ${dateLabel(v)}.`, "success");
      await onDone?.();
    } catch (err) {
      row.showError(err.message);
    }
  });
  row.focus();
}
