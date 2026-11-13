"""MAL mapping unit tests: no network, no provider.

Everything here runs against a fake metadata source, a fake title source and an
in-memory SQLite store. These tests prove the *decision logic* (what counts as a
verified mapping, what is refused as ambiguous, what is reused) - never that a real
stream plays. Real delivery is covered by tests/test_mal_live.py under -m network.
"""
from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from ani_api.cache import cache
from ani_api.config import settings
from ani_api.errors import AniError
from ani_api.mapping_store import MappingStore
from ani_api.models import (EpisodeRef, MalAnimeDetail, MalEpisodeRef, MalMapping, MalPart,
                            ServerRef)
from ani_api.services.mal import _norm, _tokens, decode_hash, parse_embed_mal
from ani_api.adapters.anilist import MalTitleRecord, _episode_label, _episode_number


def embed(mal_id: int, episode: int, audio: str = "sub") -> str:
    return f"https://zokoanime.video/stream/mal/{mal_id}/{episode}/{audio}"


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


# ---------------------------------------------------------------- fakes

class FakeMetadata:
    """HiAnime stand-in. Counts calls so reuse is provable, not assumed."""

    name = "hianime"

    def __init__(self, catalogue: dict[str, dict], search_hits: dict[str, list[str]] | None = None):
        self.catalogue = catalogue          # slug -> {"episodes": [...], "mal": int, "start": int}
        self.search_hits = search_hits or {}  # query -> slugs
        self.calls = {"search": 0, "episodes": 0, "server_hash": 0}

    async def search(self, query: str):
        from ani_api.models import AnimeRef

        self.calls["search"] += 1
        slugs = self.search_hits.get(query)
        if slugs is None:
            # default: every known slug whose title shares a word with the query
            words = set(_norm(query).split())
            slugs = [slug for slug, data in self.catalogue.items()
                     if words & set(_norm(data["title"]).split())]
        if not slugs:
            from ani_api.errors import no_results

            raise no_results(query)
        return [AnimeRef(id=slug, title=self.catalogue[slug]["title"]) for slug in slugs]

    async def episodes(self, anime_id: str):
        self.calls["episodes"] += 1
        data = self.catalogue.get(anime_id)
        if not data:
            raise AniError(404, "NO_RESULTS", "no such anime")
        return [EpisodeRef(id=f"{anime_id}:{n}", number=str(n)) for n in data["episodes"]]

    async def servers(self, episode_id: str, audio: str):
        return [ServerRef(name="ZokoAnime", audio=audio, supported=True)]

    async def server_hash(self, episode_id: str, audio: str, server: str) -> str:
        self.calls["server_hash"] += 1
        slug, number = episode_id.rsplit(":", 1)
        data = self.catalogue[slug]
        mal_episode = data["start"] + (int(number) - 1)
        return b64(embed(data["mal"], mal_episode, audio))


class FakeTitles:
    name = "anilist"

    def __init__(self, records: dict[int, MalTitleRecord] | None = None, *, disabled=False,
                 raises: Exception | None = None):
        self.records = records or {}
        self.disabled = disabled
        self.raises = raises
        self.calls = 0

    async def lookup(self, mal_id: int):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.records.get(mal_id)

    async def aclose(self):
        return None


@pytest.fixture(autouse=True)
def clean_cache():
    cache._data.clear()
    yield
    cache._data.clear()


@pytest.fixture()
def store():
    return MappingStore(":memory:")


def one_piece() -> MalTitleRecord:
    return MalTitleRecord(mal_id=21, romaji="ONE PIECE", english="One Piece",
                          native="ONE PIECE", synonyms=["ワンピース"], format="TV",
                          episodes=None, season_year=1999)


def resolver(metadata, titles, store):
    from ani_api.services.mal import MalResolver

    return MalResolver(metadata, titles=titles, store=store)


# ---------------------------------------------------------------- pure helpers

def test_parse_embed_mal_reads_provider_path():
    assert parse_embed_mal(embed(21, 1000)) == (21, 1000, "sub")
    assert parse_embed_mal(embed(52299, 7, "dub")) == (52299, 7, "dub")
    assert parse_embed_mal("https://megaplay.buzz/stream/s-2/114721/sub?s=tcdn") is None
    assert parse_embed_mal("") is None


