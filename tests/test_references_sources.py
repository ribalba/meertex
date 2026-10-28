"""The reference lookups (app/references/sources.py) against a fake network:
the address rules, redirects, retries, and what each service's answer
becomes. No database, no network."""

import json

import httpx
import pytest

from app.references import sources
from app.references.sources import Http, MemoryCache, Sources

PUBLIC = "93.184.215.14"


def resolver(table: dict[str, str] | None = None):
    table = table or {}

    def resolve(host: str, port: int) -> list[str]:
        if host in table:
            return [table[host]]
        if host == "nowhere.example":
            raise OSError("not used")
        return [PUBLIC]

    return resolve


def make(handler, table=None, sleeps=None) -> Sources:
    http = Http(transport=httpx.MockTransport(handler), resolve=resolver(table),
                sleep=(sleeps.append if sleeps is not None else (lambda s: None)))
    return Sources(http, MemoryCache())


@pytest.fixture(autouse=True)
def no_pace(monkeypatch):
    monkeypatch.setattr(sources, "PACE", {})
    monkeypatch.setattr(sources, "DEFAULT_PACE", 0.0)


# --- Http ---------------------------------------------------------------------------


def test_is_public():
    assert sources.is_public("93.184.215.14")
    assert sources.is_public("2606:2800:21f:cb07:6820:80da:af6b:8b2c")
    for address in ("127.0.0.1", "10.1.2.3", "172.20.0.5", "192.168.1.1", "169.254.169.254", "100.64.0.1",
                    "0.0.0.0", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "224.0.0.1", "not an ip"):
        assert not sources.is_public(address), address


def test_private_hosts_schemes_and_redirects_are_refused():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/hop":
            return httpx.Response(302, headers={"location": "http://db.internal/secret"})
        if request.url.path == "/loop":
            return httpx.Response(302, headers={"location": "/loop"})
        return httpx.Response(200, text="ok")

    s = make(handler, {"db.internal": "172.18.0.2", "localhost": "127.0.0.1"})
    for url in ("http://127.0.0.1:5432/", "http://localhost/", "http://[::1]/", "http://db.internal/"):
        f = s.http.get(url)
        assert f.error == "private", url
    assert s.http.get("ftp://example.com/x").error == "scheme"
    assert s.http.get("file:///etc/passwd").error == "scheme"
    assert seen == []

    f = s.http.get("https://example.com/hop")
    assert f.error == "private"
    assert seen == ["https://example.com/hop"]  # the internal address was never asked
    assert s.http.get("https://example.com/loop").error == "redirects"


def test_unknown_hosts_size_cap_and_retries():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/big":
            return httpx.Response(200, content=b"x" * 5000)
        if request.url.path == "/busy":
            calls["n"] += 1
            return httpx.Response(429 if calls["n"] < 3 else 200, headers={"retry-after": "3"}, text="fine")
        return httpx.Response(200)

    def resolve(host: str, port: int) -> list[str]:
        if host == "gone.example":
            import socket

            raise socket.gaierror("Name or service not known")
        return [PUBLIC]

    sleeps: list[float] = []
    http = Http(transport=httpx.MockTransport(handler), resolve=resolve, sleep=sleeps.append)
    assert http.get("https://gone.example/").error == "dns"
    assert len(http.get("https://example.com/big", max_bytes=1000).body) == 1000
    f = http.get("https://example.com/busy")
    assert f.status == 200 and f.body == b"fine"
    assert sleeps == [3.0, 3.0]
    calls["n"] = 0
    assert http.get("https://example.com/busy", retries=0).status == 429


# --- Services -----------------------------------------------------------------------

CSL = {
    "type": "paper-conference",
    "title": "Transformation through Provocation?",
    "author": [{"family": "Roussou", "given": "Maria"}, {"family": "Perry", "given": "Sara"},
               {"literal": "The Museum Group"}],
    "issued": {"date-parts": [[2019, 5, 2]]},
    "published-online": {"date-parts": [[2019, 4, 30]]},
    "container-title": "Proceedings of the 2019 CHI Conference on Human Factors in Computing Systems",
    "page": "1-13",
    "publisher": "ACM",
    "DOI": "10.1145/3290605.3300857",
    "URL": "http://dx.doi.org/10.1145/3290605.3300857",
}


