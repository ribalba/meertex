// /settings is your account. It holds the profile picture, name and time
// zone, the naming pattern
// with a live example, your usual writing time, spelling and grammar in the
// editor, the reminder mails, the mails about mentions, the API token, and how
// to set up the command line.

import { api } from "../api.js";
import { h, clear, debounce } from "../dom.js";
import { icon } from "../icons.js";
import { meChanged, session } from "../session.js";
import { patternProblem, previewName } from "../naming.js";
import { allTimezones } from "../time.js";
import { avatar, busy, commandLine, confirmDialog, copyButton, counter, field, flash, flashError, openDialog, setTitle, spinner, textInput } from "../ui.js";
import { onLeave } from "../router.js";
import { writingDaysField } from "./timeline.js";

const EXAMPLES = [
  { conference: "ICSE", year: 2027, title: "Towards Green Large Language Models", track: "Research Track", paper: "" },
  { conference: "EuroSys", year: 2027, title: "Pacer: Energy-aware Scheduling for Serverless", track: "", paper: "pacer" },
];

const PLACEHOLDERS = ["{year}", "{conference}", "{conference|lower}", "{paper}", "{paper|lower}", "{track}", "{title}"];

export async function render(ctx) {
  setTitle("Settings");
  const me = session.me;

  const menu = h("nav", { class: "side-menu", "aria-label": "Settings" },
    h("a", { href: "#profile" }, icon("person"), "Profile"),
    h("a", { href: "#naming" }, icon("book"), "Naming"),
    h("a", { href: "#writing" }, icon("calendar"), "Writing time"),
    h("a", { href: "#grammar" }, icon("check"), "Spelling and grammar"),
    h("a", { href: "#reminders" }, icon("bell"), "Reminders"),
    h("a", { href: "#mentions" }, icon("mail"), "Mentions"),
    h("a", { href: "#token" }, icon("key"), "API token"),
    h("a", { href: "#cli" }, icon("terminal"), "Command line"));

  const head = h("div", { class: "flex mb-4" },
    avatar(me, 40),
    h("div", {},
      h("h1", { style: { fontSize: "20px" }, text: me.name || me.email }),
      h("div", { class: "color-muted", text: me.name ? `${me.email} · your personal account` : "Your personal account" })));
  // A new name or picture shows at the top of this page and in the header.
  const changed = () => {
    head.firstChild.replaceWith(avatar(session.me, 40));
    meChanged();
  };

  const content = h("div", {},
    profileSection(me, changed),
    namingSection(me),
    writingSection(me),
    grammarSection(),
    remindersSection(me),
    mentionsSection(me),
    tokenSection(me),
    cliSection(),
  );

  clear(ctx.main).append(h("div", { class: "container container-md page" },
    head,
    h("div", { class: "layout-settings" }, h("div", {}, menu), content)));
}

function profileSection(me, changed) {
  const name = textInput({ value: me.name || "", autocomplete: "name", maxlength: "200" });
  const email = textInput({ value: me.email, disabled: true });
  const zones = allTimezones();
  if (me.timezone && !zones.includes(me.timezone)) zones.unshift(me.timezone);
  const tz = h("select", { class: "form-select input-block", value: me.timezone || "UTC" }, zones.map((z) => h("option", { value: z }, z)));
  const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const useBrowser = browserTz && browserTz !== me.timezone
    ? h("button", { type: "button", class: "btn-link text-small", text: `Use this device's zone (${browserTz})`, onclick: () => { tz.value = browserTz; } })
    : null;
  const save = h("button", { type: "submit", class: "btn btn-primary", text: "Update profile" });
  const form = h("form", { class: "settings-section", id: "profile" },
    h("div", { class: "subhead" }, h("h2", { text: "Profile" })),
    pictureField(changed),
    field("Name", name, { hint: "Shown to your coauthors next to your edits." }),
    field("Email", email, { hint: "You sign in with this address. It cannot be changed here." }),
    field("Time zone", tz, { hint: h("span", {}, "Times in the history and tooltips use it. Deadlines are always shown in the venue's zone. ", useBrowser) }),
    h("div", { class: "form-actions" }, save));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const updated = await busy(save, () => api.patch("/api/auth/me", { name: name.value.trim(), timezone: tz.value }));
      Object.assign(session.me, updated);
      changed();
      flash("Profile saved.", "success");
    } catch (err) {
      flashError(err);
    }
  });
  return form;
}

