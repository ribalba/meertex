// The Reviews tab, like GitHub's pull request list (docs/DESIGN.md §13, In the
// web app). /p/<name>/reviews lists the reviews and, below them, the
// conversations of the whole paper in file order, so the authors work through
// them in one place. Single comments live there too. /reviews/<id>[/files|/changes]
// is one review (review.js).
//
// A link to /reviews#thread-<id> (a mail about a single comment) scrolls to
// that conversation and marks it, also when its state is not the one listed.
//
// The authors ask a coauthor for a review here. The mail links to
// /reviews/requests/<id>, which starts that review on the state asked for and
// opens its Files view.

import { api, enc, projectUrl, request } from "../api.js";
import { h, append, clear, nextId, plural } from "../dom.js";
import { icon } from "../icons.js";
import { navigate, onLeave } from "../router.js";
import { session } from "../session.js";
import { timeAgo } from "../time.js";
import {
  avatar, blankslate, busy, confirmDialog, counter, flash, flashError, loading, openDialog, personName, queueFlash,
  setTitle,
} from "../ui.js";
import {
  FileTexts, highlightHash, permissions, refreshOpenCounts, reviewBase, reviewTitle, threadCard,
  verdictIcon, verdictLabel,
} from "../threads.js";
import * as reviewPage from "./review.js";

const FILTERS = [
  ["open", "Open"],
  ["resolved", "Resolved"],
  ["accepted", "Accepted"],
  ["all", "All"],
];

export async function render(ctx) {
  if (ctx.sub[1] === "requests") return openRequest(ctx);
  if (ctx.sub[1]) return reviewPage.render(ctx);
  const p = ctx.project;
  setTitle("Reviews", p.name);
  const perms = permissions(p);
  const state = FILTERS.some(([k]) => k === ctx.query.get("state")) ? ctx.query.get("state") : "open";

  const [reviews, requests, threads] = await Promise.all([
    api.get(projectUrl(ctx.ref, "/reviews")),
    api.get(projectUrl(ctx.ref, "/review-requests")),
    api.get(projectUrl(ctx.ref, "/threads"), { query: { state } }),
  ]);
  if (!ctx.alive()) return;

  // A conversation the URL names but the filter leaves out is fetched on its own.
  const wanted = /^#thread-([0-9a-f-]{8,36})$/.exec(location.hash);
  let linked = null;
  if (wanted && !threads.some((t) => t.id === wanted[1])) {
    try {
      linked = await api.get(projectUrl(ctx.ref, `/threads/${enc(wanted[1])}`));
    } catch (err) {
      if (err.status !== 404) throw err;
      flash("That conversation is gone, or it is part of a review that is not submitted yet.", "warn");
    }
    if (!ctx.alive()) return;
  }

  const mine = reviews.find((r) => r.state === "pending" && r.reviewer?.id === session.me?.id) || null;
  const env = {
    project: p,
    base: ctx.base,
    perms,
    texts: new FileTexts(p.id, "live"),
    reviews: new Map(reviews.map((r) => [r.id, r])),
    onChange: () => refreshOpenCounts(p.id),
  };

  let action = null;
  if (mine) {
    action = h("a", { class: "btn btn-primary", href: `${ctx.base}/reviews/${enc(mine.id)}/files` }, icon("comment"), "Continue your review");
  } else if (perms.comment) {
    action = h("button", { type: "button", class: "btn btn-primary" }, icon("comment"), "Start a review");
    action.addEventListener("click", () => startReviewDialog(ctx));
  }

  const ask = perms.resolve
    ? h("button", { type: "button", class: "btn", onclick: () => requestReviewDialog(ctx, requests) }, icon("person"), "Request a review")
    : null;

  const head = h("div", { class: "subhead" }, h("h2", {}, "Reviews ", counter(reviews.length)), ask, action);
  const rows = [...requests.map((rq) => requestRow(rq, ctx, perms, mine)), ...reviews.map((r) => reviewRow(r, ctx))];
  const intro = rows.length ? null : h("div", { class: "box mb-4" }, blankslate({
    iconName: "comment",
    title: "No reviews yet",
    text: perms.comment
      ? "A review comments on a fixed state of the paper, a version or the text as it is now. The authors reply, change the text and resolve each comment. You see how it changed and accept it."
      : "Reviewers, editors and the owner can review the paper. Their reviews show up here.",
    action: action && !mine ? h("button", { type: "button", class: "btn btn-primary", text: "Start the first review", onclick: () => startReviewDialog(ctx) }) : null,
  }));
  const list = rows.length ? h("ul", { class: "box box-list mb-4 rv-review-list", "aria-label": "Reviews" }, rows) : null;

  const filter = h("div", { class: "segmented", role: "group", "aria-label": "Which conversations" },
    FILTERS.map(([key, text]) => h("button", {
      type: "button", "aria-pressed": String(key === state), text,
      onclick: () => navigate(`${ctx.base}/reviews${key === "open" ? "" : `?state=${key}`}`, { replace: true, keepScroll: true }),
    })));
  const convHead = h("div", { class: "rv-section-head" },
    h("h3", { id: "conversations" }, state === "open" ? "Open conversations " : "Conversations ", counter(threads.length)),
    h("span", { class: "flex-1" }),
    filter);

  const cards = threads.map((t) => threadCard(t, env));
  const empty = cards.length ? null : h("div", { class: "box" }, blankslate({
    iconName: "check-circle",
    title: state === "open" ? "No open conversations" : "Nothing here",
    text: state === "open"
      ? "Comments from reviews and single comments stay here until they are resolved."
      : "No conversation is in this state.",
  }));
  const linkedBox = linked ? h("div", { class: "mb-4" },
    h("div", { class: "rv-section-head" }, h("h3", { text: "The conversation you followed" })),
    threadCard(linked, env)) : null;

  append(clear(ctx.main), [head, intro || list, linkedBox, convHead,
    h("p", { class: "text-small color-muted rv-section-lede", text: "Every conversation on the paper in file order, with the text it is on as it reads now." }),
    h("div", { class: "rv-thread-list" }, cards), empty]);

  highlightHash(ctx.main);
  const onHash = () => highlightHash(ctx.main)?.scrollIntoView({ block: "start" });
  window.addEventListener("hashchange", onHash);
  onLeave(() => window.removeEventListener("hashchange", onHash));
}

