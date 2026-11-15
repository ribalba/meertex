// /new creates a paper or a plan. It is laid out like GitHub's "Create a new
// repository". A plan is a venue you want to hand something in to before the
// topic is clear (DESIGN §4, Planned papers), so it needs no title and no
// files. The name it will get is shown live (a debounced dry run), then come
// the venue, the dates from the CFP, the folder and labels.

import { api, enc, encPath } from "../api.js";
import { h, clear, debounce, formatBytes, nextId, plural } from "../dom.js";
import { icon } from "../icons.js";
import { navigate, onLeave, setQuery } from "../router.js";
import { dateList, fromInput } from "../dates.js";
import { busy, commandLine, field, flashBox, queueFlash, setTitle, spinner, textInput } from "../ui.js";
import { VENUE_KEYS, labelEditor, venueFields } from "../forms.js";
import { editorHref } from "./editor.js";

// The server takes a request body of at most 50 MB (MAX_BLOB_BYTES).
export const MAX_ZIP_BYTES = 50 * 1024 * 1024;

// The venue fields that sit folded under "More about the venue".
const MORE_VENUE_KEYS = ["conference_name", "track", "location", "cfp_url", "conference_url", "publisher"];

// An http or https address as a URL, or null. Only those become links, so a
// javascript: address from the server never does.
function webUrl(raw) {
  try {
    const url = new URL(String(raw ?? ""));
    return url.protocol === "http:" || url.protocol === "https:" ? url : null;
  } catch {
    return null;
  }
}

// Text set in a LaTeX argument. One pass, so the braces of \textbackslash{}
// are not escaped again.
const LATEX_SPECIAL = { "\\": "\\textbackslash{}", "~": "\\textasciitilde{}", "^": "\\textasciicircum{}" };
function latexEscape(text) {
  return text.replace(/[\\&%$#_{}~^]/g, (c) => LATEX_SPECIAL[c] || `\\${c}`);
}

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
// here and by "Start the paper" on a plan's overview. On /new the assistant can
// add the venue's own format with offer().
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

  // What the assistant on /new found (offer() below). The venue's own main.tex
  // becomes a choice above Empty, and the author kit's address goes above the
  // zip input.
  let venueTex = null;
  let venueOption = null;
  let kitLine = null;
  function offer(found, template = "") {
    const { main_tex: tex = null, author_kit_url: kit = null } = found || {};
    const wasVenue = value() === "venue";
    venueOption?.remove();
    kitLine?.remove();
    venueOption = null;
    kitLine = null;
    venueTex = typeof tex === "string" && tex ? tex : null;
    if (venueTex) {
      venueOption = option("venue", "The venue's format",
        `A main.tex set up for ${template || "the venue's class"} and an empty refs.bib, written by Claude from the call for papers.`);
      // A <details> is interactive content, so a click on it opens the file
      // and does not go on to the label's radio.
      venueOption.querySelector("div").append(h("details", { class: "mt-2" },
        h("summary", { class: "text-small", text: "Show main.tex" }),
        h("pre", { class: "starter-preview mt-2", text: venueTex })));
      el.prepend(venueOption);
      // Checked after it is in the group, so the other choices lose the check.
      venueOption.querySelector("input").checked = true;
    } else if (wasVenue) {
      el.querySelector("input[value=empty]").checked = true;
    }
    const kitUrl = webUrl(kit);
    if (kitUrl) {
      kitLine = h("div", { class: "text-small color-muted mt-2" },
        "The venue's author kit is at ",
        h("a", { href: kitUrl.href, target: "_blank", rel: "noopener noreferrer", text: kitUrl.host }),
        ". Download it and choose the zip here to start from its files.");
      zipInput.before(kitLine);
    }
  }
  // The files of the venue's format, with the paper's title in place of
  // \title{Untitled}. Null for any other choice.
  function venueFiles(paperTitle = "") {
    if (value() !== "venue" || !venueTex) return null;
    const t = String(paperTitle || "").trim();
    const tex = t ? venueTex.replace("\\title{Untitled}", () => `\\title{${latexEscape(t)}}`) : venueTex;
    return [["main.tex", tex], ["refs.bib", ""]];
  }
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
    offer,
    venueFiles,
  };
}