// The picture is saved when it is chosen, apart from the rest of the form.
function pictureField(changed) {
  const preview = h("span", {});
  const file = h("input", { type: "file", accept: "image/*", hidden: true, "aria-label": "Profile picture" });
  const upload = h("button", { type: "button", class: "btn" });
  const remove = h("button", { type: "button", class: "btn btn-danger", text: "Remove" });
  const paint = () => {
    clear(preview).append(avatar(session.me, 96));
    upload.textContent = session.me.avatar_url ? "Change picture" : "Upload a picture";
    remove.hidden = !session.me.avatar_url;
  };
  const saved = (updated, text) => {
    Object.assign(session.me, updated);
    paint();
    changed();
    flash(text, "success");
  };
  upload.addEventListener("click", () => file.click());
  file.addEventListener("change", async () => {
    const picked = file.files?.[0];
    file.value = "";
    if (!picked) return;
    try {
      const square = await squarePicture(picked);
      saved(await busy(upload, () => api.put("/api/auth/me/avatar", square)), "Profile picture saved.");
    } catch (err) {
      flashError(err);
    }
  });
  remove.addEventListener("click", async () => {
    try {
      saved(await busy(remove, () => api.del("/api/auth/me/avatar")), "Profile picture removed. Your initials show again.");
    } catch (err) {
      flashError(err);
    }
  });
  paint();
  return h("fieldset", { class: "form-group" },
    h("legend", { text: "Profile picture" }),
    h("div", { class: "flex flex-wrap", style: { gap: "16px" } }, preview, h("div", { class: "flex" }, upload, remove), file),
    h("div", { class: "form-hint", text: "Shown next to your name for your coauthors, instead of your initials. The middle square of the picture is used." }));
}

// The size a profile picture is stored at. The largest place it shows is 96px.
const PICTURE_PX = 256;

// The middle square of a picture, scaled to PICTURE_PX. WebP where the browser
// writes it, PNG where it does not.
async function squarePicture(file) {
  const url = URL.createObjectURL(file);
  const img = new Image();
  img.src = url;
  try {
    await img.decode();
  } catch {
    throw new Error("This picture could not be read. Try a JPEG or PNG.");
  } finally {
    URL.revokeObjectURL(url);
  }
  let side = Math.min(img.naturalWidth, img.naturalHeight);
  let source = img;
  let x = (img.naturalWidth - side) / 2;
  let y = (img.naturalHeight - side) / 2;
  // Halving step by step keeps a large photo from turning grainy.
  while (side / 2 >= PICTURE_PX) {
    const half = h("canvas", { width: Math.floor(side / 2), height: Math.floor(side / 2) });
    half.getContext("2d").drawImage(source, x, y, side, side, 0, 0, half.width, half.height);
    source = half;
    side = half.width;
    x = 0;
    y = 0;
  }
  const size = Math.min(PICTURE_PX, side);
  const out = h("canvas", { width: size, height: size });
  const g = out.getContext("2d");
  g.imageSmoothingQuality = "high";
  g.drawImage(source, x, y, side, side, 0, 0, size, size);
  const blob = size ? await new Promise((resolve) => out.toBlob(resolve, "image/webp", 0.9)) : null;
  if (!blob) throw new Error("This picture could not be read. Try a JPEG or PNG.");
  return blob;
}