def test_decode_hash_is_base64():
    assert decode_hash(b64(embed(21, 1))) == embed(21, 1)


def test_title_normalisation_and_tokens():
    assert _norm("ONE PIECE") == "one piece"
    assert _norm("Shingeki no Kyojin  ") == "shingeki no kyojin"
    assert _tokens("One Piece") == {"one", "piece"}
    assert "op" not in _tokens("OP")          # 2-letter synonyms must not drive candidates


def test_anilist_episode_title_helpers():
    assert _episode_number("Episode 12 - The Blade") == 12
    assert _episode_label("Episode 12 - The Blade") == "The Blade"
    assert _episode_label("Episode 12") is None


# ---------------------------------------------------------------- mapping

async def test_mapping_is_proved_by_embed_not_by_title(store):
    metadata = FakeMetadata(
        {"one-piece-1": {"title": "One Piece", "episodes": list(range(1, 6)), "mal": 21, "start": 1},
         "one-piece-film-red-2825": {"title": "One Piece Film: Red", "episodes": [1],
                                     "mal": 50410, "start": 1}},
        search_hits={"ONE PIECE": ["one-piece-film-red-2825", "one-piece-1"]})
    res = resolver(metadata, FakeTitles({21: one_piece()}), store)

    mapping = await res.mapping(21)
    assert mapping.hianime_id == "one-piece-1"
    assert mapping.verified is True
    assert mapping.verified_by == "zokoanime_embed"
    assert mapping.parts[0].mal_episode_start == 1
    assert mapping.parts[0].mal_episode_end == 5
    # the movie scored well on title and was rejected on evidence only
    assert "one-piece-film-red-2825" not in [p.hianime_id for p in mapping.parts]


async def test_mapping_is_reused_without_touching_upstream(store):
    metadata = FakeMetadata(
        {"solo-leveling-235": {"title": "Solo Leveling", "episodes": [1, 2, 3],
                               "mal": 52299, "start": 1}})
    titles = FakeTitles({52299: MalTitleRecord(mal_id=52299, romaji="Ore dake Level Up na Ken",
                                               english="Solo Leveling", format="TV")})
    res = resolver(metadata, titles, store)
    await res.mapping(52299)
    first = dict(metadata.calls)
    titles_after = titles.calls

    await res.mapping(52299)                        # served from cache
    cache._data.clear()
    await res.mapping(52299)                        # served from the durable store

    assert metadata.calls == first, "a stored mapping must not re-search upstream"
    assert titles.calls == titles_after, "a stored mapping must not re-query titles"


async def test_ambiguous_mapping_is_refused(store):
    # two different HiAnime entries both prove MAL 21 episode 1 with different episode ids
    catalogue = {
        "one-piece-1": {"title": "One Piece", "episodes": [1, 2], "mal": 21, "start": 1},
        "one-piece-dup": {"title": "One Piece (Duplicate)", "episodes": [1, 2], "mal": 21, "start": 1},
    }
    metadata = FakeMetadata(catalogue, search_hits={"ONE PIECE": list(catalogue)})
    res = resolver(metadata, FakeTitles({21: one_piece()}), store)
    with pytest.raises(AniError) as exc:
        await res.mapping(21)
    assert exc.value.detail["code"] == "MAL_MAPPING_AMBIGUOUS"
    assert "one-piece-1" in exc.value.detail["detail"]


async def test_identical_duplicate_keeps_longer_run(store):
    catalogue = {
        "one-piece-1": {"title": "One Piece", "episodes": [1, 2, 3], "mal": 21, "start": 1},
    }
    metadata = FakeMetadata(catalogue, search_hits={"ONE PIECE": ["one-piece-1"]})
    res = resolver(metadata, FakeTitles({21: one_piece()}), store)
    mapping = await res.mapping(21)
    assert [p.hianime_id for p in mapping.parts] == ["one-piece-1"]
    assert mapping.episode_count == 3


