// Dates as a venue states them, and countdowns.
//
// A deadline is shown in the venue's time zone with the zone's name
// ("15 Jan 2027, 23:59 AoE"), because that is how the CFP says it and what
// people compare against. The reader's own clock goes into the tooltip.

import { h } from "./dom.js";
import { myTimezone } from "./session.js";

export const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const FIXED = /^(?:UTC|GMT)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$/i;

// Minutes east of UTC for AoE, UTC and fixed offsets. Null for an IANA zone,
// which needs the browser's zone database.
export function tzOffsetMinutes(tz) {
  const t = String(tz || "AoE").trim();
  if (t.toLowerCase() === "aoe") return -12 * 60;
  if (/^(utc|gmt|z)$/i.test(t)) return 0;
  const m = FIXED.exec(t);
  if (m) {
    const v = Number(m[2]) * 60 + Number(m[3] || 0);
    return m[1] === "-" ? -v : v;
  }
  return null;
}

export function isValidTz(tz) {
  if (!tz) return false;
  if (tzOffsetMinutes(tz) !== null) return true;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: tz });
    return true;
  } catch {
    return false;
  }
}

const formatters = new Map();
function zoneFormatter(tz) {
  if (!formatters.has(tz)) {
    let f = null;
    try {
      f = new Intl.DateTimeFormat("en-US", {
        timeZone: tz, year: "numeric", month: "numeric", day: "numeric",
        hour: "numeric", minute: "numeric", weekday: "short", hourCycle: "h23",
      });
    } catch {
      f = null;
    }
    formatters.set(tz, f);
  }
  return formatters.get(tz);
}

// The wall-clock parts of an instant in a zone.
export function partsIn(when, tz) {
  const d = when instanceof Date ? when : new Date(when);
  const off = tzOffsetMinutes(tz);
  const f = off === null ? zoneFormatter(tz) : null;
  if (off !== null || !f) {
    const s = new Date(d.getTime() + (off || 0) * 60000);
    return { y: s.getUTCFullYear(), mo: s.getUTCMonth(), d: s.getUTCDate(), h: s.getUTCHours(), mi: s.getUTCMinutes(), wd: s.getUTCDay() };
  }
  const p = {};
  for (const { type, value } of f.formatToParts(d)) p[type] = value;
  return { y: +p.year, mo: +p.month - 1, d: +p.day, h: +p.hour % 24, mi: +p.minute, wd: WEEKDAYS.indexOf(p.weekday) };
}

const pad = (n) => String(n).padStart(2, "0");
const LOCAL = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/;

// A date's wall clock in the venue's zone. The server writes it as `local`,
// which is taken as written. Only without it does the browser work it out.
export function venueParts(date) {
  const m = LOCAL.exec(date?.local || "");
  if (m) return { y: +m[1], mo: +m[2] - 1, d: +m[3], h: +m[4], mi: +m[5] };
  return partsIn(date.due_at, date.tz || "AoE");
}

export function dayText(p, { year = true } = {}) {
  return year ? `${p.d} ${MONTHS[p.mo]} ${p.y}` : `${p.d} ${MONTHS[p.mo]}`;
}

export function timeText(p) {
  return `${pad(p.h)}:${pad(p.mi)}`;
}

// "15 Jan 2027, 23:59 AoE", or "15 Jan 2027" for a date the venue gave as a day.
export function formatDue(date) {
  if (!date?.due_at) return "";
  const tz = date.tz || "AoE";
  const p = venueParts(date);
  if (date.date_only) return dayText(p);
  return `${dayText(p)}, ${timeText(p)} ${tz}`;
}

// A short form for "moved from 15 Jan". The year only when it differs from the
// date it is compared with, the time only when the day alone would hide the move.
export function formatShort(iso, tz, { compareTo = null, dateOnly = false } = {}) {
  const p = partsIn(iso, tz);
  const q = compareTo ? partsIn(compareTo, tz) : null;
  const sameDay = q && q.y === p.y && q.mo === p.mo && q.d === p.d;
  let s = dayText(p, { year: !q || q.y !== p.y });
  if (!dateOnly && sameDay) s += `, ${timeText(p)}`;
  return s;
}

// The date and time inputs' values for an existing date, in its own zone.
export function inputValues(date) {
  const p = venueParts(date);
  return {
    date: `${p.y}-${pad(p.mo + 1)}-${pad(p.d)}`,
    time: date.date_only ? "" : timeText(p),
  };
}

