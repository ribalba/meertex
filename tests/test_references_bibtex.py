"""Reading and editing .bib files (DESIGN §16, Entries and The checked mark)."""

import datetime
import hashlib

import pytest

from app.references import bibtex
from app.references.bibtex import (
    MARK_FIELD,
    braces_balanced,
    fingerprint,
    format_entry,
    mark_value,
    parse,
    parse_mark,
    remove_field,
    resolve_crossrefs,
    set_field,
)

GOOGLE_SCHOLAR = """@inproceedings{vaswani2017attention,
  title={Attention is all you need},
  author={Vaswani, Ashish and Shazeer, Noam and Parmar, Niki and Uszkoreit, Jakob and Jones, Llion and Gomez, Aidan N and Kaiser, {\\L}ukasz and Polosukhin, Illia},
  booktitle={Advances in neural information processing systems},
  volume={30},
  year={2017}
}

@article{lecun2015deep,
  title={Deep learning},
  author={LeCun, Yann and Bengio, Yoshua and Hinton, Geoffrey},
  journal={nature},
  volume={521},
  number={7553},
  pages={436--444},
  year={2015},
  publisher={Nature Publishing Group UK London}
}
"""

DBLP = """@inproceedings{DBLP:conf/nips/VaswaniSPUJGKP17,
  author       = {Ashish Vaswani and
                  Noam Shazeer and
                  Niki Parmar and
                  Jakob Uszkoreit and
                  Llion Jones and
                  Aidan N. Gomez and
                  Lukasz Kaiser and
                  Illia Polosukhin},
  editor       = {Isabelle Guyon and
                  Ulrike von Luxburg},
  title        = {Attention is All you Need},
  booktitle    = {Advances in Neural Information Processing Systems 30: Annual Conference
                  on Neural Information Processing Systems 2017, December 4-9, 2017, Long
                  Beach, CA, {USA}},
  pages        = {5998--6008},
  year         = {2017},
  url          = {https://proceedings.neurips.cc/paper/2017/hash/3f5ee243547dee91fbd053c1c4a845aa-Abstract.html},
  timestamp    = {Thu, 21 Jan 2021 15:15:21 +0100},
  biburl       = {https://dblp.org/rec/conf/nips/VaswaniSPUJGKP17.bib},
  bibsource    = {dblp computer science bibliography, https://dblp.org}
}
"""

BIBLATEX = """@online{kiss2020,
  author = {Kiss, Anna and M{\\"u}ller, J{\\"o}rg},
  title = {Energy of {LLM} Inference},
  date = {2020-03-14},
  eprint = {2003.01234},
  eprinttype = {arxiv},
  eprintclass = {cs.LG},
  urldate = {2026-10-01},
}
"""


def check_positions(f: bibtex.BibFile) -> None:
    """Every position of every entry and field points into the text."""
    text = f.text
    for e in f.entries:
        assert text[e.start] == "@"
        assert text[e.end - 1] in "})"
        assert e.line == text.count("\n", 0, e.start) + 1
        assert e.end_line == text.count("\n", 0, e.end - 1) + 1
        for fl in e.fields:
            assert text[fl.value_start : fl.value_end] == fl.raw
            assert fl.end == fl.value_end
            assert text[fl.start : fl.start + len(fl.name)].lower() == fl.name
            assert fl.line == text.count("\n", 0, fl.start) + 1
            assert e.start < fl.start < fl.end < e.end


def only(text: str) -> bibtex.Entry:
    f = parse(text)
    assert f.problems == []
    assert len(f.entries) == 1
    return f.entries[0]


def reparse(text: str, key: str) -> bibtex.Entry:
    f = parse(text)
    assert f.problems == [], (text, f.problems)
    check_positions(f)
    found = [e for e in f.entries if e.key == key]
    assert len(found) == 1
    return found[0]


# --- reading ------------------------------------------------------------------


