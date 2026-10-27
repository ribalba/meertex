"""Spelling and grammar checks while writing, done by a checker of this server's own.

The editor sends the open file here a moment after every pause in typing. This
module cuts it into paragraphs (app/grammar/latex.py), sends the ones it has not
seen before to a LanguageTool server (or anything else speaking its v2 HTTP API),
and turns the answer into the underlines the browser draws. It is meerail's
checker (app/grammar.py there), and four things shape it more than the feature
itself does:

  * **The text never leaves the server unless the operator says it may.** An
    unpublished paper is private, and the checker is a container on the compose
    network. Before any request goes out the configured host is resolved and
    refused if any address it has is public (see ``_guard``).
    GRAMMAR_ALLOW_PUBLIC_HOSTS is the only way past that. Nothing here logs the
    text or puts it into an error message, and nothing is sent at all while
    GRAMMAR_URL is empty.
  * **Offsets are UTF-16 code units.** LanguageTool is Java, and a Java string is
    UTF-16. So is a JavaScript one, and so is every position CodeMirror counts.
    Python counts code points instead, so a 𝔸 ahead of a typo would shift every
    underline after it by one if this module counted the Python way. All offset
    arithmetic below is done in UTF-16 units for that reason.
  * **A file is long and a keystroke changes one paragraph.** Results are cached
    per paragraph, keyed by its source, so a check sends only the paragraphs
    that changed. The rest come from the cache, shared by everybody checking the
    same language.
  * **One server, many writers.** Each check has a budget of paragraph text it
    may send, nearest the cursor first. What is left over is answered as
    ``pending``, and the editor asks again. At most GRAMMAR_MAX_CONCURRENT
    requests are with LanguageTool at once, and one check per user at a time.

Kept free of FastAPI, so the whole of it can be driven by the unit tests through
an httpx.MockTransport (see ``use_transport``). The router, app/routers/grammar.py,
does the database and turns the exceptions below into status codes.
"""

import hashlib
import ipaddress
import json
import logging
import re
import socket
import threading
import time
import unicodedata
from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import get_settings
from . import latex

log = logging.getLogger(__name__)

# Two paragraphs in one request are joined with a blank line, so each reads
# as a paragraph of its own. A match that starts in it, or runs through it,
# belongs to no paragraph and is dropped (see _locate).
SEPARATOR = "\n\n"
_SEPARATOR_UNITS = 2

# The largest file a check takes, in characters. The editor's text files are
# at most MAX_TEXT_BYTES (2 MiB), and this is the same order.
MAX_TEXT_CHARS = 2_000_000

# How much paragraph source one check may send, nearest the cursor first. About
# five pages of a paper, which LanguageTool checks in a second or two. A file
# opened for the first time is checked over a few requests, and a keystroke
# later only sends its own paragraph.
BUDGET_CHARS = 20_000

# Paragraph results kept in memory. A paper has a few hundred paragraphs, so
# this holds the papers of everybody writing at the moment, in two languages.
CACHE_SIZE = 20_000

# How long a check waits for one of the GRAMMAR_MAX_CONCURRENT slots before it
# answers 503. The editor asks again on its own a moment later.
SLOT_WAIT = 10.0

# Limits on the stored settings. The dictionary is the one that grows. A word is
# added every time somebody presses "Add to dictionary", so it needs a ceiling,
# and ten thousand is more names and jargon than anybody collects by hand.
MAX_LANGUAGE_CHARS = 40
MAX_VARIANTS = 12
MAX_WORDS = 10_000
MAX_WORD_CHARS = 100
MAX_RULES = 1000
MAX_RULE_CHARS = 120

# LanguageTool offers up to dozens of replacements for a misspelling, ranked.
# Past the first few they are guesses nobody picks.
MAX_REPLACEMENTS = 6

# How much of the checker's own error message is passed on. Java LanguageTool
# answers an unknown language code with the list of every code it knows, which
# is several hundred characters after the one sentence that matters.
MAX_DETAIL_CHARS = 300

# The checker is on the same compose network, so three seconds to connect is
# already generous. The read side is GRAMMAR_TIMEOUT_SECONDS, which has to allow
# for LanguageTool loading a language the first time it is asked for one.
CONNECT_TIMEOUT = 3.0

# How long a privacy verdict on GRAMMAR_URL is trusted. Checks run on every
# pause in typing, and a DNS lookup each time would cost without helping. Five
# minutes still notices a name that is re-pointed.
GUARD_TTL = 300.0

# How long "that name does not resolve" is remembered. Much shorter, because it
# is not a verdict but a state. The checker's container is stopped or still
# starting, and it should be found soon after it is back. It is remembered at
# all because the failing lookup is slow (about four seconds for a missing
# service name on a compose network).
UNRESOLVED_TTL = 10.0

