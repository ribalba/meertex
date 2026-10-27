"""A stand-in for the reference lookups (app/references/sources.py), so the
checks and the API can be tested without the network.

Each lookup is answered from the dict given for its name, by its argument
(lowercased). A lookup without an answer says "answered, nothing there",
which is what most services say about a made-up work.
"""

from app.references.sources import Answer, MemoryCache, _record


def rec(source: str = "doi.org", **values: object) -> dict:
    """A record as the sources give it. ``people`` may be given as names."""
    people = values.pop("people", [])
    values["people"] = [
        p if isinstance(p, dict) else {"family": p.rsplit(" ", 1)[-1], "given": p.rsplit(" ", 1)[0] if " " in p else ""}
        for p in people
    ]
    return _record(source, **values)


def found(*records: dict, **info: object) -> Answer:
    return Answer(True, list(records), dict(info))


def down(what: str = "The service") -> Answer:
    return Answer(False, error=f"{what} could not be reached (timeout).")


class FakeSources:
    def __init__(self, **answers: dict) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []
        self.cache = MemoryCache()

    def _a(self, name: str, arg: str) -> Answer:
        self.calls.append((name, arg))
        table = self.answers.get(name, {})
        answer = table.get(arg.lower()) if isinstance(table, dict) else table
        if callable(answer):
            answer = answer()
        return answer if answer is not None else Answer(True)

    def close(self) -> None:
        pass

    def doi(self, doi: str) -> Answer:
        return self._a("doi", doi)

    def crossref_work(self, doi: str) -> Answer:
        return self._a("crossref_work", doi)

    def crossref_prefix(self, prefix: str) -> Answer:
        return self._a("crossref_prefix", prefix)

    def crossref_search(self, title: str, author: str = "", year: int | None = None) -> Answer:
        return self._a("crossref_search", title)

    def s2_match(self, title: str) -> Answer:
        return self._a("s2_match", title)

    def s2_paper(self, ident: str) -> Answer:
        return self._a("s2_paper", ident)

    def s2_author_titles(self, name: str) -> Answer:
        return self._a("s2_author_titles", name)

    def dblp_search(self, title: str) -> Answer:
        return self._a("dblp_search", title)

    def arxiv_search(self, title: str) -> Answer:
        return self._a("arxiv_search", title)

    def arxiv_paper(self, arxiv_id: str) -> Answer:
        return self._a("arxiv_paper", arxiv_id)

    def arxiv_fulltext(self, arxiv_id: str) -> Answer:
        return self._a("arxiv_fulltext", arxiv_id)

    def openlibrary_isbn(self, isbn: str) -> Answer:
        return self._a("openlibrary_isbn", isbn)

    def openlibrary_search(self, title: str) -> Answer:
        return self._a("openlibrary_search", title)

    def page(self, url: str) -> Answer:
        return self._a("page", url)

    def wayback(self, url: str, timestamp: str = "") -> Answer:
        return self._a("wayback", url)


def page(url: str, status: int | None = 200, title: str = "", error: str = "", final_url: str = "", h1: str = "",
         record: dict | None = None) -> Answer:
    """What ``Sources.page`` gives for a link."""
    info = {"url": url, "final_url": final_url or url, "status": status, "error": error, "detail": "",
            "title": title, "h1": h1, "record": record}
    return Answer(error not in ("scheme", "private"), info=info)