function namingSection(me) {
  const pattern = textInput({ value: me.naming_pattern || "{year}-{conference|lower}-{paper|lower}", spellcheck: "false", class: "form-control input-block input-mono" });
  pattern.id = "naming-pattern";
  const examples = EXAMPLES.map(() => h("span", { class: "name" }));
  const error = h("div", { class: "form-error", role: "alert" });
  let saved = me.naming_pattern;
  let token = 0;

  const serverCheck = debounce(async () => {
    const mine = ++token;
    // The server's dry run knows the saved pattern only, so it confirms the
    // example once the typed pattern is the saved one.
    if (pattern.value.trim() !== saved) return;
    try {
      const res = await api.post("/api/projects", { ...EXAMPLES[0], dry_run: true, dates: [] });
      // An example that collides with a real paper gets "-2" from the server.
      // That suffix says nothing about the pattern, so it is left off.
      const local = previewName(saved, EXAMPLES[0]);
      const name = res?.name && res.name.replace(/-\d+$/, "") === local ? local : res?.name;
      if (mine === token && name && pattern.value.trim() === saved) examples[0].textContent = name;
    } catch {
      // The local preview stays.
    }
  }, 300);

  const paint = () => {
    const value = pattern.value.trim();
    const problem = patternProblem(value);
    if (problem) {
      error.textContent = problem;
      examples.forEach((ex) => { ex.textContent = "…"; });
      return;
    }
    error.textContent = "";
    EXAMPLES.forEach((ex, i) => { examples[i].textContent = previewName(value, ex) || "…"; });
    serverCheck();
  };
  pattern.addEventListener("input", paint);

  const chips = h("div", { class: "topic-list mt-2" }, PLACEHOLDERS.map((ph) => {
    const b = h("button", { type: "button", class: "topic", style: { border: "0", cursor: "pointer" }, text: ph, title: `Insert ${ph}` });
    b.addEventListener("click", () => {
      const start = pattern.selectionStart ?? pattern.value.length;
      const end = pattern.selectionEnd ?? start;
      pattern.value = pattern.value.slice(0, start) + ph + pattern.value.slice(end);
      pattern.focus();
      pattern.setSelectionRange(start + ph.length, start + ph.length);
      paint();
    });
    return b;
  }));

  const save = h("button", { type: "submit", class: "btn btn-primary", text: "Save pattern" });
  const reset = h("button", { type: "button", class: "btn", text: "Use the default" });
  reset.addEventListener("click", () => { pattern.value = "{year}-{conference|lower}-{paper|lower}"; paint(); });
  const form = h("form", { class: "settings-section", id: "naming" },
    h("div", { class: "subhead" }, h("h2", { text: "Naming pattern" })),
    h("p", { class: "color-muted", text: "A new paper gets its name from this pattern once, when it is created. The name is the folder of every checkout, so it stays fixed after that." }),
    h("div", { class: "form-group" },
      h("label", { class: "form-label", for: pattern.id, text: "Pattern" }),
      pattern,
      error,
      chips,
      h("div", { class: "form-hint" }, "Values become slugs. ", h("code", { text: "|lower" }), " and ", h("code", { text: "|upper" }),
        " change the case. An empty placeholder disappears together with its separator. Between placeholders use only letters, digits, dot, underscore and dash.")),
    h("div", { class: "box mb-3" },
      h("div", { class: "box-header box-header-sm" }, h("h3", { class: "box-title", text: "Examples" })),
      EXAMPLES.map((ex, i) => h("div", { class: "box-row box-row-sm" },
        h("div", { class: "color-muted text-small" },
          `${ex.conference} ${ex.year}${ex.track ? `, ${ex.track}` : ""}, "${ex.title}"${ex.paper ? `, short name ${ex.paper}` : ""}`),
        h("div", { class: "flex gap-4 mt-0" }, icon("arrow-right", { cls: "color-muted" }),
          h("span", { class: "text-mono text-bold" }, examples[i]))))),
    h("div", { class: "form-actions" }, save, reset));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const value = pattern.value.trim();
    if (patternProblem(value)) { pattern.focus(); return; }
    try {
      const updated = await busy(save, () => api.patch("/api/auth/me", { naming_pattern: value }));
      Object.assign(session.me, updated);
      saved = updated?.naming_pattern || value;
      flash("Naming pattern saved. It applies to papers you create from now on.", "success");
      paint();
    } catch (err) {
      error.textContent = err.message;
    }
  });
  paint();
  return form;
}

