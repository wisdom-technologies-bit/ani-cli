"""AniKoto / MegaPlay / mapper unit tests. No network: every fetch is canned."""
import os

import pytest
from fastapi.testclient import TestClient

from ani_api.adapters import anikoto, megaplay, nekostream
from ani_api.config import settings
from ani_api.main import app
from ani_api.services import anikoto_source

EPISODE_HTML = (
    '<li><a href="#" data-id="30298" data-num="1" data-slug="1" data-mal="21" '
    'data-timestamp="1778430601" data-sub="1" data-dub="1" data-ids="TOKEN-EP-1">1</a></li>'
)
SERVER_HTML = (
    '<div class="type" data-type="sub"><ul>'
    '<li data-ep-id="30298" data-sv-id="323" data-link-id="LINK-HD1">HD-1</li>'
    '<li data-ep-id="30298" data-sv-id="e54" data-link-id="LINK-VS2">Vidstream-2</li>'
    '</ul></div>'
    '<div class="type" data-type="dub"><ul>'
    '<li data-ep-id="30298" data-sv-id="323" data-link-id="LINK-HD1-DUB">HD-1</li>'
    '</ul></div>'
)


def test_pad_and_file_id():
    assert megaplay._pad("abc", 32) == b"abc" + b"\0" * 29
    assert megaplay.file_id_from_embed("https://megaplay.buzz/stream/s-2/2142/sub?s=tcdn") == "2142"


def test_master_url_passthrough():
    assert megaplay.master_url({"file": "https://x/y/master.m3u8"}) == "https://x/y/master.m3u8"
    with pytest.raises(megaplay.MegaPlayError):
        megaplay.master_url({})


def test_decrypt_enc_rejects_garbage():
    with pytest.raises(megaplay.MegaPlayError):
        megaplay.decrypt_enc("not-a-real-payload")


def test_episodes_parse(monkeypatch):
    monkeypatch.setattr(anikoto, "_ajax", lambda url, referer: {"status": 200, "result": EPISODE_HTML})
    rows = anikoto.episodes("one-piece-odmau")
    assert len(rows) == 1
    values = set(rows[0].values())          # key names are the adapter's business
    assert {"1", "TOKEN-EP-1"} <= values
    assert 21 in values and 1778430601 in values


def test_servers_parse(monkeypatch):
    def fake_ajax(url, referer):
        result = SERVER_HTML if "server" in url else EPISODE_HTML
        return {"status": 200, "result": result}
    monkeypatch.setattr(anikoto, "_ajax", fake_ajax)
    rows = anikoto.servers("one-piece-odmau", "1")
    assert [(r["name"], r["audio"]) for r in rows] == [
        ("HD-1", "sub"), ("Vidstream-2", "sub"), ("HD-1", "dub")]
    assert "LINK-HD1" in set(rows[0].values())


def test_dub_defaults_on_and_can_be_disabled(monkeypatch):
    monkeypatch.delenv("ANIKOTO_DUB_ENABLED", raising=False)
    assert anikoto_source.dub_enabled() is True
    monkeypatch.setenv("ANIKOTO_DUB_ENABLED", "0")
    assert anikoto_source.dub_enabled() is False


def test_downloads_for_mal_unmapped(monkeypatch):
    monkeypatch.setattr(anikoto_source, "_mapped", lambda mal_id: None)
    out = anikoto_source.downloads_for_mal(999999, 1)
    assert out == {"slug": None, "sources": [], "reason": "ANIKOTO_NOT_MAPPED"}


def test_downloads_for_mal_reads_mapper(monkeypatch):
    monkeypatch.setattr(anikoto_source, "_mapped", lambda mal_id: "one-piece-odmau")
    def fake_ajax(url, referer):
        return {"status": 200, "result": EPISODE_HTML}
    monkeypatch.setattr(anikoto, "_ajax", fake_ajax)
    monkeypatch.setattr(nekostream, "downloads", lambda mal, ep, ts: [
        {"provider": "Kiwi", "audio": "sub", "quality": "1080p", "url": "https://p/1"},
        {"provider": "Kiwi", "audio": "sub", "quality": "720p", "url": "https://p/2"}])
    out = anikoto_source.downloads_for_mal(21, 1)
    assert out["slug"] == "one-piece-odmau" and len(out["sources"]) == 2


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", {"pytest-key"})
    with TestClient(app) as c:
        yield c


def test_downloads_route_shape(client, monkeypatch):
    monkeypatch.setattr(anikoto_source, "downloads_for_mal", lambda mal, ep, title=None: {
        "slug": "one-piece-odmau",
        "sources": [{"provider": "Kiwi", "audio": "sub", "quality": "1080p", "url": "https://p/1"}],
        "reason": None})
    r = client.get("/v1/downloads/mal/21/episode/1", headers={"x-api-key": "pytest-key"})
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True and body["provider"] == "nekostream"
    assert body["sources"][0]["url"] == "https://p/1"


def test_downloads_route_unmapped_and_auth(client, monkeypatch):
    monkeypatch.setattr(anikoto_source, "downloads_for_mal",
                        lambda mal, ep, title=None: {"slug": None, "sources": [], "reason": "ANIKOTO_NOT_MAPPED"})
    r = client.get("/v1/downloads/mal/999999/episode/1", headers={"x-api-key": "pytest-key"})
    assert r.status_code == 404 and r.json()["code"] == "ANIKOTO_NOT_MAPPED"
    r2 = client.get("/v1/downloads/mal/21/episode/1", headers={"x-api-key": "wrong"})
    assert r2.status_code == 401


def test_pin_ok_accepts_expected_episode():
    from ani_api.adapters import anikoto as site
    assert site.pin_ok("30298", {"30298"}) is True


def test_pin_ok_rejects_foreign_episode():
    from ani_api.adapters import anikoto as site
    assert site.pin_ok("30298", {"30299"}) is False


def test_pin_ok_is_lenient_when_unknown():
    from ani_api.adapters import anikoto as site
    assert site.pin_ok("", {"30299"}) is True
    assert site.pin_ok("30298", set()) is True


def test_episode_id_reads_data_id(monkeypatch):
    from ani_api.adapters import anikoto as site
    page = '<div data-anime-id="1642"></div>'
    listing = {"result": '<a href="#" data-id="30298" data-num="1" data-mal="21">1</a>'
                        '<a href="#" data-id="30299" data-num="2" data-mal="21">2</a>'}
    monkeypatch.setattr(site, "_fetch", lambda *a, **k: page)
    monkeypatch.setattr(site, "_ajax", lambda *a, **k: listing)
    site._EP_ID_CACHE.clear()
    assert site.episode_id("unit-slug", "2") == "30299"
    site._EP_ID_CACHE.clear()
