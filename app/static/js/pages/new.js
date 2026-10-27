// /new creates a paper or a plan. It is laid out like GitHub's "Create a new
// repository". A plan is a venue you want to hand something in to before the
// topic is clear (DESIGN §4, Planned papers), so it needs no title and no
// files. The name it will get is shown live (a debounced dry run), then come
// the venue, the dates from the CFP, the folder and labels.

import { api, enc } from "../api.js";
import { h, clear, debounce, formatBytes, nextId, plural } from "../dom.js";
import { icon } from "../icons.js";
import { navigate, setQuery } from "../router.js";
import { dateList } from "../dates.js";
import { busy, commandLine, field, flashBox, queueFlash, setTitle, textInput } from "../ui.js";
import { labelEditor, venueFields } from "../forms.js";
import { editorHref } from "./editor.js";

// The server takes a request body of at most 50 MB (MAX_BLOB_BYTES).
export const MAX_ZIP_BYTES = 50 * 1024 * 1024;

// What the import did, in two short lines for the flash after creating.
export function importSummary(result, zipName) {
  const others = result.main_candidates.filter((c) => c !== result.main_file);
  return h("p", {},
    `Imported ${plural(result.files.length, "file")} from ${zipName}`,
    result.skipped.length ? ` and left out ${plural(result.skipped.length, "file")} that ${result.skipped.length === 1 ? "is" : "are"} not part of a template` : "",
    ". ",
    result.main_file ? ["The main file is ", h("code", { text: result.main_file }), "."] : "No file looks like the paper itself. Set the main file in Settings.",
    others.length ? [" Other papers in the kit: ", others.slice(0, 3).join(", "), others.length > 3 ? " and more" : "", ". Change the main file in Settings if you want one of those."] : "");
}

// "Start with" offers an empty start, the basic starter or a publisher's template zip. Used
// here and by "Start the paper" on a plan's overview.
export function starterChoice() {
  const name = nextId("starter");
  const option = (value, title, text, checked = false) => h("label", { class: "choice" },
    h("input", { type: "radio", name, value, checked }),
    h("div", {}, h("strong", { text: title }), h("span", { text })));
  const el = h("div", {},
    option("empty", "Empty", "Start without files, for example to link an existing folder with meertex link.", true),
    option("basic", "Basic", "A main.tex and a refs.bib to start writing."),
    option("zip", "A publisher's template", "The zip of an author kit, such as ACM's acmart or IEEE's conference template. Its files become the paper's files."));
  // Choosing a file picks the template option, so nobody uploads a zip and
  // then creates an empty paper by mistake.
  const zipInput = h("input", { type: "file", accept: ".zip,application/zip", class: "form-control mt-2", "aria-label": "Template zip" });
  zipInput.addEventListener("change", () => {
    if (zipInput.files.length) el.querySelector("input[value=zip]").checked = true;
  });
  el.lastElementChild.querySelector("div").append(zipInput);

  const value = () => el.querySelector(`input[name="${name}"]:checked`)?.value || "empty";
  // The reason the choice cannot be used, or null.
  const problem = () => {
    if (value() !== "zip") return null;
    const zip = zipInput.files[0];
    if (!zip) return "Choose the template's zip file, or start empty.";
    if (zip.size > MAX_ZIP_BYTES) return `The zip is ${formatBytes(zip.size)}. A template zip can be at most ${formatBytes(MAX_ZIP_BYTES)}.`;
    return null;
  };
  return {
    el,
    zipInput,
    value,
    problem,
    // What the server's `starter` takes. A zip is imported after creating.
    starter: () => (value() === "basic" ? "basic" : "empty"),
    zip: () => (value() === "zip" ? zipInput.files[0] || null : null),
  };
}

