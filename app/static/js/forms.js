// Form pieces shared by the new-paper form, the paper settings and the
// resubmit form: the venue fields of DESIGN §4 and the labels editor.

import { h } from "./dom.js";
import { icon } from "./icons.js";
import { describeLimit, parseLimit } from "./pagelimit.js";
import { field, openDialog, textInput } from "./ui.js";

export const REVIEW_MODELS = [
  ["", "Not stated"],
  ["double-blind", "Double-blind"],
  ["single-blind", "Single-blind"],
  ["open", "Open"],
];

export const VENUE_KEYS = [
  "conference_name", "track", "location", "cfp_url", "conference_url", "submission_url",
  "publisher", "page_limit", "template", "review_model",
];

export function venueFields(initial = {}, { split = false } = {}) {
  const inputs = {
    conference_name: textInput({ value: initial.conference_name || "", placeholder: "International Conference on Software Engineering" }),
    track: textInput({ value: initial.track || "", placeholder: "Research Track" }),
    location: textInput({ value: initial.location || "", placeholder: "Rio de Janeiro, Brazil" }),
    cfp_url: textInput({ type: "url", value: initial.cfp_url || "", placeholder: "https://…" }),
    conference_url: textInput({ type: "url", value: initial.conference_url || "", placeholder: "https://…" }),
    submission_url: textInput({ type: "url", value: initial.submission_url || "", placeholder: "https://….hotcrp.com" }),
    publisher: textInput({ value: initial.publisher || "", placeholder: "ACM, IEEE, Springer, LIPIcs, USENIX", list: "publisher-suggestions" }),
    page_limit: textInput({ value: initial.page_limit || "", placeholder: "10 pages + 2 for references" }),
    template: textInput({ value: initial.template || "", placeholder: "acmart sigconf, IEEEtran conference, llncs" }),
    review_model: h("select", { class: "form-select input-block", value: initial.review_model || "" },
      REVIEW_MODELS.map(([v, t]) => h("option", { value: v }, t))),
  };
  // How the PDF is checked against the limit (pagelimit.js), as it is typed.
  const limitHint = h("span");
  const paintLimit = () => {
    const limit = parseLimit(inputs.page_limit.value);
    limitHint.textContent = limit
      ? `The PDF is checked against ${describeLimit(limit)}.`
      : "With a number in it the PDF is checked, like 8 + references or 10 + 2 for references.";
  };
  inputs.page_limit.addEventListener("input", paintLimit);
  paintLimit();
  const limitInfo = h("button", {
    type: "button", class: "btn-octicon form-info", "aria-label": "How to write the page limit", title: "How to write the page limit",
    onclick: () => pageLimitHelp(),
  }, icon("info", { size: 14 }));
  if (!document.getElementById("publisher-suggestions")) {
    document.body.append(h("datalist", { id: "publisher-suggestions" },
      ["ACM", "IEEE", "Springer", "LIPIcs", "USENIX", "Elsevier", "PMLR"].map((v) => h("option", { value: v }))));
  }
  const f = {
    conference_name: field("Full conference name", inputs.conference_name, { optional: true }),
    track: field("Track", inputs.track, { optional: true }),
    location: field("Location", inputs.location, { optional: true }),
    cfp_url: field("Call for papers", inputs.cfp_url, { optional: true }),
    conference_url: field("Conference website", inputs.conference_url, { optional: true }),
    submission_url: field("Submission site", inputs.submission_url, { optional: true, hint: "HotCRP, EasyChair and so on." }),
    publisher: field("Publisher", inputs.publisher, { optional: true }),
    page_limit: withInfo(field("Page limit", inputs.page_limit, { optional: true, hint: limitHint }), limitInfo),
    template: field("LaTeX template", inputs.template, { optional: true }),
    review_model: field("Review model", inputs.review_model, { optional: true }),
  };
  const row = (...cells) => h("div", { class: "form-row" }, ...cells);
  const values = () => Object.fromEntries(Object.entries(inputs).map(([k, input]) => [k, input.value.trim()]));
  if (split) {
    // Forms that create a paper keep in view what decides how it is written:
    // where it goes in, how long it may be, whether it must be anonymous and
    // which class it uses. The rest folds away under "More about the venue".
    const main = h("div", {}, row(f.submission_url, f.page_limit), row(f.review_model, f.template));
    const more = h("div", {}, f.conference_name, row(f.track, f.location), f.cfp_url, row(f.conference_url, f.publisher));
    return { main, more, inputs, values };
  }
  const el = h("div", {},
    f.conference_name,
    row(f.track, f.location),
    f.cfp_url,
    row(f.conference_url, f.submission_url),
    row(f.publisher, f.page_limit),
    row(f.template, f.review_model),
  );
  return { el, inputs, values };
}

