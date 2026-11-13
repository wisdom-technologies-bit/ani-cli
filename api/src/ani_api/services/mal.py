"""MAL id -> HiAnime anime/episode resolution.

The one non-negotiable rule in this module: a mapping exists only after a provider
embed proves it. Title similarity selects *candidates*; it never selects the answer.

Verification mechanism (discovered from the live providers, not assumed): the
ZokoAnime embed URL that HiAnime hands out for an episode is

    https://zokoanime.video/stream/mal/<mal_id>/<mal_episode>/<audio>

so decoding HiAnime's base64 ``data-hash`` yields both the MAL id and the MAL episode
number the provider itself is serving. Comparing that to the request is the proof.

Resolution order (cheapest first, all proven):
  1. in-process/Redis cache        (settings.ttl_mal_mapping)
  2. durable SQLite mapping store  (survives restarts when the disk does)
  3. discovery: AniList titles -> HiAnime search -> embed verification -> store
"""
from __future__ import annotations

import asyncio
import base64
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Optional, Protocol, runtime_checkable

from .. import obs
from ..cache import cache
from ..config import settings
from ..errors import (AniError, mal_ambiguous, mal_episode_not_found, mal_not_found,
                      mal_not_mapped, mal_title_source_disabled, no_results)
from ..mapping_store import MappingStore, StoredAnime, get_store
from ..models import (AnimeRef, AudioType, EpisodeRef, MalAnimeDetail, MalEpisodeRef,
                      MalMapping, MalPart)
from ..adapters.anilist import AnilistTitleSource, MalTitleRecord

EMBED_MAL = re.compile(r"/mal/(\d+)/(\d+)(?:/|$)")
VERIFICATION_METHOD = "zokoanime_embed"

# Servers whose embed has proven to carry the /mal/<id>/<ep>/ path. The resolver still
# falls back to discovering supported servers when this one is absent.
PREFERRED_EMBED_SERVER = "ZokoAnime"


@runtime_checkable
class MalMetadataSource(Protocol):
    """What verification needs from the metadata adapter."""

    name: str

    async def search(self, query: str) -> list[AnimeRef]: ...
    async def episodes(self, anime_id: str) -> list[EpisodeRef]: ...
    async def server_hash(self, episode_id: str, audio: AudioType, server: str) -> str: ...
    async def servers(self, episode_id: str, audio: AudioType) -> list: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_embed_mal(embed_url: str) -> Optional[tuple[int, int, str]]:
    """(mal_id, mal_episode, audio) from a provider embed URL, or None."""
    m = EMBED_MAL.search(embed_url or "")
    if not m:
        return None
    audio = "dub" if embed_url.rstrip("/").endswith("/dub") else "sub"
    return int(m.group(1)), int(m.group(2)), audio


def decode_hash(hash_b64: str) -> str:
    return base64.b64decode(hash_b64 + "=" * (-len(hash_b64) % 4)).decode("utf-8", "replace")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _tokens(text: str) -> set[str]:
    return {t for t in _norm(text).split() if len(t) >= 3}


@dataclass
class MalEpisode:
    mal_id: int
    hianime_id: str
    episode_id: str
    number: str
    mal_number: Optional[int]
    part: MalPart
    title: Optional[str] = None
    title_source: Optional[str] = None
    episode_verified: bool = False


@dataclass
class _Candidate:
    hianime_id: str
    title: str
    jname: str = ""
    score: float = 0.0


@dataclass
class _Verified:
    hianime_id: str
    title: str
    mal_episode_start: int
    episode_count: int
    first_episode_id: str
    embed_url: str