function reviewRow(r, ctx) {
  const href = `${ctx.base}/reviews/${enc(r.id)}`;
  const c = r.counts || {};
  const total = (c.open || 0) + (c.resolved || 0) + (c.accepted || 0);
  return h("li", { class: "box-row rv-review-row" },
    h("span", { class: "rv-review-icon" }, verdictIcon(r)),
    h("div", { class: "flex-1" },
      h("div", { class: "rv-review-title" },
        h("a", { class: "text-bold", href, text: reviewTitle(r) }),
        verdictLabel(r)),
      h("div", { class: "text-small color-muted rv-review-meta" },
        "on ", reviewBase(r, ctx.base), " · ",
        r.submitted_at ? ["submitted ", timeAgo(r.submitted_at)] : ["started ", timeAgo(r.created_at)]),
      r.summary ? h("p", { class: "rv-review-summary", text: r.summary.split("\n")[0] }) : null),
    h("a", { class: "rv-review-counts text-small", href, title: `${plural(total, "comment")}` },
      h("span", { class: c.open ? "color-fg" : "color-muted" }, icon("comment", { size: 14 }), ` ${c.open || 0} open`),
      h("span", { class: "color-muted" }, `${c.resolved || 0} resolved`),
      h("span", { class: "color-muted" }, `${c.accepted || 0} accepted`)));
}

