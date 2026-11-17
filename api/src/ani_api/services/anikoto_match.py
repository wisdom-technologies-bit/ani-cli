"""Title identity matching for AniKoto.

Ported from Anivexa-API (`core/new-provider-utils.js`, `providers/anikoto.js`),
which resolved without mismatching because it never trusted a title search hit:
it scored candidates by *title identity* (with season/part/cour/format markers
stripped and compared separately) before it ever inspected a catalog, and it
accepted a show only when an episode row proved the MAL id.

AniKoto exposes no MAL lookup, so the only hard proof that a slug is the right
anime is `data-mal` on its episode rows. That proof is reached *through* a title
search, and a search for a franchise returns its movies, recaps, compilations
and other seasons first. Ranking those hits by title identity is what stops a
request for a series from being answered by "Naruto the Movie" or
"One Piece Recap".

No third-party dependency, no I/O: pure functions over strings, so the ranking
is unit-testable without network.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

# Words that carry no identity: articles, particles and the *structural* words
# that describe an entry's place in a franchise. The structural ones are not
# dropped silently - they become markers instead (see `markers`).
_STOP_WORDS = {
    "a", "an", "and", "of", "the", "to", "in", "on", "for", "no", "wa",
    "season", "part", "stage", "cour", "chapter", "chapters", "episode",
    "episodes", "movie", "film", "tv", "ova", "ona", "special", "specials",
    "final", "finale", "arc", "the", "x",
}

_MARKER_WORDS = ("season", "part", "stage", "cour")
_ORDINAL_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
}
_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6",
          "vii": "7", "viii": "8", "ix": "9", "x": "10"}


def title_text(value: object) -> str:
    """Lowercase, accent-folded, punctuation-collapsed form of a title."""
    text = unicodedata.normalize("NFKD", str(value or "").lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).split())


def norm(value: object) -> str:
    """Identity form: lowercase, alphanumerics only, no spaces."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def words(value: object) -> set[str]:
    """Identity-bearing words of a title (stop words and ordinals removed)."""
    out: set[str] = set()
    for word in title_text(value).split():
        if len(word) <= 1 or word in _STOP_WORDS:
            continue
        if re.fullmatch(r"\d+(?:st|nd|rd|th)?", word):
            continue
        out.add(word)
    return out


def markers(value: object) -> dict[str, set[str]]:
    """Structural markers of a title: which season/part/format it claims.

    Two titles with conflicting markers are different entries even when their
    names are nearly identical ("Demon Slayer" vs "Demon Slayer Season 2").
    """
    text = title_text(value)
    squashed = text.replace(" ", "")
    found: dict[str, set[str]] = {}

    def add(marker: str, number: str) -> None:
        found.setdefault(marker, set()).add(number)

    for marker in _MARKER_WORDS:
        for match in re.finditer(rf"{marker}(\d+)", squashed):
            add(marker, match.group(1))
        for match in re.finditer(rf"(\d+)(?:st|nd|rd|th){marker}", squashed):
            add(marker, match.group(1))
        for word, number in _ORDINAL_WORDS.items():
            if f"{marker}{word}" in squashed or f"{word}{marker}" in squashed:
                add(marker, number)

    if re.search(r"final(season|chapters?|part|arc)", squashed):
        add("final", "chapters" if "finalchapter" in squashed else "season")

    if re.search(r"movie|film|gekijouban", squashed):
        add("format", "movie")
    type_marker = re.search(r"\b(ova|ona|special|specials)\b", text)
    if type_marker:
        add("format", "special" if type_marker.group(1) == "specials" else type_marker.group(1))

    spelled = text.split()
    ending = spelled[-1] if spelled else ""
    numbered = ending[:2] if re.fullmatch(r"\d{1,2}(?:st|nd|rd|th)?", ending or "") else ""
    numbered = numbered or _ROMAN.get(ending, "")
    if numbered and not any(
        marker in _MARKER_WORDS and numbered in values for marker, values in found.items()
    ):
        add("numbered", numbered)
    for match in re.finditer(r"(?:movie|film)(\d+)", squashed):
        add("numbered", match.group(1))
    return found


def _dice(left: str, right: str) -> float:
    """Sørensen-Dice coefficient over character bigrams (1.0 when equal)."""
    a, b = left.replace(" ", ""), right.replace(" ", "")
    if a == b:
        return 1.0
    if len(a) < 2 or len(b) < 2:
        return 0.0
    counts: dict[str, int] = {}
    for index in range(len(a) - 1):
        pair = a[index:index + 2]
        counts[pair] = counts.get(pair, 0) + 1
    hits = 0
    for index in range(len(b) - 1):
        pair = b[index:index + 2]
        if counts.get(pair, 0):
            counts[pair] -= 1
            hits += 1
    return (2 * hits) / (len(a) + len(b) - 2)