// The same field as on the timeline. It saves while you type.
function writingSection(me) {
  const days = writingDaysField({ value: me.writing_days || 30 });
  onLeave(() => days.flush());
  return h("section", { class: "settings-section", id: "writing" },
    h("div", { class: "subhead" }, h("h2", { text: "Writing time" })),
    h("p", { class: "color-muted" },
      "How long it usually takes you to write a paper. The ", h("a", { href: "/timeline", text: "timeline" }),
      " starts each paper's writing window this many days before its submission deadline. A paper can have its own time in its row's menu on the timeline."),
    h("div", { class: "form-group" }, days.el));
}

// Spelling and grammar in the editor (editor-grammar.js), like meerail's
// Settings. Kept on the server, so every browser gets the same. The form
// saves with its button. The dictionary and the rules turned off change at
// once, as they do from the editor's suggestions, which fill them.
function grammarSection() {
  const out = h("section", { class: "settings-section", id: "grammar" },
    h("div", { class: "subhead" }, h("h2", { text: "Spelling and grammar" })),
    h("div", { class: "loading" }, spinner()));
  (async () => {
    let config = null;
    let problem = null;
    try {
      config = await api.get("/api/grammar/config");
    } catch (err) {
      if (err.status === 401) return;
      // A server without the checker's routes has no checker either.
      if (err.status !== 404) problem = err.message;
    }
    out.lastElementChild.remove();
    if (config?.available) out.append(...grammarForm(config));
    else out.append(h("p", { class: "color-muted", text: problem || "The spelling and grammar checker is not set up on this server, so the editor's Spellcheck button uses your browser's spellchecker." }));
  })();
  return out;
}

