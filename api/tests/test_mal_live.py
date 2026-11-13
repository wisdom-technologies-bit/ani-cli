"""Live MAL tests: real HiAnime, real ZokoAnime, real segments.

Run with: pytest -m network tests/test_mal_live.py

Nothing here is mocked. A stream only counts as working when the first media segment
returns bytes, exactly like tests/test_live.py. These tests need outbound internet and
are excluded from the offline suite.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ani_api.cache import cache
from ani_api.config import settings
from ani_api.main import app
from ani_api.mapping_store import reset_store

pytestmark = pytest.mark.network

MAL_ONE_PIECE = 21
MAL_SOLO_LEVELING = 52299


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Authenticated client with an isolated mapping database and cold cache."""
    original_keys = set(settings.api_keys)
    original_db = settings.mapping_db
    settings.api_keys = {"live-key"}
    settings.mapping_db = str(tmp_path / "mal_live.sqlite3")
    reset_store(settings.mapping_db)
    cache._data.clear()
    with TestClient(app) as test_client:
        yield test_client
    cache._data.clear()
    settings.api_keys = original_keys
    settings.mapping_db = original_db
    reset_store(":memory:")


def auth() -> dict:
    return {"x-api-key": "live-key"}


def test_mal_id_21_resolves_to_hianime_anime(client):
    r = client.get(f"/v1/anime/mal/{MAL_ONE_PIECE}", headers=auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mal_id"] == MAL_ONE_PIECE
    assert body["hianime_id"] == "one-piece-1"
    assert body["mapping"]["verified"] is True
    assert body["mapping"]["verified_by"] == "zokoanime_embed"
    assert body["episode_count"] > 1000
    assert body["parts"] and body["parts"][0]["mal_episode_start"] == 1


def test_mal_id_52299_maps_to_solo_leveling(client):
    r = client.get(f"/v1/anime/mal/{MAL_SOLO_LEVELING}", headers=auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hianime_id"] == "solo-leveling-235"
    assert body["episode_count"] >= 12
    # the neighbouring season has its own MAL id and must not be selected
    assert "season-2" not in body["hianime_id"]


def test_mal_episodes_are_mapped_and_verified(client):
    r = client.get(f"/v1/anime/mal/{MAL_ONE_PIECE}/episodes", headers=auth())
    assert r.status_code == 200, r.text
    episodes = r.json()
    assert len(episodes) > 1000
    first = episodes[0]
    assert first["mal_number"] == 1
    assert first["hianime_id"] == "one-piece-1"
    assert first["episode_id"]
    assert first["episode_verified"] is True, "episode 1 is proved by the provider embed"
    # numbering must follow MAL, not a HiAnime offset
    assert episodes[499]["mal_number"] == 500


def test_invalid_mal_id_returns_a_clear_404(client):
    r = client.get("/v1/anime/mal/999999999", headers=auth())
    assert r.status_code == 404
    assert r.json()["code"] == "MAL_NOT_FOUND"


def test_episode_out_of_range_returns_a_clear_404(client):
    r = client.get(f"/v1/anime/mal/{MAL_SOLO_LEVELING}/episodes", headers=auth())
    assert r.status_code == 200
    count = len(r.json())
    r = client.get(f"/v1/streams/mal/{MAL_SOLO_LEVELING}/episode/{count + 50}", headers=auth())
    assert r.status_code == 404
    assert r.json()["code"] == "MAL_EPISODE_NOT_FOUND"


@pytest.mark.parametrize("audio", ["sub", "dub"])
def test_mal_stream_resolution_proves_delivery(client, audio):
    r = client.get(f"/v1/streams/mal/{MAL_ONE_PIECE}/episode/1",
                   params={"audio": audio, "quality": "best"}, headers=auth())
    if r.status_code == 422:
        # a legitimate real outcome: this episode has no supported server for the audio
        assert r.json()["code"] == "AUDIO_TYPE_UNAVAILABLE", r.text
        return
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mal"]["mal_id"] == MAL_ONE_PIECE
    assert body["mal"]["mal_episode"] == 1
    assert body["mal"]["stream_verified"] is True
    assert body["mal"]["verification"] == "provider_embed"
    assert body["validation"]["first_segment_checked"] is True
    assert body["validation"]["segment_http_code"] in (200, 206)
    assert (body["validation"]["segment_bytes"] or 0) > 0
    assert body["master_url"].endswith(".m3u8")
    assert body["proxied_master_url"].startswith("/v1/stream/")
    assert body["url_lifetime"]["expiry_known"] is False


def test_hls_proxy_and_subtitles_deliver_with_api_key(client):
    r = client.get(f"/v1/streams/mal/{MAL_ONE_PIECE}/episode/1",
                   params={"audio": "sub"}, headers=auth())
    if r.status_code != 200:
        pytest.skip(f"no stream to proxy this run: {r.status_code} {r.text[:200]}")
    body = r.json()

    master = client.get(body["proxied_master_url"], headers=auth())
    assert master.status_code == 200, master.text[:300]
    assert master.text.startswith("#EXTM3U")
    media_paths = [line for line in master.text.splitlines()
                   if line.startswith("/v1/stream/")]
    assert media_paths, "master playlist must be rewritten to this API"

    media = client.get(media_paths[0], headers=auth())
    assert media.status_code == 200, media.text[:300]
    assert "#EXTM3U" in media.text

    assert body["subtitles"], "this episode serves at least one subtitle track"
    subtitle = client.get(body["subtitles"][0]["proxied_url"], headers=auth())
    assert subtitle.status_code == 200, subtitle.text[:300]
    assert "WEBVTT" in subtitle.text[:200].upper()


def test_proxy_refuses_without_api_key(client):
    r = client.get(f"/v1/streams/mal/{MAL_ONE_PIECE}/episode/1",
                   params={"audio": "sub"}, headers=auth())
    if r.status_code != 200:
        pytest.skip("no stream to proxy this run")
    master_path = r.json()["proxied_master_url"]
    assert client.get(master_path).status_code == 401
    assert client.get(master_path, headers={"x-api-key": "wrong"}).status_code == 401


def test_existing_endpoints_still_work(client):
    """Regression: the original id-based surface is untouched by MAL support."""
    search = client.get("/v1/search", params={"q": "solo leveling"}, headers=auth())
    assert search.status_code == 200, search.text
    results = search.json()
    assert results and results[0]["id"]
    slug = results[0]["id"]

    detail = client.get(f"/v1/anime/{slug}", headers=auth())
    assert detail.status_code == 200
    assert detail.json()["detail_source"] == "episodes_api"

    episodes = client.get(f"/v1/anime/{slug}/episodes", headers=auth())
    assert episodes.status_code == 200
    episode_id = episodes.json()[0]["id"]

    servers = client.get(f"/v1/episodes/{episode_id}/servers",
                         params={"audio": "sub"}, headers=auth())
    assert servers.status_code == 200
    assert any(s["supported"] for s in servers.json())

    stream = client.get(f"/v1/streams/{episode_id}", params={"audio": "sub"}, headers=auth())
    if stream.status_code == 200:
        body = stream.json()
        assert body["validation"]["segment_http_code"] in (200, 206)
        assert body["mal"] is None, "the original endpoint must not gain MAL fields"
    else:
        assert stream.json()["code"] in {"PROVIDER_OBJECT_BLOCKED", "PROVIDER_UNAVAILABLE"}


def test_health_and_metrics_require_the_key(client):
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers=auth()).status_code == 200
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers=auth()).status_code == 200
