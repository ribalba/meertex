"""Project names from the naming pattern (DESIGN §4)."""

from helpers import needs_db, running_app, sign_in

from app.naming import derive_paper, render, slug, valid_name, validate_pattern

DEFAULT = "{year}-{conference|lower}-{paper|lower}"


def test_slug_spells_out_umlauts_and_drops_accents():
    assert slug("Grüße aus Köln") == "Gruesse-aus-Koeln"
    assert slug("ÄÖÜ äöü ß") == "AeOeUe-aeoeue-ss"
    assert slug("Café Señor Øre") == "Cafe-Senor-re"
    # Decomposed umlauts (a + combining diaeresis) count too.
    assert slug("München") == "Muenchen"
    assert slug("  --Hello,   World!--  ") == "Hello-World"
    assert slug("") == ""
    assert slug(2027) == "2027"


def test_paper_from_title_skips_stop_words():
    assert derive_paper("Towards Green LLMs: Energy-Aware Inference") == "green-llms-energy-aware"
    assert derive_paper("The Cost of a Token") == "cost-token"
    assert derive_paper("On the Use of Über-Caches for Fast Builds") == "use-ueber-caches-fast"
    assert derive_paper("") == ""


def test_default_pattern():
    fields = {"year": 2027, "conference": "ICSE", "paper": "Green LLM", "title": "Whatever"}
    assert render(DEFAULT, fields) == "2027-icse-green-llm"


def test_paper_derived_when_empty():
    fields = {"year": 2027, "conference": "ICSE", "paper": "", "title": "Towards Green LLMs: Energy-Aware Inference"}
    assert render(DEFAULT, fields) == "2027-icse-green-llms-energy-aware"


def test_text_outside_placeholders_and_filters():
    fields = {"year": 2027, "conference": "EuroSys", "paper": "pacer"}
    assert render("{conference}{year}-{paper}", fields) == "EuroSys2027-pacer"
    assert render("{conference|upper}_{year}", fields) == "EUROSYS_2027"
    assert render("paper.{paper|upper}", fields) == "paper.PACER"


def test_empty_placeholders_collapse_with_their_separator():
    fields = {"year": 2027, "conference": "ICSE", "paper": "green"}
    assert render("{year}-{conference|lower}-{track}-{paper}", fields) == "2027-icse-green"
    assert render("{track}-{year}-{paper}", fields) == "2027-green"
    assert render("{year}-{paper}-{track}", fields) == "2027-green"
    assert render("{year}-{track}{conference}", fields) == "2027-ICSE"
    assert render("{year}-{track}-{track}-{paper}", fields) == "2027-green"
    assert render("{track}", fields) == ""
    assert render(DEFAULT, {}) == ""


def test_long_names_are_cut():
    name = render("{title}", {"title": "word " * 60})
    assert len(name) <= 120
    assert valid_name(name)
    assert not name.endswith("-")


def test_valid_names_and_patterns():
    assert valid_name("2027-icse-green-llm")
    assert valid_name("a")
    assert not valid_name("-leading")
    assert not valid_name("has space")
    assert not valid_name("x" * 121)
    assert validate_pattern(DEFAULT) is None
    assert validate_pattern("{conference}{year}-{paper}") is None
    assert validate_pattern("paper") is not None
    assert validate_pattern("{year}-{venue}") is not None
    assert validate_pattern("{year} {paper}") is not None


@needs_db
def test_collisions_get_a_number():
    with running_app() as client:
        sign_in(client)
        body = {"title": "Green LLMs", "conference": "ICSE", "year": 2027}
        names = [client.post("/api/projects", json=body).json()["name"] for _ in range(3)]
        assert names == ["2027-icse-green-llms", "2027-icse-green-llms-2", "2027-icse-green-llms-3"]
        r = client.post("/api/projects", json=body | {"dry_run": True})
        assert r.status_code == 200
        assert r.json() == {"name": "2027-icse-green-llms-4", "dry_run": True}
        # Another owner has a namespace of their own.
        from helpers import second_client

        with second_client("arne@example.com") as other:
            assert other.post("/api/projects", json=body).json()["name"] == "2027-icse-green-llms"


@needs_db
def test_empty_name_is_refused():
    with running_app() as client:
        sign_in(client)
        r = client.post("/api/projects", json={"notes": "nothing to name it by"})
        assert r.status_code == 422
        assert "conference, year and title" in r.json()["detail"]
        r = client.post("/api/projects", json={"name": "my-paper"})
        assert r.status_code == 201
        assert r.json()["name"] == "my-paper"
        assert client.post("/api/projects", json={"name": "bad name"}).status_code == 422
