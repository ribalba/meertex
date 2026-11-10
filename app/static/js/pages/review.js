// One review, like a GitHub pull request's review (docs/DESIGN.md §13), but
// without the submit step. Everybody sees its comments as they are written.
// /p/<name>/reviews/<id> is the Conversation: every thread with its snippet,
// comments, suggestions, resolution and buttons, and the summary of a review
// submitted before reviews lost that step.
// /reviews/<id>/files is the source at the review's commit with the threads
// under their lines, a comment box on selected text and the PDF of that
// commit (review-files.js). /reviews/<id>/changes is the diff from the
// review's commit to the live text, and its latexdiff PDF.
//
// Each view has its own URL, so Back and Forward move between them.

import { api, enc, projectUrl } from "../api.js";
import { h, clear } from "../dom.js";
import { icon } from "../icons.js";
import { navigate, onLeave } from "../router.js";
import { renderMarkdown } from "../markdown.js";
import { renderDiff } from "../diff.js";
import { session } from "../session.js";
import { timeAgo, formatInMyZone } from "../time.js";
import {
  avatar, blankslate, busy, confirmDialog, counter, flashBox, flashError, loading,
  personName, queueFlash, setTitle, spinner,
} from "../ui.js";
import {
  FileTexts, VERDICTS, highlightHash, permissions, refreshOpenCounts, reviewBase, reviewTitle, threadCard, verdictLabel,
} from "../threads.js";
import { compileWait, pdfBox, renderFiles } from "./review-files.js";

const VIEWS = ["conversation", "files", "changes"];

export async function render(ctx) {
  const id = decodeURIComponent(ctx.sub[1]);
  const view = VIEWS.includes(ctx.sub[2]) ? ctx.sub[2] : "conversation";
  const p = ctx.project;
  let review;
  try {
    review = await api.get(projectUrl(ctx.ref, `/reviews/${enc(id)}`));
  } catch (err) {
    if (err.status !== 404 && err.status !== 422) throw err;
    setTitle("Review not found", p.name);
    clear(ctx.main).append(h("div", { class: "box" }, blankslate({
      iconName: "comment",
      title: "This review is not here",
      text: "Its reviewer discarded it, or the link is wrong.",
      action: h("a", { class: "btn", href: `${ctx.base}/reviews` }, icon("arrow-left"), "All reviews"),
    })));
    return;
  }
  if (!ctx.alive()) return;

  const perms = permissions(p);
  const rbase = `${ctx.base}/reviews/${enc(review.id)}`;
  // Your own open review, which takes your comments.
  const mine = review.reviewer?.id === session.me?.id && review.state === "open";
  setTitle(view === "files" ? "Files" : view === "changes" ? "Changes" : "Conversation", reviewTitle(review), p.name);

  const head = reviewHeader(ctx, review, { mine });
  const threadCount = counter(review.threads.length);
  const tab = (key, text, iconName, href, extra = null) => h("a", {
    class: "rv-tab", href, "aria-current": key === view ? "page" : null,
  }, icon(iconName), h("span", { text }), extra);
  const tabs = h("nav", { class: "rv-tabnav", "aria-label": "Review" },
    tab("conversation", "Conversation", "comment", rbase, threadCount),
    tab("files", "Files", "file-text", `${rbase}/files`),
    tab("changes", "Changes since this review", "arrow-switch", `${rbase}/changes`));
  const body = h("div", { class: "rv-view" });

  const setCounts = (threads) => {
    const own = threads.filter((t) => t.review_id === review.id);
    const counts = { open: 0, resolved: 0, accepted: 0 };
    own.forEach((t) => { counts[t.state] = (counts[t.state] || 0) + 1; });
    head.setCounts(counts);
    threadCount.textContent = String(own.length);
  };

  const rctx = { ...ctx, review, perms, rbase, mine, setCounts, main: body };
  if (view === "files") await renderFiles(rctx);
  else if (view === "changes") await renderChanges(rctx);
  else renderConversation(rctx);
  if (!ctx.alive()) return;
  if (view === "files") {
    // The source and the PDF take the full width below the paper's header,
    // like the editor. The review's header keeps the page's width.
    ctx.main.className = "rv-files-page";
    clear(ctx.main).append(h("div", { class: "rv-files-head" }, h("div", { class: "container" }, head.el, tabs)), body);
  } else {
    clear(ctx.main).append(head.el, tabs, body);
  }
}

// --- Header -----------------------------------------------------------------------

