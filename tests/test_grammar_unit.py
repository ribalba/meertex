"""The checker without a server or a database (DESIGN §17): settings, the
request, offsets, the paragraph cache, the budget, errors and the privacy guard.

LanguageTool is an httpx.MockTransport standing in through
checker.use_transport, and name resolution is a monkeypatched
socket.getaddrinfo, so nothing here touches the network. The fake reads the
annotated text it is sent and reports the words it is told to, at the offsets
a real LanguageTool would, in UTF-16 units into the joined items with markup
counted as written. tests/test_grammar_live.py does the same against a real one.

What matters most, and why each part is pinned:

  * offsets, because they are counted in UTF-16 units on both ends and in code
    points by Python, and an off-by-one is an underline on the wrong word;
  * the cache and the budget, because a file is long and the check runs on
    every pause in typing;
  * the privacy guard, because the promise of the feature is that a paper goes
    nowhere off this server unless the operator said it may;
  * the error mapping, because every failure has to reach the editor as a
    sentence, and none of them may carry the text.
"""

import json
import logging
import socket
import threading
import time
from urllib.parse import parse_qs

import httpx
import pytest

from app.config import Settings
from app.grammar import checker

LOCAL = "http://127.0.0.1:8010"

# Text that must never appear in an error message or in the log.
SECRET = "the merger closes on friday, tell nobody"


def _settings(**values) -> Settings:
    # _env_file=None keeps a checkout's own .env out of the assertions.
    return Settings(_env_file=None, **{"grammar_url": LOCAL, **values})


