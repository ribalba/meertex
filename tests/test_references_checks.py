"""What the reference check finds (app/references/checks.py), without the
network and without a database. Each test is a kind of wrong or made-up
citation, and the correct entries next to it must stay quiet."""

from datetime import date

from references_fakes import FakeSources, down, found, page, rec

from app.references import bibtex, checks

TODAY = date(2026, 10, 27)


def entry(text: str) -> bibtex.Entry:
    parsed = bibtex.parse(text, "refs.bib")
    assert not parsed.problems, parsed.problems
    return parsed.entries[0]


def codes(findings: list[dict]) -> dict[str, str]:
    return {f["code"]: f["level"] for f in findings}


# --- Without the network ------------------------------------------------------------


def test_identifiers():
    ids = checks.identifiers(entry("@misc{a, url = {https://doi.org/10.1145/3290605.3300857}, doi = {}}"))
    assert ids.doi == "10.1145/3290605.3300857"
    ids = checks.identifiers(entry("@misc{a, eprint = {1706.03762}, archivePrefix = {arXiv}}"))
    assert ids.arxiv == "1706.03762" and ids.lookup_doi == "10.48550/arXiv.1706.03762"
    ids = checks.identifiers(entry("@article{a, journal = {CoRR}, volume = {abs/1706.03762}}"))
    assert ids.arxiv == "1706.03762"
    ids = checks.identifiers(entry("@misc{a, howpublished = {arXiv preprint arXiv:2305.12345v2}}"))
    assert ids.arxiv == "2305.12345"
    ids = checks.identifiers(entry("@misc{a, url = {https://arxiv.org/pdf/hep-th/9901001v1}}"))
    assert ids.arxiv == "hep-th/9901001" and ids.urls == []
    ids = checks.identifiers(entry("@book{a, isbn = {978-0-262-03561-3}}"))
    assert ids.isbn == "9780262035613"
    ids = checks.identifiers(entry(r"@misc{a, howpublished = {\url{https://example.com/x}}}"))
    assert ids.urls == ["https://example.com/x"]


def test_offline_findings():
    def found_codes(text: str, project_year: int | None = 2027) -> dict[str, str]:
        return codes(checks.offline(entry(text), project_year, TODAY))

    assert found_codes("@article{a, title = {A real title}, author = {Doe, Jane}, year = {2020}}") == {}
    assert found_codes("@article{a, title = {TODO}}") == {"placeholder": "error"}
    assert found_codes("@article{a, author = {Smith, J. et al.}}") == {"placeholder": "error"}
    assert found_codes("@article{a, pages = {??}}") == {"placeholder": "error"}
    assert found_codes("@article{a, author = {Author Name}}") == {"placeholder": "error"}
    assert found_codes("@article{a, year = {2031}}") == {"year": "error"}
    assert found_codes("@article{a, year = {2026}}", 2025) == {"year": "warning"}
    assert found_codes("@article{a, year = {in press}}") == {"year": "warning"}
    assert found_codes("@article{a, date = {2020-05-01}}") == {}
    assert found_codes("@article{a, pages = {30--12}}") == {"pages": "warning"}
    assert found_codes("@article{a, pages = {12--30}}") == {}
    assert found_codes("@misc{a, eprint = {2713.12345}, eprinttype = {arXiv}}") == {"arxiv_id": "error"}
    assert found_codes("@misc{a, eprint = {2611.12345}, eprinttype = {arXiv}, year = {2026}}") == {"arxiv_id": "error"}
    assert found_codes("@misc{a, eprint = {1706.0376}, eprinttype = {arXiv}, year = {2017}}") == {"arxiv_id": "error"}
    assert found_codes("@misc{a, eprint = {1706.03762}, eprinttype = {arXiv}, year = {2019}}") == {"arxiv_year": "warning"}
    # The published version may be later than its preprint.
    assert found_codes("@inproceedings{a, booktitle = {NeurIPS}, eprint = {1706.03762}, eprinttype = {arXiv}, "
                       "year = {2019}}") == {}
    assert found_codes("@book{a, isbn = {978-0-262-03561-4}}") == {"isbn": "error"}
    assert found_codes("@book{a, isbn = {0-262-03561-7}}") == {"isbn": "error"}
    assert found_codes("@book{a, isbn = {0-262-03561-8}}") == {}
    assert found_codes("@book{a, isbn = {978-0-262-03561-3}}") == {}
    assert found_codes("@article{a, doi = {doi.org/whatever}}") == {"doi_syntax": "error"}
    assert found_codes("@online{a, url = {https://x.org}}") == {"urldate": "info"}
    assert found_codes("@online{a, url = {https://x.org}, urldate = {2027-01-01}}") == {"urldate": "warning"}


