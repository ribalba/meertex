// A PDF viewer on PDF.js (app/static/vendor/pdfjs), shared by the editor, the
// versions and the reviews. It draws the pages near the view only and lets go
// of the others, so a long paper at a high zoom does not fill the memory.
//
//   const view = createPdfView(container, { onDoubleClick, onChange });
//   await view.load(url);       // load, or replace and keep the scroll position
//   view.setZoom(1.25);         // or "width" to fit the width; zoomIn(), zoomOut()
//   view.showBoxes(boxes);      // mark [{page, x, y, width, height}] and scroll there
//   view.destroy();
//
// Coordinates are PDF points from the top left of a page, the way SyncTeX
// gives them. Pages count from 1.
//
// Options:
//   onDoubleClick({page, x, y, word, before, after})
//                                        a double-click on a page. word is the
//                                        word under the pointer as the page's
//                                        text has it ("measured" for a "mea-"
//                                        "sured" broken at a line's end), before
//                                        and after up to 30 characters around
//                                        it. The three are missing where the
//                                        page has no text.
//   onChange({page, pages, zoom, mode})  the page in view, the page count or the zoom changed
//
// The view: load(url), setZoom(zoom | "width"), zoomIn(), zoomOut(),
// showBoxes(boxes, {fade = true, scroll = true}), clearBoxes(),
// scrollToPage(page), destroy(), and the read-only page, pages, zoom (1 is
// 100 percent) and mode ("width" or "fixed").
//
// wordAt({page}, x, y) is exported for tests: the word at a point in PDF user
// space of a PDF.js page, as onDoubleClick gets it.

const PDFJS = "/static/vendor/pdfjs/pdf.js";
const WORKER = "/static/vendor/pdfjs/pdf.worker.js";
// CSS pixels per PDF point at 100 percent, as in PDF.js's own viewer.
const PX_PER_POINT = 96 / 72;
const ZOOMS = [0.25, 0.33, 0.5, 0.67, 0.75, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3, 4];
const GAP = 12;
const PADDING = 16;
// Pages within this many pixels of the view are drawn. Farther ones let go of their canvas.
const NEAR = 1200;
const FAR = 4000;
// A canvas larger than this is drawn at a lower resolution.
const MAX_CANVAS_PIXELS = 16_777_216;

let pdfjs = null;

function loadPdfjs() {
  pdfjs ??= import(PDFJS).then((lib) => {
    lib.GlobalWorkerOptions.workerSrc = WORKER;
    return lib;
  }).catch((err) => {
    pdfjs = null;
    throw err;
  });
  return pdfjs;
}

// Fonts without accented letters (TeX's OT1) draw an accent and a letter, and
// their text has a spacing accent next to the letter: "na¨ıve" for "naïve".
// Those accents count as part of a word and are put together with the letter.
const WORD = /[\p{L}\p{N}\u00A8\u00B4\u02C6\u02DC\u00AF\u02D9\u02D8\u02C7\u02DD\u02DA\u00B8\u02DB]/u;
const CONTEXT = 30;
const SPACING_ACCENTS = {
  "\u00A8": "\u0308", "\u00B4": "\u0301", "\u02C6": "\u0302", "\u02DC": "\u0303", "\u00AF": "\u0304", "\u02D9": "\u0307",
  "\u02D8": "\u0306", "\u02C7": "\u030C", "\u02DD": "\u030B", "\u02DA": "\u030A", "\u00B8": "\u0327", "\u02DB": "\u0328",
};

function joinAccents(text) {
  return text
    // Most accents come before their letter. A dotless i or j under one is a plain one.
    .replace(/([\u00A8\u00B4\u02C6\u02DC\u00AF\u02D9\u02D8\u02C7\u02DD\u02DA])(\p{L})/gu,
      (_, accent, letter) => ({ "\u0131": "i", "\u0237": "j" }[letter] || letter) + SPACING_ACCENTS[accent])
    // A cedilla and an ogonek come after it.
    .replace(/(\p{L})([\u00B8\u02DB])/gu, (_, letter, accent) => letter + SPACING_ACCENTS[accent])
    .normalize("NFC");
}