// The assistant box fills the form from the address of a call for papers.
// The server reads the pages, which takes a minute or two, so the box shows
// how long it has been running and has a Stop button. `apply(answer)` puts
// the answer into the form.
function assistantBox(ctx, { apply, isPlan }) {
  // The lengths are the server's limits (NewPaperIn in routers/assistant.py).
  const address = textInput({ type: "url", placeholder: "https://", autocomplete: "off", spellcheck: "false", maxlength: "2000" });
  const hint = textInput({ placeholder: "Research track, second cycle", autocomplete: "off", maxlength: "300" });
  const go = h("button", { type: "button", class: "btn" }, icon("sparkle"), "Fill in");
  const status = h("div", { class: "assistant-status", "aria-live": "polite" });
  const headingId = nextId("assistant");

  let running = null;
  // Leaving the page stops the request. Nothing would use its answer.
  onLeave(() => running?.abort());

  const setBusy = (on) => {
    go.disabled = on;
    address.disabled = on;
    hint.disabled = on;
  };
  const invalid = (message) => {
    address.classList.add("is-invalid");
    status.replaceChildren(h("div", { class: "form-error", role: "alert", text: message }));
    address.focus();
  };

  async function run() {
    if (running) return;
    address.classList.remove("is-invalid");
    const raw = address.value.trim();
    if (!raw) { invalid("Paste the address of the call for papers first."); return; }
    // An address pasted without its scheme is read as https, like the server does.
    const url = webUrl(raw.includes("://") ? raw : `https://${raw}`);
    if (!url || !url.hostname.includes(".")) { invalid("That is not a web address. It looks like https://conf.example.org/cfp."); return; }
    const body = { url: url.href };
    if (hint.value.trim()) body.hint = hint.value.trim();

    const controller = new AbortController();
    running = controller;
    const started = Date.now();
    // Hidden from screen readers, or the live region would read out every second.
    const seconds = h("span", { class: "color-muted", "aria-hidden": "true" });
    const tick = () => { seconds.textContent = plural(Math.floor((Date.now() - started) / 1000), "second"); };
    tick();
    const timer = setInterval(tick, 1000);
    setBusy(true);
    status.replaceChildren(h("div", { class: "assistant-running" },
      spinner(),
      h("span", { text: "Reading the call for papers. This takes a minute or two." }),
      seconds,
      h("button", { type: "button", class: "btn btn-sm", text: "Stop", onclick: () => controller.abort() })));

    let stopped = false;
    try {
      const answer = await api.post("/api/assistant/new-paper", body, { signal: controller.signal });
      if (!ctx.alive()) return;
      apply(answer);
      status.replaceChildren(report(answer, url));
    } catch (err) {
      if (!ctx.alive()) return;
      stopped = err?.name === "AbortError";
      if (stopped) status.replaceChildren();
      else status.replaceChildren(flashBox(err.message, "error"));
    } finally {
      clearInterval(timer);
      running = null;
      setBusy(false);
    }
    // The Stop button went with the status line, so the focus goes back.
    if (stopped) address.focus();
  }

  function report(answer, url) {
    const fieldCount = Object.keys(answer?.fields || {}).length;
    const dateCount = (answer?.dates || []).length;
    const remarks = (answer?.remarks || []).filter(Boolean);
    const pages = (answer?.pages || []).filter(Boolean).map((page) => {
      const u = webUrl(page);
      if (!u) return h("span", { text: String(page) });
      const shown = u.host + (u.pathname === "/" ? "" : u.pathname);
      return h("a", { href: u.href, target: "_blank", rel: "noopener noreferrer", text: shown });
    });
    const created = isPlan() ? "plan" : "paper";
    return flashBox(h("div", {},
      h("p", { text: `Filled in ${plural(fieldCount, "field")} and ${plural(dateCount, "date")} from ${url.host}. Check them against the call for papers before you create the ${created}.` }),
      remarks.length ? [h("p", { text: "To check:" }), h("ul", {}, remarks.map((r) => h("li", { text: String(r) })))] : null,
      pages.length ? h("p", { class: "text-small color-muted" }, "Pages read: ", pages.flatMap((a, i) => (i ? [", ", a] : [a]))) : null),
    // Green only when something was filled in.
    fieldCount || dateCount ? "success" : "warn");
  }

  // The box sits in the big form. Enter here asks the assistant and does not
  // create the paper.
  for (const input of [address, hint]) {
    input.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || e.isComposing) return;
      e.preventDefault();
      run();
    });
  }
  address.addEventListener("input", () => address.classList.remove("is-invalid"));
  go.addEventListener("click", run);

  return h("section", { class: "assistant-box", "aria-labelledby": headingId },
    h("h2", { class: "assistant-head", id: headingId }, icon("sparkle"), "Fill in from the call for papers"),
    h("p", { class: "color-muted text-small", text: "Paste the address of the call for papers or of the conference. Claude reads it and fills in the venue, the deadlines and the paper's format. Check what it found before you create." }),
    h("div", { class: "assistant-row" },
      field("Address of the call for papers", address, { cls: "assistant-url" }),
      field("Track or cycle", hint, { optional: true, cls: "assistant-track" }),
      go),
    status);
}