async def test_unmapped_mal_id_is_reported_not_guessed(store):
    catalogue = {"some-other-show-9": {"title": "Some Other Show", "episodes": [1, 2],
                                       "mal": 777, "start": 1}}
    metadata = FakeMetadata(catalogue, search_hits={"ONE PIECE": ["some-other-show-9"]})
    res = resolver(metadata, FakeTitles({21: one_piece()}), store)
    with pytest.raises(AniError) as exc:
        await res.mapping(21)
    assert exc.value.detail["code"] == "MAL_NOT_MAPPED"
    assert "some-other-show-9" in exc.value.detail["detail"]


async def test_unknown_mal_id_is_not_found(store):
    res = resolver(FakeMetadata({}), FakeTitles({}), store)
    with pytest.raises(AniError) as exc:
        await res.mapping(999999999)
    assert exc.value.detail["code"] == "MAL_NOT_FOUND"


async def test_disabled_title_source_is_an_explicit_503(store):
    metadata = FakeMetadata({})
    res = resolver(metadata, FakeTitles(disabled=True), store)
    original = settings.mal_title_source
    settings.mal_title_source = "off"
    try:
        with pytest.raises(AniError) as exc:
            await res.mapping(21)
        assert exc.value.detail["code"] == "MAL_TITLE_SOURCE_DISABLED"
    finally:
        settings.mal_title_source = original


async def test_split_cours_become_parts_with_offsets(store):
    catalogue = {
        "show-part-1": {"title": "Show", "episodes": [1, 2, 3], "mal": 100, "start": 1},
        "show-part-2": {"title": "Show Part 2", "episodes": [1, 2, 3], "mal": 100, "start": 4},
    }
    metadata = FakeMetadata(catalogue, search_hits={"Show": list(catalogue)})
    titles = FakeTitles({100: MalTitleRecord(mal_id=100, romaji="Show", format="TV")})
    res = resolver(metadata, titles, store)

    mapping = await res.mapping(100)
    spans = [(p.hianime_id, p.mal_episode_start, p.mal_episode_end) for p in mapping.parts]
    assert spans == [("show-part-1", 1, 3), ("show-part-2", 4, 6)]

    episodes = await res.episodes(100)
    numbers = [(e.hianime_id, e.number, e.mal_number) for e in episodes]
    assert ("show-part-2", "1", 4) in numbers
    assert [e.mal_number for e in episodes] == [1, 2, 3, 4, 5, 6]


# ---------------------------------------------------------------- episodes

async def test_episode_lookup_maps_mal_number_to_hianime_episode(store):
    metadata = FakeMetadata(
        {"solo-leveling-235": {"title": "Solo Leveling", "episodes": [1, 2, 3],
                               "mal": 52299, "start": 1}})
    res = resolver(metadata, FakeTitles({52299: MalTitleRecord(mal_id=52299,
                                                              romaji="Solo Leveling")}), store)
    candidates = await res.episodes_for_stream(52299, 2)
    assert [c.episode_id for c in candidates] == ["solo-leveling-235:2"]
    assert candidates[0].mal_number == 2


async def test_episode_not_found_is_reported(store):
    metadata = FakeMetadata(
        {"solo-leveling-235": {"title": "Solo Leveling", "episodes": [1, 2],
                               "mal": 52299, "start": 1}})
    res = resolver(metadata, FakeTitles({52299: MalTitleRecord(mal_id=52299,
                                                              romaji="Solo Leveling")}), store)
    with pytest.raises(AniError) as exc:
        await res.episodes_for_stream(52299, 99)
    assert exc.value.detail["code"] == "MAL_EPISODE_NOT_FOUND"
    assert "MAL 1-2" in exc.value.detail["detail"]


async def test_verified_episode_is_recorded_and_preferred(store):
    metadata = FakeMetadata(
        {"solo-leveling-235": {"title": "Solo Leveling", "episodes": [1, 2],
                               "mal": 52299, "start": 1}})
    res = resolver(metadata, FakeTitles({52299: MalTitleRecord(mal_id=52299,
                                                              romaji="Solo Leveling")}), store)
    await res.mapping(52299)
    await res.record_episode(52299, 2, "solo-leveling-235", "solo-leveling-235:2",
                             "embed /mal/52299/2/sub")
    stored = store.get_episode(52299, 2)
    assert stored["episode_id"] == "solo-leveling-235:2"
    episodes = await res.episodes(52299)
    assert next(e for e in episodes if e.mal_number == 2).episode_verified is True