def test_google_scholar_export():
    f = parse(GOOGLE_SCHOLAR, "refs.bib")
    assert f.problems == []
    assert f.path == "refs.bib"
    assert f.text == GOOGLE_SCHOLAR
    check_positions(f)
    first, second = f.entries
    assert (first.type, first.key, first.path, first.line, first.end_line) == (
        "inproceedings",
        "vaswani2017attention",
        "refs.bib",
        1,
        7,
    )
    assert [fl.name for fl in first.fields] == ["title", "author", "booktitle", "volume", "year"]
    assert first.get("title") == "Attention is all you need"
    assert "Kaiser, {\\L}ukasz" in first.get("author")
    assert first.field("year").raw == "{2017}"
    assert first.field("year").line == 6
    assert second.get("pages") == "436--444"
    assert second.line == 9


def test_dblp_export_keeps_multiline_values():
    e = only(DBLP)
    check_positions(parse(DBLP))
    assert e.key == "DBLP:conf/nips/VaswaniSPUJGKP17"
    assert e.get("author").startswith("Ashish Vaswani and\n                  Noam Shazeer")
    assert e.get("booktitle").endswith("Long\n                  Beach, CA, {USA}")
    assert e.field("title").line == 12
    assert e.end_line == len(DBLP.splitlines())


def test_biblatex_entry_with_trailing_comma():
    e = only(BIBLATEX)
    assert e.type == "online"
    assert e.values() == {
        "author": 'Kiss, Anna and M{\\"u}ller, J{\\"o}rg',
        "title": "Energy of {LLM} Inference",
        "date": "2020-03-14",
        "eprint": "2003.01234",
        "eprinttype": "arxiv",
        "eprintclass": "cs.LG",
        "urldate": "2026-10-01",
    }


def test_types_and_field_names_ignore_case():
    e = only("@ARTICLE{Key2020,\n  TITLE = {T},\n  Year = 2020\n}\n")
    assert e.type == "article"
    assert e.key == "Key2020"
    assert [fl.name for fl in e.fields] == ["title", "year"]
    assert e.get("Title") == "T"
    assert e.get("YEAR") == "2020"
    assert e.field("tItLe").raw == "{T}"
    assert e.get("doi") is None
    assert e.field("doi") is None


def test_string_macros_and_concatenation():
    text = """@String{acm = "ACM"}
@STRING(pub = acm # " Press")
@string{ TSE = {IEEE Transactions on} # " Software Engineering" }

@book{b2019,
  publisher = pub # {, New York},
  journal = tse,
  month = feb # "~1",
  note = "x" # 42 # {y},
  series = unknownmacro,
  address = Jan,
}
"""
    f = parse(text)
    assert f.problems == []
    check_positions(f)
    e = f.entries[0]
    assert e.get("publisher") == "ACM Press, New York"
    assert e.field("publisher").raw == 'pub # {, New York}'
    assert e.get("journal") == "IEEE Transactions on Software Engineering"
    assert e.get("month") == "February~1"
    assert e.get("note") == "x42y"
    # An unknown macro keeps its name and is not a problem.
    assert e.get("series") == "unknownmacro"
    assert e.get("address") == "January"


def test_a_macro_works_only_after_its_definition():
    text = '@misc{early, publisher = acm}\n@string{acm = "ACM"}\n@misc{late, publisher = acm}\n'
    early, late = parse(text).entries
    assert early.get("publisher") == "acm"
    assert late.get("publisher") == "ACM"


def test_month_macros():
    for short, long in bibtex.MONTHS.items():
        e = only(f"@misc{{m, month = {short}}}")
        assert e.get("month") == long
    assert only("@misc{m, month = {jan}}").get("month") == "jan"


def test_entries_in_parentheses():
    text = '@article(paren2019,\n  title = "With (parens) and {braces}",\n  note = {a ) b},\n  year = 2019)\n'
    e = only(text)
    check_positions(parse(text))
    assert e.key == "paren2019"
    assert e.get("title") == "With (parens) and {braces}"
    assert e.get("note") == "a ) b"
    assert text[e.end - 1] == ")"