export async function render(ctx) {
  const folders = await api.get("/api/folders");
  if (!ctx.alive()) return;

  const startFolder = ctx.query.get("folder") || "";
  const title = textInput({ placeholder: "Green Large Language Models", autocomplete: "off" });
  const conference = textInput({ placeholder: "ICSE", autocomplete: "off" });
  const year = textInput({ type: "number", min: "1990", max: "2100", value: String(new Date().getFullYear() + 1), inputmode: "numeric" });
  const paper = textInput({ placeholder: "derived from the title", autocomplete: "off", pattern: "[A-Za-z0-9._\\-]*" });
  const venue = venueFields({}, { split: true });
  const dates = dateList([{ kind: "abstract" }, { kind: "submission" }]);
  const labels = labelEditor([]);
  labels.input.id = "new-labels";
  const notes = h("textarea", { class: "form-control input-block", rows: "4", placeholder: "Anything else from the CFP: formatting rules, artifact track, open questions." });
  const folder = h("select", { class: "form-select input-block", value: folders.find((f) => f.path.toLowerCase() === startFolder.toLowerCase())?.id || "" },
    h("option", { value: "" }, "Top level"),
    folders.map((f) => h("option", { value: f.id }, f.path)));
  const starter = starterChoice();

  // A paper or a plan. /new?plan=1 opens with the plan chosen.
  const kindOption = (value, titleText, text, checked) => h("label", { class: "choice" },
    h("input", { type: "radio", name: "new-kind", value, checked }),
    h("div", {}, h("strong", { text: titleText }), h("span", { text })));
  const planFirst = ctx.query.get("plan") === "1";
  const kind = h("fieldset", { class: "form-group" },
    h("legend", { class: "sr-only", text: "What do you want to create?" }),
    h("div", { class: "kind-choice" },
      kindOption("paper", "A paper", "You know what you will write. It gets a title and its first files.", !planFirst),
      kindOption("plan", "A plan: I know the venue, not the paper yet", "Keep the venue and its dates on the timeline. Start the paper when the topic is clear.", planFirst)));
  const isPlan = () => kind.querySelector("input:checked")?.value === "plan";

  // The name comes from the dry run, and can be overridden.
  const nameOut = h("span", { class: "name", text: "…" });
  const nameStatus = h("span", { class: "text-small color-muted" });
  const nameIntro = h("span", { class: "color-muted" });
  const nameOverride = textInput({ placeholder: "Leave empty to use your naming pattern", pattern: "[A-Za-z0-9][A-Za-z0-9._\\-]*", spellcheck: "false" });
  const overrideGroup = field("Name", nameOverride, {
    hint: "Letters, digits, dot, dash and underscore. It becomes the folder name of every checkout.",
  });
  overrideGroup.hidden = true;
  const changeName = h("button", { type: "button", class: "btn-link text-small", text: "Choose a name yourself" });
  changeName.addEventListener("click", () => {
    overrideGroup.hidden = !overrideGroup.hidden;
    changeName.textContent = overrideGroup.hidden ? "Choose a name yourself" : "Use the naming pattern";
    if (overrideGroup.hidden) nameOverride.value = "";
    else nameOverride.focus();
    preview();
  });

  const required = h("p", { class: "text-small color-muted" }, "Required fields are marked with an asterisk (*).");
  const heading = h("h1");
  const lede = h("p", { class: "page-lede" });
  const titleGroup = field("Title", title);
  const titleLabel = titleGroup.querySelector("label");
  const titleHint = h("div", { class: "form-hint" });
  titleGroup.append(titleHint);
  const paperGroup = field("Short name", paper, { optional: true, hint: "The {paper} in your naming pattern." });
  const starterGroup = h("fieldset", { class: "form-group" }, h("legend", { text: "Start with" }), starter.el);

  const error = h("div");
  const submit = h("button", { type: "submit", class: "btn btn-primary" });

  function paintKind() {
    const plan = isPlan();
    setTitle(plan ? "New plan" : "New paper");
    heading.textContent = plan ? "Plan a paper for a venue" : "Create a new paper";
    lede.textContent = plan
      ? "A plan holds a venue and its dates before the paper's topic is clear. It shows on the timeline, and you start the paper from it later."
      : "A paper holds its LaTeX files, its venue and deadlines, and the versions you hand in.";
    titleLabel.replaceChildren(plan ? "Working title" : "Title",
      plan ? h("span", { class: "optional", text: " (optional)" }) : h("span", { class: "color-danger", text: " *" }));
    title.required = !plan;
    title.placeholder = plan ? "Leave it empty until the topic is clear" : "Green Large Language Models";
    titleHint.textContent = plan ? "You give the paper its title when you start it." : "";
    titleHint.hidden = !plan;
    paperGroup.hidden = plan;
    required.hidden = plan;
    starterGroup.hidden = plan;
    nameIntro.textContent = plan ? "Your plan will be called" : "Your paper will be called";
    submit.textContent = plan ? "Create plan" : "Create paper";
    title.classList.remove("is-invalid");
    clear(error);
  }
  kind.addEventListener("change", () => {
    paintKind();
    setQuery({ plan: isPlan() ? "1" : null });
    preview();
  });

  function body(dryRun) {
    const plan = isPlan();
    const out = {
      title: title.value.trim(),
      conference: conference.value.trim(),
      paper: plan ? "" : paper.value.trim(),
      ...venue.values(),
      notes: notes.value,
      labels: labels.values(),
      folder: folder.value || null,
      starter: plan ? "empty" : starter.starter(),
    };
    if (plan) out.planned = true;
    if (year.value) out.year = Number(year.value);
    if (nameOverride.value.trim()) out.name = nameOverride.value.trim();
    if (dryRun) out.dry_run = true;
    return out;
  }

  let previewToken = 0;
  const preview = debounce(async () => {
    const mine = ++previewToken;
    const plan = isPlan();
    const enough = plan
      ? title.value.trim() || conference.value.trim() || nameOverride.value.trim()
      : title.value.trim() || paper.value.trim();
    if (!enough) {
      nameOut.textContent = "…";
      nameStatus.textContent = plan ? "Type the conference to see the name." : "Type a title to see the name.";
      return;
    }
    nameStatus.textContent = "";
    try {
      const res = await api.post("/api/projects", { ...body(true), dates: [] });
      if (mine !== previewToken) return;
      nameOut.textContent = res?.name || "…";
    } catch (err) {
      if (mine !== previewToken) return;
      nameOut.textContent = "…";
      nameStatus.textContent = err.message;
    }
  }, 300);
  [title, conference, year, paper, nameOverride, venue.inputs.track].forEach((el) => el.addEventListener("input", preview));

  const form = h("form", { class: "new-form", novalidate: true },
    h("div", { class: "page-head" }, h("div", {}, heading, lede)),
    kind,
    required,

    titleGroup,
    h("div", { class: "form-row" },
      field("Conference", conference, { hint: "The acronym as the venue writes it." }),
      field("Year", year, { hint: "The year of the conference." }),
      paperGroup),

    h("div", { class: "form-group" },
      h("div", { class: "name-preview", "aria-live": "polite" },
        icon("book", { cls: "color-muted" }),
        nameIntro,
        nameOut,
        h("span", { class: "flex-1" }),
        changeName),
      h("div", { class: "mt-2" }, nameStatus)),
    overrideGroup,

    h("hr"),
    h("h2", { class: "mb-2", style: { fontSize: "16px" }, text: "Dates" }),
    h("p", { class: "color-muted text-small", text: "Leave the time empty when the CFP only gives a day. Most CFPs use AoE (Anywhere on Earth)." }),
    dates.el,

    h("hr"),
    h("h2", { class: "mb-2", style: { fontSize: "16px" }, text: "Venue" }),
    venue.main,
    h("details", { class: "form-group" },
      h("summary", { class: "text-bold", style: { cursor: "pointer" }, text: "More about the venue" }),
      h("div", { class: "mt-3" }, venue.more)),

    h("hr"),
    h("div", { class: "form-row" },
      field("Folder", folder, { hint: "Where it sits in your own tree." }),
      h("div", { class: "form-group" }, h("label", { class: "form-label", for: labels.input.id, text: "Labels" }), labels.el,
        h("div", { class: "form-hint", text: "Press Enter or comma after each label." }))),
    field("Notes", notes, { optional: true, hint: "Markdown. Shown on the paper's overview." }),
    starterGroup,

    h("hr"),
    error,
    h("div", { class: "form-actions" }, submit),
  );

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    clear(error);
    const plan = isPlan();
    if (!plan && !title.value.trim()) {
      title.classList.add("is-invalid");
      title.focus();
      error.append(flashBox("A paper needs a title. Choose a plan if the topic is not clear yet.", "error"), h("div", { class: "mb-3" }));
      return;
    }
    title.classList.remove("is-invalid");
    const ds = dates.values();
    if (ds === null) return;
    const zipProblem = plan ? null : starter.problem();
    if (zipProblem) {
      error.append(flashBox(zipProblem, "error"), h("div", { class: "mb-3" }));
      starter.zipInput.focus();
      return;
    }
    const zip = plan ? null : starter.zip();
    try {
      const { project, imported } = await busy(submit, async () => {
        const made = await api.post("/api/projects", { ...body(false), dates: ds });
        if (!zip) return { project: made, imported: null };
        try {
          const result = await api.post(`/api/projects/${enc(made.id)}/import`, zip, { contentType: "application/zip" });
          return { project: made, imported: result };
        } catch (err) {
          // A paper without its template is not what was asked for. It was
          // made a moment ago and holds nothing else, so it goes again.
          await api.del(`/api/projects/${enc(made.id)}`, { query: { confirm: made.name } }).catch(() => {});
          throw new Error(`The template could not be imported: ${err.message.replace(/\.?$/, ".")} Nothing was created.`);
        }
      });
      const base = `/p/${enc(project.name)}`;
      if (plan) {
        queueFlash(h("p", {}, "Created the plan ", h("strong", { text: project.name }), ". It is on the ",
          h("a", { href: "/timeline", text: "timeline" }), " now. Start the paper here when the topic is clear."), "success");
      } else {
        queueFlash(h("div", {},
          imported ? importSummary(imported, zip.name) : null,
          h("p", {}, "Created ", h("strong", { text: project.name }), ". ",
            h("a", { href: editorHref(base), text: "Write in the browser" }),
            ", or check it out on your computer:"),
          commandLine(`meertex checkout ${project.name}`)), "success");
      }
      navigate(base);
    } catch (err) {
      error.append(flashBox(err.message, "error"), h("div", { class: "mb-3" }));
      error.scrollIntoView({ block: "center" });
    }
  });

  paintKind();
  clear(ctx.main).append(h("div", { class: "container container-form page" }, form));
  (isPlan() ? conference : title).focus();
  preview();
}