def test_mapping_store_roundtrip_and_forget():
    store = MappingStore(":memory:")
    from ani_api.mapping_store import StoredAnime

    store.save_anime(StoredAnime(
        mal_id=21, hianime_id="one-piece-1", title="ONE PIECE", titles=["ONE PIECE"],
        format="TV", season_year=1999, episode_count=1180, verified_by="zokoanime_embed",
        verified_at="2026-11-13T00:00:00+00:00", candidate_source="anilist",
        parts=[{"hianime_id": "one-piece-1", "title": "One Piece", "episode_count": 1180,
                "mal_episode_start": 1, "mal_episode_end": 1180,
                "verified_at": "2026-11-13T00:00:00+00:00", "evidence": "embed /mal/21/1/"}]))
    record = store.get_anime(21)
    assert record.hianime_id == "one-piece-1" and record.parts[0]["mal_episode_end"] == 1180
    assert store.stats() == {"anime": 1, "episodes": 0}

    store.save_episode(21, 1, "one-piece-1", "1", "2026-11-13T00:00:00+00:00", "embed")
    assert store.get_episode(21, 1)["episode_id"] == "1"
    assert set(store.episodes_for(21)) == {1}

    store.clear(21)
    assert store.get_anime(21) is None and store.get_episode(21, 1) is None


# ---------------------------------------------------------------- HTTP surface

class FakeResolver:
    """Stands in for the real resolver so the route contract is testable offline."""

    def __init__(self, detail: MalAnimeDetail, episodes: list[MalEpisodeRef],
                 stream_episodes=None, error: AniError | None = None):
        self._detail = detail
        self._episodes = episodes
        self._stream_episodes = stream_episodes or []
        self._error = error
        self.recorded: list[tuple] = []
        self.store = MappingStore(":memory:")

    async def anime_detail(self, mal_id, refresh=False):
        if self._error:
            raise self._error
        return self._detail

    async def episodes(self, mal_id, refresh=False):
        if self._error:
            raise self._error
        return self._episodes

    async def episodes_for_stream(self, mal_id, number, refresh=False):
        if self._error:
            raise self._error
        return self._stream_episodes

    async def record_episode(self, mal_id, mal_number, hianime_id, episode_id, evidence):
        self.recorded.append((mal_id, mal_number, hianime_id, episode_id, evidence))