def test_entries_on_one_line_and_without_fields():
    text = "@misc{one, title={One}, year=2001}\n@misc{bare}\n@misc{comma,}\n@misc{ spaced , title = {S} , }\n"
    f = parse(text)
    assert f.problems == []
    check_positions(f)
    one, bare, comma, spaced = f.entries
    assert one.values() == {"title": "One", "year": "2001"}
    assert (bare.key, bare.fields) == ("bare", [])
    assert (comma.key, comma.fields) == ("comma", [])
    assert spaced.key == "spaced"
    assert spaced.get("title") == "S"


def test_whitespace_between_at_type_brace_and_key():
    e = only("@ article \n { key2020 ,\n title = {T} }")
    assert (e.type, e.key, e.get("title")) == ("article", "key2020", "T")


def test_comments_and_junk_between_entries():
    text = """This file was written by hand. Mail didi@example.org with questions.
% @article{commented, title = {not an entry because of the brace count? no, it is text}}
@comment{jabref-meta: databaseType:biblatex;}
@Comment This line is a comment in bibtex too.
@preamble{ "\\newcommand{\\noopsort}[1]{}" # "\\newcommand{\\x}{y}" }
Some text (with parentheses) and } stray braces {.

@misc{real, title = {Real}}
@comment(this one uses parentheses)
trailing junk
"""
    f = parse(text)
    assert f.problems == []
    # A % does not start a comment between entries. bibtex reads the
    # commented entry as an entry, and so does the parser.
    assert [e.key for e in f.entries] == ["commented", "real"]


def test_percent_comments_between_fields_are_skipped():
    text = "@misc{k,\n  title = {50% of {GPU} time},\n  % doi = {10.1/x},\n  year = 2020 % the year\n}\n"
    e = only(text)
    assert e.get("title") == "50% of {GPU} time"
    assert e.get("doi") is None
    assert e.get("year") == "2020"


def test_quotes_and_braces():
    e = only('@misc{q, title = {The "best" paper}, note = "A {"}quoted{"} word", x = "{a}{b}"}')
    assert e.get("title") == 'The "best" paper'
    assert e.get("note") == 'A {"}quoted{"} word'
    assert e.get("x") == "{a}{b}"


def test_keys_with_special_characters_and_unicode():
    text = (
        "@misc{doi:10.1145/3377811.3380330, title = {A}}\n"
        "@misc{müller2021, title = {Grüße aus Köln}}\n"
        "@misc{a+b&c_d.e-f, title = {C}}\n"
    )
    f = parse(text)
    assert f.problems == []
    check_positions(f)
    assert [e.key for e in f.entries] == ["doi:10.1145/3377811.3380330", "müller2021", "a+b&c_d.e-f"]
    assert f.entries[1].get("title") == "Grüße aus Köln"


def test_duplicate_keys_are_both_returned():
    f = parse("@misc{same, title={A}}\n@book{same, title={B}}\n")
    assert [(e.type, e.key) for e in f.entries] == [("misc", "same"), ("book", "same")]


def test_a_field_twice_uses_the_first():
    e = only("@misc{k, title = {First}, title = {Second}}")
    assert e.get("title") == "First"
    assert e.values() == {"title": "First"}
    assert len(e.fields) == 2


def test_crlf_line_endings():
    text = GOOGLE_SCHOLAR.replace("\n", "\r\n")
    f = parse(text)
    assert f.problems == []
    check_positions(f)
    assert [e.line for e in f.entries] == [1, 9]
    assert f.entries[0].get("year") == "2017"


def test_byte_order_mark():
    f = parse("\ufeff@misc{bom, title = {T}}\n")
    assert [e.key for e in f.entries] == ["bom"]


