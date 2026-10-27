// LaTeX autocomplete for the editor (docs/DESIGN.md §14, In the editor).
//
// Commands with snippets, environments after \begin{, labels after \ref{ and
// its relatives, citation keys after \cite{ and its relatives, and file paths
// after \input{, \include{ and \includegraphics{. Labels, keys and the
// project's own commands come from every file of the live document. The index
// reads a file again only after it changed, and only when a completion asks.

const LATEX_FILE = /\.(tex|sty|cls|ltx)$/i;
const BIB_FILE = /\.bib$/i;
const TEX_FILE = /\.tex$/i;
const GRAPHIC = /\.(pdf|png|jpe?g|eps|svg|gif|tiff?)$/i;

// --- Reading the project ---------------------------------------------------------

// A line without its comment. `\%` is a percent sign, not a comment.
function uncomment(line) {
  for (let i = line.indexOf("%"); i >= 0; i = line.indexOf("%", i + 1)) {
    let slashes = 0;
    for (let j = i - 1; j >= 0 && line[j] === "\\"; j--) slashes++;
    if (slashes % 2 === 0) return line.slice(0, i);
  }
  return line;
}

// The outline's commands. A heading's number is its depth.
const HEADINGS = { part: 0, chapter: 1, section: 2, subsection: 3, subsubsection: 4, paragraph: 5, subparagraph: 6 };
const OUTLINE = /\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph|input|include|subfile|import|subimport)(?![a-zA-Z@])\*?/g;

// The text inside the braces that open at `at`, or the rest of the line when
// they close on a later one.
function braced(line, at) {
  let depth = 0;
  for (let i = at; i < line.length; i++) {
    if (line[i] === "\\") i++;
    else if (line[i] === "{") depth++;
    else if (line[i] === "}" && --depth === 0) return line.slice(at + 1, i);
  }
  return line.slice(at + 1);
}

