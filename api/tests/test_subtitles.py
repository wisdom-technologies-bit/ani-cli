"""Subtitle track tests: every track kept, labelled from provider metadata, playable.

Fixtures are real subtitle arrays captured from the live provider on 2026-11-13
(5 tracks on one episode, 9 on another), so the label shapes under test - including
the duplicated-language shapes - are the ones the provider actually emits.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ani_api.config import settings
from ani_api.main import app
from ani_api.models import SubtitleTrack
from ani_api.services import security
from ani_api.services.subtitles import (SubtitleConversionError, detect_unknown_formats,
                                        enrich_tracks, format_from_url, non_subtitle_url,
                                        parse_label, sniff_format, to_vtt)

FIX = Path(__file__).parent / "fixtures"


def provider_tracks(name: str) -> list[SubtitleTrack]:
    raw = json.loads((FIX / name).read_text())
    return [SubtitleTrack(url=item["src"], label=item["label"],
                          provider_lang=item["lang"], default=item["default"])
            for item in raw]


# ---------------------------------------------------------------- labels

@pytest.mark.parametrize("label,code,name,descriptor", [
    ("English", "en", "English", None),
    ("Japanese", "ja", "Japanese", None),
    ("English 2", "en", "English", "2"),
    ("English (- (Crunchyroll))", "en", "English", "Crunchyroll"),
    ("Portuguese (- Portuguese(Brazil))", "pt", "Portuguese", "Brazil"),
    ("Chinese - Chinese (Simplified)", "zh", "Chinese", "Simplified"),
    ("Spanish (- Spanish(Latin America))", "es", "Spanish", "Latin America"),
    ("Norwegian (Bokmål)", "no", "Norwegian", "Bokmål"),
    ("Thai", "th", "Thai", None),
    ("Arabic", "ar", "Arabic", None),
    ("Signs & Songs", None, "Unknown Language", "Signs & Songs"),
    ("", None, "Unknown Language", None),
])
def test_label_parsing_uses_provider_text(label, code, name, descriptor):
    assert parse_label(label) == (code, name, descriptor)


def test_unknown_label_is_not_guessed_as_english():
    code, name, descriptor = parse_label("Forced")
    assert code is None and name == "Unknown Language" and descriptor == "Forced"


def test_every_captured_track_is_labelled_and_distinct():
    tracks = enrich_tracks(provider_tracks("subtitles_payload_solo_s2e2_dub.json"))
    titles = [track.title for track in tracks]
    assert titles == ["Arabic", "English", "French", "German", "Italian",
                      "Portuguese", "Russian", "Spanish", "Spanish (2)"]
    assert len(set(title.lower() for title in titles)) == len(titles)
    # provider lang is preserved verbatim, but never used as the track language
    assert {track.provider_lang for track in tracks} == {"en"}
    assert tracks[0].language == "ar"


def test_duplicate_language_tracks_are_both_kept_and_distinguished():
    tracks = enrich_tracks(provider_tracks("subtitles_payload_aot_e1_sub.json"))
    assert len(tracks) == 5
    english = [track.title for track in tracks if track.language == "en"]
    assert english == ["English", "English (2)", "English (Crunchyroll)"]


# ---------------------------------------------------------------- formats

def test_formats_are_read_from_the_url_not_assumed():
    assert format_from_url("https://h/subs/a.vtt") == "vtt"
    assert format_from_url("https://h/subs/a.srt") == "srt"
    assert format_from_url("https://h/subs/a.ass") == "ass"
    assert format_from_url("https://h/subs/a.ssa") == "ass"
    assert format_from_url("https://h/subs/a") is None
    assert format_from_url("https://h/subs/a?x=1.vtt") is None


def test_non_subtitle_urls_are_refused():
    assert non_subtitle_url("https://h/preview/thumbnails.jpg") is True
    assert non_subtitle_url("https://h/subs/a.json") is True
    assert non_subtitle_url("https://h/subs/a.vtt") is False


def test_unknown_extension_is_not_advertised_playable_until_sniffed():
    tracks = enrich_tracks([SubtitleTrack(url="https://h/subs/track", label="English")])
    assert tracks[0].format == "unknown"
    assert tracks[0].playable is False


async def test_unknown_extension_sniffed_from_content():
    tracks = enrich_tracks([SubtitleTrack(url="https://h/subs/track", label="English")])

    async def fetch(_url):
        return "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n"

    await detect_unknown_formats(tracks, fetch)
    assert tracks[0].format == "vtt"
    assert tracks[0].format_source == "sniffed"
    assert tracks[0].playable is True


def test_sniff_format():
    assert sniff_format("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n") == "vtt"
    assert sniff_format("1\n00:00:01,000 --> 00:00:02,000\nhi\n") == "srt"
    assert sniff_format("[Script Info]\nTitle: x\n\n[Events]\n"
                        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,hi\n") == "ass"


# ---------------------------------------------------------------- conversion

SRT = ("1\n00:00:01,000 --> 00:00:02,500\nHello\n\n"
       "2\n00:00:03,000 --> 00:00:04,000\n<i>World</i>\n")

ASS = """[Script Info]
Title: test
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname
Style: Default,Arial

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Comment: 0,0:00:00.00,0:00:00.50,Default,,0,0,0,,a comment
Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,{\\an8}Hello\\Nthere
Dialogue: 0,0:00:02.60,0:00:03.00,Default,,0,0,0,,{\\p1}m 0 0 l 100 0 100 100
Dialogue: 0,0:00:03.10,0:00:04.20,Default,,0,0,0,,World
"""


def test_srt_is_served_as_webvtt():
    source, body = to_vtt(SRT)
    assert source == "srt"
    assert body.startswith("WEBVTT\n")
    assert "00:00:01.000 --> 00:00:02.500" in body
    assert "00:00:01,000" not in body
    assert "\n1\n" not in body


def test_ass_is_served_as_webvtt():
    source, body = to_vtt(ASS)
    assert source == "ass"
    assert body.startswith("WEBVTT\n")
    assert "00:00:01.000 --> 00:00:02.500" in body
    assert "Hello\nthere" in body            # \N is a line break, tags stripped
    assert "a comment" not in body           # Comment: lines are not cues
    assert "m 0 0 l 100 0" not in body       # drawing commands are not text
    assert "World" in body


def test_vtt_passes_through_unchanged():
    text = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n"
    source, body = to_vtt(text)
    assert source == "vtt" and body == text


def test_empty_and_unsupported_bodies_are_errors():
    with pytest.raises(SubtitleConversionError):
        to_vtt("   ")
    with pytest.raises(SubtitleConversionError):
        to_vtt("\x00\x01binary noise")


# ---------------------------------------------------------------- adapter mapping

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1920x1080
/media/1080.m3u8
"""