// A field with a button next to its label, the (i) that explains it.
function withInfo(group, button) {
  const label = group.querySelector("label");
  label.classList.add("form-label");
  const row = h("div", { class: "form-label-row" });
  label.before(row);
  row.append(label, button);
  return group;
}

// The examples of the page limit's help. The table says what the check
// (pagelimit.js, DESIGN §14) makes of each, so it cannot tell a different story.
const LIMIT_EXAMPLES = [
  "8", "6 pages including references", "8 + references", "12 pages, references not counted",
  "10 + 2", "10 pages + 2 pages of references", "6-10 pages", "no limit",
];

function pageLimitHelp() {
  const rows = LIMIT_EXAMPLES.map((text) => {
    const limit = parseLimit(text);
    return h("tr", {}, h("td", {}, h("code", { text })), h("td", { text: limit ? describeLimit(limit) : "any number of pages, no check" }));
  });
  openDialog({
    title: "How to write the page limit",
    wide: true,
    body: h("div", { class: "limit-help" },
      h("p", { text: "Write the limit as the call for papers words it. The first number is the pages for the text. A second number or the words around it say what the references get." }),
      h("table", { class: "limit-table" },
        h("thead", {}, h("tr", {}, h("th", { text: "You write" }), h("th", { text: "The PDF may have" }))),
        h("tbody", {}, rows)),
      h("ul", {},
        h("li", {}, h("strong", { text: "References free" }), " with + or plus, excluding, without, unlimited, extra or not counted."),
        h("li", {}, h("strong", { text: "References counted" }), " with including, incl. or with, and when the limit does not name them."),
        h("li", {}, h("strong", { text: "A range" }), " like 6-10 pages allows the larger number.")),
      h("p", { text: "The references start at the last heading that reads References or Bibliography. The page with that heading counts as text too when text comes before it. An appendix after the references counts as references." }),
      h("p", { class: "color-muted", text: "A PDF that is too long gets a warning in the editor and in a version's preview." })),
  });
}

// Labels as removable pills with a text input, like GitHub's topics editor.
export function labelEditor(initial = [], { onChange = null } = {}) {
  let labels = [...new Set((initial || []).filter(Boolean))];
  const input = h("input", { type: "text", placeholder: "Add a label", "aria-label": "Add a label", autocomplete: "off" });
  const el = h("div", { class: "label-editor" });

  function paint() {
    el.replaceChildren(
      ...labels.map((l) => h("span", { class: "topic" }, l,
        h("button", { type: "button", "aria-label": `Remove label ${l}`, onclick: () => remove(l) }, icon("x", { size: 12 })))),
      input,
    );
  }
  function add(raw) {
    const parts = String(raw).split(",").map((s) => s.trim()).filter(Boolean);
    let changed = false;
    for (const p of parts) {
      if (!labels.includes(p)) { labels.push(p); changed = true; }
    }
    input.value = "";
    if (changed) { paint(); onChange?.(labels); }
    input.focus();
  }
  function remove(l) {
    labels = labels.filter((x) => x !== l);
    paint();
    onChange?.(labels);
    input.focus();
  }
  input.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === ",") && input.value.trim()) {
      e.preventDefault();
      add(input.value);
    } else if (e.key === "Enter") {
      // An empty Enter must not submit the surrounding form by accident.
      e.preventDefault();
    } else if (e.key === "Backspace" && !input.value && labels.length) {
      remove(labels[labels.length - 1]);
    }
  });
  input.addEventListener("blur", () => { if (input.value.trim()) add(input.value); });
  el.addEventListener("click", (e) => { if (e.target === el) input.focus(); });
  paint();
  // Text still in the input counts too, so a label typed without Enter is kept.
  const values = () => {
    const pending = input.value.split(",").map((s) => s.trim()).filter(Boolean);
    return [...new Set([...labels, ...pending])];
  };
  return { el, input, values };
}