// A heading as it reads in the PDF, more or less. Labels and notes go,
// commands give up their name and keep their text.
function plainTitle(tex) {
  return tex
    .replace(/\\(?:label|index|footnote|thanks|cite[a-zA-Z]*)\*?(?:\[[^\]]*\])*\{[^{}]*\}/g, "")
    .replace(/\\\\|~/g, " ")
    .replace(/\\([&%$#_{}])|\\[a-zA-Z@]+\*?\s*|[{}$]/g, (_, escaped) => escaped || "")
    .replace(/\s+/g, " ")
    .trim();
}

// Headings and the files a line reads in, in the order LaTeX meets them.
function outlineOf(line, n, outline) {
  for (const m of line.matchAll(OUTLINE)) {
    const rest = line.slice(m.index + m[0].length);
    const level = HEADINGS[m[1]];
    if (level !== undefined) {
      const open = /^\s*(?:\[[^\]]*\])?\s*\{/.exec(rest);
      const title = open ? braced(rest, open[0].length - 1) : null;
      // A heading in a macro's body has #1 for its title.
      if (title !== null && !title.includes("#")) outline.push({ level, title: plainTitle(title), line: n });
    } else if (m[1].endsWith("import")) {
      const args = /^\s*\{([^{}]*)\}\s*\{([^{}]+)\}/.exec(rest);
      const dir = args?.[1].trim().replace(/\/+$/, "");
      if (args) outline.push({ input: dir ? `${dir}/${args[2].trim()}` : args[2].trim(), line: n });
    } else {
      const arg = /^\s*\{([^{}]+)\}/.exec(rest);
      if (arg) outline.push({ input: arg[1].trim(), line: n });
    }
  }
}

function scanTex(text) {
  const labels = [];
  const commands = [];
  const environments = [];
  const outline = [];
  text.split("\n").forEach((raw, i) => {
    const line = uncomment(raw);
    if (!line.includes("\\")) return;
    outlineOf(line, i + 1, outline);
    for (const m of line.matchAll(/\\label\{([^{}]+)\}/g)) labels.push({ name: m[1].trim(), line: i + 1, text: raw.trim() });
    for (const m of line.matchAll(/\\(?:re)?newcommand\*?\s*\{?\\([a-zA-Z@]+)\}?\s*(?:\[(\d)\])?/g)) commands.push({ name: m[1], args: Number(m[2] || 0) });
    for (const m of line.matchAll(/\\providecommand\*?\s*\{?\\([a-zA-Z@]+)\}?\s*(?:\[(\d)\])?/g)) commands.push({ name: m[1], args: Number(m[2] || 0) });
    for (const m of line.matchAll(/\\DeclareMathOperator\*?\s*\{\\([a-zA-Z@]+)\}/g)) commands.push({ name: m[1], args: 0 });
    for (const m of line.matchAll(/\\def\s*\\([a-zA-Z@]+)\s*(#\d)*/g)) commands.push({ name: m[1], args: m[2] ? Number(m[2][1]) : 0 });
    for (const m of line.matchAll(/\\(?:re)?newenvironment\*?\s*\{([^{}]+)\}/g)) environments.push(m[1].trim());
    for (const m of line.matchAll(/\\newtheorem\*?\s*\{([^{}]+)\}/g)) environments.push(m[1].trim());
  });
  return { labels, commands, environments, cites: [], outline };
}

// The text of a field's value at `at`: {...} with nested braces, "..." or a bare word.
function fieldValue(body, at) {
  const open = body[at];
  if (open === "{" || open === '"') {
    let depth = 0;
    for (let i = at; i < body.length; i++) {
      const c = body[i];
      if (c === "\\") { i++; continue; }
      if (c === "{") depth++;
      else if (c === "}") {
        depth--;
        if (open === "{" && depth === 0) return body.slice(at + 1, i);
      } else if (c === '"' && open === '"' && i > at && depth === 0) return body.slice(at + 1, i);
    }
    return body.slice(at + 1);
  }
  return (/^[^,}\s]*/.exec(body.slice(at)) || [""])[0];
}

function field(body, name) {
  const m = new RegExp(`(?:^|[,\\s])${name}\\s*=\\s*`, "i").exec(body);
  if (!m) return "";
  return fieldValue(body, m.index + m[0].length)
    .replace(/\\[^a-zA-Z\s]/g, "") // accents like \"o
    .replace(/\\[a-zA-Z]+\s*/g, "")
    .replace(/[{}]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function shortAuthors(authors) {
  if (!authors) return "";
  const last = (name) => (name.includes(",") ? name.split(",")[0] : name.split(" ").pop()).trim();
  const names = authors.split(/\s+and\s+/i).filter(Boolean);
  if (names.length === 1) return last(names[0]);
  if (names.length === 2) return `${last(names[0])} and ${last(names[1])}`;
  return `${last(names[0])} et al.`;
}

function scanBib(text) {
  const cites = [];
  const entry = /@([a-zA-Z]+)\s*[{(]\s*([^,\s{}()"#%'=]+)\s*,/g;
  for (let m = entry.exec(text); m; m = entry.exec(text)) {
    const type = m[1].toLowerCase();
    if (type === "string" || type === "comment" || type === "preamble") continue;
    // The entry runs to the brace that closes it, or to the next entry.
    let depth = 1;
    let end = entry.lastIndex;
    while (end < text.length && depth > 0 && end - entry.lastIndex < 20000) {
      if (text[end] === "{" || text[end] === "(") depth++;
      else if (text[end] === "}" || text[end] === ")") depth--;
      end++;
    }
    const body = text.slice(entry.lastIndex, end);
    cites.push({
      key: m[2],
      type,
      title: field(body, "title"),
      authors: shortAuthors(field(body, "author") || field(body, "editor")),
      year: field(body, "year") || field(body, "date").slice(0, 4),
    });
  }
  return { labels: [], commands: [], environments: [], cites };
}

// What the completions and the outline (editor-outline.js) know about the
// project. `changed(events)` takes the events of the "files" map's
// observeDeep. `read(byPath)` takes the materialized files (livefiles.js) and
// returns everything, rescanning only the files that changed since the last
// read. `outlines` maps each LaTeX file's path to its headings and inputs.
export class ProjectIndex {
  constructor() {
    this.scans = new Map();
    this.dirty = new Set();
    this.everything = true;
    this.version = 0;
    this.result = null;
  }

  changed(events) {
    for (const e of events) {
      if (e.path.length) this.dirty.add(String(e.path[0]));
      else this.everything = true;
    }
    this.version += 1;
  }

  read(byPath) {
    if (this.result && this.result.version === this.version && this.result.byPath === byPath) return this.result;
    if (this.everything) this.scans.clear();
    for (const id of this.dirty) this.scans.delete(id);
    this.everything = false;
    this.dirty.clear();
    const result = { version: this.version, byPath, labels: [], cites: [], commands: new Map(), environments: new Set(), files: [], outlines: new Map() };
    for (const entry of byPath.values()) {
      result.files.push(entry);
      if (entry.kind !== "text" || !entry.text) continue;
      const kind = BIB_FILE.test(entry.shownAt) ? "bib" : LATEX_FILE.test(entry.shownAt) ? "tex" : null;
      if (!kind) continue;
      let scan = this.scans.get(entry.id);
      if (!scan || scan.path !== entry.shownAt) {
        const text = entry.text.toString();
        scan = { path: entry.shownAt, ...(kind === "bib" ? scanBib(text) : scanTex(text)) };
        this.scans.set(entry.id, scan);
      }
      for (const l of scan.labels) result.labels.push({ ...l, path: scan.path });
      for (const c of scan.cites) result.cites.push({ ...c, path: scan.path });
      for (const c of scan.commands) result.commands.set(c.name, c);
      for (const e of scan.environments) result.environments.add(e);
      if (scan.outline) result.outlines.set(scan.path, scan.outline);
    }
    this.result = result;
    return result;
  }
}

// --- Commands and environments -------------------------------------------------------

// [name, what follows the name in the snippet, a few words about it]. Fields
// are ${}. A field right after a backslash needs a name, because the snippet
// parser reads an empty one as an escaped brace. A command that takes a label, a key
// or a path opens the list for it.
const COMMANDS = [
  ["documentclass", "{${}}", "document class"],
  ["usepackage", "{${}}", "load a package"],
  ["title", "{${}}"], ["author", "{${}}"], ["date", "{${}}"], ["thanks", "{${}}"],
  ["maketitle", ""], ["tableofcontents", ""], ["appendix", ""], ["today", ""],
  ["part", "{${}}", "heading"], ["chapter", "{${}}", "heading"],
  ["section", "{${}}", "heading"], ["section*", "{${}}", "heading, unnumbered"],
  ["subsection", "{${}}", "heading"], ["subsection*", "{${}}", "heading, unnumbered"],
  ["subsubsection", "{${}}", "heading"], ["paragraph", "{${}}", "heading"], ["subparagraph", "{${}}", "heading"],
  ["begin", "{${}}", "environment"], ["end", "{${}}", "environment"],
  ["label", "{${}}", "name this place"],
  ["ref", "{${}}", "reference"], ["eqref", "{${}}", "equation reference"], ["autoref", "{${}}", "reference with its kind"],
  ["cref", "{${}}", "cleveref"], ["Cref", "{${}}", "cleveref, capitalized"], ["pageref", "{${}}", "page of a label"],
  ["nameref", "{${}}", "title of a label"], ["vref", "{${}}", "varioref"],
  ["cite", "{${}}", "citation"], ["citep", "{${}}", "natbib, in parentheses"], ["citet", "{${}}", "natbib, in the text"],
  ["citeauthor", "{${}}", "natbib"], ["citeyear", "{${}}", "natbib"], ["nocite", "{${}}", "list without citing"],
  ["parencite", "{${}}", "biblatex"], ["textcite", "{${}}", "biblatex"], ["autocite", "{${}}", "biblatex"],
  ["footcite", "{${}}", "biblatex"],
  ["bibliography", "{${}}", "bib files"], ["bibliographystyle", "{${}}"], ["addbibresource", "{${}}", "biblatex"],
  ["printbibliography", ""],
  ["input", "{${}}", "insert a file"], ["include", "{${}}", "insert a file on its own page"], ["includeonly", "{${}}"],
  ["includegraphics", "[width=\\linewidth]{${}}", "graphicx"], ["includegraphics*", "{${}}", "graphicx"],
  ["graphicspath", "{{${}}}"],
  ["caption", "{${}}"], ["centering", ""], ["footnote", "{${}}"], ["footnotemark", ""], ["footnotetext", "{${}}"],
  ["emph", "{${}}"], ["textbf", "{${}}", "bold"], ["textit", "{${}}", "italic"], ["texttt", "{${}}", "monospace"],
  ["textsc", "{${}}", "small caps"], ["textsf", "{${}}", "sans serif"], ["textrm", "{${}}", "roman"],
  ["textup", "{${}}"], ["textmd", "{${}}"], ["textnormal", "{${}}"], ["underline", "{${}}"],
  ["textsuperscript", "{${}}"], ["textsubscript", "{${}}"], ["textcolor", "{${color}}{${}}", "xcolor"],
  ["colorbox", "{${color}}{${}}", "xcolor"], ["color", "{${}}", "xcolor"], ["mbox", "{${}}"], ["makebox", "[${}]{${}}"],
  ["fbox", "{${}}"], ["parbox", "{${width}}{${}}"], ["raisebox", "{${}}{${}}"], ["scalebox", "{${}}{${}}"],
  ["resizebox", "{${width}}{!}{${}}"], ["rotatebox", "{${}}{${}}"],
  ["bfseries", ""], ["itshape", ""], ["ttfamily", ""], ["scshape", ""], ["normalfont", ""],
  ["tiny", ""], ["scriptsize", ""], ["footnotesize", ""], ["small", ""], ["normalsize", ""],
  ["large", ""], ["Large", ""], ["LARGE", ""], ["huge", ""], ["Huge", ""],
  ["url", "{${}}", "hyperref"], ["href", "{${url}}{${}}", "hyperref"], ["hyperref", "[${}]{${}}"],
  ["item", " ${}"], ["item[]", "", "item with a label"],
  ["newcommand", "{\\${name}}[${}]{${}}"], ["renewcommand", "{\\${name}}{${}}"],
  ["newenvironment", "{${name}}{${}}{${}}"], ["newtheorem", "{${name}}{${}}"],
  ["DeclareMathOperator", "{\\${name}}{${}}"], ["setlength", "{\\${length}}{${}}"], ["addtolength", "{\\${length}}{${}}"],
  ["vspace", "{${}}"], ["vspace*", "{${}}"], ["hspace", "{${}}"], ["hspace*", "{${}}"], ["vfill", ""], ["hfill", ""],
  ["smallskip", ""], ["medskip", ""], ["bigskip", ""], ["noindent", ""], ["indent", ""], ["par", ""],
  ["newline", ""], ["linebreak", ""], ["newpage", ""], ["clearpage", ""], ["cleardoublepage", ""], ["pagebreak", ""],
  ["linewidth", ""], ["textwidth", ""], ["columnwidth", ""], ["textheight", ""], ["baselineskip", ""],
  ["hline", "", "table rule"], ["cline", "{${}}"], ["toprule", "", "booktabs"], ["midrule", "", "booktabs"],
  ["bottomrule", "", "booktabs"], ["cmidrule", "{${}}", "booktabs"], ["addlinespace", "", "booktabs"],
  ["multicolumn", "{${n}}{${align}}{${}}"], ["multirow", "{${n}}{*}{${}}", "multirow"],
  ["tabularnewline", ""], ["arraystretch", ""], ["tabcolsep", ""],
  ["frac", "{${}}{${}}", "math"], ["dfrac", "{${}}{${}}", "math"], ["tfrac", "{${}}{${}}", "math"],
  ["sqrt", "{${}}", "math"], ["sum", "_{${}}^{${}}", "math"], ["prod", "_{${}}^{${}}", "math"],
  ["int", "_{${}}^{${}}", "math"], ["oint", "", "math"], ["lim", "_{${}}", "math"], ["max", "_{${}}", "math"],
  ["min", "_{${}}", "math"], ["arg", "", "math"], ["argmax", "", "math"], ["argmin", "", "math"],
  ["log", "", "math"], ["ln", "", "math"], ["exp", "", "math"], ["sin", "", "math"], ["cos", "", "math"], ["tan", "", "math"],
  ["infty", "", "math"], ["partial", "", "math"], ["nabla", "", "math"], ["forall", "", "math"], ["exists", "", "math"],
  ["in", "", "math"], ["notin", "", "math"], ["subset", "", "math"], ["subseteq", "", "math"], ["supset", "", "math"],
  ["cup", "", "math"], ["cap", "", "math"], ["setminus", "", "math"], ["emptyset", "", "math"],
  ["leq", "", "math"], ["geq", "", "math"], ["neq", "", "math"], ["approx", "", "math"], ["equiv", "", "math"],
  ["sim", "", "math"], ["propto", "", "math"], ["ll", "", "math"], ["gg", "", "math"], ["pm", "", "math"],
  ["times", "", "math"], ["cdot", "", "math"], ["cdots", "", "math"], ["ldots", "", "dots"], ["dots", "", "dots"],
  ["vdots", "", "math"], ["ddots", "", "math"], ["to", "", "math"], ["rightarrow", "", "math"], ["leftarrow", "", "math"],
  ["Rightarrow", "", "math"], ["Leftrightarrow", "", "math"], ["mapsto", "", "math"], ["implies", "", "math"],
  ["iff", "", "math"], ["mid", "", "math"], ["vert", "", "math"], ["lVert", "", "math"], ["rVert", "", "math"],
  ["left", "(${}\\right)", "math"], ["right", "", "math"], ["big", "", "math"], ["Big", "", "math"],
  ["langle", "", "math"], ["rangle", "", "math"], ["lfloor", "", "math"], ["rfloor", "", "math"],
  ["lceil", "", "math"], ["rceil", "", "math"],
  ["mathbb", "{${}}", "math"], ["mathcal", "{${}}", "math"], ["mathbf", "{${}}", "math"], ["mathrm", "{${}}", "math"],
  ["mathit", "{${}}", "math"], ["mathsf", "{${}}", "math"], ["mathtt", "{${}}", "math"], ["boldsymbol", "{${}}", "math"],
  ["text", "{${}}", "text in math"], ["operatorname", "{${}}", "math"], ["hat", "{${}}", "math"], ["bar", "{${}}", "math"],
  ["tilde", "{${}}", "math"], ["vec", "{${}}", "math"], ["dot", "{${}}", "math"], ["overline", "{${}}", "math"],
  ["underbrace", "{${}}_{${}}", "math"], ["overbrace", "{${}}^{${}}", "math"], ["binom", "{${}}{${}}", "math"],
  ["quad", "", "space"], ["qquad", "", "space"], ["displaystyle", "", "math"], ["limits", "", "math"],
  ["alpha", "", "Greek"], ["beta", "", "Greek"], ["gamma", "", "Greek"], ["delta", "", "Greek"], ["epsilon", "", "Greek"],
  ["varepsilon", "", "Greek"], ["zeta", "", "Greek"], ["eta", "", "Greek"], ["theta", "", "Greek"], ["vartheta", "", "Greek"],
  ["iota", "", "Greek"], ["kappa", "", "Greek"], ["lambda", "", "Greek"], ["mu", "", "Greek"], ["nu", "", "Greek"],
  ["xi", "", "Greek"], ["pi", "", "Greek"], ["rho", "", "Greek"], ["sigma", "", "Greek"], ["tau", "", "Greek"],
  ["upsilon", "", "Greek"], ["phi", "", "Greek"], ["varphi", "", "Greek"], ["chi", "", "Greek"], ["psi", "", "Greek"],
  ["omega", "", "Greek"], ["Gamma", "", "Greek"], ["Delta", "", "Greek"], ["Theta", "", "Greek"], ["Lambda", "", "Greek"],
  ["Xi", "", "Greek"], ["Pi", "", "Greek"], ["Sigma", "", "Greek"], ["Phi", "", "Greek"], ["Psi", "", "Greek"], ["Omega", "", "Greek"],
  ["si", "{${}}", "siunitx"], ["SI", "{${}}{${}}", "siunitx"], ["num", "{${}}", "siunitx"], ["qty", "{${}}{${}}", "siunitx"],
  ["unit", "{${}}", "siunitx"], ["percent", "", "siunitx"],
  ["todo", "{${}}", "todonotes"], ["enquote", "{${}}", "csquotes"], ["xspace", "", "xspace"],
  ["lstinline", "|${}|", "listings"], ["verb", "|${}|"], ["lstinputlisting", "{${}}", "listings"],
  ["subfloat", "[${}]{${}}"], ["subcaption", "{${}}"], ["subref", "{${}}"],
  ["acmConference", "[${short}]{${name}}{${date}}{${venue}}", "acmart"], ["acmYear", "{${}}", "acmart"],
  ["acmDOI", "{${}}", "acmart"], ["affiliation", "{${}}", "acmart"], ["institution", "{${}}", "acmart"],
  ["email", "{${}}", "acmart"], ["orcid", "{${}}", "acmart"], ["keywords", "{${}}", "acmart"],
  ["ccsdesc", "[${}]{${}}", "acmart"], ["settopmatter", "{${}}", "acmart"],
  ["IEEEauthorblockN", "{${}}", "IEEEtran"], ["IEEEauthorblockA", "{${}}", "IEEEtran"], ["IEEEPARstart", "{${}}{${}}", "IEEEtran"],
  ["institute", "{${}}", "llncs"], ["authorrunning", "{${}}", "llncs"], ["titlerunning", "{${}}", "llncs"],
  ["LaTeX", ""], ["TeX", ""], ["ie", ""], ["eg", ""], ["etal", ""],
];

// What the arguments of these commands are, so inserting one opens the next list.
const ARGUMENT_LISTS = new Set(["begin", "end", "ref", "eqref", "autoref", "cref", "Cref", "pageref", "nameref", "vref",
  "cite", "citep", "citet", "citeauthor", "citeyear", "nocite", "parencite", "textcite", "autocite", "footcite",
  "input", "include", "includeonly", "includegraphics", "includegraphics*", "bibliography", "addbibresource",
  "usepackage", "documentclass", "lstinputlisting", "subref"]);

const ENVIRONMENTS = [
  "document", "abstract", "figure", "figure*", "table", "table*", "tabular", "tabularx", "tabular*", "longtable",
  "itemize", "enumerate", "description", "equation", "equation*", "align", "align*", "alignat", "gather", "gather*",
  "multline", "multline*", "split", "cases", "matrix", "pmatrix", "bmatrix", "vmatrix", "array", "subequations",
  "center", "flushleft", "flushright", "minipage", "quote", "quotation", "verse", "verbatim", "lstlisting", "minted",
  "theorem", "lemma", "proposition", "corollary", "definition", "example", "remark", "proof", "algorithm",
  "algorithmic", "algorithm2e", "subfigure", "wrapfigure", "thebibliography", "tikzpicture", "axis", "frame",
  "columns", "column", "block", "acks", "CCSXML", "comment", "footnotesize", "small",
];

// The insides of a few environments, with fields. Every other one gets an empty line.
const BODIES = {
  itemize: "\t\\item ${}", enumerate: "\t\\item ${}", description: "\t\\item[${}] ${}",
  figure: "\t\\centering\n\t\\includegraphics[width=\\linewidth]{${}}\n\t\\caption{${}}\n\t\\label{fig:${}}",
  "figure*": "\t\\centering\n\t\\includegraphics[width=\\textwidth]{${}}\n\t\\caption{${}}\n\t\\label{fig:${}}",
  table: "\t\\centering\n\t\\caption{${}}\n\t\\label{tab:${}}\n\t\\begin{tabular}{${}}\n\t\t${}\n\t\\end{tabular}",
  "table*": "\t\\centering\n\t\\caption{${}}\n\t\\label{tab:${}}\n\t\\begin{tabular}{${}}\n\t\t${}\n\t\\end{tabular}",
  equation: "\t${}\n\t\\label{eq:${}}", align: "\t${}\n\t\\label{eq:${}}",
  frame: "\t\\frametitle{${}}\n\t${}",
};
const ARGUMENTS = { tabular: "{${cols}}", tabularx: "{\\linewidth}{${cols}}", minipage: "{${width}}", array: "{${cols}}",
  subfigure: "{${width}}", wrapfigure: "{r}{${width}}", alignat: "{${}}", thebibliography: "{${}}", column: "{${width}}" };

const PACKAGES = [
  "amsmath", "amssymb", "amsthm", "mathtools", "graphicx", "xcolor", "hyperref", "cleveref", "booktabs", "tabularx",
  "multirow", "array", "longtable", "siunitx", "listings", "minted", "subcaption", "caption", "float", "wrapfig",
  "natbib", "biblatex", "csquotes", "babel", "fontenc", "inputenc", "lmodern", "microtype", "geometry", "enumitem",
  "todonotes", "xspace", "url", "algorithm", "algorithmic", "algpseudocode", "algorithm2e", "tikz", "pgfplots",
  "balance", "comment", "soul", "ulem", "framed", "mdframed", "tcolorbox", "fancyhdr", "setspace", "titlesec",
];
const CLASSES = ["article", "acmart", "IEEEtran", "llncs", "lipics-v2021", "report", "book", "beamer", "memoir",
  "scrartcl", "elsarticle", "revtex4-2", "amsart", "sig-alternate", "usenix"];

// --- The completion source ---------------------------------------------------------------

// A completion list for the editor. `cm` is the editor bundle, `read()` gives
// ProjectIndex.read() for the current files.
export function latexAutocomplete(cm, read) {
  const commandOptions = COMMANDS.map(([name, args, detail]) => ({
    label: `\\${name}`,
    detail,
    type: "keyword",
    apply: insertSnippet(cm, `\\${name.replace(/\[\]$/, "[${}]")}${args}`, ARGUMENT_LISTS.has(name)),
  }));
  const known = new Set(commandOptions.map((o) => o.label));
  return cm.autocompletion({
    override: [(context) => complete(cm, read, context, commandOptions, known)],
    icons: false,
    maxRenderedOptions: 80,
  });
}

function insertSnippet(cm, template, reopen) {
  return (view, completion, from, to) => {
    cm.snippet(template)(view, completion, from, to);
    if (reopen) setTimeout(() => cm.startCompletion(view), 0);
  };
}

// The start of the item the cursor is in, in a comma separated argument.
function itemStart(pos, arg) {
  const tail = arg.slice(arg.lastIndexOf(",") + 1);
  return pos - tail.length + (tail.length - tail.trimStart().length);
}

function complete(cm, read, context, commandOptions, known) {
  const line = context.state.doc.lineAt(context.pos);
  const before = line.text.slice(0, context.pos - line.from);
  if (uncomment(before) !== before) return null;
  let m;

  if ((m = /\\(begin|end)\{([^{}\s]*)$/.exec(before))) {
    const project = read();
    const names = [...new Set([...ENVIRONMENTS, ...project.environments])];
    const options = names.map((name) => m[1] === "begin"
      ? { label: name, type: "type", apply: beginEnvironment(cm, name) }
      : { label: name, type: "type", apply: closeArgument(name) });
    if (m[1] === "end") {
      const open = openEnvironment(context.state.sliceDoc(0, context.pos - m[0].length));
      if (open) options.forEach((o) => { if (o.label === open) o.boost = 99; });
    }
    return { from: context.pos - m[2].length, options, validFor: /^[^{}\s]*$/ };
  }

  if ((m = /\\(?:ref|eqref|autoref|pageref|nameref|vref|Vref|cref|Cref|cpageref|Cpageref|labelcref|crefrange|Crefrange|subref)\*?\{([^{}]*)$/.exec(before))) {
    const seen = new Set();
    const options = [];
    for (const l of read().labels) {
      if (seen.has(l.name)) continue;
      seen.add(l.name);
      options.push({ label: l.name, detail: `${l.path}:${l.line}`, info: l.text, type: "variable" });
    }
    return { from: itemStart(context.pos, m[1]), options, validFor: /^[^,{}\s]*$/ };
  }

  if ((m = /\\[a-zA-Z]*cite[a-zA-Z]*\*?(?:\[[^\]]*\]){0,2}\{([^{}]*)$/.exec(before))) {
    const seen = new Set();
    const options = [];
    for (const c of read().cites) {
      if (seen.has(c.key)) continue;
      seen.add(c.key);
      const title = c.title.length > 64 ? `${c.title.slice(0, 63)}…` : c.title;
      const who = [c.authors, c.year].filter(Boolean).join(", ");
      options.push({
        label: c.key,
        detail: title || who,
        info: [who, c.title, c.path].filter(Boolean).join("\n"),
        type: "variable",
      });
    }
    return { from: itemStart(context.pos, m[1]), options, validFor: /^[^,{}\s]*$/ };
  }

  if ((m = /\\(input|include|includeonly|subfile|includegraphics\*?|includepdf|lstinputlisting|verbatiminput|bibliography|addbibresource|usepackage|RequirePackage|documentclass)(?:\[[^\]]*\])?\{([^{}]*)$/.exec(before))) {
    const options = argumentOptions(m[1], read().files);
    const list = ["includeonly", "bibliography", "usepackage", "RequirePackage"].includes(m[1]);
    return { from: list ? itemStart(context.pos, m[2]) : context.pos - m[2].length, options, validFor: list ? /^[^,{}\s]*$/ : /^[^{}]*$/ };
  }

  if ((m = /\\[a-zA-Z@]*\*?$/.exec(before)) && (m[0].length > 1 || context.explicit)) {
    const own = [...read().commands.values()]
      .filter((c) => !known.has(`\\${c.name}`))
      .map((c) => ({
        label: `\\${c.name}`, detail: "defined in this paper", type: "keyword",
        apply: insertSnippet(cm, `\\${c.name}${"{${}}".repeat(c.args)}`, false),
      }));
    return { from: context.pos - m[0].length, options: [...own, ...commandOptions], validFor: /^\\[a-zA-Z@]*\*?$/ };
  }
  return null;
}

function argumentOptions(command, files) {
  const paths = (test) => files.filter((f) => test.test(f.shownAt)).map((f) => f.shownAt).sort();
  const noExtension = (p, ext) => p.replace(ext, "");
  switch (command) {
    case "input": case "include": case "includeonly": case "subfile":
      return paths(TEX_FILE).map((p) => ({ label: noExtension(p, TEX_FILE), detail: p, type: "text" }));
    case "includegraphics": case "includegraphics*": case "includepdf":
      return paths(GRAPHIC).map((p) => ({ label: p, type: "text" }));
    case "bibliography":
      return paths(BIB_FILE).map((p) => ({ label: noExtension(p, BIB_FILE), detail: p, type: "text" }));
    case "addbibresource":
      return paths(BIB_FILE).map((p) => ({ label: p, type: "text" }));
    case "usepackage": case "RequirePackage":
      return [...PACKAGES.map((p) => ({ label: p, type: "namespace" })),
        ...paths(/\.sty$/i).map((p) => ({ label: p.replace(/\.sty$/i, ""), detail: "in this paper", type: "namespace" }))];
    case "documentclass":
      return [...CLASSES.map((c) => ({ label: c, type: "class" })),
        ...paths(/\.cls$/i).map((p) => ({ label: p.replace(/\.cls$/i, ""), detail: "in this paper", type: "class" }))];
    default:
      return files.filter((f) => f.kind === "text").map((f) => ({ label: f.shownAt, type: "text" }));
  }
}

// The name, and the closing brace when it is not there yet.
function closeArgument(name) {
  return (view, completion, from, to) => {
    const brace = view.state.sliceDoc(to, to + 1) === "}";
    view.dispatch({
      changes: { from, to: brace ? to + 1 : to, insert: `${name}}` },
      selection: { anchor: from + name.length + 1 },
      userEvent: "input.complete",
    });
  };
}

// \begin{name} with its arguments, its body and \end{name}.
function beginEnvironment(cm, name) {
  const template = `${name}}${ARGUMENTS[name] || ""}\n${BODIES[name] || "\t${}"}\n\\end{${name}}`;
  return (view, completion, from, to) => {
    const brace = view.state.sliceDoc(to, to + 1) === "}";
    cm.snippet(template)(view, completion, from, brace ? to + 1 : to);
  };
}

// The innermost environment that is open before `text`, which \end{ should close.
function openEnvironment(text) {
  const stack = [];
  for (const m of text.matchAll(/\\(begin|end)\{([^{}]+)\}/g)) {
    if (m[1] === "begin") stack.push(m[2]);
    else if (stack[stack.length - 1] === m[2]) stack.pop();
  }
  return stack.pop() || null;
}
