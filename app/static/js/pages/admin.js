// /admin is the instance dashboard (DESIGN §18), for the accounts that
// INSTANCE_ADMINS names. The route answers everybody else with 404, so they
// see this app's "This page does not exist". The tiles are the instance now.
// The period switch scopes the charts under it, and the accounts close the page.

import { api } from "../api.js";
import { h, clear, formatBytes } from "../dom.js";
import { chartCard } from "../charts.js";
import { MONTHS, formatDay, formatInMyZone } from "../time.js";
import { avatar, counter, flashError, setTitle } from "../ui.js";
import { onLeave, setQuery } from "../router.js";

const PERIODS = [["30d", "30 days"], ["90d", "90 days"], ["12m", "12 months"]];
const IN_PERIOD = { "30d": "in the last 30 days", "90d": "in the last 13 weeks", "12m": "in the last 12 months" };
const EACH = { day: "day", week: "week", month: "month" };
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

// One small chart per measure: its series key, title, and what it counts.
// Active people is distinct per bucket, so its caption has no total.
const MEASURES = [
  ["active", "Active people", null],
  ["accounts", "New accounts", "first sign-ins"],
  ["papers", "New papers", null],
  ["snapshots", "Snapshots", "history commits of the live text"],
  ["compiles", "Compiles", null],
  ["comments", "Comments", "on the text and in reviews"],
  ["reviews", "Reviews submitted", null],
  ["versions", "Versions", "submissions, revisions, camera-ready"],
];

function utcDay(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}

// "29 Oct" on the axis, "Thu 29 Oct 2026" in the tooltip and the table.
function bucketLabels(buckets, unit) {
  return buckets.map((iso) => {
    const d = utcDay(iso);
    const month = MONTHS[d.getUTCMonth()];
    const day = `${d.getUTCDate()} ${month}`;
    if (unit === "month") return { axis: month, long: `${month} ${d.getUTCFullYear()}` };
    if (unit === "week") return { axis: day, long: `Week of ${day} ${d.getUTCFullYear()}` };
    return { axis: day, long: `${WEEKDAYS[d.getUTCDay()]} ${day} ${d.getUTCFullYear()}` };
  });
}

