"""LaTeX as text, names and citations (DESIGN §16)."""

import pytest

from app.references.latex import (
    Name,
    cites,
    family_key,
    fold,
    is_prefix_title,
    parse_name,
    sentence_at,
    similarity,
    split_names,
    to_text,
    words,
)

EM = "\u2014"
EN = "\u2013"
ELLIPSIS = "\u2026"


# --- to_text ------------------------------------------------------------------


@pytest.mark.parametrize(
    "latex, text",
    [
        ('\\"u', "ü"),
        ('\\"{u}', "ü"),
        ('{\\"u}', "ü"),
        ('{\\" u}', "ü"),
        ('\\"{O}', "Ö"),
        ("\\'e", "é"),
        ("\\'{E}", "É"),
        ("\\`e", "è"),
        ("\\^e", "ê"),
        ("\\~n", "ñ"),
        ("\\=a", "ā"),
        ("\\.z", "ż"),
        ("\\u{g}", "ğ"),
        ("\\u g", "ğ"),
        ("\\v{s}", "š"),
        ("\\v s", "š"),
        ("{\\v S}", "Š"),
        ("\\H{o}", "ő"),
        ("{\\H o}", "ő"),
        ("\\c{c}", "ç"),
        ("\\c c", "ç"),
        ("\\k{a}", "ą"),
        ("\\r{a}", "å"),
        ("\\d{s}", "ṣ"),
        ("\\b{b}", "ḇ"),
        ("\\t{oo}", "o\u0361o"),
        ("\\'{\\i}", "í"),
        ("\\'\\i", "í"),
        ('\\"{\\i}', "ï"),
        ("\\v{\\j}", "ǰ"),
    ],
)
def test_accents(latex, text):
    assert to_text(latex) == text


def test_accents_compose():
    # One code point each, not a letter and a combining mark.
    for latex in ('\\"u', "\\'e", "\\v{s}", "\\H{o}", "\\c{c}", "\\k{a}"):
        assert len(to_text(latex)) == 1


@pytest.mark.parametrize(
    "latex, text",
    [
        ("Stra\\ss e", "Straße"),
        ("Stra{\\ss}e", "Straße"),
        ("{\\aa}", "å"),
        ("\\AA{}ngstr\\\"om", "Ångström"),
        ("Bj{\\o}rn", "Bjørn"),
        ("Bj\\o rn", "Bjørn"),
        ("\\O", "Ø"),
        ("\\ae", "æ"),
        ("\\AE", "Æ"),
        ("{\\oe}uvre", "œuvre"),
        ("\\OE", "Œ"),
        ("{\\l}", "ł"),
        ("{\\L}ukasz", "Łukasz"),
        ("\\i", "ı"),
        ("\\j", "ȷ"),
    ],
)
def test_special_letters(latex, text):
    assert to_text(latex) == text


def test_escaped_characters_dashes_and_quotes():
    assert to_text("\\& \\% \\$ \\# \\_ \\{ \\}") == "& % $ # _ { }"
    assert to_text("Smith~and~Jones") == "Smith and Jones"
    assert to_text("pages 1--10") == f"pages 1{EN}10"
    assert to_text("yes---no") == f"yes{EM}no"
    assert to_text("a-b") == "a-b"
    assert to_text("``quoted''") == "\u201cquoted\u201d"
    assert to_text("`single'") == "\u2018single'"
    assert to_text("O'Brien") == "O'Brien"


def test_commands_keep_their_arguments():
    assert to_text("\\textit{x} \\emph{y} \\textbf{z} \\textsc{w} \\mbox{v}") == "x y z w v"
    assert to_text("\\url{https://example.org/~user/a--b}") == "https://example.org/~user/a--b"
    assert to_text("\\href{https://example.org}{the site}") == "the site"
    assert to_text("\\unknowncommand{kept}") == "kept"
    assert to_text("a \\relax b") == "a b"
    assert to_text("{\\em Deep} {\\bf Learning} with {\\sc Bert}") == "Deep Learning with Bert"
    assert to_text("\\label{sec:x}Results\\index{energy}") == "Results"


def test_known_commands_without_arguments():
    assert to_text("The \\LaTeX{} Companion") == "The LaTeX Companion"
    assert to_text("{\\TeX} and \\LaTeX\\ are") == "TeX and LaTeX are"
    assert to_text("1\\textendash 2 \\textemdash{} x") == f"1{EN}2 {EM} x"
    assert to_text("it\\textquoteright s") == "it\u2019s"
    assert to_text("and so on \\dots") == f"and so on {ELLIPSIS}"
    assert to_text("\\ldots") == ELLIPSIS


