"""The checker against a real LanguageTool (DESIGN §17).

Skipped unless GRAMMAR_TEST_URL points at one, for instance
``docker run -d -p 127.0.0.1:8011:8010 erikvl87/languagetool:6.8`` and
``GRAMMAR_TEST_URL=http://127.0.0.1:8011``. What it pins is what a fake
cannot. A correct paper in LaTeX gets no underlines at all, and the
offsets a real LanguageTool reports for annotated text land on the typo in
the source, in the UTF-16 units the editor counts in.
"""

import os

import httpx
import pytest

from app.config import Settings
from app.grammar import checker

URL = os.environ.get("GRAMMAR_TEST_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="GRAMMAR_TEST_URL not set")


class Counting(httpx.HTTPTransport):
    """The network, counted."""

    def __init__(self):
        super().__init__()
        self.count = 0

    def handle_request(self, request):
        self.count += 1
        return super().handle_request(request)


@pytest.fixture
def live(monkeypatch):
    settings = Settings(_env_file=None, grammar_url=URL, grammar_timeout_seconds=60)
    monkeypatch.setattr(checker, "get_settings", lambda: settings)
    transport = Counting()
    checker.use_transport(transport)
    yield transport
    checker.use_transport(None)


def config(language="en-US", **values) -> dict:
    return {**checker.defaults(), "language": language, **values}


def shown(text: str, result: dict) -> list[tuple[str, str]]:
    """The matches as (rule, the source they cover), for assertion messages."""
    return [(m["rule"], checker._slice(text, m["offset"], m["length"])) for m in result["matches"]]


def utf16(text: str, word: str) -> int:
    """Where JavaScript's indexOf finds ``word`` in ``text``."""
    return len(text[: text.index(word)].encode("utf-16-le")) // 2


PAPER = r"""\documentclass{article}
\usepackage{amsmath}
\title{Measuring the Energy of Software}
\begin{document}
\maketitle

\begin{abstract}
We present a method for measuring the energy consumption of software. % TODO: shorten
Our results show a reduction of 30\,\% compared to the baseline~\cite{smith2020,doe2021}.
\end{abstract}

\begin{IEEEkeywords}
green software, energy measurement, record and replay
\end{IEEEkeywords}

\section{Introduction}\label{sec:intro}

Software consumes a growing share of the world's electricity~\cite{jones2019}.
As shown in Section~\ref{sec:method}, the energy $E$ of a run is the integral of
the power $P(t)$ over its duration $T$.\footnote{We ignore idle power here.}
In particular, we compute
\begin{equation}
  E = \int_0^T P(t)\, dt,
  \label{eq:energy}
\end{equation}
where $P$ is measured with RAPL~\cite{intel2016}.
Formally, for an $n$-dimensional input $x \in \mathbb{R}^n$ we report an
$\ell_2$ norm \( \|x\|_2 \) and a running time of $O(n \log n)$.
The $i$th run starts after a pause of $k$~seconds.
The overhead is
\[
  o = \frac{E_{\text{tool}} - E_{\text{base}}}{E_{\text{base}}}.
\]

Our contributions are:

\begin{itemize}
  \item a new method for measuring energy (Section~\ref{sec:method});
  \item an open dataset of 1\,200 runs;
  \item an evaluation on three machines --- see Table~\ref{tab:results}.
\end{itemize}

\begin{figure}[t]
  \centering
  \includegraphics[width=\linewidth]{figures/setup.pdf}
  \caption{The measurement setup with two machines.}
  \label{fig:setup}
\end{figure}

\begin{table}[t]
  \centering
  \caption{Results for each machine.}
  \label{tab:results}
  \begin{tabular}{lrr}
    \toprule
    Machine & Energy (J) & Time (s) \\
    \midrule
    A & 12.3 & 4.5 \\
    \bottomrule
  \end{tabular}
\end{table}

\subsection{Related work}
Earlier work by Smith et al.~\cite{smith2020} measured pages 10--20 of the
\emph{Green Software} report, and \textcite{doe2021} extended it.
As M\"{u}ller~\cite[p.~5]{mueller2018} put it, ``energy is the new performance''.
The code is available at \url{https://example.com/code} and in our
\href{https://github.com/example/repo}{repository}.
We use \texttt{perf}, and the \LaTeX{} sources are online.

\paragraph{Setup.} We ran all experiments on an idle machine\footnote{A server
with 32\,GB of memory.} with Turbo Boost disabled.
\noindent Each run is repeated ten times and used \SI{30}{\joule} on an \ac{LLM}.\\
\vspace{1em}
We thank the reviewers.

% Numbers from a generated file, as argument-less macros.
Replaying cuts the spread by a median factor of
\RepSessSDRatioMedian, because it removes the drift of the machine. A check
costs \OvhCheckJ\,J (\OvhCheckJLo{} to \OvhCheckJHi) on a virtual display.
\CorpusScriptedApps{} applications were recorded with driver scripts, and
Parrot measured the real page of \WebBlockParrotOk{} of them. All
\VerChecks{} checks of \VerGoodRuns{} runs passed.
Every run has a \texttt{[BASELINE]} and an \texttt{[IDLE]} phase, and we ran
an \texttt{ollama} container with an \texttt{energy\_model} for each endpoint
(Alibaba).\footnote{\raggedright Model IDs are listed in the artifact. }

\begin{verbatim}
$ gmt run --uri .

$ gmt report  % teh output
\end{verbatim}

\end{document}
"""

GERMAN = r"""\section{Einleitung}
Software verbraucht einen wachsenden Anteil des weltweiten Stroms~\cite{jones2019}.
Wie in Abschnitt~\ref{sec:method} gezeigt, ist die Energie $E$ eines Laufs das
Integral der Leistung $P(t)$ \"uber die Dauer $T$.\footnote{Die Leerlaufleistung
wird hier nicht ber\"ucksichtigt.} Die Ergebnisse sind in Tabelle~\ref{tab:x}
zusammengefasst, z.\,B. f\"ur drei Rechner --- siehe auch \emph{Anhang A}.

Unsere Beitr\"age sind:
\begin{itemize}
  \item eine neue Methode zur Messung;
  \item ein offener Datensatz.
\end{itemize}
"""


def test_a_correct_paper_stays_quiet(live):
    result = checker.check(PAPER, config())
    assert result["complete"] is True
    assert result["matches"] == [], shown(PAPER, result)


def test_a_correct_german_section_stays_quiet(live):
    result = checker.check(GERMAN, config("de-DE"))
    assert result["matches"] == [], shown(GERMAN, result)


def test_typos_are_found_where_they_are_in_the_source(live):
    text = (
        "\\section{Introdcution}\n\n"
        "Let 𝔸 be the set $\\mathbb{A}$, and we recieve a value.\\footnote{It is seperate.}\n"
        "\\begin{equation}\n  x = 1,\n\\end{equation}\n"
        "where the \\emph{definately} correct result occured~\\cite{a}.\n\n"
        "\\begin{itemize}\n  \\item a mesurement;\n\\end{itemize}\n"
        "\\begin{figure}\\caption{The resluts.}\\end{figure}"
    )
    result = checker.check(text, config())
    spelling = {(m["offset"], m["length"]) for m in result["matches"] if m["type"] == "spelling"}
    expected = {(utf16(text, w), len(w)) for w in
                ("Introdcution", "recieve", "seperate", "definately", "occured", "mesurement", "resluts")}
    assert spelling == expected, shown(text, result)
    # The UTF-16 count matters: 𝔸 is before most of them.
    assert utf16(text, "recieve") == text.index("recieve") + 1
    assert all(m["type"] == "spelling" for m in result["matches"]), shown(text, result)


def test_a_and_an_before_math(live):
    for text, flagged in [("This is a $x$ value.", True), ("This is an $x$ value.", False),
                          ("This is an $k$ value.", True), ("This is a $k$ value.", False),
                          ("This is an $\\ell_2$ norm.", False)]:
        result = checker.check(text, config())
        assert any(m["rule"] == "EN_A_VS_AN" for m in result["matches"]) is flagged, (text, shown(text, result))


def test_auto_detects_the_language_and_the_next_check_is_cached(live):
    result = checker.check(GERMAN, config("auto"))
    code = result["language"]["code"]
    assert code.startswith("de") and result["language"]["auto"] is True
    assert result["matches"] == [], shown(GERMAN, result)
    requests = live.count
    again = checker.check(GERMAN, config("auto"), language=code)
    assert live.count == requests
    assert again["language"]["code"] == code and again["language"]["name"]


def test_the_language_list(live):
    codes = {row["code"] for row in checker.languages()}
    assert {"en-US", "de-DE"} <= codes