function grammarForm(config) {
  const on = h("input", { type: "checkbox", id: "grammar-on" });
  on.checked = config.enabled;
  const language = h("select", { class: "form-select", id: "grammar-language", style: { width: "auto", maxWidth: "100%" } });
  const variants = textInput({ id: "grammar-variants", value: (config.variants || []).join(", "), spellcheck: "false", autocomplete: "off", placeholder: "en-GB, de-AT", class: "form-control input-block input-mono" });
  const variantsField = field("Preferred variants", variants, { hint: "When the language is detected, check it in these variants, for example British instead of American English. Separate them with commas." });
  const picky = h("input", { type: "checkbox", id: "grammar-picky" });
  picky.checked = config.picky;

  let languages = [];
  const fillLanguages = (current) => {
    const list = languages.some((l) => l.code === current) || current === "auto" ? languages : [{ code: current, name: current }, ...languages];
    clear(language).append(h("option", { value: "auto", text: "Detect from the text" }), ...list.map((l) => h("option", { value: l.code, text: l.name })));
    language.value = current;
    variantsField.hidden = current !== "auto";
  };
  fillLanguages(config.language || "auto");
  language.addEventListener("change", () => { variantsField.hidden = language.value !== "auto"; });
  api.get("/api/grammar/languages").then((res) => {
    languages = res?.languages || [];
    fillLanguages(language.value);
  }).catch(() => {
    // The checker is down. The setting can still be kept as it is.
  });

  const save = h("button", { type: "submit", class: "btn btn-primary", text: "Save" });
  const form = h("form", {},
    h("div", { class: "form-checkbox" }, on,
      h("div", {},
        h("label", { for: on.id, text: "Check spelling and grammar in the editor" }),
        h("div", { class: "form-hint", text: "The Spellcheck button in the editor turns it on and off as well." }))),
    field("Language", language, { hint: "Detecting looks at each file on its own." }),
    variantsField,
    h("div", { class: "form-checkbox" }, picky,
      h("div", {},
        h("label", { for: picky.id, text: "Picky" }),
        h("div", { class: "form-hint", text: "Also point out style and typography, like wordy phrases or a missing comma." }))),
    h("div", { class: "form-actions mt-3" }, save));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      config = await busy(save, () => api.put("/api/grammar/config", {
        enabled: on.checked,
        language: language.value,
        variants: variants.value.split(/[\s,]+/).filter(Boolean),
        picky: picky.checked,
      }));
      flash(config.enabled ? "Spelling and grammar saved." : "Spelling and grammar are off. The editor underlines nothing.", "success");
      paintLists();
    } catch (err) {
      flashError(err);
    }
  });

  // The dictionary and the rules turned off, each a list with a button per row.
  const words = h("div", { class: "box mb-3", id: "grammar-words" });
  const rules = h("div", { class: "box", id: "grammar-rules" });
  const change = async (button, fn) => {
    try {
      config = await busy(button, fn);
      paintLists();
    } catch (err) {
      flashError(err);
    }
  };
  const word = textInput({ "aria-label": "A word for your dictionary", placeholder: "A word", spellcheck: "false", autocomplete: "off", class: "form-control form-control-sm flex-1" });
  const add = h("button", { type: "submit", class: "btn btn-sm", text: "Add word" });
  const addForm = h("form", { class: "box-footer flex" }, word, add);
  addForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const value = word.value.trim();
    if (!value) return;
    change(add, () => api.post("/api/grammar/ignore", { word: value })).then(() => {
      word.value = "";
      word.focus();
    });
  });

  function paintLists() {
    const list = [...(config.words || [])].sort((a, b) => a.localeCompare(b));
    clear(words).append(
      h("div", { class: "box-header box-header-sm" }, h("h3", { class: "box-title flex-1", text: "Your dictionary" }), counter(list.length)),
      ...(list.length ? list.map((w) => {
        const remove = h("button", { type: "button", class: "btn-octicon danger", "aria-label": `Remove ${w} from your dictionary`, title: "Remove" }, icon("x"));
        remove.addEventListener("click", () => change(remove, () => api.put("/api/grammar/config", { words: (config.words || []).filter((x) => x !== w) })));
        return h("div", { class: "box-row box-row-sm flex grammar-row" }, h("span", { class: "flex-1 text-mono truncate", text: w }), remove);
      }) : [h("div", { class: "box-row box-row-sm color-muted text-small", text: "No words yet. They are never flagged as misspelled. “Add to dictionary” in the editor adds one too." })]),
      addForm);
    const off = config.disabled_rules || [];
    clear(rules).append(
      h("div", { class: "box-header box-header-sm" }, h("h3", { class: "box-title flex-1", text: "Rules you turned off" }), counter(off.length)),
      ...(off.length ? off.map((r) => {
        const enable = h("button", { type: "button", class: "btn btn-sm", text: "Turn on", "aria-label": `Turn ${r} on again` });
        enable.addEventListener("click", () => change(enable, () => api.put("/api/grammar/config", { disabled_rules: (config.disabled_rules || []).filter((x) => x !== r) })));
        return h("div", { class: "box-row box-row-sm flex grammar-row" }, h("span", { class: "flex-1 text-mono truncate", text: r }), enable);
      }) : [h("div", { class: "box-row box-row-sm color-muted text-small", text: "None. “Turn off this rule” in the editor's suggestions adds one here." })]));
  }
  paintLists();

  return [
    h("p", { class: "color-muted", text: "The editor underlines spelling mistakes in red, grammar in amber and style in blue, in .tex, .md and .txt files. Click an underline for suggestions, or press Alt+Shift+G for the next one. The text goes to the LanguageTool server this meertex is set up with, without LaTeX's commands, math and citations." }),
    form,
    h("h3", { class: "grammar-lists-head", text: "Words and rules" }),
    words,
    rules,
  ];
}

// The daily reminder mail (DESIGN §5, Reminders): on or off, how many days
// before a date it mentions it, and the hour of your own day it comes at.
const REMINDER_DAYS = [30, 14, 7, 3, 1, 0];

function reminderDayText(d) {
  if (d === 0) return "On the day";
  return d === 1 ? "1 day before" : `${d} days before`;
}

