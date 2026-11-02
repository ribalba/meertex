// Charts for the instance dashboard (DESIGN §18): columns for one series per
// bucket, lines for a few series side by side. Each one is drawn as SVG at
// the width its card has, and again when that changes. Thin marks, hairline
// grid, a tooltip on hover and on the arrow keys, and a table for every chart,
// so no number is only in a picture.

import { h, clear } from "./dom.js";

const SVG_NS = "http://www.w3.org/2000/svg";

// Categorical slots 1 and 2 of the dataviz reference palette, checked on
// white (colour-blind separation and 3:1 contrast). A single series is slot 1.
export const SERIES_COLORS = ["#2a78d6", "#eb6834"];
const GRID = "#e6eaef";
const AXIS = "#d1d9e0";
const INK = "#59636e";
const BAND = "#f6f8fa";

const MARGIN = { top: 8, right: 8, bottom: 22, left: 36 };
const BAR_MAX = 24;
const BAR_GAP = 2;
const LABEL_GAP = 64;

function s(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) node.setAttribute(k, v);
  for (const c of children) if (c !== null && c !== undefined) node.append(c);
  return node;
}

// 0 / 950 / 1.2K / 12K / 1.2M
export function compact(n) {
  if (Math.abs(n) < 1000) return String(n);
  const [d, unit] = Math.abs(n) < 1e6 ? [1e3, "K"] : [1e6, "M"];
  const v = n / d;
  return `${v < 10 && v % 1 ? v.toFixed(1) : Math.round(v)}${unit}`;
}

// Clean tick steps (1, 2, 5 × 10^k) from 0 to the first step at or over max.
function ticks(max) {
  if (max <= 0) return [0, 1];
  if (max <= 4) return Array.from({ length: max + 1 }, (_, i) => i);
  const raw = max / 3;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * pow).find((x) => x >= raw);
  const top = Math.ceil(max / step) * step;
  return Array.from({ length: top / step + 1 }, (_, i) => i * step);
}

// Which buckets get an x label: evenly spaced, counted back from the newest.
function labelled(n, width) {
  const every = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(width / LABEL_GAP))));
  return new Set(Array.from({ length: n }, (_, i) => i).filter((i) => (n - 1 - i) % every === 0));
}

function roundedTop(x, y, w, y0) {
  const r = Math.min(4, w / 2, y0 - y);
  return `M${x},${y0}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y0}Z`;
}

// A card with the chart, its title and caption, and a switch to a table of
// the same numbers. ``labels`` are [{axis, long}] per bucket, ``series``
// [{name, values}]. ``kind`` is "bars" (one series) or "lines".
export function chartCard({ title, caption = null, labels, series, kind = "bars", height = 140, empty = "None in this period" }) {
  const colored = series.map((x, i) => ({ ...x, color: x.color || SERIES_COLORS[i] }));
  const plot = h("div", { class: "chart-plot", style: { height: `${height}px` } });
  const table = h("div", { class: "chart-table", hidden: true }, dataTable(labels, colored));
  const toggle = h("button", { type: "button", class: "btn-link text-small", "aria-pressed": "false", text: "Table" });
  toggle.addEventListener("click", () => {
    const showTable = table.hidden;
    table.hidden = !showTable;
    plot.hidden = showTable;
    toggle.textContent = showTable ? "Chart" : "Table";
    toggle.setAttribute("aria-pressed", String(showTable));
  });
  const legend = colored.length > 1
    ? h("div", { class: "chart-legend" }, colored.map((x) =>
      h("span", { class: "chart-legend-item" }, h("span", { class: "chart-key", style: { background: x.color } }), x.name)))
    : null;
  const card = h("div", { class: "box chart-card" },
    h("div", { class: "chart-head" },
      h("div", { class: "flex-1" },
        h("h3", { class: "chart-title", text: title }),
        caption ? h("div", { class: "chart-caption", text: caption }) : null),
      toggle),
    legend, plot, table);

  let width = 0;
  const draw = () => {
    const w = plot.clientWidth;
    if (!w || w === width) return;
    width = w;
    clear(plot).append(...(kind === "lines" ? lineChart : barChart)(w, height, labels, colored, { title, empty }));
  };
  const observer = new ResizeObserver(draw);
  observer.observe(plot);
  return { node: card, destroy: () => observer.disconnect() };
}

