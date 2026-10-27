"""The page limit check (app/static/js/pagelimit.js) on PDFs that LaTeX made.

Compiles small papers with the article, IEEEtran, acmart and llncs classes,
with the references in the middle of a page, at the top of one, in the right
column and after a table that has a References cell. Two labels tell where
the text really ends and where the references really start, read from the
.aux file. PDF.js (the vendored copy in app/static/vendor/pdfjs) reads each
PDF in node and pagelimit.js has to find the same pages. The script also
checks how page limits are read and what the warnings say. It needs node and
pdflatex with those classes and lipsum (see tools/editor/README.md).
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

NODE = """
import { readFileSync } from "node:fs";
const [pdfjsUrl, workerUrl, pagelimitUrl] = process.argv.slice(1);
const pdfjs = await import(pdfjsUrl);
pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;
const { parseLimit, measure, checkLimit } = await import(pagelimitUrl);
const input = JSON.parse(readFileSync(0, "utf8"));
const finite = (l) => l && { ...l, references: l.references === Infinity ? "unlimited" : l.references };
const out = { limits: input.limits.map((t) => finite(parseLimit(t))), pdfs: [], checks: [] };
for (const path of input.pdfs) {
  const doc = await pdfjs.getDocument({ data: new Uint8Array(readFileSync(path)), isEvalSupported: false, disableFontFace: true }).promise;
  const pages = [];
  for (let i = 1; i <= doc.numPages; i++) {
    const page = await doc.getPage(i);
    pages.push({ view: page.view, items: (await page.getTextContent()).items });
  }
  out.pdfs.push(measure(pages));
  await doc.loadingTask.destroy();
}
for (const [text, m] of input.checks) out.checks.push(checkLimit(parseLimit(text), m));
console.log(JSON.stringify(out));
"""

LIPSUM = r"\usepackage{lipsum}"
BIB = r"""
\begin{thebibliography}{9}
\bibitem{a}\label{refsstart} A. Author. A paper about things. In \emph{Proceedings of a Conference}, 2026.
\bibitem{b} B. Author and C. Author. Another paper. \emph{A Journal}, 12(3), 2025.
\bibitem{c} D. Author. A book. Publisher, 2024.
\end{thebibliography}
"""
END = r"End of the text.\label{textend}" "\n"
FANCY = r"""\usepackage{fancyhdr}
\pagestyle{fancy}\fancyhf{}\lhead{Draft of a paper for a conference}\rhead{\thepage}\cfoot{\thepage}
"""
TABLE = r"""
\begin{table}[h]\centering
\begin{tabular}{ll}Tool & References\\ A & [1]\\ B & [2]\end{tabular}
\caption{Tools and their references.}
\end{table}
"""
ACM = r"""\documentclass[sigconf]{acmart}
\settopmatter{printacmref=false}
\setcopyright{none}
\renewcommand\footnotetextcopyrightpermission[1]{}
""" + LIPSUM + r"""
\begin{document}
\title{A Paper}
\author{An Author}
\affiliation{\institution{A University}\city{A City}\country{A Country}}
\begin{abstract}\lipsum[1]\end{abstract}
\maketitle
"""


def doc(head: str, body: str, *, start: str = r"\begin{document}") -> str:
    return head + "\n" + start + "\n" + body + "\n" + r"\end{document}" + "\n"


# name: the paper's source.
PAPERS = {
    "article, references mid-page": doc(r"\documentclass{article}" + LIPSUM, r"\lipsum[1-9]" "\n\n" + END + BIB),
    "article, references at the top, running heads": doc(
        r"\documentclass{article}" + LIPSUM + FANCY, r"\lipsum[1-7]" "\n\n" + END + r"\clearpage" + BIB),
    "article, one line of text above the references": doc(
        r"\documentclass{article}" + LIPSUM + FANCY, r"\lipsum[1-7]" "\n\n" r"\clearpage" "\n" + END + BIB),
    "article, numbered References section": doc(
        r"\documentclass{article}" + LIPSUM,
        r"\section{Introduction}\lipsum[1-6]" "\n\n" + END
        + r"\section{References}" "\n" r"\begin{enumerate}\item\label{refsstart} A. Author. A paper. 2026.\item B. Author. A book. 2025.\end{enumerate}"),
    "article, table with a References cell before them": doc(
        r"\documentclass{article}" + LIPSUM, r"\lipsum[1-3]" + TABLE + r"\lipsum[4-8]" "\n\n" + END + BIB),
    "article, German Literatur": doc(
        r"\documentclass{article}\usepackage[ngerman]{babel}" + LIPSUM, r"\lipsum[1-6]" "\n\n" + END + BIB),
    "article, no references": doc(r"\documentclass{article}" + LIPSUM, r"\lipsum[1-9]" "\n\n" + END),
    "IEEEtran, references mid-column": doc(
        r"\documentclass[conference]{IEEEtran}" + LIPSUM, r"\lipsum[1-14]" "\n\n" + END + BIB),
    "IEEEtran, references at the top of a page": doc(
        r"\documentclass[conference]{IEEEtran}" + LIPSUM, r"\lipsum[1-14]" "\n\n" + END + r"\clearpage" + BIB),
    "IEEEtran, references at the top of the right column": doc(
        r"\documentclass[conference]{IEEEtran}" + LIPSUM, r"\lipsum[1-11]" "\n\n" + END + r"\newpage" + BIB),
    "acmart sigconf, references mid-page": ACM + r"\lipsum[2-16]" "\n\n" + END + BIB + r"\end{document}" "\n",
    "acmart sigconf, references at the top of a page": ACM + r"\lipsum[2-16]" "\n\n" + END + r"\clearpage" + BIB + r"\end{document}" "\n",
    "acmart sigconf review, line numbers, references at the top of a page":
        ACM.replace("[sigconf]", "[sigconf,review]") + r"\lipsum[2-16]" "\n\n" + END + r"\clearpage" + BIB + r"\end{document}" "\n",
    "acmart sigconf review, one line of text above the references":
        ACM.replace("[sigconf]", "[sigconf,review]") + r"\lipsum[2-16]" "\n\n" r"\clearpage" "\n" + END + BIB + r"\end{document}" "\n",
    "llncs, references mid-page": doc(
        r"\documentclass{llncs}" + LIPSUM, r"\title{A Paper}\author{An Author}\institute{A University}\maketitle" "\n" r"\lipsum[1-8]" "\n\n" + END + BIB),
}

# The limit as written, and how it must be read.
LIMITS = {
    "8": {"pages": 8, "references": 0},
    "8 pages": {"pages": 8, "references": 0},
    "6 pages including references": {"pages": 6, "references": 0},
    "15 pages incl. references": {"pages": 15, "references": 0},
    "must not exceed 8 pages including references": {"pages": 8, "references": 0},
    "10 pages with references": {"pages": 10, "references": 0},
    "6-10 pages": {"pages": 10, "references": 0},
    "two-column, 6 pages": {"pages": 6, "references": 0},
    "ACM sigconf 2-column, max 10 pages": {"pages": 10, "references": 0},
    "8.5 x 11 in, 10 pages": {"pages": 10, "references": 0},
    "ICSE 2027: 10 pages": {"pages": 10, "references": 0},
    "Long papers 10 pages, short papers 4 pages": {"pages": 10, "references": 0},
    "10pp": {"pages": 10, "references": 0},
    "8 + unlimited references": {"pages": 8, "references": "unlimited"},
    "8 pages + references": {"pages": 8, "references": "unlimited"},
    "eight pages plus references": {"pages": 8, "references": "unlimited"},
    "12 pages, references not counted": {"pages": 12, "references": "unlimited"},
    "8 pages excluding references": {"pages": 8, "references": "unlimited"},
    "8 pages, references not included": {"pages": 8, "references": "unlimited"},
    "8 pages without references": {"pages": 8, "references": "unlimited"},
    "8 pages, unlimited refs": {"pages": 8, "references": "unlimited"},
    "10 pages (excluding references and appendices)": {"pages": 10, "references": "unlimited"},
    "8 pages (references, appendices excluded)": {"pages": 8, "references": "unlimited"},
    "12 pages, references and appendices not counted": {"pages": 12, "references": "unlimited"},
    "10 pages + 2 for references": {"pages": 10, "references": 2},
    "10 pages + 2 pages of references": {"pages": 10, "references": 2},
    "4 pages + 1 page for references": {"pages": 4, "references": 1},
    "10 + 2": {"pages": 10, "references": 2},
    "": None,
    "no limit": None,
    "as long as it needs": None,
}

# [limit, measured, what checkLimit must say: the start of the message, or None when it keeps to it].
CHECKS = [
    ["8", {"pages": 8, "text": None, "references": None}, None],
    ["8", {"pages": 9, "text": None, "references": None}, "The PDF has 9 pages. The limit is 8 pages, references included."],
    ["8 + references", {"pages": 11, "text": 8, "references": 8}, None],
    ["8 + references", {"pages": 11, "text": 8, "references": 9}, None],
    ["8 + references", {"pages": 11, "text": 9, "references": 9},
     "The text runs onto page 9, where the references start. The limit is 8 pages plus the references."],
    ["8 + references", {"pages": 11, "text": 9, "references": 10},
     "The text takes 9 pages. The limit is 8 pages plus the references."],
    ["8 + references", {"pages": 8, "text": None, "references": None}, None],
    ["8 + references", {"pages": 9, "text": None, "references": None},
     "The PDF has 9 pages and no references heading was found in it. The limit is 8 pages plus the references."],
    ["10 + 2 for references", {"pages": 12, "text": 10, "references": 10}, None],
    ["10 + 2 for references", {"pages": 13, "text": 10, "references": 10},
     "The PDF has 13 pages. The limit is 10 pages plus 2 for the references."],
    ["10 + 2 for references", {"pages": 12, "text": 11, "references": 11},
     "The text runs onto page 11, where the references start. The limit is 10 pages plus 2 for the references."],
]


def label_page(aux: str, name: str) -> int | None:
    m = re.search(r"\\newlabel\{" + name + r"\}\{\{[^{}]*\}\{(\d+)\}", aux)
    return int(m.group(1)) if m else None


def build(folder: Path) -> list[tuple[str, Path, dict]]:
    """Compile every paper. Returns (name, pdf, the pages LaTeX says)."""
    out = []
    for i, (name, source) in enumerate(PAPERS.items()):
        tex = folder / f"paper{i}.tex"
        tex.write_text(source)
        run = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", tex.name],
                             cwd=folder, capture_output=True, text=True)
        pdf = tex.with_suffix(".pdf")
        if run.returncode != 0 or not pdf.exists():
            print(f"FAIL  {name}: pdflatex failed\n{run.stdout[-2000:]}")
            sys.exit(1)
        aux = tex.with_suffix(".aux").read_text(errors="replace")
        pages = int(re.search(r"Output written on .*?\((\d+) pages?", run.stdout, re.S).group(1))
        refs = label_page(aux, "refsstart")
        text = label_page(aux, "textend")
        if refs is None:
            want = {"pages": pages, "text": None, "references": None}
        else:
            # The text's last line is on the references' page, or the page before it.
            assert text in (refs, refs - 1), (name, text, refs)
            want = {"pages": pages, "text": text, "references": refs}
        out.append((name, pdf, want))
    return out


def main() -> None:
    for tool in ("node", "pdflatex"):
        if shutil.which(tool) is None:
            print(f"SKIP  {tool} is not installed")
            sys.exit(0)
    vendor = REPO / "app" / "static" / "vendor" / "pdfjs"
    pagelimit = REPO / "app" / "static" / "js" / "pagelimit.js"
    failed = 0
    with tempfile.TemporaryDirectory(prefix="meertex-pagelimit-") as tmp:
        papers = build(Path(tmp))
        payload = {"limits": list(LIMITS), "pdfs": [str(p) for _, p, _ in papers], "checks": [c[:2] for c in CHECKS]}
        result = subprocess.run(
            ["node", "--input-type=module", "-e", NODE, (vendor / "pdf.js").as_uri(), (vendor / "pdf.worker.js").as_uri(), pagelimit.as_uri()],
            input=json.dumps(payload), capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stderr)
            sys.exit(1)
        js = json.loads(result.stdout.strip().splitlines()[-1])
    for (text, want), got in zip(LIMITS.items(), js["limits"]):
        if got != want:
            failed += 1
            print(f"FAIL  limit {text!r}: {got}, wanted {want}")
    for (name, _, want), got in zip(papers, js["pdfs"]):
        if got != want:
            failed += 1
            print(f"FAIL  {name}: {got}, wanted {want}")
        else:
            print(f"ok    {name}: {got}")
    for (limit, measured, want), got in zip(CHECKS, js["checks"]):
        if got["message"] != want or got["over"] != (want is not None):
            failed += 1
            print(f"FAIL  check {limit!r} {measured}: {got}, wanted {want!r}")
    if failed:
        print(f"FAIL  {failed} checks failed")
        sys.exit(1)
    print(f"ok    {len(LIMITS)} limits read, {len(papers)} PDFs measured, {len(CHECKS)} checks said")


if __name__ == "__main__":
    main()
