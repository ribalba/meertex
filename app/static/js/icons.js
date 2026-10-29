// Line icons on a 16px grid, drawn for meertex in the spirit of GitHub's
// Octicons. Every entry is fixed markup written here, never user content, which
// is why icon() may assemble the SVG from strings.

const ICONS = {
  book: '<path d="M2 2.75h4.25A1.75 1.75 0 0 1 8 4.5v9a1.25 1.25 0 0 0-1.25-1.25H2z"/><path d="M14 2.75H9.75A1.75 1.75 0 0 0 8 4.5v9a1.25 1.25 0 0 1 1.25-1.25H14z"/>',
  code: '<path d="M5.5 4.25 1.75 8l3.75 3.75M10.5 4.25 14.25 8l-3.75 3.75"/>',
  file: '<path d="M9.25 1.75h-5a1 1 0 0 0-1 1v10.5a1 1 0 0 0 1 1h7.5a1 1 0 0 0 1-1v-8z"/><path d="M9.25 1.75v3.5h3.5"/>',
  "file-text": '<path d="M9.25 1.75h-5a1 1 0 0 0-1 1v10.5a1 1 0 0 0 1 1h7.5a1 1 0 0 0 1-1v-8z"/><path d="M9.25 1.75v3.5h3.5M5.5 8.25h5M5.5 11h5"/>',
  "file-binary": '<path d="M9.25 1.75h-5a1 1 0 0 0-1 1v10.5a1 1 0 0 0 1 1h7.5a1 1 0 0 0 1-1v-8z"/><path d="M9.25 1.75v3.5h3.5"/><rect x="5.5" y="8" width="2" height="3.5" rx=".5"/><path d="M9.75 8v3.5"/>',
  folder: '<path d="M1.75 3.5a1 1 0 0 1 1-1h3.5l1.5 1.75h5.5a1 1 0 0 1 1 1v7.25a1 1 0 0 1-1 1H2.75a1 1 0 0 1-1-1z"/>',
  "folder-fill": '<path fill="currentColor" stroke="none" d="M1.75 3.5a1 1 0 0 1 1-1h3.5l1.5 1.75h5.5a1 1 0 0 1 1 1v7.25a1 1 0 0 1-1 1H2.75a1 1 0 0 1-1-1z"/>',
  inbox: '<path d="M1.75 9.25h3.5l1 1.75h3.5l1-1.75h3.5"/><path d="M3.6 3h8.8l1.85 6.25v3.5a1 1 0 0 1-1 1H2.75a1 1 0 0 1-1-1v-3.5z"/>',
  tag: '<path d="M1.75 2.75v4.6l6.6 6.6a1 1 0 0 0 1.4 0l4.2-4.2a1 1 0 0 0 0-1.4l-6.6-6.6h-4.6a1 1 0 0 0-1 1z"/><circle cx="5" cy="5" r="1" fill="currentColor" stroke="none"/>',
  history: '<path d="M1.9 8.1A6.15 6.15 0 1 0 3.7 3.7"/><path d="M1.75 1.75v2.6h2.6M8 4.75V8l2.25 1.5"/>',
  clock: '<circle cx="8" cy="8" r="6.25"/><path d="M8 4.5V8l2.25 1.5"/>',
  gear: '<circle cx="8" cy="8" r="2"/><circle cx="8" cy="8" r="4.6"/><path stroke-width="2.4" stroke-linecap="butt" d="M8 1.2v2.2M8 12.6v2.2M1.2 8h2.2M12.6 8h2.2M3.2 3.2l1.55 1.55M11.25 11.25l1.55 1.55M3.2 12.8l1.55-1.55M11.25 4.75l1.55-1.55"/>',
  calendar: '<rect x="1.75" y="2.75" width="12.5" height="11.5" rx="1.5"/><path d="M1.75 6.5h12.5M5 1.25v3M11 1.25v3"/>',
  check: '<path d="M2.75 8.5 6 11.75l7.25-7.5"/>',
  "check-circle": '<circle cx="8" cy="8" r="6.25"/><path d="M5.25 8.25 7.25 10.25 10.75 6.25"/>',
  x: '<path d="M3.75 3.75l8.5 8.5M12.25 3.75l-8.5 8.5"/>',
  "x-circle": '<circle cx="8" cy="8" r="6.25"/><path d="M5.75 5.75l4.5 4.5M10.25 5.75l-4.5 4.5"/>',
  pencil: '<path d="M11 2.25l2.75 2.75L5.5 13.25H2.75V10.5z"/><path d="M9.5 3.75l2.75 2.75"/>',
  trash: '<path d="M2.5 4h11M6 4V2.5h4V4M3.75 4l.75 9.75h7L12.25 4M6.75 6.75v4.5M9.25 6.75v4.5"/>',
  download: '<path d="M8 1.75v8.5M4.5 7 8 10.5 11.5 7M2.25 11.5v2.25h11.5V11.5"/>',
  upload: '<path d="M8 10.25v-8.5M4.5 5 8 1.5 11.5 5M2.25 11.5v2.25h11.5V11.5"/>',
  copy: '<rect x="5.25" y="5.25" width="9" height="9" rx="1.25"/><path d="M10.75 5.25v-2.5a1 1 0 0 0-1-1H2.75a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h2.5"/>',
  plus: '<path d="M8 2.75v10.5M2.75 8h10.5"/>',
  "chevron-down": '<path d="M4 6l4 4 4-4"/>',
  "chevron-right": '<path d="M6 4l4 4-4 4"/>',
  "chevron-left": '<path d="M10 4 6 8l4 4"/>',
  "triangle-down": '<path fill="currentColor" stroke="none" d="M4.4 6.25h7.2a.4.4 0 0 1 .3.67L8.3 10.5a.4.4 0 0 1-.6 0L4.1 6.92a.4.4 0 0 1 .3-.67z"/>',
  kebab: '<circle cx="3" cy="8" r="1.15" fill="currentColor" stroke="none"/><circle cx="8" cy="8" r="1.15" fill="currentColor" stroke="none"/><circle cx="13" cy="8" r="1.15" fill="currentColor" stroke="none"/>',
  people: '<circle cx="6" cy="5.25" r="2.5"/><path d="M1.5 13.75c.4-2.6 2.2-4 4.5-4s4.1 1.4 4.5 4M10.25 2.9a2.4 2.4 0 0 1 0 4.7M11.75 9.9c1.4.45 2.4 1.65 2.75 3.85"/>',
  person: '<circle cx="8" cy="5" r="2.75"/><path d="M2.75 14c.45-2.9 2.5-4.5 5.25-4.5s4.8 1.6 5.25 4.5"/>',
  link: '<path d="M6.9 9.1a2.6 2.6 0 0 0 3.7 0l2.6-2.6a2.6 2.6 0 0 0-3.7-3.7l-.85.85"/><path d="M9.1 6.9a2.6 2.6 0 0 0-3.7 0L2.8 9.5a2.6 2.6 0 0 0 3.7 3.7l.85-.85"/>',
  location: '<path d="M8 14.5s5-4.4 5-8.25a5 5 0 0 0-10 0C3 10.1 8 14.5 8 14.5z"/><circle cx="8" cy="6.25" r="1.75"/>',
  commit: '<circle cx="8" cy="8" r="2.75"/><path d="M1 8h4.25M10.75 8H15"/>',
  alert: '<path d="M7.13 2.5a1 1 0 0 1 1.74 0l5.6 9.9a1 1 0 0 1-.87 1.5H2.4a1 1 0 0 1-.87-1.5z"/><path d="M8 6.25v3M8 11.6v.15"/>',
  question: '<circle cx="8" cy="8" r="6.25"/><path d="M6.1 6.1a1.95 1.95 0 1 1 2.75 1.8c-.55.25-.85.65-.85 1.25v.3M8 11.35v.2"/>',
  info: '<circle cx="8" cy="8" r="6.25"/><path d="M8 7.25v4M8 4.85v.2"/>',
  archive: '<rect x="1.5" y="2.25" width="13" height="3.25" rx=".75"/><path d="M2.75 5.5v7.25a1 1 0 0 0 1 1h8.5a1 1 0 0 0 1-1V5.5M6.25 8.5h3.5"/>',
  search: '<circle cx="7" cy="7" r="4.75"/><path d="M10.5 10.5 14.25 14.25"/>',
  fork: '<circle cx="4" cy="3.25" r="1.5"/><circle cx="12" cy="3.25" r="1.5"/><circle cx="8" cy="12.75" r="1.5"/><path d="M4 4.75v.75a2 2 0 0 0 2 2h4a2 2 0 0 0 2-2v-.75M8 7.5v3.75"/>',
  eye: '<path d="M1.25 8S3.75 3.25 8 3.25 14.75 8 14.75 8 12.25 12.75 8 12.75 1.25 8 1.25 8z"/><circle cx="8" cy="8" r="2"/>',
  "eye-closed": '<path d="M1.25 8S3.75 3.25 8 3.25 14.75 8 14.75 8 12.25 12.75 8 12.75 1.25 8 1.25 8z"/><circle cx="8" cy="8" r="2"/><path d="M2.25 2.25l11.5 11.5"/>',
  comment: '<path d="M2.75 2.75h10.5a1 1 0 0 1 1 1v6.75a1 1 0 0 1-1 1H8.5l-3.25 2.75v-2.75h-2.5a1 1 0 0 1-1-1V3.75a1 1 0 0 1 1-1z"/>',
  "sign-out": '<path d="M6.25 2.25h-3a1 1 0 0 0-1 1v9.5a1 1 0 0 0 1 1h3M10.25 4.75 13.5 8l-3.25 3.25M13.5 8H6"/>',
  key: '<circle cx="5" cy="11" r="2.75"/><path d="M7 9l6.75-6.75M11.25 4.5l1.75 1.75M9.5 6.25l1.25 1.25"/>',
  terminal: '<rect x="1.25" y="2.25" width="13.5" height="11.5" rx="1.5"/><path d="M4.25 6 6.5 8l-2.25 2M8.25 10.25h3.5"/>',
  "arrow-switch": '<path d="M4.5 1.75v9.5M2 8.75l2.5 2.5L7 8.75M11.5 14.25v-9.5M9 7.25l2.5-2.5 2.5 2.5"/>',
  "arrow-left": '<path d="M13.5 8h-11M6.5 4 2.5 8l4 4"/>',
  "arrow-right": '<path d="M2.5 8h11M9.5 4l4 4-4 4"/>',
  mail: '<rect x="1.5" y="3" width="13" height="10" rx="1.25"/><path d="M1.9 4 8 8.75 14.1 4"/>',
  globe: '<circle cx="8" cy="8" r="6.25"/><path d="M1.75 8h12.5M8 1.75c1.75 1.75 2.5 3.9 2.5 6.25s-.75 4.5-2.5 6.25C6.25 12.5 5.5 10.35 5.5 8S6.25 3.5 8 1.75z"/>',
  "move-to": '<path d="M1.75 4.5a1 1 0 0 1 1-1h3l1.5 1.5h5.5a1 1 0 0 1 1 1v6.25a1 1 0 0 1-1 1H2.75a1 1 0 0 1-1-1z"/><path d="M6 9h4.25M8.5 7.25 10.25 9 8.5 10.75"/>',
  dot: '<circle cx="8" cy="8" r="4" fill="currentColor" stroke="none"/>',
  milestone: '<path d="M8 1.5v13M3 3.5h7.75L13 5.75 10.75 8H3z"/>',
  pdf: '<path d="M9.25 1.75h-5a1 1 0 0 0-1 1v10.5a1 1 0 0 0 1 1h7.5a1 1 0 0 0 1-1v-8z"/><path d="M9.25 1.75v3.5h3.5"/><path d="M5.25 11.5v-3h1a1 1 0 0 1 0 2h-1M8.5 8.5v3h.5a1.5 1.5 0 0 0 0-3z"/>',
  zip: '<path d="M9.25 1.75h-5a1 1 0 0 0-1 1v10.5a1 1 0 0 0 1 1h7.5a1 1 0 0 0 1-1v-8z"/><path d="M9.25 1.75v3.5h3.5M6.5 2.25v1.5M6.5 5v1.5M6.5 7.75v1.5"/><rect x="5.5" y="10" width="2" height="2.25" rx=".5"/>',
  unfold: '<path d="M8 1.5v4.25M5.75 3.75 8 1.5l2.25 2.25M8 14.5v-4.25M5.75 12.25 8 14.5l2.25-2.25M2 8h1.5M6.25 8h3.5M12.5 8H14"/>',
  graph: '<path d="M1.75 1.75v12.5h12.5M4.5 11l3-3.5 2.25 2 4-5"/>',
  sun: '<circle cx="8" cy="8" r="3"/><path d="M8 1v1.5M8 13.5V15M1 8h1.5M13.5 8H15M3 3l1 1M12 12l1 1M3 13l1-1M12 4l1-1"/>',
  bell: '<path d="M4 6.5a4 4 0 0 1 8 0c0 3 1.25 4.5 1.75 5H2.25C2.75 11 4 9.5 4 6.5z"/><path d="M6.5 13.75a1.6 1.6 0 0 0 3 0"/>',
  "bell-slash": '<path d="M4 6.5a4 4 0 0 1 8 0c0 3 1.25 4.5 1.75 5H2.25C2.75 11 4 9.5 4 6.5z"/><path d="M6.5 13.75a1.6 1.6 0 0 0 3 0M1.75 1.75l12.5 12.5"/>',
  quote: '<path d="M6.75 7.75v-3a1 1 0 0 0-1-1h-2.5a1 1 0 0 0-1 1v2a1 1 0 0 0 1 1zm0 0c0 2.25-1.25 3.75-3.5 4.5M13.75 7.75v-3a1 1 0 0 0-1-1h-2.5a1 1 0 0 0-1 1v2a1 1 0 0 0 1 1zm0 0c0 2.25-1.25 3.75-3.5 4.5"/>',
  // The editor's formatting bar (editor-format.js).
  bold: '<path stroke-width="2" d="M4.5 2.75h4a2.6 2.6 0 0 1 0 5.2h-4zm0 5.2h4.75a2.65 2.65 0 0 1 0 5.3H4.5z"/>',
  italic: '<path d="M7 2.75h5.25M3.75 13.25H9M9.6 2.75 6.4 13.25"/>',
  heading: '<path d="M3.75 2.75v10.5M12.25 2.75v10.5M3.75 8h8.5"/>',
  "list-unordered": '<path d="M6 4h8.25M6 8h8.25M6 12h8.25"/><circle cx="2.5" cy="4" r="1" fill="currentColor" stroke="none"/><circle cx="2.5" cy="8" r="1" fill="currentColor" stroke="none"/><circle cx="2.5" cy="12" r="1" fill="currentColor" stroke="none"/>',
  "list-ordered": '<path d="M6.5 4h7.75M6.5 8h7.75M6.5 12h7.75"/><path stroke-width="1.2" d="M1.9 2.6l1-.6v3.6M1.75 7.1a1 1 0 0 1 1.9.35c0 .75-1.85 1.45-1.85 2.3h1.95M1.8 10.6h1.8l-.95 1.2a1 1 0 1 1-.95 1.5"/>',
  sigma: '<path d="M12.25 4.25v-1.5h-8.5L8.25 8l-4.5 5.25h8.5v-1.5"/>',
  percent: '<path d="M12.75 3.25 3.25 12.75"/><circle cx="4.5" cy="4.5" r="1.75"/><circle cx="11.5" cy="11.5" r="1.75"/>',
  keyboard: '<rect x="1.25" y="3.75" width="13.5" height="8.5" rx="1.5"/><path stroke-width="1.75" d="M4.25 6.75h0M6.75 6.75h0M9.25 6.75h0M11.75 6.75h0M5.25 9.5h5.5"/>',
};

const SVG_NS = "http://www.w3.org/2000/svg";

export function icon(name, { size = 16, cls = "", label = "" } = {}) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.5");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("class", `icon icon-${name}${cls ? " " + cls : ""}`);
  if (label) {
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", label);
  } else {
    svg.setAttribute("aria-hidden", "true");
  }
  svg.innerHTML = ICONS[name] || ICONS.dot;
  return svg;
}