def _embed_page(payload: dict) -> str:
    """Build the real embed page shape: base64(json XOR "otaku-embed-v1")."""
    import base64

    from ani_api.adapters.zokoanime import KEY
    raw = json.dumps(payload).encode()
    blob = base64.b64encode(bytes(b ^ KEY[i % len(KEY)] for i, b in enumerate(raw))).decode()
    return f'<html><script>window.__P="{blob}";</script></html>'


async def test_adapter_returns_every_track_and_skips_unusable_ones(monkeypatch):
    """Drives the real ZokoAnime adapter over a faked embed + master playlist."""
    import httpx

    payload = {
        "src": "https://hls.example/master.m3u8",
        "sprite_vtt": "https://hls.example/sprite.vtt",
        "subtitles": [
            {"lang": "en", "label": "English", "default": True, "src": "https://h/a.vtt"},
            {"lang": "en", "label": "Japanese", "default": False, "src": "https://h/b.vtt"},
            {"lang": "en", "label": "Broken", "default": False, "src": ""},
            {"lang": "en", "label": "Sprite", "default": False,
             "src": "https://hls.example/sprite.vtt"},
            {"lang": "en", "label": "Thumb", "default": False, "src": "https://h/x.jpg"},
            {"label": "No lang field", "default": False, "src": "https://h/c.vtt"},
            "not-an-object",
        ],
    }

    class Response:
        def __init__(self, text, status_code=200):
            self.text = text
            self.status_code = status_code

    async def get(self, url, headers=None, timeout=None):
        if url.endswith("master.m3u8"):
            return Response(MASTER)
        return Response(_embed_page(payload))

    monkeypatch.setattr(httpx.AsyncClient, "get", get)

    from ani_api.adapters.zokoanime import ZokoAnimeAdapter
    adapter = ZokoAnimeAdapter()
    streams = await adapter.resolve("https://zokoanime.video/stream/mal/16498/1/sub",
                                    "https://zokoanime.video/")
    await adapter.aclose()

    assert [track.url for track in streams.subtitles] == [
        "https://h/a.vtt", "https://h/b.vtt", "https://h/c.vtt"]
    enriched = enrich_tracks(streams.subtitles)
    assert [track.title for track in enriched] == [
        "English", "Japanese", "No lang field"]
    assert [track.language for track in enriched] == ["en", "ja", "und"]
    assert enriched[0].default is True and enriched[1].default is False
    assert enriched[2].provider_lang is None
    assert streams.mal_id == "16498"


