"""Refuse a stream whose own package proves it is a different anime.

AniKoto's catalogue is honest - `data-mal` and `data-num` really do describe the
episode - but its *file* ids are not always. Live example: `one-piece-odmau`
episode 1 resolves to a MegaPlay package whose shipped subtitle track reads
"Toilet-bound Hanako-kun 2", so a request for One Piece episode 1 played a
different show while every id in the chain looked correct.

No catalogue check can catch that, because the catalogue is right. So the
package is asked instead: a subtitle track names the show it belongs to, and a
title card that resolves to a different anime disqualifies the file. The check
is deliberately one-sided - it only refuses when the file *positively* names
another anime, so an episode with no captions, no title card, or an
unrecognised card keeps working exactly as before.
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Iterable, Optional

from .anikoto_match import identity_score, words

_ANILIST_URL = "https://graphql.anilist.co"
_SEARCH = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
    idMal
    title { english romaji native }
    synonyms
    format
  }
}
"""

_CARD_CACHE: "dict[str, Optional[dict]]" = {}
_VERDICT_CACHE: "dict[str, str]" = {}


def _cue_lines(text: str) -> list[str]:
    """Cue text only: no timestamps, no tags, no cue numbers, no NOTE blocks."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "-->" in line:
            continue
        if line.isdigit() or line.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = " ".join(line.split())
        if line:
            out.append(line)
    return out


def title_cards(text: str, minimum: int = 3) -> list[str]:
    """Strings repeated often enough to behave like a title or section card.

    A season title card appears several times (once per episode segment) and
    never as a full dialogue sentence, so repeats plus a two-word floor isolate
    them from lines like "What?" or "Huh?" that recur in any episode.
    """
    counts: dict[str, int] = {}
    for line in _cue_lines(text):
        if len(line) < 6 or len(line) > 64:
            continue
        if len(line.split()) < 2:
            continue
        counts[line] = counts.get(line, 0) + 1
    cards = [line for line, count in counts.items() if count >= minimum]
    cards.sort(key=lambda line: counts[line], reverse=True)
    return cards[:5]


def _anilist_search(term: str, timeout: float = 8.0) -> Optional[dict]:
    """AniList search for one term, cached (including misses)."""
    key = term.lower()
    if key in _CARD_CACHE:
        return _CARD_CACHE[key]
    found: Optional[dict] = None
    body = json.dumps({"query": _SEARCH, "variables": {"search": term}}).encode()
    request = urllib.request.Request(
        _ANILIST_URL, data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/124.0.0.0 Safari/537.36")})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "ignore"))
        media = (payload.get("data") or {}).get("Media")
        if media:
            found = media
    except Exception:
        found = None
    _CARD_CACHE[key] = found
    return found


def _same_show(card: str, names: Iterable[str]) -> bool:
    """True when a card and a candidate title share their identity words.

    Marker words are dropped by `words`, so "Hanako-kun 2" and
    "Hanako-kun Season 2" still read as the same show.
    """
    card_words = words(card)
    if len(card_words) < 2:
        return False
    for name in names:
        name_words = words(name)
        if len(name_words) < 2:
            continue
        shared = card_words & name_words
        if len(shared) >= 2 and len(shared) / len(card_words) >= 0.75 \
                and len(shared) / len(name_words) >= 0.6:
            return True
    return False


def _media_titles(media: dict) -> list[str]:
    title = media.get("title") or {}
    names = [title.get("english"), title.get("romaji"), title.get("native")]
    names.extend(media.get("synonyms") or [])
    return [name for name in names if name]


def verify(texts: Iterable[str], mal_id: int, titles: Iterable[str]) -> str:
    """Classify a package as 'match', 'foreign' or 'unknown'.

    'foreign' means the package named an anime that is not the requested one -
    the only outcome that ever causes a refusal.
    """
    expected = [title for title in titles if title]
    cards: list[str] = []
    for text in texts:
        cards.extend(title_cards(text))
    if not cards:
        return "unknown"

    # A card that already matches the requested show settles it without a lookup.
    for card in cards:
        if identity_score(expected, [card]) >= 0.55:
            return "match"

    for card in cards[:3]:
        term = re.sub(r"[()\[\]{}]", " ", card)
        term = " ".join(term.split())
        media = _anilist_search(term)
        if not media:
            continue
        if not _same_show(card, _media_titles(media)):
            continue                      # the card is not that anime's title
        media_mal = media.get("idMal")
        if media_mal and int(media_mal) != int(mal_id):
            return "foreign"
        if not media_mal and int(media.get("id") or 0) != int(mal_id):
            return "match" if int(media.get("id") or 0) == int(mal_id) else "foreign"
        return "match"
    return "unknown"


def verify_stream(stream: dict, mal_id: int, titles: Iterable[str],
                  fetch_text, limit: int = 2) -> str:
    """Verify one resolved stream; short-circuits on a cached verdict per file."""
    file_id = str(stream.get("file_id") or "")
    if file_id and file_id in _VERDICT_CACHE:
        return _VERDICT_CACHE[file_id]

    texts: list[str] = []
    for track in (stream.get("subtitles") or [])[:limit]:
        url = track.get("url")
        if not url:
            continue
        try:
            texts.append(fetch_text(url))
        except Exception:
            continue

    verdict = verify(texts, mal_id, titles) if texts else "unknown"
    if file_id:
        _VERDICT_CACHE[file_id] = verdict
    return verdict