# --- broken entries -------------------------------------------------------------


def test_a_broken_entry_in_the_middle_does_not_hide_the_rest():
    text = """@misc{before, title = {Before}}

@article{kiss2020,
  title = {Never {closed},
  year = 2020,
}

@book{after,
  title = {After},
}
"""
    f = parse(text, "refs.bib")
    assert [e.key for e in f.entries] == ["before", "after"]
    assert len(f.problems) == 1
    p = f.problems[0]
    assert (p.path, p.line, p.key) == ("refs.bib", 3, "kiss2020")
    assert p.message == "The entry kiss2020 never closes its braces."
    check_positions(f)


def test_a_missing_closing_brace_before_the_next_entry():
    text = "@article{a,\n  title = {A},\n  year = 2020,\n\n@book{b,\n  title = {B},\n}\n"
    f = parse(text)
    assert [e.key for e in f.entries] == ["b"]
    assert [(p.line, p.key, p.message) for p in f.problems] == [(1, "a", "The entry a never closes its braces.")]


def test_end_of_file_inside_an_entry():
    f = parse("@misc{ok, title={T}}\n@misc{open,\n  title = {T}")
    assert [e.key for e in f.entries] == ["ok"]
    assert f.problems[0].message == "The entry open never closes its braces."
    assert f.problems[0].line == 2
    f = parse("@misc(open, title = {T}")
    assert f.problems[0].message == "The entry open never closes its parentheses."


@pytest.mark.parametrize(
    "body, message",
    [
        ("title {T}", "The field title in the entry k has no = sign."),
        ("title = {T}\n  year = 2020", "The entry k has no comma before the field year."),
        ("title = ,", "The field title in the entry k has no value."),
        ("title = }", "The field title in the entry k has no value."),
        ('title = "A" # ,', "The field title in the entry k has nothing after its #."),
        ('title = "a } b"', "The value of title in the entry k has a closing brace without an opening one."),
        ("title = {T} junk", "The entry k has unexpected text after the field title."),
        ("{oops}", "The entry k has unexpected text where a field name should be."),
    ],
)
def test_syntax_errors(body, message):
    text = f"@misc{{k,\n  {body}\n}}\n\n@misc{{next, title = {{N}}}}\n"
    f = parse(text)
    assert [(p.line, p.key, p.message) for p in f.problems] == [(1, "k", message)]
    assert [e.key for e in f.entries] == ["next"]


def test_a_quote_that_never_closes():
    f = parse('@misc{ok}\n@misc{k,\n  title = "open\n')
    assert [(p.line, p.message) for p in f.problems] == [(2, "The value of title in the entry k never closes its quotes.")]
    assert [e.key for e in f.entries] == ["ok"]


def test_entry_without_key_or_brace():
    f = parse("@misc{title = {T}}\n@misc{, title = {T}}\n@article missing brace\n@misc{ok}\n")
    assert [p.message for p in f.problems] == [
        "The @misc entry has no key.",
        "The @misc entry has no key.",
        "The @article entry has no opening brace.",
    ]
    assert [p.line for p in f.problems] == [1, 2, 3]
    assert all(p.key is None for p in f.problems)
    assert [e.key for e in f.entries] == ["ok"]


def test_missing_comma_after_key():
    f = parse("@misc{k title = {T}}\n@misc{ok}\n")
    assert f.problems[0].message == "The entry k has no comma after its key."
    assert [e.key for e in f.entries] == ["ok"]


def test_broken_string_and_preamble():
    f = parse('@string{x = "open}\n@preamble{ "\\x"\n\n@misc{ok, title = x}\n')
    assert [p.line for p in f.problems] == [1, 2]
    assert f.problems[0].key is None
    assert f.problems[1].message == "The @preamble never closes its braces."
    # The broken @string defined nothing.
    assert f.entries[0].get("title") == "x"