function reviewHeader(ctx, review, { mine }) {
  const counts = h("div", { class: "rv-counts text-small" });
  // A review started by mistake can go while it has no comments.
  const discard = mine
    ? h("button", { type: "button", class: "btn btn-sm rv-discard", title: "Throw this review away. It has no comments yet." }, icon("trash"), "Discard")
    : null;
  discard?.addEventListener("click", () => discardReview(ctx, review, discard));
  const setCounts = (c) => {
    clear(counts).append(
      h("span", {}, icon("comment", { size: 14 }), ` ${c.open || 0} open`),
      h("span", { text: `${c.resolved || 0} resolved` }),
      h("span", { text: `${c.accepted || 0} accepted` }));
    if (discard) discard.hidden = (c.open || 0) + (c.resolved || 0) + (c.accepted || 0) > 0;
  };
  setCounts(review.counts || {});
  const el = h("div", { class: "rv-head" },
    h("div", { class: "rv-head-top" },
      h("div", { class: "flex-1" },
        h("h2", { class: "rv-title" }, h("span", { text: reviewTitle(review) }), " ", verdictLabel(review)),
        h("div", { class: "rv-head-meta" },
          avatar(review.reviewer, 20),
          h("strong", { text: personName(review.reviewer) }),
          h("span", {}, " reviews ", reviewBase(review, ctx.base)),
          h("span", { class: "color-muted" }, " · started ", timeAgo(review.created_at)),
          review.submitted_at ? h("span", { class: "color-muted" }, " · submitted ", timeAgo(review.submitted_at)) : null)),
      h("div", { class: "rv-head-actions" },
        h("a", { class: "btn btn-sm", href: `${ctx.base}/reviews` }, icon("arrow-left"), "All reviews"),
        discard)),
    counts);
  return { el, setCounts };
}

async function discardReview(ctx, review, button) {
  const ok = await confirmDialog({
    title: "Discard your review?",
    message: "It has no comments yet, so nothing else goes with it.",
    confirmText: "Discard review",
    danger: true,
  });
  if (!ok) return;
  try {
    await busy(button, () => api.del(projectUrl(ctx.ref, `/reviews/${enc(review.id)}`)));
    queueFlash("Discarded your review.", "success");
    navigate(`${ctx.base}/reviews`);
  } catch (err) {
    flashError(err);
  }
}

// --- Conversation -------------------------------------------------------------------

function renderConversation(ctx) {
  const { review, perms } = ctx;
  const p = ctx.project;
  const threads = [...review.threads];
  const env = {
    project: p,
    base: ctx.base,
    perms,
    texts: new FileTexts(p.id, "live"),
    reviews: new Map([[review.id, review]]),
    onChange: (next, old) => {
      const i = threads.findIndex((t) => t.id === old.id);
      if (next) threads[i] = next;
      else threads.splice(i, 1);
      ctx.setCounts(threads);
      refreshOpenCounts(p.id);
    },
  };

  const parts = [];
  if (ctx.mine) {
    parts.push(flashBox(h("div", {},
      h("p", { class: "mb-2" }, h("strong", { text: "This is your review." }), " ",
        "Everybody on the paper sees each comment as soon as you add it. There is nothing to submit at the end."),
      h("div", { class: "flash-actions mt-0" },
        h("a", { class: "btn btn-sm", href: `${ctx.rbase}/files` }, icon("file-text"), "Comment on the files"))), "info"));
  }

  if (review.state === "submitted") {
    const v = VERDICTS[review.verdict] || VERDICTS.comment;
    parts.push(h("div", { class: `box rv-summary is-${review.verdict}` },
      h("div", { class: "rv-summary-head" },
        h("span", { class: "rv-summary-badge" }, icon(v.icon, { cls: v.color })),
        avatar(review.reviewer, 20),
        h("strong", { text: personName(review.reviewer) }),
        h("span", { text: ` ${v.says}` }),
        h("span", { class: "color-muted", title: formatInMyZone(review.submitted_at) }, timeAgo(review.submitted_at))),
      h("div", { class: "rv-summary-body" }, review.summary
        ? h("div", { class: "markdown", html: renderMarkdown(review.summary, { mentions: review.mentions }) })
        : h("p", { class: "color-muted mb-0", text: "No summary." }))));
  }

  const cards = threads.map((t) => threadCard(t, env, { reviewCommit: review.commit, showReview: false }));
  parts.push(h("div", { class: "rv-section-head" }, h("h3", {}, "Comments ", counter(threads.length))));
  if (cards.length) parts.push(h("div", { class: "rv-thread-list" }, cards));
  else {
    parts.push(h("div", { class: "box" }, blankslate({
      iconName: "comment",
      title: "No comments on the text",
      text: ctx.mine ? "Open Files, select text or click a line number, and comment." : "This review has no comments on the text yet.",
      action: ctx.mine ? h("a", { class: "btn", href: `${ctx.rbase}/files` }, icon("file-text"), "Open the files") : null,
    })));
  }
  clear(ctx.main).append(...parts);

  highlightHash(ctx.main);
  const onHash = () => highlightHash(ctx.main)?.scrollIntoView({ block: "start" });
  window.addEventListener("hashchange", onHash);
  onLeave(() => window.removeEventListener("hashchange", onHash));
}

