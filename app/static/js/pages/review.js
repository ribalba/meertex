// One review, like a GitHub pull request's review (docs/DESIGN.md §13).
// /p/<name>/reviews/<id> is the Conversation: the summary and every thread
// with its snippet, comments, suggestions, resolution and buttons.
// /reviews/<id>/files is the source at the review's commit with the threads
// under their lines, a comment box on selected text and the PDF of that
// commit (review-files.js). /reviews/<id>/changes is the diff from the
// review's commit to the live text, and its latexdiff PDF.
//
// Each view has its own URL, so Back and Forward move between them.

import { api, enc, projectUrl } from "../api.js";
import { h, clear, plural } from "../dom.js";
import { icon } from "../icons.js";
import { navigate, onLeave } from "../router.js";
import { renderMarkdown } from "../markdown.js";
import { renderDiff } from "../diff.js";
import { session } from "../session.js";
import { timeAgo, formatInMyZone } from "../time.js";
import {
  avatar, blankslate, busy, confirmDialog, counter, flash, flashBox, flashError, loading, openDialog,
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
      text: "It was discarded, or it is somebody's review that is not submitted yet. Only its reviewer sees a pending review.",
      action: h("a", { class: "btn", href: `${ctx.base}/reviews` }, icon("arrow-left"), "All reviews"),
    })));
    return;
  }
  if (!ctx.alive()) return;

  const perms = permissions(p);
  const rbase = `${ctx.base}/reviews/${enc(review.id)}`;
  const mine = review.reviewer?.id === session.me?.id;
  const finishing = mine && review.state === "pending";
  setTitle(view === "files" ? "Files" : view === "changes" ? "Changes" : "Conversation", reviewTitle(review), p.name);

  const head = reviewHeader(ctx, review, { finishing });
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

  const rctx = { ...ctx, review, perms, rbase, finishing, setCounts, main: body };
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

function reviewHeader(ctx, review, { finishing }) {
  const counts = h("div", { class: "rv-counts text-small" });
  const setCounts = (c) => {
    clear(counts).append(
      h("span", {}, icon("comment", { size: 14 }), ` ${c.open || 0} open`),
      h("span", { text: `${c.resolved || 0} resolved` }),
      h("span", { text: `${c.accepted || 0} accepted` }));
  };
  setCounts(review.counts || {});
  const finish = finishing
    ? h("button", { type: "button", class: "btn btn-primary rv-finish" }, "Finish your review", icon("triangle-down"))
    : null;
  finish?.addEventListener("click", () => finishDialog(ctx, review));
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
        finish)),
    counts);
  return { el, setCounts };
}

function finishDialog(ctx, review) {
  const summary = h("textarea", { class: "form-control input-block", rows: "5", placeholder: "Leave a comment", "aria-label": "Summary" });
  summary.value = review.summary || "";
  const name = "rv-verdict";
  const choice = (value, title, text) => h("label", { class: "choice" },
    h("input", { type: "radio", name, value, checked: value === "comment" }),
    h("div", {}, h("strong", { text: title }), h("span", { text })));
  const submit = h("button", { type: "button", class: "btn btn-primary", text: "Submit review" });
  const discard = h("button", { type: "button", class: "btn btn-danger rv-discard", text: "Discard review" });
  const error = h("div", { class: "form-error", role: "alert", hidden: true });
  const n = review.threads.length;
  const d = openDialog({
    title: "Finish your review",
    wide: true,
    body: [
      h("p", { class: "color-muted", text: n
        ? `Submitting shows your ${plural(n, "comment")} to the authors and mails the owner and the editors.`
        : "You have not commented on the text. You can still submit a summary and a verdict." }),
      summary,
      h("fieldset", { class: "form-group mt-3 mb-0" },
        h("legend", { class: "sr-only", text: "Verdict" }),
        choice("comment", "Comment", "Feedback without a verdict."),
        choice("approve", "Approve", "The paper is fine as it is."),
        choice("changes_requested", "Request changes", "Things must change before you are content.")),
      error,
    ],
    footer: [discard, h("span", { class: "flex-1" }),
      h("button", { type: "button", class: "btn", text: "Cancel", onclick: () => d.close() }), submit],
  });
  submit.addEventListener("click", async () => {
    const verdict = d.el.querySelector(`input[name="${name}"]:checked`)?.value || "comment";
    try {
      await busy(submit, () => api.post(projectUrl(ctx.ref, `/reviews/${enc(review.id)}/submit`), { verdict, summary: summary.value }));
      d.close();
      refreshOpenCounts(ctx.project.id);
      flash(`Submitted your review (${VERDICTS[verdict].text.toLowerCase()}).`, "success");
      ctx.reload();
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    }
  });
  discard.addEventListener("click", async () => {
    d.close();
    const ok = await confirmDialog({
      title: "Discard your review?",
      message: n ? `Your ${plural(n, "pending comment")} go with it. Nobody else has seen them.` : "Nobody else has seen it.",
      confirmText: "Discard review",
      danger: true,
    });
    if (!ok) return;
    try {
      await api.del(projectUrl(ctx.ref, `/reviews/${enc(review.id)}`));
      queueFlash("Discarded your review.", "success");
      navigate(`${ctx.base}/reviews`);
    } catch (err) {
      flashError(err);
    }
  });
  summary.focus();
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
  if (ctx.finishing) {
    const finish = h("button", { type: "button", class: "btn btn-sm", text: "Finish your review", onclick: () => finishDialog(ctx, review) });
    parts.push(flashBox(h("div", {},
      h("p", { class: "mb-2" }, h("strong", { text: "This review is pending." }), " ",
        `Only you see it and its ${plural(threads.length, "comment")} until you finish it.`),
      h("div", { class: "flash-actions mt-0" }, finish,
        h("a", { class: "btn btn-sm", href: `${ctx.rbase}/files` }, icon("file-text"), "Comment on the files"))), "warn", { iconName: "clock" }));
  } else if (review.state === "pending") {
    parts.push(flashBox("This review is pending.", "warn"));
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
        ? h("div", { class: "markdown", html: renderMarkdown(review.summary) })
        : h("p", { class: "color-muted mb-0", text: "No summary." }))));
  }

  const cards = threads.map((t) => threadCard(t, env, { reviewCommit: review.commit, showReview: false }));
  parts.push(h("div", { class: "rv-section-head" }, h("h3", {}, "Comments ", counter(threads.length))));
  if (cards.length) parts.push(h("div", { class: "rv-thread-list" }, cards));
  else {
    parts.push(h("div", { class: "box" }, blankslate({
      iconName: "comment",
      title: "No comments on the text",
      text: ctx.finishing ? "Open Files, select text or click a line number, and comment." : "This review has no comments on the text.",
      action: ctx.finishing ? h("a", { class: "btn", href: `${ctx.rbase}/files` }, icon("file-text"), "Open the files") : null,
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