function dataTable(labels, series) {
  return h("table", { class: "stats-table" },
    h("thead", {}, h("tr", {}, h("th", { scope: "col", text: "Period" }), series.map((x) => h("th", { scope: "col", class: "num", text: x.name })))),
    h("tbody", {}, labels.map((l, i) => h("tr", {},
      h("th", { scope: "row", text: l.long }),
      series.map((x) => h("td", { class: "num", text: x.values[i].toLocaleString("en") }))))));
}

// The axes both chart kinds share: y ticks with hairline grid, x labels.
function frame(width, height, max) {
  const innerW = width - MARGIN.left - MARGIN.right;
  const innerH = height - MARGIN.top - MARGIN.bottom;
  const ys = ticks(max);
  const top = ys[ys.length - 1];
  const y = (v) => MARGIN.top + innerH - (v / top) * innerH;
  const g = s("g", { "aria-hidden": "true" });
  for (const t of ys) {
    g.append(s("line", { x1: MARGIN.left, x2: width - MARGIN.right, y1: y(t) + 0.5, y2: y(t) + 0.5, stroke: t === 0 ? AXIS : GRID }));
    g.append(s("text", { x: MARGIN.left - 6, y: y(t), dy: "0.32em", "text-anchor": "end", class: "chart-tick" }, compact(t)));
  }
  return { g, innerW, innerH, y, base: y(0) };
}

// Under the baseline. The first and the last label hug the plot's edges.
function xLabels(g, labels, center, innerW, base) {
  const n = labels.length;
  for (const i of labelled(n, innerW)) {
    const anchor = n > 1 && i === 0 ? "start" : n > 1 && i === n - 1 ? "end" : "middle";
    g.append(s("text", { x: center(i), y: base + 16, class: "chart-tick", "text-anchor": anchor }, labels[i].axis));
  }
}

// The tooltip and the active bucket, by pointer and by the arrow keys.
function interactive(svg, tip, labels, series, { at, show }) {
  let active = null;
  const place = (i) => {
    active = i;
    show(i);
    clear(tip).append(
      h("div", { class: "chart-tip-head", text: labels[i].long }),
      ...series.map((x) => h("div", { class: "chart-tip-row" },
        series.length > 1 ? h("span", { class: "chart-tip-key", style: { background: x.color } }) : null,
        h("strong", { text: x.values[i].toLocaleString("en") }),
        h("span", { class: "color-muted", text: x.name }))));
    tip.hidden = false;
    // Beside the bucket, not over it: right of it where there is room.
    const width = svg.getBoundingClientRect().width;
    const x = at(i);
    const left = x + 12 + tip.offsetWidth <= width ? x + 12 : Math.max(0, x - 12 - tip.offsetWidth);
    tip.style.left = `${left}px`;
  };
  const hide = () => {
    active = null;
    show(null);
    tip.hidden = true;
  };
  const nearest = (clientX) => {
    const x = clientX - svg.getBoundingClientRect().left;
    let best = 0;
    for (let i = 1; i < labels.length; i++) if (Math.abs(at(i) - x) < Math.abs(at(best) - x)) best = i;
    return best;
  };
  svg.addEventListener("pointermove", (e) => place(nearest(e.clientX)));
  svg.addEventListener("pointerleave", () => { if (document.activeElement !== svg) hide(); });
  svg.addEventListener("focus", () => place(active ?? labels.length - 1));
  svg.addEventListener("blur", hide);
  svg.addEventListener("keydown", (e) => {
    const n = labels.length;
    const moves = { ArrowLeft: -1, ArrowRight: 1 };
    let next = null;
    if (e.key in moves) next = Math.min(n - 1, Math.max(0, (active ?? n - 1) + moves[e.key]));
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = n - 1;
    if (next === null) return;
    e.preventDefault();
    place(next);
  });
}