# The checker's language list changes when the checker is upgraded, so it is
# asked once an hour at most.
LANGUAGES_TTL = 3600.0

NOT_CONFIGURED = ("Spelling and grammar checking is not set up on this server. "
                  "Set GRAMMAR_URL to a LanguageTool server.")
BAD_URL = ("GRAMMAR_URL is not an http:// or https:// address with a host name in it, "
           "so there is nothing to send a check to.")
UNREACHABLE = "The grammar checker is not answering. Is the languagetool container running?"
BUSY = "The grammar checker is busy with other checks. It will be asked again in a moment."
ALREADY_CHECKING = "A check of yours is still running. It will be asked again in a moment."
BAD_ANSWER = "The grammar checker sent an answer meertex could not read."
PUBLIC_REFUSAL = ("GRAMMAR_URL points at a public address, so your text would leave this "
                  "server. Point it at a checker on your own network, or set "
                  "GRAMMAR_ALLOW_PUBLIC_HOSTS=true if that is intended.")


# --- Errors ---------------------------------------------------------------------


class GrammarError(Exception):
    """The check could not be done. Each subclass carries a sentence meant for
    the person typing, and none of them ever contains any of the text."""


class NotConfigured(GrammarError):
    """GRAMMAR_URL is empty, or is not a URL a request could be sent to."""


class PublicHost(GrammarError):
    """The privacy guard. GRAMMAR_URL resolves to a public address and the
    server has not been told that is allowed. Nothing was sent."""


class Unreachable(GrammarError):
    """The checker could not be reached, or did not answer in time. Also a host
    that does not resolve. In a compose stack that is what a service which is
    not running looks like, and it is not a verdict about privacy."""


class Busy(GrammarError):
    """Every slot for a request to the checker stayed taken for SLOT_WAIT."""


class AlreadyChecking(GrammarError):
    """This user has a check running already (another tab, or a request the
    editor gave up on that is still with the checker)."""


class Refused(GrammarError):
    """The checker answered 4xx (or 501, which LingoTweaker uses for a language
    its build lacks). The message is the checker's own, trimmed."""


class BadAnswer(GrammarError):
    """The checker answered with a server error, or with something that is not
    the LanguageTool v2 shape."""


class InvalidSetting(ValueError):
    """A settings value that cannot be stored, with a sentence saying why."""


# --- The settings on the user row ---------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "language": "auto",
    "variants": [],
    "picky": False,
    "words": [],
    "disabled_rules": [],
}

# BCP 47 as far as LanguageTool uses it, a two- or three-letter language and
# up to four subtags. That covers de-DE, ca-ES-valencia and the private-use
# de-DE-x-simple-language. Matched with fullmatch, not ^...$, because `$` also
# matches before a trailing newline.
_LANGUAGE = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]{1,8}){0,4}")

# LanguageTool rule ids are upper-case words joined by underscores
# (MORFOLOGIK_RULE_EN_US). Other checkers add dots, colons and dashes. Nothing
# else is needed, and no comma, so a list of them can be joined safely.
_RULE = re.compile(rf"[A-Za-z0-9_.:\-]{{1,{MAX_RULE_CHARS}}}")

# Unicode categories a dictionary word may not contain: control characters
# (newlines and tabs among them), lone surrogates (which Postgres cannot store),
# and the line and paragraph separators.
_FORBIDDEN_IN_WORDS = frozenset({"Cc", "Cs", "Zl", "Zp"})


def defaults() -> dict[str, Any]:
    """A fresh copy of the defaults, safe to mutate."""
    return {key: list(value) if isinstance(value, list) else value for key, value in DEFAULTS.items()}


def _shown(value: Any) -> str:
    """A value quoted for an error message, cut short. The message is shown in a
    settings field, and echoing a megabyte back would help nobody."""
    text = repr(value)
    return text if len(text) <= 50 else text[:47] + "..."


def _flag(name: str) -> Callable[[Any], bool]:
    def check(value: Any) -> bool:
        # isinstance(True, int) holds, but not the other way round: 1 is not
        # accepted as a boolean, so a client that means something else by it
        # is told rather than guessed at.
        if not isinstance(value, bool):
            raise InvalidSetting(f"{name} must be true or false.")
        return value

    return check


def validate_language(value: Any, *, allow_auto: bool = True) -> str:
    """A language code LanguageTool could be asked for, or "auto"."""
    if not isinstance(value, str):
        raise InvalidSetting("The language must be a code such as en-US or de-DE, or auto.")
    code = value.strip()
    if allow_auto and code == "auto":
        return code
    if len(code) > MAX_LANGUAGE_CHARS or not _LANGUAGE.fullmatch(code):
        tail = ", or auto." if allow_auto else "."
        raise InvalidSetting(f"{_shown(value)} is not a language code. Use a code such as en-US or de-DE{tail}")
    return code