def test_doi_records_and_the_cache():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["accept"] == "application/vnd.citationstyles.csl+json"
        assert "mailto:" in request.headers["user-agent"]
        if request.url.path == "/10.1145/3290605.3300857":
            return httpx.Response(302, headers={"location": "https://api.crossref.org/v1/works/x/transform"})
        if request.url.host == "api.crossref.org":
            return httpx.Response(200, json=CSL)
        if request.url.path == "/10.1145/0000000.0000000":
            return httpx.Response(404, text="DOI Not Found")
        return httpx.Response(503)

    s = make(handler)
    a = s.doi("https://doi.org/10.1145/3290605.3300857")
    assert a.ok
    r = a.record
    assert r["title"] == "Transformation through Provocation?"
    assert r["authors"] == ["Maria Roussou", "Sara Perry", "The Museum Group"]
    assert r["people"][2] == {"family": "The Museum Group", "given": ""}
    assert r["years"] == [2019]
    assert r["year"] == 2019
    assert r["pages"] == "1-13"
    assert r["doi"] == "10.1145/3290605.3300857"
    assert s.doi("10.1145/3290605.3300857").record == r
    assert calls.count("/10.1145/3290605.3300857") == 1  # cached

    missing = s.doi("10.1145/0000000.0000000")
    assert missing.ok and missing.record is None
    broken = s.doi("10.9999/down")
    assert not broken.ok and "503" in broken.error


def test_crossref_updates_prefix_and_search():
    work = {"message": {
        **CSL, "title": ["RETRACTED: A paper"], "container-title": ["The Lancet"],
        "updated-by": [{"type": "retraction", "DOI": "10.1016/R", "label": "Retraction",
                        "updated": {"date-parts": [[2010, 2, 6]]}}],
        "relation": {"is-preprint-of": [{"id-type": "doi", "id": "10.1/published"}]},
        "abstract": "<jats:p>We show <jats:italic>things</jats:italic>.</jats:p>",
    }}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/works/"):
            return httpx.Response(200, json=work)
        if request.url.path == "/prefixes/10.1145":
            return httpx.Response(200, json={"message": {"name": "Association for Computing Machinery (ACM)"}})
        if request.url.path == "/works":
            assert "query.bibliographic" in str(request.url)
            return httpx.Response(200, json={"message": {"items": [{**CSL, "title": ["A"], "container-title": ["B"]}]}})
        return httpx.Response(404)

    s = make(handler)
    r = s.crossref_work("10.1016/x").record
    assert r["venue"] == "The Lancet"
    assert r["updates"] == [{"type": "retraction", "doi": "10.1016/R", "label": "Retraction", "year": 2010}]
    assert r["preprint_of"] == ["10.1/published"]
    assert r["abstract"] == "We show things."
    assert s.crossref_prefix("10.1145").info == {"name": "Association for Computing Machinery (ACM)"}
    assert s.crossref_prefix("10.9999").ok
    assert [x["title"] for x in s.crossref_search("A", "Roussou", 2019).records] == ["A"]