class MalResolver:
    def __init__(self, metadata: MalMetadataSource,
                 titles: AnilistTitleSource | None = None,
                 store: MappingStore | None = None) -> None:
        self.metadata = metadata
        self._titles = titles
        self._store = store
        self._store_lock = asyncio.Lock()

    # ---------- plumbing ----------
    @property
    def store(self) -> MappingStore:
        return self._store or get_store()

    @property
    def titles(self) -> AnilistTitleSource | None:
        if settings.mal_title_source in {"off", "none", "disabled", ""}:
            return None
        if self._titles is None:
            self._titles = AnilistTitleSource()
        return self._titles

    async def _store_call(self, fn, *args, **kwargs):
        async with self._store_lock:               # SQLite writes: one at a time
            return await asyncio.to_thread(fn, *args, **kwargs)

    async def _episodes(self, hianime_id: str, refresh: bool = False) -> list[EpisodeRef]:
        key = f"eps:{hianime_id}"
        if not refresh:
            hit = await cache.get(key)
            if hit:
                return [EpisodeRef(**e) for e in hit]
        episodes = await self.metadata.episodes(hianime_id)
        await cache.set(key, [e.model_dump() for e in episodes], settings.ttl_episodes)
        return episodes

    async def _title_record(self, mal_id: int) -> MalTitleRecord | None:
        source = self.titles
        if source is None:
            raise mal_title_source_disabled()
        key = f"mal:title:{mal_id}"
        hit = await cache.get(key)
        if hit:
            return MalTitleRecord(**{**hit, "episode_titles": {
                int(k): v for k, v in (hit.get("episode_titles") or {}).items()}})
        record = await source.lookup(mal_id)
        if record is None:
            raise mal_not_found(str(mal_id))
        payload = {
            "mal_id": record.mal_id, "romaji": record.romaji, "english": record.english,
            "native": record.native, "synonyms": record.synonyms, "format": record.format,
            "episodes": record.episodes, "season_year": record.season_year,
            "episode_titles": {str(k): v for k, v in record.episode_titles.items()},
        }
        await cache.set(key, payload, settings.ttl_mal_mapping)
        return record

    # ---------- anime mapping ----------
    async def mapping(self, mal_id: int, refresh: bool = False) -> MalMapping:
        key = f"mal:anime:{mal_id}"
        if not refresh:
            hit = await cache.get(key)
            if hit:
                mapping = MalMapping(**hit)
                mapping.reused = True
                obs.counter("mal_mapping_total", {"result": "cache_hit"})
                return mapping
            stored = await self._store_call(self.store.get_anime, mal_id)
            if stored and stored.parts:
                mapping = self._mapping_from_stored(stored)
                await cache.set(key, mapping.model_dump(), settings.ttl_mal_mapping)
                mapping.reused = True
                obs.counter("mal_mapping_total", {"result": "store_hit"})
                return mapping

        mapping = await self._discover(mal_id)
        await self._store_call(self.store.save_anime, self._stored_from(mapping))
        await cache.set(key, mapping.model_dump(), settings.ttl_mal_mapping)
        obs.counter("mal_mapping_total", {"result": "discovered"})
        return mapping

    def _mapping_from_stored(self, stored: StoredAnime) -> MalMapping:
        parts = [MalPart(**p) for p in stored.parts]
        return MalMapping(
            mal_id=stored.mal_id, hianime_id=stored.hianime_id, title=stored.title,
            title_english=stored.title_english, title_native=stored.title_native,
            titles=stored.titles, format=stored.format, season_year=stored.season_year,
            episode_count=stored.episode_count, parts=parts, verified=True,
            verified_by=stored.verified_by, verified_at=stored.verified_at,
            candidate_source=stored.candidate_source)

    def _stored_from(self, mapping: MalMapping) -> StoredAnime:
        return StoredAnime(
            mal_id=mapping.mal_id, hianime_id=mapping.hianime_id, title=mapping.title,
            title_english=mapping.title_english, title_native=mapping.title_native,
            titles=mapping.titles, format=mapping.format, season_year=mapping.season_year,
            episode_count=mapping.episode_count, verified_by=mapping.verified_by,
            verified_at=mapping.verified_at, candidate_source=mapping.candidate_source,
            parts=[p.model_dump() for p in mapping.parts])

    async def _discover(self, mal_id: int) -> MalMapping:
        record = await self._title_record(mal_id)
        if record is None:  # defensive: _title_record raises instead
            raise mal_not_found(str(mal_id))

        candidates = await self._candidates(record)
        if not candidates:
            raise mal_not_mapped(str(mal_id), [], "HiAnime search returned no candidates")

        verified = await self._verify_all(mal_id, candidates)
        if not verified:
            raise mal_not_mapped(
                str(mal_id), [f"{c.hianime_id} ({c.title})" for c in candidates],
                "no candidate embed reported this MAL id")

        parts = self._select_parts(mal_id, verified)
        primary = min(parts, key=lambda p: (p.mal_episode_start or 10**9, -p.episode_count))
        titles = record.titles
        return MalMapping(
            mal_id=mal_id, hianime_id=primary.hianime_id,
            title=record.romaji or record.english or primary.title,
            title_english=record.english, title_native=record.native,
            titles=titles, format=record.format, season_year=record.season_year,
            episode_count=primary.episode_count, parts=parts, verified=True,
            verified_by=VERIFICATION_METHOD, verified_at=_now(),
            candidate_source=getattr(self.titles, "name", "none"))

    async def _candidates(self, record: MalTitleRecord) -> list[_Candidate]:
        target = {t for t in record.titles if t}
        target_tokens: set[str] = set()
        for t in target:
            target_tokens |= _tokens(t)

        seen: dict[str, _Candidate] = {}
        queries = [t for t in record.titles if len(t) >= 3][:6]
        for query in queries:
            try:
                results = await self.metadata.search(query)
            except AniError as exc:
                code = exc.detail.get("code") if isinstance(exc.detail, dict) else ""
                if code in {"NO_RESULTS", "UPSTREAM_BLOCKED"}:
                    if code == "UPSTREAM_BLOCKED":
                        raise
                    continue
                raise
            for ref in results:
                if ref.id not in seen:
                    seen[ref.id] = _Candidate(hianime_id=ref.id, title=ref.title)

        ranked: list[_Candidate] = []
        for cand in seen.values():
            names = [n for n in (_norm(cand.title), _norm(cand.jname)) if n]
            best = max((SequenceMatcher(None, n, _norm(t)).ratio()
                        for n in names for t in target), default=0.0)
            cand.score = round(best, 4)
            # Cheap relevance gate before the expensive proof: a candidate must share a
            # meaningful word with a title/synonym, or be a near-identical string. This
            # only orders/shortlists; the embed still decides.
            if not (_tokens(cand.title) & target_tokens) and best < 0.72:
                continue
            ranked.append(cand)
        ranked.sort(key=lambda c: c.score, reverse=True)
        if not ranked:  # never starve discovery on the gate alone
            ranked = sorted(seen.values(), key=lambda c: c.score, reverse=True)[:2]
        return ranked[: settings.mal_candidates]

    async def _verify_candidate(self, cand: _Candidate) -> Optional[_Verified]:
        try:
            episodes = await self._episodes(cand.hianime_id)
        except AniError as exc:
            code = exc.detail.get("code") if isinstance(exc.detail, dict) else ""
            if code == "UPSTREAM_BLOCKED":
                raise
            return None
        except Exception:
            return None
        if not episodes:
            return None
        first = episodes[0]
        embed_url = await self._first_embed(first.id)
        if not embed_url:
            return None
        parsed = parse_embed_mal(embed_url)
        if not parsed:
            return None
        return _Verified(hianime_id=cand.hianime_id, title=cand.title,
                         mal_episode_start=parsed[1], episode_count=len(episodes),
                         first_episode_id=first.id, embed_url=embed_url)

    async def _first_embed(self, episode_id: str) -> Optional[str]:
        """Embed URL for an episode: preferred server first, then any supported one."""
        for server in [PREFERRED_EMBED_SERVER]:
            try:
                return decode_hash(await self.metadata.server_hash(episode_id, "sub", server))
            except AniError:
                pass
        try:
            listed = await self.metadata.servers(episode_id, "sub")
        except AniError:
            return None
        for ref in listed:
            if not getattr(ref, "supported", False):
                continue
            try:
                return decode_hash(await self.metadata.server_hash(
                    episode_id, "sub", ref.name))
            except AniError:
                continue
        return None

    async def _verify_all(self, mal_id: int, candidates: list[_Candidate]) -> list[_Verified]:
        semaphore = asyncio.Semaphore(max(1, settings.mal_verify_concurrency))

        async def run(cand: _Candidate) -> Optional[_Verified]:
            async with semaphore:
                found = await self._verify_candidate(cand)
                if found is None:
                    return None
                # only a matching MAL id counts; the episode number is recorded, not assumed
                return found if found.embed_url and parse_embed_mal(found.embed_url)[0] == mal_id else None

        results = await asyncio.gather(*(run(c) for c in candidates), return_exceptions=True)
        verified: list[_Verified] = []
        for item in results:
            if isinstance(item, _Verified):
                verified.append(item)
            elif isinstance(item, AniError):
                raise item
        obs.counter("mal_verify_total", {"result": "ok" if verified else "none"})
        return verified

    def _select_parts(self, mal_id: int, verified: list[_Verified]) -> list[MalPart]:
        by_start: dict[int, list[_Verified]] = {}
        for item in verified:
            by_start.setdefault(item.mal_episode_start, []).append(item)

        parts: list[MalPart] = []
        for start, group in sorted(by_start.items()):
            if len(group) > 1:
                first_ids = {g.first_episode_id for g in group}
                if len(first_ids) > 1:
                    raise mal_ambiguous(mal_id, [
                        f"{g.hianime_id} ({g.title}, {g.episode_count} eps, from MAL {start})"
                        for g in group])
                group = sorted(group, key=lambda g: -g.episode_count)[:1]
            item = group[0]
            parts.append(MalPart(
                hianime_id=item.hianime_id, title=item.title,
                episode_count=item.episode_count,
                mal_episode_start=item.mal_episode_start,
                mal_episode_end=item.mal_episode_start + max(item.episode_count, 1) - 1,
                verified_at=_now(), evidence=f"embed /mal/{mal_id}/{item.mal_episode_start}/ on "
                                             f"{item.first_episode_id}"))
        return parts

    # ---------- episodes ----------
    async def anime_detail(self, mal_id: int, refresh: bool = False) -> MalAnimeDetail:
        mapping = await self.mapping(mal_id, refresh=refresh)
        record: MalTitleRecord | None = None
        if self.titles is not None:
            try:
                record = await self._title_record(mal_id)
            except AniError:
                record = None
        return MalAnimeDetail(
            mal_id=mal_id, hianime_id=mapping.hianime_id, title=mapping.title,
            title_english=(record.english if record else mapping.title_english),
            title_native=(record.native if record else mapping.title_native),
            titles=mapping.titles, format=mapping.format, season_year=mapping.season_year,
            episode_count=mapping.episode_count, parts=mapping.parts, mapping=mapping)

    async def episodes(self, mal_id: int, refresh: bool = False) -> list[MalEpisodeRef]:
        mapping = await self.mapping(mal_id, refresh=refresh)
        titles: dict[int, str] = {}
        if self.titles is not None:
            try:
                record = await self._title_record(mal_id)
                titles = record.episode_titles or {}
            except AniError:
                titles = {}

        known = await self._store_call(self.store.episodes_for, mal_id)
        out: list[MalEpisodeRef] = []
        for part in mapping.parts:
            episodes = await self._episodes(part.hianime_id)
            for index, episode in enumerate(episodes):
                mal_number = None
                if part.mal_episode_start is not None:
                    mal_number = part.mal_episode_start + index
                stored = known.get(mal_number) if mal_number is not None else None
                verified_here = bool(stored and stored["episode_id"] == episode.id)
                out.append(MalEpisodeRef(
                    mal_id=mal_id, hianime_id=part.hianime_id, episode_id=episode.id,
                    number=episode.number, mal_number=mal_number,
                    title=titles.get(mal_number) if mal_number is not None else None,
                    title_source="anilist_streaming_episodes" if mal_number in titles else None,
                    part=part,
                    episode_verified=verified_here or (index == 0 and part.evidence is not None)))
        out.sort(key=lambda e: (e.mal_number if e.mal_number is not None else 10**9,
                                e.hianime_id))
        return out

    async def episodes_for_stream(self, mal_id: int, number: int,
                                  refresh: bool = False) -> list[MalEpisode]:
        """Candidate HiAnime episodes for a MAL episode number, best-evidence first.

        Ordered, never silently collapsed: the caller proves each candidate against the
        provider embed and only the first *proven* one is served.
        """
        mapping = await self.mapping(mal_id, refresh=refresh)
        all_episodes = await self.episodes(mal_id, refresh=refresh)
        if not all_episodes:
            raise mal_episode_not_found(str(mal_id), str(number), "no episodes listed")

        stored = await self._store_call(self.store.get_episode, mal_id, number)
        candidates: list[MalEpisode] = []
        seen: set[str] = set()

        def push(ref: MalEpisodeRef) -> None:
            if ref.episode_id in seen:
                return
            seen.add(ref.episode_id)
            part = ref.part or MalPart(hianime_id=ref.hianime_id)
            candidates.append(MalEpisode(
                mal_id=mal_id, hianime_id=ref.hianime_id, episode_id=ref.episode_id,
                number=ref.number, mal_number=ref.mal_number, part=part, title=ref.title,
                title_source=ref.title_source,
                episode_verified=ref.episode_verified))

        if stored:
            for ref in all_episodes:
                if ref.episode_id == stored["episode_id"]:
                    push(ref)
                    break
        for ref in all_episodes:
            if ref.mal_number == number:
                push(ref)
        if not candidates:
            # offset inference can miss when HiAnime numbering starts at 0 / includes
            # specials; the HiAnime number is a second signal, still embed-verified later.
            for ref in all_episodes:
                if ref.number.strip() == str(number):
                    push(ref)
        if not candidates:
            ranges = ", ".join(f"{p.hianime_id}: MAL {p.mal_episode_start}-{p.mal_episode_end}"
                               for p in mapping.parts)
            raise mal_episode_not_found(str(mal_id), str(number), ranges or "unknown")
        return candidates

    async def record_episode(self, mal_id: int, mal_number: int, hianime_id: str,
                             episode_id: str, evidence: str) -> None:
        await self._store_call(self.store.save_episode, mal_id, mal_number, hianime_id,
                               episode_id, _now(), evidence)
        obs.counter("mal_episode_verified_total")

    async def aclose(self) -> None:
        if self._titles is not None:
            await self._titles.aclose()