def units(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def at(text: str, word: str, nth: int = 0) -> int:
    """The UTF-16 offset of the nth ``word`` in ``text``, as JavaScript's
    indexOf would count it."""
    i = -1
    for _ in range(nth + 1):
        i = text.index(word, i + 1)
    return units(text[:i])


def lt_match(offset, length, rule="MORFOLOGIK_RULE_EN_US", issue="misspelling", category="TYPOS",
             message="Possible spelling mistake found.", short="Spelling mistake", replacements=("The",)) -> dict:
    """A match in LanguageTool's own v2 shape."""
    return {
        "message": message, "shortMessage": short,
        "replacements": [{"value": r} for r in replacements],
        "offset": offset, "length": length,
        "rule": {"id": rule, "description": f"{rule} description", "issueType": issue,
                 "category": {"id": category, "name": category.title()}},
    }


def answer(matches=None, code="en-US", name="English (US)") -> dict:
    return {"language": {"code": code, "name": name}, "matches": matches or []}


class Checker:
    """A fake LanguageTool. Records each request, and by default answers with
    a spelling match on every word in ``typos`` found in a text item, at the
    offset of the joined annotation in UTF-16 units. ``handler`` replaces that."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.typos = {"Teh", "recieve"}
        self.code, self.name = "en-US", "English (US)"
        self.extra: list[dict] = []
        self.handler = self.find

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def form(self, index: int = -1) -> dict[str, str]:
        parsed = parse_qs(self.requests[index].content.decode("utf-8"), keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items()}

    def items(self, index: int = -1) -> list[dict]:
        return json.loads(self.form(index)["data"])["annotation"]

    def joined(self, index: int = -1) -> str:
        """The request's source, text and markup as written."""
        return "".join(item.get("text", item.get("markup", "")) for item in self.items(index))

    def find(self, request: httpx.Request) -> httpx.Response:
        items = json.loads(parse_qs(request.content.decode("utf-8"))["data"][0])["annotation"]
        matches, pos = [], 0
        for item in items:
            if "text" in item:
                for word in self.typos:
                    start = 0
                    while (k := item["text"].find(word, start)) >= 0:
                        matches.append(lt_match(pos + units(item["text"][:k]), units(word)))
                        start = k + 1
                pos += units(item["text"])
            else:
                pos += units(item["markup"])
        return httpx.Response(200, json=answer(matches + self.extra, self.code, self.name))


@pytest.fixture
def configure(monkeypatch):
    """Swap the settings app/grammar/checker.py reads. Returns the setter."""

    def apply(**values):
        settings = _settings(**values)
        monkeypatch.setattr(checker, "get_settings", lambda: settings)
        return settings

    apply()
    return apply


@pytest.fixture
def lt(configure):
    fake = Checker()
    checker.use_transport(httpx.MockTransport(fake))
    yield fake
    checker.use_transport(None)


def config(**values) -> dict:
    return {**checker.defaults(), "language": "en-US", **values}


def found(result) -> list[tuple[int, int, str]]:
    return [(m["offset"], m["length"], m["rule"]) for m in result["matches"]]


# --- Configuration --------------------------------------------------------------------


def test_the_defaults_leave_the_feature_off(monkeypatch):
    for name in ("GRAMMAR_URL", "GRAMMAR_TIMEOUT_SECONDS", "GRAMMAR_ALLOW_PUBLIC_HOSTS", "GRAMMAR_MAX_CONCURRENT"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=None)
    assert (s.grammar_url, s.grammar_timeout_seconds, s.grammar_allow_public_hosts, s.grammar_max_concurrent) == (
        "", 20, False, 3)


def test_the_environment_reaches_the_settings(monkeypatch):
    monkeypatch.setenv("GRAMMAR_URL", "http://languagetool:8010")
    monkeypatch.setenv("GRAMMAR_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("GRAMMAR_ALLOW_PUBLIC_HOSTS", "true")
    monkeypatch.setenv("GRAMMAR_MAX_CONCURRENT", "5")
    s = Settings(_env_file=None)
    assert (s.grammar_url, s.grammar_timeout_seconds, s.grammar_allow_public_hosts, s.grammar_max_concurrent) == (
        "http://languagetool:8010", 45, True, 5)


def test_no_url_is_not_configured(configure, lt):
    configure(grammar_url="")
    assert checker.available() is False
    with pytest.raises(checker.NotConfigured, match="GRAMMAR_URL"):
        checker.check("Hello", config())
    with pytest.raises(checker.NotConfigured):
        checker.languages()
    assert lt.requests == []


@pytest.mark.parametrize("url", ["languagetool:8010", "ftp://lt:21", "http://", "http://lt:port"])
def test_a_url_that_is_not_one_says_so(configure, lt, url):
    configure(grammar_url=url)
    with pytest.raises(checker.NotConfigured, match="http:// or https://"):
        checker.check("Hello", config())
    assert lt.requests == []


@pytest.mark.parametrize("url", [LOCAL, LOCAL + "/", LOCAL + "/v2", LOCAL + "/v2/"])
def test_the_api_root_may_be_given_with_or_without_v2(configure, lt, url):
    configure(grammar_url=url)
    checker.check("Hello", config())
    assert str(lt.requests[-1].url) == LOCAL + "/v2/check"


# --- The stored settings ----------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "not json", "[1, 2]", '"text"', "42", [1], 42, {}])
def test_an_absent_or_unreadable_value_reads_as_the_defaults(raw):
    assert checker.parse_stored(raw) == {
        "enabled": True, "language": "auto", "variants": [], "picky": False, "words": [], "disabled_rules": [],
    }


def test_a_stored_value_keeps_what_still_validates():
    """A value from another version. One bad field or one bad entry must not
    cost the rest of it, least of all the dictionary."""
    raw = {"enabled": False, "language": "english", "picky": "yes",
           "words": ["Kubernetes", "two\nlines", 7], "disabled_rules": ["OK_RULE", "no spaces"],
           "variants": "de-AT", "unknown": 1}
    expected = {"enabled": False, "language": "auto", "variants": [], "picky": False,
                "words": ["Kubernetes"], "disabled_rules": ["OK_RULE"]}
    assert checker.parse_stored(raw) == expected
    assert checker.parse_stored(json.dumps(raw)) == expected


@pytest.mark.parametrize("code", ["auto", "en", "de-DE", "ca-ES-valencia", "de-DE-x-simple-language", "ast-ES",
                                  " en-GB "])
def test_language_codes_that_are_accepted(code):
    assert checker.validate_language(code) == code.strip()


@pytest.mark.parametrize("code", ["EN", "english", "de_DE", "de-DE\nen-GB", "d", "", "a" * 41,
                                  "de-DE-x-simple-language-DE", 5, None, ["en"]])
def test_language_codes_that_are_refused(code):
    with pytest.raises(checker.InvalidSetting):
        checker.validate_language(code)


def test_variants_are_codes_deduplicated_in_order_and_never_auto():
    assert checker.validate_variants(["de-AT", "en-GB", "de-AT"]) == ["de-AT", "en-GB"]
    with pytest.raises(checker.InvalidSetting):
        checker.validate_variants(["auto"])
    with pytest.raises(checker.InvalidSetting):
        checker.validate_variants("de-AT")
    with pytest.raises(checker.InvalidSetting, match="12"):
        checker.validate_variants([f"de-X{i:02d}" for i in range(13)])


def test_words_are_stripped_deduplicated_ignoring_case_and_sorted():
    words = checker.validate_words([" zebra ", "Kubernetes", "kubernetes", "apple", "ZEBRA", "Äpfel"])
    assert words == ["apple", "Kubernetes", "zebra", "Äpfel"]


@pytest.mark.parametrize("word", ["", "   ", "a" * 101, "two\nlines", "tab\there", "nul\x00", "line\u2028sep",
                                  "lone\ud800", 7, None])
def test_words_that_are_refused(word):
    with pytest.raises(checker.InvalidSetting):
        checker.validate_word(word)


def test_the_dictionary_has_a_ceiling():
    assert len(checker.validate_words([f"w{i}" for i in range(checker.MAX_WORDS)])) == checker.MAX_WORDS
    with pytest.raises(checker.InvalidSetting, match="10000"):
        checker.validate_words([f"w{i}" for i in range(checker.MAX_WORDS + 1)])


@pytest.mark.parametrize("rule", ["MORFOLOGIK_RULE_EN_US", "a.b:c-d", "X" * 120])
def test_rule_ids_that_are_accepted(rule):
    assert checker.validate_rule(rule) == rule


@pytest.mark.parametrize("rule", ["", "has space", "two,rules", "X" * 121, "semi;colon", 3])
def test_rule_ids_that_are_refused(rule):
    with pytest.raises(checker.InvalidSetting):
        checker.validate_rule(rule)


def test_rules_are_deduplicated_in_order_and_capped():
    assert checker.validate_rules(["B", "A", "B"]) == ["B", "A"]
    with pytest.raises(checker.InvalidSetting):
        checker.validate_rules([f"R{i}" for i in range(checker.MAX_RULES + 1)])


def test_merge_validates_the_whole_and_drops_unknown_keys():
    merged = checker.merge(checker.defaults(), {"picky": True, "available": False, "junk": 1})
    assert merged == {**checker.defaults(), "picky": True}
    with pytest.raises(checker.InvalidSetting):
        checker.merge(checker.defaults(), {"enabled": 1})
    assert issubclass(checker.InvalidSetting, ValueError)


# --- The request --------------------------------------------------------------------------


def test_one_request_carries_the_paragraphs_as_annotated_text(lt):
    text = "First $x$ paragraph \\cite{a}.\n\n% a comment\nSecond paragraph."
    checker.check(text, config())

    assert len(lt.requests) == 1
    request = lt.requests[0]
    assert request.method == "POST" and request.url.path == "/v2/check"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    form = lt.form()
    assert form["language"] == "en-US"
    assert "text" not in form and "disabledRules" not in form and "level" not in form
    assert lt.items() == [
        {"text": "First "},
        {"markup": "$x$", "interpretAs": "Ina1"},
        {"text": " paragraph "},
        {"markup": "\\cite{a}", "interpretAs": "Dummy2"},
        {"text": "."},
        {"text": "\n\n"},
        {"markup": "% a comment\n"},
        {"text": "Second paragraph."},
    ]
    # The paragraphs' sources, joined with the separator.
    assert lt.joined() == text


def test_matches_map_back_to_offsets_in_the_file(lt):
    text = "\\section{Intro}\n\nWe recieve $x$.\n\n\n\nThen Teh end and \\emph{Teh}."
    result = checker.check(text, config())
    assert found(result) == [(at(text, "recieve"), 7, "MORFOLOGIK_RULE_EN_US"),
                             (at(text, "Teh"), 3, "MORFOLOGIK_RULE_EN_US"),
                             (at(text, "Teh", 1), 3, "MORFOLOGIK_RULE_EN_US")]
    assert result["complete"] is True and result["pending"] == []


def test_a_non_bmp_character_before_a_typo_counts_as_two(lt):
    """𝔸 is one code point to Python and two units to Java and JavaScript. The
    match is where the browser's indexOf finds the word."""
    text = "Let 𝔸 be $\\mathbb{A}$.\n\nThe set 𝔸 and 𝔸 has Teh element."
    result = checker.check(text, config())
    [(offset, length, _)] = found(result)
    assert offset == at(text, "Teh") == text.index("Teh") + 3
    assert checker._slice(text, offset, length) == "Teh"


def test_a_lone_surrogate_does_not_break_the_request_or_the_offsets(lt):
    text = "\ud800 Teh"
    result = checker.check(text, config())
    assert lt.joined() == "\ufffd Teh"
    assert found(result) == [(2, 3, "MORFOLOGIK_RULE_EN_US")]


def test_plain_text_is_sent_as_it_is(lt):
    text = "A $x$ is Teh \\emph{word}."
    result = checker.check(text, config(), kind="text")
    assert lt.items() == [{"text": text}]
    assert found(result) == [(at(text, "Teh"), 3, "MORFOLOGIK_RULE_EN_US")]


def test_nothing_to_check_is_answered_without_a_request(lt):
    for text in ("", "  \n\n\t", "\\label{x}\n\n$x = 1$\n\n% comment", "\\begin{document}\n\\end{document}"):
        result = checker.check(text, config(language="auto"))
        assert result == {"language": {"code": "auto", "name": "", "auto": True}, "matches": [],
                          "pending": [], "complete": True}
    assert lt.requests == []


def test_a_match_that_is_not_inside_one_paragraph_is_dropped(lt):
    text = "one\n\ntwo"                # sent as "one\n\ntwo", the separator is at 3 and 4
    lt.handler = lambda r: httpx.Response(200, json=answer([
        lt_match(2, 4),                # runs from "one" through the separator
        lt_match(3, 1),                # starts in the separator
        lt_match(4, 2),                # starts in the separator, ends in "two"
        lt_match(5, 3),                # exactly "two"
        lt_match(90, 2),               # past the end of everything
        lt_match(-1, 2),               # nonsense
        {"offset": "5", "length": 3},  # not numbers
        "not a match",
    ]))
    assert found(checker.check(text, config())) == [(5, 3, "MORFOLOGIK_RULE_EN_US")]


def test_a_match_wholly_inside_markup_is_dropped(lt):
    """A match on a dummy word or on what a command stood for is about the
    reading, not about anything written."""
    text = "A $x$ b \\LaTeX c."
    lt.handler = lambda r: httpx.Response(200, json=answer([
        lt_match(2, 3),                                         # the math alone
        lt_match(text.index("\\LaTeX"), 6),                     # \LaTeX alone
        lt_match(0, 5, rule="EN_A_VS_AN", issue="misspelling", category="MISC"),  # "a" and the math
    ]))
    assert found(checker.check(text, config())) == [(0, 5, "EN_A_VS_AN")]


# --- Mapping the answer --------------------------------------------------------------------


@pytest.mark.parametrize("issue, category, expected", [
    ("misspelling", "TYPOS", "spelling"),
    ("misspelling", "MISC", "grammar"),
    ("style", "BRE_STYLE_OXFORD_SPELLING", "style"),
    ("locale-violation", "TYPOS", "style"),
    ("register", "STYLE", "style"),
    ("grammar", "GRAMMAR", "grammar"),
    ("uncategorized", "CASING", "grammar"),
    ("", "", "grammar"),
])
def test_issue_types(issue, category, expected):
    assert checker.issue_type(issue, category) == expected


def test_a_match_in_the_shape_the_browser_reads(lt):
    lt.handler = lambda r: httpx.Response(200, json=answer([lt_match(
        4, 3, replacements=("The", "", "Ten", "Tea", "Tech", "Ted", "TeX", "Tee"))]))
    [match] = checker.check("The Teh", config())["matches"]
    assert match == {
        "offset": 4, "length": 3,
        "message": "Possible spelling mistake found.", "short": "Spelling mistake",
        "replacements": ["The", "", "Ten", "Tea", "Tech", "Ted"],
        "rule": "MORFOLOGIK_RULE_EN_US", "rule_description": "MORFOLOGIK_RULE_EN_US description",
        "category": "TYPOS", "type": "spelling",
    }


def test_suggestion_markup_is_rendered_as_quotes(lt):
    lt.handler = lambda r: httpx.Response(200, json=answer([lt_match(
        0, 1, rule="EN_A_VS_AN", category="MISC",
        message="Use <suggestion>an</suggestion> instead of 'a'.", short="Use <suggestion>an</suggestion>")]))
    [match] = checker.check("a apple", config())["matches"]
    assert match["message"] == "Use “an” instead of 'a'."
    assert match["short"] == "Use “an”"


def test_matches_are_sorted_by_offset(lt):
    lt.typos = {"zz", "aa"}
    text = "zz aa\n\naa zz"
    offsets = [m["offset"] for m in checker.check(text, config(), focus=len(text))["matches"]]
    assert offsets == sorted(offsets) == [0, 3, 7, 10]


def test_dictionary_words_silence_spelling_matches_ignoring_case(lt):
    text = "Teh kubectl said teh"
    lt.handler = lambda r: httpx.Response(200, json=answer([
        lt_match(0, 3), lt_match(4, 7), lt_match(17, 3),
        lt_match(0, 3, rule="UPPERCASE_X", issue="grammar", category="GRAMMAR"),
    ]))
    result = checker.check(text, config(words=["TEH", "Kubectl"]))
    assert found(result) == [(0, 3, "UPPERCASE_X")]


def test_disabled_rules_are_filtered_and_not_sent(lt):
    """Not sent, because the cache is shared and has to hold the whole answer."""
    lt.handler = lambda r: httpx.Response(200, json=answer([
        lt_match(0, 3, rule="OFF_RULE", issue="grammar", category="GRAMMAR"), lt_match(4, 3)]))
    result = checker.check("abc def", config(disabled_rules=["OFF_RULE", "OTHER"]))
    assert "disabledRules" not in lt.form()
    assert [m["rule"] for m in result["matches"]] == ["MORFOLOGIK_RULE_EN_US"]


def test_preferred_variants_go_only_with_auto(lt):
    checker.check("Hello", config(language="auto", variants=["en-GB", "de-AT"]))
    assert lt.form()["preferredVariants"] == "en-GB,de-AT"
    assert lt.form()["language"] == "auto"
    checker.check("Hello there", config(language="en-US", variants=["en-GB"]))
    assert "preferredVariants" not in lt.form()
    checker.check("Hello again", config(language="auto", variants=["en-GB"]), language="de-DE")
    assert "preferredVariants" not in lt.form() and lt.form()["language"] == "de-DE"


def test_picky_asks_for_the_picky_level(lt):
    checker.check("Hello", config(picky=True))
    assert lt.form()["level"] == "picky"


def test_a_per_check_language_is_validated(lt):
    with pytest.raises(checker.InvalidSetting):
        checker.check("Hello", config(), language="english")
    assert lt.requests == []


def test_the_language_is_reported_and_auto_follows_the_setting(lt):
    lt.code, lt.name = "de-DE", "German (Germany)"
    result = checker.check("Hallo Welt.", config(language="auto"))
    assert result["language"] == {"code": "de-DE", "name": "German (Germany)", "auto": True}
    # The editor then sends the detected code. The setting is still auto.
    result = checker.check("Hallo Welt.", config(language="auto"), language="de-DE")
    assert result["language"] == {"code": "de-DE", "name": "German (Germany)", "auto": True}
    lt.code, lt.name = "en-US", "English (US)"
    result = checker.check("Hello world.", config(language="en-US"))
    assert result["language"] == {"code": "en-US", "name": "English (US)", "auto": False}


def test_a_detected_code_the_settings_would_refuse_is_not_passed_on(lt):
    """The editor sends the detected code back, and it must not get a 422."""
    lt.code = "not a code"
    result = checker.check("Teh word.", config(language="auto"))
    assert result["language"]["code"] == "auto"
    assert found(result) == [(0, 3, "MORFOLOGIK_RULE_EN_US")]
    checker.check("Teh word.", config(language="auto"))
    assert len(lt.requests) == 2


def test_a_fixed_language_keeps_its_code_whatever_the_answer_says(lt):
    lt.code = "en-US"
    first = checker.check("Teh word.", config(language="en"))
    second = checker.check("Teh word.", config(language="en"))
    assert first["language"] == second["language"] == {"code": "en", "name": "English (US)", "auto": False}
    assert len(lt.requests) == 1


def test_a_missing_language_object_falls_back_to_the_request(lt):
    lt.handler = lambda r: httpx.Response(200, json={"matches": []})
    assert checker.check("Hello", config())["language"] == {"code": "en-US", "name": "", "auto": False}


# --- The cache -----------------------------------------------------------------------------


def test_a_paragraph_checked_once_is_not_sent_again(lt):
    text = "First Teh paragraph.\n\nSecond paragraph."
    first = checker.check(text, config())
    second = checker.check(text, config())
    assert len(lt.requests) == 1
    assert first == second

    # An edit sends the paragraph it touched and nothing else, and the cached
    # paragraph's match moves with the text in front of it.
    edited = "Second paragraph, edited.\n\n" + text
    result = checker.check(edited, config())
    assert len(lt.requests) == 2
    assert lt.joined() == "Second paragraph, edited."
    assert found(result) == [(at(edited, "Teh"), 3, "MORFOLOGIK_RULE_EN_US")]


def test_the_cache_is_per_language_and_picky_level(lt):
    text = "Teh paragraph."
    checker.check(text, config())
    checker.check(text, config(language="en-GB"))
    checker.check(text, config(picky=True))
    checker.check(text, config(), kind="text")
    assert len(lt.requests) == 4
    for kwargs in ({"language": "en-GB"}, {"picky": True}, {}):
        checker.check(text, config(**kwargs))
    assert len(lt.requests) == 4


def test_users_share_the_cache_and_keep_their_own_filters(lt):
    text = "Teh word and recieve."
    one = checker.check(text, config(words=["teh"]))
    two = checker.check(text, config(disabled_rules=["MORFOLOGIK_RULE_EN_US"]))
    three = checker.check(text, config())
    assert len(lt.requests) == 1
    assert [checker._slice(text, m["offset"], m["length"]) for m in one["matches"]] == ["recieve"]
    assert two["matches"] == []
    assert len(three["matches"]) == 2


def test_with_auto_nothing_is_looked_up_and_results_are_stored_under_the_detected_code(lt):
    lt.code, lt.name = "en-GB", "English (GB)"
    text = "Teh colour."
    checker.check(text, config(language="auto"))
    checker.check(text, config(language="auto"))
    assert len(lt.requests) == 2
    assert lt.form()["language"] == "auto"
    # The editor sends the detected code next, and it is served from the cache.
    result = checker.check(text, config(language="auto"), language="en-GB")
    assert len(lt.requests) == 2
    assert result["language"] == {"code": "en-GB", "name": "English (GB)", "auto": True}
    assert found(result) == [(0, 3, "MORFOLOGIK_RULE_EN_US")]


def test_the_cache_drops_the_least_recently_used(lt, monkeypatch):
    monkeypatch.setattr(checker, "_results", checker._Results(2))
    for word in ("one", "two", "one", "three"):
        checker.check(f"Paragraph {word}.", config())
    assert len(lt.requests) == 3        # "one" was a hit the second time
    checker.check("Paragraph two.", config())
    assert len(lt.requests) == 4        # "two" was the oldest, and went


def test_paragraphs_without_text_are_not_sent(lt):
    text = "\\begin{figure}\n\\includegraphics{a}\n\\end{figure}\n\nOnly this Teh.\n\n$x = y$"
    result = checker.check(text, config())
    assert lt.joined() == "Only this Teh."
    assert found(result) == [(at(text, "Teh"), 3, "MORFOLOGIK_RULE_EN_US")]


# --- The budget ----------------------------------------------------------------------------


def paper(count: int, size: int = 100) -> tuple[str, list[int]]:
    """``count`` paragraphs of ``size`` characters, each with a typo, and
    where each one starts."""
    paragraphs = [f"P{i:03d} Teh " + "x" * (size - 9) for i in range(count)]
    starts, pos = [], 0
    for p in paragraphs:
        starts.append(pos)
        pos += len(p) + 2
    return "\n\n".join(paragraphs), starts


def test_the_budget_leaves_the_rest_pending(lt):
    text, starts = paper(10)
    result = checker.check(text, config(), budget=350)
    # Three paragraphs fit, from the top without a focus.
    assert lt.joined() == "\n\n".join(text.split("\n\n")[:3])
    assert [m["offset"] for m in result["matches"]] == [s + 5 for s in starts[:3]]
    # The rest is one range, as neighbours are joined.
    assert result["pending"] == [[starts[3], len(text)]]
    assert result["complete"] is False

    # Asking again checks the next three, the first three come from the cache.
    result = checker.check(text, config(), budget=350)
    assert [m["offset"] for m in result["matches"]] == [s + 5 for s in starts[:6]]
    assert result["pending"] == [[starts[6], len(text)]]
    checker.check(text, config(), budget=350)
    result = checker.check(text, config(), budget=350)
    assert result["complete"] is True and result["pending"] == []
    assert len(result["matches"]) == 10
    assert len(lt.requests) == 4


def test_the_paragraphs_nearest_the_focus_go_first(lt):
    text, starts = paper(10)
    focus = starts[6] + 20
    result = checker.check(text, config(), focus=focus, budget=350)
    # Sent in the order they are in the file, so LanguageTool reads them so.
    assert [p[:4] for p in lt.joined().split("\n\n")] == ["P005", "P006", "P007"]
    assert result["pending"] == [[0, starts[5] - 2], [starts[8], len(text)]]


def test_one_paragraph_is_always_sent_however_long(lt):
    text = "Teh " + "word " * 10_000
    result = checker.check(text, config(), budget=100)
    assert len(lt.requests) == 1
    assert result["complete"] is True and found(result) == [(0, 3, "MORFOLOGIK_RULE_EN_US")]


def test_cached_paragraphs_do_not_count_against_the_budget(lt):
    text, _ = paper(4)
    for p in text.split("\n\n"):
        checker.check(p, config())
    lt.requests.clear()
    result = checker.check(text, config(), budget=100)
    assert lt.requests == [] and result["complete"] is True and len(result["matches"]) == 4


# --- Errors ---------------------------------------------------------------------------------


def _raise(exc_type, message="boom"):
    def handler(request):
        raise exc_type(message, request=request)

    return handler


@pytest.mark.parametrize("exc_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError,
                                      httpx.ReadError])
def test_a_checker_that_cannot_be_reached(lt, exc_type):
    lt.handler = _raise(exc_type)
    with pytest.raises(checker.Unreachable) as info:
        checker.check(SECRET, config())
    assert str(info.value) == checker.UNREACHABLE


def test_a_checker_that_does_not_answer_in_time(lt, configure):
    configure(grammar_timeout_seconds=7)
    lt.handler = _raise(httpx.ReadTimeout)
    with pytest.raises(checker.Unreachable, match="7 seconds"):
        checker.check(SECRET, config())


def test_java_languagetool_refusal_is_passed_on_trimmed(lt):
    codes = ", ".join(f"x{i}-XX" for i in range(200))
    lt.handler = lambda r: httpx.Response(
        400, text=f"Error: 'xx' is not a language code known to LanguageTool. Supported language codes are: {codes}.")
    with pytest.raises(checker.Refused) as info:
        checker.check(SECRET, config())
    assert str(info.value).startswith("'xx' is not a language code known to LanguageTool.")
    assert len(str(info.value)) <= checker.MAX_DETAIL_CHARS


@pytest.mark.parametrize("status", [400, 413, 501])
def test_a_json_refusal_is_passed_on(lt, status):
    lt.handler = lambda r: httpx.Response(status, json={"error": {"message": "xx is not a known language."}})
    with pytest.raises(checker.Refused, match="^xx is not a known language.$"):
        checker.check(SECRET, config())


def test_a_refusal_with_nothing_to_say_still_says_something(lt):
    lt.handler = lambda r: httpx.Response(400, text="   ")
    with pytest.raises(checker.Refused, match="HTTP 400"):
        checker.check(SECRET, config())


@pytest.mark.parametrize("response", [
    httpx.Response(500, text="java.lang.NullPointerException"),
    httpx.Response(503, text="overloaded"),
    httpx.Response(302, headers={"location": "https://elsewhere.example/"}),
    httpx.Response(200, text="<html>not the checker</html>"),
    httpx.Response(200, json=[]),
    httpx.Response(200, json={"matches": "none"}),
    httpx.Response(200, json={"language": {"code": "en-US"}}),
])
def test_an_answer_that_cannot_be_read(lt, response):
    lt.handler = lambda r: response
    with pytest.raises(checker.BadAnswer) as info:
        checker.check(SECRET, config())
    assert str(info.value) == checker.BAD_ANSWER
    # And the redirect was not followed.
    assert len(lt.requests) == 1


def test_a_failed_check_caches_nothing(lt):
    lt.handler = lambda r: httpx.Response(500, text="no")
    with pytest.raises(checker.BadAnswer):
        checker.check("Teh word.", config())
    lt.handler = lt.find
    assert found(checker.check("Teh word.", config())) == [(0, 3, "MORFOLOGIK_RULE_EN_US")]
    assert len(lt.requests) == 2


def test_no_error_and_no_log_line_carries_the_text(lt, caplog):
    caplog.set_level(logging.DEBUG, logger="app.grammar.checker")
    failures = [
        _raise(httpx.ConnectError, SECRET), _raise(httpx.ReadTimeout, SECRET),
        lambda r: httpx.Response(500, text=SECRET), lambda r: httpx.Response(200, text=SECRET),
    ]
    for handler in failures:
        lt.handler = handler
        with pytest.raises(checker.GrammarError) as info:
            checker.check(SECRET, config())
        assert SECRET not in str(info.value)
    assert caplog.records, "the failures should have been logged"
    assert SECRET not in caplog.text


def test_requests_neither_follow_redirects_nor_use_a_proxy(lt):
    client = checker._http()
    assert client.follow_redirects is False
    assert client.trust_env is False


# --- Concurrency ------------------------------------------------------------------------------


def test_a_check_waits_for_a_free_slot_then_gives_up(lt, configure, monkeypatch):
    configure(grammar_max_concurrent=1)
    monkeypatch.setattr(checker, "SLOT_WAIT", 0.05)
    slot = checker._slot()
    assert slot.acquire(timeout=1)
    try:
        with pytest.raises(checker.Busy) as info:
            checker.check("Hello", config())
        assert str(info.value) == checker.BUSY
        assert lt.requests == []
    finally:
        slot.release()
    checker.check("Hello", config())
    assert len(lt.requests) == 1


def test_no_more_than_max_concurrent_requests_at_once(lt, configure):
    configure(grammar_max_concurrent=2)
    inside, most, lock = 0, 0, threading.Lock()

    def slow(request):
        nonlocal inside, most
        with lock:
            inside += 1
            most = max(most, inside)
        time.sleep(0.05)
        with lock:
            inside -= 1
        return httpx.Response(200, json=answer())

    lt.handler = slow
    threads = [threading.Thread(target=checker.check, args=(f"Paragraph {i}.", config())) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(lt.requests) == 6 and most == 2


def test_one_check_per_user():
    with checker.one_check("u1"):
        with pytest.raises(checker.AlreadyChecking) as info, checker.one_check("u1"):
            pass
        assert str(info.value) == checker.ALREADY_CHECKING
        with checker.one_check("u2"):
            pass
    with checker.one_check("u1"):
        pass


# --- The privacy guard ----------------------------------------------------------------------


def _resolver(monkeypatch, *addresses, error=None):
    """Make every name resolve to ``addresses`` (or fail with ``error``).
    Returns the list of names looked up."""
    looked_up: list[str] = []

    def fake(host, port, *args, **kwargs):
        looked_up.append(host)
        if error is not None:
            raise error
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
                for a in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return looked_up


def test_a_checker_on_a_public_address_is_refused_before_anything_is_sent(lt, configure, monkeypatch):
    configure(grammar_url="https://api.languagetool.example")
    _resolver(monkeypatch, "93.184.216.34")
    with pytest.raises(checker.PublicHost) as info:
        checker.check(SECRET, config())
    assert str(info.value) == checker.PUBLIC_REFUSAL
    assert "GRAMMAR_ALLOW_PUBLIC_HOSTS" in str(info.value)
    with pytest.raises(checker.PublicHost):
        checker.languages()
    assert lt.requests == []


def test_one_public_address_among_private_ones_is_enough_to_refuse(lt, configure, monkeypatch):
    configure(grammar_url="http://lt.example:8010")
    _resolver(monkeypatch, "10.0.0.5", "2606:2800:220:1::1")
    with pytest.raises(checker.PublicHost):
        checker.check(SECRET, config())
    assert lt.requests == []


@pytest.mark.parametrize("address", ["172.18.0.4", "10.1.2.3", "192.168.1.20", "127.0.0.1", "::1", "fd00::5",
                                     "::ffff:10.0.0.1"])
def test_a_checker_on_this_machine_or_network_is_used(lt, configure, monkeypatch, address):
    configure(grammar_url="http://languagetool:8010")
    _resolver(monkeypatch, address)
    checker.check("Hello", config())
    assert len(lt.requests) == 1


def test_allow_public_hosts_lets_a_public_checker_through(lt, configure, monkeypatch):
    configure(grammar_url="https://lt.example", grammar_allow_public_hosts=True)
    looked_up = _resolver(monkeypatch, "93.184.216.34")
    checker.check("Hello", config())
    assert len(lt.requests) == 1
    assert looked_up == []


def test_a_name_that_does_not_resolve_is_unreachable_not_refused(lt, configure, monkeypatch):
    configure(grammar_url="http://languagetool:8010")
    looked_up = _resolver(monkeypatch, error=socket.gaierror(-2, "Name or service not known"))
    for i in range(3):
        with pytest.raises(checker.Unreachable) as info:
            checker.check(f"Hello {i}", config())
        assert str(info.value) == checker.UNREACHABLE
    # Remembered briefly, because the failing lookup is the slow part...
    assert looked_up == ["languagetool"]
    assert lt.requests == []

    # ...and only briefly. Once that has run out, a checker that has come up
    # in the meantime is found and used.
    base = "http://languagetool:8010"
    expires, verdict = checker._guard_cache[base]
    assert verdict == "unresolved"
    assert 0 < expires - time.monotonic() <= checker.UNRESOLVED_TTL
    checker._guard_cache[base] = (0.0, verdict)
    _resolver(monkeypatch, "172.18.0.4")
    checker.check("Hello", config())
    assert len(lt.requests) == 1


def test_the_verdict_is_cached_so_a_keystroke_costs_no_lookup(lt, configure, monkeypatch):
    configure(grammar_url="http://languagetool:8010")
    looked_up = _resolver(monkeypatch, "172.18.0.4")
    for i in range(3):
        checker.check(f"Hello {i}", config())
    assert looked_up == ["languagetool"]
    assert len(lt.requests) == 3


def test_a_check_served_from_the_cache_needs_no_verdict(lt, configure, monkeypatch):
    configure(grammar_url="http://languagetool:8010")
    looked_up = _resolver(monkeypatch, "172.18.0.4")
    checker.check("Hello", config())
    checker._guard_cache.clear()
    checker.check("Hello", config())
    checker.check("", config())
    assert looked_up == ["languagetool"] and len(lt.requests) == 1


# --- The language list ------------------------------------------------------------------------


JAVA_LANGUAGES = [
    {"name": "German (Germany)", "code": "de", "longCode": "de-DE"},
    {"name": "English (US)", "code": "en", "longCode": "en-US"},
    {"name": "Arabic", "code": "ar", "longCode": "ar"},
    {"name": "Simple German", "code": "de-DE-x-simple-language", "longCode": "de-DE-x-simple-language"},
    {"name": "Simple German", "code": "de-DE-x-simple-language", "longCode": "de-DE-x-simple-language-DE"},
    {"name": "English (US)", "code": "en", "longCode": "en-US"},
    {"name": "nameless"},
    "junk",
]


def test_languages_are_listed_by_name_with_their_long_codes(lt):
    lt.handler = lambda r: httpx.Response(200, json=JAVA_LANGUAGES)
    assert checker.languages() == [
        {"code": "ar", "name": "Arabic"},
        {"code": "en-US", "name": "English (US)"},
        {"code": "de-DE", "name": "German (Germany)"},
        {"code": "de-DE-x-simple-language", "name": "Simple German"},
    ]
    request = lt.requests[-1]
    assert request.method == "GET" and request.url.path == "/v2/languages"


def test_languages_are_cached(lt):
    lt.handler = lambda r: httpx.Response(200, json=JAVA_LANGUAGES)
    first = checker.languages()
    first.append({"code": "xx", "name": "mutated by a caller"})
    assert checker.languages() == first[:-1]
    assert len(lt.requests) == 1


def test_a_language_list_that_is_not_a_list(lt):
    lt.handler = lambda r: httpx.Response(200, json={"languages": []})
    with pytest.raises(checker.BadAnswer):
        checker.languages()
    lt.handler = lambda r: httpx.Response(200, json=JAVA_LANGUAGES[:1])
    assert checker.languages() == [{"code": "de-DE", "name": "German (Germany)"}]


def test_a_language_from_the_list_is_named_in_a_cached_check(lt):
    lt.handler = lambda r: httpx.Response(200, json=JAVA_LANGUAGES)
    checker.languages()
    lt.handler = lambda r: httpx.Response(200, json={"matches": []})
    assert checker.check("Hallo.", config(language="de-DE"))["language"]["name"] == "German (Germany)"