// An open request, above the reviews. The person asked starts it here too,
// unless they have a pending review to continue (`pending`), and the authors
// withdraw it.
function requestRow(rq, ctx, perms, pending) {
  const who = personName(rq.reviewer);
  const mine = rq.reviewer?.id === session.me?.id;
  const start = mine && !pending ? h("button", { type: "button", class: "btn btn-sm btn-primary", text: "Start your review" }) : null;
  start?.addEventListener("click", () => busy(start, () => startRequested(ctx, rq)).catch(flashError));
  const withdraw = perms.resolve ? h("button", { type: "button", class: "btn btn-sm", text: "Withdraw" }) : null;
  withdraw?.addEventListener("click", async () => {
    const ok = await confirmDialog({
      title: "Withdraw the request?",
      message: `It goes from the list, and ${who} is not mailed about it. A review they started stays theirs.`,
      confirmText: "Withdraw",
    });
    if (!ok) return;
    try {
      await api.del(projectUrl(ctx.ref, `/review-requests/${enc(rq.id)}`));
      flash(`Withdrew the request to ${who}.`, "success");
      ctx.reload();
    } catch (err) {
      flashError(err);
    }
  });
  return h("li", { class: "box-row rv-review-row" },
    h("span", { class: "rv-review-icon" }, icon("clock", { cls: "color-attention", label: "Requested" })),
    h("div", { class: "flex-1" },
      h("div", { class: "rv-review-title" },
        avatar(rq.reviewer, 20),
        h("strong", { text: mine ? "You" : who }),
        h("span", { class: "Label label-attention", text: "Review requested" })),
      h("div", { class: "text-small color-muted rv-review-meta" },
        "on ", reviewBase(rq, ctx.base), " · asked by ", personName(rq.requested_by), " ", timeAgo(rq.created_at)),
      rq.note ? h("p", { class: "rv-review-summary", text: rq.note.split("\n")[0] }) : null),
    h("div", { class: "rv-request-actions" }, start, withdraw));
}

// --- Starting a review ------------------------------------------------------------

// The choice of the state to review: the text as it is now, the last snapshot
// or a version. `value()` is the ref of the one picked.
function stateChoices(p, { preselect = null } = {}) {
  const versions = [...(p.versions || [])].sort((a, b) => (b.number || 0) - (a.number || 0));
  const name = nextId("rv-state");
  const choice = (value, title, text, checked) => h("label", { class: "choice" },
    h("input", { type: "radio", name, value, checked }),
    h("div", {}, h("strong", { text: title }), h("span", { text })));
  const first = preselect || (versions[0] ? versions[0].tag : "live");
  const el = h("fieldset", { class: "form-group mb-0" }, h("legend", { class: "sr-only", text: "The state to review" }),
    choice("live", "The current state", "A snapshot of the live text, taken now.", first === "live"),
    choice("HEAD", "The last snapshot", "The newest commit in the history.", first === "HEAD"),
    versions.map((v) => choice(v.tag, v.label, `Version ${v.number}, tag ${v.tag}`, first === v.tag)));
  return { el, value: () => el.querySelector(`input[name="${name}"]:checked`)?.value || "HEAD" };
}

// The review opens on its Files view, ready for comments.
export function startReviewDialog(ctx, { preselect = null } = {}) {
  const states = stateChoices(ctx.project, { preselect });
  const start = h("button", { type: "button", class: "btn btn-primary", text: "Start review" });
  const d = openDialog({
    title: "Start a review",
    body: [
      h("p", { text: "Comments are made on one fixed state of the paper. Pick it here. Your comments stay pending, and only you see them, until you finish the review." }),
      states.el,
    ],
    footer: [h("button", { type: "button", class: "btn", text: "Cancel", onclick: () => d.close() }), start],
  });
  start.addEventListener("click", async () => {
    const ref = states.value();
    try {
      const res = await busy(start, () => request("POST", projectUrl(ctx.ref, "/reviews"), { body: { ref }, raw: true }));
      const review = await res.json();
      d.close();
      if (res.status === 200) queueFlash("You have a pending review already. Here it is. Finish or discard it to start another one.", "info");
      navigate(`${ctx.base}/reviews/${enc(review.id)}/files`);
    } catch (err) {
      flashError(err);
    }
  });
  start.focus();
}


// --- Asking for a review ------------------------------------------------------------