def test_venues_match_leniently():
    assert checks.venue_matches("CHI '19", "Proceedings of the 2019 CHI Conference on Human Factors in Computing Systems")
    assert checks.venue_matches("IEEE Trans. Softw. Eng.", "IEEE Transactions on Software Engineering")
    assert checks.venue_matches("NeurIPS", "Advances in Neural Information Processing Systems")
    assert checks.venue_matches("ICML", "International Conference on Machine Learning")
    assert checks.venue_matches("Proc. ICSE", "2019 IEEE/ACM 41st International Conference on Software Engineering (ICSE)")
    assert checks.venue_matches("NAACL", "Proceedings of the 2019 Conference of the North American Chapter of the "
                                "Association for Computational Linguistics: Human Language Technologies")
    assert not checks.venue_matches("Nature", "Proceedings of the 2019 CHI Conference on Human Factors in Computing Systems")


# --- With a record ------------------------------------------------------------------

PAPER = rec(
    "doi.org", type="paper-conference", doi="10.1145/3290605.3300857", title="Transformation through Provocation?",
    people=["Maria Roussou", "Sara Perry"], years=[2019], venue="Proceedings of the 2019 CHI Conference on Human "
    "Factors in Computing Systems", pages="1-13", publisher="ACM",
)
GOOD = r"""@inproceedings{roussou2019,
  title = {Transformation through {P}rovocation?},
  author = {Roussou, Maria and Perry, Sara},
  booktitle = {CHI '19},
  year = {2019},
  pages = {1--13},
  publisher = {ACM},
  doi = {10.1145/3290605.3300857},
}"""


def test_a_correct_entry_is_quiet():
    src = FakeSources(doi={PAPER["doi"]: found(PAPER)})
    result = checks.lookup(entry(GOOD), src)
    assert result.findings == []
    assert result.record["title"] == PAPER["title"]


def test_a_doi_of_another_work():
    other = rec("doi.org", doi="10.1145/1111111.1111111", title="Deep Learning for Cats", people=["Ann Other"],
                years=[2020])
    wrong = GOOD.replace("10.1145/3290605.3300857", "10.1145/1111111.1111111")
    src = FakeSources(doi={other["doi"]: found(other)},
                      crossref_search={"transformation through provocation?": found(PAPER)})
    result = checks.lookup(entry(wrong), src)
    assert codes(result.findings) == {"doi_mismatch": "error", "suggest_doi": "info"}
    suggestion = next(f for f in result.findings if f["code"] == "suggest_doi")
    assert suggestion["record_value"] == "10.1145/3290605.3300857"
    assert result.record is None


def test_a_made_up_doi_on_a_real_paper():
    wrong = GOOD.replace("10.1145/3290605.3300857", "10.1109/9999999.9999999")
    src = FakeSources(crossref_prefix={"10.1109": found(name="Institute of Electrical and Electronics Engineers (IEEE)")},
                      s2_match={"transformation through provocation?": found(PAPER)})
    result = checks.lookup(entry(wrong), src)
    assert codes(result.findings) == {"doi_not_found": "error", "suggest_doi": "info"}
    message = next(f for f in result.findings if f["code"] == "doi_not_found")["message"]
    assert "Institute of Electrical and Electronics Engineers (IEEE)" in message
    assert "The entry names ACM." in message