def identity_score(titles: Iterable[str], candidates: Iterable[str]) -> float:
    """How strongly a candidate names the same entry as the target titles.

    1.0 is a clean match; a conflicting season/format marker or a weak lexical
    overlap scores 0.0, which is what keeps a franchise's movies and recaps from
    being treated as the series.
    """
    target_titles = [title for title in titles if title]
    candidate_titles = [candidate for candidate in candidates if candidate]
    if not target_titles or not candidate_titles:
        return 0.0

    target_markers: dict[str, set[str]] = {}
    for title in target_titles[:3]:
        for marker, values in markers(title).items():
            target_markers.setdefault(marker, set()).update(values)

    best = 0.0
    for title in target_titles:
        query_words = words(title)
        if not query_words:
            continue
        for candidate in candidate_titles:
            candidate_words = words(candidate)
            if not candidate_words:
                continue

            lexical = _dice(" ".join(sorted(query_words)), " ".join(sorted(candidate_words)))
            candidate_markers = markers(candidate)

            conflict = False
            penalty = 0.0
            for marker, query_values in target_markers.items():
                candidate_values = candidate_markers.get(marker)
                if candidate_values and not (query_values & candidate_values):
                    conflict = True
                    break
                if not candidate_values and not (marker == "season" and "final" in candidate_markers):
                    penalty += 0.22 if marker in _MARKER_WORDS else 0.16
            if conflict:
                continue
            for marker in candidate_markers:
                if marker not in target_markers:
                    penalty += 0.18 if marker in _MARKER_WORDS else 0.12
            penalty = min(0.48, penalty)

            shared = len(query_words & candidate_words)
            coverage = min(shared / len(query_words), shared / len(candidate_words))
            if min(len(query_words), len(candidate_words)) < 3:
                if lexical < 0.76:
                    continue
            elif coverage < 0.5:
                continue

            best = max(best, max(0.0, lexical * 0.7 + coverage * 0.3 - penalty))
    return best


def _variants(title: str) -> list[str]:
    """Search forms of one title: whole, first few words, markers stripped."""
    out = [title]
    parts = title.split()
    if len(parts) > 4:
        out.append(" ".join(parts[:4]))
    if len(parts) > 3:
        out.append(" ".join(parts[:3]))
    stripped = re.sub(r"\b(?:season|part|cour|stage)\s*\d+\b", " ", title, flags=re.I)
    stripped = re.sub(r"\b\d+(?:st|nd|rd|th)\b", " ", stripped, flags=re.I)
    stripped = " ".join(stripped.split())
    if stripped and stripped != title:
        out.append(stripped)
    return out


def search_queries(titles: Iterable[str], limit: int = 16) -> list[str]:
    """Every useful AniKoto search phrase for a set of aliases.

    Aliases matter: a franchise is often filed under a different name than the
    one a caller passes in ("Detective Conan" is filed as "Case Closed"), and a
    single-query search then finds nothing at all.
    """
    seen: dict[str, str] = {}
    unique = []
    for title in titles:
        text = " ".join(str(title or "").split())
        if text and text.lower() not in {found.lower() for found in unique}:
            unique.append(text)

    def add(value: str) -> None:
        text = " ".join(str(value or "").split())
        if len(text) >= 3 and text.lower() not in seen:
            seen[text.lower()] = text

    for title in unique[:8]:
        add(title)
    for title in unique[:4]:
        for variant in _variants(title):
            add(variant)
        for segment in re.split(r"[:\u2013\u2014|/]", title):
            segment = segment.strip()
            if len(segment) >= 4:
                add(segment)
        broad = re.sub(
            r"\b(?:season|part|cour|stage|chapter|episode)\s*(?:\d+|one|two|three|four|five|final)?\b",
            " ", title, flags=re.I)
        broad = re.sub(r"\b\d+(?:st|nd|rd|th)\b", " ", broad, flags=re.I)
        broad = re.sub(r"\bthe\s+final\s+chapters?\b", " ", broad, flags=re.I)
        add(" ".join(broad.split()))
    for title in unique[8:]:
        add(title)
    return list(seen.values())[:limit]


def rank(titles: Iterable[str], hits: Iterable[dict[str, str]]) -> list[tuple[float, dict[str, str]]]:
    """Score search hits by title identity, best first.

    A hit's own name, its native title and its slug are all offered to the
    scorer: AniKoto's slugs carry the franchise words, which survives a rename.
    """
    target = [title for title in titles if title]
    scored: list[tuple[float, dict[str, str]]] = []
    for hit in hits:
        slug = str(hit.get("slug") or "")
        candidates = [
            str(hit.get("title") or ""),
            str(hit.get("jp") or ""),
            slug.replace("-", " "),
        ]
        scored.append((identity_score(target, candidates), hit))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored
