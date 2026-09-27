"""Pure, dependency-free text helpers shared by every agent.

Component contract
-------------------
Every function here is deterministic and side-effect free, so agents can be
tested without any of the pipeline around them.

- keyword_hits(text, keywords) -> List[str]
    Whole-phrase matches of `keywords` inside `text`, case/Unicode-insensitive, with an
    optional plural suffix. Boundaries are "not a letter or digit" on both sides, so
    "hernia" does NOT match "herniation", "supplement" DOES match "supplements", and
    "x-ray" / "(braces)" still match (plain `\\b` breaks on keywords that start or end
    with punctuation).
- find_mentions(text, keywords, negation_cues, window_words) -> Mentions(hits, negated)
    keyword_hits plus negation: an occurrence preceded in the same clause by a cue
    ("no", "denies", "family history of", ...) is reported as negated, not as a hit.
- expand_abbreviations(text, mapping) -> str
    Expands medical shorthand in place: "No HTN" -> "No HTN (hypertension)".
- match_names(a, b, fuzzy_threshold) -> NameMatch
    Compares two person names. Family members in India commonly share a
    surname, so sharing a surname alone is NEVER a match ("Rajesh Kumar" vs
    "Arjun Kumar" is MISMATCH). Returns EXACT / FUZZY / INITIALS / PARTIAL /
    MISMATCH / UNKNOWN plus a similarity score.
- parse_money(value) -> Optional[Decimal]
    Accepts 1500, 1500.5, "1,500", "Rs. 1,500/-", "₹1,50,000.00". Raises
    ValueError for strings that are not an amount (never silently returns 0).
- parse_date_lenient(value) -> Optional[date]
    For document-extracted dates only (01-Nov-2024, 01/11/2024, ...). Returns
    None when unparseable. API input dates are strict ISO, validated by Pydantic.
- classify_registration(reg, patterns) -> (valid: bool, pattern_name | None)
- fmt_inr(amount) -> "₹1,50,000" (Indian digit grouping), fmt_date(date) -> "30 Nov 2024"
Errors: parse_money raises ValueError on garbage input; nothing else raises.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Optional, Tuple

_WS = re.compile(r"\s+")
_DASHES = str.maketrans({"—": " ", "–": " ", "‐": "-"})


def normalize_text(text: Optional[str]) -> str:
    text = unicodedata.normalize("NFKC", text or "").translate(_DASHES).lower()
    return _WS.sub(" ", text).strip()


def _phrase_regex(keyword: str, plural: bool = True) -> re.Pattern:
    """Whole-phrase pattern. An optional plural suffix ("s"/"es") is allowed after a keyword
    ending in a letter, so "supplement" matches "supplements" while "hernia" still does not
    match "herniation"."""
    norm = normalize_text(keyword)
    suffix = r"(?:e?s)?" if plural and norm[-1:].isalpha() else ""
    return re.compile(r"(?<![a-z0-9])" + re.escape(norm) + suffix + r"(?![a-z0-9])")


def keyword_hits(text: Optional[str], keywords: Iterable[str]) -> List[str]:
    norm = normalize_text(text)
    if not norm:
        return []
    hits = []
    for kw in keywords:
        if normalize_text(kw) and _phrase_regex(kw).search(norm):
            hits.append(kw)
    return hits


@dataclass(frozen=True)
class Mentions:
    """Result of find_mentions: `hits` are affirmed mentions; `negated` maps each keyword that
    was ONLY seen in a negated context to the snippet that negated it."""

    hits: List[str]
    negated: Dict[str, str]


# Clause boundaries stop a negation cue from reaching across sentences: "No fever. Diabetes." is
# an affirmed diabetes mention. Commas do NOT stop it, so "denies fever, chest pain" negates both.
_CLAUSE_BREAK = re.compile(r"[.;:\n]|\bbut\b|\bhowever\b|\bexcept\b")


def find_mentions(text: Optional[str], keywords: Iterable[str], negation_cues: Iterable[str] = (),
                  window_words: int = 5) -> Mentions:
    """Like keyword_hits, but a match preceded (within `window_words` words, same clause) by a
    negation cue ("no", "denies", "family history of", "r/o", ...) does not count. A keyword is a
    hit if ANY of its occurrences is affirmed. Deliberately simple (NegEx-style prefix window):
    good enough to stop "No family history of diabetes" from triggering a waiting period, and
    every negated match is reported so the trace shows what was skipped and why."""
    norm = normalize_text(text)
    if not norm:
        return Mentions([], {})
    cue_patterns = [_phrase_regex(c, plural=False) for c in negation_cues if normalize_text(c)]
    hits: List[str] = []
    negated: Dict[str, str] = {}
    for kw in keywords:
        if not normalize_text(kw):
            continue
        affirmed, negated_snippet = False, None
        for match in _phrase_regex(kw).finditer(norm):
            before = norm[:match.start()]
            breaks = list(_CLAUSE_BREAK.finditer(before))
            clause = before[breaks[-1].end():] if breaks else before
            window = " ".join(clause.split()[-window_words:])
            if any(p.search(window) for p in cue_patterns):
                negated_snippet = negated_snippet or f"{window} {match.group(0)}".strip()
            else:
                affirmed = True
                break
        if affirmed:
            hits.append(kw)
        elif negated_snippet:
            negated[kw] = negated_snippet
    return Mentions(hits, negated)


def expand_abbreviations(text: str, mapping: Dict[str, str]) -> str:
    """Expands medical shorthand IN PLACE ("No HTN" -> "No HTN (hypertension)"), so the expansion
    keeps the surrounding context. Appending expansions at the end of the text (the earlier
    approach) lost any negation in front of the abbreviation."""
    out = text or ""
    for abbr, full in mapping.items():
        pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(abbr) + r"(?![A-Za-z0-9])", re.IGNORECASE)
        out = pattern.sub(lambda m, full=full: f"{m.group(0)} ({full})", out)
    return out


# ---------------------------------------------------------------- names

_HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "shri", "smt", "sri", "kumari", "km", "master", "baby", "late"}


def name_tokens(name: Optional[str]) -> List[str]:
    cleaned = re.sub(r"[^a-z\s]", " ", normalize_text(name))
    return [t for t in cleaned.split() if t not in _HONORIFICS]


@dataclass(frozen=True)
class NameMatch:
    level: str  # EXACT | FUZZY | INITIALS | PARTIAL | MISMATCH | UNKNOWN
    score: float

    @property
    def is_match(self) -> bool:
        return self.level in ("EXACT", "FUZZY", "INITIALS", "PARTIAL")


def match_names(a: Optional[str], b: Optional[str], fuzzy_threshold: float = 0.85) -> NameMatch:
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return NameMatch("UNKNOWN", 0.0)
    if sorted(ta) == sorted(tb):
        return NameMatch("EXACT", 1.0)
    if len(ta) > 1 and len(tb) > 1 and ta[0] == tb[0] and ta[-1] == tb[-1]:
        return NameMatch("EXACT", 0.98)  # differs only by a middle name

    if len(ta) > 1 and len(tb) > 1:
        first = SequenceMatcher(None, ta[0], tb[0]).ratio()
        last = SequenceMatcher(None, ta[-1], tb[-1]).ratio()
        if first >= fuzzy_threshold and last >= fuzzy_threshold:
            return NameMatch("FUZZY", round((first + last) / 2, 3))  # OCR typo: "Kumaar"
        if ta[-1] == tb[-1]:
            short, long_ = (ta, tb) if len(ta[0]) <= len(tb[0]) else (tb, ta)
            if len(short[0]) == 1 and long_[0].startswith(short[0]):
                return NameMatch("INITIALS", 0.8)  # "R. Kumar" vs "Rajesh Kumar"
        return NameMatch("MISMATCH", 0.0)  # includes shared-surname-only cases

    single, multi = (ta, tb) if len(ta) == 1 else (tb, ta)
    if single[0] == multi[0]:
        return NameMatch("PARTIAL", 0.7)  # first name only: "Priya" vs "Priya Singh"
    return NameMatch("MISMATCH", 0.0)


# ---------------------------------------------------------------- money / dates

_MONEY_NOISE = re.compile(r"(?i)(rs\.?|inr|₹|/-|\s)")


def parse_money(value) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean is not a monetary amount")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    text = _MONEY_NOISE.sub("", str(value)).replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Not a monetary amount: {value!r}") from exc


_DATE_FORMATS = (
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%b-%Y", "%d %b %Y",
    "%d-%B-%Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d/%m/%y", "%d-%m-%y",
)


def parse_date_lenient(value) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def classify_registration(registration: Optional[str], patterns: Dict[str, str]) -> Tuple[bool, Optional[str]]:
    if not registration:
        return False, None
    reg = registration.strip().upper().replace(" ", "")
    for name, pattern in patterns.items():
        if re.fullmatch(pattern, reg):
            return True, name
    return False, None


CENTS = Decimal("0.01")


def q2(amount: Decimal) -> Decimal:
    return Decimal(amount).quantize(CENTS, rounding=ROUND_HALF_UP)


def fmt_inr(amount) -> str:
    value = q2(Decimal(str(amount)))
    sign = "-" if value < 0 else ""
    whole, _, frac = f"{abs(value):.2f}".partition(".")
    if len(whole) > 3:
        head = re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", whole[:-3])
        whole = f"{head},{whole[-3:]}"
    return f"{sign}₹{whole}" + ("" if frac == "00" else f".{frac}")


def fmt_date(value: date) -> str:
    return value.strftime("%d %b %Y")


def humanize(code: str) -> str:
    return code.replace("_", " ").lower()