function dayText(iso) {
  const d = utcDay(iso);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

const num = (n) => n.toLocaleString("en");

function tile(label, value, sub) {
  return h("div", { class: "box stat-tile" },
    h("div", { class: "stat-label", text: label }),
    h("div", { class: "stat-value", text: value }),
    sub ? h("div", { class: "stat-sub", text: sub }) : null);
}

function tiles(d) {
  const t = d.totals;
  const st = d.storage;
  return h("div", { class: "stat-row" },
    tile("Accounts", num(t.accounts),
      t.waiting ? `${num(t.signed_in)} signed in, ${num(t.waiting)} invited and not yet` : "All of them have signed in"),
    tile("Active in the last 7 days", num(t.active_7d), `${num(t.active_30d)} in the last 30 days`),
    tile("Papers", num(t.papers), `${num(t.accepted)} accepted, ${num(t.planned)} planned`),
    tile("Storage", formatBytes(st.database_bytes + st.files_bytes),
      `Database ${formatBytes(st.database_bytes)}, files ${formatBytes(st.files_bytes)}`));
}

function activityCharts(d, charts) {
  const labels = bucketLabels(d.buckets, d.unit);
  const each = EACH[d.unit];
  const growth = chartCard({
    title: "Accounts and papers",
    caption: `Signed-in accounts and papers on the instance at the end of each ${each}`,
    labels,
    kind: "lines",
    height: 220,
    series: [{ name: "Accounts", values: d.growth.accounts }, { name: "Papers", values: d.growth.papers }],
  });
  const small = MEASURES.map(([key, title, what]) => {
    const values = d.series[key];
    const total = values.reduce((a, b) => a + b, 0);
    const caption = key === "active"
      ? `Accounts that used meertex, each once per ${each}`
      : `${num(total)} ${IN_PERIOD[d.period]}${what ? `, ${what}` : ""}`;
    return chartCard({ title, caption, labels, series: [{ name: title, values }] });
  });
  charts.push(growth, ...small);
  return [growth.node, h("div", { class: "chart-grid" }, small.map((c) => c.node))];
}

function accountsBox(d) {
  const rows = d.accounts.map((a) => h("tr", {},
    h("td", {}, h("div", { class: "stats-person" },
      avatar(a, 20),
      h("div", { class: "min-w-0" },
        h("div", { class: "truncate", text: a.name || a.email }),
        a.name ? h("div", { class: "color-muted text-small truncate", text: a.email }) : null))),
    h("td", {}, a.first_sign_in_at
      ? h("span", { title: formatInMyZone(a.first_sign_in_at), text: formatDay(a.first_sign_in_at) })
      : h("span", { class: "color-muted", text: "Invited, not signed in yet" })),
    h("td", {}, a.last_active ? dayText(a.last_active) : h("span", { class: "color-muted", text: "Never" })),
    h("td", { class: "num", text: num(a.papers) }),
    h("td", {}, a.invited_by
      ? h("span", { title: a.invited_by.email, text: a.invited_by.name || a.invited_by.email })
      : h("span", { class: "color-muted", text: "Signed up on their own" }))));
  const more = d.accounts_total > d.accounts.length
    ? h("div", { class: "box-footer color-muted text-small", text: `The newest ${num(d.accounts.length)} of ${num(d.accounts_total)} accounts.` })
    : null;
  return h("div", { class: "box stats-box" },
    h("div", { class: "stats-scroll" },
      h("table", { class: "stats-table" },
        h("thead", {}, h("tr", {},
          h("th", { scope: "col", text: "Account" }),
          h("th", { scope: "col", text: "Joined" }),
          h("th", { scope: "col", text: "Last active" }),
          h("th", { scope: "col", class: "num", text: "Papers" }),
          h("th", { scope: "col", text: "Invited by" }))),
        h("tbody", {}, rows))),
    more);
}

export async function render(ctx) {
  setTitle("Instance");
  const asked = ctx.query.get("period");
  let period = PERIODS.some(([p]) => p === asked) ? asked : "30d";
  const d = await api.get("/api/admin/instance", { query: { period } });
  if (!ctx.alive()) return;

  const charts = [];
  const destroy = () => charts.splice(0).forEach((c) => c.destroy());
  onLeave(destroy);

  const activity = h("div", { class: "stats-activity" }, activityCharts(d, charts));
  const buttons = PERIODS.map(([p, text]) => h("button", { type: "button", "aria-pressed": String(p === period), text, dataset: { period: p } }));
  const switcher = h("div", { class: "segmented", role: "group", "aria-label": "Period" }, buttons);
  switcher.addEventListener("click", async (e) => {
    const choice = e.target.closest("button")?.dataset.period;
    if (!choice || choice === period) return;
    period = choice;
    buttons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.period === period)));
    setQuery({ period: period === "30d" ? null : period });
    // The charts stay, faded, until the new numbers are in.
    activity.classList.add("is-loading");
    try {
      const next = await api.get("/api/admin/instance", { query: { period } });
      if (!ctx.alive() || next.period !== period) return;
      destroy();
      clear(activity).append(...activityCharts(next, charts));
    } catch (err) {
      flashError(err);
    } finally {
      activity.classList.remove("is-loading");
    }
  });

  const host = new URL(d.base_url).host;
  clear(ctx.main).append(h("div", { class: "container page" },
    h("div", { class: "page-head" },
      h("div", { class: "flex-1" },
        h("h1", { text: "Instance" }),
        h("p", { class: "page-lede", text: `${host}, as of ${formatInMyZone(d.generated_at)}` }))),
    tiles(d),
    h("div", { class: "subhead" }, h("h2", { text: "Activity" })),
    h("div", { class: "stats-filters" }, switcher),
    activity,
    h("div", { class: "subhead" }, h("h2", {}, "Accounts ", counter(d.accounts_total))),
    accountsBox(d)));
}