def test_chimeras_and_small_differences():
    chimera = GOOD.replace("Roussou, Maria and Perry, Sara", "Hinton, Geoffrey and LeCun, Yann")
    src = FakeSources(doi={PAPER["doi"]: found(PAPER)})
    assert codes(checks.lookup(entry(chimera), src).findings) == {"authors": "error"}

    swapped = GOOD.replace("Roussou, Maria and Perry, Sara", "Perry, Sara and Roussou, Maria")
    f = checks.lookup(entry(swapped), src).findings
    assert codes(f) == {"authors": "warning"} and "order" in f[0]["message"]

    fewer = GOOD.replace("Roussou, Maria and Perry, Sara", "Roussou, Maria")
    f = checks.lookup(entry(fewer), src).findings
    assert codes(f) == {"authors": "warning"} and "Sara Perry" in f[0]["message"]
    others = GOOD.replace("Roussou, Maria and Perry, Sara", "Roussou, Maria and others")
    assert checks.lookup(entry(others), src).findings == []
    accents = GOOD.replace("Roussou, Maria", r"Rouss{\'o}u, Mar{\'\i}a")
    assert checks.lookup(entry(accents), src).findings == []

    shifted = GOOD.replace("{2019}", "{2018}").replace("{1--13}", "{1--14}").replace("CHI '19", "Nature")
    f = checks.lookup(entry(shifted), src).findings
    assert codes(f) == {"year": "warning", "differs": "warning", "venue": "warning"}
    assert next(x for x in f if x["code"] == "differs")["record_value"] == "1--13"

    renamed = GOOD.replace("Transformation through {P}rovocation?", "Transformation through Provocation in Museums")
    assert codes(checks.lookup(entry(renamed), src).findings) == {"title": "warning"}

    bare = "@inproceedings{a, title = {Transformation through Provocation?}, author = {Roussou, Maria and Perry, Sara}," \
           " year = {2019}, doi = {10.1145/3290605.3300857}}"
    missing = checks.lookup(entry(bare), src).findings
    # A proceedings paper gets its pages, not its publisher, which styles leave out.
    assert {(f["code"], f["field"], f["record_value"]) for f in missing} == {("missing", "pages", "1--13")}


def test_retractions_corrections_and_published_preprints():
    work = rec("crossref", doi=PAPER["doi"], title=PAPER["title"])
    work["updates"] = [{"type": "correction", "doi": "10.1/c", "year": 2020},
                       {"type": "retraction", "doi": "10.1/r", "year": 2021}]
    src = FakeSources(doi={PAPER["doi"]: found(PAPER)}, crossref_work={PAPER["doi"]: found(work)})
    f = checks.lookup(entry(GOOD), src).findings
    assert codes(f) == {"retracted": "error", "corrected": "info"}
    assert next(x for x in f if x["code"] == "retracted")["url"] == "https://doi.org/10.1/r"

    preprint = rec("doi.org", doi="10.48550/arXiv.1706.03762", title="Attention Is All You Need",
                   people=["Ashish Vaswani"], years=[2017], arxiv="1706.03762")
    published = rec("semantic scholar", doi="10.5555/3295222.3295349", title="Attention is All you Need",
                    venue="NeurIPS", arxiv="1706.03762")
    src = FakeSources(doi={"10.48550/arxiv.1706.03762": found(preprint)},
                      s2_paper={"arxiv:1706.03762": found(published)})
    text = "@misc{v, title = {Attention Is All You Need}, author = {Vaswani, Ashish}, year = {2017}, " \
           "eprint = {1706.03762}, archiveprefix = {arXiv}}"
    f = checks.lookup(entry(text), src).findings
    assert codes(f) == {"published": "info"}
    assert f[0]["record_value"] == "10.5555/3295222.3295349"


def test_works_no_index_has():
    made_up = "@article{x, title = {Quantum Carbon Accounting for Language Models}, author = {Smith, John}, " \
              "journal = {Journal of Green Computing}, year = {2023}}"
    near = rec("crossref", title="Carbon Accounting for Language Models", people=["Ann Other"], years=[2023])
    src = FakeSources(
        crossref_search={"quantum carbon accounting for language models": found(near)},
        dblp_search=down("DBLP"),
        s2_author_titles={"john smith": found(name="John Smith", count=12, titles=[["Something else", 2020]])},
    )
    f = checks.lookup(entry(made_up), src).findings
    assert codes(f) == {"not_found": "error"}
    message = f[0]["message"]
    assert "Crossref, Semantic Scholar and arXiv were asked." in message
    assert "The closest is “Carbon Accounting for Language Models” by Ann Other (2023)." in message
    assert "John Smith has 12 papers on Semantic Scholar, and none has a similar title." in message

    report = made_up.replace("@article", "@techreport")
    assert codes(checks.lookup(entry(report), src).findings) == {"not_found": "warning"}

    src = FakeSources(crossref_search=down("Crossref"), s2_match=down(), dblp_search=down(), arxiv_search=found())
    assert codes(checks.lookup(entry(made_up), src).findings) == {"unavailable": "info"}


def test_a_hit_by_title_suggests_its_doi():
    text = "@inproceedings{r, title = {Transformation through Provocation?}, author = {Roussou, Maria and Perry, Sara}," \
           " booktitle = {CHI}, year = {2019}}"
    src = FakeSources(crossref_search={"transformation through provocation?": found(PAPER)})
    result = checks.lookup(entry(text), src)
    assert codes(result.findings) == {"suggest_doi": "info"}
    assert result.record["doi"] == PAPER["doi"]