// The word at (px, py), in PDF user space, from the page's text content. Each
// text item has its baseline's start (transform[4], transform[5]), its width
// and its height. The characters of an item are taken as equally wide.
export async function wordAt(p, px, py) {
  p.text ??= p.page.getTextContent();
  const { items } = await p.text;
  let full = "";
  let best = null;
  let bestDistance = Infinity;
  for (const it of items) {
    if (typeof it.str !== "string") continue;
    const start = full.length;
    full += it.str;
    if (it.str.trim()) {
      const [a, b, c, d, x, y] = it.transform;
      const size = it.height || Math.hypot(c, d) || Math.hypot(a, b) || 10;
      const dx = px < x ? x - px : px > x + it.width ? px - x - it.width : 0;
      const dy = py < y - size * 0.3 ? y - size * 0.3 - py : py > y + size ? py - y - size : 0;
      const distance = dx + 4 * dy;
      if (distance < bestDistance) {
        bestDistance = distance;
        best = { it, start, x };
      }
    }
    if (it.hasEOL) full += "\n";
  }
  if (!best || bestDistance > 4) return null;
  const { it, start, x } = best;
  const frac = Math.min(0.9999, Math.max(0, (px - x) / (it.width || 1)));
  let i = start + Math.floor(frac * it.str.length);
  if (!WORD.test(full[i] || "")) {
    if (WORD.test(full[i - 1] || "")) i -= 1;
    else if (WORD.test(full[i + 1] || "")) i += 1;
    else return null;
  }
  let from = i;
  let to = i + 1;
  while (from > 0 && WORD.test(full[from - 1])) from -= 1;
  while (to < full.length && WORD.test(full[to])) to += 1;
  // A word hyphenated at the end of a line, "mea-" and "sured", is one word.
  let word = full.slice(from, to);
  const tail = /^-[ \t]*\n\s*([\p{L}\p{N}\u00A8\u00B4\u02C6\u02DC\u00AF\u02D9\u02D8\u02C7\u02DD\u02DA\u00B8\u02DB]+)/u.exec(full.slice(to, to + 80));
  if (tail) {
    word += tail[1];
    to += tail[0].length;
  }
  const head = /([\p{L}\p{N}\u00A8\u00B4\u02C6\u02DC\u00AF\u02D9\u02D8\u02C7\u02DD\u02DA\u00B8\u02DB]+)-[ \t]*\n\s*$/u.exec(full.slice(Math.max(0, from - 80), from));
  if (head) {
    word = head[1] + word;
    from -= head[0].length;
  }
  const plain = (text) => joinAccents(text.replace(/([\p{L}\p{N}])-[ \t]*\n\s*/gu, "$1").replace(/\s+/g, " "));
  return {
    word: joinAccents(word),
    before: plain(full.slice(Math.max(0, from - CONTEXT - 4), from)).slice(-CONTEXT),
    after: plain(full.slice(to, to + CONTEXT + 4)).slice(0, CONTEXT),
  };
}