function shell(width, height, title) {
  const svg = s("svg", {
    width, height, viewBox: `0 0 ${width} ${height}`, class: "chart-svg", tabindex: "0", role: "img",
    "aria-label": `${title}. Arrow keys step through the periods, the Table switch lists them all.`,
  });
  const tip = h("div", { class: "chart-tip", hidden: true, role: "status" });
  return { svg, tip };
}

function barChart(width, height, labels, [series], { title, empty }) {
  const n = labels.length;
  const max = Math.max(0, ...series.values);
  const f = frame(width, height, max);
  const band = f.innerW / n;
  const barW = Math.max(1, Math.min(BAR_MAX, band - BAR_GAP));
  const center = (i) => MARGIN.left + band * i + band / 2;
  const { svg, tip } = shell(width, height, title);
  const highlight = s("rect", { y: MARGIN.top, height: f.innerH, width: band, fill: BAND, visibility: "hidden" });
  svg.append(highlight, f.g);
  const bars = series.values.map((v, i) => {
    if (!v) return null;
    const bar = s("path", { d: roundedTop(center(i) - barW / 2, f.y(v), barW, f.base), fill: series.color });
    svg.append(bar);
    return bar;
  });
  xLabels(f.g, labels, center, f.innerW, f.base);
  if (!max) svg.append(s("text", { x: MARGIN.left + f.innerW / 2, y: MARGIN.top + f.innerH / 2, class: "chart-empty", "text-anchor": "middle" }, empty));
  interactive(svg, tip, labels, [series], {
    at: center,
    show: (i) => {
      highlight.setAttribute("visibility", i === null ? "hidden" : "visible");
      if (i !== null) highlight.setAttribute("x", MARGIN.left + band * i);
      bars.forEach((b, j) => b?.setAttribute("opacity", i === null || j === i ? "1" : "0.55"));
    },
  });
  return [svg, tip];
}

function lineChart(width, height, labels, series, { title }) {
  const n = labels.length;
  const max = Math.max(0, ...series.flatMap((x) => x.values));
  // Room on the right for the end labels.
  const right = 72;
  const f = frame(width - right + MARGIN.right, height, max);
  const x = (i) => MARGIN.left + (n > 1 ? (f.innerW * i) / (n - 1) : f.innerW / 2);
  const { svg, tip } = shell(width, height, title);
  const cross = s("line", { y1: MARGIN.top, y2: f.base, stroke: AXIS, visibility: "hidden" });
  svg.append(f.g, cross);
  const dots = [];
  const ends = [];
  for (const ser of series) {
    const d = ser.values.map((v, i) => `${i ? "L" : "M"}${x(i)},${f.y(v)}`).join("");
    svg.append(s("path", { d, fill: "none", stroke: ser.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    const last = ser.values[n - 1];
    svg.append(s("circle", { cx: x(n - 1), cy: f.y(last), r: 4, fill: ser.color, stroke: "#ffffff", "stroke-width": 2 }));
    ends.push({ y: f.y(last), text: `${last.toLocaleString("en")} ${ser.name.toLowerCase()}` });
    const dot = s("circle", { r: 4, fill: ser.color, stroke: "#ffffff", "stroke-width": 2, visibility: "hidden" });
    dots.push(dot);
  }
  svg.append(...dots);
  // End labels only where they stay apart. Else the legend and the tooltip
  // carry the names, rather than labels pushed off their lines.
  const apart = ends.every((a, i) => ends.every((b, j) => i === j || Math.abs(a.y - b.y) >= 14));
  if (apart) for (const e of ends) svg.append(s("text", { x: x(n - 1) + 10, y: e.y, dy: "0.32em", class: "chart-end" }, e.text));
  xLabels(f.g, labels, x, f.innerW, f.base);
  interactive(svg, tip, labels, series, {
    at: x,
    show: (i) => {
      const hidden = i === null;
      cross.setAttribute("visibility", hidden ? "hidden" : "visible");
      dots.forEach((dot) => dot.setAttribute("visibility", hidden ? "hidden" : "visible"));
      if (hidden) return;
      cross.setAttribute("x1", x(i));
      cross.setAttribute("x2", x(i));
      dots.forEach((dot, k) => { dot.setAttribute("cx", x(i)); dot.setAttribute("cy", f.y(series[k].values[i])); });
    },
  });
  return [svg, tip];
}
