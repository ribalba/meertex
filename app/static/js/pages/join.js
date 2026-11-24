// /join/<token>: a paper's share link (DESIGN §2, Sharing a paper with a
// link). A signed-out visitor signs in first and comes back here. A member
// goes straight to the paper. Everyone else sees which paper it is and joins
// with one click.

import { api, enc } from "../api.js";
import { h, clear } from "../dom.js";
import { navigate } from "../router.js";
import { session } from "../session.js";
import { blankslate, busy, flashBox, flashError, loading, queueFlash, setTitle } from "../ui.js";

const ROLE_TEXT = {
  editor: "You can edit the files and details, answer questions and save versions.",
  reviewer: "You can read everything and review it.",
  viewer: "You can read everything.",
};

export async function render(ctx) {
  setTitle("Join a paper");
  const token = ctx.params[0];
  clear(ctx.main).append(loading());
  let data;
  try {
    data = await api.get(`/api/join/${enc(token)}`);
  } catch (err) {
    if (!ctx.alive()) return;
    clear(ctx.main).append(h("div", { class: "container container-sm page" }, err.status === 404
      ? blankslate({
        iconName: "link",
        title: "This link does not work",
        text: err.message,
        action: h("a", { class: "btn", href: "/", text: "Go to your papers" }),
      })
      : flashBox(err.message, "error")));
    return;
  }
  if (!ctx.alive()) return;
  const p = data.project;
  if (data.member_role) {
    navigate(`/p/${enc(p.id)}`, { replace: true });
    return;
  }

  const title = p.title || p.name;
  setTitle("Join", title);
  const by = data.shared_by ? data.shared_by.name || data.shared_by.email : null;
  const role = data.role;
  const join = h("button", { type: "button", class: "btn btn-primary", text: `Join as ${role}` });
  join.addEventListener("click", async () => {
    try {
      const r = await busy(join, () => api.post(`/api/join/${enc(token)}`));
      queueFlash(r.joined ? `You joined ${title} as ${r.role}.` : `You are already on ${title}.`, "success");
      navigate(`/p/${enc(r.project_id)}`, { replace: true });
    } catch (err) {
      flashError(err);
    }
  });

  clear(ctx.main).append(h("div", { class: "container container-sm page" },
    h("div", { class: "box" },
      h("div", { class: "box-header" }, h("h2", { class: "box-title", text: title })),
      h("div", { class: "box-body" },
        h("p", {}, by ? `${by} shares this paper with you as ${role}.` : `This link adds you to the paper as ${role}.`,
          " ", ROLE_TEXT[role] || ""),
        h("p", { class: "color-muted" }, `You join as ${session.me?.email || "the account you are signed in with"}.`),
        h("div", { class: "flex" }, join, h("a", { class: "btn", href: "/", text: "Not now" }))))));
}