def test_math():
    assert to_text("$\\alpha$-Divergence") == "alpha-Divergence"
    assert to_text("An $O(n^2)$ algorithm") == "An O(n^2) algorithm"
    assert to_text("$\\mathcal{O}(n \\log n)$") == "O(n log n)"
    assert to_text("Cost of \\$5 and $x$") == "Cost of $5 and x"
    assert to_text("$$E = mc^2$$") == "E = mc^2"
    assert to_text("open $math") == "open math"


def test_braces_and_whitespace():
    assert to_text("{BERT}: Pre-training of {{Deep}} Bidirectional\n   Transformers") == (
        "BERT: Pre-training of Deep Bidirectional Transformers"
    )
    assert to_text("  stray } brace {  ") == "stray brace"
    assert to_text("") == ""
    assert to_text("{" * 5000 + "deep" + "}" * 5000) == "deep"


def test_real_names_and_titles():
    assert to_text("Erd{\\H{o}}s") == "Erdős"
    assert to_text("G{\\\"o}del") == "Gödel"
    assert to_text("Schr\\\"{o}dinger") == "Schrödinger"
    assert to_text("Fran\\c{c}ois") == "François"
    assert to_text("Dvo\\v{r}\\'ak") == "Dvořák"
    assert to_text("Ga{\\\"e}l Varoquaux") == "Gaël Varoquaux"
    assert to_text("Garc{\\'\\i}a-Molina") == "García-Molina"
    assert to_text("Grüße") == "Grüße"


# --- fold, words and similarity ----------------------------------------------------


def test_fold():
    assert fold("The {GPU}: A Survey!") == "the gpu a survey"
    assert fold("G{\\\"o}del") == fold("Gödel") == fold("GODEL") == "godel"
    assert fold("Stra{\\ss}e") == "strasse"
    assert fold("STRASSE") == "strasse"
    assert fold("Bjørn Ærø Œuvre Łódź Đorđe Þór") == "bjorn aero oeuvre lodz dorde thor"
    assert fold("García-Molina") == "garcia molina"
    assert fold("a --- b, c; (d)") == "a b c d"
    assert fold("snake_case") == "snake case"
    assert fold("") == ""
    assert fold("!!!") == ""


def test_words():
    assert words("Attention Is {All} You Need.") == ["attention", "is", "all", "you", "need"]
    assert words("") == []


def test_similarity():
    assert similarity("Attention is all you need", "{Attention} Is All You Need.") == 1.0
    assert similarity("", "x") == 0.0
    assert similarity("x", "") == 0.0
    assert similarity("", "") == 0.0
    assert 0.8 < similarity("Attention is all you need", "Attention is all you want") < 1.0
    assert similarity("Deep learning", "Quantum chromodynamics on the lattice") < 0.5


def test_similarity_of_long_titles():
    long = "A very long title about energy measurements of large language model inference " * 4
    changed = long.replace("energy", "power", 1)
    assert similarity(long, changed) > 0.95


def test_is_prefix_title():
    full = "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding"
    assert is_prefix_title("{BERT}: Pre-training of Deep Bidirectional Transformers", full)
    assert is_prefix_title(full, "BERT pre-training of deep")
    assert not is_prefix_title("{BERT}: Pre", full)
    assert not is_prefix_title(full, full)
    assert not is_prefix_title("Attention is all you", "Attention is all your needs")
    assert not is_prefix_title("Deep learning for all", "Shallow learning for all of us")
    assert not is_prefix_title("", full)


# --- names --------------------------------------------------------------------


def test_split_names():
    assert split_names("Kiss, Anna and M{\\\"u}ller, J{\\\"o}rg") == (["Kiss, Anna", 'M{\\"u}ller, J{\\"o}rg'], False)
    assert split_names("{Barnes and Noble} and Smith, J.") == (["{Barnes and Noble}", "Smith, J."], False)
    assert split_names("A AND B And C") == (["A", "B", "C"], False)
    assert split_names("Ashish Vaswani and\n                  Noam Shazeer") == (["Ashish Vaswani", "Noam Shazeer"], False)
    assert split_names("Andersen, Hans and Anderson, Pam") == (["Andersen, Hans", "Anderson, Pam"], False)
    assert split_names("Smith and others") == (["Smith"], True)
    assert split_names("Smith and Others") == (["Smith"], True)
    assert split_names("Smith and and Jones and") == (["Smith", "Jones"], False)
    assert split_names("") == ([], False)
    assert split_names("  ") == ([], False)


@pytest.mark.parametrize(
    "name, first, von, last, jr",
    [
        ("Donald E. Knuth", "Donald E.", "", "Knuth", ""),
        ("Knuth, Donald E.", "Donald E.", "", "Knuth", ""),
        ("J. R. R. Tolkien", "J. R. R.", "", "Tolkien", ""),
        ("Ludwig van Beethoven", "Ludwig", "van", "Beethoven", ""),
        ("van Beethoven, Ludwig", "Ludwig", "van", "Beethoven", ""),
        ("W. M. P. van der Aalst", "W. M. P.", "van der", "Aalst", ""),
        ("Jean de La Fontaine", "Jean", "de", "La Fontaine", ""),
        ("de La Fontaine, Jean", "Jean", "de", "La Fontaine", ""),
        ("Charles Louis Xavier Joseph de la Vallee Poussin", "Charles Louis Xavier Joseph", "de la", "Vallee Poussin", ""),
        ("jean de la fontaine", "", "jean de la", "fontaine", ""),
        ("Ford, Jr., Henry", "Henry", "", "Ford", "Jr."),
        ("Smith, Jr, John", "John", "", "Smith", "Jr"),
        ("Jean-Paul Sartre", "Jean-Paul", "", "Sartre", ""),
        ("García-Molina, Hector", "Hector", "", "García-Molina", ""),
        ("Hector Garc{\\'\\i}a-Molina", "Hector", "", "Garc{\\'\\i}a-Molina", ""),
        ("{\\'E}mile Zola", "{\\'E}mile", "", "Zola", ""),
        ("\\v{S}tefan Banach", "\\v{S}tefan", "", "Banach", ""),
        ("John {von Neumann}", "John", "", "{von Neumann}", ""),
        ("{von Neumann}, John", "John", "", "{von Neumann}", ""),
        ("{\\v{s}}tefan van Huis", "", "{\\v{s}}tefan van", "Huis", ""),
        ("Aristotle", "", "", "Aristotle", ""),
        ("  Ashish\n   Vaswani ", "Ashish", "", "Vaswani", ""),
    ],
)
def test_parse_name(name, first, von, last, jr):
    n = parse_name(name)
    assert (n.first, n.von, n.last, n.jr, n.literal) == (first, von, last, jr, False)


def test_literal_names():
    assert parse_name("{World Health Organization}") == Name("", "", "World Health Organization", "", literal=True)
    assert parse_name("{Barnes and Noble}").literal
    assert not parse_name("{IEEE} Computer Society").literal
    assert parse_name("") == Name("", "", "", "")


def test_family():
    assert parse_name("W. M. P. van der Aalst").family == "van der Aalst"
    assert parse_name("Knuth, Donald").family == "Knuth"
    assert parse_name("{World Health Organization}").family == "World Health Organization"


def test_family_key_matches_across_spellings():
    same = ["van der Aalst", "Aalst", "W. M. P. van der Aalst", "{van der Aalst}, Wil", "Aalst, W. M. P. van der", "van der Aalst, Wil"]
    assert {family_key(n) for n in same} == {"aalst"}
    same = ["García-Molina", "Garcia-Molina", "Molina", "Hector Garc{\\'\\i}a-Molina", "Garc\\'{i}a-Molina, H."]
    assert {family_key(n) for n in same} == {"molina"}
    assert family_key("G{\\\"o}del, Kurt") == family_key("Kurt Gödel") == "godel"
    assert family_key("Martin Luther King Jr.") == family_key("King, Jr., Martin Luther") == "king"
    assert family_key("{World Health Organization}") == "organization"
    assert family_key("") == ""
    assert family_key("   ") == ""
    assert family_key("{}") == ""


# --- cites --------------------------------------------------------------------


def test_cites_basic():
    text = "Intro \\cite{a} and \\cite{ b , c ,}.\nMore \\citep{d}\n"
    found = cites(text, "main.tex")
    assert [(c.key, c.line, c.command, c.path) for c in found] == [
        ("a", 1, "cite", "main.tex"),
        ("b", 1, "cite", "main.tex"),
        ("c", 1, "cite", "main.tex"),
        ("d", 2, "citep", "main.tex"),
    ]
    b, c = found[1], found[2]
    assert (b.start, b.end) == (c.start, c.end)
    assert text[b.start : b.end] == "\\cite{ b , c ,}"
    assert text[found[0].start : found[0].end] == "\\cite{a}"


@pytest.mark.parametrize(
    "command",
    [
        "cite",
        "nocite",
        "citep",
        "citet",
        "citealp",
        "citealt",
        "citeauthor",
        "citeyear",
        "citeyearpar",
        "parencite",
        "textcite",
        "autocite",
        "footcite",
        "smartcite",
        "supercite",
        "fullcite",
        "Cite",
        "Citep",
        "Citet",
        "Textcite",
        "Parencite",
        "Autocite",
        "Citeauthor",
    ],
)
def test_every_citation_command(command):
    for written in (f"\\{command}{{k}}", f"\\{command}*{{k}}", f"\\{command}[p.~5]{{k}}", f"\\{command}[see][p.~5]{{k}}"):
        found = cites(written)
        assert [c.key for c in found] == ["k"], written
        assert found[0].command == command + ("*" if "*" in written else "")
        assert found[0].end == len(written)


def test_cites_with_options_and_whitespace():
    text = "\\citep [see] \n [p.~3, {with ] bracket}] \n {a,\n  b}"
    found = cites(text)
    assert [c.key for c in found] == ["a", "b"]
    assert found[0].end == len(text)
    assert [c.key for c in cites("\\cite[a][b][c]{k}")] == []


def test_cites_with_a_comment_between_the_parts():
    text = "\\citep[see]% the page comes later\n  {a}"
    assert [(c.key, c.line, c.end) for c in cites(text)] == [("a", 1, len(text))]


def test_nocite_star():
    found = cites("\\nocite{*}")
    assert [(c.key, c.command) for c in found] == [("*", "nocite")]


def test_what_is_not_a_citation():
    text = (
        "\\citation{x} \\citeX{y} \\recite{z} \\\\cite{w}\n"
        "\\newcommand{\\mycite}{\\cite} \\cite{} \\cite{ , }\n"
        "\\cite[unclosed{u}\n"
    )
    assert cites(text) == []


def test_cites_skip_comments():
    text = (
        "Real \\cite{a}. % but not \\cite{commented}\n"
        "50\\% of runs \\cite{b}\n"
        "% \\cite{whole-line}\n"
        "a line break \\\\% \\cite{after-break-comment}\n"
        "\\cite{c}% trailing\n"
    )
    found = cites(text)
    assert [(c.key, c.line) for c in found] == [("a", 1), ("b", 2), ("c", 5)]


def test_cites_skip_verbatim():
    text = (
        "\\cite{a}\n"
        "\\begin{verbatim}\n\\cite{v1}\n\\end{verbatim}\n"
        "\\begin{lstlisting}[language=TeX]\n\\cite{v2}\n\\end{lstlisting}\n"
        "\\begin{minted}{latex}\n\\cite{v3}\n\\end{minted}\n"
        "\\begin{comment}\n\\cite{v4}\n\\end{comment}\n"
        "Inline \\verb|\\cite{v5}| and \\verb*+\\cite{v6}+ then \\cite{b}\n"
        "\\begin{verbatim*}\n\\cite{v7}\n\\end{verbatim*}\n"
        "% \\begin{verbatim}\n"
        "\\cite{c}\n"
    )
    found = cites(text)
    assert [(c.key, c.line) for c in found] == [("a", 1), ("b", 14), ("c", 19)]


def test_cites_positions_with_crlf_and_unicode():
    text = "Grüße 🎉 \\cite{a}\r\nzweite Zeile \\citet{b}\r\n"
    found = cites(text)
    assert [(c.key, c.line) for c in found] == [("a", 1), ("b", 2)]
    for c in found:
        assert text[c.start] == "\\"
        assert text[c.end - 1] == "}"


# --- sentence_at -----------------------------------------------------------------


def at(text: str, key: str, **kwargs) -> str:
    """The sentence of the first citation of ``key``."""
    c = next(c for c in cites(text) if c.key == key)
    return sentence_at(text, c.start, **kwargs)


def test_sentence_basic():
    text = "First sentence. Transformers replaced recurrence~\\cite{vaswani,lecun}. Third one."
    assert at(text, "vaswani") == "Transformers replaced recurrence [vaswani, lecun]."
    assert at(text, "lecun") == "Transformers replaced recurrence [vaswani, lecun]."


def test_sentence_ends_with_question_and_exclamation_marks():
    text = "Is it fast? It is \\cite{a}! Really."
    assert at(text, "a") == "It is [a]!"