def test_parsing_resumes_only_at_an_at_that_starts_a_line():
    # The text after the broken value has an @ inside a line. It is not where
    # parsing goes on.
    text = "@misc{bad, title = {x} y = {mail me@home}}\n  @misc{good, title = {G}}\n"
    f = parse(text)
    assert [p.key for p in f.problems] == ["bad"]
    assert [e.key for e in f.entries] == ["good"]


def test_many_broken_entries():
    text = "".join(f"@misc{{bad{i}, title = {{x\n\n" for i in range(5)) + "@misc{ok, title={T}}\n"
    f = parse(text)
    assert [e.key for e in f.entries] == ["ok"]
    assert len(f.problems) == 5


# --- crossref -------------------------------------------------------------------


def test_crossref_inheritance():
    text = """@inproceedings{child,
  title = {Child},
  crossref = {Proc2020},
  meertex-checked = {2026-10-27, A, 12345678},
}
@proceedings{proc2020,
  title = {Proceedings},
  booktitle = {Proc. of X},
  year = 2020,
  publisher = {ACM},
  ids = {old},
  meertex-checked = {2026-10-27, B, 87654321},
}
"""
    child, parent = parse(text).entries
    resolve_crossrefs([child, parent])
    assert child.inherited == {"booktitle": "Proc. of X", "year": "2020", "publisher": "ACM"}
    assert child.get("title") == "Child"
    assert child.get("year") == "2020"
    assert child.field("year") is None
    assert "year" not in child.values()
    assert parent.inherited == {}
    # Inherited fields are not part of the fingerprint.
    before = fingerprint(child)
    child.inherited = {}
    assert fingerprint(child) == before


def test_crossref_across_files_and_missing_parents():
    a = parse("@inproceedings{c1, crossref = {p}}\n@misc{c2, crossref = {nowhere}}\n", "a.bib")
    b = parse("@proceedings{P, year = 2001}\n@proceedings{p, year = 2002}\n", "b.bib")
    entries = a.entries + b.entries
    resolve_crossrefs(entries)
    assert entries[0].get("year") == "2002"
    assert entries[1].inherited == {}
    resolve_crossrefs(entries)
    assert entries[0].inherited == {"year": "2002"}


# --- fingerprints -----------------------------------------------------------------


def test_fingerprint_canonical_form():
    e = only("@misc{k, year = 2020, title = {T}}")
    canonical = b"meertex-entry-1\nmisc\ntitle=T\nyear=2020\n"
    assert fingerprint(e) == hashlib.sha256(canonical).hexdigest()


def test_fingerprint_survives_reformatting():
    a = only(GOOGLE_SCHOLAR.split("\n\n")[0])
    b_text = """@INPROCEEDINGS { renamed ,
    YEAR = 2017 , volume = "30",
    booktitle = "Advances in neural   information
                 processing systems",
    author={Vaswani, Ashish and Shazeer, Noam and Parmar, Niki and Uszkoreit, Jakob and Jones, Llion and Gomez, Aidan N and Kaiser, {\\L}ukasz and Polosukhin, Illia},
    title = {Attention is all you need},
    meertex-checked = {2026-10-27, Didi Hoffmann, 3f2a9c1b},
}"""
    b = only(b_text)
    assert fingerprint(a) == fingerprint(b)
    assert len(fingerprint(a)) == 64


@pytest.mark.parametrize(
    "changed",
    [
        "@misc{k, title = {T.}, year = 2020}",
        "@misc{k, title = {t}, year = 2020}",
        "@misc{k, title = {T}, year = 2021}",
        "@misc{k, title = {T}, year = 2020, doi = {10.1/x}}",
        "@misc{k, title = {T}}",
        "@book{k, title = {T}, year = 2020}",
        "@misc{k, title = {{T}}, year = 2020}",
    ],
)
def test_fingerprint_changes_with_any_value(changed):
    base = only("@misc{k, title = {T}, year = 2020}")
    assert fingerprint(only(changed)) != fingerprint(base)