function remindersSection(me) {
  const on = h("input", { type: "checkbox", id: "reminders-on" });
  on.checked = me.reminders !== false;
  const chosen = new Set(me.reminder_days ?? [14, 3, 1, 0]);
  // Days set through the API that the boxes do not offer stay in the list.
  const offered = [...new Set([...REMINDER_DAYS, ...chosen])].sort((a, b) => b - a);
  const boxes = offered.map((d) => {
    const box = h("input", { type: "checkbox", id: `reminder-day-${d}`, value: String(d) });
    box.checked = chosen.has(d);
    return box;
  });
  const hour = h("select", { class: "form-select", id: "reminder-hour", style: { width: "auto" } },
    Array.from({ length: 24 }, (_, i) => h("option", { value: String(i) }, `${String(i).padStart(2, "0")}:00`)));
  hour.value = String(me.reminder_hour ?? 8);
  const zoneHint = h("span", { text: `In your time zone, ${me.timezone || "UTC"}. Change it under Profile.` });

  const sync = () => { [...boxes, hour].forEach((el) => { el.disabled = !on.checked; }); };
  on.addEventListener("change", sync);
  sync();

  const save = h("button", { type: "submit", class: "btn btn-primary", text: "Save reminders" });
  const preview = h("button", { type: "button", class: "btn", text: "Preview the next mail" });
  preview.addEventListener("click", async () => {
    try {
      const data = await busy(preview, () => api.get("/api/reminders/preview"));
      openDialog({
        title: "Your next reminder mail",
        wide: true,
        body: data?.body
          ? h("div", {},
            h("p", { class: "color-muted text-small", text: "Everything within your reminder days, also what an earlier mail said already." }),
            h("p", { class: "text-bold", text: data.subject }),
            h("pre", { class: "box box-body text-small", style: { whiteSpace: "pre-wrap", margin: "0" }, text: data.body }))
          : h("p", { class: "mb-0", text: "Nothing to remind you of right now." }),
      });
    } catch (err) {
      flashError(err);
    }
  });

  const form = h("form", { class: "settings-section", id: "reminders" },
    h("div", { class: "subhead" }, h("h2", { text: "Reminders" })),
    h("p", { class: "color-muted", text: "One mail a day, and only when there is something to say. It lists the deadlines that come within the days below, when to start writing a paper, and the questions about dates that just passed. It covers the papers where you are the owner or an editor. A paper's overview can mute it for that paper." }),
    h("div", { class: "form-checkbox" }, on, h("label", { for: on.id, text: "Email me before deadlines" })),
    h("fieldset", { class: "form-group" },
      h("legend", { text: "Remind me" }),
      h("div", { class: "flex flex-wrap", style: { gap: "0 20px" } },
        offered.map((d, i) => h("div", { class: "form-checkbox" }, boxes[i], h("label", { for: boxes[i].id, text: reminderDayText(d) }))))),
    field("Send it at", hour, { hint: zoneHint }),
    h("div", { class: "form-actions" }, save, preview));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const days = boxes.filter((b) => b.checked).map((b) => Number(b.value));
    try {
      const updated = await busy(save, () => api.patch("/api/auth/me", { reminders: on.checked, reminder_days: days, reminder_hour: Number(hour.value) }));
      Object.assign(session.me, updated);
      zoneHint.textContent = `In your time zone, ${session.me.timezone || "UTC"}. Change it under Profile.`;
      flash(on.checked ? "Reminders saved." : "Reminders are off. You get no reminder mails.", "success");
    } catch (err) {
      flashError(err);
    }
  });
  return form;
}