@pytest.mark.parametrize(
    "abbreviation",
    ["e.g.", "i.e.", "et al.", "cf.", "vs.", "Fig.", "Figs.", "Eq.", "Sec.", "Tab.", "No.", "approx.", "resp."],
)
def test_sentence_does_not_end_after_abbreviations(abbreviation):
    text = f"Before. We follow {abbreviation} the method \\cite{{a}}. After."
    assert at(text, "a") == f"We follow {abbreviation} the method [a]."


def test_sentence_does_not_end_after_initials_or_in_numbers():
    text = "Before. As J. R. Smith showed, 3.5 times less energy is used \\cite{a}. After."
    assert at(text, "a") == "As J. R. Smith showed, 3.5 times less energy is used [a]."
    text = "Before. The U.S. grid is cleaner \\cite{a}. After."
    assert at(text, "a") == "The U.S. grid is cleaner [a]."


def test_sentence_ends_at_a_blank_line():
    text = "A paragraph without a period \\cite{a}\n\n  Next paragraph \\cite{b}\n \nThird"
    assert at(text, "a") == "A paragraph without a period [a]"
    assert at(text, "b") == "Next paragraph [b]"


def test_sentence_ends_at_structural_commands():
    text = (
        "\\section{Introduction}\n"
        "Energy matters \\cite{a}\n"
        "\\subsection*{Related work \\cite{b}}\n"
        "\\begin{itemize}\n"
        "\\item First claim \\cite{c}\n"
        "\\item[(ii)] Second claim \\cite{d}\n"
        "\\end{itemize}\n"
        "\\begin{figure}[t]\n"
        "\\caption[Short]{Energy per request~\\cite{e}. Lower is better.}\n"
        "\\end{figure}\n"
        "\\paragraph{Setup.} We measure \\cite{f}"
    )
    assert at(text, "a") == "Energy matters [a]"
    assert at(text, "b") == "Related work [b]"
    assert at(text, "c") == "First claim [c]"
    assert at(text, "d") == "Second claim [d]"
    assert at(text, "e") == "Energy per request [e]."
    assert at(text, "f") == "We measure [f]"


def test_sentence_without_comments():
    text = (
        "Before. The energy % of the GPU. Not a sentence end.\n"
        "of inference grows \\cite{a} % \\cite{hidden}\n"
        "% a whole comment line\n"
        "with the model size. After."
    )
    assert at(text, "a") == "The energy of inference grows [a] with the model size."


def test_sentence_converts_latex():
    text = "Erd{\\H o}s and R\\'enyi~\\cite{er} showed \\emph{this} -- in 1959."
    assert at(text, "er") == f"Erdős and Rényi [er] showed this {EN} in 1959."


def test_sentence_at_the_edges_of_the_text():
    assert at("\\cite{a} starts the text", "a") == "[a] starts the text"
    assert at("Before. Ends the text \\cite{a}.", "a") == "Ends the text [a]."
    assert sentence_at("", 0) == ""
    assert sentence_at("One. Two.", 100) == ""
    assert sentence_at("One. Two.", -5) == "One."


def test_sentence_at_an_index_that_is_not_a_citation():
    text = "One sentence here. Another sentence with words \\cite{a} here. Last."
    assert sentence_at(text, text.index("words") + 2) == "Another sentence with words [a] here."
    assert sentence_at(text, text.index("cite")) == "Another sentence with words [a] here."


def test_period_in_a_citation_option_does_not_end_the_sentence():
    text = "Before. Shown before \\cite[see Sec. 3. and p. 4]{a} too. After."
    assert at(text, "a") == "Shown before [a] too."


def test_long_sentences_are_cut_around_the_citation():
    words_before = " ".join(f"before{i}" for i in range(200))
    words_after = " ".join(f"after{i}" for i in range(200))
    text = f"{words_before} \\cite{{key}} {words_after}."
    result = at(text, "key", limit=200)
    assert result.startswith(ELLIPSIS) and result.endswith(ELLIPSIS)
    assert "[key]" in result
    assert 150 <= len(result) <= 202
    inner = result[1:-1]
    # Cut at word boundaries.
    assert all(w.startswith(("before", "after", "[key]")) and w[-1].isdigit() or w == "[key]" for w in inner.split())
    assert inner.split()[0] in words_before.split()
    assert inner.split()[-1] in words_after.split()


def test_long_sentence_near_its_start():
    text = "\\cite{key} " + " ".join(f"word{i}" for i in range(300)) + "."
    result = at(text, "key", limit=100)
    assert result.startswith("[key] word0")
    assert result.endswith(ELLIPSIS)
    assert len(result) <= 101
    assert at(text, "key", limit=10000) == to_text(text.replace("\\cite{key}", "[key]"))