def test_fingerprint_ignores_the_checked_mark():
    base = only("@misc{k, title = {T}}")
    marked = only("@misc{k, title = {T}, meertex-checked = {2026-10-27, A, deadbeef}}")
    other = only("@misc{k, title = {T}, Meertex-Checked = {2025-01-01, B, 00000000}}")
    assert fingerprint(base) == fingerprint(marked) == fingerprint(other)


# --- the checked mark -------------------------------------------------------------


def test_parse_mark():
    m = parse_mark("2026-10-27, Didi Hoffmann, 3F2A9C1B")
    assert (m.date, m.by, m.hash) == ("2026-10-27", "Didi Hoffmann", "3f2a9c1b")
    m = parse_mark(" 2026-10-27 ,\n   Didi\n   Hoffmann ,  3f2a ")
    assert (m.date, m.by, m.hash) == ("2026-10-27", "Didi Hoffmann", "3f2a")
    assert parse_mark("2026-10-27, x, " + "a" * 64).hash == "a" * 64
    for bad in ("", "2026-10-27", "2026-10-27, Didi", "2026-10-27, Didi, Hoffmann, 3f2a9c1b", "2026-10-27, Didi, xyz12345", "2026-10-27, Didi, 3f2", "2026-10-27, Didi, " + "a" * 65):
        assert parse_mark(bad) is None, bad


def test_mark_value():
    fp = "3f2a9c1b" + "0" * 56
    assert mark_value(datetime.date(2026, 10, 27), "Didi Hoffmann", fp) == "2026-10-27, Didi Hoffmann, 3f2a9c1b"
    assert mark_value(datetime.date(2026, 1, 2), " Hoffmann,  {Didi}\n Jr ", fp) == "2026-01-02, Hoffmann Didi Jr, 3f2a9c1b"
    assert mark_value(datetime.datetime(2026, 10, 27, 23, 59), "a", fp) == "2026-10-27, a, 3f2a9c1b"
    m = parse_mark(mark_value(datetime.date(2026, 10, 27), "Ann, B.", fp))
    assert (m.date, m.by, m.hash) == ("2026-10-27", "Ann B.", "3f2a9c1b")


def test_marking_an_entry_keeps_it_checked():
    text = GOOGLE_SCHOLAR
    e = parse(text).entries[1]
    fp = fingerprint(e)
    marked = set_field(text, e, MARK_FIELD, mark_value(datetime.date(2026, 10, 27), "Didi", fp))
    again = reparse(marked, "lecun2015deep")
    assert fingerprint(again) == fp
    assert parse_mark(again.get(MARK_FIELD)).hash == fp[:8]
    assert again.fields[-1].name == MARK_FIELD
    # Marking again replaces the old mark in place.
    remarked = set_field(marked, again, MARK_FIELD, "2026-11-01, Ann, 00000000")
    entry = reparse(remarked, "lecun2015deep")
    assert [fl.name for fl in entry.fields].count(MARK_FIELD) == 1
    assert entry.get(MARK_FIELD) == "2026-11-01, Ann, 00000000"
    # Unmarking gives back the file as it was, with the comma marking added
    # after the field before it.
    assert remove_field(remarked, entry, MARK_FIELD) == text.replace("UK London}\n}", "UK London},\n}")


# --- set_field ----------------------------------------------------------------


def test_set_field_replaces_only_the_value():
    text = GOOGLE_SCHOLAR
    e = parse(text).entries[0]
    out = set_field(text, e, "Year", "2018")
    f = e.field("year")
    assert out == text[: f.value_start] + "{2018}" + text[f.value_end :]
    assert reparse(out, "vaswani2017attention").get("year") == "2018"
    # A macro value or a concatenation is replaced as a whole.
    text = '@string{x = "X"}\n@misc{k, journal = x # " Y", year = 2000}\n'
    out = set_field(text, parse(text).entries[0], "journal", "Z")
    assert out == '@string{x = "X"}\n@misc{k, journal = {Z}, year = 2000}\n'