// The instant on the reader's own clock, for tooltips.
export function formatInMyZone(iso, { time = true } = {}) {
  if (!iso) return "";
  const tz = myTimezone();
  try {
    return new Intl.DateTimeFormat("en-GB", {
      timeZone: tz, weekday: "short", day: "numeric", month: "short", year: "numeric",
      ...(time ? { hour: "2-digit", minute: "2-digit", timeZoneName: "short" } : {}),
    }).format(new Date(iso));
  } catch {
    return new Date(iso).toUTCString();
  }
}

export function formatDay(iso, tz = myTimezone()) {
  return dayText(partsIn(iso, tz));
}

export function secondsUntil(iso) {
  return (Date.parse(iso) - Date.now()) / 1000;
}

// "in 3 hours", "in 12 days", "in 7 weeks", "2 days ago". Whole units are
// rounded down, so "in 2 days" never promises more time than is left.
export function relative(seconds) {
  const past = seconds < 0;
  const a = Math.abs(seconds);
  if (a < 45) return past ? "just now" : "in less than a minute";
  let n;
  let unit;
  if (a < 3600) [n, unit] = [Math.max(1, Math.floor(a / 60)), "minute"];
  else if (a < 48 * 3600) [n, unit] = [Math.floor(a / 3600), "hour"];
  else if (a < 31 * 86400) [n, unit] = [Math.floor(a / 86400), "day"];
  else if (a < 13 * 7 * 86400) [n, unit] = [Math.floor(a / (7 * 86400)), "week"];
  else if (a < 730 * 86400) [n, unit] = [Math.floor(a / (30.44 * 86400)), "month"];
  else [n, unit] = [Math.floor(a / (365.25 * 86400)), "year"];
  const s = `${n} ${unit}${n === 1 ? "" : "s"}`;
  return past ? `${s} ago` : `in ${s}`;
}

// Red under 3 days, amber under 14 days.
export function urgency(seconds) {
  if (seconds < 0) return "past";
  if (seconds < 3 * 86400) return "danger";
  if (seconds < 14 * 86400) return "attention";
  return "normal";
}

// A live countdown to a date. The ticker below keeps its text current.
export function countdown(due_at, { cls = "" } = {}) {
  const el = h("span", { class: `countdown ${cls}`.trim(), dataset: { countdown: due_at } });
  paintCountdown(el);
  return el;
}

function paintCountdown(el) {
  const s = secondsUntil(el.dataset.countdown);
  el.textContent = relative(s);
  el.classList.remove("is-danger", "is-attention", "is-normal", "is-past");
  el.classList.add(`is-${urgency(s)}`);
}

// "3 hours ago" with the full time in the tooltip, like GitHub's relative-time.
export function timeAgo(iso) {
  if (!iso) return h("span", { text: "" });
  const el = h("time", { datetime: iso, title: formatInMyZone(iso), dataset: { ago: iso } });
  el.textContent = relative(secondsUntil(iso));
  return el;
}

// One timer for the whole page instead of one per element.
setInterval(() => {
  document.querySelectorAll("[data-countdown]").forEach(paintCountdown);
  document.querySelectorAll("[data-ago]").forEach((el) => {
    el.textContent = relative(secondsUntil(el.dataset.ago));
  });
}, 30000);

// A short list of common zones, for browsers that cannot list them all.
export const TZ_SUGGESTIONS = [
  "AoE", "UTC",
  "UTC-12", "UTC-11", "UTC-10", "UTC-9", "UTC-8", "UTC-7", "UTC-6", "UTC-5", "UTC-4", "UTC-3", "UTC-2", "UTC-1",
  "UTC+1", "UTC+2", "UTC+3", "UTC+4", "UTC+5", "UTC+5:30", "UTC+6", "UTC+7", "UTC+8", "UTC+9", "UTC+10", "UTC+11", "UTC+12", "UTC+13", "UTC+14",
  "Europe/Berlin", "Europe/London", "Europe/Paris", "Europe/Lisbon", "Europe/Zurich", "Europe/Helsinki",
  "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "America/Sao_Paulo", "America/Toronto",
  "Asia/Tokyo", "Asia/Shanghai", "Asia/Singapore", "Asia/Kolkata", "Asia/Seoul", "Asia/Dubai",
  "Australia/Sydney", "Pacific/Auckland",
];

export function allTimezones() {
  try {
    if (Intl.supportedValuesOf) return Intl.supportedValuesOf("timeZone");
  } catch {
    // Older browsers get the suggestions only, which is enough.
  }
  return TZ_SUGGESTIONS.filter((t) => t.includes("/"));
}