def test_subtitles_and_every_container_title():
    works = {
        "/works/10.1145/3177754": {**CSL, "title": ["RAPL in Action"],
                                   "subtitle": ["Experiences in Using RAPL for Power Measurements"]},
        "/works/10.1145/3555050.3569131": {**CSL, "title": ["How gullible are web measurement tools?"],
                                           "subtitle": ["a case study analysing and strengthening OpenWPM's reliability"]},
        "/works/10.1007/978-3-032-12092-2_15": {
            **CSL, "title": ["Toward Greener Background Processes"], "subtitle": [],
            "container-title": ["Lecture Notes in Computer Science", "Product-Focused Software Process Improvement"]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in works:
            return httpx.Response(200, json={"message": works[request.url.path]})
        return httpx.Response(404)

    s = make(handler)
    assert s.crossref_work("10.1145/3177754").record["title"] == \
        "RAPL in Action: Experiences in Using RAPL for Power Measurements"
    # A title that ends in a question mark takes the subtitle after a space.
    assert s.crossref_work("10.1145/3555050.3569131").record["title"] == \
        "How gullible are web measurement tools? a case study analysing and strengthening OpenWPM's reliability"
    r = s.crossref_work("10.1007/978-3-032-12092-2_15").record
    assert r["title"] == "Toward Greener Background Processes"
    assert r["venue"] == "Lecture Notes in Computer Science"
    assert r["venues"] == ["Lecture Notes in Computer Science", "Product-Focused Software Process Improvement"]


def test_semantic_scholar_dblp_arxiv_and_open_library():
    atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
      <entry><id>http://arxiv.org/abs/1706.03762v7</id><published>2017-06-12T17:57:34Z</published>
      <title>Attention Is All
        You Need</title><summary> The dominant sequence transduction models. </summary>
      <author><name>Ashish Vaswani</name></author><author><name>Noam Shazeer</name></author>
      <arxiv:doi>10.5555/3295222</arxiv:doi></entry></feed>"""

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "api.semanticscholar.org" and path.endswith("/search/match"):
            return httpx.Response(200, json={"data": [{"title": "Attention is All you Need", "year": 2017,
                                                      "authors": [{"name": "Ashish Vaswani"}], "venue": "NeurIPS",
                                                      "externalIds": {"ArXiv": "1706.03762", "DOI": None},
                                                      "abstract": "Abstract."}]})
        if host == "dblp.org":
            hit = {"info": {"title": "Attention is All you Need.", "year": "2017", "venue": "NIPS",
                            "authors": {"author": [{"text": "Ashish Vaswani"}, {"text": "Lukasz Kaiser 0001"}]}}}
            return httpx.Response(200, json={"result": {"hits": {"hit": [hit]}}},
                                  headers={"content-type": "application/json"})
        if host == "export.arxiv.org":
            assert 'ti:"Attention Is All You Need"' in request.url.params["search_query"]
            return httpx.Response(200, text=atom)
        if host == "openlibrary.org" and path == "/isbn/9780262035613.json":
            return httpx.Response(200, json={"title": "Deep Learning", "publish_date": "3 January 2017",
                                             "publishers": ["MIT Press"], "works": [{"key": "/works/OL1W"}]})
        if host == "openlibrary.org" and path == "/works/OL1W.json":
            return httpx.Response(200, json={"authors": [{"author": {"key": "/authors/OL1A"}}]})
        if host == "openlibrary.org" and path == "/authors/OL1A.json":
            return httpx.Response(200, json={"name": "Ian Goodfellow"})
        return httpx.Response(404)

    s = make(handler)
    r = s.s2_match("Attention Is All You Need").record
    assert (r["title"], r["year"], r["arxiv"], r["authors"]) == ("Attention is All you Need", 2017, "1706.03762",
                                                                  ["Ashish Vaswani"])
    d = s.dblp_search("Attention Is All You Need").record
    assert d["title"] == "Attention is All you Need"
    assert d["people"][1] == {"family": "Kaiser", "given": "Lukasz"}
    a = s.arxiv_search("Attention Is All You Need").record
    assert a["title"] == "Attention Is All You Need"
    assert a["arxiv"] == "1706.03762"
    assert a["doi"] == "10.48550/arXiv.1706.03762"
    assert a["abstract"] == "The dominant sequence transduction models."
    b = s.openlibrary_isbn("9780262035613").record
    assert (b["title"], b["authors"], b["year"], b["publisher"]) == ("Deep Learning", ["Ian Goodfellow"], 2017,
                                                                      "MIT Press")


def test_dblp_bot_wall_counts_as_no_answer():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, text="<title>Making sure you're not a bot!</title>",
                              headers={"content-type": "text/html"})

    sleeps: list[float] = []
    s = make(handler, sleeps=sleeps)
    a = s.dblp_search("Anything")
    assert not a.ok
    assert len(sleeps) == 1
    # After three failures in a row DBLP is not asked again.
    s.dblp_search("Two")
    s.dblp_search("Three")
    asked = len(sleeps)
    skipped = s.dblp_search("Four")
    assert not skipped.ok and "was not asked again" in skipped.error
    assert len(sleeps) == asked


def test_pages_meta_tags_and_wayback():
    html = """<html><head><title>Paper page</title>
      <meta name="citation_title" content="Green Software">
      <meta name="citation_author" content="Hoffmann, Didi"><meta name="citation_author" content="Jane Doe">
      <meta name="citation_publication_date" content="2024/03/01">
      <meta name="citation_doi" content="10.1234/green">
      <script type="application/ld+json">{"@type": "Article", "headline": "Ignored"}</script>
      </head><body><h1>Green Software</h1></body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "archive.org":
            return httpx.Response(200, json={"archived_snapshots": {"closest": {
                "available": True, "url": "http://web.archive.org/web/2020/https://old.example/x", "timestamp": "20200101"}}})
        if request.url.path == "/paper":
            return httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/"})
        return httpx.Response(200, text="<title>Home</title>", headers={"content-type": "text/html"})

    s = make(handler)
    p = s.page("https://example.com/paper").info
    assert p["status"] == 200 and p["title"] == "Paper page" and p["h1"] == "Green Software"
    r = p["record"]
    assert r["title"] == "Green Software"
    assert r["authors"] == ["Didi Hoffmann", "Jane Doe"]
    assert r["year"] == 2024
    assert r["doi"] == "10.1234/green"
    moved = s.page("https://example.com/old").info
    assert moved["final_url"] == "https://example.com/"
    assert s.page("http://10.0.0.1/").ok is False
    w = s.wayback("https://old.example/x", "20200101")
    assert w.info["url"] == "https://web.archive.org/web/2020/https://old.example/x"


def test_answers_survive_the_cache_as_json():
    a = sources.Answer(True, [sources._record("x", title="T", people=[{"family": "F", "given": "G"}])], {"k": 1})
    again = sources.Answer.from_json(json.loads(json.dumps(a.to_json())))
    assert again.records[0]["authors"] == ["G F"]
    assert again.info == {"k": 1}