def test_set_field_appends_a_line_above_the_closing_brace():
    text = "@misc{before}\n@article{k,\n  title = {T},\n  year = 2020\n}\n@misc{after}\n"
    out = set_field(text, parse(text).entries[1], "doi", "10.1/x")
    assert out == "@misc{before}\n@article{k,\n  title = {T},\n  year = 2020,\n  doi = {10.1/x},\n}\n@misc{after}\n"
    reparse(out, "k")


def test_set_field_keeps_a_trailing_comma_and_the_indentation():
    text = "@article{k,\n\ttitle = {T},\n\n}\n"
    out = set_field(text, parse(text).entries[0], "doi", "D")
    assert out == "@article{k,\n\ttitle = {T},\n\n\tdoi = {D},\n}\n"
    e = only(DBLP)
    out = set_field(DBLP, e, MARK_FIELD, "x")
    assert out.endswith("  bibsource    = {dblp computer science bibliography, https://dblp.org},\n  meertex-checked = {x},\n}\n")
    assert reparse(out, e.key).values() == {**e.values(), MARK_FIELD: "x"}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("@misc{k, title={T}}", "@misc{k, title={T},\n  doi = {D},\n}"),
        ("@misc{k, title={T},}", "@misc{k, title={T},\n  doi = {D},\n}"),
        ("@misc{k, title={T} , }", "@misc{k, title={T} ,\n  doi = {D},\n}"),
        ("@misc{k}", "@misc{k,\n  doi = {D},\n}"),
        ("@misc{k,}", "@misc{k,\n  doi = {D},\n}"),
        ("@misc{k,\n}", "@misc{k,\n  doi = {D},\n}"),
        ("@misc{k,\n    title = {T},\n    year = 2020}", "@misc{k,\n    title = {T},\n    year = 2020,\n    doi = {D},\n}"),
        ("@misc(k, title = {T})", "@misc(k, title = {T},\n  doi = {D},\n)"),
        ("@misc(k,\n  title = {T}\n)", "@misc(k,\n  title = {T},\n  doi = {D},\n)"),
        ("@misc{k,\n  year = 2020 % see, there\n}", "@misc{k,\n  year = 2020, % see, there\n  doi = {D},\n}"),
    ],
)
def test_set_field_appends_valid_bibtex(text, expected):
    out = set_field(text, only(text), "doi", "D")
    assert out == expected
    assert reparse(out, "k").get("doi") == "D"


def test_set_field_keeps_crlf():
    text = "@misc{a}\r\n@article{k,\r\n  title = {T}\r\n}\r\n"
    out = set_field(text, parse(text).entries[1], "doi", "D")
    assert out == "@misc{a}\r\n@article{k,\r\n  title = {T},\r\n  doi = {D},\r\n}\r\n"
    assert "\n" not in out.replace("\r\n", "")
    one = "@misc{a}\r\n@misc{k, title = {T}}\r\n"
    out = set_field(one, parse(one).entries[1], "doi", "D")
    assert out == "@misc{a}\r\n@misc{k, title = {T},\r\n  doi = {D},\r\n}\r\n"


def test_set_field_with_unicode_before_the_entry():
    text = "% Grüße 🎉\n@misc{k, title = {Köln}}\n"
    out = set_field(text, only(text), "note", "ß")
    assert reparse(out, "k").values() == {"title": "Köln", "note": "ß"}


def test_set_field_refuses_unbalanced_values_and_stale_entries():
    text = "@misc{k, title = {T}}"
    e = only(text)
    for bad in ("{", "}", "a}{b", "{{}"):
        with pytest.raises(ValueError):
            set_field(text, e, "title", bad)
    with pytest.raises(ValueError):
        set_field("x" + text, e, "doi", "D")
    assert set_field(text, e, "title", "{A} {B}") == "@misc{k, title = {{A} {B}}}"