def validate_word(value: Any) -> str:
    """One dictionary entry, stripped."""
    if not isinstance(value, str):
        raise InvalidSetting("A dictionary word must be text.")
    word = value.strip()
    if not word or len(word) > MAX_WORD_CHARS:
        raise InvalidSetting(f"A dictionary word must be between 1 and {MAX_WORD_CHARS} characters long.")
    if any(unicodedata.category(ch) in _FORBIDDEN_IN_WORDS for ch in word):
        raise InvalidSetting("A dictionary word cannot contain line breaks or control characters.")
    return word


def validate_rule(value: Any) -> str:
    """One rule id to switch off."""
    if not isinstance(value, str) or not _RULE.fullmatch(value.strip()):
        raise InvalidSetting(f"{_shown(value)} is not a rule id. Rule ids are letters, digits and "
                             f"_ . : - only, at most {MAX_RULE_CHARS} characters.")
    return value.strip()


def _each(value: Any, check: Callable[[Any], str], what: str, lenient: bool) -> list[str]:
    """Every entry of a list through `check`. Lenient (reading a stored value)
    skips an entry that no longer passes instead of refusing the list. Rules can
    tighten between versions, and one entry that stopped validating must not
    cost somebody the other nine thousand words in their dictionary."""
    if not isinstance(value, list):
        if lenient:
            return []
        raise InvalidSetting(f"{what} must be a list.")
    out = []
    for item in value:
        try:
            out.append(check(item))
        except InvalidSetting:
            if not lenient:
                raise
    return out


def validate_variants(value: Any, *, lenient: bool = False) -> list[str]:
    """Preferred variants for auto-detection (en-GB rather than en-US, de-AT
    rather than de-DE), deduplicated in the order given."""
    codes = _each(value, lambda v: validate_language(v, allow_auto=False), "variants", lenient)
    out = list(dict.fromkeys(codes))
    if len(out) > MAX_VARIANTS:
        if lenient:
            return out[:MAX_VARIANTS]
        raise InvalidSetting(f"At most {MAX_VARIANTS} preferred variants can be set.")
    return out


def validate_words(value: Any, *, lenient: bool = False) -> list[str]:
    """The personal dictionary, deduplicated ignoring case, keeping the first
    spelling seen, and stored sorted so the settings list reads alphabetically.

    Case-insensitive because that is how it is applied (see check). A word added
    as "Kubernetes" also silences "kubernetes", and two entries differing only
    in case would be one entry that the list shows twice.
    """
    seen: dict[str, str] = {}
    for word in _each(value, validate_word, "words", lenient):
        seen.setdefault(word.casefold(), word)
    if len(seen) > MAX_WORDS and not lenient:
        raise InvalidSetting(f"The dictionary holds at most {MAX_WORDS} words.")
    out = sorted(seen.values(), key=lambda w: (w.casefold(), w))
    return out[:MAX_WORDS]


def validate_rules(value: Any, *, lenient: bool = False) -> list[str]:
    """Rule ids switched off with "Turn off this rule", deduplicated in the order
    they were added."""
    out = list(dict.fromkeys(_each(value, validate_rule, "disabled_rules", lenient)))
    if len(out) > MAX_RULES:
        if lenient:
            return out[:MAX_RULES]
        raise InvalidSetting(f"At most {MAX_RULES} rules can be switched off.")
    return out


_VALIDATORS: dict[str, Callable[..., Any]] = {
    "enabled": _flag("enabled"),
    "language": validate_language,
    "variants": validate_variants,
    "picky": _flag("picky"),
    "words": validate_words,
    "disabled_rules": validate_rules,
}
_LISTS = ("variants", "words", "disabled_rules")


def validate(config: dict[str, Any]) -> dict[str, Any]:
    """A complete settings value, checked and normalised. Missing keys take the
    default, unknown keys are dropped. Raises InvalidSetting."""
    base = defaults()
    return {key: check(config.get(key, base[key])) for key, check in _VALIDATORS.items()}


