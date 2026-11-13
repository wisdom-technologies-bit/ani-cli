"""AniList title lookup: the only thing it is used for is turning a MAL id into
candidate titles to search HiAnime with.

It is deliberately powerless as a mapping authority: nothing from this adapter is ever
returned as a mapping. Its output is fed to HiAnime search, and every candidate must
then be *proved* by decoding the provider embed (``/mal/{id}/{episode}/``) before it is
stored or served. That keeps title similarity from ever deciding what a MAL id means.

AniList is used instead of MyAnimeList because the API is public, key-free and does not
involve scraping MyAnimeList itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..config import settings
from ..errors import mal_title_source_unavailable
from ..http_client import LoopClient

_QUERY = """
query ($malId: Int) {
  Media(idMal: $malId, type: ANIME) {
    id
    idMal
    title { romaji english native }
    synonyms
    format
    episodes
    seasonYear
    streamingEpisodes { title url }
  }
}
"""


@dataclass
class MalTitleRecord:
    mal_id: int
    romaji: Optional[str] = None
    english: Optional[str] = None
    native: Optional[str] = None
    synonyms: list[str] = field(default_factory=list)
    format: Optional[str] = None
    episodes: Optional[int] = None
    season_year: Optional[int] = None
    episode_titles: dict[int, str] = field(default_factory=dict)

    @property
    def titles(self) -> list[str]:
        """Candidate search strings, most specific first."""
        out: list[str] = []
        for value in (self.romaji, self.english, self.native, *self.synonyms):
            if value and value not in out:
                out.append(value)
        return out


class AnilistTitleSource:
    name = "anilist"

    def __init__(self, base_url: str | None = None) -> None:
        self._http = LoopClient(base_url=base_url or settings.anilist_url)

    async def lookup(self, mal_id: int) -> Optional[MalTitleRecord]:
        try:
            r = await self._http.client.post("", json={"query": _QUERY,
                                                       "variables": {"malId": mal_id}})
        except Exception as exc:  # transport failure is not "no such anime"
            raise mal_title_source_unavailable(type(exc).__name__) from exc
        if r.status_code == 429:
            raise mal_title_source_unavailable("rate limited by AniList")
        if r.status_code >= 500:
            raise mal_title_source_unavailable(f"HTTP {r.status_code}")
        if r.status_code >= 400 and r.status_code != 404:
            raise mal_title_source_unavailable(f"HTTP {r.status_code}")
        try:
            body = r.json()
        except Exception as exc:
            raise mal_title_source_unavailable("non-JSON response") from exc
        media = (body.get("data") or {}).get("Media")
        if not media:
            return None
        title = media.get("title") or {}
        record = MalTitleRecord(
            mal_id=mal_id,
            romaji=title.get("romaji"),
            english=title.get("english"),
            native=title.get("native"),
            synonyms=[s for s in (media.get("synonyms") or []) if isinstance(s, str)][:8],
            format=media.get("format"),
            episodes=media.get("episodes"),
            season_year=media.get("seasonYear"),
        )
        for entry in media.get("streamingEpisodes") or []:
            number = _episode_number(entry.get("title") or "")
            label = _episode_label(entry.get("title") or "")
            if number and label:
                record.episode_titles.setdefault(number, label)
        return record

    async def aclose(self) -> None:
        await self._http.aclose()


def _episode_number(title: str) -> Optional[int]:
    import re

    m = re.search(r"(?:episode|ep\.?)\s*(\d+)", title, re.I)
    if m:
        return int(m.group(1))
    m = re.match(r"\s*(\d+)\b", title)
    return int(m.group(1)) if m else None


def _episode_label(title: str) -> Optional[str]:
    """Strip an 'Episode 3 - ' prefix so the remainder is the actual title."""
    import re

    stripped = re.sub(r"^\s*(?:episode|ep\.?)\s*\d+\s*[-:–]?\s*", "", title, flags=re.I)
    stripped = re.sub(r"^\s*\d+\s*[-:–]\s*", "", stripped).strip()
    return stripped or None
