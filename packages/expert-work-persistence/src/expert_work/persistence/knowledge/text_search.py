"""Search-term tokenization for the J.5 hybrid-retrieval keyword side.

Postgres' built-in ``tsvector`` configurations do not segment CJK text
(Chinese has no word boundaries — ``simple`` would treat a whole
sentence as one token). We tokenize app-side with jieba — CJK-aware,
and a no-op-ish pass-through on English / mixed text — then store and
query under the ``simple`` config. This keeps keyword search correct
for Chinese without a ``zhparser`` / ``pg_jieba`` Postgres extension
(and the custom database image it would require).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

import jieba


def tokenize_for_search(text: str) -> str:
    """Segment ``text`` into space-joined, lower-cased search tokens.

    The result is fed to Postgres ``to_tsvector('simple', ...)`` at
    write time and ``plainto_tsquery('simple', ...)`` at query time, so
    both sides share one segmentation.
    """
    tokens = (token.strip().lower() for token in jieba.cut(text))
    return " ".join(token for token in tokens if token)


# -- knowledge-base keyword query (B-175 wave 2) ------------------------------
#
# Knowledge-base keyword search only — long-term memory keeps querying with
# ``tokenize_for_search`` and its all-words semantics. Both sides are segmented
# by ``tokenize_for_search``; the knowledge-base write side indexes
# :func:`kb_index_text` (NFKC, dashes folded, spaced numbers also joined), and
# queries are normalised the same way.

#: Function words, interrogatives and particles dropped from knowledge-base
#: keyword queries: they appear in nearly every chunk, so matching on them
#: only adds noise. Content words (吃 / 适合 / 价格 …) are never listed here.
KB_QUERY_STOPWORDS: frozenset[str] = frozenset(
    {
        # particles
        "的",
        "了",
        "吗",
        "呢",
        "吧",
        "啊",
        "呀",
        "嘛",
        "么",
        # interrogatives
        "什么",
        "哪些",
        "哪个",
        "哪里",
        "哪",
        "多少",
        "怎么",
        "怎样",
        "如何",
        "为什么",
        "是不是",
        # politeness / filler
        "请问",
        "一下",
        "这个",
        # pronouns and demonstratives
        "我",
        "你",
        "他",
        "她",
        "它",
        "我们",
        "你们",
        "他们",
        "这",
        "那",
        "这些",
        "那些",
        # copula, conjunctions, prepositions, modals
        "是",
        "和",
        "与",
        "及",
        "或",
        "在",
        "有",
        "能",
        "会",
        "可以",
        "等",
    }
)

#: Digit-only tokens shorter than this are dropped (``1`` / ``30``: counts and
#: doses found in most chunks); ``100`` and longer stay searchable.
KB_QUERY_MIN_NUMBER_LENGTH = 3

#: At most this many phrases + terms per query — phrases first, then terms in
#: first-seen order. Bounds the statement and the rows a pasted paragraph's OR
#: query can match.
KB_QUERY_MAX_TERMS = 64

#: Dash look-alikes folded to ``-`` after NFKC (which already turns fullwidth
#: letters, digits and hyphen into ASCII): hyphen, non-breaking hyphen, figure
#: dash, en dash, em dash, horizontal bar, minus sign, small and fullwidth
#: hyphen-minus — so an en-dashed ``QXA-N7`` still reads as ``QXA-N7``.
_DASHES = str.maketrans(
    dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe63\uff0d", "-")
)

#: Characters that join the parts of an identifier.
_IDENTIFIER_JOINERS = "-_./"

#: A run of ASCII letters / digits joined by ``-`` ``_`` ``.`` ``/`` — the
#: shape of product, strain and patent numbers (``QXA-N7``, ``QR07-2``,
#: ``ZL201900001234.5``, ``CN100000001A``).
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)*")

#: A run of number groups with spaces inside or after a short letter prefix —
#: how some documents print patent and application numbers
#: (``ZL 2024 1 0048761.8``, ``ZL 2021116309138``). Matched on NFKC text.
_SPACED_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9.])(?:[A-Za-z]{1,4} *)?[0-9]+(?:\.[0-9]+)*(?: +[0-9]+(?:\.[0-9]+)*)*"
    r"(?![A-Za-z0-9])(?!\.[0-9])"
)

#: A spaced stretch reads as one identifier only from this many digits on:
#: below it are counts, dates and versions (``2024 年 1 月``, ``1.2 3.4``).
KB_SPACED_IDENTIFIER_MIN_DIGITS = 10

#: …and only with a prefix of at least two letters (``ZL`` / ``CN``) or a
#: group of at least this many digits (``0048761.8``): a list of short numbers
#: — a table row, doses, years — is not an identifier, however many digits.
KB_SPACED_IDENTIFIER_MIN_GROUP_DIGITS = 5

#: Longest stretch of groups the write side joins: a flattened table can put a
#: page count or a date beside a number, so every identifier-shaped stretch of
#: a run up to this many groups is indexed joined.
KB_SPACED_IDENTIFIER_MAX_GROUPS = 8

#: A group of a spaced run: its letter prefix, then its integer digits.
_GROUP_PARTS_RE = re.compile(r"([A-Za-z]*)([0-9]*)")

#: A number with a unit (``2g`` / ``500mg`` / ``3.5g``) is a dose, not an
#: identifier.
_NUMBER_WITH_UNIT_RE = re.compile(r"\d+(?:\.\d+)?[a-z]{1,3}", re.IGNORECASE)

#: A digits-only run counts as an identifier from this many digits on
#: (``201800005678.9`` and dates yes; ``2`` / ``30`` / ``2024`` no).
KB_IDENTIFIER_MIN_DIGITS_ONLY_LENGTH = 6

#: Share of a unit's coverage weight a chunk gets when the unit is only in
#: its document title (a body hit gets all of it). A manual's body rarely
#: names its product, so the title has to count; the same word in a body is
#: still the stronger signal.
KB_DOC_TITLE_HIT_WEIGHT = 0.5

#: Added to the score of a chunk that contains any identifier phrase of the
#: query. Without it a chunk scores below 1.1 + KB_KEYWORD_QUERY_PHRASE_BOOST
#: (coverage ≤ 1, ``ts_rank`` < 0.1); a verbatim match of a question holding an
#: identifier also holds that identifier, so identifier hits still sort first.
KB_KEYWORD_PHRASE_BOOST = 2.0

#: Added to the score of a chunk that contains the whole question verbatim
#: (:attr:`KbKeywordQuery.query_phrase`). Coverage (≤ 1) plus the ``ts_rank``
#: tie-break (< 0.1) is below 1.1, so 1.5 puts every verbatim match above every
#: chunk that only holds the words — whatever its own coverage — with margin.
#: It reorders only: the match predicate does not include the question phrase.
KB_KEYWORD_QUERY_PHRASE_BOOST = 1.5


@dataclass(frozen=True)
class KbKeywordQuery:
    """What a knowledge-base keyword search looks for.

    A chunk matches when it contains any of ``terms`` (single tokens), or any
    of ``phrases`` (one per identifier in the query: its
    :func:`tokenize_for_search` segmentation, space-joined) as adjacent
    tokens. Punctuation tokens inside a phrase take no position — Postgres'
    parser drops them on both the ``tsvector`` and the ``tsquery`` side.

    ``query_phrase`` is the whole question — segmented as the write side
    segments chunks, function words kept, punctuation dropped — for the
    verbatim bonus (:data:`KB_KEYWORD_QUERY_PHRASE_BOOST`). It never decides a
    match, and it hits only a verbatim, punctuation-insensitive occurrence
    whose segmentation agrees with the question's. ``None`` for a one-token
    question or one longer than :data:`KB_QUERY_MAX_TERMS` tokens.

    The query is NFKC-normalised and its spaced number runs are joined, as the
    write side indexes chunks (:func:`kb_index_text`); chunks ingested before
    that are indexed as written. So a unit whose raw query text normalising
    changed keeps that raw form as well (``raw_forms``: unit → its raw
    tokens, matched adjacent), and so does the question phrase
    (``raw_query_phrase``): text typed the way such a chunk writes it still
    matches. Either form hitting counts as the unit hitting, once.
    """

    terms: tuple[str, ...]
    phrases: tuple[str, ...]
    query_phrase: str | None = None
    raw_forms: tuple[tuple[str, str], ...] = ()
    raw_query_phrase: str | None = None

    def unit_raw_form(self, unit: str) -> str | None:
        """The raw form of a term or identifier phrase, where it differs."""
        return dict(self.raw_forms).get(unit)

    def question_phrases(self) -> tuple[str, ...]:
        """The question phrase and its raw form, where either exists."""
        return tuple(p for p in (self.query_phrase, self.raw_query_phrase) if p is not None)


def is_searchable_token(token: str) -> bool:
    """True when ``token`` has a letter (CJK included), a decimal digit or a
    letter number (``Ⅱ``). Postgres' text-search parser drops
    everything else — punctuation, symbols, other numbers such as ``①`` /
    ``½`` / ``²`` — and does not count it as a position.

    That holds for a database with a non-C ``LC_CTYPE`` (our images use
    ``en_US.utf8``). Under a C ctype the parser treats every non-ASCII
    character as a letter, so fullwidth punctuation (the fullwidth question
    mark, U+FF1F) would become a lexeme and take a position."""
    return any(
        char.isalpha() or char.isdecimal() or unicodedata.category(char) == "Nl" for char in token
    )


def _is_identifier(candidate: str) -> bool:
    """A product, strain or patent number: a digit plus at least two letters
    or a joiner (``CN100000001A``, ``H2O``, ``QXA-N7``, ``QR07-2``), or six or
    more digits (``201800005678.9``, dates). Not a dose (``500mg``) and not a
    short code such as a vitamin or paper size (``D3`` / ``B12`` / ``A4``)."""
    if not any(char.isdigit() for char in candidate):
        return False
    if _NUMBER_WITH_UNIT_RE.fullmatch(candidate):
        return False
    letters = sum(1 for char in candidate if char.isalpha())
    if letters == 0:
        digits = sum(1 for char in candidate if char.isdigit())
        return digits >= KB_IDENTIFIER_MIN_DIGITS_ONLY_LENGTH
    return letters >= 2 or any(char in _IDENTIFIER_JOINERS for char in candidate)


def _is_query_term(token: str) -> bool:
    if token in KB_QUERY_STOPWORDS or not is_searchable_token(token):
        return False
    return not (token.isdigit() and len(token) < KB_QUERY_MIN_NUMBER_LENGTH)


def _normalise(text: str) -> tuple[str, list[int]]:
    """NFKC plus dash folding, character by character, with the index in
    ``text`` each output character came from — so a unit found in the
    normalised text can be traced back to the raw text it was typed as."""
    chars: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(text):
        folded = unicodedata.normalize("NFKC", char).translate(_DASHES)
        chars.append(folded)
        origin.extend([index] * len(folded))
    return "".join(chars), origin


def _identifier_shaped(groups: list[str]) -> bool:
    """Whether space-separated ``groups`` read as one identifier: at least
    :data:`KB_SPACED_IDENTIFIER_MIN_DIGITS` digits, and a two-letter prefix or
    a group of :data:`KB_SPACED_IDENTIFIER_MIN_GROUP_DIGITS` or more digits."""
    if sum(char.isdigit() for group in groups for char in group) < KB_SPACED_IDENTIFIER_MIN_DIGITS:
        return False
    parts = [_GROUP_PARTS_RE.match(group) for group in groups]
    if parts[0] is not None and len(parts[0].group(1)) >= 2:
        return True
    return any(
        part is not None and len(part.group(2)) >= KB_SPACED_IDENTIFIER_MIN_GROUP_DIGITS
        for part in parts
    )


def _spaced_runs(normalised: str) -> list[re.Match[str]]:
    """The runs of ``normalised`` that have a space inside."""
    return [match for match in _SPACED_NUMBER_RE.finditer(normalised) if " " in match.group()]


def _join_spaced_numbers(normalised: str, origin: list[int]) -> tuple[str, list[int]]:
    """``normalised`` with the spaces of each identifier-shaped run (as a whole,
    at most :data:`KB_SPACED_IDENTIFIER_MAX_GROUPS` groups) removed, and the
    origin of every character kept."""
    drop: set[int] = set()
    for match in _spaced_runs(normalised):
        groups = match.group().split()
        if len(groups) <= KB_SPACED_IDENTIFIER_MAX_GROUPS and _identifier_shaped(groups):
            drop.update(i for i in range(match.start(), match.end()) if normalised[i] == " ")
    kept = [i for i in range(len(normalised)) if i not in drop]
    return "".join(normalised[i] for i in kept), [origin[i] for i in kept]


def _joined_forms(normalised: str) -> list[str]:
    """Every identifier-shaped stretch (two to
    :data:`KB_SPACED_IDENTIFIER_MAX_GROUPS` groups) of each spaced run, joined,
    in order of first appearance."""
    forms: dict[str, None] = {}
    for match in _spaced_runs(normalised):
        groups = match.group().split()
        for start in range(len(groups)):
            stop_max = min(len(groups), start + KB_SPACED_IDENTIFIER_MAX_GROUPS)
            for stop in range(start + 2, stop_max + 1):
                if _identifier_shaped(groups[start:stop]):
                    forms.setdefault("".join(groups[start:stop]))
    return list(forms)


def kb_index_text(text: str) -> str:
    """The text a knowledge-base chunk is keyword-indexed as: NFKC plus dash
    folding (as queries are normalised), then the joined form of every
    identifier-shaped stretch of a spaced number run on a line of its own —
    the text as written stays indexed, so either way of typing the number
    matches, even with a page count or a date printed beside it."""
    normalised, _ = _normalise(text)
    joined = _joined_forms(normalised)
    return "\n".join([normalised, " ".join(joined)]) if joined else normalised


def _searchable_words(text: str) -> list[str]:
    return [token for token in tokenize_for_search(text).split() if is_searchable_token(token)]


def _question_phrase(text: str) -> str | None:
    words = _searchable_words(text)
    return " ".join(words) if 2 <= len(words) <= KB_QUERY_MAX_TERMS else None


def _raw_form(unit: str, raw_text: str) -> str | None:
    """``raw_text``'s searchable tokens, space-joined, when they differ from
    the unit's own (NFKC changed the text) — else ``None``."""
    raw = " ".join(_searchable_words(raw_text))
    own = " ".join(token for token in unit.split() if is_searchable_token(token))
    return raw if raw and raw != own else None