def merge(current: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """`current` with the keys in `changes` replaced, validated as a whole."""
    return validate({**current, **{k: v for k, v in changes.items() if k in DEFAULTS}})


def parse_stored(raw: Any) -> dict[str, Any]:
    """The stored value (users.grammar) as settings, never raising.

    A value that is absent or not an object reads as the defaults. Inside one,
    each field falls back on its own, and the lists keep whichever entries still
    pass. The value is only ever written by this module, so anything odd in it is
    from another version, and the right response is to keep as much as can be
    kept. JSON text is read too, for a value that was stored as a string.
    """
    out = defaults()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return out
    if not isinstance(raw, dict):
        return out
    for key, check in _VALIDATORS.items():
        if key not in raw:
            continue
        try:
            out[key] = check(raw[key], lenient=True) if key in _LISTS else check(raw[key])
        except InvalidSetting:
            pass
    return out


# --- UTF-16 offsets -------------------------------------------------------------------


def _units(text: str) -> int:
    """Length in UTF-16 code units, which is what Java and JavaScript count."""
    if text.isascii():
        return len(text)
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def clean_text(text: str) -> str:
    """The text with any unpaired surrogate replaced by U+FFFD.

    JSON can carry a lone surrogate ("\\ud800") and Python will hold one in a
    str, but it cannot be encoded to UTF-8, so it would fail the request to the
    checker. U+FFFD is one UTF-16 unit, exactly as the lone surrogate is in the
    browser's copy of the text, so every offset after it still lines up.
    """
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def _slice(text: str, offset: int, length: int) -> str:
    """The text a match covers, cut by UTF-16 units."""
    data = text.encode("utf-16-le", "surrogatepass")
    return data[offset * 2 : (offset + length) * 2].decode("utf-16-le", "replace")


def _locate(spans: list[tuple[int, int]], starts: list[int], offset: int, length: int) -> tuple[int, int] | None:
    """(paragraph index, offset within that paragraph) for a match reported
    against the joined request, or None for one that starts in a separator or runs
    on past the end of its paragraph. Neither can be drawn in the text the author wrote."""
    i = bisect_right(starts, offset) - 1
    if i < 0:
        return None
    start, end = spans[i]
    if offset + length > end:
        return None
    return i, offset - start


def _text_spans(items: list[latex.Item]) -> list[tuple[int, int]]:
    """Where a paragraph's text items are, in UTF-16 units from its start."""
    out, pos = [], 0
    for src, interp in items:
        units = _units(src)
        if interp is None:
            out.append((pos, pos + units))
        pos += units
    return out


def _touches_text(spans: list[tuple[int, int]], offset: int, length: int) -> bool:
    """Whether a match covers any of the author's text. One that lies wholly in
    markup is about what the markup was read as (a dummy word, a composed
    letter), not about anything written, and is dropped."""
    end = offset + length
    return any(a < end and offset < b if length else a <= offset <= b for a, b in spans)


# --- Matches ------------------------------------------------------------------------

_STYLE_ISSUES = frozenset({"style", "locale-violation", "register"})


def issue_type(issue: str, category: str) -> str:
    """spelling, grammar or style, the colour of the underline.

    LanguageTool's issueType alone does not say it. "misspelling" is also what
    the a/an rule (EN_A_VS_AN, category MISC) reports, which is grammar to
    anybody reading the underline. Only the dictionary rules sit in category
    TYPOS as well. Everything that is neither a dictionary miss nor a matter of
    taste is grammar.
    """
    if issue == "misspelling" and category == "TYPOS":
        return "spelling"
    if issue in _STYLE_ISSUES:
        return "style"
    return "grammar"


# Java LanguageTool renders <suggestion>x</suggestion> in a message as “x”
# before sending it. LingoTweaker leaves the markup raw, and the browser would
# show the tags.
_SUGGESTION = re.compile(r"<suggestion>(.*?)</suggestion>", re.DOTALL)


def render_message(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return _SUGGESTION.sub(lambda m: "\u201c" + m.group(1) + "\u201d", text)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _normalize(raw: Any) -> dict[str, Any] | None:
    """One checker match in the shape the browser reads, or None for one that
    cannot be placed. Offset and length are still against the joined request."""
    if not isinstance(raw, dict):
        return None
    offset, length = raw.get("offset"), raw.get("length")
    if not (_is_count(offset) and _is_count(length)):
        return None
    rule = raw.get("rule") if isinstance(raw.get("rule"), dict) else {}
    category = rule.get("category") if isinstance(rule.get("category"), dict) else {}
    replacements = []
    for item in raw.get("replacements") or []:
        # An empty value is a real suggestion. It means "delete this" (a
        # doubled word, a stray space), so it is kept.
        if isinstance(item, dict) and isinstance(item.get("value"), str):
            replacements.append(item["value"])
            if len(replacements) == MAX_REPLACEMENTS:
                break
    category_id = str(category.get("id") or "")
    return {
        "offset": offset,
        "length": length,
        "message": render_message(raw.get("message")),
        "short": render_message(raw.get("shortMessage")),
        "replacements": replacements,
        "rule": str(rule.get("id") or ""),
        "rule_description": str(rule.get("description") or ""),
        "category": category_id,
        "type": issue_type(str(rule.get("issueType") or ""), category_id),
    }


# --- Talking to the checker -----------------------------------------------------------

# One client for the life of the process. httpx.Client is thread-safe, and the
# routes calling this run in FastAPI's threadpool. Keeping it keeps the
# connection to the checker open between checks. Per-request timeouts are
# passed on each call, so the client itself holds no setting.
_client: httpx.Client | None = None
_transport: httpx.BaseTransport | None = None
_client_lock = threading.Lock()

# url -> (expires at, "public" | "private" | "unresolved"). See _guard.
_guard_cache: dict[str, tuple[float, str]] = {}
# url -> (expires at, languages). See languages().
_languages_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
# Language code -> its name, learned from every answer. A check served wholly
# from the cache asks the checker nothing, and still names its language.
_language_names: dict[str, str] = {}

# The slots for requests to the checker, sized by GRAMMAR_MAX_CONCURRENT. Made
# again when that changes, which only the tests do.
_slots: tuple[int, threading.BoundedSemaphore] | None = None
_slots_lock = threading.Lock()

# The users with a check running. See one_check.
_running: set[str] = set()
_running_lock = threading.Lock()


class _Results:
    """The paragraph results, least recently used dropped first.

    Keyed by (kind, sha256 of the paragraph's source, language code, picky).
    The value is the normalised matches with offsets relative to the paragraph,
    before the personal dictionary and the switched-off rules are applied, so
    everybody checking the same language shares an entry.
    """

    def __init__(self, size: int) -> None:
        self.size = size
        self.data: OrderedDict[tuple, tuple[dict[str, Any], ...]] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key: tuple) -> list[dict[str, Any]] | None:
        with self.lock:
            hit = self.data.get(key)
            if hit is None:
                return None
            self.data.move_to_end(key)
        return [dict(m) for m in hit]

    def put(self, key: tuple, matches: list[dict[str, Any]]) -> None:
        value = tuple(dict(m) for m in matches)
        with self.lock:
            self.data[key] = value
            self.data.move_to_end(key)
            while len(self.data) > self.size:
                self.data.popitem(last=False)

    def clear(self) -> None:
        with self.lock:
            self.data.clear()


_results = _Results(CACHE_SIZE)


def use_transport(transport: httpx.BaseTransport | None) -> None:
    """Send every request through `transport` from now on (None for the network),
    and forget everything cached. The seam the unit tests use to stand an
    httpx.MockTransport in for the checker."""
    global _client, _transport
    with _client_lock:
        old, _client, _transport = _client, None, transport
    if old is not None:
        old.close()
    _guard_cache.clear()
    _languages_cache.clear()
    _language_names.clear()
    _results.clear()


def _http() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            # No redirects. A checker that answers with a 3xx is not a checker,
            # and following it would carry the text to wherever it points, past
            # the guard that only looked at the configured host.
            #
            # And no proxy from the environment (trust_env=False). httpx would
            # otherwise honour HTTP_PROXY and HTTPS_PROXY, and on a host that
            # sets them for its outbound traffic every paragraph would travel
            # through that proxy, which is the leaving-the-server the guard is
            # there to prevent, and one it cannot see.
            _client = httpx.Client(transport=_transport, follow_redirects=False, trust_env=False)
        return _client


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(float(get_settings().grammar_timeout_seconds), connect=CONNECT_TIMEOUT)


def _slot() -> threading.BoundedSemaphore:
    global _slots
    size = max(1, int(get_settings().grammar_max_concurrent))
    with _slots_lock:
        if _slots is None or _slots[0] != size:
            _slots = (size, threading.BoundedSemaphore(size))
        return _slots[1]


@contextmanager
def one_check(user_id: str) -> Iterator[None]:
    """Hold this user's one check. Raises AlreadyChecking when they have one
    running. The editor sends one at a time, so a second is another tab, or a
    request the editor dropped that the server is still working on, and it
    should not take a second slot with the checker."""
    with _running_lock:
        if user_id in _running:
            raise AlreadyChecking(ALREADY_CHECKING)
        _running.add(user_id)
    try:
        yield
    finally:
        with _running_lock:
            _running.discard(user_id)


def available() -> bool:
    """Whether this server has a checker configured at all."""
    return bool(get_settings().grammar_url.strip())


def base_url() -> str:
    """The configured checker's base URL, without a trailing slash or /v2."""
    raw = get_settings().grammar_url.strip()
    if not raw:
        raise NotConfigured(NOT_CONFIGURED)
    parts = urlsplit(raw)
    try:
        # Read only for its ValueError. urlsplit accepts "host:abc" and leaves
        # the complaint to whoever asks for the port.
        _ = parts.port
    except ValueError as exc:
        raise NotConfigured(BAD_URL) from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise NotConfigured(BAD_URL)
    base = raw.rstrip("/")
    # Most LanguageTool clients are configured with the API root, .../v2, and a
    # URL copied from one of them should work here as well.
    base = base.removesuffix("/v2")
    return base


def _public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Is this an ordinary address on the public internet? (meerail's
    nethost.reachable.) IPv4 written as IPv6 is unwrapped first, or
    ::ffff:10.0.0.1 would not count as the private address it is."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified)


def _resolves_publicly(base: str) -> bool:
    host = urlsplit(base).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as exc:
        # Not a refusal. In a compose stack a name that does not resolve is a
        # service that is not running, and the sentence that helps is the one
        # about the container. Remembered only briefly, see UNRESOLVED_TTL.
        log.info("grammar: %s does not resolve (%s)", host, type(exc).__name__)
        raise Unreachable(UNREACHABLE) from exc
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _public(ip):
            return True
    return False


def _guard(base: str) -> None:
    """Refuse to send text to a checker on a public address.

    GRAMMAR_URL comes from the operator, and a private destination is exactly
    what it is meant to be. What must not happen silently is the opposite,
    papers going out to a hosted service because a URL was copied from a
    tutorial. So every address the host resolves to is checked, and one public
    address among them is enough to refuse.

    The verdict is cached per URL for GUARD_TTL. A name that does not resolve
    is reported as unreachable and remembered for UNRESOLVED_TTL. The checked
    address is not pinned, because the threat is a mistake in the operator's own
    configuration, not somebody racing DNS answers.
    """
    if get_settings().grammar_allow_public_hosts:
        return
    now = time.monotonic()
    hit = _guard_cache.get(base)
    if hit is None or hit[0] <= now:
        try:
            verdict = "public" if _resolves_publicly(base) else "private"
        except Unreachable:
            _guard_cache[base] = (now + UNRESOLVED_TTL, "unresolved")
            raise
        hit = (now + GUARD_TTL, verdict)
        _guard_cache[base] = hit
    if hit[1] == "unresolved":
        raise Unreachable(UNREACHABLE)
    if hit[1] == "public":
        raise PublicHost(PUBLIC_REFUSAL)


def _backend_detail(res: httpx.Response) -> str:
    """The checker's own words for a refusal.

    Java LanguageTool answers errors as plain text ("Error: 'xx' is not a
    language code known to LanguageTool. Supported language codes are: ...").
    LingoTweaker answers JSON, {"error": {"message": "..."}}. Either is reduced
    to one line and cut to MAX_DETAIL_CHARS.
    """
    text = ""
    try:
        body = res.json()
    except ValueError:
        body = None
        text = res.text
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            err = err.get("message")
        if not isinstance(err, str):
            err = body.get("message")
        text = err if isinstance(err, str) else ""
    elif isinstance(body, str):
        text = body
    text = " ".join(text.split())
    text = text.removeprefix("Error: ")
    if not text:
        return f"The grammar checker refused the check (HTTP {res.status_code})."
    if len(text) > MAX_DETAIL_CHARS:
        text = text[: MAX_DETAIL_CHARS - 3].rstrip() + "..."
    return text


def _request(method: str, url: str, **kwargs: Any) -> Any:
    """One call to the checker, answered with its decoded JSON body.

    Waits for one of the GRAMMAR_MAX_CONCURRENT slots first. Logs say what
    failed and never with what, so no text and no response body. The request carries
    a paper and a refusal could quote parts of it back.
    """
    slot = _slot()
    if not slot.acquire(timeout=SLOT_WAIT):
        log.info("grammar: no free slot for a request to the checker")
        raise Busy(BUSY)
    try:
        res = _http().request(method, url, timeout=_timeout(), **kwargs)
    except (httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        log.info("grammar: could not connect to the checker (%s)", type(exc).__name__)
        raise Unreachable(UNREACHABLE) from exc
    except httpx.TimeoutException as exc:
        # Connected, then nothing for the whole read timeout. Usually a
        # LanguageTool still loading a language, which clears by itself.
        log.info("grammar: the checker did not answer in time (%s)", type(exc).__name__)
        raise Unreachable(
            f"The grammar checker did not answer within {get_settings().grammar_timeout_seconds} "
            f"seconds. It may still be starting up. Try again in a moment.") from exc
    except httpx.DecodingError as exc:
        log.warning("grammar: the checker's answer could not be decoded")
        raise BadAnswer(BAD_ANSWER) from exc
    except httpx.InvalidURL as exc:
        raise NotConfigured(BAD_URL) from exc
    except httpx.HTTPError as exc:
        log.info("grammar: could not reach the checker (%s)", type(exc).__name__)
        raise Unreachable(UNREACHABLE) from exc
    finally:
        slot.release()

    status = res.status_code
    if 400 <= status < 500 or status == 501:
        log.info("grammar: the checker refused a request with HTTP %d", status)
        raise Refused(_backend_detail(res))
    if not 200 <= status < 300:
        log.warning("grammar: the checker answered HTTP %d", status)
        raise BadAnswer(BAD_ANSWER)
    try:
        return res.json()
    except ValueError as exc:
        log.warning("grammar: the checker answered with something that is not JSON")
        raise BadAnswer(BAD_ANSWER) from exc


# --- The check --------------------------------------------------------------------------


class _Paragraph:
    """One paragraph of the file, where it is in UTF-16 units and its source."""

    __slots__ = ("end", "items", "key", "source", "start")

    def __init__(self, start: int, end: int, source: str, key: str) -> None:
        self.start, self.end, self.source, self.key = start, end, source, key
        self.items: list[latex.Item] = []


def _split(text: str, kind: str) -> list[_Paragraph]:
    out: list[_Paragraph] = []
    pos, units = 0, 0
    for a, b in latex.paragraphs(text, kind):
        units += _units(text[pos:a])
        start = units
        source = text[a:b]
        units += _units(source)
        out.append(_Paragraph(start, units, source, hashlib.sha256(source.encode("utf-8")).hexdigest()))
        pos = b
    return out


def _by_distance(paragraphs: list[_Paragraph], focus: int | None) -> list[int]:
    """The paragraphs' indices, the one around ``focus`` first and then outwards.
    Without a focus, from the top."""
    if focus is None:
        return list(range(len(paragraphs)))

    def distance(i: int) -> tuple[int, int]:
        p = paragraphs[i]
        if p.start <= focus <= p.end:
            return 0, i
        return min(abs(focus - p.start), abs(focus - p.end)), i

    return sorted(range(len(paragraphs)), key=distance)


def _language(code: str, auto: bool) -> dict[str, Any]:
    return {"code": code, "name": _language_names.get(code, ""), "auto": auto}


def check(text: str, config: dict[str, Any], *, kind: str = "latex", language: str | None = None,
          focus: int | None = None, budget: int | None = None) -> dict[str, Any]:
    """Check a whole file. `config` is a settings value (parse_stored or
    validate), `language` overrides its language for this one check, and
    `focus` is a UTF-16 offset (the cursor) whose paragraph goes first.

    Returns {"language": {code, name, auto}, "matches": [...], "pending":
    [[start, end], ...], "complete": bool}, every offset in UTF-16 units into
    `text` and the matches sorted by offset. `auto` says whether the user's
    setting is auto, so it stays true while the editor sends back the code
    detected earlier. `pending` are the paragraphs the budget (BUDGET_CHARS
    unless given) left out.
    """
    budget = BUDGET_CHARS if budget is None else budget
    base_url()  # NotConfigured before anything else
    requested = validate_language(config.get("language", "auto") if language is None else language)
    auto = config.get("language", "auto") == "auto"
    picky = bool(config.get("picky"))
    text = clean_text(text)
    paragraphs = _split(text, kind)

    found: dict[int, list[dict[str, Any]]] = {}
    sending: list[int] = []
    left: list[int] = []
    used = 0
    for i in _by_distance(paragraphs, focus):
        p = paragraphs[i]
        # With auto the language is not known yet, so nothing can be looked up.
        hit = None if requested == "auto" else _results.get((kind, p.key, requested, picky))
        if hit is not None:
            found[i] = hit
            continue
        if left or (sending and used + len(p.source) > budget):
            left.append(i)
            continue
        p.items = latex.annotate(p.source, kind)
        if not latex.has_text(p.items):
            found[i] = []
            continue
        sending.append(i)
        used += len(p.source)

    code = requested
    if sending:
        # In the order they are in the file, which is how LanguageTool should
        # read them, whatever order the budget picked them in.
        sending.sort()
        code = _send([paragraphs[i] for i in sending], sending, found, config, kind, requested, picky)

    matches = _filtered(paragraphs, found, config)
    pending = _ranges(paragraphs, sorted(left))
    return {"language": _language(code, auto), "matches": matches, "pending": pending, "complete": not pending}


def _send(batch: list[_Paragraph], indices: list[int], found: dict[int, list[dict[str, Any]]],
          config: dict[str, Any], kind: str, requested: str, picky: bool) -> str:
    """Check the paragraphs of `batch` in one request, cache what comes back and
    put it into `found`. Returns the language code that was checked."""
    base = base_url()
    _guard(base)

    annotation: list[dict[str, str]] = []
    spans: list[tuple[int, int]] = []
    pos = 0
    for n, p in enumerate(batch):
        if n:
            annotation.append({"text": SEPARATOR})
            pos += _SEPARATOR_UNITS
        annotation.extend(latex.annotation(p.items))
        end = pos + _units(p.source)
        spans.append((pos, end))
        pos = end

    form = {"data": json.dumps({"annotation": annotation}, ensure_ascii=False), "language": requested}
    variants = config.get("variants") or []
    # Only with auto. Both LanguageTool and LingoTweaker answer 400 to
    # preferredVariants next to a fixed language.
    if requested == "auto" and variants:
        form["preferredVariants"] = ",".join(variants)
    if picky:
        form["level"] = "picky"
    # The switched-off rules are not sent as disabledRules, unlike in meerail.
    # The cache is shared between users, so what is stored has to be the whole
    # answer, and each user's rules are taken out afterwards (_filtered).

    body = _request("POST", f"{base}/v2/check", data=form)
    if not isinstance(body, dict) or not isinstance(body.get("matches"), list):
        log.warning("grammar: the checker's answer has no match list")
        raise BadAnswer(BAD_ANSWER)

    # With auto, LanguageTool reports the language it detected. The editor
    # sends that code back on its next check, so it has to be one this module
    # accepts. With a fixed language the code stays the one asked for, as it
    # is for a check served from the cache.
    answered = body.get("language") if isinstance(body.get("language"), dict) else {}
    code = requested
    if requested == "auto":
        try:
            code = validate_language(answered.get("code"), allow_auto=False)
        except InvalidSetting:
            code = "auto"
    name = answered.get("name")
    if isinstance(name, str) and name and code != "auto":
        _language_names.setdefault(code, name)

    per: list[list[dict[str, Any]]] = [[] for _ in batch]
    starts = [start for start, _ in spans]
    text_spans = [_text_spans(p.items) for p in batch]
    for raw in body["matches"]:
        match = _normalize(raw)
        if match is None:
            continue
        where = _locate(spans, starts, match["offset"], match["length"])
        if where is None:
            continue
        n, offset = where
        if not _touches_text(text_spans[n], offset, match["length"]):
            continue
        match["offset"] = offset
        per[n].append(match)

    # Stored under the code the next check will look up.
    for n, p in enumerate(batch):
        if code != "auto":
            _results.put((kind, p.key, code, picky), per[n])
        found[indices[n]] = per[n]
    return code


def _filtered(paragraphs: list[_Paragraph], found: dict[int, list[dict[str, Any]]],
              config: dict[str, Any]) -> list[dict[str, Any]]:
    """Every match at its offset in the file, without the user's switched-off
    rules and dictionary words."""
    disabled = set(config.get("disabled_rules") or [])
    words = {w.casefold() for w in config.get("words") or []}
    out: list[dict[str, Any]] = []
    for i, matches in found.items():
        p = paragraphs[i]
        for match in matches:
            if match["rule"] in disabled:
                continue
            # The personal dictionary. LanguageTool's API has nowhere to send
            # one, so it is applied to the answer. A spelling match on a word
            # the user has added is dropped, ignoring case. Only spelling,
            # because a grammar match that happens to cover that word is about
            # something else.
            if (match["type"] == "spelling" and words
                    and _slice(p.source, match["offset"], match["length"]).casefold() in words):
                continue
            out.append({**match, "offset": p.start + match["offset"]})
    out.sort(key=lambda m: (m["offset"], m["length"]))
    return out


def _ranges(paragraphs: list[_Paragraph], indices: list[int]) -> list[list[int]]:
    """The pending paragraphs as [start, end) ranges, neighbours joined."""
    out: list[list[int]] = []
    last = -2
    for i in indices:
        p = paragraphs[i]
        if out and i == last + 1:
            out[-1][1] = p.end
        else:
            out.append([p.start, p.end])
        last = i
    return out


def languages() -> list[dict[str, str]]:
    """What the checker can check, as [{code, name}] sorted by name.

    Only codes the settings validator accepts are listed, so the settings page
    never offers a language that saving would then refuse. Java LanguageTool
    lists Simple German twice, once as de-DE-x-simple-language-DE, which has one
    subtag more than a language code here may have. The other entry stays.
    """
    base = base_url()
    _guard(base)
    now = time.monotonic()
    hit = _languages_cache.get(base)
    if hit is not None and hit[0] > now:
        return [dict(row) for row in hit[1]]

    body = _request("GET", f"{base}/v2/languages")
    if not isinstance(body, list):
        log.warning("grammar: the checker's language list is not a list")
        raise BadAnswer(BAD_ANSWER)
    seen: dict[str, dict[str, str]] = {}
    for row in body:
        if not isinstance(row, dict):
            continue
        code = row.get("longCode") or row.get("code")
        name = row.get("name")
        if not isinstance(code, str) or not isinstance(name, str):
            continue
        try:
            code = validate_language(code, allow_auto=False)
        except InvalidSetting:
            continue
        seen.setdefault(code, {"code": code, "name": name.strip() or code})
    out = sorted(seen.values(), key=lambda row: (row["name"].casefold(), row["code"]))
    for row in out:
        _language_names.setdefault(row["code"], row["name"])
    _languages_cache[base] = (now + LANGUAGES_TTL, out)
    return [dict(row) for row in out]