export function createPdfView(container, { onDoubleClick = null, onChange = null } = {}) {
  const scroller = document.createElement("div");
  scroller.className = "pdfview";
  scroller.tabIndex = 0;
  scroller.setAttribute("aria-label", "PDF");
  container.append(scroller);

  let lib = null;
  let worker = null;
  let doc = null; // a document goes with its loadingTask.destroy(), the worker stays
  let pages = []; // {page, base, el, canvas, drawn, task}
  let zoom = 1;
  let mode = "width";
  let marks = [];
  let markTimer = 0;
  let loadToken = 0;
  let frame = 0;
  let lastPage = 0;
  let destroyed = false;

  const cssScale = () => zoom * PX_PER_POINT;

  function fitWidth(list = pages) {
    if (!list.length) return zoom;
    const widest = Math.max(...list.map((p) => p.base.width));
    const room = Math.max(120, scroller.clientWidth - 2 * PADDING);
    return Math.min(4, Math.max(0.25, room / (widest * PX_PER_POINT)));
  }

  function viewportOf(p) {
    return p.page.getViewport({ scale: cssScale() });
  }

  function makePage(page, base, index) {
    const el = document.createElement("div");
    el.className = "pdfview-page";
    el.dataset.page = String(index + 1);
    el.setAttribute("aria-label", `Page ${index + 1}`);
    return { page, base, el, canvas: null, drawn: 0, task: null, text: null };
  }

  function size(p) {
    const vp = viewportOf(p);
    p.el.style.width = `${Math.floor(vp.width)}px`;
    p.el.style.height = `${Math.floor(vp.height)}px`;
  }

  function release(p) {
    p.task?.cancel();
    p.task = null;
    if (p.canvas) {
      p.canvas.width = 0;
      p.canvas.height = 0;
      p.canvas.remove();
      p.canvas = null;
    }
    p.drawn = 0;
  }

  async function draw(p) {
    const scale = cssScale();
    if (p.drawn === scale || p.task) return;
    const vp = viewportOf(p);
    let ratio = window.devicePixelRatio || 1;
    if (vp.width * vp.height * ratio * ratio > MAX_CANVAS_PIXELS) ratio = Math.sqrt(MAX_CANVAS_PIXELS / (vp.width * vp.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.floor(vp.width * ratio);
    canvas.height = Math.floor(vp.height * ratio);
    // It fills its page, so after a zoom it stretches until it is drawn again.
    canvas.style.width = "100%";
    canvas.style.height = "100%";
    p.task = p.page.render({
      canvas,
      canvasContext: canvas.getContext("2d"),
      viewport: vp,
      transform: ratio === 1 ? null : [ratio, 0, 0, ratio, 0, 0],
    });
    try {
      await p.task.promise;
    } catch (err) {
      if (err?.name !== "RenderingCancelledException" && !destroyed) console.error(err);
      return;
    } finally {
      p.task = null;
    }
    // The old canvas stays until the new one is ready, so a zoom does not blink.
    p.canvas?.remove();
    p.canvas = canvas;
    p.el.prepend(canvas);
    p.drawn = scale;
    if (scale !== cssScale()) schedule();
  }

  // Draw what is near the view, let go of what is far from it.
  function update() {
    frame = 0;
    if (destroyed || !pages.length) return;
    const top = scroller.scrollTop;
    const bottom = top + scroller.clientHeight;
    const middle = (top + bottom) / 2;
    let current = 1;
    for (const [i, p] of pages.entries()) {
      const pTop = p.el.offsetTop;
      const pBottom = pTop + p.el.offsetHeight;
      if (pTop <= middle) current = i + 1;
      if (pBottom > top - NEAR && pTop < bottom + NEAR) draw(p);
      else if (pBottom < top - FAR || pTop > bottom + FAR) release(p);
    }
    if (current !== lastPage) {
      lastPage = current;
      changed();
    }
  }

  function schedule() {
    if (!frame) frame = requestAnimationFrame(update);
  }

  function changed() {
    onChange?.({ page: lastPage || 1, pages: pages.length, zoom, mode });
  }

  // Where the view is, as a page and a fraction of it, to come back to after a change.
  function anchor() {
    if (!pages.length) return null;
    const top = scroller.scrollTop;
    for (const [i, p] of pages.entries()) {
      if (p.el.offsetTop + p.el.offsetHeight + GAP > top) {
        return { index: i, frac: Math.max(0, (top - p.el.offsetTop) / p.el.offsetHeight), left: scroller.scrollLeft / Math.max(1, scroller.scrollWidth) };
      }
    }
    return null;
  }

  function restore(at) {
    if (!at) return;
    const p = pages[Math.min(at.index, pages.length - 1)];
    scroller.scrollTop = p.el.offsetTop + at.frac * p.el.offsetHeight;
    scroller.scrollLeft = at.left * scroller.scrollWidth;
  }

  // Page numbers of the pages a view at `at` would show, for drawing them before a swap.
  function pagesAround(list, at, scale) {
    const heights = list.map((p) => p.page.getViewport({ scale }).height + GAP);
    const start = Math.min(at?.index || 0, list.length - 1);
    const out = [];
    for (let i = start, room = scroller.clientHeight + heights[start]; i < list.length && room > 0; i++) {
      out.push(i);
      room -= heights[i];
    }
    return out;
  }

  async function load(url) {
    const mine = ++loadToken;
    lib ??= await loadPdfjs();
    worker ??= new lib.PDFWorker({ name: "pdfview" });
    let next;
    try {
      // A PDF can come from anyone on the paper (an attached hand-in, a
      // compile of their text). No code from inside a PDF ever runs here.
      next = await lib.getDocument({ url, worker, isEvalSupported: false, enableXfa: false }).promise;
    } catch (err) {
      // A view that was destroyed or got a newer URL meanwhile has nothing to say.
      if (mine !== loadToken || destroyed) return;
      throw err;
    }
    if (mine !== loadToken || destroyed) {
      next.loadingTask.destroy();
      return;
    }
    const loaded = await Promise.all(Array.from({ length: next.numPages }, (_, i) =>
      next.getPage(i + 1).then((page) => ({ page, base: page.getViewport({ scale: 1 }) }))));
    if (mine !== loadToken || destroyed) {
      next.loadingTask.destroy();
      return;
    }
    const at = anchor();
    const fresh = loaded.map(({ page, base }, i) => makePage(page, base, i));
    if (mode === "width") zoom = fitWidth(fresh);
    fresh.forEach(size);
    // The pages that will be in view are drawn first, then the old PDF goes in one step.
    await Promise.all(pagesAround(fresh, at, cssScale()).map((i) => draw(fresh[i])));
    if (mine !== loadToken || destroyed) {
      next.loadingTask.destroy();
      return;
    }
    const inner = document.createElement("div");
    inner.className = "pdfview-pages";
    inner.style.padding = `${PADDING}px`;
    inner.style.gap = `${GAP}px`;
    inner.append(...fresh.map((p) => p.el));
    pages.forEach(release);
    const old = doc;
    doc = next;
    pages = fresh;
    scroller.replaceChildren(inner);
    restore(at);
    old?.loadingTask.destroy();
    marks = marks.filter((m) => m.keep);
    placeMarks();
    lastPage = 0;
    update();
    changed();
  }

  function setZoom(value) {
    if (value === "width") {
      mode = "width";
      value = fitWidth();
    } else {
      mode = "fixed";
    }
    const next = Math.min(4, Math.max(0.25, value));
    if (Math.abs(next - zoom) < 0.001 && pages.length) {
      changed();
      return;
    }
    const at = anchor();
    zoom = next;
    pages.forEach(size);
    restore(at);
    placeMarks();
    update();
    changed();
  }

  function step(direction) {
    const next = direction > 0 ? ZOOMS.find((z) => z > zoom + 0.001) : [...ZOOMS].reverse().find((z) => z < zoom - 0.001);
    setZoom(next ?? zoom);
  }

  // A box in points from the page's top left, in CSS pixels on the page element.
  function boxOnPage(p, box) {
    const vp = viewportOf(p);
    const [x0, , , y1] = p.base.viewBox;
    const a = vp.convertToViewportPoint(x0 + box.x, y1 - box.y);
    const b = vp.convertToViewportPoint(x0 + box.x + box.width, y1 - box.y - box.height);
    return { left: Math.min(a[0], b[0]), top: Math.min(a[1], b[1]), width: Math.abs(b[0] - a[0]), height: Math.abs(b[1] - a[1]) };
  }

  function placeMarks() {
    scroller.querySelectorAll(".pdfview-mark").forEach((el) => el.remove());
    for (const m of marks) {
      const p = pages[m.box.page - 1];
      if (!p) continue;
      const r = boxOnPage(p, m.box);
      const el = document.createElement("div");
      el.className = `pdfview-mark${m.keep ? "" : " is-fading"}`;
      Object.assign(el.style, { left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px` });
      p.el.append(el);
    }
  }

  function showBoxes(boxes, { fade = true, scroll = true } = {}) {
    clearTimeout(markTimer);
    marks = (boxes || []).filter((b) => pages[b.page - 1]).map((box) => ({ box, keep: !fade }));
    placeMarks();
    if (fade) markTimer = setTimeout(clearBoxes, 4000);
    const first = marks[0];
    if (scroll && first) {
      const p = pages[first.box.page - 1];
      const r = boxOnPage(p, first.box);
      scroller.scrollTop = p.el.offsetTop + r.top - scroller.clientHeight / 3;
      const left = p.el.offsetLeft + r.left - scroller.clientWidth / 4;
      if (left < scroller.scrollLeft || left > scroller.scrollLeft + scroller.clientWidth / 2) scroller.scrollLeft = Math.max(0, left);
    }
  }

  function clearBoxes() {
    marks = [];
    placeMarks();
  }

  function scrollToPage(n) {
    const p = pages[n - 1];
    if (p) scroller.scrollTop = p.el.offsetTop - PADDING;
  }

  scroller.addEventListener("scroll", schedule, { passive: true });
  scroller.addEventListener("dblclick", async (e) => {
    const el = e.target.closest?.(".pdfview-page");
    if (!el || !onDoubleClick) return;
    const p = pages[Number(el.dataset.page) - 1];
    const rect = el.getBoundingClientRect();
    const [px, py] = viewportOf(p).convertToPdfPoint(e.clientX - rect.left, e.clientY - rect.top);
    const [x0, , , y1] = p.base.viewBox;
    e.preventDefault();
    const at = { page: Number(el.dataset.page), x: px - x0, y: y1 - py };
    let found = null;
    try {
      found = await wordAt(p, px, py);
    } catch (err) {
      console.warn("The word under the pointer could not be found", err);
    }
    if (!destroyed) onDoubleClick(found ? { ...at, ...found } : at);
  });
  scroller.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === "+" || e.key === "=")) { e.preventDefault(); step(1); }
    if ((e.ctrlKey || e.metaKey) && e.key === "-") { e.preventDefault(); step(-1); }
  });
  // A wider or narrower pane changes the zoom that fits the width.
  const resized = new ResizeObserver(() => {
    if (mode === "width" && pages.length) setZoom("width");
    else schedule();
  });
  resized.observe(scroller);

  return {
    load,
    setZoom,
    zoomIn: () => step(1),
    zoomOut: () => step(-1),
    showBoxes,
    clearBoxes,
    scrollToPage,
    get page() { return lastPage || (pages.length ? 1 : 0); },
    get pages() { return pages.length; },
    get zoom() { return zoom; },
    get mode() { return mode; },
    destroy() {
      destroyed = true;
      loadToken += 1;
      cancelAnimationFrame(frame);
      clearTimeout(markTimer);
      resized.disconnect();
      pages.forEach(release);
      pages = [];
      doc?.loadingTask.destroy();
      doc = null;
      worker?.destroy();
      worker = null;
      scroller.remove();
    },
  };
}