def kb_query_identifiers(query: str) -> tuple[str, ...]:
    """The identifiers in ``query`` that :func:`build_kb_keyword_query` makes
    phrases of, as typed after NFKC, dash folding and spaced-number joining, in
    order and deduplicated — for a caller that looks for them in passages (B-206 K-7b)."""
    normalised, _ = _join_spaced_numbers(*_normalise(query))
    found = (match.group() for match in _IDENTIFIER_RE.finditer(normalised))
    return tuple(dict.fromkeys(code for code in found if _is_identifier(code)))


def build_kb_keyword_query(query: str) -> KbKeywordQuery | None:
    """Turn a natural-language question into a knowledge-base keyword query.

    The query is NFKC-normalised, dash look-alikes become ``-`` and spaced
    number runs are joined (``ZL 2024 1 0048761.8`` → ``ZL202410048761.8``);
    units keep their raw form where that changed them, see
    :class:`KbKeywordQuery`.
    Identifiers become phrases and are cut out of the text, so their
    fragments (``qxa`` / ``n7`` of ``QXA-N7``) are not searched on their own;
    the rest is segmented by :func:`tokenize_for_search`, minus
    :data:`KB_QUERY_STOPWORDS`, punctuation and one- or two-digit numbers,
    deduplicated in order. At most :data:`KB_QUERY_MAX_TERMS` phrases + terms
    are kept, phrases first. The whole question also becomes
    ``query_phrase``. Returns ``None`` when nothing searchable is left —
    callers return no hits without querying.
    """
    normalised, origin = _join_spaced_numbers(*_normalise(query))
    # Spans in ``normalised`` of each unit's first occurrence; identifiers are
    # blanked out (same length) before the rest is segmented.
    phrase_spans: dict[str, tuple[int, int]] = {}
    rest = list(normalised)
    for match in _IDENTIFIER_RE.finditer(normalised):
        if not _is_identifier(match.group()):
            continue
        rest[match.start() : match.end()] = " " * (match.end() - match.start())
        phrase_spans.setdefault(tokenize_for_search(match.group()), match.span())
    term_spans: dict[str, tuple[int, int]] = {}
    for word, start, end in jieba.tokenize("".join(rest)):
        token = word.strip().lower()
        if token not in term_spans and _is_query_term(token):
            term_spans[token] = (start, end)
    phrases, terms = list(phrase_spans), list(term_spans)
    if not terms and not phrases:
        return None
    kept_phrases = phrases[:KB_QUERY_MAX_TERMS]
    kept_terms = terms[: KB_QUERY_MAX_TERMS - len(kept_phrases)]
    spans = {**term_spans, **phrase_spans}
    raw_forms = tuple(
        (unit, raw)
        for unit in (*kept_terms, *kept_phrases)
        if (raw := _raw_form(unit, query[origin[spans[unit][0]] : origin[spans[unit][1] - 1] + 1]))
    )
    query_phrase = _question_phrase(normalised)
    raw_query_phrase = _question_phrase(query) if query_phrase is not None else None
    return KbKeywordQuery(
        terms=tuple(kept_terms),
        phrases=tuple(kept_phrases),
        query_phrase=query_phrase,
        raw_forms=raw_forms,
        raw_query_phrase=raw_query_phrase if raw_query_phrase != query_phrase else None,
    )