def test_zero_subtitle_tracks_is_valid():
    assert enrich_tracks([]) == []


# ---------------------------------------------------------------- proxy route

@pytest.fixture()
def client(monkeypatch):
    original_secret = settings.token_secret
    original_keys = set(settings.api_keys)
    settings.token_secret = "test-secret"
    settings.api_keys = {"key-alpha"}
    with TestClient(app) as test_client:
        yield test_client
    settings.token_secret = original_secret
    settings.api_keys = original_keys


def token_for(url: str, ttl: int | None = 900) -> str:
    return security.sign_payload({"u": url, "r": "https://zokoanime.video/", "k": "sub"},
                                 ttl=ttl)


def fake_upstream(monkeypatch, *, status=200, text=""):
    class Response:
        status_code = status

    response = Response()
    response.text = text

    async def get(self, url, headers=None, timeout=None):
        return response

    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    return response


def test_proxy_serves_vtt(client, monkeypatch):
    fake_upstream(monkeypatch, text="WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n")
    url = f"/v1/subtitles/{token_for('https://dramahot.top/a.vtt')}.vtt"
    r = client.get(url, headers={"x-api-key": "key-alpha"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/vtt")
    assert r.headers["x-subtitle-source-format"] == "vtt"
    assert "-->" in r.text


def test_proxy_converts_srt(client, monkeypatch):
    fake_upstream(monkeypatch, text=SRT)
    url = f"/v1/subtitles/{token_for('https://dramahot.top/a.srt')}.vtt"
    r = client.get(url, headers={"x-api-key": "key-alpha"})
    assert r.status_code == 200
    assert r.headers["x-subtitle-source-format"] == "srt"
    assert r.text.startswith("WEBVTT")


def test_proxy_converts_ass(client, monkeypatch):
    fake_upstream(monkeypatch, text=ASS)
    url = f"/v1/subtitles/{token_for('https://dramahot.top/a.ass')}.vtt"
    r = client.get(url, headers={"x-api-key": "key-alpha"})
    assert r.status_code == 200
    assert r.headers["x-subtitle-source-format"] == "ass"
    assert "Hello" in r.text


def test_proxy_reports_upstream_failure_for_one_track_only(client, monkeypatch):
    fake_upstream(monkeypatch, status=404)
    url = f"/v1/subtitles/{token_for('https://dramahot.top/gone.vtt')}.vtt"
    r = client.get(url, headers={"x-api-key": "key-alpha"})
    assert r.status_code == 502
    assert r.json()["code"] == "SUBTITLE_FETCH_FAILED"


def test_proxy_reports_empty_track(client, monkeypatch):
    fake_upstream(monkeypatch, text="   \n")
    url = f"/v1/subtitles/{token_for('https://dramahot.top/empty.vtt')}.vtt"
    r = client.get(url, headers={"x-api-key": "key-alpha"})
    assert r.status_code == 502
    assert r.json()["code"] == "SUBTITLE_EMPTY"


def test_proxy_refuses_unplayable_format(client, monkeypatch):
    fake_upstream(monkeypatch, text="\x89PNG\r\n\x1a\n not a subtitle")
    url = f"/v1/subtitles/{token_for('https://dramahot.top/a.vtt')}.vtt"
    r = client.get(url, headers={"x-api-key": "key-alpha"})
    assert r.status_code == 415
    assert r.json()["code"] == "SUBTITLE_FORMAT_UNSUPPORTED"


def test_proxy_rejects_expired_token(client):
    token = token_for("https://dramahot.top/a.vtt", ttl=-10)
    r = client.get(f"/v1/subtitles/{token}.vtt", headers={"x-api-key": "key-alpha"})
    assert r.status_code == 410
    assert r.json()["code"] == "TOKEN_EXPIRED"


def test_proxy_rejects_tampered_token(client):
    token = token_for("https://dramahot.top/a.vtt").split(".")
    token[1] = "x" + token[1][1:]
    r = client.get(f"/v1/subtitles/{'.'.join(token)}.vtt", headers={"x-api-key": "key-alpha"})
    assert r.status_code == 403


def test_proxy_rejects_unlisted_host(client):
    token = token_for("https://evil.example/a.vtt")
    r = client.get(f"/v1/subtitles/{token}.vtt", headers={"x-api-key": "key-alpha"})
    assert r.status_code == 400
    assert r.json()["code"] == "HOST_NOT_ALLOWED"


def test_proxy_requires_api_key(client, monkeypatch):
    fake_upstream(monkeypatch, text="WEBVTT\n")
    url = f"/v1/subtitles/{token_for('https://dramahot.top/a.vtt')}.vtt"
    assert client.get(url).status_code == 401