class FakeStreamService:
    def __init__(self, response=None, error: AniError | None = None):
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    async def resolve(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def mal_detail() -> MalAnimeDetail:
    part = MalPart(hianime_id="one-piece-1", title="One Piece", episode_count=1180,
                   mal_episode_start=1, mal_episode_end=1180, evidence="embed /mal/21/1/")
    mapping = MalMapping(mal_id=21, hianime_id="one-piece-1", title="ONE PIECE",
                         titles=["ONE PIECE"], format="TV", season_year=1999,
                         episode_count=1180, parts=[part], verified=True,
                         verified_by="zokoanime_embed", candidate_source="anilist")
    return MalAnimeDetail(mal_id=21, hianime_id="one-piece-1", title="ONE PIECE",
                          titles=["ONE PIECE"], format="TV", season_year=1999,
                          episode_count=1180, parts=[part], mapping=mapping)


def mal_episodes() -> list[MalEpisodeRef]:
    part = MalPart(hianime_id="one-piece-1", episode_count=1180, mal_episode_start=1,
                   mal_episode_end=1180)
    return [MalEpisodeRef(mal_id=21, hianime_id="one-piece-1", episode_id=str(n),
                          number=str(n), mal_number=n, part=part)
            for n in (1, 2)]


@pytest.fixture()
def api_client(monkeypatch):
    import ani_api.api.routes as routes
    from ani_api.main import app

    settings.api_keys = {"unit-key"}
    yield app, routes
    settings.api_keys = set()


def test_mal_anime_endpoint(api_client):
    app, routes = api_client
    monkeypatch_resolver = FakeResolver(mal_detail(), mal_episodes())
    routes._mal = monkeypatch_resolver
    with TestClient(app) as client:
        r = client.get("/v1/anime/mal/21", headers={"x-api-key": "unit-key"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mal_id"] == 21
        assert body["hianime_id"] == "one-piece-1"
        assert body["mapping"]["verified"] is True


def test_mal_episodes_endpoint(api_client):
    app, routes = api_client
    routes._mal = FakeResolver(mal_detail(), mal_episodes())
    with TestClient(app) as client:
        r = client.get("/v1/anime/mal/21/episodes", headers={"x-api-key": "unit-key"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert [e["episode_id"] for e in body] == ["1", "2"]
        assert body[0]["mal_number"] == 1
        assert body[0]["hianime_id"] == "one-piece-1"


def test_mal_endpoints_require_api_key(api_client):
    app, routes = api_client
    routes._mal = FakeResolver(mal_detail(), mal_episodes())
    with TestClient(app) as client:
        for path in ("/v1/anime/mal/21", "/v1/anime/mal/21/episodes",
                     "/v1/streams/mal/21/episode/1"):
            r = client.get(path)
            assert r.status_code == 401, (path, r.status_code)


def test_mal_error_taxonomy_reaches_the_client(api_client):
    app, routes = api_client
    from ani_api.errors import mal_not_mapped

    routes._mal = FakeResolver(mal_detail(), mal_episodes(),
                               error=mal_not_mapped("21", ["one-piece-1 (One Piece)"]))
    with TestClient(app) as client:
        r = client.get("/v1/anime/mal/21", headers={"x-api-key": "unit-key"})
        assert r.status_code == 404
        assert r.json()["code"] == "MAL_NOT_MAPPED"
        assert r.headers["content-type"].startswith("application/problem+json")


def test_mal_stream_route_wires_verification(api_client):
    app, routes = api_client
    from ani_api.models import AudioInfo, DownloadInfo, MalContext, UrlLifetime, Validation

    part = MalPart(hianime_id="one-piece-1", episode_count=1180, mal_episode_start=1,
                   mal_episode_end=1180)
    response = MalContext  # placeholder to keep import used
    detail = mal_detail()
    resolver = FakeResolver(detail, mal_episodes(), stream_episodes=[
        type("C", (), {"hianime_id": "one-piece-1", "episode_id": "2", "number": "2",
                       "mal_number": 2, "part": detail.parts[0]})()])
    routes._mal = resolver
    stream = __import__("ani_api.models", fromlist=["StreamResponse"]).StreamResponse(
        request_id="t", provider="zokoanime", metadata_source="hianime",
        anime_id="one-piece-1", episode_id="2", episode_number="2",
        audio=AudioInfo(type="sub", declared="sub"),
        quality_requested="best", quality_selected="1080p",
        master_url="https://hls.drama1.cfd/a/master.m3u8",
        validation=Validation(first_segment_checked=True, segment_http_code=206,
                              segment_bytes=1024),
        url_lifetime=UrlLifetime(), download=DownloadInfo(),
        mal=None)
    service = FakeStreamService(response=stream)
    routes._service = service

    with TestClient(app) as client:
        r = client.get("/v1/streams/mal/21/episode/2",
                       headers={"x-api-key": "unit-key"})
        assert r.status_code == 200, r.text
        call = service.calls[0]
        assert call["episode_id"] == "2"
        assert call["mal_expect"] == (21, 2), "stream must be verified against the MAL episode"
        assert r.json()["mal"] is None or r.json()["mal"]["mal_id"] == 21


def test_mal_stream_route_skips_a_mismatched_candidate(api_client):
    app, routes = api_client
    from ani_api.errors import mal_episode_mismatch
    from ani_api.models import (AudioInfo, DownloadInfo, StreamResponse, UrlLifetime,
                               Validation)

    detail = mal_detail()
    candidate = type("C", (), {"hianime_id": "one-piece-1", "episode_id": "2",
                               "number": "2", "mal_number": 2, "part": detail.parts[0]})
    routes._mal = FakeResolver(detail, mal_episodes(), stream_episodes=[candidate])
    routes._service = FakeStreamService(
        error=mal_episode_mismatch("21", "2", "/mal/21/99/"))
    with TestClient(app) as client:
        r = client.get("/v1/streams/mal/21/episode/2", headers={"x-api-key": "unit-key"})
        assert r.status_code == 409
        assert r.json()["code"] == "MAL_EPISODE_MISMATCH"