// --- Changes since this review --------------------------------------------------------

async function renderChanges(ctx) {
  const { review } = ctx;
  const word = ctx.query.get("word") !== "0";
  const go = (w) => navigate(`${ctx.rbase}/changes${w ? "" : "?word=0"}`, { replace: true, keepScroll: true });
  const words = h("button", { type: "button", "aria-pressed": String(word), text: "Words", onclick: () => go(true) });
  const lines = h("button", { type: "button", "aria-pressed": String(!word), text: "Lines", onclick: () => go(false) });
  const pdfButton = h("button", { type: "button", class: "btn btn-sm rv-pdfdiff", "aria-pressed": "false", title: "latexdiff marks what changed, and the result is compiled" },
    icon("pdf"), "PDF of the changes");
  const pdfHost = h("div", { class: "rv-pdfdiff-host", hidden: true });
  const bar = h("div", { class: "range-bar" },
    h("span", {}, "From ", reviewBase(review, ctx.base), " to ", h("strong", { text: "the live text" })),
    h("span", { class: "flex-1" }),
    h("div", { class: "segmented", role: "group", "aria-label": "Diff mode" }, words, lines),
    pdfButton);

  let view = null;
  pdfButton.addEventListener("click", () => {
    const show = pdfHost.hidden;
    pdfHost.hidden = !show;
    pdfButton.setAttribute("aria-pressed", String(show));
    if (show && !pdfHost.firstChild) pdfDiff(ctx, pdfHost).then((v) => { view = v; });
  });
  onLeave(() => view?.destroy());

  const host = h("div", {}, loading());
  clear(ctx.main).append(bar,
    h("p", { class: "text-small color-muted", text: "Everything that changed in the files since the state this review is on." }),
    pdfHost, host);
  try {
    const diff = await api.get(projectUrl(ctx.ref, `/reviews/${enc(review.id)}/changes`), { query: { word: word ? "true" : "false" } });
    if (!ctx.alive()) return;
    clear(host).append(renderDiff(diff, { word }));
  } catch (err) {
    clear(host).append(h("div", { class: "box" }, blankslate({ iconName: "alert", title: "Could not compare", text: err.message })));
  }
}

async function pdfDiff(ctx, host) {
  const { review } = ctx;
  clear(host).append(h("div", { class: "box mb-4" }, h("div", { class: "blankslate" },
    spinner(true), h("h3", { text: "Making the PDF of the changes" }),
    h("p", { text: "latexdiff compares the review's state with the live text, and the result is compiled. Big papers take a while." }))));
  let c;
  try {
    c = await compileWait(ctx, "/pdfdiff", { from: review.commit, to: "live" });
  } catch (err) {
    if (!ctx.alive()) return null;
    clear(host).append(h("div", { class: "box mb-4" }, blankslate({
      iconName: err.status === 503 ? "info" : "alert",
      title: err.status === 503 ? "No compiling here" : "The PDF could not be made",
      text: err.status === 503 ? "This server has no compile service. Ask whoever runs it to set COMPILE_URL." : err.message,
    })));
    return null;
  }
  if (!c || !ctx.alive()) return null;
  if (!c.pdf_url) {
    const problems = (c.problems || []).filter((x) => x.level === "error");
    clear(host).append(h("div", { class: "box mb-4" }, blankslate({
      iconName: "alert",
      title: c.status === "timeout" ? "latexdiff took too long" : "latexdiff made no PDF",
      text: problems.length ? problems.slice(0, 3).map((x) => `${x.path ? `${x.path}:${x.line} ` : ""}${x.message}`).join(" ") : "The log says why.",
      action: c.log_url ? h("a", { class: "btn", href: c.log_url, target: "_blank", rel: "noopener" }, "Open the log") : null,
    })));
    return null;
  }
  const box = h("div", { class: "mb-4" });
  clear(host).append(c.status === "errors"
    ? h("p", { class: "text-small color-attention", text: "LaTeX reported errors, so parts of the PDF may be off." }) : "", box);
  return pdfBox(box, {
    title: "PDF of the changes since this review",
    url: c.pdf_url,
    name: `${ctx.project.name}-review-changes.pdf`,
  });
}