// The mail when somebody @-mentions you in a comment (DESIGN §13, Mentions).
// The box saves when it changes.
function mentionsSection(me) {
  const on = h("input", { type: "checkbox", id: "mention-mails" });
  on.checked = me.mention_mails !== false;
  on.addEventListener("change", async () => {
    on.disabled = true;
    try {
      const updated = await api.patch("/api/auth/me", { mention_mails: on.checked });
      Object.assign(session.me, updated);
      flash(on.checked ? "You get a mail when somebody mentions you." : "Mentions no longer send you mails.", "success");
    } catch (err) {
      on.checked = !on.checked;
      flashError(err);
    } finally {
      on.disabled = false;
    }
  });
  return h("section", { class: "settings-section", id: "mentions" },
    h("div", { class: "subhead" }, h("h2", { text: "Mentions" })),
    h("p", { class: "color-muted", text: "Type @ in a comment and pick a coauthor to mention them. They get a mail with the comment and a link to it. With this off, mentions of you send no mail. The mails about reviews and about the conversations you write in still come." }),
    h("div", { class: "form-checkbox" }, on, h("label", { for: on.id, text: "Email me when somebody mentions me" })));
}

function tokenSection(me) {
  const out = h("section", { class: "settings-section", id: "token" });
  const paint = () => {
    const token = session.me.api_token;
    clear(out).append(
      h("div", { class: "subhead" }, h("h2", { text: "API token" })),
      h("p", { class: "color-muted" },
        "The token lets the command line, scripts and agents act as you. Send it as ",
        h("code", { text: "Authorization: Bearer <token>" }), ". Keep it secret, it works like a password."),
    );
    if (!token) {
      const create = h("button", { type: "button", class: "btn btn-primary" }, icon("key"), "Create a token");
      create.addEventListener("click", async () => {
        try {
          const updated = await busy(create, () => api.post("/api/auth/api-token"));
          Object.assign(session.me, updated);
          paint();
          flash("Token created. meertex login also creates one for you.", "success");
        } catch (err) {
          flashError(err);
        }
      });
      out.append(h("p", { text: "You have no token yet." }), create);
      return;
    }
    const field2 = h("input", { type: "password", class: "form-control token-field", readonly: true, value: token, "aria-label": "API token" });
    const show = h("button", { type: "button", class: "btn", "aria-label": "Show the token", title: "Show" }, icon("eye"));
    show.addEventListener("click", () => {
      const hidden = field2.type === "password";
      field2.type = hidden ? "text" : "password";
      show.replaceChildren(icon(hidden ? "eye-closed" : "eye"));
      show.setAttribute("aria-label", hidden ? "Hide the token" : "Show the token");
      show.title = hidden ? "Hide" : "Show";
    });
    const rotate = h("button", { type: "button", class: "btn btn-danger" }, "Rotate");
    rotate.addEventListener("click", async () => {
      const ok = await confirmDialog({
        title: "Rotate the API token?",
        message: "The current token stops working right away. Every CLI and script that uses it needs the new one (meertex login on each machine).",
        confirmText: "Rotate token",
        danger: true,
      });
      if (!ok) return;
      try {
        const updated = await busy(rotate, () => api.post("/api/auth/api-token/rotate"));
        Object.assign(session.me, updated);
        paint();
        flash("New token created. The old one no longer works.", "success");
      } catch (err) {
        flashError(err);
      }
    });
    out.append(h("div", { class: "flex flex-wrap" },
      h("div", { class: "copy-field flex-1", style: { minWidth: "240px" } }, field2, show,
        copyButton(() => session.me.api_token, { label: "Copy the token", cls: "btn" })),
      rotate));
  };
  paint();
  return out;
}

function cliSection() {
  return h("section", { class: "settings-section", id: "cli" },
    h("div", { class: "subhead" }, h("h2", { text: "Use meertex from the command line" })),
    h("p", { class: "color-muted", text: "The meertex command keeps a folder on your computer in step with a paper, so you can write in VS Code, with Claude, or in any editor." }),
    h("div", { class: "box" }, h("div", { class: "box-body cmd-list" },
      commandLine("pipx install ./cli", "1. Install it from a checkout of the meertex repository (uv tool install ./cli works too)"),
      commandLine(`meertex login --server ${location.origin}`, "2. Sign in. It emails you a code and stores a token in ~/.meertex"),
      commandLine("meertex checkout <paper>", "3. Copy a paper to ~/papers/<paper>"),
      commandLine("meertex sync", "4. Keep every checkout in sync while you write"),
      commandLine("meertex service install", "Or run the sync in the background with your session"))),
  );
}