# --- remove_field -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        # On a line of its own, with the line.
        ("@misc{k,\n  title = {T},\n  doi = {D},\n  year = 2020\n}\n", "@misc{k,\n  title = {T},\n  year = 2020\n}\n"),
        # The last field, so the comma before it stays.
        ("@misc{k,\n  title = {T},\n  doi = {D}\n}\n", "@misc{k,\n  title = {T},\n}\n"),
        # A value over several lines.
        ("@misc{k,\n  doi = {a\n    b\n    c},\n  title = {T}\n}\n", "@misc{k,\n  title = {T}\n}\n"),
        # Sharing a line with other fields.
        ("@misc{k, doi = {D}, title = {T}}", "@misc{k, title = {T}}"),
        ("@misc{k, title = {T}, doi = {D}}", "@misc{k, title = {T},}"),
        ("@misc{k,\n  title = {T}, doi = {D}, year = 1\n}", "@misc{k,\n  title = {T}, year = 1\n}"),
        ("@misc{k, doi = {D},\n  title = {T}\n}", "@misc{k,\n  title = {T}\n}"),
        ("@misc{k,\n  title = {T},\n  doi = {D}}", "@misc{k,\n  title = {T},\n}"),
        ("@misc{k, doi = {D}}", "@misc{k,}"),
        ("@misc(k,\n  doi = {D},\n  title = {T}\n)", "@misc(k,\n  title = {T}\n)"),
    ],
)
def test_remove_field(text, expected):
    out = remove_field(text, only(text), "DOI")
    assert out == expected
    assert reparse(out, "k").get("doi") is None


def test_remove_field_without_the_field_and_with_crlf():
    text = "@misc{k,\r\n  title = {T},\r\n  doi = {D},\r\n}\r\n@misc{z}\r\n"
    e = parse(text).entries[0]
    assert remove_field(text, e, "pages") == text
    assert remove_field(text, e, "doi") == "@misc{k,\r\n  title = {T},\r\n}\r\n@misc{z}\r\n"


def test_remove_field_leaves_other_entries_alone():
    text = GOOGLE_SCHOLAR
    second = parse(text).entries[1]
    out = remove_field(text, second, "publisher")
    assert out[: second.start] == text[: second.start]
    assert reparse(out, "lecun2015deep").get("publisher") is None
    assert reparse(out, "lecun2015deep").get("year") == "2015"


# --- format_entry and braces ------------------------------------------------------


def test_format_entry():
    out = format_entry("Article", "smith2020", [("Title", "The {GPU} Book"), ("year", "2020")])
    assert out == "@article{smith2020,\n  title = {The {GPU} Book},\n  year = {2020},\n}\n"
    e = only(out)
    assert e.values() == {"title": "The {GPU} Book", "year": "2020"}
    assert format_entry("misc", "k", []) == "@misc{k,\n}\n"
    assert only(format_entry("misc", "k", [])).fields == []


def test_format_entry_refuses_what_bibtex_cannot_read():
    with pytest.raises(ValueError):
        format_entry("misc", "k", [("title", "{open")])
    for key in ("", "a b", "a,b", "a{b", "a=b"):
        with pytest.raises(ValueError):
            format_entry("misc", key, [])
    with pytest.raises(ValueError):
        format_entry("mi sc", "k", [])
    with pytest.raises(ValueError):
        format_entry("misc", "k", [("ti tle", "x")])


def test_braces_balanced():
    assert braces_balanced("")
    assert braces_balanced("plain")
    assert braces_balanced("{a} {b {c}}")
    assert braces_balanced("\\{escaped\\}")
    assert not braces_balanced("{")
    assert not braces_balanced("}")
    assert not braces_balanced("}{")
    assert not braces_balanced("{a}}{")
    assert not braces_balanced("\\{")