def test_a_hit_of_the_entrys_year_wins_over_a_copy():
    text = "@inproceedings{v, title = {Attention is All you Need}, author = {Vaswani, Ashish and Shazeer, Noam}, " \
           "booktitle = {Advances in Neural Information Processing Systems 30}, year = {2017}}"
    junk = rec("crossref", doi="10.65215/r5bs2d54", title="Attention Is All You Need",
               people=["Ashish Vaswani", "Noam Shazeer"],
               years=[2025])
    real = rec("arxiv", doi="10.48550/arXiv.1706.03762", title="Attention Is All You Need",
               people=["Ashish Vaswani", "Noam Shazeer"], years=[2017], arxiv="1706.03762", venue="arXiv")
    src = FakeSources(crossref_search={"attention is all you need": found(junk)},
                      arxiv_search={"attention is all you need": found(real)})
    result = checks.lookup(entry(text), src)
    assert result.findings == []  # no year warning, and no arXiv DOI for a NeurIPS paper
    assert result.record["source"] == "arxiv"

    # Only the copy: it is the hit, but its DOI is not offered.
    src = FakeSources(crossref_search={"attention is all you need": found(junk)})
    assert codes(checks.lookup(entry(text), src).findings) == {"year": "warning"}


SWAPPED = rec(
    "doi.org", doi="10.1109/GREENS66463.2025.00009",
    title="PowerLetrics: An Open-Source Framework for Power and Energy Metrics for Linux",
    people=[{"family": "Geerd-Dietger", "given": "Hoffmann"}, {"family": "Verena", "given": "Majuntke"}],
    years=[2025], venue="2025 IEEE/ACM 9th International Workshop on Green and Sustainable Software (GREENS)",
    pages="28-30",
)
POWERLETRICS = r"""@inproceedings{p,
  author = {Hoffmann, Geerd-Dietger and Majuntke, Verena},
  title = {{PowerLetrics}: An Open-Source Framework for Power and Energy Metrics for {Linux}},
  booktitle = {Proceedings of the IEEE/ACM 9th International Workshop on Green and Sustainable Software (GREENS)},
  pages = {28--30},
  year = {2025},
  doi = {10.1109/GREENS66463.2025.00009},
}"""


def test_names_the_record_has_the_wrong_way_round():
    src = FakeSources(doi={SWAPPED["doi"].lower(): found(SWAPPED)})
    f = checks.lookup(entry(POWERLETRICS), src).findings
    assert codes(f) == {"authors": "warning"}
    assert "Geerd-Dietger Hoffmann and Verena Majuntke the wrong way round" in f[0]["message"]
    # The swapped names are no value to take.
    assert f[0]["record_value"] is None

    initials = POWERLETRICS.replace("Geerd-Dietger and Majuntke, Verena", "G.-D. and Majuntke, V.")
    assert codes(checks.lookup(entry(initials), src).findings) == {"authors": "warning"}

    # Found by its title, it is the same work and not one no source has.
    search = FakeSources(crossref_search={SWAPPED["title"].lower(): found(SWAPPED)})
    no_doi = POWERLETRICS.replace("  doi = {10.1109/GREENS66463.2025.00009},\n", "")
    f = checks.lookup(entry(no_doi), search).findings
    assert codes(f) == {"authors": "warning", "suggest_doi": "info"}


LNCS = "Lecture Notes in Computer Science"
PROFES = "Product-Focused Software Process Improvement. Industry, Doctoral-Symposium, Tutorial, and Workshop Papers"
CHAPTER = rec("doi.org", doi="10.1007/978-3-032-12092-2_15",
              title="Toward Greener Background Processes: Measuring Energy Cost of Autosave Feature",
              people=["Maria Küüsvek", "Hina Anwar"], years=[2025], venue=LNCS, pages="206-217")
AUTOSAVE = r"""@inproceedings{k,
  author = {K{\"u}{\"u}svek, Maria and Anwar, Hina},
  title = {Toward Greener Background Processes: Measuring Energy Cost of Autosave Feature},
  booktitle = {Product-Focused Software Process Improvement (PROFES)},
  series = {Lecture Notes in Computer Science},
  pages = {206--217},
  year = {2025},
  doi = {10.1007/978-3-032-12092-2_15},
}"""