export async function render(ctx) {
  const [folders, assistant] = await Promise.all([
    api.get("/api/folders"),
    // Without the assistant, or when the question fails, the box is left out.
    api.get("/api/assistant").catch(() => ({ enabled: false })),
  ]);
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

  const moreVenue = h("details", { class: "form-group" },
    h("summary", { class: "text-bold", style: { cursor: "pointer" }, text: "More about the venue" }),
    h("div", { class: "mt-3" }, venue.more));

  // The assistant's answer, into the form. Only what it found is set. The
  // title, the short name, the folder and the labels stay as they are.
  function applyAnswer(answer) {
    const found = answer?.fields || {};
    const has = (key) => found[key] !== undefined && found[key] !== null && found[key] !== "";
    // The input event lets the name preview and the page limit's hint follow.
    const set = (input, value) => {
      input.value = String(value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    };
    if (has("conference")) set(conference, found.conference);
    if (has("year")) set(year, found.year);
    for (const key of VENUE_KEYS) {
      if (!has(key)) continue;
      const input = venue.inputs[key];
      // A review model the select does not offer would leave it blank.
      if (input.tagName === "SELECT" && ![...input.options].some((o) => o.value === String(found[key]))) continue;
      set(input, found[key]);
    }
    if (MORE_VENUE_KEYS.some((key) => has(key))) moreVenue.open = true;
    if (has("notes")) {
      const add = String(found.notes).trim();
      const now = notes.value;
      if (!now.trim()) set(notes, add);
      else if (!now.includes(add)) set(notes, `${now.replace(/\s+$/, "")}\n\n${add}`);
    }
    dates.fill((answer?.dates || []).map(fromInput));
    starter.offer(answer?.starter, has("template") ? String(found.template) : "");
  }

  const form = h("form", { class: "new-form", novalidate: true },
    h("div", { class: "page-head" }, h("div", {}, heading, lede)),
    kind,
    assistant?.enabled ? assistantBox(ctx, { apply: applyAnswer, isPlan }) : null,
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
    moreVenue,

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
    // The venue's format starts as an empty paper. Its two files follow.
    const files = plan ? null : starter.venueFiles(title.value.trim());
    try {
      const { project, imported } = await busy(submit, async () => {
        const made = await api.post("/api/projects", { ...body(false), dates: ds });
        if (files) {
          try {
            for (const [path, text] of files) {
              // A Blob, because api.put sends anything else as JSON.
              await api.put(`/api/projects/${enc(made.id)}/files/${encPath(path)}`, new Blob([text], { type: "text/plain" }));
            }
            return { project: made, imported: null };
          } catch (err) {
            // Like a failed import below. The paper holds nothing else yet.
            await api.del(`/api/projects/${enc(made.id)}`, { query: { confirm: made.name } }).catch(() => {});
            throw new Error(`The starting files could not be written: ${err.message.replace(/\.?$/, ".")} Nothing was created.`);
          }
        }
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
            files ? "It starts with a main.tex in the venue's format. " : null,
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