// The authors ask a coauthor to review one state of the paper. The person gets
// a mail with a link that starts the review there (openRequest).
async function requestReviewDialog(ctx, requests) {
  let members;
  try {
    members = await api.get(projectUrl(ctx.ref, "/members"));
  } catch (err) {
    flashError(err);
    return;
  }
  const people = members.filter((m) => m.role !== "viewer" && m.user_id !== session.me?.id);
  if (!people.length) {
    const d = openDialog({
      title: "Request a review",
      body: h("p", {}, "Nobody else on this paper can review it. Add a coauthor as reviewer or editor under ",
        h("a", { href: `${ctx.base}/settings/access`, onclick: () => d.close() }, "Settings, Access"), " first."),
      footer: [h("button", { type: "button", class: "btn", text: "Close", onclick: () => d.close() })],
    });
    return;
  }

  const asked = new Set(requests.map((rq) => rq.reviewer?.id));
  const first = people.find((m) => !asked.has(m.user_id)) || people[0];
  const name = nextId("rv-who");
  const personChoice = (m) => h("label", { class: "choice rv-person-choice" },
    h("input", { type: "radio", name, value: m.user_id, checked: m === first }),
    avatar(m, 20),
    h("div", {},
      h("strong", { text: m.name || m.email }),
      h("span", { text: [m.name ? m.email : null, m.role, asked.has(m.user_id) ? "asked already, asking again mails them again" : null]
        .filter(Boolean).join(" · ") })));
  const states = stateChoices(ctx.project);
  const note = h("textarea", { class: "form-control input-block", rows: "3", id: nextId("rv-note"), placeholder: "What should they look at?" });
  const send = h("button", { type: "button", class: "btn btn-primary", text: "Ask for the review" });
  const error = h("div", { class: "form-error", role: "alert", hidden: true });
  const d = openDialog({
    title: "Request a review",
    wide: true,
    body: [
      h("p", { text: "They get a mail with a link that starts their review on the state you pick. Their comments stay pending until they finish it." }),
      h("fieldset", { class: "form-group" }, h("legend", { text: "Who" }), people.map(personChoice)),
      h("div", { class: "form-group" }, h("div", { class: "form-label", text: "What" }), states.el),
      h("label", { class: "form-label", for: note.id }, "Note", h("span", { class: "optional", text: " (optional)" })),
      note,
      error,
    ],
    footer: [h("button", { type: "button", class: "btn", text: "Cancel", onclick: () => d.close() }), send],
  });
  send.addEventListener("click", async () => {
    const reviewer = d.el.querySelector(`input[name="${name}"]:checked`)?.value;
    const who = people.find((m) => m.user_id === reviewer);
    try {
      await busy(send, () => api.post(projectUrl(ctx.ref, "/review-requests"), { reviewer, ref: states.value(), note: note.value }));
      d.close();
      flash(`Asked ${who.name || who.email} for a review. They got a mail with a link that starts it.`, "success");
      ctx.reload();
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    }
  });
  d.el.querySelector(`input[name="${name}"]:checked`)?.focus();
}

// Starts the review a request asks for and opens its Files view. A pending
// review of your own comes back instead, on whatever state it is.
async function startRequested(ctx, rq, { replace = false } = {}) {
  const res = await request("POST", projectUrl(ctx.ref, `/review-requests/${enc(rq.id)}/start`), { raw: true });
  const review = await res.json();
  const who = personName(rq.requested_by);
  queueFlash(res.status === 200
    ? `${who} asked you for a review. You have a pending review already, so here it is. Finishing it answers the request.`
    : [h("strong", { text: `${who} asked you for this review.` }), rq.note ? ` ${rq.note}` : " Comment on the files, then finish it."],
  "info");
  navigate(`${ctx.base}/reviews/${enc(review.id)}/files`, { replace });
}

// /reviews/requests/<id> is the link in the mail. The person asked lands in a
// review on the state asked for. Anybody else, and a request that was
// answered or withdrawn, goes to the list or to the review that answered it.
async function openRequest(ctx) {
  setTitle("Review request", ctx.project.name);
  const id = decodeURIComponent(ctx.sub[2] || "");
  let rq = null;
  if (id) {
    try {
      rq = await api.get(projectUrl(ctx.ref, `/review-requests/${enc(id)}`));
    } catch (err) {
      if (err.status !== 404 && err.status !== 422) throw err;
    }
  }
  if (!ctx.alive()) return;
  if (!rq) {
    queueFlash("That review request was withdrawn.", "warn");
    navigate(`${ctx.base}/reviews`, { replace: true });
  } else if (rq.state !== "open") {
    queueFlash("That request is answered already. This is the review that answered it.", "info");
    navigate(rq.review_id ? `${ctx.base}/reviews/${enc(rq.review_id)}` : `${ctx.base}/reviews`, { replace: true });
  } else if (rq.reviewer?.id !== session.me?.id) {
    queueFlash(`That request asks ${personName(rq.reviewer)} for a review, not you.`, "info");
    navigate(`${ctx.base}/reviews`, { replace: true });
  } else {
    await startRequested(ctx, rq, { replace: true });
  }
}
