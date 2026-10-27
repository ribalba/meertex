// The Reviews tab, like GitHub's pull request list (docs/DESIGN.md §13, In the
// web app). /p/<name>/reviews lists the reviews and, below them, the
// conversations of the whole paper in file order, so the authors work through
// them in one place. Single comments live there too. /reviews/<id>[/files|/changes]
// is one review (review.js).
//
// A link to /reviews#thread-<id> (a mail about a single comment) scrolls to
// that conversation and marks it, also when its state is not the one listed.

import { api, enc, projectUrl, request } from "../api.js";
import { h, append, clear, plural } from "../dom.js";
import { icon } from "../icons.js";
import { navigate, onLeave } from "../router.js";
import { session } from "../session.js";
import { timeAgo } from "../time.js";
import { blankslate, busy, counter, flash, flashError, openDialog, queueFlash, setTitle } from "../ui.js";
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
  if (ctx.sub[1]) return reviewPage.render(ctx);
  const p = ctx.project;
  setTitle("Reviews", p.name);
  const perms = permissions(p);
  const state = FILTERS.some(([k]) => k === ctx.query.get("state")) ? ctx.query.get("state") : "open";

  const [reviews, threads] = await Promise.all([
    api.get(projectUrl(ctx.ref, "/reviews")),
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

  const head = h("div", { class: "subhead" }, h("h2", {}, "Reviews ", counter(reviews.length)), action);
  const intro = reviews.length ? null : h("div", { class: "box mb-4" }, blankslate({
    iconName: "comment",
    title: "No reviews yet",
    text: perms.comment
      ? "A review comments on a fixed state of the paper, a version or the text as it is now. The authors reply, change the text and resolve each comment. You see how it changed and accept it."
      : "Reviewers, editors and the owner can review the paper. Their reviews show up here.",
    action: action && !mine ? h("button", { type: "button", class: "btn btn-primary", text: "Start the first review", onclick: () => startReviewDialog(ctx) }) : null,
  }));
  const list = reviews.length ? h("ul", { class: "box box-list mb-4 rv-review-list", "aria-label": "Reviews" }, reviews.map((r) => reviewRow(r, ctx))) : null;

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

// --- Starting a review ------------------------------------------------------------

// The choice of the state to review: the text as it is now, the last snapshot
// or a version. The review opens on its Files view, ready for comments.
export function startReviewDialog(ctx, { preselect = null } = {}) {
  const p = ctx.project;
  const versions = [...(p.versions || [])].sort((a, b) => (b.number || 0) - (a.number || 0));
  const name = "rv-start";
  const choice = (value, title, text, checked) => h("label", { class: "choice" },
    h("input", { type: "radio", name, value, checked }),
    h("div", {}, h("strong", { text: title }), h("span", { text })));
  const first = preselect || (versions[0] ? versions[0].tag : "live");
  const options = [
    choice("live", "The current state", "A snapshot of the live text, taken now.", first === "live"),
    choice("HEAD", "The last snapshot", "The newest commit in the history.", first === "HEAD"),
    ...versions.map((v) => choice(v.tag, v.label, `Version ${v.number}, tag ${v.tag}`, first === v.tag)),
  ];
  const start = h("button", { type: "button", class: "btn btn-primary", text: "Start review" });
  const d = openDialog({
    title: "Start a review",
    body: [
      h("p", { text: "Comments are made on one fixed state of the paper. Pick it here. Your comments stay pending, and only you see them, until you finish the review." }),
      h("fieldset", { class: "form-group mb-0" }, h("legend", { class: "sr-only", text: "The state to review" }), options),
    ],
    footer: [h("button", { type: "button", class: "btn", text: "Cancel", onclick: () => d.close() }), start],
  });
  start.addEventListener("click", async () => {
    const ref = d.el.querySelector(`input[name="${name}"]:checked`)?.value || "HEAD";
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