def test_a_book_in_a_series():
    work = rec("crossref", doi=CHAPTER["doi"], title=CHAPTER["title"], venues=[LNCS, PROFES])
    src = FakeSources(doi={CHAPTER["doi"]: found(CHAPTER)}, crossref_work={CHAPTER["doi"]: found(work)})
    assert checks.lookup(entry(AUTOSAVE), src).findings == []

    # The series in booktitle, as taking doi.org's venue would have made it.
    series = AUTOSAVE.replace("Product-Focused Software Process Improvement (PROFES)", LNCS)
    f = checks.lookup(entry(series), src).findings
    assert codes(f) == {"venue": "warning"} and "by hand" in f[0]["message"]
    assert f[0]["record_value"] is None

    other = AUTOSAVE.replace("Product-Focused Software Process Improvement (PROFES)", "Nature")
    f = checks.lookup(entry(other), src).findings
    assert codes(f) == {"venue": "warning"} and "more than one venue" in f[0]["message"]
    assert f[0]["record_value"] is None

    # Without Crossref's titles the series is all doi.org has, and the entry names it in series.
    alone = FakeSources(doi={CHAPTER["doi"]: found(CHAPTER)})
    assert checks.lookup(entry(AUTOSAVE), alone).findings == []


def test_books_compare_only_what_editions_share():
    book = rec("openlibrary", type="book", title="Deep Learning", years=[2017], isbn="9780262035613",
               people=["Ian Goodfellow", "Yoshua Bengio", "Aaron Courville", "Francis Bach"])
    src = FakeSources(openlibrary_isbn={"9780262035613": found(book)})
    text = "@book{g, title = {Deep Learning}, author = {Goodfellow, Ian and Bengio, Yoshua and Courville, Aaron}, " \
           "year = {2016}, isbn = {9780262035613}}"
    assert checks.lookup(entry(text), src).findings == []
    wrong = text.replace("Goodfellow, Ian and Bengio, Yoshua and Courville, Aaron", "Knuth, Donald")
    assert codes(checks.lookup(entry(wrong), src).findings) == {"authors": "error"}


def test_links():
    def link(url: str, answer, **extra) -> dict[str, str]:
        src = FakeSources(page={url: answer}, **extra)
        return checks.lookup(entry(f"@online{{w, title = {{Green Metrics Tool}}, url = {{{url}}}, urldate = {{2024-01-01}}}}"),
                             src)

    ok = link("https://x.org/gmt", page("https://x.org/gmt", 200, title="Green Metrics Tool"))
    assert ok.findings == [] and ok.record["source"] == "web"

    dead = link("https://x.org/gone", page("https://x.org/gone", 404),
                wayback={"https://x.org/gone": found(url="https://web.archive.org/web/2023/x", timestamp="20231201")})
    assert codes(dead.findings) == {"url_dead": "error"}
    assert dead.findings[0]["url"] == "https://web.archive.org/web/2023/x"
    assert "copy from 2023-12-01" in dead.findings[0]["message"]

    repo = link("https://github.com/me/nothing", page("https://github.com/me/nothing", 404))
    assert "GitHub has no public repository" in repo.findings[0]["message"]
    nxdomain = link("https://gone.example/x", page("https://gone.example/x", None, error="dns"))
    assert codes(nxdomain.findings) == {"url_dead": "error"}
    soft = link("https://x.org/a", page("https://x.org/a", 200, title="Page not found | X"))
    assert codes(soft.findings) == {"url_moved": "warning"}
    home = link("https://x.org/a/b", page("https://x.org/a/b", 200, title="X", final_url="https://x.org/"))
    assert codes(home.findings) == {"url_moved": "warning"}
    blocked = link("https://x.org/b", page("https://x.org/b", 403))
    assert codes(blocked.findings) == {"url_blocked": "info"}
    other = link("https://x.org/c", page("https://x.org/c", 200, title="Recipes for apple pie"))
    assert codes(other.findings) == {"url_title": "info"}
    private = link("http://10.0.0.1/x", page("http://10.0.0.1/x", None, error="private"))
    assert codes(private.findings) == {"url_blocked": "info"}


def test_an_arxiv_id_nobody_has():
    src = FakeSources(arxiv_paper={"2305.99999": found()})
    text = "@misc{a, title = {A}, eprint = {2305.99999}, eprinttype = {arXiv}, year = {2023}}"
    assert codes(checks.lookup(entry(text), src).findings) == {"not_found": "error"}
